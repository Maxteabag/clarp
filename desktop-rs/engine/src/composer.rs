//! The composer's state (the Qt controller's drafts and composer
//! attachments): a draft and its attachments belong to the chat on this
//! Host, not to a pane, and survive a restart in settings.

use std::time::Duration;

use clarp_core::json::{self, Object};
use serde_json::{Value, json};

use crate::{Change, Engine, Message};

/// Drafts change on every keystroke; writing settings per key blocked typing
/// in the C++ client, so text waits in memory until the composer has been
/// idle this long (or the engine closes).
pub(crate) const DRAFT_FLUSH_DELAY: Duration = Duration::from_millis(1000);

impl Engine {
    fn draft_key(&self, session: &str) -> String {
        format!("{}/text", clarp_core::settings::draft_scope_key(&self.base_url, session))
    }

    fn attachments_key(&self, session: &str) -> String {
        format!("{}/attachments", clarp_core::settings::draft_scope_key(&self.base_url, session))
    }

    pub fn draft(&self, session: &str) -> String {
        if session.is_empty() {
            return String::new();
        }
        let key = self.draft_key(session);
        match self.pending_drafts.get(&key) {
            Some(text) => text.clone(),
            None => self.settings.string(&key, ""),
        }
    }

    pub fn set_draft(&mut self, session: &str, text: &str) {
        if session.is_empty() || self.draft(session) == text {
            return;
        }
        let key = self.draft_key(session);
        self.pending_drafts.insert(key, text.to_owned());
        self.draft_flush_token += 1;
        self.after(DRAFT_FLUSH_DELAY, Message::DraftsDue(self.draft_flush_token));
    }

    pub(crate) fn drafts_due(&mut self, token: u64) {
        if token == self.draft_flush_token {
            self.flush_drafts();
        }
    }

    pub fn flush_drafts(&mut self) {
        for (key, text) in std::mem::take(&mut self.pending_drafts) {
            if text.is_empty() {
                self.settings.remove(&key);
            } else {
                self.settings.set(&key, text);
            }
        }
    }

    pub fn attachments(&self, session: &str) -> Vec<Value> {
        if session.is_empty() {
            return Vec::new();
        }
        self.settings.get(&self.attachments_key(session)).and_then(Value::as_array).cloned().unwrap_or_default()
    }

    fn store_attachments(&mut self, session: &str, attachments: Vec<Value>) {
        if session.is_empty() {
            return;
        }
        let key = self.attachments_key(session);
        if attachments.is_empty() {
            self.settings.remove(&key);
        } else {
            self.settings.set(&key, Value::Array(attachments));
        }
        self.changes.push(Change::Composer(session.to_owned()));
    }

    /// Every attachment has a path and finished uploading.
    pub fn can_send(&self, session: &str) -> bool {
        clarp_core::attachments::can_send(&self.attachments(session))
    }

    pub fn uploading(&self) -> bool {
        !self.pending_uploads.is_empty()
    }

    /// The Host reads files where they are: it runs on this machine, or
    /// `CLARP_SHARED_FILESYSTEM_HOST` / the per-Host setting says so.
    pub fn shared_filesystem(&self) -> bool {
        let override_host = std::env::var("CLARP_SHARED_FILESYSTEM_HOST").unwrap_or_default();
        (!override_host.trim().is_empty() && clarp_core::settings::normalized_base_url(&override_host) == self.base_url)
            || self.settings.boolean(&clarp_core::settings::shared_filesystem_key(&self.base_url), false)
    }

