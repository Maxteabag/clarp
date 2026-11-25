//! `--check kept-processes` (feature `lane_settings`, Rust Host contract
//! 178): a Host without the feature lists no KEPT PROCESSES; one with it
//! shows a switch per tool it names, the three numbers and what the lanes
//! hold now. Stepping "Most kept processes" posts the new value and shows
//! the Host's answer, a switch posts its tool's key, a refused value shows
//! the Host's sentence and keeps its value, and `lane-settings-updated`
//! reads what the lanes hold again. Then the Host offers `lane_auto_limits`
//! (contract 180): Limits shows Auto with the memory kept free, the most in
//! auto and what the machine has free, the fixed limits say they are fixed;
//! switching Limits posts it and hides the auto rows, and stepping a fixed
//! limit in auto posts it alone and shows Fixed. Last the Host offers
//! `lanes_live` (contract 194; `turn_pressure_admission` on an older Host): no most kept in auto and nothing of the machine's pressure;
//! below the limits Live processes and Cold starts, last hour, each with a
//! line per tool.

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

/// The information rows from `label` on, as (label, detail), `n` of them.
fn info_from(window: &crate::AppWindow, label: &str, n: usize) -> Vec<(String, String)> {
    window
        .get_setting_rows()
        .iter()
        .filter(|r| r.kind == "info")
        .skip_while(|r| r.label != label)
        .take(n)
        .map(|r| (r.label.to_string(), r.detail.to_string()))
        .collect()
}

fn pairs(expected: &[(&str, &str)]) -> Vec<(String, String)> {
    expected.iter().map(|(l, d)| ((*l).to_owned(), (*d).to_owned())).collect()
}

fn change(id: &str, delta: i32) {
    let (Some(app), Some(window)) = (crate::app(), crate::window()) else { return };
    crate::settings_view::change(&app, &window, id, delta);
}

