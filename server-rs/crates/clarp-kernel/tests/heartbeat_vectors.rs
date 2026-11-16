//! The heartbeat, against the Python Host's own module.
//!
//! `fixtures/heartbeat-vectors.json` is written by `server-rs/tools/heartbeat-vectors.py`:
//! what that module makes of answers to a heartbeat, which messages it knows as the
//! heartbeat's prompt, which settings it takes and why not, and, for 1288 agents made up
//! there, why each gets no heartbeat at a moment or that it is due, and how long its
//! interval is. The same must come of the same here.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_kernel::heartbeat::{self, Known, Latest, PlanLine, Settings, State};
use serde_json::{Value, json};

fn vectors() -> Value {
    serde_json::from_str(include_str!("fixtures/heartbeat-vectors.json")).unwrap()
}

fn rows<'a>(vectors: &'a Value, name: &str) -> &'a Vec<Value> {
    vectors[name].as_array().unwrap()
}

#[test]
fn the_words_and_the_numbers_are_the_python_hosts() {
    let vectors = vectors();
    assert_eq!(vectors["ok"], heartbeat::OK);
    assert_eq!(vectors["prompt"], heartbeat::PROMPT);
    assert_eq!(vectors["restart_prefix"], heartbeat::RESTART_PREFIX);
    assert_eq!(vectors["interrupted_prefix"], heartbeat::INTERRUPTED_PREFIX);
    let numbers = &vectors["numbers"];
    assert_eq!(
        numbers["default_interval_sec"],
        heartbeat::DEFAULT_INTERVAL_S
    );
    assert_eq!(
        numbers["default_backoff_cap_sec"],
        heartbeat::DEFAULT_BACKOFF_CAP_S
    );
    assert_eq!(
        numbers["max_interrupted_retry_sec"].as_f64(),
        Some(heartbeat::MAX_INTERRUPTED_RETRY_S)
    );
    assert_eq!(
        numbers["min_wake_spacing_sec"].as_f64(),
        Some(heartbeat::MIN_WAKE_SPACING_S)
    );
    assert_eq!(
        numbers["flood_window_sec"].as_f64(),
        Some(heartbeat::FLOOD_WINDOW_S)
    );
    assert_eq!(numbers["flood_threshold"], heartbeat::FLOOD_THRESHOLD);
    assert_eq!(numbers["ack_max_chars"], heartbeat::ACK_MAX_CHARS);
}

#[test]
fn an_answer_to_a_heartbeat_is_shown_or_not_as_the_python_host_decides() {
    let vectors = vectors();
    let (mut hidden, mut neutral) = (0, 0);
    for row in rows(&vectors, "replies") {
        let text = row["text"].as_str().unwrap();
        let (skip, clean) = heartbeat::answered(text);
        assert_eq!(json!(skip), row["skip"], "{text:?}");
        assert_eq!(json!(clean), row["clean"], "{text:?}");
        let is_neutral = heartbeat::is_neutral_status(text);
        assert_eq!(json!(is_neutral), row["neutral"], "{text:?}");
        hidden += usize::from(skip);
        neutral += usize::from(is_neutral);
    }
    assert_eq!((hidden, neutral), (24, 45));
}

#[test]
fn the_prompt_of_a_heartbeat_is_known_as_the_python_host_knows_it() {
    let vectors = vectors();
    let mut known = 0;
    for row in rows(&vectors, "prompts") {
        let text = row["text"].as_str().unwrap();
        let is_prompt = heartbeat::is_prompt(text);
        assert_eq!(json!(is_prompt), row["skip"], "{text:?}");
        known += usize::from(is_prompt);
    }
    assert_eq!(known, 5);
}

