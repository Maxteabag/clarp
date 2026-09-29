//! Durable user settings: one JSON object in the user config directory,
//! standing in for the C++ client's QSettings keys (same key names).
//! `CLARP_SETTINGS=off` keeps settings in memory only (tests, probes).

use std::path::PathBuf;
use std::sync::LazyLock;

use fancy_regex::Regex;
use serde_json::{Map, Value};

#[derive(Debug, Default)]
pub struct Settings {
    path: Option<PathBuf>,
    values: Map<String, Value>,
}

impl Settings {
    pub fn in_memory() -> Self {
        Self::default()
    }

    pub fn at(path: impl Into<PathBuf>) -> Self {
        let path = path.into();
        let values = std::fs::read_to_string(&path)
            .ok()
            .and_then(|text| serde_json::from_str::<Value>(&text).ok())
            .and_then(|value| value.as_object().cloned())
            .unwrap_or_default();
        Self { path: Some(path), values }
    }

    /// The user's settings, honouring `CLARP_SETTINGS` (a path, or `off`).
    pub fn user() -> Self {
        match std::env::var("CLARP_SETTINGS") {
            Ok(value) if value == "off" => Self::in_memory(),
            Ok(value) if !value.is_empty() => Self::at(value),
            _ => match config_home() {
                Some(config) => Self::at(config.join("MaxTeaBag").join("ClarpRust").join("settings.json")),
                None => Self::in_memory(),
            },
        }
    }

    pub fn get(&self, key: &str) -> Option<&Value> {
        self.values.get(key)
    }

    pub fn string(&self, key: &str, fallback: &str) -> String {
        self.get(key).and_then(Value::as_str).unwrap_or(fallback).to_owned()
    }

    pub fn boolean(&self, key: &str, fallback: bool) -> bool {
        self.get(key).and_then(Value::as_bool).unwrap_or(fallback)
    }

    pub fn integer(&self, key: &str, fallback: i64) -> i64 {
        self.get(key).and_then(Value::as_i64).unwrap_or(fallback)
    }

    /// Set and save. A failed save is reported, never silently dropped.
    pub fn set(&mut self, key: &str, value: impl Into<Value>) {
        self.values.insert(key.to_owned(), value.into());
        self.save();
    }

    pub fn remove(&mut self, key: &str) {
        if self.values.remove(key).is_some() {
            self.save();
        }
    }

    fn save(&self) {
        let Some(path) = &self.path else { return };
        let result = (|| {
            if let Some(parent) = path.parent() {
                std::fs::create_dir_all(parent)?;
            }
            let mut temporary = path.clone().into_os_string();
            temporary.push(".tmp");
            std::fs::write(&temporary, Value::Object(self.values.clone()).to_string())?;
            std::fs::rename(&temporary, path)
        })();
        if let Err(error) = result {
            eprintln!("settings: could not save {}: {error}", path.display());
        }
    }
}

pub fn config_home() -> Option<PathBuf> {
    std::env::var_os("XDG_CONFIG_HOME")
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("HOME").map(|home| PathBuf::from(home).join(".config")))
}

/// Trim and drop trailing slashes; empty means the local default Host.
pub fn normalized_base_url(value: &str) -> String {
    let trimmed = value.trim().trim_end_matches('/');
    if trimmed.is_empty() { "http://127.0.0.1:7682".into() } else { trimmed.to_owned() }
}

/// The Host token from `~/.config/clarp/config.toml` (`auth_token = "..."`),
/// used when the Host runs on this machine.
pub fn token_from_config(contents: &str) -> String {
    static TOKEN: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r#"(?m)^\s*auth_token\s*=\s*"([^"]*)""#).expect("pattern"));
    TOKEN
        .captures(contents)
        .ok()
        .flatten()
        .and_then(|caps| caps.get(1).map(|m| m.as_str().to_owned()))
        .unwrap_or_default()
}

pub fn default_token() -> String {
    config_home()
        .and_then(|config| std::fs::read_to_string(config.join("clarp").join("config.toml")).ok())
        .map(|contents| token_from_config(&contents))
        .unwrap_or_default()
}

/// The settings group for one chat's composer on one Host (C++
/// `draftScopeSettingsKey`): the SHA-256 of `base\0session`, so drafts never
/// leak between Hosts or conversations.
pub fn draft_scope_key(base_url: &str, session: &str) -> String {
    use sha2::{Digest, Sha256};
    let mut hasher = Sha256::new();
    hasher.update(normalized_base_url(base_url).as_bytes());
    hasher.update([0u8]);
    hasher.update(session.as_bytes());
    let digest: String = hasher.finalize().iter().map(|b| format!("{b:02x}")).collect();
    format!("composerDrafts/{digest}")
}

/// Per-Host flag: this machine shares the Host's filesystem (C++
/// `sharedFilesystemSettingsKey`).
pub fn shared_filesystem_key(base_url: &str) -> String {
    use sha2::{Digest, Sha256};
    let digest: String = Sha256::digest(normalized_base_url(base_url).as_bytes()).iter().map(|b| format!("{b:02x}")).collect();
    format!("connections/{digest}/sharedFilesystem")
}
