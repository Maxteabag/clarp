//! The anchors of a turn: the rows the conversation view shows in place of its work.
//!
//! The conversation view only grows: a row that has entered it never leaves it. A reply with
//! words enters once what it is can no longer change: when the next thing the turn does is not
//! work (another reply, or a message of the user's read into the turn), or when the turn ends.
//! Words that work follows were said along the way and never enter. Each anchor keeps where it
//! entered, which is its revision from then on, so a reader that asks what is newer is given it.

use std::cell::{Cell, RefCell};
use std::collections::{HashMap, HashSet};

use clarp_kernel::agent::AgentEvent;
use clarp_kernel::conversation::{Commentary, Entry};
use clarp_kernel::journal::kind;
use clarp_kernel::turn::TurnEvent;
use clarp_ports::log::{AgentLog, Item, Kinds, Seq};

use super::{CHUNK, ReadAt, conversation_in, corrupt, joined_read};
use crate::ActorError;

/// A row the conversation view shows in place of the work of a turn: an answer to a message of
/// the user's, or, at the turn's end, what stands for the work after its last answer.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) struct Anchor {
    pub(super) id: String,
    /// How many of the turn's rows the conversation view leaves out for it: those since the
    /// anchor before it.
    pub(super) session_rows: usize,
    /// Where it entered the conversation view: the row or the end that decided it.
    pub(super) entered: Seq,
}

/// The anchors of one turn, as far as it has got.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub(super) struct TurnAnchors {
    /// Oldest first.
    anchors: Vec<Anchor>,
    /// Which of them the turn's end settled: it carries the turn's last count.
    settled: Option<usize>,
    /// The row that decided a reply was an answer, and that reply: a reader given the row is
    /// given the reply again, now in the conversation view.
    pub(super) decides: HashMap<String, String>,
    /// The rows of the messages read into the turn as it ran, oldest first: steered into it,
    /// or read with its own at its start, and which of the two.
    pub(super) read_rows: Vec<(String, ReadAt)>,
    /// The turn's id as the live view names it: the trace of the message it began for. A
    /// message read into the turn carries it as `read_into` (issue 845).
    pub(super) turn_id: Option<String>,
}

impl TurnAnchors {
    pub(super) fn of(&self, id: &str) -> Option<&Anchor> {
        self.anchors.iter().find(|anchor| anchor.id == id)
    }

    pub(super) fn settled(&self) -> Option<&Anchor> {
        self.settled.and_then(|at| self.anchors.get(at))
    }
}

/// A reply with words and no calls of its own whose kind the turn has not decided yet.
#[derive(Debug, Clone)]
struct Pending {
    id: String,
    seq: Seq,
    /// The rows since the last anchor, itself included.
    rows: usize,
}

/// A walk over a turn's rows in the log's order, with what it has decided so far. A walk of
/// the running turn is kept, so a read walks only what was written since.
#[derive(Debug, Clone, Default)]
struct Walk {
    /// The newest log item walked.
    after: Seq,
    decided: TurnAnchors,
    seen: HashSet<String>,
    /// The rows since the last anchor.
    rows: usize,
    pending: Option<Pending>,
    /// The newest reply with words since the last anchor.
    words: Option<(Seq, String)>,
    /// The newest row since the last anchor that is conversation by itself: the turn's message
    /// or a note.
    fallback: Option<(Seq, String)>,
    /// The turn's newest row was a call by itself: its id.
    call: Option<String>,
    /// The turn is in a helper's chat: a message another agent began it with is conversation
    /// by itself, as the user's is ([`super::in_conversation_view`]).
    helper: bool,
}

