//! What the Host answers to the core commands (`contract/schemas/send.json`, `stop.json`, `select.json`).

use serde::{Deserialize, Serialize};
use serde_json::Value;

/// The answer to `POST /send`.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct SendResponse {
    pub ok: bool,
    pub session: String,
    /// How the message was taken: started now, queued, steered into a running turn, and so on.
    pub dispatch: String,
    pub trace_id: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub queued: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub queue_depth: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub queue_revision: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
    /// What the router for spoken commands decided, when it took part.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub orchestrator: Option<Value>,
}

/// The answer to `POST /stop`.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct StopResponse {
    pub ok: bool,
    /// How many running turns were ended.
    pub terminated: u64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub stop_actor: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub stop_reason: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub stop_actor_verified: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub queue_paused: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub goals_paused: Option<u64>,
}

/// The answer to `POST /select`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SelectResponse {
    pub ok: bool,
    pub session: String,
}
