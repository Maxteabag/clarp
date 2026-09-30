//! The profile and overview group's checks (a child of `driver`, sharing
//! its stages and helpers): `--check profile`, `--check overview` and
//! `--check extras` (the transcript's display cells, artifact cards and
//! typing indicator). Each needs the fake Host.

use std::time::Duration;

use serde_json::{Value, json};
use slint::platform::Key;
use slint::{ComponentHandle, Model};

use super::{Stage, app_now, check, control, headless, posts, report, rows, run_stages, shot, view};
use crate::{OrchestratorBridge, OverviewBridge, ProfileBridge, VoiceBridge};

/// The scratch folder `check.sh` runs in (it exists, so it is a real
/// working directory for "Open files").
fn scratch() -> String {
    std::env::var("CLARP_TEST_HOST_LOG")
        .ok()
        .and_then(|log| std::path::Path::new(&log).parent().map(|p| p.to_string_lossy().into_owned()))
        .unwrap_or_default()
}

/// Lines recorded in the file an environment variable names.
fn recorded(variable: &str) -> Vec<String> {
    std::env::var_os(variable).and_then(|path| std::fs::read_to_string(path).ok()).unwrap_or_default().lines().map(str::to_owned).collect()
}

/// The fake Host's request log entries for `method` on `path`.
fn requests(method: &str, path: &str) -> Vec<Value> {
    let Some(log) = std::env::var_os("CLARP_TEST_HOST_LOG") else { return Vec::new() };
    std::fs::read_to_string(log)
        .unwrap_or_default()
        .lines()
        .filter_map(|line| serde_json::from_str::<Value>(line).ok())
        .filter(|entry| entry["method"] == method && entry["path"] == path)
        .collect()
}

fn last_body(path: &str) -> Value {
    posts(path).last().map(|entry| entry["body"].clone()).unwrap_or(Value::Null)
}

fn ready(app: &crate::App) -> bool {
    app.engine.borrow().conversation("rachel").is_some_and(|c| !c.rows().is_empty()) && report().composer_focused
}

