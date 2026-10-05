//! An HTML form's event journal on disk (iOS `HTMLFormEventLog`): events
//! the page logs with `window.clarpForm.log(event)` are appended here before
//! the page hears "queued", then sent in batches to the Host they were logged
//! against (`POST /artifacts/{id}/events`, docs/html-forms/README.md). Every
//! record keeps its event id across retries and restarts, so the Host's
//! idempotent append sees one row per event however often a batch is sent.
//!
//! A form's directory holds `events-outbox.jsonl` (one record a line),
//! `events-sync.json` (the Host and the last acknowledged `client_seq`),
//! `events-import.json` (how far a draft event array was imported) and
//! `events-config.json` (the Host's draft-key policy, kept for offline use).

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::time::Duration;

use serde_json::Value;

/// One event's encoded size, as the Host bounds it.
pub const MAX_EVENT_BYTES: usize = 16_384;
/// Records a batch carries, as the Host bounds it.
pub const MAX_BATCH: usize = 32;
/// Unsent records a form may keep on disk.
pub const MAX_JOURNAL_BYTES: u64 = 8 * 1024 * 1024;
/// Entries a followed draft event array may hold.
pub const MAX_DRAFT_EVENTS: usize = 10_000;

/// Where `artifact_id` at `version` keeps its journal under `root`.
pub fn directory(root: &Path, artifact_id: &str, version: &Value) -> PathBuf {
    root.join(&sha256_hex(serde_json::json!([artifact_id, version]).to_string().as_bytes())[..32])
}

/// A page's event id: a hyphenated UUID, as the iOS bridge makes them.
pub fn valid_event_id(id: &str) -> bool {
    id.len() == 36 && uuid::Uuid::parse_str(id).is_ok()
}

/// The wait before a send after `failures` failed ones in a row: a second
/// to batch a burst, then 2, 4 … 60 seconds.
pub fn retry_delay(failures: u32) -> Duration {
    Duration::from_secs(if failures == 0 { 1 } else { 60.min(2u64 << (failures - 1).min(5)) })
}

/// A Host answer that retrying the same batch cannot change; the records
/// stay until the original connection recovers.
pub fn is_permanent(status: u16) -> bool {
    matches!(status, 400 | 401 | 403 | 404 | 409 | 413 | 422 | 501)
}

fn sha256_hex(bytes: &[u8]) -> String {
    use sha2::{Digest, Sha256};
    Sha256::digest(bytes).iter().map(|b| format!("{b:02x}")).collect()
}

/// The records a send carries, up to `cursor` (the last one's `client_seq`).
#[derive(Debug, Clone, PartialEq)]
pub struct Batch {
    pub body: Value,
    pub cursor: u64,
    pub ids: Vec<String>,
}

const JOURNAL: &str = "events-outbox.jsonl";
const CHECKPOINT: &str = "events-sync.json";
const IMPORT: &str = "events-import.json";
const CONFIG: &str = "events-config.json";

pub struct EventLog {
    dir: PathBuf,
    artifact_id: String,
    version: Value,
    origin: Option<String>,
    acknowledged: u64,
    sequence: u64,
    pending: Vec<Value>,
    /// Encoded events by id, to tell a retried log from a conflicting one.
    payloads: HashMap<String, String>,
    recent: Vec<String>,
}

fn io(what: &str, path: &Path, error: impl std::fmt::Display) -> String {
    format!("cannot {what} {}: {error}", path.display())
}

/// Written whole or not at all, readable only by the user.
fn write_atomic(path: &Path, bytes: &[u8]) -> Result<(), String> {
    use std::io::Write;
    use std::os::unix::fs::OpenOptionsExt;
    let temporary = path.with_extension(format!("tmp-{}", uuid::Uuid::new_v4().simple()));
    let written = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&temporary)
        .and_then(|mut file| file.write_all(bytes).and_then(|()| file.sync_all()));
    if let Err(error) = written.and_then(|()| std::fs::rename(&temporary, path)) {
        let _ = std::fs::remove_file(&temporary);
        return Err(io("write", path, error));
    }
    Ok(())
}

fn read_json(path: &Path) -> Result<Option<Value>, String> {
    match std::fs::read(path) {
        Ok(bytes) => serde_json::from_slice(&bytes).map(Some).map_err(|e| io("read", path, e)),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(io("read", path, error)),
    }
}

