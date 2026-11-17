//! The fork of an agent, held to what the Python Host's own module made of the same requests
//! (`tests/fixtures/fork-vectors.json`, written by `server-rs/tools/fork-vectors.py`).

#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_kernel::fork::{self, Child};
use serde_json::Value;

fn vectors() -> Value {
    serde_json::from_str(include_str!("fixtures/fork-vectors.json")).unwrap()
}

fn word(row: &Value, key: &str) -> String {
    row[key].as_str().unwrap_or_default().to_string()
}

fn child_of(row: &Value) -> Child {
    Child {
        name: word(row, "name"),
        task: word(row, "task"),
        index: row["index"].as_u64().unwrap() as usize,
        total: row["total"].as_u64().unwrap() as usize,
        cwd: word(row, "cwd"),
        repo: word(row, "repo"),
        branch: word(row, "branch"),
        base: word(row, "base"),
        worktree_path: word(row, "worktree_path"),
        backend: word(row, "backend"),
        model: word(row, "model"),
        effort: word(row, "effort"),
    }
}

#[test]
fn the_limits_are_the_python_hosts() {
    let vectors = vectors();
    assert_eq!(vectors["most"], fork::MOST);
    assert_eq!(vectors["seed_most"], fork::SEED_MOST);
    assert_eq!(vectors["turn_most"], fork::SAID_MOST);
}

#[test]
fn a_request_asks_for_the_same_children_or_is_refused_in_the_same_words() {
    let vectors = vectors();
    let rows = vectors["parsed"].as_array().unwrap();
    let (mut taken, mut refused) = (0, 0);
    for row in rows {
        let request = &row["request"];
        match (fork::asked(request), row.get("refused")) {
            (Ok(children), None) => {
                let theirs: Vec<Child> = row["children"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(child_of)
                    .collect();
                assert_eq!(children, theirs, "{request}");
                taken += 1;
            }
            (Err(why), Some(theirs)) => {
                let told = (
                    u64::from(why.status),
                    why.code.as_ref(),
                    why.message.as_str(),
                );
                let theirs = (
                    theirs[0].as_u64().unwrap(),
                    theirs[1].as_str().unwrap(),
                    theirs[2].as_str().unwrap(),
                );
                assert_eq!(told, theirs, "{request}");
                refused += 1;
            }
            (ours, theirs) => panic!("{request}: here {ours:?}, there {theirs:?}"),
        }
    }
    assert_eq!((taken, refused), (29, 61), "of {} requests", rows.len());
}

#[test]
fn a_childs_first_message_is_the_same_message() {
    let vectors = vectors();
    let rows = vectors["messages"].as_array().unwrap();
    for row in rows {
        let child = child_of(&row["child"]);
        let seed = row["seed"].as_str();
        let message = fork::message(
            &word(row, "parent"),
            &word(row, "helper"),
            &child,
            &word(row, "cwd"),
            seed,
        );
        assert_eq!(message, word(row, "message"), "{row}");
    }
    assert_eq!(rows.len(), 24);
}

#[test]
fn a_worktree_is_put_in_the_same_place() {
    let vectors = vectors();
    let rows = vectors["places"].as_array().unwrap();
    for row in rows {
        let place = fork::worktree_place(&word(row, "repo"), &word(row, "branch"));
        assert_eq!(place, word(row, "path"), "{row}");
    }
    assert_eq!(rows.len(), 15);
}