#[test]
fn settings_are_taken_and_refused_as_the_python_host_takes_and_refuses_them() {
    let vectors = vectors();
    let (mut taken, mut refused) = (0, 0);
    for row in rows(&vectors, "settings") {
        let change = &row["change"];
        // Each change is asked of a Host that has the settings a Host comes with.
        let before = Settings::default();
        assert_eq!(before.shown(), row["before"], "{change}");
        match (before.changed(change), row.get("refused")) {
            (Ok(after), None) => {
                taken += 1;
                assert_eq!(after.shown(), row["after"], "{change}");
            }
            (Err(why), Some(theirs)) => {
                refused += 1;
                assert_eq!(json!(why), *theirs, "{change}");
                assert_eq!(row["after"], row["before"], "{change}");
            }
            (mine, theirs) => panic!("{change}: {mine:?} and {theirs:?}"),
        }
    }
    assert_eq!((taken, refused), (13, 17));
    // What is no object is no settings.
    assert_eq!(
        Settings::default().changed(&json!([1])).unwrap_err(),
        "heartbeat settings must be an object"
    );
}

fn policy(row: &Value) -> Settings {
    Settings {
        interval_sec: row["interval_sec"].as_i64().unwrap(),
        backoff_strategy: row["backoff_strategy"].as_str().unwrap().to_string(),
        backoff_cap_sec: row["backoff_cap_sec"].as_i64().unwrap(),
        dormant_after_noops: row["dormant_after_noops"].as_i64().unwrap(),
    }
}

fn latest(row: &Value) -> Latest {
    Latest {
        kind: row["kind"].as_str().unwrap_or_default().to_string(),
        ts_ms: row["ts"].as_f64().unwrap_or_default(),
        detail: row["detail"].clone(),
    }
}

fn state(row: &Value) -> State {
    let starts = row["recent_starts"].as_array();
    let starts = starts.map(|starts| starts.iter().map(|at| at.as_f64().unwrap()).collect());
    State {
        last_started: row["last_started"].as_f64().unwrap_or_default(),
        recent_starts: starts.unwrap_or_default(),
        noop_streak: row["noop_streak"].as_i64().unwrap_or_default(),
        dormant: row["dormant"].as_bool().unwrap_or_default(),
        last_wake_signal_ms: 0,
    }
}

#[test]
fn an_agent_is_due_when_the_python_host_says_so_and_is_left_alone_for_its_reasons() {
    let vectors = vectors();
    let now = vectors["now"].as_f64().unwrap();
    let (mut asked, mut due) = (0, 0);
    for row in rows(&vectors, "decided") {
        let policy = policy(&row["policy"]);
        let known = Known {
            latest: latest(&row["latest"]),
            busy: row["busy"].as_bool().unwrap(),
            active: row["active"].as_bool().unwrap(),
            compacting: row["compacting"].as_bool().unwrap(),
            recent_activity: row["recent_activity"].as_str().unwrap().to_string(),
        };
        let state = state(&row["state"]);
        let reason = heartbeat::skip_reason(&known, &state, &policy, now);
        assert_eq!(json!(reason), row["reason"], "{row}");
        let interval = heartbeat::interval(&state, &policy, known.latest.interrupted());
        assert_eq!(json!(interval), row["interval"], "{row}");
        asked += 1;
        due += usize::from(reason.is_empty());
    }
    assert_eq!((asked, due), (1288, 182));
}

#[test]
fn an_agent_is_shown_the_plan_it_has_standing_with_its_heartbeat() {
    let line = |status: &str, title: &str| PlanLine {
        status: status.to_string(),
        title: title.to_string(),
    };
    // No plan that is active.
    assert_eq!(
        heartbeat::prompt_text(false, None),
        format!("{}\n\nDurable plan: none active.", heartbeat::PROMPT)
    );
    // A plan: what is still to do is shown, in its order, each with how it stands.
    let lines = [
        line("done", "Read the brief"),
        line("in_progress", "  Port the kernel  "),
        line("pending", ""),
        line("blocked", "Wait for the owner's word"),
        line("cancelled", "An idea that was dropped"),
        line("pending", "Land it"),
    ];
    let shown = heartbeat::prompt_text(true, Some(("The rebuild", &lines)));
    let expected = format!(
        "{}{}\n\nDurable plan: The rebuild\n- [in_progress] Port the kernel\n\
         - [blocked] Wait for the owner's word\n- [pending] Land it",
        heartbeat::INTERRUPTED_PREFIX,
        heartbeat::PROMPT
    );
    assert_eq!(shown, expected);
    assert!(heartbeat::is_prompt(&shown));
    // A plan with nothing left to do says so.
    let finished = [line("done", "Everything")];
    assert!(
        heartbeat::prompt_text(false, Some(("Done", &finished)))
            .ends_with("\n\nDurable plan: Done\n- No pending, in-progress, or blocked items.")
    );
    // Twelve lines at most, and no line or title longer than it may be.
    let many: Vec<PlanLine> = (0..20)
        .map(|at| line("pending", &format!("{at} {}", "x".repeat(400))))
        .collect();
    let long = "T".repeat(300);
    let shown = heartbeat::prompt_text(false, Some((&long, &many)));
    let after: Vec<&str> = shown.split('\n').skip(2).collect();
    assert_eq!(after.len(), 13);
    assert_eq!(after[0].chars().count(), "Durable plan: ".len() + 240);
    assert!(after[1..].iter().all(|shown| {
        shown.starts_with("- [pending] ") && shown.chars().count() == "- [pending] ".len() + 300
    }));
}

