//! The two client projections of a chat (issue 702).

use serde::{Deserialize, Serialize};
use serde_json::Value;

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum View {
    Conversation,
    #[default]
    Session,
}

impl View {
    pub fn parse(value: &str) -> Option<Self> {
        match crate::python::strip(value).to_ascii_lowercase().as_str() {
            "conversation" => Some(Self::Conversation),
            "session" => Some(Self::Session),
            _ => None,
        }
    }

    pub fn word(self) -> &'static str {
        match self {
            Self::Conversation => "conversation",
            Self::Session => "session",
        }
    }

    pub fn reminder(self, session: &str, clean_conversation: bool) -> String {
        if self != Self::Conversation {
            return String::new();
        }
        let session = if session.is_empty() {
            "<your session>"
        } else {
            session
        };
        if clean_conversation {
            return format!(
                "Conversation view: the user sees deliberate messages and replies from the conversation session. Your ordinary work text appears in Session view. Deliver any answer or update for the user in a <speak> block, or send a note with `clarp-admin tell-user --from {session} --text '...'` if your backend supports notes."
            );
        }
        format!(
            "Conversation view: the user reads this chat in Conversation view. Your final reply reaches them; text you write in between, and your tool work, does not. To tell them something mid-turn, run `clarp-admin tell-user --from {session} --text '...'`."
        )
    }
}

pub fn setting_key(agent: &str) -> String {
    format!("chat_view.{agent}")
}

/// Rows received from another Host may predate the anchor count.
pub fn imported_is_conversation(row: &Value) -> bool {
    let role = row["role"].as_str().unwrap_or_default();
    let origin = row["origin"].as_str().filter(|word| !word.is_empty());
    if role == "user" {
        return matches!(origin, None | Some("user" | "oracle"))
            && !row["id"]
                .as_str()
                .is_some_and(crate::artifacts::forms::is_answer_row);
    }
    row["kind"] == "note"
        || origin == Some("system")
        || (role == "assistant"
            && (!row["session_rows"].is_null()
                || (row["trace_id"].as_str().unwrap_or_default().is_empty()
                    && row["phase"] == "final"
                    && row["tool_name"].as_str().unwrap_or_default().is_empty())))
}
