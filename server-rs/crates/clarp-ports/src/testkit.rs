//! The suites every implementation of a port must pass.
//!
//! An implementation's own tests call these and add what is particular to it. A suite panics
//! with a message that names the rule that was broken.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

use crate::blobs::BlobEngine;
use crate::codec::{CodecSet, Compressor};
use crate::kv::KeyValueEngine;
use crate::log::{Item, Kinds, LogEngine, NewItem, Seq};

static SCRATCH_COUNTER: AtomicU64 = AtomicU64::new(0);

/// A new empty directory under the system's temporary directory. The caller removes it.
pub fn scratch_dir(label: &str) -> PathBuf {
    let n = SCRATCH_COUNTER.fetch_add(1, Ordering::SeqCst);
    let dir =
        std::env::temp_dir().join(format!("clarp-testkit-{label}-{}-{n}", std::process::id()));
    if dir.exists() {
        std::fs::remove_dir_all(&dir).expect("a stale scratch directory can be removed");
    }
    std::fs::create_dir_all(&dir).expect("a scratch directory can be created");
    dir
}

/// Bytes that look like nothing in particular, the same on every run.
pub fn noise(len: usize, seed: u64) -> Vec<u8> {
    let mut state = seed.wrapping_mul(0x9E37_79B9_7F4A_7C15) | 1;
    let mut out = Vec::with_capacity(len);
    for _ in 0..len {
        state ^= state << 13;
        state ^= state >> 7;
        state ^= state << 17;
        out.push((state >> 24) as u8);
    }
    out
}

/// What every [`Compressor`] must do: give back exactly what it was given.
pub fn compressor_conformance(codec: &dyn Compressor) {
    assert!(!codec.name().is_empty(), "a codec has a name");
    let prose = b"The Host answers the apps. The runtime owns the agents. ".repeat(400);
    let cases: Vec<(&str, Vec<u8>)> = vec![
        ("an empty body", Vec::new()),
        ("one byte", vec![7]),
        (
            "a small body",
            b"{\"role\":\"user\",\"text\":\"hello\"}".to_vec(),
        ),
        ("repetitive prose", prose),
        ("incompressible bytes", noise(70_000, 1)),
        ("a body of a megabyte", noise(1 << 20, 2)),
    ];
    for (what, plain) in cases {
        let stored = codec
            .compress(&plain)
            .unwrap_or_else(|e| panic!("{} compresses {what}: {e}", codec.name()));
        let back = codec
            .decompress(&stored)
            .unwrap_or_else(|e| panic!("{} reads back {what}: {e}", codec.name()));
        assert!(
            back == plain,
            "{} must return {what} unchanged",
            codec.name()
        );
    }
    // Bytes it never wrote may be refused or returned as they are; they must not take the process down.
    let _ = codec.decompress(&noise(300, 3));
}

fn item(turn: Option<u64>, kind: u16, ts_ms: i64, key: Option<&str>, body: &[u8]) -> NewItem {
    NewItem {
        turn,
        kind,
        ts_ms,
        key: key.map(str::to_string),
        body: body.to_vec(),
    }
}

fn same(read: &Item, seq: u64, wrote: &NewItem) {
    assert_eq!(
        read.seq, seq,
        "items come back at the position they were given"
    );
    assert_eq!(read.turn, wrote.turn, "the turn of item {seq} survives");
    assert_eq!(read.kind, wrote.kind, "the kind of item {seq} survives");
    assert_eq!(read.ts_ms, wrote.ts_ms, "the time of item {seq} survives");
    assert_eq!(read.key, wrote.key, "the key of item {seq} survives");
    assert!(read.body == wrote.body, "the body of item {seq} survives");
}

fn positions(items: &[Item]) -> Vec<u64> {
    items.iter().map(|item| item.seq).collect()
}

