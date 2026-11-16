//! Rings, against the Python Host's own module.
//!
//! `fixtures/rings-vectors.json` is written by `server-rs/tools/rings-vectors.py`: what the
//! Python Host's ring module answered, announced, pushed, noted and wrote to its trail, step
//! by step, in scripts of requests from agents, two phones and the administrator; and the
//! push a phone is sent for a ring in each of its states. The same must come of the same here.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::collections::{BTreeMap, HashSet};

use clarp_kernel::calls::{self, Asker, At, Calls, Contact};
use clarp_kernel::push;
use clarp_kernel::rings::{
    AUDIT_KEPT, Answer, Asked, BETWEEN_MS, Caller, Effects, HOURLY_MOST, KEPT, OPENING_MOST,
    People, REASON_MOST, RING_MS, Rings,
};
use serde_json::{Value, json};

fn vectors() -> Value {
    serde_json::from_str(include_str!("fixtures/rings-vectors.json")).unwrap()
}

fn text(value: &Value, key: &str) -> String {
    value[key].as_str().unwrap_or("").to_string()
}

/// The agents of the Host as the vectors made them, without the ones a script let go.
struct Agents<'a> {
    all: &'a [Value],
    gone: HashSet<String>,
}

impl Agents<'_> {
    fn here(&self) -> impl Iterator<Item = &Value> {
        let gone = &self.gone;
        self.all
            .iter()
            .filter(move |agent| !gone.contains(agent["agent_id"].as_str().unwrap()))
    }

    /// Those a name may mean: the contacts, which an archived agent and a janitor are not.
    fn roster(&self) -> Vec<Contact> {
        self.here()
            .filter(|agent| agent.get("archived_at").is_none() && agent.get("is_janitor").is_none())
            .map(|agent| Contact {
                session: text(agent, "session"),
                agent_id: text(agent, "agent_id"),
                persona: text(agent, "persona"),
                helper: false,
            })
            .collect()
    }

    fn known(&self, agent_id: &str) -> Option<Caller> {
        let agent = self.here().find(|agent| agent["agent_id"] == agent_id)?;
        Some(Caller {
            session: text(agent, "session"),
            persona: text(agent, "persona"),
            agent_id: text(agent, "agent_id"),
            muted: agent.get("muted").is_some(),
            can_call: agent.get("archived_at").is_none() && agent.get("is_janitor").is_none(),
        })
    }
}

/// What the vectors' phones answered a push with: one phone took it, and none takes a call
/// through the Host's own key.
fn delivery() -> Value {
    json!({
        "voip": { "enabled": false, "sent": 0, "failed": 0 },
        "enabled": true, "sent": 1, "failed": 0, "disabled": 0,
    })
}

