//! What each chat's last Stop did (Host contract 61): our own `/stop`'s
//! answer, or the interrupted state's `user_stop` detail when another
//! device or an agent stopped it. The chat shows one line for it until the
//! agent works again; a Stop that changed nothing is only an error.

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
    /// Chats Stop was pressed in while their next turn was held.
    held: HashSet<String>,
}

/// What Stop says when nothing runs: the next turn waits for the update.
const HELD_NOTE: &str = "Nothing is running; the next turn waits for the Clarp update";

impl Engine {
    /// The chat's Stop line ("" when none): what the last Stop did, who
    /// stopped it and why.
    pub fn stop_notice(&self, session: &str) -> String {
        if self.stops.held.contains(session) && self.live_held(session) {
            return HELD_NOTE.to_owned();
        }
        let Some((receipt, _)) = self.stops.receipts.get(session) else { return String::new() };
        let name = |s: &str| self.roster.find(s).map(|a| display_name(a).to_owned());
        match receipt.said(&name) {
            Said::Line(line) => line,
            Said::Error(_) => String::new(),
        }
    }

    /// The chat's next turn is held by the runtime's release drain
    /// (`clarp_core::live_present::held`): nothing runs, so nothing stops.
    pub fn live_held(&self, session: &str) -> bool {
        self.live_active(session) && self.live_view(session).is_some_and(clarp_core::live_present::held)
    }

    /// Stop in a held chat: a note instead of `/stop`, which would only
    /// pause the queue and drop the parked sends. True when it was held.
    pub(crate) fn stop_held(&mut self, session: &str) -> bool {
        if !self.live_held(session) {
            return false;
        }
        self.stops.held.insert(session.to_owned());
        self.changes.push(Change::Stopped(session.to_owned()));
        true
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
        let receipt = StopReceipt::from_response(object);
        let said = {
            let name = |s: &str| self.roster.find(s).map(|a| display_name(a).to_owned());
            receipt.said(&name)
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
            if self.stops.held.remove(session) {
                self.changes.push(Change::Stopped(session.to_owned()));
            }
            self.set_receipt(session, None);
            return;
        }
        if kind != "interrupted" {
            return;
        }
        let Some(receipt) = event.get("detail").and_then(|d| d.as_object()).and_then(StopReceipt::from_detail) else { return };
        // Our own Stop's answer says more (its effects): its state row does
        // not replace it.
        let answered = self.stops.receipts.get(session).is_some_and(|(r, at)| r.origin == Origin::Response && at.elapsed() < ANSWER_HOLDS);
        if self.stops.pending.contains(session) || answered {
            return;
        }
        self.set_receipt(session, Some(receipt));
    }
}
