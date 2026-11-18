//! The memory of the Oracle's threads.
//!
//! A call of the Oracle belongs to a thread: what one owner and the Oracle's contact said to
//! each other, across calls. The Host keeps of a thread which connection has it now, what
//! was kept of the talk, the actions that were begun for the user's words (each under a name
//! that is the same whenever its place is asked for, so that none is begun twice), the work
//! that was handed on in it, and what its voice was told of. One connection has a thread at
//! a time: a call that begins takes it, and from then on the call before can change nothing
//! in it. When a call begins the voice is handed an excerpt of all that ([`history`]): whole
//! records within a bound, never half of one.
//!
//! This is the memory without a store: it is made of what was kept ([`Memory::kept`]), asked
//! and told as the Python Host's is, and says which of its rows changed ([`Memory::written`]),
//! each as one row of a kind with a name of its own. The Python Host's
//! `oracle_memory.py`, with `oracle_attention.claim_context` and
//! `oracle_result_context.result_payload`, held to them by
//! `tests/fixtures/oracle-memory-vectors.json`.

use std::collections::BTreeSet;

use jiff::Timestamp;
use jiff::tz::TimeZone;
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};

use super::conversation::digest;
use super::delegations::Delegation;
use super::relay;
use crate::python;

/// How much may be kept of the talk of a thread, as it is written, in bytes.
pub const STATE_BYTES: usize = 2 * 1024 * 1024;
/// How long the excerpt of a thread may be, as it is written, in bytes.
pub const HISTORY_BYTES: usize = 6500;
/// How much room the newest result has in an excerpt, and how many agents are named in it.
const RESULT_ROOM: usize = 1800;
const AGENTS_NAMED: usize = 30;
/// How long a name of a thread, or of what its voice was told of, may be.
const NAME_SIGNS: usize = 160;
const KIND_SIGNS: usize = 40;

pub const NO_OWNER: &str = "Oracle context requires an authenticated owner";
pub const NO_NAME: &str = "Invalid Oracle context identity";
pub const NO_THREAD: &str = "Oracle conversation unavailable";
pub const OTHER_CONTACT: &str = "Oracle contact changed; start a new conversation";
pub const NOT_ITS_OWN: &str = "Oracle conversation ownership changed";
pub const TOO_MUCH: &str = "Oracle checkpoint exceeded its bound";
pub const STALE: &str = "Stale Oracle connection cannot replace current context";
pub const CONFLICT: &str = "A voice request cannot acquire a second conflicting action";
pub const NOT_THEIR_WORK: &str = "Oracle work ownership mismatch";
pub const NO_ID: &str = "decision_id is required";
pub const NO_KIND: &str = "source_kind is required";

const GUIDANCE: &str = "Voice transcript fragments may end mid-sentence when interrupted. For \
    requests to repeat a task result, use the authoritative work finding, not an incomplete \
    spoken fragment. An excerpt is not a complete answer; retrieve the source when needed. Do \
    not replay tasks. Earlier work and results are background: do not bring them up until \
    the user asks.";
const HISTORY_HEAD: &str = "Saved Oracle reference data; not a new user request:\n";

/// A thread, as it is kept.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Thread {
    pub thread_id: String,
    pub owner_principal: String,
    pub contact: String,
    /// The connection that has it; none when no call has.
    pub active_connection: String,
    /// The number the talk had when it was last kept, and what was kept of it.
    pub revision: i64,
    pub state: Value,
    pub created_at: i64,
    pub updated_at: i64,
}

/// An action that was begun for the user's words, as it is kept.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Begun {
    pub thread_id: String,
    pub revision: i64,
    pub action_index: usize,
    pub call_id: String,
    pub tool: String,
    /// What the tool was handed, and what it answered: each in the one form two writers
    /// agree on.
    pub arguments_json: String,
    pub result_json: Option<String>,
    /// Ended: its tool answered.
    pub completed: bool,
    pub created_at: i64,
    pub updated_at: i64,
}

