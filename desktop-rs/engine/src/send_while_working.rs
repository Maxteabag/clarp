//! Send while working on the engine (issue 920, feature `send_while_busy`;
//! names and words in `clarp_core::send_while_working`): whether the Host
//! offers the setting, its value (`GET /send-settings` on connecting, its
//! event, or this desktop's write while the Host answers), the `while_busy`
//! every send carries (the setting, or one send's own choice), the Host's
//! words for a send it did otherwise, per chat.

use std::collections::HashMap;

use clarp_core::json::{self, Object};
use clarp_core::send_while_working::{self as sww, Mode};
use serde_json::json;

use crate::{Change, Engine};

const WRITE_TAG: &str = "send-settings-write";
const READ_TAG: &str = "send-settings";

#[derive(Default)]
pub(crate) struct SendWhileWorking {
    /// The Host lists the feature.
    feature: bool,
    /// What the Host said last (or this desktop wrote), None before it said.
    mode: Option<Mode>,
    /// The value before this desktop's write, put back if the Host refuses it.
    before_write: Option<Mode>,
    /// Chat → the Host's reason why a send of it stopped the turn.
    reasons: HashMap<String, String>,
    /// The next send's own choice (Send's menu), instead of the setting.
    once: Option<Mode>,
}

impl Engine {
    /// The connected Host offers Send while working.
    pub fn send_while_working_offered(&self) -> bool {
        self.send_while_working.feature
    }

    /// `session`'s Host offers it.
    pub fn send_while_working_offered_for(&self, session: &str) -> bool {
        on_peer!(self.peer_ref(session), |engine, _local| engine.send_while_working_offered());
        self.send_while_working.feature
    }

    /// The connected Host's setting (Steer now until it says otherwise).
    pub fn send_while_working(&self) -> Mode {
        self.send_while_working.mode.unwrap_or_default()
    }

    /// The setting of `session`'s Host.
    pub fn send_while_working_for(&self, session: &str) -> Mode {
        on_peer!(self.peer_ref(session), |engine, _local| engine.send_while_working());
        self.send_while_working()
    }

    /// Writes the setting to the connected Host; it shows at once, and a
    /// refusal puts the value back and says so.
    pub fn set_send_while_working(&mut self, mode: Mode) {
        if !self.send_while_working.feature || self.send_while_working.before_write.is_some() {
            return;
        }
        self.send_while_working.before_write = Some(self.send_while_working());
        self.send_while_working.mode = Some(mode);
        self.changes.push(Change::Preferences);
        self.api.post_json(WRITE_TAG, sww::PATH, json!({ sww::FIELD: mode.wire() }), None);
    }

    /// The next send to `session` goes as `mode` (Send's menu during a
    /// turn), whatever the setting says.
    pub(crate) fn send_next_as(&mut self, session: &str, mode: Mode) {
        on_peer!(self.peer_mut(session), |engine, local| engine.send_next_as(local, mode));
        if self.send_while_working.feature {
            self.send_while_working.once = Some(mode);
        }
    }

    /// Every send to a Host with the feature says what to do while the
    /// agent works: its own choice from Send's menu, once, else the setting.
    pub(crate) fn send_while_working_field(&mut self, body: &mut serde_json::Value) {
        let once = self.send_while_working.once.take();
        if self.send_while_working.feature {
            let mode = once.unwrap_or_else(|| {
                if body["queue_if_busy"] == true { Mode::AfterTurn } else { self.send_while_working() }
            });
            body[sww::FIELD] = json!(mode.wire());
        }
    }

    /// The Host's reason for `session`'s last send that stopped the turn
    /// instead of steering, on one line ("" when it gave none).
    pub fn send_while_working_reason(&self, session: &str) -> String {
        on_peer!(self.peer_ref(session), |engine, local| engine.send_while_working_reason(local));
        self.send_while_working.reasons.get(session).cloned().unwrap_or_default()
    }

    fn note_send_reason(&mut self, session: &str, reason: String) {
        if !session.is_empty() && self.send_while_working.reasons.get(session) != Some(&reason) {
            self.send_while_working.reasons.insert(session.to_owned(), reason);
            self.changes.push(Change::Queue(session.to_owned()));
        }
    }

    /// `/server-info` answered: with the feature, its setting is read
    /// (again after every reconnect, for a change missed meanwhile).
    pub(crate) fn send_while_working_server_info(&mut self, info: &Object) {
        let feature = sww::offered_by(info);
        if feature != self.send_while_working.feature {
            self.send_while_working.feature = feature;
            if !feature {
                self.send_while_working.mode = None;
            }
            self.changes.push(Change::Preferences);
        }
        if feature {
            self.api.get(READ_TAG, sww::PATH, &[]);
        }
    }

    fn send_while_working_mode(&mut self, mode: Mode) {
        if !self.send_while_working.feature || self.send_while_working.before_write.is_some() {
            return;
        }
        if self.send_while_working.mode != Some(mode) {
            self.send_while_working.mode = Some(mode);
            self.changes.push(Change::Preferences);
        }
    }

    /// Follow setting changes and clear a chat's fallback when its turn ends.
    pub(crate) fn send_while_working_event(&mut self, kind: &str, session: &str, event: &Object) {
        if kind == sww::EVENT {
            if let Some(mode) = sww::read(event) {
                self.send_while_working_mode(mode);
            }
        } else if kind == "agent-state"
            && matches!(json::string(event, "kind").as_str(), "done" | "idle" | "failed")
            && self.send_while_working.reasons.remove(session).is_some()
        {
            self.changes.push(Change::Queue(session.to_owned()));
        }
    }

    /// A `/send` was answered: a fallback it reports shows on the line.
    pub(crate) fn send_while_working_send_answer(&mut self, session: &str, answer: &Object) {
        if let Some(reason) = sww::fallback(answer) {
            self.note_send_reason(session, reason);
        }
    }

    pub(crate) fn send_while_working_json(&mut self, tag: &str, object: &Object) -> bool {
        if tag != WRITE_TAG && tag != READ_TAG {
            return false;
        }
        if tag == WRITE_TAG {
            self.send_while_working.before_write = None;
        }
        if let Some(mode) = sww::read(object) {
            self.send_while_working_mode(mode);
        }
        true
    }

    pub(crate) fn send_while_working_failure(&mut self, tag: &str, message: &str) -> bool {
        if tag == READ_TAG {
            // Unread, the setting shows its default; the Host still applies its own.
            return true;
        }
        if tag != WRITE_TAG {
            return false;
        }
        if let Some(before) = self.send_while_working.before_write.take() {
            self.send_while_working.mode = Some(before);
            self.changes.push(Change::Preferences);
        }
        self.set_error(&format!("{} not saved: {message}", sww::TITLE));
        true
    }
}
