//! The string-valued Android message sent for an APNs-shaped notification.
use serde_json::{Value, json};
use crate::python;

const DATA_BYTES: usize = 4096;

fn string(value: &Value) -> Option<String> {
    match value {
        Value::Null => None,
        Value::String(text) => Some(text.clone()),
        Value::Number(n) if n.is_f64() && n.as_f64()?.fract() == 0.0 =>
            Some(format!("{:.0}", n.as_f64()? + 0.0)),
        value => Some(python::canonical(value)),
    }
}

pub fn data(payload: &Value, context: &Value, collapse: &str, now_ms: i64) -> Option<Value> {
    let aps = &payload["aps"];
    let mut data = serde_json::Map::new();
    data.insert("payload_version".into(), json!("1"));
    for key in ["title", "body", "subtitle"] {
        let value = &aps["alert"][key];
        if !value.is_null() && value != "" {
            let text = value.as_str().map_or_else(|| python::repr(value), str::to_owned);
            data.insert(key.into(), json!(text));
        }
    }
    for (source, target) in [("interruption-level", "interruption_level"), ("sound","sound"), ("category","category"), ("thread-id","thread_id")] {
        if let Some(text) = string(&aps[source]).filter(|v| !v.is_empty()) {
            data.insert(target.into(), json!(text));
        }
    }
    if let Some(fields) = payload.as_object() {
        for (key, value) in fields {
            if key != "aps" && let Some(text) = string(value) {
                data.insert(key.clone(), json!(text));
            }
        }
    }
    if let Some(fields) = context.as_object() {
        for (key, value) in fields {
            if let Some(text) = string(value) {
                data.entry(key.clone()).or_insert(json!(text));
            }
        }
    }
    if !collapse.is_empty() {
        data.entry("collapse_id").or_insert(json!(collapse));
    }
    data.insert("sent_at".into(), json!(now_ms.to_string()));
    data.retain(|k, _| !matches!(k.as_str(), "from" | "notification" | "message_type" | "collapse_key") && !k.starts_with("google") && !k.starts_with("gcm"));
    let mut data = Value::Object(data);
    for key in ["body","opening_line","subtitle","title","reason"] {
        let size = python::compact(&data).len();
        if size <= DATA_BYTES { return Some(data); }
        if key == "reason" && data["kind"] != "agent-ring" { break; }
        let Some(text) = data[key].as_str().filter(|s| !s.is_empty()) else { continue; };
        // Removing the original encoded string leaves the cost of all other fields.
        let fixed = size - data[key].to_string().len();
        let mut prefix = String::new();
        let mut used = fixed + 2 + '…'.len_utf8();
        if used <= DATA_BYTES {
            for c in text.chars() {
                let bytes = json!(c.to_string()).to_string().len() - 2;
                if used + bytes > DATA_BYTES { break; }
                prefix.push(c);
                used += bytes;
            }
            prefix.push('…');
        }
        data[key] = json!(prefix);
    }
    (python::compact(&data).len() <= DATA_BYTES).then_some(data)
}

pub fn message(token: &str, data: Value, priority: &str, collapse_key: &str) -> Value {
    let mut android = json!({"priority": if priority == "10" { "HIGH" } else { "NORMAL" }});
    if !collapse_key.is_empty() {
        android["collapse_key"] = json!(collapse_key);
    }
    json!({"message":{"token":token, "data":data, "android":android}})
}

pub fn activity(state: &Value, server: &str) -> Value {
    let busy = python::truthy(&state["working"]) || python::truthy(&state["needs_you"]);
    let mut payload = json!({"kind":"agents-working", "event": if busy {"update"} else {"end"}, "state":state});
    if !server.is_empty() { payload["server_instance_id"] = json!(server); }
    payload
}
