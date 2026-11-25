//! `--check kept-processes` (feature `lane_settings`, Rust Host contract
//! 178): a Host without the feature lists no KEPT PROCESSES; one with it
//! shows a switch per tool it names, the three numbers and what the lanes
//! hold now. Stepping "Most kept processes" posts the new value and shows
//! the Host's answer, a switch posts its tool's key, a refused value shows
//! the Host's sentence and keeps its value, and `lane-settings-updated`
//! reads what the lanes hold again.

use std::time::Duration;

use serde_json::json;
use slint::Model;

use super::{Stage, check, control, posts, run_stages, shot};

fn row(window: &crate::AppWindow, label: &str) -> Option<crate::SettingRow> {
    window.get_setting_rows().iter().find(|r| r.label == label)
}

fn by_id(window: &crate::AppWindow, id: &str) -> Option<crate::SettingRow> {
    window.get_setting_rows().iter().find(|r| r.id == id)
}

fn change(id: &str, delta: i32) {
    let (Some(app), Some(window)) = (crate::app(), crate::window()) else { return };
    crate::settings_view::change(&app, &window, id, delta);
}

pub fn kept_processes_check(out: String) {
    let (out1, out2) = (out.clone(), out.clone());
    let stages: Vec<Stage> = vec![
        ("without the feature", Box::new(|app, window, _| {
            if !app.engine.borrow().conversation("rachel").is_some_and(|c| !c.rows().is_empty()) {
                return false;
            }
            crate::settings_view::open(app, window);
            check(by_id(window, "kept-most").is_none() && row(window, "KEPT PROCESSES").is_none(), "a Host without lane_settings lists no kept processes");
            check(control("/__control/lane-settings", &json!({"on": true})).is_ok(), "the Host turns lane_settings on");
            app.engine.borrow_mut().reconnect();
            true
        })),
        ("shown", Box::new(move |app, window, _| {
            if app.engine.borrow().lane_settings().is_none() || by_id(window, "kept-most").is_none() {
                return false;
            }
            check(row(window, "KEPT PROCESSES").is_some_and(|r| r.kind == "section"), "the section is listed");
            check(row(window, "Claude kept between messages").is_some_and(|r| r.on), "Claude's switch, on");
            check(row(window, "Codex kept between messages").is_some_and(|r| r.on), "Codex's switch, on");
            check(by_id(window, "kept-tool-3").is_none(), "no switch for a tool the Host does not name");
            check(by_id(window, "kept-most").is_some_and(|r| r.detail == "8"), &format!("Most kept processes 8: {:?}", by_id(window, "kept-most").map(|r| r.detail)));
            check(by_id(window, "kept-memory").is_some_and(|r| r.detail == "2200 MB"), &format!("memory 2200 MB: {:?}", by_id(window, "kept-memory").map(|r| r.detail)));
            check(by_id(window, "kept-idle").is_some_and(|r| r.detail == "15 min"), "idle 15 min");
            let now = row(window, "Now").map(|r| r.detail.to_string()).unwrap_or_default();
            check(now == "8 kept · 1945 MB · claude 8 (2 busy) · codex 0", &format!("what the lanes hold now: {now:?}"));
            check(row(window, "When").is_some_and(|r| r.detail.contains("next message")), "says when a change takes effect");
            shot(&out1, "kept-processes-01");
            change("kept-most", 1);
            true
        })),
        ("most changed", Box::new(|app, window, elapsed| {
            let posted = posts("/lane-settings");
            let shown = by_id(window, "kept-most").is_some_and(|r| r.detail == "9");
            if (posted.is_empty() || !shown) && elapsed < Duration::from_secs(5) {
                return false;
            }
            check(posted.first().is_some_and(|p| p["body"] == json!({"most": 9})), &format!("Right posts most 9: {posted:?}"));
            check(shown, "the Host's answer shows 9");
            check(app.engine.borrow().lane_settings_error().is_empty(), "no refusal");
            change("kept-tool-2", 0);
            true
        })),
        ("switched", Box::new(|app, window, elapsed| {
            let posted = posts("/lane-settings");
            let off = row(window, "Codex kept between messages").is_some_and(|r| !r.on);
            if (posted.len() < 2 || !off) && elapsed < Duration::from_secs(5) {
                return false;
            }
            check(posted.get(1).is_some_and(|p| p["body"] == json!({"codex_chat_lane": false})), &format!("the switch posts its tool's key: {posted:?}"));
            check(off, "Codex's switch shows off");
            // A value out of the Host's range (as a stale range would let through).
            app.engine.borrow_mut().set_lane_setting("most", json!(40));
            true
        })),
        ("refused", Box::new(move |_, window, elapsed| {
            let refusal = row(window, "Not changed").map(|r| r.detail.to_string());
            let back = by_id(window, "kept-most").is_some_and(|r| r.detail == "9");
            if (refusal.is_none() || !back) && elapsed < Duration::from_secs(5) {
                return false;
            }
            check(refusal.as_deref() == Some("most must be between 1 and 32"), &format!("the Host's sentence as it came: {refusal:?}"));
            check(back, "the Host's value shows again");
            shot(&out2, "kept-processes-02-refused");
            let held = json!({"lanes": 3, "memory_mb": 500, "unanswered": 2, "tools": {"claude": {"lanes": 3, "busy": 0, "memory_mb": 500}}});
            check(control("/__control/lane-settings", &json!({"held": held})).is_ok(), "the lanes hold less");
            check(control("/__control/event", &json!({"type": "lane-settings-updated", "most": 9})).is_ok(), "another device changed something");
            true
        })),
        ("event", Box::new(|_, window, elapsed| {
            let now = row(window, "Now").map(|r| r.detail.to_string()).unwrap_or_default();
            if !now.starts_with("3 kept") && elapsed < Duration::from_secs(5) {
                return false;
            }
            check(now == "3 kept · 500 MB · claude 3 · 2 did not answer in time, not counted", &format!("the event reads what they hold again: {now:?}"));
            true
        })),
    ];
    run_stages(stages);
}
