//! The goal lane takes the same shared login lock as exec while it owns a turn.

use std::fs::{self, File, OpenOptions};
use std::hash::{DefaultHasher, Hash, Hasher};
use std::io;
use std::path::{Path, PathBuf};

use serde_json::Value;

pub(super) struct Login {
    lock: Option<PathBuf>,
    auth: Option<PathBuf>,
    original: Option<u64>,
    held: Option<File>,
}

impl Login {
    pub(super) fn read(options: &mut Value) -> io::Result<Self> {
        let lock = options["_clarpLoginLock"]
            .take()
            .as_str()
            .map(PathBuf::from);
        let auth = options["_clarpLoginFile"]
            .take()
            .as_str()
            .map(PathBuf::from);
        if let Some(fields) = options.as_object_mut() {
            fields.remove("_clarpLoginLock");
            fields.remove("_clarpLoginFile");
        }
        let original = auth.as_deref().map(identity).transpose()?.flatten();
        Ok(Self {
            lock,
            auth,
            original,
            held: None,
        })
    }

    /// False means a known login change since this process started. The lane
    /// retires before handing another attempt to the provider under that login.
    pub(super) fn acquire(&mut self) -> io::Result<bool> {
        if self.held.is_some() {
            return Ok(true);
        }
        let held = if let Some(path) = &self.lock {
            let file = OpenOptions::new()
                .read(true)
                .write(true)
                .create(true)
                .truncate(false)
                .open(path)?;
            file.try_lock_shared().map_err(io::Error::other)?;
            Some(file)
        } else {
            None
        };
        let current = self.auth.as_deref().map(identity).transpose()?.flatten();
        if current != self.original {
            return Ok(false);
        }
        self.held = held;
        Ok(true)
    }

    pub(super) fn release(&mut self) {
        self.held = None;
    }
}

fn identity(path: &Path) -> io::Result<Option<u64>> {
    let text = match fs::read(path) {
        Ok(text) => text,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error),
    };
    let value: Value = serde_json::from_slice(&text).map_err(io::Error::other)?;
    let mut hash = DefaultHasher::new();
    // Token refresh within the same account does not retire a lane. Never
    // expose a credential or the comparison value.
    if let Some(account) = value["tokens"]["account_id"].as_str() {
        account.hash(&mut hash);
    } else {
        text.hash(&mut hash);
    }
    Ok(Some(hash.finish()))
}
