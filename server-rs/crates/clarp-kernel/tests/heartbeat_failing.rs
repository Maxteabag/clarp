//! A heartbeat whose own turn fails every time.
//!
//! The Python Host tries an agent whose turn was interrupted again for eleven hours, at the
//! owner's plain interval, and counts the eleven hours from the turn that was interrupted
//! last. A heartbeat's own turn that fails is such a turn: every one of them begins the
//! eleven hours anew, and the agent is given a heartbeat at every interval for ever (issue
//! 396 there: 219 failed turns in five days on one agent). Here the eleven hours are counted
//! from where the run of interruptions began.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_kernel::heartbeat::{self, Known, Latest, Settings, State};
use serde_json::Value;

const BEGAN: f64 = 1_000_000.0;
const DAY: f64 = 86_400.0;

fn interrupted_at(seconds: f64) -> Latest {
    Latest {
        kind: "interrupted".to_string(),
        ts_ms: seconds * 1000.0,
        detail: Value::Null,
    }
}

/// An agent whose turn was interrupted at `BEGAN`, looked at once a minute as the Host's
/// runner looks at it, for so many days. Every heartbeat it is given fails ten seconds
/// later without a word. `remembers`: whether what it last did is looked at before the rule
/// is asked, as the Host does it. Answers when its heartbeats were started, and how it
/// stands at the end.
fn failing_for(days: f64, remembers: bool) -> (Vec<f64>, State) {
    let policy = Settings::default();
    let mut state = State::default();
    let mut latest = interrupted_at(BEGAN);
    let (mut started, mut now) = (Vec::new(), BEGAN);
    while now < BEGAN + days * DAY {
        now += 60.0;
        if remembers {
            state.saw(&latest);
        }
        let known = Known {
            latest: latest.clone(),
            ..Known::default()
        };
        let why_not = heartbeat::skip_reason(&known, &state, &policy, now);
        if why_not == "dormant" {
            state.dormant = true;
        }
        if !why_not.is_empty() {
            continue;
        }
        state.started(now, 0);
        started.push(now - BEGAN);
        latest = interrupted_at(now + 10.0);
    }
    (started, state)
}

#[test]
fn a_heartbeat_that_fails_every_time_is_given_up_after_eleven_hours() {
    // The Python Host's rule by itself, as it was carried over: the eleven hours begin anew
    // with every heartbeat that fails, and five days of it are a heartbeat every half hour.
    let (as_it_was, state) = failing_for(5.0, false);
    assert_eq!(as_it_was.len(), 232);
    assert!(!state.dormant);

    // With the run remembered: tried again for eleven hours from the first interruption,
    // then left alone.
    let (started, state) = failing_for(5.0, true);
    assert_eq!(started.len(), 21, "{started:?}");
    assert!(state.dormant);
    let within = |at: &f64| *at < heartbeat::MAX_INTERRUPTED_RETRY_S;
    assert!(started.iter().all(within));
    assert_eq!(started.last(), Some(&39_000.0));
    // A day of it is no more than five days of it.
    assert_eq!(failing_for(1.0, true).0, started);
}

#[test]
fn the_profile_says_that_an_agent_in_a_long_run_of_interruptions_is_left_alone() {
    let policy = Settings::default();
    // Its last heartbeat failed ten minutes ago; its run of interruptions began eleven
    // hours ago and more.
    let now = BEGAN + heartbeat::MAX_INTERRUPTED_RETRY_S + 60.0;
    let latest = interrupted_at(now - 600.0);
    let mut state = State::default();
    let shown = heartbeat::schedule(true, (&latest, &state), &policy, (now, 0.0, false));
    assert_eq!(shown["dormant"], false);
    assert!(!shown["next_heartbeat_at"].is_null());
    state.saw(&interrupted_at(BEGAN));
    state.saw(&latest);
    let shown = heartbeat::schedule(true, (&latest, &state), &policy, (now, 0.0, false));
    assert_eq!(shown["dormant"], true);
    assert!(shown["next_heartbeat_at"].is_null());
    let known = Known {
        latest: latest.clone(),
        ..Known::default()
    };
    assert_eq!(
        heartbeat::skip_reason(&known, &state, &policy, now),
        "dormant"
    );
    // Two minutes before the eleven hours are over it is only not due yet.
    let before = now - 180.0;
    assert_eq!(
        heartbeat::skip_reason(&known, &state, &policy, before),
        "not-due"
    );
}

#[test]
fn a_run_of_interruptions_ends_with_a_turn_that_completes_and_with_a_persons_word() {
    let seen = |kind: &str, at: f64, detail: Value| Latest {
        kind: kind.to_string(),
        ts_ms: at * 1000.0,
        detail,
    };
    let mut state = State::default();
    state.saw(&interrupted_at(BEGAN));
    assert_eq!(state.interrupted_since, BEGAN);
    // A later interruption is of the same run.
    state.saw(&interrupted_at(BEGAN + 3_600.0));
    assert_eq!(state.interrupted_since, BEGAN);
    // While a turn of it runs, nothing is known of how it will end.
    for kind in ["thinking", "tool", "waiting", "compacting", "spawned"] {
        state.saw(&seen(kind, BEGAN + 4_000.0, Value::Null));
        assert_eq!(state.interrupted_since, BEGAN, "{kind}");
    }
    // A turn that completed ends the run, and the next interruption begins another.
    for kind in ["done", "idle", "background"] {
        let mut state = state.clone();
        state.saw(&seen(kind, BEGAN + 5_000.0, Value::Null));
        assert_eq!(state.interrupted_since, 0.0, "{kind}");
        state.saw(&interrupted_at(BEGAN + 6_000.0));
        assert_eq!(state.interrupted_since, BEGAN + 6_000.0, "{kind}");
    }
    // Its user's own stop is no interruption to take up, and ends the run.
    let mut stopped = state.clone();
    let by_its_user = serde_json::json!({ "source": "user_stop" });
    stopped.saw(&seen("interrupted", BEGAN + 5_000.0, by_its_user));
    assert_eq!(stopped.interrupted_since, 0.0);
    // A person's word wakes an agent that was left alone: it is tried again from there.
    state.dormant = true;
    assert!(state.woken_by(7));
    assert_eq!((state.interrupted_since, state.dormant), (0.0, false));
    // And a heartbeat that did something ends the run as well.
    state.saw(&interrupted_at(BEGAN + 7_000.0));
    state.was_active();
    assert_eq!(state.interrupted_since, 0.0);
}
