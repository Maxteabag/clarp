//! Evidence that Claude can take more input and when it actually reads it.

use std::collections::BTreeSet;

use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::turn::TurnId;

/// An accepted message reserved for the process of another turn of the same agent.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Join {
    pub into: TurnId,
    pub uuid: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub recovery: Option<Recovery>,
}

/// What owns this stdin line, kept before attempting the write.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Recovery {
    pub owner: InputOwner,
    pub session: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct InputOwner {
    pub shim_pid: u32,
    pub shim_start: String,
    pub tool_pid: u32,
    pub tool_start: String,
    pub group: u32,
    pub projects: std::path::PathBuf,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FollowUp {
    pub join: Join,
    pub message_seq: u64,
    pub stage: Stage,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Stage {
    Pending,
    Read(i64),
    Settled,
}

impl Stage {
    pub fn read_at(self) -> Option<i64> {
        match self {
            Self::Read(at) => Some(at),
            _ => None,
        }
    }
}

/// The first Claude Code version whose echo and result behavior the Host has recorded.
pub fn supported_version(output: &str) -> bool {
    let Ok(pattern) = regex::Regex::new(r"([0-9]+)\.([0-9]+)\.([0-9]+)") else {
        return false;
    };
    let Some(version) = pattern.captures(output) else {
        return false;
    };
    let digits = [1, 2, 3].map(|index| version[index].parse::<u32>().ok());
    matches!(digits, [Some(major), Some(minor), Some(patch)] if [major, minor, patch] >= [2, 1, 321])
}

/// Tool IDs opened and answered by one Claude stream record.
#[derive(Default)]
pub struct ToolChanges<'a> {
    pub opened: Vec<&'a str>,
    pub answered: Vec<&'a str>,
}

fn nonempty(value: &Value) -> Option<&str> {
    value.as_str().filter(|text| !text.is_empty())
}

/// A separate Claude turn records a user entry; a follow-up read inside a tool call is a
/// queued-command attachment. Both use the UUID of the stdin message.
pub fn transcript_read_uuid(entry: &Value) -> Option<(&str, bool)> {
    match entry["type"].as_str() {
        Some("user") => nonempty(&entry["uuid"]).map(|uuid| (uuid, false)),
        Some("attachment") if entry["attachment"]["type"] == "queued_command" => {
            nonempty(&entry["attachment"]["source_uuid"]).map(|uuid| (uuid, true))
        }
        _ => None,
    }
}

fn tool_id(block: &Value) -> Option<&str> {
    match block["type"].as_str() {
        Some("tool_use" | "server_tool_use") => nonempty(&block["id"]),
        _ => None,
    }
}

pub fn tool_changes(event: &Value) -> ToolChanges<'_> {
    let mut changes = ToolChanges::default();
    match event["type"].as_str() {
        Some("stream_event") => {
            changes
                .opened
                .extend(tool_id(&event["event"]["content_block"]));
        }
        Some("assistant") => {
            for part in event["message"]["content"].as_array().into_iter().flatten() {
                changes.opened.extend(tool_id(part));
            }
        }
        Some("user") => {
            for part in event["message"]["content"].as_array().into_iter().flatten() {
                if part["type"] == "tool_result" {
                    changes.answered.extend(nonempty(&part["tool_use_id"]));
                }
            }
        }
        _ => {}
    }
    changes
}

/// Evidence from the owning Claude process. A result is also the shim's stdin fence.
#[derive(Default)]
pub struct Stream {
    tools: BTreeSet<String>,
    input_ended: bool,
}

pub enum Observation<'a> {
    Other,
    Echo(&'a str),
    Result { prompts: &'a [Value], failed: bool },
}

impl Stream {
    pub fn can_join(&self) -> bool {
        !self.input_ended && !self.tools.is_empty()
    }

    pub fn observe<'a>(&mut self, event: &'a Value) -> Observation<'a> {
        if event["type"] == "user" && event["isReplay"] == true {
            return nonempty(&event["uuid"]).map_or(Observation::Other, Observation::Echo);
        }
        if event["type"] == "result" && nonempty(&event["parent_tool_use_id"]).is_none() {
            self.tools.clear();
            self.input_ended = true;
            let prompts = event["user_message_uuids"]
                .as_array()
                .map_or(&[][..], Vec::as_slice);
            return Observation::Result {
                prompts,
                failed: event["is_error"] == true,
            };
        }
        let changes = tool_changes(event);
        self.tools
            .extend(changes.opened.into_iter().map(str::to_owned));
        for id in changes.answered {
            self.tools.remove(id);
        }
        Observation::Other
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn a_read_follow_up_is_a_user_entry_or_a_queued_command_attachment() {
        for (entry, expected) in [
            (json!({"type": "user", "uuid": "prompt"}), Some(("prompt", false))),
            (json!({"type": "assistant", "uuid": "prompt"}), None),
            (json!({"type": "attachment", "uuid": "entry", "attachment": {
                "type": "queued_command", "source_uuid": "prompt"
            }}), Some(("prompt", true))),
            (json!({"type": "attachment", "attachment": {
                "type": "background", "source_uuid": "same"
            }}), None),
            (json!({"type": "attachment", "uuid": "entry", "attachment": {
                "type": "queued_command"
            }}), None),
        ] {
            assert_eq!(transcript_read_uuid(&entry), expected);
        }
    }

    #[test]
    fn only_the_live_tool_window_takes_new_input() {
        let mut stream = Stream::default();
        let text = json!({"type":"stream_event","event":{"content_block":{"type":"text"}}});
        stream.observe(&text);
        assert!(!stream.can_join());

        let parent =
            json!({"type":"assistant","message":{"content":[{"type":"tool_use","id":"task"}]}});
        stream.observe(&parent);
        let child = json!({"type":"stream_event","parent_tool_use_id":"task","event":{"content_block":{"type":"tool_use","id":"child"}}});
        stream.observe(&child);
        let answered = |id| json!({"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":id}]}});
        stream.observe(&answered("child"));
        assert!(
            stream.can_join(),
            "the foreground Task still owns the window"
        );
        stream.observe(&answered("task"));
        assert!(!stream.can_join());

        let echo = json!({"type":"user","isReplay":true,"uuid":"two"});
        assert!(matches!(stream.observe(&echo), Observation::Echo("two")));
        let result = json!({"type":"result","user_message_uuids":["one","two"]});
        assert!(
            matches!(stream.observe(&result), Observation::Result { prompts, failed: false } if prompts == [json!("one"), json!("two")])
        );
        stream.observe(&parent);
        assert!(!stream.can_join(), "the first result closed the input");
    }
}
