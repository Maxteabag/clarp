//! The agent profile (AgentProfilePanel.qml with its MediaGallery), and the
//! layering of this group's dialogs: the profile and the overview open the
//! voice and orchestrator dialogs over themselves, and closing one returns
//! to the dialog below it.

use std::cell::RefCell;
use std::collections::HashMap;
use std::rc::Rc;

use clarp_engine::Change;
use serde_json::Value;
use slint::{ComponentHandle, Model, ModelRc, SharedString, VecModel};

use clarp_core::transit::{self, Step};

use crate::{
    App, AppWindow, MediaItem, NamedItem, PlanItem, ProfileArtifact, ProfileBridge, ProfileInfo, PromptItem, TransitChoice, commands, pump_now,
};

/// The overlays this group owns.
const OWNED: &[&str] = &["profile", "overview", "voices", "orchestrator"];

#[derive(Default)]
struct State {
    session: String,
    /// The dialogs below the one shown, to return to.
    below: Vec<String>,
    model_ids: Vec<String>,
    effort_ids: Vec<String>,
    /// Decoded images by `file://` URL, so a refresh does not decode again.
    images: HashMap<String, slint::Image>,
}

thread_local! {
    static STATE: RefCell<State> = RefCell::new(State::default());
}

fn text(value: &Value, key: &str) -> String {
    match value.get(key) {
        Some(Value::String(text)) => text.clone(),
        Some(Value::Number(number)) => number.to_string(),
        _ => String::new(),
    }
}

fn first(values: &[String], fallback: &str) -> String {
    values.iter().find(|v| !v.is_empty()).cloned().unwrap_or_else(|| fallback.to_owned())
}

/// A named entry that may be a bare string or `{name}` (MCP servers).
fn name_of(value: &Value) -> String {
    value.as_str().map_or_else(|| text(value, "name"), str::to_owned)
}

/// Adds `current` (an id the catalog's `options` lack) at the end, named by
/// its id, so a picker can show it chosen.
fn with_current(options: &mut Vec<Value>, current: &str) {
    if !current.is_empty() && !options.iter().any(|o| text(o, "id") == current) {
        options.push(serde_json::json!({"id": current, "label": current}));
    }
}

// ---- dialog layers -----------------------------------------------------------

/// Shows `name`, keeping the dialog it opens over to return to.
pub fn show(app: &App, window: &AppWindow, name: &str) {
    let current = app.overlay.borrow().clone();
    STATE.with(|s| {
        let mut state = s.borrow_mut();
        if current.is_empty() || !OWNED.contains(&current.as_str()) {
            state.below.clear();
        } else if current != name {
            state.below.push(current);
        }
    });
    commands::open_overlay(app, window, name);
}

/// Closes the shown dialog, back to the one below it (if any).
pub fn close(app: &App, window: &AppWindow) {
    if app.overlay.borrow().as_str() == "profile" {
        let session = session();
        if !session.is_empty() && !window.global::<ProfileBridge>().get_transit_step().is_empty() {
            app.engine.borrow_mut().reset_transit(&session);
        }
        window.global::<ProfileBridge>().set_transit_step("".into());
    }
    let below = STATE.with(|s| s.borrow_mut().below.pop());
    match below {
        Some(name) => commands::open_overlay(app, window, &name),
        None => commands::close_overlay(app, window),
    }
    window.global::<ProfileBridge>().set_viewing(-1);
    window.global::<ProfileBridge>().set_confirm("".into());
}

/// Whether the Backend picker is open: the keymap's "transit" context.
pub fn transit_open(app: &App, window: &AppWindow) -> bool {
    app.overlay.borrow().as_str() == "profile" && !window.global::<ProfileBridge>().get_transit_step().is_empty()
}

/// Up/Down in the Backend picker: to the next choice that can be chosen,
/// skipping dimmed ones; at either end it stays.
pub fn transit_move(window: &AppWindow, step: i32) {
    let bridge = window.global::<ProfileBridge>();
    let enabled: Vec<bool> = bridge.get_transit_choices().iter().map(|c| c.enabled).collect();
    let from = bridge.get_transit_cursor();
    let mut at = from;
    let next = loop {
        at += step;
        if at < 0 || at as usize >= enabled.len() {
            break if from >= 0 && (from as usize) < enabled.len() { from } else { enabled.iter().position(|e| *e).map_or(-1, |i| i as i32) };
        }
        if enabled[at as usize] {
            break at;
        }
    };
    bridge.set_transit_cursor(next);
}

