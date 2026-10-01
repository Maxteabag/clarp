//! How a transcript is shown: tool-only runs grouped, repeated explanations
//! folded, and provisional streaming text hidden in show-when-ready mode.
//! Port of the C++ client's `ConversationPresentationModel` (see
//! the C++ client's `activity-grouping.md`).
//!
//! [`present`] recomputes the visible rows from the conversation's rows; the
//! Qt adapter applies [`diff`] so unchanged rows keep their delegates and a
//! streamed token touches one row.

use std::collections::HashSet;

use chrono::{DateTime, Utc};
use serde_json::{Value, json};

use crate::conversation::{self, presented_display_cells};
use crate::json::Object;
use crate::list_ops::ListOp;
use crate::protocol::{Message, parse_iso_date};

/// Chats → Tool activity: 0 grouped, 1 always visible, 2 group old.
pub const GROUPED: i32 = 0;
pub const ALWAYS_VISIBLE: i32 = 1;
pub const GROUP_OLD: i32 = 2;

#[derive(Debug, Clone)]
pub struct Settings {
    pub show_when_ready: bool,
    pub activity_mode: i32,
    pub visit_started: DateTime<Utc>,
    pub expanded: HashSet<String>,
    /// Rows seen live during this visit stay expanded after completing.
    pub observed_live: HashSet<String>,
}

impl Default for Settings {
    fn default() -> Self {
        Self {
            show_when_ready: false,
            activity_mode: ALWAYS_VISIBLE,
            visit_started: Utc::now(),
            expanded: HashSet::new(),
            observed_live: HashSet::new(),
        }
    }
}

impl Settings {
    /// A fresh visit clears expansion and what was observed live.
    pub fn begin_visit(&mut self) {
        self.visit_started = Utc::now();
        self.expanded.clear();
        self.observed_live.clear();
    }

    pub fn toggle_group(&mut self, id: &str) {
        if !self.expanded.remove(id) {
            self.expanded.insert(id.to_owned());
        }
    }
}

/// A cache-only explanation lookup: grouping must never create demand.
pub type Lookup<'a> = &'a dyn Fn(&Object) -> String;

#[derive(Debug, Clone, PartialEq)]
pub struct PresentedRow {
    pub source_row: usize,
    /// The source row; roles not overridden below read from it.
    pub message: Message,
    pub body: String,
    pub activity: bool,
    pub tools: Vec<Value>,
    pub display_cells: Vec<Value>,
    pub activity_count: i32,
    pub group_ids: Vec<String>,
    pub group_label: String,
    pub group_expanded: bool,
    pub activity_inline: bool,
    pub activity_label: String,
    pub explanation_repeat: i32,
}

/// The Host stamps ordinary rows "user" and uses a dispatcher name for rows
/// sent on someone else's behalf; only unstamped fixture rows are empty.
fn own_turn(message: &Message) -> bool {
    message.origin.is_empty() || message.origin == "user"
}

fn lifecycle_placeholder(message: &Message) -> bool {
    message.tool_name == "thinking" || message.tool_name == "compacting"
}

struct Pass<'a> {
    rows: &'a [Message],
    settings: &'a Settings,
    inline: Vec<bool>,
    grouped: Vec<bool>,
}

impl<'a> Pass<'a> {
    fn new(rows: &'a [Message], settings: &'a mut Settings) -> Self {
        // Observing is a side effect in the C++ model's lazy role reads; here
        // it runs once per pass, before anything depends on it.
        let inline: Vec<bool> = rows
            .iter()
            .map(|message| match settings.activity_mode {
                ALWAYS_VISIBLE => true,
                GROUP_OLD => {
                    let recent = parse_iso_date(&message.timestamp).is_some_and(|t| t >= settings.visit_started);
                    if recent || message.kind == "live" || message.activity_status == "running" {
                        settings.observed_live.insert(message.id.clone());
                    }
                    settings.observed_live.contains(&message.id)
                }
                _ => false,
            })
            .collect();
        let settings: &'a Settings = settings;
        let grouped = rows
            .iter()
            .zip(&inline)
            .map(|(message, &inline)| {
                if inline {
                    return false;
                }
                if message.activity {
                    return !lifecycle_placeholder(message);
                }
                if !own_turn(message) || message.automated {
                    return false;
                }
                message.role == "assistant"
                    && message.display_text.is_empty()
                    && (message.activity_count > 0 || !message.tools.is_empty() || !message.display_cells.is_empty())
            })
            .collect();
        Self { rows, settings, inline, grouped }
    }

    fn grouped(&self, row: usize) -> bool {
        self.grouped.get(row).copied().unwrap_or(false)
    }

    /// A grouped row that continues the run above it is folded into it.
    fn continues_group(&self, row: usize) -> bool {
        row > 0 && self.grouped(row) && self.grouped(row - 1)
    }

