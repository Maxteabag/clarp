//! Issue 745: the conversation view only grows. A plan a Codex turn changes is work, and the
//! words before it were said along the way; the plan `codex exec` closes, unchanged, after the
//! turn's last words is no work, and leaves those words the turn's final answer.

use super::*;

/// A stand-in of `codex exec`: a preamble, a plan, another preamble, a command, the plan ticked,
/// the answer, and the plan closed as it stood when the task is complete.
const CODEX: &str = r#"#!/usr/bin/env python3
import json
import sys

if sys.argv[1:2] != ["exec"]:
    sys.exit(0)
sys.stdin.read()
def say(value):
    print(json.dumps(value), flush=True)
def message(n, text):
    say({"type": "item.completed", "item": {"id": f"item_{n}", "type": "agent_message", "text": text}})
def plan(step, done):
    say({"type": step, "item": {"id": "item_1", "type": "todo_list",
        "items": [{"text": "run the tests", "completed": done}]}})
say({"type": "thread.started", "thread_id": "00000000-0000-7000-8000-000000000745"})
say({"type": "turn.started"})
message(0, "I'll plan this.")
plan("item.started", False)
message(2, "Running the tests.")
say({"type": "item.started", "item": {"id": "item_3", "type": "command_execution",
    "command": "make test", "aggregated_output": "", "exit_code": None, "status": "in_progress"}})
say({"type": "item.completed", "item": {"id": "item_3", "type": "command_execution",
    "command": "make test", "aggregated_output": "ok\n", "exit_code": 0, "status": "completed"}})
plan("item.updated", True)
message(4, "All tests pass.")
plan("item.completed", True)
say({"type": "turn.completed", "usage": {"input_tokens": 1, "cached_input_tokens": 0,
                                         "output_tokens": 1, "reasoning_output_tokens": 0}})
"#;

#[test]
fn a_plan_closed_unchanged_after_the_answer_leaves_it_the_final_answer() {
    let place = Place::new("plan-closed");
    let codex = place.root.join("codex");
    program(&codex, CODEX);
    let host = place.start_in("", &[], &[("CLARP_CODEX_PROGRAM", codex.to_str().unwrap())]);
    let made = host.post("/agents", json!({ "name": "Cody", "backend": "codex" }));
    let session = made["session"].as_str().unwrap().to_owned();
    host.send(&session, "Run the tests.", "plan-closed");
    wait_for("the turn to end", || {
        host.state(&session) == "done"
            && host
                .turns(&session)
                .iter()
                .any(|row| row["text"] == "All tests pass." && row.get("turn").is_some())
    });
    let rows = host.turns(&session);
    let of = |text: &str| -> Value {
        rows.iter()
            .find(|row| row["text"] == text)
            .cloned()
            .unwrap_or_else(|| panic!("no row says {text}: {rows:?}"))
    };
    // The answer: said at the end, and in the conversation view.
    let answer = of("All tests pass.");
    assert_eq!(answer["phase"], "final", "{answer}");
    assert_eq!(answer["view"], "conversation", "{answer}");
    // The words a plan and a command came after were said along the way, and never entered.
    for text in ["I'll plan this.", "Running the tests."] {
        let row = of(text);
        assert_eq!(row["phase"], "commentary", "{row}");
        assert_eq!(row["view"], "session", "{row}");
    }
    let conversation = host.get(&format!("/log?session={session}&view=conversation"))["turns"]
        .as_array()
        .unwrap()
        .iter()
        .map(|row| row["text"].as_str().unwrap_or_default().to_owned())
        .collect::<Vec<_>>();
    assert_eq!(conversation, ["Run the tests.", "All tests pass."]);
}