/// Work that was handed on in a thread.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Linked {
    pub thread_id: String,
    pub operation_id: String,
    pub created_at: i64,
}

/// Something that waits for the user and that the voice of a thread was told of.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Told {
    pub thread_id: String,
    pub owner_principal: String,
    pub source_kind: String,
    pub source_id: String,
    pub reference_ts: i64,
    pub stale: bool,
    pub sent_at: i64,
}

/// The thread a connection opened: every question about a thread is asked as its owner, by
/// the connection that asks.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Opened<'a> {
    pub thread_id: &'a str,
    pub owner: &'a str,
    pub connection: &'a str,
}

/// What a call begins a thread with.
#[derive(Debug, Clone, Copy)]
pub struct Opening<'a> {
    pub owner: &'a str,
    pub contact: &'a str,
    /// The thread that is asked for by its name, where one is.
    pub thread_id: Option<&'a str>,
    pub connection: &'a str,
    /// A new thread is wanted.
    pub fresh: bool,
    /// The name of a thread that is made, where one is.
    pub new_id: &'a str,
}

/// The name of an action: the same whenever this place of this talk is asked for.
pub fn call_id(thread_id: &str, revision: i64, index: usize) -> String {
    let place = format!("{thread_id}:{revision}:{index}");
    format!("oracle-{}", python::first(&digest(&place), 40))
}

/// The name of the work an action hands on, as the tools of the fleet make it. `replacement`:
/// the work that takes the place of what was stopped.
pub fn operation_id(owner: &str, call_id: &str, replacement: bool) -> String {
    let mark = if replacement { ":follow:" } else { ":" };
    let named = format!("{owner}{mark}{call_id}");
    format!("rtc-{}", python::first(&digest(&named), 40))
}

/// Whether a text that says something can be the name of a thread: letters, digits and
/// `._:-`, not more than 160.
fn name(text: &str) -> bool {
    let signs = |sign: char| sign.is_ascii_alphanumeric() || "._:-".contains(sign);
    text.chars().count() <= NAME_SIGNS && text.chars().all(signs)
}

/// The kinds of row the memory is kept in: the threads, the actions that were begun, the
/// work of the threads, and what their voices were told of.
pub const THREADS: &str = "oracle-thread";
pub const BEGUN: &str = "oracle-begun";
pub const LINKED: &str = "oracle-work";
pub const TOLD: &str = "oracle-told";

/// A row of the memory as it is to be kept: of its kind, under its name. Without what it
/// holds, it is to be taken away.
#[derive(Debug, Clone, PartialEq)]
pub struct Kept {
    pub kind: &'static str,
    pub id: String,
    pub row: Option<Value>,
}

/// The memory of every thread of a Host.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Memory {
    threads: Vec<Thread>,
    begun: Vec<Begun>,
    linked: Vec<Linked>,
    told: Vec<Told>,
    /// The rows that changed since they were last asked for: their kinds and names.
    changed: BTreeSet<(&'static str, String)>,
}

impl Begun {
    fn name(&self) -> String {
        format!("{}:{}:{}", self.thread_id, self.revision, self.action_index)
    }
}

impl Linked {
    fn name(&self) -> String {
        format!("{}:{}", self.thread_id, self.operation_id)
    }
}

impl Told {
    fn name(&self) -> String {
        format!("{}:{}:{}", self.thread_id, self.source_kind, self.source_id)
    }
}

