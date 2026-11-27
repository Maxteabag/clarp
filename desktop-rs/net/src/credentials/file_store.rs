//! Device tokens in a private `0600` JSON file: a map from normalized Host
//! URL to device token. Written atomically via a temporary file in the same
//! directory so a crash or concurrent launch never leaves a half-written file.

use std::collections::BTreeMap;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Mutex;

use clarp_core::settings::normalized_base_url;

static FILE_LOCK: Mutex<()> = Mutex::new(());
static TEMP_COUNTER: AtomicU64 = AtomicU64::new(0);

/// Default path when `credentials/file` is empty: `credentials.json` in the
/// desktop's config directory.
pub fn default_path() -> Option<PathBuf> {
    clarp_core::dirs::app_config_dir().map(|dir| dir.join("credentials.json"))
}

/// Expands a leading `~/` against `HOME` and falls back to [`default_path`].
pub fn resolve_path(configured: Option<&Path>) -> Result<PathBuf, String> {
    match configured.filter(|p| !p.as_os_str().is_empty()) {
        Some(path) => {
            if let Some(raw) = path.to_str() {
                if let Some(rest) = raw.strip_prefix("~/") {
                    let home = clarp_core::dirs::home().ok_or_else(|| "HOME is not set; cannot expand ~ in credential file path".to_owned())?;
                    return Ok(home.join(rest));
                }
            }
            Ok(path.to_path_buf())
        }
        None => default_path().ok_or_else(|| "cannot determine config directory for credentials.json".to_owned()),
    }
}

fn read_map(path: &Path) -> Result<BTreeMap<String, String>, String> {
    let text = match std::fs::read_to_string(path) {
        Ok(text) => text,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(BTreeMap::new()),
        Err(error) => return Err(format!("cannot read {}: {error}", path.display())),
    };
    if text.trim().is_empty() {
        return Ok(BTreeMap::new());
    }
    serde_json::from_str::<BTreeMap<String, String>>(&text).map_err(|error| format!("{} is not a valid credentials map: {error}", path.display()))
}

fn write_map(path: &Path, map: &BTreeMap<String, String>) -> Result<(), String> {
    let parent = path.parent().ok_or_else(|| format!("{} has no parent directory", path.display()))?;
    std::fs::create_dir_all(parent).map_err(|error| format!("cannot create {}: {error}", parent.display()))?;
    let payload = serde_json::to_string_pretty(map).map_err(|error| format!("cannot encode credentials: {error}"))?;
    let seq = TEMP_COUNTER.fetch_add(1, Ordering::Relaxed);
    let file_name = path.file_name().and_then(|n| n.to_str()).unwrap_or("credentials.json");
    let temp = parent.join(format!(".{file_name}.tmp.{}.{seq}", std::process::id()));

    let write_result = (|| -> Result<(), String> {
        let mut options = std::fs::OpenOptions::new();
        options.write(true).create_new(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600);
        }
        let mut file = options.open(&temp).map_err(|error| format!("cannot create {}: {error}", temp.display()))?;
        file.write_all(payload.as_bytes()).map_err(|error| format!("cannot write {}: {error}", temp.display()))?;
        file.write_all(b"\n").map_err(|error| format!("cannot write {}: {error}", temp.display()))?;
        file.sync_all().map_err(|error| format!("cannot sync {}: {error}", temp.display()))?;
        drop(file);

        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(&temp, std::fs::Permissions::from_mode(0o600))
                .map_err(|error| format!("cannot set 0600 permissions on {}: {error}", temp.display()))?;
        }

        std::fs::rename(&temp, path).map_err(|error| format!("cannot replace {}: {error}", path.display()))?;
        Ok(())
    })();

    if write_result.is_err() {
        let _ = std::fs::remove_file(&temp);
    }
    write_result
}

async fn blocking<T: Send + 'static>(work: impl FnOnce() -> Result<T, String> + Send + 'static) -> Result<T, String> {
    tokio::task::spawn_blocking(work).await.map_err(|error| format!("credential file call failed: {error}"))?
}

/// The stored token for `server_url`, or an empty string when absent or
/// unreadable.
pub async fn lookup(path: Option<PathBuf>, server_url: &str) -> String {
    let server = server_url.to_owned();
    match blocking(move || {
        let resolved = resolve_path(path.as_deref())?;
        let _guard = FILE_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        let map = read_map(&resolved)?;
        let normalized = normalized_base_url(&server);
        let token = map.get(&normalized).or_else(|| map.get(&server)).map(|t| t.trim().to_owned()).unwrap_or_default();
        Ok(token)
    })
    .await
    {
        Ok(token) => token,
        Err(error) => {
            eprintln!("credentials: file lookup for {server_url} failed: {error}");
            String::new()
        }
    }
}

/// Stores (replacing) the token for `server_url` in the private `0600` file.
pub async fn store(path: Option<PathBuf>, server_url: &str, token: &str) -> Result<(), String> {
    let server = normalized_base_url(server_url);
    let token = token.trim().to_owned();
    blocking(move || {
        let resolved = resolve_path(path.as_deref())?;
        let _guard = FILE_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        let mut map = read_map(&resolved)?;
        map.insert(server, token);
        write_map(&resolved, &map)
    })
    .await
}

/// Deletes the token for `server_url`; succeeds when there was none.
pub async fn remove(path: Option<PathBuf>, server_url: &str) -> Result<(), String> {
    let raw = server_url.to_owned();
    let normalized = normalized_base_url(server_url);
    blocking(move || {
        let resolved = resolve_path(path.as_deref())?;
        let _guard = FILE_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        if !resolved.exists() {
            return Ok(());
        }
        let mut map = read_map(&resolved)?;
        let removed_norm = map.remove(&normalized).is_some();
        let removed_raw = map.remove(&raw).is_some();
        if removed_norm || removed_raw {
            write_map(&resolved, &map)?;
        }
        Ok(())
    })
    .await
}
