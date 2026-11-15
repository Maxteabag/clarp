//! A conversation as a client reads it: pages of the newest version of each thing.
//!
//! Every thing in a conversation has a revision, and a thing that changes gets a higher one. A
//! client keeps the highest revision it has seen and asks for what is newer. Revisions come from
//! the log: an item at position `seq` has revision `seq * REVISION_STEP`. A reply that is still
//! being written is not in the log; it takes the revisions in between, one for each change, and
//! the item that finally stores it is above them all.
//!
//! How a turn ended is stored once, when it ends, and belongs to the row that closed the turn.
//! That row changed when the turn ended, so it carries the end's revision from then on, and a
//! client that asks what is newer is given the row again.

use clarp_kernel::conversation::{Entry, Reply, TurnEnd};
use clarp_kernel::journal::kind;
use clarp_ports::log::{AgentLog, Item, Seq};

use crate::ActorError;

/// The room between the revisions of two neighbouring log items, for what changes without
/// being stored.
pub const REVISION_STEP: u64 = 1 << 20;

/// A thing of a conversation as it is now.
#[derive(Debug, Clone, PartialEq)]
pub struct Versioned {
    pub entry: Entry,
    pub revision: u64,
    pub ts_ms: i64,
    /// Still being written: it will change again, and what stores it will have the same id.
    pub live: bool,
    /// The turn this thing belongs to.
    pub turn_number: Option<u64>,
    /// How that turn ended, when this is the row that closed it.
    pub turn: Option<TurnEnd>,
}

/// A page of a conversation.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct Page {
    pub entries: Vec<Versioned>,
    /// The cursor for the next question: nothing in this page is newer than this, and when
    /// `has_more` is set, the things after it are still to be asked for.
    pub latest_revision: u64,
    /// For the newest page and for older history: there is more before the first entry. For
    /// "what is newer": there is more after the last.
    pub has_more: bool,
    /// The newest revision the conversation has at all.
    pub head_revision: u64,
}

/// A reply that is being written.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct Draft {
    pub id: String,
    pub turn: u64,
    pub text: String,
    pub revision: u64,
    pub ts_ms: i64,
}

impl Draft {
    fn versioned(&self) -> Versioned {
        Versioned {
            entry: Entry::Reply(Reply {
                id: self.id.clone(),
                turn: self.turn,
                text: self.text.clone(),
                trace_id: None,
            }),
            revision: self.revision,
            ts_ms: self.ts_ms,
            live: true,
            turn_number: Some(self.turn),
            turn: None,
        }
    }
}

/// Where a thing that has several versions in a page stands in it.
#[derive(Clone, Copy, PartialEq, Eq)]
enum Place {
    /// Where it first appeared: the order in which a chat shows things.
    First,
    /// Where its newest version is: the order in which changes happened.
    Newest,
}

fn corrupt(item: &Item, error: serde_json::Error) -> ActorError {
    ActorError::Corrupt(format!(
        "log item {} is not what its kind says: {error}",
        item.seq
    ))
}

fn row(item: &Item, entry: Entry) -> Versioned {
    Versioned {
        turn_number: item.turn.or(entry.turn()),
        entry,
        revision: item.seq * REVISION_STEP,
        ts_ms: item.ts_ms,
        live: false,
        turn: None,
    }
}

/// Gives a row the end of the turn it closed. The row changed when the turn ended.
fn close(row: &mut Versioned, end: TurnEnd, end_revision: u64) {
    row.revision = row.revision.max(end_revision);
    row.turn = Some(end);
}

