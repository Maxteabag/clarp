//! `--check availability` (#918, desktop #924): when the Host says an agent
//! cannot answer now (`availability` on its row, or an `agent-roster` event
//! of kind `availability`), the chat-list row's preview and the open chat's
//! header line say why: the Host's `text`, with the time part counted here
//! ("Restarting · back in ~20 s", "Busy · 25 messages ahead of you"). A `host-updating` event puts "Host updating" on every
//! agent until the snapshot is read again; `ok` takes the line away.

use std::time::Duration;

use serde_json::json;
use slint::Model;

use super::{Stage, check, control, run_stages, shot, view};

fn now() -> i64 {
    chrono::Utc::now().timestamp_millis()
}

fn row_line(window: &crate::AppWindow, session: &str) -> String {
    window.get_chats().iter().find(|c| c.session == session).map(|c| c.availability.to_string()).unwrap_or_default()
}

/// Waits up to 5 s until Rachel's row and the open chat's header both
/// start with `expected`, then checks and shoots.
fn shows(name: &'static str, expected: &'static str, out: String, shot_name: &'static str) -> Stage {
    (name, Box::new(move |_, window, elapsed| {
        let (row, header) = (row_line(window, "rachel"), view().availability.to_string());
        let ok = |t: &str| if expected.is_empty() { t.is_empty() } else { t.starts_with(expected) };
        if !(ok(&row) && ok(&header)) && elapsed < Duration::from_secs(5) {
            return false;
        }
        check(ok(&row), &format!("{name}: the chat row reads {expected:?}: {row:?}"));
        check(ok(&header), &format!("{name}: the chat header reads {expected:?}: {header:?}"));
        shot(&out, shot_name);
        true
    }))
}

pub fn availability_check(out: String) {
    let stages: Vec<Stage> = vec![
        ("ready", Box::new(|app, _, _| app.engine.borrow().conversation("rachel").is_some_and(|c| !c.rows().is_empty()))),
        shows("older host", "", out.clone(), "availability-01-none"),
        // ---- the snapshot's row
        ("restarting", Box::new(|_, _, _| {
            let set = control("/__control/agent", &json!({"session": "rachel", "set": {"availability": {"state": "restarting",
                "text": "Restarting", "since_ms": now(), "back_at_ms": now() + 19_000, "queue_ahead": 0, "last_alive_ms": null}}}));
            check(set.is_ok(), &format!("Rachel restarts: {set:?}"));
            true
        })),
        shows("restarting", "Restarting · back in ~20 s", out.clone(), "availability-02-restarting"),
        // ---- the agent-roster event of kind availability
        ("busy", Box::new(|_, _, _| {
            let set = control("/__control/roster-event", &json!({"session": "rachel", "kind": "availability", "set": {"availability": {
                "state": "busy", "text": "Busy · 25 messages ahead of you", "since_ms": now(), "back_at_ms": null, "queue_ahead": 25, "last_alive_ms": now()}}}));
            check(set.is_ok(), &format!("Rachel is busy with 25 ahead: {set:?}"));
            true
        })),
        shows("busy", "Busy · 25 messages ahead of you", out.clone(), "availability-03-busy"),
        // ---- the Host restarts: every agent says so until the snapshot
        ("host updating", Box::new(|_, _, _| {
            let sent = control("/__control/event", &json!({"type": "host-updating", "text": "Host updating", "since_ms": now(), "back_at_ms": now() + 39_500}));
            check(sent.is_ok(), &format!("the Host says it updates: {sent:?}"));
            true
        })),
        shows("host updating", "Host updating · back in ~40 s", out.clone(), "availability-04-host-updating"),
        ("every agent", Box::new(|_, window, _| {
            let lines: Vec<String> = window.get_chats().iter().map(|c| c.availability.to_string()).collect();
            check(!lines.is_empty() && lines.iter().all(|l| l.starts_with("Host updating")), &format!("every row says the Host updates: {lines:?}"));
            true
        })),
        ("reconnect", Box::new({
            let mut asked = false;
            move |app, _, elapsed| {
                if !std::mem::replace(&mut asked, true) {
                    app.engine.borrow_mut().reconnect();
                    return false;
                }
                elapsed > Duration::from_millis(1500) && !app.engine.borrow().connecting()
            }
        })),
        shows("back up", "Busy · 25 messages ahead of you", out.clone(), "availability-05-back"),
        // ---- ok: the line goes, the preview is back
        ("ok", Box::new(|_, _, _| {
            let set = control("/__control/roster-event", &json!({"session": "rachel", "kind": "availability", "set": {"availability": {
                "state": "ok", "text": "", "since_ms": now(), "back_at_ms": null, "queue_ahead": 0, "last_alive_ms": null}}}));
            check(set.is_ok(), &format!("Rachel can answer: {set:?}"));
            true
        })),
        shows("ok", "", out, "availability-06-ok"),
    ];
    run_stages(stages);
}
