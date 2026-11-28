//! A failed turn as every app draws it: a stable code, one plain sentence in Norwegian and
//! English that says what happened and what the user can do, the actions that fit, and the
//! tool's raw words for the disclosure (feature `turn_error_explained`, `docs/turn-errors.md`).
//!
//! The Host decides all of it, so that the apps only draw: a new kind of failure is a change
//! here and nowhere else. The kind comes from [`failures::classify_error`]; this module only
//! says it.

use std::fmt::Write as _;
use std::sync::LazyLock;

use regex::Regex;
use serde_json::{Value, json};

use crate::failures;

/// What is known of one failed turn.
#[derive(Debug, Clone, Copy, Default)]
pub struct Failure<'a> {
    /// The failure's kind, as the turn's end keeps it. Empty: read off `said`.
    pub kind: &'a str,
    /// What the tool said, or the row that tells it ("Sentence.\n\nthe tool's words").
    pub said: &'a str,
    /// The agent's backend (`claude`, `codex`, `agy-acp`, ...).
    pub backend: &'a str,
    pub model: &'a str,
    /// The user's message the turn was for. Empty: nothing to send again.
    pub message_id: &'a str,
    pub trace_id: &'a str,
    /// When the account that ran out has usage again, where that is known.
    pub resets_at_ms: Option<i64>,
    /// When the turn failed. 0: unknown.
    pub at_ms: i64,
}

/// The codes, stable: an app may choose an icon by one, and never reads the sentence.
pub const CODES: [&str; 22] = [
    "usage_limit",
    "billing",
    "auth",
    "account_plan",
    "free_model_restricted",
    "connection",
    "network_gone",
    "provider_busy",
    "provider_down",
    "invalid_request",
    "refused",
    "prompt_too_long",
    "timeout",
    "runner_exit",
    "ended_from_outside",
    "interrupted",
    "not_started",
    "folder_missing",
    "provider_mismatch",
    "session_locked",
    "moved",
    "unknown",
];

static FREE_MODEL_RE: LazyLock<Regex> = LazyLock::new(|| {
    #[allow(clippy::expect_used)]
    Regex::new(r"(?i)free tier can only be used from within opencode")
        .expect("a fixed pattern compiles")
});
static PROVIDER_DOWN_RE: LazyLock<Regex> = LazyLock::new(|| {
    #[allow(clippy::expect_used)]
    Regex::new(r"(?i)upstream request failed|endpoint is unavailable|provider is unavailable")
        .expect("a fixed pattern compiles")
});
// A reset as the Host's own row says it: "· resets 18:27", "· resets Wed 19:00".
static HOST_RESET_RE: LazyLock<Regex> = LazyLock::new(|| {
    #[allow(clippy::expect_used)]
    Regex::new(r"· resets (?:(Mon|Tue|Wed|Thu|Fri|Sat|Sun) )?(\d{1,2}):(\d{2})\b")
        .expect("a fixed pattern compiles")
});
// A reset as a tool says it: "resets 10:10am (Europe/Oslo)", "resets Dec 2, 7pm", "try again
// at 6:27 PM".
static TOOL_RESET_RE: LazyLock<Regex> = LazyLock::new(|| {
    #[allow(clippy::expect_used)]
    Regex::new(concat!(
        r"(?i)(?:resets|try again at)\s+(?:(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.? (\d{1,2}),?\s+)?",
        r"(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\b",
    ))
    .expect("a fixed pattern compiles")
});

