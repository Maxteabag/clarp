//! `--check quiet-chat`: the quiet chat (#653). Under Tool activity
//! Grouped (the default) a running turn reads as one work row,
//! `Working · 3 tools` (the calls it folds so far, the running one too; a
//! call that failed keeps its row and is not counted), that shimmers and opens in place; settled, its time is
//! the Host's /log summary's once it lands, the live turn's until then. Hide when done leaves the finished turn's work row out and keeps
//! its messages and what failed or stopped. Driven by the fake Host
//! replaying the recorded `turn-full` stream, as `--check live` is.

use std::time::Duration;

use serde_json::json;
use slint::{ComponentHandle, Model};

use super::{Stage, app_now, check, control, posts, requests, rows, run_stages, shot};

thread_local! {
    /// The running work row's time as first seen.
    static WORKING_FOR: std::cell::RefCell<String> = const { std::cell::RefCell::new(String::new()) };
    /// The profile's artifacts request before the menu's Artifacts.
    static ARTIFACTS_BEFORE: std::cell::Cell<i32> = const { std::cell::Cell::new(0) };
}

const AFTER_TAIL: usize = 17; // lseq 16: the command runs
const AFTER_INTERRUPT: usize = 24; // lseq 23: the turn is over
const FOLD: &str = "live:fold:tr-1";

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

fn live_rows() -> Vec<crate::MessageRow> {
    crate::window().map(|w| rows(&w)).unwrap_or_default().into_iter().filter(|r| !r.live.key.is_empty()).collect()
}

fn live_row(key: &str) -> Option<crate::MessageRow> {
    live_rows().into_iter().find(|r| r.live.key == key)
}

fn titles() -> Vec<String> {
    live_rows().iter().map(|r| r.live.title.to_string()).collect()
}

fn setting(app: &crate::App, id: &str) -> Option<crate::SettingRow> {
    crate::settings_view::rows(app).into_iter().find(|r| r.id == id)
}

