//! `--check vim`: the window driven by keys alone, no Ctrl: Escape to
//! Normal, the explorer and back to switch chats, scrolling with counts,
//! `gg`/`G`, searching the chat with `/`, `n` and `N`, copying a message
//! with `y`, turns with `[` and `]`, the leader's which-key panel, splits
//! and tabs from the leader and the command line, settings and back, and
//! Insert typing letters as letters. Then the Ctrl keys for tabs (Ctrl+T,
//! Ctrl+Tab, Ctrl+Shift+Tab) and Ctrl+B handing the explorer the keyboard.
//! Needs the fake Host.

use std::time::Duration;

use slint::platform::Key;
use slint::Model;

use super::{Stage, app_now, check, control, report, run_stages, shot};
use crate::headless;

fn selected() -> String {
    app_now().engine.borrow().selected_session().to_owned()
}

fn workspaces() -> (usize, String) {
    let app = app_now();
    let engine = app.engine.borrow();
    (engine.panes().workspaces().len(), engine.panes().active_workspace().to_owned())
}

fn panes() -> usize {
    app_now().engine.borrow().panes().pane_count()
}

fn which(window: &crate::AppWindow) -> Vec<String> {
    window.get_vim_which().iter().map(|h| format!("{} {}", h.keys, h.label)).collect()
}

fn clipboard() -> String {
    std::env::var_os("CLARP_TEST_CLIPBOARD").and_then(|p| std::fs::read_to_string(p).ok()).unwrap_or_default()
}

/// Types a command line's text and Enter.
fn ex(text: &str) {
    headless::press_with(&[Key::Shift], ":");
    headless::type_text(text);
    headless::press(Key::Return);
}

