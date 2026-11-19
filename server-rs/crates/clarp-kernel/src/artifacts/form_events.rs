//! Validation and idempotency of append-only interactive form telemetry.
use crate::python;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

pub struct Event<'a> {
    pub id: &'a str,
    pub client_seq: u64,
    pub client_at: u64,
    pub value: &'a Value,
    pub digest: String,
}

pub fn batch(data: &Value) -> Result<Vec<Event<'_>>, &'static str> {
    if !data.is_object() {
        return Err("JSON object required");
    }
    let rows = data["events"]
        .as_array()
        .filter(|v| (1..=32).contains(&v.len()))
        .ok_or("events must contain 1 to 32 records")?;
    let mut out = Vec::new();
    for row in rows {
        if !row.is_object() {
            return Err("event record must be an object");
        }
        let id = row["event_id"]
            .as_str()
            .filter(|s| {
                (16..=80).contains(&s.len())
                    && s.bytes().all(|c| c.is_ascii_alphanumeric() || c == b'-')
            })
            .ok_or("invalid event_id")?;
        let safe = |key: &str| row[key].as_u64().filter(|n| *n <= 9007199254740991);
        let client_seq =
            safe("client_seq").ok_or("client_seq must be a safe nonnegative integer")?;
        let client_at = safe("client_at").ok_or("client_at must be a safe nonnegative integer")?;
        let value = &row["event"];
        if !value.is_object() {
            return Err("event must be an object");
        }
        let encoded = python::canonical(value);
        if encoded.len() > 16384 {
            return Err("event exceeds 16 KiB");
        }
        let digest = format!(
            "{:x}",
            Sha256::digest(python::dumps_ascii(&json!([data["version"], encoded])).as_bytes())
        );
        out.push(Event {
            id,
            client_seq,
            client_at,
            value,
            digest,
        });
    }
    Ok(out)
}
pub fn draft_key(data: &Value) -> Result<&Value, &'static str> {
    let key = data
        .get("draft_key")
        .ok_or("draft_key required; use null to disable")?;
    if !key.is_null()
        && !key.as_str().is_some_and(|s| {
            (1..=80).contains(&s.len())
                && s.bytes()
                    .all(|c| c.is_ascii_alphanumeric() || matches!(c, b'_' | b'-'))
        })
    {
        return Err("invalid draft_key");
    }
    Ok(key)
}
pub fn cursors(after: i128, limit: i128, through: i128) -> Result<(i64, usize, i64), &'static str> {
    if !(0..=i64::MAX as i128).contains(&after) || !(1..=100).contains(&limit) {
        return Err("invalid event cursor or limit");
    }
    if !(after..=i64::MAX as i128).contains(&through) {
        return Err("invalid snapshot cursor");
    }
    Ok((after as i64, limit as usize, through as i64))
}

/// Ordinary JSON takes the serde path. The artifact reader also recognizes the
/// non-finite literals Python accepts on input and refuses in event payloads.
pub fn posted(bytes: &[u8]) -> Result<Value, &'static str> {
    use super::render_spec::json::{self as raw, J};
    if bytes.is_empty() {
        return Ok(json!({}));
    }
    if let Ok(data) = serde_json::from_slice(bytes) {
        return Ok(data);
    }
    fn nonfinite(value: &J) -> bool {
        match value {
            J::Float(f) => !f.is_finite(),
            J::Array(rows) => rows.iter().any(nonfinite),
            J::Object(fields) => fields.iter().any(|(_, v)| nonfinite(v)),
            _ => false,
        }
    }
    let data = raw::read(bytes).ok_or("JSON object required")?;
    let value = data.to_value();
    if let Some(J::Array(rows)) = data.get("events")
        && (1..=32).contains(&rows.len())
        && let Some(pos) = rows
            .iter()
            .position(|r| matches!(r.get("event"),Some(event @ J::Object(_)) if nonfinite(event)))
    {
        let prefix = json!({"version":value["version"],"events":&value["events"].as_array().ok_or("event record must be an object")?[..=pos]});
        batch(&prefix)?;
        return Err("event must be finite JSON");
    }
    Ok(value)
}
