//! `--check stop-receipt`: after Stop the chat says what the Host committed,
//! who stopped it and why (Host contract 61), never what will happen next.
//! The fake Host answers each Stop with one of the contract's cases: a
//! verified agent, a claimed user (an admin token), a paired user, an older
//! Host and a Stop the runtime refused; Ctrl+. stops the first, the Stop
//! command (Ctrl+K) the rest. Another device's or agent's Stop, read from
//! the interrupted state's `user_stop` detail, shows the same line. A queue
//! the Stop paused says so in the composer and the explorer.
//!
//! `--check stop-held`: a chat whose next turn is held by the release drain
//! (live `limited` with no turn) shows its headline but is not working: no
//! stop key, no timer, nothing busy; Ctrl+. and the Stop command only say
//! nothing runs and never send `/stop`. `limited` under a running turn
//! still stops.

use std::time::Duration;

use serde_json::{Value, json};
use slint::Model;
use slint::platform::Key;

use super::{Stage, app_now, check, control, posts, report, run_stages, shot, view};
use crate::headless;

const THEO: &str = "Stopped by Theo (verified): Pebble busy with no process";

fn now() -> i64 {
    chrono::Utc::now().timestamp_millis()
}

fn state(kind: &str, detail: Option<Value>) -> Result<(), String> {
    let mut event = json!({"type": "agent-state", "session": "rachel", "kind": kind, "ts": now()});
    if let Some(detail) = detail {
        event["detail"] = detail;
    }
    control("/__control/event", &event)
}

fn notice() -> String {
    view().stop_notice.to_string()
}

/// Rachel works again (which clears the last line) and the Host is set to
/// answer the next Stop with `respond`.
fn working(name: &'static str, respond: Value) -> Stage {
    let mut asked = false;
    (name, Box::new(move |app, _, _| {
        if !std::mem::replace(&mut asked, true) {
            let sent = control("/__control/stop", &json!({"respond": respond.clone()})).and_then(|()| state("thinking", None));
            check(sent.is_ok(), &format!("{name}: Rachel works and the Host's answer is set: {sent:?}"));
            return false;
        }
        if !app.engine.borrow().roster().find("rachel").is_some_and(|a| a.busy) {
            return false;
        }
        notice().is_empty()
    }))
}

/// Stops Rachel by the Stop command, as Ctrl+K runs it.
fn stop_by_command(name: &'static str) -> Stage {
    (name, Box::new(|_, window, _| {
        check(crate::commands::run(&app_now(), window, "stop-agent"), "the Stop command runs");
        true
    }))
}

/// The Host answered the `stops`th Stop and the composer says `expected`.
fn says(name: &'static str, stops: usize, expected: &'static str, out: String, shot_name: &'static str) -> Stage {
    (name, Box::new(move |_, _, elapsed| {
        let posted = posts("/stop");
        if posted.len() < stops || notice().is_empty() || elapsed < Duration::from_millis(300) {
            return false;
        }
        check(posted[stops - 1]["body"]["session"] == "rachel", &format!("{name}: Stop is sent for Rachel"));
        check(notice() == expected, &format!("{name}: the chat says {expected:?}: {:?}", notice()));
        check(!notice().to_lowercase().contains("resume"), "and never promises a resume");
        shot(&out, shot_name);
        true
    }))
}

