//! The Claude adapter against what Claude Code really printed.
//!
//! The fixtures are two turns recorded from Claude Code 2.1.321 on 15 Nov 2026, with the same
//! flags the Host starts it with: one that only answers, one that calls a tool and then
//! answers; and, recorded on 17 Nov 2026, a conversation made shorter (`/compact` for a
//! message). Recorded on 23 Nov 2026: a turn whose thinking was asked for in words
//! (`--thinking-display summarized`); three turns against a stand-in for the provider's
//! service, whose events are Claude Code's own: requests held back for a moment, a used-up
//! window, and an overload until Claude Code gave up (it was told to try twice); and one
//! whose conversation did not fit the model. Everything about the machine and the account was
//! taken out; the events, their order and their fields are as they came. When a newer Claude
//! Code changes its stream, record
//! it again, add it beside these, and keep the old ones: agents run on old versions for a while.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_backend::claude::ClaudeInterpreter;
use clarp_backend::{Interpreter, Signal};
use clarp_kernel::compaction::Boundary;
use clarp_kernel::conversation::ToolStatus;
use clarp_kernel::failures;
use clarp_kernel::live::Glimpse;
use clarp_kernel::turn::{Activity, ProviderRetry, RunOutcome};
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
                Signal::Live(_)
                    | Signal::Glimpse(_)
                    | Signal::Model(_)
                    | Signal::Usage { .. }
                    | Signal::Context { .. }
                    | Signal::Window { .. }
                    | Signal::Concluded
                    | Signal::Answered
                    | Signal::Cost { .. }
            )
        })
        .collect()
}

/// What those who watch are shown of a recorded turn: every event the hub sends for it, as a
/// client that applies each of them holds it at the end.
fn shown(fixture: &str) -> (clarp_kernel::live::View, usize) {
    use clarp_kernel::live::{Hub, View};
    use serde_json::json;

    let mut hub = Hub::new("epoch-1", "agent-1", "claire");
    let mut client = View::new();
    client.apply_snapshot(&json!({"epoch": "epoch-1", "lseq": 0}));
    let mut sent = hub.begin_turn("claire", "conv-1", "tr-1", None, "user", 0);
    let mut t = 0;
    for signal in read(fixture) {
        t += 1;
        if let Signal::Glimpse(glimpse) = signal {
            sent.extend(hub.show(&glimpse, Some("tr-1"), t));
        }
    }
    sent.extend(hub.end_turn("completed", None, t + 1));
    for event in &sent {
        assert!(
            client.apply_event(event).is_empty(),
            "a client takes {event}"
        );
    }
    (client, sent.len())
}

