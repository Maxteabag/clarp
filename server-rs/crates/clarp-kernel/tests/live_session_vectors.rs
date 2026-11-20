//! The session of a live call, against the Python Host's own.
//!
//! `fixtures/live-session-vectors.json` is written by `server-rs/tools/live-session-vectors.py`:
//! the Python Host's session (`LiveSocket`) on a ring placed, answered and joined through its
//! own modules, with a phone, a voice and a clock made up there, through stories of what each
//! side says and of time passing: what the phone and the voice were told after each step, and
//! what the ring's own record says of the call. The same must come of the same here.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::collections::BTreeMap;

use clarp_kernel::calls::{self, At, Calls, Contact, Refused};
use clarp_kernel::live_session::{self as session, Book, Session, Wire};
use clarp_kernel::rings::{Claim, Effects, Joined, Live, Profiles, Ring, Rings, World};
use serde_json::{Map, Value, json};

const NOW: i64 = 1_800_000_000_000;
const LIVE: &str = "agent_live_v1";

fn vectors() -> Value {
    serde_json::from_str(include_str!("fixtures/live-session-vectors.json")).unwrap()
}

/// A value of the fixture as it was before its long runs of one sign were written short.
fn whole(value: &Value) -> Value {
    match value {
        Value::Object(fields) if fields.len() == 1 && fields.contains_key("~run") => {
            let run = &fields["~run"];
            let count = usize::try_from(run[2].as_u64().unwrap()).unwrap();
            let after = whole(&run[3]);
            json!(format!(
                "{}{}{}",
                run[0].as_str().unwrap(),
                run[1].as_str().unwrap().repeat(count),
                after.as_str().unwrap()
            ))
        }
        Value::Object(fields) => {
            let fields = fields.iter().map(|(key, item)| (key.clone(), whole(item)));
            Value::Object(fields.collect())
        }
        Value::Array(items) => Value::Array(items.iter().map(whole).collect()),
        plain => plain.clone(),
    }
}

/// No profile is asked for once a ring is there.
struct NoProfiles;

impl Profiles for NoProfiles {
    fn profile(&self, _profile_id: &str) -> Option<String> {
        None
    }

    fn file(&self, _profile_id: &str, _name: &str) -> Option<Vec<u8>> {
        None
    }

    fn takes_calls(&self, _session: &str) -> Result<Option<String>, Refused> {
        Ok(None)
    }
}

/// The ring of a session's call, written on as the session says.
struct OnRing<'a> {
    rings: &'a mut Rings,
    call: (&'a str, i64),
}

impl Book for OnRing<'_> {
    fn holds(&self) -> bool {
        self.rings.holds(self.call.0, self.call.1)
    }

    fn opening(&mut self) -> bool {
        self.rings.opening(self.call, &mut Effects::default())
    }

    fn under_way(&mut self) -> bool {
        self.rings.under_way(self.call, &mut Effects::default())
    }

    fn heard(&mut self) -> bool {
        self.rings.heard(self.call, &mut Effects::default())
    }

    fn retire(&mut self, reason: &str) -> bool {
        self.rings
            .retired(self.call, reason, &mut Effects::default())
    }
}

/// What the two sides were told, as the stories' stand-ins write it down.
#[derive(Default)]
struct Told {
    phone: Vec<Value>,
    voice: Vec<Value>,
    shut: u64,
    /// How many things the voice still takes, when it has begun to take nothing.
    takes: Option<u64>,
}

impl Told {
    fn takes_one(&mut self) -> bool {
        match self.takes {
            Some(0) => false,
            Some(left) => {
                self.takes = Some(left - 1);
                true
            }
            None => true,
        }
    }
}

impl Wire for Told {
    fn phone(&mut self, said: Value) {
        self.phone.push(said);
    }

    fn voice(&mut self, said: Value) -> bool {
        let taken = self.takes_one();
        if taken {
            self.voice.push(said);
        }
        taken
    }

    fn voice_over(&mut self) {
        // The voice is told that the call is over, where it still takes that, and let go.
        if self.takes_one() {
            self.voice.push(json!({ "type": "session.close" }));
        }
        self.voice.push(json!("closed"));
    }

