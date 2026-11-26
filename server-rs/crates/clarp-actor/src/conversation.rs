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
use clarp_kernel::journal::{Batch, coordination_key, kind, mixed_coordination_key, mixed_turn};
use clarp_kernel::turn::{Origin, TurnEvent, TurnId, left_out_of_chats};
use clarp_ports::log::{AgentLog, Item, Kinds, NewItem, Seq};
use serde::{Deserialize, Serialize};

use crate::ActorError;

mod anchors;
mod imported_anchors;
pub(crate) use anchors::AnchorCache;
use anchors::{Anchor, TurnAnchors, find_anchors, read_into_turn};
pub(crate) use imported_anchors::ImportedAnchors;
pub(crate) mod resend;

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
    /// The app's /log read. Its submitted answers are shown on their form cards.
    Chat {
        include_automated: bool,
        hide_coordination: bool,
        /// When set, keep conversation rows only. The value says whether the agent
        /// can have a coordination audience.
        conversation_only: Option<bool>,
        /// The chat is a helper's: the turns other agents give it are its conversation
        /// ([`in_conversation_view`]).
        helper: bool,
    },
}

impl Which {
    pub fn chat(include_automated: bool) -> Self {
        Self::Chat {
            include_automated,
            hide_coordination: false,
            conversation_only: None,
            helper: false,
        }
    }

    /// The chat of a helper (`helper`), whose conversation is the turns other agents give it.
    /// The actor keeps a chat that was once read so a helper's ([`crate::Actor::chat_of`]), so
    /// a row's view stays what it was when the agent's role changes.
    pub fn of_helper(self, helper: bool) -> Self {
        match self {
            Self::Chat {
                include_automated,
                hide_coordination,
                conversation_only,
                ..
            } => Self::Chat {
                include_automated,
                hide_coordination: hide_coordination && !(helper && conversation_only.is_some()),
                conversation_only,
                helper,
            },
            _ => self,
        }
    }

    /// Conversation rows only. A helper's conversation is the agents' turns, so they are not
    /// hidden from it as coordination.
    pub fn conversation_view(self, can_have_coordination: bool) -> Self {
        match self {
            Self::Chat {
                include_automated,
                hide_coordination,
                helper,
                ..
            } => Self::Chat {
                include_automated,
                hide_coordination: hide_coordination && !helper,
                conversation_only: Some(can_have_coordination),
                helper,
            },
            _ => self,
        }
    }

    /// Whether it reads a helper's chat.
    pub fn is_helper_chat(self) -> bool {
        matches!(self, Self::Chat { helper: true, .. })
    }

    fn conversation_only(self) -> Option<bool> {
        match self {
            Self::Chat {
                conversation_only, ..
            } => conversation_only,
            _ => None,
        }
    }

    pub fn hide_coordination(self) -> Self {
        match self {
            Self::All => Self::WithoutCoordination,
            Self::WithoutAutomated => Self::WithoutAutomatedOrCoordination,
            Self::Chat {
                include_automated,
                conversation_only,
                helper,
                ..
            } => Self::Chat {
                include_automated,
                hide_coordination: true,
                conversation_only,
                helper,
            },
            _ => self,
        }
    }

    fn hides(self) -> bool {
        matches!(
            self,
            Self::WithoutCoordination
                | Self::WithoutAutomatedOrCoordination
                | Self::Chat {
                    hide_coordination: true,
                    ..
                }
        )
    }

    fn skips_form_answer(self, id: &str) -> bool {
        matches!(self, Self::Chat { .. }) && clarp_kernel::artifacts::forms::is_answer_row(id)
    }

    fn keeps(self, entry: &Entry) -> bool {
        let hides_automated = matches!(
            self,
            Self::WithoutAutomated
                | Self::WithoutAutomatedOrCoordination
                | Self::Chat {
                    include_automated: false,
                    ..
                }
        );
        !self.skips_form_answer(entry.id())
            && (!hides_automated || !left_out_of_chats(entry.origin_name()))
    }

    fn keeps_row(self, row: &Versioned) -> bool {
        self.conversation_only().is_none()
            && self.keeps(&row.entry)
            && (!self.hides() || row.coordination.is_none() || matches!(row.entry, Entry::Note(_)))
    }
}

/// How many items are read at a time when some of them are left out.
const CHUNK: usize = 256;

/// The rows of the messages read into a running turn, oldest first (issue 808), and how each
/// was read (issue 859), from the actor's kept walk of it: a read walks only what was written
/// since the last.
pub(crate) fn read_rows_of(
    log: &dyn AgentLog,
    cache: &AnchorCache,
    turn: u64,
) -> Result<Vec<(String, ReadAt)>, ActorError> {
    Ok(cache.anchors(log, turn, None, false)?.read_rows)
}

/// Rows that are conversation without needing to be a turn's anchor.
pub fn is_conversation(entry: &Entry, session_rows: Option<usize>) -> bool {
    conversation_in(entry, session_rows, false)
}

/// Whether a row is in a chat's conversation view. `for_agents`: the row's turn answers
/// another agent (its audience is the agents'). Such a turn is left out of the user's
/// conversation, except in a helper's chat (`helper`): the turns other agents give a helper are
/// what it is for, so they are its conversation, the message that began one as the user's own
/// is, and its answers anchor as a user's turn's do. Their audience stays the agents'.
pub fn in_conversation_view(
    entry: &Entry,
    session_rows: Option<usize>,
    for_agents: bool,
    helper: bool,
) -> bool {
    (!for_agents || (helper && given_by_an_agent(entry)))
        && conversation_in(entry, session_rows, helper)
}

/// Whether a row is of a turn another agent began: its message, and what the agent said and did
/// for it, which carry the origin of the turn. A turn the Host began on its own (a schedule, a
/// wake, a goal's continuation) is not, though it is for the agents too: its answers are no
/// conversation even in a helper's chat.
fn given_by_an_agent(entry: &Entry) -> bool {
    entry.origin_name() == "agent"
}

