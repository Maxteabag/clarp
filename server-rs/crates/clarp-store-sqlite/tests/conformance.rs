//! The SQLite engine passes the suite every log engine must pass, and keeps its own promises.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_codec::standard_set;
use clarp_ports::log::{Kinds, LogEngine, NewItem};
use clarp_ports::testkit::{key_value_conformance, log_engine_conformance, scratch_dir};
use clarp_store_sqlite::{LOG_FILE, SqliteEngine, SqliteKeyValue};

#[test]
fn sqlite_passes_the_log_engine_suite() {
    let root = scratch_dir("sqlite-suite");
    let none = standard_set("none").unwrap();
    let zstd = standard_set("zstd").unwrap();
    log_engine_conformance(
        &SqliteEngine::new(),
        &root.join("agents/a"),
        &root.join("agents/b"),
        &none,
        &zstd,
    );
    std::fs::remove_dir_all(root).unwrap();
}

#[test]
fn sqlite_passes_the_suite_starting_compressed() {
    let root = scratch_dir("sqlite-suite-zstd");
    let none = standard_set("none").unwrap();
    let zstd = standard_set("zstd").unwrap();
    log_engine_conformance(
        &SqliteEngine::new(),
        &root.join("agents/a"),
        &root.join("agents/b"),
        &zstd,
        &none,
    );
    std::fs::remove_dir_all(root).unwrap();
}

#[test]
fn an_agent_is_one_directory_with_one_log_file() {
    let root = scratch_dir("sqlite-dir");
    let dir = root.join("agents/maximus");
    let mut log = SqliteEngine::new()
        .open(&dir, standard_set("zstd").unwrap())
        .unwrap();
    log.append(&[NewItem {
        turn: None,
        kind: 1,
        ts_ms: 1,
        key: None,
        body: b"hello".to_vec(),
    }])
    .unwrap();
    drop(log);
    assert!(
        dir.join(LOG_FILE).is_file(),
        "the log is {LOG_FILE} inside the agent's directory"
    );

    // Moving the directory moves the agent's history.
    let moved = root.join("elsewhere/maximus");
    std::fs::create_dir_all(moved.parent().unwrap()).unwrap();
    std::fs::rename(&dir, &moved).unwrap();
    let log = SqliteEngine::new()
        .open(&moved, standard_set("zstd").unwrap())
        .unwrap();
    assert_eq!(log.read_tail(Kinds::ALL, 1).unwrap()[0].body, b"hello");
    drop(log);
    std::fs::remove_dir_all(root).unwrap();
}

#[test]
fn a_failed_append_leaves_the_log_as_it_was() {
    use clarp_ports::codec::{CodecError, CodecSet, CodecTag, Compressor};
    use std::sync::Arc;

    /// Refuses any body that contains a zero byte, to fail in the middle of a batch.
    struct Picky;
    impl Compressor for Picky {
        fn name(&self) -> &str {
            "picky"
        }
        fn compress(&self, plain: &[u8]) -> Result<Vec<u8>, CodecError> {
            if plain.contains(&0) {
                Err(CodecError::Encode("a zero byte".to_string()))
            } else {
                Ok(plain.to_vec())
            }
        }
        fn decompress(&self, stored: &[u8]) -> Result<Vec<u8>, CodecError> {
            Ok(stored.to_vec())
        }
    }

    let root = scratch_dir("sqlite-atomic");
    let dir = root.join("agents/a");
    let mut log = SqliteEngine::new()
        .open(&dir, CodecSet::new(CodecTag(99), Arc::new(Picky)))
        .unwrap();
    let ok = NewItem {
        turn: None,
        kind: 1,
        ts_ms: 1,
        key: None,
        body: b"fine".to_vec(),
    };
    let bad = NewItem {
        turn: None,
        kind: 1,
        ts_ms: 2,
        key: None,
        body: vec![1, 0, 1],
    };
    assert_eq!(log.append(std::slice::from_ref(&ok)).unwrap(), 1);
    assert!(
        log.append(&[ok.clone(), bad, ok.clone()]).is_err(),
        "a batch with a body that cannot be stored is refused"
    );
    assert_eq!(
        log.last_seq().unwrap(),
        1,
        "all or none: the items before the failure were not kept"
    );
    assert_eq!(
        log.append(std::slice::from_ref(&ok)).unwrap(),
        2,
        "and the next position is the next one"
    );
    drop(log);
    std::fs::remove_dir_all(root).unwrap();
}

