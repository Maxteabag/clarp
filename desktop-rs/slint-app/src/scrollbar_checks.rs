//! The chat's scrollbar (`--check scrollbar`), measured on screen: a long,
//! varied chat (long Markdown, code, tables, quotes, folded turns, other
//! agents' prompts, decision receipts, tool calls, user bubbles) is read top
//! to bottom and back by wheel and by Page keys, and the scrollbar's thumb
//! is read at every step. After the first full pass the thumb keeps its
//! size, and it moves the way the reader scrolls: never back up while they
//! go down, never down while they go up; at the top it is at the top of its
//! track and at the end at the bottom.

use std::cell::RefCell;
use std::rc::Rc;
use std::time::{Duration, Instant};

use serde_json::{Value, json};

use super::{Stage, check, control, report, rows, run_stages, shot};
use slint::ComponentHandle;

use crate::headless;

const SESSION: &str = "steady";
/// Turns of the long chat; each is several rows.
const TURNS: usize = 240;
/// A chat of one page (the app opens a chat with its latest 100 rows): the
/// thumb is big enough here that a change in its size shows; in the long
/// chat it is at its smallest.
const SHORT: &str = "steady-short";
const SHORT_ROWS: usize = 99;

/// The thumb and the track it travels (window coordinates).
#[derive(Debug, Clone, Copy, Default)]
struct Thumb {
    top: f32,
    height: f32,
    track_top: f32,
    track_bottom: f32,
}

impl Thumb {
    /// Where the thumb is along its travel, 0 the top and 1 the end.
    fn place(&self) -> f32 {
        let travel = self.track_bottom - self.track_top - self.height;
        if travel <= 0.0 { 1.0 } else { (self.top - self.track_top) / travel }
    }
}

/// The transcript's viewport (left, top, width, height).
fn viewport() -> Option<(f32, f32, f32, f32)> {
    use i_slint_backend_testing::ElementQuery;
    let window = crate::window()?;
    let element = ElementQuery::from_root(&window).match_type_name("Transcript").find_first()?;
    let (p, s) = (element.absolute_position(), element.size());
    Some((p.x, p.y, s.width, s.height))
}

/// The chat's scrollbar thumb as drawn: the transcript's own when it has
/// one, else the standard ScrollView's (its track is the bar less the
/// 16 px at each end that it keeps for its arrows).
fn thumb() -> Option<Thumb> {
    use i_slint_backend_testing::ElementQuery;
    let window = crate::window()?;
    let (left, top, width, height) = viewport()?;
    let inside = |p: slint::LogicalPosition, s: slint::LogicalSize| {
        s.height > s.width && p.x >= left + width - 30.0 && p.x <= left + width + 1.0 && p.y >= top - 1.0 && p.y + s.height <= top + height + 1.0
    };
    let by_id = |id: &'static str| {
        ElementQuery::from_root(&window)
            .match_predicate(move |e| e.id().is_some_and(|i| i == id))
            .find_all()
            .into_iter()
            .map(|e| (e.absolute_position(), e.size()))
            .find(|(p, s)| inside(*p, *s))
    };
    if let (Some((tp, ts)), Some((kp, ks))) = (by_id("Transcript::thumb"), by_id("Transcript::track")) {
        return Some(Thumb { top: tp.y, height: ts.height, track_top: kp.y, track_bottom: kp.y + ks.height });
    }
    let (tp, ts) = by_id("ScrollBar::thumb")?;
    let bar = ElementQuery::from_root(&window)
        .match_type_name("ScrollBar")
        .find_all()
        .into_iter()
        .map(|e| (e.absolute_position(), e.size()))
        .find(|(p, s)| inside(*p, *s))?;
    Some(Thumb { top: tp.y, height: ts.height, track_top: bar.0.y + 16.0, track_bottom: bar.0.y + bar.1.height - 16.0 })
}

