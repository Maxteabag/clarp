#![allow(clippy::unwrap_used)]
use clarp_backend::{Signal, by_id};
use clarp_kernel::{conversation::ToolStatus, turn::RunOutcome};
use clarp_spool::{Entry, Record, ShimEvent};
use serde_json::{Value, json};

#[test]
fn cli_replay_of_a_completed_part_preserves_distinct_repeated_words() {
    let mut interpreter = by_id("opencode").unwrap().interpreter(1, "replayed-text");
    let mut signals = Vec::new();
    let words = "<speak>Summary.</speak>";
    let new = "<speak>New words.</speak>";
    let text = |id: &str, text: &str| {
        json!({"type":"text","sessionID":"ses_1","part":{
            "type":"text","id":id,"messageID":"msg_1","text":text,"time":{"end":1}
        }})
    };
    for event in [
        json!({"type":"step_start"}),
        text("prt_1", words),
        json!({"type":"step_finish"}),
        json!({"type":"step_start"}),
        text("prt_2", words),
        json!({"type":"step_finish"}),
        text("prt_1", words),
        text("prt_2", &format!("{words}{new}")),
    ] {
        interpreter.on_record(
            &Record {
                t: 1,
                entry: Entry::Out(event.to_string()),
            },
            &mut signals,
        );
    }
    interpreter.on_record(
        &Record {
            t: 2,
            entry: Entry::Shim(ShimEvent::Exited {
                code: Some(0),
                signal: None,
            }),
        },
        &mut signals,
    );
    let replies: Vec<_> = signals
        .iter()
        .filter_map(|signal| match signal {
            Signal::Reply(reply) => Some(reply.text.as_str()),
            _ => None,
        })
        .collect();
    assert_eq!(replies, [words, words, new]);
}

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
                // A step's call is a reply too, with no words when none came before it.
                Signal::Reply(r) if !r.text.is_empty() => last = r.text,
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

/// What the turn driver records of a turn whose provider refused the account (OpenCode
/// 1.18.33's free tier, issue 721): the session, the user's prompt, and the `retry` OpenCode
/// would repeat without end. The driver lets the turn go there; the turn ends on the
/// provider's words, of the usage-limit kind.
#[test]
fn a_refusal_no_retry_gets_past_ends_the_turn_on_the_providers_words() {
    let lines = include_str!("fixtures/opencode-1.18.33-refused.jsonl");
    let mut interpreter = by_id("opencode").unwrap().interpreter(1, "refused");
    let mut signals = Vec::new();
    let r = |entry| Record { t: 1, entry };
    for line in lines.lines() {
        interpreter.on_record(&r(Entry::Out(line.to_string())), &mut signals);
    }
    interpreter.on_record(
        &r(Entry::Shim(ShimEvent::Exited {
            code: Some(0),
            signal: None,
        })),
        &mut signals,
    );
    let ends: Vec<RunOutcome> = signals
        .into_iter()
        .filter_map(|signal| match signal {
            Signal::Ended(outcome) => Some(outcome),
            _ => None,
        })
        .collect();
    let said = "Free usage exceeded, subscribe to Go";
    assert_eq!(
        ends,
        [RunOutcome::Failed {
            reason: said.into()
        }]
    );
    assert_eq!(
        clarp_kernel::failures::classify_error(said),
        clarp_kernel::failures::USAGE_LIMIT
    );
}

#[test]
fn a_passing_retry_is_left_to_opencode() {
    let refused = |status: Value| clarp_kernel::opencode::refused(&status);
    assert_eq!(refused(json!({"type": "busy"})), None);
    assert_eq!(
        refused(json!({"type": "retry", "attempt": 2, "message": "Rate limited, retrying"})),
        None
    );
    assert_eq!(
        refused(json!({"type": "retry", "message": "", "action": {"title": "Free limit reached"}})),
        Some("Free limit reached".into())
    );
    assert_eq!(
        refused(json!({"type": "retry", "message": "Insufficient credits"})),
        Some("Insufficient credits".into())
    );
}

