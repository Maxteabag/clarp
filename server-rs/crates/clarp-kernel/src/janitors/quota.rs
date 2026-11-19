//! The Quota keeper (the janitor job `quota-monitor`): what it reads of the tools' accounts,
//! when it tells the owner, and what it keeps of each window between two looks.
//!
//! This is the Python Host's rule (`server/lib/janitor_autonomy.py`: `quota_window_key`,
//! `seed_quota_window_state`, `quota_crossing`, the loop of `AutonomyJanitors.quota_once`
//! over a reading's windows and the receipt it makes of each crossing; Claude's
//! `quota_identity` in `server/lib/backend/claude.py`), as a thing that is handed a reading,
//! the keeper's options and its memory, and answers what to keep and whom to tell. Held to
//! that code's own answers (`tests/fixtures/quota-keeper-vectors.json`, written by
//! `server-rs/tools/quota-keeper-vectors.py`).
//!
//! **A window** of a reading counts where it is fresh, says how much of it is used as a
//! number, and says when that was seen. It is remembered by its tool, its account and its
//! identity: its id, or for Claude, which says the moment a window begins anew a little
//! differently from one reading to the next, its kind and that moment to the minute. What is
//! remembered is when it was last seen and what was left of it then.
//!
//! **The owner is told** once where what is left of a window falls to the keeper's threshold
//! or under it from above, and once more where it falls to nothing. A reading that is not
//! newer than the one remembered says nothing and changes nothing; one that says more than
//! all or less than nothing was used is no reading.
//!
//! One thing is not as there: a window whose time of seeing is no text (nothing, a number) is
//! passed over here, where the Python Host's look breaks on it and every look after it does
//! too, its run left running (`docs/reference-defects.md`).

use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

use super::adaptive::{self, State};
use crate::python;
use crate::recovery::Accounts;

/// The job, as the catalogue names it.
pub const ROLE: &str = "quota-monitor";
/// What the name of a window's memory begins with.
pub const WINDOW: &str = "quota-keeper.window.";
/// The tools whose notices an automatic Hotseat switcher tells itself.
pub const SWITCHED: [&str; 2] = ["claude", "codex"];
/// What a receipt of a crossing gives as its reason.
pub const REASON: &str = "quota_threshold";
/// What a receipt says where the switcher tells the owner itself.
pub const SWITCHER_TELLS: &str = "Automatic Hotseat owns provider notifications";
/// What a receipt not yet delivered says when its keeper was set anew.
pub const SET_ANEW: &str = "Janitor configuration changed";
/// How much of why a run of the keeper failed to recover is kept, in signs.
const REASON_MOST: usize = 500;

/// A value's digest as the Python Host takes it (`janitor_autonomy.digest`): SHA-256 of its
/// JSON with the fields in the order of their names, nothing between them, in ASCII.
pub fn digest(value: &Value) -> String {
    crate::live_call::sum(python::sorted_ascii(value).as_bytes())
}

/// Python's `float // float` (`float_floor_div`): the quotient taken down, as Python takes it.
fn floor_div(dividend: f64, divisor: f64) -> f64 {
    let rest = dividend % divisor;
    let mut quotient = (dividend - rest) / divisor;
    if rest != 0.0 && (divisor < 0.0) != (rest < 0.0) {
        quotient -= 1.0;
    }
    if quotient == 0.0 {
        return 0f64.copysign(dividend / divisor);
    }
    let floor = quotient.floor();
    if quotient - floor > 0.5 {
        floor + 1.0
    } else {
        floor
    }
}

/// A moment written as `datetime.fromisoformat` reads it once a `Z` is read as `+00:00`: the
/// microseconds since the epoch, and whether it said its offset. One without its offset is
/// taken in UTC (the Python Host takes it in the machine's zone, which its own readings,
/// written in UTC with their `Z`, never need).
pub fn instant(text: &str) -> Option<(i64, bool)> {
    let text = text.replace('Z', "+00:00");
    if let Ok(at) = text.parse::<jiff::Timestamp>() {
        return Some((at.as_microsecond(), true));
    }
    let civil = text.parse::<jiff::civil::DateTime>().ok()?;
    let at = civil.to_zoned(jiff::tz::TimeZone::UTC).ok()?;
    Some((at.timestamp().as_microsecond(), false))
}

/// Python's `timestamp()` of a moment, in seconds, a fraction.
fn seconds(microseconds: i64) -> f64 {
    microseconds as f64 / 1e6
}

