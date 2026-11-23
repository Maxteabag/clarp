//! Where Antigravity over ACP keeps things on this machine, as the environment says: a profile
//! of its own where one is named, the store of its conversations, and the marks of the ones it
//! can no longer continue.

use std::path::PathBuf;

/// The profile the environment names, when it names one.
pub fn profile() -> Option<PathBuf> {
    std::env::var_os(super::PROFILE_ENV)
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

/// Where this machine's Antigravity keeps its conversations: the profile's store, or the one
/// under the user's home.
pub fn here() -> PathBuf {
    let home = std::env::var_os("HOME").unwrap_or_default();
    super::own_dir(std::path::Path::new(&home), profile().as_deref())
}
