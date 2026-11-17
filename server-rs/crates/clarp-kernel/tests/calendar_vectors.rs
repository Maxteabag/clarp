//! What an agent asks to have put into a calendar, held to what the Python Host's own function
//! makes of the same requests (`tests/fixtures/calendar-vectors.json`, written by
//! `server-rs/tools/calendar-vectors.py`).

#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_kernel::calendar;
use serde_json::Value;

#[test]
fn a_request_for_the_calendar_is_read_as_the_python_host_reads_it() {
    let vectors: Value =
        serde_json::from_str(include_str!("fixtures/calendar-vectors.json")).unwrap();
    let (mut told, mut refused) = (0, 0);
    for row in vectors["asked"].as_array().unwrap() {
        let name = &row["name"];
        let said = row["asked"].as_object().unwrap();
        let ours = calendar::asked(said, "cal-0123456789abcdef");
        match (&ours, row.get("told"), row.get("refused")) {
            (Ok(event), Some(theirs), None) => {
                assert_eq!(event, theirs, "{name}");
                // In the same order too: an app reads what it is told as it is written.
                assert_eq!(event.to_string(), theirs.to_string(), "{name}, as text");
                told += 1;
            }
            (Err(why), None, Some(theirs)) => {
                assert_eq!(why, theirs.as_str().unwrap(), "{name}");
                refused += 1;
            }
            _ => panic!("{name}: {ours:?}"),
        }
    }
    assert_eq!((told, refused), (36, 14));
}