impl Walk {
    /// A walk from the turn's own message, read back from `from` (the turn's end, or the log's
    /// head), and over everything after it up to there.
    fn start(
        log: &dyn AgentLog,
        turn: u64,
        from: Seq,
        helper: bool,
    ) -> Result<(Self, usize), ActorError> {
        let mut found = Vec::new();
        let mut before = from;
        let mut newest = None;
        'reading: loop {
            let items = log.read_before(before, kind::ROWS, CHUNK)?;
            let Some(oldest) = items.first() else { break };
            before = oldest.seq;
            for item in items.into_iter().rev() {
                newest.get_or_insert(item.seq);
                let prompt = item.turn == Some(turn) && item.kind == kind::MESSAGE;
                found.push(item);
                if prompt {
                    break 'reading;
                }
            }
        }
        let mut walk = Self {
            helper,
            ..Self::default()
        };
        let walked = found.len();
        for item in found.iter().rev() {
            walk.step(log, turn, item)?;
        }
        walk.after = newest.unwrap_or(0);
        Ok((walk, walked))
    }

    /// Walks what was written after where it stands, up to `end` when the turn has ended.
    fn advance(
        &mut self,
        log: &dyn AgentLog,
        turn: u64,
        end: Option<Seq>,
    ) -> Result<usize, ActorError> {
        let mut walked = 0;
        loop {
            let items = log.read_after(self.after, kind::ROWS, CHUNK)?;
            let full = items.len() == CHUNK;
            for item in &items {
                if end.is_some_and(|end| item.seq >= end) {
                    return Ok(walked);
                }
                self.step(log, turn, item)?;
                self.after = item.seq;
                walked += 1;
            }
            if !full {
                return Ok(walked);
            }
        }
    }

    fn step(&mut self, log: &dyn AgentLog, turn: u64, item: &Item) -> Result<(), ActorError> {
        if item.turn != Some(turn) {
            // A message read into the turn: what was said before it answered what came before.
            if read_into(log, item)?
                && let Some(entry) = Entry::from_item(item).map_err(|why| corrupt(item, why))?
            {
                // A join no longer found is taken as a steer: the turn's work splits there, as
                // it did before reads were told apart.
                let at = read_into_turn(log, item)?.map_or(ReadAt::Steer, |(_, at)| at);
                self.call = None;
                self.decided.read_rows.push((entry.id().to_owned(), at));
                self.decide(log, item.seq, entry.id())?;
            }
            return Ok(());
        }
        let Some(entry) = Entry::from_item(item).map_err(|why| corrupt(item, why))? else {
            return Ok(());
        };
        if let Entry::Message(message) = &entry
            && self.decided.turn_id.is_none()
        {
            self.decided.turn_id =
                Some(crate::shown_name(message.trace_id.as_deref(), &message.id));
        }
        let id = entry.id();
        if clarp_kernel::artifacts::forms::is_answer_row(id) || !self.seen.insert(id.to_owned()) {
            return Ok(());
        }
        if conversation_in(&entry, None, self.helper) {
            self.call = None;
            if matches!(entry, Entry::Message(_) | Entry::Note(_)) {
                self.fallback = Some((item.seq, id.to_owned()));
            }
            return Ok(());
        }
        self.rows += 1;
        let call = self.call.take();
        match &entry {
            Entry::Reply(said) if !said.text.trim().is_empty() => {
                if said.tools.is_empty() && !announced(call.as_deref(), &said.id) {
                    // Words after words: those before were an answer.
                    self.decide(log, item.seq, id)?;
                    self.pending = Some(Pending {
                        id: id.to_owned(),
                        seq: item.seq,
                        rows: self.rows,
                    });
                } else {
                    self.pending = None;
                }
                self.words = Some((item.seq, id.to_owned()));
            }
            // Work: what was said before it was said along the way.
            Entry::Reply(said) if !said.tools.is_empty() => self.pending = None,
            Entry::Tool(tool) => {
                self.pending = None;
                if tool.reply.is_none() {
                    self.call = Some(tool.id.clone());
                }
            }
            _ => {}
        }
        Ok(())
    }

    /// The row at `at` (`by`) decides that the words waiting for it are an answer, unless work
    /// that writes no row of its own (a plan's update) came between: the actor marked them as
    /// said along the way then, before `at`.
    fn decide(&mut self, log: &dyn AgentLog, at: Seq, by: &str) -> Result<(), ActorError> {
        let Some(pending) = self.pending.take() else {
            return Ok(());
        };
        if log
            .find(&Commentary::id_of(&pending.id))?
            .is_some_and(|mark| mark.kind == kind::COMMENTARY && mark.seq < at)
        {
            return Ok(());
        }
        self.decided.anchors.push(Anchor {
            id: pending.id.clone(),
            session_rows: pending.rows - 1,
            entered: at,
        });
        self.decided.decides.insert(by.to_owned(), pending.id);
        self.rows -= pending.rows;
        self.words = None;
        if self
            .fallback
            .as_ref()
            .is_some_and(|(seq, _)| *seq < pending.seq)
        {
            self.fallback = None;
        }
        Ok(())
    }

    /// The anchors as they stand. At the turn's end (`end`), what came after its last answer is
    /// settled: its newest words, else its newest note or its message, carries the count.
    fn anchors(&self, end: Option<Seq>) -> TurnAnchors {
        let mut anchors = self.decided.clone();
        let Some(end) = end else {
            return anchors;
        };
        let settled = match (&self.words, &self.fallback) {
            (Some((_, id)), _) => Some((id.clone(), self.rows.saturating_sub(1))),
            (None, Some((_, id))) => Some((id.clone(), self.rows)),
            // Work after the last answer, and nothing said after it: the last answer counts it,
            // so no work of the turn is left out of every count.
            (None, None) if self.rows > 0 => {
                if let Some(at) = anchors.anchors.len().checked_sub(1) {
                    anchors.anchors[at].session_rows += self.rows;
                    anchors.anchors[at].entered = end;
                    anchors.settled = Some(at);
                }
                None
            }
            _ => None,
        };
        if let Some((id, session_rows)) = settled {
            anchors.settled = Some(anchors.anchors.len());
            anchors.anchors.push(Anchor {
                id,
                session_rows,
                entered: end,
            });
        }
        anchors
    }
}

/// Whether these words were written by OpenCode before this change, right after the call they
/// announced: it wrote a step's call (`t-<name>-<call>`) as a row when the call began and the
/// step's words (`r-<name>-<step>`) only when the next step began. Those words went with the
/// call, and are no answer. It now writes them first, with the call, as every other tool does.
fn announced(call: Option<&str>, reply: &str) -> bool {
    let (Some(call), Some(reply)) = (
        call.and_then(|call| call.strip_prefix("t-")),
        reply.strip_prefix("r-"),
    ) else {
        return false;
    };
    reply.rsplit_once('-').is_some_and(|(name, step)| {
        !step.is_empty()
            && step.bytes().all(|byte| byte.is_ascii_digit())
            && call
                .strip_prefix(name)
                .is_some_and(|rest| rest.starts_with('-'))
    })
}