pub fn stop_receipt_check(out: String) {
    let (out_detail, out_refused, out_queue) = (out.clone(), out.clone(), out.clone());
    let stages: Vec<Stage> = vec![
        ("ready", Box::new(|app, _, _| {
            let loaded = app.engine.borrow().conversation("rachel").is_some_and(|c| !c.rows().is_empty());
            if !loaded || !report().composer_focused {
                return false;
            }
            let added = control("/__control/add-agent", &json!({"session": "theo-97e5", "persona": "Theo"}));
            check(added.is_ok(), &format!("Theo joins the roster: {added:?}"));
            true
        })),
        ("theo", Box::new(|app, _, _| app.engine.borrow().roster().find("theo-97e5").is_some())),
        // ---- a verified agent stopped it, by Ctrl+.
        working("verified agent working", json!({"ok": true, "terminated": 1, "stop_actor": "agent:theo-97e5", "stop_actor_verified": true,
            "stop_reason": "Pebble busy with no process", "queue_paused": true, "goals_paused": 1})),
        ("ctrl+.", Box::new(|_, _, _| {
            headless::press_with(&[Key::Control], ".");
            true
        })),
        says("verified agent", 1, "Stopped by Theo (verified): Pebble busy with no process · queue paused · 1 goal paused", out.clone(), "stop-01-verified-agent"),
        // ---- an admin token: the actor is the caller's claim; goals unknown
        working("claimed user working", json!({"ok": true, "terminated": 0, "stop_actor": "user", "stop_actor_verified": false,
            "stop_reason": "", "queue_paused": true})),
        stop_by_command("claimed user stop"),
        says("claimed user", 2, "Stopped by you (claimed) · queue paused", out.clone(), "stop-02-claimed-user"),
        // ---- a paired device: verified, and no goal was paused
        working("paired user working", json!({"ok": true, "terminated": 1, "stop_actor": "user", "stop_actor_verified": true,
            "stop_reason": "", "queue_paused": true, "goals_paused": 0})),
        stop_by_command("paired user stop"),
        says("paired user", 3, "Stopped by you · queue paused · no goals paused", out.clone(), "stop-03-paired-user"),
        // ---- an older Host (contract 60): every newer field unknown
        working("older host working", json!({"ok": true, "terminated": 1})),
        stop_by_command("older host stop"),
        says("older host", 4, "Stopped · effects unknown", out.clone(), "stop-04-older-host"),
        // ---- another device or agent stopped it: the state's detail
        working("detail working", json!({"ok": true})),
        ("detail", Box::new(|_, _, _| {
            let detail = json!({"source": "user_stop", "message": "Turn stopped", "stop_actor": "agent:theo-97e5",
                "stop_actor_verified": true, "stop_reason": "Pebble busy with no process"});
            let sent = state("interrupted", Some(detail));
            check(sent.is_ok(), &format!("Theo stops Rachel from elsewhere: {sent:?}"));
            true
        })),
        ("detail line", Box::new(move |_, _, elapsed| {
            if notice().is_empty() || elapsed < Duration::from_millis(300) {
                return false;
            }
            check(posts("/stop").len() == 4, "this desktop sent no Stop");
            check(notice() == THEO, &format!("the chat names who stopped it and why: {:?}", notice()));
            shot(&out_detail, "stop-05-user-stop-detail");
            true
        })),
        // ---- the runtime refused: no line, only the error
        working("refused working", json!({"ok": true, "terminated": 0, "stop_actor": "user", "stop_actor_verified": true, "stop_reason": ""})),
        stop_by_command("refused stop"),
        ("refused", Box::new(move |app, window, elapsed| {
            let error = app.engine.borrow().error().to_owned();
            if posts("/stop").len() < 5 || error.is_empty() || elapsed < Duration::from_millis(300) {
                return false;
            }
            check(error == "Stop did not take effect", &format!("a refused Stop is only an error: {error:?}"));
            check(notice().is_empty(), &format!("and no line: {:?}", notice()));
            check(window.get_error() == "Stop did not take effect", &format!("the window says so: {:?}", window.get_error()));
            shot(&out_refused, "stop-06-runtime-refused");
            true
        })),
        // ---- the paused queue, as the composer and the explorer say it
        ("queue paused", Box::new(|_, _, _| {
            let set = control("/__control/agent", &json!({"session": "rachel", "set": {"queued_turn_count": 2, "queue_paused": true}}));
            check(set.is_ok(), &format!("two messages wait in Rachel's paused queue: {set:?}"));
            true
        })),
        ("queue shown", Box::new(move |_, window, elapsed| {
            let pane = view();
            let row = window.get_chats().iter().find(|c| c.session == "rachel");
            if pane.queued != 2 || !pane.queue_paused || elapsed < Duration::from_millis(500) {
                return false;
            }
            check(row.is_some_and(|r| r.queued == 2 && r.queue_paused), "the explorer marks Rachel's queue paused");
            true
        })),
        // The queued lines fade in (RevealText): the shot waits for them.
        ("queue drawn", Box::new(move |_, _, elapsed| {
            if elapsed < Duration::from_secs(2) {
                return false;
            }
            shot(&out_queue, "stop-07-queue-paused");
            true
        })),
    ];
    run_stages(stages);
}

const HELD: &str = "Nothing is running; the next turn waits for the Clarp update";

fn live_status(activity: Value, turn: Option<Value>) -> Result<(), String> {
    let mut ops = Vec::new();
    if let Some(turn) = turn {
        ops.push(json!({"op": "turn", "conv": "conv-1", "turn": turn}));
    }
    ops.push(json!({"op": "status", "conv": "conv-1", "activity": activity}));
    control("/__control/live-event", &json!({"session": "rachel", "event": {"server_now_ms": now(), "ops": ops}}))
}

fn shortcut_bar(window: &crate::AppWindow) -> Vec<String> {
    window.get_hints().iter().map(|h| format!("{} {}", h.keys, h.label)).collect()
}

