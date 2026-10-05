//! The key bindings editor (Ctrl+Alt+,): every action's keys in one
//! context at a time, all from the keyboard. Up/Down move between actions,
//! Left/Right between contexts, Enter captures a key to add (the same key
//! twice quickly is a double press), Delete takes one away, R resets the
//! action there, / finds an action, Tab edits the profile as JSON. A key
//! another action has is shown as a clash: Enter takes it over.

use std::cell::RefCell;

use slint::{ModelRc, VecModel};

use crate::keymap::{self, CONTEXTS, Refusal};
use crate::{App, AppWindow, KeymapRow};

/// Rows the dialog shows at once, around the current one.
const VISIBLE: usize = 12;

#[derive(Default)]
struct Editor {
    context: usize,
    /// Into the filtered actions.
    current: usize,
    filter: String,
    filtering: bool,
    /// Waiting for the key to add; the first press, waiting for a second.
    capturing: bool,
    captured: Option<String>,
    /// A key another action has, for Enter to take over.
    clash: Option<String>,
    /// Delete on an action with several keys: a digit picks which.
    deleting: bool,
    status: String,
    error: String,
}

thread_local! {
    static STATE: RefCell<Editor> = RefCell::new(Editor::default());
    static CAPTURE_TIMER: slint::Timer = slint::Timer::default();
}

fn window_ms(app: &App) -> std::time::Duration {
    keymap::double_press_window(app.engine.borrow().settings().get("keymap/doublePressMs").and_then(serde_json::Value::as_i64))
}

fn filtered(filter: &str) -> Vec<(&'static str, &'static str)> {
    let filter = filter.to_lowercase();
    keymap::actions()
        .into_iter()
        .filter(|(action, label)| filter.is_empty() || label.to_lowercase().contains(&filter) || action.contains(&filter.replace(' ', "-")))
        .collect()
}

fn context_of(editor: &Editor) -> &'static str {
    CONTEXTS[editor.context.min(CONTEXTS.len() - 1)].0
}

fn current_action(editor: &Editor) -> Option<&'static str> {
    filtered(&editor.filter).get(editor.current).map(|(a, _)| *a)
}

pub fn open(app: &App, window: &AppWindow) {
    STATE.with(|s| *s.borrow_mut() = Editor::default());
    CAPTURE_TIMER.with(slint::Timer::stop);
    window.set_keymap_profile(keymap::export(&crate::commands::overrides(app)).into());
    crate::commands::open_overlay(app, window, "keymap");
    window.invoke_open_keymap();
    render(app, window);
}

/// Shows the editor's state: the actions around the current one with
/// their keys in the chosen context.
pub fn render(app: &App, window: &AppWindow) {
    let overrides = crate::commands::overrides(app);
    STATE.with(|s| {
        let mut editor = s.borrow_mut();
        let actions = filtered(&editor.filter);
        editor.current = editor.current.min(actions.len().saturating_sub(1));
        let context = context_of(&editor);
        let start = editor.current.saturating_sub(VISIBLE / 2).min(actions.len().saturating_sub(VISIBLE));
        let rows: Vec<KeymapRow> = actions
            .iter()
            .enumerate()
            .skip(start)
            .take(VISIBLE)
            .map(|(index, (action, label))| {
                let keys = keymap::keys_in(context, action, &overrides);
                KeymapRow {
                    action: (*action).into(),
                    label: (*label).into(),
                    keys: if keys.is_empty() { "—".into() } else { keys.iter().map(|k| keymap::display(k)).collect::<Vec<_>>().join("  ·  ").into() },
                    custom: keymap::customised(&overrides, action, context),
                    current: index == editor.current,
                }
            })
            .collect();
        window.set_keymap_rows(ModelRc::new(VecModel::from(rows)));
        window.set_keymap_context(keymap::context_name(context).into());
        window.set_keymap_count(format!("{} of {}", (editor.current + 1).min(actions.len()), actions.len()).into());
        window.set_keymap_filter(if editor.filtering || !editor.filter.is_empty() { format!("/{}", editor.filter) } else { String::new() }.into());
        window.set_keymap_status(editor.status.clone().into());
        window.set_keymap_error(editor.error.clone().into());
    });
    crate::commands::show_hints(app, window);
}