/// Whether this row of another turn is a message read into a running turn of the agent's own
/// session: a message the agent's conversation fork read is the fork's turn, not a part of it.
fn read_into(log: &dyn AgentLog, item: &Item) -> Result<bool, ActorError> {
    if item.kind != kind::MESSAGE || !joined_read(log, item)? {
        return Ok(false);
    }
    let Some(joined) = item.turn else {
        return Ok(false);
    };
    Ok(log
        .find(&crate::fork::turn_key(joined))?
        .is_none_or(|record| record.kind != kind::FORK))
}

/// The turn a message was read into, when it is one: the one its join named, and whether it
/// was read with that turn's own message at its start or steered in as it ran.
pub(super) fn read_into_turn(
    log: &dyn AgentLog,
    item: &Item,
) -> Result<Option<(u64, ReadAt)>, ActorError> {
    if !read_into(log, item)? {
        return Ok(None);
    }
    let Some(joined) = item.turn else {
        return Ok(None);
    };
    let mut before = item.seq;
    loop {
        let events = log.read_before(before, Kinds::only(kind::AGENT_EVENT), CHUNK)?;
        let Some(oldest) = events.first() else {
            return Ok(None);
        };
        before = oldest.seq;
        for event in events
            .iter()
            .rev()
            .filter(|event| event.turn == Some(joined))
        {
            match serde_json::from_slice::<AgentEvent>(&event.body)
                .map_err(|why| corrupt(event, why))?
            {
                AgentEvent::Joined { join, .. } => {
                    let at = if join.together {
                        ReadAt::Start
                    } else {
                        ReadAt::Steer
                    };
                    return Ok(Some((join.into.0, at)));
                }
                // Its acceptance comes before any join: there was none.
                AgentEvent::Turn {
                    change: TurnEvent::Accepted { .. },
                    ..
                } => return Ok(None),
                _ => {}
            }
        }
    }
}

/// The anchors of a turn, read from its rows. `end`: where the turn's end is written, once it
/// has ended.
pub(super) fn find_anchors(
    log: &dyn AgentLog,
    turn: u64,
    end: Option<Seq>,
    helper: bool,
) -> Result<TurnAnchors, ActorError> {
    let (walk, _) = Walk::start(log, turn, end.unwrap_or(Seq::MAX), helper)?;
    Ok(walk.anchors(end))
}

/// How many turns' walks the actor keeps.
const KEPT: usize = 32;

/// The walks of the newest turns, kept by the actor between reads: a read of a running turn
/// walks only what was written since the last one.
#[derive(Default)]
pub(crate) struct AnchorCache {
    /// Each kept walk, by its turn and whether it was read as a helper's chat, with when it
    /// was last read.
    walks: RefCell<HashMap<(u64, bool), (u64, Walk)>>,
    reads: Cell<u64>,
    /// How many log items its walks have read, for a test to see that a read costs what is new.
    walked: Cell<usize>,
}

impl AnchorCache {
    pub(super) fn anchors(
        &self,
        log: &dyn AgentLog,
        turn: u64,
        end: Option<Seq>,
        helper: bool,
    ) -> Result<TurnAnchors, ActorError> {
        let key = (turn, helper);
        let read = self.reads.get() + 1;
        self.reads.set(read);
        let mut walks = self.walks.borrow_mut();
        if !walks.contains_key(&key) {
            let (walk, walked) = Walk::start(log, turn, end.unwrap_or(Seq::MAX), helper)?;
            self.walked.set(self.walked.get() + walked);
            // The walk read longest ago goes: the running turn is read with every page.
            while walks.len() >= KEPT {
                let Some(oldest) = walks
                    .iter()
                    .min_by_key(|(_, (last, _))| *last)
                    .map(|(key, _)| *key)
                else {
                    break;
                };
                walks.remove(&oldest);
            }
            walks.insert(key, (read, walk));
        }
        let Some((last, walk)) = walks.get_mut(&key) else {
            return Ok(TurnAnchors::default());
        };
        *last = read;
        let walked = walk.advance(log, turn, end)?;
        self.walked.set(self.walked.get() + walked);
        if end.is_some() {
            // An ended turn is walked no further: which rows it has seen no longer matters.
            walk.seen = HashSet::new();
        }
        Ok(walk.anchors(end))
    }

