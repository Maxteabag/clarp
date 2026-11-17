//! An explanation is put on the row of a tool as the Python Host's watcher puts it.
//!
//! `tests/fixtures/tool-explanation-rows-vectors.json` (written by
//! `tools/tool-explanation-rows-vectors.py`) holds scenes that were played to that Host's
//! `LiveExplainer` with a stand-in for the explainer's service and a clock of their own:
//! events that show a turn, rounds, and time that passes. Here the same scenes are played to
//! the rows: after every step the same is put on the same items, and the service is asked
//! the same.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::collections::HashMap;

use clarp_kernel::explain::rows::{Asking, Rows};
use serde_json::{Value, json};

const VECTORS: &str = include_str!("fixtures/tool-explanation-rows-vectors.json");

/// What the stand-in for the service says of what it is asked about: the rule of the tool
/// that wrote the vectors, by the first word of each item's command.
fn stand_in(asking: &Asking, level: u8, seen: &mut HashMap<String, u64>) -> Value {
    let mut answers = Vec::new();
    for item in &asking.items {
        let id = item["id"].as_str().unwrap();
        let command = item["activity"]["command"].as_str().unwrap_or_default();
        let word = command.split_whitespace().next().unwrap_or_default();
        let times = seen.entry(id.to_string()).or_default();
        *times += 1;
        let waits = word
            .strip_prefix("wait-")
            .map(|times| times.parse::<u64>().unwrap());
        answers.push(match word {
            "missing" => continue,
            _ if waits.is_some_and(|waits| *times <= waits) => {
                json!({ "id": id, "status": "pending" })
            }
            "busy" => json!({ "id": id, "status": "busy", "reason": "queue_full" }),
            "failed" => json!({ "id": id, "status": "failed", "reason": "translator_failed" }),
            "gone" => json!({ "id": id, "status": "cancelled" }),
            "off" => json!({ "id": id, "status": "disabled" }),
            "odd" => json!({ "id": id, "status": "unheard-of" }),
            "empty" => json!({ "id": id, "status": "ready", "text": "" }),
            _ => json!({
                "id": id, "status": "ready", "text": format!("Explains {command}."),
                "source": "llm", "cached": false,
            }),
        });
    }
    json!({ "model": "stand-in", "detail_level": level, "items": answers })
}

#[test]
fn an_explanation_is_put_on_a_tools_row_as_the_python_host_puts_it() {
    let vectors: Value = serde_json::from_str(VECTORS).unwrap();
    let (mut scenes, mut steps, mut patches, mut askings, mut about) = (0, 0, 0, 0, 0);
    for scene in vectors["scenes"].as_array().unwrap() {
        let name = scene["name"].as_str().unwrap();
        let service = scene["service"].as_str().unwrap();
        let mut enabled = scene["setting"]["enabled"].as_bool().unwrap();
        let mut level = u8::try_from(scene["setting"]["detail_level"].as_u64().unwrap()).unwrap();
        let mut rows = Rows::default();
        let mut seen = HashMap::new();
        let mut now = 0;
        for step in scene["steps"].as_array().unwrap() {
            let (mut patched, mut asked) = (Vec::new(), Vec::new());
            match step["do"].as_str().unwrap() {
                "observe" => patched.extend(rows.observe(&step["event"], enabled, level, now)),
                "clock" => {}
                "setting" => {
                    let change = &step["change"];
                    enabled = change["enabled"].as_bool().unwrap_or(enabled);
                    let chosen = change["detail_level"].as_u64();
                    level = chosen.map_or(level, |level| u8::try_from(level).unwrap());
                }
                "round" => {
                    let (settled, to_ask) = rows.round(enabled, level, now);
                    patched.extend(settled);
                    for asking in to_ask {
                        if service == "none" {
                            patched.extend(rows.unanswered(&asking, "unavailable"));
                            continue;
                        }
                        let release = match asking.release.as_slice() {
                            [] => Value::Null,
                            release => json!(release),
                        };
                        asked.push(json!({
                            "level": level, "items": asking.items, "release": release,
                            "target": asking.agent_id, "cwd": null,
                        }));
                        if service == "breaks" {
                            patched.extend(rows.unanswered(&asking, "error"));
                            continue;
                        }
                        let answer = stand_in(&asking, level, &mut seen);
                        patched.extend(rows.answered(&asking, &answer, level));
                    }
                }
                other => panic!("{name}: a step nobody knows: {other}"),
            }
            now = step["at_ms"].as_i64().unwrap();
            let patched: Vec<Value> = patched
                .iter()
                .map(|patch| json!([patch.agent_id, patch.item_id, patch.fields()]))
                .collect();
            let (patched, asked) = (Value::Array(patched), Value::Array(asked));
            for (ours, theirs) in [(&patched, &step["patched"]), (&asked, &step["asked"])] {
                assert_eq!(ours, theirs, "{name}: {}", step["do"]);
                assert_eq!(
                    serde_json::to_string(ours).unwrap(),
                    serde_json::to_string(theirs).unwrap(),
                    "{name}: {}",
                    step["do"]
                );
            }
            assert_eq!(rows.busy(), step["work"], "{name}: {step}");
            steps += 1;
            patches += patched.as_array().unwrap().len();
            askings += asked.as_array().unwrap().len();
            let items = asked.as_array().unwrap().iter();
            about += items
                .map(|asked| asked["items"].as_array().unwrap().len())
                .sum::<usize>();
        }
        scenes += 1;
    }
    // Counted by the tool that wrote the vectors.
    assert_eq!(
        (scenes, steps, patches, askings, about),
        (18, 91, 119, 34, 63)
    );
}
