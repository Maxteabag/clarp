//! The settings surface (SettingsPanel.qml): what it lists, and what
//! changing a row does. Preferences go to the Slint app's settings under
//! the Qt controller's keys.

use std::rc::Rc;

use clarp_engine::Change;
use serde_json::Value;
use slint::{Model, ModelRc, VecModel};

use crate::{App, AppWindow, SettingRow};

fn section(label: &str) -> SettingRow {
    SettingRow { kind: "section".into(), id: String::new().into(), label: label.into(), ..SettingRow::default() }
}
/// A row that does something, named and described by the catalogue.
fn described(kind: &str, id: &str) -> SettingRow {
    debug_assert!(IDS.contains(&id), "{id} is missing from settings_view::IDS");
    let entry = crate::catalogue::find(id);
    SettingRow {
        kind: kind.into(),
        id: id.into(),
        label: entry.map_or(id, |e| e.label).into(),
        description: entry.map_or("", |e| e.description).into(),
        ..SettingRow::default()
    }
}
fn toggle(id: &str, on: bool) -> SettingRow {
    SettingRow { on, ..described("toggle", id) }
}
fn choice(id: &str, detail: &str) -> SettingRow {
    SettingRow { detail: detail.into(), ..described("choice", id) }
}
fn action(id: &str, label: &str, detail: &str) -> SettingRow {
    SettingRow { label: label.into(), detail: detail.into(), ..described("action", id) }
}
fn info(label: &str, detail: &str) -> SettingRow {
    SettingRow { kind: "info".into(), id: String::new().into(), label: label.into(), detail: detail.into(), ..SettingRow::default() }
}

const ACTIVITY: [&str; 3] = ["Grouped", "Always visible", "Group old"];

/// Every row that does something; each has a catalogue entry
/// (`catalogue::tests` holds them to it).
pub const IDS: &[&str] = &[
    "timestamps",
    "show-when-ready",
    "reduced-motion",
    "activity",
    "tool-explanations",
    "narration",
    "tool-detail",
    "new-agent-on-startup",
    "anonymous-agents",
    "nav-rail",
    "explorer",
    "compact-explorer",
    "avatar-size",
    "live-preview",
    "workspace-bar",
    "shortcut-bar",
    "minimal-ui",
    "reading-theme",
    "font",
    "reset-font",
    "ui-scale",
    "spoken-replies",
    "voice-provider",
    "voice-fallback",
    "pause-mobile-push",
    "double-press",
    "connection",
    "orchestrator",
    "shared-filesystem",
];

/// The interface sizes offered, as scale factors (1.15 is the usual).
const SCALES: [f64; 9] = [1.0, 1.05, 1.1, 1.15, 1.2, 1.25, 1.3, 1.35, 1.4];
/// The double-press windows offered, in milliseconds.
const DOUBLE_PRESS: [u64; 8] = [150, 200, 250, 300, 400, 500, 700, 1000];

fn ui_scale(app: &App) -> f64 {
    app.engine.borrow().settings().get("appearance/uiScale").and_then(Value::as_f64).unwrap_or(1.15)
}

fn percent(scale: f64) -> String {
    format!("{}%", (scale * 100.0).round())
}

