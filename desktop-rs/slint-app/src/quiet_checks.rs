//! `--check quiet-chat`: the quiet chat (#653). Under Tool activity
//! Grouped (the default) a running turn reads as one work row,
//! `Working · 3 tools`, that shimmers and opens in place; settled it reads
//! the Host's /log summary (none here yet: `5 tools`, no worked time from
//! /live). Hide when done leaves the finished turn's work row out and keeps
//! its messages and what failed or stopped. Driven by the fake Host
//! replaying the recorded `turn-full` stream, as `--check live` is.

use std::time::Duration;

use serde_json::json;

use super::{Stage, app_now, check, control, rows, run_stages, shot};

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
