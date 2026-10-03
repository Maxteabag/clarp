//! Finished turns in history (docs/live-items.md §7.6): each settled turn
//! of the `/log` rows folds behind `Worked for N · K tools` the way the
//! live fold does, with the same entries and keys, so a turn reads the same
//! while live items hold it, after the next turn takes over, and opened
//! cold. Pure: the window splices the pieces among the chat's rows.

use std::collections::{HashMap, HashSet};

use serde_json::{Value, json};

use crate::json::Object;
use crate::live::LiveView;
use crate::live_present::{self, Entry};
use crate::protocol::Message;

#[derive(Default)]
pub struct Options<'a> {
    /// The Host explains tool items (`tool_explanations.enabled`).
    pub explanations: bool,
    /// Entry keys the reader opened.
    pub expanded: HashSet<String>,
    /// The turn live items show and the rows they own: left to them.
    pub open_turn: String,
    pub held_rows: HashSet<String>,
    /// The agent works: the chat's last turn may not be over.
    pub busy: bool,
    /// A cached explanation of a `/log` tool call (never asks for one).
    pub explain: Option<&'a dyn Fn(&Object) -> String>,
}

/// One place in a folded turn: a durable row shown where it is, or an
/// entry (the fold, a tool) shown as a live row.
#[derive(Debug, Clone, PartialEq)]
pub enum Piece {
    Row(String),
    Entry(Entry),
}

#[derive(Debug, Clone, PartialEq, Default)]
pub struct Fold {
    /// `live:fold:<turn id>`, as the live fold's.
    pub key: String,
    /// The prompt that started the turn and the row after the turn.
    pub prompt: Option<String>,
    pub next: Option<String>,
    pub pieces: Vec<Piece>,
}

#[derive(Debug, Clone, Default)]
pub struct Folded {
    pub folds: Vec<Fold>,
    /// Rows the folds stand for: not shown themselves.
    pub hidden_rows: HashSet<String>,
    /// Tool and cell ids entries show: left out of the rows that stay.
    pub stripped_calls: HashSet<String>,
}

/// The settled turns of `rows` as folds. A turn is the agent's rows after
/// a prompt (a user row) up to the next; it folds once it is over (a later
/// prompt, or the agent idle) and when it used a tool. The turn live items
/// show is theirs. A turn the live view retired folds from its items, so it
/// keeps its worked time, reasoning and keys; otherwise from `/log`.
pub fn present(rows: &[Message], live: Option<&LiveView>, options: &Options) -> Folded {
    let mut folded = Folded::default();
    let prompts: Vec<usize> = (0..rows.len()).filter(|&i| rows[i].role == "user").collect();
    let starts = std::iter::once(None).chain(prompts.iter().copied().map(Some));
    for (n, prompt) in starts.enumerate() {
        let next = prompts.get(n).copied();
        let from = prompt.map_or(0, |p| p + 1);
        let to = next.unwrap_or(rows.len());
        if from >= to {
            continue;
        }
        if let Some(fold) = turn(rows, prompt, from..to, next, live, options, &mut folded) {
            folded.folds.push(fold);
        }
    }
    folded
}

fn text<'a>(value: &'a Value, key: &str) -> &'a str {
    value.get(key).and_then(Value::as_str).unwrap_or_default()
}

/// A row's tool calls and cells with ids (provider sub-agent cells are
/// left to the row).
fn calls(row: &Message) -> impl Iterator<Item = (&Value, bool)> {
    let tools = row.tools.iter().map(|t| (t, false));
    let cells = row.display_cells.iter().filter(|c| text(c, "kind") != "subagents").map(|c| (c, true));
    tools.chain(cells).filter(|(v, _)| !text(v, "id").is_empty())
}

/// A tool's category from its name, or a Codex cell's from its kind.
fn category(name: &str) -> &'static str {
    match name {
        "Bash" | "BashOutput" | "shell" | "exec_command" | "command" | "local_shell" => "exec",
        "Read" | "NotebookRead" | "read" => "read",
        "Grep" | "Glob" | "search" => "search",
        "LS" | "list" => "list",
        "Edit" | "MultiEdit" | "NotebookEdit" | "apply_patch" | "patch" | "edit" => "edit",
        "Write" | "write" => "write",
        "WebFetch" | "WebSearch" | "web_search" | "fetch" => "fetch",
        "TodoWrite" | "update_plan" | "todo" => "todo",
        "Task" | "Agent" | "agent" => "agent",
        _ => "",
    }
}

