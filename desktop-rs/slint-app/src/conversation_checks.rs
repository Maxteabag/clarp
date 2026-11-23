//! `--check conversation-view`: Conversation and Session views of a chat,
//! chosen per agent (#702, feature `chat_view`). Without the feature the
//! chat is as before and its menu offers no view. With it, the agent's
//! Conversation view leaves out every session row (commentary, tools, a
//! run between agents); a finished turn's reply opens them again with Show
//! work, inside one bordered box under the reply (#727); a working agent is one Running box, from the snapshot alone and
//! from a live turn, which names the agent's own turn beside a
//! conversation fork's (contract 153); W opens and folds the work from the
//! keyboard; a turn another agent started reads "Working with <Name>" and
//! the kind of step only; a stalled turn says so in the box, with Stop
//! inside it, and the pane's own stalled line stands down; the chat's menu
//! and its profile pick a view and tell the Host; Clean chat puts every
//! chat in Conversation; a write the Host refuses keeps the choice on this
//! device and says so. With `turn_end_reason` (#731) a turn stopped before
//! it did any work says why in one quiet line under it; a completed turn
//! says nothing.

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

/// Where the drawn element with accessible id `id` is: (x, y, width,
/// height) in the window; None when it is not drawn.
fn frame(id: &str) -> Option<(f32, f32, f32, f32)> {
    use i_slint_backend_testing::ElementQuery;
    let window = crate::window()?;
    let id = id.to_owned();
    ElementQuery::from_root(&window)
        .match_predicate(move |e| e.accessible_id().is_some_and(|a| a == id.as_str()) && e.size().width > 0.0 && e.size().height > 0.0)
        .find_first()
        .map(|e| {
            let (p, s) = (e.absolute_position(), e.size());
            (p.x, p.y, s.width, s.height)
        })
}