/// Enter in the Backend picker: picks the highlighted choice, or moves on
/// the last step.
pub fn transit_enter(window: &AppWindow) {
    let bridge = window.global::<ProfileBridge>();
    if bridge.get_transit_step() == "confirm" {
        bridge.invoke_confirm_transit();
        return;
    }
    let cursor = bridge.get_transit_cursor();
    if let Some(choice) = (cursor >= 0).then(|| bridge.get_transit_choices().row_data(cursor as usize)).flatten().filter(|c| c.enabled) {
        bridge.invoke_pick_transit(choice.id);
    }
}

pub fn owns(app: &App) -> bool {
    OWNED.contains(&app.overlay.borrow().as_str())
}

/// Escape closes the innermost thing: a confirmation, the image viewer or
/// the overview's menu first, then the dialog.
pub fn escape(app: &App, window: &AppWindow) {
    let bridge = window.global::<ProfileBridge>();
    let overview = window.global::<crate::OverviewBridge>();
    let overlay = app.overlay.borrow().clone();
    if overlay == "profile" && !bridge.get_transit_step().is_empty() {
        let session = session();
        app.engine.borrow_mut().transit_back(&session);
        fill(app, window, &session);
    } else if overlay == "profile" && bridge.get_viewing() >= 0 {
        bridge.set_viewing(-1);
    } else if overlay == "profile" && !bridge.get_confirm().is_empty() {
        bridge.set_confirm("".into());
    } else if overlay == "overview" && !overview.get_menu().is_empty() {
        overview.set_menu("".into());
        overview.set_confirm_release("".into());
    } else {
        close(app, window);
    }
}

/// The local path of a `file://` URL (percent-decoded).
pub(crate) fn file_path(url: &str) -> Option<std::path::PathBuf> {
    let encoded = url.strip_prefix("file://")?.as_bytes();
    let mut bytes = Vec::with_capacity(encoded.len());
    let mut index = 0;
    while index < encoded.len() {
        let hex = encoded.get(index + 1..index + 3).and_then(|h| std::str::from_utf8(h).ok()).and_then(|h| u8::from_str_radix(h, 16).ok());
        match (encoded[index], hex) {
            (b'%', Some(byte)) => {
                bytes.push(byte);
                index += 3;
            }
            (byte, _) => {
                bytes.push(byte);
                index += 1;
            }
        }
    }
    use std::os::unix::ffi::OsStringExt;
    Some(std::ffi::OsString::from_vec(bytes).into())
}

/// A cached image file as a Slint image, decoded once.
pub fn image(url: &str) -> slint::Image {
    if url.is_empty() {
        return slint::Image::default();
    }
    STATE.with(|s| {
        let mut state = s.borrow_mut();
        if let Some(image) = state.images.get(url) {
            return image.clone();
        }
        // Cached files have no extension: decode by content.
        let loaded = file_path(url).map(|p| std::fs::read(&p).map_err(|e| e.to_string()).and_then(|bytes| image::load_from_memory(&bytes).map_err(|e| e.to_string())));
        let image = match loaded {
            Some(Ok(decoded)) => {
                let rgba = decoded.to_rgba8();
                let buffer = slint::SharedPixelBuffer::<slint::Rgba8Pixel>::clone_from_slice(rgba.as_raw(), rgba.width(), rgba.height());
                slint::Image::from_rgba8(buffer)
            }
            Some(Err(error)) => {
                eprintln!("clarp-slint: cannot load {url}: {error}");
                slint::Image::default()
            }
            None => {
                eprintln!("clarp-slint: not a local file: {url}");
                slint::Image::default()
            }
        };
        state.images.insert(url.to_owned(), image.clone());
        image
    })
}

pub fn reduced_motion(app: &App) -> bool {
    app.engine.borrow().settings().boolean("appearance/reducedMotion", false)
}

/// The live status line under the chat (Settings, off by default).
pub fn live_status_line(app: &App) -> bool {
    app.engine.borrow().settings().boolean(LIVE_STATUS_LINE, false)
}

pub const LIVE_STATUS_LINE: &str = "appearance/liveStatusLine";

