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

/// The mark the id of a message carries in a conversation, which the ids of what an agent
/// says never begin with.
pub const MARK: &str = "u-";

/// A message to the agent.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct Message {
    /// For a message from an app: the id the app minted, with the mark before it where the
    /// app's id comes without ([`Message::filed`]), so that the bubble the app painted and
    /// this item are the same thing.
    pub id: String,
    pub text: String,
    /// Primary selected when a managed Janitor request was admitted.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub managed_model: Option<String>,
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
    /// The header of the call the agent is spoken to in, as it stood when the message came:
    /// told to the agent's tool with this message, and no part of what the user said.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub call: Option<String>,
    /// The id its app minted came with the mark already, as the phone's ids do: the app's
    /// id is `id` whole. Otherwise the app's id is `id` without the mark.
    #[serde(default, skip_serializing_if = "is_false")]
    pub marked: bool,
    /// No app gave this message its id: the Host sent it by itself (a heartbeat, a prompt at
    /// a set time, the task of a fork). It is filed and waits under the id the Host gave it,
    /// and an app is told of it under none ([`Message::told_id`]).
    #[serde(default, skip_serializing_if = "is_false")]
    pub unminted: bool,
    /// When it was sent, for a message that had to wait for its turn: its row is written when
    /// that turn starts and stands there, and this keeps the moment it was sent ([`SENT_AT`]).
    /// A message whose turn started at once has none: its row's time is when it was sent.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub sent_at_ms: Option<i64>,
}

/// The key the log keeps a waiting message's send time under in its row ([`Message::sent_at_ms`]).
/// It is the log's, permanent once written, and no client reads it by this name.
pub const SENT_AT: &str = "sent_at_ms";

impl Message {
    /// Where a message is filed for the id its app gave it, and whether that id came with
    /// the mark: an id that has the mark is filed as it is, any other with the mark before
    /// it. So the Python Host files a message (`message_id_for_client`), and so an app
    /// finds the bubble it painted, whichever of the two its ids are.
    pub fn filed(client_id: &str) -> (String, bool) {
        if client_id.starts_with(MARK) {
            (client_id.to_string(), true)
        } else {
            (format!("{MARK}{client_id}"), false)
        }
    }

    /// The id this message was sent under: the one its app gave it, or the one the Host
    /// gave what it sent by itself. What waits in the queue is named by it.
    pub fn client_id(&self) -> &str {
        if self.marked {
            &self.id
        } else {
            self.id.strip_prefix(MARK).unwrap_or(&self.id)
        }
    }

    /// The id an app is told this message by (`client_msg_id` of an event): the one its app
    /// gave it, and none where no app did. An app looks under that id for the bubble it
    /// painted, and nobody painted what the Host sent by itself: so the Python Host says it.
    pub fn told_id(&self) -> &str {
        if self.unminted { "" } else { self.client_id() }
    }
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
    /// The cell a conversation shows of the call, where its tool's own reader shows what an
    /// agent did as cells and not as cards (Codex): made when the call was last seen, from
    /// all of it, of which the Host keeps only a part.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub cell: Option<Value>,
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

/// What an agent left for its user from one of its turns (`POST /agent-notes`): always shown,
/// whatever turn wrote it, also one that answers another agent. Written once under its id
/// (`note-<agent>-<note id>`), never rewritten.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Note {
    pub id: String,
    pub turn: u64,
    pub text: String,
    /// The trace of the turn that wrote it.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub trace_id: Option<String>,
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
/// What a tool said its whole conversation had taken in and given out by the end of a turn,
/// with the conversation it said it of. A tool that continues a conversation and says no
/// more than this (Codex) leaves a turn's own share to be told by the difference from the
/// turn before.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Total {
    pub session: String,
    pub tokens_in: u64,
    pub tokens_out: u64,
}

/// What a tool said its whole conversation had cost by the end of a turn, in millionths of a
/// dollar, with the conversation it said it of. Claude Code tells this with every result and
/// carries it over when a conversation is continued: a turn's own cost is what it grew by.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CostSoFar {
    pub session: String,
    pub micros: u64,
}

/// What a tool said of its account's windows as it ran, in its own words, and when.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct LimitsSaid {
    pub at_ms: i64,
    pub said: Value,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TurnEnd {
    pub id: String,
    pub turn: u64,
    /// The id of the row that closed the turn.
    pub closes: String,
    /// How the turn ended, in the client's words.
    pub status: String,
    /// The taking-over turn's public id, when the turn ended without an answer.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub replaced_by: Option<String>,
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
    /// How many tokens the tool's conversation held when the turn's last answer was made,
    /// where the tool said so: how full it is. Not what the turn took in, which counts
    /// every step of the turn anew.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub context_tokens: Option<u64>,
    /// What the turn cost, in millionths of a dollar, where that is known: what the tool's
    /// conversation cost more at the turn's end than before it.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub cost_micros: Option<u64>,
    /// What the tool said its whole conversation had cost at the turn's end, where it said so.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub cost_so_far: Option<CostSoFar>,
    /// The running total of the tool's conversation at the turn's end, where the tool told
    /// that and not the turn's own share.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub total: Option<Total>,
    /// What the tool said of its account's windows during the turn, where it said so: the
    /// newest it said.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub limits: Option<LimitsSaid>,
    /// How many notes the turn left for its user (`POST /agent-notes`), counted as they were
    /// written, never by reading the turn's rows. Absent: none.
    #[serde(default, skip_serializing_if = "is_zero")]
    pub note_count: u32,
}