fn double_press(app: &App) -> u64 {
    crate::keymap::double_press_window(app.engine.borrow().settings().get("keymap/doublePressMs").and_then(Value::as_i64)).as_millis() as u64
}

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
        toggle("timestamps", prefs.timestamps),
        toggle("show-when-ready", engine.show_when_ready()),
        toggle("reduced-motion", settings.boolean("appearance/reducedMotion", false)),
        choice("activity", ACTIVITY[engine.activity_mode().clamp(0, 2) as usize]),
    ];
    // The Host's setting, on Hosts that send live items (§6).
    if engine.tool_explanation_setting() {
        rows.push(toggle("tool-explanations", engine.tool_explanations_enabled(engine.selected_session())));
    }
    rows.extend(vec![
        section("EXPERIMENTS"),
        toggle("narration", engine.narrator_enabled()),
        choice("tool-detail", clarp_engine::Engine::narrator_detail_levels()[engine.narrator_detail_level().clamp(0, 4) as usize]),
        info(engine.narrator_level_description(), &engine.narrator_status()),
        section("STARTUP"),
        toggle("new-agent-on-startup", settings.boolean("launch/newAgentOnStartup", false)),
        section("AGENT IDENTITY"),
        toggle("anonymous-agents", settings.boolean("launch/anonymousAgents", true)),
        section("APPEARANCE"),
        toggle("nav-rail", settings.boolean("appearance/navRail", true)),
        toggle("explorer", app.window.upgrade().is_some_and(|w| w.get_sidebar_visible())),
        toggle("compact-explorer", settings.boolean("explorer/compact", false)),
        choice("avatar-size", &crate::avatar_view::label(app)),
        toggle("live-preview", settings.boolean("explorer/livePreview", false)),
        toggle("workspace-bar", prefs.workspace_bar),
        toggle("shortcut-bar", settings.boolean("appearance/shortcutsVisible", true)),
        toggle("minimal-ui", settings.boolean("appearance/minimalUi", false)),
        choice("reading-theme", &format!("{} · {}", text(theme, "label"), crate::view::theme_font(theme))),
        action("font", "Font", &font_detail),
        action("reset-font", "Reset font to theme default", &reset_detail),
        choice("ui-scale", &percent(ui_scale(app))),
        section("VOICE & AUDIO"),
        toggle("spoken-replies", !engine.muted()),
        choice("voice-provider", &label_of(&provider, false)),
        choice("voice-fallback", &label_of(if fallback.is_empty() { "none" } else { &fallback }, true)),
        section("NOTIFICATIONS"),
        toggle("pause-mobile-push", settings.boolean("notifications/pauseMobileWhileDesktopActive", true)),
        section("HOST"),
        action("connection", if engine.server_name().is_empty() { "Clarp Host" } else { engine.server_name() }, &format!("{}  ·  {}", engine.base_url(), engine.connection_state())),
        action("orchestrator", "Orchestrator", "Open"),
        toggle("shared-filesystem", engine.shared_filesystem()),
        section("HOST STATUS"),
        info("Diagnostics", if loading { "Loading…" } else if diagnostics.get("ready").and_then(Value::as_bool) == Some(true) { "Ready" } else { "Needs attention" }),
        info("Speech to text", if transcription.get("available").and_then(Value::as_bool) == Some(true) { "Available" } else { "Unavailable" }),
        info("Transcription model", &{
            let model = text(transcription, "default_model");
            if model.is_empty() { "Unknown".to_owned() } else { model }
        }),
        info("TTS queue", &format!("{} pending · {} in flight", count("pending"), count("in_flight"))),
        section("KEYBOARD"),
        choice("double-press", &format!("{} ms", double_press(app))),
        info("Key bindings", &key("edit-keymap")),
        info("Command palette", &key("switcher")),
        info("Settings", &key("settings")),
        info("Navigate settings", "↑↓ / Tab · Home / End · / search"),
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

/// The rows matching the search, best first and without section titles;
/// all of them when there is none. The switcher's matcher ranks them.
pub fn filter(rows: Vec<SettingRow>, query: &str) -> Vec<SettingRow> {
    if query.trim().is_empty() {
        return rows;
    }
    // Settings before the information rows (the keys listed under KEYBOARD).
    let mut scored: Vec<(bool, u32, usize, SettingRow)> = rows
        .into_iter()
        .filter(|row| row.kind != "section")
        .filter_map(|row| {
            let aliases = crate::catalogue::find(&row.id).map_or(&[][..], |e| e.aliases);
            let words = format!("{} {}", row.description, row.detail);
            let score = crate::catalogue::score(query, &row.label, aliases, &words)?;
            Some((actionable(&row), score, crate::catalogue::order(&row.id), row))
        })
        .collect();
    scored.sort_by(|a, b| b.0.cmp(&a.0).then(b.1.cmp(&a.1)).then(a.2.cmp(&b.2)));
    scored.into_iter().map(|(.., row)| row).collect()
}

fn actionable(row: &SettingRow) -> bool {
    row.kind != "section" && row.kind != "info"
}

/// Shows the rows again (a setting, the search or the Host status
/// changed), the current row kept by its id.
pub fn show(app: &App, window: &AppWindow) {
    if window.get_surface() != "settings" {
        return;
    }
    let before = window.get_setting_rows().row_data(window.get_setting_current().max(0) as usize).map(|r| r.id.to_string()).unwrap_or_default();
    let rows = filter(rows(app), &window.get_settings_query());
    let current = rows
        .iter()
        .position(|r| !before.is_empty() && r.id == before.as_str())
        .or_else(|| rows.iter().position(actionable))
        .map_or(-1, |i| i as i32);
    window.set_setting_rows(ModelRc::new(VecModel::from(rows)));
    window.set_setting_current(current);
}

/// Opens the surface on its first actionable row and asks the Host how it is.
pub fn open(app: &App, window: &AppWindow) {
    window.set_surface("settings".into());
    window.set_settings_query("".into());
    app.engine.borrow_mut().load_host_status();
    show(app, window);
    window.invoke_focus_settings();
}

/// The search typed (`/` from the list).
pub fn searched(app: &App, window: &AppWindow, query: &str) {
    window.set_settings_query(query.into());
    window.set_setting_current(-1);
    show(app, window);
}

/// Down or Enter in the search: the keyboard goes to the best match.
pub fn to_results(app: &App, window: &AppWindow) {
    window.set_setting_current(-1);
    show(app, window);
    window.invoke_focus_settings();
}

/// Escape in the search: it empties and the whole page comes back.
pub fn cancel_search(app: &App, window: &AppWindow) {
    window.set_settings_query("".into());
    show(app, window);
    window.invoke_focus_settings();
}

/// A choice's options: `(value, label, description, current)`.
pub fn choices(app: &App, id: &str) -> Vec<(String, String, String, bool)> {
    let option = |value: String, label: String, description: String, current: bool| (value, label, description, current);
    let engine = app.engine.borrow();
    match id {
        "activity" => {
            let mode = engine.activity_mode();
            let notes = ["Tool calls fold into one row per turn.", "Every tool call shows as its own row.", "Recent tool calls show; older ones fold."];
            ACTIVITY.iter().zip(notes).enumerate().map(|(i, (label, note))| option(i.to_string(), (*label).into(), note.into(), mode == i as i32)).collect()
        }
        "tool-detail" => {
            let level = engine.narrator_detail_level();
            clarp_engine::Engine::narrator_detail_levels()
                .iter()
                .enumerate()
                .map(|(i, name)| option(i.to_string(), (*name).into(), if i == 0 { "No AI" } else { "Uses AI" }.into(), level == i as i32))
                .collect()
        }
        "reading-theme" => {
            let current = engine.reading_theme();
            clarp_core::reading_theme::themes()
                .iter()
                .map(|theme| {
                    let text = |key: &str| theme.get(key).and_then(Value::as_str).unwrap_or_default().to_owned();
                    option(text("id"), format!("{} · {}", text("label"), crate::view::theme_font(theme)), text("detail"), text("id") == current)
                })
                .collect()
        }
        "voice-provider" | "voice-fallback" => {
            let tts = engine.tts_provider_status();
            let text = |key: &str| tts.get(key).and_then(Value::as_str).unwrap_or_default().to_owned();
            let current = if id == "voice-provider" { text("provider") } else { Some(text("fallback")).filter(|f| !f.is_empty()).unwrap_or_else(|| "none".into()) };
            providers(tts, id == "voice-fallback").into_iter().map(|(value, label)| option(value.clone(), label, String::new(), value == current)).collect()
        }
        "avatar-size" => {
            drop(engine);
            let now = crate::avatar_view::current(app);
            crate::avatar_view::CHOICES
                .iter()
                .map(|(id, label, sizes)| option((*id).into(), format!("{label} · {} px", sizes.row), format!("{} px in the explorer, {} px compact", sizes.row, sizes.compact), *id == now))
                .collect()
        }
        "ui-scale" => {
            drop(engine);
            let now = ui_scale(app);
            SCALES
                .iter()
                .map(|scale| option(format!("{scale:.2}"), percent(*scale), if (*scale - 1.15).abs() < 0.001 { "The usual size" } else { "" }.into(), (scale - now).abs() < 0.001))
                .collect()
        }
        "double-press" => {
            drop(engine);
            let now = double_press(app);
            DOUBLE_PRESS.iter().map(|ms| option(ms.to_string(), format!("{ms} ms"), if *ms == 300 { "The usual" } else { "" }.into(), *ms == now)).collect()
        }
        _ => Vec::new(),
    }
}

/// Chooses `value` for the choice `id` (the switcher's picker, Left/Right).
pub fn pick(app: &Rc<App>, window: &AppWindow, id: &str, value: &str) {
    match id {
        "activity" => {
            if let Ok(mode) = value.parse() {
                app.engine.borrow_mut().set_activity_mode(mode);
            }
        }
        "tool-detail" => {
            if let Ok(level) = value.parse() {
                app.engine.borrow_mut().set_narrator_detail_level(level);
            }
        }
        "reading-theme" => {
            app.engine.borrow_mut().set_reading_theme(value);
        }
        "voice-provider" | "voice-fallback" => {
            let (provider, fallback) = {
                let engine = app.engine.borrow();
                let tts = engine.tts_provider_status();
                let text = |key: &str| tts.get(key).and_then(Value::as_str).unwrap_or_default().to_owned();
                (text("provider"), text("fallback"))
            };
            if id == "voice-provider" {
                app.engine.borrow_mut().set_tts_providers(value, &fallback, "");
            } else {
                app.engine.borrow_mut().set_tts_providers(&provider, value, "");
            }
        }
        "avatar-size" => crate::avatar_view::set(app, window, Some(value), 0),
        "ui-scale" => match value.parse::<f64>() {
            Ok(scale) => {
                app.engine.borrow_mut().settings_mut().set("appearance/uiScale", scale);
                crate::platform::desktop::set_ui_scale(scale as f32);
            }
            Err(error) => eprintln!("clarp-slint: not an interface size {value}: {error}"),
        },
        "double-press" => match value.parse::<i64>() {
            Ok(ms) => {
                app.engine.borrow_mut().settings_mut().set("keymap/doublePressMs", ms);
            }
            Err(error) => eprintln!("clarp-slint: not a double-press window {value}: {error}"),
        },
        other => eprintln!("clarp-slint: {other} has no choices"),
    }
    crate::pump_now(app);
    show(app, window);
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
    // A choice steps through its options.
    let options = choices(app, id);
    if !options.is_empty() {
        let values: Vec<(String, String)> = options.iter().map(|(value, ..)| (value.clone(), String::new())).collect();
        let current = options.iter().find(|o| o.3).map(|o| o.0.clone()).unwrap_or_default();
        if let Some(next) = step(&values, &current, delta) {
            pick(app, window, id, &next);
        }
        return;
    }
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
        "narration" => {
            let enabled = app.engine.borrow().narrator_enabled();
            app.engine.borrow_mut().set_narrator_enabled(!enabled);
        }
        "tool-explanations" => crate::commands::toggle_explanations(app),
        "new-agent-on-startup" => flip(app, "launch/newAgentOnStartup", false),
        "anonymous-agents" => flip(app, "launch/anonymousAgents", true),
        "compact-explorer" => {
            flip(app, "explorer/compact", false);
            window.set_explorer_compact(app.engine.borrow().settings().boolean("explorer/compact", false));
            crate::avatar_view::remake();
        }
        "explorer" | "shortcut-bar" => {
            if !crate::commands::run(app, window, if id == "explorer" { "sidebar" } else { "shortcut-bar" }) {
                eprintln!("clarp-slint: {id} could not change");
            }
        }
        "live-preview" => flip(app, "explorer/livePreview", false),
        "nav-rail" => {
            flip(app, "appearance/navRail", true);
            window.set_nav_rail_visible(app.engine.borrow().settings().boolean("appearance/navRail", true));
        }
        "minimal-ui" => {
            flip(app, "appearance/minimalUi", false);
            let minimal = app.engine.borrow().settings().boolean("appearance/minimalUi", false);
            window.set_minimal_ui(minimal);
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
