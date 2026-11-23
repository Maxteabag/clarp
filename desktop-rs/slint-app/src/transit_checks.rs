//! `--check backend-transit` (#720, Host feature `agent_backend_transit`):
//! a Host without the feature shows no Backend row. With it, the profile's
//! Backend row reads "Claude · Opus"; from the keyboard Return opens the
//! picker, Down skips a backend dimmed with its reason, Return picks Codex
//! and its default model, Escape steps back, Return confirms; the row shows
//! "Moving to Codex… translating" over a bar while the Host holds the move,
//! then the result, and the agent's new backend. A busy agent cannot open
//! it, a refused start says why, and a click outside closes the picker.

use std::time::Duration;

use serde_json::json;
use slint::platform::Key;
use slint::{ComponentHandle, Model};

use super::{Stage, app_now, check, control, headless, posts, report, run_stages, shot};
use crate::ProfileBridge;

fn offer() -> serde_json::Value {
    json!({"providers": {
        "claude": {"label": "Claude", "sort_index": 1, "available": true, "models": [
            {"id": "opus", "label": "Opus", "default": true}, {"id": "sonnet", "label": "Sonnet"}]},
        "codex": {"label": "Codex", "sort_index": 2, "available": true, "models": [
            {"id": "gpt-5", "label": "GPT-5"}, {"id": "gpt-6", "label": "GPT-6", "default": true}]},
        "grok": {"label": "Grok", "sort_index": 3, "available": false, "unavailable_reason": "signed_out", "models": []},
    }})
}

fn ready(app: &crate::App) -> bool {
    app.engine.borrow().conversation("rachel").is_some_and(|c| !c.rows().is_empty()) && report().composer_focused
}

fn labels(bridge: &ProfileBridge) -> Vec<String> {
    bridge.get_transit_choices().iter().map(|c| if c.detail.is_empty() { c.label.to_string() } else { format!("{} ({})", c.label, c.detail) }).collect()
}

/// Gives the Backend row the keyboard, as closing the picker does.
fn focus_row(bridge: &ProfileBridge) {
    bridge.set_transit_row_request(bridge.get_transit_row_request() + 1);
}