/// What every [`LogEngine`] must do.
///
/// `dir_a` and `dir_b` are two agents' directories. `codecs` is the set the log starts with;
/// `switched` can read what `codecs` wrote and writes with a different codec, as after a change
/// of configuration.
pub fn log_engine_conformance(
    engine: &dyn LogEngine,
    dir_a: &Path,
    dir_b: &Path,
    codecs: &CodecSet,
    switched: &CodecSet,
) {
    assert!(!engine.name().is_empty(), "an engine has a name");
    assert_ne!(
        codecs.write_tag(),
        switched.write_tag(),
        "the suite needs two different writing codecs"
    );

    let big = noise(300_000, 9);
    let first = vec![
        item(None, 1, 1_000, Some("greeting"), b"hello"),
        item(Some(1), 2, 1_001, None, b""),
        item(Some(1), 3, 1_002, None, &big),
    ];
    let second = vec![
        item(Some(2), 2, 2_000, Some("greeting"), b"again"),
        item(None, 9, 2_001, Some("other"), b"{\"k\":1}"),
    ];

    let mut log = engine
        .open(dir_a, codecs.clone())
        .expect("an engine opens a log that is not there yet");
    assert_eq!(
        log.last_seq().expect("an empty log has a last position"),
        0,
        "an empty log ends at 0"
    );
    assert!(
        log.read_tail(Kinds::ALL, 10)
            .expect("an empty log reads")
            .is_empty(),
        "an empty log has no tail"
    );
    assert!(
        log.read_after(0, Kinds::ALL, 10)
            .expect("an empty log reads")
            .is_empty(),
        "an empty log has nothing after 0"
    );
    assert!(
        log.find("greeting").expect("an empty log finds").is_none(),
        "an empty log holds no key"
    );

    assert_eq!(
        log.append(&first).expect("an append succeeds"),
        3,
        "the first three items end at 3"
    );
    assert_eq!(
        log.append(&[]).expect("an empty append succeeds"),
        3,
        "appending nothing changes nothing"
    );
    assert_eq!(log.last_seq().expect("last position"), 3);

    let all = log.read_after(0, Kinds::ALL, 10).expect("read after 0");
    assert_eq!(all.len(), 3);
    for (i, wrote) in first.iter().enumerate() {
        same(&all[i], i as u64 + 1, wrote);
    }
    let one = log.read_after(1, Kinds::ALL, 1).expect("read after 1");
    assert_eq!(one.len(), 1, "the limit is honoured");
    same(&one[0], 2, &first[1]);
    assert!(
        log.read_after(3, Kinds::ALL, 10)
            .expect("read after the end")
            .is_empty(),
        "nothing lies after the end"
    );
    assert!(
        log.read_after(99, Kinds::ALL, 10)
            .expect("read after beyond the end")
            .is_empty()
    );
    let tail = log.read_tail(Kinds::ALL, 2).expect("read tail");
    assert_eq!(
        positions(&tail),
        vec![2, 3],
        "a tail is the newest items, oldest first"
    );
    assert!(
        log.read_tail(Kinds::ALL, 0)
            .expect("an empty tail")
            .is_empty()
    );
    assert_eq!(
        log.read_tail(Kinds::ALL, 50).expect("a long tail").len(),
        3,
        "a tail longer than the log is the whole log"
    );
    same(
        &log.find("greeting")
            .expect("find a key")
            .expect("the key is there"),
        1,
        &first[0],
    );

    assert_eq!(log.append(&second).expect("a second append"), 5);

    // Reads by kind skip the other kinds and still honour the limit and the direction.
    let twos = log.read_after(0, Kinds::only(2), 10).expect("read a kind");
    assert_eq!(
        positions(&twos),
        vec![2, 4],
        "a read by kind returns that kind only"
    );
    assert_eq!(
        positions(
            &log.read_after(0, Kinds::range(2, 3), 2)
                .expect("read a range of kinds")
        ),
        vec![2, 3],
        "the limit counts the items returned, not the items passed over"
    );
    assert_eq!(
        positions(
            &log.read_after(2, Kinds::only(2), 10)
                .expect("read a kind after a position")
        ),
        vec![4]
    );
    assert_eq!(
        positions(
            &log.read_before(4, Kinds::ALL, 2)
                .expect("read before a position")
        ),
        vec![2, 3],
        "before a position: the newest ones below it, oldest first"
    );
    assert_eq!(
        positions(
            &log.read_before(Seq::MAX, Kinds::only(2), 1)
                .expect("read the newest of a kind")
        ),
        vec![4]
    );
    assert_eq!(
        positions(
            &log.read_before(5, Kinds::range(1, 2), 10)
                .expect("read kinds before a position")
        ),
        vec![1, 2, 4]
    );
    assert!(
        log.read_before(1, Kinds::ALL, 10)
            .expect("read before the first")
            .is_empty(),
        "nothing lies before the first item"
    );
    assert!(
        log.read_before(0, Kinds::ALL, 10)
            .expect("read before 0")
            .is_empty()
    );
    assert!(
        log.read_after(0, Kinds::only(77), 10)
            .expect("read a kind nobody wrote")
            .is_empty()
    );

    // A key names the thing an item is a version of; the newest version is the one found.
    same(
        &log.find("greeting")
            .expect("find a key")
            .expect("the key is there"),
        4,
        &second[0],
    );
    same(
        &log.find("other")
            .expect("find a key")
            .expect("the key is there"),
        5,
        &second[1],
    );
    assert!(
        log.find("nobody").expect("find a missing key").is_none(),
        "a key nobody wrote is not found"
    );
    assert!(
        log.find("").expect("find the empty key").is_none(),
        "an item without a key is not found under the empty key"
    );
    drop(log);

    // Another agent's directory is another log.
    let other = engine
        .open(dir_b, codecs.clone())
        .expect("a second agent's log opens");
    assert_eq!(
        other.last_seq().expect("last position"),
        0,
        "one agent's items never appear in another's log"
    );
    assert!(
        other.find("greeting").expect("find").is_none(),
        "nor do its keys"
    );
    drop(other);

    // Opening again finds what was appended, and a changed compressor still reads the old items.
    let mut log = engine
        .open(dir_a, switched.clone())
        .expect("a log opens again");
    assert_eq!(
        log.last_seq().expect("last position"),
        5,
        "what was appended is there after reopening"
    );
    assert_eq!(
        log.find("greeting")
            .expect("find after reopening")
            .expect("the key is there")
            .seq,
        4,
        "keys survive reopening"
    );
    assert_eq!(
        log.append(&[item(Some(3), 4, 3_000, None, b"after the switch")])
            .expect("append after the switch"),
        6
    );
    let everything = log.read_after(0, Kinds::ALL, 100).expect("read everything");
    assert_eq!(everything.len(), 6);
    for (i, wrote) in first.iter().chain(second.iter()).enumerate() {
        same(&everything[i], i as u64 + 1, wrote);
    }
    assert!(
        everything[5].body == b"after the switch",
        "an item written with the new codec reads back"
    );
    drop(log);

    // And the first set still reads what it wrote, as long as it knows the second codec too.
    let both = switched
        .writing_with(codecs.write_tag())
        .expect("the switched set knows the first codec");
    let log = engine.open(dir_a, both).expect("a log opens a third time");
    assert_eq!(log.read_tail(Kinds::ALL, 1).expect("read tail")[0].seq, 6);
}

