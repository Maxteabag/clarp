//! Calls, against the Python Host's own module.
//!
//! `fixtures/calls-vectors.json` is written by `server-rs/tools/calls-vectors.py`: what the
//! Python Host's call module answered, announced and noted, step by step, in scripts of
//! requests from two phones and the administrator, and whom it takes a typed or spoken name
//! to mean. The same must come of the same here.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::collections::BTreeMap;

use clarp_kernel::calls::{Asker, At, Calls, Contact, Effects, KEPT_ENDED, MOST, STALE_MS, named};
use serde_json::{Value, json};

fn vectors() -> Value {
    serde_json::from_str(include_str!("fixtures/calls-vectors.json")).unwrap()
}

/// The agents that may be asked into a call: the Host's, without its janitors.
fn roster(vectors: &Value) -> Vec<Contact> {
    let said = |agent: &Value, key: &str| agent[key].as_str().unwrap_or("").to_string();
    let agents = vectors["roster"].as_array().unwrap().iter();
    agents
        .filter(|agent| agent.get("is_janitor").is_none())
        .map(|agent| Contact {
            session: said(agent, "session"),
            agent_id: said(agent, "agent_id"),
            persona: said(agent, "persona"),
            helper: agent["role"] == "helper" || agent.get("parent_agent_id").is_some(),
        })
        .collect()
}

#[test]
fn a_name_means_the_agent_the_python_host_takes_it_to_mean() {
    let vectors = vectors();
    let roster = roster(&vectors);
    assert_eq!(
        (vectors["most"].as_u64(), vectors["kept_ended"].as_u64()),
        (Some(MOST as u64), Some(KEPT_ENDED as u64))
    );
    assert_eq!(vectors["stale_ms"], STALE_MS);
    let rows = vectors["names"].as_array().unwrap();
    let (mut one, mut two, mut nobody) = (0, 0, 0);
    for row in rows {
        let name = row["name"].as_str().unwrap();
        let in_call: Vec<&str> = row["in_call"]
            .as_array()
            .unwrap()
            .iter()
            .map(|session| session.as_str().unwrap())
            .collect();
        match named(name, &roster, &in_call) {
            Ok(agent) => {
                one += 1;
                assert_eq!(
                    json!(agent.session),
                    row["session"],
                    "{name:?} among {in_call:?}"
                );
            }
            Err(refused) => {
                two += usize::from(refused.status == 409);
                nobody += usize::from(refused.status == 404);
                assert_eq!(
                    (json!(refused.status), &refused.body),
                    (row["status"].clone(), &row["refused"]),
                    "{name:?} among {in_call:?}"
                );
            }
        }
    }
    assert!(
        rows.len() >= 65 && one >= 45 && two >= 5 && nobody >= 10,
        "{one}, {two}, {nobody}"
    );
}

/// What a script's steps are replayed with.
struct Room {
    calls: Calls,
    /// How many calls were made: the next one's id.
    made: u64,
    /// The first call of the script, and each phone's newest live one.
    first: String,
    last: BTreeMap<String, String>,
}

impl Room {
    fn call_of(&self, step: &Value, caller: &str) -> String {
        match step["call"].as_str().unwrap_or("") {
            "live" => self.last.get(caller).cloned().unwrap_or_default(),
            "live-b" => self.last.get("device_b").cloned().unwrap_or_default(),
            "first" => self.first.clone(),
            named => named.to_string(),
        }
    }
}