/// The rows among these items, which are oldest first: the newest version of each thing, with
/// the turns' ends on the rows that closed them.
fn rows(log: &dyn AgentLog, items: &[Item], place: Place) -> Result<Vec<Versioned>, ActorError> {
    let mut found: Vec<Versioned> = Vec::with_capacity(items.len());
    for item in items {
        let Some(entry) = Entry::from_item(item).map_err(|error| corrupt(item, error))? else {
            continue;
        };
        let end = match entry {
            Entry::TurnEnd(end) => end,
            entry => {
                let version = row(item, entry);
                match found
                    .iter()
                    .position(|known| known.entry.id() == version.entry.id())
                {
                    Some(at) if place == Place::First => {
                        // It keeps the place and the time of its first appearance, and what
                        // it was told about its turn.
                        let (ts_ms, turn) = (found[at].ts_ms, found[at].turn.take());
                        found[at] = Versioned {
                            ts_ms,
                            turn,
                            ..version
                        };
                    }
                    Some(at) => {
                        let turn = found.remove(at).turn;
                        found.push(Versioned { turn, ..version });
                    }
                    None => found.push(version),
                }
                continue;
            }
        };
        let end_revision = item.seq * REVISION_STEP;
        match found
            .iter()
            .position(|known| known.entry.id() == end.closes)
        {
            Some(at) if place == Place::First => close(&mut found[at], end, end_revision),
            Some(at) => {
                // The row changed now: among changes, this is where it stands.
                let mut closed = found.remove(at);
                close(&mut closed, end, end_revision);
                found.push(closed);
            }
            // The row is older than this page. Someone who asks what is newer needs it again,
            // because it changed; a page of a chat simply does not reach back to it.
            None if place == Place::Newest => {
                if let Some(stored) = log.find(&end.closes)? {
                    let entry =
                        Entry::from_item(&stored).map_err(|error| corrupt(&stored, error))?;
                    if let Some(entry) = entry {
                        let mut closed = row(&stored, entry);
                        close(&mut closed, end, end_revision);
                        found.push(closed);
                    }
                }
            }
            None => {}
        }
    }
    // A turn whose end lies after this page: its closing row is here, the end is not.
    let mut asked: Vec<u64> = Vec::new();
    for at in (0..found.len()).rev() {
        let Some(turn) = found[at].turn_number else {
            continue;
        };
        if asked.contains(&turn) {
            continue;
        }
        asked.push(turn);
        if found
            .iter()
            .any(|known| known.turn.as_ref().is_some_and(|end| end.turn == turn))
        {
            continue;
        }
        let Some(stored) = log.find(&format!("t-{turn}"))? else {
            continue;
        };
        let Some(Entry::TurnEnd(end)) =
            Entry::from_item(&stored).map_err(|error| corrupt(&stored, error))?
        else {
            continue;
        };
        if let Some(closing) = found
            .iter_mut()
            .find(|known| known.entry.id() == end.closes)
        {
            close(closing, end, stored.seq * REVISION_STEP);
        }
    }
    Ok(found)
}

/// The row with this id as it is now, with its turn's end if it closed one.
pub(crate) fn one(log: &dyn AgentLog, id: &str) -> Result<Option<Versioned>, ActorError> {
    let Some(stored) = log.find(id)? else {
        return Ok(None);
    };
    if !kind::ROWS.has(stored.kind) {
        return Ok(None); // a message that still waits outside the conversation
    }
    Ok(rows(log, &[stored], Place::First)?.into_iter().next())
}

/// The newest `limit` things, in the order a chat shows them, with the replies being written.
pub(crate) fn tail(
    log: &dyn AgentLog,
    drafts: &[Draft],
    head_revision: u64,
    limit: usize,
) -> Result<Page, ActorError> {
    older(log, Seq::MAX, drafts, head_revision, limit)
}

/// The `limit` things before position `before`, in the order a chat shows them.
pub(crate) fn older(
    log: &dyn AgentLog,
    before: Seq,
    drafts: &[Draft],
    head_revision: u64,
    limit: usize,
) -> Result<Page, ActorError> {
    // A page is counted in rows. How the turns ended is looked up for the rows that are in it.
    let mut items = log.read_before(before, kind::ROWS, limit.saturating_add(1))?;
    let has_more = items.len() > limit;
    if has_more {
        items.remove(0);
    }
    let mut entries = rows(log, &items, Place::First)?;
    // A reply being written is shown where its finished version will be: at the end.
    for draft in drafts {
        if !entries.iter().any(|known| known.entry.id() == draft.id) {
            entries.push(draft.versioned());
        }
    }
    Ok(Page {
        entries,
        latest_revision: head_revision,
        has_more,
        head_revision,
    })
}

/// Up to `limit` things that are newer than `revision`, in the order they changed.
pub(crate) fn after(
    log: &dyn AgentLog,
    revision: u64,
    drafts: &[Draft],
    head_revision: u64,
    limit: usize,
) -> Result<Page, ActorError> {
    let mut items = log.read_after(
        revision / REVISION_STEP,
        kind::CONVERSATION,
        limit.saturating_add(1),
    )?;
    let has_more = items.len() > limit;
    items.truncate(limit);
    let cursor = items.last().map(|last| last.seq * REVISION_STEP);
    let mut entries = rows(log, &items, Place::Newest)?;
    let latest_revision = if has_more {
        // There is more in the log than this page: the cursor stops at what was read.
        cursor.unwrap_or(revision)
    } else {
        for draft in drafts {
            if draft.revision > revision {
                entries.push(draft.versioned());
            }
        }
        head_revision.max(revision)
    };
    Ok(Page {
        entries,
        latest_revision,
        has_more,
        head_revision,
    })
}
