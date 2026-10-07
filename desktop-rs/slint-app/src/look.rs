//! Every preference (`clarp_core::prefs`) applied to the window: the `Look`
//! global (typography and layout), the palette's colour overrides and font
//! size, the bars, the clock, folds and behaviour. Also what the `pref:`
//! actions do (Ctrl+K, Settings, `:set`), the value editor and colour
//! picker, export and import, editing the settings file and reloading it
//! when it changes on disk.

use std::cell::{Cell, RefCell};
use std::rc::Rc;

use clarp_core::prefs::{self, Kind, Spec};
use clarp_engine::Change;
use serde_json::Value;
use slint::{ComponentHandle, ModelRc, SharedString, VecModel};

use crate::{App, AppWindow, Look, Palette, PrefEditor, Swatch};

pub const EDITOR: &str = "pref-editor";
pub const CONFIRM: &str = "pref-confirm";

/// What the value editor is editing, and the value to put back on Escape.
#[derive(Debug, Clone)]
struct Editing {
    /// A setting's name, or "import" for the import path.
    name: String,
    theme: String,
    before: Option<Value>,
}

thread_local! {
    static EDITING: RefCell<Option<Editing>> = const { RefCell::new(None) };
    /// What changes a row's height, as last applied: when it changes, the
    /// chat measures its rows again.
    static SHAPE: RefCell<String> = const { RefCell::new(String::new()) };
    /// The settings file's last change seen (its mtime and length).
    static SEEN: RefCell<Option<(std::time::SystemTime, u64)>> = const { RefCell::new(None) };
    static WATCH: RefCell<Option<slint::Timer>> = const { RefCell::new(None) };
    /// The settings file's problems, and the last export/import result.
    static NOTICES: RefCell<Vec<String>> = const { RefCell::new(Vec::new()) };
    /// An action the confirmation is for, and whether it was confirmed.
    static CONFIRMING: RefCell<String> = const { RefCell::new(String::new()) };
    static CONFIRMED: Cell<bool> = const { Cell::new(false) };
    /// The settings file could not be read the last time it changed.
    static BROKEN: Cell<bool> = const { Cell::new(false) };
    /// The interface scale last applied.
    static SCALE: Cell<f32> = const { Cell::new(0.0) };
    /// Clears the zoom's "120%" a moment after the last change.
    static ZOOM_NOTE: RefCell<Option<slint::Timer>> = const { RefCell::new(None) };
}

/// The settings file's problems and the last export or import, for the
/// Settings view.
pub fn notices() -> Vec<String> {
    NOTICES.with(|n| n.borrow().clone())
}

fn notice(lines: Vec<String>) {
    for line in &lines {
        eprintln!("clarp-slint: settings: {line}");
    }
    NOTICES.with(|n| *n.borrow_mut() = lines);
}

fn text_of(settings: &clarp_core::settings::Settings, name: &str) -> String {
    prefs::find(name).map(|spec| prefs::current(settings, spec, &prefs::theme_of(settings))).and_then(|v| v.as_str().map(str::to_owned)).unwrap_or_default()
}

