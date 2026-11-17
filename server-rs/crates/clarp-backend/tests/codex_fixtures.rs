//! The Codex adapter against what Codex really printed.
//!
//! The fixtures are four turns recorded from codex-cli 0.167.1 on 15 Nov 2026, started the way
//! the Host starts them (`codex exec --json`, the message on standard input): one that runs a
//! shell command and answers, the next turn of the same thread, one that fails because its
//! model does not exist, and one that was ended by a signal while its command ran. The threads'
//! ids were replaced and two remarks of Codex about the machine's own configuration were taken
//! out; the events, their order and their fields are as they came. When a newer Codex changes
//! its stream, record it again, add it beside these, and keep the old ones.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_backend::codex::CodexInterpreter;
use clarp_backend::{Interpreter, Signal};
use clarp_kernel::conversation::ToolStatus;
use clarp_kernel::turn::{Activity, RunOutcome};
use clarp_spool::{Entry, Record, ShimEvent};
use serde_json::json;

const THREAD: &str = "00000000-0000-7000-8000-000000000002";

/// Reads a recorded turn the way the Host reads a spool: line by line, then the exit.
fn read(fixture: &str, name: &str, exit: (Option<i32>, Option<i32>)) -> Vec<Signal> {
    let path = format!("{}/tests/fixtures/{fixture}", env!("CARGO_MANIFEST_DIR"));
    let recorded = std::fs::read_to_string(&path).unwrap_or_else(|e| panic!("{path}: {e}"));
    let mut interpreter = CodexInterpreter::new(1, name);
    let mut out = Vec::new();
    let mut t = 0;
    for line in recorded.lines() {
        t += 1;
        let entry = Entry::Out(line.to_string());
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
    // What a turn means for the conversation: what is only shown is looked at by itself.
    out.retain(|signal| !matches!(signal, Signal::Glimpse(_)));
    out
}

#[test]
fn a_real_turn_that_runs_a_command_and_answers() {
    let signals = read("codex-0.167.1-tool.jsonl", "m1", (Some(0), None));
    assert_eq!(
        signals[0],
        Signal::Bound {
            backend_session: THREAD.to_string()
        },
        "Codex names its thread in its first line"
    );
    let calls: Vec<_> = signals
        .iter()
        .filter_map(|signal| match signal {
            Signal::Tool(call) => Some(call),
            _ => None,
        })
        .collect();
    assert_eq!(calls.len(), 2, "the call when it starts and when it ends");
    assert_eq!(
        (
            calls[0].id.as_str(),
            calls[0].name.as_str(),
            calls[0].status
        ),
        ("c-m1-item_0", "shell", ToolStatus::Running)
    );
    assert_eq!(
        calls[0].input["command"],
        "/usr/bin/bash -lc 'echo hello-from-codex'"
    );
    assert_eq!(
        (calls[1].status, calls[1].output.as_deref()),
        (ToolStatus::Ok, Some("hello-from-codex\n"))
    );
    assert!(calls[1].ended_at_ms > Some(calls[1].started_at_ms));
    // What a conversation shows of the call: the cell Codex's own reader makes of it,
    // known by the call, without the shell that was only wrapped around the command.
    assert_eq!(
        calls[0].cell,
        Some(json!({
            "id": "c-m1-item_0", "kind": "command", "title": "Running", "summary": "echo hello-from-codex",
            "status": "running", "lines": [],
        }))
    );
    assert_eq!(
        calls[1].cell,
        Some(json!({
            "id": "c-m1-item_0", "kind": "command", "title": "Ran", "summary": "echo hello-from-codex",
            "status": "ok", "lines": [{ "label": "", "text": "hello-from-codex", "kind": "output" }],
        }))
    );
    let replies: Vec<_> = signals
        .iter()
        .filter_map(|signal| match signal {
            Signal::Reply(reply) => Some((reply.id.as_str(), reply.text.as_str())),
            _ => None,
        })
        .collect();
    assert_eq!(replies, vec![("r-m1-item_1", "done")]);
    let moves: Vec<_> = signals
        .iter()
        .filter_map(|signal| match signal {
            Signal::Moved(activity) => Some(activity.clone()),
            _ => None,
        })
        .collect();
    assert_eq!(
        moves,
        vec![
            Activity::Thinking,
            Activity::Tool {
                name: "shell".to_string()
            },
            Activity::Thinking
        ]
    );
    assert!(
        signals.contains(&Signal::Usage {
            tokens_in: 87_566,
            tokens_out: 36
        }),
        "what the turn took in and gave out"
    );
    assert_eq!(signals.last(), Some(&Signal::Ended(RunOutcome::Completed)));
}

#[test]
fn the_next_turn_of_a_thread_counts_its_items_from_the_start_again() {
    let first = read("codex-0.167.1-tool.jsonl", "m1", (Some(0), None));
    let second = read("codex-0.167.1-resumed.jsonl", "m2", (Some(0), None));
    assert_eq!(
        second[0],
        Signal::Bound {
            backend_session: THREAD.to_string()
        },
        "the same thread"
    );
    let ids = |signals: &[Signal]| -> Vec<String> {
        signals
            .iter()
            .filter_map(|signal| match signal {
                Signal::Reply(reply) => Some(reply.id.clone()),
                Signal::Tool(call) => Some(call.id.clone()),
                _ => None,
            })
            .collect()
    };
    // Codex called the first turn's command and the second turn's answer both `item_0`.
    assert_eq!(ids(&second), vec!["r-m2-item_0"]);
    assert!(
        ids(&first).iter().all(|id| !ids(&second).contains(id)),
        "in the conversation they are two things with two ids"
    );
    assert_eq!(second.last(), Some(&Signal::Ended(RunOutcome::Completed)));
}

#[test]
fn a_real_turn_that_failed_says_why_and_codexs_remarks_are_not_the_reason() {
    let signals = read("codex-0.167.1-failed.jsonl", "m1", (Some(1), None));
    assert_eq!(
        signals.last(),
        Some(&Signal::Ended(RunOutcome::Failed {
            reason: "The 'no-such-model-zz' model is not supported when using Codex with a ChatGPT account."
                .to_string()
        }))
    );
    assert!(
        !signals.iter().any(|signal| matches!(
            signal,
            Signal::Reply(_) | Signal::Tool(_) | Signal::Notice(_)
        )),
        "nothing of it is put into the conversation by the adapter: the Host says why a turn failed"
    );
}

#[test]
fn a_real_turn_that_was_stopped_leaves_the_command_it_was_running_to_the_host() {
    let signals = read("codex-0.167.1-stopped.jsonl", "m1", (None, Some(15)));
    let calls: Vec<_> = signals
        .iter()
        .filter_map(|signal| match signal {
            Signal::Tool(call) => Some((call.status, call.output.clone().unwrap_or_default())),
            _ => None,
        })
        .collect();
    assert_eq!(
        calls,
        vec![(ToolStatus::Running, String::new())],
        "what becomes of a command that never came back follows from how the turn ended"
    );
    assert_eq!(
        signals.last(),
        Some(&Signal::Ended(RunOutcome::Interrupted {
            reason: "codex was ended by signal 15".to_string()
        }))
    );
}