pub fn quiet_chat_check(out: String) {
    let (out1, out2, out3, out4, out5, out6) = (out.clone(), out.clone(), out.clone(), out.clone(), out.clone(), out.clone());
    let stages: Vec<Stage> = vec![
        ("open", Box::new(|app, _window, _| {
            let open = app.engine.borrow().conversation("rachel").is_some_and(|c| !c.rows().is_empty());
            if !open {
                return false;
            }
            check(app.engine.borrow().activity_mode() == clarp_core::presentation::GROUPED, "Tool activity is Grouped by default");
            let activity = setting(app, "activity");
            check(activity.as_ref().is_some_and(|r| r.detail == "Grouped"), &format!("Settings show Tool activity Grouped: {:?}", activity.map(|r| r.detail)));
            let choices: Vec<String> = crate::catalogue::settings()
                .iter()
                .find(|s| s.entry.id == "activity")
                .and_then(|s| match &s.control {
                    crate::catalogue::Control::Choice { options, .. } => Some(options(app).into_iter().map(|c| c.label).collect()),
                    _ => None,
                })
                .unwrap_or_default();
            check(choices == ["Grouped", "Always visible", "Group old", "Hide when done"], &format!("exactly four choices: {choices:?}"));
            check(control("/__control/live", &json!({"on": true})).is_ok(), "the Host turns live items on");
            app.engine.borrow_mut().reconnect();
            true
        })),
        ("subscribed", Box::new(|app, _window, _| {
            // The event stream carries rachel's live events before any is sent.
            let subscribed = requests("GET", "/events").iter().any(|r| r["query"].get("live").and_then(|l| l.as_str()).is_some_and(|l| l.split(',').any(|s| s == "rachel")));
            if !(app.engine.borrow().live_items() && lseq() == Some(0) && subscribed) {
                return false;
            }
            replay(AFTER_TAIL)
        })),
        ("working", Box::new(move |app, _window, elapsed| {
            if lseq() != Some(16) || elapsed < Duration::from_millis(600) {
                if elapsed > Duration::from_millis(14_800) {
                    let engine = app.engine.borrow();
                    let view = engine.live_view("rachel");
                    println!("diag lseq {:?} awaiting {:?} epoch {:?} live_items {} active {}", lseq(), view.map(|v| v.awaiting_snapshot()), view.and_then(|v| v.epoch().map(str::to_owned)), engine.live_items(), engine.live_active("rachel"));
                }
                return false;
            }
            let fold = live_row(FOLD).unwrap_or_default();
            // Read and Grep (done) and npm test (still running: it fails only at lseq 17).
            check(fold.live.title == "Working · 3 tools", &format!("the running turn is one work row with its tools so far: {:?}", titles()));
            check(fold.live.shimmer == "accent", &format!("it shimmers while the turn runs: {:?}", fold.live.shimmer));
            check(!titles().iter().any(|t| t.starts_with("Running") || t.starts_with("Thought") || t.starts_with("Explored")), &format!("its tools and thinking fold into it: {:?}", titles()));
            check(live_row("live:cl:msg_01:1").is_none(), "the commentary folds into it too, as a working detail");
            // How long the turn has run, as the explorer's "working 15s" (#846).
            let meta = fold.live.meta.to_string();
            let timed = meta.ends_with('s') && meta.starts_with(|c: char| c.is_ascii_digit());
            check(timed, &format!("the work row says how long the turn has run: {meta:?}"));
            WORKING_FOR.with(|w| *w.borrow_mut() = meta);
            app_now().focus_transcript();
            shot(&out1, "quiet-01-working");
            true
        })),
        ("working ticks", Box::new(|app, _window, elapsed| {
            let meta = live_row(FOLD).map(|r| r.live.meta.to_string()).unwrap_or_default();
            let first = WORKING_FOR.with(|w| w.borrow().clone());
            if meta == first && elapsed < Duration::from_secs(4) {
                return false;
            }
            check(meta != first, &format!("and the time ticks by itself: {first:?} then {meta:?}"));
            crate::artifacts_view::toggle_live(app, FOLD);
            true
        })),
        ("opened", Box::new(move |app, _window, _| {
            if !live_row(FOLD).is_some_and(|r| r.live.expanded) {
                return false;
            }
            let titles = titles();
            check(titles.iter().any(|t| t == "Running npm test") && titles.iter().any(|t| t == "Explored 1 file, 1 search"), &format!("opened in place: every tool row: {titles:?}"));
            check(live_row("live:cl:msg_01:1").is_some(), "and the commentary, in its place");
            shot(&out2, "quiet-02-opened");
            crate::artifacts_view::toggle_live(app, FOLD);
            replay(AFTER_INTERRUPT)
        })),
        ("settled", Box::new(move |app, _window, elapsed| {
            if lseq() != Some(23) || elapsed < Duration::from_millis(600) {
                return false;
            }
            let titles = titles();
            check(titles.first().map(String::as_str) == Some("Worked for 12s · 3 tools"), &format!("settled: the 3 folded calls (the failed and the stopped one keep their rows), the time the live turn's until /log sums it up: {titles:?}"));
            check(titles == ["Worked for 12s · 3 tools", "Ran npm test", "", "Stopped npm test"], &format!("the commentary stays folded; the reply and what failed or stopped show: {titles:?}"));
            check(live_row("live:cl:msg_01:1").is_none() && live_row("live:cl:msg_02:0").is_some(), "the final reply never folds");
            app.engine.borrow_mut().set_activity_mode(clarp_core::presentation::HIDE_WHEN_DONE);
            // Set on the engine directly: apply it now, no Host event will.
            crate::pump();
            true
        })),
        ("hidden when done", Box::new(move |app, _window, elapsed| {
            if live_row(FOLD).is_some() || elapsed < Duration::from_millis(300) {
                return false;
            }
            check(setting(app, "activity").is_some_and(|r| r.detail == "Hide when done"), "Settings say Hide when done");
            let titles = titles();
            check(titles == ["Ran npm test", "", "Stopped npm test"], &format!("the finished turn has no work row; its reply and what failed or stopped stay, its commentary went with the row: {titles:?}"));
            check(live_row("live:cl:msg_02:0").is_some(), "the reply stays");
            shot(&out3, "quiet-03-hidden-when-done");
            app.engine.borrow_mut().set_activity_mode(clarp_core::presentation::ALWAYS_VISIBLE);
            // Set on the engine directly: apply it now, no Host event will.
            crate::pump();
            true
        })),
        ("always visible", Box::new(move |_, _window, elapsed| {
            if live_row(FOLD).is_none() || elapsed < Duration::from_millis(300) {
                return false;
            }
            check(live_row("live:cl:msg_01:1").is_some(), "Always visible shows the commentary in its place");
            // A finished turn in Mike's /log: a spoken acknowledgment, a
            // tool, plain commentary, a tool, the reply (#653).
            app_now().engine.borrow_mut().set_activity_mode(clarp_core::presentation::GROUPED);
            // Set on the engine directly: apply it now, no Host event will.
            crate::pump();
            check(control("/__control/turn-summary", &json!({"on": true})).is_ok(), "the Host sums turns up in /log");
            check(control("/__control/upsert", &json!({"session": "mike", "turns": heard_rows()})).is_ok(), "Mike's turn lands");
            app_now().engine.borrow_mut().reconnect();
            true
        })),
        ("heard", Box::new(move |app, _window, elapsed| {
            if !app.engine.borrow().log_turn_summary() {
                return false;
            }
            if app.engine.borrow().selected_session() != "mike" {
                app.engine.borrow_mut().select("mike");
                return false;
            }
            let ids = shown_ids();
            if !ids.iter().any(|id| id == "h-final") || elapsed < Duration::from_millis(600) {
                return false;
            }
            let at = |id: &str| ids.iter().position(|i| i == id);
            let fold = live_row("live:fold:t-heard").unwrap_or_default();
            check(fold.live.title == "Worked for 6s · 2 tools", &format!("the work row counts the two folded calls: {:?}", fold.live.title));
            check(at("h-ack").is_some() && at("h-ack") < at("live:fold:t-heard") && at("live:fold:t-heard") < at("h-final"), &format!("the acknowledgment heard, then the work row, then the reply: {ids:?}"));
            check(at("h-note").is_none() && at("h-tool1").is_none() && at("h-tool2").is_none(), &format!("the plain commentary and the tools fold: {ids:?}"));
            app_now().focus_transcript();
            shot(&out4, "quiet-04-heard");
            app.engine.borrow_mut().set_activity_mode(clarp_core::presentation::HIDE_WHEN_DONE);
            // Set on the engine directly: apply it now, no Host event will.
            crate::pump();
            true
        })),
        ("heard, hidden when done", Box::new(move |_, _window, elapsed| {
            if live_row("live:fold:t-heard").is_some() || elapsed < Duration::from_millis(300) {
                return false;
            }
            let ids = shown_ids();
            let at = |id: &str| ids.iter().position(|i| i == id);
            check(at("h-ack").is_some() && at("h-ack") < at("h-final"), &format!("Hide when done keeps the acknowledgment and the reply: {ids:?}"));
            check(at("h-note").is_none(), "and leaves the plain commentary folded away");
            // The global choice back to Grouped; Mike's chat gets its own
            // Hide when done from its "⋯" menu (#653).
            let app = app_now();
            let window = crate::window().expect("window");
            app.engine.borrow_mut().set_activity_mode(clarp_core::presentation::GROUPED);
            // Set on the engine directly: apply it now, no Host event will.
            crate::pump();
            let pane = app.active_id().to_string();
            crate::chat_menu::open(&app, &window, &pane);
            let menu = window.global::<crate::ChatMenu>();
            check(menu.get_open() == pane.as_str(), "the chat's ⋯ menu opens on its pane");
            check(menu.get_activity_label() == "Follow Settings", &format!("its Tool activity submenu shows Follow Settings: {:?}", menu.get_activity_label()));
            let choices: Vec<String> = menu.get_activity_choices().iter().map(|c| c.label.to_string()).collect();
            check(choices == ["Follow Settings", "Grouped", "Always visible", "Group old", "Hide when done"], &format!("five choices: {choices:?}"));
            menu.set_submenu("activity".into());
            crate::chat_menu::pick(&app, &window, "activity", "3");
            true
        })),
        ("per chat", Box::new(move |app, window, elapsed| {
            if live_row("live:fold:t-heard").is_some() || elapsed < Duration::from_millis(300) {
                return false;
            }
            let engine = app.engine.borrow();
            check(engine.activity_mode() == clarp_core::presentation::GROUPED, "the global choice stays Grouped");
            check(engine.chat_activity_mode("mike") == clarp_core::presentation::HIDE_WHEN_DONE, "Mike's chat is Hide when done");
            check(engine.chat_activity_mode("rachel") == clarp_core::presentation::GROUPED, "Rachel's follows the global choice");
            let key = clarp_core::presentation::chat_key(engine.server_id(), "mike");
            check(!key.is_empty() && engine.settings().get(clarp_core::presentation::CHAT_ACTIVITY_KEY).and_then(|m| m.get(&key)) == Some(&json!(3)),
                &format!("kept on this device under the Host's server id and the session: {key:?}"));
            drop(engine);
            check(window.global::<crate::ChatMenu>().get_open().is_empty(), "the menu closed");
            let ids = shown_ids();
            check(ids.iter().any(|id| id == "h-ack") && ids.iter().any(|id| id == "h-final"), &format!("the work row is gone, the words heard and the reply stay: {ids:?}"));
            shot(&out5, "quiet-05-per-chat");
            let app = app_now();
            crate::chat_menu::open(&app, window, &app.active_id());
            check(window.global::<crate::ChatMenu>().get_activity_label() == "Hide when done", "the menu shows the chat's own choice");
            check(window.global::<crate::ChatMenu>().get_activity_choices().iter().any(|c| c.current && c.value == "3"), "marked in its submenu");
            crate::profile_view::open(&app, window, "mike");
            true
        })),
        ("per chat in the profile", Box::new(move |app, window, _| {
            let info = window.global::<crate::ProfileBridge>().get_info();
            if info.session != "mike" {
                return false;
            }
            check(info.tool_activity_index == 4, &format!("the profile shows Mike's Tool activity Hide when done: {}", info.tool_activity_index));
            crate::profile_view::close(app, window);
            crate::chat_menu::pick(&app_now(), window, "activity", "follow");
            true
        })),
        ("follow settings", Box::new(move |app, _window, elapsed| {
            if live_row("live:fold:t-heard").is_none() || elapsed < Duration::from_millis(300) {
                return false;
            }
            check(app.engine.borrow().chat_activity_choice("mike").is_none(), "Follow Settings clears Mike's own choice");
            check(app.engine.borrow().settings().get(clarp_core::presentation::CHAT_ACTIVITY_KEY).is_none(), "nothing left stored");
            // The menu's Artifacts: the agent's profile, at its Artifacts.
            let app = app_now();
            let window = crate::window().expect("window");
            crate::chat_menu::open(&app, &window, &app.active_id());
            let before = window.global::<crate::ProfileBridge>().get_artifacts_request();
            ARTIFACTS_BEFORE.with(|b| b.set(before));
            crate::chat_menu::artifacts(&app, &window);
            true
        })),
        ("artifacts", Box::new(move |_, window, elapsed| {
            let bridge = window.global::<crate::ProfileBridge>();
            if bridge.get_info().session != "mike" || elapsed < Duration::from_millis(300) {
                return false;
            }
            check(window.global::<crate::ChatMenu>().get_open().is_empty(), "the menu closed");
            check(bridge.get_artifacts_request() == ARTIFACTS_BEFORE.with(|b| b.get()) + 1, "the profile was asked to bring its Artifacts into view");
            let titles: Vec<String> = bridge.get_artifacts().iter().map(|a| a.title.to_string()).collect();
            check(titles.iter().any(|t| t == "Link"), &format!("Mike's artifacts, as the profile lists them: {titles:?}"));
            shot(&out6, "quiet-06-artifacts");
            crate::profile_view::close(&app_now(), window);
            true
        })),
    ];
    run_stages(stages);
}

