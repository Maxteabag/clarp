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
pub mod steer;

pub use failure::{Failure, classify, error_text, reason};
pub use place::{here, poisoned_dir, profile};

/// The word that makes the shim run a turn of Antigravity over ACP.
pub const DRIVER: &str = "agy-acp-turn";
pub const READ_ONLY: &str = "--read-only";

/// The largest picture sent as a picture. A larger one stays a path in the text.
pub const IMAGE_MOST: u64 = 10 * 1024 * 1024;
/// The largest sound sent as a sound.
pub const AUDIO_MOST: u64 = 20 * 1024 * 1024;
/// The most one message carries of files, in all.
pub const ATTACHED_MOST: u64 = 50 * 1024 * 1024;
/// How many files one message may carry.
pub const ATTACHED_COUNT_MOST: usize = 20;
/// How long a prompt may stay open without a word from Antigravity before the turn is ended,
/// while no tool call of its is open.
pub const SILENCE_MOST_SECS: u64 = 15 * 60;
/// The same while a tool call is open: a build or a test suite says nothing until it ends,
/// and agy's own card gives a turn a day.
pub const TOOL_SILENCE_MOST_SECS: u64 = 24 * 60 * 60;
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
    /// How long a prompt may stay silent while no tool call is open, in milliseconds, where it
    /// is not [`SILENCE_MOST_SECS`]: for tests, which cannot wait a quarter of an hour. Nought:
    /// the bound itself.
    pub silence_ms: u64,
    /// The name of the turn's first message, which the driver names again in the result
    /// that answers it (`clarp_kernel::steering`): what tells the Host which answer is the
    /// turn's own and which one answers a message it was given while it ran.
    pub uuid: String,
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
            "silence_ms": self.silence_ms,
            "uuid": self.uuid,
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
            silence_ms: value["silence_ms"].as_u64().unwrap_or(0),
            uuid: text("uuid"),
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

/// What a turn sets of the conversation's options: the model and the thinking level, each as
/// the option's id and the value to give it (an empty id: the model is set the older way,
/// `session/set_model`), and what the user is told of what could not be applied.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Choice {
    pub model: Option<(String, String)>,
    pub thought: Option<(String, String)>,
    pub notes: Vec<String>,
}

/// The values an option offers, for a sentence.
fn listed(option: &Value) -> String {
    let values: Vec<String> = crate::schema::offered(option)
        .into_iter()
        .map(|(value, _)| value)
        .collect();
    values.join(", ")
}

