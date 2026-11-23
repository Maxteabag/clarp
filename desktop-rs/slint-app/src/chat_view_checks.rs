//! `--check chat-view`: Conversation and Session views of a chat (#702,
//! feature `chat_view`). A Host without the feature shows nothing new. With
//! it, the chat's ⋯ menu offers Chat view; Conversation leaves the session
//! rows out (commentary, tools, the work row) and keeps the user's messages
//! and the replies; the reply's Show work opens its turn's work in place and
//! Hide work folds it again; C switches back to Session. The choice goes to
//! the chat's Host (`POST /agent-chat-view`); a refused write stays on this
//! device; another device's choice replaces this one's. Clean chat puts
//! every chat without its own choice in Conversation view, where a running
//! turn is one Running box. Driven by the fake Host.

use std::time::Duration;

use serde_json::json;
use slint::{ComponentHandle, Model};

use super::{Stage, app_now, check, control, posts, requests, rows, run_stages, shot};

const FOLD: &str = "live:fold:tr-1";
const AFTER_TAIL: usize = 17; // lseq 16: the command runs
const AFTER_INTERRUPT: usize = 24; // lseq 23: the turn is over

/// Mike's finished turn, tagged as the Host tags it: his prompt, commentary
/// and a tool (session), the reply (the turn's anchor: 2 rows left out),
/// then his next prompt.
fn tagged_rows() -> serde_json::Value {
    json!([
        {"id": "c-ask", "role": "user", "text": "Is the release ready?", "origin": "user", "timestamp": "2026-12-02T10:00:00Z", "trace_id": "t-c", "view": "conversation"},
        {"id": "c-note", "role": "assistant", "text": "Checking the release notes first.", "phase": "commentary", "timestamp": "2026-12-02T10:00:01Z", "trace_id": "t-c", "view": "session"},
        {"id": "c-tool", "role": "assistant", "text": "", "timestamp": "2026-12-02T10:00:02Z", "trace_id": "t-c", "view": "session",
         "tools": [{"name": "Bash", "summary": "make release-check", "status": "ok", "id": "call-c1", "command": "make release-check", "result": "ok"}]},
        {"id": "c-final", "role": "assistant", "text": "The release is ready.", "phase": "final", "timestamp": "2026-12-02T10:00:05Z", "trace_id": "t-c",
         "view": "conversation", "session_rows": 2,
         "turn": {"turn_id": "t-c", "status": "completed", "started_at_ms": 1_000, "ended_at_ms": 6_000, "worked_ms": 5_000, "tool_count": 1}},
        {"id": "c-next", "role": "user", "text": "Ship it.", "origin": "user", "timestamp": "2026-12-02T10:01:00Z", "trace_id": "t-c-next", "view": "conversation"},
    ])
}

fn shown_ids() -> Vec<String> {
    crate::window().map(|w| rows(&w)).unwrap_or_default().iter().map(|r| r.id.to_string()).collect()
}

fn row(id: &str) -> Option<crate::MessageRow> {
    crate::window().map(|w| rows(&w)).unwrap_or_default().into_iter().find(|r| r.id == id)
}

fn live_rows() -> Vec<crate::MessageRow> {
    crate::window().map(|w| rows(&w)).unwrap_or_default().into_iter().filter(|r| !r.live.key.is_empty()).collect()
}

fn live_row(key: &str) -> Option<crate::MessageRow> {
    live_rows().into_iter().find(|r| r.live.key == key)
}

fn setting(app: &crate::App, id: &str) -> Option<crate::SettingRow> {
    crate::settings_view::rows(app).into_iter().find(|r| r.id == id)
}

fn replay(through: usize) -> bool {
    let sent = control("/__control/live-replay", &json!({"session": "rachel", "fixture": "turn-full", "through": through}));
    if let Err(error) = &sent {
        check(false, &format!("the Host replays the stream: {error}"));
    }
    sent.is_ok()
}

fn lseq() -> Option<i64> {
    app_now().engine.borrow().live_view("rachel").and_then(|v| v.lseq())
}

