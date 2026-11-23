//! `--check conversation-view`: Conversation and Session views of a chat,
//! chosen per agent (#702, feature `chat_view`). Without the feature the
//! chat is as before and its menu offers no view. With it, the agent's
//! Conversation view leaves out every session row (commentary, tools, a
//! run between agents); a finished turn's reply opens them again with Show
//! work; a working agent is one Running box, from the snapshot alone and
//! from a live turn, which names the agent's own turn beside a
//! conversation fork's (contract 153); the chat's menu picks a view and
//! tells the Host; Clean chat puts every chat in Conversation; a write the
//! Host refuses keeps the choice on this device and says so.

use std::time::Duration;

use serde_json::json;
use slint::{ComponentHandle, Model};

use super::{Stage, app_now, check, control, posts, requests, rows, run_stages, shot};

const SESSION: &str = "rachel";
const WORK: &str = "live:work:t-1";
const BOX: &str = "live:fold:t-2";

fn now_ms() -> i64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map_or(0, |d| d.as_millis() as i64)
}

fn shown() -> Vec<crate::MessageRow> {
    crate::window().map(|w| rows(&w)).unwrap_or_default()
}

fn ids() -> Vec<String> {
    shown().iter().map(|r| r.id.to_string()).collect()
}

fn row(id: &str) -> Option<crate::MessageRow> {
    shown().into_iter().find(|r| r.id == id)
}

fn has(id: &str) -> bool {
    ids().iter().any(|i| i == id)
}

fn setting(app: &crate::App, id: &str) -> Option<crate::SettingRow> {
    crate::settings_view::rows(app).into_iter().find(|r| r.id == id)
}

/// A finished turn the user asked for (its commentary and tool call are
/// session rows, its reply the anchor), a run between agents, and the
/// user's next message, as a Host with `chat_view` tags them.
fn tagged_rows() -> serde_json::Value {
    json!([
        {"id": "r1", "role": "user", "text": "Hello", "origin": "user", "view": "conversation", "timestamp": "2026-11-23T10:00:00Z"},
        {"id": "r2", "role": "assistant", "text": "Hi, how can I help?", "phase": "final", "view": "conversation", "timestamp": "2026-11-23T10:00:05Z"},
        {"id": "u-1", "role": "user", "text": "Is the build green?", "origin": "user", "trace_id": "t-1", "view": "conversation", "timestamp": "2026-11-23T10:01:00Z"},
        {"id": "w-1", "role": "assistant", "text": "Checking the build log first.", "phase": "commentary", "trace_id": "t-1", "view": "session", "timestamp": "2026-11-23T10:01:01Z"},
        {"id": "w-2", "role": "assistant", "text": "", "trace_id": "t-1", "view": "session", "timestamp": "2026-11-23T10:01:02Z",
         "tools": [{"name": "Bash", "summary": "make build", "status": "ok", "id": "call-1", "command": "make build", "result": "ok"}]},
        {"id": "f-1", "role": "assistant", "text": "All green: the build and its tests pass.", "phase": "final", "trace_id": "t-1", "view": "conversation",
         "session_rows": 2, "timestamp": "2026-11-23T10:01:06Z",
         "turn": {"turn_id": "t-1", "status": "completed", "started_at_ms": 1_000, "ended_at_ms": 7_000, "worked_ms": 6_000, "tool_count": 1}},
        {"id": "a-1", "role": "user", "text": "Can you look at the deploy for me?", "origin": "agent", "sender_name": "Arnold", "audience": "agents",
         "trace_id": "t-a", "view": "session", "timestamp": "2026-11-23T10:02:00Z"},
        {"id": "a-2", "role": "assistant", "text": "The deploy is fine.", "audience": "agents", "trace_id": "t-a", "view": "session",
         "timestamp": "2026-11-23T10:02:05Z"},
        {"id": "u-2", "role": "user", "text": "And the release notes?", "origin": "user", "trace_id": "t-2", "view": "conversation", "timestamp": "2026-11-23T10:03:00Z"},
    ])
}

