//! The string-valued Android message sent for an APNs-shaped notification.
use crate::python;
use serde_json::{Value, json};

const DATA_BYTES: usize = 4096;

fn string(value: &Value) -> Option<String> {
    match value {
        Value::Null => None,
        Value::String(text) => Some(text.clone()),
        Value::Number(n) if n.is_f64() && n.as_f64()?.fract() == 0.0 => {
            Some(format!("{:.0}", n.as_f64()? + 0.0))
        }
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
            let text = value
                .as_str()
                .map_or_else(|| python::repr(value), str::to_owned);
            data.insert(key.into(), json!(text));
        }
    }
    for (source, target) in [
        ("interruption-level", "interruption_level"),
        ("sound", "sound"),
        ("category", "category"),
        ("thread-id", "thread_id"),
    ] {
        if let Some(text) = string(&aps[source]).filter(|v| !v.is_empty()) {
            data.insert(target.into(), json!(text));
        }
    }
    if let Some(fields) = payload.as_object() {
        for (key, value) in fields {
            if key != "aps"
                && let Some(text) = string(value)
            {
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
    data.retain(|k, _| {
        !matches!(
            k.as_str(),
            "from" | "notification" | "message_type" | "collapse_key"
        ) && !k.starts_with("google")
            && !k.starts_with("gcm")
    });
    let mut data = Value::Object(data);
    for key in ["body", "opening_line", "subtitle", "title", "reason"] {
        let size = python::compact(&data).len();
        if size <= DATA_BYTES {
            return Some(data);
        }
        if key == "reason" && data["kind"] != "agent-ring" {
            break;
        }
        let Some(text) = data[key].as_str().filter(|s| !s.is_empty()) else {
            continue;
        };
        // Removing the original encoded string leaves the cost of all other fields.
        let fixed = size - data[key].to_string().len();
        let mut prefix = String::new();
        let mut used = fixed + 2 + '…'.len_utf8();
        if used <= DATA_BYTES {
            for c in text.chars() {
                let bytes = json!(c.to_string()).to_string().len() - 2;
                if used + bytes > DATA_BYTES {
                    break;
                }
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
    let mut payload =
        json!({"kind":"agents-working", "event": if busy {"update"} else {"end"}, "state":state});
    if !server.is_empty() {
        payload["server_instance_id"] = json!(server);
    }
    payload
}

/// The next action after a send. A refused bearer gets one fresh token and
/// does not consume the transient-error retry budget.
#[derive(Debug, PartialEq, Eq)]
pub enum Next {
    Refresh,
    Retry(u64),
    Finished(u16, String, String),
}

#[derive(Default)]
pub struct Sending {
    transient: u8,
    refreshed: bool,
}

impl Sending {
    pub fn answered(&mut self, status: u16, body: &Value, retry_after: &str) -> Next {
        if status == 200 {
            return Next::Finished(
                200,
                String::new(),
                body["name"].as_str().unwrap_or("").into(),
            );
        }
        if status == 401 && !self.refreshed {
            self.refreshed = true;
            return Next::Refresh;
        }
        let error = &body["error"];
        let details = error["details"].as_array().into_iter().flatten();
        let code = details
            .clone()
            .filter_map(|d| d["errorCode"].as_str())
            .find(|c| !c.is_empty())
            .unwrap_or("");
        let rpc = error["status"].as_str().unwrap_or("");
        if code == "UNREGISTERED" || (status == 404 && code.is_empty()) {
            return Next::Finished(410, "Unregistered".into(), String::new());
        }
        if status == 400 && (code == "INVALID_ARGUMENT" || rpc == "INVALID_ARGUMENT") {
            let other = details
                .filter(|d| {
                    d["@type"]
                        .as_str()
                        .unwrap_or("")
                        .ends_with("google.rpc.BadRequest")
                })
                .flat_map(|d| d["fieldViolations"].as_array().into_iter().flatten())
                .filter_map(|v| v["field"].as_str())
                .any(|f| !f.is_empty() && f != "message.token");
            if !other {
                return Next::Finished(410, "InvalidRegistration".into(), String::new());
            }
        }
        let transient = status == 0
            || status == 429
            || status >= 500
            || matches!(code, "QUOTA_EXCEEDED" | "UNAVAILABLE" | "INTERNAL");
        if transient && !matches!(status, 401 | 403) && self.transient < 2 {
            let backoff = 1 << self.transient;
            self.transient += 1;
            let raw = retry_after.trim();
            let wait = if !raw.is_empty() && raw.bytes().all(|b| b.is_ascii_digit()) {
                raw.parse::<u64>().unwrap_or(u64::MAX).min(10)
            } else {
                0
            };
            return Next::Retry(backoff.max(wait));
        }
        let reason = if status == 0 {
            "network".into()
        } else if !code.is_empty() {
            code.into()
        } else if !rpc.is_empty() {
            rpc.into()
        } else {
            format!("HTTP{status}")
        };
        Next::Finished(status, reason, String::new())
    }
}

/// A ring can have been shown even when the push service never answered. Keep
/// the tokens tried until its end, or ten minutes after its expiry. After a
/// restart an unknown ring's end goes to every currently eligible row.
#[derive(Default)]
pub struct LiveRings {
    tried: std::collections::HashMap<String, (i64, std::collections::HashSet<String>)>,
}

impl LiveRings {
    pub fn targets(
        &mut self,
        id: &str,
        kind: &str,
        expires: i64,
        now: i64,
        later: bool,
        rows: Vec<String>,
    ) -> Vec<String> {
        self.tried
            .retain(|_, (expiry, _)| *expiry >= now.saturating_sub(600_000));
        if kind == "ring" {
            let (_, tried) = self
                .tried
                .entry(id.into())
                .or_insert_with(|| (expires, Default::default()));
            if later || now >= expires {
                return Vec::new();
            }
            tried.extend(rows.iter().cloned());
            return rows;
        }
        if later {
            return Vec::new();
        }
        match self.tried.remove(id) {
            Some((_, tried)) => rows
                .into_iter()
                .filter(|token| tried.contains(token))
                .collect(),
            None => rows,
        }
    }
}

/// The update allowance for one Android phone. An end is always eligible and
/// still counts towards the day's total, so a phone can clear stale work.
#[derive(Default, serde::Serialize, serde::Deserialize)]
pub struct ActivityBudget {
    day: i64,
    count: u64,
    bucket: Option<(f64, i64)>,
}

impl ActivityBudget {
    pub fn reserve(&mut self, now: i64, update: bool) -> Result<(),i64> {
        let day = now.div_euclid(86_400_000);
        if day != self.day { self.count = 0; self.day = day; }
        if update {
            let level = self.bucket.map_or(10.0, |(remaining, at)| (remaining + (now-at).max(0) as f64 / 288_000.0).clamp(0.0,10.0));
            if level < 1.0 {return Err(now+((1.0-level)*288_000.0).ceil() as i64);}
            if self.count>=300{return Err((day+1)*86_400_000);}
            self.bucket=Some((level-1.0,now));
        }
        self.count=self.count.saturating_add(1);
        Ok(())
    }
}