/// `/log` status as an item's: a tool still running when its turn is over
/// was stopped.
fn status(value: &Value) -> &'static str {
    match text(value, "status") {
        "error" | "failed" | "killed" => "failed",
        "running" | "pending" => "interrupted",
        "interrupted" | "cancelled" => "interrupted",
        _ => "completed",
    }
}

/// A `/log` tool call or cell as a tool item.
fn tool_item(value: &Value, cell: bool, options: &Options) -> Object {
    let call = text(value, "id");
    let tool = if cell {
        let lines: Vec<&str> = value.get("lines").and_then(Value::as_array).into_iter().flatten().map(|l| text(l, "text")).filter(|t| !t.is_empty()).collect();
        let kind = text(value, "kind");
        json!({"name": text(value, "title"), "call_id": call, "category": category(kind), "label": text(value, "summary"),
            "output": {"tail": lines, "total_lines": lines.len()}})
    } else {
        let name = [text(value, "name"), text(value, "action")].into_iter().find(|s| !s.is_empty()).unwrap_or("Tool");
        let category = category(name);
        let command = text(value, "command");
        let path = text(value, "file_path");
        let label = match category {
            "exec" if !command.is_empty() => command,
            "read" | "edit" | "write" if !path.is_empty() => path,
            _ => [text(value, "summary"), text(value, "description"), command, path].into_iter().find(|s| !s.is_empty()).unwrap_or_default(),
        };
        let result: Vec<&str> = text(value, "result").lines().collect();
        let mut tool = json!({"name": name, "call_id": call, "category": category, "label": label});
        if !command.is_empty() {
            tool["command"] = json!(command);
        }
        if !result.is_empty() {
            let tail = &result[result.len().saturating_sub(20)..];
            tool["output"] = json!({"tail": tail, "total_lines": result.len()});
        }
        if let Some(explain) = options.explain.and_then(|explain| value.as_object().map(explain)).filter(|e| !e.is_empty()) {
            tool["explain"] = json!({"text": explain, "status": "ready"});
        }
        tool
    };
    json!({"id": format!("log:{call}"), "kind": "tool", "status": status(value), "tool": tool}).as_object().cloned().unwrap_or_default()
}

fn ms(row: &Message) -> Option<i64> {
    crate::time_format::epoch_ms(&row.timestamp)
}

