//! Rings held as live voice calls, against the Python Host's own modules.
//!
//! `fixtures/live-ring-vectors.json` is written by `server-rs/tools/live-ring-vectors.py`:
//! what the Python Host's ring module, its module of live calls and the join of its socket
//! answered, announced, pushed, noted, wrote to the trail and kept on the ring, step by step,
//! in scripts of requests from agents, phones and the administrator. The same must come of
//! the same here.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::cell::RefCell;
use std::collections::{BTreeMap, BTreeSet};

use clarp_kernel::calls::{self, Asker, At, Calls, Contact, Refused};
use clarp_kernel::live_call::{self, Found, Scan};
use clarp_kernel::push;
use clarp_kernel::rings::{
    Admission, Android, Answer, Asked, Caller, Claim, Effects, Given, JOIN_MS, Minted, People,
    Profiles, Ring, Rings, World, claimed,
};
use serde_json::{Map, Value, json};

fn vectors() -> Value {
    serde_json::from_str(include_str!("fixtures/live-ring-vectors.json")).unwrap()
}

fn text(value: &Value, key: &str) -> String {
    value[key].as_str().unwrap_or("").to_string()
}

/// The profiles of the vectors' Host: each folder has its `profile.json` and the same files.
struct Shelf {
    profiles: BTreeMap<String, String>,
    files: BTreeMap<String, Vec<u8>>,
    /// What is kept of the agents known to take the user's calls live.
    known: RefCell<Option<String>>,
}

impl Shelf {
    fn found(&self) -> Vec<Found> {
        let folders = self.profiles.iter().map(|(name, held)| Found {
            name: name.clone(),
            is_folder: true,
            profile: Some(held.as_bytes().to_vec()),
        });
        folders.collect()
    }

    fn taking(&self, strict: bool) -> Result<BTreeMap<String, Option<String>>, Refused> {
        let found = self.found();
        let known = self.known.borrow().clone();
        let taking = live_call::taking(known.as_deref(), Scan::Folders(&found), strict);
        if let Some(keep) = taking.keep {
            *self.known.borrow_mut() = Some(keep);
        }
        taking.takes
    }
}

impl Profiles for Shelf {
    fn profile(&self, profile_id: &str) -> Option<String> {
        self.profiles.get(profile_id).cloned()
    }

    fn file(&self, _profile_id: &str, name: &str) -> Option<Vec<u8>> {
        self.files.get(name).cloned()
    }

    fn takes_calls(&self, session: &str) -> Result<Option<String>, Refused> {
        Ok(self.taking(true)?.get(session).cloned().flatten())
    }
}

/// The agents of the vectors' Host.
struct Agents<'a>(&'a [Value]);

impl Agents<'_> {
    fn roster(&self) -> Vec<Contact> {
        let contacts = self
            .0
            .iter()
            .filter(|agent| agent.get("archived_at").is_none());
        contacts
            .map(|agent| Contact {
                session: text(agent, "session"),
                agent_id: text(agent, "agent_id"),
                persona: text(agent, "persona"),
                helper: false,
            })
            .collect()
    }

    fn known(&self, agent_id: &str) -> Option<Caller> {
        let agent = self.0.iter().find(|agent| agent["agent_id"] == agent_id)?;
        Some(Caller {
            session: text(agent, "session"),
            persona: text(agent, "persona"),
            agent_id: text(agent, "agent_id"),
            muted: agent.get("muted").is_some(),
            can_call: agent.get("archived_at").is_none(),
        })
    }
}

