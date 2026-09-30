//! What the keyboard map's actions do (Main.qml `runCommand`), and where
//! the keyboard is, which picks the map's state.

use std::rc::Rc;

use clarp_engine::Change;
use slint::{Model, ModelRc, VecModel};

use crate::keymap::{self, Facts};
use crate::{App, AppWindow, Hint, SwitcherRow, pump_now, switcher};

/// The keyboard map's state for where the keyboard is now.
pub fn context(app: &App, window: &AppWindow) -> &'static str {
    if app.switcher.borrow().open || !app.overlay.borrow().is_empty() {
        return "modal";
    }
    match window.get_surface().as_str() {
        "updates" => return "updates",
        "teams" => return "teams",
        "settings" => return "settings",
        _ => {}
    }
    if window.get_search_focused() {
        "search"
    } else if app.active_report().composer_focused {
        "composer"
    } else if window.get_sidebar_focused() {
        "sidebar"
    } else {
        "pane"
    }
}

fn facts(app: &App, window: &AppWindow) -> Facts {
    let engine = app.engine.borrow();
    let selected = engine.selected_session();
    Facts {
        attention: false,
        agent: !selected.is_empty() && !selected.starts_with("pair:"),
        rows: window.get_chats().row_count() > 0,
        can_send: engine.can_send(selected),
    }
}

pub fn overrides(app: &App) -> keymap::Overrides {
    let engine = app.engine.borrow();
    let saved = engine.settings().get("keymap/bindings").cloned().unwrap_or_default();
    serde_json::from_value(saved).unwrap_or_default()
}

/// Updates the shortcut bar for where the keyboard is.
pub fn show_hints(app: &App, window: &AppWindow) {
    let state = context(app, window);
    let hints: Vec<Hint> = keymap::hints(state, &overrides(app), facts(app, window))
        .into_iter()
        .map(|b| Hint {
            label: b.label.into(),
            keys: b.keys.first().map(|k| k.replace("Return", "Enter").replace("Escape", "Esc")).unwrap_or_default().into(),
        })
        .collect();
    let mode = match state {
        "composer" => "INSERT".to_owned(),
        "sidebar" => "AGENTS".to_owned(),
        "pane" => "CONVERSATION".to_owned(),
        other => other.to_uppercase(),
    };
    window.set_keyboard_mode(mode.into());
    window.set_hints(ModelRc::new(VecModel::from(hints)));
}

/// A key the window saw before any control: true when a binding ran.
pub fn shortcut(text: &str, control: bool, alt: bool, shift: bool) -> bool {
    let (Some(app), Some(window)) = (crate::app(), crate::window()) else { return false };
    let Some(chord) = keymap::chord(text, control, alt, shift) else { return false };
    let state = context(&app, &window);
    let Some(action) = keymap::action_for(state, &chord, &overrides(&app), facts(&app, &window)) else { return false };
    let ran = run(&app, &window, action);
    show_hints(&app, &window);
    ran
}

fn sidebar_sessions(window: &AppWindow) -> Vec<String> {
    window.get_chats().iter().map(|row| row.session.to_string()).collect()
}

