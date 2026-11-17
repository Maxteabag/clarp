//! The Antigravity adapter against what Antigravity really printed.
//!
//! The fixtures are the Python Host's own recordings of agy 1.1.21 and 1.2.16
//! (`tests/fixtures/agy` in the repository, copied as they are): a turn that only answers, one
//! that runs a command and answers, and one that searches the web, reads a page, runs a
//! command and writes its answer in pieces. When a newer Antigravity changes its stream,
//! record it, add it beside these, and keep the old ones.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_backend::agy::AgyInterpreter;
use clarp_backend::{Interpreter, Signal};
use clarp_kernel::conversation::{Reply, ToolCall, ToolStatus};
use clarp_kernel::turn::{Activity, RunOutcome};
use clarp_spool::{Entry, Record, ShimEvent};
use serde_json::{Value, json};

/// Reads lines the way the Host reads a spool: one by one, then the exit.
fn read_lines(lines: &[&str], errors: &[&str], exit: (Option<i32>, Option<i32>)) -> Vec<Signal> {
    let mut interpreter = AgyInterpreter::new(1, "m1");
    let mut out = Vec::new();
    let mut t = 0;
    for line in lines {
        t += 1;
        let entry = Entry::Out((*line).to_string());
        interpreter.on_record(&Record { t, entry }, &mut out);
    }
    for line in errors {
        t += 1;
        let entry = Entry::Err((*line).to_string());
        interpreter.on_record(&Record { t, entry }, &mut out);
    }
    let gone = Entry::Shim(ShimEvent::Exited {
        code: exit.0,
        signal: exit.1,
    });
    interpreter.on_record(
        &Record {
            t: t + 1,
            entry: gone,
        },
        &mut out,
    );
    out
}

fn recorded(fixture: &str) -> String {
    let path = format!("{}/tests/fixtures/{fixture}", env!("CARGO_MANIFEST_DIR"));
    std::fs::read_to_string(&path).unwrap_or_else(|e| panic!("{path}: {e}"))
}

/// A recorded turn that ended well, without what is only shown while it runs.
fn read(fixture: &str) -> Vec<Signal> {
    let recorded = recorded(fixture);
    let lines: Vec<&str> = recorded.lines().collect();
    let mut out = read_lines(&lines, &[], (Some(0), None));
    out.retain(|signal| !matches!(signal, Signal::Glimpse(_)));
    out
}

fn replies(signals: &[Signal]) -> Vec<&Reply> {
    let replies = signals.iter().filter_map(|signal| match signal {
        Signal::Reply(reply) => Some(reply),
        _ => None,
    });
    replies.collect()
}

fn calls(signals: &[Signal]) -> Vec<&ToolCall> {
    let calls = signals.iter().filter_map(|signal| match signal {
        Signal::Tool(call) => Some(call),
        _ => None,
    });
    calls.collect()
}

#[test]
fn a_real_turn_that_only_answers() {
    let signals = read("agy-1.1.21-success.jsonl");
    assert_eq!(
        signals[..3],
        [
            Signal::Bound {
                backend_session: "11111111-2222-3333-4444-555555555555".to_string()
            },
            Signal::Model("gemini-3.7-flash-low".to_string()),
            Signal::Moved(Activity::Thinking),
        ],
        "Antigravity names its conversation and its model in its first line"
    );
    let said = replies(&signals);
    assert_eq!(said.len(), 1, "the answer once: the result repeats it");
    assert_eq!(
        (said[0].id.as_str(), said[0].text.as_str(), said[0].turn),
        ("r-m1-1", "FIXTURE_OK\n", 1)
    );
    assert_eq!(
        signals[signals.len() - 2..],
        [
            Signal::Usage {
                tokens_in: 18_821,
                tokens_out: 5
            },
            Signal::Ended(RunOutcome::Completed),
        ]
    );
    assert_eq!(signals.len(), 6);
}

