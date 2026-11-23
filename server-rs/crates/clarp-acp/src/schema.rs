//! The messages of the protocol Clarp sends and reads, as far as Clarp uses them. What an
//! agent sends that is not described here is kept as it was sent (`Other`, or a `Value`), never
//! refused: an agent of a later version says more than this client knows.

use serde::Deserialize;
use serde_json::{Value, json};

/// What a `session/update` says the agent is doing.
#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(tag = "sessionUpdate", rename_all = "snake_case")]
pub enum SessionUpdate {
    /// A piece of what the agent says.
    AgentMessageChunk { content: ContentBlock },
    /// A piece of what the agent thinks.
    AgentThoughtChunk { content: ContentBlock },
    /// A piece of what the user said: a conversation loaded again, told anew.
    UserMessageChunk { content: ContentBlock },
    /// A tool call begins.
    ToolCall(ToolCall),
    /// A tool call changes: what it was given, how it stands, what it returned.
    ToolCallUpdate(ToolCall),
    /// The agent's plan, whole.
    Plan {
        #[serde(default)]
        entries: Vec<PlanEntry>,
    },
    /// How much of its context the conversation holds now.
    UsageUpdate {
        #[serde(default)]
        used: Option<u64>,
        #[serde(default)]
        size: Option<u64>,
    },
    /// The conversation's options changed (its model among them).
    ConfigOptionUpdate {
        #[serde(default, rename = "configOptions")]
        config_options: Vec<Value>,
    },
    #[serde(other)]
    Other,
}

impl SessionUpdate {
    /// The update a `session/update` notification carries, where it is one of the protocol.
    pub fn of_notification(message: &Value) -> Option<Self> {
        if message["method"] != crate::method::SESSION_UPDATE {
            return None;
        }
        serde_json::from_value(message["params"]["update"].clone()).ok()
    }
}

/// A block of content, as far as Clarp reads one: its text.
#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum ContentBlock {
    Text {
        text: String,
    },
    #[serde(other)]
    Other,
}

impl ContentBlock {
    pub fn text(&self) -> &str {
        match self {
            Self::Text { text } => text,
            Self::Other => "",
        }
    }
}

/// A tool call, or what changed of one: every field but its id may be left out of a change.
#[derive(Debug, Clone, Default, PartialEq, Deserialize)]
#[serde(rename_all = "camelCase", default)]
pub struct ToolCall {
    pub tool_call_id: String,
    pub title: Option<String>,
    /// `read`, `edit`, `delete`, `move`, `search`, `execute`, `think`, `fetch` or `other`.
    pub kind: Option<String>,
    /// `pending`, `in_progress`, `completed` or `failed`.
    pub status: Option<String>,
    pub raw_input: Option<Value>,
    pub raw_output: Option<Value>,
    pub content: Option<Vec<Value>>,
    pub locations: Option<Vec<Location>>,
}

impl ToolCall {
    /// Whether the call is over, one way or the other.
    pub fn ended(&self) -> bool {
        matches!(self.status.as_deref(), Some("completed" | "failed"))
    }
}

#[derive(Debug, Clone, Default, PartialEq, Deserialize)]
#[serde(default)]
pub struct Location {
    pub path: String,
    pub line: Option<u64>,
}

#[derive(Debug, Clone, Default, PartialEq, Deserialize)]
#[serde(default)]
pub struct PlanEntry {
    pub content: String,
    pub priority: Option<String>,
    pub status: Option<String>,
}

/// Why the agent stopped answering a prompt.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum StopReason {
    EndTurn,
    MaxTokens,
    MaxTurnRequests,
    Refusal,
    Cancelled,
    Other(String),
}

impl StopReason {
    pub fn read(said: &str) -> Self {
        match said {
            "end_turn" => Self::EndTurn,
            "max_tokens" => Self::MaxTokens,
            "max_turn_requests" => Self::MaxTurnRequests,
            "refusal" => Self::Refusal,
            "cancelled" => Self::Cancelled,
            other => Self::Other(other.to_string()),
        }
    }
}

/// What answering a prompt took, where the agent says it.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "camelCase", default)]
pub struct Usage {
    pub input_tokens: Option<u64>,
    pub output_tokens: Option<u64>,
    pub thought_tokens: Option<u64>,
    pub cached_read_tokens: Option<u64>,
}

