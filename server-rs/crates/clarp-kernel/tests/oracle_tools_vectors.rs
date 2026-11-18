//! The tools of the fleet as the Oracle's router uses them, against the Python Host's own.
//!
//! `fixtures/oracle-tools-vectors.json` is written by `server-rs/tools/oracle-tools-vectors.py`
//! from `server/lib/oracle_calls_stable.py`, with the agents, their messages and their states
//! made up there and a stand-in for the book of delegations: what each thing a router may
//! want means, what it is answered, and how what an agent said is read within its bounds.
//! The same must come of the same here, also the order an answer's fields are written in:
//! an answer is told to a voice as it is written.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_kernel::oracle::tools::{self, Agent, Message, Plan, State};
use clarp_kernel::python;
use jiff::Timestamp;
use serde_json::{Value, json};

/// The moment every age is told from.
const NOW: i64 = 1_800_000_000_000;

fn vectors() -> Value {
    serde_json::from_str(include_str!("fixtures/oracle-tools-vectors.json")).unwrap()
}

/// What the file has written short is written out again: one sign many times over.
fn whole(value: &Value) -> Value {
    match value {
        Value::Object(fields) if fields.len() == 1 && fields.contains_key("runs") => {
            let mut text = String::new();
            for part in fields["runs"].as_array().unwrap() {
                match part {
                    Value::String(as_it_is) => text += as_it_is,
                    run => {
                        let times = usize::try_from(run[1].as_u64().unwrap()).unwrap();
                        text += &run[0].as_str().unwrap().repeat(times);
                    }
                }
            }
            json!(text)
        }
        Value::Object(fields) => {
            let fields = fields
                .iter()
                .map(|(name, value)| (name.clone(), whole(value)));
            Value::Object(fields.collect())
        }
        Value::Array(items) => Value::Array(items.iter().map(whole).collect()),
        plain => plain.clone(),
    }
}

/// A message of the file, with the moment it was said.
struct Said {
    id: String,
    role: String,
    text: String,
    timestamp: String,
    at_ms: i64,
}

fn said(messages: &Value) -> Vec<Said> {
    let rows = messages.as_array().unwrap().iter();
    rows.map(|row| {
        let at_ms = NOW - 1000 * row["seconds_ago"].as_i64().unwrap();
        Said {
            id: row["id"].as_str().unwrap().to_string(),
            role: row["role"].as_str().unwrap().to_string(),
            text: row["text"].as_str().unwrap().to_string(),
            timestamp: Timestamp::from_millisecond(at_ms).unwrap().to_string(),
            at_ms,
        }
    })
    .collect()
}

/// The newest `most` messages, the oldest first: what a Host reads for a tool.
fn newest(rows: &[Said], most: usize) -> Vec<Message<'_>> {
    let from = rows.len().saturating_sub(most);
    rows[from..]
        .iter()
        .map(|row| Message {
            id: &row.id,
            role: &row.role,
            text: &row.text,
            timestamp: &row.timestamp,
            at_ms: row.at_ms,
        })
        .collect()
}

/// An answer with the moments of its messages as how long before now they are, as the file
/// writes them, and its ages as the file writes them where it cannot know them.
fn as_written(mut answer: Value, ages_unknown: bool) -> Value {
    if let Some(messages) = answer.get_mut("messages").and_then(Value::as_array_mut) {
        for message in messages {
            let fields = message.as_object_mut().unwrap();
            let stamp = fields.shift_remove("timestamp").unwrap();
            let at: Timestamp = stamp.as_str().unwrap().parse().unwrap();
            fields.insert(
                "seconds_ago".to_string(),
                json!((NOW - at.as_millisecond()) / 1000),
            );
        }
    }
    for age in ["newest_message_age", "since", "last_said_age"] {
        if ages_unknown && answer.get(age).is_some() {
            answer[age] = json!("<an age>");
        }
    }
    answer
}

/// Two answers are the same, to the order of their fields.
fn same(ours: &Value, theirs: &Value, of: &str) {
    assert_eq!(python::dumps(ours), python::dumps(theirs), "{of}");
}

