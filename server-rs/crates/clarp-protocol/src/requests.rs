//! What an app says to the Host with the core commands (`docs/protocol.md`, sections 4, 5, 7, 8).

use serde::{Deserialize, Serialize};
use serde_json::Value;

/// `POST /send`: a message for an agent.
#[derive(Debug, Clone, PartialEq, Default, Serialize, Deserialize)]
pub struct SendRequest {
    /// The agent it is for. Left out: the Host's default.
    #[serde(default)]
    pub session: String,
    #[serde(default)]
    pub text: String,
    /// The idempotency key. The message is filed as `u-` and this id; a repeated request with the
    /// same id is the same message.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub client_msg_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub trace_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub synthesize_audio: Option<bool>,
    /// Dictated hands-free: the Host then looks whom it is for, unless `force_session` says
    /// it is for the agent it names.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub hands_free: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub force_session: Option<bool>,
    /// The app could not tell whom it is for and asks the Host's router of spoken messages.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub orchestrator_fallback: Option<bool>,
    /// The sessions whose agents said something aloud that the user has not heard: a list of
    /// names. An agent among them is told so with this message.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub unheard_audio_sessions: Option<Value>,
    /// The call this message is a part of: it goes to the agent of the call that can take
    /// speech, whoever it names.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub call_id: Option<String>,
    /// The phone's greeting, tied to one listen-first ring and its epoch.
    #[serde(
        default,
        deserialize_with = "present_value",
        skip_serializing_if = "Option::is_none"
    )]
    pub call_opening: Option<Value>,
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

/// `POST /stop`: the agent, and who stops it and why (contract 61), each as it came: an actor or
/// a reason may be of any kind, and is read as the Python Host reads it.
#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize, Deserialize)]
pub struct StopRequest {
    pub session: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub actor: Option<Value>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub reason: Option<Value>,
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
    /// How the agent speaks: the clause its tool is told with its name. Without one it
    /// speaks as the persona of its name does.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub personality: Option<String>,
    /// The symbol it is shown with instead of a picture. Without one it has its persona's.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub avatar_symbol: Option<String>,
    /// A picture of its own, in base64. Without one it has its persona's own, when that
    /// persona has one.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub avatar_base64: Option<String>,
}

fn present_value<'de, D: serde::Deserializer<'de>>(decoder: D) -> Result<Option<Value>, D::Error> {
    Value::deserialize(decoder).map(Some)
}