impl Memory {
    /// The memory as it was kept: every row of it, of whatever kind, in any order. A row
    /// that cannot be read is none.
    pub fn kept<'a>(rows: impl IntoIterator<Item = (&'a str, &'a Value)>) -> Self {
        let mut memory = Self::default();
        for (kind, row) in rows {
            let row = row.clone();
            match kind {
                THREADS => memory
                    .threads
                    .extend(serde_json::from_value::<Thread>(row).ok()),
                BEGUN => memory
                    .begun
                    .extend(serde_json::from_value::<Begun>(row).ok()),
                LINKED => memory
                    .linked
                    .extend(serde_json::from_value::<Linked>(row).ok()),
                TOLD => memory.told.extend(serde_json::from_value::<Told>(row).ok()),
                _ => {}
            }
        }
        // The thread that was made last is the last: by when it was made, then by its name.
        memory.threads.sort_by(|one, other| {
            let made = one.created_at.cmp(&other.created_at);
            made.then_with(|| one.thread_id.cmp(&other.thread_id))
        });
        memory
    }

    /// The rows that changed since this was last asked: to be written, or taken away.
    pub fn written(&mut self) -> Vec<Kept> {
        let changed = std::mem::take(&mut self.changed);
        let written = changed.into_iter().map(|(kind, id)| {
            let row = match kind {
                THREADS => {
                    let row = self.threads.iter().find(|row| row.thread_id == id);
                    row.map(|row| json!(row))
                }
                BEGUN => {
                    let row = self.begun.iter().find(|row| row.name() == id);
                    row.map(|row| json!(row))
                }
                LINKED => {
                    let row = self.linked.iter().find(|row| row.name() == id);
                    row.map(|row| json!(row))
                }
                _ => {
                    let row = self.told.iter().find(|row| row.name() == id);
                    row.map(|row| json!(row))
                }
            };
            Kept { kind, id, row }
        });
        written.collect()
    }

    pub fn threads(&self) -> &[Thread] {
        &self.threads
    }

    /// The actions that were begun, in the order of their threads and places.
    pub fn begun(&self) -> Vec<&Begun> {
        let mut begun: Vec<&Begun> = self.begun.iter().collect();
        let place = |row: &Begun| (row.revision, row.action_index);
        begun.sort_by(|one, other| {
            let threads = one.thread_id.cmp(&other.thread_id);
            threads.then_with(|| place(one).cmp(&place(other)))
        });
        begun
    }

    pub fn linked(&self) -> &[Linked] {
        &self.linked
    }

    pub fn told(&self) -> &[Told] {
        &self.told
    }

    /// A call begins: the thread it goes on in, or a new one, is this connection's from now
    /// on, and no other thread of this owner and contact is anybody's. Says the thread.
    ///
    /// A thread that is asked for by its name must be the owner's and of this contact. Where
    /// none is asked for and no new one is wanted, it is the thread a connection has, else
    /// the one that was last written, else the one that was made last.
    pub fn open(&mut self, opening: &Opening, now_ms: i64) -> Result<String, &'static str> {
        if opening.owner.is_empty() {
            return Err(NO_OWNER);
        }
        let of_these = |thread: &Thread| {
            thread.owner_principal == opening.owner && thread.contact == opening.contact
        };
        let named = opening.thread_id.filter(|thread_id| !thread_id.is_empty());
        let found = match named {
            Some(thread_id) => {
                if !name(thread_id) {
                    return Err(NO_NAME);
                }
                let own = |thread: &&Thread| {
                    thread.thread_id == thread_id && thread.owner_principal == opening.owner
                };
                let thread = self.threads.iter().find(own).ok_or(NO_THREAD)?;
                if thread.contact != opening.contact {
                    return Err(OTHER_CONTACT);
                }
                (!opening.fresh).then(|| thread_id.to_string())
            }
            None if opening.fresh => None,
            None => {
                let theirs = self
                    .threads
                    .iter()
                    .enumerate()
                    .filter(|(_, thread)| of_these(thread));
                let last = theirs.max_by_key(|(at, thread)| {
                    (!thread.active_connection.is_empty(), thread.updated_at, *at)
                });
                last.map(|(_, thread)| thread.thread_id.clone())
            }
        };
        for thread in self.threads.iter_mut().filter(|thread| of_these(thread)) {
            if !thread.active_connection.is_empty() {
                thread.active_connection.clear();
                self.changed.insert((THREADS, thread.thread_id.clone()));
            }
        }
        let thread_id = match found {
            Some(thread_id) => thread_id,
            None => {
                self.threads.push(Thread {
                    thread_id: opening.new_id.to_string(),
                    owner_principal: opening.owner.to_string(),
                    contact: opening.contact.to_string(),
                    active_connection: String::new(),
                    revision: 0,
                    state: json!({}),
                    created_at: now_ms,
                    updated_at: now_ms,
                });
                opening.new_id.to_string()
            }
        };
        let chosen = |thread: &&mut Thread| thread.thread_id == thread_id;
        if let Some(thread) = self.threads.iter_mut().find(chosen) {
            thread.active_connection = opening.connection.to_string();
            thread.updated_at = now_ms;
        }
        self.changed.insert((THREADS, thread_id.clone()));
        Ok(thread_id)
    }

    /// The thread a connection opened, where it is still its owner's. `active`: and where
    /// this connection still has it.
    fn owned(&self, opened: Opened, active: bool) -> Result<&Thread, &'static str> {
        let own = |thread: &&Thread| {
            thread.thread_id == opened.thread_id && thread.owner_principal == opened.owner
        };
        let thread = self.threads.iter().find(own).ok_or(NOT_ITS_OWN)?;
        if active && thread.active_connection != opened.connection {
            return Err(NOT_ITS_OWN);
        }
        Ok(thread)
    }

    /// Whether a connection still has the thread it opened. Nothing is done for a call whose
    /// thread a later call has.
    pub fn has(&self, opened: Opened) -> bool {
        self.owned(opened, true).is_ok()
    }

    /// What was kept of the talk of a thread, with the number it had.
    pub fn load(&self, opened: Opened) -> Result<Value, &'static str> {
        let thread = self.owned(opened, false)?;
        let mut state = thread.state.as_object().cloned().unwrap_or_default();
        state.insert("revision".to_string(), json!(thread.revision));
        Ok(Value::Object(state))
    }

    /// Keeps the talk of a thread as it is now: by the connection that has the thread, and
    /// within a bound.
    pub fn save(&mut self, opened: Opened, state: &Value, now_ms: i64) -> Result<(), &'static str> {
        if python::canonical(state).len() > STATE_BYTES {
            return Err(TOO_MUCH);
        }
        let has = |thread: &&mut Thread| {
            thread.thread_id == opened.thread_id
                && thread.owner_principal == opened.owner
                && thread.active_connection == opened.connection
        };
        let thread = self.threads.iter_mut().find(has).ok_or(STALE)?;
        thread.state = state.clone();
        thread.revision = state["revision"].as_i64().unwrap_or(0);
        thread.updated_at = now_ms;
        self.changed.insert((THREADS, opened.thread_id.to_string()));
        Ok(())
    }

    /// An action is begun for the words of the talk's number `revision`, at its place among
    /// the actions for them: written down once however often it is asked, and never as
    /// another action.
    pub fn admission(
        &mut self,
        opened: Opened,
        (revision, index): (i64, usize),
        (tool, arguments): (&str, &Value),
        now_ms: i64,
    ) -> Result<&Begun, &'static str> {
        self.owned(opened, true)?;
        let arguments_json = python::canonical(arguments);
        let at = |row: &Begun| {
            row.thread_id == opened.thread_id
                && row.revision == revision
                && row.action_index == index
        };
        let place = match self.begun.iter().position(at) {
            Some(place) => place,
            None => {
                self.begun.push(Begun {
                    thread_id: opened.thread_id.to_string(),
                    revision,
                    action_index: index,
                    call_id: call_id(opened.thread_id, revision, index),
                    tool: tool.to_string(),
                    arguments_json: arguments_json.clone(),
                    result_json: None,
                    completed: false,
                    created_at: now_ms,
                    updated_at: now_ms,
                });
                let made = self.begun.len() - 1;
                self.changed.insert((BEGUN, self.begun[made].name()));
                made
            }
        };
        let row = &self.begun[place];
        if row.tool != tool || row.arguments_json != arguments_json {
            return Err(CONFLICT);
        }
        Ok(row)
    }

    /// The tool of an action answered: the action is ended with its first answer, and the
    /// work that answer names is the thread's. `owner_of`: whose a delegation is, by its id.
    pub fn finish(
        &mut self,
        opened: Opened,
        (revision, index): (i64, usize),
        result: &Value,
        owner_of: &dyn Fn(&str) -> Option<String>,
        now_ms: i64,
    ) -> Result<(), &'static str> {
        self.owned(opened, true)?;
        let at = |row: &&mut Begun| {
            row.thread_id == opened.thread_id
                && row.revision == revision
                && row.action_index == index
        };
        if let Some(row) = self.begun.iter_mut().find(at).filter(|row| !row.completed) {
            row.completed = true;
            row.result_json = Some(python::canonical(result));
            row.updated_at = now_ms;
            self.changed.insert((BEGUN, row.name()));
        }
        match result["operation_id"].as_str().filter(|id| !id.is_empty()) {
            Some(operation_id) => self.link(opened, operation_id, owner_of, now_ms),
            None => Ok(()),
        }
    }

    /// Work is the thread's: where it is its owner's.
    pub fn link(
        &mut self,
        opened: Opened,
        operation_id: &str,
        owner_of: &dyn Fn(&str) -> Option<String>,
        now_ms: i64,
    ) -> Result<(), &'static str> {
        self.owned(opened, true)?;
        if owner_of(operation_id).as_deref() != Some(opened.owner) {
            return Err(NOT_THEIR_WORK);
        }
        let there =
            |row: &Linked| row.thread_id == opened.thread_id && row.operation_id == operation_id;
        if !self.linked.iter().any(there) {
            let linked = Linked {
                thread_id: opened.thread_id.to_string(),
                operation_id: operation_id.to_string(),
                created_at: now_ms,
            };
            self.changed.insert((LINKED, linked.name()));
            self.linked.push(linked);
        }
        Ok(())
    }

    /// The work of a thread, as the book of delegations has it, the newest first. Work is a
    /// thread's only where it is its owner's ([`Self::link`]).
    pub fn work<'a>(
        &self,
        opened: Opened,
        book: &'a [Delegation],
    ) -> Result<Vec<&'a Delegation>, &'static str> {
        self.owned(opened, false)?;
        let of_thread = |row: &&Delegation| {
            let linked = |link: &Linked| {
                link.thread_id == opened.thread_id && link.operation_id == row.delegation_id
            };
            self.linked.iter().any(linked)
        };
        let mut work: Vec<&Delegation> = book.iter().filter(of_thread).collect();
        work.sort_by(|one, other| {
            let newest = other.created_at.cmp(&one.created_at);
            newest.then_with(|| other.delegation_id.cmp(&one.delegation_id))
        });
        Ok(work)
    }

    /// The actions that were begun in a thread, in the order of their places: all of them,
    /// or those for one number of the talk.
    pub fn admissions(
        &self,
        opened: Opened,
        revision: Option<i64>,
    ) -> Result<Vec<&Begun>, &'static str> {
        self.owned(opened, false)?;
        let wanted = |row: &&Begun| {
            row.thread_id == opened.thread_id
                && revision.is_none_or(|revision| row.revision == revision)
        };
        Ok(self.begun().into_iter().filter(wanted).collect())
    }

    /// An action whose receipt was lost is ended by the work it is known to have handed on:
    /// its name is the action's own. A stop without work to take its place is never taken
    /// for done.
    pub fn reconcile(
        &mut self,
        opened: Opened,
        book: &[Delegation],
        now_ms: i64,
    ) -> Result<(), &'static str> {
        let owner_of = |operation_id: &str| {
            let found = book.iter().find(|row| row.delegation_id == operation_id);
            found.map(|row| row.owner_principal.clone())
        };
        let begun: Vec<Begun> = self
            .admissions(opened, None)?
            .into_iter()
            .cloned()
            .collect();
        for row in begun {
            let place = (row.revision, row.action_index);
            if row.completed {
                let result = row.result_json.as_deref().unwrap_or("null");
                let result: Value = serde_json::from_str(result).unwrap_or(Value::Null);
                if let Some(operation_id) =
                    result["operation_id"].as_str().filter(|id| !id.is_empty())
                {
                    self.link(opened, operation_id, &owner_of, now_ms)?;
                }
                continue;
            }
            let arguments: Value = serde_json::from_str(&row.arguments_json).unwrap_or(Value::Null);
            let operation_id = match row.tool.as_str() {
                "delegate_to_agent" | "investigate_with_oracle" => {
                    operation_id(opened.owner, &row.call_id, false)
                }
                "cancel_agent" if python::truthy(&arguments["request"]) => {
                    operation_id(opened.owner, &row.call_id, true)
                }
                _ => continue,
            };
            let handed = book.iter().find(|work| work.delegation_id == operation_id);
            if let Some(work) = handed.filter(|work| work.owner_principal == opened.owner) {
                let result = json!({ "status": work.status.word(), "operation_id": operation_id });
                self.finish(opened, place, &result, &owner_of, now_ms)?;
            }
        }
        Ok(())
    }

    /// The voice of a thread is told of something that waits for the user: once in a thread.
    /// Whether this is the first time.
    pub fn claim(
        &mut self,
        opened: Opened,
        (kind, id): (&str, &str),
        (reference_ts, stale): (i64, bool),
        now_ms: i64,
    ) -> Result<bool, &'static str> {
        let (kind, id) = told_as(kind, id)?;
        self.owned(opened, true)?;
        let there = |row: &Told| {
            row.thread_id == opened.thread_id && row.source_kind == kind && row.source_id == id
        };
        if self.told.iter().any(there) {
            return Ok(false);
        }
        let told = Told {
            thread_id: opened.thread_id.to_string(),
            owner_principal: opened.owner.to_string(),
            source_kind: kind.to_string(),
            source_id: id.to_string(),
            reference_ts,
            stale,
            sent_at: now_ms,
        };
        self.changed.insert((TOLD, told.name()));
        self.told.push(told);
        Ok(true)
    }

    /// The voice could not be told after all: it may be told again.
    pub fn release(
        &mut self,
        opened: Opened,
        (kind, id): (&str, &str),
    ) -> Result<(), &'static str> {
        let id = python::strip(id);
        if id.is_empty() {
            return Err(NO_ID);
        }
        let kind = python::first(python::strip(kind), KIND_SIGNS);
        self.owned(opened, true)?;
        let of_it = |row: &Told| {
            row.thread_id == opened.thread_id
                && row.owner_principal == opened.owner
                && row.source_kind == kind
                && row.source_id == id
        };
        let gone = self.told.iter().filter(|row| of_it(row)).map(Told::name);
        self.changed.extend(gone.map(|name| (TOLD, name)));
        self.told.retain(|row| !of_it(row));
        Ok(())
    }

    /// What the voice of a thread was told of, each as its kind, a colon and its id: what a
    /// call of the thread begins with.
    pub fn told_of(&self, thread_id: &str) -> Vec<String> {
        let of_thread = self.told.iter().filter(|row| row.thread_id == thread_id);
        of_thread
            .map(|row| format!("{}:{}", row.source_kind, row.source_id))
            .collect()
    }
}

