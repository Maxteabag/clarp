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
