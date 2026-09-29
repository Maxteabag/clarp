//! Did the message actually arrive? Port of `static/lib/delivery.js`.
//!
//! The server files the durable user row under `u-` + the client_msg_id the
//! client minted and returns it in /log, so a send is delivered when its own
//! id comes back in the transcript, and nothing else counts. HTTP 200 only
//! means the request was received.

use std::collections::HashSet;

use serde_json::Value;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DeliveryState {
    /// Request in flight.
    Pending,
    /// Server took it, not yet in the transcript.
    Sent,
    /// Its id came back in /log — actually delivered.
    Confirmed,
    /// Rejected, errored, or never showed up.
    Failed,
}

impl DeliveryState {
    fn unsettled(self) -> bool {
        matches!(self, Self::Pending | Self::Sent)
    }
}

/// The transcript id the server will file this send under.
pub fn server_id_for(client_msg_id: &str) -> String {
    if client_msg_id.is_empty() { String::new() } else { format!("u-{client_msg_id}") }
}

#[derive(Debug, Clone, PartialEq)]
pub struct DeliveryEntry {
    pub id: String,
    pub server_id: String,
    pub session: String,
    pub text: String,
    pub state: DeliveryState,
    pub at: i64,
    pub settled_at: Option<i64>,
    pub detail: String,
}

#[derive(Debug, Clone)]
pub struct DeliveryLog {
    pub entries: Vec<DeliveryEntry>,
    pub limit: usize,
}

impl Default for DeliveryLog {
    fn default() -> Self {
        Self { entries: Vec::new(), limit: 50 }
    }
}

impl DeliveryLog {
    pub fn record_send(&mut self, id: &str, session: &str, text: &str, at: i64) {
        if id.is_empty() {
            return;
        }
        self.entries.push(DeliveryEntry {
            id: id.to_owned(),
            server_id: server_id_for(id),
            session: session.to_owned(),
            text: text.chars().take(200).collect(),
            state: DeliveryState::Pending,
            at,
            settled_at: None,
            detail: String::new(),
        });
        if self.entries.len() > self.limit {
            let excess = self.entries.len() - self.limit;
            self.entries.drain(..excess);
        }
    }

    pub fn mark_state(&mut self, id: &str, state: DeliveryState, detail: &str, now: i64) {
        for entry in self.entries.iter_mut().filter(|e| e.id == id) {
            entry.state = state;
            if !detail.is_empty() {
                entry.detail = detail.to_owned();
            }
            if !state.unsettled() {
                entry.settled_at = Some(now);
            }
        }
    }

    /// A turn list from /log confirms every send whose id it contains.
    pub fn confirm_from_turns<'a>(&mut self, turns: impl IntoIterator<Item = &'a Value>, now: i64) {
        let ids: HashSet<&str> =
            turns.into_iter().filter_map(|t| t.get("id").and_then(Value::as_str)).collect();
        for entry in &mut self.entries {
            if entry.state.unsettled() && ids.contains(entry.server_id.as_str()) {
                entry.state = DeliveryState::Confirmed;
                entry.settled_at = Some(now);
            }
        }
    }

    /// Anything the server took but never filed is lost — preempted by the
    /// next message, or dropped as a duplicate. Both used to be silent.
    pub fn stale_sends(&self, now: i64, timeout_ms: i64) -> Vec<&DeliveryEntry> {
        self.entries.iter().filter(|e| e.state.unsettled() && now - e.at >= timeout_ms).collect()
    }

    /// Server ids still awaiting confirmation, in send order.
    pub fn pending_ids(&self) -> Vec<String> {
        let mut seen = HashSet::new();
        self.entries
            .iter()
            .filter(|e| e.state.unsettled() && seen.insert(e.server_id.clone()))
            .map(|e| e.server_id.clone())
            .collect()
    }

    pub fn confirmed_ids(&self) -> Vec<String> {
        self.entries
            .iter()
            .filter(|e| e.state == DeliveryState::Confirmed)
            .map(|e| e.id.clone())
            .collect()
    }
}
