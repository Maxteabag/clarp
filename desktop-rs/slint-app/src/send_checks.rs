//! `--check send --out DIR`: a send as it is drawn (Peter, 2026-10-07),
//! timed as the iPhone's (clarp-ios 7bdc0d3; SendFlight, SEND_STATUS_GRACE).
//! Benefit of the doubt: the new bubble is drawn at its final size at once,
//! with no "Sending…" line while the Host takes under the grace period to
//! confirm it (the row's height never changes), the line shows once a slow
//! send passes the grace period, and a failure shows at once. The send
//! animation: the composer's text flies from the editor to the bubble's
//! final place while the composer shrinks to its empty height, the chat
//! stays at its end throughout, a reader scrolled up gets no flight,
//! Ctrl+Enter flies too, Reduce Motion fades the bubble in, and the setting
//! turns it off. The copy lands 320 ms after the bubble is laid out and is
//! gone 200 ms later; with no bubble laid out it is dropped at 600 ms; a
//! send with no text, or longer than the composer shows, fades in;
//! queued-turn labels wait the grace period too. Mid-flight frames are
//! saved, and the frames drawn in flight are timed.

use std::cell::{Cell, RefCell};
use std::time::{Duration, Instant};

use serde_json::json;
use slint::{ComponentHandle, Model};
use slint::platform::Key;

use super::{Rect, Stage, app_now, check, control, live_checks, rect, report, run_stages, shot};
use crate::headless;

/// What a send looked like, a frame at a time: the animation's own report
/// (`SendFlight`, written by the pane each frame), the row's height in the
/// scroll book and its status as the row model has it. Reading the element
/// tree every frame would cost more than the frames being timed.
#[derive(Debug, Clone, Default)]
struct Sample {
    at: Duration,
    id: String,
    phase: String,
    progress: f32,
    flight: Option<Rect>,
    row: Option<f32>,
    status: String,
    composer: f32,
    at_end: bool,
}
thread_local! {
    static SAMPLES: RefCell<Vec<Sample>> = const { RefCell::new(Vec::new()) };
    static SENT_AT: Cell<Option<Instant>> = const { Cell::new(None) };
    static SAMPLER: RefCell<Option<slint::Timer>> = const { RefCell::new(None) };
    /// Frames to save while sampling: (after, name), each once.
    static SHOTS: RefCell<Vec<(Duration, String)>> = const { RefCell::new(Vec::new()) };
    static OUT: RefCell<String> = const { RefCell::new(String::new()) };
    /// The composer's height focused and empty, and the flights started
    /// before the stage at hand.
    static EMPTY: Cell<f32> = const { Cell::new(0.0) };
    static STARTED: Cell<i32> = const { Cell::new(0) };
    static BOX_BEFORE: Cell<f32> = const { Cell::new(0.0) };
    static JUMPS: Cell<usize> = const { Cell::new(0) };
    /// Frame times (ms) before the flight, and one mid-flight frame's
    /// reported box beside the overlay as drawn.
    static BASELINE: RefCell<Vec<f64>> = const { RefCell::new(Vec::new()) };
    static MID_WANTED: Cell<bool> = const { Cell::new(false) };
    static MID: RefCell<Option<(Rect, Rect)>> = const { RefCell::new(None) };
    static LANDING: RefCell<Option<(Rect, f32, Option<Rect>)>> = const { RefCell::new(None) };
    static DROPPED: Cell<i32> = const { Cell::new(0) };
    static REVEAL: RefCell<Option<RevealWatch>> = const { RefCell::new(None) };
    /// The queue's count over time: (since, the Host's, the composer's
    /// label, the explorer's label).
    static QUEUE_SEEN: RefCell<Vec<(Duration, i32, i32, i32)>> = const { RefCell::new(Vec::new()) };
}

/// A draft taller than the composer grows (240px): it scrolls in the box.
const LONG_DRAFT: [&str; 16] = [
    "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
];

fn element(id: &str) -> Option<Rect> {
    use i_slint_backend_testing::ElementQuery;
    let window = crate::window()?;
    let id = id.to_owned();
    ElementQuery::from_root(&window).match_predicate(move |e| e.accessible_id().is_some_and(|a| a == id.as_str())).find_first().map(|e| rect(&e))
}

fn element_label(id: &str) -> Option<String> {
    use i_slint_backend_testing::ElementQuery;
    let window = crate::window()?;
    let id = id.to_owned();
    ElementQuery::from_root(&window)
        .match_predicate(move |e| e.accessible_id().is_some_and(|a| a == id.as_str()))
        .find_first()
        .and_then(|e| e.accessible_label())
        .map(|l| l.to_string())
}

/// The status line row `id` is drawn with (timestamps are off), as the
/// transcript words it from the row.
fn status_of(id: &str) -> String {
    let row = app_now().active_messages().and_then(|m| slint::Model::iter(&*m).find(|r| r.id == id));
    match row {
        Some(r) if r.failed => "Not delivered".into(),
        Some(r) if r.pending && r.status_due => "Sending…".into(),
        _ => String::new(),
    }
}

