//! What a running turn has said that the work after it decides on: whether a reply was the
//! answer or was said along the way. The live turn and the replay of its spool after a Host
//! restart apply these same rules, record by record, so that a restart changes nothing of which
//! replies are answers, neither in the log nor for those who watch the turn. The replay only
//! does not write again what the log holds already. The agent's fork follows its own turns by
//! the same rules, and keeps their state in its record in the log.

use clarp_kernel::conversation::{Commentary, Entry};
use clarp_kernel::journal::{SpoolCursor, kind};
use clarp_kernel::live::Hub;
use clarp_kernel::turn::TurnId;
use clarp_ports::log::{AgentLog, Kinds, Seq};
use serde::{Deserialize, Serialize};
use serde_json::Value;

use super::ActorError;

/// Those who watch the turn: its live view, the time of the record that decides, and where what
/// the view sends goes. The live turn sends it; the replay after a restart sends it to nobody.
pub(super) struct Shown<'a> {
    pub(super) hub: &'a mut Hub,
    pub(super) now: i64,
    pub(super) events: &'a mut Vec<Value>,
}

/// Kept in a fork's record under the names that record has always given the first two
/// (`open`, `open_worked`), so that a record written before loads as it was.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub(super) struct Unfollowed {
    /// The replies of this turn that said something and that no tool work has followed yet,
    /// oldest first. Tool work that comes makes them commentary.
    #[serde(rename = "open", default)]
    replies: Vec<String>,
    /// The newest of `replies` carried tool calls: it announced work, and is no answer.
    #[serde(rename = "open_worked", default)]
    worked: bool,
    /// The steps of the plan the tool last told in this turn. A plan told again unchanged (Codex
    /// closes its list so when the task is complete, after its last words) is no work.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    plan: Option<Value>,
}

impl Unfollowed {
    /// What the log says the turn has said that no work followed, as an attempt begins: an
    /// attempt before it, or an actor before this one, may have said it.
    pub(super) fn from_log((replies, worked): (Vec<String>, bool)) -> Self {
        Self {
            replies,
            worked,
            ..Self::default()
        }
    }

    pub(super) fn is_empty(&self) -> bool {
        self.replies.is_empty()
    }

    /// A reply was written: `words` whether it says something, `calls` whether it calls tools.
    /// Returns the replies its calls make said along the way. `shown`: those who watch the
    /// turn, where they watch it.
    pub(super) fn reply(
        &mut self,
        id: &str,
        words: bool,
        calls: bool,
        shown: Option<Shown<'_>>,
    ) -> Vec<String> {
        // The tools it calls are work after what the turn said before.
        let followed = if calls || (words && self.worked) {
            // Words after words whose own message called tools: those announced work and were
            // said along the way, in the conversation as for those who watch the turn, who were
            // shown them so from when their tool began (issue 836). None of them was an answer:
            // words that call tools are the newest of what the turn said until other words or
            // work come.
            self.work()
        } else {
            if words {
                // Words after words: those before were an answer, and stay one whatever work
                // follows these. The conversation view only grows.
                let answers = std::mem::take(&mut self.replies);
                if let Some(shown) = shown {
                    for answer in &answers {
                        let events = shown.hub.close_answer(answer, shown.now);
                        shown.events.extend(events);
                    }
                }
            }
            Vec::new()
        };
        if words {
            self.replies.push(id.to_owned());
            self.worked = calls;
        }
        followed
    }

    /// Work that no reply carries began: a call that is a row by itself. Returns the replies it
    /// makes said along the way.
    pub(super) fn work(&mut self) -> Vec<String> {
        self.worked = false;
        std::mem::take(&mut self.replies)
    }

    /// The tool told its plan. A plan whose steps changed (Codex's `update_plan`) is work,
    /// though no row of the conversation says so: returns the replies it makes said along the
    /// way. One told again with the same steps is no new work.
    pub(super) fn plan(&mut self, steps: &Value) -> Option<Vec<String>> {
        if self.plan.as_ref() == Some(steps) {
            return None;
        }
        self.plan = Some(steps.clone());
        Some(self.work())
    }