/// The send animation (Settings, on by default).
pub fn send_animation(app: &App) -> bool {
    app.engine.borrow().settings().boolean(SEND_ANIMATION, true)
}

pub const SEND_ANIMATION: &str = "appearance/sendAnimation";

/// Opens a `file://` URL in the desktop's file manager; checks record it in
/// `CLARP_TEST_OPEN_URL` instead.
pub(crate) fn open_file_url(url: &str) {
    if !url.starts_with("file://") {
        eprintln!("clarp-slint: not opening {url}: only local folders open here");
        return;
    }
    if let Some(path) = std::env::var_os("CLARP_TEST_OPEN_URL").filter(|p| !p.is_empty()) {
        use std::io::Write;
        let written = std::fs::OpenOptions::new().create(true).append(true).open(&path).and_then(|mut f| writeln!(f, "{url}"));
        if let Err(error) = written {
            eprintln!("clarp-slint: could not record {url}: {error}");
        }
        return;
    }
    crate::platform::opener::open(url, false);
}

// ---- the profile ---------------------------------------------------------------

pub fn session() -> String {
    STATE.with(|s| s.borrow().session.clone())
}

/// Opens `session`'s profile (Main.qml `onProfileRequested`): the chat is
/// selected and its plan, prompts, updates, teams and images load.
pub fn open(app: &Rc<App>, window: &AppWindow, session: &str) {
    if session.is_empty() || session.starts_with("pair:") {
        return;
    }
    STATE.with(|s| s.borrow_mut().session = session.to_owned());
    {
        let mut engine = app.engine.borrow_mut();
        if engine.selected_session() != session {
            engine.select(session);
        }
        engine.load_agent_profile(session);
        engine.load_media(session);
    }
    let bridge = window.global::<ProfileBridge>();
    bridge.set_confirm("".into());
    bridge.set_viewing(-1);
    show(app, window, "profile");
    pump_now(app);
    refresh(app, window, &[Change::Profile]);
}

/// Brings the open profile up to date.
pub fn refresh(app: &App, window: &AppWindow, changes: &[Change]) {
    let reduced = reduced_motion(app);
    window.global::<ProfileBridge>().set_reduced_motion(reduced);
    let look = window.global::<crate::ChatLook>();
    look.set_reduced_motion(reduced);
    look.set_live_status_line(live_status_line(app));
    look.set_send_animation(send_animation(app));
    window.global::<crate::SendFlight>().set_status_grace(crate::view::SEND_STATUS_GRACE.as_millis() as i64);
    crate::overview_view::refresh(app, window, changes, reduced);
    crate::voice_view::refresh(app, window, changes, reduced);
    crate::orchestrator_view::refresh(app, window, changes, reduced);
    let session = session();
    if session.is_empty() || *app.overlay.borrow() != "profile" {
        return;
    }
    let relevant = changes.iter().any(|c| {
        matches!(
            c,
            Change::Profile
                | Change::Roster
                | Change::AgentMutated(_)
                | Change::Launch
                | Change::Media
                | Change::Updates
                | Change::Teams
                | Change::Avatars
                | Change::Connection
        )
    });
    if relevant {
        fill(app, window, &session);
    }
}