#[test]
fn rings_go_as_the_python_hosts_go_step_by_step() {
    let vectors = vectors();
    assert_eq!(
        (&vectors["ring_ms"], &vectors["between_ms"]),
        (&json!(RING_MS), &json!(BETWEEN_MS))
    );
    assert_eq!(
        (
            &vectors["hourly_most"],
            &vectors["kept"],
            &vectors["audit_kept"]
        ),
        (&json!(HOURLY_MOST), &json!(KEPT), &json!(AUDIT_KEPT))
    );
    assert_eq!(
        (&vectors["reason_most"], &vectors["opening_most"]),
        (&json!(REASON_MOST), &json!(OPENING_MOST))
    );
    let all = vectors["agents"].as_array().unwrap();
    let (mut steps, mut refusals, mut placed) = (0, 0, 0);
    for script in vectors["scripts"].as_array().unwrap() {
        let name = script["name"].as_str().unwrap();
        let mut rings = Rings::default();
        let mut calls = Calls::kept(BTreeMap::new());
        let mut agents = Agents {
            all,
            gone: HashSet::new(),
        };
        let mut made: u64 = 0;
        for (place, row) in script["steps"].as_array().unwrap().iter().enumerate() {
            let step = &row["step"];
            let now = row["now"].as_i64().unwrap();
            let at = At {
                now_ms: now,
                host_id: "host-1",
            };
            let do_ = step["do"].as_str().unwrap();
            let (mut out, mut of_calls) = (Effects::default(), calls::Effects::default());
            let new_id = format!("{:032x}", made + 1);
            // The agents as they are when the step begins.
            let there = Agents {
                all,
                gone: agents.gone.clone(),
            };
            let roster = there.roster();
            let known = |agent_id: &str| there.known(agent_id);
            let people = People {
                roster: &roster,
                known: &known,
            };
            let ring_id = text(step, "ring_id");
            let before = made;
            let answered: Result<Value, calls::Refused> = match do_ {
                "tick" => Ok(Value::Null),
                "forget" => {
                    let gone = text(step, "agent_id");
                    agents.gone.insert(gone);
                    Ok(Value::Null)
                }
                "timer" => Ok(json!(rings.timer(now, &mut out))),
                "call" => {
                    let principal = text(step, "principal");
                    let asker = Asker {
                        caller: &principal,
                        ..Asker::default()
                    };
                    calls.snapshot(asker, at, &mut of_calls)
                }
                "settings" => match &step["change"] {
                    Value::Null => Ok(rings.shown()),
                    change => rings.change(change, now, &mut out),
                },
                "voip" => rings.voip_token(
                    &text(step, "principal"),
                    (
                        &text(step, "token"),
                        &text(step, "environment"),
                        &step["modes"],
                    ),
                    now,
                    &mut out,
                ),
                "ring" => {
                    let asked = Asked {
                        turn_agent_id: step["turn"].as_str().unwrap(),
                        agent: step["agent"].as_str().unwrap(),
                        reason: step["reason"].as_str().unwrap(),
                        opening_line: step["opening_line"].as_str().unwrap(),
                        conversation_mode: step.get("conversation_mode").unwrap_or(&Value::Null),
                        event_context: step.get("event_context").unwrap_or(&Value::Null),
                        opening_mode: step.get("opening_mode"),
                    };
                    rings.admit(&asked, people, now, &mut out).map(|admitted| {
                        let voiced = if step["voiced"] == true {
                            format!("/audio/{now}__{}.mp3", admitted.caller().session)
                        } else {
                            String::new()
                        };
                        let ring = rings.place(admitted, &voiced, &new_id, now, &mut out);
                        made += 1;
                        Rings::rung(&ring, delivery(), now)
                    })
                }
                "look" => rings.look(&ring_id, now, &mut out),
                "answer" => {
                    let asked = Answer {
                        principal: step["principal"].as_str().unwrap(),
                        ring_id: &ring_id,
                        conversation_mode: step.get("conversation_mode").unwrap_or(&Value::Null),
                        opening_mode: step.get("opening_mode"),
                    };
                    let answered = rings.answer(
                        &mut calls,
                        &asked,
                        (&roster, &new_id),
                        at,
                        (&mut out, &mut of_calls),
                    );
                    if answered
                        .as_ref()
                        .is_ok_and(|answer| answer["changed"] == true)
                    {
                        made += 1;
                    }
                    answered
                }
                "decline" => rings.decline((&text(step, "principal"), &ring_id), now, &mut out),
                "cancel" => rings.cancel((&text(step, "turn"), &ring_id), now, &mut out),
                other => panic!("{other}"),
            };
            let here = format!("{name}, step {place}: {step}");
            steps += 1;
            match answered {
                Ok(answer) => {
                    assert_eq!(row["refused"], Value::Null, "{here}");
                    assert_eq!(answer, row["result"], "{here}");
                    placed += usize::from(do_ == "ring");
                }
                Err(refused) => {
                    refusals += 1;
                    assert_eq!(
                        json!({ "status": refused.status, "body": refused.body }),
                        row["refused"],
                        "{here}"
                    );
                }
            }
            assert_eq!(json!(made - before), row["minted"], "{here}");
            // What a call says of itself comes before what the ring says of its answer.
            let events: Vec<Value> = of_calls.events.into_iter().chain(out.events).collect();
            assert_eq!(json!(events), row["events"], "{here}");
            let of_call = of_calls.notices.iter().map(|notice| {
                json!({ "agent_id": notice.agent_id, "id": notice.id, "text": notice.text })
            });
            let of_ring = out.notices.iter().map(|notice| {
                json!({ "agent_id": notice.agent_id, "id": notice.id, "text": notice.text })
            });
            let notices: Vec<Value> = of_call.chain(of_ring).collect();
            assert_eq!(json!(notices), row["notices"], "{here}");
            let pushes: Vec<Value> = out
                .pushes
                .iter()
                .map(|pushed| {
                    json!({
                        "ring_id": pushed.ring.ring_id, "kind": pushed.kind, "state": pushed.ring.state,
                    })
                })
                .collect();
            assert_eq!(json!(pushes), row["pushes"], "{here}");
            assert_eq!(json!(out.audited), row["audit"], "{here}");
        }
        if let Some(kept) = script["kept"].as_object() {
            let ids: Vec<&str> = rings
                .rings()
                .iter()
                .map(|ring| ring.ring_id.as_str())
                .collect();
            assert_eq!(json!(ids), kept["rings"], "{name}");
            assert_eq!(json!(rings.audit().len()), kept["audit"], "{name}");
            let oldest = rings.audit().first().cloned().unwrap_or(Value::Null);
            assert_eq!(oldest, kept["oldest_audit"], "{name}");
            assert_eq!(json!(rings.voip()), kept["voip"], "{name}");
        }
    }
    assert!(
        steps >= 470 && refusals >= 140 && placed >= 90,
        "{steps} steps, {refusals} refused, {placed} rings placed"
    );
}

#[test]
fn a_phone_is_pushed_what_the_python_host_pushes_for_a_ring() {
    let vectors = vectors();
    let cases = vectors["pushed"].as_array().unwrap();
    for case in cases {
        let ring = &case["ring"];
        let said = |key: &str| ring[key].as_str().unwrap();
        let rung = push::Rung {
            ring_id: said("ring_id"),
            state: said("state"),
            session: said("session"),
            persona: said("persona"),
            reason: said("reason"),
            opening_line: said("opening_line"),
            opening_audio_url: said("opening_audio_url"),
            expires_at: ring["expires_at"].as_i64().unwrap(),
            kind: case["kind"].as_str().unwrap(),
            show_reason: case["show_reason"].as_bool().unwrap(),
            avatar_url: case["avatar_url"].as_str().unwrap(),
            avatar_custom: case["avatar_custom"].as_bool().unwrap(),
            server_instance_id: case["server_instance_id"].as_str().unwrap(),
        };
        let payload = push::ring(&rung);
        assert_eq!(payload, case["payload"], "{case}");
        assert_eq!(
            json!(push::ring_notification_id(rung.ring_id)),
            case["collapse_id"]
        );
        // The name it goes by is the one a later push of the ring replaces it under.
        assert_eq!(json!(push::collapse_id(&payload)), case["collapse_id"]);
    }
    assert!(cases.len() >= 18);
}
