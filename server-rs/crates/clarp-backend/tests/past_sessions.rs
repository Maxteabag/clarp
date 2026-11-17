//! The sessions Claude Code kept, as they are listed for whoever makes an agent out of one,
//! held to what the Python Host's own function lists for the same files
//! (`tests/fixtures/past-sessions-vectors.json`, written by
//! `server-rs/tools/past-sessions-vectors.py`).
//!
//! Two things this list does otherwise, on purpose. Each is named here with what the Python
//! Host did, so that the day it does otherwise is noticed:
//!
//! - `/srv/my_project/v2.0`: Claude Code names the project of that directory with a dash
//!   for every sign that is no letter and no digit. The Python Host looks under a name with
//!   a dash for every slash only, finds no such project, and offers nothing.
//! - `a-side`: a session its index marks as a side chain. The Python Host leaves it out
//!   where it reads the index, and then offers it as a transcript the index does not name.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::{Duration, SystemTime};

use clarp_backend::past::{claude_project, claude_sessions};
use serde_json::{Value, json};

static PLACES: AtomicUsize = AtomicUsize::new(0);

fn vectors() -> Value {
    serde_json::from_str(include_str!("fixtures/past-sessions-vectors.json")).unwrap()
}

/// The made-up directory of projects, written out as the Python Host was given it.
fn projects(vectors: &Value) -> PathBuf {
    let n = PLACES.fetch_add(1, Ordering::SeqCst);
    let name = format!("clarp-past-sessions-{}-{n}", std::process::id());
    let root = std::env::temp_dir().join(name);
    let _ = std::fs::remove_dir_all(&root);
    for file in vectors["files"].as_array().unwrap() {
        let path = root.join(file["path"].as_str().unwrap());
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        let text = file["text"].as_str().unwrap();
        std::fs::write(&path, text.replace("{ROOT}", root.to_str().unwrap())).unwrap();
        let written = SystemTime::UNIX_EPOCH + Duration::from_secs(file["mtime"].as_u64().unwrap());
        let file = std::fs::File::options().write(true).open(&path).unwrap();
        file.set_modified(written).unwrap();
    }
    root
}

fn ids(sessions: &[Value]) -> Vec<&str> {
    let ids = sessions
        .iter()
        .map(|session| session["id"].as_str().unwrap());
    ids.collect()
}

#[test]
fn the_sessions_claude_code_kept_are_listed_as_the_python_host_lists_them() {
    let vectors = vectors();
    let root = projects(&vectors);
    let asked = vectors["asked"].as_array().unwrap();
    // Every list as the Python Host gave it, at its longest: what a shorter one is cut from.
    let whole = |cwd: &Value, everywhere: &Value| -> Vec<Value> {
        let same = |row: &&Value| {
            row["cwd"] == *cwd && row["all_projects"] == *everywhere && row["limit"] == 100
        };
        let row = asked.iter().find(same);
        row.map(|row| row["sessions"].as_array().unwrap().clone())
            .unwrap_or_default()
    };
    let mut side_chains = 0;
    for row in asked {
        let (cwd, everywhere) = (row["cwd"].as_str().unwrap(), row["all_projects"] == true);
        let limit = usize::try_from(row["limit"].as_u64().unwrap()).unwrap();
        let what = format!("{cwd:?}, everywhere: {everywhere}, at most {limit}");
        let theirs = row["sessions"].as_array().unwrap();
        // A side chain is offered by the Python Host, and not here.
        let mut expected: Vec<Value> = match limit {
            100 => theirs.clone(),
            _ => whole(&json!(cwd), &json!(everywhere)),
        };
        let before = expected.len();
        expected.retain(|session| session["id"] != "a-side");
        side_chains += before - expected.len();
        expected.truncate(limit);
        if limit < 100 && !ids(theirs).contains(&"a-side") {
            assert_eq!(
                &expected, theirs,
                "{what}: a shorter list is the longer one, cut"
            );
        }
        if cwd == "/srv/my_project/v2.0" {
            // Nothing, in the Python Host: it looks for the project under another name.
            assert_eq!(theirs, &Vec::<Value>::new(), "{what}");
            let everything = whole(&json!(""), &json!(true));
            let own = everything.iter().filter(|session| session["id"] == "m-one");
            expected = own.cloned().collect();
            assert_eq!(expected.len(), 1);
        }
        let ours = claude_sessions(&root, cwd, everywhere, limit);
        assert_eq!(ids(&ours), ids(&expected), "{what}");
        assert_eq!(ours, expected, "{what}");
        assert_eq!(
            json!(ours).to_string(),
            json!(expected).to_string(),
            "{what}, as text"
        );
    }
    assert!(
        side_chains >= 5,
        "the Python Host offers the side chain: {side_chains}"
    );
    assert_eq!(asked.len(), 14);
    // A machine on which Claude Code never ran has no sessions.
    assert_eq!(vectors["without_projects"], json!([]));
    assert!(claude_sessions(&root.join("no-such-directory"), "/srv/alpha", false, 100).is_empty());
    let _ = std::fs::remove_dir_all(&root);
}

#[test]
fn a_project_is_named_as_claude_code_names_it() {
    let named = |cwd: &str| claude_project(cwd);
    assert_eq!(named("/srv/alpha"), "-srv-alpha");
    assert_eq!(named("/srv/alpha/"), "-srv-alpha");
    assert_eq!(named("/srv//alpha"), "-srv-alpha");
    assert_eq!(named("/srv/my_project/v2.0"), "-srv-my-project-v2-0");
    assert_eq!(named("/home/x/.gemini"), "-home-x--gemini");
    assert_eq!(named("/srv/a b/çé"), "-srv-a-b---");
    assert_eq!(named(""), "-");
    assert_eq!(named("/"), "-");
    assert!(Path::new(&named("relative/dir")).is_relative());
    assert_eq!(named("relative/dir"), "relative-dir");
}
