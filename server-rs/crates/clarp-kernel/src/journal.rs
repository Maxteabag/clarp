//! The agent and its log together: write first, then change.
//!
//! [`Recorder`] is the agent's state with the log it is rebuilt from. Whatever happens is first
//! appended to the log in one transaction, and only when that append has succeeded is the state
//! changed and the effects handed out. If the append fails, nothing happened: the state is as it
//! was and the caller is told. Opening a recorder replays the log, so a process that died at any
//! point comes back in the state of its last successful append.
//!
//! Things that happen together are written together, as a [`Batch`]: a message and the turn it
//! becomes; what a stretch of a tool's output means, with the position in that output the log
//! has now reached. There is never one without the other.

use clarp_ports::log::{AgentLog, Kinds, LogError, NewItem, Seq};
use serde::{Deserialize, Serialize};

use crate::agent::{Agent, AgentEvent, Command, Effect, Refusal};
use crate::turn::{Admission, IllegalTransition, Origin, TurnId};

/// What an item in an agent's log is. The numbers are permanent once a log holds them.
pub mod kind {
    use clarp_ports::log::Kinds;

    /// An [`AgentEvent`](crate::agent::AgentEvent) as JSON.
    pub const AGENT_EVENT: u16 = 1;
    /// A [`SpoolCursor`](super::SpoolCursor) as JSON: how far into a turn's spool the log has got.
    pub const SPOOL_CURSOR: u16 = 2;
    /// A [`Message`](crate::conversation::Message) that waits its turn outside the conversation.
    /// When its turn starts, the same message is written again as a [`MESSAGE`]: that is the
    /// moment it enters the conversation, and where it stands in it.
    pub const WAITING: u16 = 3;
    /// A [`Message`](crate::conversation::Message) to the agent.
    pub const MESSAGE: u16 = 100;
    /// A [`Reply`](crate::conversation::Reply) of the agent.
    pub const REPLY: u16 = 101;
    /// A [`ToolCall`](crate::conversation::ToolCall) that is a row by itself: a call no
    /// message of the agent carries, and every call written before messages were rows.
    pub const TOOL: u16 = 102;
    /// A [`Notice`](crate::conversation::Notice) from the Host.
    pub const NOTICE: u16 = 103;
    /// An [`Imported`](crate::conversation::Imported) row: one this agent's conversation had on
    /// the Host it came from, kept as that Host served it.
    pub const IMPORTED: u16 = 104;
    /// A [`TurnEnd`](crate::conversation::TurnEnd): how a turn ended, for the row that closed it.
    pub const TURN_END: u16 = 110;
    /// A [`ToolCall`](crate::conversation::ToolCall) that names the reply its message is: a
    /// part of that row, written when the call starts and again when it ends.
    pub const TOOL_USE: u16 = 111;
    /// A [`Commentary`](crate::conversation::Commentary): tool work came after a reply in the
    /// same turn, for that reply's row.
    pub const COMMENTARY: u16 = 112;

    /// A [`JobRecord`](crate::jobs::JobRecord): a background job as a change left it.
    pub const JOB: u16 = 300;
    /// A [`GoalRecord`](crate::goals::GoalRecord) as JSON: what one change set in one of the
    /// agent's plans.
    pub const GOAL: u16 = 400;
    /// An [`ArtifactRecord`](crate::artifacts::ArtifactRecord) as JSON: an artifact of the
    /// agent's as a change left it.
    pub const ARTIFACT: u16 = 500;
    /// An [`Asset`](crate::media::Asset) as JSON: a file the agent published.
    pub const MEDIA: u16 = 600;
    /// A [`Line`](crate::spoken::Line) as JSON: a line the agent says aloud, as a change
    /// left it.
    pub const LINE: u16 = 700;

    /// What an agent is rebuilt from.
    pub const MACHINERY: Kinds = Kinds::range(1, 99);
    /// The agent's background jobs.
    pub const JOBS: Kinds = Kinds::range(300, 399);
    /// The agent's goals and plans.
    pub const GOALS: Kinds = Kinds::range(400, 499);
    /// What the agent made for its user and what it asks its user.
    pub const ARTIFACTS: Kinds = Kinds::range(500, 599);
    /// The files the agent published for its user to see.
    pub const PUBLISHED: Kinds = Kinds::range(600, 699);
    /// What the agent says aloud.
    pub const SPEECH: Kinds = Kinds::range(700, 799);
    /// What a conversation is read from.
    pub const CONVERSATION: Kinds = Kinds::range(100, 199);
    /// The rows of a conversation: what a page of a chat is counted in. An item of one of
    /// these kinds is where its row stands. What changes a row afterwards is an item of
    /// another kind that names the row: its tools' uses, what followed it, how its turn ended.
    pub const ROWS: Kinds = Kinds::range(MESSAGE, IMPORTED);
}

const PAGE: usize = 1_000;

/// How far into a turn's spool the log has got: everything before `offset` is in the log.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct SpoolCursor {
    pub turn: TurnId,
    pub offset: u64,
}

#[derive(Debug)]
pub enum JournalError {
    Log(LogError),
    /// An item that should hold an event does not.
    Corrupt {
        seq: Seq,
        why: String,
    },
    /// The log holds a history that cannot have happened.
    Illegal {
        seq: Seq,
        transition: IllegalTransition,
    },
    /// Something could not be put into words for the log. Nothing was written.
    Encode(String),
    Refused(Refusal),
}

impl std::fmt::Display for JournalError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Log(error) => write!(f, "{error}"),
            Self::Corrupt { seq, why } => write!(f, "log item {seq} is not an event: {why}"),
            Self::Illegal { seq, transition } => write!(f, "log item {seq}: {transition}"),
            Self::Encode(why) => write!(f, "an item could not be written: {why}"),
            Self::Refused(refusal) => write!(f, "{refusal}"),
        }
    }
}

