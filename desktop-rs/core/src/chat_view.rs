//! Conversation and Session views of a chat, chosen per agent (issue 702,
//! feature `chat_view`, Rust Host contract 145; docs/protocol.md
//! "Conversation and session views"). The same rules and words as the iOS
//! app's `ChatView.swift` and `ChatViewRows.swift`.
//!
//! - Conversation: the user's messages, the agent's deliberate posts, each
//!   turn's final reply, and cards, forms, decisions and artifacts. While a
//!   turn runs one Running box stands in for its work; a finished turn's
//!   work opens from its reply (Show work / Hide work), never from a line.
//! - Session: everything, as before, with the quiet chat's Tool activity.
//!
//! The choice is the Host's agent setting `chat_view`, so every device
//! agrees; a write the Host refuses is kept on this device. Clean chat is
//! this device's switch that shows every chat without its own choice in
//! Conversation view. Like the phones, the desktop reads every row and
//! filters by each row's `view`. Pure: the engine and the window use it.

use std::collections::{HashMap, HashSet};

use crate::live_present::{Entry, Kind, Presented};
use crate::protocol::Message;

pub const FEATURE: &str = "chat_view";
/// `POST {session, chat_view}` answers `{ok, session, chat_view}`.
pub const WRITE_PATH: &str = "/agent-chat-view";
/// The `agent-roster` kind that carries `session` and `chat_view`.
pub const ROSTER_KIND: &str = "chat_view";
pub const TITLE: &str = "Chat view";
/// This device's own choices, `{chat key: "conversation" | "session"}`.
pub const LOCAL_KEY: &str = "conversation/chatView";
/// Settings › Chats › Clean chat: this device's default for every chat.
pub const CLEAN_KEY: &str = "conversation/cleanChat";
pub const CLEAN_CHAT: &str = "Clean chat";
pub const CLEAN_CHAT_DETAIL: &str =
    "Every chat in Conversation view: only your messages and the replies, one Running… box while an agent works.";
/// Tool activity in Conversation view (the chat's profile).
pub const TOOL_ACTIVITY_IN_CONVERSATION: &str = "Applies in Session view";
pub const SHOW_WORK: &str = "Show work";
pub const HIDE_WORK: &str = "Hide work";
/// The key of a finished turn's work, `work:<trace id>`: what J/K select on
/// its reply and what the reader opened.
pub const WORK_PREFIX: &str = "work:";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Choice {
    Conversation,
    Session,
}

impl Choice {
    pub fn wire(self) -> &'static str {
        match self {
            Choice::Conversation => "conversation",
            Choice::Session => "session",
        }
    }

    pub fn label(self) -> &'static str {
        match self {
            Choice::Conversation => "Conversation",
            Choice::Session => "Session",
        }
    }

    pub fn detail(self) -> &'static str {
        match self {
            Choice::Conversation => {
                "Your messages, the agent's posts and replies, cards and artifacts. Each turn's work opens with Show work."
            }
            Choice::Session => "Everything, as it ran: tool calls, the agent's notes along the way and messages between agents.",
        }
    }

    /// A value from the Host: nil for none said; an unknown one reads as
    /// Session, the view that hides nothing.
    pub fn decoded(wire: &str) -> Option<Choice> {
        let wire = wire.trim();
        if wire.is_empty() {
            return None;
        }
        Some(if wire.eq_ignore_ascii_case("conversation") { Choice::Conversation } else { Choice::Session })
    }

    pub fn other(self) -> Choice {
        match self {
            Choice::Conversation => Choice::Session,
            Choice::Session => Choice::Conversation,
        }
    }
}

/// The entries of a chat's Chat view menu (its ⋯ menu and its profile):
/// Follow Settings (no choice of its own: Clean chat, then the Host), or one
/// of the two views as the chat's own choice.
pub const MENU: [(&str, &str); 3] = [("default", "Follow Settings"), ("conversation", "Conversation"), ("session", "Session")];

/// The menu entry for a chat's own choice.
pub fn menu_value(own: Option<Choice>) -> &'static str {
    own.map_or("default", Choice::wire)
}

/// The words of a menu entry.
pub fn menu_label(value: &str) -> &'static str {
    MENU.iter().find(|(v, _)| *v == value).map_or("Follow Settings", |(_, label)| label)
}

/// The view a menu entry makes the chat's own; None: Follow Settings.
pub fn menu_choice(value: &str) -> Option<Choice> {
    match value {
        "conversation" => Some(Choice::Conversation),
        "session" => Some(Choice::Session),
        _ => None,
    }
}

/// The quiet line after a refused write.
pub fn refused_words(host: &str) -> String {
    if host.trim().is_empty() {
        "Not saved on the server. This device keeps the view.".to_owned()
    } else {
        format!("Not saved on {}. This device keeps the view.", host.trim())
    }
}

