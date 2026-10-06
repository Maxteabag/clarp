//! `--check fonts --out DIR`: the reader's own font per theme, from the
//! keyboard. On Hacker, Ctrl+K "Choose font…" opens the picker over the
//! chat; Ctrl+M keeps monospace families, typing filters, and the chat shows
//! the current family as it moves (still at its latest message); Escape puts
//! the theme's font back. Chosen again (Down, Ctrl+Up for a size, Enter) it
//! is Hacker's alone: Paper keeps its own. A chosen family that is not
//! installed falls back to the theme's and Settings says so; Ctrl+K "Reset
//! font to theme default" removes it. The second pass (`restart`) is the app
//! started again on the same settings: the font is still there, and the
//! picker's Ctrl+R, opened from Settings, restores the theme's default.

use std::time::Duration;

use slint::platform::Key;
use slint::{ComponentHandle, Model};

use super::{Stage, app_now, check, report, run_stages, shot};
use crate::headless;

fn family(window: &crate::AppWindow) -> String {
    window.global::<crate::Palette>().get_body_family().to_string()
}

fn size(window: &crate::AppWindow) -> f32 {
    window.global::<crate::Palette>().get_body_size()
}

fn bar(window: &crate::AppWindow) -> Vec<String> {
    window.get_hints().iter().map(|h| format!("{} {}", h.keys, h.label).trim().to_owned()).collect()
}

fn setting(window: &crate::AppWindow, label: &str) -> Option<crate::SettingRow> {
    window.get_setting_rows().iter().find(|r| r.label == label)
}

fn current_setting(window: &crate::AppWindow) -> String {
    window.get_setting_rows().row_data(window.get_setting_current() as usize).map(|r| r.label.to_string()).unwrap_or_default()
}

fn saved(theme: &str) -> Option<clarp_core::reading_theme::FontOverride> {
    app_now().engine.borrow().font_override(theme)
}

/// Ctrl+K, the command's words, Enter.
fn command(words: &str) {
    headless::press_with(&[Key::Control], "k");
    headless::type_text(words);
}

