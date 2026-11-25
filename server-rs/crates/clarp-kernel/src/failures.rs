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
/// The conversation no longer fits the model: asked again, it fits no better, and no other
/// account has a longer one. A kind of this Host's own: the Python Host reads it as the tool's
/// exit.
pub const PROMPT_TOO_LONG: &str = "prompt_too_long";
/// The tool's process ended badly, without a word that says why.
pub const RUNNER_EXIT: &str = "runner_exit";
pub const TIMEOUT: &str = "timeout";
/// The Host ended the turn before a process ran for it. Never read off a text: the Host
/// names it itself.
pub const NOT_STARTED: &str = "not_started";
pub const UNKNOWN: &str = "unknown";
pub const MUSE_SESSION_LOCKED: &str = "muse_session_locked";
/// The tool would have run, or ran, on another model provider than the one its agent is
/// bound to (an agent with a provider of its own, `codex_provider`). Never tried again: the
/// agent's configuration has to be put right first. The Host names it itself, by the words
/// [`provider_mismatch`] begins with; a tool's own words never read as it.
pub const PROVIDER_MISMATCH: &str = "provider_mismatch";

/// Something outside the Host ended the turn's process with a signal (an operator's `kill`, a
/// service manager): not the tool's failure, nor a stop the Host was asked for. Never tried
/// again, and not counted as the tool's failure. The Host names it itself, by the words
/// [`ended_from_outside`] begins with (issue 829).
pub const ENDED_FROM_OUTSIDE: &str = "ended_from_outside";

/// What the words of a turn ended from outside begin with.
const ENDED_FROM_OUTSIDE_MARK: &str = "ended_from_outside:";

/// The words a turn ends with whose process a signal from outside the Host ended: the mark
/// of the kind, then the sentence with the signal.
pub fn ended_from_outside(signal: &str) -> String {
    format!(
        "{ENDED_FROM_OUTSIDE_MARK} The turn's process was ended from outside ({})",
        signal.trim()
    )
}

/// Whether a failure of this kind says nothing more than that the process went: what a tool
/// writes as a signal ends it (an error with no words, a bad exit, an interruption, a pipe or
/// stream that closed under it, a wait that ran out). Only such a failure is told as ended
/// from outside when a signal from outside came first: a kill is not a network to try again.
/// One that is about the account (a usage limit, a sign-in) keeps its kind, so the account
/// switch still happens.
pub fn yields_to_outside(kind: &str) -> bool {
    matches!(
        kind,
        UNKNOWN | RUNNER_EXIT | INTERRUPTED | CLEAN | CONNECTION | TIMEOUT
    )
}

/// The sentence of a turn ended from outside, without its mark. `None` for any other words.
pub fn outside_sentence(said: &str) -> Option<&str> {
    said.trim()
        .strip_prefix(ENDED_FROM_OUTSIDE_MARK)
        .map(str::trim)
}

/// The agent's working folder is not there, or is no folder (a worktree removed after its
/// branch landed): nothing can be started in it. Never tried again: no try brings the folder
/// back. The Host names it itself, by the words [`clarp_spool::folder_gone`] makes; a tool's
/// own words never read as it.
pub const FOLDER_MISSING: &str = "folder_missing";

/// What follows the mark in words of a missing folder: the folder, said as no folder where it
/// is one. `None` for any other words.
pub fn missing_folder(said: &str) -> Option<&str> {
    said.trim()
        .strip_prefix(clarp_spool::FOLDER_MISSING_MARK)
        .map(str::trim)
}

/// What the user is told of a turn that could not start because its agent's folder is gone:
/// which folder, and what to do. `missing`: what [`missing_folder`] found.
pub fn folder_missing_sentence(missing: &str) -> String {
    match missing.strip_prefix(clarp_spool::NOT_A_FOLDER) {
        Some(dir) => format!(
            "This agent's folder {} is not a folder. Recreate it as a folder, or make a new \
             agent in another folder",
            dir.trim()
        ),
        None => format!(
            "This agent's folder {missing} no longer exists. Recreate it, or make a new agent \
             in another folder"
        ),
    }
}

/// Whether a turn that failed of this kind never had its tool started.
pub fn never_started(kind: &str) -> bool {
    kind == NOT_STARTED || kind == FOLDER_MISSING
}

