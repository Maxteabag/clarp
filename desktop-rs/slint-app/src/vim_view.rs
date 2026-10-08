//! Vim mode in the window: Normal mode's keys, the command line and the
//! chat search, the cursor row, and what the shortcut bar and the
//! which-key panel show. The keys themselves are `vim.rs`'s.

use std::cell::RefCell;
use std::rc::Rc;

use clarp_engine::Change;
use slint::{ComponentHandle, Model, ModelRc, VecModel};

use crate::vim::{self, Ex, Font, LineOutcome, Outcome, Region};
use crate::{App, AppWindow, Hint, commands, pump_now};

/// The setting that turns vim mode on and off.
pub const SETTING: &str = "keymap/vim";

#[derive(Default)]
struct State {
    normal: vim::Normal,
    line: Option<vim::Line>,
    /// The last search, for n and N.
    search: String,
    /// The chat row the cursor is on ("" for none: the view's top row, or
    /// the latest message at the end).
    cursor: String,
    /// What the last command said ("Copied", an error).
    message: String,
}

thread_local! {
    static STATE: RefCell<State> = RefCell::new(State::default());
}

/// The id of the chat row the vim cursor is on ("" for none).
pub fn cursor() -> String {
    STATE.with(|s| s.borrow().cursor.clone())
}

fn say(message: impl Into<String>) {
    STATE.with(|s| s.borrow_mut().message = message.into());
}

/// A key in vim mode: Some(used) when vim mode took it, None to leave it
/// to the keyboard map. `state` is the map's state, `text` what the key
/// types.
pub fn key(app: &Rc<App>, window: &AppWindow, state: &str, text: &str, chord: &str) -> Option<bool> {
    // The command line takes every key while it is open.
    if STATE.with(|s| s.borrow().line.is_some()) {
        line_key(app, window, chord, text);
        commands::show_hints(app, window);
        return Some(true);
    }
    let region = match state {
        "pane" => Region::Chat,
        "sidebar" => Region::Explorer,
        _ => {
            STATE.with(|s| s.borrow_mut().normal.reset());
            return None;
        }
    };
    STATE.with(|s| s.borrow_mut().message.clear());
    // The user's own keys win, unless a sequence is under way.
    let pending = STATE.with(|s| s.borrow().normal.pending());
    if !pending && crate::keymap::user_bound(state, chord, &commands::overrides(app)) {
        return None;
    }
    let on_card = crate::artifacts_view::selected(app).is_some();
    let outcome = STATE.with(|s| s.borrow_mut().normal.feed(region, chord, on_card));
    let used = match outcome {
        Outcome::Pending | Outcome::Cancelled => Some(true),
        Outcome::Pass => None,
        Outcome::Run { action, count } => {
            run(app, window, region, action, count);
            Some(true)
        }
    };
    commands::show_hints(app, window);
    used
}

/// Drops a half-typed sequence (the keyboard moved elsewhere).
pub fn reset() {
    STATE.with(|s| s.borrow_mut().normal.reset());
}

// ---- the command line ---------------------------------------------------------

fn open_line(kind: char) {
    STATE.with(|s| s.borrow_mut().line = Some(vim::Line::new(kind)));
}

fn agent_names(window: &AppWindow) -> Vec<String> {
    window.get_chats().iter().map(|c| c.name.to_string()).collect()
}

fn theme_ids() -> Vec<String> {
    clarp_core::reading_theme::themes().iter().filter_map(|t| t.get("id").and_then(serde_json::Value::as_str)).map(str::to_owned).collect()
}

fn line_key(app: &Rc<App>, window: &AppWindow, chord: &str, text: &str) {
    let (agents, themes) = (agent_names(window), theme_ids());
    let outcome = STATE.with(|s| {
        let mut state = s.borrow_mut();
        let line = state.line.as_mut()?;
        let kind = line.kind;
        Some((kind, line.key(chord, text, |typed| if kind == ':' { vim::complete(typed, &agents, &themes) } else { Vec::new() })))
    });
    let Some((kind, outcome)) = outcome else { return };
    match outcome {
        LineOutcome::Editing => {}
        LineOutcome::Cancelled => STATE.with(|s| s.borrow_mut().line = None),
        LineOutcome::Done(text) => {
            STATE.with(|s| s.borrow_mut().line = None);
            if kind == '/' {
                if !text.is_empty() {
                    STATE.with(|s| s.borrow_mut().search = text);
                }
                search(app, true);
            } else if !text.trim().is_empty() {
                ex(app, window, &text);
            }
        }
    }
}