fn fill(app: &App, window: &AppWindow, session: &str) {
    let bridge = window.global::<ProfileBridge>();
    let Some(details) = app.engine.borrow_mut().agent_details(session) else {
        // Released or gone: the profile has nothing left to show.
        close(app, window);
        return;
    };
    let portrait = crate::avatar_view::portrait(app, session, crate::avatar_view::sizes(app).card);
    let engine = app.engine.borrow();
    let value = |key: &str| text(&details, key);
    let flag = |key: &str| details.get(key).and_then(Value::as_bool).unwrap_or(false);
    let count = |key: &str| details.get(key).and_then(Value::as_i64).unwrap_or(0);
    let backend = value("backend");
    let name = first(&[value("name")], "Agent");

    // Model and effort, as the catalog lists them. What the agent runs
    // (its roster row) is always among them: a model or effort the catalog
    // does not list (one set with /agent-llm, a provider's own) is added,
    // so the box says "Harness default" only when the agent runs it.
    let mut models = engine.models_for_backend(&backend);
    with_current(&mut models, &value("model"));
    let model_ids: Vec<String> = models.iter().map(|m| text(m, "id")).collect();
    let model_index = model_ids.iter().position(|id| *id == value("model")).unwrap_or(0);
    let mut efforts = engine.efforts_for_model(&backend, &value("model"));
    with_current(&mut efforts, &value("effort"));
    let effort_ids: Vec<String> = efforts.iter().map(|e| text(e, "id")).collect();
    let effort_index = effort_ids.iter().position(|id| *id == value("effort")).unwrap_or(0);
    bridge.set_models(ModelRc::new(VecModel::from(models.iter().map(|m| SharedString::from(first(&[text(m, "label"), text(m, "id")], "")))
        .collect::<Vec<_>>())));
    bridge.set_efforts(ModelRc::new(VecModel::from(efforts.iter().map(|e| SharedString::from(first(&[text(e, "label"), text(e, "id")], "")))
        .collect::<Vec<_>>())));
    STATE.with(|s| {
        let mut state = s.borrow_mut();
        state.model_ids = model_ids;
        state.effort_ids = effort_ids;
    });

    // The plan.
    let plan = engine.profile_task_plan();
    let plan_value = Value::Object(plan.clone());
    let items: Vec<PlanItem> = plan
        .get("items")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .map(|item| {
            let steps = item.get("subtasks").and_then(Value::as_array).map_or(0, Vec::len);
            PlanItem {
                title: first(&[text(item, "title")], "Task").into(),
                status: text(item, "status").into(),
                steps: if steps > 0 { format!("{steps} steps") } else { String::new() }.into(),
            }
        })
        .collect();
    let number = |key: &str| plan.get(key).and_then(Value::as_i64).unwrap_or(0);
    bridge.set_plan(ModelRc::new(VecModel::from(items)));

    // Prompts, heartbeat, teams, MCP servers, schedules.
    let prompts: Vec<PromptItem> = engine
        .profile_prompts()
        .iter()
        .map(|p| {
            let channel = first(&[p.get("prompt_origin").map(|o| text(o, "channel")).unwrap_or_default()], "chat");
            let stamp = text(p, "created_at");
            PromptItem {
                text: first(&[text(p, "text"), text(p, "preview")], "").into(),
                meta: if stamp.is_empty() { channel } else { format!("{stamp}  ·  {channel}") }.into(),
            }
        })
        .collect();
    bridge.set_prompts(ModelRc::new(VecModel::from(prompts)));
    let heartbeat = Value::Object(engine.profile_heartbeat().clone());
    let schedule = heartbeat.get("schedule").cloned().unwrap_or(Value::Null);
    let scheduled = |key: &str| schedule.get(key).and_then(Value::as_bool).unwrap_or(false);
    let history: Vec<SharedString> = heartbeat
        .get("history")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .map(|h| SharedString::from(first(&[text(h, "text")], "Heartbeat check")))
        .collect();
    bridge.set_heartbeat_history(ModelRc::new(VecModel::from(history)));
    let teams: Vec<NamedItem> = details
        .get("team_ids")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .map(|id| NamedItem { id: id.into(), name: engine.team_name_by_id(id).into(), ..NamedItem::default() })
        .collect();
    bridge.set_teams(ModelRc::new(VecModel::from(teams)));
    let chosen: Vec<String> = details.get("mcp_servers").and_then(Value::as_array).into_iter().flatten().map(name_of).collect();
    let available: Vec<String> = engine.available_mcp_servers().iter().map(name_of).filter(|n| !n.is_empty()).collect();
    let mcp: Vec<NamedItem> =
        available.iter().map(|n| NamedItem { id: n.as_str().into(), name: n.as_str().into(), on: chosen.contains(n), ..NamedItem::default() }).collect();
    bridge.set_mcp(ModelRc::new(VecModel::from(mcp)));
    let schedules: Vec<NamedItem> = details
        .get("schedules")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .map(|s| NamedItem {
            id: text(s, "schedule_id").into(),
            name: first(&[text(s, "name")], "Scheduled task").into(),
            detail: text(s, "cron_expression").into(),
            on: s.get("enabled").and_then(Value::as_bool).unwrap_or(false),
        })
        .collect();
    bridge.set_schedules(ModelRc::new(VecModel::from(schedules)));

    // Artifacts and images.
    let artifacts: Vec<ProfileArtifact> = engine
        .artifacts_for_session(session)
        .iter()
        .map(|a| ProfileArtifact {
            title: first(&[text(a, "title")], "Artifact").into(),
            kind: first(&[text(a, "type")], "item").to_uppercase().into(),
            summary: text(a, "summary").into(),
        })
        .collect();
    bridge.set_artifacts(ModelRc::new(VecModel::from(artifacts)));
    let media: Vec<(String, String, bool)> = engine
        .media_for_session(session)
        .iter()
        .map(|asset| {
            let id = text(asset, "asset_id");
            let source = engine.media_source(&id).unwrap_or_default().to_owned();
            let loading = source.is_empty() && text(asset, "mime_type").starts_with("image/");
            (first(&[text(asset, "caption"), text(asset, "source_name")], "Image"), source, loading)
        })
        .collect();

    let transit_row = backend_row(&engine, session, &name, &backend, &value("model"));

    // Unknown usage (null) stays unknown: no gauge and no "0 tokens" (#403).
    let (tokens, window_size) = (details.get("context_tokens").and_then(Value::as_i64), count("context_window"));
    let queue = count("queue_count");
    let state = value("state");
    let info = ProfileInfo {
        session: session.into(),
        name: name.as_str().into(),
        initial: crate::view::initial(&name),
        symbol: SharedString::new(),
        portrait,
        backend: backend.into(),
        folder: first(&[value("working_directory")], "Unavailable").into(),
        state: first(&[state.clone()], "offline").into(),
        model_index: model_index as i32,
        effort_index: effort_index as i32,
        heartbeat: flag("heartbeat_enabled"),
        dreaming: flag("dreaming_enabled"),
        push_alerts: !flag("muted"),
        context_fraction: match tokens {
            Some(tokens) if window_size > 0 => tokens as f32 / window_size as f32,
            Some(_) => 0.0,
            None => -1.0,
        },
        context_used: match tokens {
            Some(tokens) => format!("{tokens} / {} tokens", if window_size > 0 { window_size.to_string() } else { "unknown".into() }),
            // Unknown usage says nothing: the row is left out.
            None => String::new(),
        }
        .into(),
        compacting: state == "compacting",
        queue_text: if queue == 0 { "Nothing waiting".to_owned() } else { format!("{queue} waiting") }.into(),
        plan_shown: engine.profile_loading() || !plan.is_empty(),
        plan_loading: engine.profile_loading(),
        plan_title: text(&plan_value, "title").into(),
        plan_progress: format!("{}/{}", number("completed_count"), number("total_count")).into(),
        // The Heartbeat switch says on or off; only a dormant one adds a word.
        heartbeat_note: if scheduled("dormant") { "Dormant" } else { "" }.into(),
        prompts_loading: engine.profile_prompts_loading(),
        prompts_more: engine.profile_prompts_have_more(),
        prompts_shown: engine.profile_prompts_offered(),
        mcp_shown: value("backend") == "claude" && !available.is_empty(),
        agent_messages_shown: engine.chat_hidden_coordination(session),
        agent_messages_index: crate::chat_menu::COORDINATION.iter().position(|(v, _)| *v == engine.chat_coordination_choice(session)).unwrap_or(0) as i32,
        tool_activity_index: engine.chat_activity_choice(session).map_or(0, |mode| mode + 1),
        backend_shown: transit_row.shown,
        backend_value: transit_row.value.into(),
        backend_detail: transit_row.detail.into(),
        backend_enabled: transit_row.enabled,
        backend_moving: transit_row.moving,
        backend_failed: transit_row.failed,
        // The chat's view (#702), as its menu offers it.
        chat_view_shown: engine.chat_view_offered(session),
        chat_view_index: clarp_core::chat_view::MENU.iter().position(|(v, _)| *v == engine.chat_view_menu_value(session)).unwrap_or(0) as i32,
        tool_activity_applies: !(engine.chat_view_offered(session) && engine.chat_view_conversation(session)),
        error: engine.profile_error().into(),
    };
    drop(engine);
    let media: Vec<MediaItem> =
        media.into_iter().map(|(caption, source, loading)| MediaItem { id: SharedString::new(), caption: caption.into(), image: image(&source), loading }).collect();
    bridge.set_media(ModelRc::new(VecModel::from(media)));
    bridge.set_info(info);
    bridge.set_transit_step(transit_row.picker.step.into());
    bridge.set_transit_title(transit_row.picker.title.into());
    bridge.set_transit_body(transit_row.picker.body.into());
    bridge.set_transit_loading(transit_row.picker.loading);
    bridge.set_transit_error(transit_row.picker.error.into());
    // The keyboard starts on what the agent runs now, else the first
    // choice that can be chosen; a new list (backends, then models) resets it.
    let choices = transit_row.picker.choices;
    let start = choices.iter().position(|c| c.current && c.enabled).or_else(|| choices.iter().position(|c| c.enabled));
    let same = bridge.get_transit_choices().iter().map(|c| c.id).eq(choices.iter().map(|c| c.id.clone()));
    if !same {
        bridge.set_transit_cursor(start.map_or(-1, |i| i as i32));
    }
    bridge.set_transit_choices(ModelRc::new(VecModel::from(choices)));
}

