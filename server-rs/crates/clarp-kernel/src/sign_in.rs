//! A tool's sign-in started from an app, as the app is told it (the feature `backend_auth`).
//!
//! The Host runs the tool's own sign-in (`claude auth login`, `codex login --device-auth`,
//! `grok login --device-auth`) and shows the app what it prints: without a terminal's signs and
//! without anything that looks like a key, with the address to open, the code to type, and
//! whether the tool waits for a code pasted back. The rules are the Python Host's
//! (`server/lib/backend_auth.py`), held to it by `tests/fixtures/backend-auth-vectors.json`.
//! Nothing here starts a tool or reads a file: the Host does that and asks these rules what to
//! say.

use std::sync::LazyLock;

use regex::Regex;
use serde_json::{Value, json};

use crate::python;

/// How long a sign-in may run before its tool is stopped, in milliseconds. It is also how
/// long an address or a code it showed is said to be good for.
pub const TIMEOUT_MS: i64 = 600_000;
/// How much of what a sign-in printed is kept, in signs: the newest. What an app is shown is
/// the last [`SHOWN`] of it, far inside this, so a key cut in two where the kept text begins
/// can never be shown half.
pub const KEPT: usize = 65_536;
/// How much of what a sign-in printed an app is shown, in signs.
pub const SHOWN: usize = 8_000;
/// The longest code an app may paste back, in signs.
pub const LONGEST_CODE: usize = 512;
/// How long a sign-out may take, in milliseconds.
pub const SIGN_OUT_MOST_MS: u64 = 30_000;

/// The tools a person can sign in to from an app, in the order the Host lists them.
pub const TOOLS: [&str; 3] = ["claude", "codex", "grok"];

/// How a tool is signed in and out: its own commands, after the program's name.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Driver {
    pub login: &'static [&'static str],
    pub logout: &'static [&'static str],
    /// Whether an app that does not ask for every harness is shown the tool. Grok came later,
    /// and an older app is never shown a row it was not built for.
    pub listed_by_default: bool,
}

/// How the tool of this id is signed in and out, when it is one a person signs in to from an
/// app. The id is taken as the Python Host's registry takes one: without blanks around it and
/// in small letters (none of these tools has another name).
pub fn driver(tool: &str) -> Option<Driver> {
    match python::strip(tool).to_lowercase().as_str() {
        "claude" => Some(Driver {
            login: &["auth", "login", "--claudeai"],
            logout: &["auth", "logout"],
            listed_by_default: true,
        }),
        "codex" => Some(Driver {
            login: &["login", "--device-auth"],
            logout: &["logout"],
            listed_by_default: true,
        }),
        "grok" => Some(Driver {
            login: &["login", "--device-auth"],
            logout: &["logout"],
            listed_by_default: false,
        }),
        _ => None,
    }
}

/// What a sign-in or sign-out of a tool that cannot be signed in to from here, or is not on
/// this machine, is refused with.
pub fn not_installed(tool: &str) -> String {
    format!("{tool} CLI is not installed")
}

/// A field of a request as the Python Host reads it (`str(data.get(key) or "")`): nothing for
/// what Python counts as false, a text as it is, anything else as Python writes it.
pub fn field(value: Option<&Value>) -> String {
    match value {
        Some(value) if python::truthy(value) => match value {
            Value::String(text) => text.clone(),
            other => python::repr(other),
        },
        _ => String::new(),
    }
}

/// The tool a request names: its field `backend`, without blanks around it and in small
/// letters, as the Python Host's routes take it.
pub fn tool_named(body: &serde_json::Map<String, Value>) -> String {
    python::strip(&field(body.get("backend"))).to_lowercase()
}

// ---- what a sign-in printed --------------------------------------------------------------

