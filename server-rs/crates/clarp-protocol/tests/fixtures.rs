//! The types read every core event the contract's fixtures contain, and write nothing the
//! fixture did not say.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::path::{Path, PathBuf};

use clarp_protocol::events::{AgentStateKind, Envelope, Event};
use serde_json::Value;

const CORE_TYPES: [&str; 11] = [
    "transcript-updated",
    "agent-state",
    "agent-activity",
    "agent-roster",
    "agent-focus",
    "queue-updated",
    "user-notification",
    "audio",
    "tts-error",
    "server-version",
    "remote-action",
];

fn fixtures_dir() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../contract/fixtures")
}

fn json_files(dir: &Path, out: &mut Vec<PathBuf>) {
    for entry in std::fs::read_dir(dir).unwrap_or_else(|e| panic!("{}: {e}", dir.display())) {
        let path = entry.unwrap().path();
        if path.is_dir() {
            json_files(&path, out);
        } else if path.extension().is_some_and(|ext| ext == "json") {
            out.push(path);
        }
    }
}

/// The events in the fixtures: a step's `sse` value is one event as the Host sent it, and a
/// `clip` with a `url` is an `audio` event (one fixture has a clip with no URL at all, to test a
/// client's precedence rules; that is not an event the Host can send).
fn core_events(value: &Value, out: &mut Vec<Value>) {
    match value {
        Value::Object(map) => {
            for (key, child) in map {
                let is_event = child
                    .get("type")
                    .and_then(Value::as_str)
                    .is_some_and(|t| CORE_TYPES.contains(&t));
                if is_event && (key == "sse" || (key == "clip" && child.get("url").is_some())) {
                    out.push(child.clone());
                }
                core_events(child, out);
            }
        }
        Value::Array(items) => items.iter().for_each(|item| core_events(item, out)),
        _ => {}
    }
}

#[test]
fn every_core_event_in_the_fixtures_reads_and_writes_back_only_what_it_said() {
    let mut files = Vec::new();
    json_files(&fixtures_dir(), &mut files);
    assert!(
        !files.is_empty(),
        "the contract's fixtures are beside server-rs"
    );
    let mut events = Vec::new();
    for file in &files {
        let text = std::fs::read_to_string(file).unwrap();
        let document: Value =
            serde_json::from_str(&text).unwrap_or_else(|e| panic!("{}: {e}", file.display()));
        core_events(&document, &mut events);
    }
    assert!(
        events.len() >= 10,
        "the fixtures hold core events, found {}",
        events.len()
    );
    for original in events {
        let envelope: Envelope =
            serde_json::from_value(original.clone()).unwrap_or_else(|e| panic!("{original}: {e}"));
        assert!(
            envelope.event != Event::Unknown,
            "a core event must not read as unknown: {original}"
        );
        let written = serde_json::to_value(&envelope).unwrap();
        let (written, original) = (written.as_object().unwrap(), original.as_object().unwrap());
        assert_eq!(written.get("type"), original.get("type"));
        for (key, value) in written {
            assert_eq!(
                Some(value),
                original.get(key),
                "field {key} must be written as it was read"
            );
        }
    }
}

#[test]
fn an_event_from_a_newer_host_is_unknown_and_an_unknown_field_is_ignored() {
    let newer: Envelope =
        serde_json::from_str(r#"{"type":"something-new","session":"s","x":1}"#).unwrap();
    assert_eq!(newer.event, Event::Unknown);
    let extra: Envelope = serde_json::from_str(
        r#"{"type":"server-version","version":"1.2","from_the_future":true,"event_id":7}"#,
    )
    .unwrap();
    assert_eq!(extra.event_id, Some(7));
    assert!(matches!(extra.event, Event::ServerVersion(ref v) if v.version == "1.2"));
}

#[test]
fn an_agent_state_writes_the_wire_names() {
    let text = r#"{"type":"agent-state","session":"maximus-6c37","agent_id":"a1","kind":"thinking","ts":5}"#;
    let envelope: Envelope = serde_json::from_str(text).unwrap();
    let Event::AgentState(ref state) = envelope.event else {
        panic!("an agent-state reads as one")
    };
    assert_eq!(state.kind, AgentStateKind::Thinking);
    assert!(state.kind.is_busy());
    assert!(!AgentStateKind::Done.is_busy());
    let written: Value = serde_json::to_value(&envelope).unwrap();
    assert_eq!(
        written,
        serde_json::from_str::<Value>(text).unwrap(),
        "what was read is what is written, no more"
    );
    let newer: Envelope = serde_json::from_str(
        r#"{"type":"agent-state","session":"s","agent_id":"a","kind":"levitating"}"#,
    )
    .unwrap();
    assert!(
        matches!(newer.event, Event::AgentState(ref s) if s.kind == AgentStateKind::Other("levitating".to_string())),
        "a newer kind is kept as it was sent"
    );
    assert_eq!(
        serde_json::to_value(&newer).unwrap()["kind"],
        "levitating",
        "and written back unchanged"
    );
}
