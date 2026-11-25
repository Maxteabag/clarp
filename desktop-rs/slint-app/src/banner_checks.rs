//! `--check banner`: the error banner over the chat names its dismiss key,
//! and that key (and the switcher's command) clears it, even when the
//! failure also left an error on the conversation. The banner floats over
//! the chat: the chat's header stays where it was. A "not spoken" failure
//! says nothing on a desktop where speech never worked, says it once per
//! chat (not after every reply), again once a clip has played, and leaves
//! with the chat. Needs the fake Host.

use std::cell::Cell;
use std::rc::Rc;
use std::time::Duration;

use serde_json::json;
use slint::platform::Key;

use super::{Rect, Stage, check, control, labelled, run_stages, shot};
use crate::headless;

fn fail_next_log() -> bool {
    control("/__control/fail", &json!({"path": "/log", "status": 504, "count": 1})).is_ok()
}

pub fn banner_check(out: String) {
    let (out2, out3) = (out.clone(), out.clone());
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
    let mut stages = stages;
    stages.extend(voice_stages(out3));
    run_stages(stages);
}

const NOT_SPOKEN: &str = "Not spoken: Cartesia Sonic is not set up on this Host.";

fn tts_error() -> bool {
    control("/__control/event", &json!({"type": "tts-error", "session": "rachel", "message": NOT_SPOKEN})).is_ok()
}

/// The chat's header: where the banner must not move it to.
fn header() -> Option<Rect> {
    labelled("Chat menu")
}

/// Sends `send` once, then after `settle` passes `then` the window.
fn after(name: &'static str, send: fn() -> bool, settle: u64, then: impl Fn(&crate::AppWindow) + 'static) -> Stage {
    let mut sent = false;
    (name, Box::new(move |_, window, elapsed| {
        if !std::mem::replace(&mut sent, true) {
            check(send(), &format!("{name}: the Host's event is sent"));
            return false;
        }
        if elapsed < Duration::from_millis(settle) {
            return false;
        }
        then(window);
        true
    }))
}

fn voice_stages(out: String) -> Vec<Stage> {
    let before: Rc<Cell<Option<Rect>>> = Rc::new(Cell::new(None));
    let (b1, b2) = (before.clone(), before.clone());
    vec![
        after("speech never set up", tts_error, 900, move |window| {
            check(window.get_error().is_empty(), &format!("speech never worked on this desktop: no banner: {:?}", window.get_error()));
            b1.set(header());
            check(b1.get().is_some(), "the chat's header is drawn");
            let app = super::app_now();
            app.engine.borrow_mut().settings_mut().set(clarp_engine::SPEECH_HEARD, true);
        }),
        after("speech set up, a reply not spoken", tts_error, 0, |_| {}),
        ("banner shown", Box::new(move |_, window, elapsed| {
            if window.get_error() != NOT_SPOKEN || elapsed < Duration::from_millis(300) {
                return false;
            }
            check(labelled("Error banner").is_some(), "the banner is drawn");
            check(header() == b2.get(), &format!("the banner floats: the chat's header stays put: {:?} then {:?}", b2.get(), header()));
            shot(&out, "03-not-spoken-overlay");
            headless::press(Key::Escape);
            true
        })),
        ("dismissed voice", Box::new(|_, window, elapsed| {
            if elapsed < Duration::from_millis(300) {
                return false;
            }
            check(window.get_error().is_empty(), &format!("Escape dismisses it: {:?}", window.get_error()));
            true
        })),
        after("the next reply not spoken", tts_error, 900, |window| {
            check(window.get_error().is_empty(), &format!("once per chat: the next reply says nothing: {:?}", window.get_error()));
        }),
        after("a clip plays", || control("/__control/event", &json!({"type": "audio", "clip_id": 51, "session": "rachel",
            "url": "/clips/51/complete.mp3", "complete_url": "/clips/51/complete.mp3"})).is_ok(), 300, |_| {}),
        after("speech fails again", tts_error, 0, |_| {}),
        ("shown again", Box::new(|app, window, elapsed| {
            if window.get_error() != NOT_SPOKEN {
                check(elapsed < Duration::from_secs(5), "after a clip played, a new failure shows once more");
                return elapsed >= Duration::from_secs(5);
            }
            app.engine.borrow_mut().select("mike");
            crate::pump();
            true
        })),
        ("chat changed", Box::new(|app, window, elapsed| {
            if elapsed < Duration::from_millis(500) {
                return false;
            }
            app.engine.borrow_mut().select("rachel");
            crate::pump();
            check(window.get_error().is_empty(), &format!("the banner leaves with its chat: {:?}", window.get_error()));
            true
        })),
    ]
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