impl EventLog {
    /// Opens (or starts) `artifact_id`'s journal in `dir`.
    pub fn open(dir: &Path, artifact_id: &str, version: &Value) -> Result<Self, String> {
        std::fs::create_dir_all(dir).map_err(|e| io("create", dir, e))?;
        let mut log = Self {
            dir: dir.to_owned(),
            artifact_id: artifact_id.to_owned(),
            version: version.clone(),
            origin: None,
            acknowledged: 0,
            sequence: 0,
            pending: Vec::new(),
            payloads: HashMap::new(),
            recent: Vec::new(),
        };
        if let Some(saved) = read_json(&dir.join(CHECKPOINT))? {
            let (origin, cursor) = (saved["origin"].as_str(), saved["acknowledged"].as_u64());
            let (Some(origin), Some(cursor)) = (origin, cursor) else {
                return Err(format!("the event journal's checkpoint in {} is unreadable", dir.display()));
            };
            if saved["artifact_id"].as_str().is_some_and(|id| id != artifact_id) || saved.get("version").is_some_and(|v| v != version) {
                return Err(format!("the event journal in {} belongs to another form", dir.display()));
            }
            log.origin = Some(origin.to_owned());
            log.acknowledged = cursor;
            log.sequence = cursor;
        }
        log.load_journal()?;
        Ok(log)
    }

    /// Opens a journal from what it saved, with no artifact at hand (after a
    /// restart); None when it never logged anything.
    pub fn reopen(dir: &Path) -> Result<Option<Self>, String> {
        let Some(saved) = read_json(&dir.join(CHECKPOINT))? else { return Ok(None) };
        let Some(artifact_id) = saved["artifact_id"].as_str() else {
            return Err(format!("the event journal in {} does not name its form", dir.display()));
        };
        Self::open(dir, artifact_id, saved.get("version").unwrap_or(&Value::Null)).map(Some)
    }

    fn load_journal(&mut self) -> Result<(), String> {
        let path = self.dir.join(JOURNAL);
        let mut bytes = match std::fs::read(&path) {
            Ok(bytes) => bytes,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
            Err(error) => return Err(io("read", &path, error)),
        };
        if bytes.len() as u64 > MAX_JOURNAL_BYTES {
            return Err("The local event journal exceeds its storage bound.".into());
        }
        // A torn, never-acknowledged final write is not a queued event.
        if bytes.last().is_some_and(|b| *b != b'\n') {
            bytes.truncate(bytes.iter().rposition(|b| *b == b'\n').map_or(0, |i| i + 1));
            write_atomic(&path, &bytes)?;
        }
        for line in bytes.split(|b| *b == b'\n').filter(|l| !l.is_empty()) {
            let record: Value = serde_json::from_slice(line).map_err(|e| io("read a record of", &path, e))?;
            let (Some(id), Some(seq), Some(event)) = (record["event_id"].as_str(), record["client_seq"].as_u64(), record.get("event").filter(|e| e.is_object()))
            else {
                return Err(format!("a record in {} is unreadable", path.display()));
            };
            if !valid_event_id(id) {
                return Err(format!("a record in {} has no event id", path.display()));
            }
            self.sequence = self.sequence.max(seq);
            self.payloads.insert(id.to_owned(), event.to_string());
            self.recent.push(id.to_owned());
            if seq > self.acknowledged {
                self.pending.push(record);
            }
        }
        if self.origin.is_none() && !self.pending.is_empty() {
            return Err(format!("the event journal in {} does not say which Host it belongs to", self.dir.display()));
        }
        Ok(())
    }

    fn save_checkpoint(&self, cursor: u64) -> Result<(), String> {
        let Some(origin) = &self.origin else { return Err("the event journal has no Host".into()) };
        let saved = serde_json::json!({"origin": origin, "acknowledged": cursor, "artifact_id": self.artifact_id, "version": self.version});
        write_atomic(&self.dir.join(CHECKPOINT), saved.to_string().as_bytes())
    }

    pub fn dir(&self) -> &Path {
        &self.dir
    }
    pub fn artifact_id(&self) -> &str {
        &self.artifact_id
    }
    pub fn version(&self) -> &Value {
        &self.version
    }
    /// The Host its records belong to, once it logged one.
    pub fn origin(&self) -> Option<&str> {
        self.origin.as_deref()
    }
    pub fn pending(&self) -> usize {
        self.pending.len()
    }

