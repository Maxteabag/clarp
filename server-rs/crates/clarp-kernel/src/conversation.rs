//! What a conversation is made of: the bodies of the log's conversation items.
//!
//! Each of these is stored as JSON under the kind that names it and the key that is its id. A
//! thing that changes, such as a tool call that starts and later ends, is written again under the
//! same key; the newest item is the thing as it is now.

use clarp_ports::log::{Item, NewItem};
use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::journal::kind;
use crate::turn::{Origin, Outcome, Settled};

/// Who wrote a message that came from another agent.
#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize, Deserialize)]
pub struct Sender {
    pub agent_id: String,
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub name: String,
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub session: String,
}

/// A message to the agent.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Message {
    /// For a message from an app: `u-` and the id the app minted, so that the bubble the app
    /// painted and this item are the same thing.
    pub id: String,
    pub text: String,
    pub origin: Origin,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub trace_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub sender: Option<Sender>,
    /// The app that sent it, when it said.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub client: Option<String>,
}

/// Something the agent said: one message of its tool, once that message is whole.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Reply {
    pub id: String,
    pub turn: u64,
    pub text: String,
    /// The trace of the turn that said it: the id the message was sent under.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub trace_id: Option<String>,
    /// Who asked for the turn that said it. Written with the row, so that a page of a
    /// conversation says of every row whose turn it was without looking the turn up.
    #[serde(default, skip_serializing_if = "Origin::is_user")]
    pub origin: Origin,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum ToolStatus {
    Running,
    Ok,
    Error,
}

/// A tool call. One item when it starts and one when it ends, under the same key.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ToolCall {
    pub id: String,
    pub turn: u64,
    pub name: String,
    #[serde(default, skip_serializing_if = "Value::is_null")]
    pub input: Value,
    pub status: ToolStatus,
    /// What the tool gave back, shortened. Absent while it runs.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub output: Option<String>,
    pub started_at_ms: i64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub ended_at_ms: Option<i64>,
    /// Who asked for the turn that made the call.
    #[serde(default, skip_serializing_if = "Origin::is_user")]
    pub origin: Origin,
}

/// Something the Host itself says in a conversation: a turn that failed, a limit that was hit.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Notice {
    pub id: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub turn: Option<u64>,
    /// A stable name for what happened, for a client that wants to act on it.
    pub code: String,
    pub text: String,
}

/// How a turn ended. It is not a row of the conversation: it belongs to the row that closed
/// the turn, which is the last thing the turn put there, or the message itself when the turn
/// put nothing.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TurnEnd {
    pub id: String,
    pub turn: u64,
    /// The id of the row that closed the turn.
    pub closes: String,
    /// `completed`, `failed` or `interrupted`: the three ends a client tells apart.
    pub status: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub started_at_ms: Option<i64>,
    pub ended_at_ms: i64,
    pub tool_count: u32,
    /// The trace the turn's message was sent under.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub trace_id: Option<String>,
    /// The model the tool said it ran on.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub model: Option<String>,
    /// What the tool said the turn took in and gave out, in tokens.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tokens_in: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tokens_out: Option<u64>,
}

impl TurnEnd {
    /// The end of a settled turn, for the row with the id `closes`. A turn that was taken back
    /// before it started has no end to tell.
    pub fn of(settled: &Settled, closes: &str) -> Option<Self> {
        let status = match &settled.outcome {
            Outcome::Completed => "completed",
            Outcome::Failed { .. } => "failed",
            Outcome::Stopped { .. } | Outcome::Interrupted { .. } => "interrupted",
            Outcome::Withdrawn { .. } => return None,
        };
        Some(Self {
            id: format!("t-{}", settled.id.0),
            turn: settled.id.0,
            closes: closes.to_string(),
            status: status.to_string(),
            started_at_ms: settled.started_at_ms,
            ended_at_ms: settled.ended_at_ms,
            tool_count: settled.tool_count,
            trace_id: None,
            model: None,
            tokens_in: None,
            tokens_out: None,
        })
    }
}

/// A row of the conversation an agent had on the Host it came from, exactly as that Host served
/// it to the apps. It is kept as it was: what the apps showed before the move is what they show
/// after it.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Imported {
    pub id: String,
    /// The row as the other Host's `GET /log` gave it.
    pub row: Value,
}

/// Any one thing a conversation holds.
#[derive(Debug, Clone, PartialEq)]
pub enum Entry {
    Message(Message),
    Reply(Reply),
    Tool(ToolCall),
    Notice(Notice),
    Imported(Imported),
    TurnEnd(TurnEnd),
}

impl Entry {
    pub fn kind(&self) -> u16 {
        match self {
            Self::Message(_) => kind::MESSAGE,
            Self::Reply(_) => kind::REPLY,
            Self::Tool(_) => kind::TOOL,
            Self::Notice(_) => kind::NOTICE,
            Self::Imported(_) => kind::IMPORTED,
            Self::TurnEnd(_) => kind::TURN_END,
        }
    }

    /// The id of the thing: what a newer version of it is found by.
    pub fn id(&self) -> &str {
        match self {
            Self::Message(message) => &message.id,
            Self::Reply(reply) => &reply.id,
            Self::Tool(tool) => &tool.id,
            Self::Notice(notice) => &notice.id,
            Self::Imported(imported) => &imported.id,
            Self::TurnEnd(end) => &end.id,
        }
    }

