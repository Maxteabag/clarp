//! The visual rubric's text rules, as code: words a reader of the app should
//! never meet, and the plain words a failed request is told in.
//!
//! `breaks` names what a piece of screen text does wrong; the source check
//! (`core/tests/ui_text.rs`) runs it over every string the desktop draws, in
//! the `.slint` files and in the Rust that builds screen text. A string that
//! must keep such a word (a settings search alias, a protocol header) carries
//! `ui-text: allow (<why>)` on its line or the line above; an allow without a
//! reason fails the check.
//!
//! What the rules come from (the error catalogue): internal jargon in a row
//! after Stop ("Stop acknowledged · Actor: user (verified)", CL-078); the
//! agent's name said again beside its own header ("Theo is working", CL-070);
//! thinking text cut into the header ("Thinking: We need to…", CL-079); a
//! zero-length thought as a row ("Thought for 0s", CL-071); an em dash in
//! copy (CL-077). A string on today's screen that breaks a rule and waits
//! for its fix to be approved is named in the check's allow-list.

use std::sync::LazyLock;

use fancy_regex::Regex;

/// Words of the Host's and the client's insides: the reader never needs them.
const INTERNAL: &[&str] = &[
    "lane", "lanes", "spool", "lseq", "epoch", "harness", "trace", "traces", "shim", "stdin", "stdout", "stderr", "pid", "actor", "payload", "sse",
];

static INTERNAL_WORD: LazyLock<Regex> = LazyLock::new(|| Regex::new(&format!(r"(?i)\b({})\b", INTERNAL.join("|"))).expect("static pattern"));
/// A raw id: a field name (`turn_id`), a long hex id or a UUID.
static RAW_ID: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?i)\b[a-z]+_id\b|\b[0-9a-f]{12,}\b|\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-").expect("static pattern"));
/// An HTTP status, written or about to be filled in ("HTTP 404", "HTTP {status}").
static HTTP_STATUS: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"(?i)\bHTTP\s*(\d{3}\b|\{)|\b(status|answered)\s+[1-5]\d\d\b").expect("static pattern"));
/// The catalogue's phrasing.
static PHRASING: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?i)thought for 0s|\backnowledged\b|no active goals|refresh pending|used: unknown|\(verified\)|\}\s+is working|\bthinking:").expect("static pattern"));
/// A placeholder a format string fills in (`{name}`, Slint's `\{expr}`).
static PLACEHOLDER: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\\?\{[^{}]*\}").expect("static pattern"));

/// What `text` does against the rubric, one short reason each; empty when
/// nothing. Placeholders are not read as words, but a placeholder standing
/// right before "is working" (a name said again) is.
pub fn breaks(text: &str) -> Vec<String> {
    let bare = PLACEHOLDER.replace_all(text, " ");
    let mut found = Vec::new();
    for word in INTERNAL_WORD.find_iter(&bare).flatten() {
        found.push(format!("internal word '{}'", word.as_str()));
    }
    for id in RAW_ID.find_iter(&bare).flatten().filter(|m| m.as_str().contains(['_', '-']) || m.as_str().contains(|c: char| c.is_ascii_digit())) {
        found.push(format!("raw id '{}'", id.as_str()));
    }
    if let Some(status) = HTTP_STATUS.find(text).ok().flatten() {
        found.push(format!("HTTP status '{}'", status.as_str()));
    }
    if let Some(phrase) = PHRASING.find(text).ok().flatten() {
        found.push(format!("catalogue phrasing '{}'", phrase.as_str()));
    }
    // A dash between two thoughts: say it as two, or with a colon (CL-077).
    // A lone dash for an empty value is not prose and is not read here.
    if text.contains('\u{2014}') || text.contains("\\u{2014}") {
        found.push("an em dash".to_owned());
    }
    found
}

/// A failed request in plain words: the Host's own sentence where it sent
/// one, else what the status means to the reader; never the status number.
/// `status` 0: the Host was not reached, and `message` says why.
pub fn failure(message: &str, status: u16) -> String {
    let message = message.trim();
    let only_status = message.is_empty() || message.to_ascii_uppercase().starts_with("HTTP ");
    if status == 0 || !only_status {
        return if message.is_empty() { "The Host could not be reached".to_owned() } else { message.to_owned() };
    }
    match status {
        401 | 403 => "The Host refused this",
        404 | 410 => "The Host does not have that",
        408 | 504 => "The Host did not answer in time",
        409 | 412 => "That changed meanwhile; try again",
        413 => "That is too large for the Host",
        429 => "The Host is busy; try again shortly",
        400..=499 => "The Host could not take that",
        _ => "Something went wrong on the Host",
    }
    .to_owned()
}