/// Runs `action`; false for one this app does not do yet, so the key
/// reaches the focused control instead.
pub fn run(app: &Rc<App>, window: &AppWindow, action: &str) -> bool {
    let selected = app.engine.borrow().selected_session().to_owned();
    let mut layout_changed = false;
    match action {
        "shortcut-bar" => window.set_shortcuts_visible(!window.get_shortcuts_visible()),
        "switcher" => open_switcher(app, window),
        "escape" if app.switcher.borrow().open => close_switcher(app, window, None),
        "escape" if !app.overlay.borrow().is_empty() => close_overlay(app, window),
        "edit-keymap" => {
            window.set_keymap_actions(ModelRc::new(VecModel::from(keymap::EDITABLE.iter().map(|a| slint::SharedString::from(*a)).collect::<Vec<_>>())));
            window.set_keymap_error("".into());
            window.set_keymap_profile(keymap::export(&overrides(app)).into());
            open_overlay(app, window, "keymap");
            window.invoke_open_keymap();
        }
        // Escape on another surface goes back to the chats.
        "escape" if window.get_surface() != "chats" => {
            window.set_surface("chats".into());
            app.focus_composer();
        }
        "escape" => {
            let state = app.engine.borrow().roster().find(&selected).map(|a| a.latest_state.clone()).unwrap_or_default();
            let context = context(app, window);
            if matches!(state.as_str(), "thinking" | "tool" | "compacting") {
                app.engine.borrow_mut().stop();
            } else if context == "search" {
                focus_sidebar(app, window);
            } else {
                // From the list or the composer, the conversation keeps the
                // keyboard: arrows and Page keys scroll it.
                app.focus_transcript();
            }
        }
        "focus-sidebar" => focus_sidebar(app, window),
        "focus-pane" => app.focus_transcript(),
        "focus-composer" => app.focus_composer(),
        "toggle-focus" => {
            if window.get_sidebar_focused() {
                app.focus_transcript();
            } else {
                focus_sidebar(app, window);
            }
        }
        "agent-next" | "agent-previous" => {
            let sessions = sidebar_sessions(window);
            let cursor = window.get_sidebar_cursor().to_string();
            let at = sessions.iter().position(|s| *s == cursor);
            let next = match (at, action == "agent-next") {
                (None, _) => sessions.first(),
                (Some(i), true) => sessions.get((i + 1).min(sessions.len().saturating_sub(1))),
                (Some(i), false) => sessions.get(i.saturating_sub(1)),
            };
            if let Some(next) = next {
                window.set_sidebar_cursor(next.as_str().into());
            }
        }
        "agent-open" => {
            let cursor = window.get_sidebar_cursor().to_string();
            if !cursor.is_empty() {
                app.engine.borrow_mut().select(&cursor);
                pump_now(app);
                app.focus_composer();
            }
        }
        "agent-search" => window.invoke_focus_search(),
        "sidebar" => {
            window.set_sidebar_visible(!window.get_sidebar_visible());
            app.focus_composer();
        }
        "split-right" | "split-down" => {
            let direction = if action == "split-right" { "vertical" } else { "horizontal" };
            app.engine.borrow_mut().with_panes(|p| p.split_active(direction, &selected));
            layout_changed = true;
        }
        "close-pane" => {
            let active = app.engine.borrow().panes().active_pane_id().to_owned();
            app.engine.borrow_mut().with_panes(|p| p.close_pane(&active));
            layout_changed = true;
        }
        "zoom" => {
            app.engine.borrow_mut().with_panes(|p| p.toggle_zoom());
            layout_changed = true;
        }
        "balance" => app.engine.borrow_mut().with_panes(|p| p.equalize()),
        "next-workspace" => {
            let (workspaces, active) = {
                let engine = app.engine.borrow();
                (engine.panes().workspaces(), engine.panes().active_workspace().to_owned())
            };
            let ids: Vec<String> = workspaces.iter().map(|w| clarp_core::json::string(w, "id")).collect();
            if ids.len() > 1 {
                let index = ids.iter().position(|id| *id == active).unwrap_or(0);
                let next = ids[(index + 1) % ids.len()].clone();
                app.engine.borrow_mut().with_panes(|p| p.switch_workspace(&next));
                layout_changed = true;
            }
        }
        _ if action.starts_with("move-") => {
            let direction = action.trim_start_matches("move-").to_owned();
            app.engine.borrow_mut().with_panes(|p| p.navigate(&direction));
            layout_changed = true;
        }
        "jump-latest" => app.to_latest(),
        "stop-agent" => app.engine.borrow_mut().stop(),
        "mute" => {
            let muted = app.engine.borrow().muted();
            app.engine.borrow_mut().set_muted(!muted);
        }
        "refresh" => app.engine.borrow_mut().refresh_session(&selected),
        "tools" => {
            let mode = app.engine.borrow().activity_mode();
            let always = clarp_core::presentation::ALWAYS_VISIBLE;
            app.engine.borrow_mut().set_activity_mode(if mode == always { 0 } else { always });
        }
        _ if action.starts_with("setting:") => apply_setting(app, window, action),
        "settings" => crate::settings_view::open(app, window),
        "chats" => {
            window.set_surface("chats".into());
            app.focus_composer();
        }
        _ => return false,
    }
    pump_now(app);
    if layout_changed {
        app.refresh(&[Change::Panes]);
        app.focus_composer();
    }
    true
}

