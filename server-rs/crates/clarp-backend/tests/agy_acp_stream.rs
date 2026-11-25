//! What the Antigravity ACP card's interpreter makes of a turn's spool: the lines its driver
//! prints (`clarp-shim/src/agy_acp.rs`), as the driver's own tests against a stand-in for
//! `agy acp` see it print them.
#![allow(clippy::unwrap_used)]

use clarp_backend::{Signal, Steers, by_id};
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
        update(
            json!({"sessionUpdate": "tool_call", "toolCallId": "t-1", "title": "run_command",
                      "kind": "execute", "status": "pending", "rawInput": {"CommandLine": "ls", "Cwd": "/w"}}),
        ),
        json!({"clarp": "permission", "title": "Run ls?", "kind": "allow_once", "answer": "Allow"}),
        update(
            json!({"sessionUpdate": "tool_call_update", "toolCallId": "t-1", "status": "completed",
                      "rawOutput": {"combinedOutput": "Output:\na\nb"}}),
        ),
        update(
            json!({"sessionUpdate": "tool_call", "toolCallId": "t-2", "title": "Reading notes.md",
                      "kind": "read", "status": "in_progress", "locations": [{"path": "/w/notes.md"}]}),
        ),
        update(
            json!({"sessionUpdate": "tool_call_update", "toolCallId": "t-2", "status": "failed",
                      "content": [{"type": "content", "content": {"type": "text", "text": "no such file"}}]}),
        ),
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
    assert!(
        notices(&signals).is_empty(),
        "an allowed permission says nothing"
    );
}

#[test]
fn a_question_answered_by_policy_and_a_turn_limit_are_told_and_a_request_after_a_stop_is_not() {
    let lines = [
        json!({"clarp": "bound", "session": "conv-new", "resumed": false}),
        json!({"clarp": "notice", "code": "agy-model", "text": "Antigravity does not offer x."}),
        json!({"clarp": "permission", "title": "Which colour?", "kind": "reject_once", "answer": "Red", "stopped": false}),
        json!({"clarp": "permission", "title": "Run after stop?", "kind": null, "answer": null, "stopped": true}),
        json!({"clarp": "prompted", "result": {"stopReason": "max_tokens"}}),
    ];
    let signals = read(&lines, "", Some(0), None);
    let codes: Vec<&str> = notices(&signals).iter().map(|(code, _)| *code).collect();
    assert_eq!(codes, ["agy-model", "agy-question", "agy-turn-limit"]);
    assert!(
        notices(&signals)[1].1.contains("Which colour?") && notices(&signals)[1].1.contains("Red")
    );
    assert_eq!(ended(&signals), &RunOutcome::Completed);
}

