//! What each chat's last Stop did (Host contract 61): our own `/stop`'s
//! answer, or the interrupted state's `user_stop` detail when another
//! device or an agent stopped it. The chat shows one line for it until the
//! agent works again; a Stop that changed nothing is only an error. A Host
//! that lists `stop_receipt_text` (Rust Host contract 184) tells the Stop in
//! one sentence of its own, and the line is that sentence.

use std::collections::{HashMap, HashSet};
use std::time::{Duration, Instant};

use clarp_core::json::{self, Object};
use clarp_core::protocol::display_name;
use clarp_core::stop_receipt::{Origin, Said, StopReceipt};

use crate::{Change, Engine};

/// A state row of our own Stop can trail its answer: within this window it
/// does not replace the answer's fuller receipt.
const ANSWER_HOLDS: Duration = Duration::from_secs(10);

#[derive(Default)]
pub(crate) struct Stops {
    receipts: HashMap<String, (StopReceipt, Instant)>,
    /// Chats whose own `/stop` has not answered yet.
    pending: HashSet<String>,
    /// The Host lists `stop_receipt_text`: its sentence is the line.
    host_text: bool,
}

impl Engine {
    /// The chat's Stop line ("" when none): what the last Stop did, who
    /// stopped it and why.
    pub fn stop_notice(&self, session: &str) -> String {
        on_peer!(self.peer_ref(session), |engine, local| engine.stop_notice(local));
        let Some((receipt, _)) = self.stops.receipts.get(session) else { return String::new() };
        let name = |s: &str| self.roster.find(s).map(|a| display_name(a).to_owned());
        match receipt.said(&name, self.queue_waiting(session)) {
            Said::Line(line) => line,
            Said::Error(_) => String::new(),
        }
    }

    /// The line under the chat's own composer: `stop_notice`, but nothing
    /// when it is only the word "Stopped" and the stopped turn already ends
    /// with its own line (`turn_end_reason`), so the chat says it once, on
    /// the turn. A sentence that says more (who, why, what waits) stays.
    pub fn composer_stop_notice(&self, session: &str) -> String {
        on_peer!(self.peer_ref(session), |engine, local| engine.composer_stop_notice(local));
        let notice = self.stop_notice(session);
        let bare = notice.trim().trim_end_matches('.').eq_ignore_ascii_case("stopped");
        let on_the_turn = bare
            && self.turn_end_reason(session)
            && self.conversation(session).is_some_and(|c| clarp_core::turn_reason::last_says_why(c.rows()));
        if on_the_turn { String::new() } else { notice }
    }

    /// Whether messages wait in the chat's queue.
    fn queue_waiting(&self, session: &str) -> bool {
        self.roster.find(session).is_some_and(|a| a.queued_turn_count > 0)
    }

    /// `/server-info` answered: whether the Host tells a Stop in a sentence.
    pub(crate) fn stop_server_info(&mut self, info: &Object) {
        self.stops.host_text = info
            .get("capabilities")
            .and_then(|c| c.get("features"))
            .and_then(serde_json::Value::as_array)
            .is_some_and(|features| features.iter().any(|f| f.as_str() == Some(clarp_core::stop_receipt::FEATURE)));
    }

    /// The Host's sentence where it lists the feature, else the line as before.
    fn with_host_text(&self, receipt: StopReceipt) -> StopReceipt {
        if self.stops.host_text { receipt } else { receipt.without_host_text() }
    }

    pub(crate) fn stop_requested(&mut self, session: &str) {
        self.stops.pending.insert(session.to_owned());
    }

    fn set_receipt(&mut self, session: &str, receipt: Option<StopReceipt>) {
        let changed = match receipt {
            Some(receipt) => {
                self.stops.receipts.insert(session.to_owned(), (receipt, Instant::now()));
                true
            }
            None => self.stops.receipts.remove(session).is_some(),
        };
        if changed {
            self.changes.push(Change::Stopped(session.to_owned()));
        }
    }

    pub(crate) fn stop_json(&mut self, tag: &str, object: &Object) -> bool {
        let Some(session) = tag.strip_prefix("stop:") else { return false };
        let session = session.to_owned();
        self.stops.pending.remove(&session);
        let receipt = self.with_host_text(StopReceipt::from_response(object));
        let said = {
            let name = |s: &str| self.roster.find(s).map(|a| display_name(a).to_owned());
            receipt.said(&name, self.queue_waiting(&session))
        };
        match said {
            Said::Line(_) => self.set_receipt(&session, Some(receipt)),
            Said::Error(error) => {
                self.set_receipt(&session, None);
                self.set_error(&error);
            }
        }
        true
    }

    /// The Stop request failed: the window's error says so, as before.
    pub(crate) fn stop_failure(&mut self, tag: &str) {
        if let Some(session) = tag.strip_prefix("stop:") {
            self.stops.pending.remove(session);
        }
    }

    /// An `agent-state` event: a Stop's detail sets the line, work clears it.
    pub(crate) fn stop_state_event(&mut self, session: &str, event: &Object) {
        if session.is_empty() {
            return;
        }
        let kind = json::string(event, "kind");
        if clarp_core::protocol::is_busy_state(&kind) {
            self.set_receipt(session, None);
            return;
        }
        if kind != "interrupted" {
            return;
        }
        let Some(receipt) = event.get("detail").and_then(|d| d.as_object()).and_then(StopReceipt::from_detail) else { return };
        let receipt = self.with_host_text(receipt);
        // Our own Stop's answer says more (its effects): its state row does
        // not replace it.
        let answered = self.stops.receipts.get(session).is_some_and(|(r, at)| r.origin == Origin::Response && at.elapsed() < ANSWER_HOLDS);
        if self.stops.pending.contains(session) || answered {
            return;
        }
        self.set_receipt(session, Some(receipt));
    }

    /// An `agent-activity` event: while a Stop is the agent's latest state the
    /// Host's sentence rides on it as `stop_text`. It sets the line only where
    /// none is shown yet (an app that missed the state event); our own Stop's
    /// fuller answer and a detail already read stay.
    pub(crate) fn stop_activity_event(&mut self, session: &str, event: &Object) {
        if !self.stops.host_text || session.is_empty() || self.stops.pending.contains(session) || self.stops.receipts.contains_key(session) {
            return;
        }
        let text = json::string(event, "stop_text");
        let text = text.trim();
        if text.is_empty() {
            return;
        }
        let receipt = StopReceipt {
            origin: Origin::Detail,
            ok: None,
            terminated: None,
            actor: None,
            actor_verified: None,
            reason: None,
            queue_paused: None,
            goals_paused: None,
            text: Some(text.to_owned()),
        };
        self.set_receipt(session, Some(receipt));
    }
}
