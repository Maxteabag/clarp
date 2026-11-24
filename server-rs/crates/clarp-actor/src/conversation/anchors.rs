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
