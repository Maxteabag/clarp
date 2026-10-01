//! Transcript state for one agent. Port of the C++ client's `ConversationModel`.
//!
//! Every mutation is recorded as an ordered [`Op`]. The Qt list model keeps a
//! mirror of the rows and replays each op between the matching
//! begin/end notifications, so views never observe a half-applied change and
//! the model never re-enters this state while it is borrowed.

use std::collections::HashMap;
use std::sync::LazyLock;

use chrono::{SecondsFormat, Utc};
use fancy_regex::Regex;
use serde_json::{Value, json};

use crate::json::{self, Object};
pub use crate::list_ops::{ListOp, replay};
use crate::protocol::{Message, describe_subagent_cell};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Role {
    MessageId,
    Author,
    Body,
    Timestamp,
    DayLabel,
    Revision,
    Kind,
    ToolName,
    Origin,
    SenderName,
    Pending,
    DeliveryFailed,
    Activity,
    Tools,
    DisplayCells,
    ActivityStatus,
    Automated,
    Category,
    ToolDetailsAvailable,
    ActivityCount,
    SenderAgentId,
    SenderSession,
    ReplyToAgentId,
    ReplyToName,
    ReplyToSession,
    Delivery,
}

#[derive(Debug, Clone, PartialEq)]
pub enum Signal {
    SessionChanged,
    ConversationIdChanged,
    LatestRevisionChanged,
    HasMoreChanged,
    LoadingChanged,
    ErrorChanged,
    VoiceErrorChanged,
    CountChanged,
    RowsAppended { from_current_user: bool },
    RowsPrepended,
    ReplacementRequired,
    DeliveryConfirmed(String),
    /// Bracket one applied log response; whole-transcript listeners defer to
    /// BatchFinished instead of repeating work per row.
    BatchStarted,
    BatchFinished,
}

pub type Op = ListOp<Message, Role, Signal>;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LoadKind {
    Tail,
    Delta,
    Older,
    Replace,
}

const MAX_ACTIVITY_ROWS: usize = 80;

#[derive(Debug, Default)]
pub struct Conversation {
    session: String,
    conversation_id: String,
    latest_revision: i64,
    has_more: bool,
    loading: bool,
    error: String,
    voice_error: String,
    messages: Vec<Message>,
    by_id: HashMap<String, usize>,
    activity_counter: u64,
    ops: Vec<Op>,
}

/// QJsonValue::toString(fallback): the value when it is a string.
fn string_or(object: &Object, key: &str, fallback: &str) -> String {
    object.get(key).and_then(Value::as_str).unwrap_or(fallback).to_owned()
}

fn now_iso() -> String {
    Utc::now().to_rfc3339_opts(SecondsFormat::Millis, true)
}

impl Conversation {
    pub fn new() -> Self {
        Self::default()
    }

    // ---- read side -------------------------------------------------------

    pub fn session(&self) -> &str {
        &self.session
    }
    pub fn conversation_id(&self) -> &str {
        &self.conversation_id
    }
    pub fn latest_revision(&self) -> i64 {
        self.latest_revision
    }
    pub fn has_more(&self) -> bool {
        self.has_more
    }
    pub fn loading(&self) -> bool {
        self.loading
    }
    pub fn error(&self) -> &str {
        &self.error
    }
    pub fn voice_error(&self) -> &str {
        &self.voice_error
    }
    pub fn rows(&self) -> &[Message] {
        &self.messages
    }
    pub fn len(&self) -> usize {
        self.messages.len()
    }
    pub fn is_empty(&self) -> bool {
        self.messages.is_empty()
    }
    pub fn index_of(&self, id: &str) -> Option<usize> {
        self.by_id.get(id).copied()
    }

    /// Ops recorded since the last call, in the order they must be applied.
    pub fn take_ops(&mut self) -> Vec<Op> {
        std::mem::take(&mut self.ops)
    }

    fn emit(&mut self, signal: Signal) {
        self.ops.push(Op::Signal(signal));
    }

    fn rebuild_index(&mut self) {
        self.by_id = self
            .messages
            .iter()
            .enumerate()
            .filter(|(_, m)| !m.id.is_empty())
            .map(|(row, m)| (m.id.clone(), row))
            .collect();
    }

    fn insert(&mut self, at: usize, rows: Vec<Message>) {
        self.ops.push(Op::Insert { at, rows: rows.clone() });
        self.messages.splice(at..at, rows);
        self.rebuild_index();
    }

    fn remove(&mut self, at: usize) {
        self.ops.push(Op::Remove { at, count: 1 });
        self.messages.remove(at);
        self.rebuild_index();
    }