/// The theme's colours with the user's overrides, and the font size, over
/// what `view::apply_theme` just set (it calls this).
pub fn over_theme(window: &AppWindow, theme: &str) {
    let Some(app) = crate::app() else { return };
    let Ok(engine) = app.engine.try_borrow() else { return };
    let settings = engine.settings();
    let palette = window.global::<Palette>();
    for spec in prefs::specs() {
        let Kind::Color { role } = spec.kind else { continue };
        let Some(color) = prefs::stored(settings, spec, theme).and_then(|v| prefs::validate(spec, &v).ok()).and_then(|v| v.as_str().and_then(crate::view::color)) else {
            continue;
        };
        match role {
            "window" => palette.set_window(color),
            "raised" => palette.set_raised(color),
            "sunken" => palette.set_sunken(color),
            "control" => palette.set_control(color),
            "hover" => palette.set_hover(color),
            "border" => palette.set_border(color),
            "rule" => palette.set_rule(color),
            "text" => palette.set_text(color),
            "chromeText" => palette.set_chrome_text(color),
            "mutedText" => palette.set_muted(color),
            "faintText" => palette.set_faint(color),
            "accent" => palette.set_accent(color),
            "focus" => palette.set_focus(color),
            "accentText" => palette.set_accent_text(color),
            "bubble" => palette.set_bubble(color),
            "success" => palette.set_success(color),
            "warning" => palette.set_warning(color),
            "danger" => palette.set_danger(color),
            "dangerSurface" => palette.set_danger_surface(color),
            "link" => palette.set_link(color),
            other => eprintln!("clarp-slint: no palette colour {other}"),
        }
    }
    // A focus colour of its own only when chosen; else the accent override
    // carries it, as the theme's accent does.
    let focus = prefs::find("color.focus").and_then(|s| prefs::stored(settings, s, theme));
    if focus.is_none()
        && reading_has_no_focus(theme)
        && let Some(accent) = prefs::find("color.accent").and_then(|s| prefs::stored(settings, s, theme)).and_then(|v| v.as_str().and_then(crate::view::color))
    {
        palette.set_focus(accent);
    }
    // The font size is the font picker's override (apply_theme reads it);
    // a code font of one's own replaces the one the theme's font gives.
    if let Some(code) = prefs::find("codefont").and_then(|s| prefs::stored(settings, s, theme)).and_then(|v| v.as_str().map(str::to_owned)).filter(|c| !c.trim().is_empty()) {
        palette.set_code_family(code.trim().into());
    }
}

fn reading_has_no_focus(theme: &str) -> bool {
    clarp_core::reading_theme::theme(theme).get("focus").is_none()
}

/// Applies every preference to the window.
pub fn apply(app: &App, window: &AppWindow) {
    let Ok(engine) = app.engine.try_borrow() else {
        eprintln!("clarp-slint: settings: the engine is busy; applying later");
        return;
    };
    let settings = engine.settings();
    let n = |name: &str| prefs::number_of(settings, name) as f32;
    let flag = |name: &str| prefs::flag(settings, name);
    let choice = |name: &str| prefs::choice_of(settings, name);
    let look = window.global::<Look>();
    look.set_unit(n("chromesize") / 100.0);
    let code = text_of(settings, "codefont");
    look.set_code_scale(n("codesize") / 100.0);
    look.set_paragraph_spacing(n("paragraphspacing"));
    look.set_heading_scale(n("headingscale") / 100.0);
    // Characters to pixels: about 0.6 em a character in a monospace face,
    // 0.5 in a proportional one (as readability.rs measures).
    let body = window.global::<Palette>().get_body_size();
    let family = window.global::<Palette>().get_body_family().to_string();
    let advance = if family.contains("Mono") || family == "monospace" { 0.6 } else { 0.5 };
    look.set_measure(n("measure") * body * advance);
    look.set_weight(choice("fontweight").parse().unwrap_or(400));
    look.set_density_pad(match choice("density").as_str() {
        "compact" => -4.0,
        "spacious" => 5.0,
        _ => 0.0,
    });
    look.set_explorer_width(n("explorerwidth"));
    look.set_bubble_share(n("bubblewidth") / 100.0);
    look.set_message_spacing(n("messagespacing"));
    look.set_pane_gap(n("panegap"));
    look.set_border_width(n("borderwidth"));
    look.set_focus_border_width(n("focusborderwidth"));
    look.set_radius(n("radius"));
    look.set_scrollbar_width(n("scrollbarwidth"));
    look.set_scrollbar(choice("scrollbar").into());
    look.set_follow(flag("follow"));
    look.set_line_step(40.0 * n("scrollspeed") / 100.0);
    look.set_zoom(n("chatzoom") / 100.0);
    // A row's height changed: the chats measure their rows again (without
    // rebuilding them, so nobody's place in a chat moves). Interface size
    // is not one: the chat's rows follow the reading size (ChatText).
    let shape = ["codesize", "paragraphspacing", "headingscale", "measure", "bubblewidth", "messagespacing"]
        .iter()
        .map(|name| n(*name).to_string())
        .chain([code, choice("fontweight"), body.to_string(), family])
        .collect::<Vec<_>>()
        .join("/");
    let reshaped = SHAPE.with(|s| {
        let changed = *s.borrow() != shape;
        *s.borrow_mut() = shape;
        changed
    });
    if reshaped {
        look.set_generation(look.get_generation().wrapping_add(1));
    }
    window.set_minimal_ui(flag("minimalui"));
    window.set_nav_rail_visible(flag("navrail"));
    window.set_explorer_compact(flag("compactexplorer"));
    window.set_shortcuts_visible(flag("shortcutbar"));
    {
        let mut kept = app.prefs.borrow_mut();
        kept.timestamps = flag("timestamps");
        kept.workspace_bar = flag("workspacebar");
    }
    clarp_core::time_format::set_clock_24h(choice("timeformat") == "24h");
    crate::view::set_prompts_folded(flag("foldprompts"));
}