/// The rows drawn in the viewport: (id, height).
fn drawn() -> Vec<(String, f32)> {
    use i_slint_backend_testing::ElementQuery;
    let (Some(window), Some((_, top, _, height))) = (crate::window(), viewport()) else { return Vec::new() };
    ElementQuery::from_root(&window)
        .match_predicate(|e| e.accessible_id().is_some_and(|id| id.starts_with("row:")))
        .find_all()
        .into_iter()
        .filter_map(|e| {
            let (p, s) = (e.absolute_position(), e.size());
            let id = e.accessible_id()?;
            (s.height > 0.0 && p.y + s.height > top && p.y < top + height).then(|| (id.trim_start_matches("row:").to_owned(), s.height))
        })
        .collect()
}

/// Rows whose drawn height differs from the scroll book's by over a pixel
/// (the book's heights come from rows measured off screen). The rows'
/// accessible element is the message inside its 7 px padding.
fn misfits() -> Vec<String> {
    let app = super::app_now();
    drawn()
        .into_iter()
        .filter_map(|(id, height)| match app.measured_height(&id).0 {
            Some(book) if (book - (height + 14.0)).abs() <= 1.0 => None,
            book => Some(format!("{id}: drawn {:.1}, book {book:?}", height + 14.0)),
        })
        .collect()
}

fn wheel(delta: f32) {
    if let Some((left, top, width, height)) = viewport() {
        headless::wheel(left + width / 2.0, top + height / 2.0, delta);
    }
}

fn resolved_prompt(i: usize, question: &str, outcome: &str) -> String {
    format!("[Clarp decision resolved]\nDecision ID: d-steady-{i}\nArtifact ID: steady-{i}\nQuestion: {question}\nContext: Two reviews are in.\nReference: \nPayload: {{\"ticket\": \"OPS-{i}\"}}\n{outcome}")
}

const LOREM: &str = "The build runs the unit tests first, then the integration suite against a scratch database, and only then packages the desktop app. ";