    fn group_rows(&self, row: usize) -> std::ops::Range<usize> {
        let mut end = row;
        while self.grouped(end) {
            end += 1;
        }
        row..end
    }

    fn activity_label(&self, rows: std::ops::Range<usize>) -> String {
        if rows.is_empty() {
            return String::new();
        }
        let count: i32 = self.rows[rows.clone()]
            .iter()
            .map(|m| i32::from(m.activity).max(m.activity_count).max(m.tools.len() as i32).max(m.display_cells.len() as i32))
            .sum();
        if count == 0 {
            return String::new();
        }
        let mut label = format!("{count} tool call{}", if count == 1 { "" } else { "s" });
        let start = parse_iso_date(&self.rows[rows.start].timestamp);
        let mut end = parse_iso_date(&self.rows[rows.end - 1].timestamp);
        // The following assistant message closes the interval. Never count
        // time waiting for the next user or a teammate as tool execution.
        if let Some(next) = self.rows.get(rows.end)
            && next.role == "assistant" && own_turn(next) && !next.automated && next.kind != "live" && !next.activity
                && let Some(boundary) = parse_iso_date(&next.timestamp)
                    && end.is_none_or(|e| boundary > e) {
                        end = Some(boundary);
                    }
        if let (Some(start), Some(end)) = (start, end) {
            let seconds = (end - start).num_seconds();
            if seconds > 0 {
                let mut elapsed = String::new();
                if seconds >= 3600 {
                    elapsed += &format!("{}h ", seconds / 3600);
                }
                if seconds >= 60 {
                    elapsed += &format!("{}m ", seconds / 60 % 60);
                }
                elapsed += &format!("{}s", seconds % 60);
                label += &format!(" · {elapsed} elapsed");
            }
        }
        label
    }

    /// Everything the view reads for a source row, before explanation runs.
    fn row(&self, index: usize) -> PresentedRow {
        let message = &self.rows[index];
        let group = if self.grouped(index) { self.group_rows(index) } else { index..index };
        let mut row = PresentedRow {
            source_row: index,
            message: message.clone(),
            body: message.display_text.clone(),
            activity: message.activity,
            tools: message.tools.clone(),
            display_cells: presented_display_cells(message),
            activity_count: message.activity_count,
            group_ids: Vec::new(),
            group_label: String::new(),
            group_expanded: false,
            activity_inline: self.inline[index],
            activity_label: self.activity_label(if group.is_empty() { index..index + 1 } else { group.clone() }),
            explanation_repeat: 1,
        };
        if !group.is_empty() {
            let expanded = self.settings.expanded.contains(&message.id);
            let members = &self.rows[group.clone()];
            row.group_expanded = expanded;
            row.group_ids = members.iter().filter(|m| m.tool_details_available).map(|m| m.id.clone()).collect();
            row.activity_count = members
                .iter()
                .map(|m| 1.max(m.activity_count).max(m.tools.len() as i32).max(m.display_cells.len() as i32))
                .sum();
            row.group_label = self.activity_label(group);
            row.body = String::new();
            row.activity = false;
            row.tools = Vec::new();
            row.display_cells = Vec::new();
            if expanded {
                for member in members {
                    row.tools.extend(member.tools.iter().cloned());
                    if member.activity {
                        row.tools.push(json!({"name": member.tool_name, "summary": member.display_text}));
                    }
                    row.display_cells.extend(presented_display_cells(member));
                }
            }
            return row;
        }
        if self.settings.show_when_ready && !message.activity && message.role == "assistant" && message.kind == "live" {
            row.body = String::new();
        }
        row
    }

    fn accepts(&self, index: usize, repeated: &HashSet<usize>) -> bool {
        if repeated.contains(&index) || self.continues_group(index) {
            return false;
        }
        if !self.settings.show_when_ready {
            return true;
        }
        let message = &self.rows[index];
        if message.activity {
            // The typing indicator already represents these placeholders.
            return !lifecycle_placeholder(message);
        }
        if message.role != "assistant" {
            return true;
        }
        let has_tools = !message.tools.is_empty() || !message.display_cells.is_empty() || message.activity_count > 0;
        has_tools || (message.kind != "live" && !message.display_text.is_empty())
    }
}

#[derive(Clone, Copy)]
enum Slot {
    Repeat(usize),
    Tools(usize, usize),
    Cells(usize, usize),
}