/// The code of a failure of this kind with these words.
pub fn code_of(kind: &str, said: &str) -> &'static str {
    let kind = if kind.is_empty() {
        failures::classify_error(said)
    } else {
        kind
    };
    if FREE_MODEL_RE.is_match(said) {
        return "free_model_restricted";
    }
    if said.contains("No network for 15 minutes") {
        return "network_gone";
    }
    match kind {
        failures::USAGE_LIMIT => {
            let words = raw_words(said);
            let words = if words.is_empty() { said } else { words };
            if failures::usage_limit_resets(words) || failures::usage_limit_resets(said) {
                "usage_limit"
            } else if billing(said) {
                "billing"
            } else {
                "usage_limit"
            }
        }
        failures::AUTH => "auth",
        failures::ACCOUNT_PLAN => "account_plan",
        failures::CONNECTION => "connection",
        failures::TRANSIENT if PROVIDER_DOWN_RE.is_match(said) => "provider_down",
        failures::TRANSIENT => "provider_busy",
        failures::INVALID_REQUEST => "invalid_request",
        failures::REFUSED => "refused",
        failures::PROMPT_TOO_LONG => "prompt_too_long",
        failures::TIMEOUT => "timeout",
        failures::RUNNER_EXIT => "runner_exit",
        failures::ENDED_FROM_OUTSIDE => "ended_from_outside",
        failures::INTERRUPTED => "interrupted",
        failures::NOT_STARTED => "not_started",
        failures::FOLDER_MISSING => "folder_missing",
        failures::PROVIDER_MISMATCH => "provider_mismatch",
        failures::SESSION_LOCKED | failures::MUSE_SESSION_LOCKED => "session_locked",
        crate::agent_moves::MOVED => "moved",
        _ if PROVIDER_DOWN_RE.is_match(said) => "provider_down",
        _ => "unknown",
    }
}

/// Whether a usage limit is one of credits, billing or payment: no reset clears it.
fn billing(said: &str) -> bool {
    let lower = said.to_lowercase();
    [
        "credit",
        "billing",
        "payment",
        "402",
        "spending limit",
        "monthly limit",
        "hard limit",
        "trial",
    ]
    .iter()
    .any(|word| lower.contains(word))
        && !lower.contains("usage credits")
}

/// The tool's own words of a row that tells a failure: what follows the Host's sentence, or
/// the whole when there is no sentence before it.
pub fn raw_words(said: &str) -> &str {
    said.split_once("\n\n").map_or(said, |(_, words)| words).trim()
}

/// How a backend is called to its user.
pub fn backend_name(backend: &str) -> String {
    match backend {
        "claude" => "Claude".to_string(),
        "codex" => "Codex".to_string(),
        "agy" | "agy-acp" | "antigravity" => "Antigravity".to_string(),
        "grok" | "muse" => "Grok".to_string(),
        "opencode" => "OpenCode".to_string(),
        "deepseek" => "DeepSeek".to_string(),
        "gemini" => "Gemini".to_string(),
        "" => String::new(),
        other => {
            let mut signs = other.chars();
            signs.next().map_or_else(String::new, |first| {
                first.to_uppercase().chain(signs).collect()
            })
        }
    }
}

/// One text in both languages.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Said {
    pub en: String,
    pub nb: String,
}

impl Said {
    fn new(en: impl Into<String>, nb: impl Into<String>) -> Self {
        Self {
            en: en.into(),
            nb: nb.into(),
        }
    }

    fn json(&self) -> Value {
        json!({"en": self.en, "nb": self.nb})
    }
}

const NB_DAYS: [&str; 7] = [
    "mandag", "tirsdag", "onsdag", "torsdag", "fredag", "lørdag", "søndag",
];
const NB_MONTHS: [&str; 12] = [
    "jan.", "feb.", "mars", "april", "mai", "juni", "juli", "aug.", "sep.", "okt.", "nov.", "des.",
];

