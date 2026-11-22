#![allow(clippy::unwrap_used)]
use clarp_kernel::rings::opening::{self as rules, Effects, Greeting, World};
use serde_json::{Value, json};

// The Python lifecycle stores trace-based clip metadata in the same transaction.
// The Rust actor binds its durable turn after admission; that policy is tested separately.
fn lifecycle_record(record: &Value) -> Value {
    let mut record = record.clone();
    if let Some(fields) = record.as_object_mut() {
        fields.remove("clip_bindings");
    }
    record
}

#[test]
fn every_claim_greeting_and_supersession_matches_the_reference() {
    let fixture: Value =
        serde_json::from_str(include_str!("fixtures/listen-first-vectors.json")).unwrap();
    let mut count = 0;
    let stories = fixture["stories"].as_array().unwrap();
    for story in stories {
        let mut op = story["initial"].clone();
        let mut generation = op["fence"]["host_gen"].clone();
        let mut row = Value::Null;
        for step in story["steps"].as_array().unwrap() {
            let cmd = &step["cmd"];
            let body = &step["body"];
            if !step["before"].is_null() {
                op = lifecycle_record(&step["before"]);
            }
            if let Some(g) = cmd.get("gen") {
                generation = g.clone();
            }
            if let Some(q) = cmd.get("row") {
                row = if q.is_null() {
                    Value::Null
                } else {
                    json!({"status":q,"claimed_at":cmd["claimed_at"],"trace_id":cmd.get("trace").unwrap_or(&op["intent"]["trace_id"])})
                };
            }
            let at = step["at"].as_i64().unwrap();
            let owner = cmd["principal"].as_str().unwrap_or("device_one");
            let minted = step["written"]["owner_nonce"].as_str().unwrap_or("unused");
            let trace = step["written"]["intent"]["trace_id"]
                .as_str()
                .unwrap_or("unused");
            let w = World {
                now: at,
                host: "host-test",
                fence: None,
                queue: &row,
                generation: &generation,
                nonce: minted,
                trace,
            };
            let mut effects = Effects::default();
            let before = op.clone();
            let action = cmd["do"].as_str().unwrap_or("claim");
            let (status, answer) = match action {
                "claim" => rules::claim(&mut op, owner, body, &w, &mut effects),
                "greet" => rules::greet(
                    &mut op,
                    Greeting {
                        principal: owner,
                        token: &body["token"],
                        epoch: &body["epoch"],
                        message: cmd["msg"].as_str().unwrap_or("hi"),
                        trace: cmd["trace"].as_str().unwrap_or("trace-hi"),
                        session: cmd["session"].as_str().unwrap_or("guard-session"),
                        call: cmd["call_id"].as_str().unwrap_or("call-one"),
                    },
                    &w,
                    &mut effects,
                ),
                "retire" => (
                    200,
                    json!(rules::retire(
                        &mut op,
                        cmd["reason"].as_str().unwrap_or("call_ended"),
                        &w,
                        &mut effects
                    )),
                ),
                "admitted" => {
                    rules::greeting_admitted(
                        &mut op,
                        cmd["msg"].as_str().unwrap_or("hi"),
                        cmd["ok"] == true,
                        &w,
                        &mut effects,
                    );
                    (200, Value::Null)
                }
                _ => unreachable!(),
            };
            assert_eq!(json!(status), step["status"], "{} {cmd}", story["name"]);
            assert_eq!(answer, step["answer"], "{} {cmd}", story["name"]);
            if effects.dirty && action != "retire" {
                op["updated_at"] = json!(at);
            }
            let mut expected = before;
            for (k, v) in lifecycle_record(&step["written"]).as_object().unwrap() {
                expected[k] = v.clone();
            }
            assert_eq!(op, expected, "{} {cmd}", story["name"]);
            let changes = step["changes"].as_array().unwrap();
            let has = |kind: &str| changes.iter().any(|c| c[0] == kind);
            assert_eq!(effects.admit, has("admit"));
            assert_eq!(effects.withdraw, has("withdraw"));
            assert_eq!(effects.announce, has("announce"));
            if effects.withdraw {
                row["status"] = json!("withdrawn");
            }
            count += 1;
        }
    }
    for row in fixture["headers"].as_array().unwrap() {
        let op =
            json!({"reason":row["reason"],"silence_ms":row["ms"],"intent":{"cause":row["cause"]}});
        assert_eq!(json!(rules::header(&op)), row["text"]);
    }
    for row in fixture["uuids"].as_array().unwrap() {
        assert_eq!(
            json!(rules::uuid(row["raw"].as_str().unwrap().as_bytes())),
            row["canonical"]
        );
    }
    assert_eq!(count, 421);
}