pub fn kept_processes_check(out: String) {
    let (out1, out2, out3, out4) = (out.clone(), out.clone(), out.clone(), out.clone());
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
            check(row(window, "Live processes").is_none() && by_id(window, "kept-pressure-cpu").is_none(), "no live lanes without lanes_live");
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
        ("event", Box::new(|app, window, elapsed| {
            let now = row(window, "Now").map(|r| r.detail.to_string()).unwrap_or_default();
            if !now.starts_with("3 kept") && elapsed < Duration::from_secs(5) {
                return false;
            }
            check(now == "3 kept · 500 MB · claude 3 · 2 did not answer in time, not counted", &format!("the event reads what they hold again: {now:?}"));
            check(by_id(window, "kept-limits").is_none(), "no Limits without lane_auto_limits");
            check(control("/__control/lane-settings", &json!({"auto": true})).is_ok(), "the Host turns lane_auto_limits on");
            app.engine.borrow_mut().reconnect();
            true
        })),
        ("auto shown", Box::new(move |_, window, elapsed| {
            let shown = by_id(window, "kept-limits").is_some_and(|r| r.detail == "Auto");
            if !shown && elapsed < Duration::from_secs(8) {
                return false;
            }
            let detail = |id: &str| by_id(window, id).map(|r| r.detail.to_string());
            check(shown, &format!("Limits shows Auto: {:?}", detail("kept-limits")));
            check(detail("kept-reserve").as_deref() == Some("20%"), &format!("memory kept free 20%: {:?}", detail("kept-reserve")));
            check(detail("kept-ceiling").as_deref() == Some("24"), &format!("most in auto 24: {:?}", detail("kept-ceiling")));
            let free = row(window, "Free memory").map(|r| r.detail.to_string()).unwrap_or_default();
            check(free == "15800 MB free · 6200 MB kept free · 9600 MB room for kept processes", &format!("what the machine has free: {free:?}"));
            check(detail("kept-most").as_deref() == Some("9 · fixed limit, setting it switches to fixed"), &format!("most says it is a fixed limit: {:?}", detail("kept-most")));
            check(detail("kept-memory").is_some_and(|d| d.starts_with("2200 MB · fixed limit")), &format!("memory says it is a fixed limit: {:?}", detail("kept-memory")));
            check(row(window, "Fixed limits").is_some_and(|r| r.detail.contains("switches the Host to fixed")), "the fixed-limits note");
            shot(&out3, "kept-processes-03-auto");
            change("kept-limits", 1);
            true
        })),
        ("switched to fixed", Box::new(|_, window, elapsed| {
            let posted = posts("/lane-settings");
            let fixed = by_id(window, "kept-limits").is_some_and(|r| r.detail == "Fixed");
            if (!fixed || by_id(window, "kept-reserve").is_some()) && elapsed < Duration::from_secs(5) {
                return false;
            }
            check(posted.last().is_some_and(|p| p["body"] == json!({"limits": "fixed"})), &format!("Right posts limits fixed: {:?}", posted.last()));
            check(fixed, "Limits shows Fixed");
            check(by_id(window, "kept-reserve").is_none() && by_id(window, "kept-ceiling").is_none(), "the auto rows leave in fixed");
            check(row(window, "Fixed limits").is_none() && row(window, "Free memory").is_none(), "no fixed-limits note or free memory in fixed");
            check(by_id(window, "kept-most").is_some_and(|r| r.detail == "9"), "most is plain in fixed");
            change("kept-limits", 1);
            true
        })),
        ("auto again", Box::new(|_, window, elapsed| {
            let auto = by_id(window, "kept-limits").is_some_and(|r| r.detail == "Auto") && by_id(window, "kept-reserve").is_some();
            if !auto && elapsed < Duration::from_secs(5) {
                return false;
            }
            check(auto, "Limits shows Auto again");
            change("kept-most", 1);
            true
        })),
        ("a fixed limit switches to fixed", Box::new(|app, window, elapsed| {
            let posted = posts("/lane-settings");
            let done = by_id(window, "kept-most").is_some_and(|r| r.detail == "10") && by_id(window, "kept-limits").is_some_and(|r| r.detail == "Fixed");
            if !done && elapsed < Duration::from_secs(5) {
                return false;
            }
            check(posted.last().is_some_and(|p| p["body"] == json!({"most": 10})), &format!("most posted alone: {:?}", posted.last()));
            check(done, "most 10, and the Host switched to fixed");
            let held = json!({"lanes": 8, "memory_mb": 1945, "unanswered": 0, "tools": {"claude": {"lanes": 8, "busy": 2, "memory_mb": 1945}}});
            let on = json!({"live": true, "held": held, "auto_settings": {"limits": "auto"}});
            check(control("/__control/lane-settings", &on).is_ok(), "the Host turns lanes_live on");
            app.engine.borrow_mut().reconnect();
            true
        })),
        ("live lanes shown", Box::new(move |app, window, elapsed| {
            let shown = row(window, "Live processes").is_some();
            if !shown && elapsed < Duration::from_secs(8) {
                return false;
            }
            // No pressure percentages anywhere (the owner, 25 Nov 2026).
            check(["kept-pressure-cpu", "kept-pressure-io", "kept-pressure-memory"].iter().all(|id| by_id(window, id).is_none()), "no pressure thresholds shown");
            check(by_id(window, "kept-limits").is_some_and(|r| r.detail == "Auto") && by_id(window, "kept-reserve").is_some(), "auto, with the memory kept free");
            check(by_id(window, "kept-ceiling").is_none(), "no most kept in auto: it bounds nothing");
            check(row(window, "Now").is_none(), "the live processes say what Now said");
            let groups = info_from(window, "Live processes", 5);
            let expected = pairs(&[
                ("Live processes", "8 · 2 busy · 6 idle · 1945 MB"),
                ("    Claude", "8 · 2 busy · 6 idle"),
                ("Cold starts, last hour", "5"),
                ("    Claude", "3"),
                ("    Codex", "2"),
            ]);
            check(groups == expected, &format!("live processes and cold starts: {groups:?}"));
            // Nothing of the machine's pressure, whatever the Host sends (the owner, 25 Nov 2026).
            check(row(window, "Pressure").is_none(), "no pressure row");
            // A search for a group's title finds its lines too (#846).
            let found = |query: &str| -> Vec<(String, String)> {
                crate::settings_view::filter(crate::settings_view::rows(app), query).iter().filter(|r| r.kind == "info").map(|r| (r.label.to_string(), r.detail.to_string())).collect()
            };
            let cold = found("cold");
            check(cold.contains(&("    Claude".to_owned(), "3".to_owned())) && cold.contains(&("    Codex".to_owned(), "2".to_owned())), &format!("searching \"cold\" shows the cold starts per tool: {cold:?}"));
            shot(&out4, "kept-processes-04-pressure");
            true
        })),
    ];
    run_stages(stages);
}
