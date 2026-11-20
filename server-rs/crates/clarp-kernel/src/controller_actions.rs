//! The controller's saved definitions, mappings and invocation rules (contract 63).
use crate::{python, tools::cells};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::collections::BTreeMap;

pub const TYPES: [&str; 2] = ["http_post", "agent_prompt"];
pub const BUTTONS: [&str; 2] = ["primary", "secondary"];
pub const GESTURES: [&str; 9] = [
    "double-click",
    "hold",
    "rotate-clockwise",
    "rotate-counterclockwise",
    "single-click",
    "swipe-down",
    "swipe-left",
    "swipe-right",
    "swipe-up",
];
pub const AUDIT_KEEP: usize = 500;
pub const REQUEST_MS: i64 = 600_000;
pub const DEBOUNCE_MS: u64 = 1_500;
pub type Error = (u16, String);
fn bad(message: &str) -> Error {
    (400, message.into())
}
fn missing(id: &str) -> Error {
    (404, format!("no such action {id}"))
}

pub use crate::destination::Destination;

pub fn word(value: &Value) -> String {
    if python::truthy(value) {
        cells::said(value)
    } else {
        String::new()
    }
}

fn text(data: &Value, key: &str, limit: usize) -> Result<String, Error> {
    let s = match &data[key] {
        Value::Null => "",
        Value::String(s) => python::strip(s),
        _ => return Err(bad(&format!("{key} must be a string"))),
    };
    if s.is_empty() {
        return Err(bad(&format!("{key} required")));
    }
    if s.chars().count() > limit {
        return Err(bad(&format!("{key} too long")));
    }
    Ok(s.into())
}

pub fn validate(data: &Value, entries: &[Destination]) -> Result<Value, Error> {
    if !data.is_object() {
        return Err(bad("action must be an object"));
    }
    let id = word(&data["id"]);
    let id = python::strip(&id);
    if id.is_empty()
        || id.len() > 64
        || !id.as_bytes()[0].is_ascii_alphanumeric()
        || !id
            .bytes()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || matches!(b, b'-' | b'_'))
    {
        return Err(bad("id must be lowercase letters, digits, - or _ (max 64)"));
    }
    let kind = word(&data["type"]);
    let kind = python::strip(&kind);
    if !TYPES.contains(&kind) {
        return Err(bad("type must be one of http_post, agent_prompt"));
    }
    let enabled = data
        .get("enabled")
        .map_or(Some(true), Value::as_bool)
        .ok_or_else(|| bad("enabled must be a boolean"))?;
    let mut action = json!({"id":id,"type":kind,"title":text(data,"title",80)?,"enabled":enabled});
    if kind == "http_post" {
        let url = text(data, "url", 2000)?;
        if crate::destination::allowed(&url, entries).is_none() {
            return Err(bad("url is not allow-listed"));
        }
        let body = data.get("body").cloned().unwrap_or_else(|| json!({}));
        if !body.is_object() {
            return Err(bad("body must be a JSON object"));
        }
        if python::dumps_ascii(&body).len() > 8192 {
            return Err(bad("body too large"));
        }
        action["url"] = json!(url);
        action["body"] = body;
    } else {
        action["session"] = json!(text(data, "session", 128)?);
        action["text"] = json!(text(data, "text", 4000)?);
    }
    Ok(action)
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct Registry {
    pub revision: i64,
    pub actions: BTreeMap<String, Value>,
    mappings: Vec<Value>,
}

impl Registry {
    pub fn screen(&self) -> Value {
        json!({"revision":self.revision,"actions":self.actions.values().collect::<Vec<_>>(),
        "mappings":self.mappings,"types":TYPES,"buttons":BUTTONS,"gestures":GESTURES})
    }

    pub fn enabled(&self, id: &str) -> Result<Value, Error> {
        let action = self.actions.get(id).ok_or_else(|| missing(id))?;
        if action["enabled"] == false {
            return Err((409, format!("action {id} is disabled")));
        }
        Ok(action.clone())
    }

    pub fn mapped(&self, device: &str, button: &str, gesture: &str) -> Option<&str> {
        [device, "*"].into_iter().find_map(|d| {
            self.mappings
                .iter()
                .find(|m| m["device"] == d && m["button"] == button && m["gesture"] == gesture)?
                .get("action_id")?
                .as_str()
        })
    }