    fn update(&mut self, row: usize, message: Message, roles: Vec<Role>) {
        self.ops.push(Op::Update { first: row, items: vec![message.clone()], roles });
        self.messages[row] = message;
    }

    fn reset(&mut self, rows: Vec<Message>) {
        self.ops.push(Op::Reset(rows.clone()));
        self.messages = rows;
        self.rebuild_index();
    }

    // ---- write side ------------------------------------------------------

    pub fn open_session(&mut self, session: &str) {
        if self.session == session {
            return;
        }
        self.session = session.to_owned();
        self.conversation_id.clear();
        self.latest_revision = 0;
        self.has_more = false;
        self.error.clear();
        self.set_voice_error("");
        self.reset(Vec::new());
        for signal in [
            Signal::SessionChanged,
            Signal::ConversationIdChanged,
            Signal::LatestRevisionChanged,
            Signal::HasMoreChanged,
            Signal::ErrorChanged,
            Signal::CountChanged,
        ] {
            self.emit(signal);
        }
    }

    pub fn apply_log(&mut self, response: &Object, kind: LoadKind) {
        let next_id = json::string(response, "conversation_id");
        if kind == LoadKind::Delta && json::boolean(response, "replace_required") {
            self.emit(Signal::ReplacementRequired);
            return;
        }
        if !matches!(kind, LoadKind::Tail | LoadKind::Replace)
            && !self.conversation_id.is_empty()
            && !next_id.is_empty()
            && next_id != self.conversation_id
        {
            self.emit(Signal::ReplacementRequired);
            return;
        }

        self.emit(Signal::BatchStarted);
        let turns = json::array(response, "turns");
        let replaces = kind == LoadKind::Replace
            || (kind == LoadKind::Tail
                && (self.conversation_id.is_empty() || next_id != self.conversation_id));
        if replaces {
            let rows = turns.iter().filter_map(Value::as_object).map(Message::from_json).collect();
            self.replace_rows(rows);
        } else if kind == LoadKind::Older {
            self.prepend_rows(&turns);
        } else {
            self.merge_rows(&turns);
        }

        if self.conversation_id != next_id {
            self.conversation_id = next_id;
            self.emit(Signal::ConversationIdChanged);
        }
        let response_revision = json::integer(response, "latest_revision");
        let revision =
            if replaces { response_revision } else { self.latest_revision.max(response_revision) };
        if self.latest_revision != revision {
            self.latest_revision = revision;
            self.emit(Signal::LatestRevisionChanged);
        }
        // `has_more` says whether older history exists only on a full or
        // older page. On a delta the Host uses it for a backlog of newer rows
        // beyond the limit, so a delta never changes it (the C++ client let a
        // delta that arrived just after opening a long chat hide "load older").
        let more = if kind == LoadKind::Delta { self.has_more } else { json::boolean(response, "has_more") };
        if self.has_more != more {
            self.has_more = more;
            self.emit(Signal::HasMoreChanged);
        }
        self.set_loading(false);
        self.set_error("");
        self.emit(Signal::BatchFinished);
    }

    /// Durable rows only: activity, pending and live rows are never cached.
    pub fn cache_snapshot(&self) -> Object {
        let turns: Vec<Value> = self
            .messages
            .iter()
            .filter(|m| !m.activity && !m.pending && m.kind != "live")
            .map(message_to_json)
            .collect();
        json!({
            "conversation_id": self.conversation_id,
            "turns": turns,
            "latest_revision": self.latest_revision,
            "has_more": self.has_more,
        })
        .as_object()
        .cloned()
        .unwrap_or_default()
    }

    pub fn restore_cache_snapshot(&mut self, snapshot: &Object) -> bool {
        if snapshot.is_empty() || !snapshot.get("turns").is_some_and(Value::is_array) {
            return false;
        }
        self.apply_log(snapshot, LoadKind::Tail);
        true
    }

    pub fn add_optimistic(&mut self, client_message_id: &str, text: &str) {
        let message = Message {
            id: format!("u-{client_message_id}"),
            role: "user".into(),
            text: text.to_owned(),
            display_text: text.to_owned(),
            timestamp: now_iso(),
            pending: true,
            ..Message::default()
        };
        let at = self.messages.len();
        self.insert(at, vec![message]);
        self.emit(Signal::CountChanged);
        self.emit(Signal::RowsAppended { from_current_user: true });
    }

