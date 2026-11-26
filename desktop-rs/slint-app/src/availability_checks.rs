//! `--check agent-availability` (#918, feature `agent_availability`): when
//! the Host says an agent cannot answer now (`availability` on its snapshot
//! row, then live in an `agent-roster` event of kind `availability`), the
//! chat row's status line and the open chat's header carry one line with
//! why and for how long ("Restarting · back in ~20 s"). `ok` takes it away.
//! A `host-updating` event puts "Host updating" on every agent until the
//! desktop reconnects. A Host without the feature shows nothing new,
//! whatever its rows carry.

use std::time::Duration;

use serde_json::{Value, json};
use slint::Model;

use super::{Stage, check, control, run_stages, shot, view};

fn now() -> i64 {
    chrono::Utc::now().timestamp_millis()
}

fn activity(window: &crate::AppWindow, session: &str) -> String {
    window.get_chats().iter().find(|c| c.session == session).map(|c| c.activity.to_string()).unwrap_or_default()
}

/// The header's line as drawn: the frame's info text of the open pane.
fn header_info() -> String {
    use i_slint_backend_testing::ElementQuery;
    let Some(window) = crate::window() else { return String::new() };
    let id = format!("pane-info:{}", view().id);
    ElementQuery::from_root(&window)
        .match_predicate(move |e| e.accessible_id().is_some_and(|a| a == id.as_str()) && e.size().width > 0.0)
        .find_first()
        .and_then(|e| e.accessible_label())
        .map(|l| l.to_string())
        .unwrap_or_default()
}

/// A row's `availability` as the Host writes it: its text without the
/// time part, which the desktop adds.
fn availability(state: &str, text: &str, back_in_ms: Option<i64>, queue_ahead: i64) -> Value {
    json!({"state": state, "text": text, "since_ms": now(), "back_at_ms": back_in_ms.map(|ms| now() + ms),
        "queue_ahead": queue_ahead, "last_alive_ms": null})
}

/// Rachel's snapshot row says `value` (the list is read again).
fn rachel_row(value: Value) -> Result<(), String> {
    control("/__control/agent", &json!({"session": "rachel", "set": {"availability": value}}))
}

/// Rachel's change as the Host sends it live: an `agent-roster` event of
/// kind `availability` carrying the field.
fn rachel_event(value: Value) -> Result<(), String> {
    control("/__control/roster-event", &json!({"session": "rachel", "kind": "availability", "set": {"availability": value}}))
}

/// Reconnects with the `agent_availability` feature on or off (and the
/// rows' `fields`, which carry it live).
fn host_feature(on: bool) -> Stage {
    let mut asked = false;
    (if on { "feature on" } else { "feature off" }, Box::new(move |app, _, elapsed| {
        if !std::mem::replace(&mut asked, true) {
            let set = control("/__control/availability-feature", &json!({"on": on}));
            let fields = control("/__control/roster-rows", &json!({"fields": on, "row": false}));
            check(set.is_ok() && fields.is_ok(), &format!("the Host's agent_availability feature is {}: {set:?} {fields:?}", if on { "on" } else { "off" }));
            app.engine.borrow_mut().reconnect();
            return false;
        }
        elapsed > Duration::from_millis(1500) && !app.engine.borrow().connecting()
    }))
}

/// The line drawn in both places: Rachel's row reads `prefix…`, as do the
/// pane's line and its header.
fn shows(name: &'static str, prefix: &'static str, out: String, shot_name: &'static str) -> Stage {
    (name, Box::new(move |_, window, elapsed| {
        let (row, line, header) = (activity(window, "rachel"), view().availability.to_string(), header_info());
        let all = row.starts_with(prefix) && line.starts_with(prefix) && header.contains(prefix);
        if !all && elapsed < Duration::from_secs(5) {
            return false;
        }
        check(row.starts_with(prefix), &format!("{name}: the chat row reads {prefix:?}: {row:?}"));
        check(line.starts_with(prefix), &format!("{name}: the pane's line reads {prefix:?}: {line:?}"));
        check(header.contains(prefix), &format!("{name}: the header shows it: {header:?}"));
        check(!header.contains("working"), &format!("{name}: it takes the place of the header's working: {header:?}"));
        shot(&out, shot_name);
        true
    }))
}

fn gone(name: &'static str, out: String, shot_name: &'static str) -> Stage {
    (name, Box::new(move |_, window, elapsed| {
        let line = view().availability.to_string();
        if (!line.is_empty() && elapsed < Duration::from_secs(5)) || elapsed < Duration::from_secs(1) {
            return false;
        }
        check(line.is_empty(), &format!("{name}: the pane says nothing of it: {line:?}"));
        let row = activity(window, "rachel");
        for word in ["Restarting", "Busy", "Host updating", "Usage limit"] {
            check(!row.starts_with(word), &format!("{name}: nor the chat row: {row:?}"));
        }
        shot(&out, shot_name);
        true
    }))
}

pub fn agent_availability_check(out: String) {
    let stages: Vec<Stage> = vec![
        ("ready", Box::new(|app, _, _| app.engine.borrow().conversation("rachel").is_some_and(|c| !c.rows().is_empty()))),
        // ---- an older Host: the field is ignored
        host_feature(false),
        ("older host", Box::new(|_, _, _| {
            let sent = rachel_row(availability("restarting", "Restarting", Some(20_500), 0));
            check(sent.is_ok(), &format!("an older Host's row says Rachel restarts: {sent:?}"));
            true
        })),
        gone("older host shows nothing", out.clone(), "availability-01-older-host"),
        // ---- a current Host: the snapshot's row brings it
        host_feature(true),
        ("restarting", Box::new(|_, _, _| {
            let sent = rachel_row(availability("restarting", "Restarting", Some(20_500), 0));
            check(sent.is_ok(), &format!("Rachel restarts: {sent:?}"));
            true
        })),
        shows("restarting", "Restarting · back in ~", out.clone(), "availability-02-restarting"),
        // ---- the agent-roster event of kind availability patches it live
        ("busy", Box::new(|_, _, _| {
            let sent = rachel_event(availability("busy", "Busy · 25 messages ahead of you", None, 25));
            check(sent.is_ok(), &format!("Rachel is busy with a queue: {sent:?}"));
            true
        })),
        shows("busy", "Busy · 25 messages ahead of you", out.clone(), "availability-03-busy"),
        // ---- ok clears it
        ("ok", Box::new(|_, _, _| {
            let sent = rachel_event(availability("ok", "", None, 0));
            check(sent.is_ok(), &format!("Rachel can answer again: {sent:?}"));
            true
        })),
        gone("ok", out.clone(), "availability-04-ok"),
        // ---- the Host updating: every agent says so until it is back
        ("host updating", Box::new(|_, _, _| {
            let sent = control("/__control/event", &json!({"type": "host-updating", "since_ms": now(), "back_at_ms": now() + 40_000, "text": "Host updating"}));
            check(sent.is_ok(), &format!("the Host is about to restart: {sent:?}"));
            true
        })),
        shows("host updating", "Host updating · back in ~", out.clone(), "availability-05-host-updating"),
        ("every agent", Box::new(|_, window, _| {
            let mike = activity(window, "mike");
            check(mike.starts_with("Host updating"), &format!("Mike's row says it too: {mike:?}"));
            true
        })),
        host_feature(true),
        gone("back up", out, "availability-06-back-up"),
    ];
    run_stages(stages);
}
