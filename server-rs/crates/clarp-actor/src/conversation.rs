//! A conversation as a client reads it: pages of the newest version of each thing.
//!
//! Every thing in a conversation has a revision, and a thing that changes gets a higher one. A
//! client keeps the highest revision it has seen and asks for what is newer. Revisions come from
//! the log: an item at position `seq` has revision `seq * REVISION_STEP`. A reply that is still
//! being written is not in the log; it takes the revisions in between, one for each change, and
//! the item that finally stores it is above them all.
//!
//! A row stands where its own item is, which is written once. What happens to it afterwards
//! is stored as parts that name it: the uses of the tools its message calls, that tool work
//! came after it, how its turn ended when it is the row that closed the turn. A row is read
//! as it is now, with its parts, and carries the revision of the newest of them: it changed
//! when that part was written, and a client that asks what is newer is given the row again.

use std::collections::HashMap;

use clarp_kernel::conversation::{Commentary, Entry, Reply, ToolCall, TurnEnd};
use clarp_kernel::agent::AgentEvent;
use clarp_kernel::journal::{coordination_key, kind};
use clarp_kernel::turn::{Origin, TurnEvent, left_out_of_chats};
use clarp_ports::log::{AgentLog, Item, Seq};

use crate::ActorError;

/// The room between the revisions of two neighbouring log items, for what changes without
/// being stored.
pub const REVISION_STEP: u64 = 1 << 20;

/// Which things of a conversation a reader asks for.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum Which {
    #[default]
    All,
    /// Without what a watcher or the Host's own routine began. The apps read a chat this way:
    /// a page is counted in what is left, so a busy heartbeat cannot fill one and make a
    /// chat look as if it ended days ago.
    WithoutAutomated,
}

impl Which {
    fn keeps(self, entry: &Entry) -> bool {
        self == Self::All || !left_out_of_chats(entry.origin_name())
    }

    /// Whether a stored item is one this reader gets. An item that is no thing of a
    /// conversation is passed on: what reads the items leaves it out itself.
    fn keeps_item(self, item: &Item) -> Result<bool, ActorError> {
        if self == Self::All {
            return Ok(true);
        }
        let entry = Entry::from_item(item).map_err(|error| corrupt(item, error))?;
        Ok(entry.is_none_or(|entry| self.keeps(&entry)))
    }
}

/// How many items are read at a time when some of them are left out.
const CHUNK: usize = 256;

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
    /// The uses of the tools a reply's message calls, as they are now, in its order.
    pub tools: Vec<ToolCall>,
    /// Tool work came after this reply in the same turn: it was said along the way.
    pub commentary: bool,
    /// The turn it belongs to answers another agent: that agent's id, empty for an agent of a
    /// peer Host. `None`: the user's turn, or no turn.
    pub coordination: Option<String>,
}

impl Versioned {
    /// A thing by itself: no part of it is known, and no turn's end.
    pub fn of(entry: Entry, revision: u64, ts_ms: i64) -> Self {
        Self {
            turn_number: entry.turn(),
            entry,
            revision,
            ts_ms,
            live: false,
            turn: None,
            tools: Vec::new(),
            commentary: false,
            coordination: None,
        }
    }
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
    /// The revision the conversation begins after: 0, or where the agent was last started
    /// again. A reader that holds a revision up to this one holds a conversation that ended.
    pub begins_after: u64,
}

impl Page {
    /// Whether a reader that holds the revision `held` (the cursor an earlier page gave it)
    /// holds a conversation that has ended: a revision this conversation never reached, above
    /// its head, or one at or below where it begins, since the agent was started again after
    /// it. Such a reader is told to drop what it has and read the conversation anew: a page of
    /// changes cannot say that a row is gone. A reader that holds nothing (0) asks for the
    /// newest page, and is not asked this.
    pub fn ended_for(&self, held: u64) -> bool {
        held > self.head_revision || held <= self.begins_after
    }
}

/// A reply that is being written.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct Draft {
    pub id: String,
    pub turn: u64,
    pub text: String,
    pub revision: u64,
    pub ts_ms: i64,
    /// Who asked for the turn that writes it.
    pub origin: Origin,
}