/// What every [`KeyValueEngine`] must do. `path_a` and `path_b` are two stores.
pub fn key_value_conformance(engine: &dyn KeyValueEngine, path_a: &Path, path_b: &Path) {
    assert!(!engine.name().is_empty(), "an engine has a name");
    let mut store = engine
        .open(path_a)
        .expect("an engine opens a store that is not there yet");
    assert!(
        store.get("agent", "a1").expect("get").is_none(),
        "a new store holds nothing"
    );
    assert!(store.list("agent").expect("list").is_empty());
    assert!(
        !store.delete("agent", "a1").expect("delete"),
        "removing what is not there says so"
    );

    store.put("agent", "b2", b"second").expect("put");
    store.put("agent", "a1", b"first").expect("put");
    store.put("setting", "a1", b"another table").expect("put");
    store
        .put("agent", "empty", b"")
        .expect("put an empty value");
    assert_eq!(
        store.get("agent", "a1").expect("get").as_deref(),
        Some(&b"first"[..])
    );
    assert_eq!(
        store.get("setting", "a1").expect("get").as_deref(),
        Some(&b"another table"[..]),
        "the same key in another table is another value"
    );
    assert_eq!(
        store.get("agent", "empty").expect("get").as_deref(),
        Some(&b""[..]),
        "an empty value is a value"
    );
    let big = noise(200_000, 5);
    store.put("agent", "a1", &big).expect("replace");
    assert!(
        store.get("agent", "a1").expect("get").as_deref() == Some(&big[..]),
        "a put replaces the value that was there"
    );
    let keys: Vec<String> = store
        .list("agent")
        .expect("list")
        .into_iter()
        .map(|(key, _)| key)
        .collect();
    assert_eq!(
        keys,
        vec!["a1", "b2", "empty"],
        "a table is listed by key, and only that table"
    );
    assert!(store.delete("agent", "b2").expect("delete"));
    assert!(store.get("agent", "b2").expect("get").is_none());
    assert!(
        store.get("setting", "a1").expect("get").is_some(),
        "removing a key leaves the other tables alone"
    );
    drop(store);

    // Another path is another store.
    let other = engine.open(path_b).expect("a second store opens");
    assert!(
        other.list("agent").expect("list").is_empty(),
        "one store's values never appear in another"
    );
    drop(other);

    // Opening again finds what was put.
    let store = engine.open(path_a).expect("a store opens again");
    assert!(store.get("agent", "a1").expect("get").as_deref() == Some(&big[..]));
    assert_eq!(store.list("agent").expect("list").len(), 2);
    assert_eq!(store.list("setting").expect("list").len(), 1);
}

