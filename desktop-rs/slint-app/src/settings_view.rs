//! The settings surface (SettingsPanel.qml): what it lists, and what
//! changing a row does. Preferences go to the Slint app's settings under
//! the Qt controller's keys.

use std::rc::Rc;

use serde_json::Value;
use slint::{Model, ModelRc, VecModel};

use crate::{App, AppWindow, SettingRow};

fn section(label: &str) -> SettingRow {
    SettingRow { kind: "section".into(), id: String::new().into(), label: label.into(), ..SettingRow::default() }
}
fn info(label: &str, detail: &str) -> SettingRow {
    SettingRow { kind: "info".into(), id: String::new().into(), label: label.into(), detail: detail.into(), ..SettingRow::default() }
}

/// A registered setting as a row: its label, description and value.
fn row(app: &App, setting: &crate::catalogue::Setting) -> SettingRow {
    let kind = setting.kind();
    SettingRow {
        kind: kind.into(),
        id: setting.id().into(),
        label: setting.label(app).into(),
        description: setting.entry.description.into(),
        detail: if kind == "toggle" { String::new() } else { setting.value(app) }.into(),
        on: setting.on(app),
    }
}

/// What a section shows below its settings: the Host's status, the keys.
fn information(app: &App, section: &str) -> Vec<SettingRow> {
    let engine = app.engine.borrow();
    let text = |object: &clarp_core::json::Object, key: &str| object.get(key).and_then(Value::as_str).unwrap_or_default().to_owned();
    match section {
        "EXPERIMENTS" => vec![info(engine.narrator_level_description(), &engine.narrator_status())],
        "HOST STATUS" => {
            let diagnostics = engine.diagnostics_health();
            let transcription = engine.transcription_capabilities();
            let queue = diagnostics.get("tts_queue").and_then(Value::as_object).cloned().unwrap_or_default();
            let count = |key: &str| queue.get(key).and_then(Value::as_i64).unwrap_or(0);
            let model = text(transcription, "default_model");
            let ready = diagnostics.get("ready").and_then(Value::as_bool) == Some(true);
            vec![
                info("Diagnostics", if engine.host_status_loading() { "Loading…" } else if ready { "Ready" } else { "Needs attention" }),
                info("Speech to text", if transcription.get("available").and_then(Value::as_bool) == Some(true) { "Available" } else { "Unavailable" }),
                info("Transcription model", if model.is_empty() { "Unknown" } else { &model }),
                info("TTS queue", &format!("{} pending · {} in flight", count("pending"), count("in_flight"))),
            ]
        }
        "KEYBOARD" => {
            // The keys as the user bound them.
            let overrides = crate::commands::overrides_of(&engine);
            let key = |action: &str| crate::keymap::shown("pane", action, &overrides).unwrap_or_default();
            vec![
                info("Key bindings", &key("edit-keymap")),
                info("Command palette", &key("switcher")),
                info("Settings", &key("settings")),
                info("Navigate settings", "↑↓ / Tab · Home / End · / search · Delete default"),
                info("Open native CLI", &key("agent-terminal")),
                info("Start idle contact", &key("new-contact")),
                info("Show / hide sidebar", &key("sidebar")),
                info("Move between panes", "Ctrl+Alt+Arrow"),
                info("Split right / down", "Ctrl+Alt+V / S"),
                info("Zoom / close / balance", "Ctrl+Alt+Z / X / ="),
                info("Queue message", "Ctrl+Enter"),
            ]
        }
        "ABOUT" => vec![
            info("Desktop client", concat!(env!("CARGO_PKG_VERSION"), " preview (Slint)")),
            info("Host version", if engine.server_version().is_empty() { "Unknown" } else { engine.server_version() }),
        ],
        _ => Vec::new(),
    }
}

/// The page: each section's registered settings, then its information.
pub fn rows(app: &App) -> Vec<SettingRow> {
    let settings = crate::catalogue::settings();
    let mut rows = Vec::new();
    for name in crate::catalogue::sections() {
        let mut part: Vec<SettingRow> = settings.iter().filter(|s| s.section == name && s.is_shown(app)).map(|s| row(app, s)).collect();
        part.extend(information(app, name));
        if !part.is_empty() {
            rows.push(section(name));
            rows.extend(part);
        }
    }
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
    // A typo is forgiven while nothing matches as typed.
    let typed = crate::catalogue::as_typed(query);
    if scored.iter().any(|s| s.1 >= typed) {
        scored.retain(|s| s.1 >= typed);
    }
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
    let Some(setting) = crate::catalogue::setting(id) else { return Vec::new() };
    let now = setting.current(app);
    setting
        .options(app)
        .into_iter()
        .map(|option| {
            let current = option.value == now;
            (option.value, option.label, option.description, current)
        })
        .collect()
}

/// Chooses `value` for the choice `id` (the switcher's picker).
pub fn pick(app: &Rc<App>, window: &AppWindow, id: &str, value: &str) {
    match crate::catalogue::setting(id) {
        Some(setting) if setting.apply(app, window, value) => {}
        _ => eprintln!("clarp-slint: {id} has no choice {value}"),
    }
    crate::pump_now(app);
    show(app, window);
}

/// Space/Enter (delta 1) or Left/Right on a row.
pub fn change(app: &Rc<App>, window: &AppWindow, id: &str, delta: i32) {
    let Some(setting) = crate::catalogue::setting(id) else {
        eprintln!("clarp-slint: unknown setting {id}");
        return;
    };
    setting.change(app, window, delta);
    crate::pump_now(app);
    show(app, window);
}

/// Delete on a row: the setting goes back to its default.
pub fn reset(app: &Rc<App>, window: &AppWindow, id: &str) {
    match crate::catalogue::setting(id) {
        Some(setting) if setting.reset(app, window) => {}
        _ => eprintln!("clarp-slint: {id} has no default to go back to"),
    }
    crate::pump_now(app);
    show(app, window);
}