/// What the words of a provider mismatch begin with, so that wherever they are read they are
/// read as that kind.
const PROVIDER_MISMATCH_MARK: &str = "provider_mismatch:";

/// The words a turn ends with whose tool was refused, or stopped, for running on another
/// provider than its agent's: the mark of the kind, then why in plain words.
pub fn provider_mismatch(why: &str) -> String {
    format!("{PROVIDER_MISMATCH_MARK} {}", why.trim())
}

/// The kinds the user is told of, once the Host has stopped trying.
pub const NOTIFY: [&str; 14] = [
    CONNECTION,
    TRANSIENT,
    INTERRUPTED,
    USAGE_LIMIT,
    AUTH,
    ACCOUNT_PLAN,
    PROMPT_TOO_LONG,
    RUNNER_EXIT,
    TIMEOUT,
    NOT_STARTED,
    MUSE_SESSION_LOCKED,
    PROVIDER_MISMATCH,
    FOLDER_MISSING,
    ENDED_FROM_OUTSIDE,
];

/// The kinds this Host reads that the Python Host does not (`docs/differences.md`).
pub const OWN_KINDS: [&str; 4] = [
    PROMPT_TOO_LONG,
    PROVIDER_MISMATCH,
    FOLDER_MISSING,
    ENDED_FROM_OUTSIDE,
];

/// The words a turn ends with whose tool said the conversation is too long for its model:
/// the tool's own, after the Host's where they do not say it themselves, so that they are
/// read as that kind whatever the tool's wording.
pub fn prompt_too_long(said: &str) -> String {
    let said = said.trim();
    if PROMPT_TOO_LONG_RE.is_match(said) {
        said.to_string()
    } else if said.is_empty() {
        PROMPT_TOO_LONG_SAID.to_string()
    } else {
        format!("{PROMPT_TOO_LONG_SAID}: {said}")
    }
}

/// How the Host says it, where the tool's words do not.
const PROMPT_TOO_LONG_SAID: &str = "Prompt is too long";

/// What a turn shows while it waits for the machine's network to come back.
pub const NETWORK_WAIT: &str = "Waiting for the network";

/// How the Host says, before the tool's own words, that a turn ended because the machine had
/// no network for as long as a turn waits for it (`recovery::NETWORK_WAIT_MOST_MS`).
const NETWORK_GONE_SAID: &str = "No network for 15 minutes";

/// The words a turn ends with that waited for the network in vain: the Host's, then the
/// tool's, so that both the sentence and what the tool said are kept.
pub fn network_gone(said: &str) -> String {
    let said = said.trim();
    if said.starts_with(NETWORK_GONE_SAID) {
        said.to_string()
    } else if said.is_empty() {
        NETWORK_GONE_SAID.to_string()
    } else {
        format!("{NETWORK_GONE_SAID}: {said}")
    }
}

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
        r"temporarily limiting|",
        r"upstream request failed|endpoint is unavailable",
    ))
});
static USAGE_LIMIT_RE: LazyLock<Regex> =
    LazyLock::new(|| pattern(&format!("{USAGE_WINDOW}|{USAGE_OTHER}")));
// A usage limit named only to be denied: Claude Code 2.1.321 says "(not your usage limit)"
// when its provider's servers hold requests back for a moment.
static DENIED_USAGE_LIMIT_RE: LazyLock<Regex> =
    LazyLock::new(|| pattern(r"\bnot (?:your |an? |the )?usage (?:limit|cap|quota)"));
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
// Claude Code 2.1.321: "Prompt is too long · the request is ~1050000 tokens (limit 1000000)
// ...", with `terminal_reason` `prompt_too_long` on its result.
static PROMPT_TOO_LONG_RE: LazyLock<Regex> = LazyLock::new(|| pattern(r"\bprompt is too long\b"));
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
// A provider that turned a request away for a moment, in its own words or by its status.
// Narrower than the transient kind, which also takes any number of the 500s in a text.
static PROVIDER_BUSY_RE: LazyLock<Regex> = LazyLock::new(|| {
    pattern(concat!(
        r"overloaded|rate[ _]?limit|too many requests|\b429\b|\b529\b|",
        r"temporarily (?:limiting|unavailable)|service unavailable|bad gateway|",
        r"gateway time-?out|internal server error|api error:? 5\d\d\b",
    ))
});

