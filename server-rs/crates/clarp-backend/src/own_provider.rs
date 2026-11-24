//! A Codex agent bound to a model provider of its own.
//!
//! Such an agent runs every turn with a Codex home of its own (`CODEX_HOME`), whose
//! `config.toml` defines the provider, and never on the machine's own Codex configuration.
//! Codex 0.167.1 reads `model_provider` and `model_providers` only from the `config.toml` of
//! its home: in a project's `.codex/config.toml` it leaves them out and runs on its default
//! provider without a word (seen on 24 Nov 2026). So the home is the only place the provider
//! can be set, and the Host holds the agent to it twice:
//!
//! - before a turn starts, the home's `config.toml` must name the agent's provider as
//!   `model_provider` and define it under `[model_providers.<name>]` ([`OwnProvider::admits`]).
//!   The turn is also started with `model_provider` set on the command line or in the lane's
//!   thread, so that nothing in a profile or a project can move it elsewhere;
//! - once Codex runs, the provider it names for its thread must be the agent's
//!   ([`named_providers`] for exec, the lane's own check for the app-server). Any other ends
//!   the turn at once.
//!
//! Either refusal ends the turn as [`failures::PROVIDER_MISMATCH`], in plain words, and the
//! turn is not tried again.

use std::io::{BufRead as _, Read as _, Seek as _, SeekFrom};
use std::path::{Path, PathBuf};

use clarp_kernel::failures;
use serde_json::Value;

/// How much of a rollout's end is read for the settings its turns ran with.
const TAIL: u64 = 256 * 1024;

/// Where an agent's Codex lives, and the provider it must run on.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OwnProvider {
    /// The agent's Codex home: what `CODEX_HOME` is set to for its turns.
    pub home: PathBuf,
    /// The provider, by the name its `config.toml` defines it under.
    pub provider: String,
}

/// Whether a provider name is one Codex's configuration can hold as a table key and the Host
/// can pass on a command line: letters, digits, `-` and `_`.
pub fn valid_name(provider: &str) -> bool {
    !provider.is_empty()
        && provider.len() <= 128
        && provider
            .chars()
            .all(|sign| sign.is_ascii_alphanumeric() || sign == '-' || sign == '_')
}

impl OwnProvider {
    /// The agent's provider, where both halves are given and well formed. `Err`: why not, in
    /// words for whoever set them.
    pub fn new(home: &str, provider: &str) -> Result<Self, String> {
        let home = home.trim();
        let provider = provider.trim();
        if home.is_empty() || provider.is_empty() {
            return Err("codex_home and codex_provider are set together, or neither".into());
        }
        let path = PathBuf::from(home);
        if !path.is_absolute() {
            return Err("codex_home must be an absolute directory".into());
        }
        if !valid_name(provider) {
            return Err(
                "codex_provider must be a provider name of letters, digits, '-' or '_'".into(),
            );
        }
        Ok(Self {
            home: path,
            provider: provider.to_string(),
        })
    }

    /// Where Codex keeps this agent's rollouts.
    pub fn sessions(&self) -> PathBuf {
        self.home.join("sessions")
    }

