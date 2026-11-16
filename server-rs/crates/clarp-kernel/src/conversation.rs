//! What a conversation is made of: the bodies of the log's conversation items.
//!
//! Each of these is stored as JSON under the kind that names it and the key that is its id. A
//! thing that changes, such as a tool call that starts and later ends, is written again under the
//! same key; the newest item is the thing as it is now.
//!
//! A conversation is shown in rows: a message to the agent, a message of the agent, a word of
//! the Host. A row's item is written once, and stands where the row stands. What happens to a
//! row afterwards is an item of its own that names the row ([`Entry::part_of`]): the uses of
//! the tools its message calls, that tool work came after it, how its turn ended.

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
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
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
    /// What the agent answers is said aloud: the parts of its replies it marks for the ear.
    /// So it is for what a person sends, unless the app says otherwise; for anything else
    /// only when it is asked for.
    #[serde(default, skip_serializing_if = "is_false")]
    pub speak: bool,
    /// The app said its user began to speak before hearing what this agent last said
    /// aloud: the agent's tool is told so with this message.
    #[serde(default, skip_serializing_if = "is_false")]
    pub unheard: bool,
}

#[allow(clippy::trivially_copy_pass_by_ref)]
fn is_false(truth: &bool) -> bool {
    !*truth
}

/// One message of the agent's tool, once that message is whole: what the agent said in it,
/// and the tools it calls in it. A conversation shows it as one row: the text, and a card
/// for each tool. A message that only calls tools says nothing and is a row all the same.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
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
    /// The tools the message calls, by the ids of their uses, in the order it calls them.
    /// Each use is an item of its own ([`ToolCall`]), written when it starts and when it ends.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub tools: Vec<String>,
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum ToolStatus {
    #[default]
    Running,
    Ok,
    Error,
    /// Its turn was interrupted before it returned, and it never will.
    Interrupted,
}

/// A tool call. One item when it starts and one when it ends, under the same key. It is a part
/// of the reply its message is, and is shown on that row as a card.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
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
    /// The reply its message is: the row of the conversation it is shown in. A call that
    /// names none is a row by itself, as every call was before messages were rows.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub reply: Option<String>,
    /// The trace of the turn that made the call: what its row is filed under when the
    /// call is a row by itself.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub trace_id: Option<String>,
}

/// Tool work came after a reply in the same turn: what the reply said was said along the way,
/// not at the end. It is not a row: it belongs to the reply it names, which changed with it.
/// (The tools a reply's own message calls do not make it this: only work that comes later.)
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Commentary {
    pub id: String,
    pub turn: u64,
    /// The id of the reply that was said along the way.
    pub reply: String,
}

impl Commentary {
    /// What says of the reply with this id that tool work followed it.
    pub fn of(reply: &str, turn: u64) -> Self {
        Self {
            id: Self::id_of(reply),
            turn,
            reply: reply.to_string(),
        }
    }

    /// The id under which it is said of this reply, if it was.
    pub fn id_of(reply: &str) -> String {
        format!("c-{reply}")
    }
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
    /// The trace of the turn it is about, when it is about one.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub trace_id: Option<String>,
}

impl Notice {
    /// The code of the row that says why a turn failed, under the message that caused it.
    pub const TURN_FAILED: &'static str = "turn-failed";
    /// The code of a quiet word of the Host's in a conversation that belongs to no turn:
    /// that the agent was asked into a call, put on hold, that the call ended. Shown in
    /// the chat, never spoken, never a reason to page anybody.
    pub const SYSTEM: &'static str = "system";
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
    Commentary(Commentary),
}

impl Entry {
    pub fn kind(&self) -> u16 {
        match self {
            Self::Message(_) => kind::MESSAGE,
            Self::Reply(_) => kind::REPLY,
            Self::Tool(call) if call.reply.is_some() => kind::TOOL_USE,
            Self::Tool(_) => kind::TOOL,
            Self::Notice(_) => kind::NOTICE,
            Self::Imported(_) => kind::IMPORTED,
            Self::TurnEnd(_) => kind::TURN_END,
            Self::Commentary(_) => kind::COMMENTARY,
        }
    }

    /// The id of the row this thing is a part of, when it is no row itself: the reply a
    /// tool's use is shown on, the reply that tool work followed, the row that closed a turn.
    /// A row changes when a part of it is written.
    pub fn part_of(&self) -> Option<&str> {
        match self {
            Self::Tool(call) => call.reply.as_deref(),
            Self::Commentary(commentary) => Some(&commentary.reply),
            Self::TurnEnd(end) => Some(&end.closes),
            Self::Message(_) | Self::Reply(_) | Self::Notice(_) | Self::Imported(_) => None,
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
            Self::Commentary(commentary) => &commentary.id,
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
            Self::Notice(_) | Self::TurnEnd(_) | Self::Commentary(_) => "user",
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
            Self::Commentary(commentary) => Some(commentary.turn),
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
            Self::Commentary(commentary) => serde_json::to_vec(commentary),
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
            kind::TOOL | kind::TOOL_USE => Self::Tool(serde_json::from_slice(&item.body)?),
            kind::NOTICE => Self::Notice(serde_json::from_slice(&item.body)?),
            kind::IMPORTED => Self::Imported(serde_json::from_slice(&item.body)?),
            kind::TURN_END => Self::TurnEnd(serde_json::from_slice(&item.body)?),
            kind::COMMENTARY => Self::Commentary(serde_json::from_slice(&item.body)?),
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
                ..Message::default()
            }),
            Entry::Reply(Reply {
                origin: Origin::Trigger {
                    name: "automation".to_string(),
                },
                id: "r-msg_1".to_string(),
                turn: 3,
                text: "hi".to_string(),
                trace_id: None,
                tools: vec!["toolu_2".to_string()],
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
                ..ToolCall::default()
            }),
            Entry::Tool(ToolCall {
                id: "toolu_2".to_string(),
                turn: 3,
                name: "Read".to_string(),
                started_at_ms: 12,
                reply: Some("r-msg_1".to_string()),
                ..ToolCall::default()
            }),
            Entry::Commentary(Commentary::of("r-msg_0", 3)),
            Entry::Notice(Notice {
                id: "n-3-limit".to_string(),
                turn: Some(3),
                code: "usage-limit".to_string(),
                text: "The usage limit was reached.".to_string(),
                trace_id: None,
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
            // A row's item is of a kind a page is counted in; a part's is not, and names its row.
            assert_eq!(
                kind::ROWS.has(stored.kind),
                entry.part_of().is_none(),
                "{}",
                entry.id()
            );
        }
        let parts: Vec<(&str, Option<&str>)> = entries
            .iter()
            .map(|entry| (entry.id(), entry.part_of()))
            .filter(|(_, row)| row.is_some())
            .collect();
        assert_eq!(
            parts,
            [
                ("toolu_2", Some("r-msg_1")),
                ("c-r-msg_0", Some("r-msg_0")),
                ("t-3", Some("r-msg_1")),
            ]
        );
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