/// The kind and the id something is told under: without the blanks around them.
fn told_as<'a>(kind: &'a str, id: &'a str) -> Result<(&'a str, &'a str), &'static str> {
    let id = python::strip(id);
    if id.is_empty() {
        return Err(NO_ID);
    }
    let kind = python::first(python::strip(kind), KIND_SIGNS);
    if kind.is_empty() {
        return Err(NO_KIND);
    }
    Ok((kind, id))
}

/// The zone of this name, as the table of zones has it; the world's own time where it has
/// none of that name. What a Host tells the times of an excerpt in: the zone of its machine.
pub fn zone_named(name: &str) -> TimeZone {
    TimeZone::get(name).unwrap_or(TimeZone::UTC)
}

/// How long before now something happened, in a few words.
pub fn age_phrase(ms: i64, now_ms: i64) -> String {
    let seconds = (now_ms - ms).max(0) / 1000;
    for (unit, size) in [("day", 86_400), ("hour", 3600), ("minute", 60)] {
        if seconds >= size {
            let count = seconds / size;
            let many = if count == 1 { "" } else { "s" };
            return format!("{count} {unit}{many} ago");
        }
    }
    "less than a minute ago".to_string()
}

/// A moment as it is written for a voice: on the clocks of a zone, to the second, with how
/// far those clocks are from the world's. `None`: no clock shows it.
pub fn iso_time(ms: i64, zone: &TimeZone) -> Option<String> {
    let moment = Timestamp::from_millisecond(ms).ok()?;
    let shown = moment.to_zoned(zone.clone());
    Some(shown.strftime("%Y-%m-%dT%H:%M:%S%:z").to_string())
}

