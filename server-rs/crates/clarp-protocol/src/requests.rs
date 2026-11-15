//! What an app says to the Host with the core commands (`docs/protocol.md`, sections 4, 5, 7, 8).

use serde::{Deserialize, Serialize};
use serde_json::Value;

/// `POST /send`: a message for an agent.
#[derive(Debug, Clone, PartialEq, Default, Serialize, Deserialize)]
pub struct SendRequest {
    pub session: String,
    pub text: String,
    /// The idempotency key. The message is filed as `u-` and this id; a repeated request with the
    /// same id is the same message.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub client_msg_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub trace_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub synthesize_audio: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub hands_free: Option<bool>,
    /// Wait behind a running turn instead of taking its place.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub queue_if_busy: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub transcription_id: Option<String>,
    /// The app's short name, such as `desktop`.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub client: Option<String>,
    /// `agent` when another agent wrote the message; a user's message leaves it out.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub origin: Option<String>,
    /// Who wrote it, for a message from another agent: its session, its agent id, or an object.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub sender: Option<Value>,
}

/// `POST /stop`, `POST /select` and the other commands that only name an agent.
#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize, Deserialize)]
pub struct SessionRequest {
    pub session: String,
}

/// `POST /agents`: a new agent.
#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize, Deserialize)]
pub struct CreateAgentRequest {
    pub name: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub backend: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub cwd: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub model: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub effort: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub voice_id: Option<String>,
    /// The agent that makes this one, by its session name or its id.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub parent: Option<String>,
    /// `agent` or `helper`. A helper is made by another agent to do one piece of work.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub role: Option<String>,
    /// The session name asked for. It is the agent's when no agent has it.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub session: Option<String>,
}
