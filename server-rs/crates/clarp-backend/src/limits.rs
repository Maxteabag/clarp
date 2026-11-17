//! What a tool says of its account's limits: how much of each window is used, and when the
//! window begins anew.
//!
//! Codex says it by itself, with every count of tokens it writes into a conversation's
//! rollout, and in the same words where its usage endpoint or its app-server is asked. What is
//! read of such a record is what the Python Host reads of it (`backend_usage_codex.py`,
//! `_codex_snapshot_from_payload`; `backend_usage_store.py`, `_window_from_payload`), held to
//! that code's own answers (`tests/fixtures/codex-limits-vectors.json`, written by
//! `server-rs/tools/codex-limits-vectors.py`). Nothing here asks anybody anything.

use serde_json::{Map, Value, json};

/// A number, as the Python Host takes one: a number, or a text that is one. A yes or no is
/// none.
fn number(value: &Value) -> Option<f64> {
    match value {
        Value::Number(number) => number.as_f64(),
        Value::String(text) => text.trim().parse::<f64>().ok(),
        _ => None,
    }
}

/// The last second the Python Host's calendar has: the end of the year 9999.
const LAST_SECOND: f64 = 253_402_300_799.0;

/// A second since 1970 as a date and a time of day in UTC (the rule of days into dates that
/// every calendar library uses), for any second from 1970 to the end of the year 9999.
fn written(second: i64) -> String {
    let (days, rest) = (second.div_euclid(86_400), second.rem_euclid(86_400));
    let shifted = days + 719_468;
    let (era, day_of_era) = (shifted.div_euclid(146_097), shifted.rem_euclid(146_097));
    let year_of_era =
        (day_of_era - day_of_era / 1_460 + day_of_era / 36_524 - day_of_era / 146_096) / 365;
    let day_of_year = day_of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);
    let shifted_month = (5 * day_of_year + 2) / 153;
    let day = day_of_year - (153 * shifted_month + 2) / 5 + 1;
    let month = if shifted_month < 10 {
        shifted_month + 3
    } else {
        shifted_month - 9
    };
    let year = year_of_era + era * 400 + i64::from(month <= 2);
    let (hour, minute, of_minute) = (rest / 3_600, rest % 3_600 / 60, rest % 60);
    format!("{year:04}-{month:02}-{day:02}T{hour:02}:{minute:02}:{of_minute:02}Z")
}

/// A moment given as seconds since 1970, as text: to the second, in UTC; empty for a moment
/// that is none (zero, or before). `None` for one no calendar has.
fn moment(seconds: f64) -> Option<String> {
    if seconds.is_nan() || seconds >= LAST_SECOND + 1.0 {
        return None;
    }
    if seconds <= 0.0 {
        return Some(String::new());
    }
    // Whole seconds: what is left of one is dropped.
    let whole = format!("{:.0}", seconds.floor()).parse::<i64>().ok()?;
    Some(written(whole))
}

/// When a window begins anew, as text (`_normalize_time`): a number is seconds since 1970,
/// and so is a text that is a number; any other text is kept as it is, with `+00:00`
/// written as `Z`. Nothing where nothing is said. A number no calendar has is no time (the
/// Python Host's reader breaks on it); a text that is such a number is kept as the text it
/// is, as there.
fn time(value: Option<&Value>) -> String {
    match value {
        None | Some(Value::Null) => String::new(),
        Some(Value::Number(number)) => number.as_f64().and_then(moment).unwrap_or_default(),
        // A yes is a number to the Python Host: one second.
        Some(Value::Bool(yes)) => moment(if *yes { 1.0 } else { 0.0 }).unwrap_or_default(),
        Some(Value::String(text)) => {
            let raw = text.trim();
            match raw.parse::<f64>() {
                _ if raw.is_empty() => String::new(),
                Ok(seconds) => moment(seconds).unwrap_or_else(|| raw.replace("+00:00", "Z")),
                Err(_) => raw.replace("+00:00", "Z"),
            }
        }
        Some(other) => other.to_string(),
    }
}

/// The value under the first of these names that the record has at all, also where it has
/// it with nothing in it.
fn first<'a>(fields: &'a Map<String, Value>, names: &[&str]) -> Option<&'a Value> {
    names.iter().find_map(|name| fields.get(*name))
}

/// A whole number of minutes out of a count of seconds, as Python's `int(...) // 60` gives it.
fn minutes_of(seconds: &Value) -> Option<i64> {
    let whole = match seconds {
        Value::Number(number) => match number.as_i64() {
            Some(whole) => whole,
            None => format!("{:.0}", number.as_f64()?.trunc()).parse().ok()?,
        },
        Value::Bool(yes) => i64::from(*yes),
        Value::String(text) => text.trim().parse::<i64>().ok()?,
        _ => return None,
    };
    Some(whole.div_euclid(60))
}

