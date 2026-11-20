//! Count operations at the fleet's actual storage port, including on reopen.
use super::*;
use crate::{
    config::Config, fleet, hub::Hub, launcher::Launcher, owner::OwnerFile, telling::Telling,
};
use clarp_ports::{
    blobs::BlobEngine,
    clock::SystemClock,
    kv::{KeyValue, KeyValueEngine, StoreError, Write},
};
use clarp_store_mem::{MemBlobs, MemEngine, MemKeyValue};
use std::{path::Path, sync::Mutex};

#[derive(Debug, PartialEq, Eq)]
enum Op {
    Read(String, String),
    Write(String, String),
    Batch(Vec<Op>),
    Scan(String),
    Delete(String, String),
}
#[derive(Default)]
struct Counts {
    ops: Vec<Op>,
    fail_write: Option<String>,
}
struct Counted {
    inner: Box<dyn KeyValue>,
    counts: Arc<Mutex<Counts>>,
}
impl KeyValue for Counted {
    fn get(&self, table: &str, key: &str) -> std::result::Result<Option<Vec<u8>>, StoreError> {
        self.counts
            .lock()
            .unwrap()
            .ops
            .push(Op::Read(table.into(), key.into()));
        self.inner.get(table, key)
    }
    fn put(&mut self, table: &str, key: &str, value: &[u8]) -> std::result::Result<(), StoreError> {
        let mut counts = self.counts.lock().unwrap();
        counts.ops.push(Op::Write(table.into(), key.into()));
        if counts.fail_write.as_deref() == Some(table) {
            return Err(StoreError("cut before write".into()));
        }
        self.inner.put(table, key, value)
    }
    fn put_together(&mut self, writes: &[Write]) -> std::result::Result<(), StoreError> {
        let mut counts = self.counts.lock().unwrap();
        counts.ops.push(Op::Batch(
            writes
                .iter()
                .map(|(table, key, value)| match value {
                    Some(_) => Op::Write(table.clone(), key.clone()),
                    None => Op::Delete(table.clone(), key.clone()),
                })
                .collect(),
        ));
        if writes
            .iter()
            .any(|(table, _, _)| counts.fail_write.as_ref() == Some(table))
        {
            return Err(StoreError("cut before commit".into()));
        }
        self.inner.put_together(writes)
    }
    fn list(&self, table: &str) -> std::result::Result<Vec<(String, Vec<u8>)>, StoreError> {
        self.counts.lock().unwrap().ops.push(Op::Scan(table.into()));
        self.inner.list(table)
    }
    fn delete(&mut self, table: &str, key: &str) -> std::result::Result<bool, StoreError> {
        self.counts
            .lock()
            .unwrap()
            .ops
            .push(Op::Delete(table.into(), key.into()));
        self.inner.delete(table, key)
    }
}

struct Place {
    engine: MemKeyValue,
    counts: Arc<Mutex<Counts>>,
}
impl Place {
    fn new() -> Self {
        Self {
            engine: MemKeyValue::new(),
            counts: Arc::default(),
        }
    }
    fn start(&self) -> FleetHandle {
        let dir = std::env::temp_dir().join("clarp-vocabulary-counting-store");
        let config = Config::from_args(
            [
                "--data",
                dir.to_str().unwrap(),
                "--run",
                dir.to_str().unwrap(),
                "--cwd",
                dir.to_str().unwrap(),
            ]
            .into_iter()
            .map(String::from),
        )
        .unwrap();
        let works = fleet::Works {
            runtime: tokio::runtime::Handle::current(),
            opening_gate: crate::calls::opening_launch::channel().0,
            config: Arc::new(config),
            engine: Arc::new(MemEngine::new()),
            codecs: clarp_codec::standard_set("zstd").unwrap(),
            launcher: Arc::new(Launcher::by_name("direct", dir.join("unused-shim")).unwrap()),
            clock: Arc::new(SystemClock),
            hub: Arc::new(Hub::new(0)),
            revisions: Arc::default(),
            tool_env: Vec::new(),
            pusher: crate::pusher::pusher().0,
            blobs: MemBlobs::new().open(&dir).unwrap(),
            speaks: crate::voice::mouth().0,
            telling: Telling::new(OwnerFile::new(Some(dir.join("absent.toml")))),
            tally: crate::usage::tally().0,
            turns: std::sync::Arc::default(),
            pools: Vec::new(),
            log: crate::unkept::Log::stderr(Arc::new(SystemClock)),
            left_out: Arc::default(),
        };
        let store = Counted {
            inner: self.engine.open(Path::new("vocab-test")).unwrap(),
            counts: self.counts.clone(),
        };
        fleet::start(works, Box::new(store)).unwrap()
    }
    fn take(&self) -> Vec<Op> {
        std::mem::take(&mut self.counts.lock().unwrap().ops)
    }
    fn cut(&self, kind: Option<&str>) {
        self.counts.lock().unwrap().fail_write = kind.map(|k| format!("kept.{k}"));
    }
}
fn read(k: &str, key: &str) -> Op {
    Op::Read(format!("kept.{k}"), key.into())
}
fn write(k: &str, key: &str) -> Op {
    Op::Write(format!("kept.{k}"), key.into())
}
fn clip(session: &str, trace: &str, speech: &str) -> Run {
    Run {
        session: session.into(),
        trace: trace.into(),
        agent: String::new(),
        profile: None,
        provider: "local".into(),
        model: String::new(),
        audit: json!({}),
        transcript: speech.into(),
        latency: 0,
        now: 0,
    }
}

