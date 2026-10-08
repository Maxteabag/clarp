//! `--check live`: live items in the window (docs/live-items.md §7), driven
//! by the fake Host replaying the recorded `turn-full` stream, by keyboard.
//! Without `live_items` the chat keeps to `/log`; with it one status line
//! names what runs with an elapsed time from the Host's clock and the
//! interrupt key, item rows update in place (labels, Thought for Ns,
//! Explored N, tails, +N −M) without moving a reader who scrolled up,
//! J/K and O reach and open them, the settled turn folds, a durable `/log`
//! row takes over its items in place, and a gap recovers from GET /live.
//! The status line is hidden unless Settings show it: the working row's
//! label shimmers instead, without moving the chat, and only while it is on
//! screen, the window shows and motion is not reduced.

use std::cell::RefCell;
use std::rc::Rc;
use std::time::Duration;

use serde_json::json;
use slint::{ComponentHandle, Model};
use slint::platform::Key;

use super::scroll_checks::{anchor_now, anchor_moved, last_row_visible, place_of, row_height, wheel_up};
use super::{Stage, app_now, check, control, posts, quiet, report, requests, rows, run_stages, shot, view};
use crate::headless;

/// Step indices in turn-full.json: replay up to (not including) them.
const AFTER_THINKING_TITLE: usize = 5; // lseq 4
const AFTER_FIRST_CHUNK: usize = 8; // lseq 7
const AFTER_SEARCH: usize = 11; // lseq 10
const AFTER_COMMAND_START: usize = 14; // lseq 13
const AFTER_EXPLAIN: usize = 15; // lseq 14
const AFTER_TAIL: usize = 17; // lseq 16
const AFTER_FAILED: usize = 18; // lseq 17
const AFTER_EDIT: usize = 20; // lseq 19
const AFTER_INTERRUPT: usize = 24; // lseq 23

fn replay(through: usize) -> bool {
    let sent = control("/__control/live-replay", &json!({"session": "rachel", "fixture": "turn-full", "through": through}));
    if let Err(error) = &sent {
        check(false, &format!("the Host replays the stream: {error}"));
    }
    sent.is_ok()
}

fn lseq() -> Option<i64> {
    app_now().engine.borrow().live_view("rachel").and_then(|v| v.lseq())
}

fn live_rows() -> Vec<crate::MessageRow> {
    crate::window().map(|w| rows(&w)).unwrap_or_default().into_iter().filter(|r| !r.live.key.is_empty()).collect()
}

fn live_row(key: &str) -> Option<crate::MessageRow> {
    live_rows().into_iter().find(|r| r.live.key == key)
}

const REAL_FAILED: &str = "cl:toolu_01RL4aA1EqQJ43Y7Ay7wuYXt";
const REAL_ANSWER_ROW: &str = "live-783d39d132789c798a9a";
const REAL_FOLD: &str = "live:fold:537555728357bf76";
const REAL_RUNNING: &str = "live:cl:toolu_017NGGuPh5DkHZyPKu7aJyz5";
const REAL_RUNNING_ROW: &str = "msg-e60c25b9161ac8b660ca";
const REAL_PREVIOUS_ANSWER: &str = "live-567165763b2310817088";
const REAL_NEXT_PROMPT: &str = "u-clarp-admin-3c48398b85b08a09";

/// The live rows of the real turn (the probe chat's, once it shows).
fn probe_titles() -> Vec<String> {
    if crate::app().is_none_or(|app| app.engine.borrow().selected_session() != "probe") {
        return Vec::new();
    }
    titles()
}

fn titles() -> Vec<String> {
    live_rows().iter().map(|r| r.live.title.to_string()).collect()
}

fn lines(row: &crate::MessageRow) -> Vec<String> {
    row.live.lines.iter().map(|l| l.to_string()).collect()
}

fn status() -> String {
    view().live_status.to_string()
}

/// A message row's text as revealed so far.
fn message_text(row: &crate::MessageRow) -> String {
    row.live.text.trim_end().to_owned()
}

fn events_queries() -> Vec<serde_json::Value> {
    requests("GET", "/events").into_iter().map(|r| r["query"].clone()).collect()
}

/// A drawn element whose accessible id is `id`.
fn drawn(id: &str) -> bool {
    use i_slint_backend_testing::ElementQuery;
    let Some(window) = crate::window() else { return false };
    let id = id.to_owned();
    ElementQuery::from_root(&window)
        .match_predicate(move |e| e.accessible_id().is_some_and(|a| a == id.as_str()) && e.size().width > 0.0 && e.size().height > 0.0)
        .find_first()
        .is_some()
}

/// Whether row `key`'s label is drawn shimmering.
fn shimmering(key: &str) -> bool {
    drawn(&format!("shimmer:{key}"))
}

/// Any label drawn shimmering.
fn any_shimmering() -> bool {
    use i_slint_backend_testing::ElementQuery;
    let Some(window) = crate::window() else { return false };
    ElementQuery::from_root(&window).match_predicate(|e| e.accessible_id().is_some_and(|a| a.starts_with("shimmer:"))).find_first().is_some()
}

/// Whether the active pane's status line is drawn.
fn status_line_drawn() -> bool {
    drawn(&format!("live-status:{}", view().id))
}

fn set_status_line(on: bool) {
    let app = app_now();
    if crate::profile_view::live_status_line(&app) != on {
        crate::settings_view::change(&app, &crate::window().expect("window"), "live-status-line", 1);
    }
}

pub(super) fn set_reduced_motion(on: bool) {
    let app = app_now();
    if crate::profile_view::reduced_motion(&app) != on {
        crate::settings_view::change(&app, &crate::window().expect("window"), "reduced-motion", 1);
    }
}

/// The process's CPU time so far (user and system).
fn cpu_time() -> Duration {
    // SAFETY: getrusage fills the zeroed struct it is given.
    let mut usage: libc::rusage = unsafe { std::mem::zeroed() };
    if unsafe { libc::getrusage(libc::RUSAGE_SELF, &mut usage) } != 0 {
        check(false, &format!("getrusage: {}", std::io::Error::last_os_error()));
        return Duration::ZERO;
    }
    let time = |t: libc::timeval| Duration::from_secs(t.tv_sec as u64) + Duration::from_micros(t.tv_usec as u64);
    time(usage.ru_utime) + time(usage.ru_stime)
}

/// What a running tool's clock alone redraws, at most: its elapsed time and
/// the hidden status line change twice a second. The shimmer draws 15–30.
pub(super) const NO_SHIMMER_FPS: f64 = 6.0;

thread_local! {
    /// Frames per second and CPU % the window takes idle (no turn).
    static IDLE: std::cell::Cell<(f64, f64)> = const { std::cell::Cell::new((0.0, 0.0)) };
    /// Frames drawn, CPU time and when, at the start of a measurement.
    static MARK: std::cell::Cell<(usize, Duration, Option<std::time::Instant>)> = const { std::cell::Cell::new((0, Duration::ZERO, None)) };
}

/// Starts counting the frames the app draws by itself (the stages stop
/// asking for them until `since_mark`).
pub(super) fn mark() {
    quiet(true);
    MARK.with(|m| m.set((crate::perf::stats().frames.len(), cpu_time(), Some(std::time::Instant::now()))));
}