/// The toggle of one chat: the Host's value, a write in flight, and the
/// chat's own choice made on this device. The engine performs the write and
/// reports back with the generation `choose` gave it.
///
/// What a chat draws: a write in flight, else the chat's own choice, else
/// Clean chat (Conversation), else the Host's value, else Session.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Setting {
    /// The Host's agent setting as last read or written; None: not said.
    pub host: Option<Choice>,
    /// The choice whose write is in flight.
    pub pending: Option<Choice>,
    /// The chat's own choice on this device, kept whether the Host saved
    /// it or refused it.
    pub local: Option<Choice>,
    /// The last write was refused: one quiet line says so.
    pub refused: bool,
    /// Counts writes, so a late answer to an older one changes nothing.
    pub generation: u64,
}

impl Setting {
    pub fn effective(&self, clean: bool) -> Choice {
        self.pending.or(self.local).unwrap_or(if clean { Choice::Conversation } else { self.host.unwrap_or(Choice::Session) })
    }

    /// The reader picks `choice`. Returns the write's generation, or None
    /// when nothing changes (it is already this chat's own choice and
    /// nothing is pending).
    pub fn choose(&mut self, choice: Choice) -> Option<u64> {
        let changes = match self.pending {
            Some(pending) => pending != choice,
            None => self.local != Some(choice) || self.refused,
        };
        if !changes {
            return None;
        }
        self.pending = Some(choice);
        self.refused = false;
        self.generation += 1;
        Some(self.generation)
    }

    /// The Host saved write `generation`; `echo` is what it now holds.
    pub fn accepted(&mut self, generation: u64, echo: Option<Choice>) {
        let Some(pending) = self.pending.filter(|_| generation == self.generation) else { return };
        self.host = echo.or(Some(pending));
        self.local = Some(pending);
        self.pending = None;
        self.refused = false;
    }

    /// The Host refused write `generation`: the choice stays, on this device.
    pub fn refused_write(&mut self, generation: u64) {
        let Some(pending) = self.pending.filter(|_| generation == self.generation) else { return };
        self.local = Some(pending);
        self.pending = None;
        self.refused = true;
    }

    /// The Host's value changed (`agent-roster` kind `chat_view`). Another
    /// device's different choice replaces this device's own; the echo of
    /// this device's write keeps it.
    pub fn host_read(&mut self, value: Option<Choice>) {
        self.host = value;
        if let (Some(local), Some(value)) = (self.local, value) {
            if local != value {
                self.local = None;
                self.refused = false;
            }
        }
    }

    /// The chat's own choice, in flight or kept.
    pub fn own(&self) -> Option<Choice> {
        self.pending.or(self.local)
    }

    /// Follow Settings: the chat's own choice goes (no Host write). A write
    /// in flight is forgotten: its late answer changes nothing. Returns
    /// whether anything changed.
    pub fn clear_own(&mut self) -> bool {
        if self.own().is_none() && !self.refused {
            return false;
        }
        if self.pending.is_some() {
            self.generation += 1;
        }
        self.pending = None;
        self.local = None;
        self.refused = false;
        true
    }
}

/// Whether a row belongs to the Conversation view. A value this client
/// does not know, or none, reads as Session: a row the Host meant to hide
/// is never shown. One exception: the user's own message this desktop made
/// (not yet read back) is always conversation.
pub fn is_conversation(row: &Message) -> bool {
    let view = row.view.trim();
    if view.is_empty() {
        return row.role == "user"
            && matches!(row.origin.as_str(), "" | "user")
            && row.sender_agent_id.is_empty()
            && row.sender_session.is_empty();
    }
    view.eq_ignore_ascii_case("conversation")
}

/// Whether the rows carry the Host's tags at all. A chat drawn from a cache
/// written before them is drawn as Session until the Host's read brings
/// them, so it never opens empty.
pub fn tagged(rows: &[Message]) -> bool {
    rows.iter().any(|r| !r.view.trim().is_empty())
}

pub fn work_key(trace: &str) -> String {
    format!("{WORK_PREFIX}{trace}")
}

/// The turn that runs now: the live turn while it runs; else, while the
/// agent works, the newest prompt's turn unless its anchor says it ended
/// (`Some("")` when that prompt has no trace: a turn runs, which is not
/// known); else None.
pub fn running(rows: &[Message], live_trace: Option<&str>, live_running: bool, busy: bool) -> Option<String> {
    if live_running {
        if let Some(trace) = live_trace.filter(|t| !t.is_empty()) {
            return Some(trace.to_owned());
        }
    }
    if !busy {
        return None;
    }
    let prompt = rows.iter().rev().find(|r| r.role == "user")?;
    if prompt.trace_id.is_empty() {
        return Some(String::new());
    }
    if rows.iter().any(|r| r.trace_id == prompt.trace_id && r.session_rows.is_some()) {
        return None;
    }
    Some(prompt.trace_id.clone())
}

/// The Conversation view of a chat's rows.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Projection {
    /// Rows not drawn: every session row but those of a turn whose work
    /// the reader opened.
    pub hidden: HashSet<String>,
    /// The row a finished turn's work belongs to (its anchor, else its last
    /// drawn row) → the work's key: its Show work / Hide work.
    pub work_of_row: HashMap<String, String>,
    /// The turns whose work is open (trace ids).
    pub open: HashSet<String>,
}

