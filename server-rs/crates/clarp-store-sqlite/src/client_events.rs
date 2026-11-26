//! The bounded client log. Insertion, context pinning and pruning commit together.
use std::path::Path;

use rusqlite::{Connection, TransactionBehavior, params};

const SCHEMA: &str = "
CREATE TABLE IF NOT EXISTS client_events (
    id INTEGER PRIMARY KEY,
    received_at INTEGER NOT NULL,
    device TEXT NOT NULL,
    client TEXT NOT NULL,
    client_build TEXT NOT NULL,
    client_contract INTEGER,
    server_id TEXT NOT NULL,
    event TEXT NOT NULL,
    level TEXT NOT NULL,
    detail TEXT,
    trace_id TEXT,
    keep INTEGER NOT NULL,
    size INTEGER NOT NULL
) STRICT;
CREATE INDEX IF NOT EXISTS client_events_received ON client_events(received_at);
CREATE INDEX IF NOT EXISTS client_events_window ON client_events(keep, received_at);
CREATE INDEX IF NOT EXISTS client_events_device ON client_events(device, id);
CREATE INDEX IF NOT EXISTS client_events_context ON client_events(device, received_at);
";

pub const CONTEXT_MS: i64 = 30 * 60 * 1000;
const MAX_AGE_MS: i64 = 7 * 24 * 60 * 60 * 1000;

pub struct Event {
    pub received_at: i64,
    pub device: String,
    pub client: String,
    pub client_build: String,
    pub client_contract: Option<i64>,
    pub server_id: String,
    pub event: String,
    pub level: String,
    pub detail: Option<String>,
    pub trace_id: Option<String>,
    pub kept: bool,
}

impl Event {
    fn size(&self) -> usize {
        64 + self.event.len()
            + self.detail.as_ref().map_or(0, String::len)
            + self.trace_id.as_ref().map_or(0, String::len)
    }
}

/// Minimal event metadata for the watcher's crash check. Detail is needed
/// only for a stored MetricKit report's counts.
pub struct CrashCandidate {
    pub device: String,
    pub client: String,
    pub build: String,
    pub contract: Option<i64>,
    pub event: String,
    pub report: Option<String>,
}

/// One chat-view scroll flash an app reported (`scroll_flash`): for the watcher's count.
pub struct Flash {
    pub device: String,
    pub client: String,
    /// The build of the app that reported it, as the app said it (`X-Clarp-Client`).
    pub client_build: String,
    pub received_at: i64,
    pub detail: Option<String>,
}

pub struct Query {
    pub device: Option<String>,
    pub since_ms: Option<i64>,
    pub level: Option<String>,
    pub event: Option<String>,
    pub limit: i64,
}

pub struct Store {
    connection: Connection,
    total: (i64, i64),
    per_device: (i64, i64),
}

