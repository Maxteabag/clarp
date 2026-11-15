//! A log engine that keeps everything in memory.
//!
//! The engine is the "disk": a log opened twice in the same directory is the same log, so a test
//! or the simulator can drop a log, as a crash would, and open it again. Bodies are stored
//! through the codecs, exactly as a durable engine stores them.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, MutexGuard};

use clarp_ports::codec::{CodecSet, CodecTag};
use clarp_ports::log::{AgentLog, Item, LogEngine, LogError, NewItem, Seq};

struct Row {
    turn: Option<u64>,
    kind: u16,
    ts_ms: i64,
    tag: CodecTag,
    stored: Vec<u8>,
}

type Rows = Arc<Mutex<Vec<Row>>>;

#[derive(Default)]
pub struct MemEngine {
    disks: Mutex<HashMap<PathBuf, Rows>>,
}

impl MemEngine {
    pub fn new() -> Self {
        Self::default()
    }
}

fn poisoned() -> LogError {
    LogError::Storage("a writer panicked while holding the in-memory log".to_string())
}

impl LogEngine for MemEngine {
    fn name(&self) -> &'static str {
        "memory"
    }

    fn open(&self, agent_dir: &Path, codecs: CodecSet) -> Result<Box<dyn AgentLog>, LogError> {
        let mut disks = self.disks.lock().map_err(|_| poisoned())?;
        let rows = disks.entry(agent_dir.to_path_buf()).or_default().clone();
        Ok(Box::new(MemLog { rows, codecs }))
    }
}

struct MemLog {
    rows: Rows,
    codecs: CodecSet,
}

impl MemLog {
    fn rows(&self) -> Result<MutexGuard<'_, Vec<Row>>, LogError> {
        self.rows.lock().map_err(|_| poisoned())
    }

    fn read(&self, rows: &[Row], from_index: usize, count: usize) -> Result<Vec<Item>, LogError> {
        let mut items = Vec::with_capacity(count.min(rows.len()));
        for (offset, row) in rows.iter().enumerate().skip(from_index).take(count) {
            items.push(Item {
                seq: offset as Seq + 1,
                turn: row.turn,
                kind: row.kind,
                ts_ms: row.ts_ms,
                body: self.codecs.decode(row.tag, &row.stored)?,
            });
        }
        Ok(items)
    }
}

impl AgentLog for MemLog {
    fn append(&mut self, items: &[NewItem]) -> Result<Seq, LogError> {
        // Encode first: an item that cannot be stored must leave the log as it was.
        let mut encoded = Vec::with_capacity(items.len());
        for item in items {
            let (tag, stored) = self.codecs.encode(&item.body)?;
            encoded.push(Row {
                turn: item.turn,
                kind: item.kind,
                ts_ms: item.ts_ms,
                tag,
                stored,
            });
        }
        let mut rows = self.rows()?;
        rows.extend(encoded);
        Ok(rows.len() as Seq)
    }

    fn last_seq(&self) -> Result<Seq, LogError> {
        Ok(self.rows()?.len() as Seq)
    }

    fn read_after(&self, after: Seq, limit: usize) -> Result<Vec<Item>, LogError> {
        let rows = self.rows()?;
        let from = usize::try_from(after).unwrap_or(usize::MAX);
        self.read(&rows, from, limit)
    }

    fn read_tail(&self, limit: usize) -> Result<Vec<Item>, LogError> {
        let rows = self.rows()?;
        let from = rows.len().saturating_sub(limit);
        self.read(&rows, from, limit)
    }
}
