//! Whether an agent can answer now (Host feature `agent_availability`,
//! #918, desktop #924). The Host puts `availability` {state, text,
//! since_ms, back_at_ms, queue_ahead, last_alive_ms} on every agent row of
//! `/agents/snapshot` and `/agents/{session}`, and sends it again in an
//! `agent-roster` event of kind `availability` (`fields`) when it changes.
//! Just before a planned restart it sends one `host-updating` event
//! {text, since_ms, back_at_ms}: until the desktop has read the snapshot
//! again, every agent of that Host shows "Host updating".
//!
//! The times are the Host's clock (epoch ms); the line counts with the
//! offset to it, so it never goes stale here. An older Host sends neither,
//! and nothing new shows. The words are the Host's `text`; only the time
//! part ("back in ~20 s", "last sign of life 4 min ago") is made here.

use chrono::{DateTime, TimeZone};
use serde_json::Value;

use crate::json::Object;

/// The Host's feature name in `/server-info`.
pub const FEATURE: &str = "agent_availability";
/// The `agent-roster` kind whose `fields` carry `availability`.
pub const ROSTER_KIND: &str = "availability";
/// The Host's event before a planned restart.
pub const HOST_UPDATING: &str = "host-updating";

/// One agent's `availability`, as the Host sent it.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Availability {
    /// For logic only: `ok` shows nothing.
    pub state: String,
    /// The Host's line without its time part ("Restarting", "Busy · 25
    /// messages ahead of you"), shown as sent; "" at `ok`.
    pub text: String,
    pub since_ms: i64,
    pub back_at_ms: Option<i64>,
    pub queue_ahead: i64,
    pub last_alive_ms: Option<i64>,
}

fn positive(object: &Object, key: &str) -> Option<i64> {
    object.get(key).and_then(Value::as_i64).filter(|t| *t > 0)
}

fn text(object: &Object, key: &str) -> String {
    object.get(key).and_then(Value::as_str).unwrap_or_default().trim().to_owned()
}

impl Availability {
    /// A row's `availability`: None when it is missing, not an object or
    /// names no state.
    pub fn from_json(value: Option<&Value>) -> Option<Self> {
        let object = value?.as_object()?;
        let state = text(object, "state").to_ascii_lowercase();
        if state.is_empty() {
            return None;
        }
        Some(Self {
            state,
            text: text(object, "text"),
            since_ms: positive(object, "since_ms").unwrap_or(0),
            back_at_ms: positive(object, "back_at_ms"),
            queue_ahead: object.get("queue_ahead").and_then(Value::as_i64).unwrap_or(0).max(0),
            last_alive_ms: positive(object, "last_alive_ms"),
        })
    }
}

/// A Host's `host-updating` event: it restarts, and is back about then.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct HostUpdating {
    /// The Host's words ("Host updating"), shown as sent.
    pub text: String,
    pub since_ms: i64,
    pub back_at_ms: Option<i64>,
}

impl HostUpdating {
    pub fn from_event(event: &Object) -> Self {
        Self {
            text: text(event, "text"),
            since_ms: positive(event, "since_ms").unwrap_or(0),
            back_at_ms: positive(event, "back_at_ms"),
        }
    }
}

/// Further off than this, the time part names the clock time.
const COUNT_UP_TO_MS: i64 = 10 * 60_000;

/// "back in ~40 s", "back in ~3 min", or "back at 14:05" further off;
/// None once the time has passed.
fn back<Tz: TimeZone>(back_at_ms: i64, host_now_ms: i64, offset_ms: i64, zone: &Tz) -> Option<String>
where
    Tz::Offset: std::fmt::Display,
{
    let left_ms = back_at_ms - host_now_ms;
    if left_ms <= 0 {
        return None;
    }
    let seconds = (left_ms + 999) / 1000;
    Some(if left_ms > COUNT_UP_TO_MS {
        format!("back {}", at(back_at_ms, host_now_ms, offset_ms, zone))
    } else if seconds < 60 {
        // Rounded up to 5 s: a calm count, not a stopwatch.
        format!("back in ~{} s", (seconds + 4) / 5 * 5)
    } else {
        format!("back in ~{} min", (seconds + 30) / 60)
    })
}

/// "at 14:05" in this machine's zone, "at Fri 14:05" on another day.
fn at<Tz: TimeZone>(host_ms: i64, host_now_ms: i64, offset_ms: i64, zone: &Tz) -> String
where
    Tz::Offset: std::fmt::Display,
{
    let local = host_ms - offset_ms;
    let clock = crate::time_format::clock_at(local, zone);
    let day = |ms: i64| zone.timestamp_millis_opt(ms).single().map(|d: DateTime<Tz>| d.date_naive());
    match (day(local), day(host_now_ms - offset_ms)) {
        (Some(then), Some(today)) if then != today => format!("at {} {clock}", then.format("%a")),
        _ => format!("at {clock}"),
    }
}

/// "45 s", "4 min", "2 h", "3 d".
fn ago(milliseconds: i64) -> String {
    let seconds = milliseconds.max(0) / 1000;
    if seconds < 60 {
        format!("{seconds} s")
    } else if seconds < 3600 {
        format!("{} min", seconds / 60)
    } else if seconds < 86_400 {
        format!("{} h", seconds / 3600)
    } else {
        format!("{} d", seconds / 86_400)
    }
}

fn joined(head: &str, tail: Option<String>) -> String {
    match tail {
        Some(tail) => format!("{head} · {tail}"),
        None => head.to_owned(),
    }
}

/// The one line for an agent, or empty when it can answer now. The words
/// are the Host's `text` as sent; only the time part is added here, since
/// only this side can count. A Host that is updating comes first; then the
/// agent's own state. `local_now_ms` is this machine's clock, `offset_ms`
/// the Host's clock minus it, and `zone` the zone clock times are shown in.
pub fn line<Tz: TimeZone>(
    availability: Option<&Availability>,
    updating: Option<&HostUpdating>,
    offset_ms: i64,
    local_now_ms: i64,
    zone: &Tz,
) -> String
where
    Tz::Offset: std::fmt::Display,
{
    let host_now = local_now_ms + offset_ms;
    if let Some(updating) = updating.filter(|u| !u.text.is_empty()) {
        return joined(&updating.text, updating.back_at_ms.and_then(|b| back(b, host_now, offset_ms, zone)));
    }
    let Some(a) = availability.filter(|a| a.state != "ok" && !a.text.is_empty()) else { return String::new() };
    let time = if a.state == "unresponsive" {
        a.last_alive_ms.map(|t| format!("last sign of life {} ago", ago(host_now - t)))
    } else {
        a.back_at_ms.and_then(|b| back(b, host_now, offset_ms, zone))
    };
    joined(&a.text, time)
}

/// Drops `availability` from a cached snapshot's rows: a state read at an
/// earlier start says nothing about now.
pub fn strip_cached(snapshot: &mut Object) {
    if let Some(Value::Array(agents)) = snapshot.get_mut("agents") {
        for agent in agents.iter_mut().filter_map(Value::as_object_mut) {
            agent.remove("availability");
        }
    }
}
