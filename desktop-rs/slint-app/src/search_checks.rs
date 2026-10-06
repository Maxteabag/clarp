//! `--check message-search`: Ctrl+F finds a message's words in another
//! chat (one loaded in this run), says it searched only this computer's
//! chats, and Enter opens that chat with the message marked and in view
//! (it is near the top, so the chat scrolls up to it). Ctrl+K lists
//! messages too: one in a chat only the transcript cache holds opens the
//! same way.
//!
//! `--check mention`: `@` in the composer lists the agents, the arrows move
//! through them, Tab completes the name, the route chip says where Enter
//! sends, the message goes to that agent's chat (`/send` to its session),
//! which opens with the sent row saying so; Escape closes the list and
//! leaves the composer typing.

use std::time::Duration;

use serde_json::json;
use slint::Model;

use super::{Stage, app_now, check, control, rows, run_stages, sends, shot, view};
use crate::headless;

/// Where the drawn element `id` is: (top, bottom).
fn drawn(id: &str) -> Option<(f32, f32)> {
    use i_slint_backend_testing::ElementQuery;
    let (window, id) = (crate::window()?, id.to_owned());
    ElementQuery::from_root(&window)
        .match_predicate(move |e| e.accessible_id().is_some_and(|i| i == id.as_str()) && e.size().height > 0.0)
        .find_first()
        .map(|e| (e.absolute_position().y, e.absolute_position().y + e.size().height))
}

/// Row `id` is drawn wholly inside the active chat.
fn in_view(id: &str) -> bool {
    let chat = drawn(&format!("chat:{}", app_now().active_id()));
    match (drawn(&format!("row:{id}")), chat) {
        (Some((top, bottom)), Some((chat_top, chat_bottom))) => top >= chat_top - 1.0 && bottom <= chat_bottom + 1.0,
        _ => false,
    }
}

fn first(window: &crate::AppWindow) -> (String, String) {
    window.get_switcher_rows().row_data(0).map(|r| (r.kind.to_string(), r.label.to_string())).unwrap_or_default()
}

fn cache_root() -> std::path::PathBuf {
    std::path::PathBuf::from(std::env::var_os("XDG_CACHE_HOME").expect("the check runs with a scratch cache")).join("clarp").join("transcripts")
}

pub fn message_search_check(out: String) {
    use slint::platform::Key;
    let (out1, out2, out3) = (out.clone(), out.clone(), out);
    let stages: Vec<Stage> = vec![
        ("live", Box::new(|app, _, _| {
            let open = app.engine.borrow().conversation("rachel").is_some_and(|c| !c.loading() && !c.rows().is_empty());
            if !open {
                return false;
            }
            // Mike's chat: the words to find near its top, under a screenful.
            let mut turns = Vec::new();
            for i in 0..30 {
                let role = if i % 2 == 0 { "user" } else { "assistant" };
                let text = if i == 2 { "The quarterly pelican report is attached, with the totals.".to_owned() } else { format!("Row {i}: a routine note about the build and its tests.") };
                turns.push(json!({"id": if i == 2 { "m-target".to_owned() } else { format!("m-{i}") }, "role": role, "text": text}));
            }
            check(control("/__control/turns", &json!({"session": "mike", "turns": turns})).is_ok(), "the Host holds Mike's long chat");
            // Dana: an agent this run never opened, whose chat the transcript
            // cache holds from an earlier one.
            let walrus = json!({"id": "d-walrus", "role": "assistant", "text": "Here is the walrus migration plan, step by step.", "timestamp": "2026-09-20T10:00:00Z"});
            check(control("/__control/add-agent", &json!({"session": "dana", "persona": "Dana"})).is_ok(), "the Host adds Dana");
            check(control("/__control/turns", &json!({"session": "dana", "turns": [walrus]})).is_ok(), "the Host holds Dana's chat");
            let snapshot = json!({"conversation_id": "c-dana", "latest_revision": 1, "has_more": false,
                "turns": [{"revision": 1, "id": "d-walrus", "role": "assistant", "text": "Here is the walrus migration plan, step by step.", "timestamp": "2026-09-20T10:00:00Z"}]});
            let saved = clarp_core::transcript_cache::TranscriptCache::new(cache_root()).save(app.engine.borrow().base_url(), "dana", snapshot.as_object().expect("an object"));
            check(saved.is_ok(), &format!("Dana's chat is in the transcript cache {saved:?}"));
            true
        })),
        ("mike loaded", Box::new(|app, window, _| {
            if app.engine.borrow().roster().find("dana").is_none() {
                return false;
            }
            window.invoke_chat_chosen("mike".into());
            true
        })),
        ("back to rachel", Box::new(|app, window, _| {
            let loaded = app.engine.borrow().conversation("mike").is_some_and(|c| c.index_of("m-target").is_some() && !c.loading());
            if !loaded {
                return false;
            }
            window.invoke_chat_chosen("rachel".into());
            true
        })),
        ("ctrl f", Box::new(|app, _, elapsed| {
            if app.engine.borrow().selected_session() != "rachel" || !super::report().composer_focused || elapsed < Duration::from_millis(300) {
                return false;
            }
            headless::press_with(&[Key::Control], "f");
            true
        })),
        ("search open", Box::new(|_, window, elapsed| {
            if !window.get_switcher_open() || elapsed < Duration::from_millis(200) {
                return false;
            }
            check(window.get_switcher_placeholder() == "Search messages in every chat", "Ctrl+F opens message search");
            let note = window.get_switcher_note().to_string();
            check(note.contains("on this computer") && note.contains("no message search"), &format!("it says it searches only this computer's chats: {note}"));
            headless::type_text("pelican report");
            true
        })),
        ("found", Box::new(move |_, window, elapsed| {
            if window.get_switcher_query() != "pelican report" || elapsed < Duration::from_millis(300) {
                return false;
            }
            let (kind, label) = first(window);
            check(kind == "message" && label.starts_with("Mike · you"), &format!("the message in Mike's chat is found: {kind} {label}"));
            let snippet = window.get_switcher_rows().row_data(0).map(|r| r.detail.to_string()).unwrap_or_default();
            check(snippet.contains("**pelican** **report**"), &format!("the snippet marks the match: {snippet}"));
            shot(&out1, "message-search-01-results");
            headless::press(Key::Return);
            true
        })),
        ("jumped", Box::new(move |app, window, _| {
            if window.get_switcher_open() || app.engine.borrow().selected_session() != "mike" || app_now().marked_row() != "m-target" || !in_view("m-target") {
                return false;
            }
            check(true, "Enter opens Mike's chat with the message marked and in view");
            check(!super::report().at_end, "the chat stayed on the message, not the latest");
            shot(&out2, "message-search-02-jumped");
            headless::press_with(&[Key::Control], "k");
            true
        })),
        ("ctrl k", Box::new(|_, window, elapsed| {
            if !window.get_switcher_open() || elapsed < Duration::from_millis(200) {
                return false;
            }
            headless::type_text("walrus");
            true
        })),
        ("mixed", Box::new(move |_, window, elapsed| {
            if window.get_switcher_query() != "walrus" || elapsed < Duration::from_millis(300) || first(window).0 != "message" {
                return false;
            }
            let (_, label) = first(window);
            check(label.starts_with("Dana · agent") && label.ends_with("· cached"), &format!("Ctrl+K finds a message only the cache holds, and says so: {label}"));
            check(window.get_switcher_note().contains("Ctrl+F"), "and points to message search");
            shot(&out3, "message-search-03-ctrl-k");
            headless::press(Key::Return);
            true
        })),
        ("cached jump", Box::new(|app, window, _| {
            if window.get_switcher_open() || app.engine.borrow().selected_session() != "dana" || app_now().marked_row() != "d-walrus" || !in_view("d-walrus") {
                return false;
            }
            check(true, "the cached chat opens with its message marked");
            true
        })),
    ];
    run_stages(stages);
}