/// The rules every store of whole files keeps. `path_a` and `path_b` are two places the
/// engine has not opened before.
pub fn blob_conformance(engine: &dyn BlobEngine, path_a: &Path, path_b: &Path) {
    assert!(!engine.name().is_empty(), "an engine has a name");
    let store = engine
        .open(path_a)
        .expect("an engine opens a store that is not there yet");
    assert_eq!(
        store.len("first.bin").expect("len"),
        None,
        "a new store keeps nothing"
    );
    assert!(
        store.read("first.bin", 0, 10).expect("read").is_empty(),
        "nothing is read from a name under which nothing is kept"
    );

    store.put("first.bin", b"first").expect("put");
    assert_eq!(store.len("first.bin").expect("len"), Some(5));
    assert_eq!(store.read("first.bin", 0, 100).expect("read"), b"first");
    assert_eq!(
        store.read("first.bin", 1, 3).expect("read"),
        b"irs",
        "a part is read from where it was asked, and no more than was asked"
    );
    assert!(
        store.read("first.bin", 5, 10).expect("read").is_empty(),
        "nothing is read at a file's end"
    );
    assert!(store.read("first.bin", 99, 1).expect("read").is_empty());
    assert!(store.read("first.bin", 0, 0).expect("read").is_empty());

    // A name stands for its bytes.
    store
        .put("first.bin", b"something else")
        .expect("put again");
    assert_eq!(
        store.read("first.bin", 0, 100).expect("read"),
        b"first",
        "what is kept under a name is never written again"
    );
    store.put("empty", b"").expect("put an empty file");
    assert_eq!(
        store.len("empty").expect("len"),
        Some(0),
        "an empty file is a file"
    );

    // A file of some size comes back whole, read in parts.
    let big = noise(300_000, 9);
    store.put("big-file_1.bin", &big).expect("put");
    assert_eq!(store.len("big-file_1.bin").expect("len"), Some(300_000));
    let mut back = Vec::new();
    loop {
        let part = store
            .read("big-file_1.bin", back.len() as u64, 65_536)
            .expect("read");
        if part.is_empty() {
            break;
        }
        assert!(part.len() <= 65_536, "no more is read than was asked");
        back.extend(part);
    }
    assert!(back == big, "a file is read back as it was kept");

    // What could be read as a path is no name.
    for name in ["", "..", "../outside", "a/b", ".hidden", "a b"] {
        assert!(
            store.put(name, b"x").is_err(),
            "{name:?} is not kept: it is no name"
        );
        assert_eq!(store.len(name).expect("len"), None, "{name:?}");
        assert!(
            store.read(name, 0, 10).expect("read").is_empty(),
            "{name:?}"
        );
    }

    // Many keep at the same time, the same name among them.
    std::thread::scope(|scope| {
        for writer in 0..8u64 {
            let store = &store;
            scope.spawn(move || {
                store.put("shared.bin", b"the same bytes").expect("put");
                let own = noise(10_000, writer + 20);
                let name = format!("own-{writer}.bin");
                store.put(&name, &own).expect("put");
                assert!(store.read(&name, 0, 20_000).expect("read") == own);
            });
        }
    });
    assert_eq!(
        store.read("shared.bin", 0, 100).expect("read"),
        b"the same bytes"
    );
    drop(store);

    // Another path is another store.
    let other = engine.open(path_b).expect("a second store opens");
    assert_eq!(
        other.len("first.bin").expect("len"),
        None,
        "one store's files never appear in another"
    );
    drop(other);

    // Opening again finds what was kept.
    let store = engine.open(path_a).expect("a store opens again");
    assert_eq!(store.read("first.bin", 0, 100).expect("read"), b"first");
    assert_eq!(store.len("big-file_1.bin").expect("len"), Some(300_000));
    assert_eq!(store.len("own-7.bin").expect("len"), Some(10_000));
}