/// When a window was seen, in milliseconds, as the keeper reads it:
/// `int(fromisoformat(observed_at).timestamp() * 1000)`. `None`: no moment.
pub fn stamp(observed_at: &Value) -> Option<i64> {
    let (at, _) = instant(observed_at.as_str()?)?;
    Some((seconds(at) * 1000.0).trunc() as i64)
}

/// What a window is told apart by: its id, or for Claude, whose moment a window begins anew
/// wavers by a fraction of a second, its kind and that moment to the nearest minute, where
/// both are said and the moment says its offset.
pub fn identity(provider: &str, window: &Value) -> Value {
    let id = window["window_id"].clone();
    if provider != "claude" {
        return id;
    }
    let (kind, resets) = (&window["kind"], &window["resets_at"]);
    if !python::truthy(kind) || !python::truthy(resets) {
        return id;
    }
    match resets.as_str().and_then(instant) {
        Some((at, true)) => {
            let minute = floor_div(seconds(at) + 30.0, 60.0) as i64;
            json!(["claude-reset-minute-v1", kind, minute])
        }
        _ => id,
    }
}

/// The name a window's memory is kept under.
pub fn window_key(provider: &str, account: &Value, window: &Value) -> String {
    let named = json!([provider, account, identity(provider, window)]);
    format!("{WINDOW}{}", digest(&named))
}

/// The name it was kept under before Claude's windows were told apart to the minute: by the
/// window's id.
pub fn old_key(provider: &str, account: &Value, window: &Value) -> String {
    let named = json!([provider, account, window["window_id"]]);
    format!("{WINDOW}{}", digest(&named))
}

/// What the keeper remembers of the windows, by the names they are kept under: what was last
/// seen of each, `{"stamp", "remaining"}`.
pub trait Memory {
    fn get(&self, key: &str) -> Option<Value>;
    fn set(&mut self, key: &str, kept: Value);
}

/// What was kept under a window's old name is carried to its new one, where nothing is kept
/// under the new one yet.
pub fn seed(provider: &str, account: &Value, window: &Value, memory: &mut dyn Memory) {
    let key = window_key(provider, account, window);
    let old = old_key(provider, account, window);
    if key != old
        && memory.get(&key).is_none()
        && let Some(previous) = memory.get(&old)
    {
        memory.set(&key, previous);
    }
}

/// A number of JSON as a fraction, for comparing.
fn fraction(value: &Value) -> Option<f64> {
    value.as_f64()
}

/// What is left of a window, as `100 - used` makes it: whole where `used` is whole.
fn left_of(used: &Value) -> Option<Value> {
    if let Some(whole) = used.as_i64() {
        return Some(json!(100 - i128::from(whole)));
    }
    used.as_f64().map(|used| json!(100.0 - used))
}

/// One reading of a window against what is remembered of it: the memory to keep (`None`:
/// none is kept, the reading counts for nothing), and what is left where the owner is told.
pub fn crossing(
    prior: Option<&Value>,
    used: &Value,
    threshold: f64,
    stamp: i64,
) -> (Option<Value>, Option<Value>) {
    let Some(remaining) = left_of(used) else {
        return (None, None);
    };
    let left = fraction(&remaining).unwrap_or(f64::NAN);
    if !(0.0..=100.0).contains(&left) {
        return (None, None);
    }
    let prior = prior.filter(|prior| !prior.is_null());
    let before = prior.and_then(|prior| fraction(&prior["remaining"]));
    if let Some(prior) = prior.filter(|prior| python::truthy(prior))
        && prior["stamp"].as_i64().is_some_and(|kept| stamp <= kept)
    {
        return (None, None);
    }
    let above = |than: f64| before.is_some_and(|before| before > than);
    let cross = (left <= threshold && (prior.is_none() || above(threshold)))
        || (left == 0.0 && prior.is_some() && above(0.0));
    let kept = json!({ "stamp": stamp, "remaining": remaining });
    (Some(kept), cross.then(|| json!({ "remaining": remaining })))
}

/// Python's `format(number, 'g')`: six figures, no zeros at the end, and the form with an
/// exponent where the number is under a ten-thousandth or a million or more.
pub fn general(number: f64) -> String {
    if number == 0.0 {
        return if number.is_sign_negative() { "-0" } else { "0" }.to_string();
    }
    if !number.is_finite() {
        return if number.is_nan() {
            "nan".to_string()
        } else if number > 0.0 {
            "inf".to_string()
        } else {
            "-inf".to_string()
        };
    }
    let written = format!("{number:.5e}");
    let (figures, exponent) = written.split_once('e').unwrap_or((&written, "0"));
    let exponent: i32 = exponent.parse().unwrap_or(0);
    let trim = |text: &str| -> String {
        if text.contains('.') {
            text.trim_end_matches('0').trim_end_matches('.').to_string()
        } else {
            text.to_string()
        }
    };
    if (-4..6).contains(&exponent) {
        let places = usize::try_from(5 - exponent).unwrap_or(0);
        trim(&format!("{number:.places$}"))
    } else {
        let sign = if exponent < 0 { '-' } else { '+' };
        format!("{}e{sign}{:02}", trim(figures), exponent.abs())
    }
}

