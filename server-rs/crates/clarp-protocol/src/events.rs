//! The live events of `GET /events` (`contract/schemas/sse.json`).

use serde::{Deserialize, Serialize};
use serde_json::Value;

/// One event as it travels: the event, and its durable id when the Host recorded it.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Envelope {
    /// The SSE `id:` of a durable event. A client sends the last one it saw to catch up.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub event_id: Option<u64>,
    #[serde(flatten)]
    pub event: Event,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "kebab-case")]
pub enum Event {
    TranscriptUpdated(TranscriptUpdated),
    AgentState(AgentState),
    AgentActivity(AgentActivity),
    AgentRoster(AgentRoster),
    AgentFocus(AgentFocus),
    QueueUpdated(QueueUpdated),
    UserNotification(UserNotification),
    Audio(Audio),
    TtsError(TtsError),
    ServerVersion(ServerVersion),
    RemoteAction(RemoteAction),
    /// An event type from a newer Host. A client ignores it.
    #[serde(other)]
    Unknown,
}

/// The conversation changed. A wake-up, not the data: the client fetches a delta.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct TranscriptUpdated {
    pub session: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub agent_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub backend_session_id: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum AgentStateKind {
    Thinking,
    Tool,
    Idle,
    Spawned,
    Stopped,
    Done,
    Compacting,
    Waiting,
    Interrupted,
    Background,
    /// A kind from a newer Host, kept as it was sent.
    #[serde(untagged)]
    Other(String),
}

impl AgentStateKind {
    /// Whether an agent in this state is working on a turn. `thinking` starts one; `done`,
    /// `idle`, `stopped` and `interrupted` end one (docs/protocol.md, section 3).
    pub fn is_busy(&self) -> bool {
        matches!(
            self,
            Self::Thinking
                | Self::Tool
                | Self::Spawned
                | Self::Compacting
                | Self::Waiting
                | Self::Background
        )
    }
}

/// An agent's state changed. The client patches the agent's `latest_state`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct AgentState {
    pub session: String,
    pub agent_id: String,
    pub kind: AgentStateKind,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub persona: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub ts: Option<i64>,
    /// An object, a string or nothing, depending on the kind.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub detail: Option<Value>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub status_text: Option<String>,
    // A dispatch adds these.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub trace_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub client_msg_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub queue_started: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub queue_remaining: Option<u64>,
    // A janitor's cancel adds this.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub origin: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum ActivityStatus {
    Running,
    Ok,
    Error,
    Recorded,
    #[serde(untagged)]
    Other(String),
}

/// What an agent is doing inside a turn: a tool starting or ending, a stop, a repair.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct AgentActivity {
    pub session: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub agent_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub persona: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub kind: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub phase: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub status: Option<ActivityStatus>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tool: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub action: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub summary: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub file_path: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub ts: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub stop_actor: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub stop_actor_verified: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub stop_reason: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub repair_actor: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub repair_actor_verified: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub repair_reason: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub released_trace: Option<String>,
}

/// The set of agents changed. With no fields it means "fetch the snapshot now".
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct AgentRoster {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub kind: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub session: Option<String>,
}

/// Which agent is open.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct AgentFocus {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub session: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub agent_id: Option<String>,
}

/// How the message came to be in the queue.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum QueueKind {
    Waiting,
    Later,
}

/// An agent's queue of waiting messages changed.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct QueueUpdated {
    pub session: String,
    pub agent_id: String,
    pub queue_depth: u64,
    pub queue_paused: bool,
    pub queue_revision: u64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub queue_started: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub client_msg_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub text: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub sent_at: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub kind: Option<QueueKind>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub queue_change: Option<String>,
}

/// A finished turn the user may want to know about, with what the Host decided to do about it.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct UserNotification {
    pub session: String,
    pub notification_id: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub agent_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub persona: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub done_ts: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source_message_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub cause_message_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub origin: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub push: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub badge: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub unread: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub muted: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub preview: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub reason: Option<String>,
}

/// A spoken clip is ready, or has started to be.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Audio {
    pub url: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub clip_id: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub name: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub session: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub agent_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub persona: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub trace_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub streamable: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub delivery: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub stream_url: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub playlist_url: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub complete_url: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub audio_format: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub preview: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct TtsError {
    pub session: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub agent_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub persona: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub message: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

/// The Host's version, sent when a stream opens: a client that sees a new one reloads.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ServerVersion {
    pub version: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum ClientAction {
    Record,
    RecordToggle,
    StopAgent,
    ControllerEvent,
    #[serde(untagged)]
    Other(String),
}

#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum ControllerButton {
    Primary,
    Secondary,
    #[serde(untagged)]
    Other(String),
}

#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum ControllerGesture {
    Down,
    Up,
    SingleClick,
    DoubleClick,
    Hold,
    SwipeLeft,
    SwipeRight,
    SwipeUp,
    SwipeDown,
    RotateClockwise,
    RotateCounterclockwise,
    #[serde(untagged)]
    Other(String),
}

/// Something a hardware controller asked the open client to do.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RemoteAction {
    pub action: ClientAction,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub controller_event_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub controller_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub button: Option<ControllerButton>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub controller_event: Option<ControllerGesture>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub duration_ms: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub age_ms: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub queued: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub ts: Option<i64>,
}
