//! `--check refocus`: the window loses the keyboard to another window and
//! gets it back (Super+number, Alt+Tab, a click elsewhere), each time with
//! a modifier still down when it left and no release seen, and every
//! keyboard state still works afterwards: typing in the composer, J/K in
//! the explorer, Ctrl+E, Ctrl+K and Escape. Needs the fake Host.

use std::time::Duration;

use slint::platform::Key;

use super::{Stage, check, report, run_stages, shot};
use crate::headless;
use crate::platform::keyboard::Modifiers;

/// Leaves for another window with `modifier` held and comes back, as
/// winit reports it on Wayland: the pointer leaves with the keyboard and
/// enters again over the chat (no key release reaches the window).
fn away_and_back(modifier: Key) {
    use slint::ComponentHandle;
    headless::hold(modifier);
    headless::pointer_exit();
    headless::set_active(false);
    headless::set_active(true);
    if let Some(window) = crate::window() {
        let size = window.window().size().to_logical(window.window().scale_factor());
        headless::pointer_move(size.width * 0.6, size.height * 0.4);
    }
}

pub fn refocus_check(out: String) {
    let (out2, out3, out4) = (out.clone(), out.clone(), out.clone());
    let stages: Vec<Stage> = vec![
        ("ready", Box::new(|app, _window, _| {
            let open = app.engine.borrow().conversation("rachel").is_some_and(|c| !c.rows().is_empty());
            if !open || !report().composer_focused {
                return false;
            }
            // Super+2 to another workspace, Super+1 back.
            away_and_back(Key::Meta);
            true
        })),
        ("composer back", Box::new(|app, window, elapsed| {
            if elapsed < Duration::from_millis(200) {
                return false;
            }
            check(report().composer_focused, &format!("the composer has the keyboard again after Super+number: {:?}", app.active_report()));
            check(window.get_keyboard_mode() == "INSERT", &format!("the bar is back in INSERT: {}", window.get_keyboard_mode()));
            headless::type_text("back again");
            true
        })),
        ("typed", Box::new(move |app, _window, elapsed| {
            if app.active_draft() != "back again" {
                return elapsed > Duration::from_secs(2) && {
                    check(false, &format!("typing reaches the composer after coming back: {:?}", app.active_draft()));
                    true
                };
            }
            check(true, "typing reaches the composer after coming back");
            shot(&out, "refocus-01-composer");
            // Ctrl held into an Alt+Tab away, and back.
            away_and_back(Key::Control);
            headless::press_with(&[Key::Control], "e");
            true
        })),
        ("explorer", Box::new(|_, window, elapsed| {
            if !window.get_sidebar_focused() {
                return elapsed > Duration::from_secs(2) && {
                    check(false, &format!("Ctrl+E reaches the explorer after coming back: {}", window.get_keyboard_mode()));
                    true
                };
            }
            check(window.get_keyboard_mode() == "EXPLORER", "Ctrl+E reaches the explorer after coming back");
            // Alt held into an Alt+Tab away, and back: J moves the cursor.
            away_and_back(Key::Alt);
            check(window.get_sidebar_focused(), "the explorer keeps the keyboard across the switch");
            headless::press("j");
            true
        })),
        ("j", Box::new(|_, window, elapsed| {
            if window.get_sidebar_cursor() != "mike" {
                return elapsed > Duration::from_secs(2) && {
                    check(false, &format!("J moves the explorer's cursor after coming back: {}", window.get_sidebar_cursor()));
                    true
                };
            }
            check(true, "J moves the explorer's cursor after coming back");
            away_and_back(Key::Meta);
            headless::press("k");
            true
        })),
        ("k", Box::new(move |_, window, elapsed| {
            if window.get_sidebar_cursor() != "rachel" {
                return elapsed > Duration::from_secs(2) && {
                    check(false, &format!("K moves it back after coming back: {}", window.get_sidebar_cursor()));
                    true
                };
            }
            check(true, "K moves it back after coming back");
            shot(&out2, "refocus-02-explorer");
            away_and_back(Key::Shift);
            headless::press_with(&[Key::Control], "k");
            true
        })),
        ("switcher", Box::new(|_, window, elapsed| {
            if !window.get_switcher_open() {
                return elapsed > Duration::from_secs(2) && {
                    check(false, "Ctrl+K opens the switcher after coming back");
                    true
                };
            }
            check(true, "Ctrl+K opens the switcher after coming back");
            headless::press(Key::Escape);
            true
        })),
        ("switcher closed", Box::new(|_, window, elapsed| {
            if window.get_switcher_open() {
                return elapsed > Duration::from_secs(2) && {
                    check(false, "Escape closes the switcher");
                    true
                };
            }
            // From the explorer Escape goes to the conversation.
            headless::press(Key::Escape);
            true
        })),
        ("transcript", Box::new(|app, _window, _| {
            if !report().transcript_focused {
                return false;
            }
            // Away from the conversation with Ctrl and Alt both down.
            headless::hold(Key::Control);
            away_and_back(Key::Alt);
            check(app.active_report().transcript_focused, "the conversation keeps the keyboard across the switch");
            headless::press("i");
            true
        })),
        ("insert", Box::new(move |app, window, elapsed| {
            if !report().composer_focused {
                return elapsed > Duration::from_secs(2) && {
                    check(false, &format!("I reaches the composer from the conversation after coming back: {:?}", app.active_report()));
                    true
                };
            }
            check(window.get_keyboard_mode() == "INSERT", "I reaches the composer from the conversation after coming back");
            shot(&out3, "refocus-03-insert");
            // Back with Alt+Tab, Ctrl seen going down but its release taken
            // by the input method: the window system says nothing is held.
            headless::hold(Key::Control);
            headless::modifiers_changed(Modifiers::default());
            headless::type_text("ty");
            true
        })),
        ("typed past a lost release", Box::new(|app, window, elapsed| {
            if !app.active_draft().ends_with("ty") {
                return elapsed > Duration::from_secs(2) && {
                    check(false, &format!("typing works once the window system says Ctrl is up: {:?} (switcher open {})", app.active_draft(), window.get_switcher_open()));
                    true
                };
            }
            check(!window.get_switcher_open(), "typing works once the window system says Ctrl is up");
            headless::press_with(&[Key::Control], "e");
            true
        })),
        ("explorer past a lost release", Box::new(|_, window, elapsed| {
            if !window.get_sidebar_focused() {
                return false;
            }
            if elapsed < Duration::from_millis(200) {
                return false;
            }
            headless::hold(Key::Alt);
            headless::modifiers_changed(Modifiers::default());
            headless::press("j");
            true
        })),
        ("j past a lost release", Box::new(move |_, window, elapsed| {
            if window.get_sidebar_cursor() != "mike" {
                return elapsed > Duration::from_secs(2) && {
                    check(false, &format!("J moves the cursor once the window system says Alt is up: {}", window.get_sidebar_cursor()));
                    true
                };
            }
            check(true, "J moves the cursor once the window system says Alt is up");
            shot(&out4, "refocus-04-lost-release");
            true
        })),
    ];
    run_stages(stages);
}