impl Usage {
    /// What went in, cache reads with it, and what came out, thinking with it: as the Host
    /// counts every tool's turn. `None` where neither in nor out is said.
    pub fn tokens(&self) -> Option<(u64, u64)> {
        if self.input_tokens.is_none() && self.output_tokens.is_none() {
            return None;
        }
        Some((
            self.input_tokens
                .unwrap_or(0)
                .saturating_add(self.cached_read_tokens.unwrap_or(0)),
            self.output_tokens
                .unwrap_or(0)
                .saturating_add(self.thought_tokens.unwrap_or(0)),
        ))
    }
}

/// The answer to a prompt.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PromptResponse {
    pub stop_reason: StopReason,
    pub usage: Option<Usage>,
}

impl PromptResponse {
    pub fn read(result: &Value) -> Self {
        Self {
            stop_reason: StopReason::read(result["stopReason"].as_str().unwrap_or_default()),
            usage: serde_json::from_value(result["usage"].clone()).ok(),
        }
    }
}

/// A way to answer a permission request.
#[derive(Debug, Clone, Default, PartialEq, Deserialize)]
#[serde(rename_all = "camelCase", default)]
pub struct PermissionOption {
    pub option_id: String,
    pub name: String,
    /// `allow_once`, `allow_always`, `reject_once` or `reject_always`.
    pub kind: String,
}

/// The options of a permission request.
pub fn permission_options(params: &Value) -> Vec<PermissionOption> {
    serde_json::from_value(params["options"].clone()).unwrap_or_default()
}

/// Clarp's policy for a permission request: an agent is never kept waiting for a person. It
/// is allowed (once, else for good), as its tool's own skip of every permission would; a
/// request that offers no way to allow (a question with fixed choices) is answered with its
/// first choice. `None` only for a request that offers nothing at all.
pub fn chosen(options: &[PermissionOption]) -> Option<&PermissionOption> {
    let usable = |option: &&PermissionOption| !option.option_id.trim().is_empty();
    ["allow_once", "allow_always"]
        .iter()
        .find_map(|kind| {
            options
                .iter()
                .filter(usable)
                .find(|option| option.kind == *kind)
        })
        .or_else(|| options.iter().find(usable))
}

/// The answer to a permission request: the option chosen, or cancelled where there is none.
pub fn permission_outcome(chosen: Option<&PermissionOption>) -> Value {
    match chosen {
        Some(option) => json!({"outcome": {"outcome": "selected", "optionId": option.option_id}}),
        None => json!({"outcome": {"outcome": "cancelled"}}),
    }
}

/// The values a select option offers, with their names, groups flattened.
pub fn offered(option: &Value) -> Vec<(String, String)> {
    let mut values = Vec::new();
    let mut take = |entry: &Value| {
        if let Some(value) = entry["value"].as_str() {
            let name = entry["name"].as_str().unwrap_or(value);
            values.push((value.to_string(), name.to_string()));
        }
    };
    for entry in option["options"].as_array().into_iter().flatten() {
        match entry["options"].as_array() {
            Some(group) => group.iter().for_each(&mut take),
            None => take(entry),
        }
    }
    values
}

/// The option of a category (`model`, `thought_level`), by its category or else its id.
pub fn option<'a>(options: &'a Value, category: &str) -> Option<&'a Value> {
    let options = options.as_array()?;
    options
        .iter()
        .find(|option| option["category"] == category)
        .or_else(|| options.iter().find(|option| option["id"] == category))
}

/// The value of an option that is what was asked for: the same value or the same name,
/// whatever the case.
pub fn matching(option: &Value, wanted: &str) -> Option<String> {
    let wanted = wanted.trim();
    offered(option)
        .into_iter()
        .find(|(value, name)| {
            value.eq_ignore_ascii_case(wanted) || name.eq_ignore_ascii_case(wanted)
        })
        .map(|(value, _)| value)
}

/// The current value of the option of a category.
pub fn current(options: &Value, category: &str) -> Option<String> {
    option(options, category)
        .and_then(|option| option["currentValue"].as_str())
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(str::to_string)
}

pub fn text_block(text: &str) -> Value {
    json!({"type": "text", "text": text})
}

pub fn image_block(mime: &str, base64: String) -> Value {
    json!({"type": "image", "mimeType": mime, "data": base64})
}

pub fn audio_block(mime: &str, base64: String) -> Value {
    json!({"type": "audio", "mimeType": mime, "data": base64})
}

