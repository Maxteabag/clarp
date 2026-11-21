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
        let mut current = if row["current"].is_null() {
            voice_names::Snapshot::default()
        } else {
            voice_names::Snapshot::from_file(&row["current"])
        };
        let _ = current.refresh(
            voice_names::Snapshot::from_file(&row["fetched"]),
            row["has_key"] == true,
            row["failed"] == true,
        );
        let expected = if row["result"].is_null() {
            voice_names::Snapshot::default()
        } else {
            voice_names::Snapshot::from_file(&row["result"])
        };
        assert_eq!(current, expected, "{row}");
        let age = current.format.map(|_| Duration::ZERO);
        let due = voice_names::start_due(age, current.format);
        assert_eq!(json!(due), row["due"], "{row}");
        let wait = if due {
            voice_names::RETRY_AFTER
        } else {
            voice_names::REFRESH_EVERY
        };
        assert_eq!(json!(wait.as_secs()), row["wait"], "{row}");
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
    assert!(current.refresh(fetched, true, false).unwrap().is_empty());
    assert_eq!(json!(current), access["result"]);

    let deleted = &fixture["deleted"];
    let mut current = voice_names::Snapshot::from_file(&deleted["current"]);
    let fetched = voice_names::Snapshot::from_file(&deleted["fetched"]);
    let newly_gone = current.refresh(fetched.clone(), true, false).unwrap();
    assert_eq!(
        newly_gone,
        [(String::from("cartesia"), String::from("missing"))]
    );
    assert_eq!(json!(current), deleted["result"]);
    assert!(current.refresh(fetched, true, false).unwrap().is_empty());
    assert_eq!(deleted["logged"].as_array().unwrap().len(), 1);
    let mut backoff = voice_names::Backoff::default();
    for row in fixture["backoff"].as_array().unwrap() {
        let (wait, retry) = backoff.next_pass(row["failed"] == true);
        assert_eq!(json!(wait.as_secs()), row["wait"], "{row}");
        assert_eq!(json!(retry), row["retry"], "{row}");
    }
    let old_mark = voice_names::key_mark("fixture-old");
    let new_mark = voice_names::key_mark("fixture-new");
    for row in fixture["refusals"].as_array().unwrap() {
        let provider = row["provider"].as_str().unwrap();
        let status = u16::try_from(row["status"].as_u64().unwrap()).unwrap();
        let refused = voice_names::refuses_key(provider, status);
        let mut fetched = voice_names::Snapshot::default();
        if refused {
            fetched.refused.insert(
                provider.to_owned(),
                voice_names::Refusal {
                    key: old_mark.clone(),
                    status,
                },
            );
        }
        let mut current = voice_names::Snapshot::default();
        let _ = current.refresh(fetched, true, !refused);
        assert_eq!(json!(current.refused), row["marks"], "{row}");
        assert_eq!(current.key_refused(provider, &old_mark), refused);
        assert!(!current.key_refused(provider, &new_mark));
        if refused {
            let due = voice_names::start_due(Some(Duration::ZERO), current.format);
            assert_eq!(
                json!(due || current.key_changed(provider, &old_mark)),
                row["same_due"]
            );
            assert_eq!(
                json!(due || current.key_changed(provider, &new_mark)),
                row["changed_due"]
            );
        }
        let parsed = voice_names::Snapshot::from_file(&json!(current));
        assert_eq!(parsed.refused, current.refused);
        let mut fetched = voice_names::Snapshot::default();
        fetched.names.insert(provider.into(), Default::default());
        let _ = current.refresh(fetched, true, false);
        assert_eq!(json!(current.refused), row["after"], "{row}");
    }

    let lookup = &fixture["lookup"];
    let used = serde_json::from_value(lookup["used"].clone()).unwrap();
    let listed = serde_json::from_value(lookup["listed"].clone()).unwrap();
    let asked = voice_names::lookups(&used, &listed);
    assert_eq!(json!(asked), lookup["asked"]);
    assert_eq!(
        (start.len(), cases.len(), rows.len(), asked.len()),
        (11, 10, 12, 100)
    );
}
