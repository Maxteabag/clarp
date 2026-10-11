//! Codex provider profiles (the codex_provider_profiles feature,
//! docs/codex-eu-profile.md):
//! `--check eu-codex`, against the fake Host.

use std::time::Duration;

use serde_json::{Value, json};
use slint::{ComponentHandle, Model};

use super::{Stage, app_now, check, control, posts, report, run_stages, shot, view};
use crate::{LaunchHub, OverviewBridge, ProfileBridge, StartAgent, headless, launch_view};

const RULE: &str = "Use text input, not voice, for real cases with this agent.";
const MISSING: &str = "The Host has no key for this profile yet. Use text input, not voice, for real cases with this agent.";
const LOCKED: &str = "An agent's provider profile and backend are fixed when it is created; start a new agent instead.";
/// What a Host with the feature gives a provider-profile agent in
/// `interaction_capabilities` (keys as `clarp_core::protocol::capability`).
fn text_only() -> Value {
    use clarp_core::protocol::capability::{CAN_CLOUD_DICTATION, CAN_SPEAK, CAN_VOICE_TARGET};
    let mut caps = serde_json::Map::new();
    caps.insert("can_chat".into(), json!(true));
    for key in [CAN_VOICE_TARGET, CAN_SPEAK, CAN_CLOUD_DICTATION] {
        caps.insert(key.into(), json!(false));
    }
    Value::Object(caps)
}
const TYPE_INSTEAD: &str = "This agent only accepts dictation transcribed on this computer; type the message instead";

fn catalog(available: bool) -> Value {
    json!({"providers": {
        "claude": {"label": "Claude", "sort_index": 1, "models": [{"id": "opus", "label": "Opus"}]},
        "codex": {"label": "Codex", "sort_index": 2, "supported_efforts": ["medium"], "models": [{"id": "gpt-5", "label": "GPT-5"}],
                  "provider_profiles": [{
                      "id": "azure-eu", "label": "Codex (EU, Azure)",
                      "detail": if available { format!("Azure OpenAI in Sweden Central. {RULE}") } else { MISSING.to_owned() },
                      "operating_rule": RULE, "available": available, "default_model": "sol61-eu",
                      "models": [{"id": "sol61-eu", "label": "sol61-eu", "supported_efforts": ["low", "medium", "high", "xhigh"]}]}]},
    }})
}

fn last_create() -> Value {
    posts("/agents").pop().map(|r| r["body"].clone()).unwrap_or_default()
}

fn waited(elapsed: Duration, millis: u64) -> bool {
    elapsed >= Duration::from_millis(millis)
}

fn typing_ready() -> bool {
    if report().composer_focused {
        return true;
    }
    app_now().focus_composer();
    false
}

fn backend_ids(app: &crate::App) -> Vec<String> {
    app.engine.borrow().backend_options().iter().filter_map(|o| o["id"].as_str().map(str::to_owned)).collect()
}

fn chips(window: &crate::AppWindow) -> Vec<(String, bool)> {
    window.global::<LaunchHub>().get_providers().iter().map(|c| (c.label.to_string(), c.available)).collect()
}