fn composer_box() -> Option<Rect> {
    element(&format!("composer-box:{}", app_now().active_id()))
}

/// The newest message of one's own in rachel's chat: id, unsent, failed.
fn last_own() -> Option<(String, bool, bool)> {
    let app = app_now();
    let engine = app.engine.borrow();
    engine.conversation("rachel")?.rows().iter().rev().find(|m| m.role == "user").map(|m| (m.id.clone(), m.pending, m.delivery_failed))
}

/// The animation the last send is in ("wait", "flight", "settle", "fade", "" once over).
fn phase() -> String {
    crate::window().map(|w| w.global::<crate::SendFlight>().get_phase().to_string()).unwrap_or_default()
}

/// Send animations started so far.
fn started() -> i32 {
    crate::window().map_or(0, |w| w.global::<crate::SendFlight>().get_started())
}

/// Jumps of over a viewport nobody asked for, so far.
fn unexplained() -> usize {
    crate::scroll_journal::logged().iter().filter(|c| *c == "unexplained").count()
}

/// Sends what the composer holds (Ctrl+Enter when `queue`) and samples
/// every frame until `stop_sampling`, saving `shots` on the way.
fn send_and_sample(queue: bool, shots: &[(u64, &str)], known: Option<String>) {
    SAMPLES.with(|s| s.borrow_mut().clear());
    SHOTS.with(|s| *s.borrow_mut() = shots.iter().map(|(ms, name)| (Duration::from_millis(*ms), (*name).to_owned())).collect());
    STARTED.with(|s| s.set(started()));
    JUMPS.with(|j| j.set(unexplained()));
    let before = known;
    let sent = Instant::now();
    SENT_AT.with(|s| s.set(Some(sent)));
    if queue {
        headless::press_with(&[Key::Control], Key::Return);
    } else {
        headless::press(Key::Return);
    }
    let timer = slint::Timer::default();
    timer.start(slint::TimerMode::Repeated, Duration::from_millis(12), move || {
        let Some(window) = crate::window() else { return };
        let flight = window.global::<crate::SendFlight>();
        let id = last_own().map(|(id, ..)| id).filter(|id| Some(id) != before.as_ref()).unwrap_or_default();
        let phase = flight.get_phase().to_string();
        let sample = Sample {
            at: sent.elapsed(),
            progress: flight.get_progress(),
            flight: matches!(phase.as_str(), "wait" | "flight" | "settle").then(|| (flight.get_x(), flight.get_y(), flight.get_width(), flight.get_height())),
            composer: flight.get_composer_height(),
            row: if id.is_empty() { None } else { app_now().measured_height(&id).0 },
            status: if id.is_empty() { String::new() } else { status_of(&id) },
            at_end: report().at_end,
            phase,
            id,
        };
        // Once, mid-flight: the overlay as drawn, against the report.
        if let Some(reported) = sample.flight.filter(|_| sample.progress > 0.3 && sample.progress < 0.9 && MID_WANTED.with(Cell::get))
            && let Some(drawn) = element("send-flight")
        {
            MID_WANTED.with(|m| m.set(false));
            MID.with(|m| *m.borrow_mut() = Some((reported, drawn)));
        }
        // The frame it landed on: its last report beside the bubble as
        // drawn then.
        let was_flying = SAMPLES.with(|s| s.borrow().last().is_some_and(|l| l.phase == "flight"));
        if was_flying && sample.phase != "flight" {
            let report = (flight.get_x(), flight.get_y(), flight.get_width(), flight.get_height());
            let bubble = element(&format!("row:{}", sample.id));
            LANDING.with(|l| *l.borrow_mut() = Some((report, flight.get_progress(), bubble)));
        }
        watch_reveal(&flight, &sample.id);
        SAMPLES.with(|s| s.borrow_mut().push(sample));
        let due: Vec<String> = SHOTS.with(|s| {
            let mut shots = s.borrow_mut();
            let due = shots.iter().filter(|(after, _)| sent.elapsed() >= *after).map(|(_, name)| name.clone()).collect();
            shots.retain(|(after, _)| sent.elapsed() < *after);
            due
        });
        for name in due {
            OUT.with(|o| shot(&o.borrow(), &name));
        }
    });
    SAMPLER.with(|s| *s.borrow_mut() = Some(timer));
}

/// A status line's reveal, as drawn: the first frame drawn with it, and
/// one 0.4 s later (well after its 0.2 s fade), each with its box.
#[derive(Default)]
struct RevealWatch {
    text: String,
    from: i32,
    frames: Option<usize>,
    early: Option<Rect>,
    late_due: Option<Instant>,
    late: Option<Rect>,
}

/// Watches for the reveal of a status line saying `text` (from now on).
fn watch_reveal_of(text: &str) {
    let from = crate::window().map_or(0, |w| w.global::<crate::SendFlight>().get_reveals());
    REVEAL.with(|r| *r.borrow_mut() = Some(RevealWatch { text: text.to_owned(), from, ..RevealWatch::default() }));
}