/// What a ring's own record says of its call, under the names the Python Host keeps it by:
/// only what is there.
fn kept(ring: &Ring) -> Value {
    let mut said = Map::new();
    let mut put = |name: &str, value: Value, there: bool| {
        if there {
            said.insert(name.to_string(), value);
        }
    };
    let live = ring.live.as_deref();
    put(
        "conversation_mode",
        json!(ring.conversation_mode),
        !ring.conversation_mode.is_empty(),
    );
    if let Some(live) = live {
        let answered = !live.call_id.is_empty();
        put("_live_call_id", json!(live.call_id), answered);
        put("_live_call_epoch", json!(live.call_epoch), answered);
        put("_live_attempt_id", json!(live.attempt_id), answered);
        put("_live_owner_nonce", json!(live.owner_nonce), answered);
        put("_live_join_ticket", json!(live.join_ticket), answered);
        put(
            "_live_join_expires_at",
            json!(live.join_expires_at),
            answered,
        );
        put("_live_join_consumed", json!(true), live.join_consumed);
        put("_live_phase", json!(live.phase), !live.phase.is_empty());
        put(
            "_live_retired_reason",
            json!(live.retired_reason),
            !live.retired_reason.is_empty(),
        );
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
        put(
            "_live_fallback_cancelled",
            json!(true),
            live.fallback_cancelled,
        );
        put("_live_opening_consumed", json!(true), live.opening_consumed);
        put("_live_audio_forwarded", json!(true), live.audio_forwarded);
        put("_live_request_id", json!(live.request_id), answered);
        put("_live_client_nonce", json!(live.client_nonce), answered);
        put(
            "_user_call",
            json!(live.user_call),
            !live.user_call.is_empty(),
        );
        put(
            "_live_event_key",
            json!(live.event_key),
            !live.event_key.is_empty(),
        );
        put(
            "_live_event_digest",
            json!(live.event_digest),
            !live.event_digest.is_empty(),
        );
        put("_live_context_digest", json!(live.context_digest), true);
    }
    put("state", json!(ring.state), true);
    put("call_id", json!(ring.call_id), true);
    put("opening_audio_url", json!(ring.opening_audio_url), true);
    put("answered_by", json!(ring.answered_by), true);
    // And whether what its voice would be told is still kept.
    let told = live.is_some_and(|live| !live.snapshot.is_null());
    put("_live_snapshot", json!(true), told);
    Value::Object(said)
}

/// What the vectors' phones answer a push with: every call token takes it, and where none
/// does one phone takes the alert.
/// The way to the push service with a key of the Host's own, as a story has it: whether the
/// Host has such a key, and what the service answers for a phone from now on (a status and
/// its reason, or nothing at all).
struct CallPushes {
    ready: bool,
    answers: BTreeMap<String, Value>,
}

impl CallPushes {
    /// A ring pushed as an incoming call to the phones that are to have it: what is handed
    /// over for each, what the service answers, and what the one who rang is told of it.
    fn pushed(
        &self,
        (rings, world): (&Rings, &World),
        ring: &Ring,
        phones: &[String],
    ) -> (Vec<Value>, Value) {
        let asked = self.ready && !phones.is_empty();
        let rung = push::Rung {
            ring_id: &ring.ring_id,
            session: &ring.session,
            persona: &ring.persona,
            reason: &ring.reason,
            expires_at: ring.expires_at,
            show_reason: rings.shows_reason(),
            server_instance_id: "host-1",
            ..push::Rung::default()
        };
        let (mut sends, mut answers) = (Vec::new(), Vec::new());
        let handed = rings.pushed_as_calls(phones, world);
        for (phone, token, environment) in handed.iter().filter(|_| asked) {
            let answer = self.answers.get(phone).cloned();
            let answer = answer.unwrap_or_else(|| json!([200, ""]));
            answers.push(
                answer[0]
                    .as_u64()
                    .and_then(|status| u16::try_from(status).ok()),
            );
            sends.push(json!({
                "principal": phone, "token": token, "environment": environment,
                "payload": push::call(&rung, &ring.conversation_mode), "push_type": "voip",
                "priority": "10", "collapse_id": push::ring_notification_id(&ring.ring_id),
                "expiration": push::call_expires(ring.expires_at), "answered": answer,
            }));
        }
        (sends, push::calls_counted(asked, &answers))
    }
}

/// What the one who rang is told became of ringing the phones: the call pushes, the Android
/// phones a live ring is told to, and the alert where no call push reached a phone.
fn delivery(rings: &Rings, ring: &Ring, world: &World, calls: &CallPushes) -> Value {
    let sent = rings.sent(ring, "ring", world);
    let (_, counted) = calls.pushed((rings, world), ring, &sent.voip.unwrap_or_default());
    let reached = counted["sent"].as_u64().unwrap();
    let mut delivery = json!({ "voip": counted });
    if !sent.android.is_empty() {
        delivery["fcm_live"] = json!({ "pending": sent.android.len() });
    }
    if rings.alerted(ring, reached).is_some() {
        for (name, value) in [
            ("enabled", json!(true)),
            ("sent", json!(1)),
            ("failed", json!(0)),
        ] {
            delivery[name] = value;
        }
        delivery["disabled"] = json!(0);
    }
    delivery
}

