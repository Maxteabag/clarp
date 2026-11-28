//! The settings surface (SettingsPanel.qml): what it lists, and what
//! changing a row does. Preferences go to the Slint app's settings under
//! the Qt controller's keys.

use std::cell::RefCell;
use std::rc::Rc;
use std::time::Duration;

use clarp_core::tr;
use serde_json::Value;
use slint::{Model, ModelRc, VecModel};

use crate::{App, AppWindow, SettingRow};

/// A section's title row; `name` is the section's English name, shown in the app's language.
fn section(name: &str) -> SettingRow {
    SettingRow { kind: "section".into(), id: String::new().into(), label: crate::catalogue::section_title(name).into(), ..SettingRow::default() }
}
fn info(label: &str, detail: &str) -> SettingRow {
    SettingRow { kind: "info".into(), id: String::new().into(), label: label.into(), detail: detail.into(), ..SettingRow::default() }
}

/// A registered setting as a row: its label, description and value.
fn row(app: &App, setting: &crate::catalogue::Setting) -> SettingRow {
    let kind = setting.kind();
    let row = SettingRow {
        kind: kind.into(),
        id: setting.id().into(),
        label: setting.label(app).into(),
        description: setting.description(app).into(),
        detail: if kind == "toggle" { String::new() } else { setting.value(app) }.into(),
        on: setting.on(app),
        ..SettingRow::default()
    };
    match crate::look::pref_of(setting.id()) {
        Some(spec) => with_pref(app, row, spec),
        None => row,
    }
}

/// A preference's row also shows its `:set` name and file key, whether it
/// differs from its default, a colour's swatch and the readability rule a
/// chosen colour breaks.
fn with_pref(app: &App, row: SettingRow, spec: &clarp_core::prefs::Spec) -> SettingRow {
    use clarp_core::prefs::{self, Kind, Scope};
    let engine = app.engine.borrow();
    let settings = engine.settings();
    let theme = prefs::theme_of(settings);
    let value = prefs::current(settings, spec, &theme);
    let file_key = match spec.scope {
        Scope::Global => spec.key.to_owned(),
        Scope::Theme { field } => format!("{}.{theme}.{field}", spec.key),
    };
    let changed = !prefs::is_default(settings, spec, &theme);
    let (kind, warning) = match spec.kind {
        Kind::Color { role } => ("color".to_owned(), if changed { prefs::readability_warnings(settings, &theme, role).into_iter().next().unwrap_or_default() } else { String::new() }),
        Kind::Text { .. } => ("text".to_owned(), String::new()),
        _ => (row.kind.to_string(), String::new()),
    };
    SettingRow {
        kind: kind.into(),
        key: format!("{} · {file_key}", spec.name).into(),
        changed,
        swatch: value.as_str().and_then(crate::view::color).unwrap_or_default(),
        warning: warning.into(),
        ..row
    }
}

