//! What a phone may send into a call of the Oracle.
//!
//! A phone sends sound, and a few words of its own: that it ends the call (and, after it was
//! put through to an agent, which handoff that was), that the voice is to stop speaking, and
//! what its user prefers. Nothing else is taken: the socket of a call is not a way to the
//! voice model or the router with the owner's key.
//!
//! The Python Host's `oracle_live_stable.client_event`, held to it by
//! `tests/fixtures/oracle-conversation-vectors.json`.

use serde_json::{Map, Value, json};

use crate::python;

/// The longest piece of sound a phone sends, as it is written.
pub const SOUND_SIGNS: usize = 262_144;

/// Whether this is the name of a handoff: `hof_` and 32 digits of a hexadecimal number in
/// small letters.
pub fn handoff_id(text: &str) -> bool {
    let hex = |sign: u8| sign.is_ascii_digit() || (b'a'..=b'f').contains(&sign);
    text.strip_prefix("hof_")
        .is_some_and(|rest| rest.len() == 32 && rest.bytes().all(hex))
}

/// How often a phone may ask to be told how work is going: never, or every ten seconds to
/// two minutes. A whole number.
pub fn interval(value: &Value) -> Option<i64> {
    let seconds = value.as_i64()?;
    (seconds == 0 || (10..=120).contains(&seconds)).then_some(seconds)
}

/// What a phone sent, as the Host takes it: its kind and what belongs to that kind, and
/// nothing else of what was written. `None`: nothing the Host takes.
pub fn event(raw: &str) -> Option<Value> {
    let value: Value = serde_json::from_str(raw).ok()?;
    let fields = value.as_object()?;
    let kind = fields.get("type")?.as_str()?;
    match kind {
        "session.input_audio.append" => {
            let sound = fields.get("audio")?.as_str()?;
            if sound.chars().count() > SOUND_SIGNS {
                return None;
            }
            let bytes = python::b64decode(sound)?;
            (!bytes.is_empty() && bytes.len().is_multiple_of(2))
                .then(|| json!({ "type": kind, "audio": sound }))
        }
        "session.close" => match fields.get("handoff_id") {
            None | Some(Value::Null) => Some(json!({ "type": kind })),
            Some(Value::String(id)) if handoff_id(id) => {
                Some(json!({ "type": kind, "handoff_id": id }))
            }
            Some(_) => None,
        },
        "oracle_v2.interrupt" => Some(json!({ "type": kind })),
        "oracle_v2.preferences" => {
            let mut event = Map::new();
            event.insert("type".to_string(), json!(kind));
            if let Some(seconds) = fields.get("progress_interval_seconds") {
                interval(seconds)?;
                event.insert("progress_interval_seconds".to_string(), seconds.clone());
            }
            if let Some(narration) = fields.get("narration") {
                if narration != "on" && narration != "off" {
                    return None;
                }
                event.insert("narration".to_string(), narration.clone());
            }
            if let Some(earcons) = fields.get("earcons") {
                earcons.as_bool()?;
                event.insert("earcons".to_string(), earcons.clone());
            }
            (event.len() > 1).then_some(Value::Object(event))
        }
        _ => None,
    }
}