/// The keeper, as a look reads it: its agent and the options it runs with.
#[derive(Debug, Clone, Copy)]
pub struct Owner<'a> {
    pub agent_id: &'a str,
    pub session: &'a str,
    pub name: &'a str,
    pub generation: u64,
    pub threshold: f64,
    pub recovery_mode: &'a str,
}

/// One crossing, as a receipt keeps it.
#[derive(Debug, Clone, PartialEq)]
pub struct Notice {
    pub provider: String,
    pub receipt_id: String,
    /// What the phones are told, with the keeper's mode of recovery and generation.
    pub payload: Value,
    /// Whether nothing is left of the window.
    pub spent: bool,
}

/// The crossings of one look, in the order of the reading: each window that counts is read
/// against the memory, and the memory is kept as the reading says.
pub fn looked(usage: &Value, owner: &Owner<'_>, memory: &mut dyn Memory) -> Vec<Notice> {
    let mut notices = Vec::new();
    let Some(providers) = usage["providers"].as_object() else {
        return notices;
    };
    for (provider_id, provider) in providers {
        let Some(windows) = provider["windows"].as_array() else {
            continue;
        };
        for window in windows {
            let used = &window["used_percentage"];
            if window["freshness"] != "fresh" || !used.is_number() {
                continue;
            }
            let Some(stamp) = stamp(&window["observed_at"]) else {
                continue;
            };
            if window.get("window_id").is_none() {
                continue;
            }
            let account = match provider.get("provider_instance_id") {
                Some(account) => account.clone(),
                None => json!(provider_id),
            };
            let Some(remaining) = left_of(used) else {
                continue;
            };
            if !fraction(&remaining).is_some_and(|left| (0.0..=100.0).contains(&left)) {
                continue;
            }
            seed(provider_id, &account, window, memory);
            let key = window_key(provider_id, &account, window);
            let prior = memory.get(&key);
            let (kept, told) = crossing(prior.as_ref(), used, owner.threshold, stamp);
            if let Some(kept) = kept {
                memory.set(&key, kept);
            }
            if told.is_none() {
                continue;
            }
            let receipt_id = digest(&json!([
                provider_id,
                account,
                window["window_id"],
                stamp,
                owner.generation
            ]));
            let label = window["label"].as_str().filter(|label| !label.is_empty());
            let named = match label {
                Some(label) => format!("{provider_id} {label}"),
                None => provider_id.clone(),
            };
            let left = fraction(&remaining).unwrap_or(0.0);
            let payload = json!({
                "notification_id": receipt_id,
                "session": owner.session,
                "agent_id": owner.agent_id,
                "persona": owner.name,
                "preview": format!("{named}: {}% quota remaining", general(left)),
                "push": true,
                "reason": REASON,
                "recovery_mode": owner.recovery_mode,
                "owner_generation": owner.generation,
            });
            notices.push(Notice {
                provider: provider_id.clone(),
                receipt_id,
                payload,
                spent: used.as_f64().is_some_and(|used| used >= 100.0),
            });
        }
    }
    notices
}

/// What a recovery asked for at a window with nothing left answers on a Host that does not
/// recover yet: the Python Host's words for a tool with no coordinated selector, for a Host
/// with no selector (`--accounts none` and `follow`: it keeps no logins of its own), and for
/// a selector with no runtime to recover with (`--accounts select`, until the keeper
/// recovers here). `None`: none is asked (the keeper only tells).
pub fn recovery(provider: &str, mode: &str, spent: bool, accounts: Accounts) -> Option<Value> {
    if !spent || mode == "notify" {
        return None;
    }
    if !SWITCHED.contains(&provider) {
        let why = "No coordinated account selector for this provider";
        return Some(json!({ "status": "unsupported", "reason": why }));
    }
    Some(match accounts {
        Accounts::Select => json!({ "status": "runtime_unavailable" }),
        Accounts::None | Accounts::Follow => {
            let why = "No runtime account selector configured";
            json!({ "status": "unconfigured", "reason": why })
        }
    })
}

