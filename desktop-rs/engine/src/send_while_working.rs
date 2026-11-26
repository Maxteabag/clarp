//! Send while working on the engine (issue 920; names and words in
//! `clarp_core::send_while_working`): whether the Host offers the setting,
//! its value (the snapshot's, the event's, or this desktop's write while the
//! Host answers), the Host's reason for a send it stopped and sent instead
//! of steering, per chat, and the waiting line's Stop and send while it
//! runs. Without the feature nothing here changes what a send does.

use std::collections::{HashMap, HashSet};

use clarp_core::json::{self, Object};
use clarp_core::send_while_working::{self as sww, Mode};
use serde_json::json;

use crate::{Change, Engine};

const WRITE_TAG: &str = "send-while-working";

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
    /// Chats whose waiting line stopped the turn, until the turn has ended
    /// or their queue changed.
    stopping: HashSet<String>,
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
        if !self.send_while_working.feature {
            return;
        }
        if self.send_while_working.before_write.is_none() {
            self.send_while_working.before_write = Some(self.send_while_working());
        }
        self.send_while_working.mode = Some(mode);
        self.changes.push(Change::Preferences);
        self.api.put_json(WRITE_TAG, sww::WRITE_PATH, json!({ sww::WRITE_FIELD: mode.wire() }));
    }

    /// The next send to `session` goes as `mode` (Send's menu during a
    /// turn), whatever the setting says.
    pub fn send_next_as(&mut self, session: &str, mode: Mode) {
        on_peer!(self.peer_mut(session), |engine, local| engine.send_next_as(local, mode));
        if self.send_while_working.feature {
            self.send_while_working.once = Some(mode);
        }
    }

    /// The send being made carries its own choice, once.
    pub(crate) fn send_while_working_once(&mut self, body: &mut serde_json::Value) {
        if let Some(mode) = self.send_while_working.once.take() {
            body[sww::SEND_FIELD] = json!(mode.wire());
        }
    }

    /// The Host's reason for `session`'s last send that stopped the turn
    /// instead of steering, on one line ("" when it gave none).
    pub fn send_while_working_reason(&self, session: &str) -> String {
        on_peer!(self.peer_ref(session), |engine, local| engine.send_while_working_reason(local));
        self.send_while_working.reasons.get(session).cloned().unwrap_or_default()
    }

    /// The waiting line's Stop and send: the plain Stop, so every waiting
    /// message of the user's goes on next, read together.
    pub fn stop_for_waiting(&mut self, session: &str) {
        on_peer!(self.peer_mut(session), |engine, local| engine.stop_for_waiting(local));
        if session.is_empty() || !self.send_while_working.stopping.insert(session.to_owned()) {
            return;
        }
        self.changes.push(Change::Queue(session.to_owned()));
        self.stop_session(session);
    }

    /// The waiting line's Stop and send runs for `session`.
    pub fn stopping_for_waiting(&self, session: &str) -> bool {
        on_peer!(self.peer_ref(session), |engine, local| engine.stopping_for_waiting(local));
        self.send_while_working.stopping.contains(session)
    }

    fn note_send_reason(&mut self, session: &str, reason: String) {
        if !session.is_empty() && self.send_while_working.reasons.get(session) != Some(&reason) {
            self.send_while_working.reasons.insert(session.to_owned(), reason);
            self.changes.push(Change::Queue(session.to_owned()));
        }
    }

    fn settle_for_waiting(&mut self, session: &str, turn_ended: bool) {
        let stopped = self.send_while_working.stopping.remove(session);
        let reason = turn_ended && self.send_while_working.reasons.remove(session).is_some();
        if stopped || reason {
            self.changes.push(Change::Queue(session.to_owned()));
        }
    }

    pub(crate) fn send_while_working_server_info(&mut self, info: &Object) {
        let feature = sww::offered_by(info);
        if feature != self.send_while_working.feature {
            self.send_while_working.feature = feature;
            if !feature {
                self.send_while_working.mode = None;
            }
            self.changes.push(Change::Preferences);
        }
    }

    /// The snapshot carries the value (absent: Steer now).
    pub(crate) fn send_while_working_snapshot(&mut self, object: &Object) {
        if object.contains_key(sww::SETTINGS) {
            self.send_while_working_mode(sww::from_snapshot(object));
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

    /// The events that change it: the setting's own, a chat's queue (a send
    /// that fell back says why; a Stop and send has been answered), and a
    /// turn that has ended.
    pub(crate) fn send_while_working_event(&mut self, kind: &str, session: &str, event: &Object) {
        if kind == sww::EVENT {
            if let Some(mode) = sww::read(event) {
                self.send_while_working_mode(mode);
            }
        } else if kind == "queue-updated" {
            if let Some(reason) = sww::fallback(event) {
                self.note_send_reason(session, reason);
            }
            self.settle_for_waiting(session, false);
        } else if kind == "agent-state" {
            let state = json::string(event, "kind");
            if matches!(state.as_str(), "done" | "idle" | "interrupted" | "stopped" | "failed") {
                self.settle_for_waiting(session, true);
            }
        }
    }

    /// A `/send` was answered: a fallback it reports shows on the line.
    pub(crate) fn send_while_working_send_answer(&mut self, session: &str, answer: &Object) {
        if let Some(reason) = sww::fallback(answer) {
            self.note_send_reason(session, reason);
        }
    }

    /// A `/stop` was answered or refused: the line's spinner goes.
    pub(crate) fn send_while_working_stop_answer(&mut self, tag: &str) {
        if let Some(session) = tag.strip_prefix("stop:") {
            let session = session.to_owned();
            self.settle_for_waiting(&session, false);
        }
    }

    pub(crate) fn send_while_working_json(&mut self, tag: &str, object: &Object) -> bool {
        if tag != WRITE_TAG {
            return false;
        }
        self.send_while_working.before_write = None;
        if let Some(mode) = sww::read(object) {
            self.send_while_working_mode(mode);
        }
        true
    }

    pub(crate) fn send_while_working_failure(&mut self, tag: &str, message: &str) -> bool {
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
