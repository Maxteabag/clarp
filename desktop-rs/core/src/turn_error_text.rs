//! The words of a failed turn's card (docs/turn-errors.md "Catalog keys",
//! docs/i18n.md): the Host sends a code (`error.key`) and its parameters, and
//! the desktop says the sentence from its own catalog (`tr!`, the key as the
//! context) in the language in use. The Host's own `text` is only the
//! fallback: for a key the catalog does not have, a placeholder left empty,
//! or a language whose catalog has no translation of the key yet (the Host's
//! Norwegian then beats the English source). `{time}` is written by
//! `i18n::format::moment`, the repeat count by the catalog's plural forms;
//! nothing here builds date words or plural endings by hand.
//!
//! Every string is made when the card is drawn, so a language change takes
//! effect at once.

// i18n-screen: chat

use chrono::{Local, TimeZone};
use serde_json::Value;

use crate::i18n::{self, format};
use crate::json::Object;
use crate::tr;
use crate::turn_error::{Action, TurnError};

/// The languages Clarp ships, as BCP-47 tags.
pub const LANGUAGES: &[&str] = &["en", "nb", "zh-Hans", "hi", "es", "fr"];

/// `error.repeat_count`'s key (a plural: [`repeat_count`]).
pub const REPEAT_COUNT: &str = "error.repeat_count";

