//! What the Antigravity ACP card's interpreter makes of a turn's spool: the lines its driver
//! prints (`clarp-shim/src/agy_acp.rs`), as the driver's own tests against a stand-in for
//! `agy acp` see it print them.
#![allow(clippy::unwrap_used)]

use clarp_backend::{Signal, by_id};
use clarp_kernel::conversation::ToolStatus;
use clarp_kernel::turn::RunOutcome;
use clarp_spool::{Entry, Record, ShimEvent};
use serde_json::{Value, json};

fn update(value: Value) -> Value {
    json!({"jsonrpc": "2.0", "method": "session/update",
           "params": {"sessionId": "conv-new", "update": value}})
}

fn chunk(kind: &str, text: &str) -> Value {
    update(json!({"sessionUpdate": kind, "content": {"type": "text", "text": text}}))
}

/// The signals of a turn whose driver printed `lines`, wrote `stderr`, and ended so.
fn read(lines: &[Value], stderr: &str, code: Option<i32>, signal: Option<i32>) -> Vec<Signal> {
    let mut interpreter = by_id("agy-acp").unwrap().interpreter(4, "t4");
    let mut out = Vec::new();
    let mut t = 0;
    let mut record = |entry, out: &mut Vec<Signal>| {
        t += 1;
        interpreter.on_record(&Record { t, entry }, out);
    };
    for line in lines {
        record(Entry::Out(line.to_string()), &mut out);
    }
    for line in stderr.lines() {
        record(Entry::Err(line.to_string()), &mut out);
    }
    record(Entry::Shim(ShimEvent::Exited { code, signal }), &mut out);
    out
}

fn ended(signals: &[Signal]) -> &RunOutcome {
    signals
        .iter()
        .find_map(|signal| match signal {
            Signal::Ended(outcome) => Some(outcome),
            _ => None,
        })
        .unwrap()
}

fn replies(signals: &[Signal]) -> Vec<&str> {
    signals
        .iter()
        .filter_map(|signal| match signal {
            Signal::Reply(reply) => Some(reply.text.as_str()),
            _ => None,
        })
        .collect()
}

fn notices(signals: &[Signal]) -> Vec<(&str, &str)> {
    signals
        .iter()
        .filter_map(|signal| match signal {
            Signal::Notice(notice) => Some((notice.code.as_str(), notice.text.as_str())),
            _ => None,
        })
        .collect()
}

fn failure(signals: &[Signal]) -> String {
    match ended(signals) {
        RunOutcome::Failed { reason } => reason.clone(),
        other => panic!("not a failure: {other:?}"),
    }
}

#[test]
fn a_turn_binds_its_conversation_names_its_model_and_answers_once() {
    let lines = [
        json!({"clarp": "initialized", "agent": {"name": "agy"}, "appends": true}),
        json!({"clarp": "bound", "session": "conv-new", "resumed": false, "lost": null}),
        json!({"clarp": "model", "model": "claude-b"}),
        chunk("agent_thought_chunk", "Considering"),
        chunk("agent_message_chunk", "Hel"),
        chunk("agent_message_chunk", "lo"),
        update(json!({"sessionUpdate": "usage_update", "used": 1234, "size": 1_000_000})),
        json!({"clarp": "prompted", "result": {"stopReason": "end_turn",
               "usage": {"inputTokens": 10, "outputTokens": 5, "thoughtTokens": 2, "cachedReadTokens": 3}}}),
    ];
    let signals = read(&lines, "", Some(0), None);
    assert!(signals.contains(&Signal::Bound {
        backend_session: "conv-new".into()
    }));
    assert!(signals.contains(&Signal::Model("claude-b".into())));
    assert!(signals.contains(&Signal::Context { tokens: 1234 }));
    assert!(signals.contains(&Signal::Usage {
        tokens_in: 13,
        tokens_out: 7
    }));
    assert!(signals.contains(&Signal::Concluded));
    assert_eq!(replies(&signals), ["Hello"]);
    assert_eq!(ended(&signals), &RunOutcome::Completed);
    // What is said after the answer is no part of the turn.
    let mut after = lines.to_vec();
    after.push(chunk("agent_message_chunk", "late"));
    assert_eq!(replies(&read(&after, "", Some(0), None)), ["Hello"]);
}