// ---- the Backend row (feature agent_backend_transit) ---------------------------

#[derive(Default)]
struct BackendRow {
    shown: bool,
    value: String,
    detail: String,
    enabled: bool,
    moving: bool,
    failed: bool,
    picker: Picker,
}

#[derive(Default)]
struct Picker {
    /// "", "backends", "models" or "confirm".
    step: &'static str,
    title: String,
    body: String,
    loading: bool,
    error: String,
    choices: Vec<TransitChoice>,
}

/// The chat header's "Moving to …" while the agent changes backend (#723),
/// the words the profile's Backend row shows; empty otherwise. The roster
/// row's `transit` drives it (also a move another app started), and it
/// clears when the move is done or failed.
pub(crate) fn moving_line(engine: &clarp_engine::Engine, session: &str) -> String {
    let Some(agent) = engine.roster().find(session) else { return String::new() };
    let row = backend_row(engine, session, &engine.chat_name(session), &agent.backend, &agent.model);
    if row.moving { row.detail } else { String::new() }
}

/// The Backend row and its picker, from the agent's roster row, its flow and
/// the options the Host offered it. Nothing without the feature.
fn backend_row(engine: &clarp_engine::Engine, session: &str, name: &str, backend: &str, model: &str) -> BackendRow {
    if !engine.transit_offered(session) {
        return BackendRow::default();
    }
    let options = engine.transit_options(session);
    let flow = engine.transit_flow(session);
    // A backend and a model in words: the picker's labels, else the catalog's.
    let backend_label = |id: &str| -> String {
        options
            .backends
            .iter()
            .find(|b| b.id == id)
            .map(|b| b.label.clone())
            .or_else(|| engine.backend_options().iter().find(|o| text(o, "id") == id).map(|o| text(o, "label")))
            .filter(|l| !l.is_empty())
            .unwrap_or_else(|| id.to_owned())
    };
    let model_label = |backend: &str, id: &str| -> String {
        if id.is_empty() {
            return String::new();
        }
        options
            .backends
            .iter()
            .find(|b| b.id == backend)
            .and_then(|b| b.models.iter().find(|m| m.id == id).map(|m| m.label.clone()))
            .or_else(|| engine.models_for_backend(backend).iter().find(|m| text(m, "id") == id).map(|m| text(m, "label")))
            .filter(|l| !l.is_empty())
            .unwrap_or_else(|| id.to_owned())
    };
    let blocked = engine.transit_blocked(session);
    // The row names the tool alone: the Model line below says the model.
    let mut row = BackendRow { shown: true, value: backend_label(backend), ..BackendRow::default() };
    match &flow.step {
        Step::Moving { backend: target, phase, .. } => {
            row.moving = true;
            let target_model = model_label(target, &flow.target_model);
            let shown_model = if target_model.is_empty() { "the default model".to_owned() } else { target_model };
            row.detail = transit::progress(&backend_label(target), &shown_model, target == backend, *phase);
        }
        _ if blocked => row.detail = transit::TURN_BLOCKED.to_owned(),
        Step::Failed { reason, .. } => {
            row.detail = reason.clone();
            row.failed = true;
        }
        _ => {}
    }
    row.enabled = !blocked && !row.moving;
    let picker = &mut row.picker;
    picker.loading = options.loading;
    picker.error = options.error.clone();
    match &flow.step {
        Step::Picking => {
            picker.step = "backends";
            picker.title = transit::ROW_TITLE.to_owned();
            picker.choices = options
                .backends
                .iter()
                .map(|b| TransitChoice {
                    id: b.id.as_str().into(),
                    label: b.label.as_str().into(),
                    detail: b.reason.as_str().into(),
                    enabled: b.available,
                    current: b.id == backend,
                })
                .collect();
        }
        Step::Model { backend: chosen } => {
            picker.step = "models";
            picker.title = format!("{} · Model", backend_label(chosen));
            let models = options.backends.iter().find(|b| b.id == *chosen).map(|b| b.models.clone()).unwrap_or_default();
            let current = |id: &str| chosen == backend && id == model;
            picker.choices = if models.is_empty() {
                // A tool that lists no models runs its own default ("").
                vec![TransitChoice { id: "".into(), label: transit::DEFAULT_MARK.into(), enabled: !current(""), current: current(""), ..TransitChoice::default() }]
            } else {
                models
                    .iter()
                    .map(|m| TransitChoice {
                        id: m.id.as_str().into(),
                        label: m.label.as_str().into(),
                        detail: if m.default { transit::DEFAULT_MARK } else { "" }.into(),
                        enabled: !current(&m.id),
                        current: current(&m.id),
                    })
                    .collect()
            };
        }
        Step::Confirming { backend: chosen, model: target } => {
            picker.step = "confirm";
            let label = model_label(chosen, target);
            let label = if label.is_empty() { transit::DEFAULT_MARK.to_lowercase() + " model" } else { label };
            if chosen == backend {
                picker.title = transit::switch_title(name, &label);
                picker.body = transit::SWITCH_BODY.to_owned();
            } else {
                picker.title = transit::confirm_title(name, &backend_label(chosen), &label);
                picker.body = transit::confirm_body(engine.transit_carries_tools(session)).to_owned();
            }
        }
        _ => {}
    }
    row
}

