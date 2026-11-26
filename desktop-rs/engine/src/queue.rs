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
    /// Send now on a `queue_steer` Host, by the tag of its step in flight:
    /// a step the Host refuses (409) goes on to the next way of sending it
    /// now (`Step`), so a turn that cannot be steered is stopped instead,
    /// and one that ended meanwhile is sent to plainly.
    chains: HashMap<String, Chain>,
    /// The Host's answer to the last refused Send now (a turn still runs),
    /// and its chat. A reload keeps it: the Host reloads the queue with
    /// `queue-updated` on the same refusal.
    refused: String,
    refused_session: String,
    /// The Host lists `queue_steer`: a waiting message can be read into the
    /// running turn (`/steer`) or stop it and run now (`/take-over`).
    steer: bool,
}

/// The Host feature that offers Steer now and Take over.
pub const STEER_FEATURE: &str = "queue_steer";

/// The ways of sending a waiting message now, in the order Send now tries
/// them after a refusal.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Step {
    /// Read into the running turn (`/steer`).
    Steer,
    /// Stop the running turn and run it (`/take-over`).
    TakeOver,
    /// Send it (`/send`): passes a held queue, refused while a turn runs.
    Send,
}

struct Chain {
    queue_id: String,
    tried: Vec<Step>,
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
            self.changes.push(Change::Queue(self.queue.session.clone()));
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
    /// cannot be, the running turn stops and this message runs
    /// (`/take-over`). `/send`
    /// alone only passes a held queue and is refused while a turn runs. An
    /// idle agent, or a Host without `queue_steer`, sends as before. A step
    /// the Host refuses goes on to the next (`queue_failure`).
    pub fn send_queued_turn(&mut self, queue_id: &str) {
        on_peer!(self.queue_peer_mut(), |engine, _local| engine.send_queued_turn(queue_id));
        if queue_id.is_empty() {
            return;
        }
        let busy = self.roster().find(&self.queue.session).is_some_and(|a| a.busy);
        let first = if !self.queue.steer || !busy {
            Step::Send
        } else if self.queued_steerable(queue_id) {
            Step::Steer
        } else {
            Step::TakeOver
        };
        self.queue_step(queue_id, first, Vec::new());
    }

    /// Reads a waiting message into the running turn now (`queue_steer`);
    /// where the turn cannot take it, it stops and the message runs.
    pub fn steer_queued_turn(&mut self, queue_id: &str) {
        on_peer!(self.queue_peer_mut(), |engine, _local| engine.steer_queued_turn(queue_id));
        if !queue_id.is_empty() {
            self.queue_step(queue_id, Step::Steer, Vec::new());
        }
    }

    /// Stops the running turn and runs this waiting message now
    /// (`queue_steer`); with no turn left to stop it is sent.
    pub fn take_over_queued_turn(&mut self, queue_id: &str) {
        on_peer!(self.queue_peer_mut(), |engine, _local| engine.take_over_queued_turn(queue_id));
        if !queue_id.is_empty() {
            self.queue_step(queue_id, Step::TakeOver, Vec::new());
        }
    }

    /// The loaded item may be read into the running turn: the Host says it
    /// is `steerable`, and it met the busy agent (not `kind: later`).
    fn queued_steerable(&self, queue_id: &str) -> bool {
        self.queue
            .items
            .iter()
            .find(|item| item.get("queue_id").or_else(|| item.get("id")).and_then(Value::as_str) == Some(queue_id))
            .is_some_and(|item| {
                item.get("steerable").and_then(Value::as_bool) == Some(true)
                    && item.get("kind").and_then(Value::as_str).is_none_or(|k| k == "waiting")
            })
    }

    /// Sends one step of Send now. A plain send says whether to speak the
    /// answer as the composer's send does (`synthesize_audio`): a Host that
    /// hears nothing speaks nothing.
    fn queue_step(&mut self, queue_id: &str, step: Step, mut tried: Vec<Step>) {
        let (verb, mut body) = match step {
            Step::Steer => ("steer", json!({})),
            Step::TakeOver => ("take-over", json!({})),
            Step::Send => ("send", json!({"synthesize_audio": !self.muted})),
        };
        if step != Step::Send {
            self.ask_voice(&mut body);
        }
        let tag = self.queue_action("POST", &format!("{}/{verb}", Self::queue_item_path(queue_id)), body);
        if let Some(tag) = tag.filter(|_| self.queue.steer) {
            tried.push(step);
            self.queue.chains.insert(tag, Chain { queue_id: queue_id.to_owned(), tried });
        }
    }

    /// A Send now, steer or take-over of `session`'s runs: the line's
    /// action shows it is under way and takes no second press.
    pub fn turn_queue_acting(&self, session: &str) -> bool {
        on_peer!(self.peer_ref(session), |engine, local| engine.turn_queue_acting(local));
        self.queue.sends.iter().any(|tag| self.queue.action_sessions.get(tag).is_some_and(|s| s == session))
    }

    /// The next way to send a refused message now, or None when every way
    /// was refused.
    fn next_step(&self, chain: &Chain) -> Option<Step> {
        let steer = self.queued_steerable(&chain.queue_id).then_some(Step::Steer);
        [steer, Some(Step::TakeOver), Some(Step::Send)].into_iter().flatten().find(|step| !chain.tried.contains(step))
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
            self.queue.sends.remove(tag);
            let session = self.queue.action_sessions.remove(tag).unwrap_or_default();
            self.queue.chains.remove(tag);
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
            // Send now refused (a turn runs after all, it cannot be steered,
            // or it ended meanwhile): the next way of sending it now, until
            // none is left; then it waits on, and says so calmly.
            let chain = self.queue.chains.remove(tag);
            if let Some(chain) = chain.filter(|_| status == 409) {
                self.queue.sends.remove(tag);
                let session = self.queue.action_sessions.remove(tag).unwrap_or_default();
                if let Some(next) = self.next_step(&chain).filter(|_| session == self.queue.session) {
                    self.queue_step(&chain.queue_id, next, chain.tried);
                } else {
                    self.queue.refused = clarp_core::queue_line::STILL_WAITING.to_owned();
                    self.queue.refused_session = session.clone();
                    self.changes.push(Change::Queue(session.clone()));
                    if session == self.queue.session {
                        self.load_turn_queue(&session);
                    }
                }
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