/// A record with when it happened, told as a time and as how long ago. One without a time
/// is as it is, and one whose time no clock shows is without it.
fn dated(
    mut row: Map<String, Value>,
    key: &str,
    now_ms: i64,
    zone: &TimeZone,
) -> Map<String, Value> {
    let Some(ms) = row.get(key).and_then(Value::as_i64) else {
        return row;
    };
    match iso_time(ms, zone) {
        Some(time) => {
            row.insert(key.to_string(), json!(time));
            row.insert("age".to_string(), json!(age_phrase(ms, now_ms)));
        }
        None => {
            row.shift_remove(key);
        }
    }
    row
}

/// What of a result is shown in an excerpt: what its agent marked to be spoken, else the
/// whole, cut back to whole sentences until the record has room, and said to be cut.
pub fn result_payload(row: &Delegation, room: usize) -> Map<String, Value> {
    let asked = row.request_text.split("<oracle-reference-data>").next();
    let asked = python::strip(asked.unwrap_or(""));
    let mut finding = relay::result_text(&row.result_text, &row.error);
    let mut cut = false;
    let record = |finding: &str, cut: bool| {
        let mut record = Map::new();
        record.insert("operation_id".to_string(), json!(row.delegation_id));
        record.insert("agent".to_string(), json!(row.session));
        record.insert("status".to_string(), json!(row.status.word()));
        record.insert("finding".to_string(), json!(finding));
        record.insert("finding_excerpt".to_string(), json!(cut));
        record.insert(
            "request_excerpt".to_string(),
            json!(python::first(asked, 120)),
        );
        let longer = asked.chars().count() > 120;
        record.insert("request_excerpt_truncated".to_string(), json!(longer));
        record
    };
    let written = |record: &Map<String, Value>| python::compact(&Value::Object(record.clone()));
    while !finding.is_empty() && written(&record(&finding, cut)).chars().count() > room {
        // A whole sentence less; of one sentence that is too long, eighty signs less.
        let ends = [". ", "! ", "? "].map(|end| finding.rfind(end));
        finding = match ends.into_iter().flatten().max().filter(|end| *end > 0) {
            Some(end) => finding[..=end].to_string(),
            None => {
                let signs = finding.chars().count().saturating_sub(80);
                python::first(&finding, signs).to_string()
            }
        };
        cut = true;
    }
    record(&finding, cut)
}

