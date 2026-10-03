//! The recorded live streams (`contract/live/*.json`) through the Rust
//! reducer, asserting what `tests/contract/test_live_fixtures.py` asserts of
//! the Host's reference reducer: the same effects, held lseq, activity, turn
//! and items (ids in ordinal order, each a superset of the expected fields).

use std::path::{Path, PathBuf};

use clarp_core::live::{FETCH_LIVE, LiveView};
use serde_json::{Value, json};

fn fixtures() -> Vec<(String, Value)> {
    let dir: PathBuf = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../contract/live");
    let mut files: Vec<_> = std::fs::read_dir(&dir)
        .expect("contract/live exists")
        .filter_map(Result::ok)
        .map(|e| e.path())
        .filter(|p| p.extension().is_some_and(|x| x == "json"))
        .collect();
    files.sort();
    files
        .into_iter()
        .map(|p| {
            let name = p.file_stem().unwrap().to_string_lossy().into_owned();
            (name, serde_json::from_str(&std::fs::read_to_string(&p).unwrap()).expect("fixture is JSON"))
        })
        .collect()
}

fn fixture(name: &str) -> Value {
    fixtures().into_iter().find(|(n, _)| n == name).map(|(_, b)| b).unwrap_or_else(|| panic!("no fixture {name}"))
}

/// `_subset` of the Python test: every expected key present, scalars and
/// arrays equal.
fn subset(actual: &Value, expected: &Value, path: &str) {
    match expected {
        Value::Object(map) => {
            let actual = actual.as_object().unwrap_or_else(|| panic!("{path}: expected an object, got {actual}"));
            for (key, value) in map {
                let found = actual.get(key).unwrap_or_else(|| panic!("{path}.{key} missing"));
                subset(found, value, &format!("{path}.{key}"));
            }
        }
        _ => assert_eq!(actual, expected, "{path}"),
    }
}

/// Feeds a fixture's steps through a fresh view: its effects and the view.
fn replay(body: &Value) -> (Vec<String>, LiveView) {
    let mut view = LiveView::new();
    let mut effects = Vec::new();
    for step in body["steps"].as_array().expect("steps") {
        if let Some(snapshot) = step.get("snapshot") {
            view.apply_snapshot(snapshot.as_object().unwrap());
        } else {
            effects.extend(view.apply_event(step["event"].as_object().unwrap()).into_iter().map(str::to_owned));
        }
    }
    (effects, view)
}

#[test]
fn every_client_fixture_exists() {
    let names: Vec<String> = fixtures().into_iter().map(|(n, _)| n).collect();
    for name in ["turn-full", "gap-needs-snapshot", "gap-recovers-from-snapshot"] {
        assert!(names.iter().any(|n| n == name), "contract/live/{name}.json");
    }
}

#[test]
fn every_fixture_reaches_the_expected_state() {
    for (name, body) in fixtures() {
        assert_eq!(body["area"], "live", "{name}");
        let (effects, view) = replay(&body);
        let expect = &body["expect"];
        assert_eq!(json!(effects), expect["effects"], "{name}: effects");
        assert_eq!(view.lseq(), expect["lseq"].as_i64(), "{name}: lseq");
        subset(&Value::Object(view.activity().clone()), &expect["activity"], &format!("{name} $.activity"));
        let turn = view.turn().cloned().map_or(Value::Null, Value::Object);
        subset(&turn, &expect["turn"], &format!("{name} $.turn"));
        let items = view.items();
        let ids: Vec<&str> = items.iter().map(|i| i["id"].as_str().unwrap_or_default()).collect();
        let expected: Vec<&str> = expect["items"].as_array().unwrap().iter().map(|i| i["id"].as_str().unwrap()).collect();
        assert_eq!(ids, expected, "{name}: items in ordinal order");
        for (actual, wanted) in items.iter().zip(expect["items"].as_array().unwrap()) {
            subset(&Value::Object((*actual).clone()), wanted, &format!("{name} $.items[{}]", wanted["id"]));
        }
        for item in &items {
            for key in ["id", "conv", "turn_id", "kind", "status", "ordinal", "started_at_ms", "ended_at_ms", "rev"] {
                assert!(item.contains_key(key), "{name}: item {} lacks {key}", item["id"]);
            }
        }
    }
}