pub fn fonts_check(out: String) {
    if std::env::var("CLARP_CHECK_PASS").as_deref() == Ok("restart") {
        return fonts_restart_check(out);
    }
    let (out1, out2, out3, out4) = (out.clone(), out.clone(), out.clone(), out);
    let stages: Vec<Stage> = vec![
        ("ready", Box::new(|app, _, _| {
            let open = app.engine.borrow().conversation("rachel").is_some_and(|c| !c.rows().is_empty());
            if !open || !report().composer_focused {
                return false;
            }
            app.engine.borrow_mut().set_reading_theme("hacker");
            crate::pump();
            true
        })),
        // The installed families load off the UI thread.
        ("hacker", Box::new(|_, window, elapsed| {
            if family(window) != "JetBrains Mono" || elapsed < Duration::from_millis(1500) {
                return false;
            }
            command("choose font");
            true
        })),
        ("command", Box::new(|_, window, _| {
            if !window.get_switcher_open() || window.get_switcher_rows().row_data(0).is_none_or(|r| r.label != "Choose font…") {
                return false;
            }
            headless::press(Key::Return);
            true
        })),
        ("open", Box::new(move |_, window, elapsed| {
            if window.get_overlay() != "fonts" || elapsed < Duration::from_millis(300) {
                return false;
            }
            check(window.get_keyboard_mode() == "FONT", &format!("Ctrl+K Choose font… opens the picker with the keyboard in it: {}", window.get_keyboard_mode()));
            let hints = bar(window);
            for hint in ["↑↓ Move", "Enter Choose", "Ctrl+M Monospace only", "Ctrl+↑↓ Size", "Ctrl+R Theme default", "Esc Cancel"] {
                check(hints.iter().any(|h| h == hint), &format!("the bar shows {hint}: {hints:?}"));
            }
            check(report().at_end, "the chat is at its latest message");
            shot(&out1, "fonts-01-open");
            headless::press_with(&[Key::Control], "m");
            headless::type_text("dejavu");
            true
        })),
        ("preview", Box::new(move |_, window, elapsed| {
            if family(window) != "DejaVu Sans Mono" || elapsed < Duration::from_millis(600) {
                return false;
            }
            check(true, "monospace only and \"dejavu\" leave DejaVu Sans Mono, and the chat shows it");
            check(report().at_end, "the chat stays at its latest message as it changes");
            check(saved("hacker").is_none(), "nothing is saved while previewing");
            shot(&out2, "fonts-02-preview");
            headless::press(Key::Escape);
            true
        })),
        ("cancelled", Box::new(|_, window, _| {
            if window.get_overlay() != "" || family(window) != "JetBrains Mono" {
                return false;
            }
            check(saved("hacker").is_none(), "Escape cancels: the theme's font is back and nothing is saved");
            command("choose font");
            true
        })),
        ("again", Box::new(|_, window, _| {
            if !window.get_switcher_open() || window.get_switcher_rows().row_data(0).is_none_or(|r| r.label != "Choose font…") {
                return false;
            }
            headless::press(Key::Return);
            true
        })),
        ("reopened", Box::new(|_, window, elapsed| {
            if window.get_overlay() != "fonts" || elapsed < Duration::from_millis(300) {
                return false;
            }
            headless::type_text("dejavu sans");
            true
        })),
        ("first", Box::new(|_, window, _| {
            if family(window) != "DejaVu Sans" {
                return false;
            }
            check(true, "every family again (monospace only is off), the first match previewed");
            headless::press(Key::DownArrow);
            true
        })),
        ("down", Box::new(|_, window, _| {
            if family(window) != "DejaVu Sans Mono" {
                return false;
            }
            check(true, "Down previews the next family");
            headless::press_with(&[Key::Control], Key::UpArrow);
            true
        })),
        ("larger", Box::new(|_, window, _| {
            if size(window) != 16.0 {
                return false;
            }
            check(true, "Ctrl+Up previews a larger size");
            headless::press(Key::Return);
            true
        })),
        ("chosen", Box::new(move |app, window, elapsed| {
            if window.get_overlay() != "" || elapsed < Duration::from_millis(600) {
                return false;
            }
            let chosen = saved("hacker");
            check(
                chosen == Some(clarp_core::reading_theme::FontOverride { family: "DejaVu Sans Mono".into(), size: Some(16.0) }),
                &format!("Enter keeps DejaVu Sans Mono at 16 px for Hacker: {chosen:?}"),
            );
            check(family(window) == "DejaVu Sans Mono" && size(window) == 16.0, "and the chat shows it");
            let code = window.global::<crate::Palette>().get_code_family();
            check(code == "DejaVu Sans Mono", &format!("a monospace choice sets the code blocks too: {code}"));
            shot(&out3, "fonts-03-chosen");
            app.engine.borrow_mut().set_reading_theme("paper");
            crate::pump();
            true
        })),
        ("paper", Box::new(|app, window, _| {
            if size(window) != 17.0 {
                return false;
            }
            check(family(window) != "DejaVu Sans Mono" && saved("paper").is_none(), &format!("Paper keeps its own font: {}", family(window)));
            check(window.global::<crate::Palette>().get_code_family() == "JetBrains Mono", "and its code blocks theirs");
            // A family chosen once and since uninstalled.
            app.engine.borrow_mut().set_reading_theme("terminal");
            app.engine.borrow_mut().set_font_override("terminal", Some(clarp_core::reading_theme::FontOverride { family: "Gone Mono".into(), size: None }));
            crate::pump();
            true
        })),
        ("missing", Box::new(|_, window, _| {
            if size(window) != 15.0 || family(window) != "JetBrains Mono" {
                return false;
            }
            check(true, "a chosen family that is not installed falls back to the theme's");
            headless::press_with(&[Key::Control], ",");
            true
        })),
        ("settings say so", Box::new(move |_, window, elapsed| {
            if window.get_surface() != "settings" || elapsed < Duration::from_millis(300) {
                return false;
            }
            let detail = setting(window, "Font").map(|r| r.detail.to_string()).unwrap_or_default();
            check(detail.contains("Gone Mono") && detail.contains("not installed") && detail.contains("JetBrains Mono"), &format!("Settings says so: {detail}"));
            shot(&out4, "fonts-04-missing");
            headless::press(Key::Escape);
            true
        })),
        ("chats", Box::new(|_, window, _| {
            if window.get_surface() != "chats" {
                return false;
            }
            command("reset font");
            true
        })),
        ("reset command", Box::new(|_, window, _| {
            if !window.get_switcher_open() || window.get_switcher_rows().row_data(0).is_none_or(|r| r.label != "Reset font to theme default") {
                return false;
            }
            headless::press(Key::Return);
            true
        })),
        ("reset", Box::new(|app, _, _| {
            if saved("terminal").is_some() {
                return false;
            }
            check(saved("hacker").is_some(), "Ctrl+K Reset font to theme default removes this theme's font only");
            app.engine.borrow_mut().set_reading_theme("hacker");
            crate::pump();
            true
        })),
        ("hacker again", Box::new(|_, window, _| {
            if family(window) != "DejaVu Sans Mono" || size(window) != 16.0 {
                return false;
            }
            check(true, "back on Hacker, its font is there");
            true
        })),
    ];
    run_stages(stages);
}