/// The shortcut bar while the editor is open: only the keys that do
/// something now.
pub fn hints(app: &App, window: &AppWindow) -> Vec<(String, String)> {
    let pair = |k: &str, l: &str| (k.to_owned(), l.to_owned());
    if window.get_keymap_json_focused() {
        return vec![pair("Esc", "Back to the actions")];
    }
    let overrides = crate::commands::overrides(app);
    STATE.with(|s| {
        let editor = s.borrow();
        if editor.capturing {
            return vec![pair("", "Press the key, twice quickly for a double press"), pair("Esc", "Cancel")];
        }
        if editor.clash.is_some() {
            return vec![pair("Enter", "Take it over"), pair("Esc", "Keep it there")];
        }
        if editor.deleting {
            return vec![pair("1-9", "Delete that key"), pair("Esc", "Cancel")];
        }
        if editor.filtering {
            return vec![pair("Enter", "Done"), pair("Esc", "Clear")];
        }
        let context = context_of(&editor);
        let action = current_action(&editor);
        let mut out = vec![pair("↑↓", "Action"), pair("←→", "Context"), pair("Enter", "Add key")];
        if action.is_some_and(|a| !keymap::keys_in(context, a, &overrides).is_empty()) {
            out.push(pair("Del", "Delete key"));
        }
        if action.is_some_and(|a| keymap::customised(&overrides, a, context)) {
            out.push(pair("R", "Reset"));
        }
        out.extend([pair("/", "Find"), pair("Tab", "Profile JSON"), pair("Esc", "Close")]);
        out
    })
}

fn save(app: &App, window: &AppWindow, overrides: &keymap::Overrides) {
    crate::commands::save_overrides(app, overrides);
    window.set_keymap_profile(keymap::export(overrides).into());
    crate::commands::refresh_keys(app, window);
}

/// Adds the captured key to the current action, or says why not.
fn commit(app: &App, window: &AppWindow, key: &str) {
    CAPTURE_TIMER.with(slint::Timer::stop);
    let target = STATE.with(|s| {
        let mut editor = s.borrow_mut();
        editor.capturing = false;
        editor.captured = None;
        current_action(&editor).map(|a| (a, context_of(&editor)))
    });
    let Some((action, context)) = target else { return render(app, window) };
    let (status, error, clash) = match keymap::add(&crate::commands::overrides(app), action, context, key) {
        Ok(next) => {
            save(app, window, &next);
            (format!("{} runs {} in {}", keymap::display(key), label(action), keymap::context_name(context)), String::new(), None)
        }
        Err(refusal @ Refusal::Clash(_)) => (String::new(), format!("{refusal}. Enter takes it over, Esc keeps it there."), Some(key.to_owned())),
        Err(refusal) => (String::new(), refusal.to_string(), None),
    };
    STATE.with(|s| {
        let mut editor = s.borrow_mut();
        (editor.status, editor.error, editor.clash) = (status, error, clash);
    });
    render(app, window);
}

fn label(action: &str) -> &'static str {
    keymap::actions().into_iter().find(|(a, _)| *a == action).map_or("", |(_, l)| l)
}