    /// Appends `event` durably; false when `event_id` was already logged
    /// with the same event (nothing new is written).
    pub fn enqueue(&mut self, event: &Value, event_id: &str, origin: &str, now_ms: u64) -> Result<bool, String> {
        use std::io::{Seek, Write};
        use std::os::unix::fs::OpenOptionsExt;
        if !event.is_object() {
            return Err("Event must be an object.".into());
        }
        if !valid_event_id(event_id) {
            return Err("Event id must be a UUID.".into());
        }
        if self.origin.as_deref().is_some_and(|o| o != origin) {
            return Err("Pending events belong to another Host connection.".into());
        }
        // serde_json's maps are sorted, so this is the Host's encoding.
        let payload = event.to_string();
        if payload.len() > MAX_EVENT_BYTES {
            return Err("An event may contain at most 16 KiB.".into());
        }
        if let Some(previous) = self.payloads.get(event_id) {
            if *previous != payload {
                return Err("This event id was already used for another event.".into());
            }
            return Ok(false);
        }
        if self.origin.is_none() {
            self.origin = Some(origin.to_owned());
            if let Err(error) = self.save_checkpoint(self.acknowledged) {
                self.origin = None;
                return Err(error);
            }
        }
        let next = self.sequence + 1;
        let record = serde_json::json!({"event_id": event_id, "client_seq": next, "client_at": now_ms, "event": event});
        let mut line = record.to_string().into_bytes();
        line.push(b'\n');
        let path = self.dir.join(JOURNAL);
        let mut file = std::fs::OpenOptions::new().create(true).append(true).mode(0o600).open(&path).map_err(|e| io("open", &path, e))?;
        let offset = file.seek(std::io::SeekFrom::End(0)).map_err(|e| io("open", &path, e))?;
        if offset + line.len() as u64 > MAX_JOURNAL_BYTES {
            return Err("Offline event storage is full; existing events have been retained.".into());
        }
        if let Err(error) = file.write_all(&line).and_then(|()| file.sync_data()) {
            let _ = file.set_len(offset);
            return Err(io("write", &path, error));
        }
        self.sequence = next;
        self.pending.push(record);
        self.payloads.insert(event_id.to_owned(), payload);
        self.recent.push(event_id.to_owned());
        Ok(true)
    }

    /// Imports the entries `values[key]` gained since the last import, each
    /// under an id derived from its content and place; how many were new.
    pub fn capture_draft(&mut self, values: &Value, key: &str, origin: &str, now_ms: u64) -> Result<usize, String> {
        let Some(events) = values.get(key).and_then(Value::as_array) else { return Ok(0) };
        if events.len() > MAX_DRAFT_EVENTS {
            return Err("Draft event history is too large; use explicit log(event) for new events.".into());
        }
        let cursor_file = self.dir.join(IMPORT);
        let (artifact_id, version) = (self.artifact_id.clone(), self.version.clone());
        let fingerprint = |index: usize, event: &Value| sha256_hex(serde_json::json!([artifact_id, version, key, index, event]).to_string().as_bytes());
        let mut count = 0;
        if let Some(saved) = read_json(&cursor_file)?
            && saved["origin"] == origin
            && saved["key"] == key
            && let Some(old) = saved["count"].as_u64().map(|c| c as usize)
            && old > 0
            && old <= events.len()
            && saved["tail"].as_str() == Some(fingerprint(old - 1, &events[old - 1]).as_str())
        {
            count = old;
        }
        let mut imported = 0;
        while count < events.len() {
            let digest = fingerprint(count, &events[count]);
            let id = format!("{}-{}-{}-{}-{}", &digest[0..8], &digest[8..12], &digest[12..16], &digest[16..20], &digest[20..32]);
            if !events[count].is_object() {
                return Err(format!("Draft event {count} is not an object."));
            }
            if self.enqueue(&events[count], &id, origin, now_ms)? {
                imported += 1;
            }
            count += 1;
            let saved = serde_json::json!({"origin": origin, "key": key, "count": count, "tail": digest});
            write_atomic(&cursor_file, saved.to_string().as_bytes())?;
        }
        Ok(imported)
    }