fn watch_reveal(flight: &crate::SendFlight, id: &str) {
    REVEAL.with(|r| {
        let mut slot = r.borrow_mut();
        let Some(watch) = slot.as_mut() else { return };
        if watch.late.is_some() || id.is_empty() || flight.get_reveals() <= watch.from || flight.get_reveal_text().as_str() != watch.text {
            return;
        }
        let drawn = crate::perf::stats().frames.len();
        let Some(frames) = watch.frames else {
            watch.frames = Some(drawn);
            return;
        };
        if watch.early.is_none() && drawn > frames {
            OUT.with(|o| shot(&o.borrow(), "send-12-reveal-early"));
            watch.early = element(&format!("status:{id}"));
            watch.late_due = Some(Instant::now() + Duration::from_millis(400));
        } else if watch.late_due.is_some_and(|due| Instant::now() >= due) {
            OUT.with(|o| shot(&o.borrow(), "send-13-reveal-late"));
            watch.late = element(&format!("status:{id}"));
        }
    });
}

/// How far the text in `rect` stands out from its background (the box's
/// corner) in a saved frame: the largest difference in luminance, 0..255.
fn contrast(name: &str, rect: Rect) -> Option<f32> {
    let frame = image::open(format!("{}/{name}.png", OUT.with(|o| o.borrow().clone()))).ok()?.to_rgba8();
    let scale = crate::window().map_or(1.0, |w| w.window().scale_factor());
    let (x0, y0) = ((rect.0 * scale) as u32, (rect.1 * scale) as u32);
    let (x1, y1) = (((rect.0 + rect.2) * scale) as u32, ((rect.1 + rect.3) * scale) as u32);
    let luma = |x: u32, y: u32| {
        let p = frame.get_pixel(x.min(frame.width() - 1), y.min(frame.height() - 1));
        0.299 * p[0] as f32 + 0.587 * p[1] as f32 + 0.114 * p[2] as f32
    };
    let background = luma(x0, y0);
    let mut most: f32 = 0.0;
    for y in y0..y1 {
        for x in x0..x1 {
            most = most.max((luma(x, y) - background).abs());
        }
    }
    Some(most)
}

fn stop_sampling() -> Vec<Sample> {
    SAMPLER.with(|s| s.borrow_mut().take());
    SAMPLES.with(|s| s.borrow().clone())
}

fn since_send() -> Duration {
    SENT_AT.with(Cell::get).map_or(Duration::ZERO, |at| at.elapsed())
}

fn type_draft(text: &str) {
    app_now().focus_composer();
    for (index, line) in text.split('\n').enumerate() {
        if index > 0 {
            headless::press_with(&[Key::Shift], Key::Return);
        }
        headless::type_text(line);
    }
}

fn draft_is(text: &str) -> bool {
    app_now().active_draft() == text
}

fn set_delay(seconds: f64) {
    check(control("/__control/send-delay", &json!({"seconds": seconds})).is_ok(), &format!("the Host takes {seconds} s to confirm a send"));
}

fn near(a: f32, b: f32, slack: f32) -> bool {
    (a - b).abs() <= slack
}

fn heights(samples: &[Sample]) -> Vec<f32> {
    samples.iter().filter_map(|s| s.row).collect()
}

/// The last reveal finished was `text`'s, and took 0.2 s (a frame's slack:
/// the fade is drawn on time, its end noticed on the next frame).
fn reveal_took(text: &str) -> bool {
    let Some(window) = crate::window() else { return false };
    let flight = window.global::<crate::SendFlight>();
    let took = flight.get_reveal_took();
    let ok = flight.get_reveal_text().as_str().contains(text) && (200..=330).contains(&took);
    println!("perf reveal: {:?} took {took} ms", flight.get_reveal_text());
    ok
}

/// The frames drawn in the flight that started at `from` (the renderer's
/// own times, ms).
fn flight_frames(from: Duration) -> Vec<f64> {
    let until = from + Duration::from_millis(900);
    crate::perf::stats().frames.iter().filter(|f| f.at >= from && f.at <= until).map(|f| crate::perf::ms(f.took)).collect()
}