/// A terminal's title or link, closed by a bell or by `ESC \`.
static OSC: LazyLock<Regex> = LazyLock::new(|| regex(r"(?s)\x1b\].*?(?:\x07|\x1b\\)"));
/// One that is never closed: the rest of the text goes with it.
static OSC_OPEN: LazyLock<Regex> = LazyLock::new(|| regex(r"(?s)\x1b\].*\z"));
static CSI: LazyLock<Regex> = LazyLock::new(|| regex(r"\x1b\[[0-?]*[ -/]*[@-~]"));
static SINGLE: LazyLock<Regex> = LazyLock::new(|| regex(r"\x1b[@-_]"));
/// What looks like a key: Python's `(?i)(sk-[a-z0-9_-]{12,}|bearer\s+[a-z0-9._-]{12,})`,
/// written out sign by sign. Python's way of ignoring case takes four letters beyond ASCII
/// for some of these (the dotted and dotless i, the long s, the Kelvin sign) that Unicode's
/// simple folding gives otherwise; `\s` is Python's blank, of which nothing but the ones
/// [`clean`] keeps is left by then.
static SECRET: LazyLock<Regex> = LazyLock::new(|| {
    let letters = r"a-zA-Z0-9_\x{130}\x{131}\x{17f}\x{212a}";
    regex(&format!(
        r"[sS\x{{17f}}][kK\x{{212a}}]-[{letters}-]{{12,}}|[bB][eE][aA][rR][eE][rR]\s+[{letters}.-]{{12,}}"
    ))
});
/// An address; the first about signing in is the one to open.
static URL: LazyLock<Regex> = LazyLock::new(|| regex(r"https://[^\s\x1b]+"));
/// The prompt at the end of what a tool printed when it waits for a code pasted back
/// (`claude auth login`: "Paste code here if prompted > "). Python ignores case; the long s
/// is the one sign beyond ASCII that it takes for a letter of these words.
const PROMPT: &str =
    r"(?:[pP][aA][sS\x{17f}][tT][eE]|[eE][nN][tT][eE][rR])[^\n]*[cC][oO][dD][eE][^\n]*>[ \t]*";
static PROMPT_AT_END: LazyLock<Regex> = LazyLock::new(|| regex(&format!(r"{PROMPT}\z")));
static PROMPT_AT_START: LazyLock<Regex> = LazyLock::new(|| regex(&format!(r"\A{PROMPT}")));

#[allow(clippy::expect_used)] // the patterns are this module's own, and its tests compile them
fn regex(pattern: &str) -> Regex {
    Regex::new(pattern).expect("a pattern of the sign-in's own")
}

/// The newest of what a sign-in printed, after more came: no more than [`KEPT`] signs.
pub fn append(kept: &mut String, more: &str) {
    kept.push_str(more);
    let count = kept.chars().count();
    if count > KEPT
        && let Some((at, _)) = kept.char_indices().nth(count - KEPT)
    {
        kept.drain(..at);
    }
}

/// What an app is shown of what a sign-in printed: no terminal's signs, no other sign below
/// the space but the line feed and the tab, nothing that looks like a key, the last
/// [`SHOWN`] signs of it, without blanks at the ends.
pub fn clean(printed: &str) -> String {
    let text = OSC.replace_all(printed, "");
    let text = OSC_OPEN.replace_all(&text, "");
    let text = CSI.replace_all(&text, "");
    let text = SINGLE.replace_all(&text, "");
    let text: String = text
        .chars()
        .filter(|sign| matches!(sign, '\n' | '\t') || u32::from(*sign) >= 32)
        .collect();
    let text = SECRET.replace_all(&text, "[redacted]");
    let count = text.chars().count();
    let last = match text.char_indices().nth(count.saturating_sub(SHOWN)) {
        Some((at, _)) if count > SHOWN => &text[at..],
        _ => &text[..],
    };
    python::strip(last).to_string()
}

/// Why a sign-in or sign-out failed, in one line: the last line it printed, without the
/// prompt for a code that the tool writes its failure behind; at most 500 signs.
pub fn failure(printed: &str) -> String {
    let clean = clean(printed);
    let last = python::splitlines(&clean)
        .into_iter()
        .map(python::strip)
        .rfind(|line| !line.is_empty());
    let summary = last.map_or(String::new(), |line| {
        PROMPT_AT_START.replace(line, "").into_owned()
    });
    let summary = if summary.is_empty() {
        "Sign-in failed"
    } else {
        &summary
    };
    python::first(summary, 500).to_string()
}