/// What a tool said, without what it says a failure is not: a denial is no reading of it.
fn affirmed(message: &str) -> std::borrow::Cow<'_, str> {
    DENIED_USAGE_LIMIT_RE.replace_all(message, " ")
}

/// Whether a failure's words say that its provider turned the request away for a moment:
/// overloaded, holding requests back, or answering with a status of its own that passes.
/// Only such a transient failure is worth waiting for and trying again.
pub fn provider_busy(message: &str) -> bool {
    PROVIDER_BUSY_RE.is_match(message)
}

/// The kind of a failure, from what a tool said of it.
pub fn classify_error(message: &str) -> &'static str {
    let text = message.trim();
    if text.is_empty() {
        return UNKNOWN;
    }
    if text.starts_with("muse_session_locked:") {
        return MUSE_SESSION_LOCKED;
    }
    if text.starts_with(PROVIDER_MISMATCH_MARK) {
        return PROVIDER_MISMATCH;
    }
    if text.starts_with(clarp_spool::FOLDER_MISSING_MARK) {
        return FOLDER_MISSING;
    }
    if text.starts_with(ENDED_FROM_OUTSIDE_MARK) {
        return ENDED_FROM_OUTSIDE;
    }
    let text = affirmed(text);
    // The order is the reading. A watchdog's kill is a signal too, and is a timeout, not a
    // stop. A turn that was stopped often also prints a broken pipe as it goes, and must
    // not be tried again. A prompt that is too long counts its tokens, and a count can read
    // as a server's error (`1,529,000`); the tool exits badly on it as well. A usage limit
    // that advertises a plan is a usage limit.
    let order: [(&LazyLock<Regex>, &str); 9] = [
        (&TIMEOUT_RE, TIMEOUT),
        (&INTERRUPTED_RE, INTERRUPTED),
        (&PROMPT_TOO_LONG_RE, PROMPT_TOO_LONG),
        (&USAGE_LIMIT_RE, USAGE_LIMIT),
        (&ACCOUNT_PLAN_RE, ACCOUNT_PLAN),
        (&AUTH_RE, AUTH),
        (&CONNECTION_RE, CONNECTION),
        (&TRANSIENT_RE, TRANSIENT),
        (&RUNNER_EXIT_RE, RUNNER_EXIT),
    ];
    order
        .iter()
        .find(|(wording, _)| wording.is_match(&text))
        .map_or(UNKNOWN, |(_, kind)| *kind)
}

/// Whether a failure is a usage window that clears at its reset: something to wait for.
/// True only for a text that reads as a spent window and as nothing else of the kind: one
/// that also names credits, billing or payment is not known to clear by waiting. A text
/// of the kind that states its own reset is a window, unless it names something no reset
/// clears.
pub fn usage_limit_resets(message: &str) -> bool {
    let message = affirmed(message);
    let message = message.as_ref();
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
    } else if message.trim_start().starts_with(NETWORK_GONE_SAID) {
        "This computer had no network for 15 minutes, so the turn stopped. Send it again once \
         it is online"
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
    /// When the account that ran out has usage again, where the tool said so.
    pub resets: Option<Resets>,
}

/// When an account that ran out has usage again, and when that was said: what the time is
/// told against. A time on the day it was said is told as a time of day, and one on another
/// day with its day.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Resets {
    pub at_ms: i64,
    pub said_at_ms: i64,
}

/// The text, and after it when the account has usage again, in the Host's own time: "Usage
/// limit reached · resets 18:27". The text alone where no reset is known.
pub fn with_reset(text: &str, resets: Option<Resets>) -> String {
    // The zone is looked up only for a reset: this is asked for every agent's state.
    match resets.and_then(|resets| reset_time_in(resets, &jiff::tz::TimeZone::system())) {
        Some(time) => format!("{text} · resets {time}"),
        None => text.to_string(),
    }
}

