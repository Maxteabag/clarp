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
use clarp_acp::client::{Answer, Ended, Handler};
use clarp_acp::{method, schema};
use serde_json::{Value, json};

use super::{
    Cut, Outcome, Result, Turn, early_for, emit, failed_for, mark_poisoned, read_out, said,
};

/// How long the driver waits, once nothing is open, for the Host to close its input: a message
/// the Host wrote before it read the last answer is still taken up; one that comes later is
/// left unread, and the Host runs it as a turn of its own.
const INPUT_CLOSES_WITHIN: Duration = Duration::from_secs(5);

/// How long a message held for a cut waits for a line end in what agy writes: then the reply
/// is cut where it is.
const LINE_END_WITHIN: Duration = Duration::from_secs(3);

/// How long a cut waits for the cancelled prompt's answer: then the held message goes as a
/// prompt all the same, and agy takes it as it takes any prompt sent while one runs.
const CUT_ANSWER_WITHIN: Duration = Duration::from_secs(8);

/// A message held to cut the reply agy is writing (Peter's ask of 24 Nov 2026, clarp #753).
/// agy 1.5 reads a prompt sent while it writes only once it has written its whole answer: a
/// list of fifty goes on to the end before a "stop" is read. So the reply is cut at its next
/// line end: the prompt is cancelled there, and the message goes as the next prompt on the
/// same conversation, with the reply so far given back before it, since agy forgets the text
/// of a cancelled prompt (it keeps the tools it ran). The cut is no Stop: the turn goes on,
/// and the message is read and answered as any other.
struct Held {
    uuid: String,
    text: String,
    /// The cancel was sent.
    cancelled: bool,
    /// When the wait for the line end, or for the cancel's answer, is over.
    until: Instant,
}

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
    /// The prompt was cancelled for a cut: its answer, whenever it comes, is no Stop.
    cut: bool,
}

