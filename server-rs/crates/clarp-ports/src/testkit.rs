//! The suites every implementation of a port must pass.
//!
//! An implementation's own tests call these and add what is particular to it. A suite panics
//! with a message that names the rule that was broken.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

use crate::codec::{CodecSet, Compressor};
use crate::log::{Item, LogEngine, NewItem};

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

fn item(turn: Option<u64>, kind: u16, ts_ms: i64, body: &[u8]) -> NewItem {
    NewItem {
        turn,
        kind,
        ts_ms,
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
    assert!(read.body == wrote.body, "the body of item {seq} survives");
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
        item(None, 1, 1_000, b"hello"),
        item(Some(1), 2, 1_001, b""),
        item(Some(1), 3, 1_002, &big),
    ];
    let second = vec![
        item(Some(2), 2, 2_000, b"again"),
        item(None, 9, 2_001, b"{\"k\":1}"),
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
        log.read_tail(10).expect("an empty log reads").is_empty(),
        "an empty log has no tail"
    );
    assert!(
        log.read_after(0, 10)
            .expect("an empty log reads")
            .is_empty(),
        "an empty log has nothing after 0"
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

    let all = log.read_after(0, 10).expect("read after 0");
    assert_eq!(all.len(), 3);
    for (i, wrote) in first.iter().enumerate() {
        same(&all[i], i as u64 + 1, wrote);
    }
    let one = log.read_after(1, 1).expect("read after 1");
    assert_eq!(one.len(), 1, "the limit is honoured");
    same(&one[0], 2, &first[1]);
    assert!(
        log.read_after(3, 10)
            .expect("read after the end")
            .is_empty(),
        "nothing lies after the end"
    );
    assert!(
        log.read_after(99, 10)
            .expect("read after beyond the end")
            .is_empty()
    );
    let tail = log.read_tail(2).expect("read tail");
    assert_eq!(
        tail.iter().map(|i| i.seq).collect::<Vec<_>>(),
        vec![2, 3],
        "a tail is the newest items, oldest first"
    );
    assert!(log.read_tail(0).expect("an empty tail").is_empty());
    assert_eq!(
        log.read_tail(50).expect("a long tail").len(),
        3,
        "a tail longer than the log is the whole log"
    );

    assert_eq!(log.append(&second).expect("a second append"), 5);
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
        log.append(&[item(Some(3), 4, 3_000, b"after the switch")])
            .expect("append after the switch"),
        6
    );
    let everything = log.read_after(0, 100).expect("read everything");
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
    assert_eq!(log.read_tail(1).expect("read tail")[0].seq, 6);
}
