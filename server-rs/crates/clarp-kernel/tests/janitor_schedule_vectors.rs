//! A janitor's schedule in a time zone, held to what the Python Host's own module answered
//! (`tests/fixtures/janitor-schedule-vectors.json`, written by
//! `server-rs/tools/janitor-schedule-vectors.py`).

#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_kernel::janitors::schedule;
use serde_json::Value;

fn vectors() -> Value {
    serde_json::from_str(include_str!("fixtures/janitor-schedule-vectors.json")).unwrap()
}

fn word(row: &Value, key: &str) -> String {
    row[key].as_str().unwrap().to_string()
}

#[test]
fn a_schedule_runs_next_when_the_python_hosts_does() {
    let vectors = vectors();
    let rows = vectors["runs"].as_array().unwrap();
    let (mut moments, mut none) = (0, 0);
    for row in rows {
        let (expression, zone) = (word(row, "expression"), word(row, "zone"));
        for pair in row["next"].as_array().unwrap() {
            let from = pair[0].as_i64().unwrap();
            let ours = schedule::next_run(&expression, &zone, from).unwrap();
            assert_eq!(
                ours,
                pair[1].as_i64(),
                "{expression} in {zone} after {from}"
            );
            moments += 1;
            none += usize::from(ours.is_none());
        }
    }
    assert_eq!((rows.len(), moments, none), (216, 3024, 341));
}

#[test]
fn two_runs_are_as_far_apart_as_the_python_host_says() {
    let vectors = vectors();
    let rows = vectors["apart"].as_array().unwrap();
    for row in rows {
        let (expression, zone) = (word(row, "expression"), word(row, "zone"));
        let ours = schedule::base_interval(&expression, &zone, row["from"].as_i64().unwrap());
        assert_eq!(ours.ok(), row["seconds"].as_i64(), "{expression} in {zone}");
    }
    assert_eq!(rows.len(), 96);
}

#[test]
fn a_preview_shows_the_same_runs_or_is_refused_in_the_same_words() {
    let vectors = vectors();
    let rows = vectors["previews"].as_array().unwrap();
    let mut refused = 0;
    for row in rows {
        let (expression, zone) = (word(row, "expression"), word(row, "zone"));
        let (from, count) = (
            row["from"].as_i64().unwrap(),
            row["count"].as_i64().unwrap(),
        );
        match (
            schedule::preview(&expression, &zone, from, count),
            row.get("is"),
            row.get("refused"),
        ) {
            (Ok(ours), Some(theirs), _) => assert_eq!(&serde_json::json!(ours), theirs, "{row}"),
            (Err(ours), _, Some(theirs)) => {
                assert_eq!(&serde_json::json!(ours), theirs, "{row}");
                refused += 1;
            }
            (ours, theirs, why) => panic!("{row}: here {ours:?}, there {theirs:?} {why:?}"),
        }
    }
    assert_eq!((rows.len(), refused), (91, 6));
}

/// Files of the machine's table that the Python Host takes as a zone because they are files of
/// the table, and this Host does not: they are no names of zones (the rule a clock without a
/// rule falls back on, the same zones once more with and without leap seconds, the zone of a
/// machine that was never told where it is).
const FILES_ONLY: [&str; 4] = [
    "posixrules",
    "posix/Europe/Oslo",
    "right/Europe/Oslo",
    "Factory",
];

#[test]
fn a_name_is_a_time_zone_where_the_python_host_has_one() {
    let vectors = vectors();
    let rows = vectors["names"].as_array().unwrap();
    let mut differ = Vec::new();
    for row in rows {
        let (name, theirs) = (row[0].as_str().unwrap(), row[1].as_bool().unwrap());
        let ours = schedule::zone(name).is_some();
        if ours != theirs {
            differ.push((name, ours, theirs));
        }
    }
    // Every difference is one of the files that are no names, taken there and not here.
    for (name, ours, theirs) in &differ {
        assert!(
            FILES_ONLY.contains(name) && !ours && *theirs,
            "{name}: here {ours}, there {theirs}"
        );
    }
    assert_eq!(rows.len(), 42);
}