#[tokio::test]
async fn a_clip_and_a_restart_use_only_bounded_keyed_records() {
    let place = Place::new();
    let fleet = place.start();
    let desk = Desk::default();
    desk.administration(&fleet, "packs", &json!({"name":"Test"}), 0, true, true)
        .await
        .unwrap();
    place.take();

    desk.inputs(&fleet, "A").await.unwrap();
    assert_eq!(
        desk.record(&fleet, clip("A", "voice", "")).await.unwrap(),
        1
    );
    desk.finish(&fleet, 1, "one".into(), 7, None).await.unwrap();
    assert_eq!(
        place.take(),
        vec![
            read(BOOK, "book"),
            read(SESSIONS, "A"),
            read(COUNTER, "last"),
            read(SESSIONS, "A"),
            read(GLOBAL, "all"),
            Op::Batch(vec![
                write(COUNTER, "last"),
                write(SESSIONS, "A"),
                write(GLOBAL, "all"),
                write(RUNS, "1"),
                write(TRACES, "voice")
            ]),
            read(RUNS, "1"),
            read(SESSIONS, "A"),
            Op::Batch(vec![write(SESSIONS, "A"), write(RUNS, "1")]),
        ]
    );
    let late = desk.record(&fleet, clip("A", "voice", "")).await.unwrap();
    desk.record(&fleet, clip("A", "voice", "three"))
        .await
        .unwrap();
    desk.record(&fleet, clip("A", "voice", "four"))
        .await
        .unwrap();
    desk.finish(&fleet, late, "two".into(), 1, None)
        .await
        .unwrap();
    for _ in 0..201 {
        desk.record(&fleet, clip("A", "", "")).await.unwrap();
    }
    let last = desk
        .record(&fleet, clip("B", "other", "other words"))
        .await
        .unwrap();
    drop(fleet);
    drop(desk);
    place.take();
    let fleet = place.start();
    let desk = Desk::default();
    let opening = place.take();
    assert!(
        !opening
            .iter()
            .any(|op| matches!(op,Op::Scan(k) if k.starts_with("kept.vocabulary")))
    );

    let row = desk
        .report(&fleet, "run", &json!({"trace_id":"voice"}))
        .await
        .unwrap();
    assert_eq!(
        (row["run_id"].as_i64(), row["transcript"].as_str()),
        (Some(4), Some("four"))
    );
    assert_eq!(place.take(), vec![read(TRACES, "voice"), read(RUNS, "4")]);
    let rows = desk
        .report(&fleet, "runs", &json!({"session":"A","limit":"2"}))
        .await
        .unwrap();
    assert_eq!(rows["runs"][0]["run_id"], last - 1);
    assert_eq!(rows["runs"][1]["run_id"], last - 2);
    assert_eq!(
        place.take(),
        vec![
            read(SESSIONS, "A"),
            read(RUNS, &(last - 1).to_string()),
            read(RUNS, &(last - 2).to_string())
        ]
    );
    assert_eq!(
        desk.inputs(&fleet, "A").await.unwrap().1,
        ["four", "three", "two"]
    );
    assert_eq!(
        place.take(),
        vec![
            read(BOOK, "book"),
            read(SESSIONS, "A"),
            read(RUNS, "4"),
            read(RUNS, "3"),
            read(RUNS, "2")
        ]
    );
    let rows = desk
        .report(&fleet, "runs", &json!({"limit":"999"}))
        .await
        .unwrap();
    let ids: Vec<_> = rows["runs"]
        .as_array()
        .unwrap()
        .iter()
        .map(|r| r["run_id"].as_i64().unwrap())
        .collect();
    assert_eq!(ids, (last - 199..=last).rev().collect::<Vec<_>>());
    // The page on disk is bounded too, even if the app always asks for two.
    let session: Session = load(&fleet, SESSIONS, "A").await.unwrap().unwrap();
    assert_eq!(session.newest, (last - 200..last).rev().collect::<Vec<_>>());
    let all: Vec<i64> = load(&fleet, GLOBAL, "all").await.unwrap().unwrap();
    assert_eq!(all, ids);
    assert_eq!(
        desk.record(&fleet, clip("B", "other", "last"))
            .await
            .unwrap(),
        last + 1
    );
}