fn ex(app: &Rc<App>, window: &AppWindow, text: &str) {
    match vim::parse(text) {
        Ok(Ex::Run(action)) => {
            if !commands::run(app, window, action) {
                say(format!("{action} is not available here"));
            }
        }
        Ok(Ex::TabNew(name)) => commands::new_workspace(app, window, &name),
        Ok(Ex::Open(name)) => open_by_name(app, window, &name),
        Ok(Ex::Theme(id)) => {
            if theme_ids().contains(&id) {
                app.engine.borrow_mut().set_reading_theme(&id);
                pump_now(app);
            } else {
                say(format!("No theme {id} (Tab lists them)"));
            }
        }
        Ok(Ex::Font(font)) => {
            let action = match font {
                Font::Picker => "choose-font",
                Font::ThemeDefault => "reset-font",
                Font::Larger => "chat-zoom-in",
                Font::Smaller => "chat-zoom-out",
                Font::Reset => "chat-zoom-reset",
                Font::Zoom(percent) => return set(app, window, &format!("chatzoom={percent}")),
            };
            commands::run(app, window, action);
        }
        Ok(Ex::Set(line)) => set(app, window, &line),
        Ok(Ex::NoHighlight) => STATE.with(|s| s.borrow_mut().search.clear()),
        Err(error) => say(error),
    }
}

/// `:set` is the preferences' own: every name, alias and vim form they
/// know (`fontsize+=2`, `notimestamps`, `novim`).
fn set(app: &Rc<App>, window: &AppWindow, line: &str) {
    if commands::run(app, window, &format!("pref-set:{line}")) {
        STATE.with(|s| s.borrow_mut().normal.reset());
        say(format!(":set {line}"));
    } else {
        say(crate::look::notices().last().cloned().unwrap_or_else(|| format!("Cannot set {line}")));
    }
}

fn open_by_name(app: &Rc<App>, window: &AppWindow, name: &str) {
    let wanted = name.to_lowercase();
    let chats: Vec<(String, String)> = window.get_chats().iter().map(|c| (c.session.to_string(), c.name.to_lowercase())).collect();
    let found = chats.iter().find(|(_, n)| n.starts_with(&wanted)).or_else(|| chats.iter().find(|(_, n)| n.contains(&wanted)));
    let Some((session, _)) = found else { return say(format!("No agent {name}")) };
    app.engine.borrow_mut().select(session);
    pump_now(app);
    app.focus_transcript();
}

// ---- the chat ---------------------------------------------------------------------

/// The active chat's rows: id, author, and the text a search reads.
fn rows(app: &App) -> Vec<(String, String, String)> {
    let Some(messages) = app.active_messages() else { return Vec::new() };
    let engine = app.engine.borrow();
    let conversation = engine.conversation(&app.active_session());
    messages
        .iter()
        .map(|row| {
            let id = row.id.to_string();
            let text = conversation
                .and_then(|c| c.rows().iter().find(|m| m.id == id).map(|m| if m.display_text.is_empty() { m.text.clone() } else { m.display_text.clone() }))
                .unwrap_or_else(|| {
                    let tools: Vec<String> = row.tools.iter().map(|t| format!("{} {}", t.name, t.summary)).collect();
                    format!("{} {}", row.activity_label, tools.join(" "))
                });
            (id, row.author.to_string(), text)
        })
        .collect()
}

/// Where the cursor is: its row, or the view's (the latest at the end).
fn anchor(app: &App, rows: &[(String, String, String)]) -> Option<usize> {
    let cursor = cursor();
    if let Some(at) = rows.iter().position(|(id, _, _)| !cursor.is_empty() && *id == cursor) {
        return Some(at);
    }
    if rows.is_empty() {
        return None;
    }
    let report = app.active_report();
    if report.at_end { Some(rows.len() - 1) } else { Some((report.top.max(0) as usize).min(rows.len() - 1)) }
}

/// Puts the cursor on row `index` and brings it to the top of the view.
fn go(app: &App, rows: &[(String, String, String)], index: usize) {
    STATE.with(|s| s.borrow_mut().cursor = rows[index].0.clone());
    app.scroll_to_row(index);
}

fn search(app: &App, forward: bool) {
    let pattern = STATE.with(|s| s.borrow().search.clone());
    if pattern.is_empty() {
        return say("No search yet: / searches the chat");
    }
    let rows = rows(app);
    let texts: Vec<String> = rows.iter().map(|(_, _, t)| t.clone()).collect();
    // From the top of the chat when nothing is chosen yet and it is at
    // the end, as a fresh search in Vim starts after the cursor.
    let from = anchor(app, &rows);
    match vim::find(&texts, &pattern, from, forward) {
        Some(index) => {
            go(app, &rows, index);
            let count = texts.iter().filter(|t| vim::find(std::slice::from_ref(t), &pattern, None, true).is_some()).count();
            let nth = texts[..=index].iter().filter(|t| vim::find(std::slice::from_ref(t), &pattern, None, true).is_some()).count();
            say(format!("/{pattern}  {nth} of {count}"));
        }
        None => say(format!("Not found: {pattern}")),
    }
}

