//! Where the desktop keeps its files. The XDG variables win on every
//! platform (the checks point them at scratch folders); without them Linux
//! uses the XDG defaults under `HOME` and macOS its Library folders.

use std::path::PathBuf;

/// `linux` under `HOME` on Linux, `macos` on macOS.
const fn per_platform(linux: &'static str, macos: &'static str) -> &'static str {
    if cfg!(target_os = "macos") { macos } else { linux }
}

fn under(variable: &str, home_relative: &str) -> Option<PathBuf> {
    std::env::var_os(variable)
        .filter(|dir| !dir.is_empty())
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("HOME").filter(|home| !home.is_empty()).map(|home| PathBuf::from(home).join(home_relative)))
}

/// `~/.config`, `~/Library/Application Support`.
pub fn config_home() -> Option<PathBuf> {
    under("XDG_CONFIG_HOME", per_platform(".config", "Library/Application Support"))
}

/// `~/.cache`, `~/Library/Caches`.
pub fn cache_home() -> Option<PathBuf> {
    under("XDG_CACHE_HOME", per_platform(".cache", "Library/Caches"))
}

/// `~/.local/share`, `~/Library/Application Support`.
pub fn data_home() -> Option<PathBuf> {
    under("XDG_DATA_HOME", per_platform(".local/share", "Library/Application Support"))
}

/// `~/.local/state`, `~/Library/Application Support`.
pub fn state_home() -> Option<PathBuf> {
    under("XDG_STATE_HOME", per_platform(".local/state", "Library/Application Support"))
}

/// The Slint desktop's settings and pane layouts, apart from the Qt apps':
/// `~/.config/MaxTeaBag/ClarpSlint`, `~/Library/Application Support/clarp`.
pub fn app_config_dir() -> Option<PathBuf> {
    Some(config_home()?.join(per_platform("MaxTeaBag/ClarpSlint", "clarp")))
}