    /// The next records to send, oldest first.
    pub fn batch(&self) -> Option<Batch> {
        let records: Vec<Value> = self.pending.iter().take(MAX_BATCH).cloned().collect();
        let cursor = records.last()?["client_seq"].as_u64()?;
        let ids = records.iter().filter_map(|r| r["event_id"].as_str().map(str::to_owned)).collect();
        Some(Batch { body: serde_json::json!({"version": self.version, "events": records}), cursor, ids })
    }

    /// The Host's receipt for `batch`: once it names every record, they are
    /// acknowledged on disk and leave the queue.
    pub fn acknowledge(&mut self, batch: &Batch, receipt: &Value) -> Result<(), String> {
        let receipts = receipt["events"].as_array().map(Vec::as_slice).unwrap_or_default();
        let named: std::collections::HashSet<&str> = receipts.iter().filter_map(|r| r["event_id"].as_str()).collect();
        let sent: std::collections::HashSet<&str> = batch.ids.iter().map(String::as_str).collect();
        let valid = receipt["accepted"] == true
            && receipt["artifact_id"] == self.artifact_id.as_str()
            && receipt["version"] == self.version
            && receipts.len() == batch.ids.len()
            && named == sent
            && receipts.iter().all(|r| r["seq"].as_u64().is_some_and(|seq| seq > 0));
        if !valid {
            return Err("The Host's receipt does not match the events sent.".into());
        }
        self.save_checkpoint(batch.cursor)?;
        self.acknowledged = batch.cursor;
        self.pending.retain(|r| r["client_seq"].as_u64().unwrap_or(0) > self.acknowledged);
        let keep = self.recent.len().saturating_sub(256);
        self.recent.drain(..keep);
        let retained: std::collections::HashSet<&str> =
            self.recent.iter().map(String::as_str).chain(self.pending.iter().filter_map(|r| r["event_id"].as_str())).collect();
        self.payloads.retain(|id, _| retained.contains(id.as_str()));
        if self.pending.is_empty() {
            write_atomic(&self.dir.join(JOURNAL), b"")?;
        }
        Ok(())
    }

    /// The draft key the Host's policy follows for `origin`, as last saved.
    pub fn cached_draft_key(&self, origin: &str) -> Option<Option<String>> {
        let saved = read_json(&self.dir.join(CONFIG)).ok()??;
        (saved["origin"] == origin).then(|| saved["draft_key"].as_str().map(str::to_owned))
    }