/// After a setting changed: the engine's own copies, the window, and the
/// rows that show it.
fn changed(app: &Rc<App>, window: &AppWindow, spec: &Spec) {
    app.engine.borrow_mut().reload_preferences();
    apply(app, window);
    // What a row's text or folds depend on: the chats are drawn again.
    if ["timestamps", "timeformat", "foldprompts"].contains(&spec.name) {
        app.rebuild_transcripts();
    }
    if spec.name == "uiscale" {
        rescale(app);
    }
    if spec.name == "chatzoom" {
        note_zoom(window, prefs::number_of(app.engine.borrow().settings(), "chatzoom"));
    }
    // Portraits are made at their device size.
    if spec.name == "avatarsize" || spec.name == "compactexplorer" {
        crate::avatar_view::apply(app, window);
        crate::avatar_view::remake();
    }
    if spec.name == "readingtheme" {
        crate::view::apply_app_theme(app, window);
    }
    app.refresh(&[Change::Panes, Change::Preferences]);
    crate::pump_now(app);
    refresh_views(app, window);
}

/// Shows the chat zoom ("120%") for a moment.
fn note_zoom(window: &AppWindow, percent: f64) {
    window.global::<Look>().set_zoom_note(format!("{percent:.0}%").into());
    let timer = slint::Timer::default();
    timer.start(slint::TimerMode::SingleShot, std::time::Duration::from_millis(1200), || {
        if let Some(window) = crate::window() {
            window.global::<Look>().set_zoom_note("".into());
        }
    });
    ZOOM_NOTE.with(|n| *n.borrow_mut() = Some(timer));
}

/// The interface scale, when it is not the one applied (the window starts
/// on the saved one; checks draw at 1.0 until a check changes it).
fn rescale(app: &App) {
    let scale = prefs::number_of(app.engine.borrow().settings(), "uiscale") as f32;
    if SCALE.with(|s| s.replace(scale)) != scale {
        crate::platform::desktop::set_ui_scale(scale);
    }
}

fn refresh_views(app: &Rc<App>, window: &AppWindow) {
    crate::settings_view::show(app, window);
    crate::commands::refresh_switcher(app, window);
    crate::commands::show_hints(app, window);
}

/// One preference action: `pref-set:NAME=VALUE` (`:set`), `pref-reset-all`,
/// `pref-edit-file`, `pref-export`, `pref-import`, and the chat's zoom
/// (`chat-zoom-in`, `-out`, `-reset`, through its catalogue setting). True
/// when it was one. Stepping and resetting one setting go through the
/// catalogue (`Setting::change`, `Setting::reset`).
pub fn run(app: &Rc<App>, window: &AppWindow, action: &str) -> Option<bool> {
    if let Some(line) = action.strip_prefix("pref-set:") {
        let (name, value) = prefs::split_assignment(line);
        let result = prefs::set_text(app.engine.borrow_mut().settings_mut(), &name, &value);
        return Some(match result {
            Ok(spec) => {
                changed(app, window, spec);
                true
            }
            Err(error) => {
                notice(vec![error]);
                refresh_views(app, window);
                false
            }
        });
    }
    match action {
        // The chat's zoom (Ctrl+= / Ctrl+- / Ctrl+0, Ctrl+wheel).
        "chat-zoom-in" => Some(step_setting(app, window, "chatzoom", 1)),
        "chat-zoom-out" => Some(step_setting(app, window, "chatzoom", -1)),
        "chat-zoom-reset" => Some(crate::catalogue::setting("chatzoom").is_some_and(|zoom| zoom.reset(app, window))),
        "pref-reset-all" => {
            reset_all(app, window);
            Some(true)
        }
        "pref-edit-file" => Some(edit_file(app)),
        "pref-export" => Some(export(app, window)),
        "pref-import" => {
            open_import(app, window);
            Some(true)
        }
        _ => None,
    }
}

