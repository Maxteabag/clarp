//! How a turn ended, beyond "clean", read off the tool's own words.
//!
//! What the Host does next depends on it. A dropped connection is worth trying again in
//! silence. A usage limit, a sign-in that ran out, a plan that lacks the model: another
//! account may serve the turn, so it waits for one. A stop was meant. Everything else the
//! user is told. A tool says these things in its own words, and they change with every
//! release of every tool, so the reading is a list of wordings, kept in one place.
//!
//! These are the Python Host's readings (`server/lib/error_classify.py`), wording for
//! wording, and they are held to it by every text its own tests use
//! (`tests/failure_vectors.rs`).

use std::sync::LazyLock;

use regex::Regex;
use serde_json::Value;

pub const CLEAN: &str = "clean";
/// The link between the tool and its provider broke in the middle. Worth a few tries.
pub const CONNECTION: &str = "connection";
/// The provider answered, with back-pressure or an error of its own.
pub const TRANSIENT: &str = "transient";
/// The turn was ended on purpose. Never tried again.
pub const INTERRUPTED: &str = "interrupted";
/// The account is out of usage, quota or credits.
pub const USAGE_LIMIT: &str = "usage_limit";
/// The sign-in ran out or could not be renewed.
pub const AUTH: &str = "auth";
/// The account's plan does not include the model.
pub const ACCOUNT_PLAN: &str = "account_plan";
/// The tool's process ended badly, without a word that says why.
pub const RUNNER_EXIT: &str = "runner_exit";
pub const TIMEOUT: &str = "timeout";
/// The Host ended the turn before a process ran for it. Never read off a text: the Host
/// names it itself.
pub const NOT_STARTED: &str = "not_started";
pub const UNKNOWN: &str = "unknown";
pub const MUSE_SESSION_LOCKED: &str = "muse_session_locked";

/// The kinds the user is told of, once the Host has stopped trying.
pub const NOTIFY: [&str; 10] = [
    CONNECTION,
    TRANSIENT,
    INTERRUPTED,
    USAGE_LIMIT,
    AUTH,
    ACCOUNT_PLAN,
    RUNNER_EXIT,
    TIMEOUT,
    NOT_STARTED,
    MUSE_SESSION_LOCKED,
];

/// What a Codex turn says when its failure reads like a usage limit and the provider did
/// not confirm that the quota is spent.
pub const QUOTA_UNCONFIRMED_MESSAGE: &str = "Codex could not complete this request. Try again";

// A usage window: the account's allowance is spent until its window resets. Nothing to do
// but wait, or use another account.
const USAGE_WINDOW: &str = r"out of usage|usage (limit|cap|quota|exhausted|exceeded|reached)|session limit|hit your .*limit.*resets";
// Everything else of the kind: credits, billing, payment, a suspended account, a monthly or
// hard limit, a trial, a model that refuses this client. No reset clears these.
const USAGE_OTHER: &str = concat!(
    r"out of credits?|workspace is out of credits?|",
    r"(quota|credits?|(?:usage|credit|account) balance) (exceeded|exhausted|depleted|reached|used up)|",
    r"insufficient (quota|credits?|credit balance)|",
    r"exceeded your current quota|monthly limit|billing hard limit|",
    r"monthly spending limit|monthly included credits|",
    r"free tier can only be used from within opencode|",
    r"payment required|status(?: code)? 402\b|\b402 payment\b|",
    r"resource[_ ]exhausted|no credits? remaining|trial quota|",
    r"credits? balance is too low",
);

fn pattern(source: &str) -> Regex {
    // The wordings are written here, not read from anywhere: one that does not compile is a
    // mistake in this file, which the tests of this module would not get past.
    #[allow(clippy::expect_used)]
    Regex::new(&format!("(?i){source}")).expect("a pattern of this module compiles")
}

