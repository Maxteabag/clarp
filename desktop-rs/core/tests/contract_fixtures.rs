//! Runs every golden scenario in `contract/fixtures` through the Rust reducers,
//! mirroring `tests/contract/fixtures.test.js` step for step.

use std::path::{Path, PathBuf};

use clarp_core::delivery::{DeliveryLog, DeliveryState};
use clarp_core::protocol::BUSY_STATES;
use clarp_core::sync::{LogMode, SyncState, is_clip_replay, pick_clip_source};
use serde_json::{Value, json};

const NOW: i64 = 1_000_000;

fn fixtures_dir() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../contract/fixtures")
}

fn load_fixtures() -> Vec<(String, Value)> {
    let mut out = Vec::new();
    let mut areas: Vec<_> = std::fs::read_dir(fixtures_dir())
        .expect("contract/fixtures exists")
        .filter_map(Result::ok)
        .filter(|e| e.path().is_dir())
        .collect();
    areas.sort_by_key(|e| e.file_name());
    for area in areas {
        let mut files: Vec<_> = std::fs::read_dir(area.path())
            .unwrap()
            .filter_map(Result::ok)
            .filter(|e| e.path().extension().is_some_and(|x| x == "json"))
            .collect();
        files.sort_by_key(|e| e.file_name());
        for file in files {
            let body: Value = serde_json::from_str(&std::fs::read_to_string(file.path()).unwrap())
                .expect("fixture is JSON");
            let name = format!(
                "{}/{}",
                area.file_name().to_string_lossy(),
                file.file_name().to_string_lossy()
            );
            out.push((name, body));
        }
    }
    out
}

struct Outcome {
    sync: SyncState,
    delivery: DeliveryLog,
    effects: Vec<&'static str>,
    clip_sources: Vec<Value>,
    replays: Vec<bool>,
    stale: Vec<String>,
    optimistic: Vec<Value>,
}

fn run_fixture(name: &str, fixture: &Value) -> Outcome {
    let mut o = Outcome {
        sync: SyncState::new("rachel"),
        delivery: DeliveryLog::default(),
        effects: Vec::new(),
        clip_sources: Vec::new(),
        replays: Vec::new(),
        stale: Vec::new(),
        optimistic: Vec::new(),
    };
    for step in fixture["steps"].as_array().expect("steps") {
        let step = step.as_object().expect("step object");
        let effects = if step.contains_key("open") {
            o.sync.on_open()
        } else if let Some(row) = step.get("snapshot") {
            o.sync.apply_snapshot(row)
        } else if let Some(log) = step.get("log") {
            o.sync.apply_log(&log["response"], LogMode::parse(log["mode"].as_str().unwrap_or("")))
        } else if let Some(event) = step.get("sse") {
            o.sync.on_event(event)
        } else if let Some(kind) = step.get("beginFetch") {
            o.sync.begin_fetch(kind.as_str().unwrap_or(""));
            Vec::new()
        } else if step.contains_key("endFetch") {
            o.sync.end_fetch()
        } else if let Some(send) = step.get("send") {
            let id = send["client_msg_id"].as_str().unwrap_or("");
            let at = send["at"].as_i64().unwrap_or(NOW);
            // A retry re-posts the same id; it must not open a second entry.
            if !send["retry"].as_bool().unwrap_or(false) {
                let session = send["session"].as_str().unwrap_or("rachel");
                o.delivery.record_send(id, session, send["text"].as_str().unwrap_or(""), at);
            }
            o.delivery.mark_state(id, DeliveryState::Sent, "", at + 1);
            Vec::new()
        } else if step.contains_key("confirm") {
            o.delivery.confirm_from_turns(o.sync.current_turns(), NOW + 5000);
            Vec::new()
        } else if let Some(stale) = step.get("stale") {
            let now = stale["now"].as_i64().unwrap_or(NOW);
            let timeout = stale["timeoutMs"].as_i64().unwrap_or(20_000);
            o.stale = o.delivery.stale_sends(now, timeout).iter().map(|e| e.id.clone()).collect();
            Vec::new()
        } else if let Some(clip) = step.get("clip") {
            o.clip_sources.push(pick_clip_source(clip).map_or(Value::Null, Value::from));
            Vec::new()
        } else if let Some(check) = step.get("replayCheck") {
            o.replays.push(is_clip_replay(check.get("clip_id"), check.get("seen")));
            Vec::new()
        } else if let Some(bubble) = step.get("optimistic") {
            // The bubble a client paints before POST /send returns.
            let mut row = json!({"role": "user", "optimistic": true, "revision": 0});
            for (k, v) in bubble.as_object().expect("optimistic object") {
                row[k] = v.clone();
            }
            o.optimistic.push(row);
            Vec::new()
        } else {
            panic!("{name}: unknown fixture step {step:?}");
        };
        o.effects.extend(effects.into_iter().map(|e| e.as_str()));
    }
    o
}

