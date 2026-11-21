//! The fleet as an app sees it at start: `GET /agents/snapshot` (`contract/schemas/agents-snapshot.json`).

use serde::{Deserialize, Serialize};
use serde_json::Value;

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct Snapshot {
    pub agents: Vec<AgentSummary>,
    /// The session that is open, when one is.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub focus: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub roster: Option<Vec<String>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub personas: Option<Vec<Value>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub available_mcp_servers: Option<Vec<String>>,
}

/// One agent in the snapshot: who it is, what it is doing, and where its conversation stands.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct AgentSummary {
    pub agent_id: String,
    pub session: String,
    pub persona: String,
    pub backend: String,
    pub cwd: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub moved: Option<AgentMove>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub model: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub effort: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub voice_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub avatar_symbol: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub avatar_url: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub model_avatar_url: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub backend_session_id: Option<String>,
    /// With `head_revision`, what a client compares its cached conversation against.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub conversation_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub head_revision: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub last_message_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub last_message: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub alive: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub busy: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub focused: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub latest_state: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub latest_state_ts: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub turn_started_at: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub last_activity: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub last_turn_end: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub status_text: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub compacting: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub context_tokens: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub context_window: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub queued_turn_count: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub queued_turn_revision: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub queue_paused: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub muted: Option<bool>,
    /// An epoch in milliseconds or an ISO time, depending on the Host's age.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub archived_at: Option<Value>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub heartbeat_enabled: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub dreaming_enabled: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub schedules: Option<Vec<Schedule>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub team_ids: Option<Vec<String>>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Schedule {
    pub schedule_id: String,
    pub agent_id: String,
    pub session: String,
    pub name: String,
    pub cron_expression: String,
    pub prompt: String,
    pub enabled: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub last_run_at: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub next_run_at: Option<i64>,
    pub created_at: i64,
    pub updated_at: i64,
}

/// Where the agent moved, and when the source set its mark.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AgentMove {
    pub to: MoveTarget,
    pub at: i64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct MoveTarget {
    pub host: String,
    pub id: String,
}