pub(super) fn backend_transit_check(out: String) {
    let (out1, out2, out3, out4, out5) = (out.clone(), out.clone(), out.clone(), out.clone(), out.clone());
    let stages: Vec<Stage> = vec![
        ("ready", Box::new(|app, window, _| {
            if !ready(app) {
                return false;
            }
            let catalog = offer();
            let rachel = json!({"session": "rachel", "set": {"model": "opus"}});
            check(control("/__control/catalog", &catalog).is_ok() && control("/__control/agent", &rachel).is_ok(), "the Host has a catalog and Rachel on Opus");
            window.global::<ProfileBridge>().invoke_open_for_pane(app_now().active_id());
            true
        })),
        ("an older Host", Box::new(|_, window, elapsed| {
            if window.get_overlay() != "profile" || elapsed < Duration::from_millis(300) {
                return false;
            }
            check(!window.global::<ProfileBridge>().get_transit_shown(), "a Host without agent_backend_transit shows no Backend row");
            headless::press(Key::Escape);
            check(control("/__control/transit", &json!({"on": true, "options": offer(), "hold": true})).is_ok(), "the Host offers backend changes");
            true
        })),
        ("feature read", Box::new(|app, _window, _| {
            if !report().composer_focused {
                return false;
            }
            // The features are read from /server-info on connecting.
            app.engine.borrow_mut().reconnect();
            true
        })),
        ("reopened", Box::new(|app, window, _| {
            let engine = app.engine.borrow();
            let live = engine.connection_state() == "live" && engine.backend_transit_offered("rachel") && engine.model_catalog_loaded();
            let on_opus = engine.roster().find("rachel").is_some_and(|a| a.model == "opus");
            drop(engine);
            if !live || !on_opus {
                return false;
            }
            window.global::<ProfileBridge>().invoke_open_for_pane(app_now().active_id());
            true
        })),
        ("the row", Box::new(move |_, window, elapsed| {
            let bridge = window.global::<ProfileBridge>();
            if window.get_overlay() != "profile" || !bridge.get_transit_shown() || elapsed < Duration::from_millis(300) {
                return false;
            }
            check(bridge.get_transit_value() == "Claude · Opus", &format!("the Backend row reads {}", bridge.get_transit_value()));
            check(bridge.get_transit_enabled() && bridge.get_transit_detail().is_empty(), "and can be opened");
            shot(&out1, "transit-01-row");
            focus_row(&bridge);
            true
        })),
        ("row focused", Box::new(|_, _window, elapsed| {
            if elapsed < Duration::from_millis(100) {
                return false;
            }
            headless::press(Key::Return);
            true
        })),
        ("backends", Box::new(move |_, window, elapsed| {
            let bridge = window.global::<ProfileBridge>();
            if bridge.get_transit_step() != "backend" || bridge.get_transit_loading() || bridge.get_transit_choices().row_count() != 3 || elapsed < Duration::from_millis(200) {
                return false;
            }
            check(true, "Return on the row opens the picker");
            let shown = labels(&bridge);
            check(shown == ["Claude (Current)", "Codex", "Grok (Not signed in)"], &format!("each backend, the dimmed one with its reason: {shown:?}"));
            check(!bridge.get_transit_choices().row_data(2).is_some_and(|c| c.enabled), "Grok cannot be chosen");
            check(bridge.get_transit_index() == 0, &format!("the first choice is highlighted: {}", bridge.get_transit_index()));
            check(posts("/agent-transit").is_empty(), "nothing starts by opening it");
            shot(&out2, "transit-02-backends");
            // Down twice skips Grok and wraps to Claude; once more is Codex.
            headless::press(Key::DownArrow);
            headless::press(Key::DownArrow);
            headless::press(Key::DownArrow);
            true
        })),
        ("down skips the dimmed", Box::new(|_, window, _| {
            let bridge = window.global::<ProfileBridge>();
            if bridge.get_transit_index() != 1 {
                return false;
            }
            check(true, "Down walks the choices and skips the dimmed one");
            headless::press(Key::Return);
            true
        })),
        ("models", Box::new(|_, window, _| {
            let bridge = window.global::<ProfileBridge>();
            if bridge.get_transit_step() != "model" {
                return false;
            }
            check(bridge.get_transit_title() == "Model · Codex", &format!("Codex's models: {}", bridge.get_transit_title()));
            check(labels(&bridge) == ["GPT-5", "GPT-6 (Default)"] && bridge.get_transit_index() == 1, &format!("its default highlighted: {:?} {}", labels(&bridge), bridge.get_transit_index()));
            headless::press(Key::Escape);
            true
        })),
        ("escape steps back", Box::new(|_, window, _| {
            let bridge = window.global::<ProfileBridge>();
            if bridge.get_transit_step() != "backend" {
                return false;
            }
            check(window.get_overlay() == "profile", "Escape steps back to the backends, the profile stays");
            headless::press(Key::DownArrow);
            headless::press(Key::Return);
            true
        })),
        ("models again", Box::new(|_, window, _| {
            let bridge = window.global::<ProfileBridge>();
            if bridge.get_transit_step() != "model" {
                return false;
            }
            headless::press(Key::Return);
            true
        })),
        ("confirm", Box::new(move |_, window, elapsed| {
            let bridge = window.global::<ProfileBridge>();
            if bridge.get_transit_step() != "confirm" || elapsed < Duration::from_millis(200) {
                return false;
            }
            check(bridge.get_transit_title() == "Move Rachel to Codex · GPT-6?", &format!("the confirmation names the move: {}", bridge.get_transit_title()));
            check(bridge.get_transit_body().contains("takes the conversation's messages along"), "and what is carried");
            shot(&out3, "transit-03-confirm");
            headless::press(Key::Return);
            true
        })),
        ("moving", Box::new(move |_, window, elapsed| {
            let bridge = window.global::<ProfileBridge>();
            if posts("/agent-transit").is_empty() || bridge.get_transit_detail() != "Moving to Codex… translating" || elapsed < Duration::from_millis(200) {
                return false;
            }
            let body = posts("/agent-transit").last().map(|p| p["body"].clone()).unwrap_or_default();
            check(body["session"] == "rachel" && body["backend"] == "codex" && body["model"] == "gpt-6" && body["request_id"].as_str().is_some_and(|r| !r.is_empty()),
                &format!("Return starts the move: {body}"));
            check(bridge.get_transit_step().is_empty(), "the picker closes");
            check(bridge.get_transit_moving() && !bridge.get_transit_enabled(), "the row shows the phase over a bar and cannot be opened");
            shot(&out4, "transit-04-moving");
            check(control("/__control/transit", &json!({"release": true, "hold": false})).is_ok(), "the Host finishes the move");
            true
        })),
        ("done", Box::new(|app, window, _| {
            let bridge = window.global::<ProfileBridge>();
            let moved = app.engine.borrow().roster().find("rachel").is_some_and(|a| a.backend == "codex");
            if !moved || bridge.get_transit_moving() || bridge.get_transit_value() != "Codex · GPT-6" {
                return false;
            }
            let detail = bridge.get_transit_detail().to_string();
            check(detail == "Now on Codex · GPT-6. The conversation came along.", &format!("the result: {detail}"));
            check(bridge.get_transit_enabled(), "the row opens again");
            let busy = json!({"session": "rachel", "set": {"latest_state": "thinking"}});
            check(control("/__control/agent", &busy).is_ok(), "Rachel starts a turn");
            true
        })),
        ("busy", Box::new(|_, window, _| {
            let bridge = window.global::<ProfileBridge>();
            if bridge.get_transit_enabled() {
                return false;
            }
            check(bridge.get_transit_detail() == "Finish or stop the current turn first", &format!("a running turn says why: {}", bridge.get_transit_detail()));
            bridge.invoke_transit_open();
            check(bridge.get_transit_step().is_empty(), "and the picker does not open");
            let idle = json!({"session": "rachel", "set": {"latest_state": "idle"}});
            let refuse = json!({"refuse": [409, {"error": "messages_waiting"}]});
            check(control("/__control/agent", &idle).is_ok() && control("/__control/transit", &refuse).is_ok(), "the turn ends; the Host will refuse the next start");
            true
        })),
        ("idle", Box::new(|_, window, _| {
            let bridge = window.global::<ProfileBridge>();
            if !bridge.get_transit_enabled() {
                return false;
            }
            bridge.invoke_transit_open();
            true
        })),
        ("picker for the refusal", Box::new(|_, window, _| {
            let bridge = window.global::<ProfileBridge>();
            if bridge.get_transit_step() != "backend" || bridge.get_transit_choices().row_count() != 3 || bridge.get_transit_loading() {
                return false;
            }
            check(labels(&bridge)[1] == "Codex (Current)", &format!("Codex is current now: {:?}", labels(&bridge)));
            // Claude, its default, Move.
            bridge.invoke_transit_choose(0);
            bridge.invoke_transit_choose(bridge.get_transit_index());
            bridge.invoke_transit_confirm();
            true
        })),
        ("refused", Box::new(move |_, window, _| {
            let bridge = window.global::<ProfileBridge>();
            if !bridge.get_transit_failed() {
                return false;
            }
            let detail = bridge.get_transit_detail().to_string();
            check(detail == "Couldn't move to Claude: Messages are waiting for this agent. Try again once they have gone.", &format!("a refused start says why: {detail}"));
            check(bridge.get_transit_value() == "Codex · GPT-6", "and the agent stays");
            shot(&out5, "transit-05-refused");
            bridge.invoke_transit_open();
            true
        })),
        ("open to click outside", Box::new(|_, window, elapsed| {
            let bridge = window.global::<ProfileBridge>();
            if bridge.get_transit_step() != "backend" || elapsed < Duration::from_millis(200) {
                return false;
            }
            headless::click(6.0, 6.0);
            true
        })),
        ("clicked outside", Box::new(|_, window, _| {
            let bridge = window.global::<ProfileBridge>();
            if !bridge.get_transit_step().is_empty() {
                return false;
            }
            check(window.get_overlay() == "profile", "a click outside closes the picker, the profile stays");
            check(posts("/agent-transit").len() == 2, "and starts nothing");
            true
        })),
    ];
    run_stages(stages);
}