#[test]
fn an_answer_names_the_model_that_gave_it() {
    let mut interpreter = by_id("opencode").unwrap().interpreter(1, "model");
    let mut signals = Vec::new();
    let updated = |role: &str, provider: &str, model: &str| {
        json!({"type": "message.updated", "properties": {"info": {
            "id": "msg_1", "sessionID": "ses_1", "role": role,
            "providerID": provider, "modelID": model}}})
        .to_string()
    };
    for line in [
        updated("user", "opencode", "big-pickle"),
        updated("assistant", "opencode", "big-pickle"),
        // Told again as the answer grows: named once.
        updated("assistant", "opencode", "big-pickle"),
        updated("assistant", "huggingface", "zai-org/GLM-5.3"),
    ] {
        interpreter.on_record(
            &Record {
                t: 1,
                entry: Entry::Out(line),
            },
            &mut signals,
        );
    }
    let models: Vec<String> = signals
        .into_iter()
        .filter_map(|signal| match signal {
            Signal::Model(model) => Some(model),
            _ => None,
        })
        .collect();
    assert_eq!(
        models,
        ["opencode/big-pickle", "huggingface/zai-org/GLM-5.3"]
    );
}

/// OpenCode's server tells a step's parts as they come: the step's start, its words, the call
/// they announce, the call again as it runs and ends, the step's end. The words of a step that
/// calls a tool go with that call, written before it, so what announced a call is never read as
/// words after work. The last step's words, with no call, are a reply of their own.
#[test]
fn a_steps_words_go_with_its_call_and_are_written_before_it() {
    let mut interpreter = by_id("opencode").unwrap().interpreter(1, "steps");
    let mut signals = Vec::new();
    let part = |part: Value| {
        json!({"type": "message.part.updated", "properties": {"part": part}}).to_string()
    };
    let start =
        |n: u32| part(json!({"type": "step-start", "id": format!("s{n}"), "messageID": "msg_1"}));
    let text = |n: u32, words: &str| {
        part(json!({"type": "text", "id": format!("x{n}"), "messageID": "msg_1", "text": words}))
    };
    let tool = |n: u32, status: &str| {
        part(
            json!({"type": "tool", "id": format!("p{n}"), "messageID": "msg_1",
            "callID": format!("call_{n}"), "tool": "bash",
            "state": {"status": status, "input": {"command": "make test"}, "output": "ok"}}),
        )
    };
    let finish =
        |n: u32| part(json!({"type": "step-finish", "id": format!("f{n}"), "messageID": "msg_1"}));
    for line in [
        start(1),
        text(1, "Let me look."),
        tool(1, "running"),
        tool(1, "completed"),
        finish(1),
        start(2),
        text(2, "Running the tests."),
        tool(2, "running"),
        tool(3, "running"),
        tool(2, "completed"),
        tool(3, "completed"),
        finish(2),
        start(3),
        text(3, "Done, all green."),
        finish(3),
    ] {
        interpreter.on_record(
            &Record {
                t: 1,
                entry: Entry::Out(line),
            },
            &mut signals,
        );
    }
    interpreter.on_record(
        &Record {
            t: 1,
            entry: Entry::Shim(ShimEvent::Exited {
                code: Some(0),
                signal: None,
            }),
        },
        &mut signals,
    );
    // The rows as the conversation keeps them, in order: a reply with its words and calls,
    // a call when it begins (with the reply it belongs to).
    let rows: Vec<Value> = signals
        .iter()
        .filter_map(|signal| match signal {
            Signal::Reply(r) => Some(json!({"reply": r.id, "text": r.text, "tools": r.tools})),
            Signal::Tool(c) if c.status == ToolStatus::Running => {
                Some(json!({"call": c.id, "of": c.reply}))
            }
            _ => None,
        })
        .collect();
    assert_eq!(
        rows,
        [
            json!({"reply": "r-steps-1", "text": "Let me look.", "tools": ["t-steps-call_1"]}),
            json!({"call": "t-steps-call_1", "of": "r-steps-1"}),
            json!({"reply": "r-steps-2", "text": "Running the tests.", "tools": ["t-steps-call_2"]}),
            json!({"call": "t-steps-call_2", "of": "r-steps-2"}),
            json!({"reply": "r-steps-2-1", "text": "", "tools": ["t-steps-call_3"]}),
            json!({"call": "t-steps-call_3", "of": "r-steps-2-1"}),
            json!({"reply": "r-steps-3", "text": "Done, all green.", "tools": []}),
        ]
    );
}

