//! What the Host and its turn driver share of Antigravity run over the Agent Client Protocol
//! (`agy acp`): the turn order the backend writes and the driver reads, the limits on what a
//! message carries, where a conversation Antigravity can no longer continue is marked, and
//! how a failure Antigravity reports is read.
//!
//! Every number here is our own choice, not Antigravity's: the sizes keep one message within
//! what a model is given for a picture or a sound, and the silence bound ends a turn whose
//! tool has gone quiet for longer than any turn of ours has been seen to think.

use std::path::{Path, PathBuf};

use serde_json::{Value, json};

/// The word that makes the shim run a turn of Antigravity over ACP.
pub const DRIVER: &str = "agy-acp-turn";

/// The largest picture sent as a picture. A larger one stays a path in the text.
pub const IMAGE_MOST: u64 = 10 * 1024 * 1024;
/// The largest sound sent as a sound.
pub const AUDIO_MOST: u64 = 20 * 1024 * 1024;
/// The most one message carries of files, in all.
pub const ATTACHED_MOST: u64 = 50 * 1024 * 1024;
/// How many files one message may carry.
pub const ATTACHED_COUNT_MOST: usize = 20;
/// How long a prompt may stay open without a word from Antigravity before the turn is ended.
pub const SILENCE_MOST_SECS: u64 = 15 * 60;
/// How long a stopped turn waits for Antigravity to say it cancelled.
pub const CANCEL_WAIT_MS: u64 = 8_000;

/// The environment variable that names a profile directory of its own for Antigravity:
/// a Gemini directory whose login is kept in a file there rather than in the keyring.
pub const PROFILE_ENV: &str = "CLARP_AGY_ACP_PROFILE";

/// Variables of the Host's environment the driver does not hand to Antigravity: a stray key
/// or project would sign it in as another account than its own login, and a browser variable
/// would make it open pages on the Host's desktop.
pub const STRIPPED: [&str; 8] = [
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_CLOUD_PROJECT",
    "GOOGLE_CLOUD_LOCATION",
    "GOOGLE_GENAI_USE_VERTEXAI",
    "GEMINI_HOME",
    "BROWSER",
];

/// What kind of block a file is sent as.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    Image,
    Audio,
    /// A document Antigravity reads itself, by a link to it.
    Link,
}

/// The kind and type of a file by its name, for the files that go as more than a path.
pub fn kind_of(path: &Path) -> Option<(Kind, &'static str)> {
    let extension = path.extension()?.to_str()?.to_ascii_lowercase();
    Some(match extension.as_str() {
        "png" => (Kind::Image, "image/png"),
        "jpg" | "jpeg" => (Kind::Image, "image/jpeg"),
        "gif" => (Kind::Image, "image/gif"),
        "webp" => (Kind::Image, "image/webp"),
        "heic" => (Kind::Image, "image/heic"),
        "mp3" => (Kind::Audio, "audio/mpeg"),
        "m4a" => (Kind::Audio, "audio/mp4"),
        "wav" => (Kind::Audio, "audio/wav"),
        "ogg" | "oga" | "opus" => (Kind::Audio, "audio/ogg"),
        "flac" => (Kind::Audio, "audio/flac"),
        "aac" => (Kind::Audio, "audio/aac"),
        "webm" => (Kind::Audio, "audio/webm"),
        "pdf" => (Kind::Link, "application/pdf"),
        _ => return None,
    })
}

/// The turn order, as the backend writes it on the driver's standard input.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Order {
    /// The conversation to continue; empty for a new one.
    pub session: String,
    pub cwd: String,
    /// The message, without the Host's head.
    pub prompt: String,
    /// The message with the Host's head before it: what is sent where Antigravity takes no
    /// addition to its instructions.
    pub headed: String,
    /// The Host's head, for Antigravity's instructions where it takes an addition to them.
    pub append: String,
    pub model: String,
    pub effort: String,
    /// The files of the message that may go as more than their path.
    pub attachments: Vec<String>,
}

impl Order {
    pub fn to_json(&self) -> Value {
        json!({
            "session": self.session,
            "cwd": self.cwd,
            "prompt": self.prompt,
            "headed": self.headed,
            "append": self.append,
            "model": self.model,
            "effort": self.effort,
            "attachments": self.attachments,
        })
    }

