//! The turn queue of one chat (the Qt controller's turn-queue section):
//! the turns waiting behind the running one, and editing, sending or
//! deleting them. Every action reloads the queue it changed.

use std::collections::HashMap;

use clarp_core::json::{self, Object};
use serde_json::{Value, json};

use crate::{Change, Engine};

#[derive(Default)]
pub(crate) struct TurnQueue {
    items: Vec<Value>,
    session: String,
    paused: bool,
    loading: bool,
    error: String,
    generation: u64,
    /// tag → the session whose queue the action changes
    action_sessions: HashMap<String, String>,
    /// The Send now tags in flight.
    sends: std::collections::HashSet<String>,
    /// The Host's answer to the last refused Send now (a turn still runs),
    /// and its chat. A reload keeps it: the Host reloads the queue with
    /// `queue-updated` on the same refusal.
    refused: String,
    refused_session: String,
}

impl Engine {
    /// The loaded queue's items when it is `session`'s, else none.
    pub fn turn_queue(&self, session: &str) -> Vec<Value> {
        on_peer!(self.peer_ref(session), |engine, local| engine.turn_queue(local));
        if session.is_empty() || session != self.queue.session { Vec::new() } else { self.queue.items.clone() }
    }
    /// The chat whose queue is loaded (or loading).
    pub fn turn_queue_session(&self) -> &str {
        if let Some(session) = self.queue_peer_session() {
            return session;
        }
        &self.queue.session
    }
    pub fn turn_queue_paused(&self) -> bool {
        on_peer!(self.queue_peer_ref(), |engine, _local| engine.turn_queue_paused());
        self.queue.paused
    }
    pub fn turn_queue_loading(&self) -> bool {
        on_peer!(self.queue_peer_ref(), |engine, _local| engine.turn_queue_loading());
        self.queue.loading
    }
    /// The Host's answer to `session`'s last refused Send now, in its own
    /// words ("agent is still working"), or "".
    pub fn turn_queue_refusal(&self, session: &str) -> String {
        on_peer!(self.peer_ref(session), |engine, local| engine.turn_queue_refusal(local));
        if session.is_empty() || session != self.queue.refused_session { String::new() } else { self.queue.refused.clone() }
    }
    pub fn turn_queue_error(&self) -> &str {
        on_peer!(self.queue_peer_ref(), |engine, _local| engine.turn_queue_error());
        &self.queue.error
    }

    pub fn load_turn_queue(&mut self, session: &str) {
        if session.is_empty() {
            return;
        }
        self.note_queue_host(session);
        on_peer!(self.peer_mut(session), |engine, local| engine.load_turn_queue(local));
        if self.queue.session != session {
            self.queue.items.clear();
            self.queue.paused = false;
        }
        self.queue.session = session.to_owned();
        self.queue.loading = true;
        self.queue.error.clear();
        self.queue.generation += 1;
        self.changes.push(Change::Queue(session.to_owned()));
        self.api.get(&format!("turn-queue:{}:{session}", self.queue.generation), "/turn-queue", &[("session", session)]);
    }

    fn queue_action(&mut self, method: &str, path: &str, body: Value) {
        if self.queue.session.is_empty() {
            return;
        }
        let tag = format!("queue-action:{}", uuid::Uuid::new_v4());
        self.queue.action_sessions.insert(tag.clone(), self.queue.session.clone());
        // A new action answers the last refusal.
        if self.queue.refused_session == self.queue.session {
            self.queue.refused.clear();
        }
        if path.ends_with("/send") {
            self.queue.sends.insert(tag.clone());
        }
        match method {
            "PUT" => self.api.put_json(&tag, path, body),
            "DELETE" => self.api.delete(&tag, path),
            _ => self.api.post_json(&tag, path, body, None),
        }
    }

    fn queue_item_path(id: &str) -> String {
        format!("/turn-queue/{}", clarp_core::endpoint::percent_encode_segment(id))
    }