pub fn mention_check(out: String) {
    use slint::platform::Key;
    let (out1, out2, out3) = (out.clone(), out.clone(), out);
    let mentions = || view().mentions.iter().map(|m| m.name.to_string()).collect::<Vec<_>>();
    let stages: Vec<Stage> = vec![
        ("ready", Box::new(|app, _, elapsed| {
            let open = app.engine.borrow().conversation("rachel").is_some_and(|c| !c.loading() && !c.rows().is_empty());
            if !open || !super::report().composer_focused || elapsed < Duration::from_millis(300) {
                return false;
            }
            headless::type_text("@mi");
            true
        })),
        ("listed", Box::new(move |_, _, elapsed| {
            if mentions().is_empty() || elapsed < Duration::from_millis(200) {
                return false;
            }
            check(mentions().first().is_some_and(|m| m == "Mike"), &format!("@mi lists Mike first: {:?}", mentions()));
            shot(&out1, "mention-01-list");
            headless::press(Key::Tab);
            true
        })),
        ("completed", Box::new(|_, _, _| {
            if app_now().active_draft() != "@Mike " {
                return false;
            }
            check(view().mentions.row_count() == 0, "Tab completes the name and closes the list");
            check(view().route == "Mike", &format!("the route chip names Mike: {:?}", view().route));
            check(super::report().composer_focused, "the composer keeps the keyboard");
            headless::type_text("status please");
            true
        })),
        ("routed", Box::new(move |_, _, elapsed| {
            if app_now().active_draft() != "@Mike status please" || elapsed < Duration::from_millis(200) {
                return false;
            }
            shot(&out2, "mention-02-route");
            headless::press(Key::Return);
            true
        })),
        ("sent", Box::new(move |app, window, _| {
            let sent = sends().into_iter().find(|s| s["body"]["text"] == "@Mike status please");
            let Some(sent) = sent else { return false };
            let marked = rows(window).iter().any(|r| r.author == "user" && r.meta == "→ Mike");
            if app.engine.borrow().selected_session() != "mike" || !marked {
                return false;
            }
            check(sent["body"]["session"] == "mike", &format!("the message went to Mike's session: {}", sent["body"]));
            check(true, "Mike's chat opens with the sent row saying where it went");
            check(app.engine.borrow().draft("rachel").is_empty(), "Rachel's draft is cleared");
            shot(&out3, "mention-03-sent");
            headless::type_text("@");
            true
        })),
        ("all agents", Box::new(move |_, _, elapsed| {
            if mentions().len() < 2 || elapsed < Duration::from_millis(200) {
                return false;
            }
            check(view().mention_current == 0, "the first agent is chosen");
            headless::press(Key::DownArrow);
            true
        })),
        ("down", Box::new(|_, _, _| {
            if view().mention_current != 1 {
                return false;
            }
            headless::press(Key::UpArrow);
            true
        })),
        ("up", Box::new(|_, _, _| {
            if view().mention_current != 0 {
                return false;
            }
            check(true, "Down and Up move through the agents");
            headless::press(Key::Escape);
            true
        })),
        ("escaped", Box::new(|_, _, elapsed| {
            if view().mentions.row_count() != 0 || elapsed < Duration::from_millis(200) {
                return false;
            }
            check(super::report().composer_focused && app_now().active_draft() == "@", "Escape closes the list and the composer keeps typing");
            true
        })),
    ];
    run_stages(stages);
}
