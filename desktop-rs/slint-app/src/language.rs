//! The app's language in the window: chosen at start from Settings →
//! Language (or this computer's languages), and switched at once when the
//! setting changes. Slint text (`@tr`) follows `select_bundled_translation`;
//! Rust text (`tr!`) follows `clarp_core::i18n::current`, and the views that
//! show it are drawn again. docs/i18n.md has the pattern.

use std::rc::Rc;

use clarp_core::i18n::{self, SETTING_KEY, SYSTEM};
use clarp_core::settings::Settings;
use clarp_engine::Change;

use crate::{App, AppWindow};

/// The stored choice: "system" or a shipped language's tag.
pub fn stored(settings: &Settings) -> String {
    let value = settings.string(SETTING_KEY, SYSTEM);
    if i18n::LANGUAGES.iter().any(|l| l.tag == value) { value } else { SYSTEM.to_owned() }
}

/// Makes `tag` the language of Rust text and of the Slint window.
fn select(tag: &str) {
    i18n::set_current(tag);
    // English is the source text: Slint selects it with "".
    let slint_tag = if tag == "en" { "" } else { tag };
    if let Err(error) = slint::select_bundled_translation(slint_tag) {
        eprintln!("clarp-slint: cannot select the {tag} translation: {error:?}");
    }
}

/// At start, once the window exists: `CLARP_LANGUAGE` (a tag or "system",
/// for screenshots in a given language), else the stored language, else the
/// system's.
pub fn start(settings: &Settings) {
    let chosen = std::env::var("CLARP_LANGUAGE").ok().filter(|v| !v.is_empty()).unwrap_or_else(|| stored(settings));
    select(i18n::resolve(&chosen));
}

/// Settings → Language chose `value`: kept for this device, applied now, and
/// everything Rust wrote in the old language drawn again.
pub fn choose(app: &Rc<App>, window: &AppWindow, value: &str) {
    app.engine.borrow_mut().settings_mut().set(SETTING_KEY, value);
    select(i18n::resolve(value));
    app.rebuild_transcripts();
    app.refresh(&[Change::Roster, Change::Panes, Change::Preferences, Change::Launch]);
    crate::commands::refresh_switcher(app, window);
    crate::commands::show_hints(app, window);
}