#[test]
fn a_real_turn_is_shown_as_items_that_all_settle() {
    let (plain, events) = shown("claude-2.1.321-plain.jsonl");
    assert!(events >= 4, "{events} events");
    let items = plain.items();
    let kinds: Vec<&str> = items
        .iter()
        .map(|item| item["kind"].as_str().unwrap())
        .collect();
    assert!(kinds.contains(&"message"), "{kinds:?}");
    for item in &items {
        assert_eq!(item["status"], "completed", "{item}");
        assert!(item["ended_at_ms"].is_i64(), "{item}");
    }
    let said = items
        .iter()
        .rfind(|item| item["kind"] == "message")
        .unwrap();
    assert_eq!(said["phase"], "final");
    assert!(!said["text"].as_str().unwrap().is_empty());
    assert!(said["row_id"].as_str().unwrap().starts_with("r-"), "{said}");
    assert_eq!(plain.activity()["state"], "idle");
    assert_eq!(plain.turn().unwrap()["status"], "completed");

    let (with_tool, _) = shown("claude-2.1.321-tool.jsonl");
    let items = with_tool.items();
    let tool = items.iter().find(|item| item["kind"] == "tool").unwrap();
    assert_eq!(tool["status"], "completed", "{tool}");
    assert_eq!(tool["tool"]["output"]["exit_code"], 0, "{tool}");
    assert!(
        !tool["tool"]["label"].as_str().unwrap().is_empty(),
        "{tool}"
    );
    assert_eq!(
        tool["id"],
        format!("cl:{}", tool["tool"]["call_id"].as_str().unwrap())
    );
    assert_eq!(with_tool.turn().unwrap()["tool_count"], 1);
    // What was said before the tool was narration; what was said after it is the answer.
    let phases: Vec<&str> = items
        .iter()
        .filter(|item| item["kind"] == "message")
        .map(|item| item["phase"].as_str().unwrap())
        .collect();
    assert_eq!(phases.last(), Some(&"final"), "{phases:?}");
    assert!(items.iter().all(|item| item["status"] == "completed"));
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
    // Its result says the answer is whole, after the answer and before the process goes.
    let at = |wanted: &Signal| signals.iter().position(|signal| signal == wanted).unwrap();
    let answer = signals
        .iter()
        .position(|signal| matches!(signal, Signal::Reply(_)));
    assert!(answer.unwrap() < at(&Signal::Answered));
    assert!(at(&Signal::Answered) < at(&Signal::Ended(RunOutcome::Completed)));
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
fn a_real_conversation_made_shorter_moves_the_agent_and_says_nothing() {
    // Recorded on 17 Nov 2026 with Claude Code 2.1.321 on Sonnet, started as the Host starts a
    // turn (print mode, a session continued by its name), with `/compact` for its message,
    // after two short turns in that session. It says that it is making the conversation
    // shorter, then how much shorter (30,729 tokens before, 8,957 after: its record of the
    // compaction), and ends well without a word of its own: its summary of the conversation
    // and the echo of the command come as messages of the user's, and are nobody's words. The
    // session keeps its name.
    let signals = read("claude-2.1.321-compact.jsonl");
    let stored = stored(&signals);
    assert_eq!(
        stored,
        [
            &Signal::Moved(Activity::Compacting),
            &Signal::Bound {
                backend_session: SESSION.to_string()
            },
            &Signal::Moved(Activity::Thinking),
            // The tool's own record of it, with both sizes: what the user's compaction did is
            // said from it (`clarp_kernel::compaction`).
            &Signal::Compacted(Boundary {
                pre_tokens: 30_729,
                post_tokens: Some(8_957),
            }),
            &Signal::Ended(RunOutcome::Completed),
        ],
        "{stored:#?}"
    );
    let said =
        |signal: &Signal| matches!(signal, Signal::Reply(_) | Signal::Live(_) | Signal::Tool(_));
    assert!(!signals.iter().any(said), "nothing was said: {signals:#?}");
    assert!(signals.contains(&Signal::Model("claude-sonnet-5-5".to_string())));
    // It tells no tokens of its own for the work, and what its session has cost so far: the
    // two turns before it and its own summary together.
    assert!(signals.contains(&Signal::Usage {
        tokens_in: 0,
        tokens_out: 0
    }));
    assert!(signals.contains(&Signal::Cost { micros: 143_641 }));
    // And how much the conversation holds afterwards: how full it is from here on.
    assert!(signals.contains(&Signal::Context { tokens: 8_957 }));
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

/// The words a recorded turn that failed ends on.
fn failed_with(fixture: &str) -> String {
    match read(fixture).last() {
        Some(Signal::Ended(RunOutcome::Failed { reason })) => reason.clone(),
        other => panic!("{fixture}: the turn fails: {other:?}"),
    }
}

#[test]
fn requests_held_back_for_a_moment_are_a_busy_provider_and_no_account_is_out() {
    // Claude Code tried twice, was refused with no window named, and gave up in its own words.
    let reason = failed_with("claude-2.1.321-held-back.jsonl");
    assert!(
        reason.starts_with(
            "API Error: Server is temporarily limiting requests (not your usage limit)"
        ),
        "{reason}"
    );
    assert_eq!(failures::classify_error(&reason), failures::TRANSIENT);
    assert!(failures::provider_busy(&reason));
}

#[test]
fn a_used_up_window_is_a_usage_limit() {
    // Claude Code's own words, which say when the window resets, are the reason.
    let reason = failed_with("claude-2.1.321-window-spent.jsonl");
    assert!(
        reason.contains("limit") && reason.contains("resets"),
        "{reason}"
    );
    assert_eq!(failures::classify_error(&reason), failures::USAGE_LIMIT);
}

#[test]
fn an_overloaded_provider_is_a_busy_one() {
    let reason = failed_with("claude-2.1.321-overloaded.jsonl");
    assert!(reason.starts_with("API Error: 529 Overloaded"), "{reason}");
    assert_eq!(failures::classify_error(&reason), failures::TRANSIENT);
    assert!(failures::provider_busy(&reason));
}

#[test]
fn thinking_in_words_is_the_turns_reasoning_and_never_its_reply() {
    let fixture = "claude-2.1.321-thinking.jsonl";
    let path = format!("{}/tests/fixtures/{fixture}", env!("CARGO_MANIFEST_DIR"));
    let recorded = std::fs::read_to_string(&path).unwrap();
    // What the model thought, as its pieces came.
    let whole: String = recorded
        .lines()
        .filter_map(|line| serde_json::from_str::<serde_json::Value>(line).ok())
        .filter(|event| event["event"]["delta"]["type"] == "thinking_delta")
        .filter_map(|event| {
            event["event"]["delta"]["thinking"]
                .as_str()
                .map(str::to_string)
        })
        .collect();
    assert!(
        whole.starts_with("I'm checking that 40 is the answer"),
        "{whole}"
    );
    let signals = read(fixture);
    let shown = signals.iter().rev().find_map(|signal| match signal {
        Signal::Glimpse(Glimpse::Reasoning { text, .. }) => Some(text.as_str()),
        _ => None,
    });
    assert_eq!(
        shown,
        Some(whole.as_str()),
        "the summary is shown whole, as reasoning"
    );
    assert!(
        !signals
            .iter()
            .any(|signal| matches!(signal, Signal::Reply(reply) if reply.text.contains("40 is the answer"))),
        "what the model thought is no part of its answer"
    );
    assert_eq!(signals.last(), Some(&Signal::Ended(RunOutcome::Completed)));
}

#[test]
fn a_real_turn_whose_provider_stays_overloaded_says_each_try() {
    let signals = read("claude-2.1.321-overloaded.jsonl");
    let stored = stored(&signals);
    let tried = |attempt: u32| {
        Signal::Moved(Activity::Retrying(ProviderRetry {
            attempt,
            of: 2,
            error: "overloaded".to_string(),
            status: Some(529),
        }))
    };
    assert_eq!(&stored[1..3], [&tried(1), &tried(2)], "{stored:#?}");
    // Once it has given up, the turn no longer says it tries again.
    assert_eq!(stored[3], &Signal::Moved(Activity::Thinking), "{stored:#?}");
    // The tool gives up in its own words, which say the provider is busy: a failure that
    // passes, asked again by the Host only where the turn has done nothing.
    let Some(Signal::Ended(RunOutcome::Failed { reason })) = signals.last() else {
        panic!("the turn fails: {signals:#?}");
    };
    assert!(reason.starts_with("API Error: 529 Overloaded."), "{reason}");
    assert_eq!(failures::classify_error(reason), failures::TRANSIENT);
}

#[test]
fn a_real_turn_whose_conversation_does_not_fit_the_model_says_so() {
    let signals = read("claude-2.1.321-too-long.jsonl");
    let Some(Signal::Ended(RunOutcome::Failed { reason })) = signals.last() else {
        panic!("the turn fails: {signals:#?}");
    };
    assert!(
        reason.starts_with("Prompt is too long · the request is ~1050000 tokens (limit 1000000)"),
        "the tool's own words: {reason}"
    );
    assert_eq!(failures::classify_error(reason), failures::PROMPT_TOO_LONG);
    let told = failures::told(
        failures::PROMPT_TOO_LONG,
        reason,
        "claude-haiku-5-5",
        failures::Known::default(),
    );
    assert_eq!(
        told.message,
        "This conversation is too long for the model. Compact it or start a new one"
    );
    assert!(told.row.ends_with(reason.as_str()), "{}", told.row);
}

/// A turn's result says how many tokens its model holds, and the turn tells it as the other
/// end of its gauge (issue 869). A turn whose result says nothing of it tells no window.
#[test]
fn a_real_turn_says_what_its_model_holds() {
    let signals = read("claude-2.1.321-thinking.jsonl");
    assert!(signals.contains(&Signal::Window { tokens: 1_000_000 }));
    let signals = read("claude-2.1.321-plain.jsonl");
    assert!(
        !signals
            .iter()
            .any(|signal| matches!(signal, Signal::Window { .. }))
    );
}
