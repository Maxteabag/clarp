//! What Send does while the agent works, on the engine (feature
//! `send_while_busy`, issue #920; `clarp_core::send_while_busy`).
//!
//! Where the Host lists the feature the setting is read at connect
//! (`GET /send-settings`), followed on `send-settings-updated` and changed
//! with `POST /send-settings`. Every send the user makes carries it as
//! `while_busy`; what the Host did instead (`fallback` in the send's
//! answer) shows in the chat's chip for a few seconds. The chip itself is
//! the loaded turn queue's (`send_chip`), its button the queue's own Steer
//! now and take over (contract 197).

use std::collections::HashMap;
use std::time::Duration;

use clarp_core::json::Object;
use clarp_core::send_while_busy::{self as busy, Chip, WhileBusy};
use serde_json::{Value, json};

use crate::{Change, Engine, Message};

const READ: &str = "send-settings";
/// A change's tag: `send-settings-write:<n>`, the n-th change.
const WRITE: &str = "send-settings-write:";
/// How long the chip shows what the Host did instead.
pub const NOTICE: Duration = Duration::from_secs(5);

#[derive(Default)]
pub(crate) struct SendSettings {
    /// `send_while_busy` was in the last `/server-info`.
    feature: bool,
    /// The setting was asked for on this connection: once at connect, as
    /// `/server-info` may answer more than once.
    asked: bool,
    /// The Host's setting (Steer now until it says otherwise).
    while_busy: WhileBusy,
    /// Why the last change did not go through, as the Host said it.
    error: String,
    /// Changes posted so far: only the latest one's answer is kept.
    writes: u64,
    /// Per chat: the Host's line for a send it handled otherwise, and its
    /// timer's token.
    notices: HashMap<String, (String, u64)>,
    notice_counter: u64,
}

impl Engine {
    /// The connected Host lets the owner choose what Send does while the
    /// agent works.
    pub fn send_while_busy_offered(&self) -> bool {
        self.send_settings.feature
    }

    /// What Send does while the agent works (Steer now by default).
    pub fn while_busy(&self) -> WhileBusy {
        self.send_settings.while_busy
    }

    /// Why the last change did not go through ("" when it did).
    pub fn send_settings_error(&self) -> &str {
        &self.send_settings.error
    }

    /// Changes the setting: shown at once, then the Host's answer.
    pub fn set_while_busy(&mut self, mode: WhileBusy) {
        if !self.send_settings.feature {
            return;
        }
        self.send_settings.while_busy = mode;
        self.send_settings.error.clear();
        self.send_settings.writes += 1;
        self.changes.push(Change::HostStatus);
        let tag = format!("{WRITE}{}", self.send_settings.writes);
        self.api.post_json(&tag, "/send-settings", json!({"while_busy": mode.as_str()}), None);
    }

    /// `while_busy` for a user's send (None on a Host without the feature:
    /// the send stays as it was).
    pub(crate) fn send_while_busy_value(&self, queue_if_busy: bool) -> Option<WhileBusy> {
        self.send_settings.feature.then(|| busy::send_value(self.send_settings.while_busy, queue_if_busy))
    }

    /// The chip above `session`'s message field: its loaded queue while
    /// the agent works, on a Host with the feature.
    pub fn send_chip(&self, session: &str) -> Option<Chip> {
        on_peer!(self.peer_ref(session), |engine, local| engine.send_chip(local));
        if !self.send_settings.feature {
            return None;
        }
        let working = self.roster().find(session).is_some_and(|a| a.busy);
        busy::chip(working, self.turn_queue_steer(session), &self.turn_queue(session))
    }

    /// The Host's line for `session`'s last send it handled otherwise,
    /// while the chip shows it ("" else).
    pub fn send_notice(&self, session: &str) -> String {
        on_peer!(self.peer_ref(session), |engine, local| engine.send_notice(local));
        self.send_settings.notices.get(session).map(|(text, _)| text.clone()).unwrap_or_default()
    }

    /// A send's answer: what the Host did instead, for the chip.
    pub(crate) fn send_settings_send_answer(&mut self, session: &str, answer: &Object) {
        let Some(fallback) = busy::fallback(answer).filter(|_| self.send_settings.feature) else { return };
        self.send_settings.notice_counter += 1;
        let token = self.send_settings.notice_counter;
        self.send_settings.notices.insert(session.to_owned(), (fallback.text, token));
        self.changes.push(Change::Queue(session.to_owned()));
        self.after(NOTICE, Message::SendNoticeDue { session: session.to_owned(), token });
    }

    pub(crate) fn send_notice_due(&mut self, session: &str, token: u64) {
        if self.send_settings.notices.get(session).is_some_and(|(_, t)| *t == token) {
            self.send_settings.notices.remove(session);
            self.changes.push(Change::Queue(session.to_owned()));
        }
    }

    /// `/server-info` answered.
    pub(crate) fn send_settings_server_info(&mut self, info: &Object) {
        let has = info
            .get("capabilities")
            .and_then(|c| c.get("features"))
            .and_then(Value::as_array)
            .is_some_and(|features| features.iter().any(|f| f.as_str() == Some(busy::FEATURE)));
        if has != self.send_settings.feature {
            self.send_settings = SendSettings { feature: has, ..Default::default() };
            self.changes.push(Change::HostStatus);
        }
        if has && !self.send_settings.asked {
            self.send_settings.asked = true;
            self.api.get(READ, "/send-settings", &[]);
        }
    }

    /// The Host or its token changed: read again from `/server-info`.
    pub(crate) fn reset_send_settings(&mut self) {
        if self.send_settings.feature {
            self.changes.push(Change::HostStatus);
        }
        self.send_settings = SendSettings::default();
    }

    /// `send-settings-updated`: another app (or this one) changed it.
    pub(crate) fn send_settings_event(&mut self, event: &Object) {
        if !self.send_settings.feature {
            return;
        }
        match busy::setting(event) {
            Some(mode) => {
                if mode != self.send_settings.while_busy {
                    self.send_settings.while_busy = mode;
                    self.changes.push(Change::HostStatus);
                }
            }
            None => self.api.get(READ, "/send-settings", &[]),
        }
    }

    /// Replies for the setting's tags; true when it was one.
    pub(crate) fn send_settings_json(&mut self, tag: &str, object: &Object) -> bool {
        if let Some(n) = tag.strip_prefix(WRITE) {
            if n.parse::<u64>().ok() != Some(self.send_settings.writes) {
                return true;
            }
        } else if tag != READ {
            return false;
        }
        if let Some(mode) = busy::setting(object) {
            self.send_settings.while_busy = mode;
        }
        self.changes.push(Change::HostStatus);
        true
    }

    /// A refused change or a failed read; true when it was one.
    pub(crate) fn send_settings_failure(&mut self, tag: &str, message: &str, status: u16) -> bool {
        if tag.starts_with(WRITE) {
            eprintln!("Engine: POST /send-settings refused ({status}): {message}");
            self.send_settings.error = message.to_owned();
            self.changes.push(Change::HostStatus);
            // What the Host holds goes back.
            self.api.get(READ, "/send-settings", &[]);
            true
        } else if tag == READ {
            eprintln!("Engine: GET /send-settings failed ({status}): {message}");
            // The next `/server-info` asks again.
            self.send_settings.asked = false;
            true
        } else {
            false
        }
    }
}
