#![allow(clippy::unwrap_used)]
use clarp_kernel::vocabulary::guidance::{self, Agent};
use serde_json::{Value, json};

#[test]
fn the_two_guidance_previews_follow_the_reference() {
    let cases: Vec<Value> =
        serde_json::from_str(include_str!("fixtures/transcription-guidance-vectors.json")).unwrap();
    for (i, case) in cases.iter().enumerate() {
        let names = guidance::active_names(case["agents"].as_array().unwrap().iter().map(|row| {
            Agent {
                persona: row["name"].as_str().unwrap(),
                created: row["created"].as_i64().unwrap(),
                deleted: row["deleted"].as_bool().unwrap(),
                id: row["id"].as_str().unwrap(),
                role: row["role"].as_str().unwrap(),
                parent: row["parent"].as_str().unwrap_or(""),
                latest_start: row["turns"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|v| v.as_i64().unwrap())
                    .max()
                    .unwrap_or(0),
                archived: row["archived"].as_bool().unwrap(),
                janitor: row["role"] == "janitor",
            }
        }));
        let enabled = guidance::names_enabled(case["switch"].as_str());
        let stored = case["glossary"].as_str().unwrap();
        assert_eq!(
            guidance::payload(enabled, stored, &names),
            case["before"],
            "before {i}"
        );
        match guidance::update(enabled, stored, &case["change"]) {
            Ok((switch, cleaned)) => {
                assert_eq!(json!([switch, cleaned]), case["updated"], "update {i}");
                assert_eq!(
                    guidance::payload(switch, &cleaned, &names),
                    case["after"],
                    "after {i}"
                );
            }
            Err(error) => assert_eq!(error, case["error"], "refusal {i}"),
        }
    }
    assert_eq!(cases.len(), 81);
}