fn focus_sidebar(app: &App, window: &AppWindow) {
    window.set_sidebar_visible(true);
    let selected = app.engine.borrow().selected_session().to_owned();
    window.set_sidebar_cursor(selected.into());
    window.invoke_focus_sidebar();
}

// ---- the quick switcher ------------------------------------------------------

fn toggles(app: &App, window: &AppWindow) -> switcher::Toggles {
    let engine = app.engine.borrow();
    let prefs = *app.prefs.borrow();
    switcher::Toggles {
        sidebar_visible: window.get_sidebar_visible(),
        muted: engine.muted(),
        show_when_ready: engine.show_when_ready(),
        timestamps_visible: prefs.timestamps,
        workspace_bar: prefs.workspace_bar,
        shared_filesystem: engine.shared_filesystem(),
        activity_mode: engine.activity_mode(),
    }
}

pub fn open_switcher(app: &App, window: &AppWindow) {
    {
        let mut state = app.switcher.borrow_mut();
        state.open = true;
        state.query.clear();
        state.selected.clear();
        state.restore_composer = app.active_report().composer_focused;
    }
    window.set_switcher_open(true);
    window.invoke_show_switcher();
    refresh_switcher(app, window);
}

/// Rebuilds the results (a query typed, or the roster changed while open).
pub fn refresh_switcher(app: &App, window: &AppWindow) {
    if !app.switcher.borrow().open {
        return;
    }
    let toggles = toggles(app, window);
    let query = app.switcher.borrow().query.clone();
    let items = switcher::results(&app.engine.borrow(), &query, toggles);
    let mut state = app.switcher.borrow_mut();
    let current = switcher::keep_selection(&items, &state.selected);
    state.selected = usize::try_from(current).ok().and_then(|i| items.get(i)).map(switcher::Item::key_of).unwrap_or_default();
    let rows: Vec<SwitcherRow> = items
        .iter()
        .map(|item| SwitcherRow {
            kind: if item.kind == switcher::Kind::Agent { "agent".into() } else { "command".into() },
            label: item.label.clone().into(),
            detail: item.detail.clone().into(),
            key: item.key.clone().into(),
            group: item.group.into(),
        })
        .collect();
    state.items = items;
    drop(state);
    window.set_switcher_rows(ModelRc::new(VecModel::from(rows)));
    window.set_switcher_current(current);
}

pub fn switcher_moved(app: &App, window: &AppWindow, index: i32) {
    let mut state = app.switcher.borrow_mut();
    if let Some(item) = usize::try_from(index).ok().and_then(|i| state.items.get(i)) {
        state.selected = item.key_of();
        drop(state);
        window.set_switcher_current(index);
    }
}

/// Closes the switcher; the composer gets the keyboard back when it had it
/// (or `restore` says so).
pub fn close_switcher(app: &App, window: &AppWindow, restore: Option<bool>) {
    let restore = {
        let mut state = app.switcher.borrow_mut();
        state.open = false;
        restore.unwrap_or(state.restore_composer)
    };
    window.set_switcher_open(false);
    if restore {
        app.focus_composer();
    } else {
        app.focus_transcript();
    }
    show_hints(app, window);
}