fn written() -> Vec<(String, String)> {
    posts("/agent-chat-view")
        .iter()
        .map(|p| (p["body"]["session"].as_str().unwrap_or_default().to_owned(), p["body"]["chat_view"].as_str().unwrap_or_default().to_owned()))
        .collect()
}

pub fn chat_view_check(out: String) {
    let (out1, out2, out3, out4) = (out.clone(), out.clone(), out.clone(), out.clone());
    let stages: Vec<Stage> = vec![
        ("open", Box::new(|app, window, _| {
            if !app.engine.borrow().conversation("rachel").is_some_and(|c| !c.rows().is_empty()) {
                return false;
            }
            check(!app.engine.borrow().chat_view_feature("rachel"), "without chat_view the chat has no view to choose");
            check(app.engine.borrow().chat_view("rachel").is_none(), "and draws as before");
            check(setting(app, "clean-chat").is_none(), "Settings do not list Clean chat");
            let app = app_now();
            crate::chat_menu::open(&app, window, &app.active_id());
            check(!window.global::<crate::ChatMenu>().get_view_shown(), "the ⋯ menu offers no Chat view");
            check(window.global::<crate::ChatMenu>().get_activity_shown(), "and keeps Tool activity");
            crate::chat_menu::close(window);
            check(control("/__control/turn-summary", &json!({"on": true})).is_ok(), "the Host sums turns up in /log");
            check(control("/__control/upsert", &json!({"session": "mike", "turns": tagged_rows()})).is_ok(), "Mike's tagged turn lands");
            check(control("/__control/chat-view", &json!({"on": true})).is_ok(), "the Host turns chat_view on");
            app.engine.borrow_mut().reconnect();
            true
        })),
        ("feature", Box::new(|app, window, elapsed| {
            if !app.engine.borrow().chat_view_feature("mike") {
                return false;
            }
            if app.engine.borrow().selected_session() != "mike" {
                app.engine.borrow_mut().select("mike");
                return false;
            }
            let ids = shown_ids();
            if !ids.iter().any(|id| id == "c-final") || elapsed < Duration::from_millis(400) {
                return false;
            }
            check(app.engine.borrow().chat_view("mike") == Some(clarp_core::chat_view::Choice::Session), "the Host's default is Session");
            check(ids.iter().any(|id| id == "live:fold:t-c"), &format!("Session view draws the turn's work row as before: {ids:?}"));
            check(setting(app, "clean-chat").is_some_and(|r| !r.on), "Settings list Clean chat, off");
            let app = app_now();
            crate::chat_menu::open(&app, window, &app.active_id());
            let menu = window.global::<crate::ChatMenu>();
            check(menu.get_view_shown(), "the ⋯ menu offers Chat view");
            check(menu.get_view_label() == "Follow Settings", &format!("showing Follow Settings: {:?}", menu.get_view_label()));
            let choices: Vec<String> = menu.get_view_choices().iter().map(|c| c.label.to_string()).collect();
            check(choices == ["Follow Settings", "Conversation", "Session"], &format!("three choices: {choices:?}"));
            menu.set_submenu("view".into());
            crate::chat_menu::pick(&app, window, "view", "conversation");
            true
        })),
        ("conversation", Box::new(move |app, window, elapsed| {
            let ids = shown_ids();
            if ids.iter().any(|id| id == "live:fold:t-c") || elapsed < Duration::from_millis(400) {
                return false;
            }
            check(written().contains(&("mike".to_owned(), "conversation".to_owned())), &format!("the choice goes to the chat's Host: {:?}", written()));
            check(app.engine.borrow().chat_view("mike") == Some(clarp_core::chat_view::Choice::Conversation), "the chat is in Conversation view");
            check(window.global::<crate::ChatMenu>().get_open().is_empty(), "the menu closed");
            check(["c-ask", "c-final", "c-next"].iter().all(|id| ids.iter().any(|i| i == id)), &format!("the user's messages and the reply stay: {ids:?}"));
            check(!ids.iter().any(|id| id == "c-note" || id == "c-tool"), &format!("the commentary and the tool are left out, the work row too: {ids:?}"));
            let reply = row("c-final").unwrap_or_default();
            check(reply.work_key == "work:t-c" && !reply.work_open, &format!("the reply offers Show work: {:?}", reply.work_key));
            check(crate::artifacts_view::selectables(app).iter().any(|k| k == "work:t-c"), "J/K reach the reply");
            let app = app_now();
            crate::chat_menu::open(&app, window, &app.active_id());
            check(!window.global::<crate::ChatMenu>().get_activity_shown(), "the ⋯ menu leaves Tool activity out in Conversation view");
            check(window.global::<crate::ChatMenu>().get_view_label() == "Conversation", "and shows the chat's own view");
            crate::chat_menu::close(window);
            app.focus_transcript();
            shot(&out1, "chat-view-01-conversation");
            // The reply's Show work (its button and O do the same).
            crate::artifacts_view::open(&app, window, "work:t-c");
            true
        })),
        ("show work", Box::new(move |_, _window, elapsed| {
            let ids = shown_ids();
            if !ids.iter().any(|id| id == "live:fold:t-c") || elapsed < Duration::from_millis(300) {
                return false;
            }
            check(live_row("live:fold:t-c").is_some_and(|r| r.live.expanded), "Show work opens the turn's work row, in place");
            check(row("c-final").is_some_and(|r| r.work_open), "the reply now offers Hide work");
            check(live_rows().iter().any(|r| r.live.title.contains("make release-check")), &format!("its tool shows: {:?}", live_rows().iter().map(|r| r.live.title.to_string()).collect::<Vec<_>>()));
            shot(&out2, "chat-view-02-show-work");
            crate::artifacts_view::open(&app_now(), &crate::window().expect("window"), "work:t-c");
            true
        })),
        ("hide work", Box::new(|app, window, elapsed| {
            let ids = shown_ids();
            if ids.iter().any(|id| id == "live:fold:t-c") || elapsed < Duration::from_millis(300) {
                return false;
            }
            check(row("c-final").is_some_and(|r| !r.work_open), "Hide work folds it again");
            // C: the other view, from the keyboard.
            check(crate::commands::run(&app_now(), window, "chat-view"), "C switches the chat's view");
            check(app.engine.borrow().chat_view("mike") == Some(clarp_core::chat_view::Choice::Session), "to Session");
            true
        })),
        ("session again", Box::new(|_, _window, elapsed| {
            let ids = shown_ids();
            if !ids.iter().any(|id| id == "live:fold:t-c") || elapsed < Duration::from_millis(300) {
                return false;
            }
            check(written().last() == Some(&("mike".to_owned(), "session".to_owned())), &format!("written to the Host: {:?}", written()));
            check(row("c-final").is_some_and(|r| r.work_key.is_empty()), "Session view has no Show work");
            // A refused write keeps the choice on this device.
            check(control("/__control/chat-view", &json!({"refuse": true})).is_ok(), "the Host refuses the next write");
            app_now().engine.borrow_mut().choose_chat_view_menu("mike", "conversation");
            true
        })),
        ("refused", Box::new(|app, _window, elapsed| {
            let refused = app.engine.borrow().chat_view_setting("mike").refused;
            if !refused || elapsed < Duration::from_millis(200) {
                return false;
            }
            check(app.engine.borrow().chat_view("mike") == Some(clarp_core::chat_view::Choice::Conversation), "the refused choice stays on this device");
            check(app.engine.borrow().error().contains("This device keeps the view"), &format!("one line says so: {:?}", app.engine.borrow().error()));
            check(!shown_ids().iter().any(|id| id == "c-note"), "the chat is drawn in Conversation view");
            // Another device's choice replaces this device's own.
            check(control("/__control/chat-view", &json!({"set": {"session": "mike", "chat_view": "session"}})).is_ok(), "another device chooses Session");
            true
        })),
        ("another device", Box::new(|app, _window, elapsed| {
            if app.engine.borrow().chat_view("mike") != Some(clarp_core::chat_view::Choice::Session) || elapsed < Duration::from_millis(300) {
                return false;
            }
            check(app.engine.borrow().chat_view_menu_value("mike") == "default", "this device's own choice gave way: Follow Settings");
            app.engine.borrow_mut().clear_error();
            crate::settings_view::change(&app_now(), &crate::window().expect("window"), "clean-chat", 1);
            true
        })),
        ("clean chat", Box::new(move |app, window, elapsed| {
            if !app.engine.borrow().clean_chat() || elapsed < Duration::from_millis(300) {
                return false;
            }
            check(setting(app, "clean-chat").is_some_and(|r| r.on), "Clean chat is on");
            check(app.engine.borrow().chat_view("mike") == Some(clarp_core::chat_view::Choice::Conversation), "every chat without its own choice is in Conversation view");
            check(app.engine.borrow().chat_view("rachel") == Some(clarp_core::chat_view::Choice::Conversation), "Rachel's too");
            check(written().len() == 3, &format!("Clean chat writes nothing to the Host: {:?}", written()));
            let app = app_now();
            crate::profile_view::open(&app, window, "mike");
            true
        })),
        ("profile", Box::new(|app, window, _| {
            let info = window.global::<crate::ProfileBridge>().get_info();
            if info.session != "mike" {
                return false;
            }
            check(info.chat_view_shown && info.chat_view_index == 0, &format!("the profile offers Chat view, Follow Settings: {}", info.chat_view_index));
            check(!info.tool_activity_applies, "and Tool activity does not apply in Conversation view");
            crate::profile_view::close(app, window);
            app.engine.borrow_mut().select("rachel");
            check(control("/__control/live", &json!({"on": true})).is_ok(), "the Host turns live items on");
            app.engine.borrow_mut().reconnect();
            true
        })),
        ("subscribed", Box::new(|app, _window, _| {
            let subscribed = requests("GET", "/events").iter().any(|r| r["query"].get("live").and_then(|l| l.as_str()).is_some_and(|l| l.split(',').any(|s| s == "rachel")));
            if !(app.engine.borrow().live_items() && lseq() == Some(0) && subscribed && app.engine.borrow().chat_view_feature("rachel")) {
                return false;
            }
            replay(AFTER_TAIL)
        })),
        ("running box", Box::new(move |app, _window, elapsed| {
            if lseq() != Some(16) || elapsed < Duration::from_millis(600) {
                return false;
            }
            let titles: Vec<String> = live_rows().iter().map(|r| r.live.title.to_string()).collect();
            let fold = live_row(FOLD).unwrap_or_default();
            check(fold.live.title.ends_with('…') && !fold.live.title.starts_with("Working"), &format!("one Running box in the status line's words: {titles:?}"));
            check(live_rows().iter().filter(|r| r.live.key.contains("tr-1") || r.live.key.starts_with("live:cl:")).count() == 1, &format!("and nothing else of the running turn: {titles:?}"));
            app_now().focus_transcript();
            shot(&out3, "chat-view-03-running");
            crate::artifacts_view::toggle_live(app, FOLD);
            true
        })),
        ("running box open", Box::new(move |app, _window, elapsed| {
            if !live_row(FOLD).is_some_and(|r| r.live.expanded) || elapsed < Duration::from_millis(300) {
                return false;
            }
            let titles: Vec<String> = live_rows().iter().map(|r| r.live.title.to_string()).collect();
            check(titles.len() > 1, &format!("opened, the turn's steps list under it: {titles:?}"));
            check(live_row("live:cl:msg_01:1").is_none(), "never its words");
            shot(&out4, "chat-view-04-running-open");
            crate::artifacts_view::toggle_live(app, FOLD);
            replay(AFTER_INTERRUPT)
        })),
        ("ended", Box::new(|_, _window, elapsed| {
            if lseq() != Some(23) || elapsed < Duration::from_millis(600) {
                return false;
            }
            check(live_row(FOLD).is_none(), &format!("the ended turn's work leaves the chat: {:?}", live_rows().iter().map(|r| r.live.title.to_string()).collect::<Vec<_>>()));
            true
        })),
    ];
    run_stages(stages);
}
