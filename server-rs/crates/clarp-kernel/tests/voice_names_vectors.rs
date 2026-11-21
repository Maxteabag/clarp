//! The reference's refresh policy, run with a scratch file.
#![allow(clippy::unwrap_used)]

use clarp_kernel::voice_names;
use serde_json::{Value, json};
use std::time::Duration;

#[test]
fn voice_names_follow_the_reference_age_merge_and_lookup_rules() {
    let fixture: Value =
        serde_json::from_str(include_str!("fixtures/voice-names-vectors.json")).unwrap();
    let start = fixture["startup"].as_array().unwrap();
    for row in start {
        let age = row["age"]
            .as_i64()
            .map(|age| Duration::from_secs(age.max(0) as u64));
        assert_eq!(json!(voice_names::start_due(age)), row["due"], "{row}");
    }
    let cases = fixture["merge"].as_array().unwrap();
    for row in cases {
        let mut current = voice_names::from_file(&row["current"]);
        voice_names::merge(&mut current, voice_names::from_file(&row["fetched"]));
        assert_eq!(json!(current), row["result"], "{row}");
    }
    let lookup = &fixture["lookup"];
    let used = serde_json::from_value(lookup["used"].clone()).unwrap();
    let listed = serde_json::from_value(lookup["listed"].clone()).unwrap();
    let asked = voice_names::lookups(&used, &listed);
    assert_eq!(json!(asked), lookup["asked"]);
    assert_eq!((start.len(), cases.len(), asked.len()), (6, 4, 100));
}