pub fn send_check(out: String) {
    OUT.with(|o| *o.borrow_mut() = out.clone());
    let frames_from = std::rc::Rc::new(Cell::new(Duration::ZERO));
    let (f1, f2) = (frames_from.clone(), frames_from.clone());
    let previous = std::rc::Rc::new(RefCell::new(None::<String>));
    let (p1, p2, p3, p4, p5, p6, p7) = (previous.clone(), previous.clone(), previous.clone(), previous.clone(), previous.clone(), previous.clone(), previous.clone());
    let stages: Vec<Stage> = vec![
        ("rachel open", Box::new(|app, _window, _| {
            let open = app.engine.borrow().conversation("rachel").is_some_and(|c| !c.loading() && !c.rows().is_empty());
            if !open {
                return false;
            }
            // A chat longer than the window, so the new bubble lands at the
            // viewport's end.
            check(control("/__control/fill", &json!({"session": "rachel", "count": 30})).is_ok(), "the Host takes a long chat");
            true
        })),
        ("filled", Box::new(|app, _window, elapsed| {
            let filled = app.engine.borrow().conversation("rachel").is_some_and(|c| c.rows().len() >= 30);
            if !filled || elapsed < Duration::from_millis(300) {
                return false;
            }
            app_now().focus_composer();
            true
        })),
        ("empty composer", Box::new(|_, _window, elapsed| {
            if !report().composer_focused || elapsed < Duration::from_millis(300) || !report().at_end {
                return false;
            }
            let Some(empty) = composer_box() else { return false };
            EMPTY.with(|e| e.set(empty.3));
            check(empty.3 > 20.0, &format!("the empty composer is drawn: {empty:?}"));
            set_delay(0.8);
            type_draft("Benefit of the doubt");
            true
        })),
        // (a) Confirmed within the grace period: never a status, and the row
        // is as tall when sent as when it first showed.
        ("quick send", Box::new(move |_, _window, _| {
            if !draft_is("Benefit of the doubt") {
                return false;
            }
            *p1.borrow_mut() = last_own().map(|(id, ..)| id);
            send_and_sample(false, &[], p1.borrow().clone());
            true
        })),
        ("quick confirmed", Box::new(|_, _window, _| {
            let confirmed = last_own().is_some_and(|(id, pending, _)| id.starts_with("u-") && !pending);
            if !confirmed || since_send() < Duration::from_millis(1800) {
                return false;
            }
            let samples = stop_sampling();
            let shown: Vec<&Sample> = samples.iter().filter(|s| s.row.is_some()).collect();
            check(shown.len() > 10, &format!("the new row was sampled while it was sent and after: {} frames", shown.len()));
            let statuses: Vec<String> = samples.iter().map(|s| s.status.clone()).filter(|s| !s.is_empty()).collect();
            check(statuses.is_empty(), &format!("confirmed in 0.8 s, under the grace period: no status was ever shown ({statuses:?})"));
            let heights = heights(&samples);
            let (low, high) = heights.iter().fold((f32::MAX, f32::MIN), |(l, h), x| (l.min(*x), h.max(*x)));
            check(!heights.is_empty() && high - low < 0.5, &format!("the row's height never changed from unsent to sent: {low}..{high}px over {} frames", heights.len()));
            shot(&OUT.with(|o| o.borrow().clone()), "send-01-confirmed");
            set_delay(2.9);
            type_draft("A slow Host");
            true
        })),
        // Over the grace period: the status shows, once it is due.
        ("slow send", Box::new(move |_, _window, _| {
            if !draft_is("A slow Host") {
                return false;
            }
            *p2.borrow_mut() = last_own().map(|(id, ..)| id);
            watch_reveal_of("Sending…");
            send_and_sample(false, &[(2100, "send-02-sending")], p2.borrow().clone());
            true
        })),
        ("slow confirmed", Box::new(|_, _window, _| {
            let confirmed = last_own().is_some_and(|(id, pending, _)| id.starts_with("u-") && !pending);
            if !confirmed || since_send() < Duration::from_millis(3300) {
                return false;
            }
            let samples = stop_sampling();
            let first = samples.iter().find(|s| s.status == "Sending…").map(|s| s.at);
            check(
                first.is_some_and(|at| at >= Duration::from_millis(1700) && at <= Duration::from_millis(2600)),
                &format!("unsent past the grace period (1.75 s), it says Sending… from {first:?}"),
            );
            let heights = heights(&samples);
            let changes = heights.windows(2).filter(|w| (w[1] - w[0]).abs() >= 0.5).count();
            check(changes <= 2, &format!("the late line grows the row once (and confirmed, it is gone): {changes} height changes"));
            // It fades in (ease-out 0.2 s): fainter on the first frame that
            // draws it than once revealed.
            let watch = REVEAL.with(|r| r.borrow_mut().take()).unwrap_or_default();
            let early = watch.early.and_then(|r| contrast("send-12-reveal-early", r));
            let late = watch.late.and_then(|r| contrast("send-13-reveal-late", r));
            check(
                early.zip(late).is_some_and(|(e, l)| l > 40.0 && e < l * 0.9),
                &format!("Sending… fades in: contrast {early:?} on its first frame, {late:?} once revealed"),
            );
            check(reveal_took("Sending…"), "and its fade takes 0.2 s");
            set_delay(0.3);
            check(control("/__control/fail", &json!({"path": "/send", "status": 500, "count": 1})).is_ok(), "the next send fails");
            type_draft("This one fails");
            true
        })),
        // A failure shows at once, grace period or not.
        ("failing send", Box::new(move |_, _window, _| {
            if !draft_is("This one fails") {
                return false;
            }
            *p3.borrow_mut() = last_own().map(|(id, ..)| id);
            send_and_sample(false, &[], p3.borrow().clone());
            true
        })),
        ("failed", Box::new(|_, _window, _| {
            let failed = last_own().is_some_and(|(_, _, failed)| failed);
            if !failed || since_send() < Duration::from_millis(900) {
                return false;
            }
            let samples = stop_sampling();
            let first = samples.iter().find(|s| s.status == "Not delivered").map(|s| s.at);
            check(first.is_some_and(|at| at < Duration::from_millis(1000)), &format!("a failed send says Not delivered at once: {first:?}"));
            check(reveal_took("Not delivered"), "fading in over 0.2 s");
            shot(&OUT.with(|o| o.borrow().clone()), "send-03-failed");
            // (b) The flight: a two-line draft, the Host a little slow so its
            // reply does not land mid-flight.
            set_delay(1.6);
            type_draft("Lift it out of the box\nand into the chat");
            true
        })),
        ("flight", Box::new(move |_, _window, elapsed| {
            if !draft_is("Lift it out of the box\nand into the chat") || elapsed < Duration::from_millis(300) || !report().at_end {
                return false;
            }
            let Some(before) = composer_box() else { return false };
            BOX_BEFORE.with(|b| b.set(before.3));
            check(before.3 > EMPTY.with(Cell::get) + 10.0, &format!("two lines make the composer taller: {} > {}", before.3, EMPTY.with(Cell::get)));
            let now = crate::perf::now();
            BASELINE.with(|b| *b.borrow_mut() = crate::perf::stats().frames.iter().filter(|f| f.at + Duration::from_secs(2) >= now).map(|f| crate::perf::ms(f.took)).collect());
            f1.set(now);
            MID.with(|m| m.borrow_mut().take());
            LANDING.with(|l| l.borrow_mut().take());
            MID_WANTED.with(|m| m.set(true));
            *p4.borrow_mut() = last_own().map(|(id, ..)| id);
            send_and_sample(false, &[(60, "send-04-flight-early"), (140, "send-05-flight-mid"), (220, "send-06-flight-late")], p4.borrow().clone());
            true
        })),
        ("landed", Box::new(move |_, window, _| {
            let flight = window.global::<crate::SendFlight>();
            // Landed, and the frame after it sampled.
            let sampled = LANDING.with(|l| l.borrow().is_some());
            if flight.get_landed() < flight.get_started() || flight.get_started() <= STARTED.with(Cell::get) || since_send() < Duration::from_millis(500) || !sampled {
                return false;
            }
            let samples = stop_sampling();
            let frames = flight_frames(f2.get());
            let flying: Vec<&Sample> = samples.iter().filter(|s| s.phase == "flight" && s.flight.is_some()).collect();
            check(flying.len() >= 3, &format!("the text flew, sampled over {} frames", flying.len()));
            // Landed, the copy fades over the real bubble (0.14 s).
            let settling: Vec<Duration> = samples.iter().filter(|s| s.phase == "settle").map(|s| s.at).collect();
            let span = settling.last().zip(settling.first()).map(|(l, f)| *l - *f);
            check(!settling.is_empty() && span.is_some_and(|d| d <= Duration::from_millis(300)), &format!("landed, the copy fades over the bubble: {} frames over {span:?}", settling.len()));
            let progress: Vec<f32> = flying.iter().map(|s| s.progress).collect();
            // A spring with a little bounce: forward, give or take its
            // overshoot of a fraction of a percent.
            check(progress.windows(2).all(|w| w[1] >= w[0] - 0.01), &format!("sprung forward only: {progress:?}"));
            let from = (flight.get_from_x(), flight.get_from_y(), flight.get_from_width(), flight.get_from_height());
            let id = samples.iter().rev().find(|s| !s.id.is_empty()).map(|s| s.id.clone()).unwrap_or_default();
            let bubble = element(&format!("row:{id}"));
            let distance = |a: Rect, b: Rect| ((a.0 - b.0).powi(2) + (a.1 - b.1).powi(2)).sqrt();
            let landing = LANDING.with(|l| l.borrow_mut().take());
            match (flying.first().and_then(|s| s.flight.map(|f| (f, s.progress))), landing, bubble) {
                (Some((start, early)), Some((end, last, Some(landed_on))), Some(bubble)) => {
                    // Its first frame is on the way out of the composer: on
                    // the line from the editor's text to the bubble, as far
                    // along as the animation is.
                    let along = (from.0 + (bubble.0 - from.0) * early, from.1 + (bubble.1 - from.1) * early);
                    check(
                        early < 0.7 && distance(start, from) < distance(start, bubble) && near(start.0, along.0, 12.0) && near(start.1, along.1, 12.0),
                        &format!("it starts from the composer's text: {start:?} at {early:.2} from {from:?} towards {bubble:?}"),
                    );
                    let close = near(end.0, landed_on.0, 1.0) && near(end.1, landed_on.1, 1.0) && near(end.2, landed_on.2, 1.0) && near(end.3, landed_on.3, 1.0);
                    check(last >= 0.999 && close, &format!("it ends on the bubble's final place: {end:?} at {last:.3} on {landed_on:?}"));
                    check(near(landed_on.1, bubble.1, 1.0) && near(landed_on.3, bubble.3, 1.0), &format!("where the bubble stays: {landed_on:?} then {bubble:?}"));
                }
                other => check(false, &format!("the flight's frames and the bubble were drawn: {other:?}")),
            }
            // As drawn, mid-flight: between the composer and the bubble.
            match (MID.with(|m| m.borrow_mut().take()), bubble) {
                (Some((reported, drawn)), Some(bubble)) => {
                    // The element is read a frame or two after the report,
                    // while the chat's end still settles under the shrinking
                    // composer: a few pixels' slack.
                    let between = |v: f32, a: f32, b: f32| v >= a.min(b) - 8.0 && v <= a.max(b) + 8.0;
                    let inside = between(drawn.0, from.0, bubble.0) && between(drawn.1, from.1, bubble.1) && between(drawn.2, from.2, bubble.2) && between(drawn.3, from.3, bubble.3);
                    check(inside, &format!("mid-flight the text is drawn between the composer and the bubble: {drawn:?} (reported {reported:?})"));
                }
                other => check(false, &format!("a mid-flight frame of the overlay was found: {other:?}")),
            }
            let boxes: Vec<f32> = flying.iter().map(|s| s.composer).collect();
            let empty = EMPTY.with(Cell::get);
            let shrinking = boxes.windows(2).all(|w| w[1] <= w[0] + 0.5);
            let between = boxes.iter().filter(|h| **h < BOX_BEFORE.with(Cell::get) - 0.5 && **h > empty + 0.5).count();
            check(shrinking && between >= 1, &format!("the composer shrinks smoothly ({between} frames between its heights): {boxes:?}"));
            check(composer_box().is_some_and(|c| near(c.3, empty, 1.0)), &format!("and ends at its empty height {empty}: {:?}", composer_box()));
            // The iPhone's timing, by the animation's own clock.
            let ms = |d: i64| d.max(0) as u64;
            // What is drawn follows the clock to the frame; the pane's
            // bookkeeping (when it saw each moment) runs a frame or so
            // behind, so a frame's slack on what it saw.
            let worst_frame = frames.iter().copied().fold(0.0, f64::max) as u64;
            let slack = worst_frame.max(60) + 60;
            let (placed, seen, gone) = (ms(flight.get_placed_at() - flight.get_sent_at()), ms(flight.get_settle_seen_at() - flight.get_placed_at()), ms(flight.get_gone_at() - flight.get_settled_at()));
            println!("perf send timing: bubble laid out {placed} ms after the send; landing seen {seen} ms later (lands at 320 ms); copy gone {gone} ms after landing (slack {slack} ms)");
            check(samples.iter().any(|s| s.phase == "wait") || placed < 100, &format!("the copy waits on the composer until the bubble is laid out ({placed} ms)"));
            check((320..=320 + slack).contains(&seen), &format!("it lands 320 ms after the bubble is laid out: seen at {seen} ms"));
            check((200..=200 + slack).contains(&gone), &format!("the copy fades and is removed by +200 ms after landing: {gone} ms"));
            check(element("send-flight").is_none(), "the copy is gone");
            let away = samples.iter().filter(|s| !s.at_end).count();
            check(away == 0 && report().at_end, &format!("the chat stays at its end before, during and after ({away} frames away)"));
            let jumps = unexplained() - JUMPS.with(Cell::get);
            check(jumps == 0, &format!("and the scroll journal saw no jump ({jumps})"));
            // The software renderer draws the whole window each frame: a
            // flight frame should cost about what a frame did before it.
            let mean = |f: &[f64]| if f.is_empty() { 0.0 } else { f.iter().sum::<f64>() / f.len() as f64 };
            let baseline = BASELINE.with(|b| b.borrow().clone());
            let worst = frames.iter().copied().fold(0.0, f64::max);
            println!("perf send flight: {} frames, mean {:.1} ms, worst {worst:.1} ms; before it {} frames, mean {:.1} ms", frames.len(), mean(&frames), baseline.len(), mean(&baseline));
            check(frames.len() >= 3, &format!("the flight was drawn frame by frame: {} frames", frames.len()));
            check(baseline.is_empty() || mean(&frames) <= mean(&baseline) * 1.5 + 4.0, &format!("a flight frame costs about an ordinary one: {:.1} ms against {:.1} ms", mean(&frames), mean(&baseline)));
            shot(&OUT.with(|o| o.borrow().clone()), "send-07-landed");
            // Ctrl+Enter (a queued send) flies too.
            type_draft("Queued behind the turn");
            true
        })),
        ("queued", Box::new(move |_, _window, elapsed| {
            if !draft_is("Queued behind the turn") || elapsed < Duration::from_millis(300) || !report().at_end {
                return false;
            }
            *p5.borrow_mut() = last_own().map(|(id, ..)| id);
            send_and_sample(true, &[], p5.borrow().clone());
            true
        })),
        ("queued landed", Box::new(|_, window, _| {
            let flight = window.global::<crate::SendFlight>();
            if flight.get_started() <= STARTED.with(Cell::get) || flight.get_landed() < flight.get_started() || since_send() < Duration::from_millis(500) {
                return false;
            }
            let samples = stop_sampling();
            check(samples.iter().any(|s| s.phase == "flight" && s.flight.is_some()), "Ctrl+Enter's send flies too");
            check(super::sends().last().is_some_and(|s| s["body"]["queue_if_busy"] == true), "and was queued");
            // A reader scrolled up: no flight, the usual jump to the latest.
            app_now().scroll_by(-900.0);
            true
        })),
        ("scrolled up", Box::new(|_, _window, elapsed| {
            if report().at_end || elapsed < Duration::from_millis(400) {
                return false;
            }
            app_now().focus_composer();
            type_draft("Sent from above");
            true
        })),
        ("send from above", Box::new(move |_, _window, elapsed| {
            if !draft_is("Sent from above") || elapsed < Duration::from_millis(200) {
                return false;
            }
            check(!report().at_end, "the reader is still scrolled up");
            *p6.borrow_mut() = last_own().map(|(id, ..)| id);
            send_and_sample(false, &[], p6.borrow().clone());
            true
        })),
        ("no flight", Box::new(|_, window, _| {
            if since_send() < Duration::from_millis(900) {
                return false;
            }
            let samples = stop_sampling();
            let flight = window.global::<crate::SendFlight>();
            check(flight.get_started() == STARTED.with(Cell::get) && samples.iter().all(|s| s.flight.is_none()), "scrolled up, a send does not fly");
            check(report().at_end, "and the latest comes into view as before");
            type_draft("Nowhere to land");
            true
        })),
        // No bubble laid out in time (the send reports a row no chat
        // draws): the copy waits on the composer, then is dropped at 600 ms.
        ("no destination", Box::new(|_, _window, elapsed| {
            if !draft_is("Nowhere to land") || elapsed < Duration::from_millis(300) || !report().at_end {
                return false;
            }
            super::LOSE_SEND_ROW.with(|l| l.set(true));
            DROPPED.with(|d| d.set(crate::window().map_or(0, |w| w.global::<crate::SendFlight>().get_dropped())));
            send_and_sample(false, &[(300, "send-09-waiting")], last_own().map(|(id, ..)| id));
            true
        })),
        ("dropped", Box::new(|_, window, _| {
            let flight = window.global::<crate::SendFlight>();
            if flight.get_dropped() <= DROPPED.with(Cell::get) || since_send() < Duration::from_millis(900) {
                return false;
            }
            let samples = stop_sampling();
            let waited = (flight.get_gone_at() - flight.get_sent_at()) as u64;
            check(samples.iter().any(|s| s.phase == "wait") && samples.iter().all(|s| s.phase != "flight" && s.phase != "settle"), "the copy waited on the composer and never flew");
            check((600..=820).contains(&waited), &format!("with no bubble to go to, the copy is dropped at 600 ms: {waited} ms"));
            check(element("send-flight").is_none(), "and is gone");
            check(last_own().is_some_and(|(id, ..)| element(&format!("row:{id}")).is_some()), "the bubble shows");
            // A send with no text (an attachment only): no copy, a fade.
            app_now().focus_composer();
            headless::press_with(&[Key::Control, Key::Shift], "O");
            true
        })),
        ("attached", Box::new(|_, _window, elapsed| {
            let ready = super::view().attachments.row_data(0).is_some_and(|c| c.status == "ready");
            if !ready || !app_now().active_draft().is_empty() || elapsed < Duration::from_millis(300) || !report().at_end {
                return false;
            }
            send_and_sample(false, &[(60, "send-10-no-text-fade")], last_own().map(|(id, ..)| id));
            true
        })),
        ("no text faded", Box::new(|_, window, _| {
            let flight = window.global::<crate::SendFlight>();
            if flight.get_started() <= STARTED.with(Cell::get) || flight.get_landed() < flight.get_started() || since_send() < Duration::from_millis(500) {
                return false;
            }
            let samples = stop_sampling();
            let faded = (flight.get_gone_at() - flight.get_sent_at()) as u64;
            check(super::sends().last().is_some_and(|s| s["body"]["text"].as_str().is_some_and(|t| t.contains("photo.png"))), "the attachment was sent");
            check(samples.iter().any(|s| s.phase == "fade") && samples.iter().all(|s| s.flight.is_none() && s.phase != "wait"), "with no text there is no copy: the bubble fades in");
            check((200..=360).contains(&faded), &format!("over 0.2 s: {faded} ms"));
            // A draft longer than the composer shows: a fade too.
            type_draft(&LONG_DRAFT.join("\n"));
            true
        })),
        ("long draft", Box::new(|_, _window, elapsed| {
            if !draft_is(&LONG_DRAFT.join("\n")) || elapsed < Duration::from_millis(300) || !report().at_end {
                return false;
            }
            send_and_sample(false, &[], last_own().map(|(id, ..)| id));
            true
        })),
        ("long faded", Box::new(|_, window, _| {
            let flight = window.global::<crate::SendFlight>();
            if flight.get_started() <= STARTED.with(Cell::get) || flight.get_landed() < flight.get_started() || since_send() < Duration::from_millis(500) {
                return false;
            }
            let samples = stop_sampling();
            check(samples.iter().any(|s| s.phase == "fade") && samples.iter().all(|s| s.flight.is_none()), "a draft longer than the composer shows fades in, no copy");
            live_checks::set_reduced_motion(true);
            type_draft("Still and quiet");
            true
        })),
        // Reduce Motion: a short fade, the composer cleared at once.
        ("reduced", Box::new(move |_, window, elapsed| {
            if !draft_is("Still and quiet") || elapsed < Duration::from_millis(300) || !window.global::<crate::ChatLook>().get_reduced_motion() || !report().at_end {
                return false;
            }
            *p7.borrow_mut() = last_own().map(|(id, ..)| id);
            send_and_sample(false, &[(60, "send-08-fade")], p7.borrow().clone());
            true
        })),
        ("faded", Box::new(|_, window, _| {
            let flight = window.global::<crate::SendFlight>();
            if flight.get_started() <= STARTED.with(Cell::get) || flight.get_landed() < flight.get_started() || since_send() < Duration::from_millis(400) {
                return false;
            }
            let samples = stop_sampling();
            check(samples.iter().any(|s| s.phase == "fade") && samples.iter().all(|s| s.phase != "flight" && s.flight.is_none()), "with Reduce Motion the bubble fades in, nothing flies");
            let empty = EMPTY.with(Cell::get);
            let faded = (flight.get_gone_at() - flight.get_sent_at()) as u64;
            check((200..=360).contains(&faded), &format!("a 0.2 s fade: {faded} ms"));
            let boxes: Vec<f32> = samples.iter().filter(|s| s.phase == "fade").map(|s| s.composer).collect();
            check(boxes.first().is_some_and(|h| near(*h, empty, 1.0)), &format!("and the composer is empty at once: {boxes:?}"));
            live_checks::set_reduced_motion(false);
            crate::settings_view::change(&app_now(), window, "send-animation", 1);
            type_draft("No animation at all");
            true
        })),
        ("off", Box::new(|_, window, elapsed| {
            if !draft_is("No animation at all") || elapsed < Duration::from_millis(300) || window.global::<crate::ChatLook>().get_send_animation() {
                return false;
            }
            check(crate::settings_view::rows(&app_now()).iter().any(|r| r.id == "send-animation" && !r.on), "Settings show Send animation off");
            send_and_sample(false, &[], last_own().map(|(id, ..)| id));
            true
        })),
        ("plain", Box::new(|_, window, _| {
            if since_send() < Duration::from_millis(600) {
                return false;
            }
            let samples = stop_sampling();
            let flight = window.global::<crate::SendFlight>();
            check(flight.get_started() == STARTED.with(Cell::get) && samples.iter().all(|s| s.flight.is_none()), "off, a send neither flies nor fades");
            crate::settings_view::change(&app_now(), window, "send-animation", 1);
            // A turn queued behind the running one: its labels wait too.
            let queued = json!({"session": "rachel", "set": {"queued_turn_count": 1}});
            check(control("/__control/agent", &queued).is_ok(), "a turn waits in rachel's queue");
            QUEUE_SEEN.with(|q| q.borrow_mut().clear());
            SENT_AT.with(|s| s.set(Some(Instant::now())));
            true
        })),
        ("queue label", Box::new(|app, _window, _| {
            let host = app.engine.borrow().queue_count("rachel");
            let composer = super::view().queued;
            let explorer = app.chats.iter().find(|c| c.session == "rachel").map_or(-1, |c| c.queued);
            QUEUE_SEEN.with(|q| q.borrow_mut().push((since_send(), host, composer, explorer)));
            if (composer < 1 || explorer < 1) && since_send() < Duration::from_secs(6) {
                return false;
            }
            let seen = QUEUE_SEEN.with(|q| q.borrow().clone());
            let known = seen.iter().find(|s| s.1 >= 1).map(|s| s.0);
            let shown = seen.iter().find(|s| s.2 >= 1 && s.3 >= 1).map(|s| s.0);
            let early = seen.iter().filter(|s| s.1 >= 1 && (s.2 >= 1 || s.3 >= 1)).any(|s| known.is_some_and(|k| s.0 < k + Duration::from_millis(1600)));
            check(known.is_some() && !early, &format!("the queued-turn labels (composer, explorer) stay hidden under 1.75 s: known at {known:?}"));
            check(
                known.zip(shown).is_some_and(|(k, s)| s >= k + Duration::from_millis(1600) && s <= k + Duration::from_millis(2600)),
                &format!("and show after it: known {known:?}, shown {shown:?}"),
            );
            true
        })),
        ("queue label revealed", Box::new(|_, _window, elapsed| {
            if elapsed < Duration::from_millis(500) {
                return false;
            }
            check(reveal_took("queued"), "the queued-turn labels fade in over 0.2 s");
            shot(&OUT.with(|o| o.borrow().clone()), "send-11-queued-label");
            check(control("/__control/agent", &json!({"session": "rachel", "set": {"queued_turn_count": 0}})).is_ok(), "the queue empties");
            SENT_AT.with(|s| s.set(Some(Instant::now())));
            true
        })),
        ("queue emptied", Box::new(|app, _window, _| {
            if app.engine.borrow().queue_count("rachel") != 0 {
                return false;
            }
            let composer = super::view().queued;
            let explorer = app.chats.iter().find(|c| c.session == "rachel").map_or(-1, |c| c.queued);
            check(composer == 0 && explorer == 0, &format!("an emptied queue's labels go at once: composer {composer}, explorer {explorer}"));
            true
        })),
    ];
    run_stages(stages);
}