impl Store {
    pub fn open(path: &Path) -> Result<Self, String> {
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt as _;
            std::fs::OpenOptions::new()
                .write(true)
                .create(true)
                .truncate(false)
                .mode(0o600)
                .open(path)
                .map_err(|error| error.to_string())?;
        }
        let connection = Connection::open(path).map_err(|error| error.to_string())?;
        connection
            .execute_batch("PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;")
            .map_err(|error| error.to_string())?;
        connection
            .execute_batch(SCHEMA)
            .map_err(|error| error.to_string())?;
        Ok(Self {
            connection,
            total: (50_000, 50 * 1024 * 1024),
            per_device: (20_000, 20 * 1024 * 1024),
        })
    }

    pub fn record(&mut self, rows: &[Event], device: &str, now: i64) -> rusqlite::Result<usize> {
        if rows.is_empty() {
            return Ok(0);
        }
        let tx = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        {
            let mut insert = tx.prepare_cached(
                "INSERT INTO client_events
                (received_at, device, client, client_build, client_contract, server_id,
                 event, level, detail, trace_id, keep, size)
                 VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12)",
            )?;
            for row in rows {
                insert.execute(params![
                    row.received_at,
                    row.device,
                    row.client,
                    row.client_build,
                    row.client_contract,
                    row.server_id,
                    row.event,
                    row.level,
                    row.detail,
                    row.trace_id,
                    row.kept,
                    row.size()
                ])?;
            }
        }
        if rows.iter().any(|row| row.kept) {
            tx.execute(
                "UPDATE client_events SET keep=1
                        WHERE device=?1 AND keep=0 AND received_at BETWEEN ?2 AND ?3",
                params![device, now.saturating_sub(CONTEXT_MS), now],
            )?;
        }
        expire(&tx, now)?;
        let (mut count, mut bytes) = usage(&tx, None)?;
        // A device cannot hold more rows or bytes than the whole log.
        if count > self.per_device.0 || bytes > self.per_device.1 {
            let device_usage = usage(&tx, Some(device))?;
            let (removed, freed) = trim(&tx, Some(device), device_usage, self.per_device)?;
            count -= removed;
            bytes -= freed;
        }
        trim(&tx, None, (count, bytes), self.total)?;
        tx.commit()?;
        Ok(rows.len())
    }

    pub fn prune(&mut self, now: i64) -> rusqlite::Result<()> {
        let tx = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        expire(&tx, now)?;
        tx.commit()
    }

    pub fn crash_candidates(&self, since: i64, now: i64) -> rusqlite::Result<Vec<CrashCandidate>> {
        let mut select = self.connection.prepare_cached(
            "SELECT device, client, client_build, client_contract, event,
                    CASE WHEN event = 'metrickit_report' THEN detail END
             FROM client_events
             WHERE received_at BETWEEN ?1 AND ?2
               AND device NOT IN ('', 'administrator', 'local', 'unknown')
               AND (event = 'metrickit_report' OR event LIKE '%crash%'
                    OR event LIKE '%fatal%' OR event LIKE '%uncaught%')
             ORDER BY received_at, id",
        )?;
        let rows = select.query_map(params![since, now], |row| {
            Ok(CrashCandidate {
                device: row.get(0)?,
                client: row.get(1)?,
                build: row.get(2)?,
                contract: row.get(3)?,
                event: row.get(4)?,
                report: row.get(5)?,
            })
        })?;
        rows.collect()
    }

    /// The chat-view scroll flashes apps reported in the window, the oldest first.
    pub fn flashes(&self, since: i64, now: i64) -> rusqlite::Result<Vec<Flash>> {
        let mut select = self.connection.prepare_cached(
            "SELECT device, client, client_build, received_at, detail FROM client_events
             WHERE received_at BETWEEN ?1 AND ?2 AND event = 'scroll_flash'
             ORDER BY received_at, id",
        )?;
        let rows = select.query_map(params![since, now], |row| {
            Ok(Flash {
                device: row.get(0)?,
                client: row.get(1)?,
                client_build: row.get(2)?,
                received_at: row.get(3)?,
                detail: row.get(4)?,
            })
        })?;
        rows.collect()
    }

    pub fn read(&mut self, query: &Query, now: i64) -> rusqlite::Result<Vec<Event>> {
        self.prune(now)?;
        let mut select = self.connection.prepare_cached(
            "SELECT
            received_at, device, client, client_build, client_contract, server_id,
            event, level, detail, trace_id, keep FROM client_events
            WHERE (?1 IS NULL OR device = ?1) AND (?2 IS NULL OR received_at >= ?2)
              AND (?3 IS NULL OR event = ?3)
              AND (?4 IS NULL OR level = ?4
                OR (?4 IN ('error','warning') AND level IN ('error','fault','critical','fatal'))
                OR (?4 = 'warning' AND level = 'warn'))
            ORDER BY id DESC LIMIT ?5",
        )?;
        let rows = select.query_map(
            params![
                query.device,
                query.since_ms,
                query.event,
                query.level,
                query.limit
            ],
            |row| {
                Ok(Event {
                    received_at: row.get(0)?,
                    device: row.get(1)?,
                    client: row.get(2)?,
                    client_build: row.get(3)?,
                    client_contract: row.get(4)?,
                    server_id: row.get(5)?,
                    event: row.get(6)?,
                    level: row.get(7)?,
                    detail: row.get(8)?,
                    trace_id: row.get(9)?,
                    kept: row.get(10)?,
                })
            },
        )?;
        let mut rows = rows.collect::<rusqlite::Result<Vec<_>>>()?;
        rows.reverse();
        Ok(rows)
    }
}

fn expire(db: &Connection, now: i64) -> rusqlite::Result<()> {
    db.execute(
        "DELETE FROM client_events WHERE received_at < ?1",
        [now.saturating_sub(MAX_AGE_MS)],
    )?;
    db.execute(
        "DELETE FROM client_events WHERE keep=0 AND received_at < ?1",
        [now.saturating_sub(CONTEXT_MS)],
    )?;
    Ok(())
}

fn usage(db: &Connection, device: Option<&str>) -> rusqlite::Result<(i64, i64)> {
    let sql = if device.is_some() {
        "SELECT COUNT(*), COALESCE(SUM(size),0) FROM client_events WHERE device = ?1"
    } else {
        "SELECT COUNT(*), COALESCE(SUM(size),0) FROM client_events WHERE ?1 IS NULL"
    };
    db.prepare_cached(sql)?
        .query_row([device], |row| Ok((row.get(0)?, row.get(1)?)))
}

