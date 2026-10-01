//! Connecting to a Host (the Qt controller's `connectToServer`, `pairDevice`
//! and `forgetCredential`). Device tokens live in the Secret Service; with
//! `Config::keyring` off (checks, tests) it is never touched.

use clarp_core::json::{self, Object};
use clarp_core::settings::normalized_base_url;
use serde_json::{Value, json};
use url::Url;

use crate::{Change, Engine, Message};

impl Engine {
    pub fn has_stored_credential(&self) -> bool {
        self.has_stored_credential
    }

    /// Points the engine at another Host: its roster, rooms and chats go.
    pub fn set_base_url(&mut self, url: &str) {
        let normalized = normalized_base_url(url);
        if self.base_url == normalized {
            return;
        }
        self.reset_transient_state();
        self.reset_narrator();
        self.sse.stop();
        self.base_url = normalized.clone();
        self.server_name.clear();
        self.server_version.clear();
        let empty = serde_json::Map::from_iter([("agents".to_owned(), Value::Array(Vec::new()))]);
        self.mutate_roster(|r| r.apply_snapshot(&empty));
        self.archived.apply_snapshot(&empty);
        self.archived.take_ops();
        self.rooms.clear();
        self.conversations.clear();
        if !self.selected.is_empty() {
            self.selected.clear();
            self.changes.push(Change::Selection);
        }
        self.settings.set("connection/baseUrl", normalized);
        self.changes.extend([Change::Roster, Change::Archive, Change::Rooms, Change::ServerInfo]);
    }

    /// Connects with `token`, or without one the stored device token (then
    /// the local admin token for a loopback Host).
    pub fn connect_to_server(&mut self, url: &str, token: &str) {
        self.set_base_url(url);
        self.token = token.trim().to_owned();
        if !self.token.is_empty() {
            self.reconnect();
            return;
        }
        self.look_up_credential();
    }

    pub(crate) fn look_up_credential(&mut self) {
        let base = self.base_url.clone();
        if !self.keyring {
            self.credential_looked_up(&base, String::new());
            return;
        }
        let (sender, wake) = (self.sender.clone(), self.wake.clone());
        self.runtime.spawn(async move {
            let token = clarp_net::credentials::lookup(&base).await;
            if sender.send(Message::Credential { base, token }).is_ok() {
                wake();
            }
        });
    }

    /// Exchanges a one-time code for a device token, then connects with it.
    pub fn pair_device(&mut self, url: &str, code: &str) {
        let code = code.trim().to_owned();
        if code.is_empty() {
            self.set_error("Enter the one-time pairing code");
            return;
        }
        self.set_base_url(url);
        let Some(endpoint) = Url::parse(&self.base_url).ok().filter(|u| u.host_str().is_some_and(|h| !h.is_empty())) else {
            self.set_error("Enter a valid Clarp server URL");
            return;
        };
        self.token.clear();
        self.sse.stop();
        self.api.set_endpoint(endpoint, "");
        self.set_connecting(true);
        self.set_connection_state("pairing");
        self.set_error("");
        self.api.post_json("pairing", "/pairing/exchange", json!({"code": code, "device_name": "Clarp desktop"}), None);
    }

    pub(crate) fn handle_pairing(&mut self, object: &Object) {
        let token = object.get("device").and_then(Value::as_object).map(|d| json::string(d, "token")).unwrap_or_default();
        if token.is_empty() {
            self.set_connecting(false);
            self.set_connection_state("offline");
            self.set_error("Pairing response did not contain a device credential");
            return;
        }
        self.token = token.clone();
        self.store_credential(token);
        self.reconnect();
    }

    /// A device token that worked is worth keeping; plain tokens from the
    /// environment or config are not written to the keyring.
    pub(crate) fn keep_working_token(&mut self) {
        if self.token.starts_with("cld_") {
            let token = self.token.clone();
            self.store_credential(token);
        }
    }

    fn store_credential(&mut self, token: String) {
        let base = self.base_url.clone();
        if !self.keyring {
            eprintln!("Engine: keyring off; not storing the device token for {base}");
            return;
        }
        let (sender, wake) = (self.sender.clone(), self.wake.clone());
        self.runtime.spawn(async move {
            let result = clarp_net::credentials::store(&base, &token).await;
            if sender.send(Message::CredentialStored { base, result }).is_ok() {
                wake();
            }
        });
    }

    pub(crate) fn credential_stored(&mut self, server: &str, result: Result<(), String>) {
        match result {
            Err(message) => {
                eprintln!("Engine: storing the device token failed: {message}");
                if !message.is_empty() {
                    self.set_error(&message);
                }
            }
            Ok(()) if normalized_base_url(server) == self.base_url => self.set_has_stored_credential(true),
            Ok(()) => {}
        }
    }

    /// Removes this Host's device token and disconnects.
    pub fn forget_credential(&mut self) {
        let base = self.base_url.clone();
        if !self.keyring {
            self.credential_removed(&base, Ok(()));
            return;
        }
        let (sender, wake) = (self.sender.clone(), self.wake.clone());
        self.runtime.spawn(async move {
            let result = clarp_net::credentials::remove(&base).await;
            if sender.send(Message::CredentialRemoved { base, result }).is_ok() {
                wake();
            }
        });
    }

    pub(crate) fn credential_removed(&mut self, server: &str, result: Result<(), String>) {
        if let Err(message) = result {
            eprintln!("Engine: forgetting the device token failed: {message}");
            self.set_error(&message);
            return;
        }
        if normalized_base_url(server) != self.base_url {
            return;
        }
        self.token.clear();
        self.sse.stop();
        self.changes.push(Change::Endpoint);
        self.set_connecting(false);
        self.set_connection_state("offline");
        self.set_error("");
        self.set_has_stored_credential(false);
    }

    fn set_has_stored_credential(&mut self, stored: bool) {
        if self.has_stored_credential != stored {
            self.has_stored_credential = stored;
            self.changes.push(Change::Connection);
        }
    }
}