fn with_session(act: impl FnOnce(&Rc<App>, &AppWindow, &str)) {
    let session = session();
    if session.is_empty() {
        return;
    }
    if let (Some(app), Some(window)) = (crate::app(), crate::window()) {
        act(&app, &window, &session);
        pump_now(&app);
    }
}

/// The agent's current details (the switches' state before a change).
fn detail(app: &App, session: &str, key: &str) -> Value {
    app.engine.borrow_mut().agent_details(session).and_then(|d| d.get(key).cloned()).unwrap_or(Value::Null)
}

/// Connects the profile's commands.
pub fn wire(window: &AppWindow) {
    let bridge = window.global::<ProfileBridge>();
    bridge.on_close(|| {
        if let (Some(app), Some(window)) = (crate::app(), crate::window()) {
            close(&app, &window);
        }
    });
    bridge.on_open_for_pane(|pane| {
        if let (Some(app), Some(window)) = (crate::app(), crate::window()) {
            let session = app.session_of(&pane);
            open(&app, &window, &session);
        }
    });
    bridge.on_open_files(|| {
        with_session(|app, _, session| {
            let url = app.engine.borrow_mut().agent_files_url(session);
            if let Some(url) = url {
                open_file_url(&url);
            }
        });
    });
    bridge.on_open_terminal(|| with_session(|app, _, session| app.engine.borrow_mut().open_agent_terminal(session)));
    bridge.on_voice(|| {
        with_session(|app, window, session| {
            let name = app.engine.borrow().chat_name(session);
            crate::voice_view::open(app, window, session, &name);
        });
    });
    bridge.on_queue(|| {
        with_session(|app, window, _| {
            if !commands::run(app, window, "manage-queue") {
                eprintln!("clarp-slint: the queue dialog is not available yet");
            }
        });
    });
    bridge.on_relaunch(|| {
        with_session(|app, window, _| {
            if !commands::run(app, window, "relaunch-agent") {
                eprintln!("clarp-slint: relaunching is not available yet");
            }
        });
    });
    bridge.on_load_older(|| with_session(|app, _, session| app.engine.borrow_mut().load_prompt_history(session, true)));
    bridge.on_set_model(|index| {
        with_session(|app, _, session| {
            let backend = detail(app, session, "backend").as_str().unwrap_or_default().to_owned();
            let chosen = STATE.with(|s| s.borrow().model_ids.get(index as usize).cloned()).unwrap_or_default();
            let options: Vec<String> = app.engine.borrow().efforts_for_model(&backend, &chosen).iter().map(|e| text(e, "id")).collect();
            // An effort the new model lacks (or its first) becomes the default.
            let mut effort = detail(app, session, "effort").as_str().unwrap_or_default().to_owned();
            if !effort.is_empty() && options.iter().position(|id| *id == effort).unwrap_or(0) == 0 {
                effort.clear();
            }
            app.engine.borrow_mut().set_agent_llm(session, &chosen, &effort);
        });
    });
    bridge.on_set_effort(|index| {
        with_session(|app, _, session| {
            let model = detail(app, session, "model").as_str().unwrap_or_default().to_owned();
            let effort = STATE.with(|s| s.borrow().effort_ids.get(index as usize).cloned()).unwrap_or_default();
            app.engine.borrow_mut().set_agent_llm(session, &model, &effort);
        });
    });
    bridge.on_open_transit(|| {
        with_session(|app, window, session| {
            app.engine.borrow_mut().open_transit(session);
            fill(app, window, session);
        });
    });
    bridge.on_pick_transit(|id| {
        with_session(|app, window, session| {
            let step = window.global::<ProfileBridge>().get_transit_step();
            if step == "backends" {
                app.engine.borrow_mut().pick_transit_backend(session, &id);
            } else if step == "models" {
                app.engine.borrow_mut().pick_transit_model(session, &id);
            }
            fill(app, window, session);
        });
    });
    bridge.on_transit_back(|| {
        with_session(|app, window, session| {
            app.engine.borrow_mut().transit_back(session);
            fill(app, window, session);
        });
    });
    bridge.on_confirm_transit(|| {
        with_session(|app, window, session| {
            app.engine.borrow_mut().confirm_transit(session);
            fill(app, window, session);
        });
    });
    bridge.on_set_heartbeat(|on| with_session(|app, _, session| app.engine.borrow_mut().set_agent_heartbeat(session, on)));
    bridge.on_set_dreaming(|on| with_session(|app, _, session| app.engine.borrow_mut().set_agent_dreaming(session, on)));
    bridge.on_set_push_alerts(|on| with_session(|app, _, session| app.engine.borrow_mut().set_agent_push_muted(session, !on)));
    // This chat's own choices (#653): messages between agents (its Host's)
    // and Tool activity (this device's).
    bridge.on_set_agent_messages(|index| {
        with_session(|app, _, session| {
            let choice = crate::chat_menu::COORDINATION.get(index.max(0) as usize).map_or("default", |(v, _)| v);
            if app.engine.borrow().chat_coordination_choice(session) != choice {
                app.engine.borrow_mut().set_chat_coordination(session, choice);
            }
        });
    });
    // The chat's view (#702): this device's own choice, written to its Host.
    bridge.on_set_chat_view(|index| {
        with_session(|app, _, session| {
            let value = clarp_core::chat_view::MENU.get(index.max(0) as usize).map_or("default", |(v, _)| v);
            if app.engine.borrow().chat_view_menu_value(session) != value {
                app.engine.borrow_mut().set_chat_view(session, value);
            }
        });
    });
    bridge.on_set_tool_activity(|index| {
        with_session(|app, _, session| {
            let value = clarp_core::presentation::CHAT_ACTIVITY_CHOICES.get(index.max(0) as usize).map_or("follow", |(v, _)| v);
            crate::chat_menu::set_activity(&mut app.engine.borrow_mut(), session, value);
        });
    });
    bridge.on_set_mcp(|name, on| {
        with_session(|app, _, session| {
            let mut chosen: Vec<String> = detail(app, session, "mcp_servers").as_array().into_iter().flatten().map(name_of).collect();
            let name = name.to_string();
            match chosen.iter().position(|n| *n == name) {
                Some(index) if !on => {
                    chosen.remove(index);
                }
                None if on => chosen.push(name),
                _ => {}
            }
            app.engine.borrow_mut().set_agent_mcp(session, &chosen);
        });
    });
    bridge.on_set_schedule(|id, on| with_session(|app, _, _| app.engine.borrow_mut().set_schedule_enabled(&id, on)));
    bridge.on_compact(|| {
        with_session(|app, window, session| {
            window.global::<ProfileBridge>().set_confirm("".into());
            app.engine.borrow_mut().compact_session(session);
        });
    });
    bridge.on_release(|| {
        with_session(|app, window, session| {
            window.global::<ProfileBridge>().set_confirm("".into());
            app.engine.borrow_mut().release_agent(session);
            close(app, window);
        });
    });
}
