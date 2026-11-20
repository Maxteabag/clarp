//! An agent's note to the user (`POST /agent-notes`), held to the Python Host's (`tests/fixtures/agent-note-vectors.json`,
//! written by `server-rs/tools/agent-note-vectors.py` from its `lib/agent_notes.py`, `routes/agents.py`,
//! `message_writes.record_note` and `user_notifications.classify_note`, its PR 482). Each story is staged here as the
//! generator staged it, the Host's part (the agents it has, the turn a token names, the notes an agent already has,
//! the milliseconds its notifications are filed under) kept by the test, and every step asked of the kernel in the
//! reference's order: the body, the agent, the turn, the id asked again, the conversation. What is held: the answer
//! whole, what the apps are told of the notification, what is pushed, and the notes the agent's chat then has.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::collections::{BTreeMap, HashSet};

use clarp_kernel::coordination::{
    self, AGAIN, COMMAND, Caller, MAX_TEXT, NOTE_KIND, NOTE_ORIGIN, Refusal,
};
use clarp_kernel::notify::{Capabilities, Decision};
use regex::Regex;
use serde_json::{Value, json};

/// The trace of every turn the generator opens for a story's agent.
const TRACE: &str = "trace-a";

/// A text the generator wrote short: a run of hundreds of one letter as `<w x 8000>`.
fn whole(text: &str) -> String {
    let run = Regex::new(r"<(.) x (\d+)>").unwrap();
    run.replace_all(text, |found: &regex::Captures<'_>| {
        found[1].repeat(found[2].parse().unwrap())
    })
    .into_owned()
}

fn body_of(step: &Value) -> Value {
    let mut body = step["body"].clone();
    if let Some(text) = body.get("text").and_then(Value::as_str) {
        body["text"] = json!(whole(text));
    }
    body
}

/// An agent of a story: the Host's part of it.
#[derive(Default)]
struct Agent {
    /// Its notes by their own ids, in the order they were written: (id, words).
    notes: Vec<(String, String)>,
    /// The milliseconds its notifications are filed under.
    taken: HashSet<i64>,
    muted: bool,
    conversation: bool,
}

/// The number the generator gave an agent: its session's (`ivy-4`) and its id's (`agent-4`) alike, both counted
/// at every agent it made.
fn number(session: &str) -> Option<u32> {
    let (name, number) = session.rsplit_once('-')?;
    matches!(name, "ivy" | "oak")
        .then(|| number.parse().ok())
        .flatten()
}

/// The turn a story's requests present, as the test keeps it.
enum Token {
    Nobody,
    Own,
    /// Another agent's turn: its session, or none for an agent the Host no longer has.
    Other(Option<String>),
}

/// Where a story's requests stand to a turn: the turn their token names, and whether it runs.
fn standing(story: &str, session: &str) -> (Token, bool) {
    match story {
        "from no token" | "from a token that is none" | "what is asked, without a token" => {
            (Token::Nobody, true)
        }
        // The other agent is the one the generator made right after this story's own.
        "from another agent's token" => {
            let other = format!("oak-{}", number(session).unwrap() + 1);
            (Token::Other(Some(other)), true)
        }
        "from a removed agent's token" => (Token::Other(None), true),
        "from the turn ended"
        | "from an older turn"
        | "from the turn completed normally"
        | "from the turn is parked for its account" => (Token::Own, false),
        "from the running turn"
        | "what is asked"
        | "a note again"
        | "a note of a muted agent"
        | "a note of a helper"
        | "a note of an agent with no conversation" => (Token::Own, true),
        other => panic!("a story the test does not know: {other}"),
    }
}

/// What the Host answers a step, and what it tells and pushes, as the kernel decides it.
struct Answered {
    answer: Value,
    told: Vec<Value>,
    pushed: usize,
}

fn refusal(refused: Refusal) -> Answered {
    Answered {
        answer: json!({"status": refused.status, "body": {"error": refused.words}}),
        told: vec![],
        pushed: 0,
    }
}