    #[cfg(test)]
    pub(super) fn walked(&self) -> usize {
        self.walked.get()
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use std::collections::BTreeSet;
    use std::path::Path;

    use clarp_kernel::agent::Command;
    use clarp_kernel::conversation::{Commentary, Entry, Message, Reply, ToolCall, TurnEnd};
    use clarp_kernel::journal::{Incoming, Recorder};
    use clarp_kernel::steering::Join;
    use clarp_kernel::turn::{Admission, Origin, RunOutcome, TurnId};
    use clarp_ports::log::LogEngine;

    use super::super::{REVISION_STEP, Span, Versioned, Which, after, tail};
    use super::AnchorCache;

    /// One thing the turn does, written to its log.
    type Step = Box<dyn Fn(&mut Recorder)>;

    fn open() -> Recorder {
        let log = clarp_store_mem::MemEngine::new()
            .open(
                Path::new("/agents/anchors"),
                clarp_codec::standard_set("zstd").unwrap(),
            )
            .unwrap();
        Recorder::open(log).unwrap()
    }

    fn accept(recorder: &mut Recorder, id: &str, now: i64) -> TurnId {
        let message = Message {
            id: id.into(),
            text: id.into(),
            origin: Origin::User,
            ..Message::default()
        };
        recorder
            .accept_message(
                Incoming {
                    key: Some(message.id.clone()),
                    body: serde_json::to_vec(&message).unwrap(),
                    origin: Origin::User,
                    admission: Admission::Queue,
                    coordination: None,
                    user_word: false,
                },
                now,
            )
            .unwrap()
            .turn
            .unwrap()
    }

    /// A turn of the user's that runs: its message is `u-ask`.
    fn start(recorder: &mut Recorder) -> TurnId {
        let turn = accept(recorder, "u-ask", 100);
        recorder.handle(Command::TurnStarted { turn }, 110).unwrap();
        turn
    }

    /// Writes what the actor writes when the agent says something whole: the reply, with the
    /// calls its message makes, and that what was said before it is along the way (`along`).
    fn say(recorder: &mut Recorder, turn: TurnId, id: &str, tools: &[&str], along: &[&str]) {
        let mut batch = recorder.batch();
        for earlier in along {
            let part = Entry::Commentary(Commentary::of(earlier, turn.0));
            batch.item(part.to_item(200).unwrap());
        }
        let reply = Entry::Reply(Reply {
            id: id.into(),
            turn: turn.0,
            text: id.into(),
            tools: tools.iter().map(|tool| (*tool).to_owned()).collect(),
            ..Reply::default()
        });
        batch.item(reply.to_item(200).unwrap());
        batch.commit().unwrap();
    }

    /// A call no message carries (a Codex command): a row of work by itself. What was said
    /// before it is along the way (`along`).
    fn call(recorder: &mut Recorder, turn: TurnId, id: &str, along: &[&str]) {
        let mut batch = recorder.batch();
        for earlier in along {
            let part = Entry::Commentary(Commentary::of(earlier, turn.0));
            batch.item(part.to_item(200).unwrap());
        }
        let call = Entry::Tool(ToolCall {
            id: id.into(),
            turn: turn.0,
            name: "Bash".into(),
            ..ToolCall::default()
        });
        batch.item(call.to_item(200).unwrap());
        batch.commit().unwrap();
    }

    /// A message of the user's read into the running turn (steering).
    fn read_in(recorder: &mut Recorder, into: TurnId, id: &str) {
        let followup = accept(recorder, id, 300);
        let mut batch = recorder.batch();
        let join = Join {
            into,
            uuid: id.into(),
            recovery: None,
            together: false,
        };
        batch
            .command(
                Command::Join {
                    turn: followup,
                    join,
                },
                310,
            )
            .unwrap();
        batch
            .command(Command::JoinRead { turn: followup }, 320)
            .unwrap();
        batch.commit().unwrap();
    }

    fn end(recorder: &mut Recorder, turn: TurnId, closes: &str) {
        let mut batch = recorder.batch();
        batch
            .command(
                Command::TurnEnded {
                    turn,
                    outcome: RunOutcome::Completed,
                },
                400,
            )
            .unwrap();
        let end = TurnEnd::of(batch.agent().last().unwrap(), closes).unwrap();
        batch.item(Entry::TurnEnd(end).to_item(400).unwrap());
        batch.commit().unwrap();
    }

    fn span(recorder: &Recorder) -> Span<'static> {
        Span {
            head_revision: recorder.conversation_seq() * REVISION_STEP,
            ..Span::default()
        }
    }

    /// What a chat holds now, in the order it shows it: each row and its count, when it is
    /// in the conversation view.
    fn now(recorder: &Recorder) -> Vec<(String, Option<usize>)> {
        tail(recorder.log(), &[], span(recorder), 100, Which::chat(true))
            .unwrap()
            .entries
            .iter()
            .map(|row| (row.entry.id().to_owned(), row.session_rows))
            .collect()
    }

    /// What a reader of the conversation view alone holds now.
    fn conversation(recorder: &Recorder) -> Vec<String> {
        let which = Which::chat(true).conversation_view(false);
        tail(recorder.log(), &[], span(recorder), 100, which)
            .unwrap()
            .entries
            .iter()
            .map(|row| row.entry.id().to_owned())
            .collect()
    }

    /// What a reader of the conversation view that held everything up to `revision` is given,
    /// and its next cursor.
    fn delta(recorder: &Recorder, revision: u64) -> (Vec<Versioned>, u64) {
        let which = Which::chat(true).conversation_view(false);
        let page = after(recorder.log(), revision, &[], span(recorder), 100, which).unwrap();
        (page.entries, page.latest_revision)
    }

    fn revision_of(recorder: &Recorder, id: &str) -> u64 {
        recorder.log().find(id).unwrap().unwrap().seq * REVISION_STEP
    }

