//! Engines that keep everything in memory: an agent's log, the fleet's small tables, and
//! the files agents publish.
//!
//! The engine is the "disk": a log opened twice in the same directory is the same log, so a test
//! or the simulator can drop a log, as a crash would, and open it again. Bodies are stored
//! through the codecs, exactly as a durable engine stores them.

use std::collections::{BTreeMap, HashMap};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, MutexGuard};

use clarp_ports::blobs::{BlobEngine, Blobs, is_name, not_a_name};
use clarp_ports::codec::{CodecSet, CodecTag};
use clarp_ports::kv::{KeyValue, KeyValueEngine, StoreError};
use clarp_ports::log::{AgentLog, Item, Kinds, LogEngine, LogError, NewItem, Seq};

struct Row {
    turn: Option<u64>,
    kind: u16,
    ts_ms: i64,
    key: Option<String>,
    tag: CodecTag,
    stored: Vec<u8>,
}

#[derive(Default)]
struct IndexedRows {
    items: Vec<Row>,
    latest: HashMap<String, usize>,
}

type Rows = Arc<Mutex<IndexedRows>>;

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
    fn rows(&self) -> Result<MutexGuard<'_, IndexedRows>, LogError> {
        self.rows.lock().map_err(|_| poisoned())
    }

    fn item(&self, index: usize, row: &Row) -> Result<Item, LogError> {
        Ok(Item {
            seq: index as Seq + 1,
            turn: row.turn,
            kind: row.kind,
            ts_ms: row.ts_ms,
            key: row.key.clone(),
            body: self.codecs.decode(row.tag, &row.stored)?,
        })
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
                key: item.key.clone(),
                tag,
                stored,
            });
        }
        let mut rows = self.rows()?;
        for row in encoded {
            if let Some(key) = &row.key {
                let position = rows.items.len();
                rows.latest.insert(key.clone(), position);
            }
            rows.items.push(row);
        }
        Ok(rows.items.len() as Seq)
    }

    fn last_seq(&self) -> Result<Seq, LogError> {
        Ok(self.rows()?.items.len() as Seq)
    }

    fn read_after(&self, after: Seq, kinds: Kinds, limit: usize) -> Result<Vec<Item>, LogError> {
        let rows = self.rows()?;
        let from = usize::try_from(after).unwrap_or(usize::MAX);
        let mut items = Vec::new();
        for (index, row) in rows.items.iter().enumerate().skip(from) {
            if items.len() >= limit {
                break;
            }
            if kinds.has(row.kind) {
                items.push(self.item(index, row)?);
            }
        }
        Ok(items)
    }

    fn read_before(&self, before: Seq, kinds: Kinds, limit: usize) -> Result<Vec<Item>, LogError> {
        let rows = self.rows()?;
        // The item at index i has position i + 1, so "before position p" is "index below p - 1".
        let end = usize::try_from(before.saturating_sub(1))
            .unwrap_or(usize::MAX)
            .min(rows.items.len());
        let mut items = Vec::new();
        for (index, row) in rows.items.iter().enumerate().take(end).rev() {
            if items.len() >= limit {
                break;
            }
            if kinds.has(row.kind) {
                items.push(self.item(index, row)?);
            }
        }
        items.reverse();
        Ok(items)
    }

    fn find(&self, key: &str) -> Result<Option<Item>, LogError> {
        let rows = self.rows()?;
        rows.latest
            .get(key)
            .map(|&index| self.item(index, &rows.items[index]))
            .transpose()
    }
}

type Tables = Arc<Mutex<BTreeMap<(String, String), Vec<u8>>>>;

/// The fleet's tables in memory. As with the log engine, the engine is the disk.
#[derive(Default)]
pub struct MemKeyValue {
    disks: Mutex<HashMap<PathBuf, Tables>>,
}

impl MemKeyValue {
    pub fn new() -> Self {
        Self::default()
    }
}

fn lost() -> StoreError {
    StoreError("a writer panicked while holding the in-memory store".to_string())
}

impl KeyValueEngine for MemKeyValue {
    fn name(&self) -> &'static str {
        "memory"
    }

    fn open(&self, path: &Path) -> Result<Box<dyn KeyValue>, StoreError> {
        let mut disks = self.disks.lock().map_err(|_| lost())?;
        let tables = disks.entry(path.to_path_buf()).or_default().clone();
        Ok(Box::new(MemTables { tables }))
    }
}

struct MemTables {
    tables: Tables,
}

impl KeyValue for MemTables {
    fn get(&self, table: &str, key: &str) -> Result<Option<Vec<u8>>, StoreError> {
        let tables = self.tables.lock().map_err(|_| lost())?;
        Ok(tables.get(&(table.to_string(), key.to_string())).cloned())
    }

