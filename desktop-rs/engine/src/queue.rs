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
    /// Plain `/send`s on a `queue_steer` Host, by tag: refused because a
    /// turn runs after all (the agent's busy state lagged), they take over.
    take_over_on_refusal: HashMap<String, String>,
    /// The Host's answer to the last refused Send now (a turn still runs),
    /// and its chat. A reload keeps it: the Host reloads the queue with
    /// `queue-updated` on the same refusal.
    refused: String,
    refused_session: String,
    /// The Host lists `queue_steer`: a waiting message can be read into the
    /// running turn (`/steer`) or stop it and run now (`/take-over`).
    steer: bool,
    /// Steer now tags in flight, by the message they steer.
    steer_ids: HashMap<String, String>,
    /// Messages the Host refused to steer (409, `not_steerable`): they
    /// offer Stop and send instead, whatever a later read says.
    unsteerable: std::collections::HashSet<String>,
}

fn queue_item_id(item: &Value) -> &str {
    item.get("queue_id").and_then(Value::as_str).filter(|id| !id.is_empty()).or_else(|| item.get("id").and_then(Value::as_str)).unwrap_or_default()
}

/// The Host feature that offers Steer now and Take over.
pub const STEER_FEATURE: &str = "queue_steer";

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
    /// The Host offers Steer now (`queue_steer`) for `session`'s queue.
    pub fn turn_queue_steer(&self, session: &str) -> bool {
        on_peer!(self.peer_ref(session), |engine, local| engine.turn_queue_steer(local));
        self.queue.steer
    }
    pub(crate) fn queue_server_info(&mut self, info: &Object) {
        let has = info
            .get("capabilities")
            .and_then(|c| c.get("features"))
            .and_then(Value::as_array)
            .is_some_and(|features| features.iter().any(|f| f.as_str() == Some(STEER_FEATURE)));
        if has != self.queue.steer {
            self.queue.steer = has;
            // Its items gain or lose `steerable`: read them again.
            let session = self.queue.session.clone();
            if !session.is_empty() {
                self.load_turn_queue(&session);
            }
        }
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

    fn queue_action(&mut self, method: &str, path: &str, body: Value) -> Option<String> {
        if self.queue.session.is_empty() {
            return None;
        }
        let tag = format!("queue-action:{}", uuid::Uuid::new_v4());
        self.queue.action_sessions.insert(tag.clone(), self.queue.session.clone());
        // A new action answers the last refusal.
        if self.queue.refused_session == self.queue.session {
            self.queue.refused.clear();
        }
        if path.ends_with("/send") || path.ends_with("/steer") || path.ends_with("/take-over") {
            self.queue.sends.insert(tag.clone());
        }
        match method {
            "PUT" => self.api.put_json(&tag, path, body),
            "DELETE" => self.api.delete(&tag, path),
            _ => self.api.post_json(&tag, path, body, None),
        }
        Some(tag)
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

    /// Sends a queued turn now instead of after the running one. While the
    /// agent works, on a Host that offers it (`queue_steer`), "now" means now:
    /// the message is read into the running turn (`/steer`), or, where it
    /// cannot be, the running turn stops and this message runs (`/take-over`).
    /// `/send` alone only passes a held queue and is refused while a turn
    /// runs. An idle agent, or a Host without `queue_steer`, sends as before.
    pub fn send_queued_turn(&mut self, queue_id: &str) {
        on_peer!(self.queue_peer_mut(), |engine, _local| engine.send_queued_turn(queue_id));
        if queue_id.is_empty() {
            return;
        }
        let busy = self.roster().find(&self.queue.session).is_some_and(|a| a.busy);
        if self.queue.steer && busy {
            let item = self.queue.items.iter().find(|item| {
                item.get("queue_id").or_else(|| item.get("id")).and_then(Value::as_str) == Some(queue_id)
            });
            let steerable = item.is_some_and(|item| {
                item.get("steerable").and_then(Value::as_bool) == Some(true)
                    && item.get("kind").and_then(Value::as_str).is_none_or(|k| k == "waiting")
            });
            if steerable {
                self.steer_queued_turn(queue_id);
            } else {
                self.take_over_queued_turn(queue_id);
            }
            return;
        }
        let mut body = json!({});
        self.ask_voice(&mut body);
        let tag = self.queue_action("POST", &format!("{}/send", Self::queue_item_path(queue_id)), body);
        if let Some(tag) = tag.filter(|_| self.queue.steer) {
            self.queue.take_over_on_refusal.insert(tag, queue_id.to_owned());
        }
    }

    /// Reads a waiting message into the running turn now (`queue_steer`).
    pub fn steer_queued_turn(&mut self, queue_id: &str) {
        on_peer!(self.queue_peer_mut(), |engine, _local| engine.steer_queued_turn(queue_id));
        if !queue_id.is_empty() {
            let mut body = json!({});
            self.ask_voice(&mut body);
            if let Some(tag) = self.queue_action("POST", &format!("{}/steer", Self::queue_item_path(queue_id)), body) {
                self.queue.steer_ids.insert(tag, queue_id.to_owned());
            }
        }
    }

    /// Stops the running turn and runs this waiting message now
    /// (`queue_steer`).
    pub fn take_over_queued_turn(&mut self, queue_id: &str) {
        on_peer!(self.queue_peer_mut(), |engine, _local| engine.take_over_queued_turn(queue_id));
        if !queue_id.is_empty() {
            let mut body = json!({});
            self.ask_voice(&mut body);
            self.queue_action("POST", &format!("{}/take-over", Self::queue_item_path(queue_id)), body);
        }
    }

    /// The messages the Host refused to steer read as not steerable; ones
    /// gone from the queue are forgotten.
    fn mark_unsteerable(&mut self) {
        let unsteerable = &mut self.queue.unsteerable;
        unsteerable.retain(|id| self.queue.items.iter().any(|item| queue_item_id(item) == id));
        for item in self.queue.items.iter_mut() {
            if unsteerable.contains(queue_item_id(item))
                && let Some(object) = item.as_object_mut()
            {
                object.insert("steerable".to_owned(), Value::Bool(false));
            }
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
            self.mark_unsteerable();
            self.queue.paused = json::boolean(object, "paused");
            // The Host's queue is the truth: a count the agent list kept from
            // an older revision follows it (a stale strip never lingers).
            let revision = json::integer(object, "queue_revision").max(json::integer(object, "revision"));
            self.roster.apply_queue_truth(session, self.queue.items.len() as i32, self.queue.paused, revision);
            // Nothing left waiting: nothing left refused.
            if self.queue.items.is_empty() && self.queue.refused_session == session {
                self.queue.refused.clear();
            }
            self.queue.loading = false;
            self.queue.error.clear();
            self.changes.push(Change::Queue(session.to_owned()));
        } else if tag.starts_with("queue-action:") {
            self.queue.steer_ids.remove(tag);
            self.queue.sends.remove(tag);
            self.queue.take_over_on_refusal.remove(tag);
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
            // Steer now refused (409, not steerable): that message offers
            // Stop and send from now on.
            if let Some(queue_id) = self.queue.steer_ids.remove(tag)
                && status == 409
            {
                self.queue.unsteerable.insert(queue_id);
                self.mark_unsteerable();
            }
            // Send now refused because a turn runs after all: it takes over.
            if let Some(queue_id) = self.queue.take_over_on_refusal.remove(tag)
                && status == 409
            {
                self.queue.sends.remove(tag);
                self.queue.action_sessions.remove(tag);
                self.take_over_queued_turn(&queue_id);
                return true;
            }
            let session = self.queue.action_sessions.remove(tag).unwrap_or_default();
            // Refused in the Host's words: a turn still runs, the message is
            // not steerable, another sender's, or already gone (404).
            if self.queue.sends.remove(tag) && matches!(status, 404 | 409) {
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