/// Mike's finished turn: a spoken acknowledgment, a tool, plain
/// commentary, a tool, the reply with the Host's summary; then his next prompt.
fn heard_rows() -> serde_json::Value {
    let tool = |id: &str, at: &str, call: &str, command: &str| json!({"id": id, "role": "assistant", "timestamp": at, "text": "", "trace_id": "t-heard",
        "tools": [{"name": "Bash", "summary": command, "status": "ok", "id": call, "command": command, "result": "ok"}]});
    json!([
        {"id": "u-heard", "role": "user", "text": "Is the build green?", "origin": "user", "timestamp": "2026-12-01T10:00:00Z", "trace_id": "t-heard"},
        {"id": "h-ack", "role": "assistant", "text": "<speak>On it, checking the build.</speak>", "phase": "commentary", "timestamp": "2026-12-01T10:00:01Z", "trace_id": "t-heard"},
        tool("h-tool1", "2026-12-01T10:00:02Z", "call-h1", "make build"),
        {"id": "h-note", "role": "assistant", "text": "Looking at the build log now.", "phase": "commentary", "timestamp": "2026-12-01T10:00:03Z", "trace_id": "t-heard"},
        tool("h-tool2", "2026-12-01T10:00:04Z", "call-h2", "make test"),
        {"id": "h-final", "role": "assistant", "text": "All green.", "phase": "final", "timestamp": "2026-12-01T10:00:06Z", "trace_id": "t-heard",
         "turn": {"turn_id": "t-heard", "status": "completed", "started_at_ms": 1_000, "ended_at_ms": 7_000, "worked_ms": 6_000, "tool_count": 2}},
        {"id": "u-heard-next", "role": "user", "text": "Thanks.", "origin": "user", "timestamp": "2026-12-01T10:01:00Z", "trace_id": "t-heard-next"},
    ])
}