impl std::error::Error for JournalError {}

impl From<LogError> for JournalError {
    fn from(error: LogError) -> Self {
        Self::Log(error)
    }
}

/// A message on its way to an agent.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Incoming {
    /// The message's id. A second message with the same id is the same message, sent again.
    pub key: Option<String>,
    pub body: Vec<u8>,
    pub origin: Origin,
    pub admission: Admission,
}

/// What a message did: where it is in the log, and what the actor must now do.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Accepted {
    pub message_seq: Seq,
    /// The turn the message became. `None` when this was a message the log already held.
    pub turn: Option<TurnId>,
    /// The log already held this message: nothing was written and nothing is to be done.
    pub duplicate: bool,
    /// The message is in the conversation. One that was left for later and has to wait is not,
    /// until its turn starts.
    pub entered: bool,
    pub effects: Vec<Effect>,
}

/// What a batch did, once it is in the log.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Committed {
    /// The position of the batch's first item. 0 when the batch was empty.
    pub first_seq: Seq,
    /// The position of the batch's last item. 0 when the batch was empty.
    pub last_seq: Seq,
    pub events: Vec<AgentEvent>,
    pub effects: Vec<Effect>,
}

impl Committed {
    /// The position of the item that [`Batch::item`] put at `index`.
    pub fn seq_of(&self, index: usize) -> Seq {
        self.first_seq + index as Seq
    }

    pub fn is_empty(&self) -> bool {
        self.first_seq == 0
    }
}

/// What became of the wish to change the words of a waiting message.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Reworded {
    Done,
    /// It does not wait: it ran, was taken back, or never was.
    NotWaiting,
    /// It waits its turn in the conversation, where it was said: what was said stands.
    InConversation,
}

pub struct Recorder {
    log: Box<dyn AgentLog>,
    agent: Agent,
    cursor: Option<SpoolCursor>,
    /// The position of the newest item. This recorder is the log's one writer, so it knows.
    last_seq: Seq,
    /// The position of the newest item of the conversation: 0 when it has none.
    conversation_seq: Seq,
    /// Where the agent was last started again: its conversation, as it is read, is what
    /// was written after this. 0: it never was.
    floor: Seq,
}

fn event_item(event: &AgentEvent, ts_ms: i64) -> Result<NewItem, JournalError> {
    let body = serde_json::to_vec(event).map_err(|e| JournalError::Encode(e.to_string()))?;
    let mut item = NewItem::new(kind::AGENT_EVENT, ts_ms, body);
    item.turn = match event {
        AgentEvent::Turn { turn, .. }
        | AgentEvent::StopRequested { turn, .. }
        | AgentEvent::SentNow { turn, .. }
        | AgentEvent::Reworded { turn, .. } => Some(turn.0),
        AgentEvent::Paused { .. }
        | AgentEvent::Resumed { .. }
        | AgentEvent::Adopted { .. }
        | AgentEvent::TookUp { .. }
        | AgentEvent::StartedAgain { .. }
        | AgentEvent::Labelled { .. } => None,
    };
    Ok(item)
}

impl Recorder {
    /// Opens an agent from its log: the state is whatever its history says.
    pub fn open(log: Box<dyn AgentLog>) -> Result<Self, JournalError> {
        let mut agent = Agent::new();
        let mut cursor = None;
        let mut after = 0;
        let mut floor = 0;
        loop {
            let page = log.read_after(after, kind::MACHINERY, PAGE)?;
            let Some(last) = page.last() else { break };
            after = last.seq;
            for item in &page {
                let corrupt = |e: serde_json::Error| JournalError::Corrupt {
                    seq: item.seq,
                    why: e.to_string(),
                };
                match item.kind {
                    kind::AGENT_EVENT => {
                        let event: AgentEvent =
                            serde_json::from_slice(&item.body).map_err(corrupt)?;
                        if matches!(event, AgentEvent::StartedAgain { .. }) {
                            floor = item.seq;
                        }
                        agent
                            .apply(&event)
                            .map_err(|transition| JournalError::Illegal {
                                seq: item.seq,
                                transition,
                            })?;
                    }
                    kind::SPOOL_CURSOR => {
                        cursor = Some(serde_json::from_slice(&item.body).map_err(corrupt)?);
                    }
                    // A kind a newer build wrote: not ours to understand, not ours to refuse.
                    _ => {}
                }
            }
        }
        let last_seq = log.last_seq()?;
        let conversation_seq = log
            .read_before(Seq::MAX, kind::CONVERSATION, 1)?
            .last()
            .map_or(0, |item| item.seq);
        Ok(Self {
            log,
            agent,
            cursor,
            last_seq,
            conversation_seq,
            floor,
        })
    }

    /// The position of the newest item in the log.
    pub fn last_seq(&self) -> Seq {
        self.last_seq
    }

    /// The position of the newest item of the conversation: the conversation changed last when
    /// that item was written. 0 when there is none: nothing was ever written, or nothing
    /// since the agent was started again.
    pub fn conversation_seq(&self) -> Seq {
        if self.conversation_seq > self.floor {
            self.conversation_seq
        } else {
            0
        }
    }

    /// Where the agent was last started again: its conversation, as it is read, is what
    /// was written after this position. 0: it never was.
    pub fn floor(&self) -> Seq {
        self.floor
    }