#[test]
fn a_real_turn_that_runs_a_command_and_answers() {
    let signals = read("agy-1.1.21-tool.jsonl");
    let calls = calls(&signals);
    assert_eq!(calls.len(), 2, "the call when it starts and when it ends");
    assert_eq!(
        (
            calls[0].id.as_str(),
            calls[0].name.as_str(),
            calls[0].status,
            &calls[0].input
        ),
        (
            "c-m1-step-2",
            "Bash",
            ToolStatus::Running,
            &json!({ "command": "printf AGY_TOOL_OK", "description": "" })
        ),
        "its tool under the name the conversation knows it by, with what it was given"
    );
    assert_eq!(
        (
            calls[1].id.as_str(),
            calls[1].status,
            calls[1].output.as_deref()
        ),
        ("c-m1-step-2", ToolStatus::Ok, Some("AGY_TOOL_OK"))
    );
    let moves: Vec<&Activity> = signals
        .iter()
        .filter_map(|signal| match signal {
            Signal::Moved(activity) => Some(activity),
            _ => None,
        })
        .collect();
    let tool = Activity::Tool {
        name: "Bash".to_string(),
    };
    assert_eq!(moves, [&Activity::Thinking, &tool, &Activity::Thinking]);
    // A step in which the agent said nothing is no message: one message, after the tool.
    let said = replies(&signals);
    assert_eq!(said.len(), 1);
    assert_eq!(
        (said[0].id.as_str(), said[0].text.as_str()),
        ("r-m1-3", "TOOL_DONE\n")
    );
    // What the turn took is the result's own count, which is the sum of its steps.
    assert!(signals.contains(&Signal::Usage {
        tokens_in: 21_549,
        tokens_out: 84
    }));
    assert_eq!(signals.last(), Some(&Signal::Ended(RunOutcome::Completed)));
}

#[test]
fn a_real_turn_that_searches_reads_and_writes_its_answer_in_pieces() {
    let recorded_turn = recorded("agy-1.2.16-web.jsonl");
    let lines: Vec<&str> = recorded_turn.lines().collect();
    let all = read_lines(&lines, &[], (Some(0), None));
    let result: Value = serde_json::from_str(lines.last().unwrap()).unwrap();
    let response = result["result"]["response"].as_str().unwrap();

    let calls = calls(&all);
    let ended: Vec<(&str, &Value)> = calls
        .iter()
        .filter(|call| call.status == ToolStatus::Ok)
        .map(|call| (call.name.as_str(), &call.input))
        .collect();
    assert_eq!(
        calls.len(),
        6,
        "three calls, each when it starts and when it ends"
    );
    assert_eq!(ended.len(), 3);
    assert_eq!(
        ended[0],
        (
            "WebSearch",
            &json!({ "query": "httpx latest release version PyPI", "url": "" })
        )
    );
    assert_eq!(
        ended[1],
        (
            "WebFetch",
            &json!({ "query": "", "url": "https://pypi.org/pypi/httpx/json" })
        )
    );
    assert_eq!(ended[2].0, "Bash");
    assert_eq!(calls[5].output.as_deref(), Some("\"version\":\"0.28.1\""));
    // The answer grows while it is written, and is one message when its step is done: the
    // one the result names.
    let growing: Vec<&str> = all
        .iter()
        .filter_map(|signal| match signal {
            Signal::Live(live) => Some(live.id.as_str()),
            _ => None,
        })
        .collect();
    assert_eq!(growing, ["r-m1-7"]);
    let said = replies(&all);
    assert_eq!(said.len(), 1);
    assert_eq!(said[0].id, "r-m1-7");
    assert_eq!(said[0].text.trim(), response.trim());
    assert!(all.contains(&Signal::Model("claude-opus-5-5-high".to_string())));
    assert!(all.contains(&Signal::Usage {
        tokens_in: 31_764,
        tokens_out: 688
    }));
    assert_eq!(all.last(), Some(&Signal::Ended(RunOutcome::Completed)));
}

