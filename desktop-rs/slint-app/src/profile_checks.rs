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
    let (out1, out2, out3, out4, out5) = (out.clone(), out.clone(), out.clone(), out.clone(), out.clone());
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
            check(models == ["Harness default", "Opus", "Sonnet"] && info.model_index == 1, &format!("the catalog's models, Opus current: {models:?} {}", info.model_index));
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
            let terminal = recorded("CLARP_TEST_TERMINAL_LOG").last().cloned().unwrap_or_default();
            check(
                error.is_empty() && terminal.contains(r#""--agent-terminal","rachel","--host""#),
                &format!("the terminal opens the Host's terminal for the chat: {terminal} {error}"),
            );
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
            // A Host without prompt history (it answers 404), and a model
            // the catalog does not list (set with /agent-llm).
            let rachel = json!({"session": "rachel", "set": {"model": "opus-preview", "effort": ""}});
            let missing = json!({"path": "/identity/prompt-history", "status": 404});
            check(control("/__control/agent", &rachel).is_ok() && control("/__control/fail", &missing).is_ok(), "the Host drops prompt history and runs Rachel on an unlisted model");
            app_now().engine.borrow_mut().reconnect();
            true
        })),
        ("unlisted model", Box::new(|app, window, _| {
            let engine = app.engine.borrow();
            if !engine.roster().find("rachel").is_some_and(|a| a.model == "opus-preview") || engine.connection_state() != "live" {
                return false;
            }
            drop(engine);
            window.global::<ProfileBridge>().invoke_open_for_pane(app_now().active_id());
            true
        })),
        ("no prompt history", Box::new(move |_, window, elapsed| {
            let bridge = window.global::<ProfileBridge>();
            let info = bridge.get_info();
            if window.get_overlay() != "profile" || info.prompts_shown || info.prompts_loading || info.plan_loading || elapsed < Duration::from_millis(300) {
                return false;
            }
            check(info.error.is_empty(), &format!("a Host without prompt history shows no error: {:?}", info.error));
            check(texts(|t| t == "PROMPT HISTORY").is_empty(), "and no prompt history section");
            let models: Vec<String> = bridge.get_models().iter().map(|m| m.to_string()).collect();
            check(
                models.last().is_some_and(|m| m == "opus-preview") && info.model_index as usize == models.len() - 1,
                &format!("the model box shows the model the agent runs, not the harness default: {models:?} {}", info.model_index),
            );
            check(texts(|t| t == "THIS CHAT").len() == 1, &format!("one THIS CHAT section: {:?}", texts(|t| t == "THIS CHAT")));
            // Rachel's session is "rachel": it only repeats her name, and the
            // backend is under BACKEND, so the header says neither (VQ-18).
            check(
                texts(|t| t == "Session").is_empty() && texts(|t| t == "rachel" || t.contains("·  rachel")).is_empty(),
                &format!("no session row, and no subtitle repeating the name: {:?}", texts(|t| t.contains("rachel"))),
            );
            shot(&out4, "profile-04-no-prompt-history");
            let broken = json!({"path": "/task-plan", "status": 500});
            check(control("/__control/fail", &broken).is_ok(), "the Host's plan fails next");
            bridge.invoke_close();
            bridge.invoke_open_for_pane(app_now().active_id());
            true
        })),
        ("plan failed", Box::new(move |_, window, elapsed| {
            let info = window.global::<ProfileBridge>().get_info();
            if window.get_overlay() != "profile" || info.error.is_empty() || info.prompts_loading || elapsed < Duration::from_millis(300) {
                return false;
            }
            let said = "Could not load the plan: Gateway Timeout (the Host answered 500)";
            check(info.error.as_str() == said, &format!("a real failure in plain words: {}", info.error));
            check(info.prompts_shown, "prompt history is back once the Host answers it");
            let error = boxes(move |t| t == said);
            let heading = boxes(|t| t == "THIS CHAT");
            // In line with the cards' titles, inside the panel's padding.
            let inside = matches!((error.as_slice(), heading.as_slice()), ([e], [h]) if (e.0 - h.0).abs() < 1.0);
            check(inside, &format!("said once, inside the panel's padding: {error:?} {heading:?}"));
            shot(&out5, "profile-05-error");
            headless::press(Key::Escape);
            true
        })),
        ("closed again", Box::new(|_, window, _| {
            if !window.get_overlay().is_empty() {
                return false;
            }
            check(true, "Escape closes it");
            true
        })),
    ];
    run_stages(stages);
}