    /// Who asked for the turn this thing belongs to, by the contract's word for it. What
    /// the Host says itself is nobody's turn, and is shown like what a person began.
    pub fn origin_name(&self) -> &str {
        match self {
            Self::Message(message) => message.origin.name(),
            Self::Reply(reply) => reply.origin.name(),
            Self::Tool(call) => call.origin.name(),
            Self::Imported(imported) => imported.row["origin"].as_str().unwrap_or("user"),
            Self::Notice(_) | Self::TurnEnd(_) => "user",
        }
    }

    pub fn turn(&self) -> Option<u64> {
        match self {
            Self::Message(_) => None,
            Self::Reply(reply) => Some(reply.turn),
            Self::Tool(tool) => Some(tool.turn),
            Self::Notice(notice) => notice.turn,
            Self::Imported(_) => None,
            Self::TurnEnd(end) => Some(end.turn),
        }
    }

    /// The item that stores this entry.
    pub fn to_item(&self, ts_ms: i64) -> Result<NewItem, serde_json::Error> {
        let body = match self {
            Self::Message(message) => serde_json::to_vec(message),
            Self::Reply(reply) => serde_json::to_vec(reply),
            Self::Tool(tool) => serde_json::to_vec(tool),
            Self::Notice(notice) => serde_json::to_vec(notice),
            Self::Imported(imported) => serde_json::to_vec(imported),
            Self::TurnEnd(end) => serde_json::to_vec(end),
        }?;
        let mut item = NewItem::new(self.kind(), ts_ms, body).keyed(self.id());
        item.turn = self.turn();
        Ok(item)
    }

    /// Reads an entry back. An item of a kind that is not part of a conversation is `None`.
    pub fn from_item(item: &Item) -> Result<Option<Self>, serde_json::Error> {
        Ok(Some(match item.kind {
            kind::MESSAGE => Self::Message(serde_json::from_slice(&item.body)?),
            kind::REPLY => Self::Reply(serde_json::from_slice(&item.body)?),
            kind::TOOL => Self::Tool(serde_json::from_slice(&item.body)?),
            kind::NOTICE => Self::Notice(serde_json::from_slice(&item.body)?),
            kind::IMPORTED => Self::Imported(serde_json::from_slice(&item.body)?),
            kind::TURN_END => Self::TurnEnd(serde_json::from_slice(&item.body)?),
            _ => return Ok(None),
        }))
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    #[test]
    fn every_entry_comes_back_from_its_item_under_its_own_id() {
        let entries = [
            Entry::Message(Message {
                id: "u-1".to_string(),
                text: "hello".to_string(),
                origin: Origin::User,
                trace_id: None,
                sender: None,
                client: Some("desktop".to_string()),
            }),
            Entry::Reply(Reply {
                origin: Origin::Trigger {
                    name: "automation".to_string(),
                },
                id: "r-msg_1".to_string(),
                turn: 3,
                text: "hi".to_string(),
                trace_id: None,
            }),
            Entry::Tool(ToolCall {
                origin: Origin::User,
                id: "toolu_1".to_string(),
                turn: 3,
                name: "Bash".to_string(),
                input: serde_json::json!({"command": "ls"}),
                status: ToolStatus::Ok,
                output: Some("a\nb".to_string()),
                started_at_ms: 10,
                ended_at_ms: Some(20),
            }),
            Entry::Notice(Notice {
                id: "n-3-limit".to_string(),
                turn: Some(3),
                code: "usage-limit".to_string(),
                text: "The usage limit was reached.".to_string(),
            }),
            Entry::Imported(Imported {
                id: "live-abc".to_string(),
                row: serde_json::json!({"id": "live-abc", "role": "assistant", "text": "from before"}),
            }),
            Entry::TurnEnd(TurnEnd {
                id: "t-3".to_string(),
                turn: 3,
                closes: "r-msg_1".to_string(),
                status: "completed".to_string(),
                started_at_ms: Some(5),
                ended_at_ms: 30,
                tool_count: 1,
                trace_id: None,
                model: None,
                tokens_in: None,
                tokens_out: None,
            }),
        ];
        for (seq, entry) in entries.iter().enumerate() {
            let new = entry.to_item(99).unwrap();
            assert_eq!(new.key.as_deref(), Some(entry.id()));
            assert_eq!(new.turn, entry.turn());
            let stored = Item {
                seq: seq as u64 + 1,
                turn: new.turn,
                kind: new.kind,
                ts_ms: new.ts_ms,
                key: new.key,
                body: new.body,
            };
            assert!(kind::CONVERSATION.has(stored.kind));
            assert_eq!(Entry::from_item(&stored).unwrap().as_ref(), Some(entry));
        }
        let event = Item {
            seq: 9,
            turn: None,
            kind: kind::AGENT_EVENT,
            ts_ms: 1,
            key: None,
            body: b"{}".to_vec(),
        };
        assert_eq!(
            Entry::from_item(&event).unwrap(),
            None,
            "an event is not part of the conversation"
        );
    }
}
