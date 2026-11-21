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
        let snapshot =
            row["content"]
                .as_str()
                .map_or_else(voice_names::Snapshot::default, |text| {
                    let raw = serde_json::from_str(text).unwrap_or(Value::Null);
                    voice_names::Snapshot::from_file(&raw)
                });
        assert_eq!(
            json!(voice_names::start_due(age, snapshot.format)),
            row["due"],
            "{row}"
        );
    }
    let cases = fixture["merge"].as_array().unwrap();
    for row in cases {
        let mut current = voice_names::Snapshot::from_file(&row["current"]);
        current.apply(voice_names::Snapshot::from_file(&row["fetched"]));
        if row["same_bytes"] == true {
            assert_eq!(
                current,
                voice_names::Snapshot::from_file(&row["result"]),
                "{row}"
            );
        } else {
            assert_eq!(json!(current), row["result"], "{row}");
        }
    }
    let access = &fixture["access"];
    let mut fetched = voice_names::Snapshot::default();
    let rows = access["rows"].as_array().unwrap();
    let voices = rows
        .iter()
        .map(|row| {
            (
                row["id"].as_str().unwrap().to_owned(),
                voice_names::CatalogueVoice::from_cartesia(row),
            )
        })
        .collect();
    fetched.catalogue.insert("cartesia".into(), voices);
    fetched.names.insert("cartesia".into(), Default::default());
    fetched.gone.insert("cartesia".into(), Default::default());
    let mut current = voice_names::Snapshot::default();
    assert!(current.apply(fetched).is_empty());
    assert_eq!(json!(current), access["result"]);

    let deleted = &fixture["deleted"];
    let mut current = voice_names::Snapshot::from_file(&deleted["current"]);
    let fetched = voice_names::Snapshot::from_file(&deleted["fetched"]);
    let newly_gone = current.apply(fetched.clone());
    assert_eq!(
        newly_gone,
        [(String::from("cartesia"), String::from("missing"))]
    );
    assert_eq!(json!(current), deleted["result"]);
    assert!(current.apply(fetched).is_empty());
    assert_eq!(deleted["logged"].as_array().unwrap().len(), 1);
    let lookup = &fixture["lookup"];
    let used = serde_json::from_value(lookup["used"].clone()).unwrap();
    let listed = serde_json::from_value(lookup["listed"].clone()).unwrap();
    let asked = voice_names::lookups(&used, &listed);
    assert_eq!(json!(asked), lookup["asked"]);
    assert_eq!(
        (start.len(), cases.len(), rows.len(), asked.len()),
        (11, 4, 12, 100)
    );
}
