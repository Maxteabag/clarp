//! The words of a failed turn's card (docs/turn-errors.md "Catalog keys",
//! docs/i18n conventions): the Host sends a code (`error.key`) and its
//! parameters, and the desktop says the sentence from its own catalog in the
//! app's language. The Host's own `text` is only the fallback, for a key the
//! catalog does not have or a placeholder left empty. `{time}` is formatted
//! by ICU in the app's locale (the time alone today, with the weekday within
//! a week, else with the date); the repeat count goes through the plural
//! rules. Nothing here builds date words or plural endings by hand.
//!
//! The catalog holds the English source under the shared key names; the
//! translations come from clarp-i18n and are looked up by the same keys.

use chrono::{Datelike, Local, TimeZone, Timelike};
use serde_json::Value;

use crate::json::Object;
use crate::turn_error::{Action, TurnError};

/// The languages Clarp ships, as BCP-47 tags.
pub const LANGUAGES: &[&str] = &["en", "nb", "zh-Hans", "hi", "es", "fr"];

/// English source strings under the shared keys (`error.repeat_count` is
/// plural: [`REPEAT_COUNT`]).
pub const ENGLISH: &[(&str, &str)] = &[
    ("error.usage_limit", "Your {backend} account is out of usage. Try again later, or switch model."),
    ("error.usage_limit_until", "Your {backend} account is out of usage until {time}. Try again then, or switch model."),
    ("error.billing", "Your {backend} account is out of credits. Top it up, or switch model."),
    ("error.auth", "Your {backend} sign-in has expired. Sign in again, then try again."),
    ("error.account_plan", "Your {backend} plan does not include {model}. Switch model or account."),
    ("error.free_model_restricted", "This free OpenCode model only runs in OpenCode's own apps. Choose another model."),
    ("error.connection", "Lost the connection to {backend}, and the Host's retries did not get through. Try again."),
    ("error.network_gone", "This computer had no network for 15 minutes, so the turn stopped. Try again once it is online."),
    ("error.provider_busy", "{backend} is overloaded right now, and the Host's retries did not get through. Try again in a minute, or switch model."),
    ("error.provider_down", "The provider of {model} is unavailable right now. Try again later, or switch model."),
    ("error.invalid_request", "{backend} rejected the conversation history. Start a fresh session with the same conversation and try again."),
    ("error.refused", "The model refused to answer (a safety filter blocked it). Rephrase, or switch model."),
    ("error.prompt_too_long", "This conversation has grown too long for the model. Compact it and continue."),
    ("error.timeout", "{backend} stopped responding. Try again."),
    ("error.runner_exit", "{backend} stopped unexpectedly. Try again."),
    ("error.ended_from_outside", "Something outside Clarp ended the turn. Send the message again."),
    ("error.interrupted", "The turn was cut off before it finished. Send the message again."),
    ("error.not_started", "The Host could not start the turn. Try again."),
    ("error.folder_missing", "This agent's folder {folder} no longer exists. Recreate it, or make a new agent in another folder."),
    ("error.folder_missing_no_path", "This agent's folder no longer exists. Recreate it, or make a new agent in another folder."),
    ("error.provider_mismatch", "This agent ran on another model provider than the one it is bound to. Check its Codex home and provider."),
    ("error.session_locked", "The {backend} session is busy in another process. Try again in a moment."),
    ("error.moved", "This agent moved to another Host. Continue the conversation there."),
    ("error.unknown", "The turn failed at {backend}. Try again, or switch model if it happens again."),
    ("error.action.retry", "Try again"),
    ("error.action.retry_at", "Try again at {time}"),
    ("error.action.fresh_retry", "New session, try again"),
    ("error.action.compact_retry", "Compact and continue"),
    ("error.action.switch_model", "Switch backend or model"),
    ("error.action.sign_in", "Sign in again"),
    ("error.action.copy_details", "Copy diagnostics"),
    ("error.details", "Details"),
    ("error.copied", "Diagnostics copied"),
    ("error.retry_scheduled", "Will try again at {time}"),
    ("error.retry_sent", "Sent again"),
    ("error.action_failed", "That didn't work. Try again."),
];

/// `error.repeat_count`, by plural category: `{count}×`.
pub const REPEAT_COUNT: &str = "error.repeat_count";
const REPEAT_COUNT_ENGLISH: &[(&str, &str)] = &[("one", "{count}×"), ("other", "{count}×")];