    /// Whether a turn may start: the home's `config.toml` names this provider as
    /// `model_provider` and defines it. `Err`: the words the turn ends with, of the kind
    /// [`failures::PROVIDER_MISMATCH`].
    pub fn admits(&self) -> Result<(), String> {
        let refused = |why: String| Err(failures::provider_mismatch(&why));
        let file = self.home.join("config.toml");
        let text = match std::fs::read_to_string(&file) {
            Ok(text) => text,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                return refused(format!(
                    "the agent's Codex home has no config.toml ({})",
                    file.display()
                ));
            }
            Err(error) => {
                return refused(format!(
                    "the agent's Codex config.toml could not be read ({}): {error}",
                    file.display()
                ));
            }
        };
        let Ok(config) = text.parse::<toml::Table>() else {
            return refused(format!(
                "the agent's Codex config.toml is not valid TOML ({})",
                file.display()
            ));
        };
        let wanted = self.provider.as_str();
        match config.get("model_provider") {
            Some(toml::Value::String(named)) if named == wanted => {}
            Some(toml::Value::String(named)) => {
                return refused(format!(
                    "the agent's Codex config.toml sets model_provider = \"{named}\", not \"{wanted}\""
                ));
            }
            _ => {
                return refused(format!(
                    "the agent's Codex config.toml does not set model_provider = \"{wanted}\""
                ));
            }
        }
        let defined = config
            .get("model_providers")
            .and_then(toml::Value::as_table)
            .and_then(|providers| providers.get(wanted))
            .is_some_and(toml::Value::is_table);
        if !defined {
            return refused(format!(
                "the agent's Codex config.toml does not define [model_providers.{wanted}]"
            ));
        }
        Ok(())
    }

    /// The words a turn ends with whose Codex said it runs on `named`.
    pub fn ran_on(&self, named: &str) -> String {
        failures::provider_mismatch(&format!(
            "Codex reported model provider \"{named}\" for this turn, not \"{}\"; the turn was stopped",
            self.provider
        ))
    }
}

/// Every provider a Codex rollout names for its thread: the one it was begun on
/// (`session_meta`), and the one each turn's settings name, of what its end holds. Empty when
/// the rollout cannot be read or names none yet.
pub fn named_providers(rollout: &Path) -> Vec<String> {
    let Ok(mut file) = std::fs::File::open(rollout) else {
        return Vec::new();
    };
    let mut named = Vec::new();
    let mut first = String::new();
    if std::io::BufReader::new(&file).read_line(&mut first).is_ok()
        && let Ok(entry) = serde_json::from_str::<Value>(&first)
    {
        named.extend(provider_of(&entry));
    }
    let size = file.metadata().map_or(0, |meta| meta.len());
    let from = size.saturating_sub(TAIL);
    let mut tail = Vec::new();
    if file.seek(SeekFrom::Start(from)).is_ok() && file.read_to_end(&mut tail).is_ok() {
        for line in tail.split(|byte| *byte == b'\n') {
            let mentions = line.windows(14).any(|w| w == b"model_provider");
            if let Some(entry) = mentions
                .then(|| serde_json::from_slice::<Value>(line).ok())
                .flatten()
            {
                named.extend(provider_of(&entry));
            }
        }
    }
    named.sort();
    named.dedup();
    named
}