/// What a run of the keeper says of itself: how it went, its line, and its result.
pub fn ran(told: usize, recoveries: &[String]) -> (&'static str, String, Value) {
    let summary = format!("Checked provider quota; {told} threshold notifications");
    let activity = if told > 0 || !recoveries.is_empty() {
        adaptive::WORKED
    } else {
        adaptive::IDLE
    };
    let reason = python::first(&recoveries.join("; "), REASON_MOST).to_string();
    let result = json!({ "summary": summary, "reason": reason, "item_count": told });
    (activity, summary, result)
}

/// Whether a look of the keeper is due at `now_ms`: it was never armed, the moment it was
/// armed for has come, or the janitors' book changed since (`changes`, the count of its
/// writes, against the count it was armed at): the keeper may have been turned on, off or set
/// anew, and nobody has to remember to say so.
pub fn due(armed: Option<(i64, u64)>, now_ms: i64, changes: u64) -> bool {
    armed.is_none_or(|(at, count)| now_ms >= at || changes != count)
}

/// When the keeper looks again, as its lane keeps it (`janitor_autonomy.lane_state`,
/// `lane_next_run`, `lane_start`, `lane_record`): for its agent and generation, the cadence
/// that grows while looks find nothing, and when it runs next. `last_check` is the moment of
/// the last look of any keeper (the Python Host's `quota-keeper.last-check`), which a keeper
/// set anew counts from.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct Lane {
    pub agent_id: String,
    pub generation: u64,
    #[serde(flatten)]
    pub cadence: State,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub next_run_at: Option<i64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub last_check: Option<i64>,
}

impl Lane {
    /// The lane of a keeper: as it was kept where it is this keeper's in this generation;
    /// anew, but for the moment of the last look, otherwise.
    pub fn of(kept: Option<Lane>, agent_id: &str, generation: u64) -> Lane {
        let kept = kept.unwrap_or_default();
        if kept.agent_id == agent_id && kept.generation == generation {
            return kept;
        }
        Lane {
            agent_id: agent_id.to_string(),
            generation,
            last_check: kept.last_check,
            ..Lane::default()
        }
    }

    /// When the keeper looks next.
    pub fn next_run(&self, base_seconds: i64) -> i64 {
        match self.next_run_at {
            Some(at) => at,
            None => self.last_check.unwrap_or(0) + base_seconds * 1000,
        }
    }

    /// A look begins: until it says how it went, the next waits the interval as it stands.
    pub fn start(&mut self, (base, longest): (i64, i64), now_ms: i64) {
        self.last_check = Some(now_ms);
        let interval = adaptive::current(&self.cadence, base, Some(longest));
        self.next_run_at = Some(now_ms + interval * 1000);
    }

    /// A look is over: how it went sets when the next is.
    pub fn record(&mut self, (base, longest): (i64, i64), done: (&str, &str), now_ms: i64) {
        let (activity, summary) = done;
        let bounds = (base, Some(longest));
        if let Some(interval) =
            adaptive::advance(&mut self.cadence, activity, summary, bounds, Some(now_ms))
        {
            self.next_run_at = Some(now_ms + interval * 1000);
        }
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;

    #[test]
    fn a_number_is_written_as_python_writes_it_with_g() {
        for (number, written) in [
            (20.0, "20"),
            (9.5, "9.5"),
            (0.5, "0.5"),
            (100.0 - 99.99, "0.01"),
            (100.0, "100"),
            (0.0001, "0.0001"),
            (0.00001, "1e-05"),
            (1_234_567.0, "1.23457e+06"),
            (25.000_000_1, "25"),
            (33.333_333_333, "33.3333"),
        ] {
            assert_eq!(general(number), written, "{number}");
        }
    }

    #[test]
    fn a_look_is_due_at_its_moment_or_when_the_janitors_changed() {
        assert!(due(None, 0, 0));
        assert!(!due(Some((1_000, 7)), 999, 7));
        assert!(due(Some((1_000, 7)), 1_000, 7));
        // Nobody keeps: nothing is due until a janitor changes, and then at once.
        assert!(!due(Some((i64::MAX, 7)), 5_000, 7));
        assert!(due(Some((i64::MAX, 7)), 5_000, 8));
    }

    #[test]
    fn a_quotient_is_taken_down_as_python_takes_it() {
        assert_eq!(floor_div(119.9, 60.0), 1.0);
        assert_eq!(floor_div(120.0, 60.0), 2.0);
        assert_eq!(floor_div(-0.5, 60.0), -1.0);
    }
}