/// Rows that are conversation without needing to be a turn's anchor, in a helper's chat
/// (`helper`) or another.
pub(crate) fn conversation_in(entry: &Entry, session_rows: Option<usize>, helper: bool) -> bool {
    match entry {
        Entry::Message(message) => {
            (message.origin.is_user_send()
                || (helper && matches!(message.origin, Origin::Agent { .. })))
                && !clarp_kernel::artifacts::forms::is_answer_row(&message.id)
        }
        Entry::Note(_) => true,
        Entry::Notice(notice) => matches!(
            notice.code.as_str(),
            clarp_kernel::conversation::Notice::SYSTEM
                | clarp_kernel::conversation::Notice::TURN_FAILED
                | clarp_kernel::conversation::Notice::TRANSIT
        ),
        Entry::Reply(_) => session_rows.is_some(),
        Entry::Imported(import) => {
            session_rows.is_some()
                || clarp_kernel::chat_view::imported_is_conversation(&import.row)
                // A helper's chat that moved here: the messages other agents gave it came
                // as imported rows (issue 777).
                || (helper && import.row["role"] == "user" && import.row["origin"] == "agent")
        }
        _ => false,
    }
}

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
    /// On a turn's conversation anchor (the answer to one of its messages), the number of rows
    /// omitted by that view for it.
    pub session_rows: Option<usize>,
    /// The uses of the tools a reply's message calls, as they are now, in its order.
    pub tools: Vec<ToolCall>,
    /// Tool work came after this reply in the same turn: it was said along the way.
    pub commentary: bool,
    /// The turn it belongs to answers another agent: that agent's id, empty for an agent of a
    /// peer Host. `None`: the user's turn, or no turn.
    pub coordination: Option<String>,
    /// The session of the agent's tool its turn ran in, where the Host knows it: the agent's
    /// own, or its conversation fork's (issue 718). Filled by the actor that reads the page.
    pub session: Option<String>,
    /// A message read into a turn other than its own (steered into it as it ran, or read with
    /// its own message at its start): that turn. Found from what the join wrote with the row,
    /// so the row never stands without it (issue 845).
    pub read_into: Option<ReadInto>,
}

/// The turn a message was read into, other than its own.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ReadInto {
    /// The turn's id as the live view names it (`turn_id`): the trace of its own message.
    pub trace_id: String,
    /// The turn's number.
    pub turn: u64,
    /// How it was read: with the turn's own message at its start, or steered in as it ran.
    pub at: ReadAt,
}

/// How a message was read into a turn other than its own (issue 859).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ReadAt {
    /// With the turn's own message, in the prompt the turn started with: it waited behind
    /// that message and the turn took it along (`Join::together`). It stands above the turn's
    /// work, as part of what the turn was asked, and splits none of it.
    Start,
    /// Into the tool's process as the turn ran: what the turn did before it answered what
    /// came before, and its work splits there.
    Steer,
}

impl ReadAt {
    /// The word clients are given (`read_into.at`, the live turn's `read_rows_at`).
    pub fn name(self) -> &'static str {
        match self {
            Self::Start => "start",
            Self::Steer => "steer",
        }
    }
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
            session_rows: None,
            tools: Vec::new(),
            commentary: false,
            coordination: None,
            session: None,
            read_into: None,
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
    /// It was read as a helper's chat ([`in_conversation_view`]): the view of each row is told
    /// so too.
    pub helper_chat: bool,
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
            backend_session: None,
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

pub(super) fn original_mark(log: &dyn AgentLog, turn: u64) -> Result<Option<String>, ActorError> {
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

fn restamp_key(id: &str) -> String {
    format!("coordination-row-{id}")
}

fn mixed_record(log: &dyn AgentLog, turn: u64) -> Result<Option<Item>, ActorError> {
    Ok(log
        .find(&mixed_coordination_key(turn))?
        .filter(|item| item.kind == kind::MIXED_TURN))
}

/// Whether a turn that answers another agent was split by a message of the user's read into
/// it: rows after the split are the user's, until a note answers the user (rule FU).
pub(super) fn split_by_the_user(log: &dyn AgentLog, turn: u64) -> Result<bool, ActorError> {
    Ok(
        mixed_record(log, turn)?.is_some_and(|item| !item.body.is_empty())
            && original_mark(log, turn)?.is_some(),
    )
}

pub(super) fn mixed(log: &dyn AgentLog, turn: u64) -> Result<bool, ActorError> {
    Ok(mixed_record(log, turn)?.is_some())
}

/// Rows in flight at the echo retain their original audience even if they
/// are written or updated after the echo. All other rows follow journal order.
#[derive(Serialize, Deserialize)]
struct Split {
    earlier: BTreeSet<String>,
}

#[derive(Clone, Copy, Default)]
pub(super) struct OpenRows<'a> {
    pub(super) drafts: &'a [Draft],
    pub(super) calls: &'a [ToolCall],
}

/// Record the audience boundary just after the follow-up row, in the same
/// transaction that records the tool's read. A legacy empty mark was a full
/// reveal and keeps its meaning.
pub(super) fn split(
    batch: &mut Batch<'_>,
    turn: TurnId,
    open: OpenRows<'_>,
    at: i64,
) -> Result<bool, ActorError> {
    if mark_of(batch.log(), turn.0)?.is_none() {
        return Ok(false);
    }
    let split = Split {
        earlier: open
            .drafts
            .iter()
            .map(|draft| draft.id.clone())
            .chain(open.calls.iter().map(|call| call.id.clone()))
            .collect(),
    };
    let mut item = mixed_turn(turn.0, at);
    item.body = serde_json::to_vec(&split).map_err(|error| {
        ActorError::Corrupt(format!("could not write audience boundary: {error}"))
    })?;
    batch.item(item);
    Ok(true)
}

/// The writes that reveal an existing turn when a trusted relay uses its trace.
/// A waiting message still has no rows to restamp.
pub(super) fn loosen_from(
    log: &dyn AgentLog,
    turn: TurnId,
    first: Option<Seq>,
    at: i64,
) -> Result<Vec<NewItem>, ActorError> {
    let mut items = Vec::new();
    if mark_of(log, turn.0)?.is_none() {
        return Ok(items);
    }
    if let Some(first) = first {
        let end = log
            .find(&format!("t-{}", turn.0))?
            .map_or(Seq::MAX, |row| row.seq);
        let mut after = first.saturating_sub(1);
        let mut stamped = HashSet::new();
        while after < end {
            let rows = log.read_after(after, kind::ROWS, CHUNK)?;
            let Some(last) = rows.last() else { break };
            after = last.seq;
            for row in rows
                .iter()
                .take_while(|row| row.seq < end)
                .filter(|row| row.turn == Some(turn.0))
            {
                let Some(entry) = Entry::from_item(row).map_err(|error| corrupt(row, error))?
                else {
                    continue;
                };
                let id = entry.id();
                if stamped.insert(id.to_owned()) {
                    items.push(
                        NewItem::new(kind::AUDIENCE_ROW, at, id.as_bytes().to_vec())
                            .in_turn(turn.0)
                            .keyed(restamp_key(id)),
                    );
                }
            }
        }
    }
    items.push(mixed_turn(turn.0, at));
    Ok(items)
}

