//! The SQLite log engine: `log.db` in the agent's directory.
//!
//! One table, clustered by position, append-only. The newest page of a conversation is the end
//! of one small file, so reading it costs the same whatever the other agents have written.
//! `synchronous=FULL` in WAL mode: when `append` returns, the items are on disk. That is rule 1
//! (an accepted message is never lost); the kernel batches appends rather than weaken it.

use std::path::Path;

use clarp_ports::codec::{CodecSet, CodecTag};
use clarp_ports::log::{AgentLog, Item, LogEngine, LogError, NewItem, Seq};
use rusqlite::{Connection, params};

/// The name of the log file inside an agent's directory.
pub const LOG_FILE: &str = "log.db";

const SCHEMA_VERSION: i64 = 1;

const SCHEMA: &str = "
CREATE TABLE IF NOT EXISTS item(
    seq   INTEGER PRIMARY KEY,
    turn  INTEGER,
    kind  INTEGER NOT NULL,
    ts    INTEGER NOT NULL,
    codec INTEGER NOT NULL,
    body  BLOB NOT NULL
) STRICT;
";

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
            body: self.codecs.decode(tag, &row.body)?,
        })
    }

    fn select(&self, sql: &str, first: i64, second: i64) -> Result<Vec<Item>, LogError> {
        let mut statement = self.conn.prepare_cached(sql).map_err(storage)?;
        let rows = statement
            .query_map(params![first, second], |row| {
                Ok(Stored {
                    seq: row.get(0)?,
                    turn: row.get(1)?,
                    kind: row.get(2)?,
                    ts_ms: row.get(3)?,
                    codec: row.get(4)?,
                    body: row.get(5)?,
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
                .prepare_cached("INSERT INTO item(seq, turn, kind, ts, codec, body) VALUES (?1, ?2, ?3, ?4, ?5, ?6)")
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

    fn read_after(&self, after: Seq, limit: usize) -> Result<Vec<Item>, LogError> {
        let after = i64::try_from(after).unwrap_or(i64::MAX);
        self.select("SELECT seq, turn, kind, ts, codec, body FROM item WHERE seq > ?1 ORDER BY seq LIMIT ?2", after, as_limit(limit))
    }

    fn read_tail(&self, limit: usize) -> Result<Vec<Item>, LogError> {
        // The newest `limit` by position, then oldest first: a range read at the end of the file.
        self.select(
            "SELECT seq, turn, kind, ts, codec, body FROM (SELECT * FROM item ORDER BY seq DESC LIMIT ?1) WHERE ?2 = ?2 ORDER BY seq",
            as_limit(limit),
            0,
        )
    }
}