    #[test]
    fn words_that_a_call_follows_never_enter_the_conversation_view() {
        // Codex: a message whole by itself, then a command of its own.
        let mut recorder = open();
        let turn = start(&mut recorder);
        say(&mut recorder, turn, "r-look", &[], &[]);
        // Not yet: what the turn does next decides what these words were.
        assert_eq!(conversation(&recorder), ["u-ask"]);
        call(&mut recorder, turn, "call-1", &["r-look"]);
        assert_eq!(conversation(&recorder), ["u-ask"]);
        say(&mut recorder, turn, "r-done", &[], &[]);
        assert_eq!(conversation(&recorder), ["u-ask"]);
        end(&mut recorder, turn, "r-done");
        assert_eq!(conversation(&recorder), ["u-ask", "r-done"]);
        assert_eq!(
            now(&recorder),
            [
                ("u-ask".to_owned(), None),
                ("r-look".to_owned(), None),
                ("call-1".to_owned(), None),
                ("r-done".to_owned(), Some(2)),
            ]
        );
    }

    #[test]
    fn two_replies_with_no_work_between_both_stay_in_the_conversation_view() {
        let mut recorder = open();
        let turn = start(&mut recorder);
        say(&mut recorder, turn, "r-one", &[], &[]);
        let held = revision_of(&recorder, "r-one");
        say(&mut recorder, turn, "r-two", &[], &[]);
        // The second words decide that the first were an answer: it enters with them.
        assert_eq!(conversation(&recorder), ["u-ask", "r-one"]);
        let (given, _) = delta(&recorder, held);
        assert_eq!(given.len(), 1);
        assert_eq!(given[0].entry.id(), "r-one");
        assert_eq!(given[0].session_rows, Some(0));
        assert_eq!(given[0].revision, revision_of(&recorder, "r-two"));
        // Work after the second makes it words along the way; the first stays an answer.
        say(&mut recorder, turn, "r-call", &["t-1"], &["r-two"]);
        say(&mut recorder, turn, "r-three", &[], &[]);
        end(&mut recorder, turn, "r-three");
        assert_eq!(conversation(&recorder), ["u-ask", "r-one", "r-three"]);
        assert_eq!(
            now(&recorder),
            [
                ("u-ask".to_owned(), None),
                ("r-one".to_owned(), Some(0)),
                ("r-two".to_owned(), None),
                ("r-call".to_owned(), None),
                ("r-three".to_owned(), Some(2)),
            ]
        );
    }

    #[test]
    fn an_answer_given_before_a_message_read_into_the_turn_stays_an_answer() {
        // Issue 745: the user's follow-up is read into the turn after a whole answer.
        let mut recorder = open();
        let turn = start(&mut recorder);
        say(&mut recorder, turn, "r-first", &[], &[]);
        let held = revision_of(&recorder, "r-first");
        read_in(&mut recorder, turn, "u-more");
        assert_eq!(conversation(&recorder), ["u-ask", "r-first", "u-more"]);
        // A reader of the conversation view alone is given the answer with the message.
        let (given, _) = delta(&recorder, held);
        let ids: Vec<_> = given.iter().map(|row| row.entry.id()).collect();
        assert_eq!(ids, ["r-first", "u-more"]);
        say(&mut recorder, turn, "r-call", &["t-1"], &[]);
        say(&mut recorder, turn, "r-second", &[], &[]);
        let before = now(&recorder);
        end(&mut recorder, turn, "r-second");
        assert_eq!(
            conversation(&recorder),
            ["u-ask", "r-first", "u-more", "r-second"]
        );
        let settled = now(&recorder);
        assert_eq!(settled[1], ("r-first".to_owned(), Some(0)));
        assert_eq!(before[1], settled[1]);
        assert_eq!(settled[4], ("r-second".to_owned(), Some(1)));
    }

    #[test]
    fn a_turn_whose_last_words_were_work_settles_as_it_always_did() {
        // Nothing that answered a message by itself: at the end its newest words carry the
        // count of the whole turn, a message read into it or not.
        let mut recorder = open();
        let turn = start(&mut recorder);
        say(&mut recorder, turn, "r-look", &["t-1"], &[]);
        read_in(&mut recorder, turn, "u-more");
        say(&mut recorder, turn, "r-call", &["t-2"], &[]);
        end(&mut recorder, turn, "r-call");
        assert_eq!(
            now(&recorder),
            [
                ("u-ask".to_owned(), None),
                ("r-look".to_owned(), None),
                ("u-more".to_owned(), None),
                ("r-call".to_owned(), Some(1)),
            ]
        );
        // A turn that said nothing: its message carries the count.
        let mut recorder = open();
        let turn = start(&mut recorder);
        let quiet = Entry::Reply(Reply {
            id: "r-quiet".into(),
            turn: turn.0,
            tools: vec!["t-1".into()],
            ..Reply::default()
        });
        let mut batch = recorder.batch();
        batch.item(quiet.to_item(250).unwrap());
        batch.commit().unwrap();
        end(&mut recorder, turn, "r-quiet");
        assert_eq!(
            now(&recorder),
            [("u-ask".to_owned(), Some(1)), ("r-quiet".to_owned(), None)]
        );
    }

