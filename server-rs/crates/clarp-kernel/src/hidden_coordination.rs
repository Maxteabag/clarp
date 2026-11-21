//! The user's coordination policy, separate from a turn's recorded audience.

use serde::Serialize;

use crate::python;

pub const GLOBAL_KEY: &str = "coordination.visibility";
pub const ROSTER_KIND: &str = "coordination-changed";
pub const GLOBAL_ERROR: &str = "coordination_visibility must be one of: show, hide";
pub const CHAT_ERROR: &str = "visibility must be one of: show, hide, default";
pub const MODE_ERROR: &str = "coordination must be auto, hide or show";

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Visibility {
    Show,
    #[default]
    Hide,
}

impl Visibility {
    pub fn read(raw: &str) -> Option<Self> {
        match python::strip(raw).to_lowercase().as_str() {
            "show" => Some(Self::Show),
            "hide" => Some(Self::Hide),
            _ => None,
        }
    }

    pub fn word(self) -> &'static str {
        match self {
            Self::Show => "show",
            Self::Hide => "hide",
        }
    }
}

pub fn chat_choice(raw: &str) -> Result<Option<Visibility>, &'static str> {
    if python::strip(raw).eq_ignore_ascii_case("default") {
        Ok(None)
    } else {
        Visibility::read(raw).map(Some).ok_or(CHAT_ERROR)
    }
}

pub fn chat_key(agent_id: &str) -> String {
    format!("coordination.chat.{agent_id}")
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Mode {
    Auto,
    Request(Visibility),
}

impl Mode {
    pub fn parse(raw: &str) -> Result<Option<Self>, &'static str> {
        match python::strip(raw).to_lowercase().as_str() {
            "" => Ok(None),
            "auto" => Ok(Some(Self::Auto)),
            "hide" => Ok(Some(Self::Request(Visibility::Hide))),
            "show" => Ok(Some(Self::Request(Visibility::Show))),
            _ => Err(MODE_ERROR),
        }
    }

    pub fn policy(self, global: Visibility, chat: Option<Visibility>) -> Policy {
        let (visibility, source) = match (self, chat) {
            (Self::Request(visibility), _) => (visibility, "request"),
            (_, Some(visibility)) => (visibility, "chat"),
            _ => (global, "global"),
        };
        Policy {
            hidden: visibility == Visibility::Hide,
            source,
            visibility,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub struct Policy {
    pub hidden: bool,
    pub source: &'static str,
    pub visibility: Visibility,
}

impl Policy {
    pub fn replaces(self, read: &str) -> bool {
        let read = python::strip(read);
        !read.is_empty() && !read.eq_ignore_ascii_case(self.visibility.word())
    }
}

pub fn reminder(session: &str, sender: Option<&str>) -> String {
    let sender = sender
        .filter(|sender| !sender.is_empty())
        .map_or_else(String::new, |name| format!(" ({name})"));
    format!(
        "Coordination: this turn answers a message from another agent{sender}. The user has \
         chosen not to see such turns in this chat, so nothing you write in it, your final \
         reply included, reaches the user. To tell the user something, run \
         `clarp-admin tell-user --from {session} --text '...'`: it appears in this chat \
         and notifies the user. Decision cards and artifacts are shown as always."
    )
}

/// The words shown while an agent works on a coordination turn its chat hides.
pub fn working_headline(name: Option<&str>) -> String {
    let partner = name
        .filter(|name| !name.is_empty())
        .unwrap_or("another agent");
    format!("Working with {partner}")
}

pub fn veil_activity(activity: &mut serde_json::Value, headline: &str) {
    use serde_json::{Value, json};

    if !headline.is_empty()
        && let Some(row) = activity.as_object_mut()
    {
        row.insert("tool".into(), json!(""));
        if row.get("phase").and_then(Value::as_str) != Some("account_recovery") {
            row.insert("action".into(), json!("working"));
            row.insert("summary".into(), json!(headline));
        }
        row.insert("file_path".into(), json!(""));
        row.insert("call_id".into(), Value::Null);
    }
}

pub fn veil_live(activity: &mut serde_json::Value, headline: &str) {
    use serde_json::{Value, json};

    if !headline.is_empty()
        && let Some(row) = activity.as_object_mut()
    {
        row.insert("headline".into(), json!(headline));
        row.insert("tool".into(), Value::Null);
    }
}

pub fn veil_detail(detail: &mut serde_json::Value, headline: &str) {
    if !headline.is_empty()
        && let Some(row) = detail.as_object_mut()
    {
        row.retain(|key, _| matches!(key.as_str(), "trace_id" | "phase" | "account_recovery"));
        row.insert("summary".into(), serde_json::json!(headline));
    }
}