/// Turn `i` of a long chat whose rows differ a lot in kind and height.
fn turn(session: &str, i: usize, turns: &mut Vec<Value>) {
    let trace = format!("t-{i}");
    let at = format!("2026-10-05T{:02}:{:02}:00Z", 6 + i / 60, i % 60);
    let id = |name: &str| format!("{session}-{i}-{name}");
    let size = 1 + (i * 7) % 5;
    let user = |text: String| json!({"id": id("u"), "role": "user", "timestamp": at, "text": text, "origin": "user", "trace_id": trace});
    let reply = |text: String| json!({"id": id("a"), "role": "assistant", "timestamp": at, "text": text, "origin": "agent", "trace_id": trace, "phase": "final"});
    match i % 8 {
        0 => {
            turns.push(user(format!("Question {i}: what changed in the build?")));
            let items: String = (0..2 + size).map(|n| format!("- **Part {n}:** {}\n", &LOREM[..40 + n * 12])).collect();
            turns.push(reply(format!("## Summary {i}\n\n{}\n\n{items}\n### Next\n\n{}", LOREM.repeat(size), LOREM.repeat(1 + size / 2))));
        }
        1 => {
            turns.push(user(format!("Show me the code for {i}.")));
            let code: Vec<String> = (0..4 + size * 4).map(|n| format!("    let value_{n} = compute({n}, \"step\");")).collect();
            turns.push(reply(format!("Code {i}:\n\n```rust\nfn main() {{\n{}\n}}\n```\n\nThat is all.", code.join("\n"))));
        }
        2 => {
            turns.push(user(format!("Long request {i}. {}", LOREM.repeat(1 + size))));
            let table: String = (0..2 + size * 2).map(|n| format!("| step {n} | {} | {}s |\n", if n % 2 == 1 { "done" } else { "running" }, n * 3)).collect();
            turns.push(reply(format!("Table {i}:\n\n| step | state | time |\n|---|---|---|\n{table}")));
        }
        3 => {
            turns.push(user(format!("Quote it for {i}.")));
            turns.push(reply(format!("> {}\n\n> Keep the main checkout untouched.\n\nAnd {}", LOREM.repeat(size), LOREM)));
        }
        4 => {
            // A turn that works: tool calls and a note fold behind "Worked for".
            turns.push(user(format!("Run the tests for {i}.")));
            for n in 0..1 + size % 3 {
                turns.push(json!({"id": id(&format!("tool{n}")), "role": "assistant", "timestamp": at, "text": "", "origin": "agent", "trace_id": trace,
                    "tools": [{"name": "Bash", "summary": format!("cargo test -p part{n}"), "status": "ok", "id": format!("call-{i}-{n}"), "command": format!("cargo test -p part{n}"),
                               "result": (0..4 + n).map(|m| format!("test case_{m} ... ok")).collect::<Vec<_>>().join("\n")}]}));
            }
            turns.push(json!({"id": id("note"), "role": "assistant", "timestamp": at, "text": "Tests pass; writing it up.", "origin": "agent", "trace_id": trace, "phase": "commentary"}));
            let mut done = reply(format!("Ran the tests for {i}: all green. {}", LOREM.repeat(size / 2)));
            done["turn"] = json!({"turn_id": trace, "status": "completed", "started_at_ms": 1_000, "ended_at_ms": 9_000, "worked_ms": 8_000, "tool_count": 1 + size % 3});
            turns.push(done);
        }
        5 => {
            // Another agent's prompt, folded to one line.
            turns.push(json!({"id": id("u"), "role": "user", "timestamp": at, "origin": "agent", "sender_name": "Dagger", "sender_agent_id": "dagger-id", "trace_id": trace,
                "text": format!("Go ahead with step {i}: it matches the review.\n\n{}", LOREM.repeat(size))}));
            turns.push(reply(format!("Done {i}.")));
        }
        6 => {
            // A decision the user resolved: a receipt in their column.
            let outcome = if i % 16 == 6 { "The user chose: accepted. Approval applies only to the described action." } else { "The user chose: rejected. Do not perform the protected action." };
            turns.push(json!({"id": id("u"), "role": "user", "timestamp": at, "origin": "automation", "trace_id": trace,
                "text": resolved_prompt(i, &format!("Deploy build {i} to staging? {}", &LOREM[..20 + size * 15]), outcome)}));
            turns.push(reply(format!("Noted {i}.")));
        }
        _ => {
            turns.push(user(format!("Follow-up {i}: {}", &LOREM[..30 + size * 20])));
            turns.push(reply(format!("### Mixed {i}\n\n{}\n\n> A short quote.\n\n```sh\ncargo build\ncargo test\n```\n\n1. one\n2. two\n3. {}", LOREM.repeat(size), LOREM)));
        }
    }
}

/// `count` turns of `session`, at most `most` rows.
fn chat(session: &str, count: usize, most: usize) -> Value {
    let mut turns = Vec::new();
    for i in 0..count {
        turn(session, i, &mut turns);
    }
    turns.truncate(most);
    json!({"session": session, "turns": turns})
}

/// One sample: the thumb, the list's offset and the chat's rows (an
/// older page landing above the reader moves the thumb down, rightly).
#[derive(Debug, Clone, Copy)]
struct Sample {
    thumb: Thumb,
    offset: f32,
    rows: usize,
    /// The reader's place by the rows drawn: the measured height above the
    /// topmost row on screen less how far up it is (-1 when unknown).
    truth: f32,
}