/// The catalog's own string for `key` in `language`: the English source in
/// English, a translation in a language that has one (none yet: clarp-i18n
/// adds them here), else `None`, so a sentence falls back to the Host's text
/// in that language rather than to English (docs/turn-errors.md, "key and
/// params").
pub fn tr(language: &str, key: &str) -> Option<&'static str> {
    if language == "en" { english(key) } else { None }
}

/// The English source for `key`.
pub fn english(key: &str) -> Option<&'static str> {
    ENGLISH.iter().find(|(k, _)| *k == key).map(|(_, text)| *text)
}

/// The app's language: the device's preferred language mapped onto the
/// shipped ones (nb, nn, no → nb; zh-Hans, zh-CN, zh-SG, zh → zh-Hans;
/// anything unsupported → en). `LANGUAGE`'s first entry, then `LC_ALL`,
/// `LC_MESSAGES`, `LANG`, as gettext orders them.
pub fn app_language() -> &'static str {
    let var = |name: &str| std::env::var(name).ok().map(|v| v.trim().to_owned()).filter(|v| !v.is_empty() && v != "C" && v != "POSIX");
    var("LANGUAGE")
        .map(|list| list.split(':').next().unwrap_or_default().to_owned())
        .or_else(|| var("LC_ALL"))
        .or_else(|| var("LC_MESSAGES"))
        .or_else(|| var("LANG"))
        .map_or("en", |name| language_of(&name))
}

/// A POSIX locale or BCP-47 tag as one of [`LANGUAGES`].
pub fn language_of(name: &str) -> &'static str {
    let base = name.split(['.', '@']).next().unwrap_or_default().replace('_', "-");
    let mut parts = base.split('-');
    let language = parts.next().unwrap_or_default().to_ascii_lowercase();
    let rest: Vec<String> = parts.map(str::to_ascii_lowercase).collect();
    match language.as_str() {
        "nb" | "nn" | "no" => "nb",
        "zh" if rest.is_empty() || rest.iter().any(|p| matches!(p.as_str(), "hans" | "cn" | "sg")) => "zh-Hans",
        "hi" => "hi",
        "es" => "es",
        "fr" => "fr",
        _ => "en",
    }
}

fn locale(language: &str) -> icu_locale_core::Locale {
    icu_locale_core::Locale::try_from_str(language).unwrap_or_else(|_| icu_locale_core::locale!("en"))
}

/// `{time}`: `at_ms` in the app's locale and this machine's time zone,
/// against `now_ms`: the time alone when it is today, with the weekday
/// within a week, else with the date. Empty when it cannot be formatted.
pub fn format_time(language: &str, at_ms: i64, now_ms: i64) -> String {
    use icu_datetime::input::{Date, DateTime, Time};
    let (Some(at), Some(now)) = (Local.timestamp_millis_opt(at_ms).single(), Local.timestamp_millis_opt(now_ms).single()) else {
        return String::new();
    };
    let Ok(time) = Time::try_new(at.hour() as u8, at.minute() as u8, 0, 0) else { return String::new() };
    let Ok(date) = Date::try_new_iso(at.year(), at.month() as u8, at.day() as u8) else { return String::new() };
    let datetime = DateTime { date, time };
    let days = (at.date_naive() - now.date_naive()).num_days();
    FORMATTERS.with(|cell| {
        let mut cell = cell.borrow_mut();
        if cell.as_ref().is_none_or(|f| f.language != language) {
            *cell = Some(Formatters { language: language.to_owned(), ..Formatters::default() });
        }
        let Some(f) = cell.as_mut() else { return String::new() };
        let prefs = || locale(language).into();
        let written = if days == 0 {
            made(&mut f.time, || NoCalendarFormatter::try_new(prefs(), T::hm()).ok()).map(|f| f.format(&time).to_string())
        } else if days.abs() < 7 {
            made(&mut f.weekday, || DateTimeFormatter::try_new(prefs(), E::short().with_time_hm()).ok()).map(|f| f.format(&datetime).to_string())
        } else if at.year() == now.year() {
            made(&mut f.date, || DateTimeFormatter::try_new(prefs(), MD::medium().with_time_hm()).ok()).map(|f| f.format(&datetime).to_string())
        } else {
            made(&mut f.year, || DateTimeFormatter::try_new(prefs(), YMD::medium().with_time_hm()).ok()).map(|f| f.format(&datetime).to_string())
        };
        written.unwrap_or_default()
    })
}

use icu_datetime::fieldsets::{E, ET, MD, MDT, T, YMD, YMDT};
use icu_datetime::{DateTimeFormatter, NoCalendarFormatter};

