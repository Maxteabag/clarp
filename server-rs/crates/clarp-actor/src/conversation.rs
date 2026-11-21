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

use std::collections::{BTreeSet, HashMap, HashSet};

use clarp_kernel::agent::AgentEvent;
use clarp_kernel::conversation::{Commentary, Entry, Reply, ToolCall, TurnEnd};
use clarp_kernel::journal::{Batch, coordination_key, kind};
use clarp_kernel::turn::{Origin, Turn, TurnEvent, TurnId, left_out_of_chats};
use clarp_ports::log::{AgentLog, Item, NewItem, Seq};

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
    WithoutCoordination,
    WithoutAutomatedOrCoordination,
}

impl Which {
    pub fn hide_coordination(self) -> Self {
        match self {
            Self::All => Self::WithoutCoordination,
            Self::WithoutAutomated => Self::WithoutAutomatedOrCoordination,
            _ => self,
        }
    }

    fn hides(self) -> bool {
        matches!(
            self,
            Self::WithoutCoordination | Self::WithoutAutomatedOrCoordination
        )
    }

    fn keeps(self, entry: &Entry) -> bool {
        !matches!(
            self,
            Self::WithoutAutomated | Self::WithoutAutomatedOrCoordination
        ) || !left_out_of_chats(entry.origin_name())
    }