/// The drawn texts whose words match: their words and boxes (x, y,
/// width, height).
fn drawn_texts(matching: impl Fn(&str) -> bool + 'static) -> Vec<(String, (f32, f32, f32, f32))> {
    use i_slint_backend_testing::{AccessibleRole, ElementQuery};
    let Some(window) = crate::window() else { return Vec::new() };
    ElementQuery::from_root(&window)
        .match_predicate(move |e| {
            e.accessible_role() == Some(AccessibleRole::Text) && e.size().width > 0.0 && e.accessible_label().is_some_and(|l| matching(l.as_str()))
        })
        .find_all()
        .iter()
        .map(|e| {
            let (at, size) = (e.absolute_position(), e.size());
            (e.accessible_label().map(|l| l.to_string()).unwrap_or_default(), (at.x, at.y, size.width, size.height))
        })
        .collect()
}

fn texts(matching: impl Fn(&str) -> bool + 'static) -> Vec<String> {
    drawn_texts(matching).into_iter().map(|(words, _)| words).collect()
}

fn boxes(matching: impl Fn(&str) -> bool + 'static) -> Vec<(f32, f32, f32, f32)> {
    drawn_texts(matching).into_iter().map(|(_, at)| at).collect()
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
                reply.artifacts.row_count() == 1 && card.title == "Ship plan" && card.label == "PLAN" && card.progress == "1 / 3 completed" && card.outcome == "active",
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

/// What the profile's Backend row and its picker show now.
fn backend_now(window: &crate::AppWindow) -> (crate::ProfileInfo, String, Vec<crate::TransitChoice>) {
    let bridge = window.global::<ProfileBridge>();
    (bridge.get_info(), bridge.get_transit_step().to_string(), bridge.get_transit_choices().iter().collect())
}

/// `--check backend-row --out DIR` (feature agent_backend_transit, issue
/// 720): a Host without the feature shows no Backend row; with it the row
/// names the tool ("Claude", the Model line says the model), is held while messages wait, and opens the picker:
/// the Host's backends for this agent (unavailable ones dimmed with their
/// reason), the chosen one's models (the default marked), then Move. The
/// move's phases land on the moved row, the Host's one line is in the chat
/// beside the old rows (never cleared); a refused start and a failed move
/// read in plain words. Escape steps back one level.
pub(super) fn backend_row_check(out: String) {
    let (out1, out2, out3, out4, out5, out6) = (out.clone(), out.clone(), out.clone(), out.clone(), out.clone(), out.clone());
    let out7 = out.clone();
    let stages: Vec<Stage> = vec![
        ("ready", Box::new(|app, window, _| {
            if !ready(app) {
                return false;
            }
            let catalog = json!({"providers": {
                "claude": {"label": "Claude", "sort_index": 1, "available": true, "models": [
                    {"id": "opus", "label": "Opus", "default": true, "supported_efforts": ["low", "high"]},
                    {"id": "sonnet", "label": "Sonnet", "default": false, "supported_efforts": ["medium"]}]},
                "codex": {"label": "Codex", "sort_index": 2, "available": true, "models": [
                    {"id": "gpt-5.5", "label": "GPT-5.5", "default": true, "supported_efforts": ["low", "medium", "high"]},
                    {"id": "gpt-5.5-mini", "label": "GPT-5.5 mini", "default": false}]},
                "agy": {"label": "Antigravity", "sort_index": 3, "available": false, "unavailable_reason": "signed_out", "models": []},
                "muse": {"label": "Muse", "sort_index": 4, "available": false, "unavailable_reason": "not_supported", "models": []}}});
            let rachel = json!({"session": "rachel", "set": {"model": "opus"}});
            check(control("/__control/catalog", &catalog).is_ok() && control("/__control/agent", &rachel).is_ok(), "the Host takes a catalog with availability");
            window.global::<ProfileBridge>().invoke_open_for_pane(app_now().active_id());
            true
        })),
        ("no feature", Box::new(|app, window, elapsed| {
            if window.get_overlay() != "profile" || elapsed < Duration::from_millis(300) {
                return false;
            }
            check(!app.engine.borrow().transit_offered("rachel") && !backend_now(window).0.backend_shown, "a Host without agent_backend_transit shows no Backend row");
            headless::press(Key::Escape);
            check(control("/__control/transit", &json!({"on": true})).is_ok(), "the Host offers moving agents");
            // The feature is read from /server-info on connecting.
            app.engine.borrow_mut().reconnect();
            true
        })),
        ("feature", Box::new(|app, window, _| {
            let engine = app.engine.borrow();
            let ready = engine.transit_offered("rachel") && engine.model_catalog_loaded() && engine.connection_state() == "live"
                && engine.roster().find("rachel").is_some_and(|a| a.model == "opus");
            if !ready || !window.get_overlay().is_empty() {
                return false;
            }
            drop(engine);
            window.global::<ProfileBridge>().invoke_open_for_pane(app_now().active_id());
            true
        })),
        ("row", Box::new(move |_, window, elapsed| {
            let (info, step, _) = backend_now(window);
            if window.get_overlay() != "profile" || !info.backend_shown || elapsed < Duration::from_millis(300) {
                return false;
            }
            check(info.backend_value == "Claude" && info.backend_enabled && info.backend_detail.is_empty() && step.is_empty(),
                &format!("the Backend row names the tool alone: {:?} {}", info.backend_value, info.backend_enabled));
            // Issue 887: the model, the heartbeat and the context each say
            // what they know once.
            let bridge = window.global::<ProfileBridge>();
            let shown_model = bridge.get_models().row_data(info.model_index as usize).map(|m| m.to_string()).unwrap_or_default();
            check(shown_model == "Opus" && texts(|t| t.contains(" · Opus")).is_empty(), &format!("the model is said once, on the Model line: {shown_model:?}"));
            check(info.context_used.is_empty(), &format!("unknown context usage gives no Used words: {:?}", info.context_used));
            shot(&out1, "backend-01-row");
            true
        })),
        // The sections below the fold are drawn only once scrolled to.
        ("heartbeat once", Box::new(|_, _, _| {
            if texts(|t| t == "Push alerts").is_empty() {
                headless::wheel(640.0, 400.0, -120.0);
                return false;
            }
            let heartbeat = texts(|t| t.to_lowercase().contains("heartbeat"));
            check(heartbeat == ["Heartbeat"] && texts(|t| t == "State").is_empty(), &format!("the heartbeat is said once, by its switch: {heartbeat:?}"));
            true
        })),
        ("context once", Box::new(move |_, _, _| {
            if texts(|t| t == "CONTEXT").is_empty() || texts(|t| t == "Compact context").is_empty() {
                headless::wheel(640.0, 400.0, -120.0);
                return false;
            }
            check(texts(|t| t == "Used" || t == "Unknown").is_empty(), "unknown context usage shows no Used row, only Compact context");
            shot(&out7, "backend-01b-said-once");
            headless::wheel(640.0, 400.0, 20000.0);
            check(control("/__control/agent", &json!({"session": "rachel", "set": {"queued_turn_count": 1}})).is_ok(), "a message waits for Rachel");
            true
        })),
        ("held", Box::new(|_, window, _| {
            let (info, _, _) = backend_now(window);
            if info.backend_enabled {
                return false;
            }
            check(info.backend_detail == clarp_core::transit::TURN_BLOCKED, &format!("while messages wait it cannot move: {:?}", info.backend_detail));
            window.global::<ProfileBridge>().invoke_open_transit();
            check(backend_now(window).1.is_empty(), "and the picker does not open");
            check(control("/__control/agent", &json!({"session": "rachel", "set": {"queued_turn_count": 0}})).is_ok(), "the message has gone");
            true
        })),
        ("free", Box::new(|_, window, _| {
            if !backend_now(window).0.backend_enabled {
                return false;
            }
            window.global::<ProfileBridge>().invoke_open_transit();
            true
        })),
        ("backends", Box::new(move |_, window, elapsed| {
            let bridge = window.global::<ProfileBridge>();
            let (_, step, choices) = backend_now(window);
            if step != "backends" || bridge.get_transit_loading() || choices.len() != 4 || elapsed < Duration::from_millis(300) {
                return false;
            }
            let asked = requests("GET", "/agent-model-options").last().map(|r| r["query"]["session"].clone());
            check(asked == Some(json!("rachel")), &format!("the picker asks the Host for this agent's options: {asked:?}"));
            let labels: Vec<String> = choices.iter().map(|c| c.label.to_string()).collect();
            check(labels == ["Claude", "Codex", "Antigravity", "Muse"], &format!("in the Host's order: {labels:?}"));
            check(choices[0].current && choices[1].enabled && !choices[1].current, "Claude is current, Codex can be chosen");
            check(!choices[2].enabled && choices[2].detail == "Not signed in", &format!("a signed-out tool is dimmed with its reason: {:?}", choices[2].detail));
            check(!choices[3].enabled && choices[3].detail == "Can't carry a conversation there yet", &format!("so is one that cannot take a conversation: {:?}", choices[3].detail));
            shot(&out2, "backend-02-picker");
            bridge.invoke_pick_transit("agy".into());
            check(backend_now(window).1 == "backends", "a dimmed backend cannot be chosen");
            bridge.invoke_pick_transit("codex".into());
            true
        })),
        ("models", Box::new(move |_, window, elapsed| {
            let bridge = window.global::<ProfileBridge>();
            let (_, step, choices) = backend_now(window);
            if step != "models" || elapsed < Duration::from_millis(200) {
                return false;
            }
            check(bridge.get_transit_title() == "Codex · Model", &format!("the model step names the backend: {}", bridge.get_transit_title()));
            let labels: Vec<(String, String)> = choices.iter().map(|c| (c.label.to_string(), c.detail.to_string())).collect();
            check(labels == [("GPT-5.5".into(), "Default".into()), ("GPT-5.5 mini".into(), String::new())], &format!("its models, the default marked: {labels:?}"));
            shot(&out3, "backend-03-models");
            bridge.invoke_pick_transit("gpt-5.5".into());
            true
        })),
        ("confirm", Box::new(move |_, window, elapsed| {
            let bridge = window.global::<ProfileBridge>();
            if backend_now(window).1 != "confirm" || elapsed < Duration::from_millis(200) {
                return false;
            }
            check(bridge.get_transit_title() == "Move Rachel to Codex · GPT-5.5?", &format!("Move asks first: {}", bridge.get_transit_title()));
            check(bridge.get_transit_body().contains("takes the conversation's messages along"), "and says what comes along");
            shot(&out4, "backend-04-confirm");
            headless::press(Key::Escape);
            true
        })),
        ("escape steps back", Box::new(|_, window, _| {
            if backend_now(window).1 != "models" {
                return false;
            }
            check(window.get_overlay() == "profile", "Escape steps back to the models, the profile stays");
            // The keyboard alone from here: Up/Down move, Enter picks, Enter moves.
            let bridge = window.global::<ProfileBridge>();
            check(bridge.get_transit_cursor() == 0, &format!("the keyboard starts on the first model: {}", bridge.get_transit_cursor()));
            headless::press(Key::DownArrow);
            check(bridge.get_transit_cursor() == 1, &format!("Down moves to the next model: {}", bridge.get_transit_cursor()));
            headless::press(Key::DownArrow);
            check(bridge.get_transit_cursor() == 1, "and stays on the last one");
            headless::press(Key::UpArrow);
            check(bridge.get_transit_cursor() == 0, "Up moves back");
            headless::press(Key::Return);
            true
        })),
        ("enter moves", Box::new(|_, window, _| {
            let bridge = window.global::<ProfileBridge>();
            if backend_now(window).1 != "confirm" {
                return false;
            }
            check(bridge.get_transit_title() == "Move Rachel to Codex · GPT-5.5?", &format!("Enter picked the highlighted model: {}", bridge.get_transit_title()));
            headless::press(Key::Return);
            true
        })),
        ("moving header", Box::new(|_, window, _| {
            // The open chat's header says the move too, as on iOS (#723).
            let moving = window.get_panes().iter().find(|p| p.session == "rachel").map(|p| p.moving.to_string()).unwrap_or_default();
            if moving.is_empty() {
                return false;
            }
            check(moving.starts_with("Moving to Codex"), &format!("the chat header says the move: {moving:?}"));
            true
        })),
        ("moved", Box::new(move |app, window, elapsed| {
            let (info, step, _) = backend_now(window);
            let engine = app.engine.borrow();
            let moved = engine.roster().find("rachel").is_some_and(|a| a.backend == "codex" && a.model == "gpt-5.5");
            let line = engine.conversation("rachel").is_some_and(|c| c.rows().iter().any(|r| r.text.starts_with("Now on Codex") && r.role == "system"));
            if !moved || !line || info.backend_moving || elapsed < Duration::from_millis(300) {
                return false;
            }
            let drawn = rows(window).into_iter().find(|r| r.id.starts_with("transit-")).map(|r| r.author.to_string());
            check(drawn.as_deref() == Some("system"), &format!("the Host's result line is drawn as a quiet system line, not as a reply: {drawn:?}"));
            let body = posts("/agent-transit").last().map(|p| p["body"].clone()).unwrap_or(Value::Null);
            check(body["session"] == "rachel" && body["backend"] == "codex" && body["model"] == "gpt-5.5" && body["request_id"].as_str().is_some_and(|r| !r.is_empty()) && body.get("effort").is_none(),
                &format!("Move starts it with a request id: {body}"));
            check(step.is_empty() && info.backend_value == "Codex" && info.backend_detail.is_empty(), &format!("the row lands on the moved agent: {:?} {:?}", info.backend_value, info.backend_detail));
            let header = window.get_panes().iter().find(|p| p.session == "rachel").map(|p| p.moving.to_string()).unwrap_or_default();
            check(header.is_empty(), &format!("and the header's moving line clears: {header:?}"));
            let rows: Vec<&str> = engine.conversation("rachel").map(|c| c.rows().iter().map(|r| r.text.as_str()).collect()).unwrap_or_default();
            check(rows.contains(&"Hello") && rows.contains(&"Now on Codex · GPT-5.5. The conversation came along."), &format!("the chat keeps its rows and gains the Host's one line: {rows:?}"));
            check(rows.iter().filter(|r| r.starts_with("Now on")).count() == 1, "the app adds no line of its own");
            drop(engine);
            shot(&out5, "backend-05-moved");
            let refusal = json!({"respond": {"status": 409, "body": {"error": "messages_waiting"}}});
            check(control("/__control/transit", &refusal).is_ok(), "the Host will refuse the next start");
            let bridge = window.global::<ProfileBridge>();
            bridge.invoke_open_transit();
            true
        })),
        ("open again", Box::new(|_, window, _| {
            let bridge = window.global::<ProfileBridge>();
            let (_, step, choices) = backend_now(window);
            if step != "backends" || bridge.get_transit_loading() || choices.len() != 4 {
                return false;
            }
            check(choices[1].current && !choices[0].current, "Codex is current now");
            bridge.invoke_pick_transit("claude".into());
            bridge.invoke_pick_transit("sonnet".into());
            bridge.invoke_confirm_transit();
            true
        })),
        ("refused", Box::new(move |_, window, elapsed| {
            let (info, step, _) = backend_now(window);
            if !info.backend_failed || elapsed < Duration::from_millis(200) {
                return false;
            }
            check(step.is_empty() && info.backend_detail == "Messages are waiting for this agent. Try again once they have gone." && info.backend_value == "Codex",
                &format!("a refused start says why in plain words, nothing changed: {:?}", info.backend_detail));
            shot(&out6, "backend-06-refused");
            let fail = json!({"fail": "the conversation couldn't be imported into Claude"});
            check(control("/__control/transit", &fail).is_ok(), "the next move will fail");
            let bridge = window.global::<ProfileBridge>();
            bridge.invoke_open_transit();
            true
        })),
        ("retry", Box::new(|_, window, _| {
            let bridge = window.global::<ProfileBridge>();
            let (info, step, choices) = backend_now(window);
            if step != "backends" || bridge.get_transit_loading() || choices.len() != 4 {
                return false;
            }
            check(!info.backend_failed, "opening the picker again clears the old refusal");
            bridge.invoke_pick_transit("claude".into());
            bridge.invoke_pick_transit("sonnet".into());
            bridge.invoke_confirm_transit();
            true
        })),
        ("failed", Box::new(|app, window, _| {
            let (info, _, _) = backend_now(window);
            let line = app.engine.borrow().conversation("rachel").is_some_and(|c| c.rows().iter().any(|r| r.text.starts_with("Couldn't move to Claude")));
            if !info.backend_failed || !line {
                return false;
            }
            check(info.backend_detail == "the conversation couldn't be imported into Claude" && info.backend_value == "Codex",
                &format!("a failed move says why and the agent stays: {:?} {:?}", info.backend_detail, info.backend_value));
            headless::press(Key::Escape);
            true
        })),
        ("closed", Box::new(|_, window, _| {
            if !window.get_overlay().is_empty() {
                return false;
            }
            check(true, "Escape closes the profile");
            true
        })),
    ];
    run_stages(stages);
}
