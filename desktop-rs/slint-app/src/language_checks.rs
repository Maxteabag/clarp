//! `--check language --out DIR`: Settings → Language. The row and its picker
//! (System first, then each language by its own name), then Norsk bokmål,
//! simplified Chinese and Hindi chosen in turn: the Slint text (the page's
//! title, `@tr`) and the Rust text (the row, `tr!`) change at once, shot as
//! `DIR/language-*.png`. System at the end follows the device again.
//! Needs the fake Host.

use slint::Model;

use super::{Stage, app_now, check, report, run_stages, shot};
use crate::headless;

fn language_row(window: &crate::AppWindow) -> Option<crate::SettingRow> {
    window.get_setting_rows().iter().find(|r| r.id == "language")
}

/// A stage that chooses `tag` and, once the page shows it, checks the
/// row's words in that language and takes the shot.
fn choose(out: &str, tag: &'static str, label: &'static str) -> Stage {
    let out = out.to_owned();
    let mut chosen = false;
    (tag, Box::new(move |_, window, _| {
        if !chosen {
            crate::settings_view::pick(&app_now(), window, "language", tag);
            chosen = true;
            return false;
        }
        let Some(row) = language_row(window) else { return false };
        if row.label != label {
            return false;
        }
        check(clarp_core::i18n::current() == tag, &format!("{tag}: Rust text is in {tag}: {}", clarp_core::i18n::current()));
        check(row.detail == clarp_core::i18n::language(tag).endonym, &format!("{tag}: the row shows the language by its own name: {}", row.detail));
        check(app_now().engine.borrow().settings().string(clarp_core::i18n::SETTING_KEY, "") == tag, &format!("{tag} is kept for this device"));
        shot(&out, &format!("language-{tag}"));
        true
    }))
}

pub fn language_check(out: String) {
    use slint::platform::Key;
    let (out1, out2) = (out.clone(), out.clone());
    let mut stages: Vec<Stage> = vec![
        ("ready", Box::new(|app, _, _| {
            let open = app.engine.borrow().conversation("rachel").is_some_and(|c| !c.rows().is_empty());
            if !open || !report().composer_focused {
                return false;
            }
            headless::press_with(&[Key::Control], ",");
            true
        })),
        ("settings", Box::new(move |app, window, _| {
            if window.get_surface() != "settings" || app.engine.borrow().host_status_loading() {
                return false;
            }
            let Some(row) = language_row(window) else {
                check(false, "Settings has a Language row");
                return true;
            };
            check(row.label == "Language" && row.kind == "choice", &format!("the Language row is a choice: {} ({})", row.label, row.kind));
            check(row.detail.starts_with("System ("), &format!("it follows the system by default: {}", row.detail));
            check(row.description.contains("System follows"), &format!("and says what it does: {}", row.description));
            shot(&out1, "language-en-settings");
            crate::commands::open_picker(app, window, "language");
            true
        })),
        ("picker", Box::new(move |_, window, _| {
            if !window.get_switcher_open() || window.get_switcher_rows().row_count() < 7 {
                return false;
            }
            let labels: Vec<String> = window.get_switcher_rows().iter().map(|r| r.label.to_string()).collect();
            let wanted = ["System", "English", "Norsk bokmål", "简体中文", "हिन्दी", "Español", "Français"];
            check(labels.iter().map(String::as_str).take(7).eq(wanted), &format!("System first, then each language by its own name: {labels:?}"));
            shot(&out2, "language-picker-en");
            crate::commands::close_switcher(&app_now(), window, Some(false));
            true
        })),
        ("closed", Box::new(|_, window, _| !window.get_switcher_open())),
    ];
    stages.push(choose(&out, "nb", "Språk"));
    stages.push(choose(&out, "zh-Hans", "语言"));
    stages.push(choose(&out, "hi", "भाषा"));
    // The picker in Hindi: "System" is the language's own word for it.
    let out3 = out.clone();
    stages.push(("picker hi", Box::new(move |app, window, _| {
        if !window.get_switcher_open() {
            crate::commands::open_picker(app, window, "language");
            return false;
        }
        let first = window.get_switcher_rows().row_data(0).map(|r| r.label.to_string()).unwrap_or_default();
        if first != "सिस्टम" {
            return false;
        }
        check(true, "the picker's System is in Hindi");
        shot(&out3, "language-picker-hi");
        crate::commands::close_switcher(&app_now(), window, Some(false));
        true
    })));
    stages.push(("system", Box::new(|_, window, _| {
        if window.get_switcher_open() {
            return false;
        }
        crate::settings_view::pick(&app_now(), window, "language", clarp_core::i18n::SYSTEM);
        let device = clarp_core::i18n::resolve(clarp_core::i18n::SYSTEM);
        check(clarp_core::i18n::current() == device, &format!("System follows the device again: {device}"));
        true
    })));
    run_stages(stages);
}