pub fn resource_link(uri: &str, name: &str, mime: &str, size: u64) -> Value {
    json!({"type": "resource_link", "uri": uri, "name": name, "mimeType": mime, "size": size})
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;

    #[test]
    fn updates_are_read_by_their_kind_and_an_unknown_one_is_kept_apart() {
        let message = json!({"jsonrpc": "2.0", "method": "session/update", "params": {
            "sessionId": "s", "update": {"sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": "Hi"}}}});
        let update = SessionUpdate::of_notification(&message).unwrap();
        assert!(
            matches!(&update, SessionUpdate::AgentMessageChunk { content } if content.text() == "Hi")
        );
        let call: SessionUpdate = serde_json::from_value(json!({"sessionUpdate": "tool_call",
            "toolCallId": "t", "kind": "execute", "status": "pending",
            "rawInput": {"CommandLine": "ls"}, "locations": [{"path": "/w"}]}))
        .unwrap();
        let SessionUpdate::ToolCall(call) = call else {
            panic!("a tool call");
        };
        assert_eq!(call.tool_call_id, "t");
        assert_eq!(call.raw_input.unwrap()["CommandLine"], "ls");
        assert_eq!(call.locations.unwrap()[0].path, "/w");
        let later: SessionUpdate =
            serde_json::from_value(json!({"sessionUpdate": "something_new", "x": 1})).unwrap();
        assert_eq!(later, SessionUpdate::Other);
        let image: SessionUpdate = serde_json::from_value(json!({"sessionUpdate":
            "agent_message_chunk", "content": {"type": "image", "data": "", "mimeType": "image/png"}}))
        .unwrap();
        assert!(matches!(
            image,
            SessionUpdate::AgentMessageChunk {
                content: ContentBlock::Other
            }
        ));
    }

    #[test]
    fn a_prompt_answer_says_why_it_stopped_and_what_it_took() {
        let answer = PromptResponse::read(&json!({"stopReason": "end_turn",
            "usage": {"inputTokens": 10, "outputTokens": 5, "thoughtTokens": 2, "cachedReadTokens": 3}}));
        assert_eq!(answer.stop_reason, StopReason::EndTurn);
        assert_eq!(answer.usage.unwrap().tokens(), Some((13, 7)));
        let bare = PromptResponse::read(&json!({"stopReason": "cancelled"}));
        assert_eq!(bare.stop_reason, StopReason::Cancelled);
        assert_eq!(bare.usage.and_then(|usage| usage.tokens()), None);
    }

    #[test]
    fn a_permission_is_allowed_and_a_question_takes_its_first_choice() {
        let options = |value: Value| permission_options(&json!({"options": value}));
        let allowing = options(json!([
            {"optionId": "no", "name": "No", "kind": "reject_once"},
            {"optionId": "always", "name": "Always", "kind": "allow_always"},
            {"optionId": "once", "name": "Once", "kind": "allow_once"}]));
        assert_eq!(chosen(&allowing).unwrap().option_id, "once");
        let question = options(json!([
            {"optionId": "red", "name": "Red", "kind": "reject_once"},
            {"optionId": "blue", "name": "Blue", "kind": "reject_once"}]));
        assert_eq!(chosen(&question).unwrap().option_id, "red");
        assert_eq!(
            permission_outcome(chosen(&question)),
            json!({"outcome": {"outcome": "selected", "optionId": "red"}})
        );
        assert_eq!(chosen(&[]), None);
        assert_eq!(
            permission_outcome(None),
            json!({"outcome": {"outcome": "cancelled"}})
        );
    }

    #[test]
    fn options_are_found_by_category_or_id_and_matched_by_value_or_name() {
        let options = json!([
            {"id": "mode", "category": "mode", "options": [{"value": "default"}]},
            {"id": "model", "category": "model", "currentValue": "gemini-a",
             "options": [{"group": "g", "name": "G", "options": [
                 {"value": "gemini-a", "name": "Gemini A"},
                 {"value": "claude-b", "name": "Claude B"}]}]},
            {"id": "thought_level", "options": [{"value": "low"}, {"value": "high"}]}
        ]);
        let model = option(&options, "model").unwrap();
        assert_eq!(matching(model, "Claude B").as_deref(), Some("claude-b"));
        assert_eq!(matching(model, "GEMINI-A").as_deref(), Some("gemini-a"));
        assert_eq!(matching(model, "nope"), None);
        assert_eq!(current(&options, "model").as_deref(), Some("gemini-a"));
        let thought = option(&options, "thought_level").unwrap();
        assert_eq!(matching(thought, "high").as_deref(), Some("high"));
    }
}
