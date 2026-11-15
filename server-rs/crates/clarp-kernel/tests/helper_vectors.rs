//! The rules for helper agents against the Python Host's.
//!
//! `fixtures/helper-vectors.json` is written by `server-rs/tools/helper-vectors.py` from
//! `server/lib/policies/helper_state.py`: every state with every event, the roles, who may be
//! whose parent, and which helpers are put away. The same must be decided here.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_kernel::helpers::{
    DEFAULT_ARCHIVE_GRACE_MS, FINISHED, Kept, archive_due, initial_state, marked, next_state,
    parent_refusal, parse_role,
};
use serde_json::{Value, json};

/// A value as the text a request would carry it as: what says nothing is no text.
fn said(value: &Value) -> String {
    match value {
        Value::String(text) => text.clone(),
        Value::Null | Value::Bool(false) => String::new(),
        Value::Number(number) if number.as_f64() == Some(0.0) => String::new(),
        other => other.to_string(),
    }
}

#[test]
fn a_helpers_work_moves_as_the_python_host_moves_it() {
    let vectors: Value =
        serde_json::from_str(include_str!("fixtures/helper-vectors.json")).unwrap();
    let moves = vectors["moves"].as_array().unwrap();
    for vector in moves {
        let ours = next_state(&said(&vector["state"]), &said(&vector["event"]));
        let ours = ours.map_or(
            Value::Null,
            |moved| json!({ "state": moved.state, "completed": moved.completed }),
        );
        assert_eq!(
            ours, vector["to"],
            "{} on {}",
            vector["event"], vector["state"]
        );
    }
    assert!(moves.len() >= 70);
    for (state, event) in vectors["markable"].as_object().unwrap() {
        assert_eq!(marked(state), event.as_str());
    }
    assert_eq!(marked("reported"), None);
    let mut finished = FINISHED.to_vec();
    finished.sort_unstable();
    assert_eq!(json!(finished), vectors["finished"]);
    assert_eq!(
        json!(DEFAULT_ARCHIVE_GRACE_MS),
        vectors["default_archive_grace_ms"]
    );
    for vector in vectors["roles"].as_array().unwrap() {
        let role = parse_role(&said(&vector["given"]));
        assert_eq!(json!(role), vector["role"], "{}", vector["given"]);
        assert_eq!(
            json!(role.and_then(initial_state)),
            vector["initial_state"],
            "{}",
            vector["given"]
        );
    }
    for vector in vectors["parents"].as_array().unwrap() {
        let ancestors: Vec<&str> = vector["ancestors"]
            .as_array()
            .unwrap()
            .iter()
            .map(|ancestor| ancestor.as_str().unwrap())
            .collect();
        let refused = parent_refusal(
            vector["child"].as_str().unwrap(),
            vector["parent"].as_str().unwrap(),
            &ancestors,
        );
        assert_eq!(refused.unwrap_or(""), vector["refusal"], "{vector}");
    }
    let rows: Vec<Kept<'_>> = vectors["rows"]
        .as_array()
        .unwrap()
        .iter()
        .map(|row| Kept {
            agent_id: row["agent_id"].as_str().unwrap(),
            role: row["role"].as_str().unwrap(),
            helper_state: row["helper_state"].as_str().unwrap(),
            archived: !row["archived_at"].is_null(),
            helper_completed_at: row["helper_completed_at"].as_i64(),
        })
        .collect();
    for round in vectors["due"].as_array().unwrap() {
        let grace = round["grace_ms"]
            .as_i64()
            .unwrap_or(DEFAULT_ARCHIVE_GRACE_MS);
        let due = archive_due(&rows, round["now_ms"].as_i64().unwrap(), grace);
        assert_eq!(json!(due), round["due"], "grace {}", round["grace_ms"]);
    }
}