fn fonts_restart_check(out: String) {
    let (out1, out2) = (out.clone(), out);
    let stages: Vec<Stage> = vec![
        ("ready", Box::new(move |app, window, elapsed| {
            let open = app.engine.borrow().conversation("rachel").is_some_and(|c| !c.rows().is_empty());
            if !open || !report().composer_focused || elapsed < Duration::from_millis(1500) {
                return false;
            }
            check(app.engine.borrow().reading_theme() == "hacker", "Hacker is still the theme");
            check(family(window) == "DejaVu Sans Mono" && size(window) == 16.0, &format!("its font survives a restart: {} {}", family(window), size(window)));
            shot(&out1, "fonts-05-restart");
            headless::press_with(&[Key::Control], ",");
            true
        })),
        ("settings", Box::new(|_, window, _| {
            if window.get_surface() != "settings" || !window.get_settings_focused() {
                return false;
            }
            let detail = setting(window, "Font").map(|r| r.detail.to_string()).unwrap_or_default();
            check(detail.contains("DejaVu Sans Mono") && detail.contains("16"), &format!("Settings shows the font: {detail}"));
            true
        })),
        // Down, one row a tick, to the font row.
        ("font row", Box::new(|_, window, _| {
            if current_setting(window) != "Font" {
                headless::press(Key::DownArrow);
                return false;
            }
            headless::press(Key::Return);
            true
        })),
        ("picker", Box::new(|_, window, elapsed| {
            if window.get_overlay() != "fonts" || elapsed < Duration::from_millis(300) {
                return false;
            }
            check(window.get_surface() == "chats", "the picker opens over the chat, to preview in it");
            headless::press_with(&[Key::Control], "r");
            true
        })),
        ("default", Box::new(move |_, window, elapsed| {
            if window.get_overlay() != "" || elapsed < Duration::from_millis(600) {
                return false;
            }
            check(saved("hacker").is_none(), "Ctrl+R removes Hacker's font");
            check(family(window) == "JetBrains Mono" && size(window) == 15.0, &format!("the theme's default is back: {} {}", family(window), size(window)));
            check(window.get_surface() == "settings", "back in Settings, where it was opened");
            let detail = setting(window, "Font").map(|r| r.detail.to_string()).unwrap_or_default();
            check(detail.contains("theme default"), &format!("Settings says so: {detail}"));
            shot(&out2, "fonts-06-reset");
            true
        })),
    ];
    run_stages(stages);
}