/// The catalog's text for `key` in the language in use, its `{name}`
/// placeholders still in it (`tr!` fills only `{}`, `{0}` and `{n}`); `None`
/// for a key the catalog does not have. Untranslated, it is the English.
fn template(language: &str, key: &str) -> Option<String> {
    macro_rules! tr {
        ($context:literal => $text:literal) => {
            i18n::translate_in(language, $context, $text, "", None, &[])
        };
    }
    Some(match key {
        // i18n: chat, failed-turn card, the sentence saying why the agent's turn failed (the account's usage window is spent); {backend} is a backend name (Claude, Codex, ...), not translated
        "error.usage_limit" => tr!("error.usage_limit" => "Your {backend} account is out of usage. Try again later, or switch model."),
        // i18n: chat, failed-turn card, the sentence saying why the agent's turn failed (the account's usage window is spent); {backend} is a backend name (Claude, Codex, ...), not translated; {time} is a time already written in the language ("18:27", "Sat 18:27")
        "error.usage_limit_until" => tr!("error.usage_limit_until" => "Your {backend} account is out of usage until {time}. Try again then, or switch model."),
        // i18n: chat, failed-turn card, the sentence saying why the agent's turn failed (credits, payment, a hard or monthly limit: no reset clears it); {backend} is a backend name (Claude, Codex, ...), not translated
        "error.billing" => tr!("error.billing" => "Your {backend} account is out of credits. Top it up, or switch model."),
        // i18n: chat, failed-turn card, the sentence saying why the agent's turn failed (the sign-in ran out); {backend} is a backend name (Claude, Codex, ...), not translated
        "error.auth" => tr!("error.auth" => "Your {backend} sign-in has expired. Sign in again, then try again."),
        // i18n: chat, failed-turn card, the sentence saying why the agent's turn failed (the plan lacks the model); {backend} is a backend name (Claude, Codex, ...), not translated; {model} is a model name, not translated
        "error.account_plan" => tr!("error.account_plan" => "Your {backend} plan does not include {model}. Switch model or account."),
        // i18n: chat, failed-turn card, the sentence saying why the agent's turn failed (an OpenCode free model refused outside OpenCode)
        "error.free_model_restricted" => tr!("error.free_model_restricted" => "This free OpenCode model only runs in OpenCode's own apps. Choose another model."),
        // i18n: chat, failed-turn card, the sentence saying why the agent's turn failed (the link to the provider broke, retries used up); {backend} is a backend name (Claude, Codex, ...), not translated
        "error.connection" => tr!("error.connection" => "Lost the connection to {backend}, and the Host's retries did not get through. Try again."),
        // i18n: chat, failed-turn card, the sentence saying why the agent's turn failed (the computer had no network for 15 minutes)
        "error.network_gone" => tr!("error.network_gone" => "This computer had no network for 15 minutes, so the turn stopped. Try again once it is online."),
        // i18n: chat, failed-turn card, the sentence saying why the agent's turn failed (overloaded, rate limited, 429/5xx after the Host's own retries); {backend} is a backend name (Claude, Codex, ...), not translated
        "error.provider_busy" => tr!("error.provider_busy" => "{backend} is overloaded right now, and the Host's retries did not get through. Try again in a minute, or switch model."),
        // i18n: chat, failed-turn card, the sentence saying why the agent's turn failed (the model's provider is unavailable); {model} is a model name, not translated
        "error.provider_down" => tr!("error.provider_down" => "The provider of {model} is unavailable right now. Try again later, or switch model."),
        // i18n: chat, failed-turn card, the sentence saying why the agent's turn failed (the provider rejected the request as malformed (bad tool or call ids, an empty tool result, invalid payload, 400). Trying the same session again replays the same history, so the first action starts a fresh one); {backend} is a backend name (Claude, Codex, ...), not translated
        "error.invalid_request" => tr!("error.invalid_request" => "{backend} rejected the conversation history. Start a fresh session with the same conversation and try again."),
        // i18n: chat, failed-turn card, the sentence saying why the agent's turn failed (the model refused (a safety filter blocked the answer))
        "error.refused" => tr!("error.refused" => "The model refused to answer (a safety filter blocked it). Rephrase, or switch model."),
        // i18n: chat, failed-turn card, the sentence saying why the agent's turn failed (the conversation no longer fits the model)
        "error.prompt_too_long" => tr!("error.prompt_too_long" => "This conversation has grown too long for the model. Compact it and continue."),
        // i18n: chat, failed-turn card, the sentence saying why the agent's turn failed (the backend stopped responding); {backend} is a backend name (Claude, Codex, ...), not translated
        "error.timeout" => tr!("error.timeout" => "{backend} stopped responding. Try again."),
        // i18n: chat, failed-turn card, the sentence saying why the agent's turn failed (the backend's process ended unexpectedly); {backend} is a backend name (Claude, Codex, ...), not translated
        "error.runner_exit" => tr!("error.runner_exit" => "{backend} stopped unexpectedly. Try again."),
        // i18n: chat, failed-turn card, the sentence saying why the agent's turn failed (a signal from outside the Host ended the turn)
        "error.ended_from_outside" => tr!("error.ended_from_outside" => "Something outside Clarp ended the turn. Send the message again."),
        // i18n: chat, failed-turn card, the sentence saying why the agent's turn failed (the turn was cut off before it finished (a restart, a process that went without a word))
        "error.interrupted" => tr!("error.interrupted" => "The turn was cut off before it finished. Send the message again."),
        // i18n: chat, failed-turn card, the sentence saying why the agent's turn failed (the Host could not start the turn)
        "error.not_started" => tr!("error.not_started" => "The Host could not start the turn. Try again."),
        // i18n: chat, failed-turn card, the sentence saying why the agent's turn failed (the agent's folder is gone); {folder} is a folder path
        "error.folder_missing" => tr!("error.folder_missing" => "This agent's folder {folder} no longer exists. Recreate it, or make a new agent in another folder."),
        // i18n: chat, failed-turn card, the sentence saying why the agent's turn failed (the agent's folder is gone)
        "error.folder_missing_no_path" => tr!("error.folder_missing_no_path" => "This agent's folder no longer exists. Recreate it, or make a new agent in another folder."),
        // i18n: chat, failed-turn card, the sentence saying why the agent's turn failed (the agent ran on another provider than it is bound to)
        "error.provider_mismatch" => tr!("error.provider_mismatch" => "This agent ran on another model provider than the one it is bound to. Check its Codex home and provider."),
        // i18n: chat, failed-turn card, the sentence saying why the agent's turn failed (the backend's session is held by another process (a Codex thread with another active writer, a locked Grok session)); {backend} is a backend name (Claude, Codex, ...), not translated
        "error.session_locked" => tr!("error.session_locked" => "The {backend} session is busy in another process. Try again in a moment."),
        // i18n: chat, failed-turn card, the sentence saying why the agent's turn failed (the agent moved to another Host)
        "error.moved" => tr!("error.moved" => "This agent moved to another Host. Continue the conversation there."),
        // i18n: chat, failed-turn card, the sentence saying why the agent's turn failed (anything else); {backend} is a backend name (Claude, Codex, ...), not translated
        "error.unknown" => tr!("error.unknown" => "The turn failed at {backend}. Try again, or switch model if it happens again."),
        // i18n: chat, failed-turn card, button (an action, imperative)
        "error.action.retry" => tr!("error.action.retry" => "Try again"),
        // i18n: chat, failed-turn card, button (an action, imperative); {time} is a time already written in the language ("18:27", "Sat 18:27")
        "error.action.retry_at" => tr!("error.action.retry_at" => "Try again at {time}"),
        // i18n: chat, failed-turn card, button (an action, imperative): starts a new session with the same conversation and sends the message again
        "error.action.fresh_retry" => tr!("error.action.fresh_retry" => "New session, try again"),
        // i18n: chat, failed-turn card, button (an action, imperative): compacts the conversation, then sends the message again
        "error.action.compact_retry" => tr!("error.action.compact_retry" => "Compact and continue"),
        // i18n: chat, failed-turn card, button (an action, imperative)
        "error.action.switch_model" => tr!("error.action.switch_model" => "Switch backend or model"),
        // i18n: chat, failed-turn card, button (an action, imperative)
        "error.action.sign_in" => tr!("error.action.sign_in" => "Sign in again"),
        // i18n: chat, failed-turn card, button (an action, imperative): copies a diagnostic text for an agent; Norwegian per owner: "Kopier diagnose"
        "error.action.copy_details" => tr!("error.action.copy_details" => "Copy diagnostics"),
        // i18n: chat, failed-turn card, disclosure label (noun): opens the provider's raw error text
        "error.details" => tr!("error.details" => "Details"),
        // i18n: chat, failed-turn card, short confirmation after Copy diagnostics; must match the Host-down screen's "copied"
        "error.copied" => tr!("error.copied" => "Diagnostics copied"),
        // i18n: chat, failed-turn card, note under the buttons after a scheduled retry was accepted; {time} is a time already written in the language ("18:27", "Sat 18:27")
        "error.retry_scheduled" => tr!("error.retry_scheduled" => "Will try again at {time}"),
        // i18n: chat, failed-turn card, note under the buttons after the message was sent again
        "error.retry_sent" => tr!("error.retry_sent" => "Sent again"),
        // i18n: chat, failed-turn card, note under the buttons when a button's request was refused
        "error.action_failed" => tr!("error.action_failed" => "That didn't work. Try again."),
        _ => return None,
    })
}

