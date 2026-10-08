//! `--check processes`: an agent's background work by keyboard alone.
//! Ctrl+Shift+P from the composer lists Rachel's two jobs and her helper
//! Mike; j/k and the arrows move; Enter shows a job's output, whose tail
//! reads again while it shows and scrolls back with k and End; x asks
//! before stopping (n keeps it running, y stops it, naming the run to the
//! Host); Escape goes back and then closes, the keyboard back where it was
//! (all with vim mode off, as the checks start).
//! Ctrl+K "jobs" opens it too, and there x stops the helper (`/stop`). In
//! vim mode Space j opens it from the chat, Shift+P from an explorer row,
//! `:jobs` from the command line. On Updates Ctrl+Shift+P lists every job,
//! and a job's Output button opens its output straight away.

use std::time::Duration;

use serde_json::json;
use slint::Model;
use slint::platform::Key;

use super::{Stage, check, control, posts, report, requests, run_stages, shot};
use crate::headless;

fn lines(window: &crate::AppWindow) -> Vec<String> {
    window.get_process_output_lines().iter().map(|l| l.to_string()).collect()
}

fn last_line(window: &crate::AppWindow) -> String {
    lines(window).last().cloned().unwrap_or_default()
}

fn titles(window: &crate::AppWindow) -> Vec<String> {
    window.get_process_jobs().iter().map(|j| j.title.to_string()).collect()
}

fn which(window: &crate::AppWindow) -> Vec<String> {
    window.get_vim_which().iter().map(|h| format!("{} {}", h.keys, h.label)).collect()
}

fn open(window: &crate::AppWindow) -> bool {
    window.get_overlay() == "processes"
}

