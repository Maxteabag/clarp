//! A lane takes the same shared login lock as a turn of its own process while it owns a turn,
//! and leaves once the tool's login changed since it started (`lane-login-changed`).

use std::fs::{self, File, OpenOptions};
use std::hash::{DefaultHasher, Hash, Hasher};
use std::io;
use std::path::{Path, PathBuf};

use clarp_shim::lane::key;
use serde_json::Value;

/// Names the account of a tool's login file, when it names one: what a lane compares, so a token
/// refreshed within the same account does not retire it.
pub(super) type Account = fn(&Value) -> Option<String>;

pub(super) struct Login {
    lock: Option<PathBuf>,
    auth: Option<PathBuf>,
    original: Option<u64>,
    held: Option<File>,
    account: Account,
}

impl Login {
    pub(super) fn read(options: &mut Value, account: Account) -> io::Result<Self> {
        let lock = options[key::LOGIN_LOCK].take().as_str().map(PathBuf::from);
        let auth = options[key::LOGIN_FILE].take().as_str().map(PathBuf::from);
        if let Some(fields) = options.as_object_mut() {
            fields.remove(key::LOGIN_LOCK);
            fields.remove(key::LOGIN_FILE);
        }
        let original = auth
            .as_deref()
            .map(|path| identity(path, account))
            .transpose()?
            .flatten();
        Ok(Self {
            lock,
            auth,
            original,
            held: None,
            account,
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
        let current = self
            .auth
            .as_deref()
            .map(|path| identity(path, self.account))
            .transpose()?
            .flatten();
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

fn identity(path: &Path, account: Account) -> io::Result<Option<u64>> {
    let text = match fs::read(path) {
        Ok(text) => text,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error),
    };
    let value: Value = serde_json::from_slice(&text).map_err(io::Error::other)?;
    let mut hash = DefaultHasher::new();
    // Token refresh within the same account does not retire a lane. Never
    // expose a credential or the comparison value.
    if let Some(account) = account(&value) {
        account.hash(&mut hash);
    } else {
        text.hash(&mut hash);
    }
    Ok(Some(hash.finish()))
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::Login;
    use base64::{Engine as _, engine::general_purpose::URL_SAFE_NO_PAD};
    use serde_json::json;

    use crate::codex_lane_runtime::account;

    #[test]
    fn a_lane_leaves_for_another_account_and_stays_for_a_refreshed_token() {
        let dir = std::env::temp_dir().join(format!("lane-login-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let file = dir.join("auth.json");
        let write = |workspace: &str, user: &str, expiry: u64| {
            let payload = URL_SAFE_NO_PAD.encode(json!({"sub":user,"exp":expiry}).to_string());
            let token = format!("header.{payload}.signature");
            let text = json!({"tokens": {"account_id": workspace, "id_token": token}});
            std::fs::write(&file, text.to_string()).unwrap();
        };
        write("shared", "first", 1);
        let mut options = json!({"_clarpLoginFile": file, "_clarpLoginLock": dir.join("login.lock"), "model": "m"});
        let mut login = Login::read(&mut options, account).unwrap();
        // The lane's own keys are taken out; the tool's are left.
        assert_eq!(options, json!({"model": "m"}));
        assert!(login.acquire().unwrap());
        login.release();
        write("shared", "first", 2);
        assert!(login.acquire().unwrap());
        login.release();
        write("other", "first", 2);
        assert!(!login.acquire().unwrap());
        write("shared", "second", 2);
        assert!(!login.acquire().unwrap());
        let write_access = |token: &str| {
            let auth = json!({"tokens":{"account_id":"shared","access_token":token}});
            std::fs::write(&file, auth.to_string()).unwrap();
        };
        let access = |user: &str, sub: &str| {
            let claims = json!({"https://api.openai.com/auth":{"chatgpt_user_id":user},"sub":sub});
            format!(
                "header.{}.signature",
                URL_SAFE_NO_PAD.encode(claims.to_string())
            )
        };
        let read_login = || Login::read(&mut json!({"_clarpLoginFile":file}), account).unwrap();
        write_access(&access("first", "old-sub"));
        let mut access_login = read_login();
        write_access(&access("first", "new-sub"));
        assert!(access_login.acquire().unwrap());
        access_login.release();
        write_access(&access("second", "old-sub"));
        assert!(!access_login.acquire().unwrap());

        write_access("opaque-one");
        let mut opaque_login = read_login();
        assert!(opaque_login.acquire().unwrap());
        opaque_login.release();
        write_access("opaque-two");
        assert!(!opaque_login.acquire().unwrap());
        std::fs::remove_dir_all(&dir).unwrap();
    }
}