    pub fn mark_delivery_failed(&mut self, client_message_id: &str) {
        let Some(row) = self.index_of(&format!("u-{client_message_id}")) else { return };
        if !self.messages[row].pending {
            return;
        }
        let mut message = self.messages[row].clone();
        message.delivery_failed = true;
        message.pending = false;
        self.update(row, message, vec![Role::Pending, Role::DeliveryFailed]);
    }

    /// Remove a failed optimistic row and hand back its text for a resend.
    pub fn take_failed_message_for_retry(&mut self, message_id: &str) -> Option<String> {
        let row = self.index_of(message_id).filter(|&row| self.messages[row].delivery_failed)?;
        let text = self.messages[row].text.clone();
        self.remove(row);
        self.emit(Signal::CountChanged);
        Some(text)
    }

    pub fn apply_activity_event(&mut self, event: &Object) {
        let field = |primary: &str, fallback: &str| string_or(event, primary, &json::string(event, fallback));
        let action = field("activity_action", "action");
        let tool = field("activity_tool", "tool");
        let file_path = field("activity_file_path", "file_path");
        let phase = field("activity_phase", "phase");
        let kind = field("activity_kind", "kind");
        // Session creation is lifecycle metadata, not a tool performed for a
        // user turn. Rendering it invites the explainer to invent a task.
        if kind == "spawned" || phase == "spawned" {
            return;
        }
        let label = [&action, &phase, &tool, &kind]
            .into_iter()
            .find(|c| !c.is_empty())
            .cloned()
            .unwrap_or_default();
        let mut summary = field("activity_summary", "summary");
        if summary.is_empty() {
            summary = tool.clone();
        }
        if label.is_empty() && summary.is_empty() {
            return;
        }
        let mut status = field("activity_status", "status");
        if status.is_empty() {
            let state = json::string(event, "state");
            status = if matches!(state.as_str(), "thinking" | "tool" | "compacting") {
                "running".into()
            } else {
                "ok".into()
            };
        }
        let match_key = format!("{action}|{tool}|{file_path}");

        for row in (0..self.messages.len()).rev() {
            let existing = &self.messages[row];
            if !existing.activity || existing.activity_match_key != match_key {
                continue;
            }
            let existing_running = existing.activity_status == "running";
            if status == "running" && !existing_running {
                continue;
            }
            if status != "running" && !existing_running {
                return;
            }
            let mut message = existing.clone();
            message.activity_status = status;
            if !label.is_empty() {
                message.tool_name = label;
            }
            if !summary.is_empty() {
                message.text = summary.clone();
                message.display_text = summary;
            }
            self.update(row, message, vec![Role::Body, Role::ToolName, Role::ActivityStatus]);
            return;
        }

        self.activity_counter += 1;
        let message = Message {
            id: format!("activity:{}:{}", self.session, self.activity_counter),
            role: "activity".into(),
            activity: true,
            kind,
            tool_name: label,
            text: summary.clone(),
            display_text: summary,
            activity_status: status,
            activity_match_key: match_key,
            timestamp: now_iso(),
            ..Message::default()
        };
        let at = self.messages.len();
        self.insert(at, vec![message]);
        self.emit(Signal::CountChanged);
        self.emit(Signal::RowsAppended { from_current_user: false });

        let mut overflow = self.messages.iter().filter(|m| m.activity).count().saturating_sub(MAX_ACTIVITY_ROWS);
        let mut row = 0;
        while row < self.messages.len() && overflow > 0 {
            if !self.messages[row].activity {
                row += 1;
                continue;
            }
            self.remove(row);
            overflow -= 1;
            self.emit(Signal::CountChanged);
        }
    }

    pub fn apply_tool_details(&mut self, message_id: &str, details: &Object) -> bool {
        let Some(row) = self.index_of(message_id) else { return false };
        let mut message = self.messages[row].clone();
        message.tools = json::array(details, "tools");
        message.display_cells = json::array(details, "display_cells");
        message.tool_details_available = false;
        self.update(row, message, vec![Role::Tools, Role::DisplayCells, Role::ToolDetailsAvailable]);
        true
    }

    pub fn clear_activity(&mut self) {
        self.remove_where(|m| m.activity);
    }

    pub fn show_transient_thinking(&mut self, persona: &str) {
        let persona = if persona.is_empty() { "Agent" } else { persona };
        let event = json!({
            "activity_status": "running",
            "activity_action": "thinking",
            "activity_summary": format!("{persona} is working"),
        });
        self.apply_activity_event(event.as_object().expect("literal object"));
    }

    pub fn clear_running_activity(&mut self) {
        self.remove_where(|m| m.activity && m.activity_status == "running");
    }

