//! `--check conversation-view`: Conversation and Session views of a chat,
//! chosen per agent (#702, feature `chat_view`). Without the feature the
//! chat is as before and its menu offers no view. With it, the agent's
//! Conversation view leaves out every session row (commentary, tools, a
//! run between agents); a finished turn's reply opens them again with Show
//! work; a working agent is one Running box, from the snapshot alone and
//! from a live turn, which names the agent's own turn beside a
//! conversation fork's (contract 153); W opens and folds the work from the
//! keyboard; a turn another agent started reads "Working with <Name>" and
//! the kind of step only; a stalled turn says so in the box, with Stop
//! inside it, and the pane's own stalled line stands down; the chat's menu
//! and its profile pick a view and tell the Host; Clean chat puts every
//! chat in Conversation; a write the Host refuses keeps the choice on this
//! device and says so.

use std::time::Duration;

use serde_json::json;
use slint::{ComponentHandle, Model};

use super::{Stage, app_now, check, control, posts, requests, rows, run_stages, shot, view};

const SESSION: &str = "rachel";
const WORK: &str = "live:work:t-1";
const BOX: &str = "live:fold:t-2";
/// How long the stalled turn's tool has been silent: 12 whole minutes.
const STALLED_FOR_MS: i64 = 12 * 60_000 + 30_000;

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

/// Presses the drawn element whose accessible id is `id`, as a screen
/// reader or a click would; false when it is not drawn.
fn press(id: &str) -> bool {
    use i_slint_backend_testing::ElementQuery;
    let Some(window) = crate::window() else { return false };
    let id = id.to_owned();
    ElementQuery::from_root(&window)
        .match_predicate(move |e| e.accessible_id().is_some_and(|a| a == id.as_str()) && e.size().width > 0.0 && e.size().height > 0.0)
        .find_first()
        .map(|e| e.invoke_accessible_default_action())
        .is_some()
}

/// W in the chat, as the keyboard runs it.
fn w() -> bool {
    let window = crate::window().expect("window");
    crate::commands::run(&app_now(), &window, "show-work")
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
    let (out8, out9) = (out.clone(), out.clone());
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
            // W, with the keyboard on nothing: the latest turn's work.
            check(w(), "W opens the latest finished turn's work");
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
        ("box opened", Box::new(move |_app, _window, elapsed| {
            if !row(BOX).is_some_and(|r| r.live.expanded) || elapsed < Duration::from_millis(300) {
                return false;
            }
            check(has("live:it-1"), &format!("opened, the box lists the turn's steps under it: {:?}", ids()));
            check(!has("live:it-0"), "its words in between stay out");
            shot(&out5, "conversation-05-box-opened");
            // W: the keyboard is on the box (its O opened it): it folds.
            check(w(), "W folds the Running box the keyboard is on");
            true
        })),
        ("w folds the box", Box::new(|_app, _window, elapsed| {
            if row(BOX).is_none_or(|r| r.live.expanded) || elapsed < Duration::from_millis(300) {
                return false;
            }
            check(!has("live:it-1"), "folded, its steps leave");
            // The turn now runs for another agent: who, from the Host.
            let at = now_ms();
            check(control("/__control/agent", &json!({"session": SESSION, "set": {"working_audience": "agents", "working_with": {"agent_id": "b-id", "name": "Bella"}}})).is_ok(), "the agent row says whom it works with");
            let event = json!({"ops": [
                {"op": "turn", "turn": {"turn_id": "t-2", "status": "running", "audience": "agents", "started_at_ms": at - 5_000}},
                {"op": "status", "activity": {"state": "tool", "headline": "Running clarp-admin prompt --to bella", "turn_started_ms": at - 5_000,
                    "tool": {"name": "Bash", "label": "clarp-admin prompt --to bella", "started_at_ms": at - 1_000}}},
                {"op": "main_turn", "main_turn": null},
            ]});
            check(control("/__control/live-event", &json!({"session": SESSION, "event": event})).is_ok(), "the running turn is one between agents");
            true
        })),
        ("between agents", Box::new(move |_app, _window, elapsed| {
            let Some(boxed) = row(BOX) else { return false };
            if boxed.live.title != "Working with Bella" || elapsed < Duration::from_millis(300) {
                return false;
            }
            check(boxed.live.secondary == "Running a command", &format!("its second line is the kind of step, never its text: {:?}", boxed.live.secondary));
            check(!boxed.live.expandable, "and it does not open: the turn is between agents");
            check(boxed.live.stop.is_empty(), "a working turn holds no Stop");
            shot(&out8, "conversation-08-working-with");
            // The Host marks the turn stalled, Stop offered.
            check(control("/__control/agent", &json!({"session": SESSION, "set": {"stalled_since": now_ms() - STALLED_FOR_MS, "stalled_action": "stop", "queued_turn_count": 1}})).is_ok(), "the turn stalls");
            true
        })),
        ("stalled", Box::new(move |_app, _window, elapsed| {
            let Some(boxed) = row(BOX) else { return false };
            if boxed.live.status != "stalled" || elapsed < Duration::from_millis(300) {
                return false;
            }
            let words = boxed.live.title.to_string();
            check(words == "Seems stuck · no progress for 12 min · Your message is waiting", &format!("the box says it seems stuck, in the indicator's words: {words:?}"));
            check(!boxed.live.stop.is_empty(), &format!("with Stop inside it: {:?}", boxed.live.stop));
            check(view().stall.is_empty(), &format!("the pane's own stalled line stands down for the box: {:?}", view().stall));
            check(has(BOX) && ids().last().map(String::as_str) == Some(BOX), "the stalled agent is never hidden: the box is under the newest message");
            shot(&out9, "conversation-09-stalled");
            check(press(&format!("box-stop:{BOX}")), "the box's Stop is drawn and pressed");
            true
        })),
        ("stopped", Box::new(|_app, window, _| {
            let stops = posts("/stop");
            if stops.is_empty() {
                return false;
            }
            check(stops.iter().any(|p| p["body"]["session"] == SESSION), &format!("the box's Stop stops the agent, as its key does: {stops:?}"));
            check(control("/__control/agent", &json!({"session": SESSION, "set": {"busy": false, "latest_state": "idle", "stalled_since": null, "stalled_action": null,
                "working_audience": null, "working_with": null, "queued_turn_count": 0}})).is_ok(), "the turn ends");
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
            crate::profile_view::open(&app_now(), window, SESSION);
            true
        })),
        ("the profile", Box::new(|app, window, _| {
            let info = window.global::<crate::ProfileBridge>().get_info();
            if info.session != SESSION {
                return false;
            }
            check(info.chat_view_shown, "the profile's This chat offers Chat view");
            check(info.chat_view_index == 2, &format!("showing the chat's own Session: {}", info.chat_view_index));
            check(info.tool_activity_applies, "Tool activity applies in Session view");
            crate::profile_view::close(app, window);
            crate::settings_view::change(&app_now(), window, "clean-chat", 1);
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
