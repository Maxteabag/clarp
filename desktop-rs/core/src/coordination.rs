//! Coordination between agents (feature `hidden_coordination`, Host
//! contract 120, docs/protocol.md "Hidden coordination"): what a `/log`
//! read of a chat asks for, when a transcript must be read again, and the
//! words of the quiet line drawn where the Host left rows out
//! (`hidden_before` on the next visible row, `hidden_after` after the
//! newest). The same rules and words as the iOS app's
//! `HiddenCoordination.swift`. Pure: the engine and the window use it.

use serde_json::Value;

use crate::json::Object;

pub const FEATURE: &str = "hidden_coordination";
pub const SHOW: &str = "show";
pub const HIDE: &str = "hide";

/// Whether a value of the Host's open set hides: only "hide" does; an
/// unknown value, or none, reads as show (and is never written back).
pub fn hides(visibility: &str) -> bool {
    visibility.trim().eq_ignore_ascii_case(HIDE)
}

/// The visibility a value reads as: "hide" or "show".
pub fn effective(visibility: &str) -> &'static str {
    if hides(visibility) { HIDE } else { SHOW }
}

/// What a transcript was read under (the echo's `visibility`). A read
/// without the parameter (an older cache, a Host before the feature) holds
/// every row: show.
pub fn read_visibility(echo: Option<&Object>) -> &'static str {
    effective(echo.and_then(|e| e.get("visibility")).and_then(Value::as_str).unwrap_or_default())
}

/// A page read for one visit (`source: request`): never cached.
pub fn requested(echo: Option<&Object>) -> bool {
    echo.and_then(|e| e.get("source")).and_then(Value::as_str) == Some("request")
}

/// The `/log` parameters of a read under the feature: `coordination=show`
/// for a chat opened with every run shown for the visit, else `auto`; a
/// delta also says what its transcript was read under.
pub fn query(feature: bool, show_visit: bool, delta: bool, echo: Option<&Object>) -> Vec<(&'static str, &'static str)> {
    if !feature {
        return Vec::new();
    }
    let mut query = vec![("coordination", if show_visit { SHOW } else { "auto" })];
    if delta {
        query.push(("coordination_read", read_visibility(echo)));
    }
    query
}

/// Whether the transcript in hand must be read again: the chat's setting
/// (the snapshot's `coordination_visibility`) is no longer what it was read
/// under. A visit with every run shown is never reloaded for it, nor a chat
/// whose value is not known.
pub fn needs_reload(feature: bool, show_visit: bool, echo: Option<&Object>, agent_visibility: &str) -> bool {
    if !feature || show_visit || agent_visibility.trim().is_empty() {
        return false;
    }
    read_visibility(echo) != effective(agent_visibility)
}

/// One count of rows left out (`hidden_before` / `hidden_after`).
#[derive(Debug, Clone, PartialEq, Default)]
pub struct Count {
    pub messages: i64,
    pub exchanges: i64,
    /// The agents' names (empty for one without a name).
    pub agents: Vec<String>,
    /// The run goes on beyond what was counted: "100+".
    pub capped: bool,
}

impl Count {
    pub fn from_json(object: &Object) -> Self {
        let int = |key: &str| object.get(key).and_then(Value::as_i64).unwrap_or(0).max(0);
        Count {
            messages: int("messages"),
            exchanges: int("exchanges"),
            agents: object
                .get("agents")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
                .map(|a| a.get("name").and_then(Value::as_str).unwrap_or_default().trim().to_owned())
                .collect(),
            capped: object.get("capped").and_then(Value::as_bool).unwrap_or(false),
        }
    }
}

/// The quiet line: "3 messages between agents, with Arnold".
pub fn line(count: &Count) -> String {
    let head = if count.capped {
        "100+ messages between agents".to_owned()
    } else if count.messages == 0 {
        "Work between agents".to_owned()
    } else if count.messages == 1 {
        "1 message between agents".to_owned()
    } else {
        format!("{} messages between agents", count.messages)
    };
    match names(&count.agents) {
        Some(names) => format!("{head}, with {names}"),
        None => head,
    }
}

/// At most two names; the rest (and every agent without a name) are others.
pub fn names(agents: &[String]) -> Option<String> {
    if agents.is_empty() {
        return None;
    }
    let named: Vec<&str> = agents.iter().map(|a| a.trim()).filter(|a| !a.is_empty()).collect();
    let shown: Vec<&str> = named.into_iter().take(2).collect();
    let others = agents.len() - shown.len();
    let rest = if others == 1 { "1 other".to_owned() } else { format!("{others} others") };
    Some(match (shown.len(), others) {
        (0, _) => rest,
        (1, 0) => shown[0].to_owned(),
        (_, 0) => format!("{} and {}", shown[0], shown[1]),
        _ => format!("{} and {rest}", shown.join(", ")),
    })
}

/// What the quiet line says when tapped.
pub const HINT: &str = "Shows the messages between agents in this chat";
/// The global setting and the chat's own switch.
pub const SETTING: &str = "Show messages between agents";
/// The device's line setting, shown while the global one is off.
pub const LINE_SETTING: &str = "Show a line where agent messages were";