fn ask(
    story: &str,
    step: &Value,
    agents: &mut BTreeMap<String, Agent>,
) -> Result<Answered, Refusal> {
    let body = body_of(step);
    let at = step["at"].as_i64().unwrap();
    let asked = coordination::asked(&body)?;
    let session = asked.session.clone();
    let Some(n) = number(&session) else {
        return Err(coordination::no_such_agent());
    };
    let (token, running) = standing(story, &session);
    let caller = match &token {
        Token::Nobody => Caller::Nobody,
        Token::Own => Caller::Own,
        Token::Other(session) => Caller::Other(session.as_deref()),
    };
    coordination::may_note(&session, caller)?;
    if !running {
        return Err(coordination::not_running(&session));
    }
    let agent_id = format!("agent-{n}");
    // A note that names no id gets one the Host mints: the generator's are its own counter's, taken from its
    // answer where it wrote the note (this Host mints sixteen hex signs of its own), and one no note has where
    // it refused it.
    let note_id = asked.note_id.clone().unwrap_or_else(|| {
        step["answer"]["body"]["message_id"]
            .as_str()
            .map_or("minted-by-the-test", |id| {
                id.strip_prefix(&format!("note-{agent_id}-")).unwrap()
            })
            .to_string()
    });
    let agent = agents.entry(session.clone()).or_insert_with(|| Agent {
        muted: matches!(story, "a note of a muted agent" | "a note of a helper"),
        conversation: story != "a note of an agent with no conversation",
        ..Agent::default()
    });
    let held = agent
        .notes
        .iter()
        .find(|(id, _)| *id == note_id)
        .map(|(_, words)| words.as_str());
    let again = coordination::again(&session, &note_id, held, &asked.text)?;
    if !agent.conversation {
        return Err(coordination::no_conversation(&session));
    }
    let message_id = coordination::note_message_id(&agent_id, &note_id);
    let mut told = vec![];
    if again {
        let answer = json!({"status": 200, "body": {
            "ok": true, "session": session, "message_id": message_id, "trace_id": TRACE,
            "created": false, "notified": false, "reason": AGAIN}});
        return Ok(Answered {
            answer,
            told,
            pushed: 0,
        });
    }
    agent.notes.push((note_id, asked.text.clone()));
    told.push(json!({"type": "transcript-updated", "agent_id": agent_id, "session": session}));
    let decision = coordination::note_decision(&Capabilities {
        muted: agent.muted,
        ..Capabilities::default()
    });
    let Decision::Notify {
        reason,
        push,
        muted,
        ..
    } = decision
    else {
        panic!("a note of these agents always notifies");
    };
    let done_ts = coordination::free_ms(at, |ms| agent.taken.contains(&ms));
    agent.taken.insert(done_ts);
    told.push(json!({
        "type": "user-notification",
        "notification_id": coordination::note_notification_id(&message_id),
        "agent_id": agent_id, "session": session, "done_ts": done_ts,
        "source_message_id": message_id, "cause_message_id": "", "origin": NOTE_ORIGIN,
        "push": push, "badge": true, "unread": true, "muted": muted, "reason": reason,
    }));
    let answer = json!({"status": 200, "body": {
        "ok": true, "session": session, "message_id": message_id, "trace_id": TRACE,
        "created": true, "notified": true, "reason": reason}});
    Ok(Answered {
        answer,
        told,
        pushed: 1,
    })
}

