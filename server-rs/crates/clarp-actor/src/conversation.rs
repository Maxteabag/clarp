//! A conversation as a client reads it: pages of the newest version of each thing.
//!
//! Every thing in a conversation has a revision, and a thing that changes gets a higher one. A
//! client keeps the highest revision it has seen and asks for what is newer. Revisions come from
//! the log: an item at position `seq` has revision `seq * REVISION_STEP`. A reply that is still
//! being written is not in the log; it takes the revisions in between, one for each change, and
//! the item that finally stores it is above them all.

use clarp_kernel::conversation::{Entry, Reply};
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
            }),
            revision: self.revision,
            ts_ms: self.ts_ms,
            live: true,
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

/// The newest version of each thing among these items, which are oldest first.
fn newest_versions(items: &[Item], place: Place) -> Result<Vec<Versioned>, ActorError> {
    let mut found: Vec<(String, Versioned)> = Vec::with_capacity(items.len());
    for item in items {
        let Some(entry) = Entry::from_item(item).map_err(|error| corrupt(item, error))? else {
            continue;
        };
        let id = entry.id().to_string();
        let version = Versioned {
            entry,
            revision: item.seq * REVISION_STEP,
            ts_ms: item.ts_ms,
            live: false,
        };
        match found.iter().position(|(known, _)| *known == id) {
            Some(at) if place == Place::First => {
                // It keeps the place and the time of its first appearance.
                let first_ts = found[at].1.ts_ms;
                found[at].1 = Versioned {
                    ts_ms: first_ts,
                    ..version
                };
            }
            Some(at) => {
                found.remove(at);
                found.push((id, version));
            }
            None => found.push((id, version)),
        }
    }
    Ok(found.into_iter().map(|(_, version)| version).collect())
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
    let mut items = log.read_before(before, kind::CONVERSATION, limit.saturating_add(1))?;
    let has_more = items.len() > limit;
    if has_more {
        items.remove(0);
    }
    let mut entries = newest_versions(&items, Place::First)?;
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
    let mut entries = newest_versions(&items, Place::Newest)?;
    let latest_revision = if has_more {
        // There is more in the log than this page: the cursor stops at what was given.
        entries.last().map_or(revision, |last| last.revision)
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