#[test]
fn a_tool_call_is_named_as_the_conversation_names_tools_and_ends_with_its_output() {
    let lines = [
        json!({"clarp": "bound", "session": "conv-new", "resumed": false}),
        chunk("agent_message_chunk", "Looking."),
        update(json!({"sessionUpdate": "tool_call", "toolCallId": "t-1", "title": "run_command",
                      "kind": "execute", "status": "pending", "rawInput": {"CommandLine": "ls", "Cwd": "/w"}})),
        json!({"clarp": "permission", "title": "Run ls?", "answer": "allow_once"}),
        update(json!({"sessionUpdate": "tool_call_update", "toolCallId": "t-1", "status": "completed",
                      "rawOutput": {"combinedOutput": "Output:\na\nb"}})),
        update(json!({"sessionUpdate": "tool_call", "toolCallId": "t-2", "title": "Reading notes.md",
                      "kind": "read", "status": "in_progress", "locations": [{"path": "/w/notes.md"}]})),
        update(json!({"sessionUpdate": "tool_call_update", "toolCallId": "t-2", "status": "failed",
                      "content": [{"type": "content", "content": {"type": "text", "text": "no such file"}}]})),
        chunk("agent_message_chunk", "Done."),
        json!({"clarp": "prompted", "result": {"stopReason": "end_turn"}}),
    ];
    let signals = read(&lines, "", Some(0), None);
    let calls: Vec<_> = signals
        .iter()
        .filter_map(|signal| match signal {
            Signal::Tool(call) if call.status != ToolStatus::Running => Some(call),
            _ => None,
        })
        .collect();
    assert_eq!(calls.len(), 2);
    assert_eq!(calls[0].name, "Bash");
    assert_eq!(calls[0].input["command"], "ls");
    assert_eq!(calls[0].status, ToolStatus::Ok);
    assert_eq!(calls[0].output.as_deref(), Some("a\nb"));
    assert_eq!(calls[1].name, "Read");
    assert_eq!(calls[1].input["file_path"], "/w/notes.md");
    assert_eq!(calls[1].status, ToolStatus::Error);
    // A message before a tool and one after it are two messages.
    assert_eq!(replies(&signals), ["Looking.", "Done."]);
    assert!(notices(&signals).is_empty(), "an allowed permission says nothing");
}

#[test]
fn a_declined_question_a_lost_conversation_and_a_turn_limit_are_told() {
    let lines = [
        json!({"clarp": "bound", "session": "conv-new", "resumed": false, "lost": "conv-gone"}),
        json!({"clarp": "notice", "code": "agy-model", "text": "Antigravity does not offer x."}),
        json!({"clarp": "permission", "title": "Which colour?", "answer": "cancelled"}),
        json!({"clarp": "prompted", "result": {"stopReason": "max_tokens"}}),
    ];
    let signals = read(&lines, "", Some(0), None);
    let codes: Vec<&str> = notices(&signals).iter().map(|(code, _)| *code).collect();
    assert_eq!(
        codes,
        ["agy-new-conversation", "agy-model", "agy-question", "agy-turn-limit"]
    );
    assert!(notices(&signals)[0].1.contains("conv-gone"));
    assert!(notices(&signals)[2].1.contains("Which colour?"));
    assert_eq!(ended(&signals), &RunOutcome::Completed);
}

#[test]
fn a_stopped_turn_is_interrupted_and_what_was_said_before_is_kept() {
    let lines = [
        json!({"clarp": "bound", "session": "conv-new", "resumed": false}),
        chunk("agent_message_chunk", "working"),
        json!({"clarp": "cancel", "session": "conv-new"}),
        json!({"clarp": "prompted", "result": {"stopReason": "cancelled"}}),
    ];
    let signals = read(&lines, "", None, Some(15));
    assert_eq!(replies(&signals), ["working"]);
    assert_eq!(
        ended(&signals),
        &RunOutcome::Interrupted {
            reason: "Antigravity's turn was stopped".into()
        }
    );
}

#[test]
fn failures_are_told_in_words_the_hosts_reading_of_failures_knows() {
    let failed = |stage: &str, text: &str| {
        let lines = [json!({"clarp": "failed", "stage": stage, "text": text})];
        failure(&read(&lines, "", Some(1), None))
    };
    let quota = failed(
        "prompt",
        "Error 429, Message: Resource has been exhausted, Status: RESOURCE_EXHAUSTED \
         [reason:QUOTA_EXHAUSTED quotaResetTimeStamp:2026-11-23T18:00:00Z]",
    );
    assert!(quota.contains("usage quota exhausted") && quota.contains("2026-11-23T18:00:00Z"));
    assert_eq!(
        clarp_kernel::failures::classify_error(&quota),
        clarp_kernel::failures::USAGE_LIMIT
    );
    let signature = failed(
        "prompt",
        "messages.3.content.0: Invalid `signature` in `thinking` block",
    );
    assert!(signature.contains("new conversation"), "{signature}");
    let signed_out = failed("auth", "Authentication required");
    assert_eq!(
        clarp_kernel::failures::classify_error(&signed_out),
        clarp_kernel::failures::AUTH
    );
    let silent = failed("prompt", "Antigravity stopped answering");
    assert_eq!(silent, "Antigravity stopped answering");

    // A driver that ended without a word says what agy wrote last.
    let signals = read(&[], "agy: cannot start\n", Some(1), None);
    assert_eq!(failure(&signals), "agy: cannot start");
    // An error whose reason is only in what agy wrote is read there.
    let lines = [json!({"clarp": "failed", "stage": "prompt", "text": "Internal error"})];
    let signals = read(
        &lines,
        "RESOURCE_EXHAUSTED (code 429): [reason:QUOTA_EXHAUSTED quotaResetDelay:2h]",
        Some(1),
        None,
    );
    assert!(failure(&signals).contains("resets in 2h"));
}

#[test]
fn the_card_shares_antigravitys_models_and_is_shown_under_its_own_name() {
    let card = clarp_backend::catalogue::declared("agy-acp");
    let agy = clarp_backend::catalogue::declared("agy");
    assert_eq!(card["shown"]["label"], "Antigravity (ACP)");
    assert_eq!(card["models"], agy["models"]);
    assert_eq!(card["program"], "agy");
    assert_eq!(card["runs_through"], "agy");
}