#[test]
fn a_conversation_agy_no_longer_has_fails_the_turn_as_lost() {
    let lines = [json!({"clarp": "failed", "stage": "session",
                        "error": {"code": -32002, "message": "Session not found: conv-gone"},
                        "text": "Session not found: conv-gone"})];
    let reason = failure(&read(&lines, "", Some(1), None));
    assert!(
        reason.starts_with("Antigravity no longer has this conversation"),
        "{reason}"
    );
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
    // The result the driver settles a failure before the prompt with keeps its stage.
    let settled = [
        json!({"clarp": "failed", "stage": "auth", "text": "Authentication required"}),
        json!({"type": "result", "subtype": "success", "is_error": true,
               "result": "Authentication required", "user_message_uuids": ["primary"]}),
    ];
    assert_eq!(
        clarp_kernel::failures::classify_error(&failure(&read(&settled, "", Some(1), None))),
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

#[test]
fn a_write_shows_what_it_wrote_and_an_edit_what_it_replaced() {
    let lines = [
        json!({"clarp": "bound", "session": "conv-new", "resumed": false}),
        update(
            json!({"sessionUpdate": "tool_call", "toolCallId": "w-1", "title": "Writing hello.txt",
                      "kind": "edit", "status": "pending",
                      "rawInput": {"TargetFile": "/w/hello.txt", "Content": "hello\n"}}),
        ),
        update(
            json!({"sessionUpdate": "tool_call_update", "toolCallId": "w-1", "status": "completed"}),
        ),
        update(
            json!({"sessionUpdate": "tool_call", "toolCallId": "e-1", "title": "Changing notes.md",
                      "kind": "edit", "status": "in_progress"}),
        ),
        update(
            json!({"sessionUpdate": "tool_call_update", "toolCallId": "e-1", "status": "completed",
                      "content": [{"type": "diff", "path": "/w/notes.md", "oldText": "old line", "newText": "new line"}]}),
        ),
        json!({"clarp": "prompted", "result": {"stopReason": "end_turn"}}),
    ];
    let signals = read(&lines, "", Some(0), None);
    let ended: Vec<_> = signals
        .iter()
        .filter_map(|signal| match signal {
            Signal::Tool(call) if call.status != ToolStatus::Running => Some(call),
            _ => None,
        })
        .collect();
    assert_eq!(ended[0].name, "Write");
    assert_eq!(ended[0].input["file_path"], "/w/hello.txt");
    assert_eq!(ended[0].input["content"], "hello\n");
    assert_eq!(ended[1].name, "Edit");
    assert_eq!(ended[1].input["file_path"], "/w/notes.md");
    assert_eq!(ended[1].input["old_string"], "old line");
    assert_eq!(ended[1].input["new_string"], "new line");
}

#[test]
fn a_model_that_is_not_offered_fails_the_turn_in_the_drivers_words() {
    let why = "Antigravity does not offer the model gemini-z-low over ACP; it offers gemini-a, claude-b, each with a thinking level of low, high.";
    let lines = [
        json!({"clarp": "bound", "session": "conv-new", "resumed": false}),
        json!({"clarp": "failed", "stage": "config", "text": why}),
    ];
    let signals = read(&lines, "", Some(1), None);
    assert_eq!(failure(&signals), why);
    assert!(
        !signals.contains(&Signal::Concluded),
        "nothing was asked of the model"
    );
    // The driver settles the turn's own message after it in general words: the failure's
    // own words stay the turn's.
    let mut settled = lines.to_vec();
    settled.push(
        json!({"type": "result", "subtype": "success", "is_error": true,
                        "result": "Antigravity's turn failed before it answered",
                        "user_message_uuids": ["primary"]}),
    );
    let signals = read(&settled, "", Some(1), None);
    assert_eq!(failure(&signals), why);
    assert!(!signals.contains(&Signal::Concluded));
}

fn echo(uuid: &str) -> Value {
    json!({"type": "user", "isReplay": true, "uuid": uuid})
}

fn result(uuids: &[&str]) -> Value {
    json!({"type": "result", "subtype": "success", "is_error": false, "result": "",
           "user_message_uuids": uuids})
}

/// An answer of the driver's, naming the prompt it answers.
fn answer(uuid: &str, stop: &str) -> Value {
    json!({"clarp": "prompted", "uuid": uuid, "result": {"stopReason": stop}})
}

fn bound_as(primary: &str) -> Value {
    json!({"clarp": "bound", "session": "conv-new", "resumed": false, "uuid": primary})
}

fn prompted(tokens_in: u64, tokens_out: u64) -> Value {
    json!({"clarp": "prompted", "result": {"stopReason": "end_turn",
           "usage": {"inputTokens": tokens_in, "outputTokens": tokens_out}}})
}

#[test]
fn a_running_turn_takes_another_message_on_its_input_in_claude_and_agy_acp_and_as_words_on_the_lanes()
 {
    for (id, steers) in [
        ("claude", Steers::Stdin),
        ("agy-acp", Steers::Stdin),
        ("codex", Steers::Words),
        ("agy", Steers::No),
        ("grok", Steers::No),
        ("muse", Steers::Words),
        ("opencode", Steers::Words),
        ("deepseek", Steers::Words),
        ("scripted", Steers::No),
    ] {
        let backend = by_id(id).unwrap();
        assert_eq!(backend.steers(), steers, "{id}");
        // Only Claude Code keeps a record of what it read beside its output; for agy-acp the
        // spool is the record.
        assert_eq!(backend.records_reads(), id == "claude", "{id}");
        assert_eq!(
            backend.steers_message("and this too"),
            steers != Steers::No,
            "{id}"
        );
        assert!(
            !backend.steers_message("  \n"),
            "{id}: an empty message is no message"
        );
    }
    // agy-acp gives a steered message as words only: one with a picture waits for its own turn.
    let acp = by_id("agy-acp").unwrap();
    assert!(!acp.steers_message("look at /data/uploads/s/shot.png"));
    assert!(acp.steers_message("look at /data/notes/shot.txt"));
    assert!(
        !acp.steers_message("/compact"),
        "a command to agy waits for its own turn"
    );
}

#[test]
fn the_steering_evidence_is_no_line_of_the_turn_and_a_message_it_took_is_answered_in_it() {
    // The separate order: the first prompt is answered, then the driver takes the message
    // the Host gave it and answers that too.
    let lines = [
        json!({"clarp": "bound", "session": "conv-new", "resumed": false}),
        chunk("agent_message_chunk", "First"),
        prompted(10, 5),
        result(&["primary"]),
        echo("steered"),
        chunk("agent_message_chunk", "Second"),
        prompted(20, 7),
        result(&["steered"]),
        chunk("agent_message_chunk", "late"),
    ];
    let signals = read(&lines, "", Some(0), None);
    assert_eq!(
        replies(&signals),
        ["First", "Second"],
        "nothing after the last answer"
    );
    let concluded = signals
        .iter()
        .filter(|signal| **signal == Signal::Concluded)
        .count();
    assert_eq!(concluded, 1, "the turn's own answer says how it went");
    assert!(
        signals.contains(&Signal::Usage {
            tokens_in: 30,
            tokens_out: 12
        }),
        "the turn took what both answers took"
    );
    assert_eq!(ended(&signals), &RunOutcome::Completed);
    let endings = signals
        .iter()
        .filter(|signal| matches!(signal, Signal::Ended(_)))
        .count();
    assert_eq!(
        endings, 1,
        "the turn ends with its process, not at an answer"
    );

    // The joined order: the message is taken before the first answer, and each prompt is
    // answered after it.
    let joined = [
        json!({"clarp": "bound", "session": "conv-new", "resumed": false}),
        chunk("agent_message_chunk", "Working"),
        echo("steered"),
        prompted(1, 1),
        result(&["primary"]),
        chunk("agent_message_chunk", "Also that"),
        prompted(1, 1),
        result(&["steered"]),
    ];
    assert_eq!(
        replies(&read(&joined, "", Some(0), None)),
        ["Working", "Also that"]
    );

    // A message the driver did not take adds nothing: the turn is over at its own answer.
    let refused = [
        json!({"clarp": "bound", "session": "conv-new", "resumed": false}),
        chunk("agent_message_chunk", "Only this"),
        prompted(1, 1),
        result(&["primary"]),
        chunk("agent_message_chunk", "late"),
    ];
    assert_eq!(replies(&read(&refused, "", Some(0), None)), ["Only this"]);

    // One answer that covers both prompts ends the turn's output, though agy answered the
    // first prompt in a word of its own.
    let covered = [
        chunk("agent_message_chunk", "Working"),
        echo("steered"),
        chunk("agent_message_chunk", "And that"),
        prompted(1, 1),
        result(&["primary", "steered"]),
        chunk("agent_message_chunk", "late"),
    ];
    assert_eq!(
        replies(&read(&covered, "", Some(0), None)),
        ["Working", "And that"],
        "what agy said before it took the message is whole at the echo"
    );

    // An echo without a name is no message taken.
    let unnamed = [
        chunk("agent_message_chunk", "One"),
        json!({"type": "user", "isReplay": true}),
        prompted(1, 1),
        chunk("agent_message_chunk", "late"),
    ];
    assert_eq!(replies(&read(&unnamed, "", Some(0), None)), ["One"]);
}

#[test]
fn a_turn_stopped_while_it_answers_a_message_it_took_is_interrupted() {
    let lines = [
        json!({"clarp": "bound", "session": "conv-new", "resumed": false}),
        chunk("agent_message_chunk", "First"),
        prompted(1, 1),
        result(&["primary"]),
        echo("steered"),
        chunk("agent_message_chunk", "Half"),
        json!({"clarp": "cancel", "session": "conv-new"}),
        json!({"clarp": "prompted", "result": {"stopReason": "cancelled"}}),
    ];
    let signals = read(&lines, "", None, Some(15));
    assert_eq!(replies(&signals), ["First", "Half"]);
    assert!(matches!(
        ended(&signals),
        RunOutcome::Interrupted { reason } if reason == "Antigravity's turn was stopped"
    ));
}

#[test]
fn an_answer_counts_for_the_message_it_names_and_the_turn_goes_as_its_own_message_went() {
    // A failure the driver tags with a message agy never took (it refused it) is no answer
    // of the turn: its own answer still comes, and it decides how the turn went.
    let refused = [
        bound_as("primary"),
        chunk("agent_message_chunk", "Working"),
        json!({"clarp": "failed", "stage": "prompt", "uuid": "refused", "text": "busy"}),
        chunk("agent_message_chunk", " on it"),
        answer("primary", "end_turn"),
        result(&["primary"]),
    ];
    let signals = read(&refused, "", Some(0), None);
    assert_eq!(replies(&signals), ["Working on it"]);
    assert_eq!(ended(&signals), &RunOutcome::Completed);

    // The joined order with one result per answer: the steered message's failure is its own
    // (its result says so to the actor); the turn's own answer went well.
    let joined = [
        bound_as("primary"),
        chunk("agent_message_chunk", "Working"),
        echo("steered"),
        answer("primary", "end_turn"),
        result(&["primary"]),
        chunk("agent_message_chunk", "Then"),
        json!({"clarp": "failed", "stage": "prompt", "uuid": "steered", "text": "boom"}),
        json!({"type": "result", "subtype": "success", "is_error": true, "result": "boom",
               "user_message_uuids": ["steered"]}),
        chunk("agent_message_chunk", "late"),
    ];
    let signals = read(&joined, "", Some(0), None);
    assert_eq!(replies(&signals), ["Working", "Then"]);
    assert_eq!(ended(&signals), &RunOutcome::Completed);

    // An echo said twice, or an answer said twice, is one: the turn's output ends at the
    // last message's answer.
    let twice = [
        bound_as("primary"),
        echo("steered"),
        echo("steered"),
        chunk("agent_message_chunk", "One"),
        answer("primary", "end_turn"),
        answer("primary", "end_turn"),
        chunk("agent_message_chunk", "Two"),
        answer("steered", "end_turn"),
        chunk("agent_message_chunk", "late"),
    ];
    assert_eq!(replies(&read(&twice, "", Some(0), None)), ["One", "Two"]);

    // The turn's own message failing fails the turn, though a message it took went well.
    let failed = [
        bound_as("primary"),
        echo("steered"),
        chunk("agent_message_chunk", "Hm"),
        json!({"clarp": "failed", "stage": "prompt", "uuid": "primary",
               "text": "Error 500, Message: internal"}),
        answer("steered", "end_turn"),
    ];
    assert!(matches!(
        ended(&read(&failed, "", Some(0), None)),
        RunOutcome::Failed { .. }
    ));
}

#[test]
fn a_chain_answered_under_the_steered_message_tells_the_turn_how_it_stopped() {
    // agy hands the work over to the newer prompt: its answer names the steered message,
    // and the result that names both settles the turn's own with how that answer went.
    let limit = [
        bound_as("primary"),
        chunk("agent_message_chunk", "Working"),
        echo("steered"),
        chunk("agent_message_chunk", "More"),
        answer("steered", "max_tokens"),
        result(&["primary", "steered"]),
    ];
    let signals = read(&limit, "", Some(0), None);
    assert_eq!(replies(&signals), ["Working", "More"]);
    assert!(
        notices(&signals)
            .iter()
            .any(|(code, _)| *code == "agy-turn-limit"),
        "the limit the chain reached is the turn's"
    );
    let failed = [
        bound_as("primary"),
        echo("steered"),
        json!({"clarp": "failed", "stage": "prompt", "uuid": "steered",
               "text": "Error 429, Message: Resource has been exhausted, Status: RESOURCE_EXHAUSTED"}),
        json!({"type": "result", "subtype": "success", "is_error": true, "result": "x",
               "user_message_uuids": ["primary", "steered"]}),
    ];
    let reason = failure(&read(&failed, "", Some(0), None));
    assert_eq!(
        clarp_kernel::failures::classify_error(&reason),
        clarp_kernel::failures::USAGE_LIMIT,
        "{reason}"
    );
}

#[test]
fn a_reply_cut_for_a_message_is_no_stop_and_the_turn_answers_both() {
    // As the driver prints a cut: the reply up to a line end, the cut's own lines (never
    // `cancel`, which is a Stop's), the read of the message, its answer, and one result.
    let lines = [
        bound_as("primary"),
        chunk("agent_message_chunk", "1. Pho\n2. Ramen\n"),
        json!({"clarp": "cut", "uuid": "steered", "stage": "held"}),
        json!({"clarp": "cut", "uuid": "steered", "stage": "cancel", "session": "conv-new"}),
        json!({"clarp": "cut", "uuid": "steered", "stage": "sent", "given_back": 16,
               "left_out": 4}),
        json!({"clarp": "steer", "uuid": "steered", "sent": true}),
        echo("steered"),
        chunk("agent_message_chunk", "BANANA"),
        answer("primary", "end_turn"),
        result(&["primary", "steered"]),
    ];
    let signals = read(&lines, "", Some(0), None);
    assert_eq!(replies(&signals), ["1. Pho\n2. Ramen\n", "BANANA"]);
    assert_eq!(ended(&signals), &RunOutcome::Completed);

    // The cut reply is a row from the cut on: at the cut's own line, before the message is
    // read, never rewritten later.
    let mut interpreter = by_id("agy-acp").unwrap().interpreter(4, "t4");
    let mut out = Vec::new();
    let mut t = 0;
    for line in lines.iter().take(4) {
        t += 1;
        let entry = Entry::Out(line.to_string());
        interpreter.on_record(&Record { t, entry }, &mut out);
    }
    assert_eq!(replies(&out), ["1. Pho\n2. Ramen\n"]);
    let mut later = Vec::new();
    for line in lines.iter().skip(4) {
        t += 1;
        let entry = Entry::Out(line.to_string());
        interpreter.on_record(&Record { t, entry }, &mut later);
    }
    assert_eq!(replies(&later), ["BANANA"], "the cut row is not told again");

    // agy refused the message after the cut: the cut prompt's answer stands, as ended.
    let refused = [
        bound_as("primary"),
        chunk("agent_message_chunk", "1. Pho\n"),
        json!({"clarp": "cut", "uuid": "steered", "stage": "cancel", "session": "conv-new"}),
        json!({"type": "steer_refused", "uuid": "steered", "reason": null}),
        answer("primary", "end_turn"),
        result(&["primary"]),
    ];
    let signals = read(&refused, "", Some(0), None);
    assert_eq!(replies(&signals), ["1. Pho\n"]);
    assert_eq!(ended(&signals), &RunOutcome::Completed);
}

/// The ids of the live items a turn of this number shows, reading these lines.
fn live_ids(turn: u64, lines: &[Value]) -> Vec<String> {
    let mut interpreter = by_id("agy-acp")
        .unwrap()
        .interpreter(turn, &format!("t{turn}"));
    let mut out = Vec::new();
    for (t, line) in (1..).zip(lines) {
        let entry = Entry::Out(line.to_string());
        interpreter.on_record(&Record { t, entry }, &mut out);
    }
    let id = regex::Regex::new(r#"\bid: "([^"]*)""#).unwrap();
    out.iter()
        .filter(|signal| matches!(signal, Signal::Glimpse(_)))
        .flat_map(|signal| {
            let shown = format!("{signal:?}");
            id.captures_iter(&shown)
                .map(|found| found[1].to_string())
                .collect::<Vec<_>>()
        })
        .collect()
}

#[test]
fn the_live_items_of_two_turns_never_share_an_id() {
    // agy numbers what it says from the start each turn: without the turn, a turn's answer
    // took the place of the turn before's (seen on 25 Nov 2026, both "aa:m2").
    let lines = [
        bound_as("primary"),
        chunk("agent_thought_chunk", "Planning"),
        chunk("agent_message_chunk", "First"),
        update(json!({"sessionUpdate": "tool_call", "toolCallId": "call_7",
                      "title": "ls", "kind": "execute", "status": "in_progress"})),
        update(
            json!({"sessionUpdate": "tool_call_update", "toolCallId": "call_7",
                      "status": "completed"}),
        ),
        chunk("agent_message_chunk", "Done"),
        answer("primary", "end_turn"),
        result(&["primary"]),
    ];
    let first = live_ids(302, &lines);
    let second = live_ids(303, &lines);
    for item in ["aa:302:t1", "aa:302:m2", "aa:302:call_7", "aa:302:m3"] {
        assert!(first.iter().any(|id| id == item), "{item}: {first:?}");
    }
    assert!(
        first.iter().all(|id| !second.contains(id)),
        "{first:?} {second:?}"
    );
    // A replay of the spool names them as the turn did.
    assert_eq!(live_ids(302, &lines), first);
}