fn copy(app: &App, rows: &[(String, String, String)]) {
    let cursor = cursor();
    let index = rows.iter().position(|(id, _, _)| !cursor.is_empty() && *id == cursor).or_else(|| {
        let report = app.active_report();
        if report.at_end { rows.iter().rposition(|(_, author, _)| author != "activity" && author != "live") } else { anchor(app, rows) }
    });
    let Some(index) = index else { return say("Nothing to copy") };
    let pane = app.active_id();
    STATE.with(|s| s.borrow_mut().cursor = rows[index].0.clone());
    if let Some(window) = crate::window() {
        window.invoke_copy_message(pane, rows[index].0.clone().into());
    }
    let preview: String = rows[index].2.chars().take(40).collect::<String>().replace('\n', " ");
    say(format!("Copied: {preview}"));
}

/// The tool activity row the cursor is on, or the nearest one after it
/// (before it, at the end).
fn fold(app: &App, window: &AppWindow, open: Option<bool>) {
    let Some(messages) = app.active_messages() else { return };
    let rows: Vec<crate::MessageRow> = messages.iter().collect();
    let ids: Vec<(String, String, String)> = rows.iter().map(|r| (r.id.to_string(), r.author.to_string(), String::new())).collect();
    let Some(at) = anchor(app, &ids) else { return };
    let foldable = |r: &crate::MessageRow| !r.activity_label.is_empty();
    let Some(index) = (at..rows.len()).find(|i| foldable(&rows[*i])).or_else(|| (0..at).rev().find(|i| foldable(&rows[*i]))) else {
        return say("No tool activity to fold");
    };
    let row = &rows[index];
    STATE.with(|s| s.borrow_mut().cursor = row.id.to_string());
    if open.is_none_or(|open| open != row.expanded) {
        window.invoke_toggle_activity(app.active_id(), row.id.clone(), row.group_id.clone());
    }
}

// ---- Normal mode's actions ----------------------------------------------------------

/// The line height a j or k scrolls (an arrow key's).
const LINE: f32 = 40.0;

fn run(app: &Rc<App>, window: &AppWindow, region: Region, action: &'static str, count: Option<u32>) {
    let times = count.unwrap_or(1).clamp(1, 500);
    let clear = || STATE.with(|s| s.borrow_mut().cursor.clear());
    match (action, region) {
        ("vim-command", _) => open_line(':'),
        ("vim-search", _) => open_line('/'),
        ("vim-insert", _) => {
            commands::run(app, window, "focus-composer");
        }
        ("vim-down" | "vim-up", _) => {
            clear();
            app.scroll_by((if action == "vim-down" { LINE } else { -LINE }) * times as f32);
        }
        ("vim-half-down" | "vim-half-up", _) => {
            clear();
            app.scroll_pages((if action == "vim-half-down" { 0.5 } else { -0.5 }) * times as f32);
        }
        ("vim-top" | "vim-bottom", Region::Explorer) => {
            let sessions: Vec<String> = window.get_chats().iter().map(|c| c.session.to_string()).collect();
            let pick = if action == "vim-top" { sessions.first() } else { sessions.last() };
            if let Some(session) = pick {
                window.set_sidebar_cursor(session.as_str().into());
            }
        }
        ("vim-top", Region::Chat) => {
            let rows = rows(app);
            if !rows.is_empty() {
                go(app, &rows, 0);
            }
        }
        ("vim-bottom", Region::Chat) => {
            let rows = rows(app);
            STATE.with(|s| s.borrow_mut().cursor = rows.last().map(|r| r.0.clone()).unwrap_or_default());
            app.to_latest();
        }
        ("vim-turn-next" | "vim-turn-previous", _) => {
            let rows = rows(app);
            let authors: Vec<String> = rows.iter().map(|(_, a, _)| a.clone()).collect();
            let (mut at, mut moved) = (anchor(app, &rows), None);
            for _ in 0..times {
                match vim::turn(&authors, at, action == "vim-turn-next") {
                    Some(next) => (at, moved) = (Some(next), Some(next)),
                    None => break,
                }
            }
            match moved {
                Some(index) => go(app, &rows, index),
                None => say(if action == "vim-turn-next" { "No later turn" } else { "No earlier turn" }),
            }
        }
        ("vim-match-next" | "vim-match-previous", _) => {
            // With no search yet, n is still the next agent needing attention.
            if STATE.with(|s| s.borrow().search.is_empty()) && action == "vim-match-next" {
                if !commands::run(app, window, "next-attention") {
                    say("No search yet: / searches the chat");
                }
            } else {
                for _ in 0..times {
                    search(app, action == "vim-match-next");
                }
            }
        }
        ("vim-yank", _) => copy(app, &rows(app)),
        ("vim-fold-open" | "vim-fold-close", Region::Explorer) => {
            commands::run(app, window, if action == "vim-fold-open" { "unfold" } else { "fold" });
        }
        ("vim-fold-open", Region::Chat) => fold(app, window, Some(true)),
        ("vim-fold-close", Region::Chat) => fold(app, window, Some(false)),
        ("vim-fold-toggle", _) => fold(app, window, None),
        // l in the explorer: a chat with folded sub-agents unfolds; any
        // other opens, with the keyboard on its chat (still Normal).
        ("vim-open", _) => {
            let cursor = window.get_sidebar_cursor().to_string();
            let folded = window.get_chats().iter().any(|c| c.session == cursor.as_str() && c.fold_count > 0 && c.folded);
            if folded {
                commands::run(app, window, "unfold");
            } else if !cursor.is_empty() {
                app.engine.borrow_mut().select(&cursor);
                pump_now(app);
                app.focus_transcript();
            }
        }
        // {count}gt: that tab, as in Vim.
        ("next-workspace", _) if count.is_some() => {
            let ids: Vec<String> = app.engine.borrow().panes().workspaces().iter().map(|w| clarp_core::json::string(w, "id")).collect();
            if let Some(id) = ids.get(times as usize - 1) {
                app.engine.borrow_mut().with_panes(|p| p.switch_workspace(id));
                pump_now(app);
                app.refresh(&[Change::Panes]);
                app.focus_transcript();
            }
        }
        // Moves repeat with a count; the rest run once.
        ("agent-next" | "agent-previous" | "artifact-next" | "artifact-previous" | "previous-workspace", _) => {
            for _ in 0..times {
                commands::run(app, window, action);
            }
        }
        _ => {
            if !commands::run(app, window, action) {
                say(format!("{action} is not available here"));
            }
        }
    }
}

