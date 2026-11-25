//! `--check roster-stale`: the agent list survives a failing and a timing
//! out `/agents/snapshot`. Refresh agents (Ctrl+K) meets a 504, then the
//! first retry times out (`CLARP_SNAPSHOT_TIMEOUT_MS`): the roster is kept,
//! no banner shows, the explorer and Ctrl+K say "Agent list from HH:MM ·
//! retrying", and the retries wait 2 s, then 5 s. An agent the Host added
//! without announcing it appears after the next good retry, with nothing
//! else asking, and the note goes. Shift+F5 asks again. Needs the fake Host.

use std::cell::{Cell, RefCell};
use std::rc::Rc;
use std::time::Duration;

use clarp_engine::roster_freshness::State;
use serde_json::json;
use slint::Model;
use slint::platform::Key;

use super::{Stage, check, control, requests, run_stages, shot};
use crate::headless;

fn first(window: &crate::AppWindow) -> (String, String) {
    window.get_switcher_rows().row_data(0).map(|r| (r.kind.to_string(), r.label.to_string())).unwrap_or_default()
}

fn snapshot_times() -> Vec<f64> {
    requests("GET", "/agents/snapshot").iter().filter_map(|r| r["at"].as_f64()).collect()
}

fn listed(app: &crate::App, session: &str) -> bool {
    app.chats.iter().any(|row| row.session == session)
}

pub fn roster_stale_check(out: String) {
    let (o1, o2, o3) = (out.clone(), out.clone(), out);
    // How many agents and snapshot requests before the failures.
    let agents = Rc::new(Cell::new(0usize));
    let from = Rc::new(Cell::new(0usize));
    let note = Rc::new(RefCell::new(String::new()));
    let (agents2, from2, note2) = (agents.clone(), from.clone(), note.clone());
    let (from3, from4) = (from.clone(), from.clone());
    let stages: Vec<Stage> = vec![
        ("ready", Box::new(move |app, window, _| {
            let engine = app.engine.borrow();
            let open = engine.conversation("rachel").is_some_and(|c| !c.rows().is_empty());
            if !open || engine.roster_freshness() != State::Fresh {
                return false;
            }
            // Startup's own snapshots are over: the failures meet the command.
            let now = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map_or(0.0, |d| d.as_secs_f64());
            if snapshot_times().last().is_some_and(|last| now - last < 1.5) {
                return false;
            }
            check(window.get_roster_note().is_empty(), &format!("a fresh list has no note: {:?}", window.get_roster_note()));
            agents.set(engine.roster().agents().len());
            drop(engine);
            from.set(snapshot_times().len());
            check(control("/__control/fail", &json!({"path": "/agents/snapshot", "status": 504, "count": 1})).is_ok(), "the next snapshot meets a gateway timeout");
            check(control("/__control/snapshot-delay", &json!({"seconds": 4, "count": 1})).is_ok(), "and the one after it takes 4 s, past the 1.5 s timeout");
            headless::press_with(&[Key::Control], "k");
            headless::type_text("refresh agents");
            true
        })),
        ("command found", Box::new(|_, window, elapsed| {
            if window.get_switcher_query() != "refresh agents" || elapsed < Duration::from_millis(300) {
                return false;
            }
            check(first(window) == ("command".into(), "Refresh agents".into()), &format!("Ctrl+K finds Refresh agents: {:?}", first(window)));
            headless::press(Key::Return);
            true
        })),
        ("stale", Box::new(move |app, window, elapsed| {
            let state = app.engine.borrow().roster_freshness();
            if !state.is_stale() || window.get_switcher_open() || elapsed < Duration::from_millis(300) {
                return false;
            }
            let shown = window.get_roster_note().to_string();
            check(shown.starts_with("Agent list from ") && shown.ends_with(" · retrying"), &format!("the explorer says the list is stale: {shown:?}"));
            check(window.get_roster_note_detail().contains("504"), &format!("and why: {:?}", window.get_roster_note_detail()));
            check(app.engine.borrow().roster().agents().len() == agents2.get() && listed(app, "rachel"), "the roster is kept");
            check(window.get_error().is_empty(), &format!("no banner for a background refresh: {:?}", window.get_error()));
            *note2.borrow_mut() = shown;
            shot(&o1, "roster-01-stale");
            // An agent the desktop is not told about: only a snapshot finds it.
            check(control("/__control/add-agent", &json!({"session": "koko", "persona": "Koko", "announce": false})).is_ok(), "the Host gains Koko silently");
            headless::press_with(&[Key::Control], "k");
            headless::type_text("rach");
            true
        })),
        ("switcher says so", Box::new(move |_, window, elapsed| {
            if window.get_switcher_query() != "rach" || elapsed < Duration::from_millis(300) {
                return false;
            }
            check(first(window).0 == "agent", &format!("the kept agents are found: {:?}", first(window)));
            let shown = window.get_switcher_note().to_string();
            check(shown.starts_with(note.borrow().as_str()), &format!("Ctrl+K carries the same note: {shown:?}"));
            shot(&o2, "roster-02-switcher-stale");
            headless::press(Key::Escape);
            true
        })),
        ("timed out", Box::new(|app, window, _| {
            let State::Stale { failures, .. } = app.engine.borrow().roster_freshness() else { return false };
            if failures < 2 {
                return false;
            }
            check(window.get_roster_note_detail().contains("did not answer within 1.5 s"), &format!("the retry timed out: {:?}", window.get_roster_note_detail()));
            check(!listed(app, "koko"), "Koko is not listed yet");
            check(!window.get_roster_note().is_empty(), "the note stays while it retries");
            true
        })),
        ("recovered", Box::new(move |app, window, elapsed| {
            if !listed(app, "koko") || elapsed < Duration::from_millis(200) {
                return false;
            }
            check(app.engine.borrow().roster_freshness() == State::Fresh, "the list is fresh again");
            check(window.get_roster_note().is_empty(), &format!("and the note is gone: {:?}", window.get_roster_note()));
            let times = snapshot_times();
            let tries: Vec<f64> = times[from2.get()..].to_vec();
            check(tries.len() == 3, &format!("three requests: the command, two retries, nothing stacked: {tries:?}"));
            if let [a, b, c] = tries[..] {
                // The second gap is the 1.5 s timeout, then the 5 s back-off.
                check((1.8..4.0).contains(&(b - a)), &format!("the first retry after 2 s: {:.2}", b - a));
                check((6.0..9.5).contains(&(c - b)), &format!("the second after the timeout and 5 s: {:.2}", c - b));
            }
            from3.set(times.len());
            shot(&o3, "roster-03-recovered");
            headless::press_with(&[Key::Shift], Key::F5);
            true
        })),
        ("shift+f5", Box::new(move |_, _, _| {
            if snapshot_times().len() <= from4.get() {
                return false;
            }
            check(true, "Shift+F5 fetches the agent list");
            true
        })),
    ];
    run_stages(stages);
}