/// #727: the rows `ids` are drawn as one bordered box, in that order: the
/// first its top band, the last its bottom one, the bands joined edge to
/// edge with one left and right side, and each row's own drawing inside
/// it. Says what it found.
fn one_box(ids: &[&str]) -> Result<(), String> {
    let bands: Vec<String> = ids.iter().map(|id| row(id).map(|r| r.work_box.to_string()).unwrap_or_else(|| "missing".into())).collect();
    let want: Vec<&str> = match ids.len() {
        1 => vec!["only"],
        n => std::iter::once("top").chain(std::iter::repeat_n("middle", n - 2)).chain(std::iter::once("bottom")).collect(),
    };
    if bands != want {
        return Err(format!("bands {bands:?}, want {want:?}"));
    }
    let boxes: Vec<(f32, f32, f32, f32)> = ids.iter().map(|id| frame(&format!("work-box:{id}")).ok_or(format!("no box band drawn for {id}"))).collect::<Result<_, _>>()?;
    let first = boxes[0];
    for (id, band) in ids.iter().zip(&boxes) {
        if (band.0 - first.0).abs() > 0.5 || (band.2 - first.2).abs() > 0.5 {
            return Err(format!("{id}'s band is not under the box's sides: {band:?} vs {first:?}"));
        }
        let Some(drawn) = frame(&format!("row:{id}")) else { return Err(format!("{id} is not drawn")) };
        let inside = drawn.0 >= band.0 && drawn.0 + drawn.2 <= band.0 + band.2 && drawn.1 >= band.1 && drawn.1 + drawn.3 <= band.1 + band.3 + 0.5;
        if !inside {
            return Err(format!("{id} is drawn outside its band: {drawn:?} in {band:?}"));
        }
    }
    for (pair, ids) in boxes.windows(2).zip(ids.windows(2)) {
        if (pair[0].1 + pair[0].3 - pair[1].1).abs() > 1.0 {
            return Err(format!("{} and {} leave a gap in the box: {:?} then {:?}", ids[0], ids[1], pair[0], pair[1]));
        }
    }
    Ok(())
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
    let (out8, out9, out10) = (out.clone(), out.clone(), out.clone());
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
            let at = |id: &str| ids.iter().position(|i| i == id).map_or(-1, |i| i as i64);
            check(at("u-1") < at("f-1") && at("f-1") + 1 == at("w-1") && at("w-1") + 1 == at("w-2") && at("w-2") < at("u-2"),
                &format!("the turn's left-out rows show under its reply, in their order (#727): {ids:?}"));
            let boxed = one_box(&["w-1", "w-2"]);
            check(boxed.is_ok(), &format!("inside one bordered box, as on iOS: {boxed:?}"));
            check(row("f-1").is_some_and(|r| r.work_box.is_empty()) && row("u-2").is_some_and(|r| r.work_box.is_empty()), "the reply and the next message are outside it");
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
            check(shown().iter().all(|r| r.work_box.is_empty()), "and no box is left");
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
            let boxed = one_box(&[BOX, "live:it-1"]);
            check(boxed.is_ok(), &format!("inside its border (#727): {boxed:?}"));
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
            check(shown().iter().all(|r| r.work_box.is_empty()), "with no work box: Session view is unchanged (#727)");
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
            check(!has(&clarp_core::turn_reason::key("t-3")), "without turn_end_reason a stopped turn says nothing new");
            // A turn stopped while it waited for an account (#729): no work at
            // all, its prompt closes it, and the Host says why (#731).
            let stopped = json!([{"id": "u-3", "role": "user", "text": "Write the summary", "origin": "user", "trace_id": "t-3", "view": "conversation",
                "timestamp": "2026-11-23T10:04:00Z",
                "turn": {"turn_id": "t-3", "status": "interrupted", "started_at_ms": 2_000, "ended_at_ms": 9_000, "worked_ms": 7_000, "tool_count": 0,
                    "reason": "Stopped by its user."}}]);
            check(control("/__control/upsert", &json!({"session": SESSION, "turns": stopped})).is_ok(), "a turn is stopped before it did any work");
            true
        })),
        ("no reason without the feature", Box::new(|app, _window, elapsed| {
            if !has("u-3") || elapsed < Duration::from_millis(300) {
                return false;
            }
            check(!has(&clarp_core::turn_reason::key("t-3")), &format!("without turn_end_reason the chat is as before: {:?}", ids()));
            check(control("/__control/turn-reason", &json!({"on": true})).is_ok(), "the Host turns turn_end_reason on");
            app.engine.borrow_mut().reconnect();
            true
        })),
        ("turn reason", Box::new(move |app, _window, elapsed| {
            let line = clarp_core::turn_reason::key("t-3");
            if !app.engine.borrow().turn_end_reason(SESSION) || !has(&line) || elapsed < Duration::from_millis(300) {
                return false;
            }
            let ids = ids();
            let at = |id: &str| ids.iter().position(|i| i == id);
            check(row(&line).is_some_and(|r| r.author == "system" && r.blocks.row_count() == 1), "the Host's reason is one quiet system line");
            // Under the stopped turn: nothing but live rows between them.
            let under = match (at("u-3"), at(line.as_str())) {
                (Some(u), Some(l)) => u < l && ids[u + 1..l].iter().all(|i| i.starts_with("live:")),
                _ => false,
            };
            check(under, &format!("under the stopped turn, which did no work: {ids:?}"));
            check(!has(&clarp_core::turn_reason::key("t-1")), "a completed turn never shows a line");
            shot(&out10, "conversation-10-turn-reason");
            true
        })),
    ];
    run_stages(stages);
}

/// What the agent writes for Bella after its note (the fake Host's
/// `FOR_BELLA`): never drawn in a chat that hides coordination.
const FOR_BELLA: &str = "Bella, the build is green, as you asked.";
const FLIP_C: &str = "t-flip-c";
const FLIP_S: &str = "t-flip-s";

/// Every word the chat draws: rows' blocks and live rows' lines.
fn drawn_text() -> String {
    let mut all = String::new();
    for r in shown() {
        for b in r.blocks.iter() {
            all.push_str(&b.text);
            all.push('\n');
        }
        for part in [&r.live.title, &r.live.secondary, &r.live.text, &r.live.meta] {
            all.push_str(part);
            all.push('\n');
        }
        for line in r.live.lines.iter() {
            all.push_str(&line);
            all.push('\n');
        }
    }
    all
}

/// Step `step` of the fake Host's sequence for turn `turn`.
fn flip(turn: &str, step: i64) -> bool {
    control("/__control/audience-flip", &json!({"session": SESSION, "turn_id": turn, "step": step})).is_ok()
}

/// The live turn's audience and whether it holds item `id`, as the desktop
/// has them.
fn live_turn(app: &crate::App, turn: &str, item: &str) -> Option<String> {
    let engine = app.engine.borrow();
    let view = engine.live_view(SESSION)?;
    let held = view.turn()?;
    if held.get("turn_id").and_then(|v| v.as_str()) != Some(turn) || (!item.is_empty() && view.item(item).is_none()) {
        return None;
    }
    Some(held.get("audience").and_then(|v| v.as_str()).unwrap_or("user").to_owned())
}

