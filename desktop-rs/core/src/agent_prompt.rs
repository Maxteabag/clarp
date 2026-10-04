//! A message another agent sent into a chat (a user row with origin
//! "agent"): it shows folded to one line, "<Name> prompted · <first line>",
//! until it is opened by its key, which keeps it open per message id.

use crate::protocol::Message;

/// The key a prompt is opened and folded by (and J/K reach it by).
pub const PREFIX: &str = "a2a:";
/// The longest first line kept; the row elides it to its width.
pub const LINE_CHARS: usize = 200;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AgentPrompt {
    pub key: String,
    pub sender: String,
    pub line: String,
}

impl AgentPrompt {
    /// "Rachel prompted · Run the build" ("Rachel prompted" when empty).
    pub fn label(&self) -> String {
        if self.line.is_empty() { format!("{} prompted", self.sender) } else { format!("{} prompted · {}", self.sender, self.line) }
    }
}

pub fn key(id: &str) -> String {
    format!("{PREFIX}{id}")
}

/// The prompt a user row from another agent is; none for the user's own
/// words or a reply.
pub fn of(message: &Message) -> Option<AgentPrompt> {
    if message.role != "user" || message.origin != "agent" {
        return None;
    }
    let sender = [&message.sender_name, &message.sender_session].into_iter().find(|s| !s.trim().is_empty());
    let text = if message.display_text.is_empty() { &message.text } else { &message.display_text };
    Some(AgentPrompt {
        key: key(&message.id),
        sender: sender.map_or_else(|| "An agent".to_owned(), |s| s.trim().to_owned()),
        line: first_line(text),
    })
}

/// The first line with words in it, as plain text (no Markdown syntax),
/// cut at `LINE_CHARS`.
pub fn first_line(text: &str) -> String {
    let line = text.lines().map(crate::text::plain_preview_text).find(|l| !l.is_empty()).unwrap_or_default();
    if line.chars().count() <= LINE_CHARS {
        return line;
    }
    let cut: String = line.chars().take(LINE_CHARS).collect();
    format!("{}…", cut.trim_end())
}
