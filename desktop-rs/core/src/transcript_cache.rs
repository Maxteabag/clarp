//! Durable transcript rows kept between runs (C++ `TranscriptCache`), so a
//! chat opens with its history before the Host answers. One file per Host
//! and session, named by digest; the envelope names both, so a file never
//! restores into another Host's chat. Every entry is refetched when missing,
//! so writes are an atomic replace, not a synced save.

use std::path::{Path, PathBuf};

use serde_json::{Value, json};
use sha2::{Digest, Sha256};

use crate::json::Object;

pub const MAX_CACHE_BYTES: u64 = 8 * 1024 * 1024;
pub const SAVE_DELAY_MS: u64 = 250;

#[derive(Debug, Clone)]
pub struct TranscriptCache {
    root: PathBuf,
}

impl TranscriptCache {
    pub fn new(root: impl Into<PathBuf>) -> Self {
        Self { root: root.into() }
    }

    pub fn root(&self) -> &Path {
        &self.root
    }

    fn path_for(&self, base_url: &str, session: &str) -> PathBuf {
        let digest: String = Sha256::digest(format!("{base_url}\0{session}").as_bytes()).iter().map(|b| format!("{b:02x}")).collect();
        self.root.join(format!("{digest}.json"))
    }

    /// The saved snapshot, or empty when there is none that fits.
    pub fn load(&self, base_url: &str, session: &str) -> Object {
        if base_url.is_empty() || session.is_empty() {
            return Object::new();
        }
        let path = self.path_for(base_url, session);
        let size = std::fs::metadata(&path).map(|m| m.len()).unwrap_or(0);
        if size == 0 || size > MAX_CACHE_BYTES {
            return Object::new();
        }
        let Ok(Value::Object(envelope)) = std::fs::read(&path).map_err(|_| ()).and_then(|b| serde_json::from_slice(&b).map_err(|_| ())) else {
            return Object::new();
        };
        let matches = envelope.get("schema").and_then(Value::as_i64) == Some(1)
            && envelope.get("base_url").and_then(Value::as_str) == Some(base_url)
            && envelope.get("session").and_then(Value::as_str) == Some(session);
        if !matches {
            return Object::new();
        }
        envelope.get("snapshot").and_then(Value::as_object).cloned().unwrap_or_default()
    }

    /// Every chat cached for `base_url`, as (session, snapshot), by
    /// session; none when nothing was cached yet.
    pub fn all(&self, base_url: &str) -> Result<Vec<(String, Object)>, String> {
        let entries = match std::fs::read_dir(&self.root) {
            Ok(entries) => entries,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(Vec::new()),
            Err(error) => return Err(format!("{}: {error}", self.root.display())),
        };
        let mut chats = Vec::new();
        for entry in entries {
            let path = entry.map_err(|e| format!("{}: {e}", self.root.display()))?.path();
            if path.extension().is_none_or(|e| e != "json") || std::fs::metadata(&path).map_or(0, |m| m.len()) > MAX_CACHE_BYTES {
                continue;
            }
            let Ok(Value::Object(envelope)) = std::fs::read(&path).map_err(|_| ()).and_then(|b| serde_json::from_slice(&b).map_err(|_| ())) else {
                continue;
            };
            let session = envelope.get("session").and_then(Value::as_str).unwrap_or_default();
            if envelope.get("schema").and_then(Value::as_i64) != Some(1) || envelope.get("base_url").and_then(Value::as_str) != Some(base_url) || session.is_empty() {
                continue;
            }
            if let Some(snapshot) = envelope.get("snapshot").and_then(Value::as_object) {
                chats.push((session.to_owned(), snapshot.clone()));
            }
        }
        chats.sort_by(|a, b| a.0.cmp(&b.0));
        Ok(chats)
    }

    pub fn save(&self, base_url: &str, session: &str, snapshot: &Object) -> Result<(), String> {
        use std::os::unix::fs::PermissionsExt;
        if base_url.is_empty() || session.is_empty() || snapshot.is_empty() {
            return Err("nothing to cache".into());
        }
        std::fs::create_dir_all(&self.root).map_err(|e| format!("{}: {e}", self.root.display()))?;
        let bytes = serde_json::to_vec(&json!({"schema": 1, "base_url": base_url, "session": session, "snapshot": snapshot}))
            .map_err(|e| e.to_string())?;
        if bytes.len() as u64 > MAX_CACHE_BYTES {
            return Err("transcript too large to cache".into());
        }
        let path = self.path_for(base_url, session);
        let temporary = path.with_extension(format!("json.tmp.{}", std::process::id()));
        let written = std::fs::write(&temporary, &bytes)
            .and_then(|()| std::fs::set_permissions(&temporary, std::fs::Permissions::from_mode(0o600)))
            .and_then(|()| std::fs::rename(&temporary, &path));
        if let Err(error) = written {
            if let Err(cleanup) = std::fs::remove_file(&temporary)
                && cleanup.kind() != std::io::ErrorKind::NotFound
            {
                return Err(format!("{}: {error}; and {cleanup}", path.display()));
            }
            return Err(format!("{}: {error}", path.display()));
        }
        Ok(())
    }

