//! The anchors of a turn: the rows the conversation view shows in place of its work.
//!
//! The conversation view only grows: a row that has entered it never leaves it. A reply with
//! words enters once what it is can no longer change: when the next thing the turn does is not
//! work (another reply, or a message of the user's read into the turn), or when the turn ends.
//! Words that work follows were said along the way and never enter. Each anchor keeps where it
//! entered, which is its revision from then on, so a reader that asks what is newer is given it.

use std::cell::{Cell, RefCell};
use std::collections::{BTreeMap, HashMap, HashSet};

use clarp_kernel::conversation::Entry;
use clarp_kernel::journal::kind;
use clarp_ports::log::{AgentLog, Item, Seq};

use super::{CHUNK, corrupt, is_conversation, joined_read};
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
    /// Where the last anchor stands: what is older is not since it.
    last_anchor: Seq,
    /// The newest reply with words since the last anchor.
    words: Option<(Seq, String)>,
    /// The newest row since the last anchor that is conversation by itself: the turn's message
    /// or a note.
    fallback: Option<(Seq, String)>,
}

impl Walk {
    /// A walk from the turn's own message, read back from `from` (the turn's end, or the log's
    /// head), and over everything after it up to there.
    fn start(log: &dyn AgentLog, turn: u64, from: Seq) -> Result<(Self, usize), ActorError> {
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
        let mut walk = Self::default();
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
                self.decide(item.seq, entry.id());
            }
            return Ok(());
        }
        let Some(entry) = Entry::from_item(item).map_err(|why| corrupt(item, why))? else {
            return Ok(());
        };
        let id = entry.id();
        if clarp_kernel::artifacts::forms::is_answer_row(id) || !self.seen.insert(id.to_owned()) {
            return Ok(());
        }
        if is_conversation(&entry, None) {
            if matches!(entry, Entry::Message(_) | Entry::Note(_)) {
                self.fallback = Some((item.seq, id.to_owned()));
            }
            return Ok(());
        }
        self.rows += 1;
        match &entry {
            Entry::Reply(said) if !said.text.trim().is_empty() => {
                if said.tools.is_empty() {
                    // Words after words: those before were an answer.
                    self.decide(item.seq, id);
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
            Entry::Tool(_) => self.pending = None,
            _ => {}
        }
        Ok(())
    }

    /// The row at `at` (`by`) decides that the words waiting for it are an answer.
    fn decide(&mut self, at: Seq, by: &str) {
        let Some(pending) = self.pending.take() else {
            return;
        };
        self.decided.anchors.push(Anchor {
            id: pending.id.clone(),
            session_rows: pending.rows - 1,
            entered: at,
        });
        self.decided.decides.insert(by.to_owned(), pending.id);
        self.rows -= pending.rows;
        self.last_anchor = pending.seq;
        self.words = None;
        if self.fallback.as_ref().is_some_and(|(seq, _)| *seq < pending.seq) {
            self.fallback = None;
        }
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

/// The turn a message was read into, when it is one: the turn of the newest row before it that
/// is neither of its own turn nor another message read in.
pub(super) fn read_into_turn(log: &dyn AgentLog, item: &Item) -> Result<Option<u64>, ActorError> {
    if !read_into(log, item)? {
        return Ok(None);
    }
    let mut before = item.seq;
    loop {
        let items = log.read_before(before, kind::ROWS, 16)?;
        let Some(oldest) = items.first() else {
            return Ok(None);
        };
        before = oldest.seq;
        for row in items.iter().rev() {
            if row.turn.is_some() && row.turn != item.turn && !read_into(log, row)? {
                return Ok(row.turn);
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
) -> Result<TurnAnchors, ActorError> {
    let (walk, _) = Walk::start(log, turn, end.unwrap_or(Seq::MAX))?;
    Ok(walk.anchors(end))
}

/// How many turns' walks the actor keeps.
const KEPT: usize = 32;

/// The walks of the newest turns, kept by the actor between reads: a read of a running turn
/// walks only what was written since the last one.
#[derive(Default)]
pub(crate) struct AnchorCache {
    walks: RefCell<BTreeMap<u64, Walk>>,
    /// How many log items its walks have read, for a test to see that a read costs what is new.
    walked: Cell<usize>,
}

impl AnchorCache {
    pub(super) fn anchors(
        &self,
        log: &dyn AgentLog,
        turn: u64,
        end: Option<Seq>,
    ) -> Result<TurnAnchors, ActorError> {
        let mut walks = self.walks.borrow_mut();
        let walk = match walks.get_mut(&turn) {
            Some(walk) => walk,
            None => {
                let (walk, walked) = Walk::start(log, turn, end.unwrap_or(Seq::MAX))?;
                self.walked.set(self.walked.get() + walked);
                while walks.len() >= KEPT {
                    walks.pop_first();
                }
                walks.entry(turn).or_insert(walk)
            }
        };
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
        recorder
            .handle(Command::TurnStarted { turn }, 110)
            .unwrap();
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
        call(&mut recorder, turn, "t-1", &["r-look"]);
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
                ("t-1".to_owned(), None),
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
            say(&mut recorder, turn, &format!("r-{at}"), &[&format!("t-{at}")], &[]);
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
        let mut steps: Vec<Box<dyn Fn(&mut Recorder)>> = vec![
            Box::new(move |r| say(r, turn, "r-a", &[], &[])),
            Box::new(move |r| say(r, turn, "r-b", &[], &[])),
            Box::new(move |r| call(r, turn, "t-1", &["r-b"])),
            Box::new(move |r| say(r, turn, "r-c", &["t-2"], &[])),
            Box::new(move |r| say(r, turn, "r-d", &[], &[])),
            Box::new(move |r| read_in(r, turn, "u-more")),
            Box::new(move |r| say(r, turn, "r-e", &[], &[])),
            Box::new(move |r| call(r, turn, "t-3", &["r-e"])),
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
}