#[allow(clippy::trivially_copy_pass_by_ref)]
fn is_zero(count: &u32) -> bool {
    *count == 0
}

impl TurnEnd {
    /// The end of a settled turn, for the row with the id `closes`. A turn that was taken back
    /// before it started has no end to tell.
    pub fn of(settled: &Settled, closes: &str) -> Option<Self> {
        let status = match &settled.outcome {
            Outcome::Completed => "completed",
            Outcome::Failed { .. } => "failed",
            Outcome::Stopped { .. } | Outcome::Interrupted { .. } => "interrupted",
            Outcome::Replaced { .. } => "replaced",
            Outcome::Withdrawn { .. } => return None,
        };
        let replaced_by = match &settled.outcome {
            Outcome::Replaced { by, trace } => {
                Some(trace.clone().unwrap_or_else(|| by.0.to_string()))
            }
            _ => None,
        };
        Some(Self {
            id: format!("t-{}", settled.id.0),
            turn: settled.id.0,
            closes: closes.to_string(),
            status: status.to_string(),
            replaced_by,
            started_at_ms: settled.started_at_ms,
            ended_at_ms: settled.ended_at_ms,
            tool_count: settled.tool_count,
            trace_id: None,
            model: None,
            tokens_in: None,
            tokens_out: None,
            context_tokens: None,
            cost_micros: None,
            cost_so_far: None,
            total: None,
            limits: None,
            note_count: 0,
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
    Note(Note),
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
            Self::Note(_) => kind::AGENT_NOTE,
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
            Self::Message(_)
            | Self::Reply(_)
            | Self::Notice(_)
            | Self::Imported(_)
            | Self::Note(_) => None,
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
            Self::Note(note) => &note.id,
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
            Self::Note(_) => crate::coordination::NOTE_ORIGIN,
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
            Self::Note(note) => Some(note.turn),
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
            Self::Note(note) => serde_json::to_vec(note),
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
            kind::AGENT_NOTE => Self::Note(serde_json::from_slice(&item.body)?),
            _ => return Ok(None),
        }))
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    /// The log keeps a waiting message's send time under its own key, and a message whose turn
    /// started at once is written without one, as before issue 491.
    #[test]
    fn a_message_keeps_its_send_time_under_the_logs_key_only_when_it_waited() {
        let at_once = serde_json::to_value(Message::default()).unwrap();
        assert!(at_once.get(SENT_AT).is_none(), "{at_once}");
        let waited = Message {
            sent_at_ms: Some(1_700),
            ..Message::default()
        };
        let row = serde_json::to_value(&waited).unwrap();
        assert_eq!(row[SENT_AT], 1_700, "{row}");
        assert_eq!(
            SENT_AT, "sent_at_ms",
            "the log's key is permanent once written"
        );
        let read: Message = serde_json::from_value(row).unwrap();
        assert_eq!(read, waited);
    }

    #[test]
    fn a_message_is_filed_under_its_apps_id_and_gives_that_id_back_as_it_was() {
        // An app whose ids come without the mark (the desk), and one whose ids have it
        // (the phone): each finds its message under the id the Python Host files it under,
        // and is told of it by the id it minted.
        for (minted, filed, marked) in [
            ("m1", "u-m1", false),
            (
                "u-6485F414-2894-4ABE-955D-55F4F289747C",
                "u-6485F414-2894-4ABE-955D-55F4F289747C",
                true,
            ),
            ("u-", "u-", true),
            ("U-1", "u-U-1", false),
            ("", "u-", false),
        ] {
            let (id, came_marked) = Message::filed(minted);
            assert_eq!((id.as_str(), came_marked), (filed, marked), "{minted:?}");
            let message = Message {
                id,
                marked: came_marked,
                ..Message::default()
            };
            assert_eq!(message.client_id(), minted);
            // Whether its id came with the mark is kept with the message, and only then.
            let kept = serde_json::to_value(&message).unwrap();
            assert_eq!(kept.get("marked").is_some(), marked, "{kept}");
            let read: Message = serde_json::from_value(kept).unwrap();
            assert_eq!(read, message);
        }
        // A message the Host itself wrote, and every message written before this was kept:
        // the id without the mark.
        let written = serde_json::to_value(Message {
            id: "u-decision-1".to_string(),
            ..Message::default()
        })
        .unwrap();
        assert!(written.get("marked").is_none(), "{written}");
        let old: Message = serde_json::from_value(written).unwrap();
        assert_eq!((old.marked, old.client_id()), (false, "decision-1"));
        // What the Host sent by itself waits under its id, and an app is told none.
        let own = Message {
            unminted: true,
            ..old.clone()
        };
        assert_eq!((own.client_id(), own.told_id()), ("decision-1", ""));
        assert_eq!(old.told_id(), "decision-1");
        let written = serde_json::to_value(&own).unwrap();
        assert_eq!(written["unminted"], true);
        assert!(
            serde_json::to_value(&old)
                .unwrap()
                .get("unminted")
                .is_none()
        );
    }

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
                replaced_by: None,
                started_at_ms: Some(5),
                ended_at_ms: 30,
                tool_count: 1,
                trace_id: None,
                model: None,
                tokens_in: None,
                tokens_out: None,
                context_tokens: None,
                cost_micros: None,
                cost_so_far: None,
                total: None,
                limits: None,
                note_count: 0,
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
