//! What an agent asks its user's phone to put into a calendar.
//!
//! An agent's tool names an event (`POST /calendar/request`): whose agent asks, a title,
//! when it begins and ends, and what else there is to say of it. The Host tells the apps
//! (`calendar-request`), and the one in front asks its user and writes the event. The Host
//! keeps nothing of it and reads no time: what is said is handed on as it was said, without
//! the blanks around it. The rules and their words are the Python Host's
//! (`server/lib/calendar_request.py`), held by `tests/fixtures/calendar-vectors.json`.

use serde_json::{Map, Value, json};

use crate::python;

/// What a request must say, in the order it is refused for the first that is missing.
const NEEDED: [&str; 4] = ["session", "title", "start", "end"];

/// What a request says under a name, as a word: nothing is no word, a text is itself, and
/// anything else is written as Python writes it (`str`); without the blanks around it.
fn word(said: &Map<String, Value>, key: &str) -> String {
    match said.get(key) {
        None | Some(Value::Null) => String::new(),
        Some(Value::String(text)) => python::strip(text).to_string(),
        Some(other) => python::strip(&python::repr(other)).to_string(),
    }
}

/// The event the apps are told for a request, under the id the Host gave it; or the words
/// the request is refused with, which name the first thing it must say and does not.
pub fn asked(said: &Map<String, Value>, request_id: &str) -> Result<Value, String> {
    if let Some(missing) = NEEDED.iter().find(|key| word(said, key).is_empty()) {
        return Err(format!("{missing} required"));
    }
    // The zone goes by two names: the first of them that says something.
    let zone = match word(said, "time_zone") {
        zone if zone.is_empty() => word(said, "timezone"),
        zone => zone,
    };
    Ok(json!({
        "type": "calendar-request",
        "request_id": request_id,
        "session": word(said, "session"),
        "title": word(said, "title"),
        "start": word(said, "start"),
        "end": word(said, "end"),
        "time_zone": zone,
        "location": word(said, "location"),
        "notes": word(said, "notes"),
        "url": word(said, "url"),
        "all_day": said.get("all_day").is_some_and(python::truthy),
        "calendar": word(said, "calendar"),
    }))
}