/// The `{time}` formatters of one language, made the first time each is
/// needed and kept until the language changes.
#[derive(Default)]
struct Formatters {
    language: String,
    time: Option<NoCalendarFormatter<T>>,
    weekday: Option<DateTimeFormatter<ET>>,
    date: Option<DateTimeFormatter<MDT>>,
    year: Option<DateTimeFormatter<YMDT>>,
}

thread_local! {
    static FORMATTERS: std::cell::RefCell<Option<Formatters>> = const { std::cell::RefCell::new(None) };
}

/// The formatter in `slot`, made by `make` when it is not there yet.
fn made<F>(slot: &mut Option<F>, make: impl FnOnce() -> Option<F>) -> Option<&F> {
    if slot.is_none() {
        *slot = make();
    }
    slot.as_ref()
}

/// `error.repeat_count` for `count` in the app's language.
pub fn repeat_count(language: &str, count: usize) -> String {
    use icu_plurals::{PluralCategory, PluralRules};
    let category = PluralRules::try_new_cardinal(locale(language).into()).map_or(PluralCategory::Other, |rules| rules.category_for(count as u64));
    let name = match category {
        PluralCategory::Zero => "zero",
        PluralCategory::One => "one",
        PluralCategory::Two => "two",
        PluralCategory::Few => "few",
        PluralCategory::Many => "many",
        PluralCategory::Other => "other",
    };
    let form = REPEAT_COUNT_ENGLISH.iter().find(|(c, _)| *c == name).or_else(|| REPEAT_COUNT_ENGLISH.iter().find(|(c, _)| *c == "other"));
    form.map_or_else(|| count.to_string(), |(_, text)| text.replace("{count}", &count.to_string()))
}

/// `template` with its `{name}` placeholders filled from `params`
/// (`{time}` from `time_ms`); `None` when one it uses is missing or empty.
pub fn fill(template: &str, params: &Object, language: &str, now_ms: i64) -> Option<String> {
    let mut out = String::with_capacity(template.len() + 16);
    let mut rest = template;
    while let Some(open) = rest.find('{') {
        out.push_str(&rest[..open]);
        let after = &rest[open + 1..];
        let close = after.find('}')?;
        let name = &after[..close];
        let value = match name {
            "time" => params.get("time_ms").and_then(Value::as_i64).map(|ms| format_time(language, ms, now_ms)).unwrap_or_default(),
            _ => params.get(name).map(|v| v.as_str().map_or_else(|| if v.is_null() { String::new() } else { v.to_string() }, str::to_owned)).unwrap_or_default(),
        };
        if value.trim().is_empty() {
            return None;
        }
        out.push_str(&value);
        rest = &after[close + 1..];
    }
    out.push_str(rest);
    Some(out)
}

/// A catalog string by key with `params`, or `None` when the catalog lacks
/// the key or a placeholder it uses is empty.
pub fn say(language: &str, key: &str, params: &Object, now_ms: i64) -> Option<String> {
    tr(language, key).and_then(|template| fill(template, params, language, now_ms))
}

/// A string the Host sends no words for (a note, a label): the catalog's in
/// `language`, else its English source, filled from `params`.
pub fn say_any(language: &str, key: &str, params: &Object, now_ms: i64) -> Option<String> {
    say(language, key, params, now_ms).or_else(|| english(key).and_then(|template| fill(template, params, language, now_ms)))
}

/// A string without parameters the Host sends no words for: the catalog's
/// in `language`, else its English source.
pub fn word(language: &str, key: &str) -> String {
    tr(language, key).or_else(|| english(key)).unwrap_or(key).to_owned()
}

/// The Host's own words for a fallback: Norwegian when the app speaks it,
/// else English.
fn host_words(words: &crate::turn_error::Words, language: &str) -> String {
    words.get(language == "nb").to_owned()
}

impl TurnError {
    /// The card's sentence in `language`: the catalog's by `key`, else the
    /// Host's text.
    pub fn sentence_in(&self, language: &str, now_ms: i64) -> String {
        say(language, &self.key, &self.params, now_ms).unwrap_or_else(|| host_words(&self.text, language))
    }
}

impl Action {
    /// The button's words in `language` (`error.action.<id>`, `{time}` the
    /// action's `at_ms`), else the Host's label.
    pub fn label_in(&self, language: &str, now_ms: i64) -> String {
        let mut params = Object::new();
        if let Some(at) = self.at_ms {
            params.insert("time_ms".into(), Value::from(at));
        }
        say(language, &format!("error.action.{}", self.id), &params, now_ms).unwrap_or_else(|| host_words(&self.label, language))
    }
}