/// The reader's place by the rows drawn (see `Sample::truth`).
fn truth(window: &crate::AppWindow) -> f32 {
    use i_slint_backend_testing::ElementQuery;
    let (Some((_, top, _, height)), before) = (viewport(), super::view().before) else { return -1.0 };
    let rows = rows(window);
    let mut drawn: Vec<(String, f32, f32)> = ElementQuery::from_root(window)
        .match_predicate(|e| e.accessible_id().is_some_and(|id| id.starts_with("row:")))
        .find_all()
        .into_iter()
        .filter_map(|e| {
            let (p, s, id) = (e.absolute_position(), e.size(), e.accessible_id()?);
            (s.height > 0.0 && p.y + s.height + 7.0 > top && p.y < top + height).then(|| (id.trim_start_matches("row:").to_owned(), p.y - 7.0, s.height + 14.0))
        })
        .collect();
    drawn.sort_by(|a, b| a.1.total_cmp(&b.1));
    let Some((id, row_top, _)) = drawn.into_iter().find(|r| r.1 + r.2 > top) else { return -1.0 };
    let Some(index) = rows.iter().position(|r| r.id == id.as_str()) else { return -1.0 };
    slint::Model::row_data(&before, index).map_or(-1.0, |start| start + (top - row_top))
}

#[derive(Default)]
struct Pass {
    samples: Vec<Sample>,
    last_offset: Option<f32>,
    last_rows: usize,
    still: usize,
    acts: usize,
    done: bool,
    since: Option<Instant>,
}

/// A pass through the chat: act (a wheel notch, a key), wait until the
/// offset holds for two polls with no row waiting to be measured, sample
/// the thumb, and again, until the offset no longer moves. `down`: the direction the reader goes.
fn pass(state: Rc<RefCell<Pass>>, label: &'static str, down: bool, by_key: bool, first: bool) -> Vec<Stage> {
    let mut stages: Vec<Stage> = Vec::new();
    // A stage times out after 15 s: the pass spans several.
    for part in 0..10 {
        let state = state.clone();
        stages.push((label, Box::new(move |app, window, elapsed| {
            let mut st = state.borrow_mut();
            if st.done {
                if part == 9 {
                    verdict(label, &st.samples, down, first);
                    *st = Pass::default();
                }
                return true;
            }
            if elapsed > Duration::from_secs(12) {
                return true;
            }
            let offset = report().offset;
            let count = rows(window).len();
            // A step is read once the rows that landed in it are measured:
            // their heights move the thumb in the step they landed in, not
            // in the next one, however long measuring takes.
            let measuring = app.measured_height("").1 .1 > 0;
            if st.last_offset.is_some_and(|o| (o - offset).abs() < 0.5) && count == st.last_rows && !measuring {
                st.still += 1;
            } else {
                st.still = 0;
            }
            st.last_offset = Some(offset);
            st.last_rows = count;
            if st.still < 2 {
                return false;
            }
            if let Some(thumb) = thumb() {
                st.samples.push(Sample { thumb, offset, rows: rows(window).len(), truth: truth(window) });
            }
            let moved = st.samples.len() >= 2 && (st.samples[st.samples.len() - 2].offset - offset).abs() >= 0.5;
            // Going up, an older page may still land at the top: hold longer.
            if st.acts > 0 && !moved && st.still >= if down { 4 } else { 10 } || st.acts > 400 {
                st.done = true;
                return false;
            }
            if st.since.is_none() {
                st.since = Some(Instant::now());
            }
            if moved || st.acts == 0 || st.still == 2 {
                st.acts += 1;
                match (by_key, down) {
                    (true, true) => headless::press(slint::platform::Key::PageDown),
                    (true, false) => headless::press(slint::platform::Key::PageUp),
                    (false, true) => wheel(-1200.0),
                    (false, false) => wheel(1200.0),
                }
            }
            false
        })));
    }
    stages
}