/// Frames per second and CPU (% of a core) since `mark`, and over how long.
pub(super) fn since_mark() -> (f64, f64, Duration) {
    quiet(false);
    let (frames, cpu, at) = MARK.with(std::cell::Cell::get);
    let elapsed = at.map(|a| a.elapsed()).unwrap_or_default();
    let seconds = elapsed.as_secs_f64().max(0.001);
    let drawn = (crate::perf::stats().frames.len() - frames) as f64;
    (drawn / seconds, (cpu_time() - cpu).as_secs_f64() * 100.0 / seconds, elapsed)
}

pub fn live_check(out: String) {
    let anchor: Rc<RefCell<Option<(String, f32)>>> = Rc::default();
    let height: Rc<RefCell<f32>> = Rc::default();
    let rows_before: Rc<RefCell<(usize, usize)>> = Rc::default();
    let (out1, out2, out3, out4, out5, out6) = (out.clone(), out.clone(), out.clone(), out.clone(), out.clone(), out.clone());
    let (out7, out8, out9, out10) = (out.clone(), out.clone(), out.clone(), out.clone());
    let (out11, out12, out13) = (out.clone(), out.clone(), out.clone());
    let (out14, out15, out16) = (out.clone(), out.clone(), out.clone());
    let retiring: Rc<RefCell<((usize, usize), Option<(String, f32)>)>> = Rc::default();
    let (retiring1, retiring2) = (retiring.clone(), retiring.clone());
    let landing: Rc<RefCell<((usize, usize), Option<(String, f32)>)>> = Rc::default();
    let (landing1, landing2) = (landing.clone(), landing.clone());
    let running_at: Rc<RefCell<usize>> = Rc::default();
    let (running_at1, running_at2) = (running_at.clone(), running_at.clone());
    let (before3, before4) = (rows_before.clone(), rows_before.clone());
    let (anchor1, anchor2) = (anchor.clone(), anchor.clone());
    let (height1, height2, height3) = (height.clone(), height.clone(), height.clone());
    let (before1, before2) = (rows_before.clone(), rows_before.clone());
    let stale: Rc<RefCell<(usize, usize)>> = Rc::default();
    let (stale1, stale2) = (stale.clone(), stale.clone());
    let out17 = out.clone();
    let (out18, out19, out20, out21) = (out.clone(), out.clone(), out.clone(), out.clone());
    let place: Rc<RefCell<Option<(String, f32)>>> = Rc::default();
    let (place1, place2) = (place.clone(), place.clone());
    let theme_before: Rc<RefCell<String>> = Rc::default();
    let (theme1, theme2) = (theme_before.clone(), theme_before.clone());
    let fetched: Rc<std::cell::Cell<usize>> = Rc::default();
    let (fetched1, fetched2) = (fetched.clone(), fetched.clone());
    let stages: Vec<Stage> = vec![
        ("fallback", Box::new(|app, _window, _| {
            let open = app.engine.borrow().conversation("rachel").is_some_and(|c| !c.rows().is_empty());
            if !open {
                return false;
            }
            check(!app.engine.borrow().live_items(), "without live_items in /server-info the desktop has no live items");
            check(requests("GET", "/live").is_empty(), "and never asks GET /live");
            check(events_queries().iter().all(|q| q.get("live").is_none()), "and opens /events without ?live=");
            // A long chat, so the reader can scroll away from the live turn.
            check(control("/__control/rich", &json!({"session": "rachel", "count": 30})).is_ok(), "the chat holds 30 rows");
            true
        })),
        ("log rows", Box::new(|app, _window, _| {
            if !app.engine.borrow().conversation("rachel").is_some_and(|c| c.len() >= 30) {
                return false;
            }
            check(true, "without the feature new rows still come from /log");
            check(control("/__control/live", &json!({"on": true})).is_ok(), "the Host turns live items on");
            app.engine.borrow_mut().reconnect();
            true
        })),
        ("subscribed", Box::new(|app, _window, _| {
            let ready = app.engine.borrow().live_items() && lseq() == Some(0);
            if !ready || !events_queries().iter().any(|q| q.get("live").and_then(|l| l.as_str()).is_some_and(|l| l.split(',').any(|s| s == "rachel"))) {
                return false;
            }
            check(requests("GET", "/live").iter().any(|r| r["query"]["session"] == "rachel"), "opening the chat asks GET /live?session=rachel");
            check(status().is_empty(), &format!("no status line while idle: {:?}", status()));
            check(!crate::profile_view::live_status_line(app), "the live status line is off by default");
            check(crate::settings_view::rows(app).iter().any(|r| r.id == "live-status-line" && r.label == "Live status line" && !r.on), "Settings list Live status line, off");
            true
        })),
        ("settled", Box::new(move |_, _window, elapsed| {
            if elapsed < Duration::from_secs(2) {
                return false;
            }
            mark();
            true
        })),
        ("idle", Box::new(move |_, _window, elapsed| {
            if elapsed < Duration::from_millis(1500) {
                return false;
            }
            let (fps, cpu, over) = since_mark();
            IDLE.with(|i| i.set((fps, cpu)));
            // The window's own baseline (the shimmer's bounds are relative to it).
            check(fps <= NO_SHIMMER_FPS, &format!("idle, the window draws little: {fps:.1} frames/s, CPU {cpu:.0}% of a core (debug build) over {over:.1?}"));
            replay(AFTER_THINKING_TITLE)
        })),
        ("thinking", Box::new(move |_, _window, elapsed| {
            if lseq() != Some(4) || elapsed < Duration::from_millis(400) {
                return false;
            }
            let line = status();
            check(line.starts_with("◌ Thinking: Finding the flaky test · 0:0"), &format!("the status line names the reasoning title, timed from turn_started_ms: {line:?}"));
            check(view().live_busy, "the status line says the agent works");
            check(view().live_stop_key == "Ctrl+.", &format!("it names the interrupt key: {:?}", view().live_stop_key));
            check(titles() == ["Thinking: Finding the flaky test"], &format!("one reasoning row: {:?}", titles()));
            check(!status_line_drawn(), "the status line is not drawn by default");
            let thinking = live_rows().into_iter().next().unwrap_or_default();
            check(thinking.live.shimmer == "muted", &format!("the reasoning row shimmers muted: {:?}", thinking.live.shimmer));
            check(shimmering(&thinking.live.key), "and its label is drawn shimmering");
            check(!view().working, "no typing dots under a working row");
            shot(&out1, "live-01-thinking");
            set_status_line(true);
            true
        })),
        ("status line on", Box::new(move |_, _window, elapsed| {
            if !status_line_drawn() {
                return elapsed > Duration::from_secs(3) && {
                    check(false, "Settings bring the status line back");
                    true
                };
            }
            check(true, "Settings bring the status line back");
            check(crate::settings_view::rows(app_now().as_ref()).iter().any(|r| r.id == "live-status-line" && r.on), "Settings show it on");
            shot(&out18, "live-01-status-line");
            set_status_line(false);
            true
        })),
        ("status line off", Box::new(move |_, _window, _| {
            if status_line_drawn() {
                return false;
            }
            check(true, "and off again hides it");
            replay(AFTER_FIRST_CHUNK)
        })),
        ("streaming", Box::new(move |_, _window, elapsed| {
            if lseq() != Some(7) || elapsed < Duration::from_millis(700) {
                return false;
            }
            check(status().starts_with("◌ Responding · 0:0"), &format!("responding: {:?}", status()));
            replay(AFTER_SEARCH)
        })),
        ("exploring", Box::new(move |_, _window, _| {
            if lseq() != Some(10) {
                return false;
            }
            let message = live_row("live:cl:msg_01:1");
            if message.as_ref().is_none_or(|m| message_text(m) != "Let me look at the parser and its tests.") {
                return false;
            }
            let line = status();
            check(line.starts_with("● Searching tokenize · 0:0") && line.ends_with(" +1"), &format!("a running tool with its elapsed time and +1 for the parallel one: {line:?}"));
            check(titles() == ["Thought for 4s: Finding the flaky test", "", "Exploring"], &format!("Thought for 4s, the commentary, one explore row: {:?}", titles()));
            let frames = crate::live_view::PACER.with(|p| p.borrow().frames("live:cl:msg_01:1"));
            check(frames >= 3, &format!("the commentary was revealed in word steps, not dropped in whole: {frames} frames"));
            replay(AFTER_COMMAND_START)
        })),
        ("command", Box::new(move |_, _window, elapsed| {
            if lseq() != Some(13) || elapsed < Duration::from_millis(800) {
                return false;
            }
            *height1.borrow_mut() = row_height("live:cl:toolu_03");
            check(*height1.borrow() > 0.0, "the command's row is drawn at the end");
            check(titles().last().map(String::as_str) == Some("Running npm test"), &format!("the command's row: {:?}", titles()));
            check(titles().contains(&"Explored 1 file, 1 search".to_owned()), &format!("the group settled: {:?}", titles()));
            let command = live_row("live:cl:toolu_03").unwrap_or_default();
            check(!command.live.explaining && command.live.secondary.is_empty(), "no explanation requested yet (explain: null): no Explaining…");
            shot(&out7, "live-00-explaining");
            let bar: Vec<String> = crate::window().map(|w| w.get_hints().iter().map(|h| format!("{} {}", h.keys, h.label)).collect()).unwrap_or_default();
            check(bar.iter().any(|h| h == "Ctrl+. Stop"), &format!("with the status line hidden, the shortcut bar names the stop key: {bar:?}"));
            check(command.live.shimmer == "accent", &format!("the running command shimmers in the accent: {:?}", command.live.shimmer));
            check(titles().contains(&"Explored 1 file, 1 search".to_owned()) && live_rows().iter().filter(|r| r.live.title.starts_with("Explored")).all(|r| r.live.shimmer.is_empty()), "the settled explore group does not");
            *place1.borrow_mut() = place_of("live:cl:toolu_03");
            mark();
            true
        })),
        ("shimmering", Box::new(move |_, _window, elapsed| {
            if elapsed < Duration::from_millis(1200) {
                return false;
            }
            check(shimmering("live:cl:toolu_03"), "the running command's label is drawn shimmering");
            let (fps, cpu, over) = since_mark();
            let (idle, idle_cpu) = IDLE.with(std::cell::Cell::get);
            check(fps >= (idle + 8.0).max(2.0 * NO_SHIMMER_FPS), &format!("the shimmer animates: {fps:.0} frames/s (idle {idle:.1}), CPU {cpu:.0}% (idle {idle_cpu:.0}%) over {over:.1?}"));
            let height = row_height("live:cl:toolu_03");
            check((height - *height3.borrow()).abs() < 0.5, &format!("shimmering keeps the row's height: {} → {height}", height3.borrow()));
            let (before, now) = (place2.borrow().clone(), place_of("live:cl:toolu_03"));
            let kept = before.as_ref().zip(now.as_ref()).is_some_and(|(b, n)| (b.1 - n.1).abs() <= 1.0);
            check(kept, &format!("and its place in the chat: {before:?} → {now:?}"));
            shot(&out19, "live-07-shimmer-terminal");
            *theme1.borrow_mut() = app_now().engine.borrow().reading_theme();
            app_now().engine.borrow_mut().set_reading_theme("word");
            crate::pump_now(&app_now());
            true
        })),
        ("shimmer word", Box::new(move |_, _window, elapsed| {
            if elapsed < Duration::from_millis(500) {
                return false;
            }
            check(shimmering("live:cl:toolu_03"), "it shimmers in Word too");
            shot(&out20, "live-08-shimmer-word");
            app_now().engine.borrow_mut().set_reading_theme("hacker");
            crate::pump_now(&app_now());
            true
        })),
        ("shimmer hacker", Box::new(move |_, _window, elapsed| {
            if elapsed < Duration::from_millis(500) {
                return false;
            }
            check(shimmering("live:cl:toolu_03"), "and in Hacker");
            shot(&out21, "live-09-shimmer-hacker");
            let before = theme2.borrow().clone();
            app_now().engine.borrow_mut().set_reading_theme(&before);
            crate::pump_now(&app_now());
            set_reduced_motion(true);
            true
        })),
        ("reduced motion", Box::new(move |app, _window, elapsed| {
            // The theme put back re-measures every row (a frame a batch):
            // what Reduce Motion draws is counted once that is done.
            let measuring = app.measured_height("").1 .1 > 0;
            if elapsed < Duration::from_millis(300) || measuring {
                return false;
            }
            check(!shimmering("live:cl:toolu_03"), "Reduce Motion: no shimmer");
            check(live_row("live:cl:toolu_03").is_some_and(|r| r.live.shimmer == "accent"), "the label keeps its accent, still");
            mark();
            true
        })),
        ("still", Box::new(move |_, _window, elapsed| {
            if elapsed < Duration::from_millis(1500) {
                return false;
            }
            let (fps, cpu, over) = since_mark();
            check(fps <= NO_SHIMMER_FPS, &format!("Reduce Motion draws only what changes (the clock): {fps:.1} frames/s, CPU {cpu:.0}% over {over:.1?}"));
            set_reduced_motion(false);
            crate::window().expect("window").global::<crate::ChatLook>().set_window_shown(false);
            true
        })),
        ("window hidden", Box::new(move |_, _window, elapsed| {
            if elapsed < Duration::from_millis(300) {
                return false;
            }
            check(!shimmering("live:cl:toolu_03"), "a covered window does not shimmer");
            mark();
            true
        })),
        ("hidden still", Box::new(move |_, _window, elapsed| {
            if elapsed < Duration::from_millis(1500) {
                return false;
            }
            let (fps, cpu, over) = since_mark();
            check(fps <= NO_SHIMMER_FPS, &format!("nor draws frames for it: {fps:.1} frames/s, CPU {cpu:.0}% over {over:.1?}"));
            crate::window().expect("window").global::<crate::ChatLook>().set_window_shown(true);
            true
        })),
        ("shown again", Box::new(move |_, _window, elapsed| {
            if !shimmering("live:cl:toolu_03") {
                return elapsed > Duration::from_secs(2) && {
                    check(false, "shown again, it shimmers again");
                    true
                };
            }
            check(true, "shown again, it shimmers again");
            replay(AFTER_EXPLAIN)
        })),
        ("explained", Box::new(move |_, _window, elapsed| {
            let explained = live_row("live:cl:toolu_03").is_some_and(|r| r.live.secondary == "Runs the parser tests");
            if !explained || elapsed < Duration::from_millis(600) {
                return false;
            }
            let now = row_height("live:cl:toolu_03");
            check((now - *height2.borrow()).abs() < 0.5, &format!("the explanation lands in its reserved line: {} → {now}", height2.borrow()));
            check(crate::settings_view::rows(app_now().as_ref()).iter().any(|r| r.id == "tool-explanations" && r.on), "Settings show the Host's tool explanations, on");
            // Ctrl+Shift+X turns the Host's explanations off.
            headless::press_with(&[Key::Control, Key::Shift], "x");
            true
        })),
        ("explanations off", Box::new(move |_, _window, _| {
            let command = live_row("live:cl:toolu_03").is_some_and(|r| r.live.secondary == "npm test -- parser");
            let posted = posts("/tool-explanations/settings").iter().any(|p| p["body"]["enabled"] == false);
            if !command || !posted {
                return false;
            }
            check(true, "off: the Host is told, and the row's second line is the raw command under its label");
            check(live_rows().iter().all(|r| !r.live.explaining), "off: nothing says Explaining…");
            check(live_row("live:cl:toolu_03").is_some_and(|r| r.live.title == "Running npm test"), "the first line stays the label");
            check(crate::settings_view::rows(app_now().as_ref()).iter().any(|r| r.id == "tool-explanations" && !r.on), "Settings show it off");
            let window = crate::window().expect("window");
            check(crate::commands::run(&app_now(), &window, "toggle-explanations"), "the Ctrl+K command turns it on again");
            true
        })),
        ("explanations on", Box::new(move |_, _window, _| {
            if !live_row("live:cl:toolu_03").is_some_and(|r| r.live.secondary == "Runs the parser tests") {
                return false;
            }
            check(posts("/tool-explanations/settings").last().is_some_and(|p| p["body"]["enabled"] == true), "on again at the Host");
            // The reader scrolls up into the history: what follows must not move them.
            app_now().focus_transcript();
            wheel_up(900.0);
            true
        })),
        ("reader up", Box::new(move |_, _window, elapsed| {
            if elapsed < Duration::from_millis(1200) || report().follows {
                return false;
            }
            *anchor1.borrow_mut() = anchor_now();
            check(anchor1.borrow().is_some(), "the reader's place is recorded");
            replay(AFTER_TAIL)
        })),
        ("tail", Box::new(move |_, _window, elapsed| {
            if lseq() != Some(16) || elapsed < Duration::from_millis(1500) {
                return false;
            }
            let tool = live_row("live:cl:toolu_03").unwrap_or_default();
            check(lines(&tool) == ["line 809", "line 810", "line 811", "line 812"], &format!("the running command shows its last lines: {:?}", lines(&tool)));
            check(tool.live.more == "+808 lines", &format!("and how many more: {:?}", tool.live.more));
            let line = status();
            check(line.starts_with("● Running npm test · 0:0") && !line.ends_with("0:00"), &format!("the elapsed time ticks: {line:?}"));
            let (still, detail) = anchor_moved(anchor2.borrow().as_ref().expect("anchor"));
            check(still, &format!("rows updating in place do not move a reader who scrolled up: {detail}"));
            check(!report().follows, "the reader still reads the history");
            shot(&out2, "live-02-reader-up");
            check(!any_shimmering(), "the running command, scrolled away, does not shimmer");
            mark();
            true
        })),
        ("off screen", Box::new(move |_, _window, elapsed| {
            if elapsed < Duration::from_millis(1500) {
                return false;
            }
            let (fps, cpu, over) = since_mark();
            check(fps <= NO_SHIMMER_FPS, &format!("nor draws frames for it: {fps:.1} frames/s, CPU {cpu:.0}% over {over:.1?}"));
            headless::press(Key::End);
            replay(AFTER_FAILED)
        })),
        ("failed", Box::new(move |_, _window, elapsed| {
            if lseq() != Some(17) || elapsed < Duration::from_millis(800) {
                return false;
            }
            let tool = live_row("live:cl:toolu_03").unwrap_or_default();
            check(tool.live.title == "Ran npm test" && tool.live.status == "failed", &format!("the command failed in place: {:?} {:?}", tool.live.title, tool.live.status));
            check(tool.live.meta == "exit 1 · 3.2s", &format!("its exit code and run time: {:?}", tool.live.meta));
            check(tool.live.shimmer.is_empty() && !shimmering("live:cl:toolu_03"), "settled, it stops shimmering");
            check(report().follows, "End follows again");
            let (visible, detail) = last_row_visible();
            check(visible, &format!("a follower stays at the end: {detail}"));
            replay(AFTER_EDIT)
        })),
        ("edited", Box::new(move |_, _window, elapsed| {
            if lseq() != Some(19) || elapsed < Duration::from_millis(600) {
                return false;
            }
            let edit = live_row("live:cl:toolu_04").unwrap_or_default();
            check(edit.live.title == "Edited src/tokenizer.ts" && edit.live.meta == "+3 −1", &format!("the edit with its diff stats: {:?} {:?}", edit.live.title, edit.live.meta));
            // K from the chat: the lowest item row on screen, the edit.
            app_now().focus_transcript();
            headless::press("k");
            true
        })),
        ("selected", Box::new(move |app, _window, elapsed| {
            if app.artifact_cursor.borrow().as_str() != "live:cl:toolu_04" {
                return elapsed > Duration::from_secs(3) && {
                    check(false, &format!("K selects the lowest item row: {:?}", app.artifact_cursor.borrow()));
                    true
                };
            }
            check(crate::artifacts_view::selected_hints(app).is_some_and(|h| h.iter().any(|(k, l)| k == "O" && l == "Expand")), "the selected row names its key: O Expand");
            headless::press("o");
            true
        })),
        ("expanded", Box::new(move |_, _window, _| {
            let edit = live_row("live:cl:toolu_04").unwrap_or_default();
            if !edit.live.expanded {
                return false;
            }
            check(lines(&edit).first().map(String::as_str) == Some("@@ -40,3 +40,5 @@"), &format!("O opens the diff: {:?}", lines(&edit)));
            shot(&out3, "live-03-diff-open");
            headless::press("o");
            replay(AFTER_INTERRUPT)
        })),
        ("interrupted", Box::new(move |app, _window, elapsed| {
            if lseq() != Some(23) || elapsed < Duration::from_millis(600) {
                return false;
            }
            check(status() == "■ Interrupted", &format!("the status line says Interrupted: {:?}", status()));
            check(!view().live_busy, "and the agent is no longer busy");
            check(titles() == ["Worked for 12s · 5 tools", "", "Ran npm test", "", "Stopped npm test"], &format!("the settled turn folds its reasoning and tools; every message and what failed or stopped stays: {:?}", titles()));
            let commentary = live_row("live:cl:msg_01:1").unwrap_or_default();
            check(message_text(&commentary) == "Let me look at the parser and its tests.", &format!("the commentary stays, in its place: {:?}", message_text(&commentary)));
            let stopped = live_row("live:cl:msg_02:0").unwrap_or_default();
            check(message_text(&stopped) == "The failure came from an off-by-one", &format!("the stopped answer stays: {:?}", message_text(&stopped)));
            check(stopped.live.meta == "interrupted", "and says it was interrupted");
            app_now().focus_transcript();
            shot(&out4, "live-04-folded");
            check(live_rows().iter().all(|r| r.live.shimmer.is_empty()) && !any_shimmering(), "nothing shimmers once the turn settled");
            // On the failed command, K is the row above it: the fold.
            *app.artifact_cursor.borrow_mut() = "live:cl:toolu_03".into();
            true
        })),
        ("on the command", Box::new(move |app, _window, elapsed| {
            // Selected once the row has reported itself on screen.
            if crate::artifacts_view::selected(app).as_deref() != Some("live:cl:toolu_03") || elapsed < Duration::from_millis(300) {
                return false;
            }
            headless::press("k");
            true
        })),
        ("to the fold", Box::new(move |app, _window, _| {
            if app.artifact_cursor.borrow().as_str() != "live:fold:tr-1" {
                return false;
            }
            check(true, "K from the failed command reaches the fold");
            headless::press("o");
            true
        })),
        ("fold open", Box::new(move |_, _window, _| {
            if !live_row("live:fold:tr-1").is_some_and(|r| r.live.expanded) {
                return false;
            }
            check(live_rows().len() == 8, &format!("the fold opens to every row: {:?}", titles()));
            shot(&out5, "live-05-fold-open");
            headless::press("o");
            *before1.borrow_mut() = crate::view::sync_stats();
            // The Host imports the turn as it does: the answer's row (the
            // items' row_id) and one row per tool, its tools[].id the call id.
            let mut turns = vec![json!({"id": "live-abc", "role": "assistant", "text": "Let me look at the parser and its tests.\n\nThe failure came from an off-by-one"})];
            for id in ["toolu_01", "toolu_02", "toolu_03", "toolu_04", "toolu_05"] {
                turns.push(json!({"id": format!("row-{id}"), "role": "assistant", "text": "", "tools": [{"id": id, "name": "Bash", "status": "ok"}]}));
            }
            control("/__control/upsert", &json!({"session": "rachel", "turns": turns})).is_ok()
        })),
        ("taken over", Box::new(move |app, _window, elapsed| {
            let landed = app.engine.borrow().conversation("rachel").is_some_and(|c| c.index_of("live-abc").is_some() && c.index_of("row-toolu_05").is_some());
            if !landed || elapsed < Duration::from_millis(600) {
                return false;
            }
            check(titles() == ["Worked for 12s · 5 tools", "Ran npm test", "Stopped npm test"], &format!("the turn keeps its fold and what failed or stopped: {:?}", titles()));
            let shown = rows(&crate::window().expect("window"));
            let at = |id: &str| shown.iter().position(|r| r.id == id || r.live.key == id);
            check(at("live-abc").is_some_and(|i| at("live:fold:tr-1") == Some(i - 1) && at("live:cl:toolu_03") == Some(i + 1)), "the durable row, carrying both messages, is where its first message was");
            check(shown.iter().all(|r| !r.id.starts_with("row-toolu")), "the tool rows show as the item rows, not again as activity");
            let (_, inserted_before) = *before2.borrow();
            let (_, inserted) = crate::view::sync_stats();
            check(inserted == inserted_before, &format!("taken over in place: no row inserted ({inserted_before} → {inserted})"));
            shot(&out6, "live-06-taken-over");
            // A gap: lseq 24 never arrives, 25 does.
            fetched1.set(requests("GET", "/live").len());
            let status = json!({"op": "status", "conv": "conv-1", "activity": {"state": "idle"}});
            control("/__control/live-event", &json!({"session": "rachel", "skip": [24], "event": {"ops": [status.clone()]}})).is_ok()
                && control("/__control/live-event", &json!({"session": "rachel", "event": {"ops": [status]}})).is_ok()
        })),
        ("gap", Box::new(move |_, _window, _| {
            if lseq() != Some(25) {
                return false;
            }
            check(requests("GET", "/live").len() > fetched2.get(), "a missing lseq asks GET /live, and the stream continues above it");
            check(status().is_empty(), &format!("idle again: {:?}", status()));
            // A new stream after a Host restart (a new epoch) asks again.
            control("/__control/live-replay", &json!({"session": "rachel", "fixture": "gap-recovers-from-snapshot", "restart": true})).is_ok()
        })),
        ("gap recovered", Box::new(move |app, _window, _| {
            let engine = app.engine.borrow();
            let view = engine.live_view("rachel");
            if !view.is_some_and(|v| v.epoch() == Some("boot-b") && !v.awaiting_snapshot()) {
                return false;
            }
            check(requests("GET", "/live").len() >= 3, "the new epoch asked GET /live again");
            check(view.and_then(|v| v.item("cx:msg_a")).is_some_and(|i| i["text"] == "Hello world!"), "the snapshot's text, then the events above it");
            true
        })),
        ("host clock", Box::new(move |_, _window, _| {
            // A tool that started 72 s ago by the Host's clock reads 1:12
            // at once: the time is the Host's, never a timer started here.
            let now = clarp_engine_now();
            control("/__control/live-event", &json!({"session": "rachel", "event": {"server_now_ms": now, "ops": [
                {"op": "turn", "conv": "conv-1", "turn": {"turn_id": "tr-9", "status": "running", "started_at_ms": now - 80_000, "ended_at_ms": null, "worked_ms": null, "tool_count": 1}},
                {"op": "status", "conv": "conv-1", "activity": {"state": "tool", "tool": {"name": "Bash", "call_id": "c9", "label": "sleep 99", "item_id": "x:c9", "started_at_ms": now - 72_000},
                    "running_tools": 1, "headline": "Running sleep 99", "item_id": "x:c9", "since_ms": now - 72_000, "turn_id": "tr-9", "turn_started_ms": now - 80_000}},
            ]}}))
            .is_ok()
        })),
        ("elapsed", Box::new(move |_, _window, _| {
            let line = status();
            if !line.starts_with("● Running sleep 99") {
                return false;
            }
            check(line.starts_with("● Running sleep 99 · 1:1"), &format!("elapsed from tool.started_at_ms: {line:?}"));
            // The Host sums finished turns up in /log (§9).
            check(control("/__control/turn-summary", &json!({"on": true})).is_ok(), "the Host turns log_turn_summary on");
            app_now().engine.borrow_mut().reconnect();
            true
        })),
        ("summaries", Box::new(move |app, _window, _| {
            if !app.engine.borrow().log_turn_summary() {
                return false;
            }
            // A turn the real Host settled before the chat opened: its
            // snapshot, then its /log rows with the tool ids.
            control("/__control/live-load", &json!({"session": "probe", "fixture": "live-real-settled-turn.json"})).is_ok()
        })),
        ("probe listed", Box::new(move |app, _window, _| {
            if app.engine.borrow().roster().find("probe").is_none() {
                return false;
            }
            app.engine.borrow_mut().select("probe");
            true
        })),
        ("real turn", Box::new(move |app, _window, elapsed| {
            let landed = app.engine.borrow().conversation("probe").is_some_and(|c| c.index_of(REAL_ANSWER_ROW).is_some());
            if !landed || probe_titles().is_empty() || elapsed < Duration::from_millis(600) {
                return false;
            }
            check(probe_titles() == ["Worked for 16s · 2 tools", "Ran sleep 60"], &format!("the real settled turn folds after its rows took the tools over: {:?}", probe_titles()));
            let failed = live_row(&format!("live:{REAL_FAILED}")).unwrap_or_default();
            check(failed.live.status == "failed" && failed.live.meta == "exit 1 · 0.0s", &format!("the failed tool stays in view: {:?} {:?}", failed.live.status, failed.live.meta));
            check(failed.live.secondary == "Pause execution for 60 seconds.", &format!("with its explanation: {:?}", failed.live.secondary));
            let shown = rows(&crate::window().expect("window"));
            check(shown.last().is_some_and(|r| r.id == REAL_ANSWER_ROW), "the answer, its durable row, closes the turn");
            check(shown.iter().all(|r| r.id != "msg-9dc44519ed4e93bcff29" && r.id != "msg-d28c2b3f8a1150792e60"), "the tool rows are not shown again as activity");
            let fold = live_row(REAL_FOLD).unwrap_or_default();
            check(fold.live.expandable && !fold.live.expanded, "the fold is closed");
            shot(&out8, "live-07-real-folded");
            app_now().focus_transcript();
            *app.artifact_cursor.borrow_mut() = format!("live:{REAL_FAILED}");
            true
        })),
        ("real on the failed tool", Box::new(move |app, _window, elapsed| {
            if crate::artifacts_view::selected(app).as_deref() != Some(&format!("live:{REAL_FAILED}")) || elapsed < Duration::from_millis(300) {
                return false;
            }
            headless::press("k");
            true
        })),
        ("real to the fold", Box::new(move |app, _window, _| {
            if app.artifact_cursor.borrow().as_str() != REAL_FOLD {
                return false;
            }
            check(true, "K from the failed tool reaches the fold");
            headless::press("o");
            true
        })),
        ("real fold open", Box::new(move |_, _window, _| {
            if !live_row(REAL_FOLD).is_some_and(|r| r.live.expanded) {
                return false;
            }
            let titles = probe_titles();
            check(
                titles
                    == [
                        "Worked for 16s · 2 tools",
                        "Thought for 4s: There's a tension here: the task explicitly says not to report to anyone, but th",
                        "Ran sleep 60",
                        "Thought for 2s: Since foreground sleep is blocked, I'll run this in the background instead to sa",
                        "Ran sleep 60",
                    ],
                &format!("open: the reasoning and one row per tool: {titles:?}"),
            );
            shot(&out9, "live-08-real-fold-open");
            headless::press("o");
            true
        })),
        ("real fold closed", Box::new(move |_, _window, _| {
            if live_row(REAL_FOLD).is_none_or(|r| r.live.expanded) {
                return false;
            }
            check(probe_titles().len() == 2, &format!("O folds it again: {:?}", probe_titles()));
            // Later: the turn in between, then a prompt whose tool's /log
            // row lands the moment the tool starts, as on the real Host.
            control("/__control/live-load", &json!({"session": "probe", "fixture": "live-real-settled-turn.json", "part": "next"})).is_ok()
        })),
        ("real running", Box::new(move |app, _window, elapsed| {
            let landed = app.engine.borrow().conversation("probe").is_some_and(|c| c.index_of(REAL_RUNNING_ROW).is_some());
            let running = live_row(REAL_RUNNING).is_some_and(|r| r.live.status == "running");
            if !landed || !running || elapsed < Duration::from_millis(1200) {
                return false;
            }
            let tool = live_row(REAL_RUNNING).unwrap_or_default();
            check(tool.live.title == "Running python3 -c 'import time; time.sleep(120)'", &format!("the running tool keeps its live row after its /log row landed: {:?}", tool.live.title));
            check(tool.live.meta.starts_with("0:1"), &format!("with its timer from the Host's clock: {:?}", tool.live.meta));
            check(!tool.live.explaining, "and no Explaining… while the Host has none pending (explain: null)");
            let shown = rows(&crate::window().expect("window"));
            check(shown.iter().all(|r| r.id != REAL_RUNNING_ROW), "its /log row is not shown again as a 'Show · 1 tool call' row");
            let at = |id: &str| shown.iter().position(|r| r.id == id || r.live.key == id);
            let order = [at(REAL_FOLD), at(&format!("live:{REAL_FAILED}")), at(REAL_ANSWER_ROW), at(BETWEEN_FOLD), at(REAL_PREVIOUS_ANSWER), at(REAL_NEXT_PROMPT), at(REAL_RUNNING)];
            check(order.iter().all(Option::is_some) && order.windows(2).all(|w| w[0] < w[1]), &format!("strict order: each earlier turn's fold, failed tool and answer, then the prompt, then the new turn: {order:?}"));
            let fold = live_row(REAL_FOLD).unwrap_or_default();
            check(fold.live.title == "Worked for 16s · 2 tools" && !fold.live.expanded, &format!("the turn seen live stays folded in history, with the Host's worked time: {:?}", fold.live.title));
            check(live_row(&format!("live:{REAL_FAILED}")).is_some_and(|r| r.live.status == "failed"), "its failed tool stays in view");
            let between = live_row(BETWEEN_FOLD).unwrap_or_default();
            check(between.live.title == "Used 3 tools" && !between.live.expanded, &format!("the turn in between, only ever in /log and ended before the Host summed turns up, folds the same without a worked time: {:?}", between.live.title));
            check(activity_rows().is_empty(), &format!("no finished turn shows its tools as 'Show · 1 tool call' rows: {:?}", activity_rows()));
            *running_at1.borrow_mut() = at(REAL_RUNNING).unwrap_or(usize::MAX);
            *before3.borrow_mut() = crate::view::sync_stats();
            shot(&out10, "live-09-real-running");
            control("/__control/live-load", &json!({"session": "probe", "fixture": "live-real-settled-turn.json", "part": "done"})).is_ok()
        })),
        ("real failed", Box::new(move |_, _window, elapsed| {
            let tool = live_row(REAL_RUNNING).unwrap_or_default();
            if tool.live.status != "failed" || elapsed < Duration::from_millis(400) {
                return false;
            }
            check(tool.live.title == "Ran python3 -c 'import time; time.sleep(120)'" && tool.live.meta == "exit 1 · 19.9s", &format!("it settles in place: {:?} {:?}", tool.live.title, tool.live.meta));
            check(lines(&tool) == ["Exit code 137"], &format!("with its tail: {:?}", lines(&tool)));
            let shown = rows(&crate::window().expect("window"));
            check(shown.iter().position(|r| r.live.key == REAL_RUNNING) == Some(*running_at2.borrow()), "at the same place");
            let (_, inserted_before) = *before4.borrow();
            check(crate::view::sync_stats().1 == inserted_before, "no row inserted");
            app_now().focus_transcript();
            *app_now().artifact_cursor.borrow_mut() = BETWEEN_FOLD.to_owned();
            true
        })),
        ("history on the fold", Box::new(move |app, _window, elapsed| {
            if crate::artifacts_view::selected(app).as_deref() != Some(BETWEEN_FOLD) || elapsed < Duration::from_millis(300) {
                return false;
            }
            check(true, "J/K reach a fold in history");
            headless::press("o");
            true
        })),
        ("history fold open", Box::new(move |_, _window, _| {
            if !live_row(BETWEEN_FOLD).is_some_and(|r| r.live.expanded) {
                return false;
            }
            let shown = rows(&crate::window().expect("window"));
            let from = shown.iter().position(|r| r.live.key == BETWEEN_FOLD).unwrap_or(0);
            let turn: Vec<String> = shown[from..].iter().take_while(|r| r.id != REAL_PREVIOUS_ANSWER).map(|r| r.live.title.to_string()).collect();
            check(
                turn == ["Used 3 tools", "Ran ls /var/tmp/probe", "Read /etc/hostname", "Ran date"],
                &format!("O opens it to one row per tool, as the live fold opens: {turn:?}"),
            );
            check(activity_rows().is_empty(), "and no activity rows");
            shot(&out14, "live-13-history-fold-open");
            headless::press("o");
            true
        })),
        ("history fold closed", Box::new(move |_, _window, _| {
            if live_row(BETWEEN_FOLD).is_none_or(|r| r.live.expanded) {
                return false;
            }
            check(true, "O folds it again");
            // Another chat with the same real turn, settled, its tool rows
            // and finalized answer not in /log yet.
            control("/__control/live-load", &json!({"session": "anchor", "fixture": "live-real-settled-turn.json", "hold": true, "agent_id": "agent-anchor"})).is_ok()
        })),
        ("anchor listed", Box::new(move |app, _window, _| {
            if app.engine.borrow().roster().find("anchor").is_none() {
                return false;
            }
            app.engine.borrow_mut().select("anchor");
            true
        })),
        ("anchor settled", Box::new(move |app, _window, elapsed| {
            let open = app.engine.borrow().selected_session() == "anchor" && app.engine.borrow().conversation("anchor").is_some_and(|c| c.index_of(REAL_ANSWER_ROW).is_some());
            if !open || live_row(REAL_FOLD).is_none() || elapsed < Duration::from_millis(600) {
                return false;
            }
            check(titles() == ["Worked for 16s · 2 tools", "Ran sleep 60", ""], &format!("settled, before its rows land: the fold, the failed tool, the answer: {:?}", titles()));
            // The next message, sent after the turn settled.
            control("/__control/upsert", &json!({"session": "anchor", "turns": [{"id": ANCHOR_NEXT, "role": "user", "text": "And now the next step.", "timestamp": "2026-10-03T11:49:30.000Z", "trace_id": ANCHOR_TURN}]})).is_ok()
        })),
        ("anchor message", Box::new(move |app, _window, elapsed| {
            if !app.engine.borrow().conversation("anchor").is_some_and(|c| c.index_of(ANCHOR_NEXT).is_some()) || elapsed < Duration::from_millis(600) {
                return false;
            }
            let order = anchor_order(&[REAL_PROMPT, REAL_FOLD, &format!("live:{REAL_FAILED}"), REAL_ANSWER, ANCHOR_NEXT]);
            check(in_order(&order), &format!("the new message is below the settled turn: prompt, fold, failed tool, answer, message: {order:?}"));
            shot(&out11, "live-10-anchor-message");
            *landing1.borrow_mut() = (crate::view::sync_stats(), anchor_now());
            control("/__control/live-load", &json!({"session": "anchor", "fixture": "live-real-settled-turn.json", "part": "held"})).is_ok()
        })),
        ("anchor landed", Box::new(move |app, _window, elapsed| {
            let landed = app.engine.borrow().conversation("anchor").is_some_and(|c| c.index_of("msg-9dc44519ed4e93bcff29").is_some() && c.rows().iter().all(|r| r.kind != "live"));
            if !landed || elapsed < Duration::from_millis(800) {
                return false;
            }
            let order = anchor_order(&[REAL_PROMPT, REAL_FOLD, &format!("live:{REAL_FAILED}"), REAL_ANSWER_ROW, ANCHOR_NEXT]);
            check(in_order(&order), &format!("the rows landing after the message move nothing: {order:?}"));
            let shown = rows(&crate::window().expect("window"));
            check(shown.iter().all(|r| r.id != "msg-9dc44519ed4e93bcff29" && r.id != "msg-d28c2b3f8a1150792e60"), "the tool rows are not shown again");
            let ((_, inserted_before), place) = landing2.borrow().clone();
            check(crate::view::sync_stats().1 == inserted_before, &format!("taken over in place: no row inserted ({inserted_before} → {})", crate::view::sync_stats().1));
            match place {
                Some(place) => {
                    let (still, detail) = anchor_moved(&place);
                    check(still, &format!("and nothing on screen moved: {detail}"));
                }
                None => check(false, "the place on screen was recorded"),
            }
            shot(&out12, "live-11-anchor-landed");
            *retiring1.borrow_mut() = (crate::view::sync_stats(), place_of(ANCHOR_NEXT));
            // The next turn starts (the message's): the settled turn retires.
            let started = ANCHOR_STARTED;
            control("/__control/live-event", &json!({"session": "anchor", "event": {"conv": "c-probe", "server_now_ms": started + 2_000, "ops": [
                {"op": "turn", "conv": "c-probe", "turn": {"turn_id": ANCHOR_TURN, "status": "running", "started_at_ms": started, "ended_at_ms": null, "worked_ms": null, "tool_count": 1}},
                {"op": "upsert", "conv": "c-probe", "id": "x:anchor-1", "kind": "tool", "rev": 1, "item": {"id": "x:anchor-1", "kind": "tool", "rev": 1, "conv": "c-probe", "turn_id": ANCHOR_TURN,
                    "status": "running", "ordinal": 20, "started_at_ms": started + 1_000, "ended_at_ms": null,
                    "tool": {"name": "Bash", "call_id": "anchor-1", "category": "exec", "label": "make test", "command": "make test"}}},
            ]}}))
            .is_ok()
        })),
        ("anchor next turn", Box::new(move |_, _window, elapsed| {
            if live_row(ANCHOR_TOOL).is_none() || elapsed < Duration::from_millis(600) {
                return false;
            }
            let fold = live_row(REAL_FOLD).unwrap_or_default();
            check(fold.live.title == "Worked for 16s · 2 tools" && !fold.live.expanded, &format!("the retired turn keeps ONE fold row in history: {:?}", fold.live.title));
            check(live_rows().iter().filter(|r| r.live.kind == "fold").count() == 1, "one fold");
            check(live_row(&format!("live:{REAL_FAILED}")).is_some_and(|r| r.live.status == "failed"), "its failed tool stays in view");
            check(activity_rows().is_empty(), &format!("its tool rows are not shown as 'Show · 1 tool call': {:?}", activity_rows()));
            let order = anchor_order(&[REAL_PROMPT, REAL_FOLD, &format!("live:{REAL_FAILED}"), REAL_ANSWER_ROW, ANCHOR_NEXT, ANCHOR_TOOL]);
            check(in_order(&order), &format!("strict order: the earlier turn folded, the message, the new turn: {order:?}"));
            let ((_, inserted_before), place) = retiring2.borrow().clone();
            check(crate::view::sync_stats().1 == inserted_before + 1, &format!("the fold stays in place: only the new tool row inserted ({inserted_before} → {})", crate::view::sync_stats().1));
            match place {
                Some(place) => {
                    let (still, detail) = anchor_moved(&place);
                    check(still, &format!("nothing above the new turn moved: {detail}"));
                }
                None => check(false, "the message's place on screen was recorded"),
            }
            shot(&out15, "live-14-anchor-retired-folded");
            app_now().focus_transcript();
            *app_now().artifact_cursor.borrow_mut() = REAL_FOLD.to_owned();
            true
        })),
        ("anchor on the fold", Box::new(move |app, _window, elapsed| {
            if crate::artifacts_view::selected(app).as_deref() != Some(REAL_FOLD) || elapsed < Duration::from_millis(300) {
                return false;
            }
            headless::press("o");
            true
        })),
        ("anchor fold open", Box::new(move |_, _window, _| {
            if !live_row(REAL_FOLD).is_some_and(|r| r.live.expanded) {
                return false;
            }
            let order = anchor_order(&[REAL_PROMPT, REAL_FOLD, &format!("live:{REAL_FAILED}"), REAL_ANSWER_ROW, ANCHOR_NEXT]);
            check(in_order(&order), &format!("open, in order: {order:?}"));
            let titles = titles();
            check(
                titles.starts_with(&[
                    "Worked for 16s · 2 tools".to_owned(),
                    "Thought for 4s: There's a tension here: the task explicitly says not to report to anyone, but th".to_owned(),
                    "Ran sleep 60".to_owned(),
                    "Thought for 2s: Since foreground sleep is blocked, I'll run this in the background instead to sa".to_owned(),
                    "Ran sleep 60".to_owned(),
                ]),
                &format!("O opens the retired turn as its live fold opened: {titles:?}"),
            );
            shot(&out16, "live-15-anchor-retired-open");
            headless::press("o");
            true
        })),
        ("anchor fold closed", Box::new(move |_, _window, _| {
            if live_row(REAL_FOLD).is_none_or(|r| r.live.expanded) {
                return false;
            }
            // A message queued while the turn runs goes below it.
            control("/__control/upsert", &json!({"session": "anchor", "turns": [{"id": ANCHOR_QUEUED, "role": "user", "text": "Also check the docs.", "timestamp": "2026-10-03T11:49:40.000Z", "trace_id": "tr-anchor-3"}]})).is_ok()
        })),
        ("anchor queued", Box::new(move |app, _window, elapsed| {
            if !app.engine.borrow().conversation("anchor").is_some_and(|c| c.index_of(ANCHOR_QUEUED).is_some()) || elapsed < Duration::from_millis(600) {
                return false;
            }
            let order = anchor_order(&[REAL_PROMPT, REAL_ANSWER_ROW, ANCHOR_NEXT, ANCHOR_TOOL, ANCHOR_QUEUED]);
            check(in_order(&order), &format!("the queued message is below the running turn: {order:?}"));
            shot(&out13, "live-12-anchor-queued");
            // A stale live turn: GET /live kept the first turn running while
            // two newer prompts started turns of their own in /log.
            control("/__control/live-load", &json!({"session": "stale", "fixture": "live-real-stale-turn.json", "agent_id": "agent-stale"})).is_ok()
        })),
        ("stale listed", Box::new(move |app, _window, _| {
            if app.engine.borrow().roster().find("stale").is_none() {
                return false;
            }
            app.engine.borrow_mut().select("stale");
            true
        })),
        ("stale turn", Box::new(move |app, _window, elapsed| {
            let open = app.engine.borrow().selected_session() == "stale" && app.engine.borrow().conversation("stale").is_some_and(|c| c.index_of(STALE_THIRD).is_some());
            if !open || live_row(STALE_LAST).is_none() || elapsed < Duration::from_millis(800) {
                return false;
            }
            let order = anchor_order(&STALE_ORDER);
            check(in_order(&order), &format!("each newer prompt is above the items that started after it: {order:?}"));
            check(status().starts_with("● git push · "), &format!("the status line keeps working: {:?}", status()));
            check(report().follows, "the chat follows");
            let (visible, detail) = last_row_visible();
            check(visible, &format!("a follower stays at the end: {detail}"));
            shot(&out17, "live-16-stale-turn");
            *stale1.borrow_mut() = crate::view::sync_stats();
            // The stale turn goes on: one more item, after the third prompt.
            control("/__control/live-event", &json!({"session": "stale", "event": {"conv": "c-stale", "server_now_ms": STALE_THIRD_MS + 22_000, "ops": [
                {"op": "upsert", "conv": "c-stale", "id": "cl:msg_c4:0", "kind": "message", "rev": 1, "item": {"id": "cl:msg_c4:0", "kind": "message", "rev": 1, "conv": "c-stale",
                    "turn_id": "73894fe2fee39d82", "status": "running", "ordinal": 14, "started_at_ms": STALE_THIRD_MS + 21_000, "ended_at_ms": null, "text": "Pushed.", "phase": "final"}},
            ]}}))
            .is_ok()
        })),
        ("stale goes on", Box::new(move |_, _window, elapsed| {
            if live_row(STALE_NEWEST).is_none() || elapsed < Duration::from_millis(800) {
                return false;
            }
            let mut ids = STALE_ORDER.to_vec();
            ids.push(STALE_NEWEST);
            let order = anchor_order(&ids);
            check(in_order(&order), &format!("the new item goes at the end, nothing else moves: {order:?}"));
            let (_, inserted_before) = *stale2.borrow();
            check(crate::view::sync_stats().1 == inserted_before + 1, &format!("only the new row inserted ({inserted_before} → {})", crate::view::sync_stats().1));
            check(report().follows, "the chat still follows");
            let (visible, detail) = last_row_visible();
            check(visible, &format!("a follower stays at the end: {detail}"));
            true
        })),
    ];
    run_stages(stages);
}

