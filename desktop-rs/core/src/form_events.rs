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
pub fn directory(_root: &Path, _artifact_id: &str, _version: &Value) -> PathBuf {
    todo!()
}

/// A page's event id: a hyphenated UUID, as the iOS bridge makes them.
pub fn valid_event_id(_id: &str) -> bool {
    false
}

/// The wait before a send after `failures` failed ones in a row: a second
/// to batch a burst, then 2, 4 … 60 seconds.
pub fn retry_delay(_failures: u32) -> Duration {
    Duration::ZERO
}

/// A Host answer that retrying the same batch cannot change; the records
/// stay until the original connection recovers.
pub fn is_permanent(_status: u16) -> bool {
    false
}

/// The records a send carries, up to `cursor` (the last one's `client_seq`).
#[derive(Debug, Clone, PartialEq)]
pub struct Batch {
    pub body: Value,
    pub cursor: u64,
    pub ids: Vec<String>,
}

pub struct EventLog {
    dir: PathBuf,
}

impl EventLog {
    /// Opens (or starts) `artifact_id`'s journal in `dir`.
    pub fn open(dir: &Path, _artifact_id: &str, _version: &Value) -> Result<Self, String> {
        Err(format!("not implemented: {}", dir.display()))
    }

    /// Opens a journal from what it saved, with no artifact at hand (after a
    /// restart); None when it never logged anything.
    pub fn reopen(_dir: &Path) -> Result<Option<Self>, String> {
        Err("not implemented".into())
    }

    pub fn dir(&self) -> &Path {
        &self.dir
    }
    pub fn artifact_id(&self) -> &str {
        ""
    }
    pub fn version(&self) -> &Value {
        static NULL: Value = Value::Null;
        &NULL
    }
    /// The Host its records belong to, once it logged one.
    pub fn origin(&self) -> Option<&str> {
        None
    }
    pub fn pending(&self) -> usize {
        0
    }

    /// Appends `event` durably; false when `event_id` was already logged
    /// with the same event (nothing new is written).
    pub fn enqueue(&mut self, _event: &Value, _event_id: &str, _origin: &str, _now_ms: u64) -> Result<bool, String> {
        Err("not implemented".into())
    }

    /// Imports the entries `values[key]` gained since the last import, each
    /// under an id derived from its content and place; how many were new.
    pub fn capture_draft(&mut self, _values: &Value, _key: &str, _origin: &str, _now_ms: u64) -> Result<usize, String> {
        Err("not implemented".into())
    }

    /// The next records to send, oldest first.
    pub fn batch(&self) -> Option<Batch> {
        None
    }

    /// The Host's receipt for `batch`: once it names every record, they are
    /// acknowledged on disk and leave the queue.
    pub fn acknowledge(&mut self, _batch: &Batch, _receipt: &Value) -> Result<(), String> {
        Err("not implemented".into())
    }

    /// The draft key the Host's policy follows for `origin`, as last saved.
    pub fn cached_draft_key(&self, _origin: &str) -> Option<Option<String>> {
        None
    }

    /// Saves the Host's draft-key policy for `origin`.
    pub fn save_draft_key(&self, _origin: &str, _key: Option<&str>) -> Result<(), String> {
        Err("not implemented".into())
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