fn verdict(label: &str, samples: &[Sample], down: bool, first: bool) {
    // Steps over which no rows came (no older page landed).
    let steady: Vec<&[Sample]> = samples.windows(2).filter(|w| w[0].rows == w[1].rows).collect();
    let pages = samples.windows(2).filter(|w| w[0].rows != w[1].rows).count();
    let last_rows = samples.last().map_or(0, |s| s.rows);
    let heights: Vec<f32> = samples.iter().filter(|s| s.rows == last_rows).map(|s| s.thumb.height).collect();
    let (min, max) = heights.iter().fold((f32::MAX, f32::MIN), |(a, b), h| (a.min(*h), b.max(*h)));
    let changes = steady.iter().filter(|w| (w[0].thumb.height - w[1].thumb.height).abs() > 1.0).count();
    let back = |w: &&[Sample]| if down { w[0].thumb.top - w[1].thumb.top } else { w[1].thumb.top - w[0].thumb.top };
    let most_back = steady.iter().map(back).fold(0.0f32, f32::max);
    let backwards: Vec<String> = steady
        .iter()
        .enumerate()
        .filter(|(_, w)| back(*w) > 1.0)
        .map(|(n, w)| format!("step {}: {:.1} → {:.1} (offset {:.0} → {:.0}, read {:.0} → {:.0})", n + 1, w[0].thumb.top, w[1].thumb.top, w[0].offset, w[1].offset, w[0].truth, w[1].truth))
        .collect();
    let read_back = steady.iter().filter(|w| w[0].truth >= 0.0 && w[1].truth >= 0.0 && if down { w[1].truth < w[0].truth - 1.0 } else { w[1].truth > w[0].truth + 1.0 }).count();
    let trace: Vec<String> = samples.iter().step_by((samples.len() / 12).max(1)).map(|s| format!("{:.0}/{:.0}", s.thumb.top, s.thumb.height)).collect();
    println!("perf scrollbar {label}: {} steps ({pages} with an older page landing, {last_rows} rows at the end), thumb height {min:.1}..{max:.1}px, changed {changes} times, {} steps backwards (the rows drawn went back {read_back} times); top/height: {}",
        samples.len(), backwards.len(), trace.join(" "));
    check(samples.len() >= 10, &format!("{label}: the pass took {} steps", samples.len()));
    let listed = backwards.iter().take(6).cloned().collect::<Vec<_>>().join(", ");
    if first {
        // The first pass brings the older pages; each is measured in the
        // moments after it lands, and the place of the rows above the
        // reader settles by a few pixels.
        check(most_back <= 12.0, &format!("{label}: while older pages are measured the thumb never moves back more than a few px (at most {most_back:.1}px; {} steps back: {listed})", backwards.len()));
    } else {
        check(max - min <= 3.0, &format!("{label}: the thumb keeps its size (height {min:.1}..{max:.1}px, changed {changes} times in {} steps)", samples.len()));
        check(backwards.is_empty(), &format!("{label}: the thumb moves with the reader, never back ({} steps backwards{}{listed})",
            backwards.len(), if backwards.is_empty() { "" } else { ": " }));
    }
    if let Some(last) = samples.last() {
        let place = last.thumb.place();
        let ok = if down { (last.thumb.top + last.thumb.height - last.thumb.track_bottom).abs() <= 1.5 } else { (last.thumb.top - last.thumb.track_top).abs() <= 1.5 };
        check(ok, &format!("{label}: at the {} the thumb is at the {} of its track (place {place:.3}, thumb {:.1}..{:.1}, track {:.1}..{:.1})",
            if down { "end" } else { "top" }, if down { "bottom" } else { "top" },
            last.thumb.top, last.thumb.top + last.thumb.height, last.thumb.track_top, last.thumb.track_bottom));
    }
}

fn pointer(kind: &str, x: f32, y: f32) {
    use slint::platform::{PointerEventButton, WindowEvent};
    let Some(window) = crate::window() else { return };
    let position = slint::LogicalPosition::new(x, y);
    let window = window.window();
    window.dispatch_event(WindowEvent::PointerMoved { position });
    match kind {
        "press" => window.dispatch_event(WindowEvent::PointerPressed { position, button: PointerEventButton::Left }),
        "release" => window.dispatch_event(WindowEvent::PointerReleased { position, button: PointerEventButton::Left }),
        _ => {}
    }
}