    pub fn from_json(value: &Value) -> Self {
        let text = |key: &str| value[key].as_str().unwrap_or_default().to_string();
        Self {
            session: text("session"),
            cwd: text("cwd"),
            prompt: text("prompt"),
            headed: text("headed"),
            append: text("append"),
            model: text("model"),
            effort: text("effort"),
            attachments: value["attachments"]
                .as_array()
                .map(|paths| {
                    paths
                        .iter()
                        .filter_map(Value::as_str)
                        .map(str::to_string)
                        .collect()
                })
                .unwrap_or_default(),
        }
    }
}

/// Where Antigravity keeps what is its own: under the profile directory where one is named,
/// else under the user's home, as `agy` keeps it.
pub fn own_dir(home: &Path, profile: Option<&Path>) -> PathBuf {
    match profile {
        Some(profile) => profile.join("antigravity-cli"),
        None => home.join(".gemini/antigravity-cli"),
    }
}

/// The profile the environment names, when it names one.
pub fn profile() -> Option<PathBuf> {
    std::env::var_os(PROFILE_ENV)
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
}

/// Where the conversations are marked that Antigravity can no longer continue.
pub fn poisoned_dir() -> Option<PathBuf> {
    let state = std::env::var_os("XDG_STATE_HOME")
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
        .or_else(|| {
            std::env::var_os("HOME")
                .filter(|value| !value.is_empty())
                .map(|home| PathBuf::from(home).join(".local/state"))
        })?;
    Some(state.join("clarp/agy-acp/poisoned"))
}

/// The mark of one conversation, for a name that is one path component.
pub fn poisoned_mark(dir: &Path, session: &str) -> Option<PathBuf> {
    crate::native_session_name(session).then(|| dir.join(session))
}

/// What went wrong, as Antigravity's own words say it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Failure {
    /// The account's quota is spent. `resets`: when it begins anew, as Antigravity says it (a
    /// moment) or how long until then (`in 3h2m`), where it says either.
    Quota {
        resets: Option<String>,
    },
    /// The provider asked to be tried again later, after this long.
    Retry {
        after: String,
    },
    /// The conversation holds a thinking block without a valid signature: every later prompt
    /// of it fails the same way.
    Signature,
    /// The conversation is not known to Antigravity.
    Unknown,
    /// Antigravity is not signed in.
    SignedOut,
    /// An error of Google's API, with its code and status where they are said.
    Api {
        code: String,
        status: String,
        message: String,
    },
    Other,
}

/// The value written after `key` (`key:value`), up to a blank, a bracket, a comma or a quote.
fn field<'a>(text: &'a str, key: &str) -> Option<&'a str> {
    let at = text.find(key)? + key.len();
    let rest = text[at..].trim_start_matches([' ', '"']);
    let end = rest
        .find(|c: char| c.is_whitespace() || matches!(c, ']' | ',' | '"' | '}' | ')'))
        .unwrap_or(rest.len());
    Some(&rest[..end]).filter(|value| !value.is_empty())
}

/// Whether a text is a duration as Go writes one (`1h2m3.5s`, `250ms`).
fn duration(text: &str) -> bool {
    let mut rest = text;
    let mut any = false;
    while !rest.is_empty() {
        let digits = rest
            .find(|c: char| !(c.is_ascii_digit() || c == '.'))
            .unwrap_or(rest.len());
        if digits == 0 {
            return false;
        }
        rest = &rest[digits..];
        let Some(unit) = ["ns", "us", "µs", "μs", "ms", "h", "m", "s"]
            .into_iter()
            .find(|unit| rest.starts_with(unit))
        else {
            return false;
        };
        rest = &rest[unit.len()..];
        any = true;
    }
    any
}