/// The time an account has usage again, in this zone: "18:27" on the day it was said,
/// "Wed 18:27" within the week after, "30 Nov 18:27" beyond, and "2 Jan 2027 18:27" in
/// another year. `None` for a time that is no time.
pub fn reset_time_in(resets: Resets, zone: &jiff::tz::TimeZone) -> Option<String> {
    let zoned = |ms: i64| {
        jiff::Timestamp::from_millisecond(ms)
            .ok()
            .map(|at| at.to_zoned(zone.clone()))
    };
    let (at, said) = (zoned(resets.at_ms)?, zoned(resets.said_at_ms)?);
    let clock = at.strftime("%H:%M").to_string();
    let ahead = said.date().until(at.date()).ok()?.get_days();
    Some(match ahead {
        0 => clock,
        1..=6 => format!("{} {clock}", at.strftime("%a")),
        _ if at.year() == said.year() => format!("{} {} {clock}", at.day(), at.strftime("%b")),
        _ => format!("{} {} {} {clock}", at.day(), at.strftime("%b"), at.year()),
    })
}

/// The sentence of a kind of failure whose words explain nothing by themselves.
fn sentence(kind: &str, model: &str) -> String {
    match kind {
        CONNECTION => "Connection lost — retries exhausted".to_string(),
        TRANSIENT => "API unavailable (overloaded / rate limited)".to_string(),
        INTERRUPTED => "Turn interrupted".to_string(),
        crate::agent_moves::MOVED => "The agent moved to another Host".to_string(),
        USAGE_LIMIT => "Usage limit reached".to_string(),
        AUTH => "Sign-in expired — could not refresh the account".to_string(),
        RUNNER_EXIT => "Agent process exited unexpectedly".to_string(),
        TIMEOUT => "Turn timed out — backend stopped responding".to_string(),
        NOT_STARTED => "The turn could not be started".to_string(),
        FOLDER_MISSING => "The turn could not start: the agent's folder no longer exists. \
             Recreate it, or make a new agent in another folder"
            .to_string(),
        PROVIDER_MISMATCH => "This agent's turn was stopped: it ran, or would have run, on \
             another model provider than the one it is bound to. Check its Codex home and provider"
            .to_string(),
        PROMPT_TOO_LONG => "This conversation is too long for the model. Compact it or start \
             a new one"
            .to_string(),
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
    // A missing folder is said in the Host's own sentence, which names the folder: there are
    // no words of a tool's to add.
    if let Some(dir) = missing_folder(said) {
        let message = folder_missing_sentence(dir);
        return Told {
            summary: format!("{message}. Your message is saved."),
            error: said.chars().take(200).collect(),
            provider_message: String::new(),
            row: format!("{message}."),
            message,
        };
    }
    // So is a turn ended from outside: the Host's sentence names the signal, and the tool's
    // words, written as it was ended, say nothing of it.
    if let Some(sentence) = outside_sentence(said) {
        let message = sentence.to_string();
        return Told {
            summary: format!("{message}. Your message is saved."),
            error: said.chars().take(200).collect(),
            provider_message: String::new(),
            row: format!("{message}."),
            message,
        };
    }
    let explained = explain(said);
    let mut message = if explained.is_empty() {
        sentence(kind, model)
    } else {
        explained.to_string()
    };
    if kind == USAGE_LIMIT && known.quota_confirmed == Some(false) {
        message = QUOTA_UNCONFIRMED_MESSAGE.to_string();
    } else if kind == USAGE_LIMIT && explained.is_empty() {
        message = with_reset(&message, known.resets);
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

// ---- what is kept of a failed tool's own words ---------------------------------------------

/// A CLI's own words on failure are what an operator debugs with ("rate limit reached",
/// "unknown model ..."), but a CLI that repeats its input puts the request into them. They are
/// kept only when they share no run of this many characters with the prompt (the whole prompt
/// when it is shorter), and as one line of at most `KEPT_MOST` characters. The Python Host's
/// `model_fallbacks` since its PR 445 (R85), held by its own answers
/// (`tests/routing_failure_vectors.rs`).
pub const ECHO_RUN: usize = 24;
pub const KEPT_MOST: usize = 300;

/// The categories a fallback chain moves on from (`model_fallbacks.PROVIDER_FAILURES`).
pub const PROVIDER_FAILURES: [&str; 5] = [CONNECTION, TRANSIENT, USAGE_LIMIT, RUNNER_EXIT, TIMEOUT];

/// A refusal is an answer, not an outage: retrying it elsewhere would launder a decision the
/// user or the sandbox already made.
static DENIED: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(
        r"(?i)permission denied|approval required|not authorized|ownership lost|configuration changed",
    )
    .unwrap_or_else(|_| unreachable!("a fixed pattern"))
});
static ESCAPE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r#"\\(u[0-9a-fA-F]{4}|U[0-9a-fA-F]{8}|x[0-9a-fA-F]{2}|["\\/bfnrt'])"#)
        .unwrap_or_else(|_| unreachable!("a fixed pattern"))
});
static BYTE_RUN: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(?:\\x[0-9a-fA-F]{2})+").unwrap_or_else(|_| unreachable!("a fixed pattern"))
});