/// Trim the given scope and return the rows and bytes removed from the log.
fn trim(
    db: &Connection,
    device: Option<&str>,
    (count, bytes): (i64, i64),
    (max_events, max_bytes): (i64, i64),
) -> rusqlite::Result<(i64, i64)> {
    if count <= max_events && bytes <= max_bytes {
        return Ok((0, 0));
    }
    let condition = if device.is_some() {
        "device = ?1"
    } else {
        "?1 IS NULL"
    };
    let (mut removed, mut freed) = (0, 0);
    // Oldest first: once both limits fit, younger rows cannot change the cutoff.
    let cutoff = {
        let mut select = db.prepare_cached(&format!(
            "SELECT id, size FROM client_events WHERE {condition} ORDER BY id"
        ))?;
        let mut rows = select.query([device])?;
        let mut cutoff = None;
        while let Some(row) = rows.next()? {
            removed += 1;
            freed += row.get::<_, i64>(1)?;
            if count - removed <= max_events && bytes - freed <= max_bytes {
                cutoff = Some(row.get::<_, i64>(0)?);
                break;
            }
        }
        cutoff
    };
    if let Some(cutoff) = cutoff {
        db.execute(
            &format!("DELETE FROM client_events WHERE {condition} AND id <= ?2"),
            params![device, cutoff],
        )?;
    }
    Ok((removed, freed))
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;

    #[test]
    fn the_scroll_flashes_of_a_window_are_read_in_order() {
        let connection = Connection::open_in_memory().unwrap();
        connection.execute_batch(SCHEMA).unwrap();
        let mut store = Store {
            connection,
            total: (100, 1_000_000),
            per_device: (100, 1_000_000),
        };
        let event = |name: &str, client: &str, received_at| Event {
            received_at,
            device: "phone".into(),
            event: name.into(),
            kept: true,
            client: client.into(),
            client_build: String::new(),
            client_contract: None,
            server_id: String::new(),
            level: "warning".into(),
            detail: Some("{\"rule\":\"jump_back\"}".into()),
            trace_id: None,
        };
        let rows = [
            event("scroll_flash", "ios", 10),
            event("app_crash", "ios", 11),
            event("scroll_flash", "desktop", 12),
            event("scroll_flash", "ios", 50),
        ];
        store.record(&rows, "phone", 60).unwrap();
        let read = store.flashes(10, 20).unwrap();
        let seen: Vec<_> = read
            .iter()
            .map(|f| (f.device.as_str(), f.client.as_str(), f.received_at))
            .collect();
        assert_eq!(seen, [("phone", "ios", 10), ("phone", "desktop", 12)]);
        assert_eq!(read[0].detail.as_deref(), Some("{\"rule\":\"jump_back\"}"));
    }

    #[test]
    fn client_events_pin_context_and_enforce_age_count_and_byte_limits() {
        let connection = Connection::open_in_memory().unwrap();
        connection.execute_batch(SCHEMA).unwrap();
        let mut store = Store {
            connection,
            total: (3, 1_000_000),
            per_device: (2, 1_000_000),
        };
        let query = Query {
            device: None,
            since_ms: None,
            level: None,
            event: None,
            limit: 20,
        };
        let event = |device: &str, name: &str, received_at, kept| Event {
            received_at,
            device: device.into(),
            event: name.into(),
            kept,
            client: String::new(),
            client_build: String::new(),
            client_contract: None,
            server_id: String::new(),
            level: String::new(),
            detail: None,
            trace_id: None,
        };
        let at = 2 * MAX_AGE_MS;
        store
            .record(&[event("one", "before", at, false)], "one", at)
            .unwrap();
        store
            .record(&[event("two", "unrelated", at, false)], "two", at)
            .unwrap();
        store
            .record(&[event("one", "crash", at + 1, true)], "one", at + 1)
            .unwrap();
        let aged = at + CONTEXT_MS + 2;
        let rows = store.read(&query, aged).unwrap();
        assert_eq!(
            rows.iter()
                .map(|row| (&*row.event, row.kept))
                .collect::<Vec<_>>(),
            [("before", true), ("crash", true)]
        );
        store
            .record(&[event("one", "after", aged, false)], "one", aged)
            .unwrap();
        store
            .record(
                &[
                    event("two", "new", aged, false),
                    event("two", "newest", aged, false),
                ],
                "two",
                aged,
            )
            .unwrap();
        let rows = store.read(&query, aged).unwrap();
        assert_eq!(
            rows.iter()
                .map(|row| row.event.as_str())
                .collect::<Vec<_>>(),
            ["after", "new", "newest"]
        );
        store.total = (3, 140);
        store.per_device = (2, 90);
        store
            .record(&[event("two", "last", aged, true)], "two", aged)
            .unwrap();
        let rows = store.read(&query, aged).unwrap();
        assert_eq!(
            rows.iter()
                .map(|row| row.event.as_str())
                .collect::<Vec<_>>(),
            ["after", "last"]
        );
        assert!(
            store
                .read(&query, aged + MAX_AGE_MS + 1)
                .unwrap()
                .is_empty()
        );
    }
}
