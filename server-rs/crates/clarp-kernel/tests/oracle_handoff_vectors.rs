//! A call of the Oracle put through to an agent and back, against the Python Host's own.
//!
//! `fixtures/oracle-handoff-vectors.json` is written by
//! `server-rs/tools/oracle-handoff-vectors.py` from `server/lib/oracle_handoffs.py`, run as it
//! is with a clock, timers, names and calls of that program's own: stories of handoffs,
//! step by step, and for every step what it was answered or refused with, what the calls
//! and the apps were told and in which order, which deadlines stand, and what the step
//! changed of what the Host keeps for each phone. The same must come of the same here.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::collections::BTreeMap;

use clarp_kernel::oracle::handoffs::{self, Agent, Call, Desk, Effect, Refused, ToCall};
use clarp_kernel::python;
use serde_json::{Value, json};

fn vectors() -> Value {
    serde_json::from_str(include_str!("fixtures/oracle-handoff-vectors.json")).unwrap()
}

/// The agents a name is looked up among in the stories, as their generator has them: two
/// by their sessions, whatever the case and the blanks; and three names that mean nobody,
/// two agents, and something that went wrong otherwise.
fn agent_of(name: &str) -> Result<Agent, String> {
    let agent = |session: &str, persona: &str| Agent {
        session: session.to_string(),
        agent_id: format!("a-{session}"),
        persona: persona.to_string(),
    };
    match python::casefold(python::strip(name)).as_str() {
        "theo" => Ok(agent("theo", "Theo")),
        "mike" => Ok(agent("mike", "Mike")),
        _ => Err(match name {
            "nobody" => "unknown agent",
            "twins" => "ambiguous agent",
            _ => "something else",
        }
        .to_string()),
    }
}

/// What a call or the apps were told, as the file writes it: a call by the name its story
/// gives it.
fn written(effect: &Effect, names: &BTreeMap<String, String>) -> Value {
    match effect {
        Effect::Apps(event) => json!(["apps", event]),
        Effect::Focus(agent) => json!(["focus", agent]),
        Effect::Call(voice, what) => {
            let name = &names[voice];
            match what {
                ToCall::Session(frame) => json!([name, "downstream", frame]),
                ToCall::Offer(persona) => json!([name, "offer", persona]),
                ToCall::Mirror(wire) => json!([name, "mirror", wire]),
                ToCall::Hold => json!([name, "hold"]),
                ToCall::Resume => json!([name, "resume"]),
                ToCall::Failed {
                    persona,
                    reason,
                    note,
                } => json!([name, "failed", persona, reason, note]),
                ToCall::Returned(persona) => json!([name, "returned", persona]),
            }
        }
    }
}

/// What the Host keeps for a phone, as the file has it after the steps so far.
#[derive(Default)]
struct Kept {
    generation: i64,
    names: Vec<String>,
    records: BTreeMap<String, Value>,
}