static TIMEOUT_RE: LazyLock<Regex> = LazyLock::new(|| {
    pattern(r"turn timed out|watchdog|idle timeout|no output for|timeout waiting for response")
});
static INTERRUPTED_RE: LazyLock<Regex> = LazyLock::new(|| {
    pattern(concat!(
        r"turn[_ ]aborted|deliberately interrupted|user interrupted|",
        r"sigterm|sigint|killed by signal|aborted by user|interrupted the previous|",
        r"operation was aborted",
    ))
});
static CONNECTION_RE: LazyLock<Regex> = LazyLock::new(|| {
    pattern(concat!(
        r"socket connection was closed|socket hang ?up|connection closed|",
        r"econnreset|epipe|etimedout|enetunreach|econnrefused|",
        r"getaddrinfo|eai_again|enotfound|",
        r"fetch failed|network (error|timeout)|stream (disconnected|interrupted)|",
        r"connection error|connection reset|premature close|terminated unexpectedly",
    ))
});
static TRANSIENT_RE: LazyLock<Regex> = LazyLock::new(|| {
    pattern(concat!(
        r"overloaded|rate[ _]?limit|too many requests|\b429\b|",
        r"\b5\d{2}\b|internal server error|service unavailable|bad gateway|",
        r"gateway time-?out|api (error|timeout).*(retry|temporar)|temporarily unavailable|",
        r"upstream request failed|endpoint is unavailable",
    ))
});
static USAGE_LIMIT_RE: LazyLock<Regex> =
    LazyLock::new(|| pattern(&format!("{USAGE_WINDOW}|{USAGE_OTHER}")));
static USAGE_WINDOW_RE: LazyLock<Regex> = LazyLock::new(|| pattern(USAGE_WINDOW));
static USAGE_OTHER_RE: LazyLock<Regex> = LazyLock::new(|| pattern(USAGE_OTHER));
static STATES_ITS_RESET_RE: LazyLock<Regex> =
    LazyLock::new(|| pattern(r"\bresets\b|\breset (?:at|in|on)\b|\bwill reset\b"));
static NO_RESET_CLEARS_RE: LazyLock<Regex> =
    LazyLock::new(|| pattern(r"\bcredits?\b|payment|suspend|billing|\btrial\b|free tier"));
static AUTH_RE: LazyLock<Regex> = LazyLock::new(|| {
    pattern(concat!(
        r"failed to authenticate|oauth session expired|could not be refreshed|",
        r"not logged in|invalid_grant|token (has )?expired|",
        r"\b401\b.*unauthori[sz]ed|unauthori[sz]ed.*\b401\b|",
        r"please run /login|run `?claude login`?|",
        r"insufficient authentication scopes|log back in \(/login\)",
    ))
});
static ACCOUNT_PLAN_RE: LazyLock<Regex> = LazyLock::new(|| {
    pattern(concat!(
        r"model is not supported when using codex with a chatgpt account|",
        r"not (?:available|supported|included) (?:on|in|with|for) your (?:current )?plan|",
        r"your (?:current )?plan does not (?:include|support|have access)",
    ))
});
static RUNNER_EXIT_RE: LazyLock<Regex> = LazyLock::new(|| {
    pattern(concat!(
        r"\b(codex|clarp|agy|claude|agent|opencode)\s+exited\s+rc=\d+|",
        r"\b(exit status|exited with code)\s+\d+|",
        r"\bprocess exited\b.*\b(rc|code)=?\s*\d+",
    ))
});
static ACCOUNT_WAIT_RE: LazyLock<Regex> = LazyLock::new(|| {
    #[allow(clippy::expect_used)]
    Regex::new(concat!(
        r"\AWaiting for an? \S+ account (?:with available usage|",
        r"that serves .+?; the active one does not right now)\z",
    ))
    .expect("a pattern of this module compiles")
});
static PROVIDER_MESSAGE_RE: LazyLock<Regex> = LazyLock::new(|| {
    #[allow(clippy::expect_used)]
    Regex::new(r#""message"\s*:\s*"((?:[^"\\]|\\.)*)""#).expect("a pattern of this module compiles")
});
static OPENCODE_FREE_RE: LazyLock<Regex> =
    LazyLock::new(|| pattern(r"free tier can only be used from within opencode"));
static PROVIDER_DOWN_RE: LazyLock<Regex> =
    LazyLock::new(|| pattern(r"upstream request failed|endpoint is unavailable"));

/// The kind of a failure, from what a tool said of it.
pub fn classify_error(message: &str) -> &'static str {
    let text = message.trim();
    if text.is_empty() {
        return UNKNOWN;
    }
    if text.starts_with("muse_session_locked:") {
        return MUSE_SESSION_LOCKED;
    }
    // The order is the reading. A watchdog's kill is a signal too, and is a timeout, not a
    // stop. A turn that was stopped often also prints a broken pipe as it goes, and must
    // not be tried again. A usage limit that advertises a plan is a usage limit.
    let order: [(&LazyLock<Regex>, &str); 8] = [
        (&TIMEOUT_RE, TIMEOUT),
        (&INTERRUPTED_RE, INTERRUPTED),
        (&USAGE_LIMIT_RE, USAGE_LIMIT),
        (&ACCOUNT_PLAN_RE, ACCOUNT_PLAN),
        (&AUTH_RE, AUTH),
        (&CONNECTION_RE, CONNECTION),
        (&TRANSIENT_RE, TRANSIENT),
        (&RUNNER_EXIT_RE, RUNNER_EXIT),
    ];
    order
        .iter()
        .find(|(wording, _)| wording.is_match(text))
        .map_or(UNKNOWN, |(_, kind)| *kind)
}