/// The provider one entry of a rollout names: a thread's beginning, or the settings a turn
/// was given.
fn provider_of(entry: &Value) -> Option<String> {
    let payload = &entry["payload"];
    let named = match (entry["type"].as_str(), payload["type"].as_str()) {
        (Some("session_meta"), _) => &payload["model_provider"],
        (Some("event_msg"), Some("thread_settings_applied")) => {
            &payload["thread_settings"]["model_provider_id"]
        }
        (Some("turn_context"), _) => &payload["model_provider"],
        _ => return None,
    };
    named
        .as_str()
        .filter(|named| !named.is_empty())
        .map(str::to_string)
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    /// A Codex home of its own in a directory nobody else uses, with this `config.toml`
    /// (none: `None`). The provider is made up.
    fn home(config: Option<&str>) -> (PathBuf, OwnProvider) {
        let dir = std::env::temp_dir().join(format!(
            "clarp-own-provider-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        if let Some(config) = config {
            std::fs::write(dir.join("config.toml"), config).unwrap();
        }
        let own = OwnProvider::new(dir.to_str().unwrap(), "stand-in").unwrap();
        (dir, own)
    }

    const MATCHING: &str = r#"
model_provider = "stand-in"
model = "made-up-model"

[model_providers.stand-in]
name = "A stand-in"
base_url = "http://127.0.0.1:9/v1"
wire_api = "responses"
"#;

    #[test]
    fn a_home_whose_config_names_and_defines_the_provider_is_admitted() {
        let (dir, own) = home(Some(MATCHING));
        assert_eq!(own.admits(), Ok(()));
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn a_home_with_no_config_is_refused_as_a_provider_mismatch() {
        let (dir, own) = home(None);
        let why = own.admits().unwrap_err();
        assert_eq!(failures::classify_error(&why), failures::PROVIDER_MISMATCH);
        assert!(why.contains("has no config.toml"), "{why}");
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn a_config_that_names_no_provider_or_another_is_refused() {
        let (dir, own) = home(Some("model = \"made-up-model\"\n"));
        let why = own.admits().unwrap_err();
        assert_eq!(failures::classify_error(&why), failures::PROVIDER_MISMATCH);
        assert!(
            why.contains("does not set model_provider = \"stand-in\""),
            "{why}"
        );
        let _ = std::fs::remove_dir_all(dir);

        let other = MATCHING.replace(
            "model_provider = \"stand-in\"",
            "model_provider = \"openai\"",
        );
        let (dir, own) = home(Some(&other));
        let why = own.admits().unwrap_err();
        assert_eq!(failures::classify_error(&why), failures::PROVIDER_MISMATCH);
        assert!(
            why.contains("sets model_provider = \"openai\", not \"stand-in\""),
            "{why}"
        );
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn a_config_that_names_the_provider_without_defining_it_is_refused() {
        let (dir, own) = home(Some("model_provider = \"stand-in\"\n"));
        let why = own.admits().unwrap_err();
        assert_eq!(failures::classify_error(&why), failures::PROVIDER_MISMATCH);
        assert!(
            why.contains("does not define [model_providers.stand-in]"),
            "{why}"
        );
        let _ = std::fs::remove_dir_all(dir);

        let (dir, own) = home(Some("model_provider = \"stand-in\"\n[[oops\n"));
        let why = own.admits().unwrap_err();
        assert!(why.contains("is not valid TOML"), "{why}");
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn the_two_halves_come_together_and_well_formed() {
        assert!(OwnProvider::new("", "").is_err());
        assert!(OwnProvider::new("/somewhere", "").is_err());
        assert!(OwnProvider::new("", "stand-in").is_err());
        assert!(OwnProvider::new("relative/home", "stand-in").is_err());
        assert!(OwnProvider::new("/somewhere", "with space").is_err());
        assert!(OwnProvider::new("/somewhere", "quote\"d").is_err());
        let own = OwnProvider::new(" /somewhere ", " stand_in-2 ").unwrap();
        assert_eq!(own.home, PathBuf::from("/somewhere"));
        assert_eq!(own.provider, "stand_in-2");
        assert_eq!(own.sessions(), PathBuf::from("/somewhere/sessions"));
    }

    #[test]
    fn a_rollout_names_the_providers_its_thread_and_its_turns_ran_on() {
        let (dir, own) = home(None);
        let rollout = dir.join("rollout.jsonl");
        let lines = [
            r#"{"type":"session_meta","payload":{"id":"t","model_provider":"stand-in"}}"#,
            r#"{"type":"event_msg","payload":{"type":"thread_settings_applied","thread_settings":{"model_provider_id":"stand-in"}}}"#,
            r#"{"type":"response_item","payload":{"type":"message"}}"#,
        ];
        std::fs::write(&rollout, lines.join("\n") + "\n").unwrap();
        assert_eq!(named_providers(&rollout), ["stand-in"]);
        let mut more = lines.join("\n");
        more.push_str("\n{\"type\":\"turn_context\",\"payload\":{\"model_provider\":\"openai\"}}\n");
        std::fs::write(&rollout, more).unwrap();
        assert_eq!(named_providers(&rollout), ["openai", "stand-in"]);
        assert!(named_providers(&dir.join("none.jsonl")).is_empty());
        assert_eq!(
            failures::classify_error(&own.ran_on("openai")),
            failures::PROVIDER_MISMATCH
        );
        let _ = std::fs::remove_dir_all(dir);
    }
}