const REAL_PROMPT: &str = "u-clarp-admin-23071c64bf0fc449";
/// The probe's turn in between: only ever in /log.
const BETWEEN_FOLD: &str = "live:fold:8b701eb0b9fe4f19";

/// Rows showing tool activity the old way ('Show · 1 tool call').
fn activity_rows() -> Vec<String> {
    rows(&crate::window().expect("window"))
        .iter()
        .filter(|r| r.live.key.is_empty() && (r.tools.row_count() > 0 || r.cells.row_count() > 0 || !r.activity_label.is_empty()))
        .map(|r| r.id.to_string())
        .collect()
}
const REAL_ANSWER: &str = "live:cl:msg_011CffFaGNXmnoVdTuwgSXcv:0";
const ANCHOR_NEXT: &str = "u-anchor-next";
const ANCHOR_QUEUED: &str = "u-anchor-queued";
const ANCHOR_TURN: &str = "tr-anchor-2";
const ANCHOR_TOOL: &str = "live:x:anchor-1";
/// 2026-10-03T11:49:30Z, when the next message was written.
const ANCHOR_STARTED: i64 = 1791028170000;

const STALE_THIRD: &str = "u-stale-third";
/// 2026-10-05T11:30:35.907Z, when the third prompt was written.
const STALE_THIRD_MS: i64 = 1791199835907;
const STALE_LAST: &str = "live:cl:msg_c3:0";
const STALE_NEWEST: &str = "live:cl:msg_c4:0";
/// The stale turn as it must read: each prompt above what started after it.
const STALE_ORDER: [&str; 9] = [
    "u-stale-first",
    "live:cl:toolu_a1",
    "live:cl:toolu_a3",
    "u-stale-second",
    "live:cl:msg_b1:0",
    "msg-b4",
    STALE_THIRD,
    "live:cl:toolu_c2",
    STALE_LAST,
];

/// Where each row (by id or live key) shows in the window.
fn anchor_order(ids: &[&str]) -> Vec<Option<usize>> {
    let shown = rows(&crate::window().expect("window"));
    ids.iter().map(|id| shown.iter().position(|r| r.id == *id || r.live.key == *id)).collect()
}

fn in_order(order: &[Option<usize>]) -> bool {
    order.iter().all(Option::is_some) && order.windows(2).all(|w| w[0] < w[1])
}

fn clarp_engine_now() -> i64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map_or(0, |d| d.as_millis() as i64)
}