fn recorded_end(log: &dyn AgentLog, turn: u64) -> Result<Option<(TurnEnd, Seq)>, ActorError> {
    if let Some(stored) = log.find(&format!("t-{turn}"))?
        && let Some(Entry::TurnEnd(end)) =
            Entry::from_item(&stored).map_err(|error| corrupt(&stored, error))?
    {
        return Ok(Some((end, stored.seq)));
    }
    Ok(None)
}

/// What happened in a split turn from its split on, in the log's order: each of the turn's notes
/// (`true`) and each message of the user's read into it (`false`), by place. A reply written
/// after a note that came after the user's latest message there belongs to the agents again:
/// the agent answered the user in the note, and what it says after is for the agent it works
/// for (rule FU). Decided by place alone, so a row is never shown and later hidden.
fn split_marks(log: &dyn AgentLog, turn: u64, split: Seq) -> Result<Vec<(Seq, bool)>, ActorError> {
    let end = recorded_end(log, turn)?.map_or(Seq::MAX, |(_, seq)| seq);
    // The echo writes the joined message immediately before the split marker.
    let mut after = split.saturating_sub(2);
    let mut marks = Vec::new();
    loop {
        let rows = log.read_after(after, Kinds::range(kind::MESSAGE, kind::AGENT_NOTE), CHUNK)?;
        let Some(last) = rows.last() else {
            break;
        };
        after = last.seq;
        for row in rows.iter().take_while(|row| row.seq < end) {
            match row.kind {
                kind::AGENT_NOTE if row.turn == Some(turn) => marks.push((row.seq, true)),
                kind::MESSAGE if row.turn != Some(turn) => {
                    let entry = Entry::from_item(row).map_err(|error| corrupt(row, error))?;
                    if let Some(Entry::Message(message)) = entry
                        && message.origin.is_user_send()
                        && joined_read(log, row)?
                    {
                        marks.push((row.seq, false));
                    }
                }
                _ => {}
            }
        }
        if rows.len() < CHUNK || last.seq >= end {
            break;
        }
    }
    Ok(marks)
}

/// Whether this message row was written by the read of a message joined into a running turn:
/// the receipt of that read is written immediately before it.
fn joined_read(log: &dyn AgentLog, row: &Item) -> Result<bool, ActorError> {
    let Some(receipt) = log
        .read_before(row.seq, Kinds::only(kind::AGENT_EVENT), 1)?
        .pop()
    else {
        return Ok(false);
    };
    Ok(receipt.turn == row.turn
        && matches!(
            serde_json::from_slice::<AgentEvent>(&receipt.body)
                .map_err(|error| corrupt(&receipt, error))?,
            AgentEvent::JoinRead { .. }
        ))
}

/// Whether the latest of the marks before this place is a note.
fn after_a_note(marks: &[(Seq, bool)], seq: Seq) -> bool {
    marks
        .iter()
        .take_while(|(at, _)| *at < seq)
        .last()
        .is_some_and(|(_, note)| *note)
}

enum TurnAudience {
    User {
        restamped: bool,
    },
    Agents {
        sender: String,
        split: Option<(Seq, Split)>,
        /// The notes and the user's messages after the split, by place ([`split_marks`]).
        marks: Vec<(Seq, bool)>,
    },
}

impl TurnAudience {
    /// The agent a row at this place answers, when the row is the agents' (`reply`: the row
    /// is the agent's text, as a draft or written).
    fn sender(&self, id: &str, seq: Seq, reply: bool) -> Option<&str> {
        match self {
            Self::Agents {
                sender,
                split,
                marks,
            } if split.as_ref().is_none_or(|(at, split)| {
                seq < *at || split.earlier.contains(id) || (reply && after_a_note(marks, seq))
            }) =>
            {
                Some(sender)
            }
            _ => None,
        }
    }
}

/// Reads rows as they are now: each with the parts that name it.
struct Reader<'a> {
    log: &'a dyn AgentLog,
    imported_anchors: Option<&'a ImportedAnchors>,
    /// The actor's walks of its newest turns, so a read of a running turn reads what is new.
    anchor_cache: Option<&'a AnchorCache>,
    /// It reads a helper's chat ([`in_conversation_view`]).
    helper: bool,
    /// How the turns asked about ended, and where in the log that is said.
    ends: HashMap<u64, Option<(TurnEnd, Seq)>>,
    anchors: HashMap<u64, TurnAnchors>,
    /// Whether the turns asked about answer another agent ([`mark_of`]).
    marks: HashMap<u64, TurnAudience>,
}

impl<'a> Reader<'a> {
    fn new(log: &'a dyn AgentLog) -> Self {
        Self {
            log,
            imported_anchors: None,
            anchor_cache: None,
            helper: false,
            ends: HashMap::new(),
            anchors: HashMap::new(),
            marks: HashMap::new(),
        }
    }

    /// For this reader of the conversation: a helper's chat counts the turns other agents give
    /// it as its conversation.
    fn reading(mut self, which: Which) -> Self {
        self.helper = which.is_helper_chat();
        self
    }

    fn with_span(mut self, span: Span<'a>) -> Self {
        self.imported_anchors = span.imported_anchors;
        self.anchor_cache = span.anchor_cache;
        self
    }