/// Enter on a row (QuickSwitcher.qml `choose`).
pub fn switcher_chosen(app: &Rc<App>, window: &AppWindow, index: i32) {
    let Some(item) = usize::try_from(index).ok().and_then(|i| app.switcher.borrow().items.get(i).cloned()) else { return };
    let mut restore = app.switcher.borrow().restore_composer;
    // Closed without moving the keyboard: the choice decides where it goes.
    app.switcher.borrow_mut().open = false;
    window.set_switcher_open(false);
    match item.kind {
        switcher::Kind::Agent => {
            app.engine.borrow_mut().select(&item.target);
            pump_now(app);
            restore = true;
        }
        switcher::Kind::Command => {
            let leaves = ["quick-new-agent", "rename-agent", "new", "overview", "connection", "orchestrator", "updates", "teams", "settings"];
            if leaves.contains(&item.target.as_str()) {
                restore = false;
            }
            if !run(app, window, &item.target) {
                eprintln!("clarp-slint: {} is not available yet", item.target);
            }
        }
    }
    if restore {
        app.focus_composer();
    } else if !window.get_switcher_open() {
        app.focus_transcript();
    }
    show_hints(app, window);
}

fn apply_setting(app: &App, window: &AppWindow, action: &str) {
    let setting = action.trim_start_matches("setting:");
    if let Some(mode) = setting.strip_prefix("activity:").and_then(|m| m.parse::<i32>().ok()) {
        app.engine.borrow_mut().set_activity_mode(mode);
    } else if let Some(theme) = setting.strip_prefix("reading:") {
        app.engine.borrow_mut().set_reading_theme(theme);
    } else {
        match setting {
            "showWhenReady" => {
                let value = app.engine.borrow().show_when_ready();
                app.engine.borrow_mut().set_show_when_ready(!value);
            }
            "sharedFilesystem" => {
                let value = app.engine.borrow().shared_filesystem();
                app.engine.borrow_mut().set_shared_filesystem(!value);
            }
            "timestampsVisible" | "workspaceBarVisible" => {
                let (key, value) = {
                    let mut prefs = app.prefs.borrow_mut();
                    if setting == "timestampsVisible" {
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
            other => eprintln!("clarp-slint: unknown setting {other}"),
        }
    }
    let _ = window;
}

// ---- dialogs -----------------------------------------------------------------

pub fn open_overlay(app: &App, window: &AppWindow, name: &str) {
    *app.overlay.borrow_mut() = name.to_owned();
    window.set_overlay(name.into());
    show_hints(app, window);
}

/// Closes the dialog; the active pane's composer gets the keyboard back.
pub fn close_overlay(app: &App, window: &AppWindow) {
    app.overlay.borrow_mut().clear();
    window.set_overlay("".into());
    app.focus_composer();
    show_hints(app, window);
}

fn save_overrides(app: &App, overrides: &keymap::Overrides) {
    let value = serde_json::to_value(overrides).unwrap_or_default();
    app.engine.borrow_mut().settings_mut().set("keymap/bindings", value);
}

pub fn keymap_apply(app: &App, window: &AppWindow, action: &str, key: &str) {
    match keymap::set_binding(&overrides(app), action, key) {
        Ok(next) => {
            save_overrides(app, &next);
            window.set_keymap_error("".into());
            window.set_keymap_profile(keymap::export(&next).into());
        }
        Err(error) => window.set_keymap_error(error.into()),
    }
}

pub fn keymap_import(app: &App, window: &AppWindow, text: &str) {
    match keymap::import(text) {
        Ok(next) => {
            save_overrides(app, &next);
            window.set_keymap_error("".into());
            window.set_keymap_profile(keymap::export(&next).into());
        }
        Err(error) => window.set_keymap_error(error.into()),
    }
}

pub fn keymap_export(app: &App, window: &AppWindow) {
    window.set_keymap_profile(keymap::export(&overrides(app)).into());
}
