//! `--check stop-receipt`: after Stop the chat says what the Host committed,
//! who stopped it and why (Host contract 61), never what will happen next.
//! The fake Host answers each Stop with one of the contract's cases: a
//! verified agent, a claimed user (an admin token), a paired user, an older
//! Host and a Stop the runtime refused; Ctrl+. stops the first, the Stop
//! command (Ctrl+K) the rest. Another device's or agent's Stop, read from
//! the interrupted state's `user_stop` detail, shows the same line. A queue
//! the Stop paused says so in the composer and the explorer.

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
