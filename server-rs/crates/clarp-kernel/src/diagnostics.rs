//! Computer-wide opt-in capture and the public health response.
use crate::python;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::collections::BTreeSet;
pub const KEY: &str = "diagnostics.capture.v1";
pub const CATEGORIES: [&str; 10] = [
    "requests",
    "database",
    "network",
    "interactions",
    "transcript",
    "resources",
    "feedback",
    "voice",
    "agents",
    "client",
];
const ENABLED: u16 = 1 << 10;
#[derive(Default, Clone, Copy)]
pub struct Settings(pub u16);
fn word(v: &Value) -> String {
    v.as_str()
        .map(str::to_owned)
        .unwrap_or_else(|| python::repr(v))
}
impl Settings {
    fn parse(v: &Value, strict: bool) -> Result<Self, String> {
        let mut bits = if v["enabled"] == true { ENABLED } else { 0 };
        let mut unknown = BTreeSet::new();
        if let Some(list) = v["categories"].as_array() {
            for item in list {
                let name = python::strip(&word(item)).to_lowercase();
                match CATEGORIES.iter().position(|k| *k == name) {
                    Some(i) => bits |= 1 << i,
                    None => {
                        unknown.insert(name);
                    }
                }
            }
        } else if strict {
            return Err("categories must be a list".into());
        }
        if strict && !unknown.is_empty() {
            return Err(format!(
                "unknown diagnostic categories: {}",
                unknown.into_iter().collect::<Vec<_>>().join(", ")
            ));
        }
        Ok(Self(bits))
    }
    pub fn update(v: &Value) -> Result<Self, String> {
        Self::parse(v, true)
    }
    pub fn decode(raw: &str) -> Self {
        serde_json::from_str(raw)
            .ok()
            .and_then(|v| Self::parse(&v, false).ok())
            .unwrap_or_default()
    }
    pub fn kept(self) -> Value {
        json!({"enabled":self.0&ENABLED!=0,"categories":CATEGORIES.iter().enumerate().filter(|(i,_)|self.0&(1<<i)!=0).map(|(_,s)|s).collect::<Vec<_>>()})
    }
    pub fn public(self) -> Value {
        let mut v = self.kept();
        v["retention_hours"] = json!(24);
        v["rollup_retention_days"] = json!(30);
        v
    }
    pub fn allows(self, category: &str) -> bool {
        self.0 & ENABLED != 0
            && CATEGORIES
                .iter()
                .position(|s| *s == category)
                .is_some_and(|i| self.0 & (1 << i) != 0)
    }
    pub fn requests(self) -> bool {
        self.allows("requests") || self.allows("database")
    }
    pub fn uploads(self) -> bool {
        ["client", "interactions", "resources", "feedback", "network"]
            .iter()
            .any(|c| self.allows(c))
    }
}
pub fn client_category(name: &str) -> &'static str {
    let n = name.to_ascii_lowercase();
    if n.contains("user-slow-report") {
        "feedback"
    } else if n.contains("device-resources") {
        "resources"
    } else if n.contains("conversation-open") {
        "interactions"
    } else {
        "client"
    }
}
pub fn masked(v: &Value) -> Value {
    let raw = word(v);
    json!(format!(
        "<redacted length={} sha256={}>",
        raw.chars().count(),
        &format!("{:x}", Sha256::digest(raw.as_bytes()))[..12]
    ))
}
pub fn safe_detail(v: &Value) -> Value {
    // Arbitrary keys and string leaves can both be dialogue. Preserve only
    // a small vocabulary of measurement keys and numeric/boolean values.
    match v {
        Value::Object(fields) => {
            let mut out = serde_json::Map::new();
            for (key, value) in fields {
                let k = key.trim().to_ascii_lowercase();
                let private = matches!(
                    k.as_str(),
                    "text"
                        | "prompt"
                        | "transcript"
                        | "content"
                        | "body"
                        | "authorization"
                        | "auth_token"
                        | "token"
                        | "api_key"
                        | "password"
                );
                let name = if private
                    || matches!(
                        k.as_str(),
                        "count" | "size" | "bytes" | "duration_ms" | "status" | "detail" | "id"
                    ) {
                    k
                } else {
                    format!("field_{:x}", Sha256::digest(key.as_bytes()))
                };
                out.insert(
                    name,
                    if private {
                        masked(value)
                    } else {
                        safe_detail(value)
                    },
                );
            }
            Value::Object(out)
        }
        Value::Array(items) => Value::Array(items.iter().map(safe_detail).collect()),
        Value::String(_) => masked(v),
        _ => v.clone(),
    }
}
pub fn health(
    stt: bool,
    ffmpeg: bool,
    subsystems: Value,
    pending: usize,
    in_flight: usize,
    version: &str,
) -> Value {
    json!({"ready":stt && ffmpeg,"checks":{"stt_ready":stt,"ffmpeg_ready":ffmpeg},"subsystems":subsystems,"tts_queue":{"pending":pending,"in_flight":in_flight},"deployed_version":version})
}