    /// Starts the agent again: its next turn begins a conversation with its tool, and its
    /// conversation, as it is read, begins anew. What waits its turn and was said already
    /// is written anew, after where the old conversation ends: it is part of the new one,
    /// and its turn answers it there. Answers how many such messages there were; `None`,
    /// with nothing changed, while a turn of the agent runs: that turn is stopped first.
    pub fn start_again(&mut self, now_ms: i64) -> Result<Option<usize>, JournalError> {
        if self.agent.live().is_some() {
            return Ok(None);
        }
        let waiting: Vec<(TurnId, Seq)> = self
            .agent
            .waiting()
            .map(|queued| (queued.id, queued.message_seq))
            .collect();
        let mut said = Vec::new();
        for (turn, message_seq) in waiting {
            let from = message_seq.saturating_sub(1);
            let kept = self.log.read_after(from, Kinds::only(kind::MESSAGE), 1)?;
            // What waits outside the conversation enters it when its turn starts.
            if let Some(stored) = kept.into_iter().find(|item| item.seq == message_seq) {
                said.push((turn, stored));
            }
        }
        // This recorder is the log's one writer: the item it writes first is the next one.
        let first = self.last_seq + 1;
        let moved = said.len();
        let mut batch = self.batch();
        batch
            .command(Command::StartAgain, now_ms)
            .map_err(JournalError::Refused)?;
        for (turn, stored) in said {
            let mut item = NewItem::new(kind::MESSAGE, stored.ts_ms, stored.body).in_turn(turn.0);
            item.key = stored.key;
            let message_seq = first + batch.item(item) as Seq;
            let anew = Command::Reword { turn, message_seq };
            batch.command(anew, now_ms).map_err(JournalError::Refused)?;
        }
        batch.commit()?;
        Ok(Some(moved))
    }

    pub fn agent(&self) -> &Agent {
        &self.agent
    }

    /// The log, for reading. Only a [`Batch`] writes to it.
    pub fn log(&self) -> &dyn AgentLog {
        self.log.as_ref()
    }

    /// How far into this turn's spool the log has got: 0 when none of it is in the log yet.
    pub fn spool_offset(&self, turn: TurnId) -> u64 {
        match self.cursor {
            Some(cursor) if cursor.turn == turn => cursor.offset,
            _ => 0,
        }
    }

    /// Starts a batch: things that happened together and are written together.
    pub fn batch(&mut self) -> Batch<'_> {
        Batch {
            work: self.agent.clone(),
            recorder: self,
            items: Vec::new(),
            events: Vec::new(),
            effects: Vec::new(),
            cursor: None,
            failed: None,
            started_again: None,
        }
    }

    /// Writes the words of a waiting message anew. Only a message that waits outside the
    /// conversation has words that can change: one that is in the conversation was said.
    /// `rewrite` is given the message as it is kept, and answers it as it is to be kept. The
    /// message as it was stays in the log where it is, and nothing reads it there any more.
    pub fn reword(
        &mut self,
        turn: TurnId,
        rewrite: impl FnOnce(&[u8]) -> Result<Vec<u8>, JournalError>,
        now_ms: i64,
    ) -> Result<Reworded, JournalError> {
        let waiting = self.agent.waiting().find(|queued| queued.id == turn);
        let Some(message_seq) = waiting.map(|queued| queued.message_seq) else {
            return Ok(Reworded::NotWaiting);
        };
        let kept =
            self.log
                .read_after(message_seq.saturating_sub(1), Kinds::only(kind::WAITING), 1)?;
        let Some(stored) = kept.into_iter().find(|item| item.seq == message_seq) else {
            return Ok(Reworded::InConversation);
        };
        let body = rewrite(&stored.body)?;
        // This recorder is the log's one writer: the item it writes first is the next one.
        let anew = self.last_seq + 1;
        let mut batch = self.batch();
        let mut item = NewItem::new(kind::WAITING, now_ms, body).in_turn(turn.0);
        item.key = stored.key;
        batch.item(item);
        batch
            .command(
                Command::Reword {
                    turn,
                    message_seq: anew,
                },
                now_ms,
            )
            .map_err(JournalError::Refused)?;
        batch.commit()?;
        Ok(Reworded::Done)
    }

    /// Records a command's events, then applies them. Returns the effects to carry out.
    pub fn handle(&mut self, command: Command, now_ms: i64) -> Result<Vec<Effect>, JournalError> {
        let mut batch = self.batch();
        batch
            .command(command, now_ms)
            .map_err(JournalError::Refused)?;
        Ok(batch.commit()?.effects)
    }

    /// Accepts a message that waits its turn and has no id of its own.
    pub fn accept(
        &mut self,
        body: Vec<u8>,
        origin: Origin,
        now_ms: i64,
    ) -> Result<Accepted, JournalError> {
        self.accept_message(
            Incoming {
                key: None,
                body,
                origin,
                admission: Admission::Queue,
            },
            now_ms,
        )
    }

    /// Accepts a message: the message and the turn it becomes are written in one transaction, so
    /// there is never a message without its turn or a turn without its message. A message whose
    /// id the log already holds was accepted before; it is not accepted again.
    pub fn accept_message(
        &mut self,
        message: Incoming,
        now_ms: i64,
    ) -> Result<Accepted, JournalError> {
        self.accept_message_with(message, Vec::new(), now_ms)
    }

    /// Accepts a message, and writes `with` in the same transaction: what is true only if
    /// the message was taken, and without which the message must not have been. For a message
    /// the log already holds, nothing is written.
    pub fn accept_message_with(
        &mut self,
        message: Incoming,
        with: Vec<NewItem>,
        now_ms: i64,
    ) -> Result<Accepted, JournalError> {
        if let Some(key) = &message.key
            && let Some(earlier) = self.log.find(key)?
        {
            return Ok(Accepted {
                message_seq: earlier.seq,
                turn: None,
                duplicate: true,
                entered: false,
                effects: Vec::new(),
            });
        }
        let message_seq = self.last_seq + 1;
        let turn = self.agent.next_turn();
        // A message that starts at once is in the conversation at once, however it was admitted.
        let outside =
            message.admission.waits_outside() && !self.agent.starts_at_once(message.admission);
        let kind = if outside {
            kind::WAITING
        } else {
            kind::MESSAGE
        };
        let mut batch = self.batch();
        let mut item = NewItem::new(kind, now_ms, message.body).in_turn(turn.0);
        item.key = message.key;
        batch.item(item);
        for item in with {
            batch.item(item);
        }
        batch
            .command(
                Command::Accept {
                    message_seq,
                    origin: message.origin,
                    admission: message.admission,
                },
                now_ms,
            )
            .map_err(JournalError::Refused)?;
        let committed = batch.commit()?;
        Ok(Accepted {
            message_seq,
            turn: Some(turn),
            duplicate: false,
            entered: !outside,
            effects: committed.effects,
        })
    }
}

