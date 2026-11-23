//! The prompts of one turn: the turn's own message, and each message the Host sends while the
//! turn runs (`clarp_acp::agy::steer`), all on the one conversation that is open.
//!
//! agy 1.5 holds one prompt at a time. Sent a prompt while another runs, it answers the
//! running one at once as ended and goes on under the new one with both messages (seen on
//! 23 Nov 2026). The work is a chain that passes from prompt to prompt: an answer given while a
//! newer prompt is on its way is held, and the newer prompt takes the chain over the moment
//! Antigravity is seen at work on it (its first update after the answer before it) or answers
//! it. That is when its message is read, and its answer then covers every message the chain
//! carried. An error to the newer prompt before either is a refusal: its message is never
//! read, and the held answer stands.
//!
//! A message read only after the turn's own message was answered is answered as a prompt of
//! its own on the same conversation, and its read comes after that answer: the Host makes it a
//! turn of its own in the same process. The driver goes on until every prompt it sent is
//! answered and the Host's input is closed (the shim closes it on the first settled line), or a
//! short while has passed: what comes later is left unread.

use std::collections::{BTreeSet, VecDeque};
use std::time::{Duration, Instant};

use clarp_acp::agy::{self as shared, Failure, steer};
use clarp_acp::client::{Answer, Ended};
use clarp_acp::{method, schema};
use serde_json::{Value, json};

use super::{Outcome, Result, Turn, early_for, emit, failed_for, mark_poisoned, read_out, said};

/// How long the driver waits, once nothing is open, for the Host to close its input: a message
/// the Host wrote before it read the last answer is still taken up; one that comes later is
/// left unread, and the Host runs it as a turn of its own.
const INPUT_CLOSES_WITHIN: Duration = Duration::from_secs(5);

/// A prompt sent and not answered.
struct Sent {
    id: u64,
    /// The message this prompt carries: the turn's own, or one the Host sent.
    uuid: String,
    /// The messages its answer covers, each of them read (or the turn's own).
    covers: Vec<String>,
    /// The message it carries, until Antigravity is seen to take it.
    unread: Option<String>,
    /// What the answers of the chain it took over spent.
    spent: Value,
}

/// An answer given while a newer prompt was on its way: the chain of work it ends, waiting for
/// the newer prompt to take it over.
struct Held {
    /// The message whose prompt was answered.
    uuid: String,
    covers: Vec<String>,
    answer: Answer,
    spent: Value,
}

struct Prompts<'a> {
    session: &'a str,
    /// The turn's own message.
    own: &'a str,
    flight: VecDeque<Sent>,
    held: Option<Held>,
    /// How the turn's own message was answered, once it was.
    outcome: Option<Outcome>,
    /// Every message the Host gave: each is sent, and read, at most once.
    given: BTreeSet<String>,
}

/// Sends the turn's own prompt and every message the Host sends while it runs, and waits until
/// each is answered or refused: how the turn's own message went.
pub(super) fn converse(
    turn: &mut Turn,
    session: &str,
    own: &str,
    first: Vec<Value>,
    silence: Duration,
) -> Result<Outcome> {
    let id = turn.client.send_request(
        method::SESSION_PROMPT,
        json!({"sessionId": session, "prompt": first}),
    )?;
    turn.record.prompting = true;
    let mut prompts = Prompts {
        session,
        own,
        flight: VecDeque::from([Sent {
            id,
            uuid: own.to_string(),
            covers: (!own.is_empty())
                .then(|| own.to_string())
                .into_iter()
                .collect(),
            unread: None,
            spent: Value::Null,
        }]),
        held: None,
        outcome: None,
        given: BTreeSet::new(),
    };
    // What the Host sent while the conversation was being opened goes now, in order.
    for line in std::mem::take(&mut turn.waiting) {
        prompts.owner(turn, &line)?;
    }
    let tool_silence = Duration::from_secs(shared::TOOL_SILENCE_MOST_SECS);
    let mut closing = None;
    loop {
        prompts.took(turn);
        if prompts.flight.is_empty() {
            if turn.owner_done || turn.stopping.is_some() {
                break;
            }
            let until = *closing.get_or_insert_with(|| Instant::now() + INPUT_CLOSES_WITHIN);
            match turn
                .client
                .wait_any(&[], INPUT_CLOSES_WITHIN, Some(until), &mut turn.record)?
            {
                Err(Ended::Owner(Some(line))) => prompts.owner(turn, &line)?,
                Err(Ended::Owner(None)) => turn.owner_done = true,
                Err(Ended::Interrupted) => turn.stop(session)?,
                Err(Ended::Silent | Ended::Closed) | Ok(_) => break,
            }
            continue;
        }
        // A tool that runs says nothing until it ends: a build or a test suite. The short
        // bound holds only while no tool call is open.
        let short = turn.record.open_tools.is_empty();
        let bound = if short { silence } else { tool_silence };
        let ids: Vec<u64> = prompts.flight.iter().map(|sent| sent.id).collect();
        match turn
            .client
            .wait_any(&ids, bound, turn.stopping, &mut turn.record)?
        {
            Ok((id, answer)) => {
                prompts.took(turn);
                prompts.answered(turn, id, answer)?;
            }
            Err(Ended::Owner(Some(line))) => prompts.owner(turn, &line)?,
            Err(Ended::Owner(None)) => turn.owner_done = true,
            Err(Ended::Interrupted) => turn.stop(session)?,
            Err(Ended::Silent)
                if short && turn.stopping.is_none() && !turn.record.open_tools.is_empty() => {}
            Err(how) => {
                prompts.took(turn);
                prompts.give_up(turn, how)?;
                break;
            }
        }
    }
    turn.record.prompting = false;
    Ok(match prompts.outcome {
        Some(outcome) => outcome,
        None if turn.stopping.is_some() => Outcome::Stopped,
        None => Outcome::Failed("Antigravity's turn ended before it answered".to_string()),
    })
}

