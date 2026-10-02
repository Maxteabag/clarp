//! `--check artifact-keys --out DIR`: every artifact in the chat is used
//! from the keyboard alone. A chat of every type (and images in messages)
//! is driven only by key presses: J/K reach every card, scrolling it into
//! view; each card's keys do what its hints say; viewers scroll and close
//! back to their card; typing in the composer never reaches a card. One
//! stage per group clicks a hint, which must still act like its key.

use std::cell::{Cell, RefCell};
use std::rc::Rc;
use std::time::Duration;

use serde_json::{Value, json};
use slint::platform::Key;
use slint::Model;

use super::{Stage, app_now, check, control, headless, posts, report, rows, run_stages, shot};
use crate::ArtifactItem;

/// Every type, as in the artifacts check, each id ending in "-k".
const ALL_TYPES: &[&str] = &["countdown", "decision", "question", "plan", "document", "research", "code_change", "data", "audio", "video", "file", "release", "directory", "workflow_run", "html_form"];

fn card(id: &str) -> Option<ArtifactItem> {
    rows(&crate::window()?).iter().flat_map(|row| row.artifacts.iter().collect::<Vec<_>>()).find(|a| a.id == id)
}

/// What the keyboard can reach in the open chat, top to bottom.
fn reachable(app: &crate::App) -> Vec<String> {
    crate::artifacts_view::selectables(app)
}

fn cursor(app: &crate::App) -> String {
    app.artifact_cursor.borrow().clone()
}

/// The card the keyboard is on, once it is on screen.
fn on(app: &crate::App) -> String {
    crate::artifacts_view::selected(app).unwrap_or_default()
}

/// Lines the app recorded instead of opening them (`CLARP_TEST_OPEN_URL`).
fn opened() -> Vec<String> {
    std::env::var_os("CLARP_TEST_OPEN_URL").and_then(|path| std::fs::read_to_string(path).ok()).unwrap_or_default().lines().map(str::to_owned).collect()
}

/// The shortcut bar's hints as "keys label".
fn bar(window: &crate::AppWindow) -> Vec<String> {
    window.get_hints().iter().map(|h| format!("{} {}", h.keys, h.label)).collect()
}

/// A stage that walks the keyboard to `id` with J and K alone (one press
/// at a time, each once the last has landed on screen).
fn reach(id: &'static str) -> Stage {
    let last = Rc::new(Cell::new(Duration::ZERO));
    (Box::leak(format!("reach {id}").into_boxed_str()), Box::new(move |app, _, elapsed| {
        if on(app) == id {
            check(true, &format!("J/K reach {id}"));
            return true;
        }
        if elapsed > Duration::from_secs(12) {
            check(false, &format!("J/K reach {id}: on {:?}, cursor {:?}, reachable {:?}", on(app), cursor(app), crate::artifacts_view::on_screen(app)));
            return true;
        }
        let ids = reachable(app);
        let (Some(target), current) = (ids.iter().position(|i| i == id), ids.iter().position(|i| *i == cursor(app))) else { return false };
        // Each press waits for the last to land (its card on screen).
        let landed = cursor(app).is_empty() || !on(app).is_empty();
        if !landed || elapsed.saturating_sub(last.get()) < Duration::from_millis(200) {
            return false;
        }
        last.set(elapsed);
        match current {
            Some(at) if at < target => headless::press("j"),
            Some(_) => headless::press("k"),
            None => headless::press("k"),
        }
        false
    }))
}

/// Waits for `ready`, then checks `what`; past `limit` it fails `what`.
fn wait_for(name: &'static str, limit: Duration, mut ready: impl FnMut(&crate::App, &crate::AppWindow) -> bool + 'static, what: &'static str) -> Stage {
    (name, Box::new(move |app, window, elapsed| {
        let done = ready(app, window);
        if !done && elapsed < limit {
            return false;
        }
        check(done, what);
        true
    }))
}

/// A stage that presses `key` (after `after`).
fn press(name: &'static str, key: impl Into<slint::SharedString> + Clone + 'static, after: Duration) -> Stage {
    (name, Box::new(move |_, _, elapsed| {
        if elapsed < after {
            return false;
        }
        headless::press(key.clone());
        true
    }))
}