/// The model and thinking level a turn asks for, as Antigravity offers them over ACP. The
/// Host's own list of Antigravity's models (`agy models`) names a model with its effort in one
/// id (`gemini-3.8-flash-low`), while ACP offers the model (`gemini-3.8-flash`) and its thinking
/// level (`low`) apart: an id that is no model offered is read as a model offered followed by
/// one of the thinking levels offered. `Err`: why the turn cannot run as asked. A model that is
/// not offered is never replaced by another one without a word.
pub fn choose(
    options: &Value,
    models: &Value,
    model: &str,
    effort: &str,
) -> Result<Choice, String> {
    use crate::schema::{matching, offered, option};
    let (model, effort) = (model.trim(), effort.trim());
    let mut choice = Choice::default();
    let thoughts = option(options, "thought_level");
    let mut folded = None;
    if !model.is_empty() {
        if let Some(models_offered) = option(options, "model") {
            let id = models_offered["id"].as_str().unwrap_or("model").to_string();
            let direct = matching(models_offered, model);
            let split = || {
                let levels = thoughts.map(offered).unwrap_or_default();
                levels.into_iter().find_map(|(level, _)| {
                    let cut = model.len().checked_sub(level.len() + 1)?;
                    let (base, tail) = (model.get(..cut)?, model.get(cut..)?);
                    let named = tail.strip_prefix('-')?.eq_ignore_ascii_case(&level);
                    named
                        .then(|| matching(models_offered, base))
                        .flatten()
                        .map(|value| (value, level))
                })
            };
            match direct
                .map(|value| (value, None))
                .or_else(|| split().map(|(value, level)| (value, Some(level))))
            {
                Some((value, level)) => {
                    choice.model = Some((id, value));
                    folded = level;
                }
                None => {
                    let levels = thoughts.map(listed).filter(|levels| !levels.is_empty());
                    return Err(format!(
                        "Antigravity does not offer the model {model} over ACP; it offers {}{}.",
                        listed(models_offered),
                        levels.map_or_else(String::new, |levels| format!(
                            ", each with a thinking level of {levels}"
                        )),
                    ));
                }
            }
        } else if let Some(available) = models["availableModels"].as_array() {
            let found = available.iter().find_map(|offer| {
                let id = offer["modelId"].as_str()?;
                let name = offer["name"].as_str().unwrap_or(id);
                (id.eq_ignore_ascii_case(model) || name.eq_ignore_ascii_case(model))
                    .then(|| id.to_string())
            });
            let Some(id) = found else {
                let ids: Vec<&str> = available
                    .iter()
                    .filter_map(|offer| offer["modelId"].as_str())
                    .collect();
                return Err(format!(
                    "Antigravity does not offer the model {model} over ACP; it offers {}.",
                    ids.join(", ")
                ));
            };
            choice.model = Some((String::new(), id));
        } else {
            return Err(format!(
                "Antigravity offers no choice of model over ACP, so {model} cannot be used."
            ));
        }
    }
    // The thinking level a model's id carries is the one asked for; else the effort named.
    let level = folded.or_else(|| (!effort.is_empty()).then(|| effort.to_string()));
    if let Some(level) = level {
        match thoughts {
            Some(offered_levels) => match matching(offered_levels, &level) {
                Some(value) => {
                    let id = offered_levels["id"].as_str().unwrap_or("thought_level").to_string();
                    choice.thought = Some((id, value));
                }
                None => choice.notes.push(format!(
                    "Antigravity does not offer {level} as a thinking level (it offers {}); it thinks as it chooses.",
                    listed(offered_levels)
                )),
            },
            None => choice.notes.push(format!(
                "Antigravity offers no choice of thinking level; {level} was not applied."
            )),
        }
    }
    Ok(choice)
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
            silence_ms: 250,
            uuid: "u-1".into(),
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

    fn acp_options() -> Value {
        json!([
            {"id": "model", "category": "model", "currentValue": "gemini-4-neon", "options": [
                {"value": "gemini-4-neon", "name": "Gemini 4 Neon"},
                {"value": "gemini-3.8-flash", "name": "Gemini 3.8 Flash"},
                {"value": "claude-sonnet-x", "name": "Claude Sonnet X"}]},
            {"id": "thought_level", "category": "thought_level", "currentValue": "medium",
             "options": [{"value": "low"}, {"value": "medium"}, {"value": "high"}]}
        ])
    }

    #[test]
    fn an_id_with_its_effort_is_the_model_offered_and_its_thinking_level() {
        let options = acp_options();
        let chosen = choose(&options, &Value::Null, "gemini-3.8-flash-low", "").unwrap();
        assert_eq!(
            chosen.model,
            Some(("model".into(), "gemini-3.8-flash".into()))
        );
        assert_eq!(chosen.thought, Some(("thought_level".into(), "low".into())));
        assert!(chosen.notes.is_empty());
        // A model offered as it is, with the effort named apart.
        let plain = choose(&options, &Value::Null, "Claude Sonnet X", "HIGH").unwrap();
        assert_eq!(
            plain.model,
            Some(("model".into(), "claude-sonnet-x".into()))
        );
        assert_eq!(plain.thought, Some(("thought_level".into(), "high".into())));
        // The level in the id wins over an effort named beside it.
        let both = choose(&options, &Value::Null, "gemini-3.8-flash-high", "low").unwrap();
        assert_eq!(both.thought, Some(("thought_level".into(), "high".into())));
        // Nothing asked: nothing set.
        assert_eq!(
            choose(&options, &Value::Null, "", "").unwrap(),
            Choice::default()
        );
    }

    #[test]
    fn a_model_not_offered_is_refused_with_what_is_offered() {
        let options = acp_options();
        for asked in [
            "gemini-9-ultra",
            "gemini-3.8-flash-extreme",
            "gemini-3.8-flash-",
        ] {
            let refused = choose(&options, &Value::Null, asked, "").unwrap_err();
            assert!(refused.contains(asked), "{refused}");
            assert!(
                refused.contains("gemini-4-neon, gemini-3.8-flash, claude-sonnet-x"),
                "{refused}"
            );
            assert!(refused.contains("low, medium, high"), "{refused}");
        }
        let older = json!({"availableModels": [{"modelId": "m-1", "name": "One"}], "currentModelId": "m-1"});
        assert_eq!(
            choose(&Value::Null, &older, "one", "").unwrap().model,
            Some((String::new(), "m-1".into()))
        );
        assert!(
            choose(&Value::Null, &older, "m-2", "")
                .unwrap_err()
                .contains("m-1")
        );
        assert!(choose(&Value::Null, &Value::Null, "m-2", "").is_err());
        // An effort that is not offered is said, and the model still runs.
        let noted = choose(&options, &Value::Null, "gemini-4-neon", "max").unwrap();
        assert_eq!(noted.thought, None);
        assert_eq!(noted.notes.len(), 1);
    }
}