/// `--check profile --out DIR`: the pane header opens Rachel's profile;
/// heartbeat, model, older prompts, files, terminal, voices (preview and
/// choice), the image viewer and compacting reach the Host; Escape closes
/// the innermost layer first, and a click outside closes the profile.
pub(super) fn profile_check(out: String) {
    let (out1, out2, out3) = (out.clone(), out.clone(), out.clone());
    let folder = scratch();
    let folder2 = folder.clone();
    let folder3 = folder.clone();
    let stages: Vec<Stage> = vec![
        ("ready", Box::new(move |app, _window, _| {
            if !ready(app) {
                return false;
            }
            let catalog = json!({"providers": {"claude": {"label": "Claude", "models": [
                {"id": "opus", "label": "Opus", "supported_efforts": ["low", "high"]},
                {"id": "sonnet", "label": "Sonnet", "supported_efforts": ["medium"]}]}}});
            let rachel = json!({"session": "rachel", "set": {"cwd": folder, "model": "opus", "effort": "high",
                "context_tokens": 50000, "context_window": 200000,
                "schedules": [{"schedule_id": "s1", "name": "Nightly tidy", "enabled": true}]}});
            check(control("/__control/catalog", &catalog).is_ok() && control("/__control/agent", &rachel).is_ok(), "the Host takes a model catalog and Rachel's folder");
            // The catalog loads on connecting.
            app.engine.borrow_mut().reconnect();
            true
        })),
        ("catalog", Box::new(move |app, window, _| {
            let engine = app.engine.borrow();
            let moved = engine.roster().find("rachel").is_some_and(|a| a.working_directory == folder2 && a.model == "opus");
            if !engine.model_catalog_loaded() || !moved || engine.connection_state() != "live" {
                return false;
            }
            drop(engine);
            // The agent's name in the pane header opens the profile.
            window.global::<ProfileBridge>().invoke_open_for_pane(app_now().active_id());
            true
        })),
        ("open", Box::new(move |app, window, elapsed| {
            let bridge = window.global::<ProfileBridge>();
            let info = bridge.get_info();
            let media_ready = bridge.get_media().row_count() == 2 && bridge.get_media().row_data(0).is_some_and(|m| !m.loading);
            if window.get_overlay() != "profile" || info.plan_loading || info.prompts_loading || !media_ready || info.portrait.size().width == 0 || elapsed < Duration::from_millis(300) {
                return false;
            }
            check(info.name == "Rachel" && info.session == "rachel" && info.backend == "claude", &format!("the profile is Rachel's: {} {}", info.name, info.backend));
            check(info.folder.as_str() == folder3 && info.context_used == "50000 / 200000 tokens", &format!("with its folder and context: {} {}", info.folder, info.context_used));
            check(info.plan_title == "Ship it" && bridge.get_plan().row_count() == 1, "and the current plan");
            check(bridge.get_prompts().row_count() == 20 && info.prompts_more, "the first 20 prompts, with more to load");
            let models: Vec<String> = bridge.get_models().iter().map(|m| m.to_string()).collect();
            check(models == ["Provider default", "Opus", "Sonnet"] && info.model_index == 1, &format!("the catalog's models, Opus current: {models:?} {}", info.model_index));
            let efforts: Vec<String> = bridge.get_efforts().iter().map(|m| m.to_string()).collect();
            check(efforts.len() == 3 && info.effort_index == 2, &format!("Opus's efforts, High current: {efforts:?} {}", info.effort_index));
            check(bridge.get_schedules().row_data(0).is_some_and(|s| s.name == "Nightly tidy" && s.on), "its scheduled task");
            check(info.mcp_shown && bridge.get_mcp().row_data(0).is_some_and(|m| m.name == "github" && !m.on), "the Host's MCP servers");
            check(bridge.get_artifacts().row_count() == 2, "its artifacts");
            check(bridge.get_media().row_data(0).is_some_and(|m| m.image.size().width == 8), "its images, fetched and shown");
            check(info.portrait.size().width > 0, "and its portrait");
            check(app.engine.borrow().selected_session() == "rachel", "the profile's chat is selected");
            shot(&out1, "profile-01");
            bridge.invoke_set_heartbeat(false);
            true
        })),
        ("heartbeat", Box::new(|_, window, _| {
            if posts("/agent-heartbeat").is_empty() {
                return false;
            }
            check(last_body("/agent-heartbeat") == json!({"session": "rachel", "heartbeat_enabled": false}), "the heartbeat switch reaches the Host");
            window.global::<ProfileBridge>().invoke_set_model(2);
            true
        })),
        ("model", Box::new(|_, window, _| {
            if posts("/agent-llm").is_empty() {
                return false;
            }
            // Sonnet has no "high": the effort falls back to the model's default.
            check(last_body("/agent-llm") == json!({"session": "rachel", "model": "sonnet", "effort": ""}), &format!("choosing a model sends it: {}", last_body("/agent-llm")));
            window.global::<ProfileBridge>().invoke_load_older();
            true
        })),
        ("older prompts", Box::new(|_, window, _| {
            let bridge = window.global::<ProfileBridge>();
            if bridge.get_prompts().row_count() != 30 || bridge.get_info().prompts_loading {
                return false;
            }
            check(!bridge.get_info().prompts_more, "Load older brings the last 10 prompts");
            bridge.invoke_open_files();
            bridge.invoke_open_terminal();
            true
        })),
        ("files", Box::new(|app, window, _| {
            let urls = recorded("CLARP_TEST_OPEN_URL");
            if urls.is_empty() {
                return false;
            }
            check(urls.last().is_some_and(|u| u.starts_with("file:///")), &format!("Open files opens the agent's folder: {urls:?}"));
            let error = app.engine.borrow().error().to_owned();
            check(error.contains("share the local filesystem") && recorded("CLARP_TEST_TERMINAL_LOG").is_empty(), &format!("the terminal needs a shared filesystem: {error}"));
            app.engine.borrow_mut().clear_error();
            window.global::<ProfileBridge>().invoke_voice();
            true
        })),
        ("voices", Box::new(move |_, window, elapsed| {
            let bridge = window.global::<VoiceBridge>();
            if window.get_overlay() != "voices" || bridge.get_loading() || bridge.get_voices().row_count() != 2 || elapsed < Duration::from_millis(200) {
                return false;
            }
            let statuses: Vec<String> = bridge.get_voices().iter().map(|v| v.status.to_string()).collect();
            check(statuses == ["Used by Mike", "Available"], &format!("the voices show who uses them: {statuses:?}"));
            check(bridge.get_bio() == "Warm and clear" && bridge.get_agent_name() == "Rachel", "with the agent's voice bio");
            shot(&out2, "profile-02-voices");
            bridge.invoke_preview("v2".into());
            bridge.invoke_choose("v2".into());
            true
        })),
        ("voice chosen", Box::new(|_, _window, _| {
            if posts("/preview").is_empty() || posts("/agent-voice").is_empty() {
                return false;
            }
            check(last_body("/preview") == json!({"voice_id": "v2", "session": "rachel", "text": "Hi, I'm Rachel."}), "Preview asks the Host to speak");
            check(last_body("/agent-voice") == json!({"session": "rachel", "voice_id": "v2"}), "Use chooses the voice");
            headless::press(Key::Escape);
            true
        })),
        ("back to the profile", Box::new(|_, window, _| {
            if window.get_overlay() != "profile" {
                return false;
            }
            check(true, "Escape closes the voices, back to the profile");
            // Choosing an image in the gallery opens the viewer.
            window.global::<ProfileBridge>().set_viewing(0);
            true
        })),
        ("viewer", Box::new(move |_, _window, elapsed| {
            if elapsed < Duration::from_millis(300) {
                return false;
            }
            shot(&out3, "profile-03-media");
            headless::press(Key::Escape);
            true
        })),
        ("viewer closed", Box::new(|_, window, _| {
            let bridge = window.global::<ProfileBridge>();
            if bridge.get_viewing() != -1 {
                return false;
            }
            check(window.get_overlay() == "profile", "Escape closes the image viewer first");
            bridge.set_confirm("compact".into());
            bridge.invoke_compact();
            true
        })),
        ("compacted", Box::new(|_, window, _| {
            if posts("/compact").is_empty() {
                return false;
            }
            check(last_body("/compact") == json!({"session": "rachel"}) && window.global::<ProfileBridge>().get_confirm().is_empty(), "Compact asks, then compacts");
            headless::press(Key::Escape);
            true
        })),
        ("closed", Box::new(|_, window, _| {
            if !window.get_overlay().is_empty() || !report().composer_focused {
                return false;
            }
            check(true, "Escape closes the profile, back to the composer");
            crate::commands::run(&app_now(), window, "agent-profile");
            true
        })),
        ("reopened", Box::new(|_, window, elapsed| {
            if window.get_overlay() != "profile" || elapsed < Duration::from_millis(200) {
                return false;
            }
            headless::click(100.0, 400.0);
            true
        })),
        ("clicked outside", Box::new(|_, window, _| {
            if !window.get_overlay().is_empty() {
                return false;
            }
            check(true, "a click outside closes the profile");
            true
        })),
    ];
    run_stages(stages);
}