/// The thumb dragged from its middle to `to` (0 the track's top, 1 its
/// end, beyond 1 past it) in steps, one a poll; then it is checked.
fn drag(label: &'static str, to: f32, done: impl Fn(&Thumb, f32) + 'static) -> Stage {
    let state = Rc::new(RefCell::new((0usize, 0.0f32, 0.0f32, 0.0f32)));
    (label, Box::new(move |_, _, _| {
        let mut st = state.borrow_mut();
        let (Some(thumb), Some((left, _, width, _))) = (thumb(), viewport()) else {
            check(false, &format!("{label}: the thumb is drawn"));
            return true;
        };
        let x = left + width - 6.0;
        if st.0 == 0 {
            let from = thumb.top + thumb.height / 2.0;
            let travel = thumb.track_bottom - thumb.track_top - thumb.height;
            let target = thumb.track_top + thumb.height / 2.0 + to * travel;
            *st = (1, from, target, 0.0);
            pointer("press", x, from);
            return false;
        }
        let steps = 12;
        if st.0 <= steps {
            let y = st.1 + (st.2 - st.1) * st.0 as f32 / steps as f32;
            pointer("move", x, y);
            st.3 = y;
            st.0 += 1;
            return false;
        }
        // Held still a moment, then let go.
        st.0 += 1;
        if st.0 < steps + 6 {
            pointer("move", x, st.3);
            return false;
        }
        done(&thumb, st.3);
        pointer("release", x, st.3);
        pointer("move", x - 300.0, st.3);
        *st = (0, 0.0, 0.0, 0.0);
        true
    }))
}