/// The busy stretch: this many `agent-roster` events, this far apart.
const BURST: usize = 55;
const BURST_GAP_MS: u64 = 100;

/// Row reads (`GET /agents/{session}`) of Rachel and Mike.
fn row_reads() -> usize {
    requests("GET", "/agents/rachel").len() + requests("GET", "/agents/mike").len()
}

/// Whether the burst tagged `tag` has landed: each kind's last value shows.
fn burst_landed(app: &crate::App, tag: &str) -> bool {
    let engine = app.engine.borrow();
    let (Some(rachel), Some(mike)) = (engine.roster().find("rachel"), engine.roster().find("mike")) else { return false };
    mike.avatar_symbol == format!("{tag}-53") && rachel.status_text == format!("{tag} stale 54")
}

/// What the burst's last events left, as the Host's rows have them.
fn burst_values(app: &crate::App, tag: &str, when: &str) {
    let engine = app.engine.borrow();
    let (Some(rachel), Some(mike)) = (engine.roster().find("rachel"), engine.roster().find("mike")) else {
        check(false, &format!("{when}: Rachel and Mike are listed"));
        return;
    };
    check(mike.helper_state == "running", &format!("{when}: Mike's helper state is the last event's: {:?}", mike.helper_state));
    check(rachel.running_children == 1 && rachel.child_count == 1, &format!("{when}: and Rachel's helper counts: {} of {}", rachel.running_children, rachel.child_count));
    check(rachel.muted, &format!("{when}: Rachel is muted, as the last mute-changed said"));
    check(mike.chat_view == "session", &format!("{when}: Mike's chat view is the last one: {:?}", mike.chat_view));
    check(mike.avatar_symbol == format!("{tag}-53"), &format!("{when}: Mike's portrait is the last one: {:?}", mike.avatar_symbol));
    check(rachel.status_text == format!("{tag} stale 54"), &format!("{when}: Rachel's line is the last one: {:?}", rachel.status_text));
    check(mike.backend == "codex" && rachel.persona == "Rachel", &format!("{when}: fields no event named are kept"));
}