/// When an account has usage again, as both languages say it against the day it was said:
/// "18:27" / "kl. 18:27" that day, "Wed 18:27" / "onsdag kl. 18:27" within the week, and
/// "2 Dec 18:27" / "2. des. kl. 18:27" beyond. `None` for a time that is no time.
pub fn reset_words(at_ms: i64, said_at_ms: i64, zone: &jiff::tz::TimeZone) -> Option<Said> {
    let zoned = |ms: i64| {
        jiff::Timestamp::from_millisecond(ms)
            .ok()
            .map(|at| at.to_zoned(zone.clone()))
    };
    let (at, said) = (zoned(at_ms)?, zoned(said_at_ms)?);
    let clock = at.strftime("%H:%M").to_string();
    let ahead = said.date().until(at.date()).ok()?.get_days();
    let weekday = usize::from(at.weekday().to_monday_zero_offset().unsigned_abs());
    let month = usize::try_from(at.month()).ok()?.saturating_sub(1);
    Some(match ahead {
        i32::MIN..=0 => Said::new(clock.clone(), format!("kl. {clock}")),
        1..=6 => Said::new(
            format!("{} {clock}", at.strftime("%a")),
            format!("{} kl. {clock}", NB_DAYS.get(weekday)?),
        ),
        _ => Said::new(
            format!("{} {} {clock}", at.day(), at.strftime("%b")),
            format!("{}. {} kl. {clock}", at.day(), NB_MONTHS.get(month)?),
        ),
    })
}

/// When the account has usage again, from words that say it as a time ("· resets 18:27",
/// "resets 10:10am", "resets Dec 2, 7pm"): the first such time after `at_ms`, in this zone.
pub fn said_reset(said: &str, at_ms: i64, zone: &jiff::tz::TimeZone) -> Option<i64> {
    if at_ms <= 0 {
        return None;
    }
    let written = jiff::Timestamp::from_millisecond(at_ms)
        .ok()?
        .to_zoned(zone.clone());
    let (day, hour, minute): (Option<jiff::civil::Date>, i8, i8) =
        if let Some(found) = HOST_RESET_RE.captures(said) {
            let hour = found.get(2)?.as_str().parse().ok()?;
            let minute = found.get(3)?.as_str().parse().ok()?;
            let day = match found.get(1) {
                Some(name) => {
                    let names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
                    let wanted = i8::try_from(names.iter().position(|day| *day == name.as_str())?).ok()?;
                    let today = written.weekday().to_monday_zero_offset();
                    let ahead = (wanted - today).rem_euclid(7);
                    Some(written.date().checked_add(jiff::Span::new().days(ahead)).ok()?)
                }
                None => None,
            };
            (day, hour, minute)
        } else {
            let found = TOOL_RESET_RE.captures(said)?;
            let mut hour: i8 = found.get(3)?.as_str().parse().ok()?;
            let minute: i8 = found.get(4).map_or(Some(0), |m| m.as_str().parse().ok())?;
            let pm = found.get(5)?.as_str().eq_ignore_ascii_case("p");
            if hour == 12 {
                hour = 0;
            }
            if pm {
                hour += 12;
            }
            let day = match (found.get(1), found.get(2)) {
                (Some(month), Some(date)) => {
                    let months = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"];
                    let month = months.iter().position(|m| month.as_str().eq_ignore_ascii_case(m))?;
                    let month = i8::try_from(month + 1).ok()?;
                    let date: i8 = date.as_str().parse().ok()?;
                    let mut day = jiff::civil::Date::new(written.year(), month, date).ok()?;
                    if day < written.date() {
                        day = jiff::civil::Date::new(written.year() + 1, month, date).ok()?;
                    }
                    Some(day)
                }
                _ => None,
            };
            (day, hour, minute)
        };
    if !(0..24).contains(&hour) || !(0..60).contains(&minute) {
        return None;
    }
    let first = day.unwrap_or_else(|| written.date());
    let at = first.at(hour, minute, 0, 0).to_zoned(zone.clone()).ok()?;
    let at = if day.is_none() && at.timestamp() <= written.timestamp() {
        at.tomorrow().ok()?
    } else {
        at
    };
    Some(at.timestamp().as_millisecond())
}

/// One action, with its label in both languages.
fn action(id: &str, label: &Said) -> Value {
    json!({"id": id, "label": label.json()})
}

/// What the user is told of a failed turn: the object of `docs/turn-errors.md`.
pub fn explained(failure: &Failure<'_>) -> Value {
    explained_in(failure, &jiff::tz::TimeZone::system())
}

