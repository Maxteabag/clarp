//! The agent profile (AgentProfilePanel.qml with its MediaGallery), and the
//! layering of this group's dialogs: the profile and the overview open the
//! voice and orchestrator dialogs over themselves, and closing one returns
//! to the dialog below it.

use std::cell::RefCell;
use std::collections::HashMap;
use std::rc::Rc;

use clarp_engine::Change;
use serde_json::Value;
use slint::{ComponentHandle, ModelRc, SharedString, VecModel};

use crate::{
    App, AppWindow, MediaItem, NamedItem, PlanItem, ProfileArtifact, ProfileBridge, ProfileInfo, PromptItem, commands, pump_now,
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
    /// Portraits the decoder has and has not delivered yet.
    decoding: std::collections::HashSet<String>,
    /// Of those, the ones a list asked for: their arrival refreshes it.
    wanted: std::collections::HashSet<String>,
    /// The paced refresh for arrived portraits is running (`portraits_due`).
    due: bool,
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
    let below = STATE.with(|s| s.borrow_mut().below.pop());
    match below {
        Some(name) => commands::open_overlay(app, window, &name),
        None => commands::close_overlay(app, window),
    }
    window.global::<ProfileBridge>().set_viewing(-1);
    window.global::<ProfileBridge>().set_confirm("".into());
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
    if overlay == "profile" && bridge.get_viewing() >= 0 {
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
fn file_path(url: &str) -> Option<std::path::PathBuf> {
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

/// The agent's portrait (fetched on first use; empty until it arrives).
/// An agent's portrait for a list: decoded on the portrait decoder, so a
/// hundred of them never hold the UI thread. Empty (the initial shows)
/// until it is there; its arrival refreshes the lists.
pub fn portrait(app: &App, session: &str) -> slint::Image {
    let Some(url) = app.engine.borrow_mut().avatar_source(session) else { return slint::Image::default() };
    let cached = STATE.with(|s| s.borrow().images.get(&url).cloned());
    cached.unwrap_or_else(|| {
        STATE.with(|s| s.borrow_mut().wanted.insert(url.clone()));
        decode_later(vec![url]);
        slint::Image::default()
    })
}

/// Whether a portraits-only change rebuilds the lists now: only in the
/// paced refresh for arrived portraits. Otherwise the new ones are
/// decoded first, or the paced refresh is asked for when they already are.
pub fn portraits_due(app: &App) -> bool {
    if STATE.with(|s| std::mem::take(&mut s.borrow_mut().due)) {
        return true;
    }
    let sessions: Vec<String> = app.engine.borrow().roster().agents().iter().map(|a| a.session.clone()).collect();
    let urls: Vec<String> = sessions.iter().filter_map(|s| app.engine.borrow_mut().avatar_source(s)).collect();
    let missing: Vec<String> = STATE.with(|s| {
        let mut state = s.borrow_mut();
        let missing: Vec<String> = urls.into_iter().filter(|u| !state.images.contains_key(u)).collect();
        state.wanted.extend(missing.iter().cloned());
        missing
    });
    if missing.is_empty() {
        refresh_for_portraits("new portraits already decoded".into());
    }
    decode_later(missing);
    false
}

/// Decodes the cached portraits (newest first) while the Host is asked
/// for the roster, so a usual launch lists every agent with its portrait.
pub fn prewarm_portraits() {
    let Some(folder) = clarp_core::media::cache_dir().map(|c| c.join("portraits")) else { return };
    let Ok(entries) = std::fs::read_dir(&folder) else { return };
    let mut files: Vec<(std::time::SystemTime, std::path::PathBuf)> = entries
        .filter_map(Result::ok)
        .filter_map(|e| Some((e.metadata().ok()?.modified().ok()?, e.path())))
        .collect();
    files.sort_by(|a, b| b.0.cmp(&a.0));
    // A portrait is 147 KB decoded: no more than a large roster's worth.
    let urls = files.into_iter().take(200).filter_map(|(_, path)| url::Url::from_file_path(path).ok()).map(String::from).collect();
    decode_later(urls);
}

type Decoded = Vec<(String, Option<slint::SharedPixelBuffer<slint::Rgba8Pixel>>)>;

/// Hands `urls` to the decoder thread, each once.
fn decode_later(urls: Vec<String>) {
    use std::sync::{Mutex, OnceLock, mpsc};
    static DECODER: OnceLock<Mutex<mpsc::Sender<String>>> = OnceLock::new();
    let urls: Vec<String> = STATE.with(|s| {
        let mut state = s.borrow_mut();
        urls.into_iter().filter(|u| !state.images.contains_key(u) && state.decoding.insert(u.clone())).collect()
    });
    if urls.is_empty() {
        return;
    }
    let decoder = DECODER.get_or_init(|| {
        let (sender, receiver) = mpsc::channel::<String>();
        let spawned = std::thread::Builder::new().name("portraits".into()).spawn(move || {
            while let Ok(first) = receiver.recv() {
                // What queued meanwhile goes in the same batch: one list
                // refresh for many portraits.
                let started = std::time::Instant::now();
                let batch: Decoded = std::iter::once(first).chain(receiver.try_iter()).map(|url| {
                    let pixels = decode(&url);
                    (url, pixels)
                }).collect();
                let took = started.elapsed();
                if let Err(error) = slint::invoke_from_event_loop(move || decoded(batch, took)) {
                    eprintln!("clarp-slint: dropped decoded portraits: {error}");
                }
            }
        });
        if let Err(error) = spawned {
            eprintln!("clarp-slint: no portrait decoder: {error}");
        }
        Mutex::new(sender)
    });
    if let Ok(sender) = decoder.lock() {
        for url in urls {
            let _ = sender.send(url);
        }
    }
}

fn decode(url: &str) -> Option<slint::SharedPixelBuffer<slint::Rgba8Pixel>> {
    let loaded = file_path(url).map(|p| std::fs::read(&p).map_err(|e| e.to_string()).and_then(|bytes| image::load_from_memory(&bytes).map_err(|e| e.to_string())));
    match loaded {
        Some(Ok(decoded)) => {
            let rgba = decoded.to_rgba8();
            Some(slint::SharedPixelBuffer::<slint::Rgba8Pixel>::clone_from_slice(rgba.as_raw(), rgba.width(), rgba.height()))
        }
        Some(Err(error)) => {
            eprintln!("clarp-slint: cannot load {url}: {error}");
            None
        }
        None => {
            eprintln!("clarp-slint: not a local file: {url}");
            None
        }
    }
}

/// On the UI thread: the decoder's batch joins the cache, and lists that
/// show one of them refresh once.
fn decoded(batch: Decoded, took: std::time::Duration) {
    let count = batch.len();
    let wanted = STATE.with(|s| {
        let mut state = s.borrow_mut();
        let mut wanted = false;
        for (url, pixels) in batch {
            state.decoding.remove(&url);
            wanted |= state.wanted.remove(&url);
            state.images.insert(url, pixels.map(slint::Image::from_rgba8).unwrap_or_default());
        }
        wanted
    });
    if wanted {
        refresh_for_portraits(format!("{count} portraits decoded in {:.1} ms", crate::perf::ms(took)));
    }
}

/// Refreshes the lists for arrived portraits, at most every 100 ms: at a
/// first launch they arrive a few at a time for a while.
fn refresh_for_portraits(why: String) {
    use std::time::{Duration, Instant};
    const EVERY: Duration = Duration::from_millis(100);
    thread_local! {
        /// The last refresh, and whether one is already scheduled.
        static PACE: std::cell::Cell<(Option<Instant>, bool)> = const { std::cell::Cell::new((None, false)) };
    }
    let (last, scheduled) = PACE.get();
    if scheduled {
        return;
    }
    let refresh = move || {
        PACE.set((Some(Instant::now()), false));
        if let Some(app) = crate::app() {
            let started = Instant::now();
            STATE.with(|s| s.borrow_mut().due = true);
            app.refresh(&[Change::Avatars]);
            STATE.with(|s| s.borrow_mut().due = false);
            crate::perf::woke(started, why);
        }
    };
    match last.map_or(Duration::ZERO, |at| EVERY.saturating_sub(at.elapsed())) {
        wait if wait.is_zero() => refresh(),
        wait => {
            PACE.set((last, true));
            slint::Timer::single_shot(wait, refresh);
        }
    }
}

pub fn reduced_motion(app: &App) -> bool {
    app.engine.borrow().settings().boolean("appearance/reducedMotion", false)
}

/// Opens a `file://` URL in the desktop's file manager; checks record it in
/// `CLARP_TEST_OPEN_URL` instead.
fn open_file_url(url: &str) {
    if !url.starts_with("file://") {
        eprintln!("clarp-slint: not opening {url}: only local folders open here");
        return;
    }
    if let Some(path) = std::env::var_os("CLARP_TEST_OPEN_URL") {
        use std::io::Write;
        let written = std::fs::OpenOptions::new().create(true).append(true).open(&path).and_then(|mut f| writeln!(f, "{url}"));
        if let Err(error) = written {
            eprintln!("clarp-slint: could not record {url}: {error}");
        }
        return;
    }
    if let Err(error) = std::process::Command::new("xdg-open").arg(url).spawn() {
        eprintln!("clarp-slint: could not open {url}: {error}");
    }
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
    let portrait = portrait(app, session);
    let engine = app.engine.borrow();
    let value = |key: &str| text(&details, key);
    let flag = |key: &str| details.get(key).and_then(Value::as_bool).unwrap_or(false);
    let count = |key: &str| details.get(key).and_then(Value::as_i64).unwrap_or(0);
    let backend = value("backend");
    let name = first(&[value("name")], "Agent");

    // Model and effort, as the catalog lists them.
    let models = engine.models_for_backend(&backend);
    let model_ids: Vec<String> = models.iter().map(|m| text(m, "id")).collect();
    let model_index = model_ids.iter().position(|id| *id == value("model")).unwrap_or(0);
    let efforts = engine.efforts_for_model(&backend, &value("model"));
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

    let (tokens, window_size) = (count("context_tokens"), count("context_window"));
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
        context_fraction: if window_size > 0 { tokens as f32 / window_size as f32 } else { 0.0 },
        context_used: format!("{tokens} / {} tokens", if window_size > 0 { window_size.to_string() } else { "unknown".into() }).into(),
        compacting: state == "compacting",
        queue_text: if queue == 0 { "Nothing waiting".to_owned() } else { format!("{queue} waiting") }.into(),
        plan_shown: engine.profile_loading() || !plan.is_empty(),
        plan_loading: engine.profile_loading(),
        plan_title: text(&plan_value, "title").into(),
        plan_progress: format!("{}/{}", number("completed_count"), number("total_count")).into(),
        heartbeat_state: if scheduled("dormant") { "Dormant" } else if scheduled("enabled") { "Active" } else { "Off" }.into(),
        heartbeat_empty: if scheduled("enabled") { "No heartbeat history yet" } else { "Heartbeat is off" }.into(),
        prompts_loading: engine.profile_prompts_loading(),
        prompts_more: engine.profile_prompts_have_more(),
        mcp_shown: value("backend") == "claude" && !available.is_empty(),
        error: engine.profile_error().into(),
    };
    drop(engine);
    let media: Vec<MediaItem> =
        media.into_iter().map(|(caption, source, loading)| MediaItem { id: SharedString::new(), caption: caption.into(), image: image(&source), loading }).collect();
    bridge.set_media(ModelRc::new(VecModel::from(media)));
    bridge.set_info(info);
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
    bridge.on_set_heartbeat(|on| with_session(|app, _, session| app.engine.borrow_mut().set_agent_heartbeat(session, on)));
    bridge.on_set_dreaming(|on| with_session(|app, _, session| app.engine.borrow_mut().set_agent_dreaming(session, on)));
    bridge.on_set_push_alerts(|on| with_session(|app, _, session| app.engine.borrow_mut().set_agent_push_muted(session, !on)));
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
