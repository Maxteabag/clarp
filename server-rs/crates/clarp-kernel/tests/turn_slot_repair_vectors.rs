//! Who stops or repairs an agent and why, held to the Python Host's own functions
//! (`tests/fixtures/turn-slot-repair-vectors.json`, written by `server-rs/tools/turn-slot-repair-vectors.py`, which
//! drives `Handler._control_actor`, `_handle_stop` and `_handle_repair_slot` on a handler that answers into a list,
//! and `activity._control_provenance`). The tokens differ: a turn's token there is a row of a table, here a signed
//! text; what is held is whose turn it names (an agent's, nobody's), and the Host answers a lookup that cannot be
//! made, as there (its Host test holds it).

#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_kernel::control::{self, Control, REPAIRED, STOPPED};
use serde_json::{Map, Value};

fn vectors() -> Value {
    serde_json::from_str(include_str!("fixtures/turn-slot-repair-vectors.json")).unwrap()
}

/// Who acts as the Host reads it: the claim of the body, the agent whose turn presented its
/// token, a paired device.
fn acting(data: &Value, principal: &str, turn: Option<&str>) -> (String, bool) {
    let claimed = control::claimed(data.get("actor"));
    let device = !principal.is_empty() && principal != "administrator";
    control::acting(&claimed, turn, device)
}

fn object(value: &Value) -> Map<String, Value> {
    value.as_object().cloned().unwrap_or_default()
}

#[test]
fn who_acts_is_read_as_the_python_host_reads_it() {
    let vectors = vectors();
    let cases = vectors["actors"].as_array().unwrap();
    let mut held = 0;
    for case in cases {
        let turn = case["turn"].as_str();
        if turn == Some("failing") {
            // A lookup that cannot be made is no answer: the Host says so, 503 (its Host test).
            assert_eq!(case["unavailable"], true);
            continue;
        }
        // The token of the agent `desk`'s turn names it; one never given, or none, names nobody.
        let turn_of = (turn == Some("desk")).then_some("desk");
        let principal = case["principal"].as_str().unwrap();
        let (actor, verified) = acting(&case["data"], principal, turn_of);
        let theirs = &case["actor"];
        assert_eq!(
            (json(&actor), json(&verified)),
            (theirs[0].clone(), theirs[1].clone()),
            "{case}"
        );
        held += 1;
    }
    assert_eq!((cases.len(), held), (58, 57));
}

fn json<T: serde::Serialize>(value: &T) -> Value {
    serde_json::to_value(value).unwrap()
}

#[test]
fn a_repair_says_who_and_why_and_leaves_a_note_as_the_python_host_does() {
    let vectors = vectors();
    let cases = vectors["repairs"].as_array().unwrap();
    let mut answered = 0;
    for case in cases {
        let said = &case["said"][0];
        if said[0] != 200 {
            continue;
        }
        let answer = &said[1];
        let data = &case["data"];
        let turn_of = (case["turn"] == "omar").then_some("omar");
        let (actor, verified) = acting(data, "administrator", turn_of);
        let reason = control::reason(data.get("reason"));
        assert_eq!(answer["repair_actor"], json(&actor), "{case}");
        assert_eq!(answer["repair_actor_verified"], json(&verified), "{case}");
        assert_eq!(answer["repair_reason"], json(&reason), "{case}");
        // Nothing to give back: no turn is named, and no queue was started.
        assert_eq!(
            (&answer["released_trace"], &answer["queue_recovered"]),
            (&json(&""), &json(&0))
        );
        // The note it keeps on the state, and what the state's activity says of it.
        let control = Control {
            actor,
            verified,
            reason,
        };
        let marked = control::marked(REPAIRED, &control, "");
        let state = &case["state"];
        for (key, value) in &marked {
            assert_eq!(&state["detail"][key], value, "{case}: {key}");
        }
        assert_eq!(
            Value::Object(control::provenance(&marked)),
            state["provenance"],
            "{case}"
        );
        answered += 1;
    }
    assert_eq!(answered, 7);
}

#[test]
fn a_stop_says_who_and_why_and_pauses_the_goals_in_those_words() {
    let vectors = vectors();
    let cases = vectors["stops"].as_array().unwrap();
    for case in cases {
        let answer = &case["said"][0][1];
        let data = &case["data"];
        let turn_of = (case["turn"] == "omar").then_some("omar");
        let (actor, verified) = acting(data, "administrator", turn_of);
        let reason = control::reason(data.get("reason"));
        let control = Control {
            actor,
            verified,
            reason,
        };
        assert_eq!(answer["stop_actor"], json(&control.actor), "{case}");
        assert_eq!(
            answer["stop_actor_verified"],
            json(&control.verified),
            "{case}"
        );
        assert_eq!(answer["stop_reason"], json(&control.reason), "{case}");
        assert_eq!(
            case["goals_paused_with"][0],
            json(&control::paused_by(&control)),
            "{case}"
        );
        let marked = control::marked(STOPPED, &control, "");
        let state = &case["state"];
        assert_eq!(state["kind"], "interrupted");
        for (key, value) in &marked {
            assert_eq!(&state["detail"][key], value, "{case}: {key}");
        }
        assert_eq!(
            Value::Object(control::provenance(&marked)),
            state["provenance"],
            "{case}"
        );
    }
    assert_eq!(cases.len(), 9);
}

#[test]
fn a_states_activity_says_who_acted_as_the_python_host_says_it() {
    let vectors = vectors();
    let cases = vectors["provenance"].as_array().unwrap();
    for case in cases {
        let ours = control::provenance(&object(&case["detail"]));
        assert_eq!(Value::Object(ours), case["provenance"], "{case}");
    }
    assert_eq!(cases.len(), 8);
    // The activity's own keys for it come last, in this order.
    let fields: Vec<&str> = vectors["activity_fields"]
        .as_array()
        .unwrap()
        .iter()
        .map(|field| field.as_str().unwrap())
        .collect();
    assert_eq!(
        &fields[fields.len() - 7..],
        [
            "stop_actor",
            "stop_actor_verified",
            "stop_reason",
            "repair_actor",
            "repair_actor_verified",
            "repair_reason",
            "released_trace"
        ]
    );
}

#[test]
fn a_stop_is_the_users_unless_an_agents_own_turn_made_it() {
    let control = |actor: &str, verified: bool| Control {
        actor: actor.to_string(),
        verified,
        reason: String::new(),
    };
    // The Python Host's `fence_user_receipts`: `not (actor_verified and actor.startswith("agent:"))`.
    assert!(control("user", true).is_the_users());
    assert!(control("user", false).is_the_users());
    assert!(
        control("agent:omar", false).is_the_users(),
        "a claim of an agent is no agent's"
    );
    assert!(!control("agent:omar", true).is_the_users());
    assert!(control("7", false).is_the_users());
}
