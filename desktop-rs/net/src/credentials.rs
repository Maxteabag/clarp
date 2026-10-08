//! The desktop keyring that holds device tokens, one per Host: the
//! freedesktop Secret Service on Linux, the login Keychain on macOS. Both
//! offer the same three calls, and neither ever asks the user anything.

pub const APPLICATION: &str = "com.maxteabag.Clarp";

#[cfg(target_os = "linux")]
mod secret_service;
#[cfg(target_os = "linux")]
pub use secret_service::{lookup, remove, store};

#[cfg(target_os = "macos")]
mod keychain;
#[cfg(target_os = "macos")]
pub use keychain::{lookup, remove, store};

/// Where the token is kept, for messages.
pub const KEYRING_NAME: &str = if cfg!(target_os = "macos") { "the macOS Keychain" } else { "the Secret Service" };