/// `Error 429, Message: …, Status: RESOURCE_EXHAUSTED`: Google's error as the agent's
/// harness writes it.
fn api_error(text: &str) -> Option<Failure> {
    let mut from = 0;
    while let Some(found) = text[from..].find("Error ") {
        let at = from + found + "Error ".len();
        from = at;
        let Some(code) = text.get(at..at + 3) else {
            continue;
        };
        if !code.bytes().all(|b| b.is_ascii_digit()) {
            continue;
        }
        let Some(rest) = text[at + 3..].strip_prefix(", Message: ") else {
            continue;
        };
        let Some((message, after)) = rest.split_once(", Status: ") else {
            continue;
        };
        let status: String = after
            .chars()
            .take_while(|c| c.is_ascii_uppercase() || *c == '_' || c.is_ascii_digit())
            .collect();
        return Some(Failure::Api {
            code: code.to_string(),
            status,
            message: message.trim().to_string(),
        });
    }
    None
}

/// `RESOURCE_EXHAUSTED (code 429): message`: the same error as Antigravity 1.5 writes it for
/// a Google account.
fn status_error(text: &str) -> Option<Failure> {
    let at = text.find(" (code ")?;
    let status: String = text[..at]
        .chars()
        .rev()
        .take_while(|c| c.is_ascii_uppercase() || *c == '_')
        .collect::<Vec<_>>()
        .into_iter()
        .rev()
        .collect();
    let rest = &text[at + " (code ".len()..];
    let code = rest.get(..3)?;
    let message = rest[3..].strip_prefix("): ")?;
    let message = message.lines().next().unwrap_or_default();
    (!status.is_empty() && code.bytes().all(|b| b.is_ascii_digit())).then(|| Failure::Api {
        code: code.to_string(),
        status,
        message: message.trim().to_string(),
    })
}

/// What a failure Antigravity reported is, from what it said: the error of a request, the
/// lines it wrote to its standard error, or both.
pub fn classify(text: &str) -> Failure {
    let lower = text.to_ascii_lowercase();
    if lower.contains("thinking.signature: field required")
        || lower.contains("invalid `signature` in `thinking` block")
        || lower.contains("invalid_thinking_signature")
    {
        return Failure::Signature;
    }
    if text.contains("QUOTA_EXHAUSTED") {
        let at = field(text, "quotaResetTimeStamp:").map(str::to_string);
        let delay = field(text, "quotaResetDelay:")
            .filter(|delay| duration(delay))
            .map(|delay| format!("in {delay}"));
        return Failure::Quota {
            resets: at.or(delay),
        };
    }
    if let Some(after) = field(text, "retryDelay:").filter(|delay| duration(delay)) {
        return Failure::Retry {
            after: after.to_string(),
        };
    }
    if lower.contains("authentication required")
        || lower.contains("not authenticated")
        || lower.contains("not logged in")
        || lower.contains("please log in")
        || lower.contains("auth_required")
    {
        return Failure::SignedOut;
    }
    if let Some(api) = api_error(text).or_else(|| status_error(text)) {
        return api;
    }
    if lower.contains("session not found")
        || lower.contains("conversation not found")
        || lower.contains("unknown session")
        || lower.contains("no such session")
    {
        return Failure::Unknown;
    }
    Failure::Other
}

/// What the user is told of a failure: in words the Host's own reading of failures knows
/// (a spent quota reads as a usage limit, a sign-in as one that is missing).
pub fn reason(failure: &Failure, said: &str) -> String {
    let said = said.trim();
    match failure {
        Failure::Quota { resets: Some(at) } if at.starts_with("in ") => {
            format!("Antigravity usage quota exhausted; it resets {at}.")
        }
        Failure::Quota { resets: Some(at) } => {
            format!("Antigravity usage quota exhausted; it resets at {at}.")
        }
        Failure::Quota { resets: None } => "Antigravity usage quota exhausted.".to_string(),
        Failure::Retry { after } => {
            format!("Antigravity was rate limited by Google; try again in {after}.")
        }
        Failure::Signature => "Antigravity's conversation holds a thinking block without a valid \
                               signature, so it cannot be continued; the next turn starts a new \
                               conversation with this one's record."
            .to_string(),
        Failure::Unknown => format!("Antigravity no longer has this conversation: {said}"),
        Failure::SignedOut => format!(
            "Antigravity is not logged in on this Host; sign in by running agy on the Host. ({said})"
        ),
        Failure::Api {
            code,
            status,
            message,
        } => format!("Google API error {code} {status}: {message}"),
        Failure::Other if said.is_empty() => "Antigravity's turn failed.".to_string(),
        Failure::Other => said.to_string(),
    }
}