/// `--check roster-rows` (issue 841): an agent's row changes by its
/// `agent-roster` event without reading the agent list. A busy stretch of
/// 55 roster events (helper states with their maker's counts, mutes, chat
/// views, a portrait and stale work, 10 a second) is sent twice: first by a
/// Host without `roster_fields` and `agent_row`, where every event reads
/// `/agents/snapshot`, then by one with them, where the rows are patched and
/// the two one-row kinds read `GET /agents/{session}`; no snapshot is read.
/// Both end with the rows the Host has. The counts are printed as a perf
/// line. Needs the fake Host.
pub fn roster_rows_check(_out: String) {
    let marks = Rc::new(Cell::new((0usize, 0usize)));
    let before = Rc::new(Cell::new((0usize, 0usize)));
    let (marks2, marks3, marks4) = (marks.clone(), marks.clone(), marks.clone());
    let before2 = before.clone();
    let quiet_since = |window: f64| {
        let now = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map_or(0.0, |d| d.as_secs_f64());
        !snapshot_times().last().is_some_and(|last| now - last < window)
    };
    let stages: Vec<Stage> = vec![
        ("ready", Box::new(move |app, _, _| {
            let open = app.engine.borrow().conversation("rachel").is_some_and(|c| !c.rows().is_empty());
            if !open || app.engine.borrow().roster_freshness() != State::Fresh || !quiet_since(1.5) {
                return false;
            }
            marks.set((snapshot_times().len(), row_reads()));
            check(control("/__control/roster-burst", &json!({"count": BURST, "interval_ms": BURST_GAP_MS, "tag": "before"})).is_ok(), "a Host without the features sends a busy stretch");
            true
        })),
        ("before", Box::new(move |app, _, elapsed| {
            if !burst_landed(app, "before") || elapsed < Duration::from_millis(BURST as u64 * BURST_GAP_MS + 1000) || !quiet_since(1.0) {
                return false;
            }
            burst_values(app, "before", "without the features");
            let (snapshots, rows) = marks2.get();
            before.set((snapshot_times().len() - snapshots, row_reads() - rows));
            check(before.get().0 > 0 && before.get().1 == 0, &format!("an older Host's events read the list: {} snapshots, {} rows", before.get().0, before.get().1));
            check(control("/__control/roster-rows", &json!({"fields": true, "row": true})).is_ok(), "the Host offers roster_fields and agent_row");
            app.engine.borrow_mut().reconnect();
            true
        })),
        ("reconnected", Box::new(move |app, _, elapsed| {
            if elapsed < Duration::from_millis(500) || app.engine.borrow().roster_freshness() != State::Fresh || !quiet_since(1.5) {
                return false;
            }
            marks3.set((snapshot_times().len(), row_reads()));
            check(control("/__control/roster-burst", &json!({"count": BURST, "interval_ms": BURST_GAP_MS, "tag": "after"})).is_ok(), "the Host with them sends the same stretch");
            true
        })),
        ("after", Box::new(move |app, _, elapsed| {
            if !burst_landed(app, "after") || elapsed < Duration::from_millis(BURST as u64 * BURST_GAP_MS + 1000) {
                return false;
            }
            burst_values(app, "after", "with the features");
            let (snapshots, rows) = marks4.get();
            let after = (snapshot_times().len() - snapshots, row_reads() - rows);
            let was = before2.get();
            check(after.0 == 0, &format!("no event read the agent list: {} snapshots", after.0));
            check(after.1 > 0 && after.1 <= 22, &format!("only portrait-selected and stale-work read their one row: {} reads for 22 events", after.1));
            println!(
                "perf roster-rows: {BURST} roster events in {:.1} s: without roster_fields/agent_row {} snapshot reads; with them {} snapshot reads and {} one-row reads",
                (BURST as u64 * BURST_GAP_MS) as f64 / 1000.0,
                was.0,
                after.0,
                after.1
            );
            true
        })),
    ];
    run_stages(stages);
}