/// `--check overview --out DIR`: Ctrl+Shift+O (outside the composer)
/// opens the overview; an agent's menu toggles its heartbeat, a schedule
/// switches, release asks then releases; the orchestrator opens over it,
/// loads, saves and returns to it; Escape and a click outside close it.
pub(super) fn overview_check(out: String) {
    let (out1, out2) = (out.clone(), out.clone());
    let stages: Vec<Stage> = vec![
        ("ready", Box::new(|app, _window, _| {
            if !ready(app) {
                return false;
            }
            let mike = json!({"session": "mike", "set": {"latest_state": "thinking", "context_tokens": 40000, "context_window": 200000,
                "schedules": [{"schedule_id": "s1", "name": "Nightly", "cron_expression": "0 3 * * *", "prompt": "Tidy up", "enabled": true}]}});
            check(control("/__control/agent", &mike).is_ok(), "the Host has Mike working, with a schedule");
            headless::press(Key::Escape);
            true
        })),
        ("keyboard in the transcript", Box::new(|app, _window, _| {
            let working = app.engine.borrow().roster().find("mike").is_some_and(|a| a.busy);
            if !report().transcript_focused || !working {
                return false;
            }
            headless::press_with(&[Key::Control, Key::Shift], "O");
            true
        })),
        ("open", Box::new(move |_, window, elapsed| {
            let bridge = window.global::<OverviewBridge>();
            // The fake Host has Rachel's portrait only; Mike keeps his initial.
            let portraits = bridge.get_cards().row_data(0).is_some_and(|c| c.portrait.size().width > 0);
            if window.get_overlay() != "overview" || !portraits || elapsed < Duration::from_millis(300) {
                return false;
            }
            let cards: Vec<crate::OverviewCard> = bridge.get_cards().iter().collect();
            check(cards.iter().map(|c| c.name.to_string()).collect::<Vec<_>>() == ["Rachel", "Mike"], "Ctrl+Shift+O shows every agent");
            let mike = &cards[1];
            check(mike.busy && mike.state == "thinking" && (mike.context - 0.2).abs() < 0.001, "a working agent with its context");
            check(mike.schedules.row_data(0).is_some_and(|s| s.title == "Nightly  ·  0 3 * * *" && s.on), "and its scheduled task");
            check(cards[0].selected, "the open chat's card is marked");
            shot(&out1, "overview-01");
            // "···" opens Rachel's menu; its heartbeat entry turns it on.
            bridge.set_menu("rachel".into());
            bridge.invoke_heartbeat("rachel".into());
            bridge.invoke_schedule("s1".into(), false);
            true
        })),
        ("acted", Box::new(|_, window, _| {
            if posts("/agent-heartbeat").is_empty() || posts("/agent-schedules/toggle").is_empty() {
                return false;
            }
            check(last_body("/agent-heartbeat") == json!({"session": "rachel", "heartbeat_enabled": true}), "the menu's heartbeat entry reaches the Host");
            check(last_body("/agent-schedules/toggle") == json!({"schedule_id": "s1", "enabled": false}), "a schedule switches off");
            let bridge = window.global::<OverviewBridge>();
            check(bridge.get_menu().is_empty(), "and the menu closes");
            bridge.invoke_release("mike".into());
            check(bridge.get_confirm_release() == "mike" && requests("DELETE", "/agents/mike").is_empty(), "the first Release… only asks");
            bridge.invoke_release("mike".into());
            true
        })),
        ("released", Box::new(|_, window, _| {
            let bridge = window.global::<OverviewBridge>();
            if requests("DELETE", "/agents/mike").is_empty() || bridge.get_cards().row_count() != 1 {
                return false;
            }
            check(true, "Confirm release releases the agent and its card goes");
            bridge.invoke_orchestrator();
            true
        })),
        ("orchestrator", Box::new(move |_, window, elapsed| {
            let bridge = window.global::<OrchestratorBridge>();
            if window.get_overlay() != "orchestrator" || !bridge.get_loaded() || elapsed < Duration::from_millis(200) {
                return false;
            }
            check(!bridge.get_enabled() && bridge.get_last_decision() == "route: rachel (0.87)", "the orchestrator loads the Host's settings and last decision");
            check(bridge.get_provider_index() == 0 && bridge.get_timeout_ms() == 30000 && bridge.get_fallback_only(), "with its defaults");
            bridge.set_enabled(true);
            bridge.set_provider_index(1);
            bridge.set_model("gpt-x".into());
            bridge.set_timeout_ms(1000);
            shot(&out2, "overview-02-orchestrator");
            bridge.invoke_save();
            true
        })),
        ("saved", Box::new(|_, window, _| {
            if posts("/orchestrator/settings").is_empty() {
                return false;
            }
            let body = last_body("/orchestrator/settings");
            check(
                body["enabled"] == true && body["provider"] == "claude" && body["model"] == "gpt-x" && body["timeout_ms"] == 1000 && body["effort"] == "",
                &format!("Save sends the settings: {body}"),
            );
            check(window.get_overlay() == "overview", "and returns to the overview");
            headless::press(Key::Escape);
            true
        })),
        ("closed", Box::new(|_, window, _| {
            if !window.get_overlay().is_empty() {
                return false;
            }
            check(true, "Escape closes the overview");
            // The rail's overview button.
            window.invoke_surface_chosen("overview".into());
            true
        })),
        ("from the rail", Box::new(|_, window, elapsed| {
            if window.get_overlay() != "overview" || elapsed < Duration::from_millis(200) {
                return false;
            }
            check(true, "the rail's button opens the overview");
            headless::click(5.0, 400.0);
            true
        })),
        ("clicked outside", Box::new(|_, window, _| {
            if !window.get_overlay().is_empty() {
                return false;
            }
            check(true, "a click outside closes it");
            // The settings' Orchestrator row runs the same action.
            crate::commands::run(&app_now(), window, "orchestrator");
            true
        })),
        ("orchestrator alone", Box::new(|_, window, elapsed| {
            if window.get_overlay() != "orchestrator" || elapsed < Duration::from_millis(200) {
                return false;
            }
            headless::click(100.0, 400.0);
            true
        })),
        ("orchestrator closed", Box::new(|_, window, _| {
            if !window.get_overlay().is_empty() {
                return false;
            }
            check(true, "the orchestrator on its own closes on a click outside");
            true
        })),
    ];
    run_stages(stages);
}