/// The catalog's own string for `key` in `language` (the language in use):
/// the English source in English, a translation where the language's catalog
/// has one, else `None`, so a sentence falls back to the Host's text in that
/// language rather than to English (docs/turn-errors.md, "key and params").
pub fn tr(language: &str, key: &str) -> Option<String> {
    if !i18n::translated(language, key) {
        return None;
    }
    template(language, key)
}

/// The English template for `key`, with placeholders in source order.
pub fn english(key: &str) -> Option<String> {
    template("en", key)
}

/// The app's language: the one in use (`i18n::current()`, the device's or
/// the user's choice in Settings).
pub fn app_language() -> &'static str {
    i18n::current()
}

/// A POSIX locale or BCP-47 tag as one of [`LANGUAGES`] (English for
/// anything not shipped).
pub fn language_of(name: &str) -> &'static str {
    i18n::supported(name).unwrap_or("en")
}

/// `{time}`: `at_ms` in `language` and this machine's time zone, against
/// `now_ms`: the time alone today, with the weekday within a week, else with
/// the date. Empty when it cannot be formatted.
pub fn format_time(language: &str, at_ms: i64, now_ms: i64) -> String {
    let (Some(at), Some(now)) = (Local.timestamp_millis_opt(at_ms).single(), Local.timestamp_millis_opt(now_ms).single()) else {
        return String::new();
    };
    format::moment(language, at.naive_local(), now.date_naive(), format::Clock::Locale)
}

/// `error.repeat_count` for `count`, by the language's plural forms ("3×").
pub fn repeat_count(language: &str, count: usize) -> String {
    let _ = language;
    // i18n: chat, failed-turn card, badge for how many times the same failure repeated in a row; keep it short
    tr!("error.repeat_count" => "{n}×" | "{n}×" % count)
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

/// The catalog's own string for `key` with `params`, or `None` when the
/// language has no translation of it, the catalog lacks it or a placeholder
/// it uses is empty.
pub fn say(language: &str, key: &str, params: &Object, now_ms: i64) -> Option<String> {
    tr(language, key).and_then(|template| fill(&template, params, language, now_ms))
}

/// A string the Host sends no words for (a note, a label): the catalog's in
/// the language in use, its English source where it has no translation.
pub fn say_any(language: &str, key: &str, params: &Object, now_ms: i64) -> Option<String> {
    template(language, key).and_then(|template| fill(&template, params, language, now_ms))
}

/// A string without parameters the Host sends no words for: the catalog's
/// in the language in use, its English source where it has no translation.
pub fn word(language: &str, key: &str) -> String {
    template(language, key).unwrap_or_else(|| key.to_owned())
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