    fn phone_over(&mut self) {
        self.shut += 1;
    }
}

/// What a ring's own record says of its call, under the names the Python Host keeps it by.
fn kept(ring: &Ring) -> Value {
    let live = ring.live.as_deref().unwrap();
    let mut said = Map::new();
    let mut put = |name: &str, value: Value, there: bool| {
        if there {
            said.insert(name.to_string(), value);
        }
    };
    put("_live_phase", json!(live.phase), true);
    put(
        "_live_retired_reason",
        json!(live.retired_reason),
        !live.retired_reason.is_empty(),
    );
    put("_live_call_epoch", json!(live.call_epoch), true);
    put("_live_opening_consumed", json!(true), live.opening_consumed);
    put("_live_audio_forwarded", json!(true), live.audio_forwarded);
    put(
        "_live_close_receipt",
        json!(live.close_receipt),
        live.close_receipt.is_some(),
    );
    let switched = !live.fallback_request_id.is_empty();
    put(
        "_live_fallback_request_id",
        json!(live.fallback_request_id),
        switched,
    );
    put("call_id", json!(ring.call_id), true);
    Value::Object(said)
}

#[test]
fn the_bounds_of_a_session_are_the_python_hosts() {
    let vectors = vectors();
    assert_eq!(
        json!({
            "ready_s": session::READY_S, "opening_ack_s": session::OPENING_TAKEN_S,
            "tick_s": session::TICK_S, "held_bytes": session::HELD_MOST,
            "held_frame_signs": session::HELD_PIECE_SIGNS, "sound_signs": session::SOUND_SIGNS,
            "device_check_ticks": session::LOOKED_EVERY, "client_idle_s": session::PHONE_IDLE_S,
            "join_wait_s": session::JOIN_WITHIN_S,
        }),
        vectors["limits"]
    );
    assert_eq!(json!(session::silence()), whole(&vectors["silence"]));
}