fn agents(vectors: &Value) -> Vec<Agent<'_>> {
    let rows = vectors["agents"].as_array().unwrap().iter();
    rows.map(|row| Agent {
        agent_id: row["agent_id"].as_str().unwrap(),
        persona: row["persona"].as_str().unwrap(),
        session: row["session"].as_str().unwrap(),
        backend: row["backend"].as_str().unwrap(),
        personality: row["personality"].as_str().unwrap_or(""),
        archived: python::truthy(&row["archived_at"]),
        janitor: python::truthy(&row["is_janitor"]),
    })
    .collect()
}

#[test]
fn what_an_agent_said_is_read_within_its_bounds_as_the_python_host_reads_it() {
    let vectors = whole(&vectors());
    let agents = agents(&vectors);
    let theo = &agents[0];
    for case in vectors["ages"].as_array().unwrap() {
        if case.get("age").is_none() {
            continue;
        }
        let then = case["seconds_ago"].as_i64().map(|ago| NOW - 1000 * ago);
        assert_eq!(json!(tools::age_text(then, NOW)), case["age"], "{case}");
    }
    let (mut cut, mut marked) = (0, 0);
    for case in vectors["readings"].as_array().unwrap() {
        let rows = said(&vectors["messages"][case["messages"].as_str().unwrap()]);
        let number = |name: &str| usize::try_from(case[name].as_u64().unwrap()).unwrap();
        // A Host reads twice as many messages as are wanted: some of them say nothing.
        assert_eq!(case["asked"][0]["limit"], json!(2 * number("limit")));
        assert_eq!(case["asked"][0]["include_automated"], false);
        let rows = newest(&rows, 2 * number("limit"));
        let bounds = (number("limit"), number("message_chars"), number("budget"));
        let (read, was_cut) = tools::read(&rows, bounds, case["mark_cuts"] == true);
        let read = as_written(json!({ "messages": read }), false);
        same(
            &read["messages"],
            &case["read"],
            &case["messages"].to_string(),
        );
        assert_eq!(json!(was_cut), case["cut"], "{case}");
        cut += usize::from(was_cut);
        marked += usize::from(python::dumps(&read).contains("\"truncated\""));
    }
    assert_eq!(
        (vectors["readings"].as_array().unwrap().len(), cut, marked),
        (64, 26, 13)
    );
    let (mut usual, mut most) = (0, 0);
    for case in vectors["transcripts"].as_array().unwrap() {
        let rows = said(&vectors["messages"][case["messages"].as_str().unwrap()]);
        let wanted = (
            "read_agent_transcript",
            &json!({ "agent": "theo", "limit": case["limit"] }),
            "c-1",
        );
        let Ok(Plan::Transcript(agent, limit)) = tools::plan(wanted, "a-phone", &agents, "theo")
        else {
            panic!("a conversation is read: {case}");
        };
        assert_eq!(agent, theo);
        usual += usize::from(limit == tools::TRANSCRIPT_USUAL);
        most += usize::from(limit == tools::TRANSCRIPT_MOST);
        let rows = newest(&rows, 2 * limit);
        let mut answer = tools::transcript(agent, &rows, limit, NOW);
        let age = answer
            .as_object_mut()
            .unwrap()
            .shift_remove("newest_message_age");
        assert_eq!(age.unwrap(), case["newest_message_age"], "{case}");
        same(
            &as_written(answer, false),
            &case["output"],
            &case["limit"].to_string(),
        );
    }
    assert_eq!(
        (
            vectors["transcripts"].as_array().unwrap().len(),
            usual,
            most
        ),
        (88, 48, 24)
    );
    for case in vectors["statuses"].as_array().unwrap() {
        let rows = said(&vectors["messages"][case["messages"].as_str().unwrap()]);
        let state = &vectors["states"][case["state"].as_str().unwrap()];
        let latest = (!state.is_null()).then(|| State {
            kind: state["kind"].as_str().unwrap(),
            detail: &state["detail"],
            at_ms: state["seconds_ago"].as_i64().map(|ago| NOW - 1000 * ago),
        });
        assert_eq!(case["asked"][0]["limit"], json!(tools::STATUS_MESSAGES));
        let rows = newest(&rows, tools::STATUS_MESSAGES);
        same(
            &tools::status(theo, latest, &rows, NOW),
            &case["output"],
            &case.to_string(),
        );
    }
    assert_eq!(vectors["statuses"].as_array().unwrap().len(), 48);
}