    /// Saves the Host's draft-key policy for `origin`.
    pub fn save_draft_key(&self, origin: &str, key: Option<&str>) -> Result<(), String> {
        write_atomic(&self.dir.join(CONFIG), serde_json::json!({"origin": origin, "draft_key": key}).to_string().as_bytes())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    const HOST: &str = "srv-1|http://127.0.0.1:7682";

    fn scratch() -> PathBuf {
        let dir = std::env::temp_dir().join(format!("clarp-form-events-{}", uuid::Uuid::new_v4().simple()));
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    fn id() -> String {
        uuid::Uuid::new_v4().to_string()
    }

    fn receipt(batch: &Batch, version: &Value) -> Value {
        let events: Vec<Value> = batch.ids.iter().enumerate().map(|(i, id)| json!({"event_id": id, "seq": i + 1})).collect();
        json!({"artifact_id": "form-1", "version": version, "accepted": true, "events": events})
    }

    #[test]
    fn a_form_and_version_get_their_own_directory() {
        let root = Path::new("/data/form-events");
        let a = directory(root, "form-1", &json!("v1"));
        assert_eq!(a, directory(root, "form-1", &json!("v1")));
        assert_ne!(a, directory(root, "form-1", &json!("v2")));
        assert_ne!(a, directory(root, "form-2", &json!("v1")));
        assert!(a.starts_with(root) && a.file_name().unwrap().to_str().unwrap().chars().all(|c| c.is_ascii_hexdigit()));
    }

    #[test]
    fn event_ids_are_the_bridge_uuids() {
        assert!(valid_event_id("0b4f3c9e-51a7-4d0a-9a8c-2f1e6d7c8b90"));
        for bad in ["", "not-a-uuid", "0b4f3c9e51a74d0a9a8c2f1e6d7c8b90", "0b4f3c9e-51a7-4d0a-9a8c-2f1e6d7c8b9/"] {
            assert!(!valid_event_id(bad), "{bad}");
        }
    }

    #[test]
    fn retries_back_off_to_a_minute() {
        let delays: Vec<u64> = (0..9).map(|f| retry_delay(f).as_secs()).collect();
        assert_eq!(delays, [1, 2, 4, 8, 16, 32, 60, 60, 60]);
    }

    #[test]
    fn refusals_that_retrying_cannot_change_stop_the_sends() {
        for status in [400, 401, 403, 404, 409, 413, 422, 501] {
            assert!(is_permanent(status), "{status}");
        }
        for status in [0, 429, 500, 502, 503, 504] {
            assert!(!is_permanent(status), "{status}");
        }
    }

    #[test]
    fn queued_events_survive_a_restart_with_their_ids() {
        let dir = scratch();
        let version = json!("v1");
        let ids: Vec<String> = (0..3).map(|_| id()).collect();
        {
            let mut log = EventLog::open(&dir, "form-1", &version).unwrap();
            for (n, event_id) in ids.iter().enumerate() {
                assert!(log.enqueue(&json!({"type": "number_shown", "number": n}), event_id, HOST, 1_000 + n as u64).unwrap());
            }
            assert_eq!(log.pending(), 3);
        }
        // The app restarts: nothing but the directory is left.
        let log = EventLog::reopen(&dir).unwrap().expect("the journal");
        assert_eq!((log.artifact_id(), log.version(), log.origin(), log.pending()), ("form-1", &version, Some(HOST), 3));
        let batch = log.batch().unwrap();
        assert_eq!(batch.ids, ids);
        assert_eq!(batch.cursor, 3);
        // The wire shape the Host takes: {version, events: [{event_id, client_seq, client_at, event}]}.
        assert_eq!(
            batch.body,
            json!({"version": "v1", "events": [
                {"event_id": ids[0], "client_seq": 1, "client_at": 1000, "event": {"type": "number_shown", "number": 0}},
                {"event_id": ids[1], "client_seq": 2, "client_at": 1001, "event": {"type": "number_shown", "number": 1}},
                {"event_id": ids[2], "client_seq": 3, "client_at": 1002, "event": {"type": "number_shown", "number": 2}},
            ]})
        );
        // A second send of the same records is the same batch.
        assert_eq!(log.batch().unwrap(), batch);
    }

    #[test]
    fn a_journal_never_logged_to_has_nothing_to_resume() {
        assert!(EventLog::reopen(&scratch()).unwrap().is_none());
    }

    #[test]
    fn a_torn_last_line_is_not_a_queued_event() {
        let dir = scratch();
        let mut log = EventLog::open(&dir, "form-1", &json!(1)).unwrap();
        log.enqueue(&json!({"a": 1}), &id(), HOST, 5).unwrap();
        drop(log);
        let journal = dir.join("events-outbox.jsonl");
        let mut bytes = std::fs::read(&journal).unwrap();
        bytes.extend_from_slice(b"{\"event_id\":\"half");
        std::fs::write(&journal, bytes).unwrap();
        let mut log = EventLog::reopen(&dir).unwrap().unwrap();
        assert_eq!(log.pending(), 1);
        log.enqueue(&json!({"a": 2}), &id(), HOST, 6).unwrap();
        let log = EventLog::reopen(&dir).unwrap().unwrap();
        assert_eq!(log.batch().unwrap().cursor, 2);
    }

    #[test]
    fn the_same_event_id_is_one_record() {
        let dir = scratch();
        let mut log = EventLog::open(&dir, "form-1", &json!(1)).unwrap();
        let event_id = id();
        assert!(log.enqueue(&json!({"b": 1, "a": [1, 2]}), &event_id, HOST, 5).unwrap());
        assert!(!log.enqueue(&json!({"a": [1, 2], "b": 1}), &event_id, HOST, 9).unwrap(), "a retried log is not a second record");
        assert_eq!(log.pending(), 1);
        let error = log.enqueue(&json!({"a": 3}), &event_id, HOST, 9).unwrap_err();
        assert!(error.contains("already"), "{error}");
        assert_eq!(EventLog::reopen(&dir).unwrap().unwrap().pending(), 1);
    }

    #[test]
    fn events_are_bounded_like_the_host_bounds_them() {
        let dir = scratch();
        let mut log = EventLog::open(&dir, "form-1", &json!(1)).unwrap();
        for not_object in [json!([1]), json!("x"), json!(3), Value::Null] {
            assert!(log.enqueue(&not_object, &id(), HOST, 1).is_err(), "{not_object}");
        }
        assert!(log.enqueue(&json!({"a": 1}), "short", HOST, 1).is_err(), "an id the bridge did not make");
        // {"t":"…"} is 8 bytes around the text.
        assert!(log.enqueue(&json!({"t": "x".repeat(MAX_EVENT_BYTES - 8)}), &id(), HOST, 1).unwrap());
        let error = log.enqueue(&json!({"t": "x".repeat(MAX_EVENT_BYTES - 7)}), &id(), HOST, 1).unwrap_err();
        assert!(error.contains("16 KiB"), "{error}");
        assert_eq!(log.pending(), 1);
    }

    #[test]
    fn a_full_journal_refuses_new_events_and_keeps_the_old() {
        let dir = scratch();
        let mut log = EventLog::open(&dir, "form-1", &json!(1)).unwrap();
        let big = json!({"t": "x".repeat(MAX_EVENT_BYTES - 8)});
        let mut queued = 0;
        let error = loop {
            match log.enqueue(&big, &id(), HOST, 1) {
                Ok(_) => queued += 1,
                Err(error) => break error,
            }
            assert!(queued < 1000, "the journal never fills");
        };
        assert!(error.contains("full"), "{error}");
        assert!(std::fs::metadata(dir.join("events-outbox.jsonl")).unwrap().len() <= MAX_JOURNAL_BYTES);
        assert_eq!(EventLog::reopen(&dir).unwrap().unwrap().pending(), queued);
    }

    #[test]
    fn records_belong_to_the_host_they_were_logged_against() {
        let dir = scratch();
        let mut log = EventLog::open(&dir, "form-1", &json!(1)).unwrap();
        log.enqueue(&json!({"a": 1}), &id(), HOST, 1).unwrap();
        let error = log.enqueue(&json!({"a": 2}), &id(), "srv-2|http://other:7682", 1).unwrap_err();
        assert!(error.contains("another Host"), "{error}");
    }

    #[test]
    fn batches_carry_at_most_32_records() {
        let dir = scratch();
        let mut log = EventLog::open(&dir, "form-1", &json!(1)).unwrap();
        for n in 0..40 {
            log.enqueue(&json!({"n": n}), &id(), HOST, n).unwrap();
        }
        let batch = log.batch().unwrap();
        assert_eq!((batch.ids.len(), batch.cursor), (MAX_BATCH, 32));
        log.acknowledge(&batch, &receipt(&batch, &json!(1))).unwrap();
        let rest = log.batch().unwrap();
        assert_eq!((rest.ids.len(), rest.cursor), (8, 40));
    }

    #[test]
    fn an_acknowledged_batch_leaves_the_queue_for_good() {
        let dir = scratch();
        let version = json!("v1");
        let mut log = EventLog::open(&dir, "form-1", &version).unwrap();
        log.enqueue(&json!({"a": 1}), &id(), HOST, 1).unwrap();
        log.enqueue(&json!({"a": 2}), &id(), HOST, 2).unwrap();
        let batch = log.batch().unwrap();
        // Records logged while the batch was out stay for the next one.
        let late = id();
        log.enqueue(&json!({"a": 3}), &late, HOST, 3).unwrap();
        log.acknowledge(&batch, &receipt(&batch, &version)).unwrap();
        assert_eq!(log.pending(), 1);
        assert_eq!(log.batch().unwrap().ids, [late.clone()]);
        let mut log = EventLog::reopen(&dir).unwrap().unwrap();
        assert_eq!(log.pending(), 1, "acknowledged records are not sent again after a restart");
        let batch = log.batch().unwrap();
        log.acknowledge(&batch, &receipt(&batch, &version)).unwrap();
        assert_eq!(log.pending(), 0);
        assert_eq!(std::fs::metadata(dir.join("events-outbox.jsonl")).unwrap().len(), 0, "a drained journal is compacted");
        // The sequence carries on past what was acknowledged.
        log.enqueue(&json!({"a": 4}), &id(), HOST, 4).unwrap();
        assert_eq!(EventLog::reopen(&dir).unwrap().unwrap().batch().unwrap().cursor, 4);
    }

    #[test]
    fn a_receipt_that_does_not_name_the_batch_acknowledges_nothing() {
        let dir = scratch();
        let version = json!("v1");
        let mut log = EventLog::open(&dir, "form-1", &version).unwrap();
        log.enqueue(&json!({"a": 1}), &id(), HOST, 1).unwrap();
        log.enqueue(&json!({"a": 2}), &id(), HOST, 2).unwrap();
        let batch = log.batch().unwrap();
        let good = receipt(&batch, &version);
        let mut short = good.clone();
        short["events"].as_array_mut().unwrap().pop();
        let mut other_version = good.clone();
        other_version["version"] = json!("v2");
        let mut not_accepted = good.clone();
        not_accepted["accepted"] = json!(false);
        let mut no_seq = good.clone();
        no_seq["events"][0]["seq"] = json!(0);
        let mut other_form = good.clone();
        other_form["artifact_id"] = json!("form-2");
        for bad in [short, other_version, not_accepted, no_seq, other_form, json!({})] {
            assert!(log.acknowledge(&batch, &bad).is_err(), "{bad}");
            assert_eq!(log.pending(), 2);
        }
    }

    #[test]
    fn a_followed_draft_array_is_imported_once_per_entry() {
        let dir = scratch();
        let mut log = EventLog::open(&dir, "form-1", &json!(1)).unwrap();
        let draft = |n: usize| json!({"run": 7, "events": (0..n).map(|i| json!({"type": "tap", "i": i})).collect::<Vec<_>>()});
        assert_eq!(log.capture_draft(&draft(2), "events", HOST, 1).unwrap(), 2);
        assert_eq!(log.capture_draft(&draft(2), "events", HOST, 2).unwrap(), 0, "the same draft again imports nothing");
        assert_eq!(log.capture_draft(&draft(3), "events", HOST, 3).unwrap(), 1);
        let first = log.batch().unwrap().ids;
        drop(log);
        let mut log = EventLog::reopen(&dir).unwrap().unwrap();
        assert_eq!(log.capture_draft(&draft(4), "events", HOST, 4).unwrap(), 1, "the import cursor survives a restart");
        let ids = log.batch().unwrap().ids;
        assert_eq!(ids.len(), 4);
        assert_eq!(&ids[..3], &first[..]);
        assert!(ids.iter().all(|id| valid_event_id(id)), "{ids:?}");
        // The ids come from the entries, not from when they were imported.
        let other = scratch();
        let mut again = EventLog::open(&other, "form-1", &json!(1)).unwrap();
        again.capture_draft(&draft(4), "events", HOST, 99).unwrap();
        assert_eq!(again.batch().unwrap().ids, ids);
        // No array under the key, or another key: nothing to import.
        assert_eq!(log.capture_draft(&json!({"events": "no"}), "events", HOST, 5).unwrap(), 0);
        assert_eq!(log.capture_draft(&draft(9), "other", HOST, 5).unwrap(), 0);
    }

    #[test]
    fn a_draft_array_has_a_bound() {
        let dir = scratch();
        let mut log = EventLog::open(&dir, "form-1", &json!(1)).unwrap();
        let huge = json!({"events": vec![json!({}); MAX_DRAFT_EVENTS + 1]});
        let error = log.capture_draft(&huge, "events", HOST, 1).unwrap_err();
        assert!(error.contains("too large"), "{error}");
        assert_eq!(log.pending(), 0);
    }

    #[test]
    fn the_draft_key_policy_is_kept_per_host() {
        let dir = scratch();
        let log = EventLog::open(&dir, "form-1", &json!(1)).unwrap();
        assert_eq!(log.cached_draft_key(HOST), None);
        log.save_draft_key(HOST, Some("events")).unwrap();
        assert_eq!(log.cached_draft_key(HOST), Some(Some("events".into())));
        assert_eq!(log.cached_draft_key("srv-2|http://other:7682"), None);
        log.save_draft_key(HOST, None).unwrap();
        assert_eq!(log.cached_draft_key(HOST), Some(None));
    }
}
