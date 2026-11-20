//! Live Shortcut and controller notifications. The sender supplies time and IDs.
use crate::{
    controller_actions::{BUTTONS, GESTURES, word},
    python,
};
use serde_json::{Value, json};

pub const PAGE: &str = "<!doctype html><meta charset=utf-8><title>ok</title>";

fn plain(action: &str, now: i64) -> Result<Value, String> {
    match action {
        "record" | "record-toggle" | "stop-agent" => {
            Ok(json!({"type":"remote-action","action":action,"ts":now}))
        }
        _ => Err("unknown action".into()),
    }
}

/// Python parse_qs drops empty values and the route uses the first value that remains.
pub fn get(query: &str, now: i64) -> Result<Value, String> {
    let pairs = url::form_urlencoded::parse(query.as_bytes());
    let action = pairs
        .into_iter()
        .find(|(key, value)| key == "action" && !value.is_empty())
        .map(|(_, v)| v.into_owned())
        .unwrap_or_default();
    plain(&python::strip(&action).to_lowercase(), now)
}

fn field(data: &Value, key: &str, required: bool, limit: usize) -> Result<String, String> {
    let text = word(&data[key]);
    let text = python::strip(&text);
    if required && text.is_empty() {
        return Err(format!("{key} required"));
    }
    if text.chars().count() > limit {
        return Err(format!("{key} too long"));
    }
    Ok(text.to_owned())
}

fn number(data: &Value, key: &str, default: i64, limit: i64) -> Result<i64, String> {
    let value = match data.get(key) {
        None => default,
        Some(v) if v.is_i64() || v.is_u64() => v.as_i64().unwrap_or(i64::MAX),
        Some(_) => return Err(format!("{key} must be an integer")),
    };
    if !(0..=limit).contains(&value) {
        return Err(format!("{key} out of range"));
    }
    Ok(value)
}

pub fn post(body: &[u8], now: i64, draw: impl FnOnce() -> String) -> Result<Value, String> {
    let data = if body.is_empty() {
        json!({})
    } else {
        serde_json::from_slice::<Value>(body)
            .ok()
            .filter(Value::is_object)
            .ok_or("bad json")?
    };
    let action = python::strip(data["action"].as_str().unwrap_or("")).to_lowercase();
    if action != "controller-event" {
        return plain(&action, now);
    }
    let button = field(&data, "button", true, 16)?;
    if !BUTTONS.contains(&button.as_str()) {
        return Err("unknown button".into());
    }
    let gesture = field(&data, "controller_event", true, 32)?;
    if !matches!(gesture.as_str(), "up" | "down") && !GESTURES.contains(&gesture.as_str()) {
        return Err("unknown controller_event".into());
    }
    let queued = data
        .get("queued")
        .map_or(Some(false), Value::as_bool)
        .ok_or("queued must be a boolean")?;
    let controller = field(&data, "controller_id", false, 128)?;
    let id = field(&data, "controller_event_id", false, 128)?;
    let duration = number(&data, "duration_ms", 0, 120_000)?;
    let age = number(&data, "age_ms", 0, 120_000)?;
    let ts = number(&data, "ts", now, 9_999_999_999_999)?;
    let mut event = json!({"type":"remote-action","action":action,"controller_event_id":if id.is_empty(){draw()}else{id},
        "button":button,"controller_event":gesture,"queued":queued,"duration_ms":duration,"age_ms":age,"ts":ts});
    if !controller.is_empty() {
        event["controller_id"] = json!(controller);
    }
    Ok(event)
}

pub fn receipt(event: &Value) -> Value {
    let mut reply = json!({"ok":true});
    if let Some(id) = event.get("controller_event_id") {
        reply["controller_event_id"] = id.clone();
    }
    reply
}
