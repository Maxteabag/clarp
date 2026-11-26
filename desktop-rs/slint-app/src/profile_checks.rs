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
            check(texts(|t| t == "Session").is_empty() && texts(|t| t == "claude  ·  rachel").len() == 1, "the session shows once, in the subtitle");
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
            check(info.error == said, &format!("a real failure in plain words: {}", info.error));
            check(info.prompts_shown, "prompt history is back once the Host answers it");
            let error = boxes(|t| t == said);
            let heading = boxes(|t| t == "THIS CHAT");
            // The card's title sits 10 px in from the content's edge.
            let inside = matches!((error.as_slice(), heading.as_slice()), ([e], [h]) if (e.0 - (h.0 - 10.0)).abs() < 2.0);
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