/// `--check eu-codex --out DIR`: a Host that does not advertise the
/// codex_provider_profiles feature (here contract 63 without it) shows nothing
/// of provider profiles even when its catalogue lists one; with the feature
/// Ctrl+K "New EU Codex agent" opens the hub on "Codex (EU, Azure)" with its
/// operating rule and default model, and creating sends `backend: codex,
/// provider_profile` and that model; the agent carries the EU marker in the
/// explorer, the chat header, Ctrl+K, its profile and the overview; voice
/// follows `interaction_capabilities`, not the profile; a relaunch keeps the
/// profile and shows the Host's 409; a refused send (403
/// provider_profile_boundary) and a refused dictation (409, never retried)
/// read plainly; and a profile without its key is shown disabled with its
/// detail.
pub(super) fn eu_codex_check(out: String) {
    use slint::platform::Key;
    let o: Vec<String> = (0..9).map(|_| out.clone()).collect();
    let [o1, o2, o3, o4, o5, o6, o7, o8, o9]: [String; 9] = o.try_into().expect("nine");
    let stages: Vec<Stage> = vec![
        ("ready", Box::new(|app, _window, _| {
            let open = app.engine.borrow().conversation("rachel").is_some_and(|c| !c.rows().is_empty());
            if !open || !report().composer_focused {
                return false;
            }
            check(control("/__control/catalog", &catalog(true)).is_ok(), "the catalogue lists Codex (EU, Azure)");
            // Contract 63 without the feature: the number alone (perhaps
            // reused for something else) must switch nothing on.
            check(control("/__control/contract", &json!({"host": 63, "codex_provider_profiles": false})).is_ok(), "contract 63, without the feature");
            check(control("/__control/personas", &json!({"personas": [{"id": "p", "name": "Paula"}]})).is_ok(), "one idle contact");
            app.engine.borrow_mut().reconnect();
            crate::pump();
            true
        })),
        ("old host", Box::new(|app, _window, _| {
            // The built-in three until the catalogue arrives, then its two.
            if !app.engine.borrow().connected() || backend_ids(app).len() != 2 || !typing_ready() {
                return false;
            }
            check(backend_ids(app) == ["claude", "codex"], &format!("a Host without the feature offers no profile, whatever its contract: {:?}", backend_ids(app)));
            check(app.engine.borrow().provider_profiles().is_empty(), "and lists none");
            headless::press_with(&[Key::Control], "k");
            true
        })),
        ("old host palette", Box::new(|_, window, elapsed| {
            if !window.get_switcher_open() || !waited(elapsed, 200) {
                return false;
            }
            headless::type_text("eu codex");
            true
        })),
        ("old host searched", Box::new(move |_, window, elapsed| {
            if window.get_switcher_query() != "eu codex" || !waited(elapsed, 300) {
                return false;
            }
            let labels: Vec<String> = window.get_switcher_rows().iter().map(|r| r.label.to_string()).collect();
            check(!labels.iter().any(|l| l == "New EU Codex agent"), &format!("Ctrl+K has no EU command without the feature: {labels:?}"));
            shot(&o1, "eu-01-old-host");
            headless::press(Key::Escape);
            true
        })),
        ("feature on", Box::new(|app, window, _| {
            if window.get_switcher_open() {
                return false;
            }
            check(control("/__control/contract", &json!({"host": 63, "codex_provider_profiles": true})).is_ok(), "the Host advertises codex_provider_profiles");
            check(
                control("/__control/add-agent", &json!({"session": "eva", "persona": "Eva", "backend": "codex", "provider_profile": "azure-eu", "model": "sol61-eu",
                                                         "interaction_capabilities": text_only()})).is_ok(),
                "an agent runs on Codex (EU, Azure), text only",
            );
            check(
                control("/__control/add-agent", &json!({"session": "tess", "persona": "Tess", "backend": "claude", "interaction_capabilities": text_only()})).is_ok(),
                "an ordinary agent the Host says cannot speak",
            );
            app.engine.borrow_mut().reconnect();
            crate::pump();
            true
        })),
        ("profiles offered", Box::new(|app, _window, _| {
            let ready = app.engine.borrow().agent_in_eu_data_zone("eva") && backend_ids(app).len() == 3;
            if !app.engine.borrow().connected() || !ready || !typing_ready() {
                return false;
            }
            check(backend_ids(app) == ["claude", "codex", "codex@azure-eu"], &format!("the profile follows Codex: {:?}", backend_ids(app)));
            check(!app.engine.borrow().agent_in_eu_data_zone("rachel"), "an ordinary agent is not in the EU data zone");
            headless::press_with(&[Key::Control], "k");
            true
        })),
        ("palette", Box::new(|_, window, elapsed| {
            if !window.get_switcher_open() || !waited(elapsed, 200) {
                return false;
            }
            headless::type_text("eu codex");
            true
        })),
        ("palette searched", Box::new(|_, window, elapsed| {
            if window.get_switcher_query() != "eu codex" || !waited(elapsed, 300) {
                return false;
            }
            let first = window.get_switcher_rows().row_data(0).map(|r| r.label.to_string()).unwrap_or_default();
            check(first == "New EU Codex agent", &format!("Ctrl+K offers New EU Codex agent first: {first}"));
            headless::press(Key::Return);
            true
        })),
        ("hub on profile", Box::new(move |_, window, elapsed| {
            let hub = window.global::<LaunchHub>();
            if window.get_overlay() != "new-session" || !waited(elapsed, 300) {
                return false;
            }
            let chips = chips(window);
            check(
                chips == [("Claude".to_owned(), true), ("Codex".to_owned(), true), ("Codex (EU, Azure)".to_owned(), true)],
                &format!("Codex (EU, Azure) is its own choice next to Codex: {chips:?}"),
            );
            check(hub.get_provider_index() == 2, "the hub opens on it");
            check(hub.get_provider_note() == RULE && !hub.get_provider_note_warning(), &format!("its operating rule is the note: {}", hub.get_provider_note()));
            check(hub.get_model_summary() == "sol61-eu", &format!("on the profile's default model: {}", hub.get_model_summary()));
            hub.invoke_toggle_edit_model();
            true
        })),
        ("profile models", Box::new(move |_, window, elapsed| {
            let hub = window.global::<LaunchHub>();
            if !hub.get_editing_model() || !waited(elapsed, 200) {
                return false;
            }
            let models: Vec<String> = hub.get_model_labels().iter().map(|l| l.to_string()).collect();
            let efforts: Vec<String> = hub.get_effort_labels().iter().map(|l| l.to_string()).collect();
            check(models == ["Server default", "sol61-eu"], &format!("only the profile's models: {models:?}"));
            check(efforts == ["Server default", "Low", "Medium", "High", "Xhigh"], &format!("and its efforts: {efforts:?}"));
            shot(&o2, "eu-02-hub");
            hub.invoke_toggle_edit_model();
            hub.invoke_toggle_new_contact();
            true
        })),
        ("named", Box::new(|_, window, elapsed| {
            if !window.global::<LaunchHub>().get_new_contact() || !waited(elapsed, 200) {
                return false;
            }
            headless::type_text("Euna");
            true
        })),
        ("typed", Box::new(|_, window, _| {
            let hub = window.global::<LaunchHub>();
            if hub.get_name() != "Euna" || !hub.get_can_confirm() {
                return false;
            }
            headless::press(Key::Return);
            true
        })),
        ("created", Box::new(move |app, window, elapsed| {
            let selected = app.engine.borrow().selected_session().to_owned();
            if !window.get_overlay().is_empty() || selected != "euna-new" || !waited(elapsed, 300) {
                return false;
            }
            let body = last_create();
            check(
                body["backend"] == "codex" && body["provider_profile"] == "azure-eu" && body["model"] == "sol61-eu",
                &format!("the create asks for codex on azure-eu with the default model: {body}"),
            );
            check(app.engine.borrow().agent_in_eu_data_zone("euna-new"), "the new agent is in the EU data zone");
            check(view().eu, "the chat header carries the EU marker");
            shot(&o3, "eu-03-created");
            headless::press_with(&[Key::Control], "k");
            true
        })),
        ("palette agents", Box::new(move |_, window, elapsed| {
            if !window.get_switcher_open() || !waited(elapsed, 300) {
                return false;
            }
            let rows: Vec<(String, bool)> = window.get_switcher_rows().iter().filter(|r| r.kind == "agent").map(|r| (r.label.to_string(), r.eu)).collect();
            let eu_of = |name: &str| rows.iter().find(|(l, _)| l == name).map(|(_, eu)| *eu);
            check(eu_of("Euna") == Some(true) && eu_of("Eva") == Some(true), &format!("Ctrl+K marks the EU agents: {rows:?}"));
            check(eu_of("Rachel") == Some(false), "and only them");
            shot(&o4, "eu-04-palette");
            headless::press(Key::Escape);
            true
        })),
        ("profile", Box::new(|_, window, _| {
            if window.get_switcher_open() {
                return false;
            }
            crate::profile_view::open(&app_now(), window, "euna-new");
            true
        })),
        ("profile shown", Box::new(move |_, window, elapsed| {
            let bridge = window.global::<ProfileBridge>();
            if window.get_overlay() != "profile" || bridge.get_info().session != "euna-new" || !waited(elapsed, 300) {
                return false;
            }
            check(bridge.get_info().eu, "the profile carries the EU marker");
            let models: Vec<String> = bridge.get_models().iter().map(|m| m.to_string()).collect();
            check(models == ["Provider default", "sol61-eu"], &format!("a model change offers only the profile's models: {models:?}"));
            shot(&o5, "eu-05-profile");
            headless::press(Key::Escape);
            true
        })),
        ("overview", Box::new(|_, window, _| {
            if !window.get_overlay().is_empty() {
                return false;
            }
            crate::overview_view::open(&app_now(), window);
            true
        })),
        ("overview shown", Box::new(move |_, window, elapsed| {
            if window.get_overlay() != "overview" || !waited(elapsed, 400) {
                return false;
            }
            let cards: Vec<(String, bool)> = window.global::<OverviewBridge>().get_cards().iter().map(|c| (c.session.to_string(), c.eu)).collect();
            let eu_of = |session: &str| cards.iter().find(|(s, _)| s == session).map(|(_, eu)| *eu);
            check(eu_of("euna-new") == Some(true) && eu_of("rachel") == Some(false), &format!("the overview marks the EU agent: {cards:?}"));
            let speaks: Vec<(String, bool)> = window.global::<OverviewBridge>().get_cards().iter().map(|c| (c.session.to_string(), c.can_speak)).collect();
            let speaks_of = |session: &str| speaks.iter().find(|(s, _)| s == session).map(|(_, v)| *v);
            check(
                speaks_of("euna-new") == Some(false) && speaks_of("tess") == Some(false) && speaks_of("rachel") == Some(true),
                &format!("voice follows can_speak, not the profile (Tess has none and no voice): {speaks:?}"),
            );
            shot(&o6, "eu-06-overview");
            headless::press(Key::Escape);
            true
        })),
        ("relaunch", Box::new(|_, window, _| {
            if !window.get_overlay().is_empty() {
                return false;
            }
            let refused = json!({"respond": {"status": 409, "body": {"error": "provider_profile_locked", "message": LOCKED}}});
            check(control("/__control/create", &refused).is_ok(), "the Host refuses the next relaunch");
            launch_view::open_start_agent(&app_now(), window, "euna-new", "Euna");
            true
        })),
        ("relaunch dialog", Box::new(|_, window, elapsed| {
            let start = window.global::<StartAgent>();
            if window.get_overlay() != "start-agent" || !waited(elapsed, 300) {
                return false;
            }
            let backends: Vec<String> = start.get_backend_labels().iter().map(|l| l.to_string()).collect();
            let models: Vec<String> = start.get_model_labels().iter().map(|l| l.to_string()).collect();
            check(start.get_relaunch() && backends == ["Codex (EU, Azure)"], &format!("a relaunch keeps the profile: {backends:?}"));
            check(models == ["Provider default", "sol61-eu"] && start.get_model_index() == 1, &format!("with only its models, its own chosen: {models:?}"));
            check(start.get_backend_note() == RULE && start.get_can_start(), "its operating rule is the note");
            headless::press(Key::Return);
            true
        })),
        ("relaunch refused", Box::new(move |app, window, _| {
            let error = window.global::<StartAgent>().get_error().to_string();
            if error.is_empty() {
                return false;
            }
            let body = last_create();
            check(
                body["replace_sid"] == "euna-new" && body["backend"] == "codex" && body["provider_profile"] == "azure-eu",
                &format!("the relaunch asks for the same profile: {body}"),
            );
            check(error.contains(LOCKED), &format!("the Host's 409 reads plainly: {error}"));
            shot(&o7, "eu-07-relaunch-locked");
            app.engine.borrow_mut().clear_error();
            headless::press(Key::Escape);
            true
        })),
        ("send", Box::new(|_, window, _| {
            if !window.get_overlay().is_empty() || !typing_ready() {
                return false;
            }
            check(control("/__control/fail", &json!({"path": "/send", "status": 403, "count": 1, "text": "provider_profile_boundary"})).is_ok(), "the Host refuses the next send");
            headless::type_text("hello eu");
            headless::press(Key::Return);
            true
        })),
        ("send refused", Box::new(move |app, _window, _| {
            let error = app.engine.borrow().error().to_owned();
            if !error.starts_with("Message refused") {
                return false;
            }
            check(error.contains("EU data zone"), &format!("a refused send says why: {error}"));
            let send = posts("/send").pop().unwrap_or_default();
            check(
                send["body"]["session"] == "euna-new" && send["body"]["synthesize_audio"] == false && send["body"]["hands_free"] == false,
                &format!("a send to an agent that cannot speak and is no voice target asks for no voice: {}", send["body"]),
            );
            shot(&o8, "eu-08-send-refused");
            app.engine.borrow_mut().clear_error();
            let refused = json!({"path": "/transcribe", "status": 409, "count": 5, "body": {"error": TYPE_INSTEAD}});
            check(control("/__control/fail", &refused).is_ok(), "the Host refuses dictation to the EU agent");
            headless::press_with(&[Key::Control, Key::Shift], " ");
            true
        })),
        ("recording", Box::new(|_, _window, _| {
            let recording = crate::platform::audio::with(|audio| audio.recording()).unwrap_or(false);
            if !recording {
                return false;
            }
            true
        })),
        ("recorded", Box::new(|_, _window, elapsed| {
            if !waited(elapsed, 400) {
                return false;
            }
            headless::press_with(&[Key::Control, Key::Shift], " ");
            true
        })),
        ("dictation refused", Box::new(move |app, _window, elapsed| {
            let error = app.engine.borrow().error().to_owned();
            if error.is_empty() || !waited(elapsed, 1500) {
                return false;
            }
            check(error == TYPE_INSTEAD, &format!("a refused dictation shows the Host's words: {error}"));
            check(posts("/transcribe").len() == 1, &format!("and is not retried: {} requests", posts("/transcribe").len()));
            check(!posts("/send").iter().any(|s| s["body"]["text"] == "dictated words"), "nothing is sent");
            shot(&o9, "eu-09-dictation-refused");
            app.engine.borrow_mut().clear_error();
            check(control("/__control/catalog", &catalog(false)).is_ok(), "the profile loses its key");
            app.engine.borrow_mut().reconnect();
            crate::pump();
            true
        })),
        ("unavailable", Box::new(|app, _window, _| {
            let unavailable = app.engine.borrow().provider_profiles().first().is_some_and(|p| !p.available);
            if !app.engine.borrow().connected() || !unavailable || !typing_ready() {
                return false;
            }
            headless::press_with(&[Key::Control], "n");
            true
        })),
        ("hub", Box::new(|_, window, elapsed| {
            if window.get_overlay() != "new-session" || !waited(elapsed, 300) {
                return false;
            }
            // The hub remembers the last provider, the profile; a click on
            // its mark chooses it either way.
            window.global::<LaunchHub>().invoke_choose_provider(2);
            true
        })),
        ("disabled", Box::new({
            let out = out.clone();
            move |_, window, elapsed| {
                let hub = window.global::<LaunchHub>();
                if hub.get_provider_index() != 2 || !waited(elapsed, 300) {
                    return false;
                }
                let chip = hub.get_providers().row_data(2).expect("the profile's mark");
                check(!chip.available && chip.detail == MISSING, &format!("an unavailable profile is marked so, with its detail: {}", chip.detail));
                check(hub.get_provider_note() == MISSING && hub.get_provider_note_warning(), "the note says why");
                check(!hub.get_can_confirm(), "and it cannot start an agent");
                shot(&out, "eu-10-unavailable");
                headless::press(Key::Escape);
                true
            }
        })),
    ];
    run_stages(stages);
}