/// Issue 858, as an Android run on OpenCode saw it (opencode/big-pickle, 25 Nov 2026): a step says
/// "OK", calls a tool, and only then OpenCode tells the text part whole once more, as it ends.
/// Those words are written once, with the call they announced, and shown as one message: they
/// are no second reply that the answer's words would make an answer. Only the answer is a reply
/// of words alone.
#[test]
fn a_text_part_told_whole_after_its_call_began_is_no_second_reply() {
    use clarp_kernel::live::Glimpse;
    let part = |part: Value| {
        json!({"type": "message.part.updated", "properties": {"sessionID": "s", "part": part}})
            .to_string()
    };
    let delta = |message: &str, id: &str, words: &str| {
        json!({"type": "message.part.delta", "properties": {
            "sessionID": "s", "messageID": message, "partID": id, "field": "text", "delta": words,
        }})
        .to_string()
    };
    let tool = |status: &str| {
        part(
            json!({"id": "prt-tool", "messageID": "m1", "type": "tool", "tool": "bash",
                    "callID": "call-1", "state": {"status": status, "input": {"command": "sleep 1"}}}),
        )
    };
    let lines = [
        part(json!({"id": "prt-s1", "messageID": "m1", "type": "step-start"})),
        part(
            json!({"id": "prt-ok", "messageID": "m1", "type": "text", "text": "",
                    "time": {"start": 1}}),
        ),
        delta("m1", "prt-ok", "OK"),
        tool("pending"),
        tool("running"),
        // The text part ends after its call began: the same words, whole.
        part(
            json!({"id": "prt-ok", "messageID": "m1", "type": "text", "text": "OK",
                    "time": {"start": 1, "end": 2}}),
        ),
        tool("completed"),
        part(
            json!({"id": "prt-f1", "messageID": "m1", "type": "step-finish",
                    "reason": "tool-calls"}),
        ),
        part(json!({"id": "prt-s2", "messageID": "m2", "type": "step-start"})),
        part(
            json!({"id": "prt-answer", "messageID": "m2", "type": "text", "text": "",
                    "time": {"start": 3}}),
        ),
        delta("m2", "prt-answer", "SKUNK"),
        part(
            json!({"id": "prt-answer", "messageID": "m2", "type": "text", "text": "SKUNK",
                    "time": {"start": 3, "end": 4}}),
        ),
        part(json!({"id": "prt-f2", "messageID": "m2", "type": "step-finish", "reason": "stop"})),
    ];
    let mut interpreter = by_id("opencode").unwrap().interpreter(1, "said-once");
    let mut signals = Vec::new();
    for line in lines {
        interpreter.on_record(
            &Record {
                t: 1,
                entry: Entry::Out(line),
            },
            &mut signals,
        );
    }
    interpreter.on_record(
        &Record {
            t: 2,
            entry: Entry::Shim(ShimEvent::Exited {
                code: Some(0),
                signal: None,
            }),
        },
        &mut signals,
    );
    let replies: Vec<(String, String, usize)> = signals
        .iter()
        .filter_map(|signal| match signal {
            Signal::Reply(reply) if !reply.text.is_empty() => {
                Some((reply.id.clone(), reply.text.clone(), reply.tools.len()))
            }
            _ => None,
        })
        .collect();
    let texts: Vec<(&str, usize)> = replies
        .iter()
        .map(|(_, text, tools)| (text.as_str(), *tools))
        .collect();
    assert_eq!(
        texts,
        [("OK", 1), ("SKUNK", 0)],
        "the words once, with their call, then the answer: {replies:?}"
    );
    // Shown as one message, the row the words are written into: never a second item of them.
    let shown: std::collections::BTreeSet<String> = signals
        .iter()
        .filter_map(|signal| match signal {
            Signal::Glimpse(Glimpse::Message { id, text, .. }) if text == "OK" => Some(id.clone()),
            _ => None,
        })
        .collect();
    assert_eq!(
        shown.into_iter().collect::<Vec<_>>(),
        [replies[0].0.clone()],
        "one live message for the words"
    );
}

