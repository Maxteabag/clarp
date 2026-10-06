//! `--check customize --out DIR`: every preference reached the ways people
//! reach it (Peter, 2026-10-06: "customise everything… font size is missing
//! in Ctrl+K"). Ctrl+K finds "font size" with its description and value,
//! + steps it in place and the chat is drawn again at the new size without
//! rows overlapping or the reader's place moving; Backspace in Settings
//! resets it; a colour typed in the picker below the readability rules
//! warns and still previews, Escape puts it back, Enter keeps one; editing
//! the settings file reloads it live and reports an invalid value or a
//! broken file without applying it; the file opens for editing beside its
//! schema; export, reset all and import round-trip; `:set` sets by name;
//! a whole custom layout still tiles; Confirm stop asks first. check.sh
//! gives this check a scratch settings file.

use std::time::Duration;

use serde_json::Value;
use slint::platform::Key;
use slint::{ComponentHandle, Model};

use super::{Stage, app_now, check, report, row_overlap_checks, run_stages, shot};
use crate::headless;

fn hex(color: slint::Color) -> String {
    format!("#{:02x}{:02x}{:02x}", color.red(), color.green(), color.blue())
}

fn body(window: &crate::AppWindow) -> f32 {
    window.global::<crate::Palette>().get_body_size()
}

fn run(window: &crate::AppWindow, action: &str) -> bool {
    crate::commands::run(&app_now(), window, action)
}

fn settings_path() -> std::path::PathBuf {
    app_now().engine.borrow().settings().path().map(std::path::Path::to_path_buf).unwrap_or_default()
}

fn stored(key: &str) -> Option<Value> {
    app_now().engine.borrow().settings().get(key).cloned()
}

fn notices() -> String {
    crate::look::notices().join(" | ")
}

fn setting_row(window: &crate::AppWindow, id: &str) -> Option<(usize, crate::SettingRow)> {
    window.get_setting_rows().iter().enumerate().find(|(_, r)| r.id == id)
}

/// Rewrites the settings file as an editor would.
fn edit_file(change: impl FnOnce(&mut serde_json::Map<String, Value>)) {
    let path = settings_path();
    let mut object = std::fs::read_to_string(&path).ok().and_then(|t| serde_json::from_str::<Value>(&t).ok()).and_then(|v| v.as_object().cloned()).unwrap_or_default();
    change(&mut object);
    let written = serde_json::to_string_pretty(&Value::Object(object)).map_err(|e| e.to_string()).and_then(|text| std::fs::write(&path, text).map_err(|e| e.to_string()));
    check(written.is_ok(), &format!("the settings file is edited by hand: {written:?}"));
}

/// The logs the app records to instead of opening things.
fn recorded(variable: &str) -> String {
    std::env::var_os(variable).and_then(|path| std::fs::read_to_string(path).ok()).unwrap_or_default()
}