#[test]
fn live_rings_go_as_the_python_hosts_go_step_by_step() {
    let vectors = vectors();
    assert_eq!(vectors["join_ms"], json!(JOIN_MS));
    assert_eq!(
        live_call::moment(1_800_000_000_000),
        "2027-01-15T08:00:00+00:00"
    );
    assert_eq!(live_call::moment(0), "1970-01-01T00:00:00+00:00");
    assert_eq!(
        live_call::moment(951_782_399_999),
        "2000-02-28T23:59:59+00:00"
    );
    assert_eq!(
        live_call::moment(951_868_800_000),
        "2000-03-01T00:00:00+00:00"
    );
    let all = vectors["agents"].as_array().unwrap();
    let agents = Agents(all);
    let watchers: Vec<String> = vectors["watchers"]
        .as_array()
        .unwrap()
        .iter()
        .map(|who| who.as_str().unwrap().to_string())
        .collect();
    let profiles: BTreeMap<String, String> = vectors["profiles"]
        .as_object()
        .unwrap()
        .iter()
        .map(|(name, held)| (name.clone(), held.as_str().unwrap().to_string()))
        .collect();
    let files: BTreeMap<String, Vec<u8>> = vectors["files"]
        .as_object()
        .unwrap()
        .iter()
        .map(|(name, held)| (name.clone(), held.as_str().unwrap().as_bytes().to_vec()))
        .collect();
    let (mut steps, mut refusals, mut placed, mut joined) = (0, 0, 0, 0);
    let (mut closes_between, mut call_pushes_sent) = (0, 0);
    for script in vectors["scripts"].as_array().unwrap() {
        let name = script["name"].as_str().unwrap();
        let shelf = Shelf {
            profiles: profiles.clone(),
            files: files.clone(),
            known: RefCell::new(None),
        };
        let mut rings = Rings::default();
        let mut calls = Calls::kept(BTreeMap::new());
        let mut made: u64 = 0;
        let mut devices: BTreeMap<String, String> = vectors["devices"]
            .as_object()
            .unwrap()
            .iter()
            .map(|(phone, state)| (phone.clone(), state.as_str().unwrap().to_string()))
            .collect();
        let mut androids: Vec<Android> = Vec::new();
        let mut call_pushes = CallPushes {
            ready: true,
            answers: BTreeMap::new(),
        };
        let (mut has_key, mut can_push_android) = (true, false);
        let mut unsettled: BTreeSet<String> = BTreeSet::new();
        for (place, row) in script["steps"].as_array().unwrap().iter().enumerate() {
            let step = &row["step"];
            let now = row["now"].as_i64().unwrap();
            let at = At {
                now_ms: now,
                host_id: "host-1",
            };
            let do_ = step["do"].as_str().unwrap();
            let here = format!("{name}, step {place}: {step}");
            let (mut out, mut of_calls) = (Effects::default(), calls::Effects::default());
            let roster = agents.roster();
            let known = |agent_id: &str| agents.known(agent_id);
            let people = People {
                roster: &roster,
                known: &known,
            };
            let paired = devices.clone();
            let device_ok = |principal: &str| {
                !principal.starts_with("device_")
                    || paired.get(principal).is_some_and(|it| it == "full")
            };
            let rows = androids.clone();
            let world = World {
                profiles: &shelf,
                android: &rows,
                can_push_android,
                device_ok: &device_ok,
                host_id: "host-1",
                has_key,
                runs_tools: true,
            };
            let ring_id = text(step, "ring_id");
            let principal = text(step, "principal");
            let before = made;
            // New names are counted on from how many were made before the step.
            let id = move |ahead: u64| format!("{:032x}", before + ahead);
            let secret = move |ahead: u64| format!("{:064x}", before + ahead);
            let of_ring = |rings: &Rings| {
                let ring = rings.rings().iter().find(|ring| ring.ring_id == ring_id);
                let live = ring.and_then(|ring| ring.live.as_deref());
                live.map_or((String::new(), 0), |live| {
                    (live.call_id.clone(), live.call_epoch)
                })
            };
            let answered: Result<Value, Refused> = match do_ {
                "tick" => Ok(Value::Null),
                "callkit" => {
                    call_pushes.ready = step["has"].as_bool().unwrap();
                    Ok(Value::Null)
                }
                "call_push" => {
                    let phone = text(step, "principal");
                    call_pushes.answers.insert(phone, step["answer"].clone());
                    Ok(Value::Null)
                }
                "key" => {
                    has_key = step["has"].as_bool().unwrap();
                    Ok(Value::Null)
                }
                "fcm" => {
                    can_push_android = step["usable"].as_bool().unwrap();
                    Ok(Value::Null)
                }
                "device" => {
                    devices.insert(principal.clone(), text(step, "state"));
                    Ok(Value::Null)
                }
                "android" => {
                    let owner = text(step, "owner");
                    androids.retain(|row| row.owner != owner);
                    if let Some(modes) = step["modes"].as_array() {
                        let modes = modes.iter().map(|mode| mode.as_str().unwrap().to_string());
                        androids.push(Android {
                            owner,
                            modes: modes.collect(),
                        });
                    }
                    Ok(Value::Null)
                }
                "timer" => Ok(json!(rings.timer(now, &mut out))),
                "call" => {
                    let asker = Asker {
                        caller: &principal,
                        ..Asker::default()
                    };
                    calls.snapshot(asker, at, &mut of_calls)
                }
                "taking" => shelf
                    .taking(false)
                    .map(|takes| json!({ "lenient": takes, "kept": shelf.known.borrow().clone() })),
                "voip" => rings.voip_token(
                    &principal,
                    (&text(step, "token"), "", &step["modes"]),
                    now,
                    &mut out,
                ),
                "prefs" => rings.receive(&principal, &step["said"], (&world, 0), now, &mut out),
                "settings" => {
                    let changed = rings.change(&step["said"], now, &mut out);
                    changed.map(|_| rings.settings())
                }
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
                    let admitted = rings.admit(&asked, people, &world, now, &mut out);
                    admitted.map(|admission| match admission {
                        Admission::Again(answer) => answer,
                        Admission::Place(admitted) => {
                            // A live ring's opening is its voice's own: nothing is voiced.
                            let voiced = if admitted.is_live() {
                                String::new()
                            } else {
                                format!("/audio/{now}__{}.mp3", admitted.caller().session)
                            };
                            let ring = rings.place(admitted, &voiced, &id(1), now, &mut out);
                            made += 1;
                            placed += 1;
                            let delivery = delivery(&rings, &ring, &world, &call_pushes);
                            Rings::rung(&ring, delivery, now)
                        }
                    })
                }
                "user_call" => {
                    let minted = (id(2), id(3), secret(4), secret(5));
                    let minted = Minted {
                        call: &minted.0,
                        attempt: &minted.1,
                        nonce: &minted.2,
                        ticket: &minted.3,
                    };
                    let called = rings.user_call(
                        (&principal, &step["agent"]),
                        (&step["request_id"], &step["client_nonce"]),
                        &known,
                        &world,
                        (&id(1), minted, now),
                        &mut out,
                    );
                    if called
                        .as_ref()
                        .is_ok_and(|answer| answer["changed"] == true)
                    {
                        made += 5;
                        placed += 1;
                    }
                    called
                }
                "look" => rings.look_as((&ring_id, &principal), &world, now, &mut out),
                "answer" => {
                    let asked = Answer {
                        principal: &principal,
                        ring_id: &ring_id,
                        conversation_mode: step.get("conversation_mode").unwrap_or(&Value::Null),
                        opening_mode: step.get("opening_mode"),
                        request_id: step.get("request_id").unwrap_or(&Value::Null),
                        client_nonce: step.get("client_nonce").unwrap_or(&Value::Null),
                    };
                    // A ring is answered as it was placed: with a call of the ordinary kind
                    // under one new name, or with a grant under four.
                    let minted = (id(1), id(2), secret(3), secret(4));
                    let given = Given {
                        roster: &roster,
                        new_id: &minted.0,
                        minted: Minted {
                            call: &minted.0,
                            attempt: &minted.1,
                            nonce: &minted.2,
                            ticket: &minted.3,
                        },
                    };
                    let answered = rings.answer(
                        &mut calls,
                        &asked,
                        given,
                        &world,
                        at,
                        (&mut out, &mut of_calls),
                    );
                    if let Ok(answer) = &answered
                        && answer["changed"] == true
                    {
                        made += if answer.get("live").is_some() { 4 } else { 1 };
                    }
                    answered
                }
                "decline" => rings.decline((&principal, &ring_id), now, &mut out),
                "cancel" => rings.cancel((&text(step, "turn"), &ring_id), now, &mut out),
                "join" => {
                    let said = &step["said"];
                    Ok(match rings.join(&principal, said, &world, now, &mut out) {
                        Ok(call) => {
                            joined += 1;
                            json!({ "joined": call.call_id, "ids": claimed(said) })
                        }
                        // Where the Python Host fails on a nonce with a sign outside ASCII,
                        // this Host refuses it as any nonce that is not the call's.
                        Err(code) if row["result"].get("raised").is_some() => {
                            assert_eq!(code, "live_owner_conflict", "{here}");
                            row["result"].clone()
                        }
                        Err(code) => json!({ "refused": code }),
                    })
                }
                "close" => {
                    let closed = rings.close(&principal, &step["said"], &world, now, &mut out);
                    closed.map(|(answer, _call)| answer)
                }
                "fallback" => {
                    let said = &step["said"];
                    match rings.switch(&principal, said, (&calls, &world), now, &mut out) {
                        Err(why) => Err(why),
                        Ok(Claim::Before(answer)) => Ok(answer),
                        Ok(Claim::Stop { call_id }) => {
                            let ring = rings.rings().iter().find(|ring| ring.ring_id == ring_id);
                            let session = ring.map_or("", |ring| ring.session.as_str());
                            let voiced = format!("/audio/{now}__{session}.mp3");
                            let settled = !unsettled.contains(&call_id);
                            // A close that comes while the line is voiced, between the halves.
                            let between = &step["meanwhile"];
                            if settled && between.is_object() {
                                let closing = &between["said"];
                                let closed =
                                    rings.close(&principal, closing, &world, now, &mut out);
                                match closed {
                                    Ok((answer, _)) => {
                                        assert_eq!(answer, between["result"], "{here}")
                                    }
                                    Err(refused) => assert_eq!(
                                        json!({ "status": refused.status, "body": refused.body }),
                                        between["refused"],
                                        "{here}"
                                    ),
                                }
                                closes_between += 1;
                            }
                            assert_eq!(
                                json!(settled && between.is_object()),
                                between.get("came").cloned().unwrap_or(json!(false)),
                                "{here}"
                            );
                            let switched = rings.switched(
                                &mut calls,
                                (&principal, said),
                                (settled, &voiced),
                                (&roster, &id(1)),
                                at,
                                (&mut out, &mut of_calls),
                            );
                            if switched.is_ok() {
                                made += 1;
                            }
                            switched
                        }
                    }
                }
                "session" => {
                    let (call_id, epoch) = of_ring(&rings);
                    let call = (call_id.as_str(), epoch);
                    Ok(match step["what"].as_str().unwrap() {
                        "unsettled" => {
                            unsettled.insert(call_id.clone());
                            Value::Null
                        }
                        "opening" => json!({ "recorded": rings.opening(call, &mut out) }),
                        "active" => json!({ "recorded": rings.under_way(call, &mut out) }),
                        _ => json!({ "recorded": rings.heard(call, &mut out) }),
                    })
                }
                other => panic!("{other}"),
            };
            steps += 1;
            match answered {
                Ok(answer) => {
                    assert_eq!(row["refused"], Value::Null, "{here}");
                    assert_eq!(answer, row["result"], "{here}");
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
            // What a call says of itself comes before what the ring says of its change, and
            // everybody is told of a call; of a ring, those who may be.
            let everybody = json!(watchers);
            let mut events: Vec<Value> = Vec::new();
            for mut event in of_calls.events {
                event["seen_by"] = everybody.clone();
                events.push(event);
            }
            for (event, pushed) in out.events.iter().zip(&out.pushes) {
                let mut event = event.clone();
                let seen = watchers
                    .iter()
                    .filter(|who| rings.sees(&pushed.ring, who, &world));
                event["seen_by"] = json!(seen.collect::<Vec<_>>());
                events.push(event);
            }
            assert_eq!(json!(events), row["events"], "{here}");
            let of_call = of_calls.notices.iter().map(|notice| {
                json!({ "agent_id": notice.agent_id, "id": notice.id, "text": notice.text })
            });
            let of_ring = out.notices.iter().map(|notice| {
                json!({ "agent_id": notice.agent_id, "id": notice.id, "text": notice.text })
            });
            let notices: Vec<Value> = of_call.chain(of_ring).collect();
            assert_eq!(json!(notices), row["notices"], "{here}");
            // Who is pushed what: the Android phones of a live ring, the call tokens of a ring
            // that begins to ring, and the alert where no call push reached a phone.
            let mut pushes: Vec<Value> = Vec::new();
            for pushed in &out.pushes {
                let sent = rings.sent(&pushed.ring, pushed.kind, &world);
                let of = |name: &str, who: &[String]| {
                    json!({
                        "ring_id": pushed.ring.ring_id, "kind": pushed.kind,
                        "state": pushed.ring.state, name: who,
                    })
                };
                if !sent.android.is_empty() {
                    pushes.push(of("fcm_live", &sent.android));
                }
                let mut reached = 0;
                if let Some(phones) = &sent.voip {
                    let (sends, counted) =
                        call_pushes.pushed((&rings, &world), &pushed.ring, phones);
                    reached = counted["sent"].as_u64().unwrap();
                    call_pushes_sent += sends.len();
                    let mut as_calls = of("voip", phones);
                    as_calls["sends"] = json!(sends);
                    as_calls["counted"] = counted;
                    pushes.push(as_calls);
                }
                if let Some(mut without) = rings.alerted(&pushed.ring, reached) {
                    without.sort();
                    pushes.push(of("alert", &without));
                }
            }
            assert_eq!(json!(pushes), row["pushes"], "{here}");
            assert_eq!(json!(out.audited), row["audit"], "{here}");
            if let Some(theirs) = row.get("kept") {
                // Of the ring a step names; of the one its answer names, where it names none.
                let named = if step.get("ring").is_some() {
                    ring_id.as_str()
                } else {
                    row["result"]["ring"]["ring_id"].as_str().unwrap_or("")
                };
                let named = rings.rings().iter().find(|ring| ring.ring_id == named);
                let ours = named.map_or(Value::Null, kept);
                assert_eq!(&ours, theirs, "{here}: what the ring keeps of its call");
            }
            if let Some(digest) = row.get("snapshot").and_then(Value::as_str) {
                let named = row["result"]["ring"]["ring_id"].as_str().unwrap_or("");
                let ring = rings.rings().iter().find(|ring| ring.ring_id == named);
                let live = ring.and_then(|ring| ring.live.as_deref());
                let ours = live.map(|live| live_call::digest(&live.snapshot));
                assert_eq!(ours.as_deref(), Some(digest), "{here}: the snapshot");
            }
        }
        if let Some(kept) = script["kept"].as_object() {
            let ids: Vec<&str> = rings
                .rings()
                .iter()
                .map(|ring| ring.ring_id.as_str())
                .collect();
            assert_eq!(json!(ids), kept["rings"], "{name}");
            // What a voice was told is kept until its call can no longer start or run.
            let told = |ring: &Ring| {
                let live = ring.live.as_deref();
                let live = live.filter(|live| !live.snapshot.is_null());
                live.map(|live| live_call::digest(&live.snapshot))
            };
            let digests: Vec<Option<String>> = rings.rings().iter().map(told).collect();
            assert_eq!(json!(digests), kept["snapshots"], "{name}");
            assert_eq!(json!(rings.voip()), kept["voip"], "{name}");
            // And a ring is read back as it was kept, with what it keeps of its call.
            let written = serde_json::to_string(rings.rings()).unwrap();
            let read: Vec<Ring> = serde_json::from_str(&written).unwrap();
            assert_eq!(read, rings.rings(), "{name}");
            assert!(
                written.contains("\"_live\"") && !written.contains("snapshot"),
                "{name}"
            );
            let shown = rings.rings()[0].told(0).to_string();
            assert!(
                !shown.contains("\"_live\"") && !shown.contains("nonce"),
                "{shown}"
            );
        }
    }
    assert_eq!((steps, refusals, placed, joined), (653, 185, 63, 14));
    assert_eq!((closes_between, call_pushes_sent), (3, 80));
}
