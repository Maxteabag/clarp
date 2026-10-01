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
}

impl Engine {
    /// The loaded queue's items when it is `session`'s, else none.
    pub fn turn_queue(&self, session: &str) -> Vec<Value> {
        if session.is_empty() || session != self.queue.session { Vec::new() } else { self.queue.items.clone() }
    }
    /// The chat whose queue is loaded (or loading).
    pub fn turn_queue_session(&self) -> &str {
        &self.queue.session
    }
    pub fn turn_queue_paused(&self) -> bool {
        self.queue.paused
    }
    pub fn turn_queue_loading(&self) -> bool {
        self.queue.loading
    }
    pub fn turn_queue_error(&self) -> &str {
        &self.queue.error
    }

    pub fn load_turn_queue(&mut self, session: &str) {
        if session.is_empty() {
            return;
        }
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
        let text = text.trim();
        if !queue_id.is_empty() && !text.is_empty() {
            self.queue_action("PUT", &Self::queue_item_path(queue_id), json!({"text": text}));
        }
    }

    pub fn delete_queued_turn(&mut self, queue_id: &str) {
        if !queue_id.is_empty() {
            self.queue_action("DELETE", &Self::queue_item_path(queue_id), Value::Null);
        }
    }

    /// Sends a queued turn now instead of after the running one.
    pub fn send_queued_turn(&mut self, queue_id: &str) {
        if !queue_id.is_empty() {
            self.queue_action("POST", &format!("{}/send", Self::queue_item_path(queue_id)), json!({}));
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
            self.queue.loading = false;
            self.queue.error.clear();
            self.changes.push(Change::Queue(session.to_owned()));
        } else if tag.starts_with("queue-action:") {
            let session = self.queue.action_sessions.remove(tag).unwrap_or_default();
            if !session.is_empty() && session == self.queue.session {
                self.load_turn_queue(&session);
            }
        } else {
            return false;
        }
        true
    }

    /// A queue failure is the queue's error, not the window's.
    pub(crate) fn queue_failure(&mut self, tag: &str, detail: &str) -> bool {
        if let Some(rest) = tag.strip_prefix("turn-queue:") {
            if rest.split(':').next().and_then(|g| g.parse::<u64>().ok()) != Some(self.queue.generation) {
                return true;
            }
            self.queue.loading = false;
        } else if tag.starts_with("queue-action:") {
            self.queue.action_sessions.remove(tag);
        } else {
            return false;
        }
        self.queue.error = detail.to_owned();
        let session = self.queue.session.clone();
        self.changes.push(Change::Queue(session));
        true
    }
}