/// Issue 784. A step's words are written when the step finishes, before anything of the next
/// step: a message the lane reads into the turn between two steps (its `steer.read`, at the next
/// answer's start) comes after the answer before it. Told whole again after that, the same words
/// are no second reply.
#[test]
fn a_steps_words_are_written_when_it_finishes_before_the_next_step() {
    let part = |part: Value| {
        json!({"type": "message.part.updated", "properties": {"sessionID": "s", "part": part}})
            .to_string()
    };
    let mut interpreter = by_id("opencode").unwrap().interpreter(1, "finished");
    let mut signals = Vec::new();
    let mut feed = |line: String, signals: &mut Vec<Signal>| {
        interpreter.on_record(
            &Record {
                t: 1,
                entry: Entry::Out(line),
            },
            signals,
        );
    };
    let replies = |signals: &[Signal]| -> Vec<String> {
        signals
            .iter()
            .filter_map(|signal| match signal {
                Signal::Reply(reply) if !reply.text.is_empty() => Some(reply.text.clone()),
                _ => None,
            })
            .collect()
    };
    feed(
        part(json!({"id": "s1", "messageID": "m1", "type": "step-start"})),
        &mut signals,
    );
    feed(
        part(json!({"id": "t1", "messageID": "m1", "type": "text", "text": "first answer"})),
        &mut signals,
    );
    assert!(replies(&signals).is_empty(), "still being written");
    feed(
        part(json!({"id": "f1", "messageID": "m1", "type": "step-finish", "reason": "stop"})),
        &mut signals,
    );
    assert_eq!(
        replies(&signals),
        ["first answer"],
        "written as its step finished"
    );
    // The next answer starts (the lane reads the steered words here), and the finished part is
    // told whole again: nothing new.
    feed(
        json!({"type": "message.updated", "properties": {"info": {"id": "m2", "sessionID": "s",
            "role": "assistant", "parentID": "u2"}}})
        .to_string(),
        &mut signals,
    );
    feed(
        r#"{"type":"steer.read","id":"u2"}"#.to_owned(),
        &mut signals,
    );
    feed(
        part(
            json!({"id": "t1", "messageID": "m1", "type": "text", "text": "first answer",
            "time": {"end": 2}}),
        ),
        &mut signals,
    );
    feed(
        part(json!({"id": "s2", "messageID": "m2", "type": "step-start"})),
        &mut signals,
    );
    feed(
        part(json!({"id": "t2", "messageID": "m2", "type": "text", "text": "second answer"})),
        &mut signals,
    );
    feed(
        part(json!({"id": "f2", "messageID": "m2", "type": "step-finish", "reason": "stop"})),
        &mut signals,
    );
    interpreter.on_record(
        &Record {
            t: 2,
            entry: Entry::Shim(ShimEvent::Exited {
                code: Some(0),
                signal: None,
            }),
        },
        &mut signals,
    );
    assert_eq!(replies(&signals), ["first answer", "second answer"]);
}