    /// Read the turn's durable audience boundary once per page.
    fn mark(&mut self, turn: u64) -> Result<&TurnAudience, ActorError> {
        if let std::collections::hash_map::Entry::Vacant(slot) = self.marks.entry(turn) {
            let marker = mixed_record(self.log, turn)?;
            let audience = match marker {
                Some(marker) if marker.body.is_empty() => TurnAudience::User { restamped: true },
                marker => match original_mark(self.log, turn)? {
                    Some(sender) => {
                        let split = marker
                            .as_ref()
                            .map(|item| {
                                serde_json::from_slice(&item.body)
                                    .map(|split| (item.seq, split))
                                    .map_err(|error| corrupt(item, error))
                            })
                            .transpose()?;
                        let marks = match &split {
                            Some((at, _)) => split_marks(self.log, turn, *at)?,
                            None => Vec::new(),
                        };
                        TurnAudience::Agents {
                            sender,
                            split,
                            marks,
                        }
                    }
                    None => TurnAudience::User { restamped: false },
                },
            };
            slot.insert(audience);
        }
        Ok(&self.marks[&turn])
    }

    fn draft_mark(&mut self, draft: &Draft) -> Result<Option<String>, ActorError> {
        Ok(self
            .mark(draft.turn)?
            .sender(&draft.id, Seq::MAX, true)
            .map(str::to_owned))
    }

    /// How this turn ended, when it has.
    fn end(&mut self, turn: u64) -> Result<Option<&(TurnEnd, Seq)>, ActorError> {
        if !self.ends.contains_key(&turn) {
            self.ends.insert(turn, recorded_end(self.log, turn)?);
        }
        Ok(self.ends.get(&turn).and_then(Option::as_ref))
    }

    /// The anchors of this turn, as far as it has got. A turn a follow-up replaced has none.
    fn anchors(&mut self, turn: u64) -> Result<&TurnAnchors, ActorError> {
        if !self.anchors.contains_key(&turn) {
            let anchors = match self.end(turn)? {
                Some((end, _)) if end.status == "replaced" => TurnAnchors::default(),
                end => {
                    let end = end.map(|(_, seq)| *seq);
                    match self.anchor_cache {
                        Some(cache) => cache.anchors(self.log, turn, end, self.helper)?,
                        None => find_anchors(self.log, turn, end, self.helper)?,
                    }
                }
            };
            self.anchors.insert(turn, anchors);
        }
        Ok(&self.anchors[&turn])
    }

    /// The turn's id as the live view names it, from the message it began for: written at
    /// its start, so the same for good, also once a newer message's turn replaced it and it
    /// has no anchors (a row's content changes only with its revision).
    fn turn_id(&mut self, turn: u64) -> Result<Option<String>, ActorError> {
        if let Some(id) = &self.anchors(turn)?.turn_id {
            return Ok(Some(id.clone()));
        }
        let end = self.end(turn)?.map(|(_, seq)| *seq);
        Ok(find_anchors(self.log, turn, end, self.helper)?.turn_id)
    }

    /// The anchor with this id, when the row is one of its turn's.
    fn anchor_of(&mut self, turn: u64, id: &str) -> Result<Option<&Anchor>, ActorError> {
        Ok(self.anchors(turn)?.of(id))
    }

    /// The reply this row decided was an answer, which entered the conversation view with it:
    /// a reply with words after it, or a message of the user's read into its turn.
    fn decided_by(&mut self, item: &Item, entry: &Entry) -> Result<Option<String>, ActorError> {
        let turn = match entry {
            Entry::Reply(reply) => Some(item.turn.unwrap_or(reply.turn)),
            Entry::Message(_) => read_into_turn(self.log, item)?.map(|(turn, _)| turn),
            _ => None,
        };
        Ok(match turn {
            Some(turn) => self.anchors(turn)?.decides.get(entry.id()).cloned(),
            None => None,
        })
    }

    /// The anchor the turn's end settled, once it has ended.
    fn settled_anchor(&mut self, turn: u64) -> Result<Option<&Anchor>, ActorError> {
        Ok(self.anchors(turn)?.settled())
    }

    fn chat_row(&mut self, row: &mut Versioned, which: Which) -> Result<(), ActorError> {
        if matches!(which, Which::Chat { .. })
            && let Entry::Imported(imported) = &row.entry
        {
            row.session_rows = self.imported_count(imported)?;
        }
        let ended = match row.turn_number {
            Some(turn) if matches!(which, Which::Chat { .. }) => Some(self.end(turn)?.is_some()),
            _ => None,
        };
        // Before its turn ends, only a reply can be an answer. A message read into a turn is of
        // a turn that never ends by itself, and is conversation without an anchor.
        if let (Some(turn), Some(ended)) = (row.turn_number, ended)
            && (ended || matches!(row.entry, Entry::Reply(_)))
        {
            // An anchor changed where it entered the conversation view: the row or the end
            // that decided it.
            match self.anchors(turn)?.of(row.entry.id()) {
                Some(anchor) => {
                    row.session_rows = Some(anchor.session_rows);
                    row.revision = row.revision.max(anchor.entered * REVISION_STEP);
                    // The answer of its turn is said at the end, also where work came after it
                    // (a turn stopped as its tool ran): as those who watched the turn were shown
                    // it, and as the conversation view holds it (issue 801).
                    row.commentary = false;
                }
                // Words of a running turn that nothing has decided yet are no answer: those
                // who watch the turn are shown them as commentary until the turn decides
                // (issue 771), and a chat read in the middle of the turn says the same (issue
                // 858). Whatever decides them later (the next words, a message read into the
                // turn, work, or the turn's end) is newer than the row, so its revision rises
                // with the change.
                None if !ended && !row.live => row.commentary = true,
                None => {}
            }
        }
        // A goal's check-in that was sent again for the apps (`resend`) is, in a chat, as new
        // as that. To a reader of everything it is where it was written.
        if matches!(which, Which::Chat { .. })
            && clarp_kernel::turn::names_a_goal_wake(row.entry.id())
            && let Some(resent) = self.log.find(&resend::key(row.entry.id()))?
            && resent.kind == kind::RESENT_ROW
        {
            row.revision = row.revision.max(resent.seq * REVISION_STEP);
        }
        Ok(())
    }

    fn imported_count(
        &self,
        imported: &clarp_kernel::conversation::Imported,
    ) -> Result<Option<usize>, ActorError> {
        match self.imported_anchors {
            Some(anchors) => anchors.count(self.log, imported),
            None => Ok(None),
        }
    }

