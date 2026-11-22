//! `--check quiet-chat`: the quiet chat (#653). Under Tool activity
//! Grouped (the default) a running turn reads as one work row,
//! `Working · 3 tools`, that shimmers and opens in place; settled it reads
//! the Host's /log summary (none here yet: `5 tools`, no worked time from
//! /live). Hide when done leaves the finished turn's work row out and keeps
//! its messages and what failed or stopped. Driven by the fake Host
//! replaying the recorded `turn-full` stream, as `--check live` is.

use std::time::Duration;

use serde_json::json;

use super::{Stage, app_now, check, control, posts, requests, rows, run_stages, shot};

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
    let (out1, out2, out3) = (out.clone(), out.clone(), out.clone());
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
            if !(app.engine.borrow().live_items() && lseq() == Some(0)) {
                return false;
            }
            replay(AFTER_TAIL)
        })),
        ("working", Box::new(move |app, _window, elapsed| {
            if lseq() != Some(16) || elapsed < Duration::from_millis(600) {
                return false;
            }
            let fold = live_row(FOLD).unwrap_or_default();
            check(fold.live.title == "Working · 3 tools", &format!("the running turn is one work row with its tools so far: {:?}", titles()));
            check(fold.live.shimmer == "accent", &format!("it shimmers while the turn runs: {:?}", fold.live.shimmer));
            check(!titles().iter().any(|t| t.starts_with("Running") || t.starts_with("Thought") || t.starts_with("Explored")), &format!("its tools and thinking fold into it: {:?}", titles()));
            check(live_row("live:cl:msg_01:1").is_some(), "the commentary stays in view");
            app_now().focus_transcript();
            shot(&out1, "quiet-01-working");
            crate::artifacts_view::toggle_live(app, FOLD);
            true
        })),
        ("opened", Box::new(move |app, _window, _| {
            if !live_row(FOLD).is_some_and(|r| r.live.expanded) {
                return false;
            }
            let titles = titles();
            check(titles.iter().any(|t| t == "Running npm test") && titles.iter().any(|t| t == "Explored 1 file, 1 search"), &format!("opened in place: every tool row: {titles:?}"));
            shot(&out2, "quiet-02-opened");
            crate::artifacts_view::toggle_live(app, FOLD);
            replay(AFTER_INTERRUPT)
        })),
        ("settled", Box::new(move |app, _window, elapsed| {
            if lseq() != Some(23) || elapsed < Duration::from_millis(600) {
                return false;
            }
            let titles = titles();
            check(titles.first().map(String::as_str) == Some("5 tools"), &format!("settled without a /log summary yet: '5 tools', no time from /live: {titles:?}"));
            app.engine.borrow_mut().set_activity_mode(clarp_core::presentation::HIDE_WHEN_DONE);
            true
        })),
        ("hidden when done", Box::new(move |app, _window, elapsed| {
            if live_row(FOLD).is_some() || elapsed < Duration::from_millis(300) {
                return false;
            }
            check(setting(app, "activity").is_some_and(|r| r.detail == "Hide when done"), "Settings say Hide when done");
            let titles = titles();
            check(titles == ["", "Ran npm test", "", "Stopped npm test"], &format!("the finished turn has no work row; its messages and what failed or stopped stay: {titles:?}"));
            check(live_row("live:cl:msg_02:0").is_some(), "the reply stays");
            shot(&out3, "quiet-03-hidden-when-done");
            true
        })),
    ];
    run_stages(stages);
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
/// "Show a line where agent messages were" (on by default, listed under
/// "Show messages between agents" while that is off) takes the line away;
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
            check(setting(app, "agent-messages-line").is_none(), "the line setting is listed only while it is off");
            true
        })),
    ];
    run_stages(stages);
}