    fn put(&mut self, table: &str, key: &str, value: &[u8]) -> Result<(), StoreError> {
        let mut tables = self.tables.lock().map_err(|_| lost())?;
        tables.insert((table.to_string(), key.to_string()), value.to_vec());
        Ok(())
    }

    fn put_together(&mut self, writes: &[clarp_ports::kv::Write]) -> Result<(), StoreError> {
        let mut tables = self.tables.lock().map_err(|_| lost())?;
        for (table, key, value) in writes {
            let entry = (table.clone(), key.clone());
            if let Some(value) = value {
                tables.insert(entry, value.clone());
            } else {
                tables.remove(&entry);
            }
        }
        Ok(())
    }

    fn delete(&mut self, table: &str, key: &str) -> Result<bool, StoreError> {
        let mut tables = self.tables.lock().map_err(|_| lost())?;
        Ok(tables
            .remove(&(table.to_string(), key.to_string()))
            .is_some())
    }

    fn list(&self, table: &str) -> Result<Vec<(String, Vec<u8>)>, StoreError> {
        let tables = self.tables.lock().map_err(|_| lost())?;
        Ok(tables
            .iter()
            .filter(|((in_table, _), _)| in_table == table)
            .map(|((_, key), value)| (key.clone(), value.clone()))
            .collect())
    }
}

type Files = Arc<Mutex<HashMap<String, Arc<Vec<u8>>>>>;

/// Whole files in memory. As with the log engine, the engine is the disk.
#[derive(Default)]
pub struct MemBlobs {
    disks: Mutex<HashMap<PathBuf, Files>>,
}

impl MemBlobs {
    pub fn new() -> Self {
        Self::default()
    }
}

impl BlobEngine for MemBlobs {
    fn name(&self) -> &'static str {
        "memory"
    }

    fn open(&self, path: &Path) -> Result<Arc<dyn Blobs>, StoreError> {
        let mut disks = self.disks.lock().map_err(|_| lost())?;
        let files = disks.entry(path.to_path_buf()).or_default().clone();
        Ok(Arc::new(MemFiles { files }))
    }
}

struct MemFiles {
    files: Files,
}

impl MemFiles {
    fn kept(&self, name: &str) -> Result<Option<Arc<Vec<u8>>>, StoreError> {
        Ok(self.files.lock().map_err(|_| lost())?.get(name).cloned())
    }
}

impl Blobs for MemFiles {
    fn names_with_prefix(&self, prefix: &str) -> Result<Vec<String>, StoreError> {
        if prefix.len() < 2 || !is_name(prefix) { return Err(not_a_name(prefix)); }
        Ok(self.files.lock().map_err(|_| lost())?.keys().filter(|key| key.starts_with(prefix)).cloned().collect())
    }

    fn remove(&self, name: &str) -> Result<(), StoreError> {
        if !is_name(name) { return Err(not_a_name(name)); }
        self.files.lock().map_err(|_| lost())?.remove(name);
        Ok(())
    }

    fn put(&self, name: &str, bytes: &[u8]) -> Result<(), StoreError> {
        if !is_name(name) {
            return Err(not_a_name(name));
        }
        let mut files = self.files.lock().map_err(|_| lost())?;
        files
            .entry(name.to_string())
            .or_insert_with(|| Arc::new(bytes.to_vec()));
        Ok(())
    }

    fn len(&self, name: &str) -> Result<Option<u64>, StoreError> {
        Ok(self.kept(name)?.map(|bytes| bytes.len() as u64))
    }

    fn read(&self, name: &str, offset: u64, most: usize) -> Result<Vec<u8>, StoreError> {
        let Some(bytes) = self.kept(name)? else {
            return Ok(Vec::new());
        };
        let from = usize::try_from(offset)
            .unwrap_or(usize::MAX)
            .min(bytes.len());
        let to = from.saturating_add(most).min(bytes.len());
        Ok(bytes[from..to].to_vec())
    }
}

#[cfg(test)]
mod batch_failure {
    use super::*;

    #[test]
    #[allow(clippy::unwrap_used)]
    fn a_refused_batch_leaves_memory_intact() {
        // Acquiring the shared map is the only fallible step; all rows change under that lock.
        let tables = Tables::default();
        let mut store = MemTables {
            tables: tables.clone(),
        };
        store.put("settings", "first", b"old").unwrap();
        let shared = tables.clone();
        assert!(
            std::thread::spawn(move || {
                let _held = shared.lock().unwrap();
                panic!("poison the store lock");
            })
            .join()
            .is_err()
        );
        assert!(
            store
                .put_together(&[
                    ("settings".into(), "first".into(), None),
                    ("settings".into(), "next".into(), Some(vec![1]))
                ])
                .is_err()
        );
        tables.clear_poison();
        assert_eq!(
            store.list("settings").unwrap(),
            vec![("first".to_owned(), b"old".to_vec())]
        );
    }
}