/// One catalogue setting a step on (the chat's zoom, the window's scale).
pub fn step_setting(app: &Rc<App>, window: &AppWindow, id: &str, delta: i32) -> bool {
    match crate::catalogue::setting(id) {
        Some(setting) => {
            setting.change(app, window, delta);
            true
        }
        None => {
            eprintln!("clarp-slint: no setting {id} to step");
            false
        }
    }
}

/// A colour or font back to the theme's or the default (the catalogue's
/// reset for a setting the value editor sets).
pub fn reset_pref(app: &Rc<App>, window: &AppWindow, name: &str) {
    let Some(spec) = prefs::find(name) else { return };
    let theme = prefs::theme_of(app.engine.borrow().settings());
    prefs::reset(app.engine.borrow_mut().settings_mut(), spec, &theme);
    changed(app, window, spec);
}

/// Every setting back to its default: the preferences in the settings file
/// at once, then the catalogue's settings that are not preferences (the
/// explorer, ...). How many changed.
pub fn reset_all(app: &Rc<App>, window: &AppWindow) -> usize {
    let theme = prefs::theme_of(app.engine.borrow().settings());
    let stored = prefs::reset_all(app.engine.borrow_mut().settings_mut(), &theme);
    everything_changed(app, window);
    let others = crate::catalogue::settings().iter().filter(|s| pref_of(s.id()).is_none()).filter(|s| s.reset(app, window)).count();
    let count = stored + others;
    notice(vec![format!("{count} setting{} back to the default", if count == 1 { "" } else { "s" })]);
    refresh_views(app, window);
    count
}

fn everything_changed(app: &Rc<App>, window: &AppWindow) {
    app.engine.borrow_mut().reload_preferences();
    apply(app, window);
    app.rebuild_transcripts();
    rescale(app);
    crate::avatar_view::apply(app, window);
    crate::avatar_view::remake();
    crate::view::apply_app_theme(app, window);
    app.refresh(&[Change::Panes, Change::Preferences]);
    crate::pump_now(app);
    refresh_views(app, window);
}

// ---- the value editor and colour picker

fn range_of(spec: &Spec, theme: &str) -> String {
    let default = prefs::display(spec, &prefs::default_value(spec, theme));
    match spec.kind {
        Kind::Number { min, max, unit, zero, .. } => {
            let zero = zero.map(|z| format!(" (0: {z})")).unwrap_or_default();
            format!("{min}–{max} {unit}{zero} · default {default}")
        }
        Kind::Choice { .. } => format!("default {default}"),
        Kind::Color { .. } => format!("#rrggbb · the theme's {default}"),
        Kind::Text { .. } => format!("default {default}"),
        Kind::Toggle { .. } => format!("on or off · default {default}"),
    }
}

/// The value editor for `name`, on its current value.
pub fn open_editor(app: &Rc<App>, window: &AppWindow, name: &str) {
    let Some(spec) = prefs::find(name) else { return };
    let (theme, before, current) = {
        let engine = app.engine.borrow();
        let settings = engine.settings();
        let theme = prefs::theme_of(settings);
        (theme.clone(), prefs::stored(settings, spec, &theme), prefs::shown(settings, spec))
    };
    let editor = window.global::<PrefEditor>();
    editor.set_name(spec.name.into());
    editor.set_label(spec.label.into());
    editor.set_description(spec.description.into());
    editor.set_key(format!(":set {} · {}", spec.name, spec.key).into());
    editor.set_range(range_of(spec, &theme).into());
    let colour = matches!(spec.kind, Kind::Color { .. });
    editor.set_colour(colour);
    // A number is typed bare ("16"), as `:set` takes it.
    let text = match spec.kind {
        Kind::Number { .. } => prefs::current(app.engine.borrow().settings(), spec, &theme).as_f64().map(|n| n.to_string()).unwrap_or(current),
        _ => current,
    };
    editor.set_text(text.clone().into());
    editor.set_error("".into());
    let swatches: Vec<Swatch> = if colour {
        prefs::palette(&theme)
            .into_iter()
            .filter_map(|(role, hex)| crate::view::color(&hex).map(|tint| Swatch { role: role.into(), hex: hex.into(), tint }))
            .collect()
    } else {
        Vec::new()
    };
    editor.set_swatches(ModelRc::new(VecModel::from(swatches)));
    EDITING.with(|e| *e.borrow_mut() = Some(Editing { name: spec.name.to_owned(), theme, before }));
    preview(app, window, &text);
    crate::commands::open_overlay(app, window, EDITOR);
    editor.set_focus_request(editor.get_focus_request() + 1);
}