/// `--check extras --out DIR`: a reply's display cells fold with its tool
/// calls and open with their detail lines, an artifact made during it shows
/// as a card under it, and with "Show when ready" a working agent shows the
/// typing indicator.
pub(super) fn extras_check(out: String) {
    let (out1, out2) = (out.clone(), out.clone());
    let turns = json!({"session": "rachel", "turns": [
        {"id": "x1", "role": "user", "text": "Run the tests", "timestamp": "2026-09-29T09:58:00Z"},
        {"id": "x2", "role": "assistant", "text": "All green.", "timestamp": "2026-09-29T10:00:00Z", "display_cells": [
            {"title": "Bash", "summary": "cargo test", "status": "ok", "detail_count": 3,
             "lines": [{"kind": "diff_new", "text": "+ 42 passed"}, {"kind": "error", "label": "stderr", "text": "1 warning"}]},
            {"title": "Read", "summary": "src/main.rs", "status": "running"}],
         "tools": [{"name": "Bash", "command": "cargo test"}, {"name": "Edit", "file_path": "src/lib.rs"}]},
    ]});
    // 09:59 UTC: made while the reply was being written.
    let artifacts = json!({"session": "rachel", "artifacts": [
        {"artifact_id": "plan1", "type": "plan", "title": "Ship plan", "summary": "Three steps to release", "status": "active",
         "session": "rachel", "plan": {"total_count": 3, "completed_count": 1}, "created_at": 1790675940000_i64},
        {"artifact_id": "old", "type": "document", "title": "Undated", "session": "rachel"}]});
    let stages: Vec<Stage> = vec![
        ("live", Box::new(move |app, _, _| {
            if !ready(app) {
                return false;
            }
            check(control("/__control/turns", &turns).is_ok() && control("/__control/artifacts", &artifacts).is_ok(), "the Host takes a reply with display cells and an artifact");
            true
        })),
        ("folded", Box::new(|_, window, _| {
            let rows = rows(window);
            let Some(reply) = rows.iter().find(|r| r.id == "x2") else { return false };
            if reply.artifacts.row_count() == 0 {
                return false;
            }
            check(reply.activity_label == "2 tool calls" && !reply.expanded, &format!("display cells fold behind the reply's toggle: {:?}", reply.activity_label));
            let card = reply.artifacts.row_data(0).expect("artifact");
            check(
                reply.artifacts.row_count() == 1 && card.title == "Ship plan" && card.kind == "PLAN" && card.progress == "1 / 3 completed" && card.outcome == "active",
                "the artifact made during the reply shows as a card under it (the undated one does not)",
            );
            window.invoke_toggle_activity(app_now().active_id(), "x2".into(), "".into());
            true
        })),
        ("open", Box::new(move |_, window, elapsed| {
            let rows = rows(window);
            let Some(reply) = rows.iter().find(|r| r.id == "x2") else { return false };
            if !reply.expanded || elapsed < Duration::from_millis(300) {
                return false;
            }
            let cells: Vec<crate::DisplayCell> = reply.cells.iter().collect();
            check(cells.len() == 2 && cells[0].title == "Bash" && cells[0].summary == "cargo test" && cells[0].status == "ok", "opening shows the display cells");
            check(cells[0].lines.row_count() == 2 && cells[0].more == 1, "with their detail lines and a count of the rest");
            check(cells[0].lines.row_data(1).is_some_and(|l| l.text == "stderr  1 warning" && l.kind == "error"), "a labelled line keeps its label");
            let tools: Vec<String> = reply.tools.iter().map(|t| t.name.to_string()).collect();
            check(tools == ["Edit"], &format!("cells stand in for the tool calls except edits: {tools:?}"));
            shot(&out1, "extras-01-cells");
            check(crate::commands::run(&app_now(), window, "setting:showWhenReady"), "Show when ready turns on");
            let thinking = json!({"session": "rachel", "set": {"latest_state": "thinking"}});
            check(control("/__control/agent", &thinking).is_ok(), "the Host has Rachel working");
            true
        })),
        ("typing", Box::new(move |_, _window, elapsed| {
            if !view().working || elapsed < Duration::from_millis(300) {
                return false;
            }
            check(true, "a working agent shows Working… while its reply waits");
            shot(&out2, "extras-02-typing");
            let idle = json!({"session": "rachel", "set": {"latest_state": "idle"}});
            check(control("/__control/agent", &idle).is_ok(), "the Host has Rachel done");
            true
        })),
        ("done", Box::new(|_, _window, _| {
            if view().working {
                return false;
            }
            check(true, "and it goes when the agent is done");
            true
        })),
    ];
    run_stages(stages);
}