/// What stands for half of a pair of signs that an escape wrote out alone (`\ud83d`): the
/// reference keeps it as such, and no prompt has one, so it is no part of any run in common.
const HALF_A_PAIR: char = '\u{FFFF}';

/// Whether a sign is one the reference's `[\s\x00-\x1f\x7f]` takes.
fn spacing(sign: char) -> bool {
    crate::python::blank(sign) || sign <= '\u{1f}' || sign == '\u{7f}'
}

/// One line: control characters and runs of white space become one space (`_plain`).
pub fn plain(text: &str) -> String {
    let mut line = String::with_capacity(text.len());
    let mut spaced = false;
    for sign in text.chars() {
        if spacing(sign) {
            spaced = true;
            continue;
        }
        if spaced && !line.is_empty() {
            line.push(' ');
        }
        spaced = false;
        line.push(sign);
    }
    line
}

/// UTF-8 bytes read as the reference's `decode("utf-8", errors="backslashreplace")`: a byte
/// that is no part of a sign is written `\xNN`.
fn utf8_or_escaped(mut bytes: &[u8]) -> String {
    let mut said = String::new();
    loop {
        match std::str::from_utf8(bytes) {
            Ok(rest) => {
                said.push_str(rest);
                return said;
            }
            Err(error) => {
                let (good, bad) = bytes.split_at(error.valid_up_to());
                said.push_str(std::str::from_utf8(good).unwrap_or_default());
                let wrong = error.error_len().unwrap_or(bad.len());
                for byte in &bad[..wrong] {
                    said.push_str(&format!("\\x{byte:02x}"));
                }
                bytes = &bad[wrong..];
            }
        }
    }
}

/// A text with JSON and Python string escapes undone (`_unescaped`). `utf8_bytes`: read as a
/// bytes repr, where each run of `\xNN` is UTF-8 bytes; else each `\xNN` is the code point NN.
pub fn unescaped(text: &str, utf8_bytes: bool) -> String {
    let text = if utf8_bytes {
        BYTE_RUN
            .replace_all(text, |run: &regex::Captures<'_>| {
                let bytes: Vec<u8> = run[0]
                    .split("\\x")
                    .skip(1)
                    .filter_map(|pair| u8::from_str_radix(pair, 16).ok())
                    .collect();
                utf8_or_escaped(&bytes)
            })
            .into_owned()
    } else {
        text.to_string()
    };
    ESCAPE
        .replace_all(&text, |found: &regex::Captures<'_>| {
            let code = &found[1];
            match code.as_bytes()[0] {
                b'u' | b'U' | b'x' => match u32::from_str_radix(&code[1..], 16) {
                    // Above U+10FFFF no sign exists: the escape stays as written.
                    Ok(value) if value <= 0x10_FFFF => {
                        char::from_u32(value).unwrap_or(HALF_A_PAIR).to_string()
                    }
                    _ => found[0].to_string(),
                },
                b'b' => "\u{8}".to_string(),
                b'f' => "\u{c}".to_string(),
                b'n' => "\n".to_string(),
                b'r' => "\r".to_string(),
                b't' => "\t".to_string(),
                _ => code.to_string(),
            }
        })
        .into_owned()
}

/// The code points the reference's Unicode (15.1.0) has not assigned: its NFKC leaves them as
/// written, where this Host's tables (a later Unicode) may fold some (the generator's
/// `unassigned-15.1.json`).
static UNASSIGNED: LazyLock<Vec<(u32, u32)>> = LazyLock::new(|| {
    let table: Value =
        serde_json::from_str(include_str!("unassigned-15.1.json")).unwrap_or(Value::Null);
    let ranges = table["unassigned"].as_array().into_iter().flatten();
    ranges
        .filter_map(|range| Some((range[0].as_u64()? as u32, range[1].as_u64()? as u32)))
        .collect()
});

