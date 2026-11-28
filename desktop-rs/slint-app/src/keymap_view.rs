//! The key bindings editor (Ctrl+Alt+,): every action's keys in one
//! context at a time, all from the keyboard. Up/Down move between actions,
//! Left/Right between contexts, Enter captures a key to add (the same key
//! twice quickly is a double press), Delete takes one away, R resets the
//! action there, / finds an action, Tab edits the profile as JSON. A key
//! another action has is shown as a clash: Enter takes it over.

use std::cell::RefCell;

use clarp_core::tr;
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

/// The actions `filter` finds: by their label in the app's language, by
/// their English command name, or by their id.
fn filtered(filter: &str) -> Vec<(&'static str, String)> {
    let filter = filter.to_lowercase();
    keymap::actions()
        .into_iter()
        .filter(|(action, label)| {
            filter.is_empty()
                || label.to_lowercase().contains(&filter)
                || action.contains(&filter.replace(' ', "-"))
                || crate::catalogue::find(action).is_some_and(|e| e.label.to_lowercase().contains(&filter))
        })
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
                    label: label.as_str().into(),
                    keys: if keys.is_empty() { "—".into() } else { keys.iter().map(|k| keymap::display(k)).collect::<Vec<_>>().join("  ·  ").into() },
                    custom: keymap::customised(&overrides, action, context),
                    current: index == editor.current,
                }
            })
            .collect();
        window.set_keymap_rows(ModelRc::new(VecModel::from(rows)));
        window.set_keymap_context(keymap::context_name(context).into());
        // i18n: key-binding editor, the position of the current action in the list; {0} its number, {1} how many actions are listed
        window.set_keymap_count(tr!("{0} of {1}", (editor.current + 1).min(actions.len()), actions.len()).into());
        window.set_keymap_filter(if editor.filtering || !editor.filter.is_empty() { format!("/{}", editor.filter) } else { String::new() }.into());
        window.set_keymap_status(editor.status.clone().into());
        window.set_keymap_error(editor.error.clone().into());
    });
    crate::commands::show_hints(app, window);
}

