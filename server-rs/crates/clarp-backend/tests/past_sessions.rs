//! The sessions Claude Code kept, as they are listed for whoever makes an agent out of one,
//! held to what the Python Host's own function lists for the same files
//! (`tests/fixtures/past-sessions-vectors.json`, written by
//! `server-rs/tools/past-sessions-vectors.py`).

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::{Duration, SystemTime};

use clarp_backend::past::{claude_project, claude_sessions, claude_transcript};
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

#[test]
fn the_sessions_claude_code_kept_are_listed_as_the_python_host_lists_them() {
    let vectors = vectors();
    let root = projects(&vectors);
    let asked = vectors["asked"].as_array().unwrap();
    for row in asked {
        let (cwd, everywhere) = (row["cwd"].as_str().unwrap(), row["all_projects"] == true);
        let limit = usize::try_from(row["limit"].as_u64().unwrap()).unwrap();
        let expected = row["sessions"].as_array().unwrap();
        let ours = claude_sessions(&root, cwd, everywhere, limit);
        assert_eq!(
            &ours, expected,
            "{cwd:?}, everywhere: {everywhere}, at most {limit}"
        );
    }
    assert_eq!(asked.len(), 15);
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
    assert_eq!(named("/srv/./alpha"), "-srv-alpha");
    assert_eq!(named("//srv/alpha"), "--srv-alpha");
    assert_eq!(named("///srv/alpha"), "-srv-alpha");
    assert_eq!(named("/srv/my_project/v2.0"), "-srv-my-project-v2-0");
    assert_eq!(named("/home/x/.gemini"), "-home-x--gemini");
    assert_eq!(named("/srv/a b/çé"), "-srv-a-b---");
    assert_eq!(named(""), "-");
    assert_eq!(named("/"), "-");
    assert!(Path::new(&named("relative/dir")).is_relative());
    assert_eq!(named("relative/dir"), "relative-dir");
}

#[test]
fn resume_looks_in_the_same_encoded_project_as_the_catalogue() {
    let root = projects(&vectors());
    let cwd = "/srv/./my_project//v2.0/";
    let transcript = claude_transcript(&root, cwd, "m-one", false).unwrap();
    assert_eq!(
        transcript,
        root.canonicalize()
            .unwrap()
            .join("-srv-my-project-v2-0/m-one.jsonl")
    );
    let _ = std::fs::remove_dir_all(&root);
}