/// Issue 874. OpenCode compacts its context by itself before it answers: its summary for itself
/// comes as an assistant message of the compaction agent (`summary`, `mode` and `agent`
/// `compaction`). It is told as a compaction, as other tools' are, with what it cost, and never
/// as a reply; its model is not the agent's. The answer after it is the turn's one reply.
#[test]
fn opencodes_own_compaction_is_told_as_a_compaction_and_never_as_a_reply() {
    use clarp_kernel::live::Glimpse;
    use clarp_kernel::turn::Activity;
    let part = |part: Value| {
        json!({"type": "message.part.updated", "properties": {"sessionID": "s", "part": part}})
            .to_string()
    };
    let updated =
        |info: Value| json!({"type": "message.updated", "properties": {"info": info}}).to_string();
    let summary =
        "## Objective\nSummarize conversation history to enable a coding agent to continue";
    let lines = [
        // OpenCode's own user message of the compaction, with its compaction part.
        part(json!({"id": "p-c", "messageID": "u-c", "type": "compaction", "auto": true})),
        updated(
            json!({"id": "m-sum", "sessionID": "s", "role": "assistant", "parentID": "u-c",
            "mode": "compaction", "agent": "compaction", "summary": true,
            "providerID": "opencode", "modelID": "compactor"}),
        ),
        part(json!({"id": "s1", "messageID": "m-sum", "type": "step-start"})),
        part(json!({"id": "t1", "messageID": "m-sum", "type": "text", "text": ""})),
        json!({"type": "message.part.delta", "properties": {"sessionID": "s",
            "messageID": "m-sum", "partID": "t1", "field": "text", "delta": summary}})
        .to_string(),
        part(
            json!({"id": "t1", "messageID": "m-sum", "type": "text", "text": summary,
            "time": {"end": 2}}),
        ),
        part(
            json!({"id": "f1", "messageID": "m-sum", "type": "step-finish", "reason": "stop",
            "cost": 0.01, "tokens": {"input": 9000, "output": 400}}),
        ),
        updated(
            json!({"id": "m-ans", "sessionID": "s", "role": "assistant", "parentID": "u-go",
            "providerID": "opencode", "modelID": "big-pickle"}),
        ),
        part(json!({"id": "s2", "messageID": "m-ans", "type": "step-start"})),
        part(json!({"id": "t2", "messageID": "m-ans", "type": "text", "text": "The answer."})),
        part(
            json!({"id": "f2", "messageID": "m-ans", "type": "step-finish", "reason": "stop",
            "cost": 0.02, "tokens": {"input": 100, "output": 10}}),
        ),
    ];
    let mut interpreter = by_id("opencode").unwrap().interpreter(1, "compacted");
    let mut signals = Vec::new();
    for line in lines {
        interpreter.on_record(
            &Record {
                t: 1,
                entry: Entry::Out(line),
            },
            &mut signals,
        );
    }
    interpreter.on_record(
        &Record {
            t: 2,
            entry: Entry::Shim(ShimEvent::Exited {
                code: Some(0),
                signal: None,
            }),
        },
        &mut signals,
    );
    let replies: Vec<&str> = signals
        .iter()
        .filter_map(|signal| match signal {
            Signal::Reply(reply) if !reply.text.is_empty() => Some(reply.text.as_str()),
            _ => None,
        })
        .collect();
    assert_eq!(replies, ["The answer."]);
    let shown_summary = signals.iter().any(|signal| match signal {
        Signal::Live(live) => live.text.contains("Summarize"),
        Signal::Glimpse(Glimpse::Message { text, .. }) => text.contains("Summarize"),
        _ => false,
    });
    assert!(
        !shown_summary,
        "the summary is never shown as words of the turn"
    );
    let compacting: Vec<bool> = signals
        .iter()
        .filter_map(|signal| match signal {
            Signal::Glimpse(Glimpse::Compacting { prefix, over }) => {
                assert_eq!(prefix, "oc");
                Some(*over)
            }
            _ => None,
        })
        .collect();
    assert_eq!(
        compacting,
        [false, true],
        "told as a compaction, begun and over"
    );
    assert!(
        signals
            .iter()
            .any(|signal| matches!(signal, Signal::Moved(Activity::Compacting)))
    );
    let models: Vec<&str> = signals
        .iter()
        .filter_map(|signal| match signal {
            Signal::Model(model) => Some(model.as_str()),
            _ => None,
        })
        .collect();
    assert_eq!(models, ["opencode/big-pickle"]);
    let cost = signals.iter().find_map(|signal| match signal {
        Signal::TurnCost { micros } => Some(*micros),
        _ => None,
    });
    assert_eq!(cost, Some(30_000), "what the compaction cost counts");
    assert!(
        signals
            .iter()
            .any(|signal| matches!(signal, Signal::Ended(RunOutcome::Completed)))
    );
}