/// Opens the active chat's ⋯ menu (if it is not open already).
fn menu_open(window: &crate::AppWindow) {
    let pane = app_now().active_id().to_string();
    if window.global::<crate::ChatMenu>().get_open() != pane.as_str() {
        crate::chat_menu::open(&app_now(), window, &pane);
    }
}

pub fn conversation_view_check(out: String) {
    let (out1, out2, out3, out4, out5, out6, out7) = (out.clone(), out.clone(), out.clone(), out.clone(), out.clone(), out.clone(), out.clone());
    let stages: Vec<Stage> = vec![
        ("session as before", Box::new(|app, window, _| {
            if !app.engine.borrow().conversation(SESSION).is_some_and(|c| !c.rows().is_empty()) {
                return false;
            }
            check(!app.engine.borrow().chat_view_offered(SESSION), "without chat_view the chat has no views");
            check(setting(app, "clean-chat").is_none(), "and Settings list no Clean chat");
            let pane = app.active_id().to_string();
            crate::chat_menu::open(&app_now(), window, &pane);
            let menu = window.global::<crate::ChatMenu>();
            check(!menu.get_view_shown() && menu.get_activity_shown(), "its ⋯ menu offers no Chat view, and Tool activity as before");
            crate::chat_menu::close(window);
            check(control("/__control/turns", &json!({"session": SESSION, "turns": tagged_rows()})).is_ok(), "the chat's rows land with the Host's view tags");
            check(control("/__control/agent", &json!({"session": SESSION, "set": {"chat_view": "conversation", "conversation_fork": true}})).is_ok(), "the agent's chat view is Conversation on the Host");
            check(control("/__control/chat-view", &json!({"on": true, "fork": true})).is_ok(), "the Host turns chat_view and conversation_fork on");
            app.engine.borrow_mut().reconnect();
            true
        })),
        ("conversation", Box::new(move |app, window, elapsed| {
            if !app.engine.borrow().chat_view_offered(SESSION) || !has("f-1") || has("w-1") || elapsed < Duration::from_millis(400) {
                return false;
            }
            let ids = ids();
            check(app.engine.borrow().chat_view(SESSION) == "conversation", "the chat follows the Host's value: Conversation");
            for id in ["r1", "r2", "u-1", "f-1", "u-2"] {
                check(ids.iter().any(|i| i == id), &format!("{id} (the user's words and the replies) shows: {ids:?}"));
            }
            for id in ["w-1", "w-2", "a-1", "a-2", "live:fold:t-1"] {
                check(!ids.iter().any(|i| i == id), &format!("{id} (commentary, a tool, a run between agents, the work row) is left out: {ids:?}"));
            }
            let reply = row("f-1").unwrap_or_default();
            check(reply.work_key == WORK && !reply.work_open, &format!("the finished turn's reply offers Show work: {:?}", reply.work_key));
            check(row("r2").is_some_and(|r| r.work_key.is_empty()), "a reply without left-out rows offers none");
            check(requests("GET", "/log").iter().all(|r| r["query"].get("view").is_none()), "the chat is read whole (its rows are left out here, not on the Host)");
            menu_open(window);
            let menu = window.global::<crate::ChatMenu>();
            check(menu.get_view_shown(), "the ⋯ menu offers Chat view");
            check(menu.get_view_label() == "Follow Settings · Conversation", &format!("showing what it follows: {:?}", menu.get_view_label()));
            let choices: Vec<String> = menu.get_view_choices().iter().map(|c| c.label.to_string()).collect();
            check(choices == ["Follow Settings", "Conversation", "Session"], &format!("three choices: {choices:?}"));
            check(!menu.get_activity_shown(), "Tool activity applies in Session view: Conversation leaves it out");
            crate::chat_menu::close(window);
            check(setting(app, "clean-chat").is_some_and(|r| !r.on && r.label == "Clean chat"), "Settings list Clean chat, off");
            app_now().focus_transcript();
            shot(&out1, "conversation-01-view");
            crate::artifacts_view::toggle_live(&app_now(), WORK);
            true
        })),
        ("show work", Box::new(move |_app, _window, elapsed| {
            if !has("w-1") || elapsed < Duration::from_millis(300) {
                return false;
            }
            let ids = ids();
            let at = |id: &str| ids.iter().position(|i| i == id);
            check(at("u-1") < at("w-1") && at("w-1") < at("f-1"), &format!("the turn's left-out rows show in their places, as the Session view draws them: {ids:?}"));
            check(row("f-1").is_some_and(|r| r.work_open), "the reply's Show work is lit (it folds them again)");
            check(!ids.iter().any(|i| i == "a-1"), "another turn's rows stay out");
            shot(&out2, "conversation-02-show-work");
            crate::artifacts_view::toggle_live(&app_now(), WORK);
            true
        })),
        ("hide work", Box::new(|_app, _window, elapsed| {
            if has("w-1") || elapsed < Duration::from_millis(300) {
                return false;
            }
            check(row("f-1").is_some_and(|r| !r.work_open), "Hide work folds them away again");
            // The agent works on the user's next message; no live turn says so.
            check(control("/__control/agent", &json!({"session": SESSION, "set": {"latest_state": "thinking", "busy": true, "turn_started_at": now_ms() - 65_000}})).is_ok(), "the agent starts working");
            true
        })),
        ("running from the snapshot", Box::new(move |_app, _window, elapsed| {
            let Some(boxed) = row(BOX) else { return false };
            if elapsed < Duration::from_millis(300) {
                return false;
            }
            check(boxed.live.title == "Running…", &format!("one Running box while the agent works: {:?}", boxed.live.title));
            check(boxed.live.meta.starts_with("1:0"), &format!("with how long the turn has run: {:?}", boxed.live.meta));
            check(boxed.live.shimmer == "accent", "it shimmers while the turn runs");
            let ids = ids();
            check(ids.last().map(String::as_str) == Some(BOX), &format!("under the newest message: {ids:?}"));
            shot(&out3, "conversation-03-running");
            check(control("/__control/live", &json!({"on": true})).is_ok(), "the Host turns live items on");
            app_now().engine.borrow_mut().reconnect();
            true
        })),
        ("live", Box::new(|app, _window, _| {
            let subscribed = requests("GET", "/events").iter().any(|r| r["query"].get("live").and_then(|l| l.as_str()).is_some_and(|l| l.split(',').any(|s| s == SESSION)));
            let lseq = app.engine.borrow().live_view(SESSION).and_then(|v| v.lseq());
            if !(app.engine.borrow().live_items() && lseq == Some(0) && subscribed) {
                return false;
            }
            let at = now_ms();
            let event = json!({"ops": [
                {"op": "turn", "turn": {"turn_id": "t-2", "status": "running", "started_at_ms": at - 5_000}},
                {"op": "status", "activity": {"state": "tool", "headline": "", "turn_started_ms": at - 5_000, "tool": {"name": "Bash", "label": "cargo build", "started_at_ms": at - 2_000}}},
                {"op": "upsert", "id": "it-1", "kind": "tool", "rev": 1, "item": {"turn_id": "t-2", "kind": "tool", "status": "running", "ordinal": 1, "started_at_ms": at - 2_000,
                    "tool": {"name": "Bash", "call_id": "call-2", "category": "exec", "label": "cargo build", "command": "cargo build"}}},
                {"op": "upsert", "id": "it-0", "kind": "message", "rev": 1, "item": {"turn_id": "t-2", "kind": "message", "status": "completed", "phase": "commentary", "ordinal": 0,
                    "started_at_ms": at - 4_000, "text": "Let me build it first."}},
                {"op": "main_turn", "main_turn": {"turn_id": "t-main", "audience": "agents", "status": "running", "started_at_ms": at - 60_000, "tool_count": 2,
                    "activity": {"state": "tool", "tool": {"name": "Read"}}}},
            ]});
            check(control("/__control/live-event", &json!({"session": SESSION, "event": event})).is_ok(), "a fork's turn runs a tool while the agent's own turn works for another agent");
            true
        })),
        ("running live", Box::new(move |_app, _window, elapsed| {
            let Some(boxed) = row(BOX) else { return false };
            if boxed.live.title != "Running cargo build…" || elapsed < Duration::from_millis(400) {
                return false;
            }
            check(boxed.live.secondary == "Also working with another agent · Reading files", &format!("the agent's own turn is a quiet line in the box: {:?}", boxed.live.secondary));
            check(boxed.live.expandable && !boxed.live.expanded, "the box opens in place");
            let ids = ids();
            check(!ids.iter().any(|i| i == "live:it-1" || i == "live:it-0"), &format!("the turn's tool and its words in between are not drawn: {ids:?}"));
            shot(&out4, "conversation-04-running-live");
            crate::artifacts_view::toggle_live(&app_now(), BOX);
            true
        })),
        ("box opened", Box::new(move |_app, window, elapsed| {
            if !row(BOX).is_some_and(|r| r.live.expanded) || elapsed < Duration::from_millis(300) {
                return false;
            }
            check(has("live:it-1"), &format!("opened, the box lists the turn's steps under it: {:?}", ids()));
            check(!has("live:it-0"), "its words in between stay out");
            shot(&out5, "conversation-05-box-opened");
            crate::artifacts_view::toggle_live(&app_now(), BOX);
            // The chat's own choice: Session, told to the Host.
            menu_open(window);
            crate::chat_menu::pick(&app_now(), window, "view", "session");
            true
        })),
        ("session chosen", Box::new(move |app, window, elapsed| {
            if !has("w-1") || elapsed < Duration::from_millis(300) {
                return false;
            }
            let written = posts("/agent-chat-view");
            check(written.iter().any(|p| p["body"] == json!({"session": SESSION, "chat_view": "session"})), &format!("the choice is written to the Host: {written:?}"));
            check(app.engine.borrow().chat_view_own(SESSION) == Some("session"), "and kept as the chat's own on this device");
            check(has("a-1") && has("w-1"), &format!("Session view shows everything again: {:?}", ids()));
            check(row("f-1").is_some_and(|r| r.work_key.is_empty()), "with no Show work");
            menu_open(window);
            let menu = window.global::<crate::ChatMenu>();
            check(menu.get_view_label() == "Session" && menu.get_activity_shown(), "the menu shows Session, and Tool activity again");
            crate::chat_menu::close(window);
            shot(&out6, "conversation-06-session");
            let window = crate::window().expect("window");
            crate::settings_view::change(&app_now(), &window, "clean-chat", 1);
            true
        })),
        ("clean chat", Box::new(move |app, window, elapsed| {
            if !app.engine.borrow().clean_chat() || elapsed < Duration::from_millis(300) {
                return false;
            }
            check(setting(app, "clean-chat").is_some_and(|r| r.on), "Clean chat is on");
            check(has("w-1") && app.engine.borrow().chat_view(SESSION) == "session", "the chat's own Session still wins over it");
            menu_open(window);
            crate::chat_menu::pick(&app_now(), window, "view", "default");
            // Picked on the menu directly: apply it now, as the menu's own
            // callback does (no Host reply wakes the engine for this one).
            crate::pump();
            true
        })),
        ("follow settings", Box::new(move |app, _window, elapsed| {
            if has("w-1") || elapsed < Duration::from_millis(300) {
                return false;
            }
            check(app.engine.borrow().chat_view_own(SESSION).is_none(), "Follow Settings clears the chat's own choice");
            check(app.engine.borrow().chat_view_host(SESSION) == "session", "the Host keeps Session");
            check(app.engine.borrow().chat_view(SESSION) == "conversation", "Clean chat puts the chat in Conversation");
            check(control("/__control/chat-view", &json!({"refuse": true})).is_ok(), "the Host refuses the next write");
            let window = crate::window().expect("window");
            menu_open(&window);
            crate::chat_menu::pick(&app_now(), &window, "view", "session");
            true
        })),
        ("refused", Box::new(move |app, _window, elapsed| {
            let error = app.engine.borrow().error().to_owned();
            if !error.starts_with("Not saved on") || elapsed < Duration::from_millis(300) {
                return false;
            }
            check(error.ends_with("This device keeps the view."), &format!("one quiet line says the Host refused it: {error:?}"));
            check(app.engine.borrow().chat_view_own(SESSION) == Some("session") && has("w-1"), "the choice stays on this device");
            shot(&out7, "conversation-07-refused");
            true
        })),
    ];
    run_stages(stages);
}