impl Prompts<'_> {
    /// A line of the Host's: a message for the running turn, sent as a prompt at once. One
    /// sent to a conversation where nothing runs is read with Antigravity's first update.
    fn owner(&mut self, turn: &mut Turn, line: &Value) -> Result<()> {
        let Some((uuid, text)) = steer::steered(line) else {
            emit(&said(
                "steer",
                json!({"sent": false, "why": "not a message"}),
            ))?;
            return Ok(());
        };
        // The turn's own message is never read again, and a message given twice is sent once.
        if uuid == self.own || !self.given.insert(uuid.clone()) {
            emit(&said(
                "steer",
                json!({"uuid": uuid, "sent": false, "why": "given before"}),
            ))?;
            return Ok(());
        }
        if turn.stopping.is_some() {
            emit(&said(
                "steer",
                json!({"uuid": uuid, "sent": false, "why": "stopped"}),
            ))?;
            return Ok(());
        }
        let sent = turn.client.send_request(
            method::SESSION_PROMPT,
            json!({"sessionId": self.session, "prompt": [schema::text_block(&text)]}),
        );
        let Ok(id) = sent else {
            emit(&said(
                "steer",
                json!({"uuid": uuid, "sent": false, "why": "agy's input is closed"}),
            ))?;
            return Ok(());
        };
        emit(&said("steer", json!({"uuid": uuid, "sent": true})))?;
        if self.flight.is_empty() {
            turn.record.read_next = Some(uuid.clone());
        }
        self.flight.push_back(Sent {
            id,
            uuid: uuid.clone(),
            covers: Vec::new(),
            unread: Some(uuid),
            spent: Value::Null,
        });
        Ok(())
    }

    /// The record printed the read of the first prompt's message: it takes over the chain.
    fn took(&mut self, turn: &mut Turn) {
        let Some(uuid) = turn.record.took.take() else {
            return;
        };
        let Some(front) = self.flight.front_mut() else {
            return;
        };
        if front.unread.as_deref() != Some(uuid.as_str()) {
            return;
        }
        front.unread = None;
        if let Some(held) = self.held.take() {
            let mut covers = held.covers;
            covers.append(&mut front.covers);
            front.covers = covers;
            front.spent = held.spent;
        }
        front.covers.push(uuid);
    }

    fn answered(&mut self, turn: &mut Turn, id: u64, answer: Answer) -> Result<()> {
        // Answers come in the order the prompts were sent. One that passed over an older
        // prompt answered it too: what that one carried and had read goes with it.
        let Some(at) = self.flight.iter().position(|sent| sent.id == id) else {
            return Ok(());
        };
        // A refusal is no passing over: agy turned the newer prompt away and goes on with the
        // older one, whose answer is still to come.
        let refused = self.flight[at].unread.is_some()
            && (matches!(answer, Answer::Error(_)) || turn.stopping.is_some());
        if refused && at > 0 {
            if let Some(sent) = self.flight.remove(at)
                && let Some(uuid) = sent.unread
            {
                emit(&json!({"type": "steer_refused", "uuid": uuid, "reason": error_of(&answer)}))?;
            }
            return Ok(());
        }
        let mut before: Vec<Sent> = self.flight.drain(..at).collect();
        let Some(mut sent) = self.flight.pop_front() else {
            return Ok(());
        };
        if turn.stopping.is_some() && !self.flight.is_empty() {
            // A prompt sent before the Stop becomes Antigravity's next: it is cancelled too,
            // so that nothing unread is worked on.
            let _ = turn
                .client
                .notify(method::SESSION_CANCEL, json!({"sessionId": self.session}));
        }
        for older in &mut before {
            let mut covers = std::mem::take(&mut older.covers);
            covers.append(&mut sent.covers);
            sent.covers = covers;
        }
        if let Some(uuid) = sent.unread.take() {
            let refused = matches!(answer, Answer::Error(_)) || turn.stopping.is_some();
            if refused {
                // Antigravity never took it: it is not read, and nothing names it. The line
                // is for the record; the Host needs none.
                emit(&json!({"type": "steer_refused", "uuid": uuid, "reason": error_of(&answer)}))?;
                self.next_unread(turn);
                if self.flight.is_empty()
                    && let Some(held) = self.held.take()
                {
                    self.settle(turn, held)?;
                }
                return Ok(());
            }
            // Answered: Antigravity took it, even with nothing said between.
            read_out(&uuid)?;
            if let Some(held) = self.held.take() {
                let mut covers = held.covers;
                covers.append(&mut sent.covers);
                sent.covers = covers;
                sent.spent = held.spent;
            }
            sent.covers.push(uuid);
        }
        let mut spent = std::mem::take(&mut sent.spent);
        if let Answer::Result(result) = &answer {
            steer::add_usage(&mut spent, &result["usage"]);
        }
        let held = Held {
            uuid: sent.uuid,
            covers: sent.covers,
            answer,
            spent,
        };
        if self.flight.is_empty() {
            // Nothing waits to be read now: a read held for this prompt was printed above.
            turn.record.read_next = None;
            self.settle(turn, held)
        } else {
            // A newer prompt is on its way: this answer hands the work to it.
            self.held = Some(held);
            self.next_unread(turn);
            Ok(())
        }
    }

    /// The first prompt's message is read with Antigravity's next update, unless the turn
    /// was stopped: after a Stop nothing more is read.
    fn next_unread(&mut self, turn: &mut Turn) {
        turn.record.read_next = self
            .flight
            .front()
            .and_then(|sent| sent.unread.clone())
            .filter(|_| turn.stopping.is_none());
    }

    /// An answer that ends the chain: printed as Antigravity gave it, with what the whole
    /// chain spent, and the messages it covers.
    fn settle(&mut self, turn: &mut Turn, held: Held) -> Result<()> {
        let text = std::mem::take(&mut turn.record.answer_text);
        turn.record.settled = true;
        // The answer ends the message being written, as the Host reads it: what agy writes
        // next is a message of its own.
        turn.record.writing = false;
        let stopped = turn.stopping.is_some();
        let own = held.covers.iter().any(|uuid| uuid == self.own)
            || (self.own.is_empty() && self.outcome.is_none());
        // An answer that covers the turn's own message is told as its answer, so that how the
        // turn stopped (a limit, a refusal) is the turn's; else as that of the prompt answered.
        let tag = tag(self.own, &held.covers, &held.uuid);
        let outcome = match held.answer {
            Answer::Result(mut result) => {
                if held
                    .spent
                    .as_object()
                    .is_some_and(|spent| !spent.is_empty())
                {
                    result["usage"] = held.spent;
                }
                emit(&said(
                    "prompted",
                    json!({"uuid": tag, "result": result}),
                ))?;
                emit(&steer::settled_line(false, &text, &held.covers))?;
                if stopped {
                    Outcome::Stopped
                } else {
                    Outcome::Answered
                }
            }
            Answer::Error(error) => {
                let said = shared::error_text(&error);
                if shared::classify(&said) == Failure::Signature {
                    mark_poisoned(self.session);
                }
                let failure = failed_for("prompt", &error, &tag)?;
                emit(&steer::settled_line(true, &said, &held.covers))?;
                if stopped { Outcome::Stopped } else { failure }
            }
        };
        if own && self.outcome.is_none() {
            self.outcome = Some(outcome);
        }
        Ok(())
    }

    /// The wait ended without an answer: Antigravity is gone or silent, or a Stop's wait is
    /// over. An answer already given stands; a message not read stays unread; work that was
    /// under way fails, unless it was stopped.
    fn give_up(&mut self, turn: &mut Turn, how: Ended) -> Result<()> {
        if let Some(held) = self.held.take() {
            self.settle(turn, held)?;
        }
        turn.record.read_next = None;
        let stopped = turn.stopping.is_some();
        for sent in self.flight.drain(..) {
            match sent.unread {
                // Never taken: nothing reads as its answer.
                Some(uuid) => emit(&json!({"type": "steer_refused", "uuid": uuid,
                                           "reason": "unanswered"}))?,
                // Under way: it failed, and says so for every message it carried. After a
                // Stop the Host ends those with the turn.
                None if !stopped => {
                    let tag = tag(self.own, &sent.covers, &sent.uuid);
                    let ended = early_for("prompt", how.clone(), &tag)?;
                    let text = std::mem::take(&mut turn.record.answer_text);
                    emit(&steer::settled_line(true, &text, &sent.covers))?;
                    turn.record.settled = true;
                    if sent.covers.iter().any(|uuid| uuid == self.own) || self.own.is_empty() {
                        self.outcome.get_or_insert(ended);
                    }
                }
                None => {}
            }
        }
        if stopped {
            self.outcome.get_or_insert(Outcome::Stopped);
        }
        Ok(())
    }
}

/// The message an answer line names: the turn's own (`own`) where the answer covers it.
fn tag(own: &str, covers: &[String], answered: &str) -> String {
    if !own.is_empty() && covers.iter().any(|uuid| uuid == own) {
        own.to_string()
    } else {
        answered.to_string()
    }
}

/// The error a refusal came with, for the record.
fn error_of(answer: &Answer) -> Value {
    match answer {
        Answer::Error(error) => error.clone(),
        Answer::Result(_) => Value::Null,
    }
}