/// Consecutive ready explanations with identical text and status share the
/// first displayed row, which gains `_explanationRepeat: N`; later rows get
/// 0 and a tool-only transcript row that is entirely repeats is hidden.
fn explanation_runs(pass: &Pass, rows: &mut [PresentedRow], lookup: Lookup) -> HashSet<usize> {
    let mut repeated = HashSet::new();
    let mut first: Option<Slot> = None;
    let mut previous = String::new();
    let mut previous_status = String::new();
    let mut count = 0;
    fn annotate(rows: &mut [PresentedRow], slot: Slot, repetitions: i32) {
        let set = |value: &mut Value| {
            if let Some(map) = value.as_object_mut() {
                map.insert("_explanationRepeat".into(), json!(repetitions));
            }
        };
        match slot {
            Slot::Repeat(row) => rows[row].explanation_repeat = repetitions,
            Slot::Tools(row, offset) => set(&mut rows[row].tools[offset]),
            Slot::Cells(row, offset) => set(&mut rows[row].display_cells[offset]),
        }
    }
    for index in 0..rows.len() {
        if pass.continues_group(index) {
            continue;
        }
        let grouped = pass.grouped(index);
        let activity = rows[index].activity;
        let prose = !rows[index].body.is_empty() && !activity;
        let collapsed = grouped && !rows[index].group_expanded;
        if prose || grouped {
            previous.clear();
        }
        if collapsed {
            continue;
        }
        let mut entries: Vec<(Slot, Object)> = Vec::new();
        if activity {
            let message = &rows[index].message;
            entries.push((
                Slot::Repeat(index),
                json!({"name": message.tool_name, "summary": message.display_text, "status": message.activity_status})
                    .as_object()
                    .cloned()
                    .unwrap_or_default(),
            ));
        } else {
            let has_cells = !rows[index].display_cells.is_empty();
            for (offset, value) in rows[index].display_cells.iter().enumerate() {
                entries.push((Slot::Cells(index, offset), value.as_object().cloned().unwrap_or_default()));
            }
            for (offset, value) in rows[index].tools.iter().enumerate() {
                let value = value.as_object().cloned().unwrap_or_default();
                let name = value.get("name").and_then(Value::as_str).unwrap_or_default();
                // With display cells, only edits keep a separate tool entry.
                if !grouped && has_cells && !matches!(name, "Edit" | "MultiEdit" | "Write") {
                    continue;
                }
                entries.push((Slot::Tools(index, offset), value));
            }
        }
        let any = !entries.is_empty();
        let mut retained = false;
        for (slot, value) in entries {
            let text = lookup(&value);
            let status = match value.get("status") {
                None => "recorded".to_owned(),
                Some(status) => status.as_str().unwrap_or_default().to_owned(),
            };
            if !text.is_empty() && text == previous && status == previous_status {
                count += 1;
                if let Some(first) = first {
                    annotate(rows, first, count);
                }
                annotate(rows, slot, 0);
                continue;
            }
            previous = text;
            previous_status = status;
            first = Some(slot);
            count = 1;
            retained = true;
        }
        if !any {
            previous.clear();
        }
        if any && !retained && !prose && !grouped {
            repeated.insert(index);
        }
        if prose || grouped {
            previous.clear();
        }
    }
    repeated
}

/// One presentation pass: the visible rows plus what `index_of_message`
/// needs to map a source row to the row that stands for it.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Presentation {
    pub rows: Vec<PresentedRow>,
    repeated: HashSet<usize>,
    grouped: Vec<bool>,
}

/// The rows a view shows, in order.
pub fn present(messages: &[Message], settings: &mut Settings, lookup: Option<Lookup>) -> Presentation {
    let pass = Pass::new(messages, settings);
    let mut rows: Vec<PresentedRow> = (0..messages.len()).map(|index| pass.row(index)).collect();
    let repeated = match lookup {
        Some(lookup) => explanation_runs(&pass, &mut rows, lookup),
        None => HashSet::new(),
    };
    let rows = rows.into_iter().enumerate().filter(|(index, _)| pass.accepts(*index, &repeated)).map(|(_, row)| row).collect();
    Presentation { rows, repeated, grouped: pass.grouped }
}

impl Presentation {
    /// The visible row standing for message `id`: a folded repeat maps to the
    /// row carrying its run, a grouped row to its group; a hidden row is None.
    pub fn index_of_message(&self, messages: &[Message], id: &str) -> Option<usize> {
        let mut row = messages.iter().position(|m| m.id == id)?;
        while row > 0 && self.repeated.contains(&row) {
            row -= 1;
        }
        let grouped = |r: usize| self.grouped.get(r).copied().unwrap_or(false);
        if grouped(row) {
            while row > 0 && grouped(row - 1) {
                row -= 1;
            }
        }
        self.rows.iter().position(|r| r.source_row == row)
    }
}

/// The first visible day heading, which the view uses to suppress a lone
/// "Today" heading at the top.
pub fn leading_day_label(presented: &[PresentedRow], day_label: impl Fn(&Message) -> String) -> String {
    presented.iter().map(|row| day_label(&row.message)).find(|label| !label.is_empty()).unwrap_or_default()
}

