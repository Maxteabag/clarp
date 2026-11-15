//! A conversation as an app reads it: `GET /log` (`contract/schemas/log.json`).
//!
//! Whether an empty optional field travels as `null` or is left out is not something the schema
//! says. These types leave it out; the conformance suite against the Python Host decides each case.

use serde::{Deserialize, Serialize};
use serde_json::Value;

/// One page of a conversation, or the changes since a revision.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct LogResponse {
    /// Changes when the agent starts a new conversation: a client then drops what it cached.
    pub conversation_id: String,
    pub turns: Vec<Turn>,
    /// The cursor for the next delta: `GET /log?after_revision=<this>`.
    pub latest_revision: u64,
    pub has_more: bool,
    /// The agent has no conversation yet.
    pub missing: bool,
    /// The client's copy can no longer be patched: reload the tail.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub replace_required: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub latest_ts: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub cwd: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub file: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub includes_automated: Option<bool>,
}

#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum Phase {
    Commentary,
    Final,
    #[serde(untagged)]
    Other(String),
}

/// One item of a conversation: something the user, the agent or the Host said.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct Turn {
    pub id: String,
    pub role: String,
    pub text: String,
    /// Grows whenever the item changes; a client upserts by `id`.
    pub revision: u64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub timestamp: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub kind: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tool_name: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tools: Option<Vec<Value>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub display_cells: Option<Vec<Value>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub origin: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub sender_agent_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub sender_name: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub sender_session: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub reply_to_agent_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub reply_to_name: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub reply_to_session: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub delivery: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub automation_kind: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub trace_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub phase: Option<Phase>,
    /// How the turn this item closed ended, on the item that closed it.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub turn: Option<TurnOutcome>,
}

#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum TurnStatus {
    Completed,
    Failed,
    Interrupted,
    #[serde(untagged)]
    Other(String),
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct TurnOutcome {
    pub turn_id: String,
    pub status: TurnStatus,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub started_at_ms: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub ended_at_ms: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub worked_ms: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tool_count: Option<u64>,
}
