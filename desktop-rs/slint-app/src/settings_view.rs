//! The settings surface (SettingsPanel.qml): what it lists, and what
//! changing a row does. Preferences go to the Slint app's settings under
//! the Qt controller's keys.

use std::rc::Rc;

use clarp_engine::Change;
use serde_json::Value;
use slint::{ModelRc, VecModel};

use crate::{App, AppWindow, SettingRow};

fn section(label: &str) -> SettingRow {
    SettingRow { kind: "section".into(), id: String::new().into(), label: label.into(), ..SettingRow::default() }
}
fn toggle(id: &str, label: &str, on: bool) -> SettingRow {
    SettingRow { kind: "toggle".into(), id: id.into(), label: label.into(), on, ..SettingRow::default() }
}
fn choice(id: &str, label: &str, detail: &str) -> SettingRow {
    SettingRow { kind: "choice".into(), id: id.into(), label: label.into(), detail: detail.into(), ..SettingRow::default() }
}
fn action(id: &str, label: &str, detail: &str) -> SettingRow {
    SettingRow { kind: "action".into(), id: id.into(), label: label.into(), detail: detail.into(), ..SettingRow::default() }
}
fn info(label: &str, detail: &str) -> SettingRow {
    SettingRow { kind: "info".into(), id: String::new().into(), label: label.into(), detail: detail.into(), ..SettingRow::default() }
}

const ACTIVITY: [&str; 3] = ["Grouped", "Always visible", "Group old"];

/// The voice providers the Host offers, as (id, label); `with_none` adds
/// "No fallback" first.
fn providers(tts: &clarp_core::json::Object, with_none: bool) -> Vec<(String, String)> {
    let mut out = if with_none { vec![("none".to_owned(), "No fallback".to_owned())] } else { Vec::new() };
    for provider in tts.get("providers").and_then(Value::as_array).into_iter().flatten() {
        let text = |key: &str| provider.get(key).and_then(Value::as_str).unwrap_or_default().to_owned();
        let id = text("id");
        if id.is_empty() {
            continue;
        }
        let label = [text("name"), text("label")].into_iter().find(|l| !l.is_empty()).unwrap_or_else(|| id.clone());
        out.push((id, label));
    }
    out
}