    /// A message was read into the turn. It closes what the turn said before it: an answer
    /// given there answered what came before, and the work that follows is for the new
    /// message. Tool work must never make it commentary, which the conversation does not show:
    /// a reply the user was shown stays shown. That holds where the message begins a turn of
    /// its own (`own_turn`: the tool had already ended its answer), and where the newest words
    /// carried no call. Words that announced a call are said along the way, and the work after
    /// them still makes them so.
    pub(super) fn read(&mut self, own_turn: bool, shown: Shown<'_>) {
        if own_turn || !self.worked {
            self.replies.clear();
            self.worked = false;
            // Those who watch the turn are shown them as answers too.
            let events = shown.hub.close_answers(shown.now);
            shown.events.extend(events);
        }
    }
}

/// The newest reply of what a turn has put into the conversation that said something and that
/// no tool work or other words have followed, as the log has it (at most one). An attempt before
/// the one that runs may have said them, and an actor before this one.
pub(super) fn unfollowed(
    log: &dyn AgentLog,
    turn: TurnId,
) -> Result<(Vec<String>, bool), ActorError> {
    unfollowed_before(log, turn, Seq::MAX)
}

/// The same, as the log stood when the attempt that runs began: what the live turn was given
/// at its start. The first attempt begins where the turn does, with nothing of the turn said
/// before it. Each attempt after it begins with a cursor of the turn's spool at its beginning,
/// in the write that tried the turn again or gave it back; it is looked for no further back
/// than the turn's message.
pub(super) fn unfollowed_at_start(
    log: &dyn AgentLog,
    turn: TurnId,
    attempt: u32,
    message_seq: Seq,
) -> Result<(Vec<String>, bool), ActorError> {
    if attempt <= 1 {
        return Ok((Vec::new(), false));
    }
    let began = attempt_began(log, turn, message_seq)?;
    // Every write that makes an attempt after the first (the turn tried again, or given back
    // unsent) writes this cursor in the same batch (`Actor::retry_as` and the two holds
    // for a move). The replay's seed depends on it: an attempt made without it would be
    // seeded from the log as it is now, not as the attempt found it.
    debug_assert!(
        began.is_some(),
        "attempt {attempt} of turn {} began without a cursor of its spool at offset 0",
        turn.0
    );
    match began {
        Some(seq) => unfollowed_before(log, turn, seq),
        // A log written before attempts were marked so: what the log says now stands.
        None => unfollowed(log, turn),
    }
}

/// Where in the log the attempt of this turn that runs began: its newest cursor of the turn's
/// spool at offset 0, looked for no further back than the turn's message.
fn attempt_began(
    log: &dyn AgentLog,
    turn: TurnId,
    message_seq: Seq,
) -> Result<Option<Seq>, ActorError> {
    const AT_A_TIME: usize = 256;
    let mut before = Seq::MAX;
    while before > message_seq {
        let chunk = log.read_before(before, Kinds::only(kind::SPOOL_CURSOR), AT_A_TIME)?;
        for item in chunk.iter().rev().take_while(|item| item.seq > message_seq) {
            let cursor: SpoolCursor = serde_json::from_slice(&item.body).map_err(|error| {
                ActorError::Corrupt(format!("a spool cursor cannot be read: {error}"))
            })?;
            if cursor.turn == turn && cursor.offset == 0 {
                return Ok(Some(item.seq));
            }
        }
        match chunk.first() {
            Some(oldest) if chunk.len() == AT_A_TIME => before = oldest.seq,
            _ => break,
        }
    }
    Ok(None)
}