#[test]
fn what_a_router_wants_done_means_what_it_means_on_the_python_host() {
    let vectors = whole(&vectors());
    let agents = agents(&vectors);
    let phone = vectors["phone"].as_str().unwrap();
    let state = &vectors["states"]["thinking"];
    let latest = Some(State {
        kind: state["kind"].as_str().unwrap(),
        detail: &state["detail"],
        at_ms: state["seconds_ago"].as_i64().map(|ago| NOW - 1000 * ago),
    });
    let (mut refused, mut handed, mut stops, mut answered, mut read) = (0, 0, 0, 0, 0);
    for case in vectors["calls"].as_array().unwrap() {
        let of = format!(
            "{} {} for a contact {}",
            case["name"], case["arguments"], case["fallback"]
        );
        let wanted = (
            case["name"].as_str().unwrap(),
            &case["arguments"],
            case["call_id"].as_str().unwrap(),
        );
        let contact = case["fallback"].as_str().unwrap();
        // What the agent has said, when this is wanted.
        let rows = said(&vectors["messages"][case["messages"].as_str().unwrap()]);
        let dispatch = |operation: &str, session: &str, request: &str| {
            json!({ "dispatch": {
                "delegation_id": operation, "session": session, "request_text": request,
                "authenticated_at_admission": true, "owner_principal": phone,
            } })
        };
        // The stand-in of the file says that work waits where its request has this word.
        let stands = |request: &str| {
            if request.contains("instead") {
                "queued"
            } else {
                "accepted"
            }
        };
        let (output, asked, owns) = match tools::plan(wanted, phone, &agents, contact) {
            Err(why) => {
                assert_eq!(json!(why), case["raises"], "{of}");
                refused += 1;
                (Value::Null, json!([]), json!([]))
            }
            Ok(Plan::Answer(answer)) => {
                answered += 1;
                (answer, json!([]), json!([]))
            }
            Ok(Plan::Status(agent)) => {
                read += 1;
                let rows = newest(&rows, tools::STATUS_MESSAGES);
                let answer = tools::status(agent, latest, &rows, NOW);
                (as_written(answer, true), json!([]), json!([]))
            }
            Ok(Plan::Messages(agent)) => {
                read += 1;
                let rows = newest(&rows, 2 * tools::MESSAGES_READ);
                let answer = tools::messages(agent, &rows, NOW);
                (as_written(answer, true), json!([]), json!([]))
            }
            Ok(Plan::Transcript(agent, limit)) => {
                read += 1;
                let rows = newest(&rows, 2 * limit);
                let answer = tools::transcript(agent, &rows, limit, NOW);
                (as_written(answer, true), json!([]), json!([]))
            }
            Ok(Plan::Stop { agent, follow }) => {
                stops += 1;
                let mut asked = vec![
                    json!({ "stop": agent.session }),
                    json!({
                        "cancel_for_session": agent.session, "owner_principal": phone,
                        "stopped": "stopped",
                    }),
                    json!({ "supersede": agent.session }),
                ];
                match follow {
                    None => (tools::stopped(), json!(asked), json!([])),
                    Some((operation, request)) => {
                        asked.push(json!({ "set_paused": agent.agent_id, "paused": false }));
                        asked.push(dispatch(&operation, agent.session, &request));
                        let answer = tools::handed(stands(&request), &operation, agent.session);
                        (answer, json!(asked), json!([operation]))
                    }
                }
            }
            Ok(Plan::Hand {
                agent,
                operation_id,
                request,
            }) => {
                handed += 1;
                let asked = json!([dispatch(&operation_id, agent.session, &request)]);
                let answer = tools::handed(stands(&request), &operation_id, agent.session);
                (answer, asked, json!([operation_id]))
            }
        };
        if case.get("raises").is_none() {
            same(&output, &case["output"], &of);
        } else {
            assert!(output.is_null(), "{of}: refused there, and here {output}");
        }
        assert_eq!(asked, case["asked"], "{of}");
        assert_eq!(owns, case["owns"], "{of}");
    }
    assert_eq!(
        (
            vectors["calls"].as_array().unwrap().len(),
            refused,
            handed,
            stops,
            answered,
            read
        ),
        (132, 59, 24, 12, 16, 21)
    );
}