/// The import path, in the value editor.
pub(crate) fn open_import(app: &Rc<App>, window: &AppWindow) {
    let editor = window.global::<PrefEditor>();
    editor.set_name("import".into());
    editor.set_label("Import settings".into());
    editor.set_description("A settings file exported from Clarp (or written by hand): its valid settings are applied, the rest is reported.".into());
    editor.set_key("".into());
    editor.set_range("".into());
    editor.set_colour(false);
    editor.set_text(export_path().to_string_lossy().into_owned().into());
    editor.set_error("".into());
    editor.set_warnings(ModelRc::new(VecModel::from(Vec::<SharedString>::new())));
    editor.set_swatches(ModelRc::new(VecModel::from(Vec::<Swatch>::new())));
    EDITING.with(|e| *e.borrow_mut() = Some(Editing { name: "import".into(), theme: String::new(), before: None }));
    crate::commands::open_overlay(app, window, EDITOR);
    editor.set_focus_request(editor.get_focus_request() + 1);
}

/// Applies what is typed as it is typed (a value that is not valid yet is
/// said, and changes nothing).
fn preview(app: &Rc<App>, window: &AppWindow, text: &str) {
    let Some(editing) = EDITING.with(|e| e.borrow().clone()) else { return };
    let editor = window.global::<PrefEditor>();
    if editing.name == "import" {
        return;
    }
    let Some(spec) = prefs::find(&editing.name) else { return };
    let result = {
        let mut engine = app.engine.borrow_mut();
        let settings = engine.settings_mut();
        let current = prefs::current(settings, spec, &editing.theme);
        prefs::parse(spec, &current, text).and_then(|value| prefs::set(settings, spec, &editing.theme, value))
    };
    match result {
        Ok(()) => {
            editor.set_error("".into());
            let (warnings, shown) = {
                let engine = app.engine.borrow();
                let settings = engine.settings();
                let warnings = match spec.kind {
                    Kind::Color { role } => prefs::readability_warnings(settings, &editing.theme, role),
                    _ => Vec::new(),
                };
                (warnings, prefs::shown(settings, spec))
            };
            if let Some(color) = crate::view::color(&shown) {
                editor.set_preview(color);
            }
            editor.set_warnings(ModelRc::new(VecModel::from(warnings.into_iter().map(SharedString::from).collect::<Vec<_>>())));
            changed(app, window, spec);
        }
        Err(error) => editor.set_error(error.into()),
    }
}

/// Puts the value from before the editor opened back.
fn restore(app: &Rc<App>, editing: &Editing) {
    let Some(spec) = prefs::find(&editing.name) else { return };
    let mut engine = app.engine.borrow_mut();
    let settings = engine.settings_mut();
    match &editing.before {
        Some(value) => {
            if let Err(error) = prefs::set(settings, spec, &editing.theme, value.clone()) {
                eprintln!("clarp-slint: could not put {} back: {error}", spec.name);
                prefs::reset(settings, spec, &editing.theme);
            }
        }
        None => prefs::reset(settings, spec, &editing.theme),
    }
}

pub fn cancel_editor(app: &Rc<App>, window: &AppWindow) {
    if let Some(editing) = EDITING.with(|e| e.borrow_mut().take()) {
        if editing.name != "import" {
            restore(app, &editing);
            if let Some(spec) = prefs::find(&editing.name) {
                changed(app, window, spec);
            }
        }
    }
    CONFIRMING.with(|c| c.borrow_mut().clear());
    crate::commands::close_overlay(app, window);
    refresh_views(app, window);
}

fn accept_editor(app: &Rc<App>, window: &AppWindow) {
    let Some(editing) = EDITING.with(|e| e.borrow().clone()) else { return };
    let text = window.global::<PrefEditor>().get_text().to_string();
    if editing.name == "import" {
        if import(app, window, &text) {
            EDITING.with(|e| e.borrow_mut().take());
            crate::commands::close_overlay(app, window);
        }
        return;
    }
    preview(app, window, &text);
    if !window.global::<PrefEditor>().get_error().is_empty() {
        return;
    }
    EDITING.with(|e| e.borrow_mut().take());
    crate::commands::close_overlay(app, window);
    refresh_views(app, window);
}

