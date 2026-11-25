//! Issue 801: a turn counts every call it made, as those who watch it are shown them. Codex
//! tells a file change only when it is done, with no beginning: it is counted all the same, in
//! the turn's end and in the live turn.

use super::*;

/// A stand-in of `codex exec`: a command, a file change that comes whole, the answer.
const CODEX: &str = r#"#!/usr/bin/env python3
import json
import sys

if sys.argv[1:2] != ["exec"]:
    sys.exit(0)
sys.stdin.read()
def say(value):
    print(json.dumps(value), flush=True)
say({"type": "thread.started", "thread_id": "00000000-0000-7000-8000-000000000801"})
say({"type": "turn.started"})
say({"type": "item.started", "item": {"id": "item_0", "type": "command_execution",
    "command": "make test", "aggregated_output": "", "exit_code": None, "status": "in_progress"}})
say({"type": "item.completed", "item": {"id": "item_0", "type": "command_execution",
    "command": "make test", "aggregated_output": "ok\n", "exit_code": 0, "status": "completed"}})
say({"type": "item.completed", "item": {"id": "item_1", "type": "file_change",
    "changes": [{"path": "src/b.rs", "kind": "update"}], "status": "completed"}})
say({"type": "item.completed", "item": {"id": "item_2", "type": "agent_message",
    "text": "Fixed b.rs."}})
say({"type": "turn.completed", "usage": {"input_tokens": 1, "cached_input_tokens": 0,
                                         "output_tokens": 1, "reasoning_output_tokens": 0}})
"#;

#[test]
fn a_call_that_comes_done_is_counted_in_the_turns_end_and_its_live_turn() {
    let place = Place::new("codex-counts");
    let codex = place.root.join("codex");
    program(&codex, CODEX);
    let host = place.start_in(
        "",
        &[],
        &[("CLARP_CODEX_PROGRAM", codex.to_str().unwrap())],
    );
    let made = host.post("/agents", json!({ "name": "Cody", "backend": "codex" }));
    let session = made["session"].as_str().unwrap().to_owned();
    host.send(&session, "Fix the test.", "codex-counts");
    wait_for("the turn's end", || {
        host.state(&session) == "done"
            && host
                .turns(&session)
                .iter()
                .any(|row| row.get("turn").is_some_and(|turn| turn.is_object()))
    });
    let rows = host.turns(&session);
    let end = rows
        .iter()
        .find_map(|row| row.get("turn").filter(|turn| turn.is_object()))
        .cloned()
        .unwrap();
    assert_eq!(end["tool_count"], 2, "{end}");
    let live = host.get(&format!("/live?session={session}"));
    assert_eq!(live["turn"]["tool_count"], 2, "{live}");
}