    fn remove_where(&mut self, predicate: impl Fn(&Message) -> bool) {
        for row in (0..self.messages.len()).rev() {
            if predicate(&self.messages[row]) {
                self.remove(row);
                self.emit(Signal::CountChanged);
            }
        }
    }

    pub fn set_loading(&mut self, loading: bool) {
        if self.loading != loading {
            self.loading = loading;
            self.emit(Signal::LoadingChanged);
        }
    }

    pub fn set_error(&mut self, error: &str) {
        if self.error != error {
            self.error = error.to_owned();
            self.emit(Signal::ErrorChanged);
        }
    }

    pub fn set_voice_error(&mut self, error: &str) {
        if self.voice_error != error {
            self.voice_error = error.to_owned();
            self.emit(Signal::VoiceErrorChanged);
        }
    }

    // ---- merging ---------------------------------------------------------

    fn replace_rows(&mut self, mut rows: Vec<Message>) {
        // Unconfirmed sends survive an authoritative replacement, in the
        // order they were sent, unless the new rows already carry them.
        let mut optimistic: Vec<Message> = self.messages.iter().filter(|m| m.pending).cloned().collect();
        let mut confirmed = Vec::new();
        for message in &rows {
            optimistic.retain(|pending| pending.id != message.id);
            if let Some(client_id) = message.id.strip_prefix("u-") {
                confirmed.push(client_id.to_owned());
            }
        }
        for client_id in confirmed {
            self.emit(Signal::DeliveryConfirmed(client_id));
        }
        rows.extend(optimistic);
        self.reset(rows);
        self.emit(Signal::CountChanged);
        self.drop_superseded_live_turns();
    }

    fn merge_rows(&mut self, rows: &[Value]) {
        let mut inserted = false;
        let mut appended = false;
        for incoming in rows.iter().filter_map(Value::as_object).map(Message::from_json) {
            if incoming.id.is_empty() {
                continue;
            }
            if let Some(existing) = self.index_of(&incoming.id) {
                let previous = &self.messages[existing];
                if previous.revision > incoming.revision && incoming.revision != 0 {
                    continue;
                }
                let confirmed = previous.pending;
                let roles = changed_roles(previous, &incoming);
                let id = incoming.id.clone();
                self.update(existing, incoming, roles);
                if confirmed
                    && let Some(client_id) = id.strip_prefix("u-") {
                        self.emit(Signal::DeliveryConfirmed(client_id.to_owned()));
                    }
                continue;
            }
            let at = self.insertion_row(&incoming);
            appended |= at == self.messages.len();
            inserted = true;
            self.insert(at, vec![incoming]);
        }
        if inserted {
            self.emit(Signal::CountChanged);
            if appended {
                self.emit(Signal::RowsAppended { from_current_user: false });
            }
            self.clear_running_activity();
        }
        self.drop_superseded_live_turns();
    }

    /// A new durable row goes after the live row it finalises, before any
    /// optimistic/failed/live tail, and before a row with a higher revision.
    fn insertion_row(&self, incoming: &Message) -> usize {
        for (row, candidate) in self.messages.iter().enumerate() {
            if candidate.kind == "live"
                && incoming.role == "assistant"
                && incoming.kind != "live"
                && (candidate.revision == 0 || incoming.revision >= candidate.revision)
            {
                return row + 1;
            }
            if candidate.pending || candidate.delivery_failed || candidate.kind == "live" {
                return row;
            }
            if incoming.revision > 0 && candidate.revision > incoming.revision {
                return row;
            }
        }
        self.messages.len()
    }

    fn prepend_rows(&mut self, rows: &[Value]) {
        let mut older: Vec<Message> = Vec::new();
        for incoming in rows.iter().filter_map(Value::as_object).map(Message::from_json) {
            if incoming.id.is_empty() || self.by_id.contains_key(&incoming.id) {
                continue;
            }
            older.push(incoming);
        }
        if older.is_empty() {
            return;
        }
        self.insert(0, older);
        self.emit(Signal::CountChanged);
        self.emit(Signal::RowsPrepended);
    }