/// [`explained`], with the times told in this zone.
#[allow(clippy::too_many_lines)]
pub fn explained_in(failure: &Failure<'_>, zone: &jiff::tz::TimeZone) -> Value {
    let said = failure.said.trim();
    let code = code_of(failure.kind, said);
    let detail: String = failures::provider_message(raw_words(said))
        .chars()
        .take(1000)
        .collect();
    let named = backend_name(failure.backend);
    let model = failure.model.trim();
    let resets_at_ms = failure
        .resets_at_ms
        .or_else(|| said_reset(said, failure.at_ms, zone));
    let said_at = if failure.at_ms > 0 {
        failure.at_ms
    } else {
        resets_at_ms.unwrap_or_default()
    };
    let reset = resets_at_ms.and_then(|at| reset_words(at, said_at, zone));

    // "Codex-kontoen din" / "Your Codex account"; "Kontoen din" / "Your account" with no name.
    let (account_en, account_nb) = if named.is_empty() {
        ("Your account".to_string(), "Kontoen din".to_string())
    } else {
        (format!("Your {named} account"), format!("{named}-kontoen din"))
    };
    let (at_en, at_nb) = if named.is_empty() {
        ("the provider".to_string(), "leverandøren".to_string())
    } else {
        (named.clone(), named.clone())
    };
    let (model_en, model_nb) = if model.is_empty() {
        ("this model".to_string(), "denne modellen".to_string())
    } else {
        (model.to_string(), model.to_string())
    };

    let text = match code {
        "usage_limit" => match &reset {
            Some(when) => Said::new(
                format!("{account_en} is out of usage until {}. Try again then, or switch model.", when.en),
                format!("{account_nb} er tom for kvote til {}. Prøv igjen da, eller bytt modell.", when.nb),
            ),
            None => Said::new(
                format!("{account_en} is out of usage. Try again later, or switch model."),
                format!("{account_nb} er tom for kvote. Prøv igjen senere, eller bytt modell."),
            ),
        },
        "billing" => Said::new(
            format!("{account_en} is out of credits. Top it up, or switch model."),
            format!("{account_nb} er tom for kreditt. Fyll på, eller bytt modell."),
        ),
        "auth" => Said::new(
            format!("Your {at_en} sign-in has expired. Sign in again, then try again."),
            format!("Innloggingen hos {at_nb} har gått ut. Logg inn på nytt, og prøv igjen."),
        ),
        "account_plan" => Said::new(
            format!("Your {at_en} plan does not include {model_en}. Switch model or account."),
            format!("Abonnementet ditt hos {at_nb} har ikke {model_nb}. Bytt modell eller konto."),
        ),
        "free_model_restricted" => Said::new(
            "This free OpenCode model only runs in OpenCode's own apps. Choose another model.",
            "Denne gratismodellen fra OpenCode virker bare i OpenCodes egne apper. Velg en annen modell.",
        ),
        "connection" => Said::new(
            format!("Lost the connection to {at_en}, and the Host's retries did not get through. Try again."),
            format!("Forbindelsen til {at_nb} brøt, og Hostens nye forsøk kom ikke gjennom. Prøv igjen."),
        ),
        "network_gone" => Said::new(
            "This computer had no network for 15 minutes, so the turn stopped. Try again once it is online.",
            "Maskinen var uten nett i 15 minutter, så turen stoppet. Prøv igjen når den er på nett.",
        ),
        "provider_busy" => Said::new(
            format!("{at_en} is overloaded right now, and the Host's retries did not get through. Try again in a minute, or switch model."),
            format!("{at_nb} er overbelastet akkurat nå, og Hostens nye forsøk kom ikke gjennom. Prøv igjen om et minutt, eller bytt modell."),
        ),
        "provider_down" => Said::new(
            format!("The provider of {model_en} is unavailable right now. Try again later, or switch model."),
            format!("Leverandøren av {model_nb} er nede akkurat nå. Prøv igjen senere, eller bytt modell."),
        ),
        "invalid_request" => Said::new(
            format!("{at_en} rejected the conversation history. Start a fresh session with the same conversation and try again."),
            format!("{at_nb} avviste samtalehistorikken. Start en ny økt med samme samtale og prøv igjen."),
        ),
        "refused" => Said::new(
            "The model refused to answer (a safety filter blocked it). Rephrase, or switch model.",
            "Modellen ville ikke svare (et sikkerhetsfilter stoppet det). Omformuler, eller bytt modell.",
        ),
        "prompt_too_long" => Said::new(
            "This conversation has grown too long for the model. Compact it and continue.",
            "Samtalen har blitt for lang for modellen. Komprimer den og fortsett.",
        ),
        "timeout" => Said::new(
            format!("{at_en} stopped responding. Try again."),
            format!("{at_nb} sluttet å svare. Prøv igjen."),
        ),
        "runner_exit" => Said::new(
            format!("{at_en} stopped unexpectedly. Try again."),
            format!("{at_nb} stoppet uventet. Prøv igjen."),
        ),
        "ended_from_outside" => Said::new(
            "Something outside Clarp ended the turn. Send the message again.",
            "Noe utenfor Clarp avbrøt turen. Send meldingen igjen.",
        ),
        "interrupted" => Said::new(
            "The turn was cut off before it finished. Send the message again.",
            "Turen ble avbrutt før den var ferdig. Send meldingen igjen.",
        ),
        "not_started" => Said::new(
            "The Host could not start the turn. Try again.",
            "Hosten fikk ikke startet turen. Prøv igjen.",
        ),
        "folder_missing" => {
            let dir = failures::missing_folder(said)
                .map(|missing| {
                    missing
                        .strip_prefix(clarp_spool::NOT_A_FOLDER)
                        .unwrap_or(missing)
                        .trim()
                        .to_string()
                })
                .unwrap_or_default();
            if dir.is_empty() {
                Said::new(
                    "This agent's folder no longer exists. Recreate it, or make a new agent in another folder.",
                    "Mappen til agenten finnes ikke lenger. Opprett den igjen, eller lag en ny agent i en annen mappe.",
                )
            } else {
                Said::new(
                    format!("This agent's folder {dir} no longer exists. Recreate it, or make a new agent in another folder."),
                    format!("Mappen til agenten, {dir}, finnes ikke lenger. Opprett den igjen, eller lag en ny agent i en annen mappe."),
                )
            }
        }
        "provider_mismatch" => Said::new(
            "This agent ran on another model provider than the one it is bound to. Check its Codex home and provider.",
            "Agenten kjørte hos en annen modelleverandør enn den er bundet til. Sjekk Codex-mappen og leverandøren.",
        ),
        "session_locked" => Said::new(
            format!("The {at_en} session is busy in another process. Try again in a moment."),
            format!("Økten hos {at_nb} er i bruk av en annen prosess. Prøv igjen om litt."),
        ),
        "moved" => Said::new(
            "This agent moved to another Host. Continue the conversation there.",
            "Agenten er flyttet til en annen Host. Fortsett samtalen der.",
        ),
        _ => Said::new(
            format!("The turn failed at {at_en}. Try again, or switch model if it happens again."),
            format!("Turen feilet hos {at_nb}. Prøv igjen, eller bytt modell hvis det skjer igjen."),
        ),
    };

    let severity = match code {
        "usage_limit" | "account_plan" | "free_model_restricted" | "provider_busy"
        | "refused" | "prompt_too_long" | "session_locked" => "warning",
        "ended_from_outside" | "interrupted" | "moved" => "info",
        _ => "error",
    };

    let retry = Said::new("Try again", "Prøv igjen");
    let switch = Said::new("Switch backend or model", "Bytt backend eller modell");
    let copy = Said::new("Copy for AI", "Kopier for AI");
    let can_send = !failure.message_id.is_empty();
    let mut actions: Vec<Value> = Vec::new();
    // An action that sends the message again is offered only where there is one.
    let push = |actions: &mut Vec<Value>, id: &str, label: &Said, sends: bool| {
        if !sends || can_send {
            actions.push(action(id, label));
        }
    };
    match code {
        "usage_limit" => {
            if let (Some(at), Some(when)) = (resets_at_ms, &reset) {
                if can_send {
                    let label = if when.en.contains(' ') {
                        Said::new(format!("Try again {}", when.en), format!("Prøv igjen {}", when.nb))
                    } else {
                        Said::new(format!("Try again at {}", when.en), format!("Prøv igjen {}", when.nb))
                    };
                    let mut retry_at = action("retry_at", &label);
                    retry_at["at_ms"] = json!(at);
                    actions.push(retry_at);
                }
            } else {
                push(&mut actions, "retry", &retry, true);
            }
            push(&mut actions, "switch_model", &switch, false);
            push(&mut actions, "copy_details", &copy, false);
        }
        "billing" | "account_plan" | "refused" => {
            push(&mut actions, "switch_model", &switch, false);
            push(&mut actions, "copy_details", &copy, false);
        }
        "free_model_restricted" => push(&mut actions, "switch_model", &switch, false),
        "auth" => {
            let mut sign_in = action("sign_in", &Said::new("Sign in again", "Logg inn på nytt"));
            sign_in["backend"] = json!(failure.backend);
            actions.push(sign_in);
            push(&mut actions, "retry", &retry, true);
            push(&mut actions, "copy_details", &copy, false);
        }
        "connection" | "timeout" | "runner_exit" | "not_started" => {
            push(&mut actions, "retry", &retry, true);
            push(&mut actions, "copy_details", &copy, false);
        }
        "network_gone" | "ended_from_outside" | "interrupted" | "session_locked" => {
            push(&mut actions, "retry", &retry, true);
        }
        "provider_busy" | "provider_down" | "unknown" => {
            push(&mut actions, "retry", &retry, true);
            push(&mut actions, "switch_model", &switch, false);
            push(&mut actions, "copy_details", &copy, false);
        }
        "invalid_request" => {
            push(&mut actions, 
                "fresh_retry",
                &Said::new("New session, try again", "Ny økt, prøv igjen"),
                true,
            );
            push(&mut actions, "switch_model", &switch, false);
            push(&mut actions, "copy_details", &copy, false);
        }
        "prompt_too_long" => {
            push(&mut actions, 
                "compact_retry",
                &Said::new("Compact and continue", "Komprimer og fortsett"),
                true,
            );
            push(&mut actions, "switch_model", &switch, false);
        }
        "folder_missing" | "provider_mismatch" => push(&mut actions, "copy_details", &copy, false),
        _ => {}
    }

    let mut error = json!({
        "code": code,
        "key": format!("turn_error.{code}"),
        "text": text.json(),
        "severity": severity,
        "actions": actions,
        "detail": detail,
        "backend": failure.backend,
        "model": model,
        "message_id": failure.message_id,
        "trace_id": failure.trace_id,
        "fingerprint": fingerprint(code, failure.backend, &detail),
        "copy_text": copy_text(code, failure, &text, &detail, zone),
    });
    if let Some(at) = resets_at_ms {
        error["resets_at_ms"] = json!(at);
    }
    error
}

