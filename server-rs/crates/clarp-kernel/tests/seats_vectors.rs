//! The seats of a machine's accounts as an app is told them, held to the shared fixtures of
//! that contract as the Python Host answers them (`tests/fixtures/seats-vectors.json`,
//! written by `server-rs/tools/seats-vectors.py` from `tests/fixtures/hotseat_status`): what
//! the owner's tool answered and what the switcher did, and the exact body that is served;
//! and from sixteen cases more, written for the rules of the keeping that the shared ones do
//! not reach.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_kernel::seats::{self, Acted, Seats};
use serde_json::Value;

fn vectors() -> Value {
    serde_json::from_str(include_str!("fixtures/seats-vectors.json")).unwrap()
}

/// Where two bodies do not say the same. A whole number and the same number written with a
/// point are one number, and the order of an object's fields says nothing.
fn unlike(ours: &Value, theirs: &Value, at: &str, found: &mut Vec<String>) {
    match (ours, theirs) {
        (Value::Number(a), Value::Number(b)) if a.as_f64() == b.as_f64() => {}
        (Value::Object(a), Value::Object(b)) => {
            let mut names: Vec<&String> = a.keys().chain(b.keys()).collect();
            names.sort();
            names.dedup();
            for name in names {
                match (a.get(name), b.get(name)) {
                    (Some(a), Some(b)) => unlike(a, b, &format!("{at}/{name}"), found),
                    (here, _) => {
                        let side = if here.is_some() {
                            "only here"
                        } else {
                            "only there"
                        };
                        found.push(format!("{at}/{name}: {side}"));
                    }
                }
            }
        }
        (Value::Array(a), Value::Array(b)) if a.len() == b.len() => {
            for (index, (a, b)) in a.iter().zip(b).enumerate() {
                unlike(a, b, &format!("{at}[{index}]"), found);
            }
        }
        (a, b) if a == b => {}
        (a, b) => found.push(format!("{at}: {a} here, {b} there")),
    }
}

fn applied(case: &Value) -> (Seats, usize) {
    let mut seats = Seats::with_salt("a salt of the test's own");
    let steps = case["steps"].as_array().unwrap();
    for step in steps {
        let provider = step["provider"].as_str().unwrap();
        match step["op"].as_str().unwrap() {
            "read" => seats.read(
                provider,
                &step["payload"],
                step["received_at"].as_i64().unwrap(),
            ),
            "action" => {
                let did = Acted {
                    decision: &step["decision"],
                    mode: step["mode"].as_str().unwrap_or(""),
                    switched: step["switched"] == true,
                    now: step["now"].as_i64().unwrap(),
                    floor_pct: &step["floor_pct"],
                    cadence_s: &step["cadence_s"],
                    next_due_at: &step["next_due_at"],
                };
                seats.acted(provider, &did);
            }
            "failure" => {
                let how = step["error"].as_str().unwrap();
                seats.failed(provider, how, step["now"].as_i64().unwrap());
            }
            other => panic!("a step this test does not know: {other}"),
        }
    }
    (seats, steps.len())
}

#[test]
fn what_is_served_of_the_seats_is_what_the_python_host_serves() {
    let vectors = vectors();
    let cases = vectors["cases"].as_array().unwrap();
    let (mut steps, mut accounts, mut actions) = (0, 0, 0);
    for case in cases {
        let name = case["case"].as_str().unwrap();
        let (seats, count) = applied(case);
        steps += count;
        let enabled = case["janitor_enabled"].as_bool().unwrap();
        let served = seats.served(enabled, case["served_at"].as_i64().unwrap());
        let mut found = Vec::new();
        unlike(&served, &case["wire"], "", &mut found);
        assert!(found.is_empty(), "{name}: {found:#?}");
        let providers = served["providers"].as_array().unwrap();
        let of = providers
            .iter()
            .map(|provider| provider["accounts"].as_array().unwrap().len());
        accounts += of.sum::<usize>();
        actions += served["actions"].as_array().unwrap().len();
    }
    // Counted by the script that wrote the vectors, and off what was served.
    assert_eq!((cases.len(), steps, accounts), (44, 95, 82));
    assert!(actions > 0);
}

#[test]
fn nothing_that_is_to_stay_inside_is_served_or_kept() {
    // The fixtures carry words that must never come out: an address, a path, an error's own
    // text, a token. None of them is in what is served, and none in what is kept.
    let vectors = vectors();
    let mut looked_at = 0;
    for case in vectors["cases"].as_array().unwrap() {
        let name = case["case"].as_str().unwrap();
        let given = case["steps"].to_string();
        let (seats, _) = applied(case);
        let served = seats
            .served(true, case["served_at"].as_i64().unwrap())
            .to_string();
        let kept = Value::Object(seats::kept_fields(&seats)).to_string();
        for inside in [
            "SENTINEL",
            "sentinel",
            "/home/",
            "example.invalid",
            "permission denied",
        ] {
            if !given.contains(inside) {
                continue;
            }
            looked_at += 1;
            assert!(!served.contains(inside), "{name}: {inside} is served");
            assert!(!kept.contains(inside), "{name}: {inside} is kept");
        }
    }
    assert!(looked_at >= 4, "{looked_at}");
}

#[test]
fn an_alias_is_shown_only_where_it_is_plainly_safe() {
    let shown = |alias: &str| seats::display_alias(&Value::String(alias.to_string()));
    assert_eq!(shown("work"), "work");
    assert_eq!(shown(" Team 2.1_b-c "), "Team 2.1_b-c");
    assert_eq!(shown("someone@example.org"), "s…@e…");
    for unsafe_one in [
        "",
        "a/b",
        "~home",
        "https://example.org",
        "back\\slash",
        "two@ats@here",
        "abcdefghijklmnopqrstuvwxyz0123456789",
        "-leading",
        "ünïcode",
        "one two three four five six seven eight",
    ] {
        assert_eq!(shown(unsafe_one), "account", "{unsafe_one:?}");
    }
    assert_eq!(seats::display_alias(&Value::Null), "account");
}