/// A turn of agy 1.5.10, recorded on 17 Nov 2026 as the Host starts one (stream-json, one
/// harmless command; the conversation's id and the directory replaced). The stream is the
/// one the older recordings have; what a command gave back now comes with when it ran and
/// how it ended written around it.
#[test]
fn a_real_turn_of_a_newer_antigravity() {
    let recorded_turn = recorded("agy-1.5.10-tool.jsonl");
    let lines: Vec<&str> = recorded_turn.lines().collect();
    let all = read_lines(&lines, &[], (Some(0), None));
    assert_eq!(
        all[..2],
        [
            Signal::Bound {
                backend_session: "77777777-8888-9999-aaaa-bbbbbbbbbbbb".to_string()
            },
            Signal::Model("gemini-3.8-flash-low".to_string()),
        ]
    );
    let calls = calls(&all);
    assert_eq!(calls.len(), 2);
    assert_eq!(
        (
            calls[1].name.as_str(),
            &calls[1].input,
            calls[1].status,
            calls[1].output.as_deref()
        ),
        (
            "Bash",
            &json!({ "command": "printf AGY_OK", "description": "" }),
            ToolStatus::Ok,
            Some("AGY_OK")
        ),
        "what the command printed, without what is written around it"
    );
    let ended_with: Vec<Option<i64>> = all
        .iter()
        .filter_map(|signal| match signal {
            Signal::Glimpse(clarp_kernel::live::Glimpse::Returned { exit_code, .. }) => {
                Some(*exit_code)
            }
            _ => None,
        })
        .collect();
    assert_eq!(ended_with, [Some(0)]);
    let said = replies(&all);
    assert_eq!(said.len(), 1);
    assert_eq!(
        (said[0].id.as_str(), said[0].text.as_str()),
        ("r-m1-3", "AGY_OK\n")
    );
    assert!(all.contains(&Signal::Usage {
        tokens_in: 13_712,
        tokens_out: 46
    }));
    assert_eq!(all.last(), Some(&Signal::Ended(RunOutcome::Completed)));
}

const INIT: &str = r#"{"event":"init","conversation_id":"c-9","init":{"model":"m"}}"#;

#[test]
fn a_real_turn_that_continues_a_conversation_took_what_its_steps_took() {
    // Recorded on 17 Nov 2026 with agy 1.5.10: the second turn of a conversation, started
    // with its name. Its one step took 4,969 tokens in and 1 out. Its result says 13,774 and
    // 3: what the whole conversation has taken so far (the turn before took 8,805 and 2),
    // which is no turn's.
    let signals = read("agy-1.5.10-continued.jsonl");
    let took: Vec<&Signal> = signals
        .iter()
        .filter(|signal| matches!(signal, Signal::Usage { .. }))
        .collect();
    assert_eq!(
        took,
        [&Signal::Usage {
            tokens_in: 4_969,
            tokens_out: 1
        }]
    );
    let said = replies(&signals);
    assert_eq!(said.last().map(|reply| reply.text.trim()), Some("falcon"));
    assert_eq!(ended(&signals), &RunOutcome::Completed);
}

fn ended(signals: &[Signal]) -> &RunOutcome {
    match signals.last() {
        Some(Signal::Ended(outcome)) => outcome,
        other => panic!("the last thing told is how the turn ended, not {other:?}"),
    }
}

#[test]
fn a_turn_that_ends_badly_says_why() {
    // Refused before a conversation was opened: the tool's own word is the reason.
    let refused = r#"{"event":"result","result":{"conversation_id":"","status":"ERROR","error":" sign-in rejected "}}"#;
    let signals = read_lines(&[refused], &[], (Some(1), None));
    assert!(
        !signals
            .iter()
            .any(|signal| matches!(signal, Signal::Bound { .. }))
    );
    assert_eq!(
        ended(&signals),
        &RunOutcome::Failed {
            reason: "status=ERROR: sign-in rejected".to_string()
        }
    );
    // A result that is no success, with nothing said of why.
    let odd = r#"{"event":"result","result":{"conversation_id":"c-9","status":"CANCELLED","response":""}}"#;
    assert_eq!(
        ended(&read_lines(&[INIT, odd], &[], (Some(0), None))),
        &RunOutcome::Failed {
            reason: "status=CANCELLED: provider turn failed".to_string()
        }
    );
    // Ended by a signal, also after it said it was done.
    let fine = r#"{"event":"result","result":{"conversation_id":"c-9","status":"SUCCESS","response":"ok"}}"#;
    assert_eq!(
        ended(&read_lines(&[INIT, fine], &[], (None, Some(15)))),
        &RunOutcome::Interrupted {
            reason: "agy was ended by signal 15".to_string()
        }
    );
    // Gone without a result: what it wrote to its errors is the reason, or that it said
    // nothing.
    let silent = read_lines(&[INIT], &[], (Some(0), None));
    assert_eq!(
        ended(&silent),
        &RunOutcome::Failed {
            reason: "agy ended without saying how its turn ended".to_string()
        }
    );
    let loud = read_lines(&[INIT], &["timeout waiting for response"], (Some(1), None));
    match ended(&loud) {
        RunOutcome::Failed { reason } => assert!(reason.contains("timeout waiting"), "{reason}"),
        other => panic!("{other:?}"),
    }
}

