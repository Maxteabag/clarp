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
    /// True where the message came from an agent bound to a model provider of its own and
    /// went to an agent that may not read it: it was given that a message came, not its words.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub words_withheld: Option<bool>,
    /// What the Host did with a send of the owner's that named `while_busy` and came while a
    /// turn ran (feature `send_while_busy`): `steer`, `after_turn` or `take_over`.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub while_busy: Option<String>,
    /// Where that is not what the send asked for: what it asked for, what was done, why, and
    /// the short line the app shows.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub fallback: Option<WhileBusyFallback>,
}

/// A send of the owner's that could not do what its `while_busy` asked for.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct WhileBusyFallback {
    pub from: String,
    pub to: String,
    /// `not_steerable`, `not_words` or `not_cuttable`.
    pub reason: String,
    pub text: String,
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
    /// The Stop in one sentence for people, built from what it left (feature
    /// `stop_receipt_text`, Rust Host contract 184): "Stopped.", whatever waits; the counts are
    /// in the fields beside it.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub text: Option<String>,
}

/// The answer to `POST /select`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SelectResponse {
    pub ok: bool,
    pub session: String,
}
