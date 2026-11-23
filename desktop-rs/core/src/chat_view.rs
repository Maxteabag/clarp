//! Conversation and Session views of a chat, chosen per agent (feature
//! `chat_view`, Host contract 145, issue 702; docs/protocol.md
//! "Conversation and session views"). The design and words are the iOS
//! app's (`ChatView.swift`, `ChatViewRows.swift`).
//!
//! Conversation: the user's messages, the agent's deliberate posts, each
//! turn's final reply, and cards; one Running box while a turn works, and
//! Show work on a finished turn's reply, which opens the rows the view left
//! out in their places. Session: everything, as before.
//!
//! What a chat draws: its own choice on this device (its menu), else Clean
//! chat (this device's switch: Conversation), else the Host's agent
//! setting, else Session. Pure: the engine keeps the choices and the
//! window splices the rows.

use std::collections::{HashMap, HashSet};

use serde_json::Value;

use crate::json::Object;
use crate::live_present::{Entry, Kind};
use crate::protocol::Message;

pub const FEATURE: &str = "chat_view";
/// The Host lets an agent answer from a fork while its own turn works.
pub const FORK_FEATURE: &str = "conversation_fork";
pub const CONVERSATION: &str = "conversation";
pub const SESSION: &str = "session";
/// `POST {session, chat_view}`; answers `{ok, session, chat_view}`.
pub const WRITE_PATH: &str = "/agent-chat-view";
/// The `agent-roster` kind carrying `session` and `chat_view`.
pub const ROSTER_KIND: &str = "chat_view";
/// The settings key of each chat's own choice on this device:
/// `{<chat key>: "conversation" | "session"}`.
pub const OWN_KEY: &str = "conversation/chatView";
/// The settings key of Clean chat (every chat in Conversation view).
pub const CLEAN_KEY: &str = "conversation/cleanChat";

pub const TITLE: &str = "Chat view";
pub const CLEAN_TITLE: &str = "Clean chat";
pub const CLEAN_DETAIL: &str = "Every chat in Conversation view: only your messages and the replies, one Running… box while an agent works.";
pub const SHOW_WORK: &str = "Show work";
pub const HIDE_WORK: &str = "Hide work";
/// The Tool activity menu's place in Conversation view.
pub const TOOL_ACTIVITY_IN_CONVERSATION: &str = "Applies in Session view";
/// The prefix of a finished turn's Show work key: `live:work:<trace>`.
/// Under `live:`, so a click or O toggles it as it does a live row.
pub const WORK_PREFIX: &str = "live:work:";

/// The chat menu's entries: (value, label). "default" follows Settings.
pub const MENU: [(&str, &str); 3] = [("default", "Follow Settings"), (CONVERSATION, "Conversation"), (SESSION, "Session")];

/// A value from the Host or the settings file: `conversation`, `session`,
/// or None for anything else (missing, empty, unknown).
pub fn decoded(value: &str) -> Option<&'static str> {
    match value.trim().to_ascii_lowercase().as_str() {
        CONVERSATION => Some(CONVERSATION),
        SESSION => Some(SESSION),
        _ => None,
    }
}

/// What a chat draws: its own choice, else Clean chat, else the Host's
/// value, else Session.
pub fn effective(own: Option<&str>, clean: bool, host: &str) -> &'static str {
    if let Some(own) = own.and_then(decoded) {
        return own;
    }
    if clean {
        return CONVERSATION;
    }
    decoded(host).unwrap_or(SESSION)
}

/// The words for an effective view.
pub fn label(view: &str) -> &'static str {
    match decoded(view) {
        Some(CONVERSATION) => "Conversation",
        _ => "Session",
    }
}

/// The words for a menu value ("default" reads Follow Settings).
pub fn menu_label(value: &str) -> &'static str {
    MENU.iter().find(|(v, _)| *v == value).map_or("Follow Settings", |(_, label)| label)
}

/// The quiet line after a write the Host refused.
pub fn refused(host: &str) -> String {
    if host.trim().is_empty() {
        "Not saved on the server. This device keeps the view.".to_owned()
    } else {
        format!("Not saved on {}. This device keeps the view.", host.trim())
    }
}

/// A chat's own choice on this device, by its chat key.
pub fn own(settings: &crate::settings::Settings, key: &str) -> Option<&'static str> {
    if key.is_empty() {
        return None;
    }
    settings.get(OWN_KEY)?.get(key)?.as_str().and_then(decoded)
}

/// Sets a chat's own choice, or (None) lets it follow Settings.
pub fn set_own(settings: &mut crate::settings::Settings, key: &str, view: Option<&str>) {
    if key.is_empty() {
        return;
    }
    let mut map = settings.get(OWN_KEY).and_then(Value::as_object).cloned().unwrap_or_default();
    match view.and_then(decoded) {
        Some(view) => {
            map.insert(key.to_owned(), Value::String(view.to_owned()));
        }
        None => {
            map.remove(key);
        }
    }
    if map.is_empty() {
        settings.remove(OWN_KEY);
    } else {
        settings.set(OWN_KEY, Value::Object(map));
    }
}

