//! What the desktop's voice needs from the engine (the Qt controller's side
//! of `AudioController`): where the Host is, the clips it announces or has
//! waiting for the open chat, and delivering a dictation as a message.

use clarp_core::json::Object;
use serde_json::Value;
use url::Url;

use crate::{Change, Engine};

impl Engine {
    /// The Host and token audio uses; None while there is none.
    pub fn endpoint(&self) -> Option<(Url, String)> {
        let url = Url::parse(&self.base_url).ok().filter(|u| u.host_str().is_some_and(|h| !h.is_empty()))?;
        (!self.token.is_empty()).then(|| (url, self.token.clone()))
    }

    /// Shows an error from the desktop side (audio, a device).
    pub fn report_error(&mut self, message: &str) {
        self.set_error(message);
    }

    /// Clips announced for `session` while this window was not listening.
    pub(crate) fn request_recoverable_clips(&mut self, session: &str) {
        if !session.is_empty() {
            self.api.get(&format!("recoverable:{session}"), "/clips/recoverable", &[("session", session)]);
        }
    }

    pub(crate) fn voice_json(&mut self, tag: &str, object: &Object) -> bool {
        if !tag.starts_with("recoverable:") {
            return false;
        }
        let clips: Vec<Object> =
            object.get("events").and_then(Value::as_array).into_iter().flatten().filter_map(|e| e.as_object().cloned()).collect();
        if !clips.is_empty() {
            self.changes.push(Change::Clips(clips));
        }
        true
    }

    /// A finished dictation goes to the chat it was recorded for, else the
    /// selection (C++ `deliverDictation`).
    pub fn send_dictation(&mut self, text: &str, trace: &str, transcription: &str, hands_free: bool, target: &str) {
        let session = clarp_core::protocol::voice_delivery_session(target, &self.selected).to_owned();
        self.send_with_voice(&session, text, false, trace, transcription, hands_free);
    }
}

impl Engine {
    /// What presence needs: whether phone alerts may pause while someone
    /// is here (the setting), and whether a Host is connected.
    pub fn presence_inputs(&self) -> (bool, bool) {
        (self.settings.boolean("notifications/pauseMobileWhileDesktopActive", true), self.connected)
    }

    /// Someone is (or is no longer) at this desktop: the Host may pause
    /// phone alerts while the lease holds.
    pub fn report_desktop_presence(&self, instance: &str, sequence: u64, active: bool) {
        if self.token.is_empty() {
            return;
        }
        let body = serde_json::json!({"instance_id": instance, "sequence": sequence, "active": active,
                                      "sent_at_ms": chrono::Utc::now().timestamp_millis()});
        self.api.post_json("desktop-presence", "/desktop-presence", body, Some(std::time::Duration::from_secs(5)));
    }

    pub fn report_application_activity(&self, instance: &str, sequence: u64, foreground: bool, input_age_ms: i64) {
        if self.token.is_empty() {
            return;
        }
        let body = serde_json::json!({"instance_id": instance, "sequence": sequence, "foreground": foreground,
                                      "input_age_ms": input_age_ms, "sent_at_ms": chrono::Utc::now().timestamp_millis()});
        self.api.post_json("application-activity", "/application-activity", body, Some(std::time::Duration::from_secs(5)));
    }
}
