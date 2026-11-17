//! The conversations Antigravity kept, as they are listed for whoever makes an agent out of
//! one, held to what the Python Host's own function lists for the same stores of agy
//! (`tests/fixtures/agy-sessions-vectors.json`, written by
//! `server-rs/tools/agy-sessions-vectors.py`).

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::path::PathBuf;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::{Duration, SystemTime};

use clarp_backend::past::{agy_kept, agy_sessions, agy_transcript};
use rusqlite::types::Value as Held;
use serde_json::Value;

static PLACES: AtomicUsize = AtomicUsize::new(0);

fn vectors() -> Value {
    serde_json::from_str(include_str!("fixtures/agy-sessions-vectors.json")).unwrap()
}

/// A made-up store of agy, written out as the Python Host was given it: its files, each
/// last written when the vectors say, and its catalogue where it has one.
fn place(store: &Value) -> PathBuf {
    let n = PLACES.fetch_add(1, Ordering::SeqCst);
    let name = format!("clarp-agy-sessions-{}-{n}", std::process::id());
    let base = std::env::temp_dir().join(name);
    let _ = std::fs::remove_dir_all(&base);
    for file in store["files"].as_array().unwrap() {
        let path = base.join(file["path"].as_str().unwrap());
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(&path, file["text"].as_str().unwrap()).unwrap();
        let written = SystemTime::UNIX_EPOCH + Duration::from_secs(file["mtime"].as_u64().unwrap());
        let file = std::fs::File::options().write(true).open(&path).unwrap();
        file.set_modified(written).unwrap();
    }
    if let Some(rows) = store["catalogue"].as_array() {
        let db = rusqlite::Connection::open(base.join("conversation_summaries.db")).unwrap();
        let table = "CREATE TABLE conversation_summaries (conversation_id, title text, preview text, \
                     step_count integer NOT NULL DEFAULT 0, last_modified_time datetime NOT NULL, \
                     workspace_uris text NOT NULL, nesting_depth integer NOT NULL DEFAULT 0)";
        db.execute(table, []).unwrap();
        let columns = [
            "conversation_id",
            "title",
            "preview",
            "workspace_uris",
            "nesting_depth",
            "last_modified_time",
        ];
        for row in rows {
            let held = columns.map(|column| match &row[column] {
                Value::Null => Held::Null,
                Value::String(text) => Held::Text(text.clone()),
                Value::Number(number) => Held::Integer(number.as_i64().unwrap()),
                other => panic!("{column}: {other}"),
            });
            let put = "INSERT INTO conversation_summaries (conversation_id, title, preview, \
                       workspace_uris, nesting_depth, last_modified_time) VALUES (?1,?2,?3,?4,?5,?6)";
            db.execute(put, rusqlite::params_from_iter(held)).unwrap();
        }
    }
    base
}

#[test]
fn the_conversations_antigravity_kept_are_listed_as_the_python_host_lists_them() {
    let vectors = vectors();
    let stores = vectors["stores"].as_array().unwrap();
    let (mut files, mut rows, mut lists, mut sessions, mut found) = (0, 0, 0, 0, 0);
    for store in stores {
        let what = format!("{}: {}", store["name"], store["what"]);
        let base = place(store);
        for asked in store["asked"].as_array().unwrap() {
            let cwd = asked["cwd"].as_str().unwrap();
            let limit = usize::try_from(asked["limit"].as_u64().unwrap()).unwrap();
            let ours = agy_sessions(&base, cwd, limit);
            let theirs = asked["sessions"].as_array().unwrap();
            for (at, (ours, theirs)) in ours.iter().zip(theirs).enumerate() {
                assert_eq!(
                    ours, theirs,
                    "{what}; {cwd:?}, {limit} at most, session {at}"
                );
                assert_eq!(
                    ours.to_string(),
                    theirs.to_string(),
                    "{what}; {cwd:?}: as text"
                );
            }
            assert_eq!(ours.len(), theirs.len(), "{what}; {cwd:?}, {limit} at most");
            lists += 1;
            sessions += ours.len();
        }
        // The transcript of a conversation by its name, and whether agy still has it.
        for row in store["found"].as_array().unwrap() {
            let conversation = row["conversation"].as_str().unwrap();
            let path = agy_transcript(&base, conversation);
            let path = path.map(|path| path.strip_prefix(&base).unwrap().display().to_string());
            assert_eq!(
                path.as_deref(),
                row["path"].as_str(),
                "{what}; {conversation:?}"
            );
            assert_eq!(
                agy_kept(&base, conversation),
                row["kept"].as_bool().unwrap(),
                "{what}; {conversation:?} kept"
            );
            found += 1;
        }
        files += store["files"].as_array().unwrap().len();
        rows += store["catalogue"].as_array().map_or(0, Vec::len);
        std::fs::remove_dir_all(&base).unwrap();
    }
    // Counted by a script over the vectors.
    assert_eq!(
        (stores.len(), files, rows, lists, sessions, found),
        (6, 192, 16, 102, 61, 36)
    );
}

#[test]
fn a_machine_without_a_store_of_agy_has_nothing_to_offer() {
    let base = std::env::temp_dir().join(format!("clarp-agy-no-store-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&base);
    assert_eq!(agy_sessions(&base, "", 100), Vec::<Value>::new());
    assert_eq!(agy_sessions(&base, "/srv/alpha", 20), Vec::<Value>::new());
    assert_eq!(agy_transcript(&base, "c-first"), None);
    // Nothing is known of what agy has: nothing counts as let go of.
    assert!(agy_kept(&base, "c-first"));
    // A name that would lead out of the store is no conversation's.
    for odd in ["..", "../c-first", "a/b"] {
        assert_eq!(agy_transcript(&base, odd), None, "{odd:?}");
    }
}