/// Presentation roles: every conversation role plus the group roles.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Role {
    Base(conversation::Role),
    GroupIds,
    GroupLabel,
    GroupExpanded,
    ActivityInline,
    ActivityLabel,
    ExplanationRepeat,
}

pub type Op = ListOp<PresentedRow, Role, ()>;

fn changed_roles(old: &PresentedRow, new: &PresentedRow) -> Vec<Role> {
    use conversation::Role as B;
    let (a, b) = (&old.message, &new.message);
    let mut roles = Vec::new();
    let mut check = |changed: bool, role: Role| {
        if changed {
            roles.push(role);
        }
    };
    check(a.id != b.id, Role::Base(B::MessageId));
    check(a.role != b.role, Role::Base(B::Author));
    check(old.body != new.body, Role::Base(B::Body));
    check(a.timestamp != b.timestamp, Role::Base(B::Timestamp));
    check(a.timestamp != b.timestamp, Role::Base(B::DayLabel));
    check(a.revision != b.revision, Role::Base(B::Revision));
    check(a.kind != b.kind, Role::Base(B::Kind));
    check(a.tool_name != b.tool_name, Role::Base(B::ToolName));
    check(a.origin != b.origin, Role::Base(B::Origin));
    check(a.sender_name != b.sender_name, Role::Base(B::SenderName));
    check(a.pending != b.pending, Role::Base(B::Pending));
    check(a.delivery_failed != b.delivery_failed, Role::Base(B::DeliveryFailed));
    check(old.activity != new.activity, Role::Base(B::Activity));
    check(old.tools != new.tools, Role::Base(B::Tools));
    check(old.display_cells != new.display_cells, Role::Base(B::DisplayCells));
    check(a.activity_status != b.activity_status, Role::Base(B::ActivityStatus));
    check(a.automated != b.automated, Role::Base(B::Automated));
    check(a.category != b.category, Role::Base(B::Category));
    check(a.tool_details_available != b.tool_details_available, Role::Base(B::ToolDetailsAvailable));
    check(old.activity_count != new.activity_count, Role::Base(B::ActivityCount));
    check(a.sender_agent_id != b.sender_agent_id, Role::Base(B::SenderAgentId));
    check(a.sender_session != b.sender_session, Role::Base(B::SenderSession));
    check(a.reply_to_agent_id != b.reply_to_agent_id, Role::Base(B::ReplyToAgentId));
    check(a.reply_to_name != b.reply_to_name, Role::Base(B::ReplyToName));
    check(a.reply_to_session != b.reply_to_session, Role::Base(B::ReplyToSession));
    check(a.delivery != b.delivery, Role::Base(B::Delivery));
    check(old.group_ids != new.group_ids, Role::GroupIds);
    check(old.group_label != new.group_label, Role::GroupLabel);
    check(old.group_expanded != new.group_expanded, Role::GroupExpanded);
    check(old.activity_inline != new.activity_inline, Role::ActivityInline);
    check(old.activity_label != new.activity_label, Role::ActivityLabel);
    check(old.explanation_repeat != new.explanation_repeat, Role::ExplanationRepeat);
    roles
}

/// Minimal ops turning `old` into `new`, matching rows by message id: rows
/// only in `old` are removed, rows only in `new` inserted, and matched rows
/// updated with exactly the roles that changed. Never a reset.
pub fn diff(old: &[PresentedRow], new: &[PresentedRow]) -> Vec<Op> {
    let wanted: HashSet<&str> = new.iter().map(|r| r.message.id.as_str()).collect();
    let mut mirror: Vec<PresentedRow> = old.to_vec();
    let mut ops = Vec::new();
    for index in (0..mirror.len()).rev() {
        if !wanted.contains(mirror[index].message.id.as_str()) {
            ops.push(Op::Remove { at: index, count: 1 });
            mirror.remove(index);
        }
    }
    for (index, row) in new.iter().enumerate() {
        let id = row.message.id.as_str();
        match mirror.iter().skip(index).position(|m| m.message.id == id).map(|p| p + index) {
            Some(found) if found == index => {}
            Some(found) => {
                ops.push(Op::Move { from: found, to: index });
                let moved = mirror.remove(found);
                mirror.insert(index, moved);
            }
            None => {
                ops.push(Op::Insert { at: index, rows: vec![row.clone()] });
                mirror.insert(index, row.clone());
                continue;
            }
        }
        let roles = changed_roles(&mirror[index], row);
        if mirror[index] != *row {
            ops.push(Op::Update { first: index, items: vec![row.clone()], roles });
            mirror[index] = row.clone();
        }
    }
    ops
}