#[test]
fn a_log_from_a_newer_build_is_refused() {
    let root = scratch_dir("sqlite-newer");
    let dir = root.join("agents/a");
    drop(
        SqliteEngine::new()
            .open(&dir, standard_set("none").unwrap())
            .unwrap(),
    );
    let conn = rusqlite::Connection::open(dir.join(LOG_FILE)).unwrap();
    conn.execute_batch("PRAGMA user_version=999;").unwrap();
    drop(conn);
    assert!(
        SqliteEngine::new()
            .open(&dir, standard_set("none").unwrap())
            .is_err(),
        "older code does not open a newer log"
    );
    std::fs::remove_dir_all(root).unwrap();
}

#[test]
fn sqlite_passes_the_key_value_suite() {
    let root = scratch_dir("sqlite-kv");
    key_value_conformance(
        &SqliteKeyValue::new(),
        &root.join("a/fleet.db"),
        &root.join("b/fleet.db"),
    );
    std::fs::remove_dir_all(root).unwrap();
}

#[test]
fn a_failed_key_value_batch_rolls_back_prior_writes_on_reopen() {
    use clarp_ports::kv::KeyValueEngine;
    let root = scratch_dir("kv-aborted-batch");
    let db = root.join("fleet.db");
    let engine = SqliteKeyValue;
    let mut store = engine.open(&db).unwrap();
    store.put("settings", "first", b"old").unwrap();
    rusqlite::Connection::open(&db).unwrap().execute_batch("CREATE TRIGGER reject_second BEFORE INSERT ON kv WHEN NEW.key='second' BEGIN SELECT RAISE(ABORT,'cut'); END;").unwrap();
    assert!(
        store
            .put_together(&[
                ("settings".into(), "first".into(), Some(b"new".to_vec())),
                ("settings".into(), "second".into(), Some(b"value".to_vec()))
            ])
            .is_err()
    );
    drop(store);
    let reopened = engine.open(&db).unwrap();
    assert_eq!(
        reopened.list("settings").unwrap(),
        vec![("first".to_string(), b"old".to_vec())]
    );
    drop(reopened);
    std::fs::remove_dir_all(root).unwrap();
}

#[cfg(target_os = "linux")]
#[test]
fn log_at_rest_releases_cached_pages_and_keeps_the_normal_cache_target() {
    // syscr counts reads made by this thread, including SQLite pread calls. Reading
    // the counter itself costs the same on each pass. The OS may cache every file.
    fn reads() -> u64 {
        std::fs::read_to_string("/proc/thread-self/io")
            .unwrap()
            .lines()
            .find_map(|line| line.strip_prefix("syscr: "))
            .unwrap()
            .parse()
            .unwrap()
    }
    let dir = scratch_dir("sqlite-log-at-rest");
    let mut log = SqliteEngine::new()
        .open(&dir, standard_set("none").unwrap())
        .unwrap();
    let rows: Vec<_> = (0..128)
        .map(|i| NewItem::new(1, i, vec![5; 4096]))
        .collect();
    log.append(&rows).unwrap();
    let scan = || {
        let before = reads();
        assert_eq!(log.read_tail(Kinds::ALL, 128).unwrap().len(), 128);
        reads() - before
    };
    scan();
    let warm = scan();
    log.at_rest();
    let refill = scan();
    assert!(
        refill > warm + 100,
        "the released pages needed file reads: {refill} versus {warm}"
    );
    let warm_again = scan();
    assert!(
        warm_again + 100 < refill,
        "the same connection can cache the full scan again"
    );
    drop(log);
    std::fs::remove_dir_all(dir).unwrap();
}
