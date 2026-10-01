//! Routes replies and stream signals to the panels (`updates`, `teams`,
//! `profile`) ahead of the engine's own handling.

use clarp_core::json::Object;
use clarp_net::SseSignal;

use crate::Engine;

impl Engine {
    /// True when a panel owned the reply.
    pub(crate) fn panels_json(&mut self, tag: &str, object: &Object) -> bool {
        self.updates_json(tag, object) || self.teams_json(tag, object) || self.profile_json(tag, object)
    }

    /// True when a panel owned the failure; the rest become the engine's error.
    pub(crate) fn panels_failure(&mut self, tag: &str, message: &str, status: u16) -> bool {
        let detail = if status > 0 { format!("{message} (HTTP {status})") } else { message.to_owned() };
        self.updates_failure(tag, &detail) || self.teams_failure(tag, &detail) || self.avatar_failure(tag, &detail) || self.profile_failure(tag, message, status, &detail)
    }

    /// Watches the stream; the engine handles every signal after.
    pub(crate) fn panels_sse(&mut self, signal: &SseSignal) {
        self.updates_sse(signal);
    }

    /// Byte replies (portraits, media images); anything else is unexpected.
    pub(crate) fn panels_bytes(&mut self, tag: &str, bytes: &[u8], content_type: &str) {
        if !self.updates_bytes(tag, bytes) && !self.avatar_bytes(tag, bytes, content_type) && !self.profile_bytes(tag, bytes, content_type) {
            eprintln!("Engine: unexpected bytes reply {tag}");
        }
    }
}