    pub fn update_queued_turn(&mut self, queue_id: &str, text: &str) {
        on_peer!(self.queue_peer_mut(), |engine, _local| engine.update_queued_turn(queue_id, text));
        let text = text.trim();
        if !queue_id.is_empty() && !text.is_empty() {
            let mut body = json!({"text": text});
            self.ask_voice(&mut body);
            self.queue_action("PUT", &Self::queue_item_path(queue_id), body);
        }
    }

    pub fn delete_queued_turn(&mut self, queue_id: &str) {
        on_peer!(self.queue_peer_mut(), |engine, _local| engine.delete_queued_turn(queue_id));
        if !queue_id.is_empty() {
            self.queue_action("DELETE", &Self::queue_item_path(queue_id), Value::Null);
        }
    }

    /// Sends a queued turn now instead of after the running one.
    pub fn send_queued_turn(&mut self, queue_id: &str) {
        on_peer!(self.queue_peer_mut(), |engine, _local| engine.send_queued_turn(queue_id));
        if !queue_id.is_empty() {
            let mut body = json!({});
            self.ask_voice(&mut body);
            self.queue_action("POST", &format!("{}/send", Self::queue_item_path(queue_id)), body);
        }
    }

    /// An event for the whole engine; the loaded queue follows its chat's
    /// `queue-updated`.
    pub(crate) fn lifecycle_event(&mut self, kind: &str, session: &str) {
        if kind == "queue-updated" && !session.is_empty() && session == self.queue.session {
            let session = session.to_owned();
            self.load_turn_queue(&session);
        }
    }

    pub(crate) fn queue_json(&mut self, tag: &str, object: &Object) -> bool {
        if let Some(rest) = tag.strip_prefix("turn-queue:") {
            let (generation, session) = rest.split_once(':').unwrap_or((rest, ""));
            if generation.parse::<u64>().ok() != Some(self.queue.generation) || session != self.queue.session {
                return true;
            }
            self.queue.items = json::array(object, "items");
            self.queue.paused = json::boolean(object, "paused");
            // Nothing left waiting: nothing left refused.
            if self.queue.items.is_empty() && self.queue.refused_session == session {
                self.queue.refused.clear();
            }
            self.queue.loading = false;
            self.queue.error.clear();
            self.changes.push(Change::Queue(session.to_owned()));
        } else if tag.starts_with("queue-action:") {
            self.queue.sends.remove(tag);
            let session = self.queue.action_sessions.remove(tag).unwrap_or_default();
            if !session.is_empty() && session == self.queue.session {
                self.load_turn_queue(&session);
            }
        } else {
            return false;
        }
        true
    }

    /// A queue failure is the queue's error, not the window's. A Send now
    /// the Host refused because a turn still runs (409) is its refusal, in
    /// the Host's words: the message waits on.
    pub(crate) fn queue_failure(&mut self, tag: &str, message: &str, status: u16) -> bool {
        let detail = if status > 0 { format!("{message} (HTTP {status})") } else { message.to_owned() };
        if let Some(rest) = tag.strip_prefix("turn-queue:") {
            if rest.split(':').next().and_then(|g| g.parse::<u64>().ok()) != Some(self.queue.generation) {
                return true;
            }
            self.queue.loading = false;
        } else if tag.starts_with("queue-action:") {
            let session = self.queue.action_sessions.remove(tag).unwrap_or_default();
            if self.queue.sends.remove(tag) && status == 409 {
                self.queue.refused = if message.trim().is_empty() { "a turn still runs".to_owned() } else { message.trim().to_owned() };
                self.queue.refused_session = session.clone();
                self.changes.push(Change::Queue(session));
                return true;
            }
        } else {
            return false;
        }
        self.queue.error = detail;
        let session = self.queue.session.clone();
        self.changes.push(Change::Queue(session));
        true
    }
}