    fn hidden(&mut self, item: &Item, entry: &Entry) -> Result<Option<String>, ActorError> {
        if matches!(entry, Entry::Note(_)) {
            return Ok(None);
        }
        // An imported row carries its audience from its source Host, outside this
        // agent's turn journal.
        if let Entry::Imported(imported) = entry {
            if imported.row["kind"] == clarp_kernel::coordination::NOTE_KIND {
                return Ok(None);
            }
            if imported.row["audience"] == clarp_kernel::coordination::AGENTS {
                let sender = ["sender_agent_id", "reply_to_agent_id"]
                    .iter()
                    .filter_map(|field| imported.row[*field].as_str())
                    .find(|id| !id.is_empty())
                    .unwrap_or_default();
                return Ok(Some(sender.to_owned()));
            }
        }
        match item.turn.or(entry.turn()) {
            Some(turn) => Ok(self
                .mark(turn)?
                .sender(entry.id(), item.seq, matches!(entry, Entry::Reply(_)))
                .map(str::to_owned)),
            None => Ok(None),
        }
    }

    fn allows(&mut self, item: &Item, entry: &Entry, which: Which) -> Result<bool, ActorError> {
        if !which.keeps(entry) || (which.hides() && self.hidden(item, entry)?.is_some()) {
            return Ok(false);
        }
        let Some(respect_audience) = which.conversation_only() else {
            return Ok(true);
        };
        let helper = which.is_helper_chat();
        if respect_audience
            && !(helper && given_by_an_agent(entry))
            && (self.hidden(item, entry)?.is_some()
                || matches!(entry, Entry::Imported(import) if import.row["audience"] == "agents"))
        {
            return Ok(false);
        }
        if conversation_in(entry, None, helper) {
            return Ok(true);
        }
        if let Entry::Imported(imported) = entry {
            return Ok(self.imported_count(imported)?.is_some());
        }
        Ok(match item.turn.or(entry.turn()) {
            Some(turn) => self.anchor_of(turn, entry.id())?.is_some(),
            None => false,
        })
    }