/// A key while the editor is open: true when the editor used it.
pub fn key(app: &App, window: &AppWindow, text: &str, chord: &str, plain: bool) -> bool {
    if window.get_keymap_json_focused() {
        if chord != "Escape" {
            return false;
        }
        window.invoke_keymap_focus_list();
        render(app, window);
        return true;
    }
    let overrides = crate::commands::overrides(app);
    let mut editor = STATE.with(|s| std::mem::take(&mut *s.borrow_mut()));
    let mut close = false;
    let mut add_now: Option<String> = None;
    if editor.capturing {
        match editor.captured.take() {
            None if chord == "Escape" => {
                editor.capturing = false;
                editor.status.clear();
            }
            None => {
                editor.captured = Some(chord.to_owned());
                editor.status = format!("{}… press it again for {}", keymap::display(chord), keymap::display(&format!("{chord} {chord}")));
                CAPTURE_TIMER.with(|timer| {
                    timer.start(slint::TimerMode::SingleShot, window_ms(app), || {
                        let (Some(app), Some(window)) = (crate::app(), crate::window()) else { return };
                        let first = STATE.with(|s| s.borrow().captured.clone());
                        if let Some(first) = first {
                            commit(&app, &window, &first);
                        }
                    });
                });
            }
            // The same key again in time: a double press. Another key ends
            // the capture with the first.
            Some(first) if first == chord => add_now = Some(format!("{first} {first}")),
            Some(first) => add_now = Some(first),
        }
    } else if let Some(key) = editor.clash.take() {
        editor.error.clear();
        if chord == "Return" {
            if let Some(action) = current_action(&editor) {
                match keymap::take_over(&overrides, action, context_of(&editor), &key) {
                    Ok(next) => {
                        save(app, window, &next);
                        editor.status = format!("{} runs {} in {}", keymap::display(&key), label(action), keymap::context_name(context_of(&editor)));
                    }
                    Err(error) => editor.error = error,
                }
            }
        }
    } else if editor.deleting {
        editor.deleting = false;
        editor.status.clear();
        if let (Some(action), Ok(n @ 1..=9)) = (current_action(&editor), chord.parse::<usize>()) {
            let context = context_of(&editor);
            if let Some(key) = keymap::keys_in(context, action, &overrides).get(n - 1) {
                save(app, window, &keymap::remove(&overrides, action, context, key));
            }
        }
    } else if editor.filtering {
        match chord {
            "Escape" => {
                editor.filter.clear();
                editor.filtering = false;
            }
            "Return" | "Down" | "Up" => editor.filtering = false,
            "Backspace" => {
                editor.filter.pop();
            }
            _ if plain && text.chars().count() == 1 && !text.chars().any(char::is_control) => editor.filter += text,
            _ => {}
        }
        editor.current = 0;
    } else {
        editor.status.clear();
        editor.error.clear();
        let count = filtered(&editor.filter).len();
        let context = context_of(&editor);
        let action = current_action(&editor);
        match chord {
            "Down" => editor.current = (editor.current + 1).min(count.saturating_sub(1)),
            "Up" => editor.current = editor.current.saturating_sub(1),
            "PageDown" => editor.current = (editor.current + VISIBLE).min(count.saturating_sub(1)),
            "PageUp" => editor.current = editor.current.saturating_sub(VISIBLE),
            "Home" => editor.current = 0,
            "End" => editor.current = count.saturating_sub(1),
            "Right" => editor.context = (editor.context + 1) % CONTEXTS.len(),
            "Left" => editor.context = (editor.context + CONTEXTS.len() - 1) % CONTEXTS.len(),
            "Return" | "A" if action.is_some() => {
                editor.capturing = true;
                editor.status = format!("Press a key for {} in {}", label(action.unwrap_or_default()), keymap::context_name(context));
            }
            "Delete" | "D" => {
                if let Some(action) = action {
                    let keys = keymap::keys_in(context, action, &overrides);
                    match keys.as_slice() {
                        [] => {}
                        [only] => save(app, window, &keymap::remove(&overrides, action, context, only)),
                        many => {
                            editor.deleting = true;
                            let list: Vec<String> = many.iter().take(9).enumerate().map(|(i, k)| format!("{} {}", i + 1, keymap::display(k))).collect();
                            editor.status = format!("Delete which? {}", list.join(" · "));
                        }
                    }
                }
            }
            "R" => {
                if let Some(action) = action.filter(|a| keymap::customised(&overrides, a, context)) {
                    save(app, window, &keymap::reset(&overrides, action, context));
                    editor.status = format!("{} is back to its default keys in {}", label(action), keymap::context_name(context));
                }
            }
            "/" => {
                editor.filtering = true;
                editor.filter.clear();
                editor.current = 0;
            }
            "Tab" => window.invoke_keymap_focus_json(),
            "Escape" => close = true,
            _ => {}
        }
    }
    STATE.with(|s| *s.borrow_mut() = editor);
    if let Some(key) = add_now {
        commit(app, window, &key);
    } else if close {
        crate::commands::close_overlay(app, window);
    } else {
        render(app, window);
    }
    true
}

/// A row clicked: it becomes the current one.
pub fn clicked(app: &App, window: &AppWindow, action: &str) {
    STATE.with(|s| {
        let mut editor = s.borrow_mut();
        if let Some(index) = filtered(&editor.filter).iter().position(|(a, _)| *a == action) {
            editor.current = index;
        }
    });
    render(app, window);
}

/// The profile imported (or reset): the rows show it.
pub fn imported(app: &App, window: &AppWindow) {
    crate::commands::refresh_keys(app, window);
    render(app, window);
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn find_matches_labels_and_action_names() {
        assert!(filtered("next agent").iter().any(|(a, _)| *a == "next-agent"));
        assert!(filtered("NEXT AGENT").iter().any(|(a, _)| *a == "next-agent"));
        assert!(filtered("split-right").iter().any(|(a, _)| *a == "split-right"));
        assert_eq!(filtered("").len(), keymap::actions().len());
    }
}