pub fn stop_held_check(out: String) {
    let (out_held, out_running) = (out.clone(), out.clone());
    let stages: Vec<Stage> = vec![
        ("ready", Box::new(|app, _, _| {
            let loaded = app.engine.borrow().conversation("rachel").is_some_and(|c| !c.rows().is_empty());
            if !loaded || !report().composer_focused {
                return false;
            }
            check(control("/__control/live", &json!({"on": true})).is_ok(), "the Host turns live items on");
            app.engine.borrow_mut().reconnect();
            true
        })),
        ("subscribed", Box::new(|app, _, _| {
            let engine = app.engine.borrow();
            engine.live_items() && engine.live_active("rachel") && engine.live_view("rachel").is_some_and(|v| v.lseq().is_some() && !v.awaiting_snapshot())
        })),
        // The release drain holds Rachel's next turn: limited, no turn.
        ("held", Box::new(|_, _, _| {
            let activity = json!({"state": "limited", "headline": "Waiting for the Clarp update", "turn_id": null, "turn_started_ms": null,
                "since_ms": now(), "tool": null, "running_tools": 0, "item_id": null});
            let sent = live_status(activity, None);
            check(sent.is_ok(), &format!("the Host holds Rachel's next turn: {sent:?}"));
            true
        })),
        ("held shown", Box::new(move |app, window, elapsed| {
            let pane = view();
            if pane.live_status != "◌ Waiting for the Clarp update" || elapsed < Duration::from_millis(500) {
                return false;
            }
            check(true, "the status line shows the headline");
            check(!pane.live_busy, "and does not say the agent works");
            check(pane.live_stop_key.is_empty(), &format!("no stop key: {:?}", pane.live_stop_key));
            check(!pane.busy && !pane.working, "the composer is not busy");
            check(!crate::live_view::ticking(&app.engine.borrow(), "rachel"), "nothing ticks");
            check(window.get_chats().iter().find(|c| c.session == "rachel").is_some_and(|c| !c.busy), "the explorer shows Rachel not working");
            let bar = shortcut_bar(window);
            check(!bar.iter().any(|h| h.ends_with(" Stop")), &format!("the shortcut bar offers no Stop: {bar:?}"));
            headless::press_with(&[Key::Control], ".");
            true
        })),
        ("ctrl+. says so", Box::new(move |_, window, elapsed| {
            if notice().is_empty() || elapsed < Duration::from_millis(500) {
                return false;
            }
            check(notice() == HELD, &format!("Ctrl+. says nothing runs: {:?}", notice()));
            check(posts("/stop").is_empty(), "and sends no /stop");
            check(crate::commands::run(&app_now(), window, "stop-agent"), "the Stop command runs");
            true
        })),
        ("command says so", Box::new(move |_, _, elapsed| {
            if elapsed < Duration::from_millis(500) {
                return false;
            }
            check(posts("/stop").is_empty(), "the Stop command (Ctrl+K) sends no /stop either");
            check(notice() == HELD, &format!("and the note stays: {:?}", notice()));
            shot(&out_held, "stop-held-01-drain");
            // A running turn under a usage limit: still working.
            let started = now() - 5_000;
            let turn = json!({"turn_id": "tr-limit", "status": "running", "started_at_ms": started, "ended_at_ms": null, "worked_ms": null, "tool_count": 0});
            let activity = json!({"state": "limited", "headline": "Waiting for the usage limit", "turn_id": "tr-limit", "turn_started_ms": started,
                "since_ms": now(), "tool": null, "running_tools": 0, "item_id": null});
            let sent = live_status(activity, Some(turn));
            check(sent.is_ok(), &format!("Rachel's running turn waits for a usage limit: {sent:?}"));
            true
        })),
        ("limited turn", Box::new(move |_, window, elapsed| {
            let pane = view();
            if pane.live_status != "◌ Waiting for the usage limit" || elapsed < Duration::from_millis(500) {
                return false;
            }
            check(pane.live_busy, "a running turn under a limit works");
            check(pane.live_stop_key == "Ctrl+.", &format!("and names the stop key: {:?}", pane.live_stop_key));
            check(notice().is_empty(), &format!("the held note is gone: {:?}", notice()));
            let bar = shortcut_bar(window);
            check(bar.iter().any(|h| h == "Ctrl+. Stop"), &format!("the shortcut bar offers Stop: {bar:?}"));
            shot(&out_running, "stop-held-02-limited-turn");
            headless::press_with(&[Key::Control], ".");
            true
        })),
        ("stopped", Box::new(|_, _, elapsed| {
            let posted = posts("/stop");
            if posted.is_empty() && elapsed < Duration::from_secs(3) {
                return false;
            }
            check(posted.len() == 1 && posted[0]["body"]["session"] == "rachel", &format!("Ctrl+. stops the running turn: {posted:?}"));
            true
        })),
    ];
    run_stages(stages);
}