#[test]
fn what_the_heartbeat_remembers_of_an_agent_changes_as_the_python_host_changes_it() {
    let policy = Settings::default();
    // Nothing to do, five times: longer each time, then dormant.
    let mut state = State::default();
    let mut intervals = Vec::new();
    for _ in 0..5 {
        intervals.push(heartbeat::interval(&state, &policy, false));
        assert!(!state.dormant);
        state.found_nothing(&policy, false);
    }
    assert_eq!(intervals, [1800, 3600, 3600, 3600, 3600]);
    assert!(state.dormant && state.noop_streak == 5);
    // An agent whose turn was interrupted is not put to sleep by that.
    let mut interrupted = State::default();
    for _ in 0..9 {
        interrupted.found_nothing(&policy, true);
    }
    assert!(!interrupted.dormant && interrupted.noop_streak == 9);
    assert_eq!(heartbeat::interval(&interrupted, &policy, true), 1800);
    // Something it did, or a sign from outside, and it is asked at the plain interval again.
    state.was_active();
    assert_eq!((state.noop_streak, state.dormant), (0, false));
    state.found_nothing(&policy, false);
    assert!(!state.woken_by(0));
    assert!(state.woken_by(5_000));
    assert_eq!((state.noop_streak, state.last_wake_signal_ms), (0, 5_000));
    assert!(!state.woken_by(5_000) && !state.woken_by(4_000));
    // Its starts are remembered, the newest six.
    for at in 1..=8 {
        state.started(f64::from(at), 4_000);
    }
    assert_eq!(state.last_started, 8.0);
    assert_eq!(state.recent_starts, [3.0, 4.0, 5.0, 6.0, 7.0, 8.0]);
    assert_eq!(state.last_wake_signal_ms, 5_000);
    // The other strategies.
    let streak = |noop_streak| State {
        noop_streak,
        ..State::default()
    };
    let linear = Settings {
        backoff_strategy: "linear".to_string(),
        backoff_cap_sec: 86_400,
        ..Settings::default()
    };
    assert_eq!(heartbeat::interval(&streak(3), &linear, false), 7200);
    assert_eq!(heartbeat::interval(&streak(1_000), &linear, false), 86_400);
    let fixed = Settings {
        backoff_strategy: "fixed".to_string(),
        ..Settings::default()
    };
    assert_eq!(heartbeat::interval(&streak(40), &fixed, false), 1800);
    assert_eq!(heartbeat::interval(&streak(500), &policy, false), 3600);
}