/// An answer given while a newer prompt was on its way: the chain of work it ends, waiting for
/// the newer prompt to take it over.
struct Handed {
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
    held: Option<Handed>,
    /// A message held for a cut of the reply agy writes, and those that came after it.
    cut: Option<Held>,
    after_cut: Vec<(String, String)>,
    /// Whether a reply is cut for a message at all ([`steer::CUT_ENV`]).
    cuts_replies: bool,
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
            cut: false,
        }]),
        held: None,
        cut: None,
        after_cut: Vec::new(),
        cuts_replies: steer::cuts_replies(std::env::var(steer::CUT_ENV).ok().as_deref()),
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
                Err(Ended::Woken) => {}
                Err(Ended::Silent | Ended::Closed) | Ok(_) => break,
            }
            continue;
        }
        // A tool that runs says nothing until it ends: a build or a test suite. The short
        // bound holds only while no tool call is open.
        let short = turn.record.open_tools.is_empty();
        let bound = if short { silence } else { tool_silence };
        let ids: Vec<u64> = prompts.flight.iter().map(|sent| sent.id).collect();
        let cut_until = prompts.cut.as_ref().map(|held| held.until);
        let deadline = match (turn.stopping, cut_until) {
            (Some(stop), Some(cut)) => Some(stop.min(cut)),
            (stop, cut) => stop.or(cut),
        };
        match turn
            .client
            .wait_any(&ids, bound, deadline, &mut turn.record)?
        {
            Ok((id, answer)) => {
                prompts.took(turn);
                prompts.answered(turn, id, answer)?;
            }
            Err(Ended::Owner(Some(line))) => prompts.owner(turn, &line)?,
            Err(Ended::Owner(None)) => turn.owner_done = true,
            Err(Ended::Interrupted) => {
                turn.stop(session)?;
                prompts.unhold(turn, "stopped")?;
            }
            Err(Ended::Woken) => prompts.woken(turn)?,
            Err(Ended::Silent) if cut_until.is_some_and(|until| Instant::now() >= until) => {
                prompts.cut_over(turn)?;
            }
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
        if self.cut.is_some() {
            // One that comes while a message is held for a cut goes after it, in order.
            emit(&said(
                "steer",
                json!({"uuid": uuid, "sent": false, "why": "after a cut"}),
            ))?;
            self.after_cut.push((uuid, text));
            return Ok(());
        }
        if self.cuts(turn) {
            emit(&said("cut", json!({"uuid": uuid, "stage": "held"})))?;
            self.cut = Some(Held {
                uuid,
                text,
                cancelled: false,
                until: Instant::now() + LINE_END_WITHIN,
            });
            turn.record.cut = Cut::Line;
            if turn.record.written.at_line_start() {
                // agy's last word ended a line: the reply is cut there.
                self.cut_now(turn)?;
            }
            return Ok(());
        }
        self.send(turn, uuid, &text)
    }

    /// Whether a message that comes now cuts the reply agy writes: agy is writing, no tool
    /// runs, and the prompt it works on is the only one sent. Else it goes at once, as a
    /// prompt agy takes over from the one it runs.
    fn cuts(&self, turn: &Turn) -> bool {
        self.cuts_replies
            && turn.record.writing
            && turn.record.open_tools.is_empty()
            && self.held.is_none()
            && self.flight.len() == 1
            && self
                .flight
                .front()
                .is_some_and(|sent| sent.unread.is_none())
    }

    /// The record saw what the cut waits for: the line end, or agy's stopping to write.
    fn woken(&mut self, turn: &mut Turn) -> Result<()> {
        match turn.record.cut {
            Cut::Due => self.cut_now(turn),
            Cut::Missed => self.release(turn, false),
            Cut::None | Cut::Line | Cut::Cutting => Ok(()),
        }
    }

    /// The prompt agy writes for is cancelled: the cut. What it writes from now on is left
    /// out, until the prompt's answer.
    fn cut_now(&mut self, turn: &mut Turn) -> Result<()> {
        let Some(held) = self.cut.as_mut().filter(|held| !held.cancelled) else {
            return Ok(());
        };
        let _ = turn
            .client
            .notify(method::SESSION_CANCEL, json!({"sessionId": self.session}));
        held.cancelled = true;
        held.until = Instant::now() + CUT_ANSWER_WITHIN;
        if let Some(front) = self.flight.front_mut() {
            front.cut = true;
        }
        // What agy writes for the cancelled prompt is left out until its answer, and what it
        // writes after that is a message of its own.
        turn.record.cut = Cut::Cutting;
        turn.record.writing = false;
        turn.record.written = super::Written::default();
        // Not `cancel`, which is a Stop's: the turn goes on.
        emit(&said(
            "cut",
            json!({"uuid": held.uuid, "stage": "cancel", "session": self.session}),
        ))?;
        Ok(())
    }

    /// The wait the cut set is over: no line end came, so the reply is cut where it is; or
    /// the cancelled prompt did not answer, so the message goes all the same.
    fn cut_over(&mut self, turn: &mut Turn) -> Result<()> {
        match self.cut.as_ref().map(|held| held.cancelled) {
            Some(false) => self.cut_now(turn),
            Some(true) => self.release(turn, true),
            None => Ok(()),
        }
    }

    /// The held message goes as a prompt, after a cut with the reply so far before it, and
    /// the messages that came after it follow.
    fn release(&mut self, turn: &mut Turn, cut: bool) -> Result<()> {
        let Some(held) = self.cut.take() else {
            return Ok(());
        };
        // A cancelled prompt not answered yet (the bound passed) still has what it writes
        // left out, until it leaves the flight; what is left out from now on is kept, since it
        // may be the message's.
        if self.flight.iter().any(|sent| sent.cut) {
            turn.record.keep_late = true;
        } else {
            turn.record.cut = Cut::None;
        }
        let mut text = held.text;
        if cut {
            let reply = turn
                .record
                .answer_text
                .get(turn.record.since..)
                .unwrap_or_default();
            emit(&said(
                "cut",
                json!({"uuid": held.uuid, "stage": "sent", "given_back": reply.len(),
                       "left_out": turn.record.dropped}),
            ))?;
            if !reply.trim().is_empty() {
                text = steer::with_reply(reply, &text);
            }
        }
        turn.record.dropped = 0;
        self.send(turn, held.uuid, &text)?;
        for (uuid, text) in std::mem::take(&mut self.after_cut) {
            self.send(turn, uuid, &text)?;
        }
        Ok(())
    }

    /// A Stop, or the end of the wait: a message held for a cut is not taken, and nothing
    /// names it but its refusal.
    fn unhold(&mut self, turn: &mut Turn, why: &str) -> Result<()> {
        turn.record.cut = Cut::None;
        turn.record.keep_late = false;
        turn.record.late.clear();
        let held = self.cut.take().map(|held| held.uuid);
        let after = std::mem::take(&mut self.after_cut)
            .into_iter()
            .map(|(uuid, _)| uuid);
        for uuid in held.into_iter().chain(after) {
            emit(&json!({"type": "steer_refused", "uuid": uuid, "reason": why}))?;
        }
        Ok(())
    }

    /// A message sent as a prompt: agy takes it over from the prompt it runs, if one runs.
    fn send(&mut self, turn: &mut Turn, uuid: String, text: &str) -> Result<()> {
        let sent = turn.client.send_request(
            method::SESSION_PROMPT,
            json!({"sessionId": self.session, "prompt": [schema::text_block(text)]}),
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
            cut: false,
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

    fn answered(&mut self, turn: &mut Turn, id: u64, mut answer: Answer) -> Result<()> {
        let own_cut = self.flight.iter().any(|sent| sent.id == id && sent.cut);
        // The answer of a prompt cancelled for a cut, in time or after the bound: agy stopped
        // where it was cut. A cut is no Stop: should this answer stand alone (the message
        // refused), it reads as ended.
        if own_cut
            && turn.stopping.is_none()
            && let Answer::Result(result) = &mut answer
            && result["stopReason"] == "cancelled"
        {
            result["stopReason"] = json!("end_turn");
        }
        if self.cut.is_some() && self.flight.front().is_some_and(|sent| sent.id == id) {
            // The prompt a message was held for answered. The held message goes first, so that
            // this answer hands the work over to it, as any answer given while a newer prompt
            // is on its way.
            let cut = self.cut.as_ref().is_some_and(|held| held.cancelled);
            turn.record.writing = false;
            turn.record.written = super::Written::default();
            self.release(turn, cut)?;
        }
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
        // A prompt cancelled for a cut leaves the flight with this answer: its own, or one
        // that passed over it (agy never answered it). What agy writes next is not of it, and
        // a message of its own.
        let passes_cut = self.flight.iter().take(at).any(|sent| sent.cut);
        let late = if own_cut || passes_cut {
            turn.record.cut = Cut::None;
            turn.record.writing = false;
            turn.record.written = super::Written::default();
            turn.record.keep_late = false;
            std::mem::take(&mut turn.record.late)
        } else {
            Vec::new()
        };
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
        if passes_cut {
            // What agy wrote after the message went was the message's, since its answer
            // passed over the cancelled prompt: it follows the message's read. Had the
            // cancelled prompt answered, it would have been that prompt's, and left out.
            for message in late {
                turn.record.notification(&message)?;
            }
        }
        let mut spent = std::mem::take(&mut sent.spent);
        if let Answer::Result(result) = &answer {
            steer::add_usage(&mut spent, &result["usage"]);
        }
        let held = Handed {
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
            turn.record.since = turn.record.answer_text.len();
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
    fn settle(&mut self, turn: &mut Turn, held: Handed) -> Result<()> {
        let text = std::mem::take(&mut turn.record.answer_text);
        turn.record.since = 0;
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
                emit(&said("prompted", json!({"uuid": tag, "result": result})))?;
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
        self.unhold(turn, "unanswered")?;
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