/// `--check scrollbar --out DIR`.
pub(super) fn scrollbar_check(out: String) {
    let state = Rc::new(RefCell::new(Pass::default()));
    let (out1, out2) = (out.clone(), out);
    let count = Rc::new(RefCell::new(0usize));
    let mut stages: Vec<Stage> = vec![
        ("live", Box::new(|app, _, _| {
            if app.engine.borrow().connection_state() != "live" {
                return false;
            }
            check(control("/__control/add-agent", &json!({"session": SESSION, "persona": "Steady"})).is_ok(), "the Host takes a new chat");
            check(control("/__control/add-agent", &json!({"session": SHORT, "persona": "Short"})).is_ok(), "and another");
            true
        })),
        ("short chat", Box::new(|app, _, _| {
            if app.engine.borrow().roster().find(SHORT).is_none() {
                return false;
            }
            check(control("/__control/turns", &chat(SHORT, 60, SHORT_ROWS)).is_ok(), &format!("the Host takes a chat of {SHORT_ROWS} rows"));
            app.engine.borrow_mut().select(SHORT);
            crate::pump();
            true
        })),
        ("short chat opened", Box::new(|app, window, elapsed| {
            let (_, (_, waiting)) = app.measured_height("");
            if (rows(window).len() < 90 || waiting > 0) && elapsed < Duration::from_secs(5) || elapsed < Duration::from_millis(1500) {
                return false;
            }
            check(report().follows && report().at_end, &format!("the chat of {} rows opens at its end", rows(window).len()));
            let thumb = thumb();
            check(thumb.is_some_and(|t| t.height > 26.0), &format!("its thumb is bigger than the smallest: {thumb:?}"));
            true
        })),
    ];
    stages.extend(pass(state.clone(), "short chat: first pass up by wheel", false, false, true));
    stages.extend(pass(state.clone(), "short chat: down by wheel", true, false, false));
    stages.push(("short chat: focus", Box::new(|app, _, _| {
        app.focus_transcript();
        true
    })));
    stages.extend(pass(state.clone(), "short chat: up by Page Up", false, true, false));
    stages.extend(pass(state.clone(), "short chat: down by Page Down", true, true, false));
    let long: Vec<Stage> = vec![
        ("fill", Box::new({
            let count = count.clone();
            move |app, _, _| {
                if app.engine.borrow().roster().find(SESSION).is_none() {
                    return false;
                }
                let body = chat(SESSION, TURNS, usize::MAX);
                *count.borrow_mut() = body["turns"].as_array().map_or(0, Vec::len);
                check(control("/__control/turns", &body).is_ok(), &format!("the Host takes a long chat of {} rows", count.borrow()));
                app.engine.borrow_mut().select(SESSION);
                crate::pump();
                true
            }
        })),
        ("opened", Box::new({
            let count = count.clone();
            move |_, window, elapsed| {
                let shown = rows(window).len();
                if shown < 300 && elapsed < Duration::from_secs(4) || elapsed < Duration::from_millis(1500) {
                    return false;
                }
                println!("perf scrollbar: the chat opens with {shown} rows (of the Host's {}), older pages as the reader reaches the top", count.borrow());
                check(report().follows && report().at_end, &format!("the chat opens at its end (follows {}, at end {})", report().follows, report().at_end));
                let thumb = thumb();
                check(thumb.is_some(), &format!("the chat shows a scrollbar thumb: {thumb:?}"));
                shot(&out1, "scrollbar-01-opened");
                true
            }
        })),
        // Every row is measured off screen soon after the chat opens.
        ("measured", Box::new(|app, _, elapsed| {
            let (_, (measured, waiting)) = app.measured_height("");
            if waiting > 0 && elapsed < Duration::from_secs(10) {
                return false;
            }
            println!("perf scrollbar: {measured} rows measured off screen in {:?} after the chat opened", app.measuring_took());
            check(waiting == 0 && measured > 0, &format!("every row is measured ({measured} measured, {waiting} waiting)"));
            let misfits = misfits();
            check(misfits.is_empty(), &format!("each drawn row is as tall as measured off screen ({} differ: {})", misfits.len(), misfits.join("; ")));
            true
        })),
    ];
    stages.extend(long);
    stages.extend(pass(state.clone(), "first pass up by wheel", false, false, true));
    stages.push(("all pages", Box::new(|app, window, elapsed| {
        let (_, (measured, waiting)) = app.measured_height("");
        if waiting > 0 && elapsed < Duration::from_secs(10) {
            return false;
        }
        let shown = rows(window).len();
        check(shown >= 300, &format!("reading up to the top brought the whole chat: {shown} rows"));
        check(waiting == 0 && measured == shown, &format!("and every row of it is measured ({measured} measured, {waiting} waiting)"));
        true
    })));
    stages.extend(pass(state.clone(), "down by wheel", true, false, false));
    stages.push(("focus", Box::new(|app, _, _| {
        app.focus_transcript();
        true
    })));
    stages.extend(pass(state.clone(), "up by Page Up", false, true, false));
    stages.extend(pass(state.clone(), "down by Page Down", true, true, false));
    stages.push(("end", Box::new(move |_, _, elapsed| {
        if elapsed < Duration::from_millis(500) {
            return false;
        }
        let (visible, numbers) = super::scroll_checks::last_row_visible();
        check(visible, &format!("Page Down reaches the true end: {numbers}"));
        let misfits = misfits();
        check(misfits.is_empty(), &format!("after reading the whole chat each drawn row is as tall as the book has it ({} differ: {})", misfits.len(), misfits.join("; ")));
        shot(&out2, "scrollbar-02-end");
        true
    })));
    // The thumb is dragged: it stays under the pointer, and the chat moves.
    stages.push(drag("dragging the thumb up the chat", 0.4, |thumb, pointer| {
        let middle = thumb.top + thumb.height / 2.0;
        check((middle - pointer).abs() <= 4.0, &format!("a dragged thumb stays under the pointer (its middle {middle:.1}, the pointer {pointer:.1}, place {:.3})", thumb.place()));
        check(!report().follows && !report().at_end, &format!("dragging the thumb up leaves the end (follows {}, at end {})", report().follows, report().at_end));
    }));
    stages.push(drag("dragging the thumb to the top", -0.2, |thumb, _| {
        check((thumb.top - thumb.track_top).abs() <= 1.5, &format!("a thumb dragged past the top stays at the top of its track ({:.1}, track from {:.1})", thumb.top, thumb.track_top));
        check(report().offset > -2.0, &format!("and the chat shows its top (offset {:.1})", report().offset));
    }));
    stages.push(drag("dragging the thumb to the end", 1.3, |thumb, _| {
        check((thumb.top + thumb.height - thumb.track_bottom).abs() <= 1.5, &format!("a thumb dragged past the end stays at the end of its track ({:.1}, track to {:.1})", thumb.top + thumb.height, thumb.track_bottom));
    }));
    stages.push(("dragged to the end", Box::new(|_, _, elapsed| {
        if elapsed < Duration::from_millis(800) {
            return false;
        }
        let (visible, numbers) = super::scroll_checks::last_row_visible();
        check(visible && report().follows, &format!("a thumb dragged to the end shows the last row and follows again (follows {}): {numbers}", report().follows));
        true
    })));
    // The wheel turned over the scrollbar scrolls the chat.
    stages.push(("wheel over the bar", Box::new({
        let start = Rc::new(RefCell::new(None::<f32>));
        move |_, _, elapsed| {
            let Some((left, top, width, height)) = viewport() else { return true };
            if start.borrow().is_none() {
                *start.borrow_mut() = Some(report().offset);
                headless::wheel(left + width - 5.0, top + height / 2.0, 240.0);
                return false;
            }
            if elapsed < Duration::from_millis(700) {
                return false;
            }
            let from = start.borrow().unwrap_or_default();
            check(report().offset - from > 100.0, &format!("the wheel over the scrollbar scrolls the chat ({from:.1} → {:.1})", report().offset));
            true
        }
    })));
    // A follower at the end while the chat grows past a thousand rows: it
    // stays at the end, and every row is measured.
    stages.push(("follow", Box::new(|app, _, _| {
        app.to_latest();
        true
    })));
    stages.push(("a thousand rows", Box::new({
        let mut since: Option<Instant> = None;
        let mut measured_at: Option<u128> = None;
        move |app, window, elapsed| {
            let Some(added) = since else {
                if elapsed < Duration::from_millis(500) {
                    return false;
                }
                let mut turns = Vec::new();
                for i in TURNS..TURNS + 200 {
                    turn(SESSION, i, &mut turns);
                }
                check(control("/__control/upsert", &json!({"session": SESSION, "turns": turns})).is_ok(), &format!("the Host adds {} rows", turns.len()));
                since = Some(Instant::now());
                return false;
            };
            let shown = rows(window).len();
            let (_, (measured, waiting)) = app.measured_height("");
            // (The book counts the rows as of its last update.)
            if (shown < 1000 || waiting > 0 || measured != shown) && elapsed < Duration::from_secs(12) {
                return false;
            }
            let took = *measured_at.get_or_insert_with(|| added.elapsed().as_millis());
            println!("perf scrollbar: the last measuring pass took {:?}", app.measuring_took());
            // The follower is pinned to the end on the next frames.
            if !super::scroll_checks::last_row_visible().0 && elapsed < Duration::from_secs(14) {
                return false;
            }
            // What each change to the chat's rows costs the book.
            let start = Instant::now();
            for _ in 0..20 {
                app.measure_rows();
            }
            let update = start.elapsed().as_micros() / 20;
            println!("perf scrollbar: {shown} rows shown and measured within {took} ms of the Host adding them; one update of the book over them takes {update} µs (debug build)");
            check(shown >= 1000 && waiting == 0 && measured == shown, &format!("a chat of {shown} rows is measured ({measured} measured, {waiting} waiting)"));
            let (visible, numbers) = super::scroll_checks::last_row_visible();
            check(visible && report().follows, &format!("a follower stays at the end as the rows arrive (follows {}): {numbers}", report().follows));
            let thumb = thumb();
            check(thumb.is_some_and(|t| (t.top + t.height - t.track_bottom).abs() <= 1.5), &format!("and the thumb is at the end of its track: {thumb:?}"));
            true
        }
    })));
    run_stages(stages);
}
