//! An agent's log as a port: the append-only history that only that agent's actor writes.

use std::fmt;
use std::path::Path;

use crate::codec::{CodecError, CodecSet};

/// The position of an item in one agent's log. The first item is 1; 0 means "before the first".
pub type Seq = u64;

/// An item on its way into a log.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct NewItem {
    /// The turn this item belongs to, when it belongs to one.
    pub turn: Option<u64>,
    /// What the body is. The kernel owns the meaning of the numbers.
    pub kind: u16,
    pub ts_ms: i64,
    pub body: Vec<u8>,
}

/// An item as read back: what was appended, with its position.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Item {
    pub seq: Seq,
    pub turn: Option<u64>,
    pub kind: u16,
    pub ts_ms: i64,
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

    /// Up to `limit` items after position `after`, oldest first. This is how a client catches up.
    fn read_after(&self, after: Seq, limit: usize) -> Result<Vec<Item>, LogError>;

    /// The newest `limit` items, oldest first. This is how a chat opens.
    fn read_tail(&self, limit: usize) -> Result<Vec<Item>, LogError>;
}

/// A way of storing agent logs: SQLite, memory, whatever comes next.
pub trait LogEngine: Send + Sync {
    /// The name this engine has in configuration.
    fn name(&self) -> &'static str;

    /// Opens the log that lives in `agent_dir`, creating it when it is not there yet.
    /// Opening the same directory again returns the same log.
    fn open(&self, agent_dir: &Path, codecs: CodecSet) -> Result<Box<dyn AgentLog>, LogError>;
}