fn reset_editor(app: &Rc<App>, window: &AppWindow) {
    let Some(editing) = EDITING.with(|e| e.borrow().clone()) else { return };
    let Some(spec) = prefs::find(&editing.name) else { return };
    prefs::reset(app.engine.borrow_mut().settings_mut(), spec, &editing.theme);
    let shown = match spec.kind {
        Kind::Number { .. } => prefs::current(app.engine.borrow().settings(), spec, &editing.theme).as_f64().map(|n| n.to_string()).unwrap_or_default(),
        _ => prefs::shown(app.engine.borrow().settings(), spec),
    };
    window.global::<PrefEditor>().set_text(shown.clone().into());
    preview(app, window, &shown);
}

// ---- confirmations (Settings → Confirm stop and release)

/// Whether `action` waits for a yes first; asks when it does.
pub fn confirm_first(app: &Rc<App>, window: &AppWindow, action: &str) -> bool {
    if !matches!(action, "stop-agent" | "release-agent") || !prefs::flag(app.engine.borrow().settings(), "confirmstop") {
        return false;
    }
    if CONFIRMED.with(Cell::take) {
        return false;
    }
    let name = {
        let engine = app.engine.borrow();
        engine.chat_name(engine.selected_session())
    };
    let editor = window.global::<PrefEditor>();
    let (title, message) = if action == "stop-agent" {
        (format!("Stop {name}?"), "It stops what it is doing now; the chat stays.".to_owned())
    } else {
        (format!("Release {name}?"), "The agent's session ends and its contact is free again.".to_owned())
    };
    editor.set_confirm_title(title.into());
    editor.set_confirm_message(message.into());
    CONFIRMING.with(|c| *c.borrow_mut() = action.to_owned());
    crate::commands::open_overlay(app, window, CONFIRM);
    editor.set_confirm_request(editor.get_confirm_request() + 1);
    true
}

fn confirmed(app: &Rc<App>, window: &AppWindow) {
    let action = CONFIRMING.with(|c| std::mem::take(&mut *c.borrow_mut()));
    crate::commands::close_overlay(app, window);
    if action.is_empty() {
        return;
    }
    CONFIRMED.with(|c| c.set(true));
    if !crate::commands::run(app, window, &action) {
        eprintln!("clarp-slint: {action} is not available now");
    }
    CONFIRMED.with(|c| c.set(false));
}

// ---- the settings file

/// Writes the schema and reference beside the settings file and points the
/// file at the schema, so an editor explains and checks every setting.
fn document(path: &std::path::Path, app: &App) -> Result<(), String> {
    let folder = path.parent().ok_or("the settings file has no folder")?;
    std::fs::create_dir_all(folder).map_err(|e| format!("{}: {e}", folder.display()))?;
    let schema = serde_json::to_string_pretty(&prefs::schema()).map_err(|e| e.to_string())?;
    std::fs::write(folder.join("settings.schema.json"), schema).map_err(|e| format!("schema: {e}"))?;
    std::fs::write(folder.join("settings-reference.md"), prefs::reference()).map_err(|e| format!("reference: {e}"))?;
    if app.engine.borrow().settings().get("$schema").is_none() {
        app.engine.borrow_mut().settings_mut().set("$schema", "./settings.schema.json");
    }
    Ok(())
}