/// One window of a record of limits (`_window_from_payload`): how much of it is used, when
/// it begins anew, and how long it is where the record says so. Nothing for what is no
/// window, and for one that says neither how much is used nor when it begins anew.
pub fn window(payload: &Value) -> Option<Value> {
    let fields = payload.as_object()?;
    let used = first(fields, &["used_percentage", "used_percent", "usedPercent"]).and_then(number);
    let resets_at = time(first(fields, &["resets_at", "reset_at", "resetsAt"]));
    let mut minutes = match fields.get("window_minutes") {
        Some(said) => said.clone(),
        None => fields
            .get("windowDurationMins")
            .cloned()
            .unwrap_or(Value::Null),
    };
    if minutes.is_null()
        && let Some(seconds) = fields
            .get("limit_window_seconds")
            .filter(|said| !said.is_null())
    {
        minutes = minutes_of(seconds).map_or(Value::Null, |whole| json!(whole));
    }
    if used.is_none() && resets_at.is_empty() {
        return None;
    }
    let mut out = Map::new();
    out.insert("used_percentage".to_string(), json!(used));
    out.insert("resets_at".to_string(), json!(resets_at));
    if !minutes.is_null() {
        out.insert("window_minutes".to_string(), minutes);
    }
    Some(Value::Object(out))
}

/// Whether a bucket of limits says of itself that it is Codex's own.
fn codex_by_name(bucket: &Value) -> bool {
    let named = bucket.get("limitId").and_then(Value::as_str);
    named.is_some_and(|name| name.to_lowercase() == "codex")
}

/// The bucket of limits that is Codex's own, in a record of any of the shapes Codex gives
/// one in (`_select_rate_limit_payload`): under the endpoint's name; or among the buckets of
/// its app-server, the one named for Codex, else the only one whose first window is five
/// hours long, else the only one there is where they are not given by name. Where more than
/// one could be meant, none is taken.
fn bucket(payload: &Map<String, Value>) -> Option<&Value> {
    if let Some(snake) = payload.get("rate_limit").filter(|said| said.is_object()) {
        return Some(snake);
    }
    let mut candidates: Vec<&Value> = Vec::new();
    match payload.get("rateLimits") {
        Some(one) if one.is_object() => candidates.push(one),
        Some(Value::Array(many)) => candidates.extend(many.iter().filter(|one| one.is_object())),
        _ => {}
    }
    let by_id = payload
        .get("rateLimitsByLimitId")
        .and_then(Value::as_object);
    if let Some(by_id) = by_id {
        let keyed = by_id.iter().filter(|(_, bucket)| bucket.is_object());
        if let Some((_, own)) = keyed.clone().find(|(key, _)| key.to_lowercase() == "codex") {
            return Some(own);
        }
        if let Some((_, own)) = keyed.clone().find(|(_, bucket)| codex_by_name(bucket)) {
            return Some(own);
        }
        candidates.extend(keyed.map(|(_, bucket)| bucket));
    }
    if let Some(own) = candidates.iter().find(|bucket| codex_by_name(bucket)) {
        return Some(own);
    }
    let five_hours = |bucket: &&&Value| {
        let first = bucket.get("primary").and_then(window);
        let minutes = first.as_ref().and_then(|first| first.get("window_minutes"));
        minutes.and_then(Value::as_f64) == Some(300.0)
    };
    let mut of_five_hours = candidates.iter().filter(five_hours);
    if let (Some(only), None) = (of_five_hours.next(), of_five_hours.next()) {
        return Some(only);
    }
    // A single bucket that came by itself, with no list of buckets by name, is the one the
    // connection it came on is about.
    match candidates[..] {
        [only] if by_id.is_none() => Some(only),
        _ => None,
    }
}

/// What a record of Codex's limits says (`_codex_snapshot_from_payload`): its windows (the
/// first, the second, and a limit of the account's own where it has one), and of the first
/// of them that is there how much is used and when it begins anew. Refused, in the Python
/// Host's words, where the record is none or has no window that can be read.
pub fn codex(payload: &Value) -> Result<Value, String> {
    let Some(fields) = payload.as_object() else {
        return Err("codex usage response was not an object".to_string());
    };
    let Some(limits) = bucket(fields).and_then(Value::as_object) else {
        return Err("codex usage response missing rate_limit".to_string());
    };
    let named = |window_name: &str, short: &str| {
        let said = limits.get(window_name).or_else(|| limits.get(short));
        said.and_then(window)
    };
    let primary = named("primary_window", "primary");
    let secondary = named("secondary_window", "secondary");
    let own = fields.get("spend_control").and_then(Value::as_object);
    let individual = own
        .and_then(|own| own.get("individual_limit"))
        .and_then(window);
    let mut windows = Map::new();
    let found = [
        ("primary", &primary),
        ("secondary", &secondary),
        ("individual_limit", &individual),
    ];
    for (name, found) in found {
        if let Some(found) = found {
            windows.insert(name.to_string(), found.clone());
        }
    }
    if windows.is_empty() {
        return Err("codex usage response had no usable windows".to_string());
    }
    let preferred = primary.or(secondary).or(individual).unwrap_or(Value::Null);
    let resets_at = preferred
        .get("resets_at")
        .and_then(Value::as_str)
        .unwrap_or("");
    Ok(json!({
        "used_percentage": preferred.get("used_percentage").cloned().unwrap_or(Value::Null),
        "resets_at": resets_at,
        "windows": windows,
    }))
}
