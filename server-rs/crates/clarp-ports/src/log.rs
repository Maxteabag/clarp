//! An agent's log as a port: the append-only history that only that agent's actor writes.
//!
//! The log is a sequence of items. An item has a kind, and may carry a key: the name of the thing
//! it is a version of (a message, a tool call). Nothing in a log is ever changed; a thing that
//! changes gets a newer item with the same key, and [`AgentLog::find`] returns the newest.

use std::fmt;
use std::path::Path;

use crate::codec::{CodecError, CodecSet};

/// The position of an item in one agent's log. The first item is 1; 0 means "before the first".
pub type Seq = u64;

/// Which kinds of item a read asks for: every kind from `min` to `max`, both included.
/// The kernel owns the meaning of the numbers; a store only compares them.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Kinds {
    pub min: u16,
    pub max: u16,
}

impl Kinds {
    pub const ALL: Self = Self {
        min: 0,
        max: u16::MAX,
    };

    pub const fn only(kind: u16) -> Self {
        Self {
            min: kind,
            max: kind,
        }
    }

    pub const fn range(min: u16, max: u16) -> Self {
        Self { min, max }
    }

    pub const fn has(self, kind: u16) -> bool {
        self.min <= kind && kind <= self.max
    }
}

/// An item on its way into a log.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct NewItem {
    /// The turn this item belongs to, when it belongs to one.
    pub turn: Option<u64>,
    /// What the body is. The kernel owns the meaning of the numbers.
    pub kind: u16,
    pub ts_ms: i64,
    /// The thing this item is a version of, when it is one.
    pub key: Option<String>,
    pub body: Vec<u8>,
}

impl NewItem {
    pub fn new(kind: u16, ts_ms: i64, body: Vec<u8>) -> Self {
        Self {
            turn: None,
            kind,
            ts_ms,
            key: None,
            body,
        }
    }

    pub fn in_turn(mut self, turn: u64) -> Self {
        self.turn = Some(turn);
        self
    }

    pub fn keyed(mut self, key: impl Into<String>) -> Self {
        self.key = Some(key.into());
        self
    }
}

/// An item as read back: what was appended, with its position.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Item {
    pub seq: Seq,
    pub turn: Option<u64>,
    pub kind: u16,
    pub ts_ms: i64,
    pub key: Option<String>,
    pub body: Vec<u8>,
}

#[derive(Debug)]
pub enum LogError {
    Codec(CodecError),
    /// The engine underneath failed. The text is for the operator.
    Storage(String),
}

impl fmt::Display for LogError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Codec(error) => write!(f, "{error}"),
            Self::Storage(why) => write!(f, "log storage failed: {why}"),
        }
    }
}

impl std::error::Error for LogError {}

impl From<CodecError> for LogError {
    fn from(error: CodecError) -> Self {
        Self::Codec(error)
    }
}

/// One agent's log. It has one writer, the agent's actor, which is why `append` takes `&mut self`.
pub trait AgentLog: Send {
    /// Appends the items in order, all or none, and returns the position of the last item in the
    /// log afterwards. When this returns `Ok`, the items survive a crash of the process.
    fn append(&mut self, items: &[NewItem]) -> Result<Seq, LogError>;

    /// The position of the newest item, 0 for an empty log.
    fn last_seq(&self) -> Result<Seq, LogError>;

    /// Up to `limit` items of these kinds after position `after`, oldest first. This is how a
    /// client catches up, and how an agent is rebuilt.
    fn read_after(&self, after: Seq, kinds: Kinds, limit: usize) -> Result<Vec<Item>, LogError>;

    /// The newest `limit` items of these kinds before position `before`, oldest first. This is
    /// how a chat opens (`before` is [`Seq::MAX`]) and how it scrolls back.
    fn read_before(&self, before: Seq, kinds: Kinds, limit: usize) -> Result<Vec<Item>, LogError>;

    /// The newest item that carries this key.
    fn find(&self, key: &str) -> Result<Option<Item>, LogError>;

    /// The newest `limit` items of these kinds, oldest first.
    fn read_tail(&self, kinds: Kinds, limit: usize) -> Result<Vec<Item>, LogError> {
        self.read_before(Seq::MAX, kinds, limit)
    }
}

/// A way of storing agent logs: SQLite, memory, whatever comes next.
pub trait LogEngine: Send + Sync {
    /// The name this engine has in configuration.
    fn name(&self) -> &'static str;

    /// Opens the log that lives in `agent_dir`, creating it when it is not there yet.
    /// Opening the same directory again returns the same log.
    fn open(&self, agent_dir: &Path, codecs: CodecSet) -> Result<Box<dyn AgentLog>, LogError>;
}
