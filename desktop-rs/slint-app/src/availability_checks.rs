//! `--check agent-availability` (#918, feature `agent_availability`): when
//! the Host says an agent cannot answer now (`availability` on its snapshot
//! row, then live in an `agent-roster` event of kind `availability`), the
//! open chat's header carries one short line under the agent's name, beside
//! its avatar ("Restarting · ~20 s"); there is no banner, and the name is no
//! longer on the frame's border. Busy shows there only while the running
//! turn answers other agents ("Answering other agents · 1 ahead of you"):
//! on the user's own turn the header says "working" and the working
//! indicator at the bottom is drawn as before. No sign of life shows over
//! the user's turn too. The chat list's row keeps its own line, as before
//! ("Restarting · back in ~20 s", "Busy · 1 message ahead of you"). `ok`
//! takes both away. A `host-updating` event puts "Host updating" on every
//! agent until the desktop reconnects. A Host without the feature shows
//! nothing new, whatever its rows carry.

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

/// The drawn element of the open pane with accessible id `prefix<pane id>`
/// and a size: its accessible label, or None.
fn drawn(prefix: &str) -> Option<String> {
    use i_slint_backend_testing::ElementQuery;
    let window = crate::window()?;
    let id = format!("{prefix}{}", view().id);
    ElementQuery::from_root(&window)
        .match_predicate(move |e| e.accessible_id().is_some_and(|a| a == id.as_str()) && e.size().width > 0.0 && e.size().height > 0.0)
        .find_first()
        .and_then(|e| e.accessible_label())
        .map(|l| l.to_string())
}

/// The header's subtitle as drawn under the agent's name ("" when none).
fn header_info() -> String {
    drawn("pane-subtitle:").unwrap_or_default()
}

/// The pane ids drawn now, for a check that found none it looked for.
fn drawn_ids() -> Vec<String> {
    use i_slint_backend_testing::ElementQuery;
    let Some(window) = crate::window() else { return vec!["no window".into()] };
    ElementQuery::from_root(&window)
        .match_predicate(|e| e.accessible_id().is_some_and(|a| a.starts_with("pane-")))
        .find_all()
        .into_iter()
        .filter_map(|e| Some(format!("{}={:?} {}x{}", e.accessible_id()?, e.accessible_label().unwrap_or_default(), e.size().width, e.size().height)))
        .collect()
}

