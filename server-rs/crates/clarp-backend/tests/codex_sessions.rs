//! The sessions Codex kept, as they are listed for whoever makes an agent out of one, held to
//! what the Python Host's own function lists for the same rollouts and the same index of
//! threads (`tests/fixtures/codex-sessions-vectors.json`, written by
//! `server-rs/tools/codex-sessions-vectors.py`).

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::path::PathBuf;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::{Duration, SystemTime};

use clarp_backend::history;
use clarp_backend::past::{codex_rollout, codex_sessions};
use rusqlite::types::Value as Held;
use serde_json::{Value, json};

static PLACES: AtomicUsize = AtomicUsize::new(0);

fn vectors() -> Value {
    serde_json::from_str(include_str!("fixtures/codex-sessions-vectors.json")).unwrap()
}

/// The made-up rollouts and the made-up index of threads, written out as the Python Host
/// was given them: where the rollouts are, and where the index is.
fn place(vectors: &Value) -> (PathBuf, PathBuf) {
    let n = PLACES.fetch_add(1, Ordering::SeqCst);
    let name = format!("clarp-codex-sessions-{}-{n}", std::process::id());
    let root = std::env::temp_dir().join(name);
    let _ = std::fs::remove_dir_all(&root);
    let sessions = root.join("sessions");
    for file in vectors["files"].as_array().unwrap() {
        let path = sessions.join(file["path"].as_str().unwrap());
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(&path, file["text"].as_str().unwrap()).unwrap();
        let written = SystemTime::UNIX_EPOCH + Duration::from_secs(file["mtime"].as_u64().unwrap());
        let file = std::fs::File::options().write(true).open(&path).unwrap();
        file.set_modified(written).unwrap();
    }
    let index = root.join("state_5.sqlite");
    let db = rusqlite::Connection::open(&index).unwrap();
    let columns = [
        "id",
        "cwd",
        "name",
        "title",
        "preview",
        "first_user_message",
        "updated_at",
        "updated_at_ms",
        "rollout_path",
        "has_user_event",
        "recency_at_ms",
        "model",
    ];
    let table = "CREATE TABLE threads (id TEXT, cwd TEXT, name TEXT, title TEXT, preview TEXT, \
                 first_user_message TEXT, updated_at INTEGER, updated_at_ms INTEGER, rollout_path TEXT, \
                 has_user_event INTEGER, recency_at_ms INTEGER, model TEXT)";
    db.execute(table, []).unwrap();
    for thread in vectors["threads"].as_array().unwrap() {
        let held = columns.map(|column| match &thread[column] {
            Value::Null => Held::Null,
            Value::String(text) => Held::Text(text.replace("{ROOT}", sessions.to_str().unwrap())),
            Value::Number(number) => Held::Integer(number.as_i64().unwrap()),
            other => panic!("{column}: {other}"),
        });
        let put = "INSERT INTO threads VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12)";
        db.execute(put, rusqlite::params_from_iter(held)).unwrap();
    }
    (sessions, index)
}

#[test]
fn the_sessions_codex_kept_are_listed_as_the_python_host_lists_them() {
    let vectors = vectors();
    let (sessions, index) = place(&vectors);
    let no_index = sessions.join("no-such-index.sqlite");
    let asked = vectors["asked"].as_array().unwrap();
    let (mut listed, mut with_index) = (0, 0);
    for row in asked {
        let cwd = row["cwd"].as_str().unwrap();
        let everywhere = row["all_projects"].as_bool().unwrap();
        let limit = usize::try_from(row["limit"].as_u64().unwrap()).unwrap();
        let indexed = row["index"].as_bool().unwrap();
        let ours = codex_sessions(
            &sessions,
            Some(if indexed { &index } else { &no_index }),
            cwd,
            everywhere,
            limit,
        );
        let what = format!("{cwd:?}, everywhere: {everywhere}, at most {limit}, index: {indexed}");
        assert_eq!(json!(ours), row["sessions"], "{what}");
        assert_eq!(
            json!(ours).to_string(),
            row["sessions"].to_string(),
            "{what}, as text"
        );
        listed += ours.len();
        with_index += usize::from(indexed);
    }
    assert_eq!((asked.len(), with_index, listed), (16, 12, 95));

    // With no index at all the rollouts say what there is, and without rollouts nothing does.
    let none = codex_sessions(&sessions, None, "/srv/alpha", false, 100);
    let without = asked
        .iter()
        .find(|row| row["cwd"] == "/srv/alpha" && row["index"] == false);
    assert_eq!(json!(none), without.unwrap()["sessions"]);
    let nowhere = sessions.join("no-such-directory");
    assert_eq!(
        json!(codex_sessions(
            &nowhere,
            Some(&index),
            "/srv/alpha",
            false,
            100
        )),
        vectors["without_sessions"]
    );
    // An index that is no database names nothing, and hides nothing.
    let broken = sessions.join("../broken.sqlite");
    std::fs::write(&broken, "this is no database\n".repeat(40)).unwrap();
    let ours = codex_sessions(&sessions, Some(&broken), "/srv/alpha", false, 100);
    assert_eq!(json!(ours), vectors["with_a_broken_index"]);
    assert_eq!(ours.len(), 10);
}

#[test]
fn the_rollout_of_a_session_is_found_as_the_python_host_finds_it() {
    let vectors = vectors();
    let (sessions, _) = place(&vectors);
    let found = vectors["found"].as_array().unwrap();
    let mut there = 0;
    for row in found {
        let session = row["session"].as_str().unwrap();
        let ours = codex_rollout(&sessions, session);
        let ours = ours.map(|path| path.strip_prefix(&sessions).unwrap().display().to_string());
        assert_eq!(json!(ours), row["path"], "{session:?}");
        there += usize::from(ours.is_some());
    }
    assert_eq!((found.len(), there), (9, 6));
    assert_eq!(
        codex_rollout(&sessions.join("no-such-directory"), "a1"),
        None
    );
}

#[test]
fn the_model_a_session_of_codex_last_ran_on_is_read_as_the_python_host_reads_it() {
    let vectors = vectors();
    let files = vectors["files"].as_array().unwrap();
    let models = vectors["models"].as_array().unwrap();
    let mut named = 0;
    for row in models {
        let file = files
            .iter()
            .find(|file| file["path"] == row["path"])
            .unwrap();
        let ours = history::model(file["text"].as_str().unwrap());
        assert_eq!(json!(ours), row["model"], "{}", row["path"]);
        named += usize::from(!ours.is_empty());
    }
    assert_eq!((models.len(), named), (16, 1));
}