    #[test]
    fn a_read_during_a_long_running_turn_walks_only_what_is_new() {
        let mut recorder = open();
        let turn = start(&mut recorder);
        for at in 0..300 {
            say(
                &mut recorder,
                turn,
                &format!("r-{at}"),
                &[&format!("t-{at}")],
                &[],
            );
        }
        let cache = AnchorCache::default();
        let read = |recorder: &Recorder| {
            let span = Span {
                anchor_cache: Some(&cache),
                ..span(recorder)
            };
            tail(recorder.log(), &[], span, 20, Which::chat(true)).unwrap();
        };
        read(&recorder);
        let first = cache.walked();
        assert!(first >= 300, "{first}");
        read(&recorder);
        assert_eq!(cache.walked(), first);
        say(&mut recorder, turn, "r-new", &[], &[]);
        read(&recorder);
        assert_eq!(cache.walked(), first + 1);
        // What the kept walk says is what a walk from the start says.
        let fresh = tail(recorder.log(), &[], span(&recorder), 20, Which::chat(true)).unwrap();
        let span = Span {
            anchor_cache: Some(&cache),
            ..span(&recorder)
        };
        let kept = tail(recorder.log(), &[], span, 20, Which::chat(true)).unwrap();
        assert_eq!(fresh.entries, kept.entries);
    }

    #[test]
    fn a_row_that_entered_the_conversation_view_never_leaves_it() {
        let mut recorder = open();
        let turn = start(&mut recorder);
        let mut entered: BTreeSet<String> = BTreeSet::new();
        let (mut held, mut cursor) = (Vec::<String>::new(), 0);
        let mut steps: Vec<Step> = vec![
            Box::new(move |r| say(r, turn, "r-a", &[], &[])),
            Box::new(move |r| say(r, turn, "r-b", &[], &[])),
            Box::new(move |r| call(r, turn, "call-1", &["r-b"])),
            Box::new(move |r| say(r, turn, "r-c", &["t-2"], &[])),
            Box::new(move |r| say(r, turn, "r-d", &[], &[])),
            Box::new(move |r| read_in(r, turn, "u-more")),
            Box::new(move |r| say(r, turn, "r-e", &[], &[])),
            Box::new(move |r| call(r, turn, "call-3", &["r-e"])),
            Box::new(move |r| say(r, turn, "r-f", &[], &[])),
            Box::new(move |r| end(r, turn, "r-f")),
        ];
        for step in steps.drain(..) {
            step(&mut recorder);
            // Whoever reads every row: a row once in the conversation view stays there.
            let shown: BTreeSet<String> = now(&recorder)
                .into_iter()
                .filter(|(_, count)| count.is_some())
                .map(|(id, _)| id)
                .collect();
            assert!(entered.is_subset(&shown), "{entered:?} {shown:?}");
            entered = shown;
            // Whoever reads the conversation view alone: it only grows, in its order, and
            // what is newer brings each row that entered.
            let view = conversation(&recorder);
            assert!(view.starts_with(&held), "{held:?} {view:?}");
            let (given, next) = delta(&recorder, cursor);
            for row in given {
                let id = row.entry.id().to_owned();
                if !held.contains(&id) {
                    held.push(id);
                }
            }
            cursor = next;
            let mut sorted = held.clone();
            sorted.sort();
            let mut expected = view.clone();
            expected.sort();
            assert_eq!(sorted, expected);
            held = view;
        }
        assert_eq!(
            held,
            ["u-ask", "r-a", "r-d", "u-more", "r-f"].map(str::to_owned)
        );
    }

    #[test]
    fn a_plan_update_between_words_is_work() {
        // Codex: a preamble, its plan updated (no row; the actor marks the preamble), more words.
        let mut recorder = open();
        let turn = start(&mut recorder);
        say(&mut recorder, turn, "r-plan", &[], &[]);
        let mut batch = recorder.batch();
        let mark = Entry::Commentary(Commentary::of("r-plan", turn.0));
        batch.item(mark.to_item(200).unwrap());
        batch.commit().unwrap();
        say(&mut recorder, turn, "r-run", &[], &[]);
        assert_eq!(conversation(&recorder), ["u-ask"]);
        end(&mut recorder, turn, "r-run");
        assert_eq!(conversation(&recorder), ["u-ask", "r-run"]);
    }

    #[test]
    fn work_after_the_last_answer_with_nothing_said_after_it_is_counted_on_that_answer() {
        let mut recorder = open();
        let turn = start(&mut recorder);
        say(&mut recorder, turn, "r-first", &[], &[]);
        read_in(&mut recorder, turn, "u-more");
        call(&mut recorder, turn, "call-1", &[]);
        let running = now(&recorder);
        assert_eq!(running[1], ("r-first".to_owned(), Some(0)));
        end(&mut recorder, turn, "call-1");
        let settled = now(&recorder);
        assert_eq!(settled[1], ("r-first".to_owned(), Some(1)));
        assert_eq!(conversation(&recorder), ["u-ask", "r-first", "u-more"]);
        // The end gives it again with its new count.
        let seq = recorder.log().find("call-1").unwrap().unwrap().seq;
        let (given, _) = delta(&recorder, seq * REVISION_STEP);
        assert!(
            given
                .iter()
                .any(|row| row.entry.id() == "r-first" && row.session_rows == Some(1))
        );
    }