#[test]
fn a_session_goes_as_the_python_hosts_goes_step_by_step() {
    // The Python Host's own minimums, as a Host whose owner names none has them.
    let minimums: Vec<(String, i64)> = clarp_kernel::live_call::AGENT_LIVE_MODEL_MIN_CLIENT
        .iter()
        .map(|(name, minimum)| (name.to_string(), *minimum))
        .collect();
    let vectors = vectors();
    let phone = vectors["phone"].as_str().unwrap();
    let host_id = vectors["host_id"].as_str().unwrap();
    let roster = [Contact {
        session: "theo-97e5".to_string(),
        agent_id: "a-theo".to_string(),
        persona: "Theo".to_string(),
        helper: false,
    }];
    let (mut steps, mut to_phone, mut to_voice) = (0, 0, 0);
    let mut ended: BTreeMap<String, usize> = BTreeMap::new();
    for story in vectors["stories"].as_array().unwrap() {
        let head = whole(&story["head"]);
        let name = head["name"].as_str().unwrap();
        let text = |key: &str| head["ids"][key].as_str().unwrap().to_string();
        let (call_id, attempt_id) = (text("call_id"), text("attempt_id"));
        let epoch = head["ids"]["call_epoch"].as_i64().unwrap();
        let by_the_user = head["call"] == "user";
        // The ring as it is when its phone has joined.
        let live = Live {
            snapshot: head["snapshot"].clone(),
            context_digest: head["context_digest"].as_str().unwrap().to_string(),
            event_key: if by_the_user { "" } else { "leak-watch:evt-1" }.to_string(),
            call_id: call_id.clone(),
            call_epoch: epoch,
            attempt_id: attempt_id.clone(),
            owner_nonce: "4".repeat(64),
            join_ticket: "5".repeat(64),
            join_expires_at: NOW + 60_000,
            join_consumed: true,
            phase: "connecting".to_string(),
            request_id: "answer-1".to_string(),
            client_nonce: "nonce-1".to_string(),
            user_call: if by_the_user { phone } else { "" }.to_string(),
            ..Live::default()
        };
        let ring = Ring {
            opening_mode: String::new(),
            generation: Value::Null,
            opening: Value::Null,
            ring_id: head["ring_id"].as_str().unwrap().to_string(),
            session: "theo-97e5".to_string(),
            persona: "Theo".to_string(),
            agent_id: "a-theo".to_string(),
            reason: "Possible water leak at pump 01".to_string(),
            opening_line: head["snapshot"]["opening_text"]
                .as_str()
                .unwrap()
                .to_string(),
            opening_audio_url: String::new(),
            state: "answered".to_string(),
            created_at: NOW,
            expires_at: NOW + 45_000,
            ended_at: Some(NOW),
            answered_by: phone.to_string(),
            call_id: String::new(),
            conversation_mode: LIVE.to_string(),
            live: Some(Box::new(live)),
        };
        let joined = Joined {
            ring_id: ring.ring_id.clone(),
            agent_id: ring.agent_id.clone(),
            call_id: call_id.clone(),
            call_epoch: epoch,
            attempt_id: attempt_id.clone(),
            context_digest: head["context_digest"].as_str().unwrap().to_string(),
            snapshot: head["snapshot"].clone(),
            principal: phone.to_string(),
            // The model the grant named, for the client that answered.
            model: head["model"].as_str().unwrap().to_string(),
        };
        let mut rings = Rings::kept(vec![ring], Vec::new(), &json!({}), Vec::new());
        let mut calls = Calls::kept(BTreeMap::new());
        // The clock of the stories: it begins at an hour of its own and goes on by ticks.
        let mut clock = 5000.0_f64;
        let mut machine = Session::new(joined, head["opening_event_id"].as_str().unwrap(), clock);
        let mut told = Told::default();
        let mut paired = true;
        let owner = |more: Value| {
            let mut said = json!({
                "call_id": call_id, "owner_nonce": "4".repeat(64), "attempt_id": attempt_id,
            });
            for (key, value) in more.as_object().unwrap() {
                said[key] = value.clone();
            }
            said
        };
        for (place, row) in story["steps"].as_array().unwrap().iter().enumerate() {
            let row = whole(row);
            let step = &row["step"];
            let here = format!("{name}, step {place}: {}", step["do"]);
            told.phone.clear();
            told.voice.clear();
            told.shut = 0;
            let device_ok = |principal: &str| !principal.starts_with("device_") || paired;
            let world = World {
                profiles: &NoProfiles,
                android: &[],
                opening: None,
                can_push_android: false,
                device_ok: &device_ok,
                host_id,
                has_key: true,
                runs_tools: true,
                live_model: clarp_kernel::live_call::LIVE_MODEL_DEFAULT,
                live_model_min_client: &minimums,
            };
            let call = (call_id.as_str(), epoch);
            let mut out = Effects::default();
            let mut answered: Result<Value, Refused> = Ok(Value::Null);
            match step["do"].as_str().unwrap() {
                "start" => {
                    if step["late"] == "closed" {
                        let said = owner(json!({
                            "call_epoch": 1, "reason": "user_hangup", "request_id": "close-0",
                        }));
                        rings.close(phone, &said, &world, NOW, &mut out).unwrap();
                    }
                    let (key, reached) = (step["key"] == true, step["connects"] == true);
                    let mut book = OnRing {
                        rings: &mut rings,
                        call,
                    };
                    machine.start((key, reached), host_id, &mut book, &mut told);
                }
                "phone" | "ready" => {
                    let mut book = OnRing {
                        rings: &mut rings,
                        call,
                    };
                    machine.phone(&step["said"], &mut book, &mut told, clock);
                }
                "voice" | "took" => {
                    let mut book = OnRing {
                        rings: &mut rings,
                        call,
                    };
                    machine.voice(&step["said"], &mut book, &mut told, clock);
                }
                // Time passes between two ticks of the session's clock.
                "wait" => clock += step["seconds"].as_f64().unwrap(),
                "ticks" => {
                    for _ in 0..step["count"].as_u64().unwrap() {
                        clock += session::TICK_S;
                        let mut book = OnRing {
                            rings: &mut rings,
                            call,
                        };
                        machine.tick(paired, &mut book, &mut told, clock);
                    }
                }
                "gone" => {
                    let mut book = OnRing {
                        rings: &mut rings,
                        call,
                    };
                    if step["who"] == "phone" {
                        machine.phone_gone(&mut book, &mut told);
                    } else {
                        machine.voice_gone(&mut book, &mut told);
                    }
                }
                "device" => paired = step["state"] == "full",
                "deaf" => told.takes = step["after"].as_u64(),
                "retired" => {
                    let reason = step["reason"].as_str().unwrap();
                    rings.retired(call, reason, &mut out);
                }
                "close" => {
                    let now_epoch = rings.rings()[0].live.as_deref().unwrap().call_epoch;
                    let said = owner(json!({
                        "call_epoch": now_epoch, "reason": step["reason"], "request_id": "close-1",
                    }));
                    answered = match rings.close(phone, &said, &world, NOW, &mut out) {
                        Ok((answer, _call)) => {
                            // The call is retired on its ring; its session is stopped after.
                            machine.stopped(&mut told, step["reason"].as_str().unwrap());
                            Ok(answer)
                        }
                        Err(why) => Err(why),
                    };
                }
                "fallback" => {
                    let now_epoch = rings.rings()[0].live.as_deref().unwrap().call_epoch;
                    let said = owner(json!({
                        "expected_epoch": now_epoch, "audio_stopped": true, "request_id": "fallback-1",
                    }));
                    answered = match rings.switch(phone, &said, (&calls, &world), NOW, &mut out) {
                        Err(why) => Err(why),
                        Ok(Claim::Before(answer)) => Ok(answer),
                        Ok(Claim::Stop { .. }) => {
                            machine.stopped(&mut told, "fallback");
                            let at = At {
                                now_ms: NOW,
                                host_id,
                            };
                            let mut of_calls = calls::Effects::default();
                            let voiced = format!("/audio/{NOW}__theo-97e5.mp3");
                            let new_id = format!("{:032x}", 7);
                            rings.switched(
                                &mut calls,
                                (phone, &said),
                                (machine.is_over(), &voiced),
                                (&roster, &new_id),
                                at,
                                (&mut out, &mut of_calls),
                            )
                        }
                    }
                    .map(|answer| {
                        json!({
                            "conversation_mode": answer["conversation_mode"],
                            "call": answer["call"].is_object(),
                        })
                    });
                }
                other => panic!("{other}"),
            }
            steps += 1;
            match answered {
                Ok(answer) => {
                    assert_eq!(row["refused"], Value::Null, "{here}");
                    assert_eq!(answer, row["result"], "{here}");
                }
                Err(refused) => {
                    let ours = json!({ "status": refused.status, "error": refused.body["error"] });
                    assert_eq!(ours, row["refused"], "{here}");
                }
            }
            assert_eq!(
                json!(told.phone),
                row["to_phone"],
                "{here}: what the phone is told"
            );
            assert_eq!(
                json!(told.voice),
                row["to_voice"],
                "{here}: what the voice is told"
            );
            assert_eq!(json!(told.shut), row["shut"], "{here}: the phone's socket");
            assert_eq!(
                kept(&rings.rings()[0]),
                row["kept"],
                "{here}: what the ring keeps"
            );
            assert_eq!(json!(machine.is_over()), row["over"], "{here}");
            assert_eq!(json!(machine.reason()), row["reason"], "{here}");
            // A session that is over has let its voice go: nothing of it is left open.
            assert_eq!(json!(machine.is_over()), row["settled"], "{here}");
            to_phone += told.phone.len();
            to_voice += told.voice.len();
        }
        let reason = match machine.reason() {
            "" => "not over",
            reason => reason,
        };
        *ended.entry(reason.to_string()).or_default() += 1;
    }
    assert_eq!((steps, to_phone, to_voice), (422, 276, 496));
    assert_eq!(ended.len(), 20, "{ended:?}");
}
