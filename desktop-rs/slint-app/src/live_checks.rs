//! `--check live`: live items in the window (docs/live-items.md §7), driven
//! by the fake Host replaying the recorded `turn-full` stream, by keyboard.
//! Without `live_items` the chat keeps to `/log`; with it one status line
//! names what runs with an elapsed time from the Host's clock and the
//! interrupt key, item rows update in place (labels, Thought for Ns,
//! Explored N, tails, +N −M) without moving a reader who scrolled up,
//! J/K and O reach and open them, the settled turn folds, a durable `/log`
//! row takes over its items in place, and a gap recovers from GET /live.

use std::cell::RefCell;
use std::rc::Rc;
use std::time::Duration;

use serde_json::json;
use slint::Model;
use slint::platform::Key;

use super::scroll_checks::{anchor_now, anchor_moved, last_row_visible, row_height, wheel_up};
use super::{Stage, app_now, check, control, posts, report, requests, rows, run_stages, shot, view};
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

pub fn live_check(out: String) {
    let anchor: Rc<RefCell<Option<(String, f32)>>> = Rc::default();
    let height: Rc<RefCell<f32>> = Rc::default();
    let rows_before: Rc<RefCell<(usize, usize)>> = Rc::default();
    let (out1, out2, out3, out4, out5, out6) = (out.clone(), out.clone(), out.clone(), out.clone(), out.clone(), out.clone());
    let (anchor1, anchor2) = (anchor.clone(), anchor.clone());
    let (height1, height2) = (height.clone(), height.clone());
    let (before1, before2) = (rows_before.clone(), rows_before.clone());
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
            shot(&out1, "live-01-thinking");
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
            check(titles() == ["Worked for 12s · 5 tools", "Ran npm test", "", "Stopped npm test"], &format!("the settled turn folds; what failed or stopped stays: {:?}", titles()));
            let stopped = live_row("live:cl:msg_02:0").unwrap_or_default();
            check(message_text(&stopped) == "The failure came from an off-by-one", &format!("the stopped answer stays: {:?}", message_text(&stopped)));
            check(stopped.live.meta == "interrupted", "and says it was interrupted");
            app_now().focus_transcript();
            shot(&out4, "live-04-folded");
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
            // The Host imports the turn: one durable row with the text and the tools.
            let tools: Vec<_> = ["toolu_01", "toolu_02", "toolu_03", "toolu_04", "toolu_05"].iter().map(|id| json!({"id": id, "name": "Bash", "status": "ok"})).collect();
            control("/__control/upsert", &json!({"session": "rachel", "turns": [
                {"id": "live-abc", "role": "assistant", "text": "Let me look at the parser and its tests.\n\nThe failure came from an off-by-one", "tools": tools}
            ]}))
            .is_ok()
        })),
        ("taken over", Box::new(move |app, _window, elapsed| {
            let landed = app.engine.borrow().conversation("rachel").is_some_and(|c| c.index_of("live-abc").is_some());
            if !landed || elapsed < Duration::from_millis(600) {
                return false;
            }
            check(live_rows().is_empty(), &format!("every item gave its place to the durable row: {:?}", titles()));
            let shown = rows(&crate::window().expect("window"));
            check(shown.last().is_some_and(|r| r.id == "live-abc"), "the durable row is where the turn was");
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
            true
        })),
    ];
    run_stages(stages);
}

fn clarp_engine_now() -> i64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map_or(0, |d| d.as_millis() as i64)
}
