//! How long a janitor that runs at intervals waits between two runs.
//!
//! Every such run ends as one of three: it `worked` (it found something and did it), it was
//! `idle` (nothing needed doing), or it `failed`. Idle doubles the wait, from the janitor's
//! base (the interval it is set to, or the time between two runs of its schedule) up to
//! its longest; worked puts it back to the base; failed leaves it as it is. A run that
//! says nothing counts as worked, so that a janitor that forgets to say keeps the cadence
//! it was given instead of drifting to a day. Set anew (another base, another longest), a
//! janitor starts from its base.
//!
//! The rule is the Python Host's (`server/lib/janitor_adaptive.py`), which reads and keeps
//! nothing either: whoever calls it keeps the [`State`]. Held to that module by
//! `tests/fixtures/janitor-adaptive-vectors.json` (`server-rs/tools/janitor-adaptive-vectors.py`).

use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

use crate::python;

/// What a run can end as.
pub const WORKED: &str = "worked";
pub const IDLE: &str = "idle";
pub const FAILED: &str = "failed";
/// The longest a janitor waits when nothing says, and whatever is said: a day, and a week.
pub const LONGEST_S: i64 = 86_400;
pub const LIMIT_S: i64 = 7 * 86_400;
/// How much of what a run said of itself is kept, in signs.
pub const SUMMARY_MOST: usize = 300;

/// What is remembered of a janitor's waiting, between its runs.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct State {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub idle_streak: Option<i64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub base_seconds: Option<i64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub max_seconds: Option<i64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub interval_seconds: Option<i64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub last_activity: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub last_summary: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub last_run_at: Option<i64>,
}

/// Whether a word is one a run can end as.
pub fn known(activity: &str) -> bool {
    [WORKED, IDLE, FAILED].contains(&activity)
}

/// What a finished run counts as: what it ended as, what it said of itself, and whether it
/// said so itself. A run that failed is failed whatever it said; one that said nothing
/// worked.
pub fn outcome(
    status: &str,
    ended: &str,
    activity: &str,
    said: &str,
    error: &str,
) -> (&'static str, String, bool) {
    let summary = python::first(said, SUMMARY_MOST).to_string();
    let reported = known(activity);
    let broke = |word: &str| word == "failed" || word == "error";
    if broke(status) || broke(ended) {
        let why = match summary.is_empty() {
            true => python::first(error, SUMMARY_MOST).to_string(),
            false => summary,
        };
        return (FAILED, why, reported);
    }
    match activity {
        IDLE => (IDLE, summary, true),
        FAILED => (FAILED, summary, true),
        _ => (WORKED, summary, reported),
    }
}

/// The base and the longest a janitor waits. A longest of nothing, or one below the base,
/// keeps the wait at the base.
pub fn bounds(base_seconds: i64, longest_seconds: Option<i64>) -> (i64, i64) {
    let base = base_seconds.max(1);
    let longest = longest_seconds.unwrap_or(LONGEST_S).min(LIMIT_S);
    (base, longest.max(base))
}

fn set_as(state: &State, base: i64, longest: i64) -> bool {
    state.base_seconds == Some(base) && state.max_seconds == Some(longest)
}

/// How long a janitor waits now, in seconds, with no run counted.
pub fn current(state: &State, base_seconds: i64, longest_seconds: Option<i64>) -> i64 {
    let (base, longest) = bounds(base_seconds, longest_seconds);
    if !set_as(state, base, longest) {
        return base;
    }
    // An interval of nothing is none: the base.
    let kept = state.interval_seconds.filter(|interval| *interval != 0);
    kept.unwrap_or(base).max(base).min(longest)
}

/// Counts one finished run, and answers how long the janitor waits next, in seconds.
/// `None`: the run ended as nothing a run can end as, and nothing is counted.
pub fn advance(
    state: &mut State,
    activity: &str,
    summary: &str,
    (base_seconds, longest_seconds): (i64, Option<i64>),
    now_ms: Option<i64>,
) -> Option<i64> {
    if !known(activity) {
        return None;
    }
    let (base, longest) = bounds(base_seconds, longest_seconds);
    let mut interval = current(state, base, Some(longest));
    if !set_as(state, base, longest) {
        state.idle_streak = Some(0);
    }
    match activity {
        IDLE => {
            interval = (interval * 2).min(longest);
            state.idle_streak = Some(state.idle_streak.unwrap_or(0) + 1);
        }
        WORKED => {
            interval = base;
            state.idle_streak = Some(0);
        }
        _ => {}
    }
    state.base_seconds = Some(base);
    state.max_seconds = Some(longest);
    state.interval_seconds = Some(interval);
    state.last_activity = Some(activity.to_string());
    state.last_summary = Some(python::first(summary, SUMMARY_MOST).to_string());
    if let Some(now_ms) = now_ms {
        state.last_run_at = Some(now_ms);
    }
    Some(interval)
}

/// What an app is shown of one thing a janitor does at intervals (`lane`): the wait it is
/// set to, the wait it has now, the longest, and when it runs next.
pub fn cadence(
    state: &State,
    (base_seconds, longest_seconds): (i64, Option<i64>),
    next_run_at: Option<i64>,
    lane: &str,
) -> Value {
    let (base, longest) = bounds(base_seconds, longest_seconds);
    let streak = match state.base_seconds == Some(base) {
        true => state.idle_streak.unwrap_or(0),
        false => 0,
    };
    json!({
        "lane": lane,
        "base_interval_seconds": base,
        "current_interval_seconds": current(state, base, Some(longest)),
        "max_interval_seconds": longest,
        "adaptive": longest > base,
        "idle_streak": streak,
        "last_activity": state.last_activity,
        "last_summary": state.last_summary.clone().unwrap_or_default(),
        "next_run_at": next_run_at,
    })
}
