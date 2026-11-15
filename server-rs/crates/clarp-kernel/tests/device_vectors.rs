//! The register of push devices against the Python Host's.
//!
//! `fixtures/device-vectors.json` is written by `server-rs/tools/device-vectors.py` from
//! `server/lib/apns.py`: sequences of registrations and the small writes beside them, each
//! with what it answered and the whole register as it then stood. The same calls must leave
//! the same register here.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_kernel::devices::{PushBook, PushRow, Refused, Registration};
use serde_json::{Value, json};

fn text<'a>(args: &'a Value, key: &str) -> &'a str {
    args[key].as_str().unwrap_or("")
}

/// A list of objects in an order that does not depend on how ties were broken.
fn sorted(mut rows: Vec<Value>) -> Vec<Value> {
    rows.sort_by_key(Value::to_string);
    rows
}

#[test]
fn the_register_of_push_devices_is_kept_as_the_python_host_keeps_it() {
    let vectors: Value =
        serde_json::from_str(include_str!("fixtures/device-vectors.json")).unwrap();
    let phone = vectors["phone"].as_str().unwrap();
    let mut wrong: Vec<String> = Vec::new();
    let (mut calls, mut refusals) = (0, 0);
    for scenario in vectors["scenarios"].as_array().unwrap() {
        let name = scenario["name"].as_str().unwrap();
        let mut book = PushBook::new();
        for (number, call) in scenario["calls"].as_array().unwrap().iter().enumerate() {
            let (op, args, at) = (
                call["op"].as_str().unwrap(),
                &call["args"],
                call["at"].as_i64().unwrap(),
            );
            let paired: Vec<&str> = call["paired"]
                .as_array()
                .unwrap()
                .iter()
                .map(|device| device.as_str().unwrap())
                .collect();
            // As on the Python Host: what is not a device may always receive.
            let can_receive = |owner: &str| {
                owner.is_empty() || !owner.starts_with("device_") || paired.contains(&owner)
            };
            let modes: Vec<String> = args["conversation_modes"]
                .as_array()
                .map(|modes| {
                    modes
                        .iter()
                        .map(|mode| mode.as_str().unwrap().to_string())
                        .collect()
                })
                .unwrap_or_default();
            let done: Result<(Vec<PushRow>, Value), Refused> = match op {
                "register" => book
                    .register(
                        &Registration {
                            token: text(args, "token"),
                            session: text(args, "session"),
                            environment: text(args, "environment"),
                            platform: text(args, "platform"),
                            base_url: text(args, "base_url"),
                            push_grant: text(args, "push_grant"),
                            owner: text(args, "owner"),
                            provider: text(args, "provider"),
                            modes: &modes,
                        },
                        &can_receive,
                        at,
                    )
                    .map(|rows| (rows, Value::Null)),
                "disable" => book
                    .disable(
                        text(args, "token"),
                        text(args, "owner"),
                        args["only_owner"].as_str(),
                        at,
                    )
                    .map(|rows| {
                        let count = rows.len();
                        (rows, json!(count))
                    }),
                "dead" => Ok((
                    book.dead(text(args, "token"), at).into_iter().collect(),
                    Value::Null,
                )),
                "grant_revoked" => Ok((
                    book.grant_revoked(text(args, "token"))
                        .into_iter()
                        .collect(),
                    Value::Null,
                )),
                "pushed" => Ok((
                    book.pushed(text(args, "token"), at).into_iter().collect(),
                    Value::Null,
                )),
                other => panic!("the vectors name a call this test does not know: {other}"),
            };
            calls += 1;
            let ours = match done {
                Ok((rows, said)) => {
                    book.apply(&rows);
                    json!({ "ok": said })
                }
                Err(Refused::OwnedElsewhere) => json!({ "refused": "owned_elsewhere" }),
                Err(Refused::Invalid(why)) => json!({ "refused": "invalid", "why": why }),
                Err(Refused::Value(why)) => json!({ "refused": "value", "why": why }),
            };
            if ours.get("refused").is_some() {
                refusals += 1;
            }
            let context = format!("{name}, call {number} ({op})");
            let mut found: Vec<String> = Vec::new();
            if ours != call["answer"] {
                found.push(format!(
                    "answered {ours}, the Python Host {}",
                    call["answer"]
                ));
            }
            let register: Vec<Value> = book
                .rows()
                .map(|row| serde_json::to_value(row).unwrap())
                .collect();
            if json!(register) != call["register"] {
                found.push(format!(
                    "the register is\n      {}\n    the Python Host's\n      {}",
                    json!(register),
                    call["register"]
                ));
            }
            let active: Vec<Value> = book
                .active()
                .map(|row| {
                    json!({
                        "token": row.token, "session": row.session, "environment": row.environment,
                        "base_url": row.base_url, "push_grant": row.push_grant,
                        "provider": row.provider, "owner": row.principal,
                    })
                })
                .collect();
            if sorted(active) != sorted(call["active"].as_array().unwrap().clone()) {
                found.push("the rows something is sent to differ".to_string());
            }
            for (who, theirs) in [
                (None, &call["shown"]),
                (Some(phone), &call["shown_to_phone"]),
            ] {
                let ours = book.shown(who, &can_receive);
                if sorted(ours.clone()) != sorted(theirs.as_array().unwrap().clone()) {
                    found.push(format!(
                        "shown to {who:?}: {}, the Python Host shows {theirs}",
                        json!(ours)
                    ));
                }
            }
            if !found.is_empty() && wrong.len() < 6 {
                wrong.push(format!("{context}:\n    {}", found.join("\n    ")));
            }
        }
    }
    assert!(wrong.is_empty(), "calls differ:\n{}", wrong.join("\n"));
    assert!(calls >= 45, "only {calls} calls were compared");
    assert!(refusals >= 12, "only {refusals} refusals were compared");
}
