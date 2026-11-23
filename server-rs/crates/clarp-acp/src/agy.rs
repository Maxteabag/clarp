//! What is Antigravity's own when it runs over the protocol (`agy acp`), shared by the shim and
//! the Host's backend: the turn order the backend writes and the driver reads, the limits on what a
//! message carries, where a conversation Antigravity can no longer continue is marked, and
//! how a failure Antigravity reports is read.
//!
//! Every number here is our own choice, not Antigravity's: the sizes keep one message within
//! what a model is given for a picture or a sound, and the silence bound ends a turn whose
//! tool has gone quiet for longer than any turn of ours has been seen to think.

use std::path::{Path, PathBuf};

use serde_json::{Value, json};

mod failure;
mod place;

pub use failure::{Failure, classify, error_text, reason};
pub use place::{here, poisoned_dir, profile};

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

/// The mark of one conversation, for a name that is one path component.
pub fn poisoned_mark(dir: &Path, session: &str) -> Option<PathBuf> {
    crate::session_name(session).then(|| dir.join(session))
}

/// What the driver gives agy of its environment, besides what it inherits.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct ChildEnv {
    /// Names taken away: [`STRIPPED`].
    pub removed: Vec<&'static str>,
    /// Names set, with their values.
    pub set: Vec<(&'static str, String)>,
    /// Arguments added after `agy acp`.
    pub args: Vec<String>,
}

/// agy's environment: its own updates off, its output unbuffered, a certificate bundle where
/// none is named (`ca_bundle`, found by the caller), and, with a profile of its own, its login
/// kept in a file there. agy 1.5 keeps a Google sign-in in the keyring unless it believes it
/// runs over SSH, whatever else it is told: so where the Host was not started over SSH
/// (`over_ssh`), it is told it was.
pub fn child_env(profile: Option<&Path>, ca_bundle: Option<&str>, over_ssh: bool) -> ChildEnv {
    let mut env = ChildEnv {
        removed: STRIPPED.to_vec(),
        set: vec![
            ("AGY_CLI_DISABLE_AUTO_UPDATE", "1".to_string()),
            ("PYTHONUNBUFFERED", "1".to_string()),
        ],
        args: Vec::new(),
    };
    if let Some(bundle) = ca_bundle {
        env.set.push(("SSL_CERT_FILE", bundle.to_string()));
    }
    if let Some(profile) = profile {
        env.args = vec!["--gemini-dir".to_string(), profile.display().to_string()];
        env.set
            .push(("AGY_ACP_FORCE_FILE_STORAGE", "1".to_string()));
        if !over_ssh {
            env.set
                .push(("SSH_CONNECTION", "127.0.0.1 0 127.0.0.1 0".to_string()));
        }
    }
    env
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;

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

    #[test]
    fn agy_is_told_to_keep_a_profile_login_in_a_file_and_nothing_else_by_default() {
        let plain = child_env(None, None, false);
        assert_eq!(plain.removed, STRIPPED);
        assert!(plain.args.is_empty());
        assert!(plain.set.iter().all(|(name, _)| !name.contains("SSH")));
        let own = child_env(Some(Path::new("/p")), Some("/etc/ca.pem"), false);
        assert_eq!(own.args, ["--gemini-dir", "/p"]);
        let names: Vec<&str> = own.set.iter().map(|(name, _)| *name).collect();
        assert!(names.contains(&"SSL_CERT_FILE") && names.contains(&"AGY_ACP_FORCE_FILE_STORAGE"));
        assert!(names.contains(&"SSH_CONNECTION"));
        let over_ssh = child_env(Some(Path::new("/p")), None, true);
        assert!(
            over_ssh
                .set
                .iter()
                .all(|(name, _)| *name != "SSH_CONNECTION")
        );
    }
}
