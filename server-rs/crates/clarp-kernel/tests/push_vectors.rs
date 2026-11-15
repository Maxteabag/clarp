//! What a phone is sent, against the Python Host's.
//!
//! `fixtures/push-vectors.json` is written by `server-rs/tools/push-vectors.py` from
//! `server/lib/apns.py`: the payload of a push for a finished turn and for a request to the
//! user, for many inputs, with the id each collapses under. The same inputs must build the
//! same payloads here.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_kernel::push::{Asked, Told, collapse_id, decision, needs_interruption, turn_done};
use serde_json::Value;

/// A field that is a text, or nothing.
fn text<'a>(vector: &'a Value, key: &str) -> &'a str {
    vector[key].as_str().unwrap_or("")
}

/// What Python takes for true.
fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(flag) => *flag,
        Value::Number(number) => number.as_f64() != Some(0.0),
        Value::String(text) => !text.is_empty(),
        Value::Array(items) => !items.is_empty(),
        Value::Object(fields) => !fields.is_empty(),
    }
}

#[test]
fn a_push_says_what_the_python_hosts_says() {
    let vectors: Value = serde_json::from_str(include_str!("fixtures/push-vectors.json")).unwrap();
    let mut wrong: Vec<String> = Vec::new();
    let mut compared = 0;
    let mut differ = |what: &str, vector: &Value, ours: &Value, id: String| {
        compared += 1;
        if (ours != &vector["payload"] || id != text(vector, "collapse_id")) && wrong.len() < 8 {
            wrong.push(format!(
                "{what}: here {ours} under {id:?}, the Python Host's {} under {:?}",
                vector["payload"], vector["collapse_id"]
            ));
        }
    };

    for vector in vectors["turns"].as_array().unwrap() {
        let ours = turn_done(&Told {
            persona: text(vector, "persona"),
            session: text(vector, "session"),
            body: text(vector, "body"),
            avatar_url: text(vector, "avatar_url"),
            avatar_custom: vector["avatar_custom"] == true,
            notification_id: text(vector, "notification_id"),
            server_instance_id: text(vector, "server_instance_id"),
            needs_response: vector["needs_response"] == true,
        });
        let id = collapse_id(&ours);
        differ("a finished turn", vector, &ours, id);
    }
    for vector in vectors["decisions"].as_array().unwrap() {
        // The fields the generator held fixed are written down only where it changed them.
        let or = |key: &str, otherwise: &'static str| match &vector[key] {
            Value::String(given) => given.clone(),
            Value::Null if vector.get(key).is_some() => String::new(),
            _ => otherwise.to_string(),
        };
        let (persona, session) = (or("persona", "Vera"), or("session", "vera"));
        let decision_id = or("decision_id", "decision-0123456789abcdef01234567");
        let artifact_id = or("artifact_id", "artifact-0123456789abcdef01234567");
        let avatar_url = or("avatar_url", "https://host.example/a.png");
        let server = or("server_instance_id", "srv-1");
        let ours = decision(&Asked {
            persona: &persona,
            session: &session,
            title: text(vector, "title"),
            question: text(vector, "question"),
            decision_id: &decision_id,
            artifact_id: &artifact_id,
            time_sensitive: vector["time_sensitive"] == true,
            text_input: vector["text_input"] == true,
            revision: vector["revision"].as_i64().unwrap_or(3),
            expires_at: vector["expires_at"].as_i64(),
            avatar_url: &avatar_url,
            avatar_custom: vector
                .get("avatar_custom")
                .is_none_or(|custom| custom == true),
            server_instance_id: &server,
        });
        let id = collapse_id(&ours);
        differ("a request", vector, &ours, id);
    }
    assert!(wrong.is_empty(), "payloads differ:\n{}", wrong.join("\n"));
    assert!(compared >= 300, "only {compared} payloads were compared");

    for vector in vectors["interruptions"].as_array().unwrap() {
        let asked = &vector["decision"];
        assert_eq!(
            needs_interruption(truthy(&asked["blocks_progress"]), text(asked, "urgency")),
            vector["interrupts"] == true,
            "{asked}"
        );
    }
    for vector in vectors["collapse"].as_array().unwrap() {
        assert_eq!(
            collapse_id(&vector["payload"]),
            text(vector, "collapse_id"),
            "{}",
            vector["payload"]
        );
    }
}