#[test]
fn a_handoff_goes_as_the_python_hosts_goes_step_by_step() {
    let vectors = vectors();
    for state in ["offered", "preparing", "activating", "active", "failed"] {
        let waits = vectors["ttl_ms"][state].as_i64();
        assert_eq!(handoffs::waits_ms(state), waits, "{state}");
    }
    assert_eq!(vectors["keep"], handoffs::KEPT);
    assert_eq!(vectors["connect_waits_ms"], handoffs::CONNECT_WAITS_MS);
    let text = |value: &Value| value.as_str().unwrap().to_string();
    let (mut steps, mut refused, mut told, mut fired) = (0, 0, 0, 0);
    let mut idle = 0;
    for story in vectors["stories"].as_array().unwrap() {
        let name = story["name"].as_str().unwrap();
        let mut desk = Desk {
            host_id: text(&vectors["host_id"]),
            ..Desk::default()
        };
        // The calls of the story: each call's own name by the name the story gives it, and
        // the other way round.
        let mut voices: BTreeMap<String, (String, String)> = BTreeMap::new();
        let mut names: BTreeMap<String, String> = BTreeMap::new();
        let mut made = 0;
        let mut kept: BTreeMap<String, Kept> = BTreeMap::new();
        for (number, step) in story["steps"].as_array().unwrap().iter().enumerate() {
            let said = &step["step"];
            let at = step["at"].as_i64().unwrap();
            let what = format!("{name}, step {number}: {said}");
            // A deadline acts only on the handoff as it stood when the deadline was set,
            // and only where the handoff is in a state that has a time: anything else that
            // is called a deadline changes nothing and tells nobody.
            for (principal, book) in desk.books.clone() {
                for record in &book.records {
                    let waits = handoffs::waits_ms(&record.state).is_some();
                    for revision in [record.revision - 1, record.revision, record.revision + 1] {
                        if waits && revision == record.revision {
                            continue;
                        }
                        let mut as_it_is = desk.clone();
                        let told = as_it_is.expire(&principal, &record.handoff_id, revision, at);
                        assert!(told.is_empty() && as_it_is == desk, "{what}: {told:?}");
                        idle += 1;
                    }
                }
            }
            let mut happened: Vec<Value> = Vec::new();
            let mut tell = |told: Vec<Effect>, names: &BTreeMap<String, String>| {
                happened.extend(told.iter().map(|effect| written(effect, names)));
            };
            let answered: Result<Value, Refused> = match said[0].as_str().unwrap() {
                "register" => {
                    let call = Call {
                        principal: text(&said[1]["principal"]),
                        voice_session_id: text(&said[1]["voice_session_id"]),
                        provider_session: text(&said[1]["provider_session"]),
                        thread_id: text(&said[1]["thread_id"]),
                        capable: said[1]["handoff_capable"].as_bool().unwrap(),
                        handoff_id: text(&said[1]["handoff_id"]),
                        held: false,
                    };
                    let known = (call.principal.clone(), call.voice_session_id.clone());
                    names.insert(call.voice_session_id.clone(), text(&said[1]["name"]));
                    voices.insert(text(&said[1]["name"]), known);
                    tell(desk.register(call, at), &names);
                    Ok(Value::Null)
                }
                "unregister" => {
                    let (principal, voice) = &voices[said[1].as_str().unwrap()];
                    desk.unregister(principal, voice);
                    Ok(Value::Null)
                }
                "connect" => {
                    let who = (said[1].as_str().unwrap(), said[3].as_str());
                    let id = format!("hof_{:032x}", made + 1);
                    let (offered, effects) = desk.offer(who, &said[2], agent_of, &id, at);
                    tell(effects, &names);
                    offered.and_then(|(who, id)| {
                        made += 1;
                        desk.outcome(&who, &id, false, at).unwrap()
                    })
                }
                "ack" => {
                    let (answer, effects) = desk.ack(said[1].as_str().unwrap(), &said[2], at);
                    tell(effects, &names);
                    answer
                }
                "snapshot" => Ok(desk.snapshot(said[1].as_str().unwrap(), at)),
                "outcome" => {
                    let id = said[1].as_str().unwrap();
                    let has = |book: &handoffs::Book| {
                        let named = |record: &handoffs::Record| record.handoff_id == id;
                        book.records.iter().any(named)
                    };
                    let whose = desk.books.iter().find(|(_, book)| has(book));
                    let whose = whose.map(|(principal, _)| principal.clone()).unwrap();
                    let waited = said[2].as_bool().unwrap();
                    desk.outcome(&whose, id, waited, at).unwrap()
                }
                "pass" => {
                    // Every deadline that is reached is acted on at its own moment, the
                    // earliest first.
                    let until = at + said[1].as_i64().unwrap();
                    while let Some((principal, id, deadline, revision)) = desk
                        .deadlines()
                        .into_iter()
                        .find(|(_, _, deadline, _)| *deadline <= until)
                    {
                        happened.push(json!(["deadline", id, revision]));
                        let effects = desk.expire(&principal, &id, revision, deadline);
                        happened.extend(effects.iter().map(|effect| written(effect, &names)));
                        fired += 1;
                    }
                    Ok(Value::Null)
                }
                "restart" => {
                    desk.recovered(at);
                    voices.clear();
                    Ok(Value::Null)
                }
                other => panic!("{what}: no such step as {other}"),
            };
            steps += 1;
            match (&answered, step.get("refused")) {
                (Ok(answer), None) => assert_eq!(answer, &step["answered"], "{what}"),
                (Err(refusal), Some(expected)) => {
                    assert_eq!(json!([refusal.status, refusal.body()]), *expected, "{what}");
                    refused += 1;
                }
                (got, expected) => panic!("{what}: {got:?}, and the Python Host: {expected:?}"),
            }
            assert_eq!(json!(happened), step["happened"], "{what}");
            told += happened.len();

            let mut stand: Vec<Value> = desk
                .deadlines()
                .iter()
                .map(|(_, id, deadline, revision)| json!([id, deadline, revision]))
                .collect();
            stand.sort_by_key(|deadline| deadline[0].as_str().map(str::to_string));
            assert_eq!(json!(stand), step["deadlines"], "{what}");

            // What the step changed of what is kept, and so all that is kept.
            for (principal, change) in step["kept"].as_object().unwrap() {
                let book = kept.entry(principal.clone()).or_default();
                book.generation = change["generation"].as_i64().unwrap();
                book.names = change["names"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(text)
                    .collect();
                for record in change["records"].as_array().unwrap() {
                    book.records
                        .insert(text(&record["handoff_id"]), record.clone());
                }
            }
            let phones: Vec<&String> = desk.books.keys().collect();
            assert_eq!(phones, kept.keys().collect::<Vec<_>>(), "{what}");
            for (principal, expected) in &kept {
                let book = &desk.books[principal];
                assert_eq!(book.generation, expected.generation, "{what}");
                let has: Vec<&String> = book.records.iter().map(|one| &one.handoff_id).collect();
                assert_eq!(has, expected.names.iter().collect::<Vec<_>>(), "{what}");
                for record in &book.records {
                    let as_kept = serde_json::to_value(record).unwrap();
                    assert_eq!(as_kept, expected.records[&record.handoff_id], "{what}");
                }
            }
        }
    }
    assert_eq!((steps, refused, told, fired), (568, 71, 923, 15));
    assert!(idle > 5_000, "{idle}");
}
