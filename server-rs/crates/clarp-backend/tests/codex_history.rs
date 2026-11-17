//! What a rollout of Codex says was said and done in its session, held to what the Python
//! Host's own function reads from the same lines
//! (`tests/fixtures/codex-history-vectors.json`, written by
//! `server-rs/tools/codex-history-vectors.py`).

#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_backend::history;
use clarp_kernel::tools::cells;
use serde_json::{Value, json};

fn vectors() -> Value {
    serde_json::from_str(include_str!("fixtures/codex-history-vectors.json")).unwrap()
}

#[test]
fn a_rollout_is_read_as_the_python_host_reads_it() {
    let vectors = vectors();
    let rollouts = vectors["rollouts"].as_array().unwrap();
    let (mut lines_read, mut turns, mut shown, mut tools) = (0, 0, 0, 0);
    for rollout in rollouts {
        let what = format!("{}: {}", rollout["name"], rollout["what"]);
        let lines: Vec<&str> = rollout["lines"]
            .as_array()
            .unwrap()
            .iter()
            .map(|line| line.as_str().unwrap())
            .collect();
        let read = history::codex(&(lines.join("\n") + "\n"));
        let theirs = rollout["turns"].as_array().unwrap();
        for (number, (ours, theirs)) in read.iter().zip(theirs).enumerate() {
            let ours_shown = ours["display_cells"].as_array().unwrap();
            let theirs_shown = theirs["display_cells"].as_array().unwrap();
            for (at, (ours, theirs)) in ours_shown.iter().zip(theirs_shown).enumerate() {
                assert_eq!(ours, theirs, "{what}, turn {number}, cell {at}");
            }
            assert_eq!(ours, theirs, "{what}, turn {number}");
            // In the same order too: a cell is read by an app as it is written.
            assert_eq!(
                ours.to_string(),
                theirs.to_string(),
                "{what}, turn {number}, as text"
            );
            shown += ours_shown.len();
            tools += ours["tools"].as_array().unwrap().len();
        }
        assert_eq!(read.len(), theirs.len(), "{what}");
        lines_read += lines.len();
        turns += read.len();
    }
    assert_eq!(
        (rollouts.len(), lines_read, turns, shown, tools),
        (17, 375, 49, 232, 75)
    );
}

#[test]
fn a_cell_with_no_id_of_its_own_is_named_as_the_python_host_names_it() {
    let vectors = vectors();
    let ids = vectors["ids"].as_array().unwrap();
    for row in ids {
        let of = row["of"].as_str().unwrap();
        assert_eq!(
            cells::safe_id("cell", of),
            row["id"].as_str().unwrap(),
            "{of:?}"
        );
    }
    assert_eq!(ids.len(), 14);
}

#[test]
fn the_cell_of_one_item_is_what_a_conversation_shows_of_it_alone() {
    let ran = json!({
        "id": "item_0", "type": "command_execution", "command": "/usr/bin/bash -lc 'cargo test'",
        "aggregated_output": "ok\n", "exit_code": 0, "status": "completed",
    });
    let cell = history::codex_cell("item.completed", &ran).unwrap();
    assert_eq!(
        cell,
        json!({
            "id": "item_0", "kind": "command", "title": "Ran", "summary": "cargo test", "status": "ok",
            "lines": [{ "label": "", "text": "ok", "kind": "output" }],
        })
    );
    let running = history::codex_cell("item.started", &ran).unwrap();
    assert_eq!(
        (&running["title"], &running["status"]),
        (&json!("Running"), &json!("running"))
    );

    // A command that only looks at things is shown as that.
    let looked = json!({ "id": "item_1", "type": "command_execution", "command": "cat src/a.rs", "exit_code": 0 });
    let cell = history::codex_cell("item.completed", &looked).unwrap();
    assert_eq!(
        cell,
        json!({
            "id": "exploration-src-a-rs", "kind": "exploration", "title": "Explored", "summary": "", "status": "ok",
            "lines": [{ "label": "Read", "text": "src/a.rs", "kind": "detail" }],
        })
    );

    // What the agent says is no cell, and neither is a thing nobody knows.
    let said = json!({ "id": "item_2", "type": "agent_message", "text": "Done." });
    assert_eq!(history::codex_cell("item.completed", &said), None);
    let unknown = json!({ "id": "item_3", "type": "todo_list", "items": [] });
    assert_eq!(history::codex_cell("item.completed", &unknown), None);
    assert_eq!(
        history::codex_cell("item.completed", &json!("no item")),
        None
    );
}
