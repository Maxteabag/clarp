//! The app's language: the languages it ships, which one is in use (the
//! device's languages, or the user's choice in Settings), the translations of
//! text written in Rust, and locale-aware formatting (`i18n::format`).
//!
//! One catalog serves both halves of the app. Slint text is `@tr("...")` and
//! Rust text is `tr!("...")`, with the same syntax; the translations live in
//! `desktop-rs/i18n/<lang>/LC_MESSAGES/clarp-slint.po`. Slint bundles those
//! files into its generated code, and this crate's build script turns the same
//! files into the table `tr!` reads. docs/i18n.md has the pattern.

use std::fmt::Display;
use std::sync::atomic::{AtomicUsize, Ordering};

pub mod format;

/// A shipped language: its tag (the catalog folder, a BCP 47 tag) and its own
/// name, which the language picker shows untranslated.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Language {
    pub tag: &'static str,
    pub endonym: &'static str,
}

/// English first (the source language), then the picker's order.
pub const LANGUAGES: [Language; 6] = [
    Language { tag: "en", endonym: "English" },
    Language { tag: "nb", endonym: "Norsk bokmål" },
    Language { tag: "zh-Hans", endonym: "简体中文" },
    Language { tag: "hi", endonym: "हिन्दी" },
    Language { tag: "es", endonym: "Español" },
    Language { tag: "fr", endonym: "Français" },
];

/// The settings value that follows the device.
pub const SYSTEM: &str = "system";
/// The settings key, per device (the app's own settings file).
pub const SETTING_KEY: &str = "appearance/language";

/// The shipped language a device language maps to, if any: nb, nn and no
/// read as Norsk bokmål; zh, zh-Hans, zh-CN and zh-SG as simplified
/// Chinese (traditional Chinese is not shipped). Takes BCP 47 or POSIX
/// forms ("nb-NO", "nb_NO.UTF-8", "zh_CN").
pub fn supported(tag: &str) -> Option<&'static str> {
    let tag = tag.split(['.', '@']).next().unwrap_or_default().replace('_', "-").to_ascii_lowercase();
    let mut parts = tag.split('-');
    let language = parts.next().unwrap_or_default();
    let rest: Vec<&str> = parts.collect();
    match language {
        "en" => Some("en"),
        "nb" | "nn" | "no" => Some("nb"),
        "hi" => Some("hi"),
        "es" => Some("es"),
        "fr" => Some("fr"),
        "zh" => {
            let traditional = rest.iter().any(|p| ["hant", "tw", "hk", "mo"].contains(p));
            let simplified = rest.iter().any(|p| ["hans", "cn", "sg"].contains(p));
            (simplified || !traditional).then_some("zh-Hans")
        }
        _ => None,
    }
}

/// The first of the device's preferred languages the app ships, else English.
pub fn from_preferred<S: AsRef<str>>(preferred: impl IntoIterator<Item = S>) -> &'static str {
    preferred.into_iter().find_map(|tag| supported(tag.as_ref())).unwrap_or("en")
}

/// The device's preferred languages, most preferred first: LANGUAGE, LC_ALL,
/// LC_MESSAGES and LANG on Linux; the system's language list on macOS.
pub fn device_languages() -> Vec<String> {
    sys_locale::get_locales().collect()
}

/// The language a setting resolves to: "system" (or anything not shipped)
/// follows the device's languages.
pub fn resolve(setting: &str) -> &'static str {
    match LANGUAGES.iter().find(|l| l.tag == setting) {
        Some(language) => language.tag,
        None => from_preferred(device_languages()),
    }
}

/// The language a tag names, or English.
pub fn language(tag: &str) -> Language {
    LANGUAGES.iter().copied().find(|l| l.tag == tag).unwrap_or(LANGUAGES[0])
}

static CURRENT: AtomicUsize = AtomicUsize::new(0);

/// Makes `tag` (a shipped language, else English) the language Rust text
/// and the formatters use. The app also selects it for Slint.
pub fn set_current(tag: &str) {
    CURRENT.store(LANGUAGES.iter().position(|l| l.tag == tag).unwrap_or(0), Ordering::Relaxed);
}

/// The language in use, as its tag ("en", "nb", "zh-Hans", ...): the
/// resolved locale the formatters take.
pub fn current() -> &'static str {
    LANGUAGES[CURRENT.load(Ordering::Relaxed)].tag
}

// ---- translations

mod catalog {
    // CATALOG: per language (LANGUAGES' order, English empty), entries sorted
    // by (context, text): (context, text, translations); one translation, or
    // one per plural form.
    include!(concat!(env!("OUT_DIR"), "/i18n_catalog.rs"));
}

/// The gettext plural rule each catalog's `Plural-Forms` header states
/// (tools/i18n_catalog.py writes them; a test keeps the two in step).
pub const PLURAL_FORMS: [(&str, &str); 5] = [
    ("nb", "nplurals=2; plural=(n != 1);"),
    ("zh-Hans", "nplurals=1; plural=0;"),
    ("hi", "nplurals=2; plural=(n > 1);"),
    ("es", "nplurals=2; plural=(n != 1);"),
    ("fr", "nplurals=2; plural=(n > 1);"),
];

/// Which plural form a count takes in a language: the index of its
/// translation, as in that language's `Plural-Forms` header.
pub fn plural_index(tag: &str, n: i64) -> usize {
    match tag {
        "zh-Hans" => 0,
        "fr" | "hi" => usize::from(n > 1),
        _ => usize::from(n != 1),
    }
}