fn ids(values: impl IntoIterator<Item = Value>) -> Vec<String> {
    values.into_iter().map(|t| t["id"].as_str().unwrap_or("").to_owned()).collect()
}

fn strings(value: &Value) -> Vec<String> {
    value.as_array().unwrap().iter().map(|v| v.as_str().unwrap().to_owned()).collect()
}

fn check(name: &str, fixture: &Value) -> Result<usize, String> {
    let o = run_fixture(name, fixture);
    let ex = fixture["expect"].as_object().expect("expect object");
    let mut checked = 0;
    let mut expect = |ok: bool, what: &str, detail: String| -> Result<(), String> {
        checked += 1;
        if ok { Ok(()) } else { Err(format!("{name}: {what} mismatch: {detail}")) }
    };
    let want_effects = ex.get("effects").map(strings).unwrap_or_default();
    expect(o.effects == want_effects, "effects", format!("{:?} != {want_effects:?}", o.effects))?;
    if let Some(cursor) = ex.get("cursor") {
        expect(o.sync.cursor == cursor.as_f64().unwrap(), "cursor", format!("{}", o.sync.cursor))?;
    }
    if let Some(want) = ex.get("turn_ids") {
        expect(o.sync.order == strings(want), "turn_ids", format!("{:?}", o.sync.order))?;
    }
    if let Some(want) = ex.get("visible_ids") {
        let got = ids(o.sync.visible_turns(&o.optimistic));
        expect(got == strings(want), "visible_ids", format!("{got:?}"))?;
    }
    if let Some(want) = ex.get("texts") {
        let got: Vec<Value> = o.sync.current_turns().iter().map(|t| t["text"].clone()).collect();
        expect(&Value::Array(got.clone()) == want, "texts", format!("{got:?}"))?;
    }
    if let Some(want) = ex.get("missing") {
        expect(Value::Bool(o.sync.missing) == *want, "missing", format!("{}", o.sync.missing))?;
    }
    if let Some(want) = ex.get("hasMore") {
        expect(Value::Bool(o.sync.has_more) == *want, "hasMore", format!("{}", o.sync.has_more))?;
    }
    if let Some(want) = ex.get("clip_sources") {
        let got = Value::Array(o.clip_sources.clone());
        expect(&got == want, "clip_sources", format!("{got}"))?;
    }
    if let Some(want) = ex.get("replays") {
        let got = Value::from(o.replays.clone());
        expect(&got == want, "replays", format!("{got}"))?;
    }
    if let Some(want) = ex.get("delivered") {
        let got = o.delivery.confirmed_ids();
        expect(got == strings(want), "delivered", format!("{got:?}"))?;
    }
    if let Some(want) = ex.get("stale") {
        expect(o.stale == strings(want), "stale", format!("{:?}", o.stale))?;
    }
    if let Some(want) = ex.get("pending") {
        let got = o.delivery.pending_ids();
        expect(got == strings(want), "pending", format!("{got:?}"))?;
    }
    if let Some(want) = ex.get("busy") {
        let mut got: Vec<String> = BUSY_STATES.iter().map(|s| s.to_string()).collect();
        let mut want = strings(want);
        got.sort();
        want.sort();
        expect(got == want, "busy", format!("{got:?}"))?;
    }
    if let Some(want) = ex.get("notBusy") {
        for state in strings(want) {
            expect(!BUSY_STATES.contains(&state.as_str()), "notBusy", state)?;
        }
    }
    let known = [
        "effects", "cursor", "turn_ids", "visible_ids", "texts", "missing", "hasMore",
        "clip_sources", "replays", "delivered", "stale", "pending", "busy", "notBusy",
    ];
    // An expectation this runner does not understand must fail, never pass
    // silently as an unchecked scenario (contract/README.md).
    if let Some(unknown) = ex.keys().find(|k| !known.contains(&k.as_str())) {
        return Err(format!("{name}: unsupported expectation {unknown}"));
    }
    Ok(checked)
}

#[test]
fn every_contract_fixture_ends_in_the_expected_state() {
    let fixtures = load_fixtures();
    assert!(fixtures.len() >= 24, "expected the full fixture set, found {}", fixtures.len());
    let failures: Vec<String> =
        fixtures.iter().filter_map(|(name, body)| check(name, body).err()).collect();
    for (name, body) in &fixtures {
        let scope = body.get("clients").map_or("shared".into(), |c| c.to_string());
        eprintln!("fixture {name} [{scope}]: {}", if failures.iter().any(|f| f.starts_with(name.as_str())) { "FAIL" } else { "ok" });
    }
    assert!(failures.is_empty(), "{} fixture(s) failed:\n{}", failures.len(), failures.join("\n"));
}
