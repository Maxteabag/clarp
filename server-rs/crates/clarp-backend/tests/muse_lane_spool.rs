//! A turn of the Muse lane (issue 762) read by the Muse reader the exec turns use.
#![allow(clippy::unwrap_used)]

use clarp_backend::{Signal, Steers, by_id};
use clarp_kernel::turn::RunOutcome;
use clarp_spool::{Entry, Record, ShimEvent};
use serde_json::json;

/// A turn of the Muse lane (issue 762) is written in exec's lines, with the lane's own
/// beside them: read as exec's, it gives the same reply, tool, usage, model and end.
#[test]
fn a_lane_turn_reads_as_an_exec_turn() {
    let backend = by_id("muse").unwrap();
    assert_eq!(backend.steers(), Steers::Words);
    let mut reader = backend.interpreter(1, "lane");
    let lines = [
        json!({"payload_type": "run.lifecycle.started", "payload": {}}),
        json!({"payload_type": "run.model.configured", "payload": {"model_id": "m"}}),
        json!({"type": "steer.open"}),
        json!({"payload_type": "tool.result", "payload": {"call_id": "c1", "tool_name": "shell",
            "text": "ok", "correlation_facts": {"tool_name": "shell", "outcome": "success"}}}),
        json!({"type": "turn.progress"}),
        json!({"payload_type": "run.output.delta", "payload": {"text": "Hello"}}),
        json!({"type": "steer.read", "id": "w"}),
        json!({"type": "turn.answered"}),
        json!({"payload_type": "run.usage", "payload": {"models": [
            {"model": "m", "input_tokens": 10, "output_tokens": 3}],
            "allowance": {"as_of_ms": 5, "weekly": {"used_percent": 6}}}}),
        json!({"payload_type": "run.terminal.completed", "payload": {"terminal": "completed"}}),
        json!({"type": "turn.completed"}),
    ];
    let mut signals = Vec::new();
    for (t, line) in lines.iter().enumerate() {
        reader.on_record(
            &Record {
                t: t as i64,
                entry: Entry::Out(line.to_string()),
            },
            &mut signals,
        );
    }
    reader.on_record(
        &Record {
            t: 20,
            entry: Entry::Shim(ShimEvent::Exited {
                code: Some(0),
                signal: None,
            }),
        },
        &mut signals,
    );
    let text: String = signals
        .iter()
        .filter_map(|signal| match signal {
            Signal::Reply(reply) => Some(reply.text.clone()),
            _ => None,
        })
        .collect();
    assert_eq!(text, "Hello");
    assert!(
        signals.contains(&Signal::Answered),
        "the answer is whole at turn.answered"
    );
    assert!(signals.contains(&Signal::Model("m".into())));
    assert!(signals.contains(&Signal::Usage {
        tokens_in: 10,
        tokens_out: 3
    }));
    assert!(signals.iter().any(|s| matches!(s, Signal::Limits(_))));
    assert!(
        signals
            .iter()
            .any(|s| matches!(s, Signal::Tool(call) if !call.name.is_empty()))
    );
    assert_eq!(signals.last(), Some(&Signal::Ended(RunOutcome::Completed)));

    // A Stop's end is exec's on SIGINT, and Muse's own cancel is a stop by Muse.
    let mut stopped = backend.interpreter(2, "lane");
    let mut out = Vec::new();
    stopped.on_record(
        &Record {
            t: 1,
            entry: Entry::Shim(ShimEvent::Exited {
                code: None,
                signal: Some(2),
            }),
        },
        &mut out,
    );
    assert!(matches!(
        out.last(),
        Some(Signal::Ended(RunOutcome::Interrupted { .. }))
    ));
}
