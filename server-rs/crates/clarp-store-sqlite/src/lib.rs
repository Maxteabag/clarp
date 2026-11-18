//! The SQLite log engine: `log.db` in the agent's directory.
//!
//! One table, clustered by position, append-only. The newest page of a conversation is the end
//! of one small file, so reading it costs the same whatever the other agents have written.
//! `synchronous=FULL` in WAL mode: when `append` returns, the items are on disk. That is rule 1
//! (an accepted message is never lost); the kernel batches appends rather than weaken it.

use std::path::Path;

use clarp_ports::codec::{CodecSet, CodecTag};
use clarp_ports::kv::{KeyValue, KeyValueEngine, StoreError};
use clarp_ports::log::{AgentLog, Item, Kinds, LogEngine, LogError, NewItem, Seq};
use rusqlite::{Connection, Params, params};

/// The name of the log file inside an agent's directory.
pub const LOG_FILE: &str = "log.db";

const SCHEMA_VERSION: i64 = 1;

const SCHEMA: &str = "
CREATE TABLE IF NOT EXISTS item(
    seq   INTEGER PRIMARY KEY,
    turn  INTEGER,
    kind  INTEGER NOT NULL,
    ts    INTEGER NOT NULL,
    key   TEXT,
    codec INTEGER NOT NULL,
    body  BLOB NOT NULL
) STRICT;
CREATE INDEX IF NOT EXISTS item_key ON item(key, seq) WHERE key IS NOT NULL;
";

const COLUMNS: &str = "seq, turn, kind, ts, key, codec, body";

#[derive(Debug, Default, Clone, Copy)]
pub struct SqliteEngine;

impl SqliteEngine {
    pub fn new() -> Self {
        Self
    }
}

fn storage(error: rusqlite::Error) -> LogError {
    LogError::Storage(error.to_string())
}

impl LogEngine for SqliteEngine {
    fn name(&self) -> &'static str {
        "sqlite"
    }

    fn open(&self, agent_dir: &Path, codecs: CodecSet) -> Result<Box<dyn AgentLog>, LogError> {
        std::fs::create_dir_all(agent_dir)
            .map_err(|e| LogError::Storage(format!("{}: {e}", agent_dir.display())))?;
        let conn = Connection::open(agent_dir.join(LOG_FILE)).map_err(storage)?;
        conn.execute_batch("PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;")
            .map_err(storage)?;
        let version: i64 = conn
            .query_row("PRAGMA user_version", [], |row| row.get(0))
            .map_err(storage)?;
        match version {
            0 => conn
                .execute_batch(&format!("{SCHEMA} PRAGMA user_version={SCHEMA_VERSION};"))
                .map_err(storage)?,
            SCHEMA_VERSION => {}
            newer => {
                return Err(LogError::Storage(format!(
                    "{} has schema {newer}, this build reads schema {SCHEMA_VERSION}",
                    agent_dir.join(LOG_FILE).display()
                )));
            }
        }
        Ok(Box::new(SqliteLog { conn, codecs }))
    }
}

struct SqliteLog {
    conn: Connection,
    codecs: CodecSet,
}

struct Stored {
    seq: i64,
    turn: Option<i64>,
    kind: i64,
    ts_ms: i64,
    key: Option<String>,
    codec: i64,
    body: Vec<u8>,
}

impl SqliteLog {
    fn item(&self, row: Stored) -> Result<Item, LogError> {
        let tag =
            CodecTag(u16::try_from(row.codec).map_err(|_| {
                LogError::Storage(format!("item {} has codec {}", row.seq, row.codec))
            })?);
        Ok(Item {
            seq: row.seq as Seq,
            turn: row.turn.map(|turn| turn as u64),
            kind: u16::try_from(row.kind).map_err(|_| {
                LogError::Storage(format!("item {} has kind {}", row.seq, row.kind))
            })?,
            ts_ms: row.ts_ms,
            key: row.key,
            body: self.codecs.decode(tag, &row.body)?,
        })
    }

