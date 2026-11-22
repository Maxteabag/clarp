//! `--check stalled-turn` (#249, Host contract 149): when the Host says a
//! turn is stalled (`stalled_since`, `stalled_action: "stop"`), the open
//! chat's working indicator reads "Seems stuck · no progress for N min",
//! adds "Your message is waiting" while a message is queued, and holds the
//! existing Stop; the chat row reads "Seems stuck". The Host's clear takes
//! both away. A Host without the `stalled_turn` feature shows nothing new,
//! whatever its rows carry.

use std::time::Duration;

use serde_json::json;
use slint::Model;

use super::{Stage, check, control, posts, run_stages, shot, view};

const STALLED_FOR_MS: i64 = 12 * 60_000 + 30_000;

fn now() -> i64 {
    chrono::Utc::now().timestamp_millis()
}

/// The drawn element whose accessible id is `id`.
fn element(id: &str) -> Option<i_slint_backend_testing::ElementHandle> {
    use i_slint_backend_testing::ElementQuery;
    let window = crate::window()?;
    let id = id.to_owned();
    ElementQuery::from_root(&window)
        .match_predicate(move |e| e.accessible_id().is_some_and(|a| a == id.as_str()) && e.size().width > 0.0 && e.size().height > 0.0)
        .find_first()
}

fn drawn(id: &str) -> bool {
    element(id).is_some()
}

/// Presses the element as a screen reader or a click would.
fn press(id: &str) -> bool {
    element(id).map(|e| e.invoke_accessible_default_action()).is_some()
}

fn row_activity(window: &crate::AppWindow) -> String {
    window.get_chats().iter().find(|c| c.session == "rachel").map(|c| c.activity.to_string()).unwrap_or_default()
}

/// Rachel's tool has been silent while the Host marks the turn stalled.
fn stalled_rachel(since: i64) -> Result<(), String> {
    control(
        "/__control/agent",
        &json!({"session": "rachel", "set": {"busy": true, "latest_state": "tool", "status_text": "Running a tool",
            "stalled_since": since, "stalled_action": "stop"}}),
    )
}

/// Reconnects with the `stalled_turn` feature on or off.
fn host_feature(on: bool) -> Stage {
    let mut asked = false;
    (if on { "feature on" } else { "feature off" }, Box::new(move |app, _, elapsed| {
        if !std::mem::replace(&mut asked, true) {
            let set = control("/__control/stall-feature", &json!({"on": on}));
            check(set.is_ok(), &format!("the Host's stalled_turn feature is {}: {set:?}", if on { "on" } else { "off" }));
            app.engine.borrow_mut().reconnect();
            return false;
        }
        elapsed > Duration::from_millis(1500) && !app.engine.borrow().connecting()
    }))
}

/// The stall drawn: `expected` in the indicator, "Seems stuck" on the row.
fn shows(name: &'static str, expected: &'static str, out: String, shot_name: &'static str) -> Stage {
    (name, Box::new(move |_, window, elapsed| {
        let stall = view().stall.to_string();
        if stall != expected && elapsed < Duration::from_secs(5) {
            return false;
        }
        check(stall == expected, &format!("{name}: the working indicator reads {expected:?}: {stall:?}"));
        check(row_activity(window) == clarp_core::stall::ROW_STATUS, &format!("{name}: the chat row reads Seems stuck: {:?}", row_activity(window)));
        check(drawn(&format!("stall-stop:{}", view().id)), &format!("{name}: the indicator holds Stop"));
        shot(&out, shot_name);
        true
    }))
}

pub fn stalled_turn_check(out: String) {
    let (out_old, out_queued, out_clear) = (out.clone(), out.clone(), out.clone());
    let stages: Vec<Stage> = vec![
        ("ready", Box::new(|app, _, _| app.engine.borrow().conversation("rachel").is_some_and(|c| !c.rows().is_empty()))),
        // ---- an older Host: the fields are ignored
        host_feature(false),
        ("older host stalls", Box::new(|_, _, _| {
            let sent = stalled_rachel(now() - STALLED_FOR_MS);
            check(sent.is_ok(), &format!("an older Host's row says Rachel is stalled: {sent:?}"));
            true
        })),
        ("older host shows nothing", Box::new(move |app, window, elapsed| {
            let busy = app.engine.borrow().roster().find("rachel").is_some_and(|a| a.busy);
            if !busy || elapsed < Duration::from_secs(1) {
                return false;
            }
            check(view().stall.is_empty(), &format!("without the feature the indicator says nothing new: {:?}", view().stall));
            check(row_activity(window) != clarp_core::stall::ROW_STATUS, &format!("nor the chat row: {:?}", row_activity(window)));
            shot(&out_old, "stall-01-older-host");
            true
        })),
        // ---- a current Host: the snapshot's stall shows, ticking from stalled_since
        host_feature(true),
        ("stalled", Box::new(|_, _, _| {
            let sent = stalled_rachel(now() - STALLED_FOR_MS);
            check(sent.is_ok(), &format!("Rachel's turn is stalled: {sent:?}"));
            true
        })),
        shows("stalled", "Seems stuck · no progress for 12 min", out.clone(), "stall-02-stalled"),
        // ---- a queued message adds the waiting words
        ("queued", Box::new(|_, _, _| {
            let set = control("/__control/agent", &json!({"session": "rachel", "set": {"queued_turn_count": 1}}));
            check(set.is_ok(), &format!("a message waits in Rachel's queue: {set:?}"));
            true
        })),
        shows("queued", "Seems stuck · no progress for 12 min · Your message is waiting", out_queued, "stall-03-message-waiting"),
        // ---- the indicator's Stop is the existing Stop
        ("stop", Box::new(|_, _, _| {
            let id = format!("stall-stop:{}", view().id);
            check(press(&id), "the indicator's Stop is pressed");
            true
        })),
        ("stop sent", Box::new(|_, _, elapsed| {
            let posted = posts("/stop");
            if posted.is_empty() && elapsed < Duration::from_secs(5) {
                return false;
            }
            check(posted.last().is_some_and(|p| p["body"]["session"] == "rachel"), &format!("Stop goes to the Host for Rachel: {posted:?}"));
            // The Host clears the stall on the activity event.
            let cleared = control("/__control/event", &json!({"type": "agent-activity", "session": "rachel", "status": "running",
                "stalled_since": null, "stalled_action": null}));
            check(cleared.is_ok(), &format!("the Host clears the stall: {cleared:?}"));
            true
        })),
        ("cleared", Box::new(move |_, window, elapsed| {
            if !view().stall.is_empty() && elapsed < Duration::from_secs(5) {
                return false;
            }
            check(view().stall.is_empty(), &format!("the indicator no longer says it seems stuck: {:?}", view().stall));
            check(row_activity(window) != clarp_core::stall::ROW_STATUS, &format!("nor the chat row: {:?}", row_activity(window)));
            shot(&out_clear, "stall-04-cleared");
            let idle = control("/__control/agent", &json!({"session": "rachel", "set": {"busy": false, "latest_state": "idle",
                "status_text": "", "queued_turn_count": 0, "stalled_since": null, "stalled_action": null}}));
            check(idle.is_ok(), "Rachel is idle again");
            true
        })),
    ];
    run_stages(stages);
}
