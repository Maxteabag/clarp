//! The gate of the desk of a machine's account seats, against the Python Host's own handlers.
//!
//! `fixtures/hotseat-gate-vectors.json` is written by `server-rs/tools/hotseat-gate-vectors.py`
//! from `server/server.py` (`_hotseat_desk`, `_handle_hotseat_desk`, `_handle_hotseat_switch`,
//! `_handle_hotseat_resume`) through the route registry, with a desk that only writes down what
//! it was asked: whether the desk is turned off, whether it is there, and whether a body is a
//! JSON object; and what the desk is asked when all of it holds.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_kernel::desk::{self, Gate};
use clarp_kernel::python;
use serde_json::{Value, json};

/// A field as the Python handler hands it on: `str(data.get(key) or otherwise)`.
fn said(asked: &Value, key: &str, otherwise: &str) -> String {
    match &asked[key] {
        Value::String(text) if !text.is_empty() => text.clone(),
        other if python::truthy(other) => python::repr(other),
        _ => otherwise.to_string(),
    }
}

#[test]
fn the_doors_of_the_desk_are_gated_as_the_python_host_gates_them() {
    let vectors: Value =
        serde_json::from_str(include_str!("fixtures/hotseat-gate-vectors.json")).unwrap();
    let (mut off, mut unavailable, mut no_object, mut asked, mut no_route) = (0, 0, 0, 0, 0);
    for case in vectors["cases"].as_array().unwrap() {
        let (method, path) = (
            case["method"].as_str().unwrap(),
            case["path"].as_str().unwrap(),
        );
        let answer = &case["answer"];
        let door = matches!(
            path,
            "/hotseat/desk" | "/hotseat/switch" | "/hotseat/resume"
        );
        if !door {
            // No route of this Host's either: the plain 404 of what is not there.
            assert_eq!(
                (answer["status"].as_u64(), answer["body"].as_str()),
                (Some(404), Some("not found")),
                "{path}"
            );
            no_route += 1;
            continue;
        }
        let body = case["body"].as_str().unwrap();
        let sent = (method == "POST").then_some(body.as_bytes());
        let on = case["desk_on"].as_bool().unwrap();
        let there = case["desk_there"].as_bool().unwrap();
        match desk::gate(on, there, sent) {
            Gate::Off => {
                assert_eq!(
                    (
                        answer["status"].as_u64(),
                        answer["type"].as_str(),
                        answer["body"].as_str()
                    ),
                    (Some(404), Some("text/plain"), Some("not found")),
                    "{path} {body}"
                );
                off += 1;
            }
            Gate::Unavailable => {
                assert_eq!(
                    (answer["status"].as_u64(), &answer["body"]),
                    (Some(503), &json!({ "error": desk::UNAVAILABLE })),
                    "{path} {body}"
                );
                unavailable += 1;
            }
            Gate::NoObject => {
                assert_eq!(
                    (answer["status"].as_u64(), &answer["body"]),
                    (Some(400), &json!({ "error": desk::NO_OBJECT })),
                    "{path} {body}"
                );
                no_object += 1;
            }
            Gate::Asked(given) => {
                assert_eq!(answer["status"].as_u64(), Some(200), "{path} {body}");
                let theirs = &case["desk_asked"];
                let ours = match path {
                    "/hotseat/switch" => {
                        json!({ "switch_default": [said(&given, "provider", ""), said(&given, "alias", "")] })
                    }
                    "/hotseat/resume" => {
                        json!({ "resume_work": [said(&given, "id", ""), said(&given, "kind", "clarp"),
                                                                  said(&given, "backend", ""), said(&given, "action", "continue")] })
                    }
                    _ => json!({ "payload": { "refresh": false } }),
                };
                assert_eq!(&ours, theirs, "{path} {body}");
                asked += 1;
            }
        }
    }
    assert_eq!(
        (off, unavailable, no_object, asked, no_route),
        (34, 17, 6, 11, 100)
    );
    assert!(desk::turned_off(Some(&json!(false))));
    for kept_on in [json!(true), json!(0), json!("false"), json!(null)] {
        assert!(!desk::turned_off(Some(&kept_on)), "{kept_on}");
    }
    assert!(!desk::turned_off(None));
}
