//! Who stops or repairs an agent, and why (contract 61, the feature `turn_slot_repair`).
//!
//! A Stop (`POST /stop`) and a repair (`POST /repair-slot`) say who acts (`actor`: `user`, or
//! `agent:<session>`) and why (`reason`). The Host believes an agent where the agent's own turn
//! presented its token, and the user where a paired device asks; anything else is the caller's
//! claim, and is told as unverified. What was said is kept on the state the Stop or the repair
//! leaves, and an app reads it on that state's activity while it is the latest. The Python Host's
//! `Handler._control_actor`, the reason as its handlers read it, `_stop_agent_session`'s reason
//! for the goals it pauses, and `activity._control_provenance`; held to them by
//! `tests/turn_slot_repair_vectors.rs`.

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};

use crate::python;

/// Who acts where nobody says otherwise.
pub const USER: &str = "user";
/// What a state left by a Stop says it came from.
pub const STOPPED: &str = "user_stop";
/// What a note left by a repair says it came from.
pub const REPAIRED: &str = "slot_repair";
/// How many signs of a claimed actor, and of a reason, are kept.
const ACTOR_MOST: usize = 80;
const REASON_MOST: usize = 200;

/// Who did it and why: one value for a Stop and for a repair.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct Control {
    /// `user`, or `agent:<session>`, or whatever a caller claimed.
    pub actor: String,
    /// The Host knows it is so: by an agent's own turn, or a paired device acting as the user.
    pub verified: bool,
    pub reason: String,
}

impl Control {
    /// Done by this actor, as its claim, for no reason given.
    pub fn by(actor: impl Into<String>) -> Self {
        Self {
            actor: actor.into(),
            ..Self::default()
        }
    }

    /// Whether it is the user's: anything but an agent's that the Host knows to be that agent's.
    /// Only the user's Stop does what is the user's to do: it drops what the agent had yet to say
    /// aloud (here, as on the Python Host), and on the Python Host it withdraws the user's sends
    /// not yet launched (`_stop_agent_session`'s `fence_user_receipts`). The one answer to the
    /// question for every reader of a Stop.
    pub fn is_the_users(&self) -> bool {
        !(self.verified && self.actor.starts_with("agent:"))
    }
}

/// A field of a request as the Python Host reads it (`str(value or "")`): nothing for what
/// Python takes for nothing, a text as it is, and anything else as Python writes it.
pub fn field(value: Option<&Value>) -> String {
    match value {
        Some(Value::String(text)) => text.clone(),
        Some(value) if python::truthy(value) => python::repr(value),
        _ => String::new(),
    }
}

/// Who a request says acts: without the blanks at its ends, cut to 80 signs, and `user` where
/// it says nobody.
pub fn claimed(value: Option<&Value>) -> String {
    let actor = field(value);
    match python::first(python::strip(&actor), ACTOR_MOST) {
        "" => USER.to_string(),
        actor => actor.to_string(),
    }
}

/// Why, as a request says it: without the blanks at its ends, cut to 200 signs.
pub fn reason(value: Option<&Value>) -> String {
    let reason = field(value);
    python::first(python::strip(&reason), REASON_MOST).to_string()
}

/// Who acts, and whether the Host knows it: the agent whose own turn asks (`turn_of`: its
/// session), whatever was claimed; the user where a paired device claims to be the user;
/// otherwise the claim, unverified.
pub fn acting(claimed: &str, turn_of: Option<&str>, device: bool) -> (String, bool) {
    match turn_of {
        Some(session) => (format!("agent:{session}"), true),
        None if device && claimed == USER => (USER.to_string(), true),
        None => (claimed.to_string(), false),
    }
}

/// Why a Stop paused the agent's goals, as their pause says it.
pub fn paused_by(control: &Control) -> String {
    let by = if control.actor == USER {
        "User stopped this agent".to_string()
    } else if control.verified {
        format!("Stopped by {}", control.actor)
    } else {
        format!("Stopped by {} (unverified)", control.actor)
    };
    match control.reason.as_str() {
        "" => by,
        reason => format!("{by}: {reason}"),
    }
}

/// What a state's detail says of who acted and why: of a Stop, its actor, whether it is known
/// and why; of a repair, the same of the repair and the turn it gave back (`released`).
pub fn marked(source: &str, control: &Control, released: &str) -> Map<String, Value> {
    let marks = match source {
        STOPPED => json!({
            "source": STOPPED,
            "stop_actor": control.actor,
            "stop_actor_verified": control.verified,
            "stop_reason": control.reason,
        }),
        REPAIRED => json!({
            "source": REPAIRED,
            "repair_actor": control.actor,
            "repair_actor_verified": control.verified,
            "repair_reason": control.reason,
            "released_trace": released,
        }),
        _ => json!({}),
    };
    match marks {
        Value::Object(marks) => marks,
        _ => Map::new(),
    }
}

/// What a state's activity says of who acted and why, from the state's detail: the keys of its
/// source that it has, and nothing where the detail says nothing of it (a Stop written before
/// who stopped was kept tells nothing, never that it was the user).
pub fn provenance(detail: &Map<String, Value>) -> Map<String, Value> {
    let keys: &[&str] = match detail.get("source").and_then(Value::as_str) {
        Some(STOPPED) if detail.contains_key("stop_actor") => {
            &["stop_actor", "stop_actor_verified", "stop_reason"]
        }
        Some(REPAIRED) => &[
            "repair_actor",
            "repair_actor_verified",
            "repair_reason",
            "released_trace",
        ],
        _ => &[],
    };
    let kept = keys
        .iter()
        .filter_map(|key| Some(((*key).to_string(), detail.get(*key)?.clone())));
    kept.collect()
}