#[test]
fn an_agents_profile_shows_when_its_next_heartbeat_is_due() {
    let policy = Settings::default();
    let now = 1_800_000_000.0;
    let idle = Latest::default();
    // Never started: due now.
    let fresh = State::default();
    assert_eq!(
        heartbeat::schedule(true, (&idle, &fresh), &policy, (now, 0.0, false)),
        json!({
            "enabled": true, "last_started_at": null, "next_heartbeat_at": 1_800_000_000_000_i64,
            "effective_interval_sec": 1800, "noop_streak": 0, "dormant": false,
        })
    );
    // Started ten minutes ago with one heartbeat that found nothing: an hour after it.
    let backed_off = State {
        last_started: now - 600.0,
        noop_streak: 1,
        ..State::default()
    };
    let shown = heartbeat::schedule(true, (&idle, &backed_off), &policy, (now, 0.0, false));
    assert_eq!(shown["last_started_at"], 1_799_999_400_000_i64);
    assert_eq!(shown["next_heartbeat_at"], 1_800_003_000_000_i64);
    assert_eq!(shown["effective_interval_sec"], 3600);
    // Somebody spoke with it: not before the quiet after that is over.
    let quiet = heartbeat::schedule(true, (&idle, &fresh), &policy, (now, now + 900.0, false));
    assert_eq!(quiet["next_heartbeat_at"], 1_800_000_900_000_i64);
    // Off, dormant, served by a keeper, waiting for its user, stopped by its user: none.
    let none = |shown: Value| shown["next_heartbeat_at"].is_null();
    assert!(none(heartbeat::schedule(
        false,
        (&idle, &fresh),
        &policy,
        (now, 0.0, false)
    )));
    let dormant = State {
        dormant: true,
        ..State::default()
    };
    assert!(none(heartbeat::schedule(
        true,
        (&idle, &dormant),
        &policy,
        (now, 0.0, false)
    )));
    assert!(none(heartbeat::schedule(
        true,
        (&idle, &fresh),
        &policy,
        (now, 0.0, true)
    )));
    let waiting = Latest {
        kind: "waiting".to_string(),
        ..Latest::default()
    };
    assert!(none(heartbeat::schedule(
        true,
        (&waiting, &fresh),
        &policy,
        (now, 0.0, false)
    )));
    let stopped = Latest {
        kind: "interrupted".to_string(),
        ts_ms: (now - 100.0) * 1000.0,
        detail: json!({ "source": "user_stop" }),
    };
    assert!(none(heartbeat::schedule(
        true,
        (&stopped, &fresh),
        &policy,
        (now, 0.0, false)
    )));
    // A turn that was interrupted long ago: the agent is shown as dormant.
    let long_ago = Latest {
        kind: "interrupted".to_string(),
        ts_ms: (now - 12.0 * 3600.0) * 1000.0,
        detail: Value::Null,
    };
    let shown = heartbeat::schedule(true, (&long_ago, &fresh), &policy, (now, 0.0, false));
    assert_eq!(
        (&shown["dormant"], &shown["next_heartbeat_at"]),
        (&json!(true), &Value::Null)
    );
}

#[test]
fn the_hours_heartbeats_are_wanted_in_are_read_as_the_python_host_reads_them() {
    let at = |hour: u32, minute: u32| hour * 60 + minute;
    // Nothing said, or nothing that is such a span: no hour is outside.
    for span in [
        "",
        "  ",
        "9-17",
        "09:00",
        "09:00-25:00",
        "24:00-06:00",
        "09:60-17:00",
        "x",
    ] {
        assert!(!heartbeat::outside_active_hours(span, at(3, 0)), "{span:?}");
    }
    assert!(!heartbeat::outside_active_hours("09:00-17:00", at(9, 0)));
    assert!(!heartbeat::outside_active_hours("09:00-17:00", at(16, 59)));
    assert!(heartbeat::outside_active_hours("09:00-17:00", at(17, 0)));
    assert!(heartbeat::outside_active_hours("09:00-17:00", at(8, 59)));
    assert!(!heartbeat::outside_active_hours(
        " 08:00-24:00 ",
        at(23, 59)
    ));
    assert!(heartbeat::outside_active_hours("08:00-24:00", at(7, 59)));
    // Through midnight.
    assert!(!heartbeat::outside_active_hours("22:00-06:00", at(23, 0)));
    assert!(!heartbeat::outside_active_hours("22:00-06:00", at(5, 59)));
    assert!(heartbeat::outside_active_hours("22:00-06:00", at(6, 0)));
    assert!(heartbeat::outside_active_hours("22:00-06:00", at(12, 0)));
    // A span of no length is no hour at all.
    assert!(heartbeat::outside_active_hours("09:00-09:00", at(9, 0)));
}