pub fn customize_check(out: String) {
    let o = move || out.clone();
    let (o1, o2, o3, o4, o5, o6, o7, o8, o9) = (o(), o(), o(), o(), o(), o(), o(), o(), o());
    let valid = std::rc::Rc::new(std::cell::RefCell::new(String::new()));
    let (valid1, valid2) = (valid.clone(), valid.clone());
    let stages: Vec<Stage> = vec![
        ("ready", Box::new(|app, _, _| {
            let open = app.engine.borrow().conversation("rachel").is_some_and(|c| !c.rows().is_empty());
            if !open || !report().composer_focused || !report().at_end {
                return false;
            }
            check(settings_path().as_os_str().len() > 0, "the check has a settings file");
            headless::press_with(&[Key::Control], "k");
            true
        })),
        ("switcher", Box::new(|_, window, elapsed| {
            if !window.get_switcher_open() || elapsed < Duration::from_millis(200) {
                return false;
            }
            headless::type_text("font size");
            true
        })),
        ("found", Box::new(move |_, window, elapsed| {
            if window.get_switcher_query() != "font size" || elapsed < Duration::from_millis(300) {
                return false;
            }
            let rows: Vec<crate::SwitcherRow> = window.get_switcher_rows().iter().collect();
            let at = rows.iter().position(|r| r.label == "Font size");
            check(at.is_some_and(|i| i < 3), &format!("Ctrl+K finds Font size in the top three: {:?}", rows.iter().take(4).map(|r| r.label.to_string()).collect::<Vec<_>>()));
            let row = at.map(|i| rows[i].clone()).unwrap_or_default();
            check(row.value.contains("15 px"), &format!("with its value: {}", row.value));
            check(!row.detail.is_empty(), &format!("and its description: {}", row.detail));
            check(row.adjustable, "and it steps with + and -");
            if let Some(i) = at {
                window.invoke_switcher_moved(i as i32);
            }
            shot(&o1, "customize-01-ctrlk-font-size");
            headless::press("+");
            headless::press("+");
            true
        })),
        ("bigger", Box::new(move |_, window, elapsed| {
            if body(window) != 17.0 || elapsed < Duration::from_millis(300) {
                return false;
            }
            check(window.get_switcher_open(), "+ steps the font size in place: the switcher stays open");
            let value = window.get_switcher_rows().iter().find(|r| r.label == "Font size").map(|r| r.value.to_string()).unwrap_or_default();
            check(value == "17 px", &format!("and shows the new value: {value}"));
            check(stored("appearance/fontOverrides").is_some_and(|v| v["terminal"]["size"] == 17.0), "and it is saved for the theme");
            shot(&o2, "customize-02-ctrlk-font-17");
            headless::press(Key::Escape);
            true
        })),
        ("chat at 17", Box::new(move |_, window, elapsed| {
            if window.get_switcher_open() || elapsed < Duration::from_millis(1000) {
                return false;
            }
            row_overlap_checks::assert_tiled("the chat drawn again at 17px");
            check(report().at_end, "and the reader is still at the end (no scroll jump)");
            shot(&o3, "customize-03-chat-17px");
            headless::press_with(&[Key::Control], ",");
            true
        })),
        ("settings", Box::new(move |_, window, elapsed| {
            if window.get_surface() != "settings" || !window.get_settings_focused() || elapsed < Duration::from_millis(300) {
                return false;
            }
            let Some((index, row)) = setting_row(window, "font-size") else {
                check(false, "Settings has a Font size row");
                return true;
            };
            check(row.detail == "17 px" && row.changed, &format!("Settings shows it changed: {} changed {}", row.detail, row.changed));
            check(!row.description.is_empty(), &format!("with its description: {}", row.description));
            check(row.key.contains("fontsize") && row.key.contains("appearance/fontOverrides"), &format!("and its name and key: {}", row.key));
            window.set_setting_current(index as i32);
            shot(&o4, "customize-04-settings-font-size");
            headless::press(Key::Backspace);
            true
        })),
        ("reset", Box::new(|_, window, _| {
            if body(window) != 15.0 {
                return false;
            }
            let row = setting_row(window, "font-size").map(|(_, r)| r).unwrap_or_default();
            check(row.detail.contains("15 px") && !row.changed, &format!("Backspace puts the theme's size back: {} changed {}", row.detail, row.changed));
            check(stored("appearance/fontOverrides").is_none(), "and leaves nothing in the file");
            // Enter on the colour's row (its catalogue action).
            crate::settings_view::change(&app_now(), window, "color.text", 0);
            true
        })),
        ("picker", Box::new(|_, window, elapsed| {
            if window.get_overlay() != crate::look::EDITOR || elapsed < Duration::from_millis(300) {
                return false;
            }
            let editor = window.global::<crate::PrefEditor>();
            check(editor.get_colour() && editor.get_swatches().row_count() >= 8, "the picker offers the theme's palette");
            check(editor.get_text() == "#e7e1dc", &format!("starting from the theme's colour: {}", editor.get_text()));
            // The field selected its text when it opened: typing replaces it.
            headless::type_text("#1a1b26");
            true
        })),
        ("warned", Box::new(move |_, window, elapsed| {
            let editor = window.global::<crate::PrefEditor>();
            if editor.get_text() != "#1a1b26" || elapsed < Duration::from_millis(300) {
                return false;
            }
            let warnings: Vec<String> = editor.get_warnings().iter().map(|w| w.to_string()).collect();
            check(warnings.iter().any(|w| w.contains("text on window")), &format!("text the colour of the window warns: {warnings:?}"));
            check(hex(window.global::<crate::Palette>().get_text()) == "#1a1b26", "and previews anyway (a warning, not a block)");
            shot(&o5, "customize-05-colour-warning");
            headless::press(Key::Escape);
            true
        })),
        ("reverted", Box::new(|_, window, _| {
            if !window.get_overlay().is_empty() {
                return false;
            }
            check(hex(window.global::<crate::Palette>().get_text()) == "#e7e1dc", "Escape puts the colour back");
            check(stored("appearance/colorOverrides").is_none(), "and nothing is kept");
            crate::settings_view::change(&app_now(), window, "color.accent", 0);
            true
        })),
        ("accent", Box::new(|_, window, elapsed| {
            if window.get_overlay() != crate::look::EDITOR || elapsed < Duration::from_millis(300) {
                return false;
            }
            headless::type_text("#ff8800");
            headless::press(Key::Return);
            true
        })),
        ("kept", Box::new(move |_, window, _| {
            if !window.get_overlay().is_empty() || hex(window.global::<crate::Palette>().get_accent()) != "#ff8800" {
                return false;
            }
            check(stored("appearance/colorOverrides").is_some_and(|v| v["terminal"]["accent"] == "#ff8800"), "Enter keeps a colour, for this theme");
            let row = setting_row(window, "color.accent").map(|(_, r)| r).unwrap_or_default();
            check(row.changed && row.detail == "#ff8800", "Settings shows the colour changed");
            *valid1.borrow_mut() = std::fs::read_to_string(settings_path()).unwrap_or_default();
            edit_file(|file| {
                file.insert("layout/radius".into(), 12.into());
                file.insert("typography/measure".into(), 5.into());
            });
            true
        })),
        ("reloaded", Box::new(move |_, window, _| {
            if window.global::<crate::Look>().get_radius() != 12.0 {
                return false;
            }
            check(true, "an edit to the settings file applies at once");
            check(notices().contains("measure"), &format!("an invalid value in it is reported: {}", notices()));
            check(window.global::<crate::Look>().get_measure() == 0.0, "and read as its default");
            check(setting_row(window, "radius").is_some_and(|(_, r)| r.detail == "12 px"), "Settings shows the file's value");
            shot(&o6, "customize-06-file-reload");
            let written = std::fs::write(settings_path(), "{ \"layout/radius\": ");
            check(written.is_ok(), "the file is half-written");
            true
        })),
        ("broken", Box::new(move |_, window, _| {
            if !notices().contains("not valid JSON") {
                return false;
            }
            check(window.global::<crate::Look>().get_radius() == 12.0, "a broken file changes nothing");
            let restored = std::fs::read_to_string(settings_path()).map(|_| ()).and_then(|()| {
                let mut file: Value = serde_json::from_str(&valid2.borrow()).unwrap_or_else(|_| Value::Object(Default::default()));
                file["layout/radius"] = 12.into();
                std::fs::write(settings_path(), serde_json::to_string_pretty(&file).unwrap_or_default())
            });
            check(restored.is_ok(), "the file is mended");
            true
        })),
        ("mended", Box::new(|_, window, _| {
            if notices().contains("JSON") {
                return false;
            }
            check(true, "mending the file clears the report");
            check(run(window, "pref-edit-file"), "Edit the settings file runs");
            true
        })),
        ("editing", Box::new(|_, _, elapsed| {
            if elapsed < Duration::from_millis(300) {
                return false;
            }
            let path = settings_path();
            let schema = path.parent().map(|p| p.join("settings.schema.json")).filter(|p| p.exists());
            check(schema.is_some(), "the schema is written beside the file");
            check(path.parent().is_some_and(|p| p.join("settings-reference.md").exists()), "and the reference");
            check(stored("$schema").is_some(), "and the file points at the schema");
            let opened = recorded("CLARP_TEST_OPEN_URL").contains("settings.json") || recorded("CLARP_TEST_TERMINAL_LOG").contains("settings.json");
            check(opened, "the file opens in the editor (recorded)");
            check(run(&crate::window().expect("window"), "pref-export"), "Export runs");
            true
        })),
        ("exported", Box::new(|_, window, _| {
            let home = std::env::var_os("HOME").map(std::path::PathBuf::from).unwrap_or_default();
            let file = std::fs::read_to_string(home.join("clarp-settings.json")).or_else(|_| std::fs::read_to_string(home.join("Downloads").join("clarp-settings.json")));
            let Ok(file) = file else { return false };
            check(file.contains("layout/radius") && file.contains("#ff8800") && !file.contains("keymap/bindings"), "the export holds the preferences only");
            run(window, "pref-reset-all");
            true
        })),
        ("reset all", Box::new(|_, window, _| {
            if window.global::<crate::Look>().get_radius() != 6.0 {
                return false;
            }
            check(hex(window.global::<crate::Palette>().get_accent()) == "#bb9af7", "Reset every setting puts the theme's colours back");
            run(window, "pref-import");
            true
        })),
        ("import", Box::new(|_, window, elapsed| {
            if window.get_overlay() != crate::look::EDITOR || elapsed < Duration::from_millis(300) {
                return false;
            }
            check(window.global::<crate::PrefEditor>().get_text().ends_with("clarp-settings.json"), "Import offers the exported file");
            headless::press(Key::Return);
            true
        })),
        ("imported", Box::new(|_, window, _| {
            if !window.get_overlay().is_empty() || window.global::<crate::Look>().get_radius() != 12.0 {
                return false;
            }
            check(hex(window.global::<crate::Palette>().get_accent()) == "#ff8800", "Import brings them back");
            check(run(window, "pref-set:fontsize=18"), ":set fontsize=18 sets it by name");
            check(!run(window, "pref-set:fontsize=99"), "and refuses a size out of range");
            check(notices().contains("between"), &format!("saying why: {}", notices()));
            true
        })),
        ("set by name", Box::new(|_, window, _| {
            if body(window) != 18.0 {
                return false;
            }
            // Numbers step in the catalogue's own control, from where they are.
            let number = |name: &str| clarp_core::prefs::number_of(app_now().engine.borrow().settings(), name);
            let app = app_now();
            crate::settings_view::change(&app, window, "radius", -1);
            check(number("radius") == 11.0, &format!("Left on Corner radius steps 12 → 11: {}", number("radius")));
            crate::settings_view::change(&app, window, "measure", 1);
            check(number("measure") == 40.0, &format!("Right on Line length goes from full width to 40 ch: {}", number("measure")));
            crate::settings_view::change(&app, window, "measure", -1);
            check(number("measure") == 0.0, &format!("and Left back to full width: {}", number("measure")));
            crate::settings_view::reset(&app, window, "radius", false);
            check(number("radius") == 6.0, &format!("Delete puts Corner radius's default back: {}", number("radius")));
            for line in [
                "density=compact", "radius=0", "explorerwidth=240", "messagespacing=24", "bubblewidth=60", "measure=60", "paragraphspacing=16",
                "chromesize=120", "headingscale=180", "codesize=110", "timeformat=24h", "timestamps", "avatarsize=large", "borderwidth=3", "panegap=10",
            ] {
                check(run(window, &format!("pref-set:{line}")), &format!(":set {line}"));
            }
            run(window, "chats");
            true
        })),
        ("custom layout", Box::new(move |_, window, elapsed| {
            if window.get_surface() != "chats" || elapsed < Duration::from_millis(1500) {
                return false;
            }
            let look = window.global::<crate::Look>();
            check(look.get_density_pad() < 0.0 && look.get_explorer_width() == 240.0 && look.get_message_spacing() == 24.0, "the layout settings reach the window");
            check(look.get_unit() == 1.2 && look.get_heading_scale() == 1.8 && look.get_measure() > 0.0, "and the typography ones");
            row_overlap_checks::assert_tiled("a whole custom layout");
            check(crate::app().and_then(|a| a.active_messages()).is_some_and(|m| m.iter().any(|r| r.stamp.contains(':') && !r.stamp.contains('M'))), "timestamps on, on a 24-hour clock");
            shot(&o7, "customize-07-custom-layout");
            run(window, "pref-set:confirmstop=on");
            run(window, "stop-agent");
            true
        })),
        ("confirm", Box::new(move |_, window, elapsed| {
            if window.get_overlay() != crate::look::CONFIRM || elapsed < Duration::from_millis(300) {
                return false;
            }
            check(window.global::<crate::PrefEditor>().get_confirm_title().starts_with("Stop "), "Confirm stop asks first");
            shot(&o8, "customize-08-confirm-stop");
            headless::press(Key::Escape);
            true
        })),
        ("declined", Box::new(|_, window, _| {
            if !window.get_overlay().is_empty() {
                return false;
            }
            check(true, "Escape says no");
            run(window, "pref-reset-all");
            true
        })),
        ("defaults", Box::new(move |_, window, elapsed| {
            if body(window) != 15.0 || window.global::<crate::Look>().get_radius() != 6.0 || elapsed < Duration::from_millis(1500) {
                return false;
            }
            row_overlap_checks::assert_tiled("every setting back at its default");
            shot(&o9, "customize-09-defaults");
            true
        })),
    ];
    run_stages(stages);
}