#[test]
fn a_call_goes_the_way_it_goes_on_the_python_host_step_by_step() {
    let vectors = vectors();
    let roster = roster(&vectors);
    let scripts = vectors["scripts"].as_array().unwrap();
    let (mut steps, mut refused_steps, mut told) = (0, 0, 0);
    for script in scripts {
        let name = script["name"].as_str().unwrap();
        let mut room = Room {
            calls: Calls::default(),
            made: 0,
            first: String::new(),
            last: BTreeMap::new(),
        };
        for (place, row) in script["steps"].as_array().unwrap().iter().enumerate() {
            let step = &row["step"];
            let said = |key: &str| step[key].as_str().unwrap_or("").to_string();
            let at = At {
                now_ms: row["now"].as_i64().unwrap(),
                host_id: "host-1",
            };
            let (caller, call_id, principal, by) = (
                said("caller"),
                room.call_of(step, &said("caller")),
                said("principal"),
                said("by"),
            );
            let asker = Asker {
                caller: &caller,
                call_id: &call_id,
                principal: &principal,
                by: &by,
            };
            let mut out = Effects::default();
            let new_id = format!("{:032x}", room.made + 1);
            let answered: Result<Value, _> = match step["do"].as_str().unwrap() {
                "tick" => Ok(Value::Null),
                "snapshot" => room.calls.snapshot(asker, at, &mut out),
                "start" => {
                    let agents: Vec<String> = step["agents"]
                        .as_array()
                        .unwrap()
                        .iter()
                        .map(|agent| agent.as_str().unwrap().to_string())
                        .collect();
                    let asker = Asker {
                        call_id: "",
                        ..asker
                    };
                    let asked = (
                        agents.as_slice(),
                        step["request_id"].as_str().unwrap_or(""),
                        None,
                    );
                    room.calls
                        .start(asker, asked, (&roster, &new_id), at, &mut out)
                }
                "change" => {
                    let asked = (
                        step["action"].as_str().unwrap(),
                        step["agent"].as_str().unwrap(),
                    );
                    room.calls.change(asker, asked, &roster, at, &mut out)
                }
                "end" => room.calls.end(asker, at, &mut out),
                "route" => room.calls.route(asker, &said("session"), at, &mut out),
                "routed" => {
                    room.calls.routed(asker, &said("session"), at, &mut out);
                    Ok(Value::Null)
                }
                other => panic!("a step this test does not know: {other}"),
            };
            steps += 1;
            let here = format!("{name}, step {place}: {step}");
            match answered {
                Ok(result) => {
                    assert!(
                        row["refused"].is_null(),
                        "{here}: refused there with {}",
                        row["refused"]
                    );
                    assert_eq!(result, row["result"], "{here}");
                    let call = &result["call"];
                    if call["state"] == "live" {
                        let id = call["call_id"].as_str().unwrap().to_string();
                        if id == format!("gcl_{new_id}") {
                            room.made += 1;
                        }
                        if room.first.is_empty() {
                            room.first = id.clone();
                        }
                        room.last
                            .insert(call["principal"].as_str().unwrap().to_string(), id);
                    } else if call["call_id"] == format!("gcl_{new_id}") {
                        room.made += 1;
                    }
                }
                Err(refused) => {
                    refused_steps += 1;
                    assert_eq!(
                        json!({ "status": refused.status, "body": refused.body }),
                        row["refused"],
                        "{here}"
                    );
                }
            }
            assert_eq!(
                json!(out.events),
                row["events"],
                "{here}: what the apps are told"
            );
            told += out.events.len();
            // What is noted for whom: there one table for each change, here one notice for
            // each agent; compared as who is told what.
            let mut there: Vec<(String, String)> = Vec::new();
            for noted in row["notices"].as_array().unwrap() {
                for (session, text) in noted.as_object().unwrap() {
                    there.push((session.clone(), text.as_str().unwrap().to_string()));
                }
            }
            there.sort();
            let mut ours: Vec<(String, String)> = out
                .notices
                .iter()
                .map(|notice| (notice.session.clone(), notice.text.clone()))
                .collect();
            ours.sort();
            assert_eq!(ours, there, "{here}: what is noted");
            for notice in &out.notices {
                let parts: Vec<&str> = notice.id.split(':').collect();
                assert!(
                    parts.len() == 3 && parts[0].starts_with("gcl_") && parts[2] == notice.agent_id,
                    "{here}: {}",
                    notice.id
                );
            }
        }
        if let Some(kept) = script["kept"].as_array() {
            let ours: Vec<Value> = room
                .calls
                .of("device_a")
                .iter()
                .map(|call| json!(call.call_id))
                .collect();
            assert_eq!(&ours, kept, "{name}: the calls that are kept");
            assert_eq!(kept.len(), KEPT_ENDED);
        }
    }
    assert!(
        scripts.len() >= 11 && steps >= 180,
        "{} scripts, {steps} steps",
        scripts.len()
    );
    assert!(
        refused_steps >= 30 && told >= 90,
        "{refused_steps} refused, {told} events"
    );
}

#[test]
fn a_phones_calls_are_kept_and_read_back_as_they_were() {
    let vectors = vectors();
    let roster = roster(&vectors);
    let mut calls = Calls::default();
    let at = At {
        now_ms: 1_800_000_000_000,
        host_id: "host-1",
    };
    let phone = Asker {
        caller: "device_a",
        ..Asker::default()
    };
    let mut out = Effects::default();
    let agents = ["theo".to_string(), "mike".to_string()];
    let ring = json!({ "reason": "a leak", "opening_line": "Shall I look?" });
    let started = calls
        .start(
            phone,
            (&agents, "one", Some(ring)),
            (&roster, "1"),
            at,
            &mut out,
        )
        .unwrap();
    calls.route(phone, "mike-86db", at, &mut out).unwrap();
    assert_eq!(out.changed.iter().collect::<Vec<_>>(), ["device_a"]);
    // What is kept is the call with what the Host keeps for itself beside what it tells.
    let kept = serde_json::to_string(calls.of("device_a")).unwrap();
    let read: Vec<clarp_kernel::calls::Call> = serde_json::from_str(&kept).unwrap();
    let again = Calls::kept(BTreeMap::from([("device_a".to_string(), read)]));
    assert_eq!(again, calls);
    let call = &again.of("device_a")[0];
    assert_eq!(call.routed(), Some(("mike-86db", at.now_ms)));
    assert_eq!(call.ring().unwrap()["session"], "theo-97e5");
    assert_eq!(call.ring().unwrap()["reason"], "a leak");
    // The same request again is the same call, also after it was read back.
    let mut again = again;
    let same = again
        .start(phone, (&agents, "one", None), (&roster, "2"), at, &mut out)
        .unwrap();
    assert_eq!(same["call"]["call_id"], started["call"]["call_id"]);
    assert_eq!(
        (&same["changed"], &same["summary"]),
        (&json!(false), &json!("The call is already up."))
    );
}