    fn keeps_row(self, row: &Versioned) -> bool {
        self.keeps(&row.entry)
            && (!self.hides() || row.coordination.is_none() || matches!(row.entry, Entry::Note(_)))
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

#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct HiddenRun {
    pub messages: usize,
    pub exchanges: usize,
    pub senders: BTreeSet<String>,
    pub capped: bool,
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
    pub hidden_before: HashMap<String, HiddenRun>,
    pub hidden_after: Option<HiddenRun>,
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
    if mixed(log, turn)? {
        return Ok(None);
    }
    original_mark(log, turn)
}

fn original_mark(log: &dyn AgentLog, turn: u64) -> Result<Option<String>, ActorError> {
    let Some(stored) = log.find(&coordination_key(turn))? else {
        return Ok(None);
    };
    let event: AgentEvent =
        serde_json::from_slice(&stored.body).map_err(|error| corrupt(&stored, error))?;
    Ok(match event {
        AgentEvent::Turn {
            change: TurnEvent::Accepted { coordination, .. },
            ..
        } => coordination,
        _ => None,
    })
}

fn mixed_key(turn: u64) -> String {
    format!("coordination-mixed-{turn}")
}

fn restamp_key(id: &str) -> String {
    format!("coordination-row-{id}")
}

pub(super) fn mixed(log: &dyn AgentLog, turn: u64) -> Result<bool, ActorError> {
    Ok(log
        .find(&mixed_key(turn))?
        .is_some_and(|item| item.kind == kind::MIXED_TURN))
}

/// Show a marked turn once a user is heard in it. New revisions let a client read the old
/// rows again without writing a second message or moving any existing row.
pub(super) fn loosen(batch: &mut Batch<'_>, turn: TurnId, at: i64) -> Result<bool, ActorError> {
    if mark_of(batch.log(), turn.0)?.is_none() {
        return Ok(false);
    }
    let Some(message_seq) = (match batch.agent().live() {
        Some(Turn::Starting(live)) if live.id == turn => Some(live.message_seq),
        Some(Turn::Running(live)) if live.id == turn => Some(live.message_seq),
        _ => None,
    }) else {
        return Ok(false);
    };
    let mut after = message_seq.saturating_sub(1);
    let mut stamped = HashSet::new();
    loop {
        let rows = batch.log().read_after(after, kind::ROWS, CHUNK)?;
        let Some(last) = rows.last() else { break };
        after = last.seq;
        for row in rows.iter().filter(|row| row.turn == Some(turn.0)) {
            let Some(entry) = Entry::from_item(row).map_err(|error| corrupt(row, error))? else {
                continue;
            };
            let id = entry.id();
            if stamped.insert(id.to_owned()) {
                batch.item(
                    NewItem::new(kind::AUDIENCE_ROW, at, id.as_bytes().to_vec())
                        .in_turn(turn.0)
                        .keyed(restamp_key(id)),
                );
            }
        }
    }
    batch.item(
        NewItem::new(kind::MIXED_TURN, at, Vec::new())
            .in_turn(turn.0)
            .keyed(mixed_key(turn.0)),
    );
    Ok(true)
}

/// Reads rows as they are now: each with the parts that name it.
struct Reader<'a> {
    log: &'a dyn AgentLog,
    /// How the turns asked about ended, and where in the log that is said.
    ends: HashMap<u64, Option<(TurnEnd, Seq)>>,
    /// Whether the turns asked about answer another agent ([`mark_of`]).
    marks: HashMap<u64, (Option<String>, bool)>,
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
    fn mark(&mut self, turn: u64) -> Result<(Option<String>, bool), ActorError> {
        if let Some(known) = self.marks.get(&turn) {
            return Ok(known.clone());
        }
        let loosened = mixed(self.log, turn)?;
        let found = (
            if loosened {
                None
            } else {
                original_mark(self.log, turn)?
            },
            loosened,
        );
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

    fn hidden(&mut self, item: &Item, entry: &Entry) -> Result<Option<String>, ActorError> {
        if matches!(entry, Entry::Note(_)) {
            return Ok(None);
        }
        match item.turn.or(entry.turn()) {
            Some(turn) => self.mark(turn).map(|(sender, _)| sender),
            None => Ok(None),
        }
    }

    fn allows(&mut self, item: &Item, entry: &Entry, which: Which) -> Result<bool, ActorError> {
        Ok(which.keeps(entry) && (!which.hides() || self.hidden(item, entry)?.is_none()))
    }

    fn keeps_item(&mut self, item: &Item, which: Which) -> Result<bool, ActorError> {
        if which == Which::All {
            return Ok(true);
        }
        let entry = Entry::from_item(item).map_err(|why| corrupt(item, why))?;
        if which.hides() {
            let parent = super::helper_cells::parent(item)?;
            if let Some(id) = parent
                .as_deref()
                .or_else(|| entry.as_ref().and_then(Entry::part_of))
            {
                if let Some(row) = self.log.find(id)?
                    && let Some(entry) = Entry::from_item(&row).map_err(|why| corrupt(&row, why))?
                {
                    return self.allows(&row, &entry, which);
                }
                return Ok(false);
            }
        }
        entry
            .as_ref()
            .map_or(Ok(true), |entry| self.allows(item, entry, which))
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
            let (marked, loosened) = self.mark(turn)?;
            row.coordination = marked;
            if loosened
                && let Some(restamp) = self.log.find(&restamp_key(row.entry.id()))?
                && restamp.kind == kind::AUDIENCE_ROW
            {
                newest = newest.max(restamp.seq);
            }
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
        if let Some(entry) = entry
            && self.allows(&stored, &entry, which)?
        {
            return self.row(&stored, entry).map(Some);
        }
        Ok(None)
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
        if item.kind == kind::AUDIENCE_ROW {
            if place == Place::Newest {
                let id = String::from_utf8_lossy(&item.body);
                found.retain(|r| r.entry.id() != id.as_ref());
                found.extend(reader.stored(&id, which)?);
            }
            continue;
        }
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
        if !reader.allows(item, &entry, which)? {
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

const COUNT_CAP: usize = 100;
const COUNT_ROW_CAP: usize = 400;

#[derive(Default)]
struct Counting {
    run: HiddenRun,
    rows: usize,
    turns: BTreeSet<u64>,
}

impl Counting {
    fn push(&mut self, entry: &Entry, turn: Option<u64>, sender: String) {
        let text = match entry {
            Entry::Message(message) => message.text.as_str(),
            Entry::Reply(reply) => reply.text.as_str(),
            Entry::Imported(row)
                if matches!(row.row["role"].as_str(), Some("user" | "assistant"))
                    && row.row["tool_name"].as_str().unwrap_or("").is_empty() =>
            {
                row.row["text"].as_str().unwrap_or("")
            }
            _ => "",
        };
        let counted = !text.trim().is_empty();
        if self.rows == COUNT_ROW_CAP || (counted && self.run.messages == COUNT_CAP) {
            self.run.capped |= counted;
            return;
        }
        self.rows += 1;
        self.run.messages += usize::from(counted);
        if let Some(turn) = turn {
            self.turns.insert(turn);
        }
        if !sender.is_empty() {
            self.run.senders.insert(sender);
        }
    }

    fn finish(mut self) -> Option<HiddenRun> {
        self.run.exchanges = self.turns.len();
        (self.rows > 0).then_some(self.run)
    }
}

/// Nearest hidden rows until the next visible row of the chat. Read one key first so
/// ordinary neighboring rows do not require reading another copy of the page.
fn adjacent(
    reader: &mut Reader<'_>,
    floor: Seq,
    mut cursor: Seq,
    forward: bool,
    which: Which,
    bound_look_back: bool,
) -> Result<Counting, ActorError> {
    let mut counted = Counting::default();
    let mut budget = if bound_look_back {
        COUNT_ROW_CAP + 1
    } else {
        usize::MAX
    };
    let mut size = 1;
    loop {
        let mut items = if forward {
            reader.log.read_after(cursor, kind::ROWS, size)?
        } else {
            reader.log.read_before(cursor, kind::ROWS, size)?
        };
        let end = items.len() < size;
        if !forward {
            items.reverse();
        }
        for item in items {
            cursor = item.seq;
            if cursor <= floor {
                return Ok(counted);
            }
            let Some(entry) = Entry::from_item(&item).map_err(|why| corrupt(&item, why))? else {
                continue;
            };
            let sender = reader.hidden(&item, &entry)?;
            if sender.is_none() && which.keeps(&entry) {
                return Ok(counted);
            }
            budget -= 1;
            if budget == 0 {
                counted.run.capped = true;
                return Ok(counted);
            }
            if let Some(sender) = sender {
                counted.push(&entry, item.turn.or(entry.turn()), sender);
                if counted.run.capped {
                    return Ok(counted);
                }
            }
        }
        if end {
            return Ok(counted);
        }
        size = CHUNK.min(budget);
    }
}

/// On a delta or an empty tail page, find the newest visible row without walking
/// an arbitrarily long hidden suffix. The extra row is the exclusive cut for the count.
fn tail_boundary(
    reader: &mut Reader<'_>,
    floor: Seq,
    which: Which,
) -> Result<(Seq, bool), ActorError> {
    let mut cursor = Seq::MAX;
    let mut remaining = COUNT_ROW_CAP + 1;
    let mut size = 1;
    loop {
        let items = reader.log.read_before(cursor, kind::ROWS, size)?;
        let end = items.len() < size;
        for item in items.into_iter().rev() {
            cursor = item.seq;
            if cursor <= floor {
                return Ok((floor, false));
            }
            if reader.keeps_item(&item, which)? {
                return Ok((cursor, false));
            }
            remaining -= 1;
            if remaining == 0 {
                return Ok((cursor, true));
            }
        }
        if end {
            return Ok((floor, false));
        }
        size = CHUNK.min(remaining);
    }
}

type Gaps = (HashMap<String, HiddenRun>, Option<HiddenRun>);

fn gaps(
    log: &dyn AgentLog,
    entries: &[Versioned],
    drafts: &[Draft],
    span: Span,
    which: Which,
    tail: bool,
    contiguous: bool,
) -> Result<Gaps, ActorError> {
    if !which.hides() {
        return Ok((HashMap::new(), None));
    }
    let mut reader = Reader::new(log);
    let mut before = HashMap::new();
    let mut saw_draft = false;
    let mut last_stored = None;
    for row in entries {
        let position = if row.live {
            if saw_draft {
                continue;
            }
            saw_draft = true;
            Seq::MAX
        } else {
            let Some(item) = log.find(row.entry.id())? else {
                continue;
            };
            item.seq
        };
        let bound = !contiguous || last_stored.is_none();
        if let Some(run) =
            adjacent(&mut reader, span.floor, position, false, which, bound)?.finish()
        {
            before.insert(row.entry.id().to_owned(), run);
        }
        if contiguous && !row.live {
            last_stored = Some(position);
        }
    }
    let after = if tail {
        let (start, cut) = match last_stored {
            Some(position) => (position, false),
            None => tail_boundary(&mut reader, span.floor, which)?,
        };
        let mut counted = adjacent(&mut reader, span.floor, start, true, which, false)?;
        counted.run.capped |= cut;
        for draft in drafts {
            let visible = draft.versioned();
            if let (Some(sender), _) = reader.mark(draft.turn)? {
                counted.push(&visible.entry, Some(draft.turn), sender);
            } else if which.keeps(&visible.entry) {
                counted = Counting::default();
            }
        }
        counted.finish()
    } else {
        None
    };
    Ok((before, after))
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
    let mut judge = Reader::new(log);
    loop {
        let chunk = log.read_before(cursor, kind::ROWS, CHUNK)?;
        let Some(oldest) = chunk.first() else {
            break;
        };
        cursor = oldest.seq;
        // The conversation begins after the floor: nothing older is read.
        let whole = chunk.len() == CHUNK && oldest.seq > floor;
        for item in chunk.into_iter().rev() {
            if kept.len() <= limit && item.seq > floor && judge.keeps_item(&item, which)? {
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
    // A reply being written belongs at the tail, not on an older filtered page.
    let mut live = Vec::new();
    if !which.hides() || before == Seq::MAX {
        let mut reader = Reader::new(log);
        for draft in drafts {
            let mut shown = draft.versioned();
            (shown.coordination, _) = reader.mark(draft.turn)?;
            if which.keeps_row(&shown) {
                live.push(shown);
            }
        }
    }
    let stored_limit = if which.hides() {
        limit.saturating_sub(live.len())
    } else {
        limit
    };
    let (items, mut has_more) = items_before(log, (before, span.floor), stored_limit, which)?;
    let mut entries = rows(log, &items, Place::First, which)?;
    live.retain(|shown| {
        !entries
            .iter()
            .any(|known| known.entry.id() == shown.entry.id())
    });
    entries.extend(live);
    if which.hides() && entries.len() > limit {
        entries.drain(..entries.len() - limit);
        has_more = true;
    }
    let tail = before == Seq::MAX;
    let (hidden_before, hidden_after) = gaps(log, &entries, drafts, span, which, tail, true)?;
    Ok(Page {
        entries,
        latest_revision: head_revision,
        has_more,
        head_revision,
        begins_after: span.floor * REVISION_STEP,
        hidden_before,
        hidden_after,
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
    let mut judge = Reader::new(log);
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
            if judge.keeps_item(&item, which)? {
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
                shown.coordination = mark_of(log, draft.turn)?;
                if draft.revision > revision && which.keeps_row(&shown) {
                    entries.push(shown);
                }
            }
            if which.hides() && entries.len() > limit {
                entries.sort_unstable_by_key(|row| row.revision);
                entries.truncate(limit);
                let last = entries.last().map_or(revision, |row| row.revision);
                break (entries, last, true);
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
    let (hidden_before, hidden_after) = gaps(log, &entries, drafts, span, which, true, false)?;
    Ok(Page {
        entries,
        latest_revision,
        has_more,
        head_revision,
        begins_after: span.floor * REVISION_STEP,
        hidden_before,
        hidden_after,
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
    #[allow(clippy::unwrap_used)]
    fn revealing_a_turn_sends_its_old_rows_through_a_delta() {
        use clarp_kernel::conversation::Message;
        use clarp_kernel::journal::{Incoming, Recorder};
        use clarp_kernel::turn::Admission;
        use clarp_ports::log::LogEngine;
        use std::path::Path;

        let engine = clarp_store_mem::MemEngine::new();
        let log = engine
            .open(
                Path::new("/agents/recipient"),
                clarp_codec::standard_set("zstd").unwrap(),
            )
            .unwrap();
        let mut recorder = Recorder::open(log).unwrap();
        let message = Message {
            id: "u-request".into(),
            text: "Look at this".into(),
            origin: Origin::Agent {
                agent_id: "sender".into(),
            },
            ..Message::default()
        };
        recorder
            .accept_message(
                Incoming {
                    key: Some(message.id.clone()),
                    body: serde_json::to_vec(&message).unwrap(),
                    origin: message.origin.clone(),
                    admission: Admission::Queue,
                    coordination: Some("sender".into()),
                    user_word: false,
                },
                100,
            )
            .unwrap();
        let mut batch = recorder.batch();
        let reply = Entry::Reply(Reply {
            id: "assistant-row".into(),
            turn: 1,
            text: "Using a tool".into(),
            ..Reply::default()
        });
        batch.item(reply.to_item(200).unwrap());
        batch.commit().unwrap();
        let cursor = recorder.conversation_seq() * REVISION_STEP;
        assert_eq!(
            mark_of(recorder.log(), 1).unwrap().as_deref(),
            Some("sender")
        );

        let mut batch = recorder.batch();
        assert!(loosen(&mut batch, TurnId(1), 300).unwrap());
        batch.commit().unwrap();
        let span = Span {
            floor: 0,
            head_revision: recorder.conversation_seq() * REVISION_STEP,
        };
        let first = after(recorder.log(), cursor, &[], span, 1, Which::All).unwrap();
        assert!(first.has_more);
        assert_eq!(first.entries[0].entry.id(), "u-request");
        assert_eq!(first.entries[0].ts_ms, 100);
        assert!(first.entries[0].coordination.is_none());
        let next = after(
            recorder.log(),
            first.latest_revision,
            &[],
            span,
            1,
            Which::All,
        )
        .unwrap();
        assert!(!next.has_more);
        assert_eq!(next.entries[0].entry.id(), "assistant-row");
        assert_eq!(next.entries[0].ts_ms, 200);
        assert!(next.entries[0].coordination.is_none());
        assert_eq!(
            original_mark(recorder.log(), 1).unwrap().as_deref(),
            Some("sender")
        );
        assert!(!loosen(&mut recorder.batch(), TurnId(1), 400).unwrap());
    }

    #[test]
    #[allow(clippy::unwrap_used)]
    fn counted_runs_match_the_python_host_at_both_caps() {
        use clarp_kernel::conversation::Imported;
        use serde_json::{Value, json};
        let vectors: Value = serde_json::from_str(include_str!(
            "../../clarp-kernel/tests/fixtures/hidden-coordination-vectors.json"
        ))
        .unwrap();
        let cases = vectors["counts"].as_array().unwrap();
        for case in cases {
            let mut counting = Counting::default();
            for (turn, kind) in (1..).zip(case["nearest"].as_str().unwrap().chars()) {
                let entry = Entry::Imported(Imported {
                    id: format!("row-{turn}"),
                    row: json!({
                        "role": "assistant",
                        "text": if kind == 'm' { "words" } else { "" },
                        "tool_name": if kind == 't' { "Bash" } else { "" },
                    }),
                });
                counting.push(&entry, Some(turn), "agent-2".to_owned());
                if counting.run.capped {
                    break;
                }
            }
            if let Some(run) = counting.finish() {
                assert_eq!(json!(run.messages), case["answer"]["messages"]);
                assert_eq!(json!(run.exchanges), case["answer"]["exchanges"]);
                assert_eq!(json!(run.capped), case["answer"]["capped"]);
                assert!(run.senders.contains("agent-2"));
            } else {
                assert!(case["answer"].is_null(), "{case}");
            }
        }
        assert_eq!(cases.len(), 7);
    }

    #[test]
    #[allow(clippy::unwrap_used)]
    fn gaps_limit_the_look_back_for_a_tail_or_delta() {
        use clarp_kernel::conversation::Imported;
        use clarp_kernel::turn::Admission;
        use clarp_ports::log::LogEngine;
        use serde_json::{Value, json};

        let vectors: Value = serde_json::from_str(include_str!(
            "../../clarp-kernel/tests/fixtures/hidden-coordination-vectors.json"
        ))
        .unwrap();
        let cases = vectors["look_back"].as_array().unwrap();
        for case in cases {
            let mut log = clarp_store_mem::MemEngine::new()
                .open(
                    std::path::Path::new("/agents/recipient"),
                    clarp_codec::standard_set("zstd").unwrap(),
                )
                .unwrap();
            let tail = case["tail"].as_bool().unwrap();
            let nearest = case["nearest"].as_str().unwrap();
            let order = if tail {
                nearest.to_owned()
            } else {
                nearest.chars().rev().collect()
            };
            let mut items = Vec::new();
            for (number, symbol) in (1..).zip(order.chars()) {
                let turn = TurnId(number);
                let accepted = AgentEvent::Turn {
                    turn,
                    change: TurnEvent::Accepted {
                        turn,
                        message_seq: 0,
                        origin: Origin::Agent {
                            agent_id: "agent-2".into(),
                        },
                        admission: Admission::Queue,
                        at_ms: 0,
                        coordination: Some("agent-2".into()),
                        user_word: Some(false),
                    },
                };
                items.push(
                    NewItem::new(kind::AGENT_EVENT, 0, serde_json::to_vec(&accepted).unwrap())
                        .keyed(coordination_key(number)),
                );
                let id = format!("h-{number}");
                let row = Imported {
                    id: id.clone(),
                    row: json!({"role": "assistant", "text": if symbol == 'm' { "words" } else { "" }}),
                };
                items.push(
                    NewItem::new(kind::IMPORTED, 0, serde_json::to_vec(&row).unwrap())
                        .in_turn(number)
                        .keyed(id),
                );
            }
            if !tail {
                let row = Imported {
                    id: "visible".into(),
                    row: json!({"role":"user","text":"visible"}),
                };
                items.push(
                    NewItem::new(kind::IMPORTED, 0, serde_json::to_vec(&row).unwrap())
                        .keyed("visible"),
                );
            }
            log.append(&items).unwrap();
            let entries: Vec<_> = one(log.as_ref(), "visible").unwrap().into_iter().collect();
            let (before, after) = gaps(
                log.as_ref(),
                &entries,
                &[],
                Span::default(),
                Which::WithoutCoordination,
                tail,
                tail,
            )
            .unwrap();
            let run = if tail {
                after.as_ref()
            } else {
                before.get("visible")
            }
            .unwrap();
            assert_eq!(json!(run.messages), case["answer"]["messages"], "{case}");
            assert_eq!(json!(run.exchanges), case["answer"]["exchanges"], "{case}");
            assert_eq!(json!(run.capped), case["answer"]["capped"], "{case}");
        }
        assert_eq!(cases.len(), 2);
    }

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