/// The shortcut bar while the editor is open: only the keys that do
/// something now.
pub fn hints(app: &App, window: &AppWindow) -> Vec<(String, String)> {
    let pair = |k: &str, l: String| (k.to_owned(), l);
    if window.get_keymap_json_focused() {
        // i18n: key-binding editor, shortcut bar while the profile JSON is edited: Esc goes back to the list of actions
        return vec![pair("Esc", tr!("Back to the actions"))];
    }
    let overrides = crate::commands::overrides(app);
    STATE.with(|s| {
        let editor = s.borrow();
        if editor.capturing {
            return vec![
                // i18n: key-binding editor, shortcut bar while waiting for the key to bind; pressing it twice quickly binds a double press
                pair("", tr!("Press the key, twice quickly for a double press")),
                // i18n: key-binding editor, shortcut bar: Esc cancels (verb)
                pair("Esc", tr!("verb" => "Cancel")),
            ];
        }
        if editor.clash.is_some() {
            return vec![
                // i18n: key-binding editor, shortcut bar when the key belongs to another action: Enter moves it to this action
                pair("Enter", tr!("Take it over")),
                // i18n: key-binding editor, shortcut bar when the key belongs to another action: Esc leaves it with that action
                pair("Esc", tr!("Keep it there")),
            ];
        }
        if editor.deleting {
            return vec![
                // i18n: key-binding editor, shortcut bar while choosing which key to delete by its number
                pair("1-9", tr!("Delete that key")),
                // i18n: key-binding editor, shortcut bar: Esc cancels (verb)
                pair("Esc", tr!("verb" => "Cancel")),
            ];
        }
        if editor.filtering {
            return vec![
                // i18n: key-binding editor, shortcut bar while typing a filter: Enter ends the filtering (OS term "Done")
                pair("Enter", tr!("Done")),
                // i18n: key-binding editor, shortcut bar while typing a filter: Esc empties the filter (verb)
                pair("Esc", tr!("verb" => "Clear")),
            ];
        }
        let context = context_of(&editor);
        let action = current_action(&editor);
        let mut out = vec![
            // i18n: key-binding editor, shortcut bar: the arrows move between actions (noun)
            pair("↑↓", tr!("Action")),
            // i18n: key-binding editor, shortcut bar: the arrows move between key contexts, where a key works (noun)
            pair("←→", tr!("Context")),
            // i18n: key-binding editor, shortcut bar: Enter adds a key to the action (verb phrase)
            pair("Enter", tr!("Add key")),
        ];
        if action.is_some_and(|a| !keymap::keys_in(context, a, &overrides).is_empty()) {
            // i18n: key-binding editor, shortcut bar: Del removes a key from the action (verb phrase)
            out.push(pair("Del", tr!("Delete key")));
        }
        if action.is_some_and(|a| keymap::customised(&overrides, a, context)) {
            // i18n: key-binding editor, shortcut bar: R puts the action's default keys back (verb)
            out.push(pair("R", tr!("verb" => "Reset")));
        }
        out.extend([
            // i18n: key-binding editor, shortcut bar: / filters the actions (verb)
            pair("/", tr!("verb" => "Find")),
            // i18n: key-binding editor, shortcut bar: Tab edits the key bindings as JSON text (noun: the profile as JSON)
            pair("Tab", tr!("Profile JSON")),
            // i18n: key-binding editor, shortcut bar: Esc closes the editor (verb)
            pair("Esc", tr!("verb" => "Close")),
        ]);
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
            (runs(key, action, context), String::new(), None)
        }
        // i18n: key-binding editor, error when the key belongs to another action; {} is "<key> already runs <action> in <context>"; Enter and Esc are keys
        Err(refusal @ Refusal::Clash(_)) => (String::new(), tr!("{}. Enter takes it over, Esc keeps it there.", refusal), Some(key.to_owned())),
        Err(refusal) => (String::new(), refusal.to_string(), None),
    };
    STATE.with(|s| {
        let mut editor = s.borrow_mut();
        (editor.status, editor.error, editor.clash) = (status, error, clash);
    });
    render(app, window);
}

fn label(action: &str) -> String {
    keymap::actions().into_iter().find(|(a, _)| *a == action).map_or_else(String::new, |(_, l)| l)
}

/// The status once a key is bound: "<key> runs <action> in <context>".
fn runs(key: &str, action: &str, context: &str) -> String {
    // i18n: key-binding editor, status after a key is bound; {0} the key, {1} the action's name, {2} the key context's name
    tr!("{0} runs {1} in {2}", keymap::display(key), label(action), keymap::context_name(context))
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
                // i18n: key-binding editor, status after the first press; {0} the key pressed, {1} the double press of it (e.g. "Right ×2")
                editor.status = tr!("{0}… press it again for {1}", keymap::display(chord), keymap::display(&format!("{chord} {chord}")));
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
                        editor.status = runs(&key, action, context_of(&editor));
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
                // i18n: key-binding editor, status while waiting for a key; {0} the action's name, {1} the key context's name
                editor.status = tr!("Press a key for {0} in {1}", label(action.unwrap_or_default()), keymap::context_name(context));
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
                            // i18n: key-binding editor, asks which of the action's keys to delete; {} lists them as "1 Ctrl+J · 2 N"
                            editor.status = tr!("Delete which? {}", list.join(" · "));
                        }
                    }
                }
            }
            "R" => {
                if let Some(action) = action.filter(|a| keymap::customised(&overrides, a, context)) {
                    save(app, window, &keymap::reset(&overrides, action, context));
                    // i18n: key-binding editor, status after a reset; {0} the action's name, {1} the key context's name
                    editor.status = tr!("{0} is back to its default keys in {1}", label(action), keymap::context_name(context));
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