/// [`unfollowed`], of what the log holds before `before`.
fn unfollowed_before(
    log: &dyn AgentLog,
    turn: TurnId,
    mut before: Seq,
) -> Result<(Vec<String>, bool), ActorError> {
    const AT_A_TIME: usize = 32;
    let corrupt = |error: serde_json::Error| {
        ActorError::Corrupt(format!("a row of turn {} cannot be read: {error}", turn.0))
    };
    let mut said = Vec::new();
    // Whether the newest of them carried tool calls.
    let mut newest_worked = None;
    'reading: loop {
        let chunk = log.read_before(before, kind::ROWS, AT_A_TIME)?;
        let Some(oldest) = chunk.first() else {
            break;
        };
        before = oldest.seq;
        for item in chunk.iter().rev() {
            match Entry::from_item(item).map_err(corrupt)? {
                Some(Entry::Reply(reply)) if reply.turn == turn.0 => {
                    let worked = !reply.tools.is_empty();
                    if !reply.text.trim().is_empty() {
                        // Work no row says (a plan's update) already followed these words.
                        if log.find(&Commentary::id_of(&reply.id))?.is_some() {
                            break 'reading;
                        }
                        // What was said before the newest words was decided when they were
                        // written: an answer when they came without work, else along the way.
                        said.push(reply.id);
                        newest_worked.get_or_insert(worked);
                        break 'reading;
                    }
                    if worked {
                        // What came before this was told when this was written.
                        break 'reading;
                    }
                }
                // A word of the Host about the turn is neither said by the agent nor work.
                Some(Entry::Notice(notice)) if notice.turn == Some(turn.0) => {}
                // A call that is a row, the turn's own message, or what was there before it.
                _ => break 'reading,
            }
        }
        if chunk.len() < AT_A_TIME {
            break;
        }
    }
    said.reverse();
    Ok((said, newest_worked.unwrap_or(false)))
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use std::path::Path;

    use clarp_kernel::conversation::{Message, Reply};
    use clarp_ports::log::{LogEngine, NewItem};

    use super::*;

    fn row(entry: &Entry, at: i64) -> NewItem {
        entry.to_item(at).unwrap()
    }

    fn cursor(turn: TurnId, offset: u64, at: i64) -> NewItem {
        let body = serde_json::to_vec(&SpoolCursor { turn, offset }).unwrap();
        NewItem::new(kind::SPOOL_CURSOR, at, body).in_turn(turn.0)
    }

    /// A retried attempt is given what the turn said before it, as the log stood at its start:
    /// the replay of that attempt after a restart starts from the same, though a message read
    /// into the turn since stands newest in the log.
    #[test]
    fn a_retried_attempt_starts_from_what_was_said_before_it() {
        let mut log = clarp_store_mem::MemEngine::new()
            .open(
                Path::new("/agents/seeded"),
                clarp_codec::standard_set("zstd").unwrap(),
            )
            .unwrap();
        let turn = TurnId(3);
        let message = Message {
            id: "m-3".into(),
            text: "go".into(),
            ..Message::default()
        };
        let message_seq = log.append(&[row(&Entry::Message(message), 1)]).unwrap();
        // The first attempt announced a call, and was tried again before the call's work.
        let announced = Reply {
            id: "r-announced".into(),
            turn: turn.0,
            text: "Looking.".into(),
            tools: vec!["call-1".into()],
            ..Reply::default()
        };
        log.append(&[
            cursor(turn, 40, 2),
            row(&Entry::Reply(announced), 2),
            cursor(turn, 80, 3),
        ])
        .unwrap();
        log.append(&[cursor(turn, 0, 4)]).unwrap();
        // The second attempt read a message into the turn.
        let read = Message {
            id: "m-4".into(),
            text: "and this".into(),
            ..Message::default()
        };
        log.append(&[row(&Entry::Message(read), 5), cursor(turn, 30, 5)])
            .unwrap();

        assert_eq!(
            unfollowed_at_start(&*log, turn, 2, message_seq).unwrap(),
            (vec!["r-announced".to_owned()], true)
        );
        // What the log says now is not what the attempt began with.
        assert_eq!(unfollowed(&*log, turn).unwrap(), (Vec::new(), false));
        // A first attempt begins with nothing of its turn said.
        assert_eq!(
            unfollowed_at_start(&*log, turn, 1, message_seq).unwrap(),
            (Vec::new(), false)
        );
    }
}
