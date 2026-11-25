//! The owner's settings of lanes on the engine (feature `lane_settings`,
//! Rust Host contract 178, docs/protocol.md "The owner's settings of
//! lanes"): a kept process per tool between messages, how many the Host
//! keeps, what they may hold together and how long an unused one stays.
//! Read from `GET /lane-settings` when the Host lists the feature and again
//! on `lane-settings-updated`; a change is `POST /lane-settings`, whose
//! answer replaces what is held. The connected Host only: a peer Host's
//! lanes are not shown.

use clarp_core::json::Object;
use serde_json::Value;

use crate::{Change, Engine};

/// The feature in `/server-info`.
pub const FEATURE: &str = "lane_settings";
/// A top-level key ending so is one tool's switch (`codex_chat_lane`).
pub const SWITCH_SUFFIX: &str = "_chat_lane";
/// The numbers the owner sets, in the order they are shown.
pub const NUMBERS: [&str; 3] = ["most", "memory_mb", "idle_minutes"];

const READ: &str = "lane-settings";
const WRITE: &str = "lane-settings-write";

#[derive(Default)]
pub(crate) struct Lanes {
    /// `lane_settings` was in the last `/server-info`.
    pub feature: bool,
    /// The Host's last answer (settings, ranges and what is held).
    pub answer: Option<Object>,
    /// Why the last change or read did not go through, as the Host said it.
    pub error: String,
}

/// One tool's switch: its key, the tool's name, and whether it is on.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LaneSwitch {
    pub key: String,
    pub tool: String,
    pub on: bool,
}

/// The switches in an answer: every top-level key ending in `_chat_lane`
/// with a boolean, ordered by key.
pub fn switches(answer: &Object) -> Vec<LaneSwitch> {
    let mut out: Vec<LaneSwitch> = answer
        .iter()
        .filter_map(|(key, value)| {
            let tool = key.strip_suffix(SWITCH_SUFFIX).filter(|t| !t.is_empty())?;
            Some(LaneSwitch { key: key.clone(), tool: tool.to_owned(), on: value.as_bool()? })
        })
        .collect();
    out.sort_by(|a, b| a.key.cmp(&b.key));
    out
}

/// A number's current value in an answer.
pub fn number(answer: &Object, key: &str) -> Option<f64> {
    answer.get(key).and_then(Value::as_f64)
}

/// A number's range in an answer (`ranges.<key>.min/max`).
pub fn range(answer: &Object, key: &str) -> Option<(f64, f64)> {
    let span = answer.get("ranges")?.get(key)?;
    let min = span.get("min")?.as_f64()?;
    let max = span.get("max")?.as_f64()?;
    (min <= max).then_some((min, max))
}

/// What the lanes hold now, in one line: "8 kept · 1945 MB · claude 8 (2
/// busy) · codex 0", with the lanes that did not answer named apart.
pub fn held_line(answer: &Object) -> String {
    let Some(held) = answer.get("held").and_then(Value::as_object) else { return String::new() };
    let count = |object: &Object, key: &str| object.get(key).and_then(Value::as_i64).unwrap_or(0);
    let mut parts = vec![format!("{} kept", count(held, "lanes")), format!("{} MB", count(held, "memory_mb"))];
    if let Some(tools) = held.get("tools").and_then(Value::as_object) {
        let mut names: Vec<&String> = tools.keys().collect();
        names.sort();
        for name in names {
            let Some(tool) = tools[name].as_object() else { continue };
            let busy = count(tool, "busy");
            let mut part = format!("{name} {}", count(tool, "lanes"));
            if busy > 0 {
                part.push_str(&format!(" ({busy} busy)"));
            }
            parts.push(part);
        }
    }
    let unanswered = count(held, "unanswered");
    if unanswered > 0 {
        parts.push(format!("{unanswered} did not answer in time, not counted"));
    }
    parts.join(" · ")
}

impl Engine {
    /// The connected Host lets the owner set its lanes.
    pub fn lane_settings_offered(&self) -> bool {
        self.lanes.feature
    }

    /// The Host's last answer, once it gave one.
    pub fn lane_settings(&self) -> Option<&Object> {
        self.lanes.answer.as_ref()
    }

    /// Why the last change or read did not go through (empty when it did).
    pub fn lane_settings_error(&self) -> &str {
        &self.lanes.error
    }

    /// Reads the settings again (the settings page opened).
    pub fn load_lane_settings(&mut self) {
        if self.lanes.feature {
            self.api.get(READ, "/lane-settings", &[]);
        }
    }

