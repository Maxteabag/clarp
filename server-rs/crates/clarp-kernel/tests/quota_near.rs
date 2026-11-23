//! What an agent's row says of an account near a limit of its usage, from what Codex and
//! Claude Code said of their windows: it appears at the window's share, follows what is used,
//! and clears when the window begins anew or the account is another.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_kernel::quota::{Kept, Login, Tool, moment};
use clarp_kernel::standing::Standing;
use serde_json::json;

const NOW: i64 = 1_900_000_000_000;
const MINUTE: i64 = 60_000;
const HOUR: i64 = 60 * MINUTE;

#[test]
fn a_codex_account_is_near_from_four_fifths_of_its_five_hours_until_they_begin_anew() {
    let tool = Tool {
        instance: "host:codex",
        provider: "codex",
    };
    let on = Login {
        generation: "a-login",
        account: Some("an-account"),
    };
    let mut kept = Kept::default();
    let mut new_id = |prefix: &str| format!("{prefix}{}", NOW);
    let resets = NOW + 2 * HOUR;
    let record = |used: f64| {
        let five =
            json!({ "used_percentage": used, "resets_at": moment(resets), "window_minutes": 300 });
        let week = json!({ "used_percentage": 40.0, "resets_at": moment(NOW + 100 * HOUR), "window_minutes": 10_080 });
        json!({ "windows": { "primary": five, "secondary": week } })
    };
    kept.seen(tool, on, &record(70.0), "a rollout", NOW, &mut new_id);
    assert_eq!(kept.near(tool, Some(on), NOW), None);
    kept.seen(
        tool,
        on,
        &record(83.0),
        "a rollout",
        NOW + MINUTE,
        &mut new_id,
    );
    let near = kept.near(tool, Some(on), NOW + MINUTE).unwrap();
    assert_eq!(
        near,
        json!({
            "state": "near", "provider_id": "codex", "reason": "usage_limit",
            "window": "five_hour", "used_percentage": 83.0, "resets_at": moment(resets),
            "observed_at": moment(NOW + MINUTE),
        })
    );
    // An hour on, with no newer word: what is used has not gone down, so it still stands.
    assert!(kept.near(tool, Some(on), NOW + HOUR).is_some());
    // On another account the Host is on now: nothing is said of it.
    let other = Login {
        generation: "another-login",
        account: Some("another-account"),
    };
    assert_eq!(kept.near(tool, Some(other), NOW + HOUR), None);
    // On no login that can be named: an old word says nothing, a fresh one still does.
    assert_eq!(kept.near(tool, None, NOW + HOUR), None);
    assert!(kept.near(tool, None, NOW + 2 * MINUTE).is_some());
    // The window began anew.
    assert_eq!(kept.near(tool, Some(on), resets), None);
    // All used: the account is used up, which is said apart, and no longer near.
    kept.seen(
        tool,
        on,
        &record(100.0),
        "a rollout",
        NOW + 2 * MINUTE,
        &mut new_id,
    );
    assert_eq!(kept.near(tool, Some(on), NOW + 2 * MINUTE), None);
    assert!(kept.used_up(tool, Some(on), NOW + 2 * MINUTE).is_some());
}

#[test]
fn a_claude_account_is_near_by_its_week_from_nine_tenths() {
    let tool = Tool {
        instance: "host:claude",
        provider: "claude",
    };
    let mut kept = Standing::default();
    let week_resets = moment(NOW + 30 * HOUR);
    let word = |five: f64, week: f64| {
        json!({
            "five_hour": { "used_percentage": five, "resets_at": moment(NOW + 3 * HOUR), "window_minutes": 300 },
            "seven_day": { "used_percentage": week, "resets_at": week_resets, "window_minutes": 10_080 },
        })
    };
    kept.seen(&word(30.0, 88.0), Some("acct"), "a word", NOW);
    assert_eq!(kept.near(tool, Some("acct"), NOW), None);
    kept.seen(&word(30.0, 91.5), Some("acct"), "a word", NOW + MINUTE);
    let near = kept.near(tool, Some("acct"), NOW + MINUTE).unwrap();
    assert_eq!(
        (
            &near["window"],
            &near["used_percentage"],
            &near["resets_at"]
        ),
        (&json!("seven_day"), &json!(91.5), &json!(week_resets))
    );
    // Another account is in use: its word says nothing of it.
    assert_eq!(kept.near(tool, Some("another"), NOW + MINUTE), None);
    // On no account that can be named: a fresh word counts, an old one does not.
    assert!(kept.near(tool, None, NOW + MINUTE).is_some());
    assert_eq!(kept.near(tool, None, NOW + 2 * HOUR), None);
    // Both near: the more used is named.
    kept.seen(&word(97.0, 91.5), Some("acct"), "a word", NOW + 2 * MINUTE);
    let near = kept.near(tool, Some("acct"), NOW + 2 * MINUTE).unwrap();
    assert_eq!(near["window"], "five_hour");
    // The five hours began anew; the week still stands near.
    let after = NOW + 3 * HOUR;
    assert_eq!(
        kept.near(tool, Some("acct"), after).unwrap()["window"],
        "seven_day"
    );
}
