//! Device tokens, one per Host, stored in one of three configurable backends:
//!
//! - `keyring` (default): the freedesktop Secret Service on Linux, the login
//!   Keychain on macOS. Never prompts the user.
//! - `file`: a private `0600` JSON map (`credentials.json` in the desktop
//!   config folder, or a configured path).
//! - `command`: user-configured shell command(s) receiving `CLARP_SERVER_URL`,
//!   `CLARP_SERVER_HOST`, and `{url}`/`{host}` placeholders (with stored tokens
//!   passed over stdin).

use std::path::PathBuf;

use clarp_core::prefs;
use clarp_core::settings::Settings;

pub const APPLICATION: &str = "com.maxteabag.Clarp";

mod command_store;
mod file_store;

#[cfg(target_os = "linux")]
mod secret_service;
#[cfg(target_os = "linux")]
pub use secret_service::{lookup, remove, store};

#[cfg(target_os = "macos")]
mod keychain;
#[cfg(target_os = "macos")]
pub use keychain::{lookup, remove, store};

/// Where the OS keyring keeps tokens, for messages.
pub const KEYRING_NAME: &str = if cfg!(target_os = "macos") { "the macOS Keychain" } else { "the Secret Service" };

/// Configured credential storage backend.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Backend {
    /// OS keyring (Secret Service on Linux, login Keychain on macOS).
    Keyring,
    /// Private `0600` JSON file (`None` uses the default `credentials.json`).
    File { path: Option<PathBuf> },
    /// External shell command(s) for lookup and optional store/remove.
    Command {
        lookup: String,
        store: Option<String>,
        remove: Option<String>,
    },
}

fn env_non_empty(name: &str) -> Option<String> {
    std::env::var(name).ok().map(|v| v.trim().to_owned()).filter(|v| !v.is_empty())
}

impl Backend {
    /// The default OS keyring backend.
    pub fn keyring() -> Self {
        Self::Keyring
    }

    /// Resolves the backend from `settings`, honouring `CLARP_CREDENTIALS_*`
    /// environment overrides when set.
    pub fn from_settings(settings: &Settings) -> Self {
        let kind = env_non_empty("CLARP_CREDENTIALS_BACKEND").unwrap_or_else(|| prefs::choice_of(settings, "credentialsbackend"));
        match kind.to_ascii_lowercase().as_str() {
            "file" => {
                let raw = env_non_empty("CLARP_CREDENTIALS_FILE").unwrap_or_else(|| prefs::text_of(settings, "credentialsfile"));
                let path = (!raw.is_empty()).then(|| PathBuf::from(raw));
                Self::File { path }
            }
            "command" => {
                let lookup = env_non_empty("CLARP_CREDENTIALS_COMMAND").unwrap_or_else(|| prefs::text_of(settings, "credentialscommand"));
                let store = env_non_empty("CLARP_CREDENTIALS_STORE_COMMAND")
                    .or_else(|| {
                        let s = prefs::text_of(settings, "credentialsstorecommand");
                        (!s.is_empty()).then_some(s)
                    });
                let remove = env_non_empty("CLARP_CREDENTIALS_REMOVE_COMMAND")
                    .or_else(|| {
                        let s = prefs::text_of(settings, "credentialsremovecommand");
                        (!s.is_empty()).then_some(s)
                    });
                Self::Command { lookup, store, remove }
            }
            _ => Self::Keyring,
        }
    }

    /// Human-readable description of where tokens are kept, for messages.
    pub fn label(&self) -> String {
        match self {
            Self::Keyring => KEYRING_NAME.to_owned(),
            Self::File { path } => match file_store::resolve_path(path.as_deref()) {
                Ok(resolved) => format!("the credentials file ({})", resolved.display()),
                Err(_) => "the private credentials file".to_owned(),
            },
            Self::Command { .. } => "the configured credential command".to_owned(),
        }
    }

    /// Actionable hint when writing fails during pairing.
    pub fn unlock_hint(&self) -> &'static str {
        match self {
            Self::Keyring => {
                if cfg!(target_os = "macos") {
                    "over SSH the login Keychain is locked to this session: run `security unlock-keychain ~/Library/Keychains/login.keychain-db` (it asks for the Mac's login password), or run --pair in Terminal on the Mac"
                } else {
                    "unlock the desktop keyring (log in to the desktop session)"
                }
            }
            Self::File { .. } => "check permissions on the credentials file directory",
            Self::Command { .. } => "check credentials/storeCommand (and credentials/command) in your settings",
        }
    }

    /// Whether this backend can store new device tokens.
    pub fn supports_store(&self) -> bool {
        match self {
            Self::Keyring | Self::File { .. } => true,
            Self::Command { store, .. } => store.as_deref().is_some_and(|s| !s.trim().is_empty()),
        }
    }

    /// Whether pairing should probe writability with a throwaway item before
    /// spending the one-time pairing code.
    pub fn needs_write_probe(&self) -> bool {
        matches!(self, Self::Keyring | Self::File { .. })
    }

    /// Looks up the device token for `server_url`, returning an empty string
    /// when none is found or the backend fails.
    pub async fn lookup(&self, server_url: &str) -> String {
        match self {
            Self::Keyring => lookup(server_url).await,
            Self::File { path } => file_store::lookup(path.clone(), server_url).await,
            Self::Command { lookup, .. } => command_store::lookup(lookup.clone(), server_url).await,
        }
    }

    /// Stores (replacing) the device token for `server_url`.
    pub async fn store(&self, server_url: &str, token: &str) -> Result<(), String> {
        match self {
            Self::Keyring => store(server_url, token).await,
            Self::File { path } => file_store::store(path.clone(), server_url, token).await,
            Self::Command { store, .. } => command_store::store(store.clone(), server_url, token).await,
        }
    }

    /// Removes the device token for `server_url`.
    pub async fn remove(&self, server_url: &str) -> Result<(), String> {
        match self {
            Self::Keyring => remove(server_url).await,
            Self::File { path } => file_store::remove(path.clone(), server_url).await,
            Self::Command { remove, .. } => command_store::remove(remove.clone(), server_url).await,
        }
    }
}
