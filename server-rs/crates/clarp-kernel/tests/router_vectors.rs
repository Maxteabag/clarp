//! The pure rules of the router of spoken messages, held to what the Python Host's own
//! functions answered (`tests/fixtures/router-vectors.json`, written by
//! `server-rs/tools/router-vectors.py`).

#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_kernel::router::{self, Decision, Settings};
use serde_json::Value;

fn vectors() -> Value {
    serde_json::from_str(include_str!("fixtures/router-vectors.json")).unwrap()
}

#[test]
fn the_json_in_a_models_answer_is_found_where_the_python_host_finds_it() {
    let vectors = vectors();
    let rows = vectors["answers"].as_array().unwrap();
    let mut refused = 0;
    for row in rows {
        let text = row["text"].as_str().unwrap();
        match (
            router::extract_json(text),
            row.get("is"),
            row.get("refused"),
        ) {
            (Ok(ours), Some(theirs), _) => assert_eq!(&Value::Object(ours), theirs, "{text}"),
            (Err(ours), _, Some(theirs)) => {
                // Where the Python Host has words of its own for it, they are these; where
                // its JSON reader speaks, this Host says it its own way.
                if theirs[0] == "ValueError" {
                    assert_eq!(ours, theirs[1], "{text}");
                }
                refused += 1;
            }
            (ours, theirs, why) => panic!("{text}: here {ours:?}, there {theirs:?} {why:?}"),
        }
    }
    assert_eq!((rows.len(), refused), (20, 11));
}

#[test]
fn a_decision_is_read_and_told_as_the_python_host_reads_and_tells_it() {
    let vectors = vectors();
    let rows = vectors["decisions"].as_array().unwrap();
    for row in rows {
        let read = Decision::read(row["raw"].as_object().unwrap());
        assert_eq!(read.shown(), row["read"], "{}", row["raw"]);
        assert_eq!(read.told(), row["told"], "{}", row["raw"]);
    }
    assert_eq!(rows.len(), 20);
}

#[test]
fn a_model_is_told_what_the_python_host_tells_it() {
    let vectors = vectors();
    let rows = vectors["prompts"].as_array().unwrap();
    for row in rows {
        assert_eq!(
            router::prompt(&row["packet"]),
            row["is"],
            "{}",
            row["packet"]
        );
    }
    assert_eq!(rows.len(), 4);
}

#[test]
fn the_router_runs_and_asks_again_when_the_python_hosts_does() {
    let vectors = vectors();
    let runs = vectors["runs"].as_array().unwrap();
    for row in runs {
        let settings = Settings {
            enabled: row["enabled"] == true,
            hands_free_only: row["hands_free_only"] == true,
            fallback_only: row["fallback_only"] == true,
            ..Settings::default()
        };
        let (hands_free, by_no_name) = (row["hands_free"] == true, row["fallback_request"] == true);
        assert_eq!(
            router::should_run(&settings, hands_free, by_no_name),
            row["is"],
            "{row}"
        );
    }
    let again = vectors["broader"].as_array().unwrap();
    for row in again {
        let decision = Decision {
            kind: row[1].as_str().unwrap().to_string(),
            target_session: row[2].as_str().unwrap().to_string(),
            confidence: row[3].as_f64().unwrap(),
            ..Decision::default()
        };
        assert_eq!(
            router::asks_again(&decision, row[0].as_str(), "mike"),
            row[4],
            "{row}"
        );
    }
    assert_eq!((runs.len(), again.len()), (32, 648));
    // A router nobody has set is the Python Host's.
    assert_eq!(Settings::default().shown(), vectors["defaults"]);
}

#[test]
fn a_provider_is_known_by_the_names_the_python_host_knows_it_by() {
    let vectors = vectors();
    let rows = vectors["providers"].as_array().unwrap();
    for row in rows {
        assert_eq!(router::provider(row[0].as_str()), row[1], "{}", row[0]);
    }
    assert_eq!(rows.len(), 20);
}