fn not_for_bella(what: &str) {
    let text = drawn_text();
    check(!text.contains(FOR_BELLA), &format!("{what}: what the agent wrote for Bella after its note is not drawn"));
}

/// The rows drawn before the flip that are gone now (the Running box's
/// own key aside: its words change with the audience).
fn gone(before: &[String], fold: &str) -> Vec<String> {
    let now = ids();
    before.iter().filter(|id| id.as_str() != fold && !now.contains(id)).cloned().collect()
}

/// `--check audience-flip`: a live turn whose audience goes back from the
/// user to other agents (#733, Rust Host contract 164, feature
/// `turn_audience_returns`), in a chat that hides coordination between
/// agents, in Conversation view and then in Session view. Bella starts the
/// turn; the user's message is read in (the turn is the user's); the agent
/// leaves a note and the turn is Bella's again: what was drawn stays drawn
/// with no gap, the note shows, and nothing it writes for Bella is drawn;
/// the user's next message makes it the user's again (the Running box says
/// Running and opens); the end keeps the note and the user's answer and
/// still leaves Bella's words out. The flips run only with the feature on:
/// the fake Host refuses step 3 without it.
pub fn audience_flip_check(out: String) {
    use std::cell::RefCell;
    use std::rc::Rc;
    let (out1, out2, out3, out4, out5, out6, out7, out8) = (out.clone(), out.clone(), out.clone(), out.clone(), out.clone(), out.clone(), out.clone(), out.clone());
    let before: Rc<RefCell<Vec<String>>> = Rc::default();
    let gaps: Rc<RefCell<Vec<String>>> = Rc::default();
    let (before1, before2, before3, before4) = (before.clone(), before.clone(), before.clone(), before.clone());
    let (gaps1, gaps2) = (gaps.clone(), gaps.clone());
    let fold_c = format!("live:fold:{FLIP_C}");
    let fold_s = format!("live:fold:{FLIP_S}");
    let (fold_c1, fold_c2, fold_c3, fold_c4, fold_c5) = (fold_c.clone(), fold_c.clone(), fold_c.clone(), fold_c.clone(), fold_c.clone());
    let fold_s1 = fold_s.clone();
    let id = |turn: &str, suffix: &str| format!("{turn}-{suffix}");
    let stages: Vec<Stage> = vec![
        ("open", Box::new(|app, _window, _| {
            if !app.engine.borrow().conversation(SESSION).is_some_and(|c| !c.rows().is_empty()) {
                return false;
            }
            check(!flip(FLIP_C, 3), "without turn_audience_returns the Host never turns a turn back to agents");
            let rows = json!([
                {"id": "r1", "role": "user", "text": "Hello", "origin": "user", "view": "conversation", "timestamp": "2026-11-23T10:00:00Z"},
                {"id": "r2", "role": "assistant", "text": "Hi, how can I help?", "phase": "final", "view": "conversation", "timestamp": "2026-11-23T10:00:05Z"},
            ]);
            check(control("/__control/turns", &json!({"session": SESSION, "turns": rows})).is_ok(), "the chat's rows land with the Host's view tags");
            check(control("/__control/agent", &json!({"session": SESSION, "set": {"chat_view": "conversation"}})).is_ok(), "the agent's chat view is Conversation on the Host");
            check(control("/__control/chat-view", &json!({"on": true})).is_ok(), "the Host turns chat_view on");
            check(control("/__control/live", &json!({"on": true})).is_ok(), "and live items");
            check(control("/__control/coordination", &json!({"on": true})).is_ok(), "and hidden_coordination (its value: hide)");
            check(control("/__control/audience-flip", &json!({"on": true})).is_ok(), "and turn_audience_returns");
            // Session view draws each item in its row.
            app.engine.borrow_mut().set_activity_mode(clarp_core::presentation::ALWAYS_VISIBLE);
            app.engine.borrow_mut().reconnect();
            true
        })),
        ("ready", Box::new(|app, _window, elapsed| {
            let subscribed = requests("GET", "/events").iter().any(|r| r["query"].get("live").and_then(|l| l.as_str()).is_some_and(|l| l.split(',').any(|s| s == SESSION)));
            let engine = app.engine.borrow();
            let ready = engine.chat_view_offered(SESSION) && engine.live_items() && engine.hidden_coordination() && subscribed
                && engine.live_view(SESSION).and_then(|v| v.lseq()).is_some() && crate::live_view::veils(&engine, SESSION) && has("r2")
                && engine.chat_view_conversation(SESSION);
            drop(engine);
            if !ready || elapsed < Duration::from_millis(300) {
                return false;
            }
            check(app.engine.borrow().chat_view_conversation(SESSION), "the chat is in Conversation view");
            check(flip(FLIP_C, 1), "Bella starts a turn");
            true
        })),
        ("conversation: for bella", Box::new(move |app, _window, elapsed| {
            let Some(boxed) = row(&fold_c1) else { return false };
            if live_turn(app, FLIP_C, &id(FLIP_C, "m0")).as_deref() != Some("agents") || boxed.live.title != "Working with Bella" || elapsed < Duration::from_millis(300) {
                return false;
            }
            check(!boxed.live.expandable, "the turn is Bella's: its box does not open");
            check(!drawn_text().contains("Bella asked me to look at the build."), "and nothing of it is drawn");
            check(flip(FLIP_C, 2), "the user's message is read into the turn");
            true
        })),
        ("conversation: stepped in", Box::new(move |app, _window, elapsed| {
            let Some(boxed) = row(&fold_c2) else { return false };
            if live_turn(app, FLIP_C, &id(FLIP_C, "c4")).as_deref() != Some("user") || !boxed.live.title.starts_with("Running") || !has(&id(FLIP_C, "u1")) || elapsed < Duration::from_millis(400) {
                return false;
            }
            check(boxed.live.expandable, &format!("the turn is the user's: its Running box opens ({:?})", boxed.live.title));
            *before1.borrow_mut() = ids();
            shot(&out1, "audience-flip-01-conversation-user");
            check(flip(FLIP_C, 3), "the agent leaves a note and goes on for Bella");
            true
        })),
        ("conversation: note", Box::new(move |app, _window, elapsed| {
            let missing = gone(&before2.borrow(), &fold_c3);
            if !missing.is_empty() {
                gaps1.borrow_mut().extend(missing);
            }
            if drawn_text().contains(FOR_BELLA) {
                gaps1.borrow_mut().push("FOR_BELLA".into());
            }
            let flipped = live_turn(app, FLIP_C, &id(FLIP_C, "c6")).as_deref() == Some("agents");
            if !flipped || !has(&id(FLIP_C, "note")) || elapsed < Duration::from_millis(800) {
                return false;
            }
            check(gaps1.borrow().is_empty(), &format!("from the flip on, what was drawn stays drawn and nothing for Bella is: {:?} (before {:?})", gaps1.borrow(), before2.borrow()));
            let boxed = row(&fold_c3).unwrap_or_default();
            check(boxed.live.title == "Working with Bella" && !boxed.live.expandable, &format!("the box says the turn is Bella's again: {:?}", boxed.live.title));
            check(has(&id(FLIP_C, "note")), "the note shows");
            check(!has(&id(FLIP_C, "live")) && !has(&format!("live:{FLIP_C}-m5")) && !has(&format!("live:{FLIP_C}-c6")), &format!("no row of Bella's is drawn: {:?}", ids()));
            not_for_bella("Conversation, after the note");
            shot(&out2, "audience-flip-02-conversation-note");
            gaps1.borrow_mut().clear();
            check(flip(FLIP_C, 4), "the user's next message is read in");
            true
        })),
        ("conversation: user again", Box::new(move |app, _window, elapsed| {
            let Some(boxed) = row(&fold_c4) else { return false };
            if live_turn(app, FLIP_C, &id(FLIP_C, "m7")).as_deref() != Some("user") || !boxed.live.title.starts_with("Running") || !has(&id(FLIP_C, "u2")) || elapsed < Duration::from_millis(400) {
                return false;
            }
            check(boxed.live.expandable, &format!("the turn reads as the user's again: the box says Running and opens ({:?})", boxed.live.title));
            check(has(&id(FLIP_C, "note")), "the note still shows");
            not_for_bella("Conversation, the user's again");
            shot(&out3, "audience-flip-03-conversation-user-again");
            check(flip(FLIP_C, 5), "the turn ends");
            true
        })),
        ("conversation: ended", Box::new(move |_app, window, elapsed| {
            if has(&fold_c5) || !has(&id(FLIP_C, "final")) || elapsed < Duration::from_millis(600) {
                return false;
            }
            check(has(&id(FLIP_C, "note")) && has(&id(FLIP_C, "u1")) && has(&id(FLIP_C, "u2")), &format!("the note and the user's messages stay: {:?}", ids()));
            not_for_bella("Conversation, the turn over");
            shot(&out4, "audience-flip-04-conversation-ended");
            menu_open(window);
            crate::chat_menu::pick(&app_now(), window, "view", "session");
            true
        })),
        ("session: the turn before", Box::new(move |app, _window, elapsed| {
            if app.engine.borrow().chat_view(SESSION) != "session" || !has(&id(FLIP_C, "r1")) || elapsed < Duration::from_millis(400) {
                return false;
            }
            check(has(&id(FLIP_C, "note")), "Session view: the finished turn's note shows");
            not_for_bella("Session, the finished turn");
            check(flip(FLIP_S, 1), "Bella starts the next turn");
            true
        })),
        ("session: for bella", Box::new(move |app, _window, elapsed| {
            if live_turn(app, FLIP_S, &id(FLIP_S, "m0")).as_deref() != Some("agents") || elapsed < Duration::from_millis(500) {
                return false;
            }
            check(!has(&format!("live:{FLIP_S}-m0")) && !has(&id(FLIP_S, "ask")), &format!("a turn of Bella's the user never saw is not drawn: {:?}", ids()));
            check(flip(FLIP_S, 2), "the user's message is read into the turn");
            true
        })),
        ("session: stepped in", Box::new(move |app, _window, elapsed| {
            if live_turn(app, FLIP_S, &id(FLIP_S, "c4")).as_deref() != Some("user") || !has(&format!("live:{FLIP_S}-c4")) || !has(&id(FLIP_S, "u1")) || elapsed < Duration::from_millis(400) {
                return false;
            }
            check(has(&format!("live:{FLIP_S}-m0")) && has(&format!("live:{FLIP_S}-m1")) && has(&id(FLIP_S, "ask")), &format!("the user's turn is drawn from then on: {:?}", ids()));
            *before3.borrow_mut() = ids();
            shot(&out5, "audience-flip-05-session-user");
            check(flip(FLIP_S, 3), "the agent leaves a note and goes on for Bella");
            true
        })),
        ("session: note", Box::new(move |app, _window, elapsed| {
            let missing = gone(&before4.borrow(), &fold_s1);
            if !missing.is_empty() {
                gaps2.borrow_mut().extend(missing);
            }
            if drawn_text().contains(FOR_BELLA) {
                gaps2.borrow_mut().push("FOR_BELLA".into());
            }
            let flipped = live_turn(app, FLIP_S, &id(FLIP_S, "c6")).as_deref() == Some("agents");
            if !flipped || !has(&id(FLIP_S, "note")) || elapsed < Duration::from_millis(800) {
                return false;
            }
            check(gaps2.borrow().is_empty(), &format!("from the flip on, what was drawn stays drawn and nothing for Bella is: {:?} (before {:?})", gaps2.borrow(), before4.borrow()));
            check(has(&id(FLIP_S, "note")), "the note shows");
            check(!has(&id(FLIP_S, "live")) && !has(&format!("live:{FLIP_S}-m5")) && !has(&format!("live:{FLIP_S}-c6")), &format!("no row of Bella's is drawn: {:?}", ids()));
            not_for_bella("Session, after the note");
            shot(&out6, "audience-flip-06-session-note");
            check(flip(FLIP_S, 4), "the user's next message is read in");
            true
        })),
        ("session: user again", Box::new(move |app, _window, elapsed| {
            if live_turn(app, FLIP_S, &id(FLIP_S, "m7")).as_deref() != Some("user") || !has(&id(FLIP_S, "u2")) || elapsed < Duration::from_millis(400) {
                return false;
            }
            check(has(&format!("live:{FLIP_S}-m7")), &format!("the turn is the user's again: what it says now is drawn: {:?}", ids()));
            check(!has(&format!("live:{FLIP_S}-m5")) && !has(&format!("live:{FLIP_S}-c6")), "what it wrote for Bella stays out");
            check(has(&id(FLIP_S, "note")), "the note still shows");
            not_for_bella("Session, the user's again");
            shot(&out7, "audience-flip-07-session-user-again");
            check(flip(FLIP_S, 5), "the turn ends");
            true
        })),
        ("session: ended", Box::new(move |_app, _window, elapsed| {
            if !has(&id(FLIP_S, "final")) || elapsed < Duration::from_millis(600) {
                return false;
            }
            check(has(&id(FLIP_S, "note")), &format!("the note stays: {:?}", ids()));
            not_for_bella("Session, the turn over");
            shot(&out8, "audience-flip-08-session-ended");
            true
        })),
    ];
    run_stages(stages);
}