/// Equal for the same failure: its code, its backend, and its words with every number taken
/// out (a reset time, an index, a request id differ from one to the next).
pub fn fingerprint(code: &str, backend: &str, detail: &str) -> String {
    let mut hash: u32 = 0x811c_9dc5;
    let mut spaced = false;
    for sign in detail.chars().take(300).flat_map(char::to_lowercase) {
        let sign = if sign.is_ascii_digit() {
            '#'
        } else if sign.is_whitespace() {
            if spaced {
                continue;
            }
            ' '
        } else {
            sign
        };
        spaced = sign == ' ';
        let mut bytes = [0; 4];
        for byte in sign.encode_utf8(&mut bytes).bytes() {
            hash ^= u32::from(byte);
            hash = hash.wrapping_mul(0x0100_0193);
        }
    }
    format!("{code}:{backend}:{hash:08x}")
}

/// What "Copy for AI" puts on the clipboard: a short prompt an agent can diagnose from.
fn copy_text(
    code: &str,
    failure: &Failure<'_>,
    text: &Said,
    detail: &str,
    zone: &jiff::tz::TimeZone,
) -> String {
    let mut out = String::from("A Clarp agent turn failed. Diagnose it and tell me what to change.\n\n");
    let _ = writeln!(out, "Code: {code}");
    if !failure.backend.is_empty() {
        let _ = writeln!(out, "Backend: {}", failure.backend);
    }
    if !failure.model.is_empty() {
        let _ = writeln!(out, "Model: {}", failure.model);
    }
    if !failure.trace_id.is_empty() {
        let _ = writeln!(out, "Trace: {}", failure.trace_id);
    }
    if failure.at_ms > 0
        && let Ok(at) = jiff::Timestamp::from_millisecond(failure.at_ms)
    {
        let _ = writeln!(out, "At: {}", at.to_zoned(zone.clone()).strftime("%Y-%m-%d %H:%M %Z"));
    }
    let _ = writeln!(out, "Clarp told the user: {}", text.en);
    if !detail.is_empty() {
        let _ = write!(out, "\nThe provider said:\n{detail}");
    }
    out.trim_end().to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn oslo() -> jiff::tz::TimeZone {
        jiff::tz::TimeZone::get("Europe/Oslo").unwrap_or(jiff::tz::TimeZone::UTC)
    }

    fn ms(text: &str) -> i64 {
        text.parse::<jiff::Timestamp>()
            .map(|at| at.as_millisecond())
            .unwrap_or_default()
    }

    fn ids(error: &Value) -> Vec<&str> {
        error["actions"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(|action| action["id"].as_str())
            .collect()
    }

    #[test]
    fn the_codex_call_id_rejection_is_an_invalid_request_with_a_fresh_session_first() {
        let said = "Invalid response.create payload: Invalid 'input[29].call_id': string too long. \
                    Expected a string with maximum length 64, but got a string with length 71 instead.";
        let error = explained_in(
            &Failure { said, backend: "codex", model: "gpt-6.1", message_id: "u1", ..Failure::default() },
            &oslo(),
        );
        assert_eq!(error["code"], "invalid_request");
        assert_eq!(ids(&error), ["fresh_retry", "switch_model", "copy_details"]);
        assert!(error["text"]["nb"].as_str().unwrap_or("").starts_with("Codex avviste"));
        assert_eq!(error["detail"], said);
    }

    #[test]
    fn the_empty_tool_result_400_is_an_invalid_request() {
        let said = "Turn failed.\n\nAPI Error: 400 messages.125.content.1.tool_result: content cannot \
                    be empty if `is_error` is true";
        assert_eq!(code_of("", said), "invalid_request");
        let other = said.replace("125", "23");
        assert_eq!(
            fingerprint("invalid_request", "claude", raw_words(said)),
            fingerprint("invalid_request", "claude", raw_words(&other)),
            "the same failure at another index collapses",
        );
    }

    #[test]
    fn a_usage_limit_says_its_reset_in_both_languages_and_offers_to_try_then() {
        let at = ms("2026-11-28T10:00:00Z");
        let resets = ms("2026-11-28T17:27:00Z");
        let error = explained_in(
            &Failure {
                kind: failures::USAGE_LIMIT,
                said: "You've hit your 5-hour limit · resets 6:27pm (Europe/Oslo)",
                backend: "claude",
                message_id: "u4",
                resets_at_ms: Some(resets),
                at_ms: at,
                ..Failure::default()
            },
            &oslo(),
        );
        assert_eq!(error["code"], "usage_limit");
        assert_eq!(
            error["text"]["nb"],
            "Claude-kontoen din er tom for kvote til kl. 18:27. Prøv igjen da, eller bytt modell."
        );
        assert_eq!(error["actions"][0]["id"], "retry_at");
        assert_eq!(error["actions"][0]["at_ms"], resets);
        assert_eq!(error["actions"][0]["label"]["en"], "Try again at 18:27");
        assert_eq!(error["actions"][0]["label"]["nb"], "Prøv igjen kl. 18:27");
    }

    #[test]
    fn an_old_row_reads_its_reset_off_its_words() {
        let at = ms("2026-11-27T05:00:00Z");
        let error = explained_in(
            &Failure {
                kind: "usage_limit",
                said: "Usage limit reached · resets 10:10.\n\nYou've hit your 5-hour limit · resets 10:10am (Europe/Oslo)",
                backend: "claude",
                message_id: "u",
                at_ms: at,
                ..Failure::default()
            },
            &oslo(),
        );
        assert_eq!(error["resets_at_ms"], ms("2026-11-27T09:10:00Z"));
        assert_eq!(error["detail"], "You've hit your 5-hour limit · resets 10:10am (Europe/Oslo)");
    }

    #[test]
    fn a_tools_own_words_give_the_reset() {
        let at = ms("2026-11-28T10:00:00Z");
        let zone = oslo();
        assert_eq!(
            said_reset("You've hit your 5-hour limit · resets 6:27pm (Europe/Oslo)", at, &zone),
            Some(ms("2026-11-28T17:27:00Z"))
        );
        assert_eq!(
            said_reset("You've hit your weekly limit · resets Dec 2, 7pm (Europe/Oslo)", at, &zone),
            Some(ms("2026-12-02T18:00:00Z"))
        );
        assert_eq!(
            said_reset("Usage limit reached · resets Wed 19:00.", at, &zone),
            Some(ms("2026-12-02T18:00:00Z"))
        );
        assert_eq!(said_reset("You've hit your usage limit.", at, &zone), None);
    }

    #[test]
    fn credits_are_billing_and_offer_no_retry() {
        let error = explained(&Failure {
            kind: "usage_limit",
            said: "Usage limit reached.\n\nYour workspace is out of credits. Ask your workspace owner to refill in order to continue.",
            backend: "codex",
            message_id: "u",
            ..Failure::default()
        });
        assert_eq!(error["code"], "billing");
        assert_eq!(ids(&error), ["switch_model", "copy_details"]);
    }

    #[test]
    fn an_expired_sign_in_offers_to_sign_in_to_its_backend() {
        let error = explained(&Failure {
            said: "Failed to authenticate: OAuth session expired and could not be refreshed",
            backend: "claude",
            message_id: "u",
            ..Failure::default()
        });
        assert_eq!(error["code"], "auth");
        assert_eq!(error["actions"][0]["id"], "sign_in");
        assert_eq!(error["actions"][0]["backend"], "claude");
    }

    #[test]
    fn the_rest_of_the_real_failures_read_as_their_codes() {
        let cases = [
            ("Connection lost — retries exhausted.\n\nAPI Error: Can't reach the API server — check your internet or DNS (EAI_AGAIN)\nclaude exited with code 1", "connection"),
            ("agy exited rc=1: status=ERROR: Your previous response was blocked by content safety filters", "refused"),
            ("thread/resume failed: thread 01a1 already has an active writer (code -32600)", "session_locked"),
            ("Prompt is too long · the request is ~1050000 tokens (limit 1000000)", "prompt_too_long"),
            ("exceeded retry limit, last status: 429 Too Many Requests", "provider_busy"),
            ("claude exited with code 143", "runner_exit"),
            ("Waiting for a claude account with available usage", "usage_limit"),
            ("claude reported an error", "unknown"),
        ];
        for (said, code) in cases {
            assert_eq!(code_of("", said), code, "{said}");
        }
    }

    #[test]
    fn no_message_means_no_retry() {
        let error = explained(&Failure { said: "claude exited with code 1", backend: "claude", ..Failure::default() });
        assert_eq!(ids(&error), ["copy_details"]);
    }

    #[test]
    fn every_code_has_both_languages() {
        for code in CODES {
            let kind = match code {
                "usage_limit" | "billing" => "usage_limit",
                other => other,
            };
            let error = explained(&Failure { kind, said: "x", backend: "codex", message_id: "u", ..Failure::default() });
            assert!(!error["text"]["en"].as_str().unwrap_or("").is_empty(), "{code}");
            assert!(!error["text"]["nb"].as_str().unwrap_or("").is_empty(), "{code}");
        }
    }
}