/// The text of a JSON-RPC error: its message, and what its data says.
pub fn error_text(error: &Value) -> String {
    let message = error["message"].as_str().unwrap_or_default();
    let data = match &error["data"] {
        Value::Null => String::new(),
        Value::String(text) => text.clone(),
        other => other.to_string(),
    };
    match (message.is_empty(), data.is_empty()) {
        (_, true) => message.to_string(),
        (true, false) => data,
        (false, false) => format!("{message}: {data}"),
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;

    #[test]
    fn a_spent_quota_is_read_with_its_reset() {
        let said = "Error 429, Message: Resource has been exhausted, Status: RESOURCE_EXHAUSTED \
                    [reason:QUOTA_EXHAUSTED quotaResetTimeStamp:2026-11-23T18:00:00Z]";
        assert_eq!(
            classify(said),
            Failure::Quota {
                resets: Some("2026-11-23T18:00:00Z".into())
            }
        );
        let delayed = "RESOURCE_EXHAUSTED (code 429): quota [reason:QUOTA_EXHAUSTED quotaResetDelay:3h2m1.5s]";
        assert_eq!(
            classify(delayed),
            Failure::Quota {
                resets: Some("in 3h2m1.5s".into())
            }
        );
        let text = reason(&classify(said), said);
        assert!(text.contains("usage quota exhausted") && text.contains("resets at"));
        assert_eq!(
            classify("reason:QUOTA_EXHAUSTED quotaResetDelay:soon"),
            Failure::Quota { resets: None }
        );
    }

    #[test]
    fn a_retry_delay_a_signature_and_api_errors_are_told_apart() {
        assert_eq!(
            classify("Error 429, Message: slow down, Status: RESOURCE_EXHAUSTED [retryDelay:30s]"),
            Failure::Retry {
                after: "30s".into()
            }
        );
        for said in [
            "messages.3.content.0.thinking.signature: Field required",
            "messages.12.content.1: Invalid `signature` in `thinking` block",
        ] {
            assert_eq!(classify(said), Failure::Signature, "{said}");
        }
        assert_eq!(
            classify(
                "Error 503, Message: The model is overloaded, Status: UNAVAILABLE\ngoroutine 1"
            ),
            Failure::Api {
                code: "503".into(),
                status: "UNAVAILABLE".into(),
                message: "The model is overloaded".into()
            }
        );
        assert_eq!(
            classify("rpc: PERMISSION_DENIED (code 403): caller lacks access\nstack"),
            Failure::Api {
                code: "403".into(),
                status: "PERMISSION_DENIED".into(),
                message: "caller lacks access".into()
            }
        );
        assert_eq!(classify("Authentication required"), Failure::SignedOut);
        assert_eq!(classify("Session not found: abc"), Failure::Unknown);
        assert_eq!(classify("something else"), Failure::Other);
    }

    #[test]
    fn an_order_reads_back_as_it_was_written() {
        let order = Order {
            session: "c-1".into(),
            cwd: "/w".into(),
            prompt: "hi".into(),
            headed: "head\n\nhi".into(),
            append: "head".into(),
            model: "m".into(),
            effort: "low".into(),
            attachments: vec!["/d/uploads/a.png".into()],
        };
        assert_eq!(Order::from_json(&order.to_json()), order);
    }

    #[test]
    fn files_are_sent_by_their_kind_and_marks_are_one_component() {
        assert_eq!(kind_of(Path::new("/a/B.PNG")).unwrap().0, Kind::Image);
        assert_eq!(kind_of(Path::new("/a/b.m4a")).unwrap().0, Kind::Audio);
        assert_eq!(kind_of(Path::new("/a/b.pdf")).unwrap().0, Kind::Link);
        assert!(kind_of(Path::new("/a/b.txt")).is_none());
        assert!(poisoned_mark(Path::new("/s"), "../x").is_none());
        assert_eq!(
            poisoned_mark(Path::new("/s"), "c-1").unwrap(),
            Path::new("/s/c-1")
        );
    }
}