/// Opens the settings file in `$VISUAL`/`$EDITOR` (in the default
/// terminal), else with the desktop's app for JSON. Checks record it
/// instead (`CLARP_TEST_TERMINAL_LOG`, `CLARP_TEST_OPEN_URL`).
pub(crate) fn edit_file(app: &App) -> bool {
    let Some(path) = app.engine.borrow().settings().path().map(std::path::Path::to_path_buf) else {
        notice(vec!["Settings are kept in memory only (CLARP_SETTINGS=off): there is no file to edit".into()]);
        return false;
    };
    if let Err(error) = document(&path, app) {
        notice(vec![format!("could not write the settings reference: {error}")]);
    }
    let editor = std::env::var("VISUAL").ok().filter(|e| !e.trim().is_empty()).or_else(|| std::env::var("EDITOR").ok().filter(|e| !e.trim().is_empty()));
    let file = path.to_string_lossy().into_owned();
    let folder = path.parent().map(|p| p.to_string_lossy().into_owned()).unwrap_or_else(|| "/".into());
    let opened = match editor {
        Some(editor) => {
            let script = format!("exec {editor} \"$1\"");
            let command: Vec<String> = vec!["sh".into(), "-c".into(), script, "sh".into(), file.clone()];
            let xdg = clarp_core::links::find_executable("xdg-terminal-exec").is_some();
            let (program, arguments) = if xdg {
                let mut launch = vec![format!("--dir={folder}"), "--title=Clarp settings".into(), "--".into()];
                launch.extend(command);
                ("xdg-terminal-exec".to_owned(), launch)
            } else {
                let mut launch = vec!["-e".to_owned()];
                launch.extend(command);
                ("x-terminal-emulator".to_owned(), launch)
            };
            let launcher = clarp_engine::lifecycle::default_terminal_launcher();
            launcher(&clarp_engine::lifecycle::TerminalCommand { program: program.clone(), arguments, directory: folder })
                .map_err(|e| format!("could not start {program}: {e}"))
        }
        None => crate::view::open_file(&path),
    };
    match opened {
        Ok(()) => {
            notice(vec![format!("Editing {file}: saved changes apply at once")]);
            true
        }
        Err(error) => {
            notice(vec![error]);
            false
        }
    }
}

/// Where an export goes: `~/Downloads/clarp-settings.json` (or the home
/// folder without a Downloads folder).
pub(crate) fn export_path() -> std::path::PathBuf {
    let home = std::env::var_os("HOME").map(std::path::PathBuf::from).unwrap_or_else(|| "/tmp".into());
    let downloads = home.join("Downloads");
    if downloads.is_dir() { downloads } else { home }.join("clarp-settings.json")
}

pub(crate) fn export(app: &Rc<App>, window: &AppWindow) -> bool {
    let path = export_path();
    let text = match serde_json::to_string_pretty(&prefs::export(app.engine.borrow().settings())) {
        Ok(text) => text,
        Err(error) => {
            notice(vec![format!("could not export: {error}")]);
            return false;
        }
    };
    let written = std::fs::write(&path, text + "\n");
    notice(vec![match &written {
        Ok(()) => format!("Exported to {}", path.display()),
        Err(error) => format!("could not export to {}: {error}", path.display()),
    }]);
    refresh_views(app, window);
    written.is_ok()
}

fn import(app: &Rc<App>, window: &AppWindow, path: &str) -> bool {
    let path = path.trim();
    let path = match path.strip_prefix("~/") {
        Some(rest) => std::env::var_os("HOME").map(std::path::PathBuf::from).unwrap_or_default().join(rest),
        None => std::path::PathBuf::from(path),
    };
    let file = std::fs::read_to_string(&path).map_err(|e| format!("cannot read {}: {e}", path.display())).and_then(|text| {
        serde_json::from_str::<Value>(&text).map_err(|e| format!("{} is not valid JSON: {e}", path.display()))
    });
    let file = match file {
        Ok(file) => file,
        Err(error) => {
            window.global::<PrefEditor>().set_error(error.into());
            return false;
        }
    };
    let (applied, problems) = prefs::import(app.engine.borrow_mut().settings_mut(), &file);
    let mut lines = vec![format!("Imported {applied} setting{} from {}", if applied == 1 { "" } else { "s" }, path.display())];
    lines.extend(problems);
    notice(lines);
    everything_changed(app, window);
    true
}

/// Watches the settings file: an edit (by hand, or another window) is
/// read again and applied; a broken file is reported and changes nothing.
pub fn watch(app: &Rc<App>) {
    let Some(path) = app.engine.borrow().settings().path().map(std::path::Path::to_path_buf) else { return };
    let stamp = move || std::fs::metadata(&path).ok().and_then(|m| Some((m.modified().ok()?, m.len())));
    SEEN.with(|s| *s.borrow_mut() = stamp());
    let timer = slint::Timer::default();
    timer.start(slint::TimerMode::Repeated, std::time::Duration::from_millis(500), move || {
        let now = stamp();
        if SEEN.with(|s| *s.borrow() == now) {
            return;
        }
        SEEN.with(|s| *s.borrow_mut() = now);
        crate::with_window(|app, window| reload(app, window));
    });
    WATCH.with(|w| *w.borrow_mut() = Some(timer));
}

