//! R77 binds audio to the admission that owns the turn. Only the opening
//! record stores the secret fields used to build the phone's tag.
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Kind {
    Initiative,
    GreetingReply,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct Binding {
    pub turn_id: u64,
    pub kind: Kind,
    pub admission_id: String,
    pub epoch: u64,
}

impl Binding {
    /// A later acknowledgment of the same admission leaves its original
    /// epoch and ownership intact.
    pub fn record(self, opening: &mut Value) -> bool {
        if self.turn_id == 0 || self.admission_id.is_empty() {
            return false;
        }
        if opening["clip_bindings"].is_null() {
            opening["clip_bindings"] = json!([]);
        }
        let Some(bindings) = opening["clip_bindings"].as_array_mut() else {
            return false;
        };
        if bindings.iter().any(|old| {
            old["turn_id"] == self.turn_id
                || old["admission_id"] == self.admission_id && old["kind"] == json!(self.kind)
        }) {
            return false;
        }
        bindings.push(json!(self));
        true
    }

    pub fn audio(&self, opening: &Value) -> Value {
        let intent = &opening["intent"];
        let skip = !opening["retired"].is_null()
            || self.kind == Kind::Initiative
                && (intent["admission_id"] != self.admission_id
                    || matches!(intent["state"].as_str(), Some("cancelled" | "unknown"))
                    || !opening["late_greeting"].is_null());
        json!({"skip":skip,"tag":{"call_id":opening["call_id"],"opening":{
            "ring_id":opening["ring_id"],"token":opening["token"],"epoch":self.epoch,
            "kind":self.kind,"admission_id":self.admission_id}}})
    }
}

pub fn key(agent_id: &str, turn_id: u64) -> Option<String> {
    (!agent_id.is_empty() && turn_id != 0).then(|| format!("{agent_id}:{turn_id}"))
}

/// Keep the retired record until the call has been over for a full day.
pub fn expired(retired_at: Option<i64>, call_ended_at: Option<i64>, now: i64) -> bool {
    retired_at.is_some_and(|retired| retired <= now)
        && call_ended_at.is_some_and(|ended| now.saturating_sub(ended) >= 86_400_000)
}
