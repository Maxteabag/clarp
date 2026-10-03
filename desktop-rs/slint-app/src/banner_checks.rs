//! `--check banner`: the error banner over the chat names its dismiss key,
//! and that key (and the switcher's command) clears it, even when the
//! failure also left an error on the conversation. Needs the fake Host.

use std::time::Duration;

use serde_json::json;
use slint::platform::Key;

use super::{Stage, check, control, run_stages, shot};
use crate::headless;

fn fail_next_log() -> bool {
    control("/__control/fail", &json!({"path": "/log", "status": 504, "count": 1})).is_ok()
}

pub fn banner_check(out: String) {
    let out2 = out.clone();
    let stages: Vec<Stage> = vec![
        ("ready", Box::new(|app, _window, _| {
            let open = app.engine.borrow().conversation("rachel").is_some_and(|c| !c.rows().is_empty());
            if !open {
                return false;
            }
            check(fail_next_log(), "the Host's next transcript fetch times out at the gateway");
            headless::press(Key::F5);
            true
        })),
        ("failed", Box::new(move |_, window, elapsed| {
            if !window.get_error().contains("504") || elapsed < Duration::from_millis(300) {
                return false;
            }
            check(window.get_error_dismiss_key() == "Esc", &format!("the banner shows its dismiss key: {:?}", window.get_error_dismiss_key()));
            shot(&out, "01-banner");
            headless::press(Key::Escape);
            true
        })),
        ("dismissed", Box::new(|_, window, elapsed| {
            if elapsed < Duration::from_millis(300) {
                return false;
            }
            check(window.get_error().is_empty(), &format!("Escape dismisses the banner: {:?}", window.get_error()));
            check(fail_next_log(), "the transcript fetch times out again");
            headless::press(Key::F5);
            true
        })),
        ("failed-again", Box::new(|_, window, elapsed| {
            if !window.get_error().contains("504") || elapsed < Duration::from_millis(300) {
                return false;
            }
            check(crate::commands::run(&super::app_now(), window, "dismiss-error"), "the switcher's Dismiss command runs");
            true
        })),
        ("dismissed-again", Box::new(move |_, window, elapsed| {
            if elapsed < Duration::from_millis(300) {
                return false;
            }
            check(window.get_error().is_empty(), &format!("the Dismiss command clears the banner: {:?}", window.get_error()));
            shot(&out2, "02-dismissed");
            true
        })),
    ];
    run_stages(stages);
}

/// What another window would have saved: a layout showing `session`.
fn external_layout(session: &str) -> String {
    let collection = json!({"version": 1, "active": "workspace-1", "names": {"workspace-1": "Other window"},
        "states": {"workspace-1": {"activePaneId": "pane-1",
            "root": {"id": "pane-1", "kind": "leaf", "session": session}, "zoomedPaneId": ""}}}).to_string();
    json!({"collectionV1": collection}).to_string()
}

fn saved_session() -> String {
    let Some(path) = clarp_engine::workspace::default_store_path() else { return String::new() };
    let document: serde_json::Value = std::fs::read_to_string(path).ok().and_then(|t| serde_json::from_str(&t).ok()).unwrap_or_default();
    let collection: serde_json::Value = document["collectionV1"].as_str().and_then(|t| serde_json::from_str(t).ok()).unwrap_or_default();
    let active = collection["active"].as_str().unwrap_or_default().to_owned();
    collection["states"][active]["root"]["session"].as_str().unwrap_or_default().to_owned()
}

fn another_window_saves(session: &str) -> bool {
    let Some(path) = clarp_engine::workspace::default_store_path() else { return false };
    std::fs::write(path, external_layout(session)).is_ok()
}

/// `--check layout-warning`: when another window saved a newer layout, the
/// bar names its keys; Escape dismisses it (the newer layout stays, and this
/// window saves normally after), and Ctrl+Shift+S keeps this window's.
pub fn layout_warning_check(out: String) {
    let out2 = out.clone();
    let stages: Vec<Stage> = vec![
        ("ready", Box::new(|app, _window, _| {
            let open = app.engine.borrow().conversation("rachel").is_some_and(|c| !c.rows().is_empty());
            if !open || saved_session() != "rachel" {
                return false;
            }
            check(another_window_saves("external"), "another window saves its layout");
            app.engine.borrow_mut().select("mike");
            crate::pump();
            true
        })),
        ("conflict", Box::new(move |_, window, elapsed| {
            if window.get_save_warning().is_empty() || elapsed < Duration::from_millis(300) {
                return false;
            }
            check(window.get_save_warning_dismiss_key() == "Esc", &format!("the bar names its dismiss key: {:?}", window.get_save_warning_dismiss_key()));
            check(window.get_save_warning_keep_key() == "Ctrl+Shift+S", &format!("the bar names its keep key: {:?}", window.get_save_warning_keep_key()));
            shot(&out, "layout-01-conflict");
            headless::press(Key::Escape);
            true
        })),
        ("dismissed", Box::new(|app, window, elapsed| {
            if elapsed < Duration::from_millis(500) {
                return false;
            }
            check(window.get_save_warning().is_empty(), &format!("Escape dismisses the layout warning: {:?}", window.get_save_warning()));
            check(saved_session() == "external", &format!("dismissing keeps the newer layout: {:?}", saved_session()));
            app.engine.borrow_mut().select("rachel");
            crate::pump();
            true
        })),
        ("saves-after", Box::new(|_, window, elapsed| {
            if elapsed < Duration::from_millis(800) {
                return false;
            }
            check(window.get_save_warning().is_empty(), &format!("after dismissing, this window saves without a new warning: {:?}", window.get_save_warning()));
            check(saved_session() == "rachel", &format!("this window's next change is saved: {:?}", saved_session()));
            check(another_window_saves("external-2"), "another window saves again");
            crate::app().expect("the app runs").engine.borrow_mut().select("mike");
            crate::pump();
            true
        })),
        ("conflict-again", Box::new(|_, window, elapsed| {
            if window.get_save_warning().is_empty() || elapsed < Duration::from_millis(300) {
                return false;
            }
            headless::press_with(&[Key::Control, Key::Shift], "s");
            true
        })),
        ("kept", Box::new(move |_, window, elapsed| {
            if elapsed < Duration::from_millis(800) {
                return false;
            }
            check(window.get_save_warning().is_empty(), &format!("Ctrl+Shift+S clears the warning: {:?}", window.get_save_warning()));
            check(saved_session() == "mike", &format!("Ctrl+Shift+S saves this window's layout: {:?}", saved_session()));
            shot(&out2, "layout-02-kept");
            true
        })),
    ];
    run_stages(stages);
}
