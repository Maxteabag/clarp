#![allow(clippy::unwrap_used)]
use clarp_kernel::rings::opening::{self as rules, Effects, Greeting, World};
use serde_json::{Value, json};

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
                op = step["before"].clone();
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
            for (k, v) in step["written"].as_object().unwrap() {
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
    for row in fixture["clips"].as_array().unwrap() {
        let result = rules::clip(&row["op"], row["trace"].as_str().unwrap());
        let (tag, skip) = result.unwrap_or((Value::Null, false));
        assert_eq!(
            (tag, json!(skip)),
            (row["tag"].clone(), row["skip"].clone())
        );
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
        assert_eq!(op, case["after"], "{case}");
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