/// Whether the reference's Unicode has not assigned this sign.
pub fn unassigned_in_reference(sign: char) -> bool {
    let at = sign as u32;
    let ranges = &*UNASSIGNED;
    let index = ranges.partition_point(|(start, _)| *start <= at);
    index > 0 && at <= ranges[index - 1].1
}

/// NFKC as the reference has it: what its Unicode has not assigned is left as written, and
/// the rest normalised around it.
pub fn nfkc(text: &str) -> String {
    use unicode_normalization::UnicodeNormalization;
    let mut said = String::with_capacity(text.len());
    let mut run = String::new();
    for sign in text.chars() {
        if unassigned_in_reference(sign) {
            said.extend(run.nfkc());
            run.clear();
            said.push(sign);
        } else {
            run.push(sign);
        }
    }
    said.extend(run.nfkc());
    said
}

/// Whether `text` shares a run of `ECHO_RUN` characters (the whole prompt when it is shorter)
/// with `prompt`, read as written or with its string escapes undone; white space compared as
/// one space, both sides NFKC-normalised (`echoes`).
pub fn echoes(text: &str, prompt: &str) -> bool {
    let request = python_strip(&plain(&nfkc(prompt)));
    if request.is_empty() {
        return false;
    }
    let run = ECHO_RUN.min(request.chars().count());
    let forms = [
        python_strip(&plain(&nfkc(text))),
        python_strip(&plain(&nfkc(&unescaped(text, false)))),
        python_strip(&plain(&nfkc(&unescaped(text, true)))),
    ];
    forms.iter().any(|form| {
        let starts: Vec<usize> = form
            .char_indices()
            .map(|(at, _)| at)
            .chain(std::iter::once(form.len()))
            .collect();
        starts
            .windows(run + 1)
            .any(|window| request.contains(&form[window[0]..window[run]]))
    })
}

fn python_strip(text: &str) -> String {
    crate::python::strip(text).to_string()
}

/// The CLI's own words as one line of at most `KEPT_MOST` characters, or nothing when they
/// hold any of the request (`_kept_line`).
pub fn kept_line(words: &str, prompt: &str) -> String {
    let line: String = python_strip(&plain(words))
        .chars()
        .take(KEPT_MOST)
        .collect();
    if line.is_empty() || echoes(&line, prompt) {
        String::new()
    } else {
        line
    }
}

/// What a failed one-off CLI says: its facts, read from its raw words and its exit, and its
/// line for a person. Nothing reads a category, a refusal or a retry out of the line again.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CliFailure {
    /// The words kept, or "<backend> exited with status N (<category>)".
    pub line: String,
    pub category: &'static str,
    /// A refusal: never moved on from, whatever line is kept.
    pub denied: bool,
    pub backend: String,
    /// The exit status, as the reference has it (a signal is its negative); none where the
    /// tool never told one.
    pub status: Option<i64>,
    /// Whether a fallback chain moves on from it (`_as_provider_failure` is not None).
    pub moves_on: bool,
}

/// What a routing CLI's failure says, wherever it is caught (`cli_failure`): the category and
/// the refusal from the raw words first, the line worked out after.
pub fn cli_failure(backend: &str, output: &str, prompt: &str, status: Option<i64>) -> CliFailure {
    let words = crate::python::strip(output);
    let mut category = classify_error(words);
    if category != INTERRUPTED && !PROVIDER_FAILURES.contains(&category) {
        category = RUNNER_EXIT;
    }
    let denied = DENIED.is_match(words);
    let line = match kept_line(words, prompt) {
        kept if !kept.is_empty() => kept,
        _ => match status {
            Some(status) => format!("{backend} exited with status {status} ({category})"),
            None => format!("{backend} failed ({category})"),
        },
    };
    // The reference promotes the failure again from its line (`provider_error`): an
    // interruption or a refusal written there holds it back too. Read here, once.
    let moves_on = category != INTERRUPTED
        && !denied
        && !DENIED.is_match(&line)
        && classify_error(&line) != INTERRUPTED;
    CliFailure {
        line,
        category,
        denied,
        backend: backend.to_string(),
        status,
        moves_on,
    }
}

