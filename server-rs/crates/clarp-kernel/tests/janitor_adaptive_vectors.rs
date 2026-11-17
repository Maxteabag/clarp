//! A janitor's adaptive intervals, held to what the Python Host's own module answered
//! (`tests/fixtures/janitor-adaptive-vectors.json`, written by
//! `server-rs/tools/janitor-adaptive-vectors.py`).

#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_kernel::janitors::adaptive::{self, State};
use serde_json::{Value, json};

fn vectors() -> Value {
    serde_json::from_str(include_str!("fixtures/janitor-adaptive-vectors.json")).unwrap()
}

/// A number of seconds as Python's `int()` leaves it; nothing is nothing.
fn whole(value: &Value) -> Option<i64> {
    value
        .as_i64()
        .or_else(|| value.as_f64().map(|fraction| fraction.trunc() as i64))
}

/// What a run says under a key, as Python's `str()` makes a text of it; nothing is no text.
fn said(run: &Value, key: &str) -> String {
    match &run[key] {
        Value::String(text) => text.clone(),
        Value::Null => String::new(),
        other => other.to_string(),
    }
}

#[test]
fn the_limits_are_the_python_hosts() {
    let vectors = vectors();
    assert_eq!(
        vectors["activities"],
        json!([adaptive::WORKED, adaptive::IDLE, adaptive::FAILED])
    );
    assert_eq!(vectors["longest"], adaptive::LONGEST_S);
    assert_eq!(vectors["limit"], adaptive::LIMIT_S);
    assert_eq!(vectors["summary_most"], adaptive::SUMMARY_MOST);
}

#[test]
fn a_finished_run_counts_as_the_python_host_counts_it() {
    let vectors = vectors();
    let rows = vectors["outcomes"].as_array().unwrap();
    for row in rows {
        let run = &row["run"];
        let (activity, summary, reported) = adaptive::outcome(
            &said(run, "status"),
            &said(run, "outcome"),
            &said(run, "activity"),
            &said(run, "activity_summary"),
            &said(run, "error"),
        );
        assert_eq!(json!([activity, summary, reported]), row["is"], "{run}");
    }
    assert_eq!(rows.len(), 18);
}

#[test]
fn an_interval_has_the_bounds_the_python_host_gives_it() {
    let vectors = vectors();
    let rows = vectors["bounds"].as_array().unwrap();
    for row in rows {
        let (base, longest) =
            adaptive::bounds(whole(&row["base"]).unwrap(), whole(&row["longest"]));
        assert_eq!(json!([base, longest]), row["is"], "{row}");
    }
    assert_eq!(rows.len(), 16);
    for row in vectors["fresh"].as_array().unwrap() {
        let set = (whole(&row["base"]).unwrap(), whole(&row["longest"]));
        let fresh = State::default();
        assert_eq!(
            adaptive::current(&fresh, set.0, set.1),
            row["waits"],
            "{row}"
        );
        assert_eq!(
            adaptive::cadence(&fresh, set, None, ""),
            row["shown"],
            "{row}"
        );
    }
}

/// Lives one stretch of a janitor's life: each run counted, and after each the state, the
/// wait that follows and what is shown, as the Python Host had them.
fn lived(state: &mut State, set: (i64, Option<i64>), runs: &Value, lane: &str) -> usize {
    let runs = runs.as_array().unwrap();
    for run in runs {
        assert_eq!(
            adaptive::current(state, set.0, set.1),
            run["waited"],
            "{run}"
        );
        let (activity, summary) = (
            run["activity"].as_str().unwrap(),
            run["summary"].as_str().unwrap(),
        );
        let interval =
            adaptive::advance(state, activity, summary, set, run["now"].as_i64()).unwrap();
        assert_eq!(interval, run["interval"], "{run}");
        assert_eq!(
            serde_json::to_value(&*state).unwrap(),
            run["state"],
            "{run}"
        );
        let next = run["shown"]["next_run_at"].as_i64();
        assert_eq!(
            adaptive::cadence(state, set, next, lane),
            run["shown"],
            "{run}"
        );
    }
    runs.len()
}

#[test]
fn a_janitors_life_is_counted_run_by_run_as_the_python_host_counts_it() {
    let vectors = vectors();
    let mut runs = 0;
    for life in vectors["lives"].as_array().unwrap() {
        let set = (whole(&life["base"]).unwrap(), whole(&life["longest"]));
        runs += lived(&mut State::default(), set, &life["runs"], "schedule:one");
    }
    // Set anew in the middle of its life, a janitor starts from its base.
    for life in vectors["changed"].as_array().unwrap() {
        let mut state = State::default();
        let set = (whole(&life["base"]).unwrap(), whole(&life["longest"]));
        runs += lived(&mut state, set, &life["runs"], "schedule:one");
        let then = (
            whole(&life["then_base"]).unwrap(),
            whole(&life["then_longest"]),
        );
        assert_eq!(
            adaptive::current(&state, then.0, then.1),
            life["waits"],
            "{life}"
        );
        assert_eq!(
            adaptive::cadence(&state, then, Some(7), "active-interval:two"),
            life["shown"]
        );
        runs += lived(&mut state, then, &life["then"], "schedule:one");
    }
    assert_eq!(runs, 71);
    // A run that ended as nothing a run can end as is not counted.
    let mut state = State::default();
    assert_eq!(
        adaptive::advance(&mut state, "napped", "", (60, None), None),
        None
    );
    assert_eq!(state, State::default());
    assert!(vectors["unknown"].as_str().unwrap().contains("napped"));
    // What is kept is written down and read back as it was.
    adaptive::advance(&mut state, "idle", "nothing", (60, Some(600)), Some(5)).unwrap();
    let read: State = serde_json::from_value(serde_json::to_value(&state).unwrap()).unwrap();
    assert_eq!(read, state);
}
