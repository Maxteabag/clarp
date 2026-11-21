//! Provider evidence for taking over a turn that has produced no answer.
//!
//! These rules interpret the input the actor has read. The actor owns the decision
//! to hold the tool and the spool fence that bounds the input.

use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha1::{Digest, Sha1};

use crate::{python, turn::TurnId};

/// The takeover persisted on its new turn, including the exact native session to launch on.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Start {
    pub from: TurnId,
    pub session: String,
    pub input: Input,
}

/// The attempt chosen before asking the shim to consume its hold.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Selection {
    pub into: TurnId,
    pub attempt: u32,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub trace: Option<String>,
    pub start: Start,
}

/// A provider launch chosen while the first turn's tool stood still.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "kebab-case")]
pub enum Input {
    ClaudeResume {
        first: String,
        resume_at: String,
        drops_turn: String,
    },
    ClaudeFresh {
        first: String,
    },
    Agy {
        first: String,
    },
}

/// The UUID Claude stores for a user line. Callers without a trace use their turn identity.
pub fn prompt_uuid(trace: &str) -> Option<String> {
    let trace = python::strip(trace);
    (!trace.is_empty()).then(|| named_uuid(trace))
}

/// A stable identity for a turn accepted before traces were kept, or without a client trace.
/// A replay of that turn must still refer to its original Claude input line.
pub fn untraced_uuid(agent: &str, turn: u64) -> String {
    named_uuid(&format!("untraced:{agent}:{turn}"))
}

fn named_uuid(trace: &str) -> String {
    const NAMESPACE: [u8; 16] = [
        0x5f, 0x0e, 0x7c, 0x52, 0x9a, 0x5b, 0x4d, 0x9e, 0x8f, 0x53, 0x2f, 0x0b, 0x9b, 0x1d, 0x49,
        0x10,
    ];
    let mut hash = Sha1::new();
    hash.update(NAMESPACE);
    hash.update(trace.as_bytes());
    let hash = hash.finalize();
    let mut uuid: [u8; 16] = [0; 16];
    uuid.copy_from_slice(&hash[..16]);
    uuid[6] = (uuid[6] & 0x0f) | 0x50;
    uuid[8] = (uuid[8] & 0x3f) | 0x80;
    let hex: String = uuid.iter().map(|byte| format!("{byte:02x}")).collect();
    format!(
        "{}-{}-{}-{}-{}",
        &hex[..8],
        &hex[8..12],
        &hex[12..16],
        &hex[16..20],
        &hex[20..]
    )
}

/// One Claude input envelope. Each text block remains distinct, including the original order.
pub fn claude_user_line(uuid: &str, texts: &[&str]) -> Value {
    let content: Vec<_> = texts
        .iter()
        .map(|text| json!({"type": "text", "text": text}))
        .collect();
    json!({"type": "user", "uuid": uuid, "message": {"role": "user", "content": content}})
}

/// Whether this Claude event has begun to answer or use a tool.
pub fn claude_output(event: &Value) -> bool {
    let output = |tag: &Value| {
        matches!(
            tag.as_str(),
            Some("text" | "text_delta" | "tool_use" | "input_json_delta" | "server_tool_use")
        )
    };
    match event["type"].as_str() {
        Some("stream_event") => {
            let inner = &event["event"];
            output(&inner["content_block"]["type"]) || output(&inner["delta"]["type"])
        }
        Some("assistant") => event["message"]["content"]
            .as_array()
            .is_some_and(|parts| parts.iter().any(|part| output(&part["type"]))),
        _ => false,
    }
}

/// Whether this Antigravity event has begun to answer or use a tool.
pub fn agy_output(event: &Value) -> bool {
    if event["event"] != "step_update" {
        return false;
    }
    let update = &event["step_update"];
    match update["step_type"].as_str() {
        Some("tool") => true,
        Some("agent_response") => {
            let delta = &update["text_delta"];
            delta.as_str().map_or_else(
                || python::truthy(delta),
                |text| !python::strip(text).is_empty(),
            )
        }
        _ => false,
    }
}

/// The original prompt's place in a readable Claude transcript.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct ClaudeFacts {
    pub readable: bool,
    pub found: bool,
    pub resume_at: String,
    pub answered: bool,
}

/// Find the last user or assistant UUID before the original prompt. A complete assistant
/// entry after that prompt rules out takeover even if the live stream has not shown it.
pub fn claude_facts(transcript: Option<&str>, prompt: &str) -> ClaudeFacts {
    let Some(transcript) = transcript else {
        return ClaudeFacts::default();
    };
    let mut facts = ClaudeFacts {
        readable: true,
        ..ClaudeFacts::default()
    };
    for line in python::splitlines(transcript) {
        let Ok(entry) = serde_json::from_str::<Value>(line) else {
            continue;
        };
        let kind = entry["type"].as_str();
        if facts.found {
            facts.answered |= kind == Some("assistant");
            continue;
        }
        let uuid = entry["uuid"].as_str().unwrap_or_default();
        if kind == Some("user") && uuid == prompt {
            facts.found = true;
        } else if !uuid.is_empty() && matches!(kind, Some("user" | "assistant")) {
            facts.resume_at = uuid.to_string();
        }
    }
    facts
}

/// Antigravity can resume with both words only when its transcript is readable and does not
/// already contain the first message in a user input.
pub fn agy_can_take_over(turns: Option<&[Value]>, first: &str) -> bool {
    let first = python::strip(first);
    !first.is_empty()
        && turns.is_some_and(|turns| {
            !turns.iter().any(|turn| {
                turn["role"] == "user"
                    && turn["text"]
                        .as_str()
                        .is_some_and(|text| text.contains(first))
            })
        })
}

/// The minimum version that provides the recorded takeover operations on each backend.
pub fn supported_version(backend: &str, output: &str) -> bool {
    let minimum = match backend {
        "claude" => [2, 1, 223],
        "agy" => [1, 5, 13],
        _ => return false,
    };
    let Ok(pattern) = regex::Regex::new(r"([0-9]+)\.([0-9]+)\.([0-9]+)") else {
        return false;
    };
    let Some(version) = pattern.captures(output) else {
        return false;
    };
    let digits = [1, 2, 3].map(|index| version[index].parse::<u32>().ok());
    matches!(digits, [Some(major), Some(minor), Some(patch)] if [major, minor, patch] >= minimum)
}

/// The prior provider transcript is the authority for a resume point. A fresh first
/// turn has no prior provider history to preserve and can use a new provider session.
pub fn claude_input(
    facts: ClaudeFacts,
    first_turn: bool,
    first: String,
    drops_turn: String,
) -> Option<Input> {
    if facts.answered {
        return None;
    }
    if facts.readable && !facts.resume_at.is_empty() {
        return Some(Input::ClaudeResume {
            first,
            resume_at: facts.resume_at,
            drops_turn,
        });
    }
    first_turn.then_some(Input::ClaudeFresh { first })
}