    /// Attaches a local file to the chat's composer: referenced in place on
    /// a shared filesystem, else uploaded to `/upload`.
    pub fn attach_file(&mut self, session: &str, path: &std::path::Path) {
        let metadata = std::fs::metadata(path).ok();
        let valid = metadata.as_ref().is_some_and(|m| m.is_file() && m.len() > 0 && m.len() <= clarp_core::attachments::MAX_UPLOAD_BYTES);
        if !valid || session.is_empty() {
            self.set_error("Choose a readable file no larger than 50 MB");
            return;
        }
        let canonical = std::fs::canonicalize(path).unwrap_or_else(|_| path.to_owned());
        let name = canonical.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
        let bytes = match std::fs::read(&canonical) {
            Ok(bytes) => bytes,
            Err(error) => {
                eprintln!("Engine: could not read {}: {error}", canonical.display());
                self.set_error(&format!("Could not read {name}"));
                return;
            }
        };
        let content_type = clarp_core::attachments::content_type(&name, &bytes[..bytes.len().min(64)]);
        let id = uuid::Uuid::new_v4().to_string();
        let mut attachments = self.attachments(session);
        if self.shared_filesystem() {
            attachments.push(json!({"id": id, "path": canonical.to_string_lossy(), "name": name,
                                    "content_type": content_type, "local": true, "status": "ready"}));
            self.store_attachments(session, attachments);
            return;
        }
        let tag = format!("composer-upload:{id}");
        let pending = json!({"session": session, "id": id, "name": name, "content_type": content_type,
                             "local_source": canonical.to_string_lossy(), "status": "uploading"});
        self.pending_uploads.insert(tag.clone(), pending.as_object().cloned().unwrap_or_default());
        attachments.push(pending);
        self.store_attachments(session, attachments);
        let encoded_name = clarp_core::endpoint::percent_encode_segment(&name);
        self.api.post_bytes(&tag, "/upload", bytes, &content_type, &[("X-File-Name", &encoded_name), ("X-Session", session), ("X-Upload-ID", &id)]);
    }

    pub fn remove_attachment(&mut self, session: &str, id: &str) {
        self.pending_uploads.retain(|_, pending| !(json::string(pending, "session") == session && json::string(pending, "id") == id));
        let mut attachments = self.attachments(session);
        let before = attachments.len();
        attachments.retain(|a| a.get("id").and_then(Value::as_str) != Some(id));
        if attachments.len() != before {
            self.store_attachments(session, attachments);
        }
    }

    /// Sends the composer's text with its attachments' paths; false (and
    /// nothing sent) while an attachment is not ready or there is nothing
    /// to send. `queue_if_busy` (Ctrl+Enter) waits for the running turn.
    pub fn send_composer(&mut self, session: &str, text: &str, queue_if_busy: bool) -> bool {
        let Some(outbound) = clarp_core::attachments::outbound_text(text, &self.attachments(session)) else {
            self.set_error("Wait for attachments to finish uploading or remove them");
            return false;
        };
        if session.is_empty() || outbound.is_empty() {
            return false;
        }
        self.set_draft(session, "");
        let key = self.attachments_key(session);
        self.settings.remove(&key);
        self.changes.push(Change::Composer(session.to_owned()));
        self.send_to(session, &outbound, queue_if_busy);
        true
    }

    /// Turns queued behind the running one.
    pub fn queue_count(&self, session: &str) -> i32 {
        self.roster.find(session).map_or(0, |a| a.queued_turn_count)
    }

    /// Said before the send, so an exhausted provider is not discovered
    /// through a failed reply.
    pub fn quota_notice(&self, session: &str) -> String {
        self.roster.find(session).map_or_else(String::new, |a| a.quota_notice(chrono::Utc::now()))
    }

    /// An `/upload` answered (`object`) or failed (`None`).
    pub(crate) fn finish_upload(&mut self, tag: &str, object: Option<&Object>) -> bool {
        let Some(pending) = self.pending_uploads.remove(tag) else { return tag.starts_with("composer-upload:") };
        let (session, id) = (json::string(&pending, "session"), json::string(&pending, "id"));
        let server_path = object.map(|o| json::string(o, "path")).unwrap_or_default();
        let mut attachments = self.attachments(&session);
        let Some(slot) = attachments.iter_mut().find(|a| a.get("id").and_then(Value::as_str) == Some(id.as_str())) else {
            self.changes.push(Change::Composer(session));
            return true;
        };
        if server_path.is_empty() {
            slot["status"] = json!("failed");
            self.store_attachments(&session, attachments);
            if object.is_some() {
                self.set_error("Upload completed without a file path");
            }
            return true;
        }
        slot["path"] = json!(server_path);
        slot["status"] = json!("ready");
        if let Some(name) = object.map(|o| json::string(o, "name")).filter(|n| !n.is_empty()) {
            slot["name"] = json!(name);
        }
        self.store_attachments(&session, attachments);
        true
    }
}