pub fn rows(app: &App) -> Vec<SettingRow> {
    // The keys as the user bound them.
    let overrides = crate::commands::overrides(app);
    let key = |action: &str| crate::keymap::shown("pane", action, &overrides).unwrap_or_default();
    let engine = app.engine.borrow();
    let prefs = *app.prefs.borrow();
    let settings = engine.settings();
    let theme_id = engine.reading_theme();
    let theme = clarp_core::reading_theme::theme(&theme_id);
    let text = |object: &clarp_core::json::Object, key: &str| object.get(key).and_then(Value::as_str).unwrap_or_default().to_owned();
    let tts = engine.tts_provider_status();
    let diagnostics = engine.diagnostics_health();
    let transcription = engine.transcription_capabilities();
    let loading = engine.host_status_loading();
    let queue = diagnostics.get("tts_queue").and_then(Value::as_object).cloned().unwrap_or_default();
    let count = |key: &str| queue.get(key).and_then(Value::as_i64).unwrap_or(0);
    let provider = text(tts, "provider");
    let fallback = text(tts, "fallback");
    let label_of = |id: &str, with_none: bool| {
        providers(tts, with_none).into_iter().find(|(p, _)| p == id).map(|(_, l)| l).unwrap_or_else(|| if id.is_empty() { "Unknown".into() } else { id.to_owned() })
    };
    // The reader's own font for this theme, and where it falls back.
    let chosen = engine.font_override(&theme_id);
    let font = crate::view::resolved_font(theme, chosen.as_ref());
    let font_detail = match (&chosen, &font.missing) {
        (_, Some(missing)) => format!("{missing} is not installed · using {} · {} px", font.family, font.size),
        (Some(_), None) => format!("{} · {} px · your choice", font.family, font.size),
        (None, None) => format!("{} · {} px · theme default", font.family, font.size),
    };
    let theme_size = theme.get("fontPixelSize").and_then(Value::as_f64).unwrap_or(15.0);
    let reset_detail =
        if chosen.is_some() { format!("Back to {} · {theme_size} px", crate::view::theme_font(theme)) } else { "Already the theme default".to_owned() };
    let mut rows = vec![
        section("CHATS"),
        toggle("timestamps", "Timestamps", prefs.timestamps),
        toggle("show-when-ready", "Show when ready", engine.show_when_ready()),
        toggle("reduced-motion", "Reduce Motion", settings.boolean("appearance/reducedMotion", false)),
        choice("activity", "Tool activity", ACTIVITY[engine.activity_mode().clamp(0, 2) as usize]),
    ];
    // The Host's setting, on Hosts that send live items (§6).
    if engine.tool_explanation_setting() {
        rows.push(toggle("tool-explanations", "Tool explanations (Host · Ctrl+Shift+X)", engine.tool_explanations_enabled(engine.selected_session())));
    }
    rows.extend(vec![
        section("EXPERIMENTS"),
        toggle("narration", "Plain-English tools (Spark · extra usage)", engine.narrator_enabled()),
        choice("tool-detail", "Tool detail", clarp_engine::Engine::narrator_detail_levels()[engine.narrator_detail_level().clamp(0, 4) as usize]),
        info(engine.narrator_level_description(), &engine.narrator_status()),
        section("STARTUP"),
        toggle("new-agent-on-startup", "Start a new agent when opening Clarp", settings.boolean("launch/newAgentOnStartup", false)),
        section("AGENT IDENTITY"),
        toggle("anonymous-agents", "Anonymous agents by default", settings.boolean("launch/anonymousAgents", true)),
        section("APPEARANCE"),
        toggle("minimal-ui", "Minimal UI", settings.boolean("appearance/minimalUi", false)),
        toggle("compact-explorer", "Compact explorer (avatar and name only)", settings.boolean("explorer/compact", false)),
        choice("avatar-size", "Agent picture size", &crate::avatar_view::label(app)),
        toggle("nav-rail", "Navigation rail (Chats, Updates, Teams, Settings)", settings.boolean("appearance/navRail", true)),
        toggle("workspace-bar", "Workspace bar", prefs.workspace_bar),
        choice("reading-theme", "Reading theme", &format!("{} · {}", text(theme, "label"), crate::view::theme_font(theme))),
        action("font", "Font", &font_detail),
        action("reset-font", "Reset font to theme default", &reset_detail),
        section("VOICE & AUDIO"),
        toggle("spoken-replies", "Spoken replies", !engine.muted()),
        section("NOTIFICATIONS"),
        toggle("pause-mobile-push", "Pause phone alerts while active on desktop", settings.boolean("notifications/pauseMobileWhileDesktopActive", true)),
        section("HOST"),
        action("connection", if engine.server_name().is_empty() { "Clarp Host" } else { engine.server_name() }, &format!("{}  ·  {}", engine.base_url(), engine.connection_state())),
        action("orchestrator", "Orchestrator", "Open"),
        toggle("shared-filesystem", "Shared filesystem", engine.shared_filesystem()),
        section("HOST STATUS"),
        info("Diagnostics", if loading { "Loading…" } else if diagnostics.get("ready").and_then(Value::as_bool) == Some(true) { "Ready" } else { "Needs attention" }),
        info("Speech to text", if transcription.get("available").and_then(Value::as_bool) == Some(true) { "Available" } else { "Unavailable" }),
        info("Transcription model", &{
            let model = text(transcription, "default_model");
            if model.is_empty() { "Unknown".to_owned() } else { model }
        }),
        info("TTS queue", &format!("{} pending · {} in flight", count("pending"), count("in_flight"))),
        choice("voice-provider", "Voice provider", &label_of(&provider, false)),
        choice("voice-fallback", "Voice fallback", &label_of(if fallback.is_empty() { "none" } else { &fallback }, true)),
        section("KEYBOARD"),
        info("Key bindings", &key("edit-keymap")),
        info("Command palette", &key("switcher")),
        info("Settings", &key("settings")),
        info("Navigate settings", "↑↓ / Tab · Home / End"),
        info("Open native CLI", &key("agent-terminal")),
        info("Start idle contact", &key("new-contact")),
        info("Show / hide sidebar", &key("sidebar")),
        info("Move between panes", "Ctrl+Alt+Arrow"),
        info("Split right / down", "Ctrl+Alt+V / S"),
        info("Zoom / close / balance", "Ctrl+Alt+Z / X / ="),
        info("Queue message", "Ctrl+Enter"),
        section("ABOUT"),
        info("Desktop client", concat!(env!("CARGO_PKG_VERSION"), " preview (Slint)")),
        info("Host version", if engine.server_version().is_empty() { "Unknown" } else { engine.server_version() }),
    ]);
    rows
}

/// Shows the rows again (a setting or the Host status changed).
pub fn show(app: &App, window: &AppWindow) {
    if window.get_surface() != "settings" {
        return;
    }
    window.set_setting_rows(ModelRc::new(VecModel::from(rows(app))));
}

/// Opens the surface on its first actionable row and asks the Host how it is.
pub fn open(app: &App, window: &AppWindow) {
    window.set_surface("settings".into());
    app.engine.borrow_mut().load_host_status();
    show(app, window);
    if window.get_setting_current() <= 0 {
        window.set_setting_current(1);
    }
    window.invoke_focus_settings();
}

fn flip(app: &App, key: &str, default: bool) {
    let value = app.engine.borrow().settings().boolean(key, default);
    app.engine.borrow_mut().settings_mut().set(key, !value);
}

