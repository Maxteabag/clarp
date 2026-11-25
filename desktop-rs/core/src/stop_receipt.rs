//! What a Stop did, as the Host committed it (Host contract 61), and the one
//! line a chat shows for it.
//!
//! `POST /stop` answers `{ok, terminated}` and, from contract 61, the actor
//! (`stop_actor`, `stop_actor_verified`), the reason and the committed
//! effects read back after the Stop (`queue_paused`, `goals_paused`). Every
//! newer field is optional and its absence means unknown: an older Host says
//! nothing about them, so nothing here becomes `false` or `0` by default, and
//! no effect is ever inferred from `terminated`. The interrupted state's
//! detail (`source: user_stop`, over SSE `agent-state`) carries the actor and
//! the reason when another device or an agent stopped the chat.
//!
//! The line says what Stop did and never what will happen next: Stop pauses,
//! it does not resume. It is plain: when you stopped the chat yourself the
//! transcript already says it was stopped, so the line names only what is
//! worth knowing beyond that (messages left waiting, goals paused) and is
//! empty otherwise.
//!
//! A Host that lists `stop_receipt_text` (Rust Host contract 184) tells the
//! Stop in one sentence of its own: `text` on the `/stop` answer, `stop_text`
//! in the Stop's detail. Where it does, the chat shows that sentence in place
//! of the line built here; the fields stay read for what acts on them.

use serde_json::Value;

use crate::json::Object;

/// Where a receipt came from: our own `/stop`'s answer, or the interrupted
/// state's detail (someone, anywhere, stopped the chat).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Origin {
    Response,
    Detail,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StopReceipt {
    pub origin: Origin,
    pub ok: Option<bool>,
    pub terminated: Option<i64>,
    pub actor: Option<String>,
    pub actor_verified: Option<bool>,
    pub reason: Option<String>,
    pub queue_paused: Option<bool>,
    pub goals_paused: Option<u64>,
    /// The Host's own sentence for the Stop (feature `stop_receipt_text`).
    pub text: Option<String>,
}

/// The Host tells a Stop in one sentence (Rust Host contract 184).
pub const FEATURE: &str = "stop_receipt_text";

/// What the chat says: a line, or (the Stop changed nothing) an error.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Said {
    Line(String),
    Error(String),
}

fn text(object: &Object, key: &str) -> Option<String> {
    object.get(key).and_then(Value::as_str).map(str::trim).filter(|s| !s.is_empty()).map(str::to_owned)
}

fn flag(object: &Object, key: &str) -> Option<bool> {
    object.get(key).and_then(Value::as_bool)
}

impl StopReceipt {
    fn read(object: &Object, origin: Origin) -> Self {
        Self {
            origin,
            ok: flag(object, "ok"),
            terminated: object.get("terminated").and_then(Value::as_i64),
            actor: text(object, "stop_actor"),
            actor_verified: flag(object, "stop_actor_verified"),
            reason: text(object, "stop_reason"),
            queue_paused: flag(object, "queue_paused"),
            goals_paused: object.get("goals_paused").and_then(Value::as_u64),
            text: text(object, if origin == Origin::Response { "text" } else { "stop_text" }),
        }
    }

    /// A Host without the feature: its sentence (if any field looks like
    /// one) is not shown, and the line is built here as before.
    pub fn without_host_text(mut self) -> Self {
        self.text = None;
        self
    }

    /// `POST /stop`'s answer. A field of the wrong type is unknown too.
    pub fn from_response(object: &Object) -> Self {
        Self::read(object, Origin::Response)
    }

    /// An `agent-state` event's (or state row's) detail, when it is a Stop's
    /// (`source: user_stop`); the detail carries no effects.
    pub fn from_detail(detail: &Object) -> Option<Self> {
        if detail.get("source").and_then(Value::as_str) != Some("user_stop") {
            return None;
        }
        let mut receipt = Self::read(detail, Origin::Detail);
        (receipt.ok, receipt.terminated, receipt.queue_paused, receipt.goals_paused) = (None, None, None, None);
        Some(receipt)
    }

    /// The Host answered with contract 61's fields (any of them).
    fn from_newer_host(&self) -> bool {
        self.actor.is_some() || self.actor_verified.is_some() || self.reason.is_some() || self.effects_known()
    }

    fn effects_known(&self) -> bool {
        self.queue_paused.is_some() || self.goals_paused.is_some()
    }

    /// The actor is you (a user's Stop, from any of your devices).
    fn by_you(&self) -> bool {
        self.actor.as_deref() == Some("user")
    }

    /// " by Theo", " by ops (unverified)"; "" when the actor is unknown or
    /// is you. `name` gives an agent session's display name.
    fn by(&self, name: &dyn Fn(&str) -> Option<String>) -> String {
        let Some(actor) = &self.actor else { return String::new() };
        if self.by_you() {
            return String::new();
        }
        let who = match actor.strip_prefix("agent:") {
            Some(session) => name(session).unwrap_or_else(|| session.to_owned()),
            None => actor.clone(),
        };
        // Someone else's Stop the Host could not confirm says so.
        let mark = if self.actor_verified == Some(false) { " (unverified)" } else { "" };
        format!(" by {who}{mark}")
    }

    /// The chat's line ("" when the transcript's own Stop line says it all).
    /// `name` resolves `agent:<session>` actors; `waiting` says whether the
    /// chat has messages queued, the only time its queue is worth naming.
    pub fn said(&self, name: &dyn Fn(&str) -> Option<String>, waiting: bool) -> Said {
        if self.origin == Origin::Response {
            if self.ok == Some(false) {
                return Said::Error("Stop did not take effect".into());
            }
            // A newer Host that terminated nothing and committed no effect
            // did nothing (the runtime refused, or there was no such agent).
            if self.from_newer_host() && !self.effects_known() && self.terminated == Some(0) {
                return Said::Error("Stop did not take effect".into());
            }
        }
        if let Some(text) = &self.text {
            return Said::Line(text.clone());
        }
        let mut parts = Vec::new();
        if !self.by_you() {
            let mut head = format!("Stopped{}", self.by(name));
            if let Some(reason) = &self.reason {
                head = format!("{head}: {reason}");
            }
            parts.push(head);
        } else if let Some(reason) = &self.reason {
            parts.push(reason.clone());
        }
        if waiting {
            match self.queue_paused {
                Some(true) => parts.push("queue paused".into()),
                Some(false) => parts.push("queue not paused".into()),
                None => {}
            }
        }
        match self.goals_paused {
            Some(0) | None => {}
            Some(1) => parts.push("1 goal paused".into()),
            Some(n) => parts.push(format!("{n} goals paused")),
        }
        if self.origin == Origin::Response && !self.effects_known() && self.actor.is_none() {
            if !self.from_newer_host() && self.terminated == Some(0) {
                parts[0] = "Stop sent".into();
            }
            parts.push("effects unknown".into());
        }
        // A line that starts with an effect ("Queue paused") reads as a sentence.
        let line = parts.join(" · ");
        let mut chars = line.chars();
        Said::Line(chars.next().map(|c| c.to_uppercase().chain(chars).collect()).unwrap_or_default())
    }
}