    fn drop_superseded_live_turns(&mut self) {
        static TAGS: LazyLock<Regex> = LazyLock::new(|| Regex::new("<[^>]+>").expect("pattern"));
        let normalized = |text: &str| {
            crate::text::simplified(&TAGS.replace_all(text, ""))
        };
        let mut row = self.messages.len();
        while row > 0 {
            row -= 1;
            let message = &self.messages[row];
            if message.role != "assistant" || message.kind != "live" {
                continue;
            }
            let live = normalized(&message.text);
            let mut covered = false;
            // Prefer the protocol correlation id. Older Hosts did not send it
            // on live rows, so the fallback is deliberately narrow: only the
            // next durable assistant row in this turn may supersede it.
            for candidate in &self.messages[row + 1..] {
                if candidate.activity {
                    continue;
                }
                if candidate.role == "user" || candidate.kind == "live" {
                    break;
                }
                if candidate.role != "assistant" {
                    continue;
                }
                let correlated = message.trace_id.is_empty()
                    || candidate.trace_id.is_empty()
                    || message.trace_id == candidate.trace_id;
                let final_text = normalized(&candidate.text);
                covered = correlated
                    && !live.is_empty()
                    && !final_text.is_empty()
                    && (final_text.starts_with(&live) || live.starts_with(&final_text));
                break;
            }
            if covered {
                self.remove(row);
                self.emit(Signal::CountChanged);
            }
        }
    }
}

fn changed_roles(previous: &Message, incoming: &Message) -> Vec<Role> {
    let mut roles = Vec::new();
    let mut check = |changed: bool, role: Role| {
        if changed {
            roles.push(role);
        }
    };
    check(previous.role != incoming.role, Role::Author);
    check(previous.display_text != incoming.display_text, Role::Body);
    check(previous.timestamp != incoming.timestamp, Role::Timestamp);
    check(previous.revision != incoming.revision, Role::Revision);
    check(previous.kind != incoming.kind, Role::Kind);
    check(previous.tool_name != incoming.tool_name, Role::ToolName);
    check(previous.origin != incoming.origin, Role::Origin);
    check(previous.sender_name != incoming.sender_name, Role::SenderName);
    check(previous.sender_agent_id != incoming.sender_agent_id, Role::SenderAgentId);
    check(previous.sender_session != incoming.sender_session, Role::SenderSession);
    check(previous.reply_to_agent_id != incoming.reply_to_agent_id, Role::ReplyToAgentId);
    check(previous.reply_to_name != incoming.reply_to_name, Role::ReplyToName);
    check(previous.reply_to_session != incoming.reply_to_session, Role::ReplyToSession);
    check(previous.delivery != incoming.delivery, Role::Delivery);
    check(previous.pending != incoming.pending, Role::Pending);
    check(previous.delivery_failed != incoming.delivery_failed, Role::DeliveryFailed);
    check(previous.activity != incoming.activity, Role::Activity);
    check(previous.tools != incoming.tools, Role::Tools);
    check(previous.display_cells != incoming.display_cells, Role::DisplayCells);
    check(previous.activity_status != incoming.activity_status, Role::ActivityStatus);
    check(previous.automated != incoming.automated, Role::Automated);
    check(previous.category != incoming.category, Role::Category);
    check(previous.tool_details_available != incoming.tool_details_available, Role::ToolDetailsAvailable);
    check(previous.activity_count != incoming.activity_count, Role::ActivityCount);
    check(previous.timestamp != incoming.timestamp, Role::DayLabel);
    roles
}

/// The cached/wire shape of a row.
pub fn message_to_json(message: &Message) -> Value {
    json!({
        "id": message.id, "role": message.role, "text": message.text,
        "timestamp": message.timestamp, "revision": message.revision, "kind": message.kind,
        "tool_name": message.tool_name, "origin": message.origin,
        "sender_name": message.sender_name, "sender_agent_id": message.sender_agent_id,
        "sender_session": message.sender_session, "reply_to_agent_id": message.reply_to_agent_id,
        "reply_to_name": message.reply_to_name, "reply_to_session": message.reply_to_session,
        "delivery": message.delivery, "trace_id": message.trace_id, "category": message.category,
        "automated": message.automated, "activity_count": message.activity_count,
        "tool_details_available": message.tool_details_available,
        "delivery_failed": message.delivery_failed, "tools": message.tools,
        "display_cells": message.display_cells,
    })
}

/// Display cells as the transcript sees them: sub-agent cells gain a derived
/// `_subagent` summary on the way out, so caches keep the Host's shape.
pub fn presented_display_cells(message: &Message) -> Vec<Value> {
    message
        .display_cells
        .iter()
        .map(|cell| {
            let Some(object) = cell.as_object() else { return cell.clone() };
            let summary = describe_subagent_cell(object);
            if summary.is_empty() {
                return cell.clone();
            }
            let mut object = object.clone();
            object.insert("_subagent".into(), Value::Object(summary));
            Value::Object(object)
        })
        .collect()
}