#[test]
fn a_note_is_refused_accepted_and_notified_as_the_python_host_does() {
    let vectors: Value =
        serde_json::from_str(include_str!("fixtures/agent-note-vectors.json")).unwrap();
    assert_eq!(vectors["command"], COMMAND);
    assert_eq!(vectors["max_text"], json!(MAX_TEXT));
    let stories = vectors["stories"].as_array().unwrap();
    let (mut steps, mut refused, mut written, mut again) = (0, 0, 0, 0);
    // The Host's agents, across the stories: two stories ask of the same agent.
    let mut agents = BTreeMap::new();
    for story in stories {
        let name = story["name"].as_str().unwrap();
        // The story's own agent, whose chat the generator writes down at every step of it.
        let own = story["steps"]
            .as_array()
            .unwrap()
            .iter()
            .filter_map(|step| step["body"]["session"].as_str())
            .find(|session| number(session).is_some())
            .unwrap_or_default()
            .to_string();
        for step in story["steps"].as_array().unwrap() {
            let what = format!("{name}: {}", step["step"].as_str().unwrap());
            let ours = ask(name, step, &mut agents).unwrap_or_else(refusal);
            assert_eq!(ours.answer, step["answer"], "{what}");
            // What the apps are told: every field the kernel decides, as the reference tells it.
            let told = step["told"].as_array().unwrap();
            assert_eq!(told.len(), ours.told.len(), "{what}");
            for (theirs, ours) in told.iter().zip(&ours.told) {
                for (field, value) in ours.as_object().unwrap() {
                    assert_eq!(&theirs[field], value, "{what}: {field}");
                }
            }
            let pushed = step["pushed"].as_array().unwrap();
            assert_eq!(pushed.len(), ours.pushed, "{what}");
            for (pushed, told) in pushed.iter().zip(told.iter().skip(1)) {
                assert_eq!(pushed["notification_id"], told["notification_id"], "{what}");
                assert_eq!(pushed["origin"], NOTE_ORIGIN, "{what}");
            }
            // The agent's chat: its notes, in the order they were written, always the user's.
            let notes = step["notes"].as_array().unwrap();
            let named = body_of(step)["session"]
                .as_str()
                .map(str::trim)
                .map(str::to_string);
            let session = named
                .filter(|named| number(named).is_some())
                .unwrap_or(own.clone());
            let kept: Vec<(String, String)> = agents
                .get(&session)
                .map(|agent: &Agent| agent.notes.clone())
                .unwrap_or_default();
            if !notes.is_empty() || !kept.is_empty() {
                let agent_id = format!("agent-{}", number(&session).unwrap());
                let ours: Vec<Value> = kept
                    .iter()
                    .map(|(id, words)| json!([coordination::note_message_id(&agent_id, id), words]))
                    .collect();
                let theirs: Vec<Value> = notes
                    .iter()
                    .map(|row| json!([row["id"], whole(row["text"].as_str().unwrap())]))
                    .collect();
                assert_eq!(ours, theirs, "{what}");
                for row in notes {
                    assert_eq!(row["kind"], NOTE_KIND, "{what}");
                    assert_eq!(row["origin"], NOTE_ORIGIN, "{what}");
                    assert_eq!(row["trace_id"], TRACE, "{what}");
                    assert_eq!(row["sender_agent_id"], "", "{what}");
                    let audience =
                        coordination::row_audience(Some(NOTE_KIND), Some(coordination::AGENTS));
                    assert_eq!(row["audience"], audience, "{what}");
                }
            }
            steps += 1;
            match ours.answer["body"]["created"].as_bool() {
                None => refused += 1,
                Some(true) => written += 1,
                Some(false) => again += 1,
            }
        }
    }
    assert_eq!(
        (stories.len(), steps, refused, written, again),
        (15, 59, 44, 13, 2)
    );
}

#[test]
fn a_notes_id_is_of_its_row_and_its_body_is_read_as_the_python_host_reads_it() {
    // The id of a note's notification is of its row alone: sha256 of "note", a zero and the row's id.
    assert_eq!(
        coordination::note_notification_id("note-agent-1-0000000000000001"),
        "pn-d964a880cd12a5884be5"
    );
    // What is not text is read as Python's str() writes it; nothing, false, zero and empty are no words.
    let read = |text: Value| coordination::asked(&json!({"session": "s", "text": text}));
    assert_eq!(read(json!(true)).unwrap().text, "True");
    assert_eq!(read(json!(4.5)).unwrap().text, "4.5");
    assert_eq!(read(json!(["a", 1])).unwrap().text, "['a', 1]");
    assert_eq!(read(json!({"a": null})).unwrap().text, "{'a': None}");
    for nothing in [
        json!(false),
        json!(0),
        json!([]),
        json!({}),
        json!(null),
        json!(""),
    ] {
        assert_eq!(read(nothing).unwrap_err().status, 400);
    }
    // The bounds counted in signs, not bytes, from both sides.
    let most = "\u{e5}".repeat(MAX_TEXT);
    assert!(read(json!(most)).is_ok());
    assert_eq!(read(json!(format!("{most}a"))).unwrap_err().status, 413);
    let id = |note_id: &str| {
        coordination::asked(&json!({"session": "s", "text": "t", "note_id": note_id}))
            .map(|asked| asked.note_id)
    };
    assert_eq!(id(&"a".repeat(64)), Ok(Some("a".repeat(64))));
    assert_eq!(id(&"a".repeat(65)).unwrap_err().status, 400);
    assert_eq!(id(" "), Ok(None));
    assert_eq!(id("\u{212a}"), Ok(Some("k".to_string())));
    assert_eq!(id("\u{e5}").unwrap_err().status, 400);
}
