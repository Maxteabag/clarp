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

    /// The file the settings are kept in (None in memory).
    pub fn path(&self) -> Option<&std::path::Path> {
        self.path.as_deref()
    }

    /// Reads the file again (someone edited it, or another window saved):
    /// the keys whose values changed, or why the file cannot be read. A
    /// file that is not a JSON object keeps the current values.
    pub fn reload(&mut self) -> Result<Vec<String>, String> {
        let Some(path) = self.path.clone() else { return Ok(Vec::new()) };
        let text = match std::fs::read_to_string(&path) {
            Ok(text) => text,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => String::from("{}"),
            Err(error) => return Err(format!("cannot read {}: {error}", path.display())),
        };
        let fresh = match serde_json::from_str::<Value>(&text) {
            Ok(Value::Object(fresh)) => fresh,
            Ok(_) => return Err(format!("{} is not a JSON object; keeping the settings as they were", path.display())),
            Err(error) => return Err(format!("{} is not valid JSON ({error}); keeping the settings as they were", path.display())),
        };
        let mut changed: Vec<String> = fresh.iter().filter(|(k, v)| self.values.get(*k) != Some(*v)).map(|(k, _)| k.clone()).collect();
        changed.extend(self.values.keys().filter(|k| !fresh.contains_key(*k)).cloned());
        self.values = fresh;
        Ok(changed)
    }

    /// Every stored value.
    pub fn values(&self) -> &Map<String, Value> {
        &self.values
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
        let value = value.into();
        self.values.insert(key.to_owned(), value.clone());
        self.save(key, Some(value));
    }

    pub fn remove(&mut self, key: &str) {
        self.values.remove(key);
        self.save(key, None);
    }

    fn read(path: &std::path::Path) -> Map<String, Value> {
        std::fs::read_to_string(path)
            .ok()
            .and_then(|text| serde_json::from_str::<Value>(&text).ok())
            .and_then(|value| value.as_object().cloned())
            .unwrap_or_default()
    }

    /// Writes one change. Other windows share the file, so this rereads it
    /// under a lock and applies only this key: saving the whole in-memory
    /// copy would erase what another window saved meanwhile. What the
    /// others saved is picked up on the way.
    fn save(&mut self, key: &str, value: Option<Value>) {
        let Some(path) = self.path.clone() else { return };
        let result = (|| -> std::io::Result<()> {
            if let Some(parent) = path.parent() {
                std::fs::create_dir_all(parent)?;
            }
            let mut lock_path = path.clone().into_os_string();
            lock_path.push(".lock");
            let lock = std::fs::File::options().create(true).truncate(false).write(true).open(&lock_path)?;
            lock.lock()?;
            let mut current = Self::read(&path);
            match &value {
                Some(value) => current.insert(key.to_owned(), value.clone()),
                None => current.remove(key),
            };
            let mut temporary = path.clone().into_os_string();
            temporary.push(format!(".tmp.{}", std::process::id()));
            // Pretty, so the file reads well in an editor.
            let text = serde_json::to_string_pretty(&Value::Object(current.clone())).map_err(std::io::Error::other)?;
            std::fs::write(&temporary, text + "\n")?;
            std::fs::rename(&temporary, &path)?;
            self.values = current;
            Ok(())
        })();
        if let Err(error) = result {
            eprintln!("settings: could not save {}: {error}", path.display());
        }
    }
}

pub fn config_home() -> Option<PathBuf> {
    crate::dirs::config_home()
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

/// Whether `base_url` names this machine (`localhost` or a loopback address).
pub fn is_loopback_url(base_url: &str) -> bool {
    let Ok(url) = url::Url::parse(base_url) else { return false };
    match url.host() {
        Some(url::Host::Domain(name)) => name.eq_ignore_ascii_case("localhost"),
        Some(url::Host::Ipv4(ip)) => ip.is_loopback(),
        Some(url::Host::Ipv6(ip)) => ip.is_loopback(),
        None => false,
    }
}

/// The administrator token in config.toml belongs to the Host on this
/// machine. Sending it to a remote Host would leak it and would hide that
/// Host's own paired device credential, so only loopback URLs get it
/// (C++ `defaultToken(baseUrl)`).
pub fn default_token(base_url: &str) -> String {
    if !is_loopback_url(base_url) {
        return String::new();
    }
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

/// Where the last revision read in a pair room is kept, per Host.
pub fn agent_conversation_seen_key(base_url: &str, conversation_id: &str) -> String {
    use sha2::{Digest, Sha256};
    let short = |text: &str| Sha256::digest(text.as_bytes()).iter().take(8).map(|b| format!("{b:02x}")).collect::<String>();
    format!("agentConversations/{}/{}/seenRevision", short(base_url), short(conversation_id))
}
