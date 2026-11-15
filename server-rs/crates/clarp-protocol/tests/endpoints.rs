//! The endpoint types read what the contract's fixtures hold and write back only what they read.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::path::{Path, PathBuf};

use clarp_protocol::log::{LogResponse, Phase, TurnStatus};
use clarp_protocol::server_info::{Compatibility, ServerInfo};
use clarp_protocol::snapshot::Snapshot;
use serde::Serialize;
use serde::de::DeserializeOwned;
use serde_json::Value;

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

/// Every object anywhere in the fixtures for which `is_one` holds.
fn objects_where(is_one: fn(&serde_json::Map<String, Value>) -> bool) -> Vec<Value> {
    fn walk(
        value: &Value,
        is_one: fn(&serde_json::Map<String, Value>) -> bool,
        out: &mut Vec<Value>,
    ) {
        match value {
            Value::Object(map) => {
                if is_one(map) {
                    out.push(value.clone());
                }
                map.values().for_each(|child| walk(child, is_one, out));
            }
            Value::Array(items) => items.iter().for_each(|item| walk(item, is_one, out)),
            _ => {}
        }
    }
    let mut files = Vec::new();
    json_files(
        &Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../contract/fixtures"),
        &mut files,
    );
    let mut out = Vec::new();
    for file in files {
        let document: Value =
            serde_json::from_str(&std::fs::read_to_string(&file).unwrap()).unwrap();
        walk(&document, is_one, &mut out);
    }
    out
}

/// Everything `written` says must be what `original` said: same value under the same key, at
/// every depth. A `null` in the original may be left out.
fn assert_subset(written: &Value, original: &Value, path: &str) {
    match (written, original) {
        (Value::Object(w), Value::Object(o)) => {
            for (key, value) in w {
                let there = o
                    .get(key)
                    .unwrap_or_else(|| panic!("{path}.{key} was written but never read"));
                assert_subset(value, there, &format!("{path}.{key}"));
            }
        }
        (Value::Array(w), Value::Array(o)) => {
            assert_eq!(w.len(), o.len(), "{path} keeps its length");
            for (i, (a, b)) in w.iter().zip(o).enumerate() {
                assert_subset(a, b, &format!("{path}[{i}]"));
            }
        }
        _ => assert_eq!(written, original, "{path} is written as it was read"),
    }
}

fn round_trips<T: DeserializeOwned + Serialize>(what: &str, originals: &[Value]) -> Vec<T> {
    originals
        .iter()
        .map(|original| {
            let parsed: T = serde_json::from_value(original.clone())
                .unwrap_or_else(|e| panic!("{what} {original}: {e}"));
            assert_subset(&serde_json::to_value(&parsed).unwrap(), original, what);
            parsed
        })
        .collect()
}

#[test]
fn every_log_response_in_the_fixtures_round_trips() {
    let originals = objects_where(|map| {
        map.contains_key("turns")
            && map.contains_key("latest_revision")
            && map.contains_key("conversation_id")
    });
    assert!(!originals.is_empty(), "the fixtures hold /log responses");
    let logs: Vec<LogResponse> = round_trips("log", &originals);
    let turns: usize = logs.iter().map(|log| log.turns.len()).sum();
    assert!(turns > 0, "and those responses hold turns");
}

#[test]
fn every_snapshot_in_the_fixtures_round_trips() {
    let originals = objects_where(|map| {
        map.get("agents")
            .and_then(Value::as_array)
            .is_some_and(|agents| {
                !agents.is_empty()
                    && agents.iter().all(|a| {
                        a.get("agent_id").is_some()
                            && a.get("session").is_some()
                            && a.get("persona").is_some()
                            && a.get("backend").is_some()
                            && a.get("cwd").is_some()
                    })
            })
    });
    let snapshots: Vec<Snapshot> = round_trips("snapshot", &originals);
    for snapshot in &snapshots {
        assert!(
            snapshot
                .agents
                .iter()
                .all(|agent| !agent.agent_id.is_empty())
        );
    }
}

#[test]
fn a_log_response_reads_as_documented() {
    let text = r#"{"conversation_id":"c1","turns":[
        {"id":"u-1","role":"user","text":"hello","revision":3,"origin":"user","kind":null},
        {"id":"a-1","role":"assistant","text":"hi","revision":4,"phase":"final","tool_name":null,
         "turn":{"turn_id":"t1","status":"completed","worked_ms":1200,"tool_count":0,"started_at_ms":null}},
        {"id":"a-2","role":"assistant","text":"...","revision":5,"phase":"musing","turn":{"turn_id":"t2","status":"paused"}}],
        "latest_revision":5,"has_more":false,"missing":false,"file":null,"from_a_newer_host":1}"#;
    let log: LogResponse = serde_json::from_str(text).unwrap();
    assert_eq!(log.turns.len(), 3);
    assert_eq!(log.turns[1].phase, Some(Phase::Final));
    assert_eq!(
        log.turns[1].turn.as_ref().unwrap().status,
        TurnStatus::Completed
    );
    assert_eq!(log.turns[1].turn.as_ref().unwrap().started_at_ms, None);
    assert_eq!(
        log.turns[2].phase,
        Some(Phase::Other("musing".to_string())),
        "a phase from a newer Host is kept as sent"
    );
    assert_eq!(
        log.turns[2].turn.as_ref().unwrap().status,
        TurnStatus::Other("paused".to_string())
    );
    assert_subset(
        &serde_json::to_value(&log).unwrap(),
        &serde_json::from_str(text).unwrap(),
        "log",
    );
}

#[test]
fn server_info_says_what_a_host_can_do() {
    let text = r#"{"server_id":"s","name":"elitebook","deployment_mode":"systemd","version":"abc","default_cwd":"/home/p",
        "clarp_version":"0.6.8","min_app_version":"1.0","capabilities":{"version":3,"features":["goal_ledger","live_items"]},
        "contract":{"host":99,"min_ios":1,"features":{"goal_ledger":52,"live_items":60}},
        "client":{"status":"client_outdated","platform":"ios","contract":40,"reason":"update the app"},
        "local_connection":{"enabled":false}}"#;
    let info: ServerInfo = serde_json::from_str(text).unwrap();
    assert!(info.capabilities.has("goal_ledger"));
    assert!(
        !info.capabilities.has("agent_live_fcm"),
        "a feature the Host does not list is one it does not have"
    );
    assert_eq!(info.contract.host, 99);
    assert_eq!(info.contract.features.get("live_items"), Some(&60));
    assert_eq!(
        info.client.as_ref().unwrap().status,
        Compatibility::ClientOutdated
    );
    assert_eq!(
        serde_json::to_value(&info).unwrap(),
        serde_json::from_str::<Value>(text).unwrap()
    );
}