/// Things that happened together, on their way into the log as one append.
///
/// A batch works on a copy of the agent. Nothing it holds is real until [`Batch::commit`] has
/// written it; a batch that is dropped never happened.
pub struct Batch<'a> {
    recorder: &'a mut Recorder,
    work: Agent,
    items: Vec<NewItem>,
    events: Vec<AgentEvent>,
    effects: Vec<Effect>,
    cursor: Option<SpoolCursor>,
    failed: Option<JournalError>,
    /// Where in this batch the agent is started again, when it is.
    started_again: Option<usize>,
}

impl Batch<'_> {
    /// Decides a command against the agent as this batch has moved it, and adds its events.
    /// A command the agent refuses adds nothing: the refusal is returned and the batch stands.
    pub fn command(&mut self, command: Command, now_ms: i64) -> Result<(), Refusal> {
        let decision = self.work.decide(command, now_ms)?;
        for event in decision.events {
            match event_item(&event, now_ms) {
                Ok(item) => {
                    if matches!(event, AgentEvent::StartedAgain { .. }) {
                        self.started_again = Some(self.items.len());
                    }
                    self.items.push(item);
                }
                Err(error) => {
                    self.failed.get_or_insert(error);
                }
            }
            // Decided on this very state a moment ago: if it does not apply, the state machine
            // is wrong, and the batch must not be written.
            if let Err(transition) = self.work.apply(&event) {
                self.failed
                    .get_or_insert(JournalError::Illegal { seq: 0, transition });
            }
            self.events.push(event);
        }
        for effect in &decision.effects {
            if let Effect::StartTurn { turn, message_seq } = effect {
                self.enter(*turn, *message_seq, now_ms);
            }
        }
        self.effects.extend(decision.effects);
        Ok(())
    }

    /// A turn starts: if its message waited outside the conversation, it enters it now, in the
    /// same write that starts the turn.
    fn enter(&mut self, turn: TurnId, message_seq: Seq, now_ms: i64) {
        if message_seq > self.recorder.last_seq {
            return; // accepted in this very batch: it is in the conversation already
        }
        let waiting = self.recorder.log.read_after(
            message_seq.saturating_sub(1),
            Kinds::only(kind::WAITING),
            1,
        );
        match waiting {
            Ok(found) => {
                if let Some(stored) = found.into_iter().find(|item| item.seq == message_seq) {
                    let mut item = NewItem::new(kind::MESSAGE, now_ms, stored.body).in_turn(turn.0);
                    item.key = stored.key;
                    self.items.push(item);
                }
            }
            Err(error) => {
                self.failed.get_or_insert(error.into());
            }
        }
    }

    /// Adds an item that is not an event, such as a piece of the conversation. Returns its place
    /// in the batch, for [`Committed::seq_of`].
    pub fn item(&mut self, item: NewItem) -> usize {
        self.items.push(item);
        self.items.len() - 1
    }

    /// Records that everything before `offset` in the turn's spool is in the log with this batch.
    pub fn cursor(&mut self, turn: TurnId, offset: u64, now_ms: i64) {
        let cursor = SpoolCursor { turn, offset };
        match serde_json::to_vec(&cursor) {
            Ok(body) => {
                self.items
                    .push(NewItem::new(kind::SPOOL_CURSOR, now_ms, body).in_turn(turn.0));
                self.cursor = Some(cursor);
            }
            Err(error) => {
                self.failed
                    .get_or_insert(JournalError::Encode(error.to_string()));
            }
        }
    }

    pub fn is_empty(&self) -> bool {
        self.items.is_empty()
    }

    /// The agent as it will be once this batch is in the log.
    pub fn agent(&self) -> &Agent {
        &self.work
    }

    /// Writes everything in one append, then changes the state.
    pub fn commit(self) -> Result<Committed, JournalError> {
        if let Some(error) = self.failed {
            return Err(error);
        }
        if self.items.is_empty() {
            return Ok(Committed::default());
        }
        let last_seq = self.recorder.log.append(&self.items)?;
        // The items are durable. From here the state follows them.
        let first_seq = last_seq + 1 - self.items.len() as Seq;
        if let Some(newest) = self
            .items
            .iter()
            .rposition(|item| kind::CONVERSATION.has(item.kind))
        {
            self.recorder.conversation_seq = first_seq + newest as Seq;
        }
        if let Some(at) = self.started_again {
            self.recorder.floor = first_seq + at as Seq;
        }
        self.recorder.last_seq = last_seq;
        self.recorder.agent = self.work;
        if let Some(cursor) = self.cursor {
            self.recorder.cursor = Some(cursor);
        }
        Ok(Committed {
            first_seq,
            last_seq,
            events: self.events,
            effects: self.effects,
        })
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use std::path::Path;
    use std::sync::Arc;
    use std::sync::atomic::{AtomicUsize, Ordering};

    use clarp_codec::standard_set;
    use clarp_ports::log::{Item, Kinds, LogEngine};
    use clarp_store_mem::MemEngine;

    use super::*;
    use crate::turn::{Activity, RunOutcome, TurnId, Wait};

    fn open(engine: &MemEngine) -> Recorder {
        Recorder::open(
            engine
                .open(Path::new("/agents/a"), standard_set("zstd").unwrap())
                .unwrap(),
        )
        .unwrap()
    }

    /// The commands of a small working life, each with what a crash right after it must preserve.
    /// One step of a script: a command applied to the recorder at a time.
    type Step = Box<dyn Fn(&mut Recorder, i64) -> Result<Vec<Effect>, JournalError>>;

    fn script() -> Vec<Step> {
        vec![
            Box::new(|r, t| {
                r.accept(b"first".to_vec(), Origin::User, t)
                    .map(|a| a.effects)
            }),
            Box::new(|r, t| {
                r.accept(b"second".to_vec(), Origin::User, t)
                    .map(|a| a.effects)
            }),
            Box::new(|r, t| r.handle(Command::TurnStarted { turn: TurnId(1) }, t)),
            Box::new(|r, t| {
                r.handle(
                    Command::TurnMoved {
                        turn: TurnId(1),
                        activity: Activity::Tool {
                            name: "Bash".to_string(),
                        },
                    },
                    t,
                )
            }),
            Box::new(|r, t| {
                r.accept(
                    b"from another agent".to_vec(),
                    Origin::Agent {
                        agent_id: "arnold".to_string(),
                    },
                    t,
                )
                .map(|a| a.effects)
            }),
            Box::new(|r, t| {
                r.handle(
                    Command::Stop {
                        by: "peter".to_string(),
                    },
                    t,
                )
            }),
            Box::new(|r, t| {
                r.handle(
                    Command::TurnEnded {
                        turn: TurnId(1),
                        outcome: RunOutcome::Completed,
                    },
                    t,
                )
            }),
            Box::new(|r, t| {
                r.handle(
                    Command::Resume {
                        by: "peter".to_string(),
                    },
                    t,
                )
            }),
            Box::new(|r, t| {
                r.accept(b"carry on".to_vec(), Origin::User, t)
                    .map(|a| a.effects)
            }),
            Box::new(|r, t| {
                r.handle(
                    Command::TurnEnded {
                        turn: TurnId(2),
                        outcome: RunOutcome::Completed,
                    },
                    t,
                )
            }),
            Box::new(|r, t| {
                r.handle(
                    Command::TurnEnded {
                        turn: TurnId(3),
                        outcome: RunOutcome::Failed {
                            reason: "limit".to_string(),
                        },
                    },
                    t,
                )
            }),
            Box::new(|r, t| {
                r.handle(
                    Command::TurnEnded {
                        turn: TurnId(4),
                        outcome: RunOutcome::Completed,
                    },
                    t,
                )
            }),
            // A turn whose connection drops: it waits, is tried again, drops again, and
            // gives up. A crash at any of these leaves it where it was.
            Box::new(|r, t| {
                r.accept(b"over a bad line".to_vec(), Origin::User, t)
                    .map(|a| a.effects)
            }),
            Box::new(|r, t| r.handle(Command::TurnStarted { turn: TurnId(5) }, t)),
            Box::new(|r, t| r.handle(dropped(5, t), t)),
            Box::new(|r, t| r.handle(Command::TurnRetried { turn: TurnId(5) }, t)),
            Box::new(|r, t| r.handle(Command::TurnStarted { turn: TurnId(5) }, t)),
            Box::new(|r, t| r.handle(dropped(5, t), t)),
            Box::new(|r, t| r.handle(Command::WaitOver { turn: TurnId(5) }, t)),
            // And one that is stopped while it waits.
            Box::new(|r, t| {
                r.accept(b"once more".to_vec(), Origin::User, t)
                    .map(|a| a.effects)
            }),
            Box::new(|r, t| r.handle(dropped(6, t), t)),
            Box::new(|r, t| {
                r.handle(
                    Command::Stop {
                        by: "peter".to_string(),
                    },
                    t,
                )
            }),
        ]
    }

    /// The connection of a turn's tool dropped: the turn waits a second for its next attempt.
    fn dropped(turn: u64, now_ms: i64) -> Command {
        Command::TurnParked {
            turn: TurnId(turn),
            wait: Wait::Retry {
                until_ms: now_ms + 1_000,
            },
            kind: "connection".to_string(),
            reason: "read ECONNRESET".to_string(),
        }
    }

    #[test]
    fn a_crash_after_any_command_comes_back_in_the_same_state() {
        let engine = MemEngine::new();
        let mut recorder = open(&engine);
        for (step, command) in script().iter().enumerate() {
            command(&mut recorder, 1_000 + step as i64)
                .unwrap_or_else(|e| panic!("step {step}: {e}"));
            let before = recorder.agent().clone();
            drop(recorder); // the process dies here
            recorder = open(&engine);
            assert_eq!(
                recorder.agent(),
                &before,
                "after a crash following step {step}"
            );
        }
        assert_eq!(
            recorder.agent().settled(),
            6,
            "all six messages reached an end"
        );
        assert!(recorder.agent().live().is_none() && recorder.agent().waiting().next().is_none());
    }

    #[test]
    fn a_message_and_its_turn_are_one_write() {
        let engine = MemEngine::new();
        let mut recorder = open(&engine);
        let accepted = recorder.accept(b"hello".to_vec(), Origin::User, 5).unwrap();
        assert_eq!(accepted.message_seq, 1);
        assert_eq!(
            accepted.effects,
            vec![Effect::StartTurn {
                turn: TurnId(1),
                message_seq: 1
            }]
        );
        let log = engine
            .open(Path::new("/agents/a"), standard_set("zstd").unwrap())
            .unwrap();
        let items = log.read_after(0, Kinds::ALL, 10).unwrap();
        let kinds: Vec<u16> = items.iter().map(|item| item.kind).collect();
        assert_eq!(
            kinds,
            vec![kind::MESSAGE, kind::AGENT_EVENT, kind::AGENT_EVENT],
            "the message, its acceptance, its start"
        );
        assert_eq!(items[0].body, b"hello");
        assert_eq!(items[1].turn, Some(1));
    }

    /// A log whose next append fails, as a full disk would make it.
    struct Failing {
        inner: Box<dyn AgentLog>,
        appends_left: Arc<AtomicUsize>,
    }

    impl AgentLog for Failing {
        fn append(&mut self, items: &[NewItem]) -> Result<Seq, LogError> {
            if self.appends_left.load(Ordering::SeqCst) == 0 {
                return Err(LogError::Storage("no space left on device".to_string()));
            }
            self.appends_left.fetch_sub(1, Ordering::SeqCst);
            self.inner.append(items)
        }
        fn last_seq(&self) -> Result<Seq, LogError> {
            self.inner.last_seq()
        }
        fn read_after(
            &self,
            after: Seq,
            kinds: Kinds,
            limit: usize,
        ) -> Result<Vec<Item>, LogError> {
            self.inner.read_after(after, kinds, limit)
        }
        fn read_before(
            &self,
            before: Seq,
            kinds: Kinds,
            limit: usize,
        ) -> Result<Vec<Item>, LogError> {
            self.inner.read_before(before, kinds, limit)
        }
        fn find(&self, key: &str) -> Result<Option<Item>, LogError> {
            self.inner.find(key)
        }
    }

    #[test]
    fn when_the_log_cannot_be_written_nothing_happened() {
        let engine = MemEngine::new();
        let appends_left = Arc::new(AtomicUsize::new(1));
        let inner = engine
            .open(Path::new("/agents/a"), standard_set("zstd").unwrap())
            .unwrap();
        let mut recorder = Recorder::open(Box::new(Failing {
            inner,
            appends_left: appends_left.clone(),
        }))
        .unwrap();
        recorder.accept(b"first".to_vec(), Origin::User, 1).unwrap();
        let before = recorder.agent().clone();

        let refused = recorder.accept(b"second".to_vec(), Origin::User, 2);
        assert!(
            matches!(refused, Err(JournalError::Log(_))),
            "the caller is told the message was not accepted"
        );
        assert_eq!(recorder.agent(), &before, "and the agent is as it was");
        assert_eq!(
            open(&engine).agent(),
            &before,
            "on disk too: no half of the message was kept"
        );

        appends_left.store(10, Ordering::SeqCst);
        let accepted = recorder
            .accept(b"second, again".to_vec(), Origin::User, 3)
            .unwrap();
        assert_eq!(
            accepted.effects,
            vec![],
            "the first turn is still live; the retried message waits"
        );
        assert_eq!(
            recorder.agent().waiting().count(),
            1,
            "and it was accepted once, not twice"
        );
    }

    #[test]
    fn a_refused_command_writes_nothing() {
        let engine = MemEngine::new();
        let mut recorder = open(&engine);
        let refused = recorder.handle(
            Command::TurnEnded {
                turn: TurnId(9),
                outcome: RunOutcome::Completed,
            },
            1,
        );
        assert!(matches!(
            refused,
            Err(JournalError::Refused(Refusal::NotLive(TurnId(9))))
        ));
        let log = engine
            .open(Path::new("/agents/a"), standard_set("zstd").unwrap())
            .unwrap();
        assert_eq!(log.last_seq().unwrap(), 0);
    }

    #[test]
    fn a_log_that_cannot_have_happened_is_refused_at_open() {
        let engine = MemEngine::new();
        let mut log = engine
            .open(Path::new("/agents/a"), standard_set("zstd").unwrap())
            .unwrap();
        let impossible = AgentEvent::Turn {
            turn: TurnId(1),
            change: crate::turn::TurnEvent::Started { at_ms: 1 },
        };
        log.append(&[event_item(&impossible, 1).unwrap()]).unwrap();
        log.append(&[NewItem::new(kind::AGENT_EVENT, 2, b"not json".to_vec())])
            .unwrap();
        drop(log);
        let opened = Recorder::open(
            engine
                .open(Path::new("/agents/a"), standard_set("zstd").unwrap())
                .unwrap(),
        );
        assert!(
            matches!(opened, Err(JournalError::Illegal { seq: 1, .. })),
            "a start with no acceptance cannot have happened"
        );
    }

    fn message(key: &str, admission: Admission) -> Incoming {
        Incoming {
            key: Some(key.to_string()),
            body: key.as_bytes().to_vec(),
            origin: Origin::User,
            admission,
        }
    }

    #[test]
    fn a_message_sent_twice_is_accepted_once() {
        let engine = MemEngine::new();
        let mut recorder = open(&engine);
        let first = recorder
            .accept_message(message("u-1", Admission::Queue), 1)
            .unwrap();
        assert_eq!((first.turn, first.duplicate), (Some(TurnId(1)), false));
        let again = recorder
            .accept_message(message("u-1", Admission::Queue), 2)
            .unwrap();
        assert!(again.duplicate, "the same id is the same message");
        assert_eq!(again.message_seq, first.message_seq);
        assert_eq!(again.effects, vec![], "and it starts nothing");
        assert_eq!(recorder.agent().next_turn(), TurnId(2), "one turn, not two");
        // The retry may arrive after a crash: the log remembers, not the process.
        drop(recorder);
        let mut recorder = open(&engine);
        assert!(
            recorder
                .accept_message(message("u-1", Admission::Queue), 3)
                .unwrap()
                .duplicate
        );
        let other = recorder
            .accept_message(message("u-2", Admission::Queue), 4)
            .unwrap();
        assert_eq!((other.turn, other.duplicate), (Some(TurnId(2)), false));
    }

    #[test]
    fn a_message_left_for_later_enters_the_conversation_when_its_turn_starts() {
        let engine = MemEngine::new();
        let mut recorder = open(&engine);
        let rows = |r: &Recorder| {
            r.log()
                .read_after(0, kind::ROWS, 100)
                .unwrap()
                .into_iter()
                .map(|item| (item.key.unwrap_or_default(), item.turn))
                .collect::<Vec<_>>()
        };
        let first = recorder
            .accept_message(message("u-1", Admission::Later), 1)
            .unwrap();
        assert!(first.entered, "nothing was in its way: it is said at once");
        let said = recorder
            .accept_message(message("u-2", Admission::Queue), 2)
            .unwrap();
        assert!(
            said.entered,
            "said while the agent is busy: in the conversation, and waiting"
        );
        let later = recorder
            .accept_message(message("u-3", Admission::Later), 3)
            .unwrap();
        assert!(!later.entered);
        assert_eq!(
            rows(&recorder),
            vec![("u-1".to_string(), Some(1)), ("u-2".to_string(), Some(2))],
            "what was left for later is in the queue, not in the conversation"
        );
        let before = recorder.conversation_seq();
        assert!(
            recorder
                .accept_message(message("u-3", Admission::Later), 4)
                .unwrap()
                .duplicate,
            "and it is still the same message when it is sent again"
        );
        // The first turn ends, the second starts: its message was in the conversation already.
        let ended = |turn| Command::TurnEnded {
            turn: TurnId(turn),
            outcome: crate::turn::RunOutcome::Completed,
        };
        recorder.handle(ended(1), 5).unwrap();
        assert_eq!(rows(&recorder).len(), 2);
        assert_eq!(
            recorder.conversation_seq(),
            before,
            "the conversation did not change"
        );
        // The second ends, the third starts: now its message enters, in the same write.
        let effects = recorder.handle(ended(2), 6).unwrap();
        assert_eq!(
            effects,
            vec![Effect::StartTurn {
                turn: TurnId(3),
                message_seq: later.message_seq
            }]
        );
        assert_eq!(
            rows(&recorder),
            vec![
                ("u-1".to_string(), Some(1)),
                ("u-2".to_string(), Some(2)),
                ("u-3".to_string(), Some(3))
            ]
        );
        assert_eq!(recorder.conversation_seq(), recorder.last_seq());
        // A restart finds the same conversation and the same newest change.
        let (seq, all) = (recorder.conversation_seq(), rows(&recorder));
        drop(recorder);
        let recorder = open(&engine);
        assert_eq!((recorder.conversation_seq(), rows(&recorder)), (seq, all));
        assert!(
            recorder
                .log()
                .find("u-3")
                .unwrap()
                .is_some_and(|item| item.kind == kind::MESSAGE),
            "the id finds the row, not the waiting copy"
        );
    }

    #[test]
    fn a_direct_message_is_recorded_as_one_and_replaces_the_live_turn() {
        let engine = MemEngine::new();
        let mut recorder = open(&engine);
        recorder
            .accept_message(message("u-1", Admission::Queue), 1)
            .unwrap();
        recorder
            .accept_message(message("u-2", Admission::Queue), 2)
            .unwrap();
        let direct = recorder
            .accept_message(message("u-3", Admission::Direct), 3)
            .unwrap();
        assert_eq!(direct.effects, vec![Effect::StopTurn { turn: TurnId(1) }]);
        let order = |r: &Recorder| r.agent().waiting().map(|q| q.id.0).collect::<Vec<_>>();
        assert_eq!(order(&recorder), vec![3, 2]);
        drop(recorder);
        assert_eq!(
            order(&open(&engine)),
            vec![3, 2],
            "the order survives a restart"
        );
    }

    #[test]
    fn an_agent_started_again_has_a_conversation_that_begins_after_it() {
        let engine = MemEngine::new();
        let mut recorder = open(&engine);
        assert_eq!((recorder.floor(), recorder.conversation_seq()), (0, 0));
        // A message that is answered, one that was said and waits, one left for later.
        recorder
            .accept_message(message("u-1", Admission::Queue), 1)
            .unwrap();
        let said = recorder
            .accept_message(message("u-2", Admission::Queue), 2)
            .unwrap();
        let later = recorder
            .accept_message(message("u-3", Admission::Later), 3)
            .unwrap();
        let waits = |r: &Recorder| -> Vec<(u64, Seq)> {
            let waiting = r.agent().waiting();
            waiting
                .map(|queued| (queued.id.0, queued.message_seq))
                .collect()
        };
        assert_eq!(
            waits(&recorder),
            vec![(2, said.message_seq), (3, later.message_seq)]
        );
        // While its turn runs it is not started again, and nothing is written.
        let before = recorder.last_seq();
        assert_eq!(recorder.start_again(4).unwrap(), None);
        assert_eq!((recorder.last_seq(), recorder.floor()), (before, 0));
        // The turn is stopped, and ends; the mailbox is held meanwhile.
        recorder
            .handle(
                Command::Stop {
                    by: "relaunch".to_string(),
                },
                5,
            )
            .unwrap();
        let ended = Command::TurnEnded {
            turn: TurnId(1),
            outcome: RunOutcome::Stopped {
                by: "relaunch".to_string(),
            },
        };
        recorder.handle(ended, 6).unwrap();
        let old_conversation = recorder.conversation_seq();
        assert!(old_conversation > 0);

        // Started again: one message was said already, and is written anew after where the
        // old conversation ends. The one left for later enters the new one when it runs.
        assert_eq!(recorder.start_again(7).unwrap(), Some(1));
        let floor = recorder.floor();
        assert!(floor > old_conversation, "{floor}");
        let moved = waits(&recorder);
        assert_eq!(moved[1], (3, later.message_seq));
        assert!(moved[0].0 == 2 && moved[0].1 > floor, "{moved:?}");
        assert_eq!(
            recorder.conversation_seq(),
            moved[0].1,
            "the conversation is what was written after"
        );
        let kept = recorder
            .log()
            .read_after(moved[0].1 - 1, Kinds::only(kind::MESSAGE), 1)
            .unwrap();
        assert_eq!(kept[0].body, b"u-2".to_vec(), "in its own words");
        assert_eq!(recorder.agent().backend_session(), None);
        // The same message sent again is still the message that is known.
        let again = recorder
            .accept_message(message("u-2", Admission::Queue), 8)
            .unwrap();
        assert!(again.duplicate);

        // All of it is what a restart of the Host reads back.
        let state = recorder.agent().clone();
        drop(recorder);
        let mut recorder = open(&engine);
        assert_eq!(recorder.agent(), &state);
        assert_eq!(
            (recorder.floor(), recorder.conversation_seq()),
            (floor, moved[0].1)
        );
        // Started again once more with nothing said since: an empty conversation.
        recorder
            .handle(
                Command::Withdraw {
                    turn: TurnId(2),
                    by: "user".to_string(),
                },
                9,
            )
            .unwrap();
        assert_eq!(recorder.start_again(10).unwrap(), Some(0));
        assert!(recorder.floor() > floor);
        assert_eq!(recorder.conversation_seq(), 0, "nothing is in it yet");
    }

    #[test]
    fn a_batch_is_one_write_and_a_refused_command_does_not_spoil_it() {
        let engine = MemEngine::new();
        let mut recorder = open(&engine);
        recorder.accept(b"go".to_vec(), Origin::User, 1).unwrap();
        let before = recorder.log().last_seq().unwrap();

        let mut batch = recorder.batch();
        batch
            .command(Command::TurnStarted { turn: TurnId(1) }, 10)
            .unwrap();
        let reply = batch.item(NewItem::new(kind::REPLY, 11, b"hello".to_vec()).keyed("r-1"));
        assert_eq!(
            batch.command(
                Command::TurnMoved {
                    turn: TurnId(7),
                    activity: Activity::Waiting
                },
                12
            ),
            Err(Refusal::NotLive(TurnId(7))),
            "a signal about another turn is refused"
        );
        batch
            .command(
                Command::TurnEnded {
                    turn: TurnId(1),
                    outcome: RunOutcome::Completed,
                },
                13,
            )
            .unwrap();
        batch.cursor(TurnId(1), 4_096, 13);
        assert!(
            batch.agent().live().is_none(),
            "the batch sees the turn as over"
        );
        let committed = batch.commit().unwrap();

        assert_eq!(committed.first_seq, before + 1);
        assert_eq!(
            recorder.log().find("r-1").unwrap().unwrap().seq,
            committed.seq_of(reply),
            "an item is where the batch said it would be"
        );
        assert_eq!(committed.last_seq, recorder.log().last_seq().unwrap());
        assert!(recorder.agent().live().is_none());
        assert_eq!(recorder.agent().settled(), 1);
        assert_eq!(recorder.spool_offset(TurnId(1)), 4_096);
        assert_eq!(
            recorder.spool_offset(TurnId(2)),
            0,
            "a cursor belongs to its turn"
        );
        let after = recorder.agent().clone();
        drop(recorder);
        let reopened = open(&engine);
        assert_eq!(reopened.agent(), &after);
        assert_eq!(
            reopened.spool_offset(TurnId(1)),
            4_096,
            "the cursor is rebuilt with the agent"
        );
    }

    #[test]
    fn a_batch_that_is_dropped_or_cannot_be_written_never_happened() {
        let engine = MemEngine::new();
        let appends_left = Arc::new(AtomicUsize::new(1));
        let inner = engine
            .open(Path::new("/agents/a"), standard_set("zstd").unwrap())
            .unwrap();
        let mut recorder = Recorder::open(Box::new(Failing {
            inner,
            appends_left: appends_left.clone(),
        }))
        .unwrap();
        recorder.accept(b"go".to_vec(), Origin::User, 1).unwrap();
        let before = recorder.agent().clone();

        let mut dropped = recorder.batch();
        dropped
            .command(Command::TurnStarted { turn: TurnId(1) }, 2)
            .unwrap();
        drop(dropped);
        assert_eq!(recorder.agent(), &before, "a dropped batch changes nothing");

        let mut batch = recorder.batch();
        batch
            .command(Command::TurnStarted { turn: TurnId(1) }, 3)
            .unwrap();
        batch.cursor(TurnId(1), 100, 3);
        assert!(matches!(batch.commit(), Err(JournalError::Log(_))));
        assert_eq!(recorder.agent(), &before, "nor does one the disk refused");
        assert_eq!(recorder.spool_offset(TurnId(1)), 0);

        assert!(
            recorder.batch().commit().unwrap().is_empty(),
            "an empty batch writes nothing and succeeds"
        );
    }
}