#[allow(clippy::too_many_arguments)]
fn turn(
    rows: &[Message],
    prompt: Option<usize>,
    span: std::ops::Range<usize>,
    next: Option<usize>,
    live: Option<&LiveView>,
    options: &Options,
    folded: &mut Folded,
) -> Option<Fold> {
    // Local placeholders (thinking, compacting) stay as they are.
    let members: Vec<&Message> = rows[span].iter().filter(|m| !m.activity).collect();
    // A streaming row: the turn is not over.
    if members.is_empty() || members.iter().any(|m| m.kind == "live" || m.pending) {
        return None;
    }
    let turn_id = prompt.map(|p| rows[p].trace_id.as_str()).filter(|t| !t.is_empty()).or_else(|| members.iter().map(|m| m.trace_id.as_str()).find(|t| !t.is_empty())).unwrap_or_default();
    if (!turn_id.is_empty() && turn_id == options.open_turn) || members.iter().any(|m| options.held_rows.contains(&m.id)) {
        return None;
    }
    let over = next.is_some_and(|n| !rows[n].pending) || !options.busy;
    let mut seen = HashSet::new();
    let tool_count = members.iter().flat_map(|m| calls(m)).filter(|(v, _)| seen.insert(text(v, "id").to_owned())).count();
    if !over || tool_count == 0 {
        return None;
    }
    let answer = members.iter().rposition(|m| !m.display_text.trim().is_empty());
    let retired = (!turn_id.is_empty()).then(|| live.and_then(|v| v.retired(turn_id))).flatten();
    let remembered: HashMap<&str, &Object> = retired
        .iter()
        .flat_map(|(_, items)| items.iter())
        .filter_map(|i| Some((i.get("tool")?.get("call_id")?.as_str()?, *i)))
        .collect();
    let mut reasoning: Vec<&Object> = retired.iter().flat_map(|(_, items)| items.iter().copied()).filter(|i| i.get("kind").and_then(Value::as_str) == Some("reasoning")).collect();
    let ordinal = |i: &Object| i.get("ordinal").and_then(Value::as_i64).unwrap_or(0);

    // The turn's items in order: its commentary and tools, the answer last.
    let mut items: Vec<Object> = Vec::new();
    let mut rows_of: HashMap<String, Entry> = HashMap::new();
    let mut emitted = HashSet::new();
    let live_options = live_present::Options { explanations: options.explanations, expanded: options.expanded.clone(), host_now_ms: 0 };
    let mut message = |items: &mut Vec<Object>, row: &Message, phase: &str| {
        let item = json!({"id": format!("log:{}", row.id), "kind": "message", "status": "completed", "phase": phase, "text": row.display_text, "row_id": row.id});
        let item = item.as_object().cloned().unwrap_or_default();
        let mut entry = live_present::item_entry(&item, &live_options);
        entry.row = row.id.clone();
        rows_of.insert(format!("log:{}", row.id), entry);
        items.push(item);
    };
    let flush = |items: &mut Vec<Object>, reasoning: &mut Vec<&Object>, before: Option<i64>| {
        reasoning.retain(|r| {
            let due = before.is_none_or(|b| ordinal(r) < b);
            if due {
                items.push((*r).clone());
            }
            !due
        });
    };
    for (index, row) in members.iter().enumerate() {
        let is_answer = answer == Some(index);
        let has_text = !row.display_text.trim().is_empty();
        if has_text && !is_answer {
            message(&mut items, row, "commentary");
        }
        for (value, cell) in calls(row) {
            let call = text(value, "id");
            if !emitted.insert(call.to_owned()) {
                continue;
            }
            folded.stripped_calls.insert(call.to_owned());
            match remembered.get(call) {
                Some(item) => {
                    flush(&mut items, &mut reasoning, Some(ordinal(item)));
                    items.push((*item).clone());
                }
                None => items.push(tool_item(value, cell, options)),
            }
        }
        if is_answer {
            flush(&mut items, &mut reasoning, None);
            message(&mut items, row, "final");
        }
    }
    flush(&mut items, &mut reasoning, None);

    let summary = match retired {
        Some((turn, _)) => turn.clone(),
        None => {
            let start = prompt.and_then(|p| ms(&rows[p])).or_else(|| members.first().and_then(|m| ms(m)));
            let end = members.iter().rev().find_map(|m| ms(m));
            let worked = match (start, end) {
                (Some(start), Some(end)) => end - start,
                _ => 0,
            };
            json!({"turn_id": turn_id, "status": "completed", "worked_ms": worked, "tool_count": tool_count}).as_object().cloned().unwrap_or_default()
        }
    };
    let id = if turn_id.is_empty() { format!("row:{}", members[0].id) } else { turn_id.to_owned() };
    let shown: Vec<&Object> = items.iter().collect();
    let entries = live_present::arrange(&id, &summary, true, &shown, rows_of, &live_options);
    let pieces: Vec<Piece> = entries.into_iter().map(|e| if e.row.is_empty() { Piece::Entry(e) } else { Piece::Row(e.row) }).collect();
    let placed: HashSet<&str> = pieces.iter().filter_map(|p| if let Piece::Row(id) = p { Some(id.as_str()) } else { None }).collect();
    for row in &members {
        let shown_here = !row.display_text.trim().is_empty() || calls(row).next().is_some();
        if shown_here && !placed.contains(row.id.as_str()) {
            folded.hidden_rows.insert(row.id.clone());
        }
    }
    Some(Fold {
        key: format!("live:fold:{id}"),
        prompt: prompt.map(|p| rows[p].id.clone()),
        next: next.map(|n| rows[n].id.clone()),
        pieces,
    })
}