#[test]
fn a_gap_asks_for_the_snapshot_once_and_ignores_events_until_it_lands() {
    let (effects, mut view) = replay(&fixture("gap-needs-snapshot"));
    assert_eq!(effects, [FETCH_LIVE]);
    assert!(view.awaiting_snapshot());
    // More events while the fetch is outstanding: ignored, no second fetch.
    let next = json!({"type": "live", "conv": "conv-1", "epoch": "boot-a", "lseq": 5, "ops": []});
    assert!(view.apply_event(next.as_object().unwrap()).is_empty());
    assert_eq!(view.lseq(), Some(2));
}

#[test]
fn nothing_held_asks_for_the_snapshot_first() {
    let mut view = LiveView::new();
    let event = json!({"type": "live", "conv": "c", "epoch": "e", "lseq": 7, "ops": [{"op": "status", "conv": "c", "activity": {"state": "thinking"}}]});
    assert_eq!(view.apply_event(event.as_object().unwrap()), [FETCH_LIVE]);
    assert_eq!(view.activity()["state"], "idle", "not applied before the snapshot");
}

#[test]
fn terminal_statuses_are_final_and_unknown_ops_kinds_and_fields_are_ignored() {
    let body = fixture("turn-full");
    let (_, mut view) = replay(&body);
    let lseq = view.lseq().unwrap();
    let event = json!({"type": "live", "conv": "conv-1", "epoch": "boot-a", "lseq": lseq + 1, "ops": [
        {"op": "teleport", "conv": "conv-1", "id": "cl:toolu_03"},
        {"op": "upsert", "conv": "conv-1", "id": "x:new", "kind": "hologram", "rev": 1,
         "item": {"id": "x:new", "kind": "hologram", "ordinal": 99, "status": "running", "sparkle": true}},
        {"op": "upsert", "conv": "conv-1", "id": "cl:toolu_03", "kind": "tool", "rev": 6, "item": {"colour": "teal"}},
    ]});
    assert!(view.apply_event(event.as_object().unwrap()).is_empty());
    assert_eq!(view.lseq(), Some(lseq + 1));
    let tool = view.item("cl:toolu_03").expect("still there");
    assert_eq!(tool["status"], "failed", "a terminal status stays");
    assert_eq!(tool["rev"], 6);
    // A late `done` cannot reopen it either: a running status on a settled item is dropped.
    let reopen = json!({"type": "live", "conv": "conv-1", "epoch": "boot-a", "lseq": lseq + 2, "ops": [
        {"op": "upsert", "conv": "conv-1", "id": "cl:toolu_03", "kind": "tool", "rev": 7, "item": {"status": "running"}},
    ]});
    view.apply_event(reopen.as_object().unwrap());
    assert_eq!(view.item("cl:toolu_03").unwrap()["status"], "failed");
}

#[test]
fn the_output_tail_keeps_the_last_fifty_lines() {
    let (_, view) = replay(&fixture("turn-full"));
    let output = &view.item("cl:toolu_03").unwrap()["tool"]["output"];
    assert_eq!(output["tail"].as_array().unwrap().len(), 50);
    assert_eq!(output["tail"][49], "line 812");
    assert_eq!(output["total_lines"], 812);
    assert_eq!(output["truncated"], true);
}

#[test]
fn a_revision_out_of_step_asks_for_the_snapshot() {
    let (_, mut view) = replay(&fixture("turn-full"));
    let lseq = view.lseq().unwrap();
    let event = json!({"type": "live", "conv": "conv-1", "epoch": "boot-a", "lseq": lseq + 1, "ops": [
        {"op": "append", "conv": "conv-1", "id": "cl:msg_02:0", "kind": "message", "rev": 9, "field": "text", "chunk": "x"},
    ]});
    assert_eq!(view.apply_event(event.as_object().unwrap()), [FETCH_LIVE]);
    assert_eq!(view.lseq(), Some(lseq), "the event is not half-applied");
    assert_eq!(view.item("cl:msg_02:0").unwrap()["text"], "The failure came from an off-by-one ");
}