/// The headers the catalogs were built with, by language (for the test).
pub fn catalog_plural_forms() -> &'static [(&'static str, &'static str)] {
    catalog::PLURAL_HEADERS
}

fn lookup(tag: &str, context: &str, text: &str) -> Option<&'static [&'static str]> {
    let index = LANGUAGES.iter().position(|l| l.tag == tag)?;
    let entries = catalog::CATALOG.get(index)?;
    let at = entries.binary_search_by(|(c, t, _)| (*c, *t).cmp(&(context, text))).ok()?;
    Some(entries[at].2)
}

/// Whether `tag`'s catalog translates the entry whose context is `context`
/// (a semantic key used as the context, as the turn-error strings are).
/// English, the source, always counts. A caller with words of its own for an
/// untranslated entry (the Host's sentence in the user's language) shows
/// those instead of the English source.
pub fn translated(tag: &str, context: &str) -> bool {
    let Some(index) = LANGUAGES.iter().position(|l| l.tag == tag) else { return false };
    if index == 0 {
        return true;
    }
    catalog::CATALOG.get(index).is_some_and(|entries| {
        let at = entries.partition_point(|(c, _, _)| *c < context);
        entries.get(at).is_some_and(|(c, _, forms)| *c == context && forms.iter().any(|f| !f.is_empty()))
    })
}

/// `text` in `tag`'s language (plural: `text` for one, `plural` otherwise,
/// chosen by `n`), its placeholders filled as Slint's `@tr` fills them: `{}`
/// takes the next argument, `{0}` a numbered one, `{n}` the count, and `{{`
/// and `}}` are braces. English, or a text with no translation, stays as
/// written. `tr!` calls this with the current language.
pub fn translate_in(tag: &str, context: &str, text: &str, plural: &str, n: Option<i64>, args: &[&dyn Display]) -> String {
    let n_value = n.unwrap_or(1);
    let chosen = match lookup(tag, context, text) {
        Some(forms) if !plural.is_empty() => forms.get(plural_index(tag, n_value)).or(forms.first()).copied(),
        Some(forms) => forms.first().copied(),
        None => None,
    };
    let source = if plural.is_empty() || n_value == 1 { text } else { plural };
    fill(chosen.unwrap_or(source), args, n)
}

/// `translate_in` with the current language.
pub fn translate(context: &str, text: &str, plural: &str, n: Option<i64>, args: &[&dyn Display]) -> String {
    translate_in(current(), context, text, plural, n, args)
}

/// Fills `{}`, `{0}` and `{n}` as Slint's formatter does; a placeholder
/// with no argument is left as written.
fn fill(template: &str, args: &[&dyn Display], n: Option<i64>) -> String {
    use std::fmt::Write;
    let mut out = String::with_capacity(template.len() + 16);
    let mut next = 0;
    let mut rest = template;
    while let Some(at) = rest.find(['{', '}']) {
        out.push_str(&rest[..at]);
        let tail = &rest[at..];
        if tail.starts_with("{{") || tail.starts_with("}}") {
            out.push_str(&tail[..1]);
            rest = &tail[2..];
            continue;
        }
        if tail.starts_with('}') {
            out.push('}');
            rest = &tail[1..];
            continue;
        }
        let Some(end) = tail.find('}') else {
            out.push_str(tail);
            rest = "";
            break;
        };
        let name = tail[1..end].trim();
        let value: Option<&dyn Display> = if name.is_empty() {
            next += 1;
            args.get(next - 1).copied()
        } else if let Ok(index) = name.parse::<usize>() {
            args.get(index).copied()
        } else {
            None
        };
        match (value, name, n) {
            (Some(value), _, _) => {
                let _ = write!(out, "{value}");
            }
            (None, "n", Some(count)) => {
                let _ = write!(out, "{count}");
            }
            _ => out.push_str(&tail[..=end]),
        }
        rest = &tail[end + 1..];
    }
    out.push_str(rest);
    out
}

/// Text in the app's language, written like Slint's `@tr`:
///
/// ```ignore
/// tr!("Settings")
/// tr!("Connected to {}", host)
/// tr!("verb" => "Open")                       // a context, when the English is ambiguous
/// tr!("{n} file" | "{n} files" % count)       // a plural; {n} is the count
/// ```
///
/// A comment for translators goes on the line above: `// i18n: ...`.
#[macro_export]
macro_rules! tr {
    ($context:literal => $one:literal | $other:literal % $n:expr $(, $arg:expr)* $(,)?) => {
        $crate::i18n::translate($context, $one, $other, Some(($n) as i64), &[$(&$arg as &dyn ::std::fmt::Display),*])
    };
    ($one:literal | $other:literal % $n:expr $(, $arg:expr)* $(,)?) => {
        $crate::i18n::translate("", $one, $other, Some(($n) as i64), &[$(&$arg as &dyn ::std::fmt::Display),*])
    };
    ($context:literal => $text:literal $(, $arg:expr)* $(,)?) => {
        $crate::i18n::translate($context, $text, "", None, &[$(&$arg as &dyn ::std::fmt::Display),*])
    };
    ($text:literal $(, $arg:expr)* $(,)?) => {
        $crate::i18n::translate("", $text, "", None, &[$(&$arg as &dyn ::std::fmt::Display),*])
    };
}