/// The first code of a device's sign-in in a text: four capitals or digits, a dash, five
/// more, with no capital or digit right before or after.
fn device_code(text: &str) -> Option<String> {
    let signs: Vec<char> = text.chars().collect();
    let part = |sign: &char| sign.is_ascii_uppercase() || sign.is_ascii_digit();
    (0..signs.len().saturating_sub(9)).find_map(|at| {
        let before = at.checked_sub(1).and_then(|at| signs.get(at));
        let candidate = &signs[at..at + 10];
        let fits = candidate[..4].iter().all(part)
            && candidate[4] == '-'
            && candidate[5..].iter().all(part)
            && !before.is_some_and(part)
            && !signs.get(at + 10).is_some_and(part);
        fits.then(|| candidate.iter().collect())
    })
}

/// Where a sign-in is.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum State {
    Idle,
    Running,
    Complete,
    Failed,
}

impl State {
    pub fn name(self) -> &'static str {
        match self {
            Self::Idle => "idle",
            Self::Running => "running",
            Self::Complete => "complete",
            Self::Failed => "failed",
        }
    }
}

/// A sign-in as an app is told it (in `GET /backend-auth`'s `tasks`, and in the answer to a
/// sign-in or a code). `started_at` 0: it starts now. `answered`: what was shown when a code
/// was last pasted back, so that a prompt the tool has already read does not ask again.
pub fn task(
    tool: &str,
    state: State,
    printed: &str,
    started_at: i64,
    now_ms: i64,
    error: &str,
    answered: &str,
) -> Value {
    let clean = clean(printed);
    let url = URL
        .find_iter(&clean)
        .map(|found| found.as_str())
        .find(|url| url.contains("auth") || url.contains("login"))
        .map_or("", |url| url.trim_end_matches(['.', ',', ')']));
    let code = device_code(&clean);
    let started = if started_at == 0 { now_ms } else { started_at };
    let waiting = state == State::Running && PROMPT_AT_END.is_match(&clean);
    let expires_at = if code.is_some() || !url.is_empty() {
        started + TIMEOUT_MS
    } else {
        0
    };
    json!({
        "backend": tool,
        "status": state.name(),
        "output": clean,
        "verification_url": url,
        "user_code": code.unwrap_or_default(),
        "awaiting_code": waiting && clean != answered,
        "code_submitted": waiting && clean == answered,
        "started_at": started,
        "expires_at": expires_at,
        "error": error,
    })
}

// ---- a code pasted back -------------------------------------------------------------------

/// The code an app pasted back, without blanks around it, or why it is refused before any
/// sign-in is looked at.
pub fn code(said: &str) -> Result<String, &'static str> {
    let code = python::strip(said);
    if code.is_empty() {
        return Err("Authorization code is required");
    }
    if code.chars().count() > LONGEST_CODE || code.contains(['\n', '\r']) {
        return Err("Authorization code is not in the expected format");
    }
    Ok(code.to_string())
}

/// Why a code cannot go to a sign-in: the app is told so with 409.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CodeConflict {
    /// No sign-in of the tool runs.
    NotWaiting,
    /// One runs, and has not asked for a code (or has been given one).
    NotAsking,
    /// The tool no longer reads what it is given.
    NotAccepting,
    /// Writing it to the tool failed, for this reason.
    NotSent(String),
}

impl CodeConflict {
    pub fn words(&self) -> String {
        match self {
            Self::NotWaiting => "No sign-in is waiting for a code".into(),
            Self::NotAsking => "This sign-in is not asking for a code".into(),
            Self::NotAccepting => "Sign-in is no longer accepting input".into(),
            Self::NotSent(why) => format!("Could not send the code: {why}"),
        }
    }
}

/// Whether a code can go to a sign-in that is as `told` says (`None`: none was ever started),
/// with or without a running tool to give it to.
pub fn code_can_go(told: Option<&Value>, tool_runs: bool) -> Result<(), CodeConflict> {
    let Some(told) = told.filter(|told| told["status"] == "running" && tool_runs) else {
        return Err(CodeConflict::NotWaiting);
    };
    if !python::truthy(&told["awaiting_code"]) {
        return Err(CodeConflict::NotAsking);
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_patterns_compile() {
        for pattern in [
            &*OSC,
            &*OSC_OPEN,
            &*CSI,
            &*SINGLE,
            &*SECRET,
            &*URL,
            &*PROMPT_AT_END,
            &*PROMPT_AT_START,
        ] {
            assert!(!pattern.as_str().is_empty());
        }
    }
}