    fn keeps_item(&mut self, item: &Item, which: Which) -> Result<bool, ActorError> {
        if which == Which::All {
            return Ok(true);
        }
        let entry = Entry::from_item(item).map_err(|why| corrupt(item, why))?;
        if which.conversation_only().is_some()
            && let Some(Entry::TurnEnd(end)) = &entry
            && let Some(anchor) = self.settled_anchor(end.turn)?
        {
            let id = anchor.id.clone();
            if self.stored(&id, which)?.is_some() {
                return Ok(true);
            }
        }
        // A row that brought an earlier reply into the conversation view brings it to a reader
        // of that view, even when the row itself is not in it.
        if which.conversation_only().is_some()
            && let Some(entry) = &entry
            && let Some(id) = self.decided_by(item, entry)?
            && self.stored(&id, which)?.is_some()
        {
            return Ok(true);
        }
        if entry
            .as_ref()
            .and_then(Entry::part_of)
            .is_some_and(|id| which.skips_form_answer(id))
        {
            return Ok(false);
        }
        if which.hides() || which.conversation_only().is_some() {
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
        row.coordination = self.hidden(item, &row.entry)?;
        // A message read into another turn names that turn: written with the read, so it is
        // there from the row's first revision on.
        if matches!(row.entry, Entry::Message(_))
            && let Some((into, at)) = read_into_turn(self.log, item)?
        {
            row.read_into = self.turn_id(into)?.map(|trace_id| ReadInto {
                trace_id,
                turn: into,
                at,
            });
        }
        let split = if let Some(turn) = row.turn_number {
            let audience = self.mark(turn)?;
            if matches!(audience, TurnAudience::User { restamped: true })
                && let Some(restamp) = self.log.find(&restamp_key(row.entry.id()))?
                && restamp.kind == kind::AUDIENCE_ROW
            {
                newest = newest.max(restamp.seq);
            }
            matches!(
                self.mark(turn)?,
                TurnAudience::Agents { split: Some(_), .. }
            )
        } else {
            false
        };
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
                    if split
                        && row.coordination.is_none()
                        && self
                            .mark(call.turn)?
                            .sender(id, stored.seq, false)
                            .is_some()
                    {
                        continue;
                    }
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
            let mut row = self.row(&stored, entry)?;
            self.chat_row(&mut row, which)?;
            return Ok(Some(row));
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
    span: Span<'_>,
) -> Result<Vec<Versioned>, ActorError> {
    let mut reader = Reader::new(log).with_span(span).reading(which);
    let mut found: Vec<Versioned> = Vec::with_capacity(items.len());
    let mut last_tool_parent = None;
    for item in items {
        let previous_tool_parent = last_tool_parent.take();
        // A row named again: it is given again, as it is now. A hidden row the user joined is
        // news to every reader; a check-in sent again for the apps (`resend`) only to a chat,
        // not to the goal keeper or a delegate's start. A sending again that names no row
        // only marks that a pass of them was made.
        if item.kind == kind::AUDIENCE_ROW || item.kind == kind::RESENT_ROW {
            let id = String::from_utf8_lossy(&item.body);
            let news = item.kind == kind::AUDIENCE_ROW || matches!(which, Which::Chat { .. });
            if place == Place::Newest && news && !id.is_empty() {
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
        if place == Place::Newest
            && matches!(which, Which::Chat { .. })
            && let Entry::TurnEnd(end) = &entry
            && let Some(anchor) = reader.settled_anchor(end.turn)?
            && anchor.id != end.closes
        {
            let id = anchor.id.clone();
            found.retain(|known| known.entry.id() != id);
            found.extend(reader.stored(&id, which)?);
        }
        if let Some(changed) = entry.part_of() {
            if place == Place::Newest {
                if matches!(entry, Entry::Tool(_)) {
                    // stored() reads the latest version of every tool on this reply. Adjacent
                    // parts of the same reply cannot change its value or move another row.
                    if previous_tool_parent.as_deref() == Some(changed) {
                        last_tool_parent = previous_tool_parent;
                        continue;
                    }
                    last_tool_parent = Some(changed.to_owned());
                }
                found.retain(|known| known.entry.id() != changed);
                found.extend(reader.stored(changed, which)?);
            }
            continue;
        }
        // A row that decided an earlier reply was an answer: that reply entered the
        // conversation view with it, and changed.
        if place == Place::Newest
            && matches!(which, Which::Chat { .. })
            && let Some(earlier) = reader.decided_by(item, &entry)?
        {
            found.retain(|known| known.entry.id() != earlier);
            found.extend(reader.stored(&earlier, which)?);
        }
        if !reader.allows(item, &entry, which)? {
            continue;
        }
        let mut row = reader.row(item, entry)?;
        reader.chat_row(&mut row, which)?;
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

/// The chat-list preview of a hiding chat and its preview without unfinished drafts.
pub(crate) fn visible_previews(
    log: &dyn AgentLog,
    drafts: &[Draft],
    candidate: impl Fn(&Entry) -> bool,
    limit: usize,
) -> Result<(Option<Versioned>, Option<Versioned>), ActorError> {
    let mut reader = Reader::new(log);
    let mut head = None;
    let mut head_left = limit;
    for draft in drafts.iter().rev() {
        let row = draft.versioned();
        if head_left > 0 && candidate(&row.entry) {
            head_left -= 1;
            if reader.draft_mark(draft)?.is_none() {
                head = Some(row);
                break;
            }
        }
    }

    let mut cursor = Seq::MAX;
    let mut completed_left = limit;
    while completed_left > 0 {
        let items = log.read_before(cursor, kind::ROWS, CHUNK)?;
        let end = items.len() < CHUNK;
        for item in items.into_iter().rev() {
            cursor = item.seq;
            let Some(entry) = Entry::from_item(&item).map_err(|why| corrupt(&item, why))? else {
                continue;
            };
            if !candidate(&entry) {
                continue;
            }
            let hidden = reader.hidden(&item, &entry)?.is_some();
            if !hidden {
                let completed = reader.row(&item, entry)?;
                if head.is_none() && head_left > 0 {
                    head = Some(completed.clone());
                }
                return Ok((head, Some(completed)));
            }
            head_left = head_left.saturating_sub(1);
            completed_left -= 1;
            if completed_left == 0 {
                break;
            }
        }
        if end {
            break;
        }
    }
    Ok((head, None))
}

/// The row with this id as it is now, with its turn's end if it closed one.
pub(crate) fn one(log: &dyn AgentLog, id: &str) -> Result<Option<Versioned>, ActorError> {
    Reader::new(log).stored(id, Which::All)
}

/// Where a conversation begins and how far it has got: what was written up to `floor` is
/// of a conversation that ended when the agent was started again, and is no part of what a
/// chat shows.
#[derive(Clone, Copy, Default)]
pub(crate) struct Span<'a> {
    pub floor: Seq,
    pub head_revision: u64,
    pub imported_anchors: Option<&'a ImportedAnchors>,
    /// The actor's walks of its newest turns ([`AnchorCache`]).
    pub anchor_cache: Option<&'a AnchorCache>,
}

const COUNT_CAP: usize = 100;
const COUNT_ROW_CAP: usize = 400;

#[derive(Default)]
struct Counting {
    run: HiddenRun,
    rows: usize,
    turns: BTreeSet<u64>,
    imported_traces: BTreeSet<String>,
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
        } else if let Entry::Imported(imported) = entry {
            let trace = imported.row["trace_id"].as_str().unwrap_or_default();
            self.imported_traces.insert(trace.to_owned());
        }
        if !sender.is_empty() {
            self.run.senders.insert(sender);
        }
    }

    fn finish(mut self) -> Option<HiddenRun> {
        self.run.exchanges = self.turns.len() + self.imported_traces.len();
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
            let sender = if which.skips_form_answer(entry.id()) {
                None
            } else {
                reader.hidden(&item, &entry)?
            };
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
    span: Span<'_>,
    which: Which,
    tail: bool,
    contiguous: bool,
) -> Result<Gaps, ActorError> {
    if !which.hides() {
        return Ok((HashMap::new(), None));
    }
    let mut reader = Reader::new(log).with_span(span).reading(which);
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
            if let Some(sender) = reader.draft_mark(draft)? {
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
    span: Span<'_>,
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
    span: Span<'_>,
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
    let mut judge = Reader::new(log).with_span(span).reading(which);
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
    span: Span<'_>,
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
            shown.coordination = reader.draft_mark(draft)?;
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
    let (items, mut has_more) = items_before(log, (before, span.floor), stored_limit, which, span)?;
    let mut entries = rows(log, &items, Place::First, which, span)?;
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
        helper_chat: which.is_helper_chat(),
    })
}

/// The revision the conversation had at a time: that of the newest thing of it that was
/// written at or before `ts_ms`, and where the conversation begins when everything in it is
/// newer. What is newer than this revision ([`after`]) is what was written after that time.
/// A log is in the order it was written, so the times in it do not go back, and the place
/// is searched for and not read up to: a few items, however long the conversation.
pub(crate) fn revision_at(
    log: &dyn AgentLog,
    span: Span<'_>,
    ts_ms: i64,
) -> Result<u64, ActorError> {
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
    span: Span<'_>,
) -> Result<(Vec<Item>, bool, Option<u64>), ActorError> {
    let mut items: Vec<Item> = Vec::new();
    let mut cursor = None;
    let mut judge = Reader::new(log).with_span(span).reading(which);
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
    span: Span<'_>,
    limit: usize,
    which: Which,
) -> Result<Page, ActorError> {
    let head_revision = span.head_revision;
    let mut reader = Reader::new(log);
    let mut from = (revision / REVISION_STEP).max(span.floor);
    let (mut entries, latest_revision, has_more) = loop {
        let (items, has_more, cursor) = items_after(log, from, limit, which, span)?;
        let mut entries = rows(log, &items, Place::Newest, which, span)?;
        if !has_more {
            for draft in drafts {
                let mut shown = draft.versioned();
                shown.coordination = reader.draft_mark(draft)?;
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
        helper_chat: which.is_helper_chat(),
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
        for row in rows(log, &part, Place::Newest, Which::All, Span::default())? {
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

    #[allow(clippy::unwrap_used)]
    fn split_turn_with_note(note_after_steer: bool) -> clarp_kernel::journal::Recorder {
        split_turn(
            if note_after_steer {
                Noted::AfterSteer
            } else {
                Noted::BeforeSteer
            },
            true,
        )
    }

    /// Where the turn's note to the user is, in a turn for another agent the user wrote into.
    #[derive(Clone, Copy, PartialEq)]
    enum Noted {
        BeforeSteer,
        AfterSteer,
        AfterReplies,
    }

    #[allow(clippy::unwrap_used)]
    fn split_turn(noted: Noted, ended: bool) -> clarp_kernel::journal::Recorder {
        use clarp_kernel::agent::Command;
        use clarp_kernel::conversation::{Message, Note};
        use clarp_kernel::journal::{Incoming, Recorder};
        use clarp_kernel::steering::Join;
        use clarp_kernel::turn::{Admission, RunOutcome};
        use clarp_ports::log::LogEngine;
        use std::path::Path;

        let log = clarp_store_mem::MemEngine::new()
            .open(
                Path::new("/agents/steer-note"),
                clarp_codec::standard_set("zstd").unwrap(),
            )
            .unwrap();
        let mut recorder = Recorder::open(log).unwrap();
        let accept = |recorder: &mut Recorder, id: &str, origin: Origin, coordination, now| {
            let message = Message {
                id: id.into(),
                text: id.into(),
                origin: origin.clone(),
                ..Message::default()
            };
            recorder
                .accept_message(
                    Incoming {
                        key: Some(message.id.clone()),
                        body: serde_json::to_vec(&message).unwrap(),
                        origin,
                        admission: Admission::Queue,
                        coordination,
                        user_word: false,
                    },
                    now,
                )
                .unwrap()
                .turn
                .unwrap()
        };
        let into = accept(
            &mut recorder,
            "u-peer",
            Origin::Agent {
                agent_id: "peer".into(),
            },
            Some("peer".into()),
            100,
        );
        recorder
            .handle(Command::TurnStarted { turn: into }, 110)
            .unwrap();
        let followup = accept(&mut recorder, "u-steer", Origin::User, None, 120);
        let mut batch = recorder.batch();
        let note = Entry::Note(Note {
            id: "note-user".into(),
            turn: into.0,
            text: "An answer for the user.".into(),
            trace_id: None,
        });
        if noted == Noted::BeforeSteer {
            batch.item(note.to_item(125).unwrap());
        }
        batch
            .command(
                Command::Join {
                    turn: followup,
                    join: Join {
                        into,
                        uuid: "steer".into(),
                        recovery: None,
                        together: false,
                    },
                },
                130,
            )
            .unwrap();
        batch
            .command(Command::JoinRead { turn: followup }, 140)
            .unwrap();
        assert!(split(&mut batch, into, OpenRows::default(), 140).unwrap());
        if noted == Noted::AfterSteer {
            batch.item(note.to_item(145).unwrap());
        }
        for id in ["post-steer", "final-reply"] {
            let reply = Entry::Reply(Reply {
                id: id.into(),
                turn: into.0,
                text: id.into(),
                ..Reply::default()
            });
            batch.item(reply.to_item(150).unwrap());
        }
        if noted == Noted::AfterReplies {
            batch.item(note.to_item(155).unwrap());
        }
        if !ended {
            batch.commit().unwrap();
            return recorder;
        }
        batch
            .command(
                Command::TurnEnded {
                    turn: into,
                    outcome: RunOutcome::Completed,
                },
                160,
            )
            .unwrap();
        let mut end = TurnEnd::of(batch.agent().last().unwrap(), "final-reply").unwrap();
        end.note_count = 1;
        batch.item(Entry::TurnEnd(end).to_item(160).unwrap());
        batch.commit().unwrap();
        recorder
    }

    #[test]
    #[allow(clippy::unwrap_used)]
    fn a_final_reply_after_steer_stays_hidden_when_a_user_note_followed_the_steer() {
        let recorder = split_turn_with_note(true);
        let log = recorder.log();
        let final_reply = one(log, "final-reply").unwrap().unwrap();
        assert_eq!(final_reply.coordination.as_deref(), Some("peer"));
        let page = tail(log, &[], Span::default(), 20, Which::WithoutCoordination).unwrap();
        let ids: Vec<_> = page.entries.iter().map(|row| row.entry.id()).collect();
        // What the agent said after the note is the agents' again, not only its last words:
        // decided by place, never later.
        assert_eq!(ids, ["u-steer", "note-user"]);
        assert_eq!(page.hidden_after.unwrap().messages, 2);
    }

    /// Rule FU: whether a reply of a turn the user wrote into is the user's is the same
    /// while the turn runs and after it ended, wherever the note is: a row the user was
    /// shown is never hidden later, and none is shown and then taken away.
    #[test]
    #[allow(clippy::unwrap_used)]
    fn a_reply_of_a_split_turn_is_shown_or_hidden_the_same_before_and_after_its_end() {
        let seen = |noted: Noted, ended: bool| -> Vec<(String, bool)> {
            let recorder = split_turn(noted, ended);
            let log = recorder.log();
            ["post-steer", "final-reply"]
                .iter()
                .map(|id| {
                    let row = one(log, id).unwrap().unwrap();
                    ((*id).to_string(), row.coordination.is_none())
                })
                .collect()
        };
        for noted in [Noted::BeforeSteer, Noted::AfterSteer, Noted::AfterReplies] {
            assert_eq!(seen(noted, false), seen(noted, true));
        }
        let both = |shown: bool| {
            vec![
                ("post-steer".to_string(), shown),
                ("final-reply".to_string(), shown),
            ]
        };
        assert_eq!(seen(Noted::AfterSteer, true), both(false));
        assert_eq!(seen(Noted::AfterReplies, true), both(true));
        assert_eq!(seen(Noted::BeforeSteer, true), both(true));
    }

    #[test]
    #[allow(clippy::unwrap_used)]
    fn a_final_reply_after_steer_is_shown_when_no_user_note_followed_the_steer() {
        let recorder = split_turn_with_note(false);
        let log = recorder.log();
        let final_reply = one(log, "final-reply").unwrap().unwrap();
        assert!(final_reply.coordination.is_none());
        let page = tail(log, &[], Span::default(), 20, Which::WithoutCoordination).unwrap();
        let ids: Vec<_> = page.entries.iter().map(|row| row.entry.id()).collect();
        assert_eq!(ids, ["note-user", "u-steer", "post-steer", "final-reply"]);
        assert!(page.hidden_after.is_none());
    }

    #[test]
    #[allow(clippy::unwrap_used)]
    fn imported_settled_anchors_survive_a_filtered_page_boundary() {
        use clarp_kernel::conversation::Imported;
        use clarp_ports::log::LogEngine;
        use serde_json::json;
        use std::path::Path;

        let mut log = clarp_store_mem::MemEngine::new()
            .open(
                Path::new("/agents/moved"),
                clarp_codec::standard_set("zstd").unwrap(),
            )
            .unwrap();
        let rows = [
            (
                "prompt",
                json!({"role":"user","origin":"user","trace_id":"old"}),
            ),
            (
                "work",
                json!({"role":"assistant","text":"Looking","phase":"commentary","trace_id":"old"}),
            ),
            (
                "answer",
                json!({"role":"assistant","text":"Found it","phase":"final","trace_id":"old"}),
            ),
            (
                "marker",
                json!({"role":"assistant","origin":"system","text":"Stopped",
                "trace_id":"old","turn":{"status":"interrupted"}}),
            ),
            (
                "peer",
                json!({"role":"assistant","audience":"agents","text":"For an agent",
                "trace_id":"peer","turn":{"status":"completed"}}),
            ),
            (
                "empty-prompt",
                json!({"role":"user","origin":"oracle","trace_id":"empty"}),
            ),
            (
                "note",
                json!({"role":"assistant","kind":"note","text":"Posted","trace_id":"empty"}),
            ),
            (
                "tool",
                json!({"role":"assistant","tool_name":"Read","text":"Tool",
                "trace_id":"empty","turn":{"status":"completed"}}),
            ),
            (
                "replaced",
                json!({"role":"assistant","text":"Replaced","phase":"final",
                "trace_id":"replaced","turn":{"status":"replaced"}}),
            ),
            (
                "running",
                json!({"role":"assistant","text":"Still working","phase":"final",
                "trace_id":"running"}),
            ),
        ];
        let items: Vec<_> = rows
            .into_iter()
            .map(|(id, mut row)| {
                row["id"] = json!(id);
                Entry::Imported(Imported {
                    id: id.to_owned(),
                    row,
                })
                .to_item(0)
                .unwrap()
            })
            .collect();
        let head_revision = log.append(&items).unwrap() * REVISION_STEP;
        let anchors = ImportedAnchors::default();
        let span = Span {
            floor: 0,
            head_revision,
            imported_anchors: Some(&anchors),
            anchor_cache: None,
        };
        let chat = Which::chat(true).conversation_view(true);
        let newest = tail(log.as_ref(), &[], span, 1, chat).unwrap();
        assert_eq!(newest.entries[0].entry.id(), "note");
        assert_eq!(newest.entries[0].session_rows, Some(1));
        assert!(newest.has_more);

        let page = after(log.as_ref(), 2 * REVISION_STEP, &[], span, 1, chat).unwrap();
        assert_eq!(page.entries[0].entry.id(), "answer");
        assert_eq!(page.entries[0].session_rows, Some(1));
        assert!(is_conversation(
            &page.entries[0].entry,
            page.entries[0].session_rows
        ));
        assert_eq!(page.latest_revision, 3 * REVISION_STEP);
        let session = tail(log.as_ref(), &[], span, 20, Which::chat(true)).unwrap();
        for row in session
            .entries
            .iter()
            .filter(|row| matches!(row.entry.id(), "work" | "running" | "replaced"))
        {
            assert!(!is_conversation(&row.entry, row.session_rows));
        }
    }

    #[test]
    #[allow(clippy::unwrap_used)]
    fn imported_coordination_is_hidden_and_counted_by_its_source_trace() {
        use clarp_kernel::conversation::Imported;
        use clarp_ports::log::LogEngine;
        use serde_json::json;
        use std::path::Path;

        let mut log = clarp_store_mem::MemEngine::new()
            .open(
                Path::new("/agents/imported"),
                clarp_codec::standard_set("zstd").unwrap(),
            )
            .unwrap();
        let rows = [
            (
                "note",
                json!({"kind": "note", "audience": "user", "role": "assistant",
                "text": "For the user", "trace_id": "source-trace"}),
            ),
            (
                "prompt",
                json!({"audience": "agents", "role": "user", "text": "Question",
                "trace_id": "source-trace", "sender_agent_id": "ivy"}),
            ),
            (
                "tool",
                json!({"audience": "agents", "role": "assistant", "tool_name": "Read",
                "text": "Tool detail", "trace_id": "source-trace"}),
            ),
            (
                "reply",
                json!({"audience": "agents", "role": "assistant", "text": "Answer",
                "trace_id": "source-trace", "reply_to_agent_id": "ivy"}),
            ),
        ];
        let items: Vec<_> = rows
            .into_iter()
            .map(|(id, row)| {
                Entry::Imported(Imported {
                    id: id.to_owned(),
                    row,
                })
                .to_item(0)
                .unwrap()
            })
            .collect();
        let head = log.append(&items).unwrap() * REVISION_STEP;
        let span = Span {
            floor: 0,
            head_revision: head,
            ..Span::default()
        };
        let show = tail(log.as_ref(), &[], span, 4, Which::chat(true)).unwrap();
        assert_eq!(show.entries.len(), 4);
        assert_eq!(show.entries[1].entry.id(), "prompt");
        assert!(show.hidden_after.is_none());

        let hide = Which::chat(true).hide_coordination();
        let hidden = tail(log.as_ref(), &[], span, 1, hide).unwrap();
        assert_eq!(hidden.entries.len(), 1);
        assert_eq!(hidden.entries[0].entry.id(), "note");
        assert!(!hidden.has_more);
        let gap = hidden.hidden_after.unwrap();
        assert_eq!((gap.messages, gap.exchanges, gap.capped), (2, 1, false));
        assert_eq!(gap.senders, BTreeSet::from(["ivy".to_owned()]));
    }

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
        let writes = loosen_from(batch.log(), TurnId(1), Some(1), 300).unwrap();
        assert!(!writes.is_empty());
        for item in writes {
            batch.item(item);
        }
        batch.commit().unwrap();
        let span = Span {
            floor: 0,
            head_revision: recorder.conversation_seq() * REVISION_STEP,
            ..Span::default()
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
        assert!(
            loosen_from(recorder.log(), TurnId(1), Some(1), 400)
                .unwrap()
                .is_empty()
        );
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