/// What a section shows below its settings: the Host's status, the keys.
fn information(app: &App, section: &str) -> Vec<SettingRow> {
    let engine = app.engine.borrow();
    let text = |object: &clarp_core::json::Object, key: &str| object.get(key).and_then(Value::as_str).unwrap_or_default().to_owned();
    match section {
        "EXPERIMENTS" => vec![info(&narrator_level_description(engine.narrator_level_description()), &engine.narrator_status())],
        "HOST STATUS" => {
            let diagnostics = engine.diagnostics_health();
            let transcription = engine.transcription_capabilities();
            let queue = diagnostics.get("tts_queue").and_then(Value::as_object).cloned().unwrap_or_default();
            let count = |key: &str| queue.get(key).and_then(Value::as_i64).unwrap_or(0);
            let model = text(transcription, "default_model");
            let ready = diagnostics.get("ready").and_then(Value::as_bool) == Some(true);
            let diagnostics_state = if engine.host_status_loading() {
                // i18n: Settings > Host status, the Diagnostics row's value while the Host's answer is loading
                tr!("Loading…")
            } else if ready {
                // i18n: Settings > Host status, the Diagnostics row's value (adjective): the Host is ready
                tr!("Ready")
            } else {
                // i18n: Settings > Host status, the Diagnostics row's value: something on the Host is wrong
                tr!("Needs attention")
            };
            let speech = if transcription.get("available").and_then(Value::as_bool) == Some(true) {
                // i18n: Settings > Host status, the Speech to text row's value (adjective): the Host can transcribe speech
                tr!("Available")
            } else {
                // i18n: Settings > Host status, the Speech to text row's value (adjective): the Host cannot transcribe speech
                tr!("Unavailable")
            };
            // i18n: Settings > Host status, a row's value when the Host has not said (e.g. its transcription model)
            let model = if model.is_empty() { tr!("Unknown") } else { model };
            vec![
                // i18n: Settings > Host status, information row label (noun): the Host's health checks
                info(&tr!("Diagnostics"), &diagnostics_state),
                // i18n: Settings > Host status, information row label (noun): the Host's speech recognition
                info(&tr!("Speech to text"), &speech),
                // i18n: Settings > Host status, information row label (noun): the model the Host transcribes speech with
                info(&tr!("Transcription model"), &model),
                info(
                    // i18n: Settings > Host status, information row label (noun): the Host's queue of text-to-speech jobs; TTS is text to speech
                    &tr!("TTS queue"),
                    // i18n: Settings > Host status, the TTS queue row's value; {0} jobs waiting, {1} jobs being spoken now
                    &tr!("{0} pending · {1} in flight", count("pending"), count("in_flight")),
                ),
            ]
        }
        "KEYBOARD" => {
            // The keys as the user bound them.
            let overrides = crate::commands::overrides_of(&engine);
            let key = |action: &str| crate::keymap::shown("pane", action, &overrides).unwrap_or_default();
            vec![
                // i18n: Settings > Keyboard, information row label (noun): the key that opens the key-binding editor; the value is the key
                info(&tr!("Key bindings"), &key("edit-keymap")),
                // i18n: Settings > Keyboard, information row label (noun): the key that opens the command palette (Ctrl+K); the value is the key
                info(&tr!("Command palette"), &key("switcher")),
                // i18n: Settings > Keyboard, information row label (noun): the key that opens Settings; the value is the key
                info(&tr!("Settings"), &key("settings")),
                info(
                    // i18n: Settings > Keyboard, information row label (an action): the keys that move around the settings page
                    &tr!("Navigate settings"),
                    // i18n: Settings > Keyboard, the keys that move around the settings page; key names stay as they are, "search" and "default" say what / and Delete do
                    &tr!("↑↓ / Tab · Home / End · / search · Delete default"),
                ),
                // i18n: Settings > Keyboard, information row label (an action); the value is the key that opens the agent's CLI in a terminal
                info(&tr!("Open agent terminal"), &key("agent-terminal")),
                // i18n: Settings > Keyboard, information row label (an action); the value is the key that starts one of your idle contacts
                info(&tr!("Start idle contact"), &key("new-contact")),
                // i18n: Settings > Keyboard, information row label (an action); the value is the key that shows or hides the sidebar
                info(&tr!("Show / hide sidebar"), &key("sidebar")),
                // i18n: Settings > Keyboard, information row label (an action); the value is the keys that move between panes
                info(&tr!("Move between panes"), "Ctrl+Alt+Arrow"),
                // i18n: Settings > Keyboard, information row label (actions); the value is the keys that split a pane to the right or down
                info(&tr!("Split right / down"), "Ctrl+Alt+V / S"),
                // i18n: Settings > Keyboard, information row label (actions: zoom a pane, close it, make panes the same size); the value is the keys
                info(&tr!("Zoom / close / balance"), "Ctrl+Alt+Z / X / ="),
                // i18n: Settings > Keyboard, information row label (an action, verb): the key that queues a message to send after the running turn
                info(&tr!("Queue message"), "Ctrl+Enter"),
            ]
        }
        crate::catalogue::lanes::SECTION => {
            crate::catalogue::lanes::information(app).iter().map(|(label, detail)| info(label, detail)).collect()
        }
        "ABOUT" => vec![
            info(
                // i18n: Settings > About, information row label (noun): this desktop app; the value is its version
                &tr!("Desktop client"),
                // i18n: Settings > About, the desktop app's version; {} is the version number, Slint is the UI toolkit's name
                &tr!("{} preview (Slint)", env!("CARGO_PKG_VERSION")),
            ),
            info(
                // i18n: Settings > About, information row label (noun): the version of the Clarp Host this window talks to
                &tr!("Host version"),
                // i18n: Settings > About, the Host version row's value when the Host has not said its version
                &if engine.server_version().is_empty() { tr!("Unknown") } else { engine.server_version().to_owned() },
            ),
        ],
        _ => Vec::new(),
    }
}

