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

    pub fn remove(&self, base_url: &str, session: &str) {
        let path = self.path_for(base_url, session);
        if let Err(error) = std::fs::remove_file(&path)
            && error.kind() != std::io::ErrorKind::NotFound
        {
            eprintln!("TranscriptCache: could not remove {}: {error}", path.display());
        }
    }
}