/// The bottom working indicator ("Working…", `TypingIndicator`) as drawn.
fn indicator_drawn() -> bool {
    super::labelled("Agent is working").is_some_and(|(_, _, width, height)| width > 0.0 && height > 0.0)
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

/// Rachel's row works on a turn for `audience` (`user`, `agents`), or is
/// idle (None), and says `value` (the list is read again).
fn rachel_turn(audience: Option<&str>, value: Value) -> Result<(), String> {
    let set = match audience {
        Some(audience) => json!({"busy": true, "latest_state": "thinking", "working_audience": audience, "availability": value}),
        None => json!({"busy": false, "latest_state": "idle", "working_audience": null, "availability": value}),
    };
    control("/__control/agent", &json!({"session": "rachel", "set": set}))
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

/// The header's line (the pane's and as drawn) reads `header_prefix…`;
/// Rachel's chat row, unchanged, reads `row_prefix…`.
fn shows(name: &'static str, header_prefix: &'static str, row_prefix: &'static str, out: String, shot_name: &'static str) -> Stage {
    (name, Box::new(move |_, window, elapsed| {
        let (row, line, header) = (activity(window, "rachel"), view().availability.to_string(), header_info());
        let all = row.starts_with(row_prefix) && line.starts_with(header_prefix) && header.contains(header_prefix);
        if !all && elapsed < Duration::from_secs(5) {
            return false;
        }
        check(row.starts_with(row_prefix), &format!("{name}: the chat row reads {row_prefix:?}, as before: {row:?}"));
        check(line.starts_with(header_prefix), &format!("{name}: the pane's line reads {header_prefix:?}: {line:?}"));
        check(header.contains(header_prefix), &format!("{name}: the header shows it under the name: {header:?} (drawn: {:?})", if header.is_empty() { drawn_ids() } else { vec![] }));
        check(drawn("pane-name:").as_deref() == Some("Agent profile"), &format!("{name}: the agent's name is drawn beside its avatar: {:?}", drawn("pane-name:")));
        check(drawn("pane-info:").is_none(), &format!("{name}: and not on the frame's border: {:?}", drawn("pane-info:")));
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
        for word in ["Restarting", "Busy", "Not responding", "Host updating", "Usage limit"] {
            check(!row.starts_with(word), &format!("{name}: nor the chat row: {row:?}"));
        }
        shot(&out, shot_name);
        true
    }))
}

thread_local! {
    /// The pane's working indicator on the user's own turn before the Host
    /// says busy.
    static WORKING: std::cell::Cell<Option<bool>> = const { std::cell::Cell::new(None) };
}

pub fn agent_availability_check(out: String) {
    let out_own = out.clone();
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
        // ---- quiet: an idle agent that cannot answer says why, in short
        shows("restarting", "Restarting · ~", "Restarting · back in ~", out.clone(), "availability-02-restarting"),
        // ---- the user's own turn: busy is the working indicator's
        ("own turn", Box::new(|app, _, _| {
            // The working indicator at the bottom of the chat shows with
            // this setting (Settings: show "Working…" until the reply is
            // ready); it is on here so the check sees it drawn.
            app.engine.borrow_mut().set_show_when_ready(true);
            let sent = rachel_turn(Some("user"), availability("ok", "", None, 0));
            check(sent.is_ok(), &format!("Rachel works on the user's message: {sent:?}"));
            true
        })),
        ("own turn working", Box::new(|_, _, elapsed| {
            let (pane, header) = (view(), header_info());
            if (!pane.busy || !header.contains("working") || !indicator_drawn()) && elapsed < Duration::from_secs(5) {
                return false;
            }
            check(pane.availability.is_empty() && header.contains("working"), &format!("the header says working: {header:?} (drawn: {:?})", drawn_ids()));
            check(pane.working && indicator_drawn(), &format!("the working indicator is drawn at the bottom: {} {}", pane.working, indicator_drawn()));
            WORKING.with(|w| w.set(Some(pane.working && indicator_drawn())));
            let sent = rachel_event(availability("busy", "Busy · 1 message ahead of you", None, 1));
            check(sent.is_ok(), &format!("the Host says Rachel is busy: {sent:?}"));
            true
        })),
        ("own turn quiet", Box::new(move |_, window, elapsed| {
            let (pane, header, row) = (view(), header_info(), activity(window, "rachel"));
            // The chat row taking the busy line is the sign the event arrived.
            if elapsed < Duration::from_millis(1500) || (!row.starts_with("Busy") && elapsed < Duration::from_secs(5)) {
                return false;
            }
            check(pane.availability.is_empty(), &format!("busy on the user's own turn is no line: {:?}", pane.availability));
            check(header.contains("working") && !header.contains("Busy") && !header.contains("Answering"), &format!("the header says working, as before: {header:?}"));
            check(row.starts_with("Busy · 1 message ahead of you"), &format!("the chat row keeps its line, as before: {row:?}"));
            let (before, now) = (WORKING.with(std::cell::Cell::get), pane.working && indicator_drawn());
            check(before == Some(true) && now, &format!("the working indicator is drawn, as it was: {before:?} then {now}"));
            shot(&out_own, "availability-03-own-turn");
            true
        })),
        // ---- other agents' turn: the user waits
        ("other agents", Box::new(|_, _, _| {
            let sent = rachel_turn(Some("agents"), availability("busy", "Busy · 1 message ahead of you", None, 1));
            check(sent.is_ok(), &format!("Rachel answers another agent: {sent:?}"));
            true
        })),
        shows("other agents", "Answering other agents · 1 ahead of you", "Busy · 1 message ahead of you", out.clone(), "availability-04-other-agents"),
        ("other agents indicator", Box::new(|_, _, _| {
            check(view().working && indicator_drawn(), "the working indicator stays as it is while it answers other agents");
            true
        })),
        // ---- no sign of life shows over the user's own turn
        ("silent", Box::new(|_, _, _| {
            let silent = json!({"state": "unresponsive", "text": "Not responding", "since_ms": now(), "back_at_ms": null,
                "queue_ahead": 0, "last_alive_ms": now() - 4 * 60_000 - 5_000});
            let sent = rachel_turn(Some("user"), silent);
            check(sent.is_ok(), &format!("Rachel's turn for the user went silent: {sent:?}"));
            true
        })),
        shows("silent", "No sign of life for 4 min", "Not responding · last sign of life 4 min ago", out.clone(), "availability-05-silent"),
        // ---- ok clears it
        ("ok", Box::new(|_, _, _| {
            let sent = rachel_turn(None, availability("ok", "", None, 0));
            check(sent.is_ok(), &format!("Rachel can answer again: {sent:?}"));
            true
        })),
        gone("ok", out.clone(), "availability-06-ok"),
        // ---- the Host updating: every agent says so until it is back
        ("host updating", Box::new(|_, _, _| {
            let sent = control("/__control/event", &json!({"type": "host-updating", "since_ms": now(), "back_at_ms": now() + 40_000, "text": "Host updating"}));
            check(sent.is_ok(), &format!("the Host is about to restart: {sent:?}"));
            true
        })),
        shows("host updating", "Host updating · ~", "Host updating · back in ~", out.clone(), "availability-07-host-updating"),
        ("every agent", Box::new(|_, window, _| {
            let mike = activity(window, "mike");
            check(mike.starts_with("Host updating"), &format!("Mike's row says it too: {mike:?}"));
            true
        })),
        host_feature(true),
        gone("back up", out, "availability-08-back-up"),
    ];
    run_stages(stages);
}