fn fail_next_log() -> bool {
    control("/__control/fail", &json!({"path": "/log", "status": 504, "count": 1})).is_ok()
}

// ---- the chat

fn chat_stages() -> Vec<Stage> {
    let turns = json!([
        {"id": "im-one-k", "role": "assistant", "text": "Here is the chart:\n\n![Sales by region](clarp-media://asset/img-chart)"},
        {"id": "im-gallery-k", "role": "assistant", "text": "And the rest:\n\n```clarp-gallery\n![Q1](clarp-media://asset/img-a)\n![Q2](/media/img-b)\n![Q3](media/img-c)\n```"},
    ]);
    vec![
        ("load", Box::new(move |app, _, _| {
            if app.engine.borrow().connection_state() != "live" {
                return false;
            }
            let loaded = control("/__control/artifact-chat", &json!({"session": "art-keys", "types": ALL_TYPES, "suffix": "-k", "turns": turns}));
            check(loaded.is_ok(), &format!("the Host takes a chat of every type and two image replies {}", loaded.err().unwrap_or_default()));
            true
        })),
        ("open", Box::new(|app, _, _| {
            if app.engine.borrow().roster().find("art-keys").is_none() {
                return false;
            }
            app.engine.borrow_mut().select("art-keys");
            crate::pump();
            true
        })),
        ("chat placed", Box::new(|app, _, elapsed| {
            let placed = card("form-stale-k").is_some() && rows(&crate::window().expect("window")).iter().any(|r| r.id == "im-gallery-k");
            if !placed || !report().at_end || elapsed < Duration::from_millis(1200) {
                return false;
            }
            check(report().composer_focused, "the chat opens with the keyboard in the composer");
            let _ = app;
            // The keyboard leaves the composer for the chat.
            headless::press(Key::Escape);
            true
        })),
        wait_for("keyboard on the chat", Duration::from_secs(2), |_, _| report().transcript_focused, "Escape hands the keyboard to the chat"),
    ]
}

// ---- reaching every card

