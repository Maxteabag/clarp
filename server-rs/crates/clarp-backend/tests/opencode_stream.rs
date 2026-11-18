#![allow(clippy::unwrap_used)]
use clarp_backend::{Signal, by_id};
use clarp_kernel::{conversation::ToolStatus, turn::RunOutcome};
use clarp_spool::{Entry, Record, ShimEvent};
use serde_json::{Value, json};

#[test]
fn stream_records_agree_with_the_reference_drain() {
    let cases: Value = serde_json::from_str(include_str!(
        "../../clarp-kernel/tests/fixtures/backend-opencode-vectors.json"
    ))
    .unwrap();
    let mut count = 0;
    for story in cases["streams"].as_array().unwrap() {
        let mut interpreter = by_id("opencode").unwrap().interpreter(1, "reference");
        let mut signals = Vec::new();
        let r = |entry| Record { t: 1, entry };
        for line in story["lines"].as_array().unwrap() {
            interpreter.on_record(
                &r(Entry::Out(line.as_str().unwrap().to_string())),
                &mut signals,
            );
        }
        interpreter.on_record(
            &r(Entry::Err(story["stderr"].as_str().unwrap().into())),
            &mut signals,
        );
        interpreter.on_record(
            &r(Entry::Shim(ShimEvent::Exited {
                code: Some(story["code"].as_i64().unwrap() as i32),
                signal: None,
            })),
            &mut signals,
        );
        let mut bound = Vec::new();
        let mut calls = Vec::new();
        let mut last = String::new();
        let mut usage = None;
        let mut cost = None;
        let mut ends = Vec::new();
        for s in signals {
            match s {
                Signal::Bound { backend_session } => bound.push(backend_session),
                Signal::Tool(c) => calls.push(json!([
                    c.name,
                    c.input,
                    match c.status {
                        ToolStatus::Running => "running",
                        ToolStatus::Error => "error",
                        ToolStatus::Ok => "ok",
                        ToolStatus::Interrupted => "interrupted",
                    }
                ])),
                Signal::Reply(r) => last = r.text,
                Signal::Usage {
                    tokens_in,
                    tokens_out,
                } => usage = Some((tokens_in, tokens_out)),
                Signal::TurnCost { micros } => cost = Some(micros),
                Signal::Ended(outcome) => ends.push(outcome),
                _ => {}
            }
        }
        assert_eq!(json!(calls), story["calls"], "calls in case {count}");
        assert_eq!(json!(bound), story["sessions"], "session in case {count}");
        if let Some(msg) = story["error"].as_str() {
            assert_eq!(ends, [RunOutcome::Failed { reason: msg.into() }]);
            assert!(last.is_empty(), "output after failure in case {count}");
        } else {
            assert_eq!(ends, [RunOutcome::Completed]);
            let result = &story["result"];
            assert_eq!(last, result["last_agent_message"], "answer in case {count}");
            let tokens = &result["usage"];
            assert_eq!(
                usage,
                (!tokens.as_object().unwrap().is_empty()).then(|| (
                    tokens["input_tokens"].as_u64().unwrap_or(0),
                    tokens["output_tokens"].as_u64().unwrap_or(0)
                ))
            );
            assert_eq!(
                cost,
                result["total_cost_usd"]
                    .as_f64()
                    .map(|v| (v * 1e6).round() as u64)
            );
        }
        count += 1;
    }
    assert_eq!(count, 127);
}