/// Reads the settings file again and applies what changed.
pub fn reload(app: &Rc<App>, window: &AppWindow) {
    let result = app.engine.borrow_mut().settings_mut().reload();
    match result {
        // Our own save, or a file mended back to what was applied: only a
        // report of a broken file needs clearing.
        Ok(keys) if keys.is_empty() => {
            if BROKEN.with(|b| b.replace(false)) {
                notice(prefs::problems(app.engine.borrow().settings()));
                refresh_views(app, window);
            }
        }
        Ok(keys) => {
            BROKEN.with(|b| b.set(false));
            let problems = prefs::problems(app.engine.borrow().settings());
            eprintln!("clarp-slint: settings file changed: {}", keys.join(", "));
            notice(problems);
            everything_changed(app, window);
        }
        Err(error) => {
            BROKEN.with(|b| b.set(true));
            notice(vec![error]);
            refresh_views(app, window);
        }
    }
}

/// Connects the `PrefEditor` global, applies the settings once and starts
/// watching their file.
pub fn wire(app: &Rc<App>, window: &AppWindow) {
    use crate::with_window;
    let editor = window.global::<PrefEditor>();
    editor.on_edited(|text| with_window(|app, window| preview(app, window, &text)));
    editor.on_accept(|| with_window(accept_editor));
    editor.on_cancel(|| with_window(cancel_editor));
    editor.on_picked(|hex| {
        with_window(|app, window| {
            window.global::<PrefEditor>().set_text(hex.clone());
            preview(app, window, &hex);
        })
    });
    editor.on_reset(|| with_window(reset_editor));
    editor.on_confirmed(|| with_window(confirmed));
    window.global::<Look>().on_zoom_by(|delta| {
        with_window(|app, window| {
            step_setting(app, window, "chatzoom", delta);
        })
    });
    let problems = prefs::problems(app.engine.borrow().settings());
    if !problems.is_empty() {
        notice(problems);
    }
    SCALE.with(|s| s.set(prefs::number_of(app.engine.borrow().settings(), "uiscale") as f32));
    apply(app, window);
    crate::view::apply_app_theme(app, window);
    watch(app);
}

// ---- the catalogue: every preference registered as a setting

/// The preferences the catalogue registers itself (its own rows, choices
/// and pickers), by its id: `:set`, the settings file, export and reset
/// still know them by their names here.
pub const CATALOGUED: &[(&str, &str)] = &[
    ("timestamps", "timestamps"),
    ("showwhenready", "show-when-ready"),
    ("reducedmotion", "reduced-motion"),
    ("toolactivity", "activity"),
    ("tooldetail", "tool-detail"),
    ("startupnewagent", "new-agent-on-startup"),
    ("anonymousagents", "anonymous-agents"),
    ("navrail", "nav-rail"),
    ("compactexplorer", "compact-explorer"),
    ("avatarsize", "avatar-size"),
    ("previewonmove", "live-preview"),
    ("workspacebar", "workspace-bar"),
    ("shortcutbar", "shortcut-bar"),
    ("minimalui", "minimal-ui"),
    ("readingtheme", "reading-theme"),
    ("uiscale", "ui-scale"),
    ("spokenreplies", "spoken-replies"),
    ("pausemobile", "pause-mobile-push"),
    ("doublepress", "double-press"),
    ("fontsize", "font-size"),
];

/// The preference a settings row or catalogue setting is (its own name, or
/// the catalogue's id for one it registers itself).
pub fn pref_of(id: &str) -> Option<&'static Spec> {
    match CATALOGUED.iter().find(|(_, catalogue)| *catalogue == id) {
        Some((name, _)) => prefs::find(name),
        None if CATALOGUED.iter().any(|(name, _)| *name == id) => None,
        None => prefs::specs().iter().find(|s| s.name == id),
    }
}

/// A preference set from its text (a choice's value, a number), as the
/// catalogue's controls and `:set` do, then shown everywhere.
pub fn set_pref(app: &Rc<App>, window: &AppWindow, name: &str, text: &str) {
    let result = prefs::set_text(app.engine.borrow_mut().settings_mut(), name, text);
    match result {
        Ok(spec) => changed(app, window, spec),
        Err(error) => notice(vec![error]),
    }
}