    fn select(&self, sql: &str, params: impl Params) -> Result<Vec<Item>, LogError> {
        let mut statement = self.conn.prepare_cached(sql).map_err(storage)?;
        let rows = statement
            .query_map(params, |row| {
                Ok(Stored {
                    seq: row.get(0)?,
                    turn: row.get(1)?,
                    kind: row.get(2)?,
                    ts_ms: row.get(3)?,
                    key: row.get(4)?,
                    codec: row.get(5)?,
                    body: row.get(6)?,
                })
            })
            .map_err(storage)?;
        let mut items = Vec::new();
        for row in rows {
            items.push(self.item(row.map_err(storage)?)?);
        }
        Ok(items)
    }
}

fn as_limit(limit: usize) -> i64 {
    i64::try_from(limit).unwrap_or(i64::MAX)
}

fn as_position(seq: Seq) -> i64 {
    i64::try_from(seq).unwrap_or(i64::MAX)
}

impl AgentLog for SqliteLog {
    fn append(&mut self, items: &[NewItem]) -> Result<Seq, LogError> {
        let transaction = self.conn.transaction().map_err(storage)?;
        let mut seq: i64 = transaction
            .query_row("SELECT COALESCE(MAX(seq), 0) FROM item", [], |row| {
                row.get(0)
            })
            .map_err(storage)?;
        {
            let mut insert = transaction
                .prepare_cached("INSERT INTO item(seq, turn, kind, ts, key, codec, body) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)")
                .map_err(storage)?;
            for item in items {
                seq += 1;
                let (tag, stored) = self.codecs.encode(&item.body)?;
                let turn = item.turn.map(|turn| turn as i64);
                insert
                    .execute(params![
                        seq,
                        turn,
                        i64::from(item.kind),
                        item.ts_ms,
                        item.key,
                        i64::from(tag.0),
                        stored
                    ])
                    .map_err(storage)?;
            }
        }
        transaction.commit().map_err(storage)?;
        Ok(seq as Seq)
    }

    fn last_seq(&self) -> Result<Seq, LogError> {
        let seq: i64 = self
            .conn
            .query_row("SELECT COALESCE(MAX(seq), 0) FROM item", [], |row| {
                row.get(0)
            })
            .map_err(storage)?;
        Ok(seq as Seq)
    }

    fn read_after(&self, after: Seq, kinds: Kinds, limit: usize) -> Result<Vec<Item>, LogError> {
        self.select(
            &format!("SELECT {COLUMNS} FROM item WHERE seq > ?1 AND kind BETWEEN ?2 AND ?3 ORDER BY seq LIMIT ?4"),
            params![
                as_position(after),
                i64::from(kinds.min),
                i64::from(kinds.max),
                as_limit(limit)
            ],
        )
    }

    fn read_before(&self, before: Seq, kinds: Kinds, limit: usize) -> Result<Vec<Item>, LogError> {
        // The newest `limit` by position, then oldest first: a range read at the end of the file.
        self.select(
            &format!("SELECT {COLUMNS} FROM (SELECT {COLUMNS} FROM item WHERE seq < ?1 AND kind BETWEEN ?2 AND ?3 ORDER BY seq DESC LIMIT ?4) ORDER BY seq"),
            params![
                as_position(before),
                i64::from(kinds.min),
                i64::from(kinds.max),
                as_limit(limit)
            ],
        )
    }

    fn find(&self, key: &str) -> Result<Option<Item>, LogError> {
        let found = self.select(
            &format!("SELECT {COLUMNS} FROM item WHERE key = ?1 ORDER BY seq DESC LIMIT 1"),
            params![key],
        )?;
        Ok(found.into_iter().next())
    }
}

const KV_SCHEMA_VERSION: i64 = 1;

const KV_SCHEMA: &str = "
CREATE TABLE IF NOT EXISTS kv(
    tbl   TEXT NOT NULL,
    key   TEXT NOT NULL,
    value BLOB NOT NULL,
    PRIMARY KEY(tbl, key)
) STRICT, WITHOUT ROWID;
";

/// The fleet's tables in one SQLite file. Durable as the log is: a `put` that returned is on disk.
#[derive(Debug, Default, Clone, Copy)]
pub struct SqliteKeyValue;