#[tokio::test]
async fn an_interrupted_batch_preserves_every_key_and_the_next_number() {
    for kind in [COUNTER, SESSIONS, GLOBAL, TRACES, RUNS] {
        let place = Place::new();
        let fleet = place.start();
        let desk = Desk::default();
        desk.record(&fleet, clip("A", "first", "first"))
            .await
            .unwrap();
        place.cut(Some(kind));
        assert!(
            desk.record(&fleet, clip("A", "cut", "missing"))
                .await
                .is_err()
        );
        drop(fleet);
        drop(desk);
        place.cut(None);
        let fleet = place.start();
        let desk = Desk::default();
        assert_eq!(
            desk.report(&fleet, "run", &json!({"trace_id":"cut"}))
                .await
                .unwrap_err()
                .0,
            StatusCode::NOT_FOUND
        );
        for query in [json!({"limit":"1"}), json!({"session":"A","limit":"1"})] {
            assert_eq!(
                desk.report(&fleet, "runs", &query).await.unwrap()["runs"][0]["run_id"],
                1
            );
        }
        assert_eq!(desk.inputs(&fleet, "A").await.unwrap().1, ["first"]);
        assert_eq!(load::<i64>(&fleet, COUNTER, "last").await.unwrap(), Some(1));
        assert_eq!(
            load::<Vec<i64>>(&fleet, GLOBAL, "all")
                .await
                .unwrap()
                .unwrap(),
            vec![1]
        );
        let index: Session = load(&fleet, SESSIONS, "A").await.unwrap().unwrap();
        assert_eq!(index.newest, [1]);
        assert_eq!(index.spoken, [1]);
        assert!(load::<i64>(&fleet, TRACES, "cut").await.unwrap().is_none());
        assert!(load::<Run>(&fleet, RUNS, "2").await.unwrap().is_none());
        let next = desk
            .record(&fleet, clip("A", "next", "next"))
            .await
            .unwrap();
        assert_eq!(next, 2);
    }
}

#[tokio::test]
async fn interrupted_finish_keeps_the_unanswered_clip_out_of_recent_speech() {
    let place = Place::new();
    let fleet = place.start();
    let desk = Desk::default();
    desk.record(&fleet, clip("A", "prior", "prior"))
        .await
        .unwrap();
    let id = desk.record(&fleet, clip("A", "clip", "")).await.unwrap();
    place.cut(Some(RUNS));
    assert!(
        desk.finish(&fleet, id, "partial".into(), 5, None)
            .await
            .is_err()
    );
    drop(fleet);
    drop(desk);
    let fleet = place.start();
    let desk = Desk::default();
    assert_eq!(desk.inputs(&fleet, "A").await.unwrap().1, ["prior"]);
    let row = desk
        .report(&fleet, "run", &json!({"trace_id":"clip"}))
        .await
        .unwrap();
    assert_eq!(row["run_id"], id);
    assert_eq!(row["transcript"], "");
    let index: Session = load(&fleet, SESSIONS, "A").await.unwrap().unwrap();
    assert_eq!(index.spoken, [1]);
    place.cut(None);
    desk.finish(&fleet, id, "completed".into(), 8, None)
        .await
        .unwrap();
    assert_eq!(
        desk.inputs(&fleet, "A").await.unwrap().1,
        ["completed", "prior"]
    );
}

