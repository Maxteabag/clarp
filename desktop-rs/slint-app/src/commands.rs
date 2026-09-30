//! What the keyboard map's actions do (Main.qml `runCommand`), and where
//! the keyboard is, which picks the map's state.

use std::rc::Rc;

use clarp_engine::Change;
use slint::{Model, ModelRc, VecModel};

use crate::keymap::{self, Facts};
use crate::{App, AppWindow, Hint, pump_now};

/// The keyboard map's state for where the keyboard is now.
pub fn context(app: &App, window: &AppWindow) -> &'static str {
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
        "chats" | "updates" | "teams" | "settings" => {
            window.set_surface(action.into());
            if action == "chats" {
                app.focus_composer();
            }
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