/// Whether a failure is a usage window that clears at its reset: something to wait for.
/// True only for a text that reads as a spent window and as nothing else of the kind: one
/// that also names credits, billing or payment is not known to clear by waiting. A text
/// of the kind that states its own reset is a window, unless it names something no reset
/// clears.
pub fn usage_limit_resets(message: &str) -> bool {
    if STATES_ITS_RESET_RE.is_match(message) && USAGE_LIMIT_RE.is_match(message) {
        return !NO_RESET_CLEARS_RE.is_match(message);
    }
    USAGE_WINDOW_RE.is_match(message) && !USAGE_OTHER_RE.is_match(message)
}

/// A plain reason for a refusal whose own words do not tell the user what to do. Empty
/// when the words of its kind will do.
pub fn explain(message: &str) -> &'static str {
    if OPENCODE_FREE_RE.is_match(message) {
        "This free OpenCode model only runs in OpenCode's own apps. Choose another model for this agent"
    } else if PROVIDER_DOWN_RE.is_match(message) {
        "This model's provider is unavailable right now. Try again later or choose another model"
    } else {
        ""
    }
}

/// A failure as a person should read it: a provider's JSON body, which a tool appends to
/// its own words, gives way to the message inside it. Anything else comes back as it was,
/// without what is around it.
pub fn provider_message(message: &str) -> String {
    let text = message.trim();
    let Some(start) = text.find('{') else {
        return text.to_string();
    };
    let Some(last) = PROVIDER_MESSAGE_RE
        .captures_iter(&text[start..])
        .last()
        .and_then(|found| found.get(1))
    else {
        return text.to_string();
    };
    let Ok(inner) = serde_json::from_str::<String>(&format!("\"{}\"", last.as_str())) else {
        return text.to_string();
    };
    let prefix = text[..start].trim();
    if prefix.is_empty() {
        inner
    } else {
        format!("{prefix} {inner}").trim().to_string()
    }
}

/// Whether a value counts as said, the way the Python Host's tools count it: nothing,
/// zero, an empty text and an empty list do not.
fn says(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(truth) => *truth,
        Value::Number(number) => number.as_f64().is_some_and(|n| n != 0.0),
        Value::String(text) => !text.is_empty(),
        Value::Array(items) => !items.is_empty(),
        Value::Object(fields) => !fields.is_empty(),
    }
}

/// The kind of a turn's end, from the tool's own last word on it (Claude's `result`).
/// A result that says it failed and gives no words anyone knows is told as `transient`:
/// better told than silent.
pub fn classify_result(event: &Value) -> &'static str {
    let Some(fields) = event.as_object() else {
        return CLEAN;
    };
    let written = |key: &str| match fields.get(key) {
        Some(Value::String(text)) => text.clone(),
        Some(value) if says(value) => value.to_string(),
        _ => String::new(),
    };
    let subtype = written("subtype");
    let failed =
        fields.get("is_error").is_some_and(says) || subtype.to_lowercase().contains("error");
    if !failed {
        return CLEAN;
    }
    let text = ["result", "error", "message", "subtype"]
        .map(written)
        .join(" ");
    match classify_error(&text) {
        UNKNOWN => TRANSIENT,
        kind => kind,
    }
}

/// What a turn shows while it waits for an account. An empty model: no account has usage.
/// A model, or none named: the active account does not serve it right now, while the rest
/// of the pool works.
pub fn account_wait_message(backend: &str, model: Option<&str>) -> String {
    match model {
        Some("") => format!("Waiting for a {backend} account with available usage"),
        model => format!(
            "Waiting for a {backend} account that serves {}; the active one does not right now",
            model.unwrap_or("this model")
        ),
    }
}

/// Whether a failure's text is, whole, a turn's wait for an account.
pub fn is_account_wait_message(text: &str) -> bool {
    ACCOUNT_WAIT_RE.is_match(text)
}