    /// Apply one validated operation and produce its response and audit row together.
    pub fn change(
        &mut self,
        op: &str,
        data: &Value,
        entries: &[Destination],
        actor: &str,
        now: i64,
    ) -> Result<(Value, Value), Error> {
        if !data.is_object() {
            return Err(bad("bad json"));
        }
        let raw = word(&data["id"]);
        let id = python::strip(&raw);
        let mut audit = json!({"at":now,"actor":actor,"action_id":id,"event":op});
        let result = match op {
            "create" | "update" => {
                let mut merged = data.clone();
                if op == "update" {
                    let mut current = self.actions.get(id).cloned().ok_or_else(|| missing(id))?;
                    for (k, v) in data.as_object().into_iter().flatten() {
                        if k != "id" {
                            current[k] = v.clone();
                        }
                    }
                    // Stored definitions contain only their own type's fields. Validation
                    // rebuilds the action and requires the new type's fields on a type change.
                    merged = current;
                }
                let action = validate(&merged, entries)?;
                if op == "create" && self.actions.contains_key(id) {
                    return Err((409, format!("action {id} exists")));
                }
                self.actions.insert(id.to_string(), action.clone());
                audit["action"] = action.clone();
                json!({"ok":true,"action":action})
            }
            "delete" => {
                self.actions.remove(id).ok_or_else(|| missing(id))?;
                let before = self.mappings.len();
                self.mappings.retain(|m| m["action_id"] != id);
                let count = before - self.mappings.len();
                audit["mappings_removed"] = json!(count);
                json!({"ok":true,"deleted":true,"mappings_removed":count})
            }
            "mappings" => {
                let device = word(&data["device"]);
                let device = if device.is_empty() {
                    "*"
                } else {
                    python::strip(&device)
                };
                if device != "*"
                    && (device.is_empty()
                        || device.len() > 128
                        || !device.as_bytes()[0].is_ascii_alphanumeric()
                        || !device
                            .bytes()
                            .all(|b| b.is_ascii_alphanumeric() || b":._-".contains(&b)))
                {
                    return Err(bad("device must be * or a controller id"));
                }
                let button = word(&data["button"]);
                let gesture = word(&data["gesture"]);
                if !BUTTONS.contains(&button.as_str()) {
                    return Err(bad("button must be one of primary, secondary"));
                }
                if !GESTURES.contains(&gesture.as_str()) {
                    return Err(bad(&format!(
                        "gesture must be one of {}",
                        GESTURES.join(", ")
                    )));
                }
                let aid = word(&data["action_id"]);
                let aid = python::strip(&aid);
                if !aid.is_empty() && !self.actions.contains_key(aid) {
                    return Err(missing(aid));
                }
                self.mappings.retain(|m| {
                    !(m["device"] == device && m["button"] == button && m["gesture"] == gesture)
                });
                let mut mapping = Value::Null;
                if !aid.is_empty() {
                    mapping =
                        json!({"device":device,"button":button,"gesture":gesture,"action_id":aid});
                    self.mappings.push(mapping.clone());
                    self.mappings.sort_by(|a, b| {
                        ["device", "button", "gesture"]
                            .iter()
                            .map(|k| a[*k].as_str())
                            .cmp(
                                ["device", "button", "gesture"]
                                    .iter()
                                    .map(|k| b[*k].as_str()),
                            )
                    });
                }
                audit = json!({"event":if mapping.is_null(){"unmap"}else{"map"},"at":now,"actor":actor,"action_id":aid,"device":device,"button":button,"gesture":gesture});
                json!({"ok":true,"mapping":mapping})
            }
            _ => return Err((404, "not found".into())),
        };
        self.revision = now.max(self.revision.saturating_add(1));
        Ok((result, audit))
    }
}

pub fn request_id(data: &Value) -> Result<String, Error> {
    let id = word(&data["request_id"]);
    let id = python::first(python::strip(&id), 128);
    if id.is_empty() {
        return Err(bad("request_id required"));
    }
    Ok(id.into())
}
pub fn source(data: &Value) -> Value {
    let mut source = json!({});
    for key in ["device", "button", "gesture", "source_kind"] {
        if python::truthy(&data[key]) {
            source[key] = json!(python::first(&word(&data[key]), 128));
        }
    }
    source
}
pub fn debounced(action: &Value, elapsed_ms: u64, last: Option<&Value>) -> Option<Value> {
    if elapsed_ms >= DEBOUNCE_MS || last.is_some_and(|r| r["ok"] != true) {
        return None;
    }
    let mut answer=last.cloned().unwrap_or_else(||json!({"ok":true,"title":action["title"],"type":action["type"],"action_id":action["id"]}));
    answer["debounced"] = true.into();
    answer["duration_ms"] = 0.into();
    Some(answer)
}

pub fn http_result(status: u16, text: &str) -> Value {
    let mut result = json!({"ok":(200..300).contains(&status),"status":status,"response":python::first(text,500)});
    if result["ok"] == false {
        result["error"] = json!(format!("HTTP {status}"));
    }
    result
}

pub fn receipt_current(at: i64, now: i64) -> bool {
    at >= now.saturating_sub(REQUEST_MS)
}
