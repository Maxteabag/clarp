//! Where the desktop keeps its files. The XDG variables win on every
//! platform (the checks point them at scratch folders); without them Linux
//! uses the XDG defaults under `HOME`, and macOS folders of the desktop's
//! own in `~/Library`: never `Application Support/Clarp`, which is the Clarp
//! server's install (macOS paths ignore case, so `clarp` is that too).

use std::path::{Path, PathBuf};

/// The desktop's folder name under `~/Library/Application Support` and
/// `~/Library/Caches`.
pub const MACOS_FOLDER: &str = "com.maxteabag.Clarp.desktop";

/// `linux` under `HOME` on Linux, `macos` on macOS.
fn per_platform(linux: &str, macos: &str) -> String {
    if cfg!(target_os = "macos") { format!("{macos}/{MACOS_FOLDER}") } else { linux.to_owned() }
}

fn under(variable: &str, linux: &str, macos: &str) -> Option<PathBuf> {
    std::env::var_os(variable)
        .filter(|dir| !dir.is_empty())
        .map(PathBuf::from)
        .or_else(|| home().map(|home| home.join(per_platform(linux, macos))))
}

fn home() -> Option<PathBuf> {
    std::env::var_os("HOME").filter(|home| !home.is_empty()).map(PathBuf::from)
}

/// `~/.config`, `~/Library/Application Support/com.maxteabag.Clarp.desktop`.
pub fn config_home() -> Option<PathBuf> {
    under("XDG_CONFIG_HOME", ".config", "Library/Application Support")
}

/// `~/.cache`, `~/Library/Caches/com.maxteabag.Clarp.desktop`.
pub fn cache_home() -> Option<PathBuf> {
    under("XDG_CACHE_HOME", ".cache", "Library/Caches")
}

/// `~/.local/share`, `~/Library/Application Support/com.maxteabag.Clarp.desktop`.
pub fn data_home() -> Option<PathBuf> {
    under("XDG_DATA_HOME", ".local/share", "Library/Application Support")
}

/// `~/.local/state`, `~/Library/Application Support/com.maxteabag.Clarp.desktop`.
pub fn state_home() -> Option<PathBuf> {
    under("XDG_STATE_HOME", ".local/state", "Library/Application Support")
}

/// The Slint desktop's settings and pane layouts, apart from the Qt apps':
/// `~/.config/MaxTeaBag/ClarpSlint`, on macOS the desktop's own folder.
pub fn app_config_dir() -> Option<PathBuf> {
    let config = config_home()?;
    Some(if cfg!(target_os = "macos") { config } else { config.join("MaxTeaBag").join("ClarpSlint") })
}

/// Early macOS builds kept settings and layouts in
/// `~/Library/Application Support/clarp`, which is the server's install
/// folder: moves them to `app_config_dir()` once. What it did, for the log.
pub fn move_out_of_the_server_folder() -> Vec<String> {
    if !cfg!(target_os = "macos") || std::env::var_os("XDG_CONFIG_HOME").is_some_and(|v| !v.is_empty()) {
        return Vec::new();
    }
    match (home(), app_config_dir()) {
        (Some(home), Some(current)) => move_desktop_files(&home.join("Library/Application Support/clarp"), &current),
        _ => Vec::new(),
    }
}

/// Moves `settings.json` and `workspaces.json` from `legacy` to `current`
/// when `current` has none and the file is the desktop's: a JSON object,
/// and for settings one whose keys are all `section/name`. Their lock files
/// in `legacy` go too; nothing else there is touched.
pub fn move_desktop_files(legacy: &Path, current: &Path) -> Vec<String> {
    let mut report = Vec::new();
    for name in ["settings.json", "workspaces.json"] {
        let from = legacy.join(name);
        let to = current.join(name);
        if !from.is_file() || to.exists() {
            continue;
        }
        let ours = std::fs::read_to_string(&from).ok().and_then(|text| serde_json::from_str::<serde_json::Value>(&text).ok()).is_some_and(|value| {
            value.as_object().is_some_and(|object| name != "settings.json" || object.keys().all(|key| key.contains('/')))
        });
        if !ours {
            report.push(format!("left {}: not the desktop's", from.display()));
            continue;
        }
        let moved = std::fs::create_dir_all(current).and_then(|()| std::fs::rename(&from, &to));
        match moved {
            Ok(()) => {
                report.push(format!("moved {} to {}", from.display(), to.display()));
                let mut lock = from.into_os_string();
                lock.push(".lock");
                match std::fs::remove_file(&lock) {
                    Err(error) if error.kind() != std::io::ErrorKind::NotFound => {
                        report.push(format!("could not remove {}: {error}", Path::new(&lock).display()));
                    }
                    _ => {}
                }
            }
            Err(error) => report.push(format!("could not move {} to {}: {error}", from.display(), to.display())),
        }
    }
    report
}

#[cfg(test)]
mod tests {
    use super::move_desktop_files;

    fn scratch(name: &str) -> std::path::PathBuf {
        let dir = std::env::temp_dir().join(format!("clarp-dirs-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join("legacy")).unwrap();
        dir
    }

    #[test]
    fn the_desktops_files_leave_the_server_folder_and_the_server_keeps_its_own() {
        let dir = scratch("move");
        let (legacy, current) = (dir.join("legacy"), dir.join("current"));
        std::fs::write(legacy.join("settings.json"), r#"{"connection/baseUrl": "https://host", "appearance/uiScale": 1.2}"#).unwrap();
        std::fs::write(legacy.join("settings.json.lock"), "").unwrap();
        std::fs::write(legacy.join("workspaces.json"), r#"{"layouts": {}}"#).unwrap();
        std::fs::write(legacy.join("install.json"), "{}").unwrap();
        let report = move_desktop_files(&legacy, &current);
        assert_eq!(report.len(), 2, "{report:?}");
        assert!(std::fs::read_to_string(current.join("settings.json")).unwrap().contains("https://host"));
        assert!(current.join("workspaces.json").is_file());
        for gone in ["settings.json", "settings.json.lock", "workspaces.json"] {
            assert!(!legacy.join(gone).exists(), "{gone}");
        }
        assert!(legacy.join("install.json").is_file(), "the server's own files stay");
        assert!(move_desktop_files(&legacy, &current).is_empty(), "once");
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn a_settings_file_that_is_not_the_desktops_or_already_moved_stays() {
        let dir = scratch("keep");
        let (legacy, current) = (dir.join("legacy"), dir.join("current"));
        std::fs::write(legacy.join("settings.json"), r#"{"port": 7682}"#).unwrap();
        assert_eq!(move_desktop_files(&legacy, &current).len(), 1);
        assert!(legacy.join("settings.json").is_file() && !current.exists());
        std::fs::write(legacy.join("settings.json"), r#"{"connection/baseUrl": "https://old"}"#).unwrap();
        std::fs::create_dir_all(&current).unwrap();
        std::fs::write(current.join("settings.json"), r#"{"connection/baseUrl": "https://new"}"#).unwrap();
        assert!(move_desktop_files(&legacy, &current).is_empty());
        assert!(std::fs::read_to_string(current.join("settings.json")).unwrap().contains("https://new"));
        std::fs::remove_dir_all(&dir).unwrap();
    }
}
