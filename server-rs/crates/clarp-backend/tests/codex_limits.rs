//! What Codex says of its account's limits, read as the Python Host's own functions read it
//! (`tests/fixtures/codex-limits-vectors.json`, written by
//! `server-rs/tools/codex-limits-vectors.py`).

#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_backend::limits::{codex, window};
use serde_json::Value;

fn vectors() -> Value {
    serde_json::from_str(include_str!("fixtures/codex-limits-vectors.json")).unwrap()
}

#[test]
fn a_window_is_read_as_the_python_host_reads_it() {
    let vectors = vectors();
    let windows = vectors["windows"].as_array().unwrap();
    let (mut read, mut broke) = (0, 0);
    for vector in windows {
        let ours = window(&vector["given"]);
        if vector.get("breaks").is_some() {
            // The Python Host's reader breaks on a time no calendar has. Here it is no time,
            // and what else the window says is read.
            assert_eq!(ours.unwrap()["resets_at"], "", "{vector}");
            broke += 1;
            continue;
        }
        assert_eq!(
            ours.clone().unwrap_or(Value::Null),
            vector["window"],
            "{vector}"
        );
        read += usize::from(ours.is_some());
    }
    // Counted by the script that wrote the vectors.
    assert_eq!((windows.len(), read, broke), (47, 38, 1));
}

#[test]
fn a_record_of_codex_limits_is_read_as_the_python_host_reads_it() {
    let vectors = vectors();
    let records = vectors["records"].as_array().unwrap();
    let (mut read, mut refused) = (0, 0);
    for vector in records {
        match codex(&vector["given"]) {
            Ok(snapshot) => {
                assert_eq!(snapshot, vector["snapshot"], "{vector}");
                read += 1;
            }
            Err(why) => {
                assert_eq!(
                    why,
                    vector["refused"].as_str().unwrap_or("not refused"),
                    "{vector}"
                );
                refused += 1;
            }
        }
    }
    assert_eq!((records.len(), read, refused), (38, 21, 17));
}