/// The rows of a run between agents, then the user's own turn after it.
fn coordination_rows() -> serde_json::Value {
    json!([
        {"id": "a2a-ask", "role": "user", "text": "Can you check the build for me?", "origin": "agent", "sender_name": "Arnold", "sender_agent_id": "agent-arnold", "audience": "agents", "trace_id": "t-a2a"},
        {"id": "a2a-reply", "role": "assistant", "text": "The build is green.", "audience": "agents", "trace_id": "t-a2a"},
        {"id": "own-ask", "role": "user", "text": "How did it go?", "origin": "user", "audience": "user", "trace_id": "t-own"},
        {"id": "own-reply", "role": "assistant", "text": "All done.", "audience": "user", "trace_id": "t-own"},
    ])
}

fn shown_ids() -> Vec<String> {
    crate::window().map(|w| rows(&w)).unwrap_or_default().iter().map(|r| r.id.to_string()).collect()
}

fn count_line() -> Option<crate::MessageRow> {
    live_rows().into_iter().find(|r| r.live.key.starts_with(crate::artifacts_view::COORDINATION_PREFIX))
}

/// `--check agent-messages`: coordination between agents (#653, feature
/// `hidden_coordination`). Under the Host's hide the run is left out and a
/// quiet "2 messages between agents, with Arnold" line stands where it was;
/// "Show a line where agent messages were" (on by default, listed next to
/// "Show messages between agents" whenever the Host offers the feature)
/// takes the line away;
/// opening the line reads the chat again with the run shown for the visit;
/// flipping the Host's setting writes it, and only then.
pub fn agent_messages_check(out: String) {
    let (out1, out2) = (out.clone(), out.clone());
    let stages: Vec<Stage> = vec![
        ("open", Box::new(|app, _window, _| {
            if !app.engine.borrow().conversation("rachel").is_some_and(|c| !c.rows().is_empty()) {
                return false;
            }
            check(!app.engine.borrow().hidden_coordination(), "without hidden_coordination the desktop reads /log as before");
            check(requests("GET", "/log").iter().all(|r| r["query"].get("coordination").is_none()), "and sends no coordination=");
            check(setting(app, "agent-messages").is_none() && setting(app, "agent-messages-line").is_none(), "and lists neither setting");
            check(control("/__control/upsert", &json!({"session": "rachel", "turns": coordination_rows()})).is_ok(), "a run between agents lands in the chat");
            check(control("/__control/coordination", &json!({"on": true})).is_ok(), "the Host turns hidden_coordination on");
            app.engine.borrow_mut().reconnect();
            true
        })),
        ("feature", Box::new(move |app, _window, _| {
            let ready = app.engine.borrow().hidden_coordination() && app.engine.borrow().coordination_visibility() == "hide";
            if !ready || count_line().is_none() {
                return false;
            }
            let list = crate::settings_view::rows(app);
            let at = |id: &str| list.iter().position(|r| r.id == id);
            check(list.iter().any(|r| r.id == "agent-messages" && r.label == "Show messages between agents" && !r.on), "Settings show 'Show messages between agents' off, the Host's value");
            check(list.iter().any(|r| r.id == "agent-messages-line" && r.label == "Show a line where agent messages were" && r.on), "'Show a line where agent messages were' is listed, on by default");
            check(at("agent-messages").is_some_and(|i| at("agent-messages-line") == Some(i + 1)), "directly under it");
            check(list.iter().any(|r| r.id == "agent-messages-line" && r.description == "Applies wherever messages between agents are hidden, in every chat."), "with its hint under it");
            check(posts("/coordination-settings").is_empty(), "the desktop never wrote the Host's value by itself");
            let line = count_line().unwrap_or_default();
            check(line.live.title == "2 messages between agents, with Arnold", &format!("the quiet line where they were: {:?}", line.live.title));
            let ids = shown_ids();
            check(!ids.iter().any(|id| id.starts_with("a2a-")), &format!("the run itself is left out: {ids:?}"));
            check(ids.iter().any(|id| id == "own-ask") && ids.iter().any(|id| id == "own-reply"), "the user's own turn shows");
            let at = |id: &str| ids.iter().position(|i| i == id);
            check(at(line.id.as_str()).zip(at("own-ask")).is_some_and(|(l, a)| l + 1 == a), "the line stands right above the row after the run");
            check(requests("GET", "/log").iter().any(|r| r["query"]["coordination"] == "auto"), "reads send coordination=auto");
            app_now().focus_transcript();
            shot(&out1, "agent-messages-01-line");
            crate::settings_view::change(&app_now(), &crate::window().expect("window"), "agent-messages-line", 1);
            true
        })),
        ("no line", Box::new(|app, _window, elapsed| {
            if count_line().is_some() || elapsed < Duration::from_millis(300) {
                return false;
            }
            check(setting(app, "agent-messages-line").is_some_and(|r| !r.on), "the line setting is off");
            let ids = shown_ids();
            check(!ids.iter().any(|id| id.starts_with("a2a-")) && ids.iter().any(|id| id == "own-ask"), &format!("no line at all, and the run stays left out: {ids:?}"));
            crate::settings_view::change(&app_now(), &crate::window().expect("window"), "agent-messages-line", 1);
            true
        })),
        ("line again", Box::new(|_, _window, _| {
            let Some(line) = count_line() else { return false };
            crate::artifacts_view::toggle_live(&app_now(), &line.live.key);
            true
        })),
        ("opened", Box::new(move |app, _window, elapsed| {
            let ids = shown_ids();
            if !ids.iter().any(|id| id == "a2a-ask") || elapsed < Duration::from_millis(300) {
                return false;
            }
            check(app.engine.borrow().coordination_visit("rachel"), "opening the line shows the run for this visit");
            check(requests("GET", "/log").iter().any(|r| r["query"]["coordination"] == "show"), "read with coordination=show");
            check(count_line().is_none(), "the line gives way to the run, in place");
            let at = |id: &str| ids.iter().position(|i| i == id);
            check(at("a2a-ask") < at("a2a-reply") && at("a2a-reply") < at("own-ask"), &format!("in its place: {ids:?}"));
            check(posts("/coordination-settings").is_empty(), "the Host's setting is unchanged");
            shot(&out2, "agent-messages-02-opened");
            crate::settings_view::change(&app_now(), &crate::window().expect("window"), "agent-messages", 1);
            true
        })),
        ("host setting", Box::new(|app, _window, _| {
            let written = posts("/coordination-settings");
            if written.is_empty() {
                return false;
            }
            check(written.iter().any(|p| p["body"]["coordination_visibility"] == "show"), &format!("flipping the switch writes the Host's value: {written:?}"));
            check(setting(app, "agent-messages").is_some_and(|r| r.on), "the switch shows it on");
            check(setting(app, "agent-messages-line").is_some_and(|r| r.on), "the line setting stays listed, on: a chat's own hide still draws the line");
            // Leaving the chat ends the visit that showed the run.
            app.engine.borrow_mut().select("mike");
            true
        })),
        ("back to rachel", Box::new(|app, _window, elapsed| {
            if elapsed < Duration::from_millis(300) {
                return false;
            }
            app.engine.borrow_mut().select("rachel");
            true
        })),
        ("chat menu", Box::new(|app, window, elapsed| {
            let ids = shown_ids();
            if app.engine.borrow().selected_session() != "rachel" || !ids.iter().any(|id| id == "a2a-ask") || elapsed < Duration::from_millis(300) {
                return false;
            }
            check(count_line().is_none(), "under the Host's show the run shows and no line is drawn");
            // Rachel's own hide, from the chat's ⋯ menu (#653).
            let app = app_now();
            crate::chat_menu::open(&app, window, &app.active_id());
            let menu = window.global::<crate::ChatMenu>();
            check(menu.get_coordination_shown(), "the menu offers Messages between agents");
            check(menu.get_coordination_label() == "Follow Settings", &format!("showing the chat's choice: {:?}", menu.get_coordination_label()));
            let choices: Vec<String> = menu.get_coordination_choices().iter().map(|c| c.label.to_string()).collect();
            check(choices == ["Follow Settings", "Show", "Hide"], &format!("three choices: {choices:?}"));
            menu.set_submenu("coordination".into());
            crate::chat_menu::pick(&app, window, "coordination", "hide");
            true
        })),
        ("chat hides", Box::new(|_, window, _| {
            let Some(line) = count_line() else { return false };
            let written = posts("/agent-coordination");
            check(written.iter().any(|p| p["body"]["session"] == "rachel" && p["body"]["visibility"] == "hide"), &format!("the chat's own hide goes to its Host: {written:?}"));
            check(line.live.title == "2 messages between agents, with Arnold", &format!("a chat's own hide under a Host set to show still draws the line: {:?}", line.live.title));
            check(!shown_ids().iter().any(|id| id.starts_with("a2a-")), "and leaves the run out");
            let app = app_now();
            crate::chat_menu::open(&app, window, &app.active_id());
            check(window.global::<crate::ChatMenu>().get_coordination_label() == "Hide", "the menu shows the chat's choice: Hide");
            crate::chat_menu::close(window);
            true
        })),
    ];
    run_stages(stages);
}