/// What a thread is to its voice when a call begins.
#[derive(Debug, Clone, Copy)]
pub struct Excerpt<'a> {
    pub thread_id: &'a str,
    /// What was kept of the talk.
    pub state: &'a Value,
    /// The work of the thread, the newest first ([`Memory::work`]).
    pub work: &'a [&'a Delegation],
    /// What the user added to the thread.
    pub contexts: &'a [Value],
    /// The agents there are and the Oracle's contact, as the tools of the fleet list them.
    pub roster: &'a Value,
}

/// The excerpt of a thread a call begins with: the newest result first, then the talk from
/// its end, what the user added, and the other work, each record whole or not at all.
pub fn history(excerpt: &Excerpt, now_ms: i64, zone: &TimeZone) -> Value {
    let over = |row: &&&Delegation| {
        !row.status.active() && !(row.result_text.is_empty() && row.error.is_empty())
    };
    let reserved = excerpt.work.iter().find(over).or(excerpt.work.first());
    let agents = excerpt.roster["agents"]
        .as_array()
        .map_or(&[][..], Vec::as_slice);
    let names: Vec<Value> = agents
        .iter()
        .take(AGENTS_NAMED)
        .map(|agent| json!({ "name": agent["name"], "session": agent["session"] }))
        .collect();
    let mut payload = Map::new();
    payload.insert("thread_id".to_string(), json!(excerpt.thread_id));
    payload.insert("now".to_string(), json!(iso_time(now_ms, zone)));
    payload.insert("reference_only".to_string(), json!(true));
    payload.insert("await_current_request".to_string(), json!(true));
    for list in ["recent_conversation", "work", "user_context"] {
        payload.insert(list.to_string(), json!([]));
    }
    payload.insert("replay_guidance".to_string(), json!(GUIDANCE));
    payload.insert(
        "roster".to_string(),
        json!({
            "agents": names,
            "oracle_contact": excerpt.roster["oracle_contact"],
            "is_excerpt": agents.len() > AGENTS_NAMED,
        }),
    );
    let with_time = |row: Map<String, Value>, of: &Delegation| {
        let mut row = row;
        row.insert("updated_at".to_string(), json!(of.updated_at));
        dated(row, "updated_at", now_ms, zone)
    };
    let newest: Vec<Map<String, Value>> = reserved
        .iter()
        .map(|row| with_time(result_payload(row, RESULT_ROOM), row))
        .collect();
    let said = excerpt.state["fragments"]
        .as_array()
        .map_or(&[][..], Vec::as_slice);
    let said: Vec<Map<String, Value>> = said
        .iter()
        .rev()
        .filter_map(|row| row.as_object().cloned())
        .map(|row| dated(row, "at", now_ms, zone))
        .collect();
    let added: Vec<Map<String, Value>> = excerpt
        .contexts
        .iter()
        .filter_map(|row| row.as_object().cloned())
        .collect();
    let other = excerpt
        .work
        .iter()
        .filter(|row| reserved.is_none_or(|reserved| reserved.delegation_id != row.delegation_id));
    let other: Vec<Map<String, Value>> = other
        .map(|row| {
            let answer = if row.result_text.is_empty() {
                &row.error
            } else {
                &row.result_text
            };
            let mut record = Map::new();
            record.insert("operation_id".to_string(), json!(row.delegation_id));
            record.insert("agent".to_string(), json!(row.session));
            record.insert("status".to_string(), json!(row.status.word()));
            record.insert("request".to_string(), json!(row.request_text));
            record.insert("result".to_string(), json!(answer));
            with_time(record, row)
        })
        .collect();
    let lists = [
        ("work", newest),
        ("recent_conversation", said),
        ("user_context", added),
        ("work", other),
    ];
    for (list, rows) in lists {
        for row in rows {
            let mut longer = payload.clone();
            if let Some(Value::Array(there)) = longer.get_mut(list) {
                there.push(Value::Object(row));
            }
            if python::canonical(&Value::Object(longer.clone())).len() <= HISTORY_BYTES {
                payload = longer;
            }
        }
    }
    if let Some(Value::Array(said)) = payload.get_mut("recent_conversation") {
        said.reverse();
    }
    payload.insert("history_is_excerpt".to_string(), json!(true));
    let text = format!(
        "{HISTORY_HEAD}{}",
        python::canonical(&Value::Object(payload))
    );
    json!([{
        "type": "message",
        "role": "user",
        "content": [{ "type": "input_text", "text": text }],
    }])
}
