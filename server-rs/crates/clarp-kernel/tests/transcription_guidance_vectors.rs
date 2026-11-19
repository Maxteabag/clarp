#![allow(clippy::unwrap_used)]
use clarp_kernel::vocabulary::guidance::{self, Agent};
use serde_json::{Value, json};

#[test]
fn the_two_guidance_previews_follow_the_reference() {
    let cases: Vec<Value> =
        serde_json::from_str(include_str!("fixtures/transcription-guidance-vectors.json")).unwrap();
    for (i, case) in cases.iter().enumerate() {
        let names =
            guidance::active_names(case["agents"].as_array().unwrap().iter().map(|row| Agent {
                persona: row[0].as_str().unwrap(),
                created: row[1].as_i64().unwrap(),
                deleted: row[3].as_bool().unwrap(),
                archived: false,
                janitor: row[2] == "janitor",
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
    assert_eq!(cases.len(), 72);
}

#[test]
fn archived_names_leave_the_guidance_budget_for_active_targets() {
    let agent = |persona, created, archived| Agent {
        persona,
        created,
        archived,
        deleted: false,
        janitor: false,
    };
    // R109: the pre-repair Python roster would include Retired in the first position.
    let names = guidance::active_names(
        [
            agent("Current", 2, false),
            agent("Retired", 1, true),
            agent("Helper", 3, false),
        ]
        .into_iter(),
    );
    assert_eq!(names, ["Current", "Helper"]);
}