pub fn processes_check(out: String) {
    let (out1, out2, out3, out4, out5) = (out.clone(), out.clone(), out.clone(), out.clone(), out);
    let stages: Vec<Stage> = vec![
        ("ready", Box::new(|app, _, _| {
            let loaded = app.engine.borrow().conversation("rachel").is_some_and(|c| !c.rows().is_empty());
            if !loaded || !report().composer_focused {
                return false;
            }
            // Rachel runs Mike as a helper and two processes; the older one
            // has a log thirty lines long.
            let now = chrono::Utc::now().timestamp_millis();
            let helper = json!({"session": "mike", "set": {"role": "helper", "parent_agent_id": "a1", "helper_state": "running", "status_text": "Reviewing the diff"}});
            let job = |id: &str, title: &str, started: i64, generation: i64| json!({"job_id": id, "agent_id": "a1", "session": "rachel", "status": "running",
                "kind": "watch", "title": title, "started_at": now - started, "heartbeat_at": now - 3_000, "updated_at": now, "generation": generation});
            let (w1, w2) = (job("w1", "Watch the build", 300_000, 2), job("w2", "Tail the logs", 60_000, 1));
            let jobs = json!({"jobs": [w1.clone(), w2], "event": {"type": "background-job-updated", "job": w1}});
            let log: String = (1..=30).map(|i| format!("build step {i} ok\n")).collect();
            let sent = control("/__control/agent", &helper)
                .and_then(|()| control("/__control/jobs", &jobs))
                .and_then(|()| control("/__control/job-log", &json!({"job_id": "w1", "append": log})));
            check(sent.is_ok(), &format!("Rachel starts a helper and two processes: {sent:?}"));
            true
        })),
        ("running", Box::new(|app, window, elapsed| {
            let processes = app.engine.borrow().agent_processes("rachel").unwrap_or_default();
            let count = |key: &str| processes.get(key).and_then(|v| v.as_array()).map_or(0, Vec::len);
            if count("jobs") != 2 || count("helpers") != 1 || elapsed < Duration::from_millis(300) {
                return false;
            }
            crate::commands::show_hints(app, window);
            let hints: Vec<String> = window.get_hints().iter().map(|h| format!("{} {}", h.keys, h.label)).collect();
            check(hints.iter().any(|h| h == "Ctrl+Shift+P Processes"), &format!("while she runs something the bar names the key: {hints:?}"));
            headless::press_with(&[Key::Control, Key::Shift], "P");
            true
        })),
        ("opened by key", Box::new(move |_, window, elapsed| {
            if !open(window) || elapsed < Duration::from_millis(300) {
                return false;
            }
            check(window.get_process_name() == "Rachel" && window.get_process_centered(), "Ctrl+Shift+P in the composer opens Rachel's processes");
            check(titles(window) == ["Watch the build", "Tail the logs"], &format!("her jobs, oldest first: {:?}", titles(window)));
            let helpers: Vec<String> = window.get_process_helpers().iter().map(|h| h.name.to_string()).collect();
            check(helpers == ["Mike"], &format!("and her running helper: {helpers:?}"));
            check(window.get_process_cursor() == 0 && window.get_keyboard_mode() == "PROCESSES", &format!("the keyboard is on the first row ({})", window.get_keyboard_mode()));
            check(window.get_process_hint() == "j/k move · Enter open · x stop · Esc close", &format!("the panel says its keys: {:?}", window.get_process_hint()));
            shot(&out1, "processes-01-list");
            headless::press("j");
            true
        })),
        ("j", Box::new(|_, window, _| {
            if window.get_process_cursor() != 1 {
                return false;
            }
            headless::press(Key::DownArrow);
            true
        })),
        ("down", Box::new(|_, window, _| {
            if window.get_process_cursor() != 2 {
                return false;
            }
            check(true, "j and Down move down to the helper");
            headless::press("k");
            headless::press(Key::UpArrow);
            true
        })),
        ("up", Box::new(|_, window, _| {
            if window.get_process_cursor() != 0 {
                return false;
            }
            check(true, "k and Up move back up");
            headless::press(Key::Return);
            true
        })),
        ("output", Box::new(move |_, window, elapsed| {
            if window.get_process_mode() != "output" || last_line(window) != "build step 30 ok" || elapsed < Duration::from_millis(300) {
                return false;
            }
            check(window.get_process_output_title() == "Watch the build", "Enter opens the job's output");
            let status = window.get_process_output_status().to_string();
            check(status.starts_with("running 5m") && status.contains("♥"), &format!("with how long it has run and its heartbeat: {status}"));
            check(lines(window).len() == 18, &format!("the log's newest lines: {}", lines(window).len()));
            check(!requests("GET", "/background-jobs/w1").is_empty(), "read from the Host's job detail");
            check(window.get_process_hint().contains("x stop") && window.get_process_hint().contains("Esc back"), &format!("{:?}", window.get_process_hint()));
            shot(&out2, "processes-02-output");
            let sent = control("/__control/job-log", &json!({"job_id": "w1", "append": "build step 31 ok\n"}));
            check(sent.is_ok(), "the job writes another line");
            true
        })),
        ("refreshed", Box::new(|_, window, _| {
            if last_line(window) != "build step 31 ok" {
                return false;
            }
            check(true, "the output reads the log again while it shows");
            headless::press("k");
            true
        })),
        ("scrolled", Box::new(|_, window, _| {
            if window.get_process_output_back() != 1 {
                return false;
            }
            check(last_line(window) == "build step 30 ok", "k scrolls the log back a line");
            headless::press(Key::End);
            true
        })),
        ("latest", Box::new(|_, window, _| {
            if window.get_process_output_back() != 0 {
                return false;
            }
            check(last_line(window) == "build step 31 ok", "End follows the newest line again");
            headless::press("x");
            true
        })),
        ("confirm", Box::new(move |_, window, elapsed| {
            if window.get_process_confirm().is_empty() || elapsed < Duration::from_millis(200) {
                return false;
            }
            check(window.get_process_confirm().contains("Stop Watch the build?"), &format!("x asks first: {:?}", window.get_process_confirm()));
            check(window.get_process_hint() == "y/Enter stop · n/Esc keep running", &format!("{:?}", window.get_process_hint()));
            shot(&out3, "processes-03-confirm");
            headless::press("n");
            true
        })),
        ("kept", Box::new(|_, window, _| {
            if !window.get_process_confirm().is_empty() {
                return false;
            }
            check(requests("DELETE", "/background-jobs/w1").is_empty(), "n keeps it running: nothing reaches the Host");
            headless::press("x");
            headless::press("y");
            true
        })),
        ("stopped", Box::new(|_, window, _| {
            let deletes = requests("DELETE", "/background-jobs/w1");
            if deletes.is_empty() || window.get_process_output_outcome() != "Stopped" || !window.get_process_output_status().starts_with("cancelled") {
                return false;
            }
            check(deletes.len() == 1 && deletes[0]["body"] == json!({"expected_generation": 2}), &format!("y stops this run of the job, by its generation: {deletes:?}"));
            check(true, &format!("it says so: {}", window.get_process_output_status()));
            headless::press(Key::Escape);
            true
        })),
        ("back", Box::new(|_, window, _| {
            if window.get_process_mode() != "list" {
                return false;
            }
            check(open(window), "Escape on the output goes back to the list");
            headless::press(Key::Escape);
            true
        })),
        ("closed", Box::new(|_, window, elapsed| {
            if open(window) || !report().composer_focused || elapsed < Duration::from_millis(200) {
                return false;
            }
            check(true, "Escape on the list closes it; the composer has the keyboard back");
            headless::press_with(&[Key::Control], "k");
            true
        })),
        ("switcher", Box::new(|_, window, elapsed| {
            if !window.get_switcher_open() || elapsed < Duration::from_millis(200) {
                return false;
            }
            headless::type_text("jobs");
            true
        })),
        ("found", Box::new(move |_, window, elapsed| {
            if window.get_switcher_query() != "jobs" || elapsed < Duration::from_millis(300) {
                return false;
            }
            let first = window.get_switcher_rows().row_data(0).map(|r| (r.label.to_string(), r.key.to_string())).unwrap_or_default();
            check(first == ("Background processes".to_owned(), "Ctrl+Shift+P".to_owned()), &format!("Ctrl+K finds it by \"jobs\", with its key: {first:?}"));
            shot(&out4, "processes-04-ctrl-k");
            headless::press(Key::Return);
            true
        })),
        ("via ctrl k", Box::new(|_, window, elapsed| {
            if !open(window) || window.get_switcher_open() || elapsed < Duration::from_millis(200) {
                return false;
            }
            check(window.get_process_name() == "Rachel", "Enter opens the selected agent's processes");
            check(titles(window) == ["Tail the logs"], &format!("the stopped job is gone: {:?}", titles(window)));
            headless::press(Key::End);
            headless::press(Key::Delete);
            true
        })),
        ("helper confirm", Box::new(|_, window, _| {
            if !window.get_process_confirm().contains("Stop Mike?") {
                return false;
            }
            check(true, "Delete on the helper asks first too");
            headless::press(Key::Return);
            true
        })),
        ("helper stopped", Box::new(|_, window, _| {
            if !posts("/stop").iter().any(|p| p["body"]["session"] == "mike") {
                return false;
            }
            check(window.get_process_note() == "Asked Mike to stop.", &format!("Enter stops the helper's turn: {:?}", window.get_process_note()));
            headless::press(Key::Escape);
            true
        })),
        ("composer again", Box::new(|app, window, elapsed| {
            if open(window) || !report().composer_focused || elapsed < Duration::from_millis(300) {
                return false;
            }
            // Vim mode (the checks start with it off): Escape leaves the
            // composer for Normal mode.
            app.engine.borrow_mut().settings_mut().set(crate::vim_view::SETTING, true);
            crate::commands::show_hints(app, window);
            headless::press(Key::Escape);
            true
        })),
        ("normal", Box::new(|_, window, elapsed| {
            if report().composer_focused || elapsed < Duration::from_millis(400) {
                return false;
            }
            check(window.get_vim_mode() == "NORMAL", &format!("Escape leaves the composer for Normal mode: {} {}", window.get_vim_mode(), window.get_keyboard_mode()));
            headless::press(Key::Space);
            true
        })),
        ("leader", Box::new(move |_, window, elapsed| {
            if which(window).is_empty() || elapsed < Duration::from_millis(200) {
                return false;
            }
            check(which(window).iter().any(|w| w == "j Jobs and helpers"), &format!("the leader's which-key lists j: {:?}", which(window)));
            shot(&out5, "processes-05-leader");
            headless::press("j");
            true
        })),
        ("via leader", Box::new(|_, window, _| {
            if !open(window) {
                return false;
            }
            check(window.get_process_name() == "Rachel", "Space j opens them from the chat");
            headless::press(Key::Escape);
            true
        })),
        ("chat back", Box::new(|_, window, elapsed| {
            if open(window) || elapsed < Duration::from_millis(200) {
                return false;
            }
            check(window.get_keyboard_mode() == "CHAT" && !report().composer_focused, &format!("Escape gives the keyboard back to the chat: {}", window.get_keyboard_mode()));
            headless::press("h");
            true
        })),
        ("explorer", Box::new(|_, window, _| {
            if !window.get_sidebar_focused() || window.get_sidebar_cursor() != "rachel" {
                return false;
            }
            headless::press_with(&[Key::Shift], "P");
            true
        })),
        ("via explorer", Box::new(|_, window, _| {
            if !open(window) {
                return false;
            }
            check(window.get_process_name() == "Rachel", "Shift+P on Rachel's explorer row opens hers");
            headless::press(Key::Escape);
            true
        })),
        ("explorer back", Box::new(|_, window, elapsed| {
            if open(window) || elapsed < Duration::from_millis(200) {
                return false;
            }
            check(window.get_sidebar_focused(), "Escape gives the keyboard back to the explorer");
            headless::press_with(&[Key::Shift], ":");
            headless::type_text("jobs");
            headless::press(Key::Return);
            true
        })),
        ("via command line", Box::new(|_, window, _| {
            if !open(window) {
                return false;
            }
            check(window.get_process_name() == "Rachel", ":jobs opens them");
            headless::press("q");
            true
        })),
        ("q", Box::new(|_, window, elapsed| {
            if open(window) || elapsed < Duration::from_millis(200) {
                return false;
            }
            check(true, "q closes it");
            headless::press_with(&[Key::Control], "2");
            true
        })),
        ("updates", Box::new(|_, window, elapsed| {
            if window.get_surface() != "updates" || !window.get_updates_focused() || elapsed < Duration::from_millis(300) {
                return false;
            }
            let hints: Vec<String> = window.get_hints().iter().map(|h| format!("{} {}", h.keys, h.label)).collect();
            check(hints.iter().any(|h| h == "Ctrl+Shift+P Jobs"), &format!("the Updates' bar names the key: {hints:?}"));
            headless::press_with(&[Key::Control, Key::Shift], "P");
            true
        })),
        ("every job", Box::new(|_, window, elapsed| {
            if !open(window) || window.get_process_jobs().row_count() < 2 || elapsed < Duration::from_millis(300) {
                return false;
            }
            check(window.get_process_name() == "Background jobs", "on Updates it lists every job");
            let details: Vec<String> = window.get_process_jobs().iter().map(|j| j.detail.to_string()).collect();
            check(details.iter().any(|d| d.starts_with("Rachel") && d.contains("cancelled")), &format!("with whose it is and how it ended: {details:?}"));
            headless::press(Key::Return);
            true
        })),
        ("job output", Box::new(|_, window, _| {
            if window.get_process_mode() != "output" || lines(window).is_empty() {
                return false;
            }
            check(window.get_process_output_title() == "Watch the build", "Enter shows a finished job's output too");
            headless::press(Key::Escape);
            headless::press(Key::Escape);
            true
        })),
        ("updates back", Box::new(|_, window, elapsed| {
            if open(window) || elapsed < Duration::from_millis(200) {
                return false;
            }
            check(window.get_updates_focused(), "Escape twice: back to the list, then closed, the Updates keeping the keyboard");
            window.invoke_job_output("w2".into());
            true
        })),
        ("output button", Box::new(|_, window, _| {
            let note = window.get_process_output_note();
            if window.get_process_mode() != "output" || note.is_empty() || note == "Reading its output…" {
                return false;
            }
            check(window.get_process_output_title() == "Tail the logs", "a job's Output button opens its output");
            check(window.get_process_output_note() == "This process didn't register a log.", &format!("and says why it has none: {:?}", window.get_process_output_note()));
            headless::press(Key::Escape);
            true
        })),
        ("output closed", Box::new(|_, window, elapsed| {
            if open(window) || elapsed < Duration::from_millis(200) {
                return false;
            }
            check(window.get_updates_focused(), "opened there, Escape closes it");
            true
        })),
    ];
    run_stages(stages);
}