#[tokio::test]
async fn refused_settings_batch_preserves_the_directory_and_store() {
    let place = Place::new();
    let fleet = place.start();
    let settings = |a: &str, b: &str| {
        vec![
            ("transcription.call_languages".into(), a.into()),
            ("transcription.call_engines".into(), b.into()),
        ]
    };
    fleet.set(settings("en", "old")).await.unwrap();
    place.take();
    place.counts.lock().unwrap().fail_write = Some("setting".into());
    for changed in [
        settings("de", "new"),
        vec![("transcription.call_engines".to_string(), "new".to_string())],
    ] {
        let expected = Op::Batch(
            changed
                .iter()
                .map(|(k, _)| Op::Write("setting".into(), k.clone()))
                .collect(),
        );
        let snapshot = fleet.directory();
        assert!(fleet.set(changed).await.is_err());
        assert_eq!(place.take(), vec![expected]);
        assert!(Arc::ptr_eq(&snapshot, &fleet.directory()));
    }
    place.cut(None);
    fleet
        .set(vec![("other.setting".into(), "next".into())])
        .await
        .unwrap();
    for reader in [&fleet, &place.start()] {
        assert_eq!(
            reader.directory().setting("transcription.call_languages"),
            Some("en")
        );
        assert_eq!(
            reader.directory().setting("transcription.call_engines"),
            Some("old")
        );
        assert_eq!(reader.directory().setting("other.setting"), Some("next"));
    }
}

#[tokio::test]
async fn a_record_it_cannot_read_is_set_aside_when_read_and_its_counter_is_not_begun_anew() {
    let place = Place::new();
    let fleet = place.start();
    let desk = Desk::default();
    let first = desk
        .record(&fleet, clip("A", "voice", "one"))
        .await
        .unwrap();
    assert_eq!(first, 1);
    // Written where they are kept: a run of another shape, and a counter that is no number.
    let mut kept = place.engine.open(Path::new("vocab-test")).unwrap();
    let other: &[u8] = br#"{"other":1}"#;
    kept.put("kept.vocabulary-runs", "1", other).unwrap();
    kept.put("kept.vocabulary-counter", "last", b"\"one\"")
        .unwrap();
    // Nothing was read at a start of it: counted only when read.
    drop(fleet);
    let fleet = place.start();
    assert!(fleet.left_out().counts().is_empty());
    // The run is not this desk's: set aside as it came and counted, and no run is answered.
    let asked = desk
        .report(&fleet, "run", &json!({ "trace_id": "voice" }))
        .await;
    assert!(asked.is_err());
    assert_eq!(fleet.left_out().counts().get("vocabulary-runs"), Some(&1));
    let aside = kept.get("kept.vocabulary-runs", "1.unreadable").unwrap();
    assert_eq!(aside.as_deref(), Some(other));
    // The counter is not begun anew: no run is recorded over the one kept.
    let refused = desk.record(&fleet, clip("B", "", "two")).await;
    assert_eq!(
        refused.err().map(|(code, _)| code),
        Some(StatusCode::SERVICE_UNAVAILABLE)
    );
    assert_eq!(
        fleet.left_out().counts().get("vocabulary-counter"),
        Some(&1)
    );
    // Moved aside, its key free: the next clip is not numbered anew over the run either.
    let again = desk.record(&fleet, clip("B", "", "three")).await;
    assert_eq!(
        again.err().map(|(code, _)| code),
        Some(StatusCode::SERVICE_UNAVAILABLE)
    );
    assert_eq!(kept.get("kept.vocabulary-runs", "1").unwrap(), None);
    let aside = kept.get("kept.vocabulary-runs", "1.unreadable").unwrap();
    assert_eq!(aside.as_deref(), Some(other));
}
