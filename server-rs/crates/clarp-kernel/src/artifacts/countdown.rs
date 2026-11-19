//! The countdown projection used by Updates. The Python attention index
//! supplies no expiry policy, acknowledgements or per-occurrence archives.
use super::{Refused, refuse};
use jiff::{civil, tz::TimeZone};
use regex::Regex;
use serde_json::{Map, Value};
use std::sync::LazyLock;

static TARGET: LazyLock<Option<Regex>> = LazyLock::new(|| {
    Regex::new(r"^([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})(?:\.([0-9]+))?(Z|[+-][0-9]{2}:[0-9]{2})$").ok()
});
const SHAPE: &str = "countdown target_at must be an ISO 8601 timestamp with offset";
const DATE: &str = "countdown target_at requires a valid date/time and explicit UTC offset";

fn target(value: Option<&Value>) -> Result<i64, &'static str> {
    let parts = value
        .and_then(Value::as_str)
        .and_then(|s| TARGET.as_ref()?.captures(s))
        .ok_or(SHAPE)?;
    let civil = &parts[1];
    if civil.starts_with("0000") || &civil[11..13] == "24" || &civil[17..19] == "60" {
        return Err(DATE);
    }
    let fraction = parts.get(2).map_or("", |m| m.as_str());
    let fraction = &fraction[..fraction.len().min(6)];
    let local = format!("{civil}.{:0<6}", fraction)
        .parse::<civil::DateTime>()
        .map_err(|_| DATE)?;
    // The input is a civil date with a separate offset. Jiff reserves
    // headroom at its timestamp endpoints for zone conversions.
    let micros = local
        .duration_since(civil::datetime(1970, 1, 1, 0, 0, 0, 0))
        .as_micros();
    let offset = &parts[3];
    let seconds = if offset == "Z" {
        0
    } else {
        let minutes = offset[1..3].parse::<i64>().map_err(|_| DATE)? * 60
            + offset[4..6].parse::<i64>().map_err(|_| DATE)?;
        if minutes >= 24 * 60 {
            return Err(DATE);
        }
        minutes * 60 * if offset.starts_with('-') { -1 } else { 1 }
    };
    // datetime.timestamp() returns seconds as a double; Python then truncates milliseconds toward zero.
    Ok(((micros - i128::from(seconds) * 1_000_000) as f64 / 1_000_000.0 * 1000.0) as i64)
}

pub(super) fn validate(payload: &Map<String, Value>) -> Result<(), Refused> {
    if let Err(error) = target(payload.get("target_at")) {
        return refuse(error);
    }
    let Some(name) = payload
        .get("time_zone")
        .and_then(Value::as_str)
        .filter(|s| !s.trim().is_empty())
    else {
        return refuse("countdown time_zone must be an IANA timezone");
    };
    if !TimeZone::get(name)
        .ok()
        .is_some_and(|zone| zone.iana_name() == Some(name))
    {
        return refuse("countdown time_zone must be a known IANA timezone");
    }
    Ok(())
}

pub fn project(
    status: &str,
    payload: &Map<String, Value>,
    now: i64,
) -> (&'static str, &'static str) {
    match status {
        "draft" => ("history", "draft"),
        "cancelled" => ("history", "cancelled"),
        "expired" => ("history", "expired"),
        "active" => ("working", "active"),
        "failed" => ("correction", "failed"),
        "ready" | "completed" => match target(payload.get("target_at")) {
            Ok(ms) => (
                "review",
                if now < ms {
                    "upcoming"
                } else if now.saturating_sub(ms) < 60_000 {
                    "due"
                } else {
                    "elapsed"
                },
            ),
            Err(_) => ("correction", "invalid_target"),
        },
        _ => ("unsupported", "unknown"),
    }
}
