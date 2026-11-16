//! The Claude adapter against what Claude Code really printed.
//!
//! The fixtures are two turns recorded from Claude Code 2.1.321 on 15 Nov 2026, with the same
//! flags the Host starts it with: one that only answers, one that calls a tool and then
//! answers. Everything about the machine and the account was taken out; the events, their
//! order and their fields are as they came. When a newer Claude Code changes its stream, record
//! it again, add it beside these, and keep the old ones: agents run on old versions for a while.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_backend::claude::ClaudeInterpreter;
use clarp_backend::{Interpreter, Signal};
use clarp_kernel::conversation::ToolStatus;
use clarp_kernel::turn::{Activity, RunOutcome};
use clarp_spool::{Entry, Record, ShimEvent};

const SESSION: &str = "00000000-0000-4000-8000-000000000001";

/// Reads a recorded turn the way the Host reads a spool: line by line, then the exit.
fn read(fixture: &str) -> Vec<Signal> {
    let path = format!("{}/tests/fixtures/{fixture}", env!("CARGO_MANIFEST_DIR"));
    let recorded = std::fs::read_to_string(&path).unwrap_or_else(|e| panic!("{path}: {e}"));
    let mut interpreter = ClaudeInterpreter::new(1, "m1");
    let mut out = Vec::new();
    let mut t = 0;
    for line in recorded.lines() {
        t += 1;
        let entry = Entry::Out(line.to_string());
        interpreter.on_record(&Record { t, entry }, &mut out);
    }
    let gone = Entry::Shim(ShimEvent::Exited {
        code: Some(0),
        signal: None,
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

fn stored(signals: &[Signal]) -> Vec<&Signal> {
    signals
        .iter()
        .filter(|signal| {
            !matches!(
                signal,
                Signal::Live(_) | Signal::Model(_) | Signal::Usage { .. }
            )
        })
        .collect()
}

#[test]
fn a_real_turn_that_only_answers() {
    let signals = read("claude-2.1.321-plain.jsonl");
    let stored = stored(&signals);
    assert_eq!(stored.len(), 4, "{stored:#?}");
    assert_eq!(
        stored[0],
        &Signal::Bound {
            backend_session: SESSION.to_string()
        }
    );
    assert_eq!(stored[1], &Signal::Moved(Activity::Responding));
    let Signal::Reply(reply) = stored[2] else {
        panic!("the answer is a reply, not {:?}", stored[2]);
    };
    assert_eq!(reply.text, "The quick brown fox.");
    assert!(
        reply.id.starts_with("r-") && reply.id.len() > 12 && !reply.id.contains('_'),
        "a reply is named after the provider's message, as one unbroken id"
    );
    assert_eq!(stored[3], &Signal::Ended(RunOutcome::Completed));
    // While it was written it was shown under the same name, and it ended as it was stored.
    let shown: Vec<_> = signals
        .iter()
        .filter_map(|signal| match signal {
            Signal::Live(live) => Some((live.id.as_str(), live.text.as_str())),
            _ => None,
        })
        .collect();
    assert_eq!(
        shown.last(),
        Some(&(reply.id.as_str(), reply.text.as_str()))
    );
}

#[test]
fn a_real_turn_that_calls_a_tool_and_then_answers() {
    let signals = read("claude-2.1.321-tool.jsonl");
    let stored = stored(&signals);
    assert_eq!(stored.len(), 9, "{stored:#?}");
    assert_eq!(
        stored[0],
        &Signal::Bound {
            backend_session: SESSION.to_string()
        }
    );
    assert_eq!(
        stored[1],
        &Signal::Moved(Activity::Tool {
            name: "Bash".to_string()
        })
    );
    // The message that calls the tool says nothing: it is a reply all the same, told when
    // the message is whole, and the tool's use is shown on it.
    let Signal::Reply(calling) = stored[2] else {
        panic!(
            "the message that calls the tool is a reply, not {:?}",
            stored[2]
        );
    };
    assert_eq!(calling.text, "");
    let Signal::Tool(started) = stored[3] else {
        panic!("a tool call starts, not {:?}", stored[3]);
    };
    assert_eq!(calling.tools, std::slice::from_ref(&started.id));
    assert_eq!(started.reply.as_deref(), Some(calling.id.as_str()));
    assert_eq!(started.status, ToolStatus::Running);
    assert_eq!(started.name, "Bash");
    assert_eq!(started.input["command"], "echo clarp-spool-check");
    let Signal::Tool(ended) = stored[4] else {
        panic!("the tool call ends, not {:?}", stored[4]);
    };
    assert_eq!(
        (ended.id.as_str(), ended.status),
        (started.id.as_str(), ToolStatus::Ok)
    );
    assert!(
        ended
            .output
            .as_deref()
            .unwrap()
            .contains("clarp-spool-check")
    );
    assert_eq!(ended.reply, started.reply);
    assert_eq!(stored[5], &Signal::Moved(Activity::Thinking));
    assert_eq!(stored[6], &Signal::Moved(Activity::Responding));
    let Signal::Reply(reply) = stored[7] else {
        panic!("then the agent answers, not {:?}", stored[7]);
    };
    assert!(reply.text.contains("clarp-spool-check"));
    assert!(reply.tools.is_empty() && reply.id != calling.id);
    assert_eq!(stored[8], &Signal::Ended(RunOutcome::Completed));
}

#[test]
fn a_recorded_turn_reads_the_same_however_often_it_is_read() {
    // A Host that restarts in the middle of a turn reads its spool again from the start.
    assert_eq!(
        read("claude-2.1.321-tool.jsonl"),
        read("claude-2.1.321-tool.jsonl")
    );
}

#[test]
fn a_real_turn_says_which_model_ran_it_and_what_it_cost() {
    let signals = read("claude-2.1.321-plain.jsonl");
    assert!(signals.contains(&Signal::Model("claude-haiku-5-5".to_string())));
    assert!(
        signals.iter().any(
            |signal| matches!(signal, Signal::Usage { tokens_in, tokens_out } if *tokens_in > 0 && *tokens_out > 0)
        ),
        "the result's usage is passed on"
    );
}