/// The Goal keeper's code for a failure it was given, where the facts name one
/// (`goal_keeper.error_code`): `timeout`, `<backend>_exit_<N>`, or `agy_exit_<N>` from the
/// fixed prefix of Antigravity's own error. `None`: the caller's own code.
pub fn keeper_code(failure: &CliFailure) -> Option<String> {
    static AGY: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(r"agy exited (-?\d+)").unwrap_or_else(|_| unreachable!("a fixed pattern"))
    });
    if failure.category == TIMEOUT {
        return Some(TIMEOUT.to_string());
    }
    if let (false, Some(status)) = (failure.backend.is_empty(), failure.status) {
        return Some(format!("{}_exit_{status}", failure.backend));
    }
    AGY.captures(&failure.line)
        .map(|status| format!("agy_exit_{}", &status[1]))
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;

    #[test]
    fn a_turn_that_waited_for_the_network_in_vain_says_the_network_was_gone() {
        let said = "API Error: Can't reach the API server (EAI_AGAIN)";
        assert_eq!(classify_error(said), CONNECTION);
        let gone = network_gone(said);
        assert_eq!(
            gone,
            "No network for 15 minutes: API Error: Can't reach the API server (EAI_AGAIN)"
        );
        // Still read as the connection it was, and said once however often it is given.
        assert_eq!(classify_error(&gone), CONNECTION);
        assert_eq!(network_gone(&gone), gone);
        assert_eq!(network_gone(""), "No network for 15 minutes");
        let told = told(CONNECTION, &gone, "", Known::default());
        assert_eq!(
            told.message,
            "This computer had no network for 15 minutes, so the turn stopped. Send it again \
             once it is online"
        );
        assert!(told.row.contains("EAI_AGAIN"), "{}", told.row);
        assert!(told.summary.ends_with("Your message is saved."));
        // A connection lost while the network was there says what it always said.
        assert_eq!(
            super::told(CONNECTION, said, "", Known::default()).message,
            "Connection lost — retries exhausted"
        );
    }

    #[test]
    fn a_prompt_too_long_for_the_model_is_read_before_what_its_count_looks_like() {
        // Claude Code 2.1.321's words, and the same with a count that reads as a server error,
        // after the tool's bad exit.
        for said in [
            "Prompt is too long · the request is ~1050000 tokens (limit 1000000)",
            "Prompt is too long · the request is ~1,529,000 tokens (limit 1,000,000)\nclaude exited with code 1",
        ] {
            assert_eq!(classify_error(said), PROMPT_TOO_LONG, "{said}");
            assert_eq!(prompt_too_long(said), said);
        }
        assert_eq!(prompt_too_long(""), "Prompt is too long");
        assert_eq!(
            prompt_too_long(" Request too large "),
            "Prompt is too long: Request too large"
        );
        // The other words of a long context are not this Host's to read differently.
        assert_eq!(
            classify_error(
                r#"{"error":{"type":"invalid_request_error","message":"context_length_exceeded"}}"#
            ),
            UNKNOWN
        );
        let told = told(PROMPT_TOO_LONG, "Prompt is too long", "m", Known::default());
        assert_eq!(
            told.summary,
            "This conversation is too long for the model. Compact it or start a new one. Your \
             message is saved."
        );
    }

    #[test]
    fn a_missing_folder_is_read_by_its_mark_and_told_in_a_sentence_that_names_it() {
        let said = clarp_spool::folder_missing("/work/helper", false);
        assert_eq!(classify_error(&said), FOLDER_MISSING);
        assert_eq!(missing_folder(&said), Some("/work/helper"));
        assert!(never_started(FOLDER_MISSING) && never_started(NOT_STARTED));
        // A tool's own words of a missing file are no such thing.
        assert_ne!(
            classify_error("flock: No such file or directory (os error 2)"),
            FOLDER_MISSING
        );
        assert_eq!(missing_folder("flock: No such file or directory"), None);
        let told = told(FOLDER_MISSING, &said, "m", Known::default());
        let sentence = "This agent's folder /work/helper no longer exists. Recreate it, or make \
             a new agent in another folder";
        assert_eq!(told.message, sentence);
        assert_eq!(told.row, format!("{sentence}."));
        assert_eq!(told.summary, format!("{sentence}. Your message is saved."));
        assert_eq!(told.provider_message, "");
        // A file where the folder should be is said to be no folder.
        let file = clarp_spool::folder_missing("/work/helper", true);
        assert_eq!(classify_error(&file), FOLDER_MISSING);
        assert_eq!(
            told_of(&file),
            "This agent's folder /work/helper is not a folder. Recreate it as a folder, or make \
             a new agent in another folder"
        );
    }

    fn told_of(said: &str) -> String {
        told(FOLDER_MISSING, said, "m", Known::default()).message
    }

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
    fn a_usage_limit_named_only_to_be_denied_is_none() {
        // Claude Code 2.1.321, when its provider's servers hold requests back for a moment.
        let held = "API Error: Server is temporarily limiting requests (not your usage limit) · \
                    Number of request tokens has exceeded your per-minute rate limit";
        assert_eq!(classify_error(held), TRANSIENT);
        assert_eq!(
            classify_error("Server is temporarily limiting requests (not your usage limit)"),
            TRANSIENT
        );
        assert!(!usage_limit_resets(held));
        assert!(provider_busy(held));
        assert!(provider_busy(
            "API Error: 529 Overloaded. This is a server-side issue, usually temporary"
        ));
        // A number of the 500s in other words is transient, and says nothing of the provider.
        assert_eq!(classify_error("failed at cli.js:512"), TRANSIENT);
        assert!(!provider_busy("failed at cli.js:512"));
        // A limit that is one is read as one, as before.
        assert_eq!(
            classify_error("You've hit your 5-hour limit · resets 6:27pm (Europe/Oslo)"),
            USAGE_LIMIT
        );
        assert_eq!(classify_error("Claude usage limit reached"), USAGE_LIMIT);
    }

    #[test]
    fn the_time_a_limit_resets_is_told_in_the_hosts_zone_with_its_day_when_not_today() {
        let oslo = jiff::tz::TimeZone::get("Europe/Oslo").unwrap();
        // 23 Nov 2026, 14:00 in Oslo (UTC+1), and 6:27 pm the same day.
        let said = 1_795_438_800_000;
        let at = 1_795_454_832_000;
        let time_of = |at_ms, zone: &jiff::tz::TimeZone| {
            reset_time_in(
                Resets {
                    at_ms,
                    said_at_ms: said,
                },
                zone,
            )
        };
        assert_eq!(time_of(at, &oslo).as_deref(), Some("18:27"));
        assert_eq!(
            time_of(at, &jiff::tz::TimeZone::UTC).as_deref(),
            Some("17:27")
        );
        let day = 86_400_000;
        assert_eq!(time_of(at + day, &oslo).as_deref(), Some("Tue 18:27"));
        assert_eq!(time_of(at + 6 * day, &oslo).as_deref(), Some("Sun 18:27"));
        assert_eq!(
            time_of(at + 7 * day, &oslo).as_deref(),
            Some("30 Nov 18:27")
        );
        assert_eq!(
            time_of(at + 40 * day, &oslo).as_deref(),
            Some("2 Jan 2027 18:27")
        );
        assert_eq!(time_of(i64::MAX, &oslo), None);
        // The row and the agent's state say it after the sentence, and the tool's words stay
        // below it. Without a reset, as before.
        let words = "You've hit your 5-hour limit · resets 6:27pm (Europe/Oslo)";
        let resets = Resets {
            at_ms: at,
            said_at_ms: said,
        };
        let known = Known {
            resets: Some(resets),
            ..Known::default()
        };
        let row = told_limit(words, known);
        let time = reset_time_in(resets, &jiff::tz::TimeZone::system()).unwrap();
        assert_eq!(row.message, format!("Usage limit reached · resets {time}"));
        assert_eq!(
            row.row,
            format!("Usage limit reached · resets {time}.\n\n{words}")
        );
        assert_eq!(
            row.summary,
            format!("Usage limit reached · resets {time}. Your message is saved.")
        );
        assert_eq!(
            told_limit(words, Known::default()).message,
            "Usage limit reached"
        );
        // Only a usage limit is told a reset.
        let other = told(TRANSIENT, "overloaded", "", known);
        assert_eq!(other.message, sentence(TRANSIENT, ""));
        assert_eq!(with_reset("Waiting", None), "Waiting");
    }

    fn told_limit(words: &str, known: Known) -> Told {
        told(USAGE_LIMIT, words, "", known)
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