/// What the user is shown of a turn that ended on a failure: in the agent's state, and as a
/// row under the message that caused it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Told {
    /// The sentence: what explains the tool's words when something does, else the kind's own.
    pub message: String,
    /// What the tool said, cut to 200 characters.
    pub error: String,
    /// The provider's own words out of it, cut to 500.
    pub provider_message: String,
    /// The sentence, and that the message is kept.
    pub summary: String,
    /// The row in the conversation: the sentence, and below it the provider's words when the
    /// sentence does not hold them already.
    pub row: String,
}

/// What only some callers know of a failure.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct Known {
    /// Whether the provider confirmed that the quota is spent. `Some(false)`: it was asked
    /// and did not.
    pub quota_confirmed: Option<bool>,
    /// The account was switched already, and the switch waits for the tool's other agents.
    pub switch_pending: bool,
}

/// The sentence of a kind of failure whose words explain nothing by themselves.
fn sentence(kind: &str, model: &str) -> String {
    match kind {
        CONNECTION => "Connection lost — retries exhausted".to_string(),
        TRANSIENT => "API unavailable (overloaded / rate limited)".to_string(),
        INTERRUPTED => "Turn interrupted".to_string(),
        USAGE_LIMIT => "Usage limit reached".to_string(),
        AUTH => "Sign-in expired — could not refresh the account".to_string(),
        RUNNER_EXIT => "Agent process exited unexpectedly".to_string(),
        TIMEOUT => "Turn timed out — backend stopped responding".to_string(),
        NOT_STARTED => "The turn could not be started".to_string(),
        ACCOUNT_PLAN => format!(
            "This account's plan does not include {}. Switch to an account that has it or \
             choose another model",
            if model.is_empty() {
                "this model"
            } else {
                model
            }
        ),
        _ => "Turn failed".to_string(),
    }
}

/// What the user is told of a turn of this model that ended on a failure of this kind, with
/// these words of the tool's. The Python Host's wording (`turn_retry._mark_interrupted` and
/// `_record_failure_row`), held to it by `tests/failure_vectors.rs`.
pub fn told(kind: &str, said: &str, model: &str, known: Known) -> Told {
    let explained = explain(said);
    let mut message = if explained.is_empty() {
        sentence(kind, model)
    } else {
        explained.to_string()
    };
    if kind == USAGE_LIMIT && known.quota_confirmed == Some(false) {
        message = QUOTA_UNCONFIRMED_MESSAGE.to_string();
    }
    if known.switch_pending {
        message.push_str(
            " — the account switch waits until no Codex agent is busy; send again once they finish",
        );
    }
    let provider = provider_message(said);
    let mut row = format!("{message}.");
    if !provider.is_empty() && !message.contains(provider.as_str()) {
        row.push_str("\n\n");
        row.extend(provider.chars().take(1000));
    }
    Told {
        summary: format!("{message}. Your message is saved."),
        error: said.chars().take(200).collect(),
        provider_message: provider.chars().take(500).collect(),
        message,
        row,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_order_of_the_wordings_is_the_reading() {
        // A watchdog kills with a signal: a timeout, not a stop.
        assert_eq!(
            classify_error("watchdog killed the turn (SIGTERM)"),
            TIMEOUT
        );
        // A stopped turn prints a broken pipe as it goes: a stop, never tried again.
        assert_eq!(
            classify_error("SIGTERM: socket connection was closed"),
            INTERRUPTED
        );
        // A limit that advertises a plan is a limit.
        assert_eq!(
            classify_error("usage limit reached; upgrade your plan"),
            USAGE_LIMIT
        );
        assert_eq!(classify_error("  "), UNKNOWN);
        assert_eq!(classify_error("nothing anyone has a word for"), UNKNOWN);
    }

    #[test]
    fn a_window_that_resets_is_waited_for_and_a_bill_is_not() {
        assert!(usage_limit_resets("Claude usage limit reached"));
        assert!(usage_limit_resets(
            "Subscription quota exhausted. Your usage window resets at 14:00"
        ));
        assert!(!usage_limit_resets(
            "Subscription quota exhausted. Your credits reset at 14:00"
        ));
        assert!(!usage_limit_resets("You exceeded your current quota"));
        assert!(!usage_limit_resets("ECONNRESET"));
    }

    #[test]
    fn a_turn_that_waits_for_an_account_says_so_in_two_forms() {
        let usage = account_wait_message("claude", Some(""));
        assert_eq!(usage, "Waiting for a claude account with available usage");
        let model = account_wait_message("claude", Some("claude-opus-5-5"));
        assert!(is_account_wait_message(&usage) && is_account_wait_message(&model));
        assert!(is_account_wait_message(&account_wait_message("agy", None)));
        assert!(!is_account_wait_message(&format!("{usage}.")));
    }
}