    #[test]
    fn the_answer_a_read_message_decided_is_given_again_past_a_forks_rows() {
        use clarp_kernel::journal::kind;
        use clarp_ports::log::NewItem;
        let mut recorder = open();
        let turn = start(&mut recorder);
        say(&mut recorder, turn, "r-first", &[], &[]);
        let held = revision_of(&recorder, "r-first");
        // The conversation fork reads a message of the user's and answers it, as its own turn.
        read_in(&mut recorder, turn, "u-fork");
        let fork = recorder
            .log()
            .find("u-fork")
            .unwrap()
            .unwrap()
            .turn
            .unwrap();
        let mut batch = recorder.batch();
        batch.item(
            NewItem::new(kind::FORK, 330, b"fork-session".to_vec())
                .keyed(crate::fork::turn_key(fork)),
        );
        let answer = Entry::Reply(Reply {
            id: "r-fork".into(),
            turn: fork,
            text: "r-fork".into(),
            ..Reply::default()
        });
        batch.item(answer.to_item(340).unwrap());
        batch.commit().unwrap();
        // Then a message is read into the running turn itself.
        read_in(&mut recorder, turn, "u-more");
        let (given, _) = delta(&recorder, held);
        let ids: Vec<_> = given.iter().map(|row| row.entry.id()).collect();
        assert!(ids.contains(&"r-first"), "{ids:?}");
        assert!(conversation(&recorder).contains(&"r-first".to_owned()));
    }

    #[test]
    fn words_old_opencode_wrote_after_the_call_they_announced_are_no_answer() {
        // Before its words went with their call, OpenCode wrote a step's call when it began
        // and the step's words when the next step began: call, words, call, words, words.
        let mut recorder = open();
        let turn = start(&mut recorder);
        call(&mut recorder, turn, "t-oc-call_1", &[]);
        say(&mut recorder, turn, "r-oc-1", &[], &[]);
        call(&mut recorder, turn, "t-oc-call_2", &["r-oc-1"]);
        say(&mut recorder, turn, "r-oc-2", &[], &[]);
        say(&mut recorder, turn, "r-oc-3", &[], &[]);
        end(&mut recorder, turn, "r-oc-3");
        assert_eq!(conversation(&recorder), ["u-ask", "r-oc-3"]);
        // Words after a call of another tool are words after work, and may be an answer.
        assert!(!super::announced(Some("c-codex-item_1"), "r-codex-item_2"));
        assert!(!super::announced(Some("t-oc-call_1"), "r-other-2"));
        assert!(super::announced(Some("t-oc-call_1"), "r-oc-2"));
    }

    #[test]
    fn a_helpers_chat_holds_the_turns_other_agents_give_it() {
        // A turn another agent asked for, marked as one that answers that agent.
        let mut recorder = open();
        let message = Message {
            id: "u-asked".into(),
            text: "u-asked".into(),
            origin: Origin::Agent {
                agent_id: "parent".into(),
            },
            ..Message::default()
        };
        let turn = recorder
            .accept_message(
                Incoming {
                    key: Some(message.id.clone()),
                    body: serde_json::to_vec(&message).unwrap(),
                    origin: message.origin.clone(),
                    admission: Admission::Queue,
                    coordination: Some("parent".into()),
                    user_word: false,
                },
                100,
            )
            .unwrap()
            .turn
            .unwrap();
        recorder.handle(Command::TurnStarted { turn }, 110).unwrap();
        // What the agent says for it carries the turn's origin, as the actor writes it.
        for (id, tools) in [("r-look", vec!["t-1".to_owned()]), ("r-answer", Vec::new())] {
            let reply = Entry::Reply(Reply {
                id: id.into(),
                turn: turn.0,
                text: id.into(),
                tools,
                origin: message.origin.clone(),
                ..Reply::default()
            });
            let mut batch = recorder.batch();
            batch.item(reply.to_item(200).unwrap());
            batch.commit().unwrap();
        }
        end(&mut recorder, turn, "r-answer");
        let read = |which: Which| -> Vec<(String, Option<usize>)> {
            tail(recorder.log(), &[], span(&recorder), 100, which)
                .unwrap()
                .entries
                .iter()
                .map(|row| (row.entry.id().to_owned(), row.session_rows))
                .collect()
        };
        // The helper's: the agent's message and the answer, which counts the work alone.
        let helper = Which::chat(true).of_helper(true);
        assert_eq!(
            read(helper.conversation_view(true)),
            [
                ("u-asked".to_owned(), None),
                ("r-answer".to_owned(), Some(1)),
            ]
        );
        assert!(super::super::in_conversation_view(
            &recorder_entry(&recorder, "u-asked"),
            None,
            true,
            true
        ));
        // Any other agent's: none of it is the user's conversation.
        assert!(read(Which::chat(true).conversation_view(true)).is_empty());
        assert!(!super::super::in_conversation_view(
            &recorder_entry(&recorder, "u-asked"),
            None,
            true,
            false
        ));
    }

    fn recorder_entry(recorder: &Recorder, id: &str) -> Entry {
        let item = recorder.log().find(id).unwrap().unwrap();
        Entry::from_item(&item).unwrap().unwrap()
    }

