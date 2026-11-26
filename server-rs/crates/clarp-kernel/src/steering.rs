//! Evidence that a running turn can take more input and when it actually reads it: Claude's
//! replayed input, or a Codex lane's `steer.read`.

use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::turn::TurnId;

pub const REPLAY_USER_MESSAGES: &str = "--replay-user-messages";

/// An accepted message reserved for the process of another turn of the same agent.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Join {
    pub into: TurnId,
    pub uuid: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub recovery: Option<Recovery>,
    /// The message goes to the tool in the prompt the turn starts with, together with the
    /// message the turn began for, rather than into a process that already runs. Such a
    /// join is made only while the turn's first attempt is being started, and its read is
    /// written in the same append: the user's messages that waited behind the turn's own
    /// are one turn, read at once.
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub together: bool,
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

/// A message of the user's that went to the tool in the prompt of another turn, with the
/// message that turn began for ([`Join::together`]).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Gathered {
    pub turn: TurnId,
    pub message_seq: u64,
    /// What it had as it waited: it waits so again if the turn gives it back.
    pub origin: crate::turn::Origin,
    pub accepted_at_ms: i64,
    /// How often it was given back before this turn took it ([`crate::turn::Queued::given_back`]).
    pub given_back: u32,
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

/// Evidence from the owning Claude process for offering and settling follow-ups.
#[derive(Default)]
pub struct Stream {
    input_ended: bool,
}

pub enum Observation<'a> {
    Other,
    Echo(&'a str),
    Result { prompts: &'a [Value], failed: bool },
}

impl Stream {
    pub fn can_join(&self) -> bool {
        !self.input_ended
    }

    pub fn observe<'a>(&mut self, event: &'a Value) -> Observation<'a> {
        if event["type"] == "user" && event["isReplay"] == true {
            return nonempty(&event["uuid"]).map_or(Observation::Other, Observation::Echo);
        }
        // A Codex lane's turn: the reading of steered words, and the end of the turn, after
        // which nothing more is taken into it.
        if event["type"] == "steer.read" {
            return nonempty(&event["id"]).map_or(Observation::Other, Observation::Echo);
        }
        if event["type"] == "turn.completed" || event["type"] == "turn.failed" {
            self.input_ended = true;
            return Observation::Other;
        }
        if event["type"] == "result" {
            // The reference observes results before filtering nested events.
            self.input_ended = true;
            if nonempty(&event["parent_tool_use_id"]).is_some() {
                return Observation::Other;
            }
            let prompts = event["user_message_uuids"]
                .as_array()
                .map_or(&[][..], Vec::as_slice);
            return Observation::Result {
                prompts,
                failed: event["is_error"] == true,
            };
        }
        Observation::Other
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn a_codex_lane_says_what_it_read_and_takes_nothing_after_its_turn_ended() {
        let mut stream = Stream::default();
        let read = json!({"type": "steer.read", "id": "words"});
        assert!(matches!(stream.observe(&read), Observation::Echo("words")));
        let nameless = json!({"type": "steer.read", "id": ""});
        assert!(matches!(stream.observe(&nameless), Observation::Other));
        assert!(stream.can_join());
        let ended = json!({"type": "turn.completed", "usage": null});
        assert!(matches!(stream.observe(&ended), Observation::Other));
        assert!(!stream.can_join());
        let mut failed = Stream::default();
        failed.observe(&json!({"type": "turn.failed", "error": "gone"}));
        assert!(!failed.can_join());
    }

    #[test]
    fn a_read_follow_up_is_a_user_entry_or_a_queued_command_attachment() {
        for (entry, expected) in [
            (
                json!({"type": "user", "uuid": "prompt"}),
                Some(("prompt", false)),
            ),
            (json!({"type": "assistant", "uuid": "prompt"}), None),
            (
                json!({"type": "attachment", "uuid": "entry", "attachment": {
                    "type": "queued_command", "source_uuid": "prompt"
                }}),
                Some(("prompt", true)),
            ),
            (
                json!({"type": "attachment", "attachment": {
                    "type": "background", "source_uuid": "same"
                }}),
                None,
            ),
            (
                json!({"type": "attachment", "uuid": "entry", "attachment": {
                    "type": "queued_command"
                }}),
                None,
            ),
        ] {
            assert_eq!(transcript_read_uuid(&entry), expected);
        }
    }

    #[test]
    fn a_nested_result_closes_offers_and_only_a_top_level_result_settles() {
        let mut stream = Stream::default();
        let text = json!({"type":"stream_event","event":{"content_block":{"type":"text"}}});
        stream.observe(&text);
        assert!(stream.can_join(), "a text-only turn can take input");

        let nested = json!({
            "type": "result", "parent_tool_use_id": "task", "user_message_uuids": ["child"],
        });
        assert!(matches!(stream.observe(&nested), Observation::Other));
        assert!(!stream.can_join(), "a nested result closes further offers");
        stream.observe(&text);
        assert!(!stream.can_join(), "later output leaves offers closed");

        let echo = json!({"type":"user","isReplay":true,"uuid":"two"});
        assert!(matches!(stream.observe(&echo), Observation::Echo("two")));
        let result = json!({"type":"result","user_message_uuids":["one","two"]});
        assert!(
            matches!(stream.observe(&result), Observation::Result { prompts, failed: false } if prompts == [json!("one"), json!("two")])
        );
        stream.observe(&text);
        assert!(!stream.can_join(), "offers stay closed after settlement");
    }
}