    /// Removes everything cached for `base_url`: every chat and its agent
    /// list and model catalog. Forgetting a Host's sign-in leaves none of its
    /// words on disk.
    pub fn forget_host(&self, base_url: &str) {
        let sessions = self.all(base_url).unwrap_or_else(|error| {
            eprintln!("TranscriptCache: could not list {base_url}: {error}");
            Vec::new()
        });
        for (session, _) in sessions {
            self.remove(base_url, &session);
        }
        for kind in ["roster", "catalog"] {
            let path = self.host_path_for(base_url, kind);
            if let Err(error) = std::fs::remove_file(&path)
                && error.kind() != std::io::ErrorKind::NotFound
            {
                eprintln!("TranscriptCache: could not remove {}: {error}", path.display());
            }
        }
    }

    pub fn remove(&self, base_url: &str, session: &str) {
        let path = self.path_for(base_url, session);
        if let Err(error) = std::fs::remove_file(&path)
            && error.kind() != std::io::ErrorKind::NotFound
        {
            eprintln!("TranscriptCache: could not remove {}: {error}", path.display());
        }
    }

    /// A Host-wide file beside the chats: `roster-<digest>.roster` for the
    /// agent list, `catalog-<digest>.catalog` for the model catalog.
    fn host_path_for(&self, base_url: &str, kind: &str) -> PathBuf {
        let digest: String = Sha256::digest(format!("{base_url}\0__{kind}__").as_bytes()).iter().map(|b| format!("{b:02x}")).collect();
        self.root.join(format!("{kind}-{digest}.{kind}"))
    }

    fn load_host(&self, base_url: &str, kind: &str) -> Object {
        if base_url.is_empty() {
            return Object::new();
        }
        let path = self.host_path_for(base_url, kind);
        let size = std::fs::metadata(&path).map(|m| m.len()).unwrap_or(0);
        if size == 0 || size > MAX_CACHE_BYTES {
            return Object::new();
        }
        let Ok(Value::Object(envelope)) = std::fs::read(&path).map_err(|_| ()).and_then(|b| serde_json::from_slice(&b).map_err(|_| ())) else {
            return Object::new();
        };
        let matches = envelope.get("schema").and_then(Value::as_i64) == Some(1)
            && envelope.get("base_url").and_then(Value::as_str) == Some(base_url);
        if !matches {
            return Object::new();
        }
        envelope.get("snapshot").and_then(Value::as_object).cloned().unwrap_or_default()
    }

    fn save_host(&self, base_url: &str, kind: &str, snapshot: &Object) -> Result<(), String> {
        use std::os::unix::fs::PermissionsExt;
        if base_url.is_empty() || snapshot.is_empty() {
            return Err("nothing to cache".into());
        }
        std::fs::create_dir_all(&self.root).map_err(|e| format!("{}: {e}", self.root.display()))?;
        let bytes = serde_json::to_vec(&json!({"schema": 1, "base_url": base_url, "snapshot": snapshot}))
            .map_err(|e| e.to_string())?;
        if bytes.len() as u64 > MAX_CACHE_BYTES {
            return Err(format!("{kind} too large to cache"));
        }
        let path = self.host_path_for(base_url, kind);
        let temporary = path.with_extension(format!("{kind}.tmp.{}", std::process::id()));
        let written = std::fs::write(&temporary, &bytes)
            .and_then(|()| std::fs::set_permissions(&temporary, std::fs::Permissions::from_mode(0o600)))
            .and_then(|()| std::fs::rename(&temporary, &path));
        if let Err(error) = written {
            if let Err(cleanup) = std::fs::remove_file(&temporary)
                && cleanup.kind() != std::io::ErrorKind::NotFound
            {
                return Err(format!("{}: {error}; and {cleanup}", path.display()));
            }
            return Err(format!("{}: {error}", path.display()));
        }
        Ok(())
    }

    /// The last `/agents/snapshot` cached for `base_url`, so the agent list
    /// and open panes appear on Frame 1 before the Host answers.
    pub fn load_roster(&self, base_url: &str) -> Object {
        self.load_host(base_url, "roster")
    }

    pub fn save_roster(&self, base_url: &str, snapshot: &Object) -> Result<(), String> {
        self.save_host(base_url, "roster", snapshot)
    }

    /// The last `/agent-model-options` catalog cached for `base_url`, so New
    /// Session shows the harnesses and models at once, before the Host answers.
    pub fn load_catalog(&self, base_url: &str) -> Object {
        self.load_host(base_url, "catalog")
    }

    pub fn save_catalog(&self, base_url: &str, catalog: &Object) -> Result<(), String> {
        self.save_host(base_url, "catalog", catalog)
    }
}