/// The tool-detail level's description in the app's language
/// (`clarp_core::narrator` keeps it in English).
fn narrator_level_description(english: &str) -> String {
    match english {
        // i18n: Settings > Experiments, the line describing the Tool detail level "Developer"
        "Original tool calls." => tr!("Original tool calls."),
        // i18n: Settings > Experiments, the line describing the Tool detail level "Technical"
        "Actions with commands and paths." => tr!("Actions with commands and paths."),
        // i18n: Settings > Experiments, the line describing the Tool detail level "Balanced"
        "Actions with key technical details." => tr!("Actions with key technical details."),
        // i18n: Settings > Experiments, the line describing the Tool detail level "Plain English"
        "Everyday language, no code or paths." => tr!("Everyday language, no code or paths."),
        // i18n: Settings > Experiments, the line describing the Tool detail level "Grandma"
        "Simple explanations, no technical terms." => tr!("Simple explanations, no technical terms."),
        other => crate::catalogue::local(other),
    }
}

/// The page: each section's registered settings, then its information.
pub fn rows(app: &App) -> Vec<SettingRow> {
    let settings = crate::catalogue::settings();
    let mut rows = Vec::new();
    // The settings file's problems, an export's or import's result.
    let notices = crate::look::notices();
    if !notices.is_empty() {
        rows.push(section("SETTINGS FILE"));
        rows.extend(notices.iter().map(|line| info(line, "")));
    }
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
    // A line under a group (indented: Kept processes' per-tool lines) is
    // found by its group's title too, so "cold" shows the group's lines,
    // not its title row alone.
    let mut group = String::new();
    let mut scored: Vec<(bool, u32, usize, SettingRow)> = rows
        .into_iter()
        .filter(|row| row.kind != "section")
        .filter_map(|row| {
            let line = row.kind == "info" && row.label.starts_with(crate::catalogue::lanes::TOOL_INDENT);
            if !line {
                group = if row.kind == "info" { row.label.to_string() } else { String::new() };
            }
            let entry = crate::catalogue::find(&row.id);
            let aliases = entry.map_or(&[][..], |e| e.aliases);
            let words = if line { format!("{} {} {group}", row.description, row.detail) } else { format!("{} {}", row.description, row.detail) };
            // Found by its words in the app's language, and by its English ones too.
            let english = entry.filter(|_| clarp_core::i18n::current() != "en").and_then(|e| crate::catalogue::score(query, e.label, e.aliases, &format!("{} {}", e.description, row.detail)));
            let score = crate::catalogue::score(query, &row.label, aliases, &words).max(english)?;
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
    app.engine.borrow_mut().load_lane_settings();
    show(app, window);
    window.invoke_focus_settings();
    watch_pressure();
}

thread_local! {
    static PRESSURE_READ: RefCell<Option<slint::Timer>> = const { RefCell::new(None) };
}

/// How often the page reads the live lanes and cold starts again while it is
/// open: the event never carries them.
const PRESSURE_EVERY: Duration = Duration::from_secs(15);

/// While the page is open on a Host that says which processes live
/// (`lanes_live`), reads them again now and then; stops once the page is
/// left.
fn watch_pressure() {
    let timer = slint::Timer::default();
    timer.start(slint::TimerMode::Repeated, PRESSURE_EVERY, || {
        let (Some(app), Some(window)) = (crate::app(), crate::window()) else { return };
        if window.get_surface() != "settings" {
            PRESSURE_READ.with(|t| t.borrow_mut().take());
            return;
        }
        if app.engine.borrow().lane_live_offered() {
            app.engine.borrow_mut().load_lane_settings();
            crate::pump_now(&app);
        }
    });
    PRESSURE_READ.with(|t| *t.borrow_mut() = Some(timer));
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
    match (setting.kind(), crate::look::pref_of(id), delta) {
        // Enter on a number: type it in the value editor.
        ("number", Some(spec), 0) => crate::look::open_editor(app, window, spec.name),
        // Enter: a switch flips, a choice steps, an action runs.
        (_, _, 0) => setting.change(app, window, 1),
        // Left/Right (a number steps from where it is).
        _ => setting.change(app, window, delta),
    }
    crate::pump_now(app);
    show(app, window);
}

/// Delete on a row: the setting goes back to its default.
pub fn reset(app: &Rc<App>, window: &AppWindow, id: &str, whole_section: bool) {
    let Some(setting) = crate::catalogue::setting(id) else { return };
    if whole_section {
        crate::catalogue::reset_section(app, window, setting.section);
    } else if !setting.reset(app, window) {
        eprintln!("clarp-slint: {id} has no default to go back to");
    }
    crate::pump_now(app);
    show(app, window);
}