impl SqliteKeyValue {
    pub fn new() -> Self {
        Self
    }
}

fn failed(error: rusqlite::Error) -> StoreError {
    StoreError(error.to_string())
}

impl KeyValueEngine for SqliteKeyValue {
    fn name(&self) -> &'static str {
        "sqlite"
    }

    fn open(&self, path: &Path) -> Result<Box<dyn KeyValue>, StoreError> {
        if let Some(dir) = path.parent() {
            std::fs::create_dir_all(dir)
                .map_err(|e| StoreError(format!("{}: {e}", dir.display())))?;
        }
        let conn = Connection::open(path).map_err(failed)?;
        conn.execute_batch("PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;")
            .map_err(failed)?;
        let version: i64 = conn
            .query_row("PRAGMA user_version", [], |row| row.get(0))
            .map_err(failed)?;
        match version {
            0 => conn
                .execute_batch(&format!(
                    "{KV_SCHEMA} PRAGMA user_version={KV_SCHEMA_VERSION};"
                ))
                .map_err(failed)?,
            KV_SCHEMA_VERSION => {}
            newer => {
                return Err(StoreError(format!(
                    "{} has schema {newer}, this build reads schema {KV_SCHEMA_VERSION}",
                    path.display()
                )));
            }
        }
        Ok(Box::new(SqliteTables { conn }))
    }
}

struct SqliteTables {
    conn: Connection,
}

impl KeyValue for SqliteTables {
    fn get(&self, table: &str, key: &str) -> Result<Option<Vec<u8>>, StoreError> {
        let mut statement = self
            .conn
            .prepare_cached("SELECT value FROM kv WHERE tbl = ?1 AND key = ?2")
            .map_err(failed)?;
        let mut rows = statement.query(params![table, key]).map_err(failed)?;
        match rows.next().map_err(failed)? {
            Some(row) => Ok(Some(row.get(0).map_err(failed)?)),
            None => Ok(None),
        }
    }

    fn put(&mut self, table: &str, key: &str, value: &[u8]) -> Result<(), StoreError> {
        self.conn
            .prepare_cached("INSERT INTO kv(tbl, key, value) VALUES (?1, ?2, ?3) ON CONFLICT(tbl, key) DO UPDATE SET value = excluded.value")
            .map_err(failed)?
            .execute(params![table, key, value])
            .map_err(failed)?;
        Ok(())
    }

    fn put_together(&mut self, writes: &[clarp_ports::kv::Write]) -> Result<(), StoreError> {
        let tx = self.conn.transaction().map_err(failed)?;
        {
            let mut put=tx.prepare_cached("INSERT INTO kv(tbl,key,value) VALUES (?1,?2,?3) ON CONFLICT(tbl,key) DO UPDATE SET value=excluded.value").map_err(failed)?;
            for (table, key, value) in writes {
                if let Some(value) = value {
                    put.execute(params![table, key, value]).map_err(failed)?;
                } else {
                    tx.execute(
                        "DELETE FROM kv WHERE tbl=?1 AND key=?2",
                        params![table, key],
                    )
                    .map_err(failed)?;
                }
            }
        }
        tx.commit().map_err(failed)
    }

    fn delete(&mut self, table: &str, key: &str) -> Result<bool, StoreError> {
        let removed = self
            .conn
            .prepare_cached("DELETE FROM kv WHERE tbl = ?1 AND key = ?2")
            .map_err(failed)?
            .execute(params![table, key])
            .map_err(failed)?;
        Ok(removed > 0)
    }

    fn list(&self, table: &str) -> Result<Vec<(String, Vec<u8>)>, StoreError> {
        let mut statement = self
            .conn
            .prepare_cached("SELECT key, value FROM kv WHERE tbl = ?1 ORDER BY key")
            .map_err(failed)?;
        let rows = statement
            .query_map(params![table], |row| Ok((row.get(0)?, row.get(1)?)))
            .map_err(failed)?;
        let mut found = Vec::new();
        for row in rows {
            found.push(row.map_err(failed)?);
        }
        Ok(found)
    }
}
