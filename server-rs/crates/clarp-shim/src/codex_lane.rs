//! Control messages for one persistent Codex lane.
//!
//! Control and watch connections are separate from Codex's stdio. A Host can
//! reconnect after a crash and learn the lane's current claim before deciding
//! whether to start, follow, stop or release a turn.

use std::path::PathBuf;

use serde::{Deserialize, Serialize};
use serde_json::Value;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Claim {
    pub turn: u64,
    pub attempt: u32,
    pub spool: PathBuf,
    pub socket: PathBuf,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum GoalAction {
    Start,
    Pause,
    Resume,
    Clear,
}

impl GoalAction {
    /// Codex is the authority on the outcome. Start alone carries an objective.
    pub fn request(self, thread: &str, objective: String) -> (&'static str, Value) {
        use serde_json::json;
        match self {
            Self::Start => (
                "thread/goal/set",
                json!({"threadId": thread, "status": "active", "objective": objective}),
            ),
            Self::Resume => (
                "thread/goal/set",
                json!({"threadId": thread, "status": "active"}),
            ),
            Self::Pause => (
                "thread/goal/set",
                json!({"threadId": thread, "status": "paused"}),
            ),
            Self::Clear => ("thread/goal/clear", json!({"threadId": thread})),
        }
    }
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(tag = "op", rename_all = "kebab-case")]
pub enum Request {
    /// Obtain the provider goal and the lane's current claim on reconnection.
    Inspect,
    /// Read native state without loading the thread or activating a goal.
    Probe,
    /// Inspect first, then receive subsequent snapshots when something changes.
    Watch,
    /// Reopen a thread with a previously stored active goal in its reserved slot.
    Recover,
    /// Reserve the next spontaneous turn. The Host commits the attempt first.
    Reserve { claim: Claim },
    /// Start an ordinary prompt in its already admitted slot.
    Prompt {
        claim: Claim,
        text: String,
        model: Option<String>,
        effort: Option<String>,
    },
    /// Start and resume are allowed only while holding a goal reservation.
    Goal {
        action: GoalAction,
        objective: String,
    },
    /// Pause an active goal and interrupt the exact native turn, if present.
    Stop { turn: u64, attempt: u32 },
    /// Hand ownership back after the last claim is settled and the goal ended.
    Retire,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Snapshot {
    pub thread: String,
    pub instance: u32,
    pub revision: u64,
    pub known_goal: bool,
    /// The provider started too soon and was interrupted. Resume on the next claim.
    pub deferred_goal: bool,
    pub available: bool,
    pub goal: Option<Value>,
    pub claim: Option<Claim>,
    pub native_turn: Option<String>,
}

/// The lane never hands a spontaneous start to an unclaimed actor slot.
/// A prompted start must be bound by its RPC reply, since a spontaneous
/// notification can arrive while that reply is outstanding.
#[derive(Debug)]
pub enum Reservation {
    Goal(Claim),
    PromptPending { claim: Claim, request: u64 },
    Running { claim: Claim, native_turn: String },
}

impl Reservation {
    pub fn claim(&self) -> &Claim {
        match self {
            Self::Goal(claim) | Self::PromptPending { claim, .. } | Self::Running { claim, .. } => {
                claim
            }
        }
    }

    pub fn bind_goal(self, native_turn: String) -> Result<Self, Self> {
        match self {
            Self::Goal(claim) if !native_turn.is_empty() => {
                Ok(Self::Running { claim, native_turn })
            }
            other => Err(other),
        }
    }

    pub fn bind_prompt(self, reply: u64, native_turn: String) -> Result<Self, Self> {
        match self {
            Self::PromptPending { claim, request }
                if reply == request && !native_turn.is_empty() =>
            {
                Ok(Self::Running { claim, native_turn })
            }
            other => Err(other),
        }
    }

    pub fn native_turn(&self) -> Option<&str> {
        match self {
            Self::Running { native_turn, .. } => Some(native_turn),
            _ => None,
        }
    }
}