#[test]
fn opening_dispatch_requires_positive_evidence_for_a_release_or_a_launch() {
    use rules::{Dispatch, Gate};
    let fixture: Value =
        serde_json::from_str(include_str!("fixtures/listen-first-vectors.json")).unwrap();
    let mut count = 0;
    for case in fixture["gates"].as_array().unwrap() {
        let mut op = case["before"].clone();
        let status = &case["status"];
        let row = if status.is_null() {
            Value::Null
        } else {
            json!({"foreign":case["breach"]=="foreign","status":status,"trace_id":"trace","claimed_at":if status=="claimed"{json!(100)}else{Value::Null}})
        };
        let mut generation = op["fence"]["host_gen"].clone();
        if case["breach"] == "reboot" {
            generation["boot"] = json!("other");
        }
        let world = World {
            now: 3000,
            host: "test",
            generation: &generation,
            queue: &row,
            trace: "unused",
            nonce: "unused",
            fence: if case["breach"] == "retired" {
                Some("call_ended")
            } else if case["breach"] == "stopped_after_answer" {
                Some("stopped_after_answer")
            } else {
                None
            },
        };
        let mut fx = Dispatch::default();
        let action = match case["action"].as_str().unwrap() {
            "claim" => Gate::Claim,
            "release" => Gate::ReleaseUnsent,
            "parked" => Gate::Parked,
            _ => Gate::FailedAfterLaunch,
        };
        let result = rules::dispatch(
            action,
            &mut op,
            "call-open:ring:1",
            "trace",
            &world,
            &mut fx,
        );
        assert_eq!(result, case["result"], "{case}");
        assert_eq!(op, case["after"], "{case}");
        for (flag, name) in [
            (fx.opening.withdraw, "withdraw"),
            (fx.opening.unknown, "unknown"),
            (fx.release, "release"),
            (fx.withdraw_claimed, "withdraw_claimed"),
            (fx.claim, "claim"),
            (fx.launched_unknown, "launched_unknown"),
        ] {
            assert_eq!(
                flag,
                case["effects"].as_array().unwrap().contains(&json!(name)),
                "{name}: {case}"
            );
        }
        count += 1;
    }
    assert_eq!(count, 424);
}

#[test]
fn admission_checks_the_stored_channel_and_both_traces() {
    let fixture: Value =
        serde_json::from_str(include_str!("fixtures/listen-first-vectors.json")).unwrap();
    let mut counts = [0usize; 3];
    for case in fixture["admissions"].as_array().unwrap() {
        let mut op = case["before"].clone();
        let mut fx = rules::Effects::default();
        let w = World {
            now: 3000,
            host: "test",
            generation: &case["generation"],
            queue: &case["row"],
            fence: (case["guard"] == "stopped_after_answer").then_some("stopped_after_answer"),
            nonce: "",
            trace: "",
        };
        let result = rules::admit(&mut op, &case["existing"], false, &w, &mut fx);
        assert_eq!(result, case["result"], "{case}");
        assert_eq!(op, lifecycle_record(&case["after"]), "{case}");
        assert_eq!(
            result == "admitted",
            case["effects"] == json!(["create", "record", "enqueue", "allow_paused"])
        );
        counts[match result {
            "admitted" => 0,
            "adopted" => 1,
            "fenced" => 2,
            _ => unreachable!(),
        }] += 1;
    }
    assert_eq!(counts, [2, 2, 236]);
}

#[test]
fn the_admitted_agent_and_turn_have_the_references_opening_audio_tag_and_fence() {
    use clarp_kernel::rings::binding::{Binding, Kind, key};
    let fixture: Value =
        serde_json::from_str(include_str!("fixtures/listen-first-vectors.json")).unwrap();
    let clips = fixture["clips"].as_array().unwrap();
    assert_eq!(clips.len(), 120);
    for case in clips {
        let op = &case["op"];
        let agent = op["agent_id"].as_str().unwrap();
        let epoch = op["epoch"].as_u64().unwrap();
        let bindings = [
            Binding {
                turn_id: 1,
                kind: Kind::Initiative,
                admission_id: op["intent"]["admission_id"].as_str().unwrap().into(),
                epoch,
            },
            Binding {
                turn_id: 2,
                kind: Kind::GreetingReply,
                admission_id: op["greeting"]["client_msg_id"].as_str().unwrap().into(),
                epoch,
            },
        ];
        let index: std::collections::BTreeMap<_, _> = bindings
            .iter()
            .map(|binding| (key(agent, binding.turn_id).unwrap(), binding.audio(op)))
            .collect();
        let found = key(
            case["agent"].as_str().unwrap(),
            case["turn"].as_u64().unwrap(),
        )
        .and_then(|key| index.get(&key));
        assert_eq!(
            found.map_or(&Value::Null, |result| &result["tag"]),
            &case["tag"],
            "{case}"
        );
        assert_eq!(
            found.is_some_and(|result| result["skip"] == true),
            case["skip"].as_bool().unwrap(),
            "{case}"
        );
    }
}
