//! Settings → Language: System (this computer's languages) or one of the
//! shipped languages, by its own name. Kept per device; applies at once.
// i18n-screen: settings

use clarp_core::i18n::{self, LANGUAGES, SYSTEM};
use clarp_core::tr;

use super::{Choice, Setting, e};

/// The resolved system language's own name ("Norsk bokmål").
fn system_name() -> &'static str {
    i18n::language(i18n::resolve(SYSTEM)).endonym
}

pub(super) fn register() -> Vec<Setting> {
    vec![
        Setting::choice(
            e(
                "language",
                "Language",
                "appearance",
                "The language of the app. System follows this computer's languages.",
                &["language", "locale", "translation", "translate", "språk", "语言", "भाषा", "idioma", "langue", "english", "norsk"],
            ),
            "APPEARANCE",
            |_| {
                // i18n: Settings > Language picker, first option (noun): use the language the computer is set to
                let system = tr!("System");
                // i18n: Settings > Language picker, line under the System option: the app follows the computer's language; {} is that language's own name, e.g. "Norsk bokmål"
                let mut options = vec![Choice::new(SYSTEM, system).about(tr!("Follows this computer: {}", system_name()))];
                options.extend(LANGUAGES.iter().map(|l| Choice::new(l.tag, l.endonym)));
                options
            },
            |app| crate::language::stored(app.engine.borrow().settings()),
            |app, window, value| crate::language::choose(app, window, value),
        )
        // i18n: Settings > Appearance, row label (noun): the setting that picks the app's language
        .label_from(|_| tr!("Language"))
        // i18n: Settings > Appearance, description line under the Language row; "System" is the picker's first option
        .describe_from(|_| tr!("The language of the app. System follows this computer's languages."))
        .detail(|app| {
            let stored = crate::language::stored(app.engine.borrow().settings());
            // i18n: Settings > Appearance, the Language row's current value when it follows the computer; {} is that language's own name
            if stored == SYSTEM { tr!("System ({})", system_name()) } else { i18n::language(&stored).endonym.to_owned() }
        })
        .default(SYSTEM),
    ]
}