pub fn vim_check(out: String) {
    let (out2, out3, out4) = (out.clone(), out.clone(), out.clone());
    let offset = std::rc::Rc::new(std::cell::Cell::new(0.0f32));
    let (offset2, offset3) = (offset.clone(), offset.clone());
    let tabs = std::rc::Rc::new(std::cell::RefCell::new(String::new()));
    let (tabs2, tabs3, tabs4) = (tabs.clone(), tabs.clone(), tabs.clone());
    let stages: Vec<Stage> = vec![
        ("ready", Box::new(|app, window, _| {
            let open = app.engine.borrow().conversation("rachel").is_some_and(|c| !c.rows().is_empty());
            if !open || !report().composer_focused {
                return false;
            }
            check(crate::commands::vim_on(app), "vim mode is on by default");
            check(window.get_vim_mode() == "INSERT", &format!("the composer is Insert: {:?}", window.get_vim_mode()));
            let rich = serde_json::json!({"session": "mike", "count": 40});
            check(control("/__control/rich", &rich).is_ok(), "Mike has a long chat");
            headless::press(Key::Escape);
            true
        })),
        ("normal", Box::new(move |_, window, elapsed| {
            if window.get_keyboard_mode() != "CHAT" || elapsed < Duration::from_millis(200) {
                return false;
            }
            check(window.get_vim_mode() == "NORMAL", &format!("Escape is Normal mode: {:?}", window.get_vim_mode()));
            shot(&out, "vim-01-normal");
            // h: the explorer, its cursor on this chat.
            headless::press("h");
            true
        })),
        ("explorer", Box::new(|_, window, _| {
            if !window.get_sidebar_focused() {
                return false;
            }
            check(window.get_sidebar_cursor() == "rachel" && window.get_vim_mode() == "NORMAL", "h goes to the explorer, on the open chat, still Normal");
            headless::press("j");
            true
        })),
        ("explorer j", Box::new(|_, window, _| {
            if window.get_sidebar_cursor() != "mike" {
                return false;
            }
            check(true, "j moves the explorer's cursor");
            headless::press("l");
            true
        })),
        ("opened", Box::new(move |_, window, elapsed| {
            let rows = app_now().active_messages().map_or(0, |m| m.row_count());
            if selected() != "mike" || rows < 40 || window.get_keyboard_mode() != "CHAT" || !report().at_end || elapsed < Duration::from_millis(600) {
                return false;
            }
            check(true, "l opens Mike's chat with the keyboard on the chat, no Ctrl");
            offset.set(report().offset);
            headless::type_text("3k");
            true
        })),
        ("3k", Box::new(move |_, _, elapsed| {
            let moved = report().offset - offset2.get();
            if moved < 100.0 && elapsed < Duration::from_secs(2) {
                return false;
            }
            check((110.0..=130.0).contains(&moved), &format!("3k scrolls up three lines: {moved}px"));
            check(!report().at_end, "and leaves the end");
            offset2.set(report().offset);
            headless::press("u");
            true
        })),
        ("u", Box::new(move |_, _, elapsed| {
            let moved = report().offset - offset3.get();
            if moved < 150.0 && elapsed < Duration::from_secs(2) {
                return false;
            }
            check(moved >= 150.0, &format!("u scrolls up half a page: {moved}px"));
            headless::press_with(&[Key::Shift], "G");
            true
        })),
        ("G", Box::new(|_, _, elapsed| {
            if !report().at_end && elapsed < Duration::from_secs(2) {
                return false;
            }
            check(report().at_end, "G goes to the latest");
            headless::type_text("gg");
            true
        })),
        ("gg", Box::new(|_, _, elapsed| {
            if report().offset < -2.0 && elapsed < Duration::from_secs(3) {
                return false;
            }
            check(report().offset >= -2.0, &format!("gg goes to the top: {}", report().offset));
            check(crate::vim_view::cursor() == "mike-0", &format!("on the first row: {:?}", crate::vim_view::cursor()));
            headless::press("/");
            true
        })),
        ("search line", Box::new(|_, window, _| {
            if window.get_vim_line() != "/" {
                return false;
            }
            check(window.get_vim_mode() == "SEARCH", "/ opens the search line");
            headless::type_text("Table");
            headless::press(Key::Return);
            true
        })),
        ("found", Box::new(|_, window, _| {
            if crate::vim_view::cursor() != "mike-4" {
                return false;
            }
            check(window.get_vim_line().is_empty() && window.get_vim_mode() == "NORMAL", "Enter finds \"Table\" in row 4 and closes the line");
            headless::press("n");
            true
        })),
        ("n", Box::new(|_, _, _| {
            if crate::vim_view::cursor() != "mike-14" {
                return false;
            }
            check(true, "n finds the next match");
            headless::press_with(&[Key::Shift], "N");
            true
        })),
        ("N", Box::new(|_, _, _| {
            if crate::vim_view::cursor() != "mike-4" {
                return false;
            }
            check(true, "N goes back to the one before");
            headless::press("y");
            true
        })),
        ("yank", Box::new(|_, window, _| {
            if !clipboard().starts_with("Table 4") {
                return false;
            }
            check(window.get_vim_message().contains("Copied"), &format!("y copies the message and says so: {:?}", window.get_vim_message()));
            headless::press("]");
            true
        })),
        ("next turn", Box::new(|_, _, _| {
            if crate::vim_view::cursor() != "mike-9" {
                return false;
            }
            check(true, "] goes to the next turn of the user's");
            headless::press("[");
            true
        })),
        ("previous turn", Box::new(|_, _, _| {
            if crate::vim_view::cursor() != "mike-3" {
                return false;
            }
            check(true, "[ goes to the turn before");
            headless::press(" ");
            true
        })),
        ("leader", Box::new(move |_, window, elapsed| {
            if window.get_vim_which().row_count() == 0 || elapsed < Duration::from_millis(200) {
                return false;
            }
            let shown = which(window);
            check(shown.iter().any(|h| h == "w Tabs and panes") && shown.iter().any(|h| h == "s Settings"), &format!("Space shows what may follow: {shown:?}"));
            shot(&out2, "vim-02-which-key");
            headless::press("w");
            true
        })),
        ("leader w", Box::new(|_, window, _| {
            if !which(window).iter().any(|h| h == "v Split right") {
                return false;
            }
            check(window.get_vim_pending() == "Space w", &format!("the panel says what was typed: {:?}", window.get_vim_pending()));
            headless::press("v");
            true
        })),
        ("split", Box::new(|_, window, _| {
            if panes() != 2 {
                return false;
            }
            check(window.get_vim_which().row_count() == 0, "Space w v splits the pane and the panel goes");
            ex("q");
            true
        })),
        (":q", Box::new(|_, _, _| {
            if panes() != 1 {
                return false;
            }
            check(true, ":q closes the pane");
            ex("vs");
            true
        })),
        (":vs", Box::new(|_, _, _| {
            if panes() != 2 {
                return false;
            }
            check(true, ":vs splits it again");
            headless::press(" ");
            headless::press("w");
            headless::press("q");
            true
        })),
        ("closed", Box::new(move |_, _, _| {
            if panes() != 1 {
                return false;
            }
            check(true, "Space w q closes it");
            *tabs.borrow_mut() = workspaces().1;
            ex("tabnew Review");
            true
        })),
        (":tabnew", Box::new(move |app, _, _| {
            let (count, active) = workspaces();
            if count != 2 || active == *tabs2.borrow() {
                return false;
            }
            let named = app.engine.borrow().panes().workspaces().iter().any(|w| w["name"] == "Review");
            check(named, ":tabnew Review opens a tab of that name");
            headless::type_text("gt");
            true
        })),
        ("gt", Box::new(move |_, _, _| {
            if workspaces().1 != *tabs3.borrow() {
                return false;
            }
            check(true, "gt goes to the next tab (round to the first)");
            headless::type_text("g");
            headless::press_with(&[Key::Shift], "T");
            true
        })),
        ("gT", Box::new(move |_, _, _| {
            if workspaces().1 == *tabs4.borrow() {
                return false;
            }
            check(true, "gT goes to the previous tab");
            ex("tabclose");
            true
        })),
        (":tabclose", Box::new(|_, _, _| {
            if workspaces().0 != 1 {
                return false;
            }
            check(true, ":tabclose closes the tab");
            headless::press(" ");
            headless::press("s");
            true
        })),
        ("settings", Box::new(|_, window, _| {
            if window.get_surface() != "settings" {
                return false;
            }
            check(true, "Space s opens the settings");
            headless::press(Key::Escape);
            true
        })),
        ("back", Box::new(|_, window, _| {
            if window.get_surface() != "chats" {
                return false;
            }
            check(true, "Escape goes back to the chats");
            headless::press(Key::Escape);
            headless::press_with(&[Key::Shift], ":");
            headless::type_text("theme h");
            headless::press(Key::Tab);
            true
        })),
        ("completed", Box::new(move |_, window, _| {
            if window.get_vim_line() != ":theme hacker" {
                return false;
            }
            check(window.get_vim_mode() == "COMMAND", "the command line is its own mode");
            shot(&out3, "vim-03-command-line");
            headless::press(Key::Return);
            true
        })),
        (":theme", Box::new(|app, window, _| {
            if app.engine.borrow().reading_theme() != "hacker" {
                return false;
            }
            check(window.get_vim_line().is_empty(), "Tab completes :theme hacker and Enter applies it");
            headless::press("i");
            true
        })),
        ("insert", Box::new(|_, window, _| {
            if !report().composer_focused {
                return false;
            }
            check(window.get_vim_mode() == "INSERT", "i is Insert in the composer");
            headless::type_text("jk gg:/");
            true
        })),
        ("typed", Box::new(|app, _, _| {
            if app.active_draft() != "jk gg:/" {
                return false;
            }
            check(true, "in Insert vim keys type as letters");
            headless::press(Key::Escape);
            true
        })),
        // ---- the Ctrl keys for tabs, and Ctrl+B.
        ("ctrl+t", Box::new(|_, window, _| {
            if window.get_vim_mode() != "NORMAL" {
                return false;
            }
            headless::press_with(&[Key::Control], "t");
            true
        })),
        ("new tab", Box::new(|_, _, _| {
            if workspaces().0 != 2 {
                return false;
            }
            check(true, "Ctrl+T opens a new tab");
            true
        })),
        ("ctrl+tab", Box::new(|_, _, elapsed| {
            if elapsed < Duration::from_millis(300) {
                return false;
            }
            let before = workspaces().1;
            headless::press_with(&[Key::Control, Key::Shift], Key::Backtab);
            let after = workspaces().1;
            check(before != after, "Ctrl+Shift+Tab goes to the other tab");
            headless::press_with(&[Key::Control], Key::Tab);
            check(workspaces().1 == before, "and Ctrl+Tab back");
            headless::press_with(&[Key::Control], "w");
            true
        })),
        ("ctrl+w", Box::new(|_, window, _| {
            if workspaces().0 != 1 {
                return false;
            }
            check(true, "Ctrl+W closes the tab from the chat");
            if !window.get_sidebar_visible() {
                headless::press_with(&[Key::Control], "b");
            }
            true
        })),
        ("ctrl+b hides", Box::new(|app, window, _| {
            if !window.get_sidebar_visible() {
                return false;
            }
            app.focus_transcript();
            headless::press_with(&[Key::Control], "b");
            true
        })),
        ("hidden", Box::new(|_, window, _| {
            if window.get_sidebar_visible() {
                return false;
            }
            headless::press_with(&[Key::Control], "b");
            true
        })),
        ("ctrl+b shows", Box::new(|_, window, _| {
            if !window.get_sidebar_visible() || !window.get_sidebar_focused() {
                return false;
            }
            check(window.get_sidebar_cursor() == selected().as_str(), "Ctrl+B shows the explorer with the keyboard on the open chat");
            headless::press_with(&[Key::Control], "b");
            true
        })),
        ("ctrl+b back", Box::new(move |_, window, elapsed| {
            if window.get_sidebar_visible() || window.get_keyboard_mode() != "CHAT" {
                return elapsed > Duration::from_secs(2) && {
                    check(false, &format!("Ctrl+B hiding it gives the chat the keyboard: {}", window.get_keyboard_mode()));
                    true
                };
            }
            check(true, "Ctrl+B hiding it gives the chat the keyboard");
            shot(&out4, "vim-04-explorer-hidden");
            headless::press_with(&[Key::Shift], ":");
            headless::type_text("set novim");
            headless::press(Key::Return);
            true
        })),
        ("novim", Box::new(|app, window, _| {
            if crate::commands::vim_on(app) {
                return false;
            }
            check(window.get_vim_mode().is_empty(), ":set novim turns vim mode off");
            headless::press("j");
            true
        })),
        ("classic", Box::new(|_, window, elapsed| {
            if elapsed < Duration::from_millis(300) {
                return false;
            }
            check(window.get_keyboard_mode() == "CHAT" && window.get_vim_which().row_count() == 0, "with vim off the classic keys are back");
            true
        })),
    ];
    run_stages(stages);
}