#[test]
fn what_the_stream_may_also_say() {
    let step = |said: Value| json!({ "event": "step_update", "step_update": said }).to_string();
    // A tool that failed says so, and what it said is what came back.
    let begun = step(json!({
        "step_index": 2, "state": "ACTIVE", "step_type": "tool", "tool_name": "view_file",
        "tool_info": { "id": "call-7", "name": "view_file", "parameters": { "AbsolutePath": "/x" } },
    }));
    let failed = step(json!({
        "step_index": 2, "state": "DONE", "step_type": "tool", "tool_name": "view_file",
        "tool_info": { "id": "call-7", "name": "view_file", "error": "no such file" },
    }));
    // The same end told twice, a line that is no event, and a result whose answer no step said.
    let fine = r#"{"event":"result","result":{"conversation_id":"c-9","status":"SUCCESS","response":"Not there.","usage":{}}}"#;
    let lines = [
        INIT,
        begun.as_str(),
        "warning: something a tool printed",
        failed.as_str(),
        failed.as_str(),
        fine,
        fine,
    ];
    let signals = read_lines(&lines, &[], (Some(0), None));
    let calls = calls(&signals);
    assert_eq!(calls.len(), 2);
    assert_eq!(
        (
            calls[1].id.as_str(),
            calls[1].name.as_str(),
            calls[1].status,
            calls[1].output.as_deref(),
            &calls[0].input
        ),
        (
            "c-m1-call-7",
            "Read",
            ToolStatus::Error,
            Some("no such file"),
            &json!({ "file_path": "/x" })
        ),
        "a call is known by its own id where it has one"
    );
    let said = replies(&signals);
    assert_eq!(said.len(), 1, "the result's answer, once");
    assert_eq!(
        (said[0].id.as_str(), said[0].text.as_str()),
        ("r-m1-result", "Not there.")
    );
    // No count at all is no usage.
    assert!(
        !signals
            .iter()
            .any(|signal| matches!(signal, Signal::Usage { .. }))
    );
    assert_eq!(ended(&signals), &RunOutcome::Completed);

    // What the steps took is added up where the result does not say: what the model
    // thought counts as what it wrote.
    let thought = step(json!({
        "step_index": 1, "state": "DONE", "step_type": "agent_response", "text_delta": "Hm.",
        "usage": { "input_tokens": 10, "output_tokens": 2, "thinking_tokens": 5 },
    }));
    let more = step(json!({
        "step_index": 2, "state": "DONE", "step_type": "agent_response", "text_delta": "Done.",
        "usage": { "input_tokens": 7, "output_tokens": 1 },
    }));
    let bare = r#"{"event":"result","result":{"conversation_id":"c-9","status":"SUCCESS","response":"Done."}}"#;
    let signals = read_lines(
        &[INIT, thought.as_str(), more.as_str(), bare],
        &[],
        (Some(0), None),
    );
    assert!(signals.contains(&Signal::Usage {
        tokens_in: 17,
        tokens_out: 8
    }));
    let said = replies(&signals);
    let texts: Vec<&str> = said.iter().map(|reply| reply.text.as_str()).collect();
    assert_eq!(
        texts,
        ["Hm.", "Done."],
        "each step that says something is a message"
    );
}