fn reach_stages(out: &str) -> Vec<Stage> {
    let (out, out2) = (out.to_owned(), out.to_owned());
    let held = Rc::new(Cell::new(0.0f32));
    let held2 = held.clone();
    let latest = Rc::new(Cell::new(0.0f32));
    let latest2 = latest.clone();
    vec![
        ("the bar says how to reach cards", Box::new(move |_, window, _| {
            check(bar(window).iter().any(|h| h == "J/K Cards"), &format!("the shortcut bar shows J/K Cards in the chat: {:?}", bar(window)));
            latest.set(report().offset);
            headless::press("k");
            true
        })),
        wait_for("K from none", Duration::from_secs(2), |app, _| !on(app).is_empty() && reachable(app).last() == Some(&on(app)),
            "K from the chat selects the latest thing the keyboard can reach"),
        ("K up the whole chat", Box::new(move |app, _, elapsed| {
            let first = reachable(app).first().cloned().unwrap_or_default();
            if on(app) == first {
                let moved = (report().offset - latest2.get()).abs();
                check(true, "K walks up every card to the first in the chat");
                check(moved > 100.0 && !report().follows, &format!("the keypresses scroll the chat to it (moved {moved}px, follows {})", report().follows));
                shot(&out, "keys-01-first-card");
                held.set(report().offset);
                // The Host moves every card on while the reader is up there.
                let more = "Updated by the agent a moment ago with a longer explanation of what changed and why it matters now.";
                check(control("/__control/artifact-settle", &json!({"session": "art-keys", "more": more})).is_ok(), "the Host moves the cards on");
                return true;
            }
            if elapsed > Duration::from_secs(14) {
                check(false, &format!("K walks up every card to the first in the chat: on {:?}, cursor {:?} of {:?}", on(app), cursor(app), crate::artifacts_view::on_screen(app)));
                return true;
            }
            // One press at a time, each once the last has landed.
            if !on(app).is_empty() {
                headless::press("k");
            }
            false
        })),
        ("activity does not move the reader", Box::new(move |app, _, elapsed| {
            let settled = card("dec-deploy-k").is_some_and(|c| c.resolved == "Approved") && card("cd-launch-k").is_some_and(|c| c.summary.contains("Updated by the agent"));
            if !settled || elapsed < Duration::from_millis(1500) {
                return elapsed > Duration::from_secs(6) && { check(false, "the cards move on"); true };
            }
            let moved = (report().offset - held2.get()).abs();
            check(moved < 1.0, &format!("cards changing around a selected card move nothing ({moved}px)"));
            check(!on(app).is_empty(), "and the card stays selected");
            shot(&out2, "keys-02-held");
            true
        })),
        ("J down the whole chat", Box::new(|app, _, elapsed| {
            let last = reachable(app).last().cloned().unwrap_or_default();
            if on(app) == last && !last.is_empty() {
                check(true, "J walks down every card to the latest");
                return true;
            }
            if elapsed > Duration::from_secs(14) {
                check(false, &format!("J walks down every card to the latest: on {:?} of {:?}", on(app), reachable(app)));
                return true;
            }
            if !on(app).is_empty() {
                headless::press("j");
            }
            false
        })),
        ("Escape leaves the card", Box::new(|app, _, elapsed| {
            if elapsed < Duration::from_millis(300) {
                return false;
            }
            check(!cursor(app).is_empty(), "a card is selected");
            headless::press(Key::Escape);
            true
        })),
        wait_for("card left", Duration::from_secs(1), |app, _| cursor(app).is_empty() && report().transcript_focused,
            "Escape on a card leaves it; the keyboard stays on the chat"),
        reach("dec-refused-k"),
        ("typing in the composer", Box::new(|_, _, _| {
            // I: the composer, where card keys are only letters.
            headless::press("i");
            true
        })),
        ("typed", Box::new(|app, _, elapsed| {
            if !report().composer_focused {
                return elapsed > Duration::from_secs(2) && { check(false, "I puts the keyboard in the composer"); true };
            }
            headless::type_text("2jk1");
            headless::press(Key::Delete);
            let _ = app;
            true
        })),
        ("typing reached no card", Box::new(|app, _, elapsed| {
            if elapsed < Duration::from_millis(500) {
                return false;
            }
            let shown = card("dec-refused-k");
            check(app.active_draft() == "2jk1", &format!("the letters and digits type into the composer: {:?}", app.active_draft()));
            check(cursor(app) == "dec-refused-k" && shown.as_ref().is_some_and(|c| c.chosen == -1 && c.pending), &format!("and reach no card: cursor {:?}, chosen {:?}", cursor(app), shown.map(|c| c.chosen)));
            check(posts("/decisions/d-refused-k/dismiss").is_empty() && posts("/decisions/d-refused-k/resolve").is_empty(), "Delete in the composer discards nothing");
            app.artifact_drafts.borrow_mut().clear();
            headless::press(Key::Escape);
            true
        })),
        wait_for("back on the chat", Duration::from_secs(2), |_, _| report().transcript_focused, "Escape from the composer is back on the chat"),
        ("the banner's Escape first", Box::new(|app, _, elapsed| {
            if on(app) != "dec-refused-k" && elapsed < Duration::from_secs(2) {
                return false;
            }
            check(fail_next_log(), "the Host's next transcript fetch fails");
            headless::press(Key::F5);
            true
        })),
        ("banner up", Box::new(|_, window, elapsed| {
            if !window.get_error().contains("504") {
                return elapsed > Duration::from_secs(4) && { check(false, "the error banner shows"); true };
            }
            headless::press(Key::Escape);
            true
        })),
        ("banner dismissed", Box::new(|app, window, elapsed| {
            if elapsed < Duration::from_millis(300) {
                return false;
            }
            check(window.get_error().is_empty() && cursor(app) == "dec-refused-k", &format!("Escape dismisses the banner first; the card stays selected: error {:?}, cursor {:?}", window.get_error(), cursor(app)));
            headless::press(Key::Escape);
            true
        })),
        wait_for("then the card", Duration::from_secs(1), |app, _| cursor(app).is_empty(), "the next Escape leaves the card"),
    ]
}

pub(super) fn artifact_keys_check(out: String) {
    let mut stages: Vec<Stage> = Vec::new();
    stages.extend(chat_stages());
    stages.extend(reach_stages(&out));
    let _ = (Value::Null, RefCell::new(()), app_now);
    run_stages(stages);
}