    /// The turn each message row says it was read into, as a reader of everything and a
    /// reader of the chat are given them now.
    /// The turn a row says it was read into: its trace, its number, and how it was read.
    type Link = (String, u64, &'static str);

    fn read_into(recorder: &Recorder) -> Vec<(String, Option<Link>)> {
        let mut read = Vec::new();
        for which in [super::super::Which::All, Which::chat(true)] {
            let page = tail(recorder.log(), &[], span(recorder), 100, which).unwrap();
            let rows: Vec<_> = page
                .entries
                .iter()
                .filter(|row| matches!(row.entry, Entry::Message(_)))
                .map(|row| {
                    let into = row.read_into.as_ref();
                    let into = into.map(|into| (into.trace_id.clone(), into.turn, into.at.name()));
                    (row.entry.id().to_owned(), into)
                })
                .collect();
            if !read.is_empty() {
                assert_eq!(read, rows, "every reader is given the same link");
            }
            read = rows;
        }
        read
    }

    #[test]
    fn a_message_steered_into_the_turn_names_it_from_its_first_revision() {
        // Issue 845: the row alone tells a steer from a message that waits.
        let mut recorder = open();
        let turn = start(&mut recorder);
        say(&mut recorder, turn, "r-look", &["t-1"], &[]);
        let before = recorder.conversation_seq() * REVISION_STEP;
        read_in(&mut recorder, turn, "u-more");
        // In the write that read it: the turn's id as the live view names it (no trace here).
        let ask = ("u-ask".to_owned(), None);
        // Steered in as the turn ran.
        let more = (
            "u-more".to_owned(),
            Some(("turn-ask".to_owned(), turn.0, "steer")),
        );
        assert_eq!(read_into(&recorder), [ask.clone(), more.clone()]);
        // A delta that brings the row brings the link with it.
        let (given, _) = {
            let page = after(
                recorder.log(),
                before,
                &[],
                span(&recorder),
                100,
                Which::chat(true),
            )
            .unwrap();
            (page.entries, page.latest_revision)
        };
        let row = given.iter().find(|row| row.entry.id() == "u-more").unwrap();
        let into = row.read_into.as_ref().unwrap();
        assert_eq!((into.trace_id.as_str(), into.turn), ("turn-ask", turn.0));
        // A message that waits and then starts a turn of its own names none.
        let next = accept(&mut recorder, "u-next", 350);
        end(&mut recorder, turn, "r-look");
        recorder
            .handle(Command::TurnStarted { turn: next }, 410)
            .unwrap();
        assert_eq!(
            read_into(&recorder),
            [ask, more, ("u-next".to_owned(), None)]
        );
    }

    #[test]
    fn a_message_read_into_a_turn_later_replaced_keeps_its_link_and_its_revision() {
        // The message was read into the turn: a newer message's turn replacing it later
        // changes neither what its row says nor the row's revision.
        let mut recorder = open();
        let turn = start(&mut recorder);
        read_in(&mut recorder, turn, "u-more");
        let row = |recorder: &Recorder| {
            tail(
                recorder.log(),
                &[],
                span(recorder),
                100,
                super::super::Which::All,
            )
            .unwrap()
            .entries
            .into_iter()
            .find(|row| row.entry.id() == "u-more")
            .unwrap()
        };
        let before = row(&recorder);
        let into = before.read_into.clone().unwrap();
        assert_eq!((into.trace_id.as_str(), into.turn), ("turn-ask", turn.0));
        let mut batch = recorder.batch();
        batch
            .command(
                Command::TurnEnded {
                    turn,
                    outcome: RunOutcome::Completed,
                },
                400,
            )
            .unwrap();
        let mut replaced = TurnEnd::of(batch.agent().last().unwrap(), "u-ask").unwrap();
        replaced.status = "replaced".into();
        batch.item(Entry::TurnEnd(replaced).to_item(400).unwrap());
        batch.commit().unwrap();
        for after in [row(&recorder), {
            let which = Which::chat(true);
            let page = tail(recorder.log(), &[], span(&recorder), 100, which).unwrap();
            page.entries
                .into_iter()
                .find(|row| row.entry.id() == "u-more")
                .unwrap()
        }] {
            assert_eq!(after.read_into, before.read_into);
            assert_eq!(after.revision, before.revision);
        }
    }

    #[test]
    fn messages_read_with_the_turns_own_at_its_start_name_the_turn_by_its_trace() {
        let mut recorder = open();
        let running = start(&mut recorder);
        let traced = |recorder: &mut Recorder, id: &str, trace: &str, now| {
            let message = Message {
                id: id.into(),
                text: id.into(),
                origin: Origin::User,
                trace_id: Some(trace.into()),
                ..Message::default()
            };
            recorder
                .accept_message(
                    Incoming {
                        key: Some(message.id.clone()),
                        body: serde_json::to_vec(&message).unwrap(),
                        origin: Origin::User,
                        admission: Admission::Queue,
                        coordination: None,
                        user_word: false,
                    },
                    now,
                )
                .unwrap()
                .turn
                .unwrap()
        };
        let first = traced(&mut recorder, "u-first", "tr-first", 200);
        let second = traced(&mut recorder, "u-second", "tr-second", 210);
        say(&mut recorder, running, "r-done", &[], &[]);
        end(&mut recorder, running, "r-done");
        // The turn of the first starts and takes the second into its prompt, as the actor
        // gathers it: reserved and read in the same write.
        let mut batch = recorder.batch();
        let join = Join {
            into: first,
            uuid: "together-second".into(),
            recovery: None,
            together: true,
        };
        batch
            .command(Command::Join { turn: second, join }, 420)
            .unwrap();
        batch
            .command(Command::JoinRead { turn: second }, 420)
            .unwrap();
        batch.commit().unwrap();
        recorder
            .handle(Command::TurnStarted { turn: first }, 430)
            .unwrap();
        assert_eq!(
            read_into(&recorder),
            [
                ("u-ask".to_owned(), None),
                ("u-first".to_owned(), None),
                (
                    "u-second".to_owned(),
                    Some(("tr-first".to_owned(), first.0, "start"))
                ),
            ]
        );
    }
}