// ---- what the window shows ------------------------------------------------------------

/// The mode, the command line, the which-key panel and the message, for
/// the keyboard map's `state`.
pub fn show(app: &App, window: &AppWindow, state: &str) {
    let on = commands::vim_on(app);
    let (mode, line, pending, which, message, cursor) = STATE.with(|s| {
        let s = s.borrow();
        let region = if state == "sidebar" { Region::Explorer } else { Region::Chat };
        let mode = match (&s.line, state) {
            _ if !on => "",
            (Some(line), _) if line.kind == '/' => "SEARCH",
            (Some(_), _) => "COMMAND",
            (None, "composer") => "INSERT",
            (None, "pane" | "sidebar") => "NORMAL",
            _ => "",
        };
        let line = s.line.as_ref().filter(|_| on).map(|l| format!("{}{}", l.kind, l.text)).unwrap_or_default();
        let which: Vec<Hint> = if on && matches!(state, "pane" | "sidebar") {
            s.normal.next(region).into_iter().map(|(keys, label)| Hint { keys: keys.into(), label: label.into() }).collect()
        } else {
            Vec::new()
        };
        let pending = if which.is_empty() { String::new() } else { s.normal.typed() };
        (mode, line, pending, which, if on { s.message.clone() } else { String::new() }, s.cursor.clone())
    });
    // This runs on every scroll report: only what changed is set, so an
    // unchanged panel never makes the window draw again.
    if window.get_vim_mode() != mode {
        window.set_vim_mode(mode.into());
    }
    if window.get_vim_line() != line.as_str() {
        window.set_vim_line(line.into());
    }
    if window.get_vim_pending() != pending.as_str() {
        window.set_vim_pending(pending.into());
    }
    let shown: Vec<Hint> = window.get_vim_which().iter().collect();
    if shown != which {
        window.set_vim_which(ModelRc::new(VecModel::from(which)));
    }
    if window.get_vim_message() != message.as_str() {
        window.set_vim_message(message.into());
    }
    let bridge = window.global::<crate::VimBridge>();
    if bridge.get_mode() != mode {
        bridge.set_mode(mode.into());
    }
    if bridge.get_cursor() != cursor.as_str() {
        bridge.set_cursor(cursor.into());
    }
}

/// The shortcut bar's vim hints for `state`, before the map's own.
pub fn hints(state: &str) -> Vec<Hint> {
    let hint = |keys: &str, label: &str| Hint { keys: keys.into(), label: label.into() };
    match state {
        "pane" => vec![hint("i", "Insert"), hint("Space", "Leader"), hint(":", "Command"), hint("/", "Search"), hint("j/k d/u", "Scroll"), hint("[ ]", "Turns"), hint("y", "Copy")],
        "sidebar" => vec![hint("l", "Open"), hint("h", "Chat"), hint("Space", "Leader"), hint(":", "Command")],
        "composer" => vec![hint("Esc", "Normal")],
        _ => Vec::new(),
    }
}