/// Whether a row belongs to the conversation view. A row the Host did not
/// tag reads as Session (a row it meant to hide is never shown), except a
/// message of one's own not yet read back, which is always conversation.
pub fn in_conversation(row: &Message) -> bool {
    if row.view.is_empty() {
        return row.role == "user" && (row.pending || row.delivery_failed);
    }
    row.view == CONVERSATION
}

/// Whether the held rows carry the Host's tags at all: a chat drawn from
/// a cache written before tags existed is drawn as Session until the
/// Host's read brings them, so it never opens as an empty chat.
pub fn tagged(rows: &[Message]) -> bool {
    rows.iter().any(|r| !r.view.is_empty())
}

pub fn work_key(trace: &str) -> String {
    format!("{WORK_PREFIX}{trace}")
}

/// A finished turn's Show work, on its anchor row.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Work {
    /// `live:work:<trace>`: what the reader toggles.
    pub key: String,
    /// How many rows the view leaves out (the anchor's `session_rows`).
    pub rows: i64,
    pub open: bool,
}

#[derive(Debug, Clone, Default)]
pub struct Projection {
    /// Rows the conversation view leaves out now.
    pub hidden_rows: HashSet<String>,
    /// Turns whose work the reader opened (their session rows show).
    pub open_traces: HashSet<String>,
    /// Anchor row id → its Show work.
    pub anchors: HashMap<String, Work>,
}

/// The conversation view of `rows`: every session row leaves, except a
/// row of a turn the reader opened with Show work (`expanded` holds its
/// key), which shows in its place as the session view draws it.
pub fn project(rows: &[Message], expanded: &HashSet<String>) -> Projection {
    let mut projection = Projection::default();
    for row in rows {
        if let Some(count) = row.session_rows.filter(|n| *n > 0)
            && !row.trace_id.is_empty()
            && in_conversation(row)
        {
            let key = work_key(&row.trace_id);
            let open = expanded.contains(&key);
            if open {
                projection.open_traces.insert(row.trace_id.clone());
            }
            projection.anchors.insert(row.id.clone(), Work { key, rows: count, open });
        }
    }
    for row in rows {
        if in_conversation(row) || (!row.trace_id.is_empty() && projection.open_traces.contains(&row.trace_id)) {
            continue;
        }
        projection.hidden_rows.insert(row.id.clone());
    }
    projection
}

/// The turn a history fold or a live fold stands for, from its key
/// (`live:fold:<turn>`, `live:handoff:<turn>`).
pub fn fold_turn(key: &str) -> &str {
    key.strip_prefix("live:fold:").or_else(|| key.strip_prefix("live:handoff:")).unwrap_or_default()
}

/// Whether an entry of a finished turn stays in the conversation view:
/// its reply (a message not of commentary), or a durable row that is.
pub fn finished_entry_stays(entry: &Entry, conversation_row: impl Fn(&str) -> bool) -> bool {
    if !entry.row.is_empty() {
        return conversation_row(&entry.row);
    }
    entry.kind == Kind::Message && entry.phase != "commentary"
}

/// The Running box's words, as the live status says: a tool's
/// "Running swift build" as "Running swift build…", another step as
/// "Thinking…", nothing known as "Running…".
pub fn running(status: &str) -> String {
    let mut status = status.trim().to_owned();
    while status.ends_with('…') || status.ends_with('.') {
        status.pop();
    }
    let status = status.trim_end();
    if status.is_empty() { "Running…".to_owned() } else { format!("{status}…") }
}

/// What the live activity (`GET /live`'s `activity`) says runs now, in
/// plain words: the Host's headline, else the tool's label, else the step.
pub fn status_words(activity: &Object) -> String {
    let text = |o: &Object, key: &str| o.get(key).and_then(Value::as_str).unwrap_or_default().trim().to_owned();
    let headline = text(activity, "headline");
    let state = text(activity, "state");
    let tool = activity.get("tool").and_then(Value::as_object);
    match state.as_str() {
        "tool" => {
            if !headline.is_empty() {
                return headline;
            }
            let label = tool.map(|t| text(t, "label")).unwrap_or_default();
            if !label.is_empty() {
                return format!("Running {}", label.lines().next().unwrap_or_default());
            }
            tool_words(&tool.map(|t| text(t, "name")).unwrap_or_default()).to_owned()
        }
        "waiting" | "limited" | "background" if !headline.is_empty() => headline,
        "thinking" if !headline.is_empty() => headline,
        _ => step_words(&state, "").unwrap_or_default().to_owned(),
    }
}