impl Draft {
    fn versioned(&self) -> Versioned {
        let reply = Entry::Reply(Reply {
            id: self.id.clone(),
            turn: self.turn,
            text: self.text.clone(),
            trace_id: None,
            origin: self.origin.clone(),
            tools: Vec::new(),
        });
        Versioned {
            live: true,
            ..Versioned::of(reply, self.revision, self.ts_ms)
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

/// Whether a turn answers another agent: its acceptance, found by its key when it was marked
/// so. One look by key, whatever the size of the conversation.
pub fn mark_of(log: &dyn AgentLog, turn: u64) -> Result<Option<String>, ActorError> {
    let Some(stored) = log.find(&coordination_key(turn))? else {
        return Ok(None);
    };
    let event: AgentEvent = serde_json::from_slice(&stored.body).map_err(|error| corrupt(&stored, error))?;
    Ok(match event {
        AgentEvent::Turn {
            change: TurnEvent::Accepted { coordination, .. },
            ..
        } => coordination,
        _ => None,
    })
}

/// Reads rows as they are now: each with the parts that name it.
struct Reader<'a> {
    log: &'a dyn AgentLog,
    /// How the turns asked about ended, and where in the log that is said.
    ends: HashMap<u64, Option<(TurnEnd, Seq)>>,
    /// Whether the turns asked about answer another agent ([`mark_of`]).
    marks: HashMap<u64, Option<String>>,
}

impl<'a> Reader<'a> {
    fn new(log: &'a dyn AgentLog) -> Self {
        Self {
            log,
            ends: HashMap::new(),
            marks: HashMap::new(),
        }
    }

    /// Whether this turn answers another agent: looked up once per turn.
    fn mark(&mut self, turn: u64) -> Result<Option<String>, ActorError> {
        if let Some(known) = self.marks.get(&turn) {
            return Ok(known.clone());
        }
        let found = mark_of(self.log, turn)?;
        self.marks.insert(turn, found.clone());
        Ok(found)
    }

    /// How this turn ended, when it has.
    fn end(&mut self, turn: u64) -> Result<Option<&(TurnEnd, Seq)>, ActorError> {
        if !self.ends.contains_key(&turn) {
            let mut found = None;
            if let Some(stored) = self.log.find(&format!("t-{turn}"))?
                && let Some(Entry::TurnEnd(end)) =
                    Entry::from_item(&stored).map_err(|error| corrupt(&stored, error))?
            {
                found = Some((end, stored.seq));
            }
            self.ends.insert(turn, found);
        }
        Ok(self.ends.get(&turn).and_then(Option::as_ref))
    }

    /// The row this item is, as it is now.
    fn row(&mut self, item: &Item, entry: Entry) -> Result<Versioned, ActorError> {
        let mut row = Versioned {
            turn_number: item.turn.or(entry.turn()),
            ..Versioned::of(entry, item.seq * REVISION_STEP, item.ts_ms)
        };
        let mut newest = item.seq;
        if let Entry::Imported(import) = &mut row.entry
            && import.row["display_cells"]
                .as_array()
                .is_some_and(|cells| cells.iter().any(|c| c["ephemeral"] == true))
            && let Some((seq, cells)) =
                super::helper_cells::current(self.log, &import.id, item.seq)?
        {
            import.row["display_cells"] = cells;
            newest = newest.max(seq);
        }
        if let Entry::Tool(call) = &mut row.entry
            && call
                .cell
                .as_ref()
                .is_some_and(|cell| cell["ephemeral"] == true)
            && let Some((seq, cell)) = super::helper_cells::current(self.log, &call.id, item.seq)?
        {
            call.cell = Some(cell);
            newest = newest.max(seq);
        }
        if let Entry::Reply(reply) = &row.entry {
            for id in &reply.tools {
                let Some(stored) = self.log.find(id)? else {
                    continue; // told of and never written: the turn was cut off there
                };
                if let Some(Entry::Tool(mut call)) =
                    Entry::from_item(&stored).map_err(|error| corrupt(&stored, error))?
                {
                    newest = newest.max(stored.seq);
                    if call
                        .cell
                        .as_ref()
                        .is_some_and(|cell| cell["ephemeral"] == true)
                        && let Some((seq, cell)) =
                            super::helper_cells::current(self.log, &call.id, stored.seq)?
                    {
                        call.cell = Some(cell);
                        newest = newest.max(seq);
                    }
                    row.tools.push(call);
                }
            }
            if !reply.text.trim().is_empty()
                && let Some(stored) = self.log.find(&Commentary::id_of(&reply.id))?
            {
                newest = newest.max(stored.seq);
                row.commentary = true;
            }
        }
        if let Some(turn) = row.turn_number {
            row.coordination = self.mark(turn)?;
        }
        if let Some(turn) = row.turn_number
            && let Some((end, seq)) = self.end(turn)?
            && end.closes == row.entry.id()
        {
            newest = newest.max(*seq);
            row.turn = Some(end.clone());
        }
        row.revision = newest * REVISION_STEP;
        Ok(row)
    }

    /// The row with this id as it is now, when this reader gets it. `None` for what is no
    /// row: a part of one, a message that still waits outside the conversation.
    fn stored(&mut self, id: &str, which: Which) -> Result<Option<Versioned>, ActorError> {
        let Some(stored) = self.log.find(id)? else {
            return Ok(None);
        };
        if !kind::ROWS.has(stored.kind) {
            return Ok(None);
        }
        let entry = Entry::from_item(&stored).map_err(|error| corrupt(&stored, error))?;
        match entry.filter(|entry| which.keeps(entry)) {
            Some(entry) => self.row(&stored, entry).map(Some),
            None => Ok(None),
        }
    }
}

/// The rows these items are about, each once and as it is now. The items are oldest first.
/// An item that is a row puts it where it stands. An item that is a part of a row says that
/// the row changed: among changes ([`Place::Newest`]) the row stands where its newest part
/// is; in a chat ([`Place::First`]) it stays where it is, and a part of a row that is not on
/// the page says nothing.
fn rows(
    log: &dyn AgentLog,
    items: &[Item],
    place: Place,
    which: Which,
) -> Result<Vec<Versioned>, ActorError> {
    let mut reader = Reader::new(log);
    let mut found: Vec<Versioned> = Vec::with_capacity(items.len());
    for item in items {
        if let Some(id) = super::helper_cells::parent(item)? {
            if place == Place::Newest {
                found.retain(|r| r.entry.id() != id);
                found.extend(reader.stored(&id, which)?);
            }
            continue;
        }
        let Some(entry) = Entry::from_item(item).map_err(|error| corrupt(item, error))? else {
            continue;
        };
        if let Some(changed) = entry.part_of() {
            if place == Place::Newest {
                found.retain(|known| known.entry.id() != changed);
                found.extend(reader.stored(changed, which)?);
            }
            continue;
        }
        if !which.keeps(&entry) {
            continue;
        }
        let row = reader.row(item, entry)?;
        match found
            .iter()
            .position(|known| known.entry.id() == row.entry.id())
        {
            // A call that is a row by itself was written again when it ended. In a chat it
            // keeps the place and the time of its first appearance.
            Some(at) if place == Place::First => {
                let ts_ms = found[at].ts_ms;
                found[at] = Versioned { ts_ms, ..row };
            }
            Some(at) => {
                found.remove(at);
                found.push(row);
            }
            None => found.push(row),
        }
    }
    Ok(found)
}

/// The row with this id as it is now, with its turn's end if it closed one.
pub(crate) fn one(log: &dyn AgentLog, id: &str) -> Result<Option<Versioned>, ActorError> {
    Reader::new(log).stored(id, Which::All)
}

/// Where a conversation begins and how far it has got: what was written up to `floor` is
/// of a conversation that ended when the agent was started again, and is no part of what a
/// chat shows.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub(crate) struct Span {
    pub floor: Seq,
    pub head_revision: u64,
}

/// The newest `limit` things, in the order a chat shows them, with the replies being written.
pub(crate) fn tail(
    log: &dyn AgentLog,
    drafts: &[Draft],
    span: Span,
    limit: usize,
    which: Which,
) -> Result<Page, ActorError> {
    older(log, Seq::MAX, drafts, span, limit, which)
}

/// The newest `limit` items of rows before `before` and after `floor` that this reader
/// gets, oldest first, and whether there are more before them.
fn items_before(
    log: &dyn AgentLog,
    (before, floor): (Seq, Seq),
    limit: usize,
    which: Which,
) -> Result<(Vec<Item>, bool), ActorError> {
    if which == Which::All {
        let mut items = log.read_before(before, kind::ROWS, limit.saturating_add(1))?;
        items.retain(|item| item.seq > floor);
        let has_more = items.len() > limit;
        if has_more {
            items.remove(0);
        }
        return Ok((items, has_more));
    }
    // Some are left out: read back until the page is full, or there is nothing older.
    let mut kept: Vec<Item> = Vec::new(); // the newest first
    let mut cursor = before;
    loop {
        let chunk = log.read_before(cursor, kind::ROWS, CHUNK)?;
        let Some(oldest) = chunk.first() else {
            break;
        };
        cursor = oldest.seq;
        // The conversation begins after the floor: nothing older is read.
        let whole = chunk.len() == CHUNK && oldest.seq > floor;
        for item in chunk.into_iter().rev() {
            if kept.len() <= limit && item.seq > floor && which.keeps_item(&item)? {
                kept.push(item);
            }
        }
        if kept.len() > limit || !whole {
            break;
        }
    }
    let has_more = kept.len() > limit;
    kept.truncate(limit);
    kept.reverse();
    Ok((kept, has_more))
}

/// The `limit` things before position `before`, in the order a chat shows them.
pub(crate) fn older(
    log: &dyn AgentLog,
    before: Seq,
    drafts: &[Draft],
    span: Span,
    limit: usize,
    which: Which,
) -> Result<Page, ActorError> {
    let head_revision = span.head_revision;
    // A page is counted in rows. How the turns ended is looked up for the rows that are in it.
    let (items, has_more) = items_before(log, (before, span.floor), limit, which)?;
    let mut entries = rows(log, &items, Place::First, which)?;
    // A reply being written is shown where its finished version will be: at the end.
    for draft in drafts {
        let mut shown = draft.versioned();
        if which.keeps(&shown.entry) && !entries.iter().any(|known| known.entry.id() == draft.id) {
            shown.coordination = mark_of(log, draft.turn)?;
            entries.push(shown);
        }
    }
    Ok(Page {
        entries,
        latest_revision: head_revision,
        has_more,
        head_revision,
        begins_after: span.floor * REVISION_STEP,
    })
}

/// The revision the conversation had at a time: that of the newest thing of it that was
/// written at or before `ts_ms`, and where the conversation begins when everything in it is
/// newer. What is newer than this revision ([`after`]) is what was written after that time.
/// A log is in the order it was written, so the times in it do not go back, and the place
/// is searched for and not read up to: a few items, however long the conversation.
pub(crate) fn revision_at(log: &dyn AgentLog, span: Span, ts_ms: i64) -> Result<u64, ActorError> {
    // Everything up to `low` was written by then; everything from `high` on, after.
    let (mut low, mut high) = (span.floor, log.last_seq()?.saturating_add(1));
    while high - low > 1 {
        let middle = low + (high - low) / 2;
        match log.read_after(middle - 1, kind::CONVERSATION, 1)?.first() {
            Some(item) if item.seq < high && item.ts_ms <= ts_ms => low = item.seq,
            _ => high = middle,
        }
    }
    Ok(low * REVISION_STEP)
}

/// The items after position `from` that a page of up to `limit` things reads, oldest first;
/// whether there are more after them; and the cursor where reading stopped. What this reader
/// does not get is passed over, and the cursor passes over it too: it is not read again.
fn items_after(
    log: &dyn AgentLog,
    mut from: Seq,
    limit: usize,
    which: Which,
) -> Result<(Vec<Item>, bool, Option<u64>), ActorError> {
    let mut items: Vec<Item> = Vec::new();
    let mut cursor = None;
    let at_a_time = if which == Which::All {
        limit.saturating_add(1)
    } else {
        CHUNK.max(limit.saturating_add(1))
    };
    loop {
        let chunk = log.read_after(from, kind::CONVERSATION, at_a_time)?;
        let whole = chunk.len() == at_a_time;
        for item in chunk {
            from = item.seq;
            if which.keeps_item(&item)? {
                if items.len() == limit {
                    return Ok((items, true, cursor));
                }
                items.push(item);
            }
            cursor = Some(from * REVISION_STEP);
        }
        if !whole {
            return Ok((items, false, cursor));
        }
    }
}

/// Up to `limit` things that are newer than `revision`, in the order they changed.
pub(crate) fn after(
    log: &dyn AgentLog,
    revision: u64,
    drafts: &[Draft],
    span: Span,
    limit: usize,
    which: Which,
) -> Result<Page, ActorError> {
    let head_revision = span.head_revision;
    let mut from = (revision / REVISION_STEP).max(span.floor);
    let (mut entries, latest_revision, has_more) = loop {
        let (items, has_more, cursor) = items_after(log, from, limit, which)?;
        let mut entries = rows(log, &items, Place::Newest, which)?;
        if !has_more {
            for draft in drafts {
                let mut shown = draft.versioned();
                if draft.revision > revision && which.keeps(&shown.entry) {
                    shown.coordination = mark_of(log, draft.turn)?;
                    entries.push(shown);
                }
            }
            break (entries, head_revision.max(revision), false);
        }
        // There is more in the log than this page: the cursor stops at what was read. A row
        // is read as it is now; one whose newest part lies beyond what this page read is
        // left out of it, and comes once, on the page that reaches that part. So no row of a
        // page is newer than its cursor, and whoever takes a row's revision for a cursor
        // passes over nothing.
        let cursor = cursor.unwrap_or(revision);
        entries.retain(|row| row.revision <= cursor);
        // A page that says there is more is never empty: a reader could take it for the end.
        // Where every row it read was left for a later page (the parts of one long reply),
        // it reads on from its cursor, as far as the head at the most.
        if entries.is_empty() && cursor / REVISION_STEP > from {
            from = cursor / REVISION_STEP;
            continue;
        }
        break (entries, cursor, true);
    };
    // Changes in the order of their revisions: a reply being written took its revision when
    // it last grew, and what was stored since stands above it.
    entries.sort_by_key(|row| row.revision);
    Ok(Page {
        entries,
        latest_revision,
        has_more,
        head_revision,
        begins_after: span.floor * REVISION_STEP,
    })
}

/// At creation, a delegate starts just before the newest `offset` distinct
/// rows, across the owner's conversations. A row edited late counts at its
/// latest revision. Read backwards until older items cannot change the answer.
pub(crate) fn delegation_baseline(
    log: &dyn AgentLog,
    drafts: &[Draft],
    offset: usize,
) -> Result<i64, ActorError> {
    let mut before = Seq::MAX;
    let mut revisions = std::collections::HashMap::new();
    for draft in drafts {
        revisions.insert(draft.id.clone(), draft.revision);
    }
    loop {
        let part = log.read_before(before, kind::CONVERSATION, CHUNK)?;
        let Some(first) = part.first() else {
            return Ok(0);
        };
        before = first.seq;
        for row in rows(log, &part, Place::Newest, Which::All)? {
            let rev = revisions.entry(row.entry.id().to_owned()).or_insert(0);
            *rev = (*rev).max(row.revision);
        }
        if revisions.len() > offset {
            let mut numbers: Vec<_> = revisions.values().copied().collect();
            let (_, cut, _) = numbers.select_nth_unstable_by(offset, |a, b| b.cmp(a));
            if *cut >= before.saturating_mul(REVISION_STEP) || part.len() < CHUNK {
                return i64::try_from(*cut)
                    .map_err(|_| ActorError::Corrupt("message revision exhausted".into()));
            }
        }
        if part.len() < CHUNK {
            return Ok(0);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_reader_is_told_its_conversation_ended_above_the_head_and_at_or_below_where_it_begins() {
        let page = Page {
            head_revision: 9 * REVISION_STEP,
            begins_after: 4 * REVISION_STEP,
            ..Page::default()
        };
        assert!(page.ended_for(9 * REVISION_STEP + 1), "above the head");
        assert!(!page.ended_for(9 * REVISION_STEP), "at the head");
        assert!(
            !page.ended_for(4 * REVISION_STEP + 1),
            "just after the floor"
        );
        assert!(page.ended_for(4 * REVISION_STEP), "at the floor");
        assert!(page.ended_for(REVISION_STEP), "below the floor");
        // An agent that was never started again: only above the head.
        let page = Page {
            head_revision: 3 * REVISION_STEP,
            ..Page::default()
        };
        assert!(!page.ended_for(1));
        assert!(!page.ended_for(3 * REVISION_STEP));
        assert!(page.ended_for(3 * REVISION_STEP + 1));
    }
}