fn step(values: &[(String, String)], current: &str, delta: i32) -> Option<String> {
    if values.is_empty() {
        return None;
    }
    let index = values.iter().position(|(id, _)| id == current).map_or(0, |i| i as i32);
    let next = (index + delta).rem_euclid(values.len() as i32) as usize;
    Some(values[next].0.clone())
}

/// Space/Enter (delta 1) or Left/Right on a row.
pub fn change(app: &Rc<App>, window: &AppWindow, id: &str, delta: i32) {
    match id {
        "timestamps" | "workspace-bar" => {
            let (key, value) = {
                let mut prefs = app.prefs.borrow_mut();
                if id == "timestamps" {
                    prefs.timestamps = !prefs.timestamps;
                    ("conversation/timestampsVisible", prefs.timestamps)
                } else {
                    prefs.workspace_bar = !prefs.workspace_bar;
                    ("appearance/workspaceBar", prefs.workspace_bar)
                }
            };
            app.engine.borrow_mut().settings_mut().set(key, value);
            app.rebuild_transcripts();
            app.refresh(&[Change::Panes, Change::Preferences]);
        }
        "show-when-ready" => {
            let value = app.engine.borrow().show_when_ready();
            app.engine.borrow_mut().set_show_when_ready(!value);
        }
        "reduced-motion" => flip(app, "appearance/reducedMotion", false),
        "activity" => {
            let mode = app.engine.borrow().activity_mode();
            app.engine.borrow_mut().set_activity_mode((mode + delta).rem_euclid(3));
        }
        "narration" => {
            let enabled = app.engine.borrow().narrator_enabled();
            app.engine.borrow_mut().set_narrator_enabled(!enabled);
        }
        "tool-explanations" => crate::commands::toggle_explanations(app),
        "tool-detail" => {
            let level = app.engine.borrow().narrator_detail_level();
            app.engine.borrow_mut().set_narrator_detail_level((level + delta).rem_euclid(5));
        }
        "new-agent-on-startup" => flip(app, "launch/newAgentOnStartup", false),
        "anonymous-agents" => flip(app, "launch/anonymousAgents", true),
        "compact-explorer" => {
            flip(app, "explorer/compact", false);
            window.set_explorer_compact(app.engine.borrow().settings().boolean("explorer/compact", false));
            crate::avatar_view::remake();
        }
        "avatar-size" => crate::avatar_view::set(app, window, None, delta),
        "nav-rail" => {
            flip(app, "appearance/navRail", true);
            window.set_nav_rail_visible(app.engine.borrow().settings().boolean("appearance/navRail", true));
        }
        "minimal-ui" => {
            flip(app, "appearance/minimalUi", false);
            let minimal = app.engine.borrow().settings().boolean("appearance/minimalUi", false);
            window.set_minimal_ui(minimal);
        }
        "reading-theme" => {
            let themes: Vec<(String, String)> = clarp_core::reading_theme::themes()
                .iter()
                .map(|t| (t.get("id").and_then(Value::as_str).unwrap_or_default().to_owned(), String::new()))
                .collect();
            let current = app.engine.borrow().reading_theme();
            if let Some(next) = step(&themes, &current, delta) {
                app.engine.borrow_mut().set_reading_theme(&next);
            }
        }
        // The picker shows the font in the chat, so it opens over the chats.
        "font" => {
            crate::font_view::open(app, window);
            return;
        }
        "reset-font" => {
            let theme = app.engine.borrow().reading_theme();
            app.engine.borrow_mut().set_font_override(&theme, None);
        }
        "spoken-replies" => {
            let muted = app.engine.borrow().muted();
            app.engine.borrow_mut().set_muted(!muted);
        }
        "pause-mobile-push" => flip(app, "notifications/pauseMobileWhileDesktopActive", true),
        "shared-filesystem" => {
            let shared = app.engine.borrow().shared_filesystem();
            app.engine.borrow_mut().set_shared_filesystem(!shared);
        }
        "voice-provider" | "voice-fallback" => {
            let (provider, fallback, options) = {
                let engine = app.engine.borrow();
                let tts = engine.tts_provider_status();
                let text = |key: &str| tts.get(key).and_then(Value::as_str).unwrap_or_default().to_owned();
                (text("provider"), text("fallback"), providers(tts, id == "voice-fallback"))
            };
            if id == "voice-provider" {
                if let Some(next) = step(&options, &provider, delta) {
                    app.engine.borrow_mut().set_tts_providers(&next, &fallback, "");
                }
            } else if let Some(next) = step(&options, if fallback.is_empty() { "none" } else { &fallback }, delta) {
                app.engine.borrow_mut().set_tts_providers(&provider, &next, "");
            }
        }
        "connection" | "orchestrator" => {
            if !crate::commands::run(app, window, id) {
                eprintln!("clarp-slint: {id} is not available yet");
            }
        }
        other => eprintln!("clarp-slint: unknown setting {other}"),
    }
    crate::pump_now(app);
    show(app, window);
}
