//! The durable text of a call, assembled from its append-only partial.
use serde_json::{Value, json};

pub fn name(call: &str) -> String {
    let mut bytes = call.bytes();
    if call.len() <= 120
        && bytes.next().is_some_and(|b| b.is_ascii_alphanumeric())
        && bytes.all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'-' | b'_'))
    {
        call.into()
    } else {
        super::team::operation("call-", &[call])
    }
}

pub fn document(meta: &Value, started: &str, ended: &str, entries: &[Value]) -> Value {
    let mut fragments: Vec<Value> = Vec::new();
    let mut receipts = Vec::new();
    let mut pacing = Vec::new();
    for e in entries {
        match e["kind"].as_str() {
            Some("text") => {
                if let Some(last) = fragments.last_mut()
                    && last["role"] == e["role"]
                {
                    if let Some(Value::String(text)) = last.get_mut("text") {
                        text.push_str(e["text"].as_str().unwrap_or(""));
                    }
                    last["end"] = e["at"].clone();
                } else {
                    fragments.push(
                        json!({"role":e["role"],"text":e["text"],"at":e["at"],"end":e["at"]}),
                    );
                }
            }
            Some("receipt") => {
                let mut receipt = e["receipt"].clone();
                if receipt.get("at").is_none() {
                    receipt["at"] = e["at"].clone();
                }
                receipts.push(receipt);
            }
            Some("pacing") => {
                pacing.push(json!({"at":e["at"],"event":e["event"],"detail":e["detail"]}))
            }
            _ => {}
        }
    }
    let mut doc = meta.clone();
    doc["started"] = json!(started);
    doc["ended"] = json!(ended);
    doc["fragments"] = json!(fragments);
    doc["receipts"] = json!(receipts);
    doc["pacing"] = json!(pacing);
    doc
}
