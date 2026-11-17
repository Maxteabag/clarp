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

/// Files of the directory of a machine's table that are no names of zones: the zone of the
/// machine itself, the rule a clock without a rule falls back on, the same zones once more with
/// and without leap seconds, the zone of a machine that was never told where it is. The Python
/// Host takes each where its machine has the file (the machine the vectors were made on has all
/// but the first); this Host takes none on any machine.
const NO_NAMES: [&str; 5] = [
    "localtime",
    "posixrules",
    "posix/Europe/Oslo",
    "right/Europe/Oslo",
    "Factory",
];

/// Names of zones that some systems leave out of their table (older names, kept in a package of
/// their own): a Host has such a zone where its machine has the file.
const SOME_TABLES: [&str; 6] = [
    "US/Eastern",
    "CET",
    "EST5EDT",
    "Zulu",
    "GMT",
    "Asia/Calcutta",
];

/// Whether this machine's table has a zone in a file of this name.
fn in_this_table(name: &str) -> bool {
    let dir = std::env::var("TZDIR").unwrap_or_else(|_| "/usr/share/zoneinfo".to_string());
    std::fs::read(std::path::Path::new(&dir).join(name)).is_ok_and(|file| file.starts_with(b"TZif"))
}

#[test]
fn a_name_is_a_time_zone_where_the_python_host_has_one() {
    let vectors = vectors();
    let rows = vectors["names"].as_array().unwrap();
    let (mut zones, mut no_names) = (0, 0);
    for row in rows {
        let (name, theirs) = (row[0].as_str().unwrap(), row[1].as_bool().unwrap());
        let ours = schedule::zone(name).is_some();
        if NO_NAMES.contains(&name) {
            assert!(!ours, "{name} is no name of a zone");
            no_names += 1;
        } else if SOME_TABLES.contains(&name) {
            assert!(theirs, "{name}: the machine of the vectors has it");
            assert_eq!(
                ours,
                in_this_table(name),
                "{name}: as this machine's table has it"
            );
        } else {
            assert_eq!(ours, theirs, "{name}");
            zones += usize::from(ours);
        }
    }
    assert_eq!((rows.len(), zones, no_names), (42, 9, NO_NAMES.len()));
}