/// The kind of a step in plain words; None for none.
pub fn step_words(state: &str, tool: &str) -> Option<&'static str> {
    match state {
        "thinking" => Some("Thinking"),
        "responding" => Some("Writing"),
        "compacting" => Some("Compacting"),
        "waiting" => Some("Waiting"),
        "limited" => Some("Waiting for the usage limit"),
        "tool" => Some(tool_words(tool)),
        _ => None,
    }
}

/// A tool's kind in plain words, never its text.
pub fn tool_words(name: &str) -> &'static str {
    let name = name.trim().to_ascii_lowercase();
    match name.as_str() {
        "bash" | "shell" | "exec_command" => "Running a command",
        "read" | "glob" | "grep" | "ls" | "view" => "Reading files",
        "edit" | "write" | "multiedit" | "notebookedit" | "apply_patch" => "Editing files",
        "websearch" | "webfetch" | "web_search" => "Searching the web",
        "task" | "agent" => "Working with a helper",
        _ if name.contains("clarp-admin") || name.contains("prompt") => "Messaging another agent",
        _ => "Using a tool",
    }
}

/// The agent's own turn beside a fork's (`main_turn`), while it runs:
/// "Also working with another agent · Running a command". Who it works
/// with and the kind of its step, never its text. None when it is not
/// running.
pub fn main_turn_line(main: &Object) -> Option<String> {
    let text = |o: &Object, key: &str| o.get(key).and_then(Value::as_str).unwrap_or_default().trim().to_ascii_lowercase();
    if text(main, "status") != "running" {
        return None;
    }
    let mut line = if text(main, "audience") == "agents" { "Also working with another agent" } else { "Also working" }.to_owned();
    let activity = main.get("activity").and_then(Value::as_object);
    let state = activity.map(|a| text(a, "state")).unwrap_or_default();
    let tool = activity.and_then(|a| a.get("tool")).and_then(Value::as_object).map(|t| text(t, "name")).unwrap_or_default();
    if let Some(step) = step_words(&state, &tool) {
        line.push_str(" · ");
        line.push_str(step);
    }
    Some(line)
}

/// The box's second line: its failed steps ("1 step failed") and the
/// fork's main-turn line, as one quiet line; empty with neither.
pub fn second_line(failed: usize, other: Option<&str>) -> String {
    let failure = match failed {
        0 => None,
        1 => Some("1 step failed".to_owned()),
        n => Some(format!("{n} steps failed")),
    };
    [failure, other.map(str::to_owned)].into_iter().flatten().filter(|p| !p.is_empty()).collect::<Vec<_>>().join(" · ")
}

/// The running turn's entries as the conversation view draws them: one
/// Running box (`title`, the turn's clock in `meta`, `other` the fork's
/// main-turn line). It stands in the place of the turn's work row (live
/// items folded as Grouped folds them) under its key, so it opens in place
/// as that row does and then lists the turn's steps under it; failed steps
/// go inside it, counted on its second line. Nothing else of the running
/// turn shows: its text in between is not the reply.
pub fn running_box(entries: &[Entry], turn_id: &str, title: &str, meta: &str, other: Option<&str>) -> Vec<Entry> {
    let fold_key = format!("live:fold:{turn_id}");
    let fold = entries.iter().find(|e| e.kind == Kind::Fold && e.key == fold_key);
    let work: Vec<&Entry> = entries.iter().filter(|e| e.kind != Kind::Message && e.kind != Kind::Fold).collect();
    let failed = work.iter().filter(|e| matches!(e.status.as_str(), "failed" | "interrupted")).count();
    let expandable = fold.is_some_and(|f| f.expandable);
    let open = expandable && fold.is_some_and(|f| f.expanded);
    let boxed = Entry {
        key: fold_key,
        kind: Kind::Fold,
        items: fold.map(|f| f.items.clone()).unwrap_or_default(),
        status: "running".to_owned(),
        title: title.to_owned(),
        meta: meta.to_owned(),
        secondary: second_line(failed, other),
        expandable,
        expanded: open,
        rev: fold.map_or(0, |f| f.rev),
        started_at_ms: entries.iter().filter_map(|e| e.started_at_ms).min(),
        ..Entry::default()
    };
    let mut out = vec![boxed];
    if open {
        out.extend(work.into_iter().cloned());
    }
    out
}

/// The newest prompt's turn, while the agent works and no live turn says
/// what runs: its trace, unless its anchor says it ended (None then).
/// Empty when that prompt has no trace.
pub fn busy_turn(rows: &[Message]) -> Option<String> {
    let prompt = rows.iter().rev().find(|r| r.role == "user")?;
    let trace = prompt.trace_id.clone();
    if !trace.is_empty() && rows.iter().any(|r| r.trace_id == trace && r.session_rows.is_some()) {
        return None;
    }
    Some(trace)
}
