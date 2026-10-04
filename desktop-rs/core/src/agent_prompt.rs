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
    pub fn label(&self) -> String {
        todo!()
    }
}

pub fn key(id: &str) -> String {
    todo!("{id}")
}

pub fn of(message: &Message) -> Option<AgentPrompt> {
    todo!("{}", message.id)
}