impl Projection {
    /// The menu item of a row: "Show work" or "Hide work", and the key it
    /// toggles; None for a row without work of its own.
    pub fn work_item(&self, row: &str) -> Option<(&'static str, &str)> {
        let key = self.work_of_row.get(row)?;
        let trace = &key[WORK_PREFIX.len()..];
        Some((if self.open.contains(trace) { HIDE_WORK } else { SHOW_WORK }, key.as_str()))
    }
}

/// - `running`: the turn that runs now (see `running`); its rows are never
///   shown, nor offered as work.
/// - `expanded`: what the reader opened, among it `work:<trace>` keys.
pub fn project(rows: &[Message], running: Option<&str>, expanded: &HashSet<String>) -> Projection {
    let runs = |trace: &str| running.is_some_and(|r| !r.is_empty() && r == trace);
    let mut session_count: HashMap<&str, usize> = HashMap::new();
    let mut anchor: HashMap<&str, &str> = HashMap::new();
    let mut last_shown: HashMap<&str, &str> = HashMap::new();
    for row in rows {
        let trace = row.trace_id.as_str();
        if is_conversation(row) {
            if !trace.is_empty() {
                last_shown.insert(trace, row.id.as_str());
                if row.session_rows.is_some() {
                    anchor.insert(trace, row.id.as_str());
                }
            }
        } else if !trace.is_empty() {
            *session_count.entry(trace).or_default() += 1;
        }
    }
    let mut projection = Projection::default();
    for (trace, count) in &session_count {
        if *count == 0 || runs(trace) {
            continue;
        }
        let Some(row) = anchor.get(trace).or_else(|| last_shown.get(trace)) else { continue };
        let key = work_key(trace);
        if expanded.contains(&key) {
            projection.open.insert((*trace).to_owned());
        }
        projection.work_of_row.insert((*row).to_owned(), key);
    }
    for row in rows {
        if is_conversation(row) {
            continue;
        }
        if !row.trace_id.is_empty() && projection.open.contains(&row.trace_id) {
            continue;
        }
        projection.hidden.insert(row.id.clone());
    }
    projection
}

/// The Running box's words, from the live status line: a tool's
/// "● Running swift build · 0:12" as "Running swift build…", "◌ Thinking ·
/// 0:03" as "Thinking…", nothing known as "Running…".
pub fn running_text(status: &str) -> String {
    let mut text = status.trim();
    if let Some(first) = text.chars().next() {
        if !first.is_alphanumeric() {
            text = text[first.len_utf8()..].trim_start();
        }
    }
    if let Some(at) = text.find(" · ") {
        text = &text[..at];
    }
    let text = text.trim().trim_end_matches(['…', '.']).trim();
    if text.is_empty() { "Running…".to_owned() } else { format!("{text}…") }
}

/// The live turn in Conversation view.
///
/// - While it runs: one Running box (`live:fold:<turn>`, the work row's
///   key, so it opens in place as the work row does) in the status line's
///   words; opened, the turn's steps (tools, groups, plans, diffs) under it,
///   never its words.
/// - Once it ended: the messages the conversation keeps (`conversation_row`
///   for a durable row, a final reply for one still live); its work only
///   when the reader opened it (`open`), then as the session view draws it.
pub fn conversation_live(presented: &mut Presented, running: bool, status: &str, open: bool, conversation_row: &dyn Fn(&str) -> bool) {
    let key = format!("live:fold:{}", presented.turn_id);
    if running {
        let entries = std::mem::take(&mut presented.entries);
        let mut fold = entries.iter().find(|e| e.key == key).cloned().unwrap_or_else(|| Entry {
            key: key.clone(),
            kind: Kind::Fold,
            started_at_ms: presented.started_at_ms,
            ..Entry::default()
        });
        fold.title = running_text(status);
        fold.status = "running".to_owned();
        let steps: Vec<Entry> = entries
            .into_iter()
            .filter(|e| e.key != key && !matches!(e.kind, Kind::Message | Kind::Reasoning | Kind::Compaction | Kind::Fold))
            .collect();
        fold.expandable = !steps.is_empty();
        fold.expanded = fold.expanded && fold.expandable;
        let open = fold.expanded;
        presented.entries.push(fold);
        if open {
            presented.entries.extend(steps);
        }
        return;
    }
    let kept = |entry: &Entry| {
        entry.kind == Kind::Message
            && if entry.row.is_empty() { entry.phase == "final" } else { conversation_row(&entry.row) }
    };
    if open {
        return;
    }
    presented.entries.retain(kept);
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn running_words() {
        assert_eq!(running_text("● Running swift build · 0:12 +1"), "Running swift build…");
        assert_eq!(running_text("◌ Thinking · 0:03"), "Thinking…");
        assert_eq!(running_text(""), "Running…");
        assert_eq!(running_text("◌ Responding…"), "Responding…");
    }
}