    /// Changes one setting: shown at once, then the Host's answer replaces
    /// it; a refusal puts back what the Host holds and says why.
    pub fn set_lane_setting(&mut self, key: &str, value: Value) {
        if !self.lanes.feature || key.is_empty() {
            return;
        }
        if let Some(answer) = self.lanes.answer.as_mut() {
            answer.insert(key.to_owned(), value.clone());
        }
        self.lanes.error.clear();
        self.changes.push(Change::HostStatus);
        let mut body = Object::new();
        body.insert(key.to_owned(), value);
        self.api.post_json(WRITE, "/lane-settings", Value::Object(body), None);
    }

    /// `/server-info` answered.
    pub(crate) fn lanes_server_info(&mut self, info: &Object) {
        let has = info
            .get("capabilities")
            .and_then(|c| c.get("features"))
            .and_then(Value::as_array)
            .is_some_and(|features| features.iter().any(|f| f.as_str() == Some(FEATURE)));
        if has != self.lanes.feature {
            self.lanes = Default::default();
            self.lanes.feature = has;
            self.changes.push(Change::HostStatus);
        }
        self.load_lane_settings();
    }

    /// The Host or its token changed: its lanes are read again.
    pub(crate) fn reset_lanes(&mut self) {
        if self.lanes.feature || self.lanes.answer.is_some() {
            self.changes.push(Change::HostStatus);
        }
        self.lanes = Default::default();
    }

    /// `lane-settings-updated`: something changed, read it all again (what
    /// the lanes hold too).
    pub(crate) fn lanes_event(&mut self) {
        self.load_lane_settings();
    }

    /// Replies for the lane tags; true when it was one.
    pub(crate) fn lanes_json(&mut self, tag: &str, object: &Object) -> bool {
        if tag != READ && tag != WRITE {
            return false;
        }
        if tag == WRITE {
            self.lanes.error.clear();
        }
        self.lanes.answer = Some(object.clone());
        self.changes.push(Change::HostStatus);
        true
    }

    /// A refused change or a failed read; true when it was one.
    pub(crate) fn lanes_failure(&mut self, tag: &str, message: &str, status: u16) -> bool {
        match tag {
            WRITE => {
                // The Host's sentence as it came ("most must be between 1 and 32").
                eprintln!("Engine: POST /lane-settings refused ({status}): {message}");
                self.lanes.error = message.to_owned();
                self.changes.push(Change::HostStatus);
                self.api.get(READ, "/lane-settings", &[]);
                true
            }
            READ => {
                eprintln!("Engine: GET /lane-settings failed ({status}): {message}");
                // A change's refusal stays shown over the read after it.
                if self.lanes.error.is_empty() {
                    self.lanes.error = format!("Could not read the kept processes: {message}");
                    self.changes.push(Change::HostStatus);
                }
                true
            }
            _ => false,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn answer() -> Object {
        json!({
            "codex_chat_lane": true, "claude_chat_lane": false, "muse_chat_lane": true, "_chat_lane": true, "most": 8, "memory_mb": 2200,
            "idle_minutes": 15,
            "ranges": {"most": {"min": 1, "max": 32}, "memory_mb": {"min": 256, "max": 16384}, "idle_minutes": {"min": 1, "max": 240}},
            "held": {"lanes": 8, "memory_mb": 1945, "unanswered": 0,
                     "tools": {"codex": {"lanes": 0, "busy": 0, "memory_mb": 0}, "claude": {"lanes": 8, "busy": 2, "memory_mb": 1945}}}
        })
        .as_object()
        .unwrap()
        .clone()
    }

    #[test]
    fn every_key_ending_in_chat_lane_is_a_tools_switch() {
        let tools: Vec<(String, bool)> = switches(&answer()).into_iter().map(|s| (s.tool, s.on)).collect();
        assert_eq!(tools, vec![("claude".into(), false), ("codex".into(), true), ("muse".into(), true)]);
    }

    #[test]
    fn numbers_and_ranges_come_from_the_answer() {
        let a = answer();
        assert_eq!(number(&a, "most"), Some(8.0));
        assert_eq!(range(&a, "memory_mb"), Some((256.0, 16384.0)));
        assert_eq!(range(&a, "nothing"), None);
    }

    #[test]
    fn the_held_line_names_busy_lanes_and_those_that_did_not_answer() {
        let mut a = answer();
        assert_eq!(held_line(&a), "8 kept · 1945 MB · claude 8 (2 busy) · codex 0");
        a["held"]["unanswered"] = json!(2);
        assert!(held_line(&a).ends_with("2 did not answer in time, not counted"));
    }
}
