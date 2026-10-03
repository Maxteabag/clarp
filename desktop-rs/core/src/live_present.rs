//! How live items read in a transcript (docs/live-items.md §7.3–7.6): the
//! status line, one entry per item row in ordinal order (explore groups as
//! one), the fold of a settled turn, and which items durable `/log` rows
//! have taken over. Pure: the window turns entries into rows.

use std::collections::{HashMap, HashSet};

use serde_json::Value;

use crate::json::Object;
use crate::live::{LiveView, is_terminal};
use crate::protocol::Message;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum Kind {
    #[default]
    Message,
    Reasoning,
    Tool,
    Explore,
    Plan,
    Diff,
    Compaction,
    Fold,
}

impl Kind {
    pub fn name(self) -> &'static str {
        match self {
            Kind::Message => "message",
            Kind::Reasoning => "reasoning",
            Kind::Tool => "tool",
            Kind::Explore => "explore",
            Kind::Plan => "plan",
            Kind::Diff => "diff",
            Kind::Compaction => "compaction",
            Kind::Fold => "fold",
        }
    }
}

/// One transcript row of the open turn.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct Entry {
    /// `live:<item id>`, `live:explore:<group id>`, `live:fold:<turn id>`:
    /// the row's identity, what the keyboard selects and expansion is kept by.
    pub key: String,
    pub kind: Kind,
    /// The items the row stands for (a group's members, the fold's items).
    pub items: Vec<String>,
    /// pending, running, completed, failed, interrupted.
    pub status: String,
    /// The first line: verb and label ("Running npm test"), empty for a message.
    pub title: String,
    /// The right side: elapsed, duration, exit code, diff stats.
    pub meta: String,
    /// The second line: the explanation, or with explanations off the command.
    pub secondary: String,
    /// Keep a line for an explanation that has not arrived.
    pub reserve_secondary: bool,
    /// The explanation is on its way: the reserved line says so.
    pub explaining: bool,
    /// Monospace lines: an output tail, a diff, a plan's steps, members.
    pub lines: Vec<String>,
    /// What `lines` leaves out ("+808 lines").
    pub more: String,
    /// A message's Markdown.
    pub text: String,
    pub phase: String,
    pub expandable: bool,
    pub expanded: bool,
    /// The items' revisions summed: a row changes only when this does.
    pub rev: i64,
    /// The durable `/log` row that took over this message: it shows here,
    /// in the item's place.
    pub row: String,
}

#[derive(Debug, Clone, Default)]
pub struct Options {
    /// The Host explains tool items (`tool_explanations.enabled`).
    pub explanations: bool,
    /// Entry keys the reader opened.
    pub expanded: HashSet<String>,
    /// Host time now (`LiveView::host_now_ms`), for running items.
    pub host_now_ms: i64,
}

#[derive(Debug, Clone, Default)]
pub struct Presented {
    pub entries: Vec<Entry>,
    /// `/log` live rows whose text an item shows instead.
    pub hidden_rows: Vec<String>,
    /// Item id → the durable row that took it over.
    pub taken_over: HashMap<String, String>,
    /// Durable rows whose every part an entry shows: not shown themselves.
    pub absorbed_rows: Vec<String>,
    /// Tool and cell ids entries show: left out of the durable rows.
    pub stripped_calls: HashSet<String>,
    /// The open turn and when it began (Host time).
    pub turn_id: String,
    pub started_at_ms: Option<i64>,
    /// The turn's own durable rows: they never push its place.
    pub own_rows: HashSet<String>,
}

impl Presented {
    /// Where the turn's rows go among the chat's rows (§7.3): after the
    /// prompt that started it and every row from before it began, before
    /// the first row written after that.
    pub fn anchor(&self, rows: &[&Message]) -> usize {
        let Some(started) = self.started_at_ms else { return rows.len() };
        // The prompt carries the turn's id: whatever its time, the turn
        // goes after it.
        let from = rows.iter().position(|r| r.role == "user" && !self.turn_id.is_empty() && r.trace_id == self.turn_id).map_or(0, |i| i + 1);
        let own = |row: &Message| self.own_rows.contains(&row.id) || (!self.turn_id.is_empty() && row.trace_id == self.turn_id);
        // Rows without a Host time (unsent ones) never push it up.
        let later = |row: &Message| crate::time_format::epoch_ms(&row.timestamp).is_some_and(|ms| ms > started);
        rows.iter().skip(from).position(|r| !own(r) && !r.pending && later(r)).map_or(rows.len(), |i| from + i)
    }
}

#[derive(Debug, Clone, PartialEq, Default)]
pub struct StatusLine {
    pub text: String,
    /// The agent is working (the interrupt key works).
    pub busy: bool,
    pub state: String,
}

const TAIL_COLLAPSED: usize = 4;

fn text<'a>(object: &'a Object, key: &str) -> &'a str {
    object.get(key).and_then(Value::as_str).unwrap_or_default()
}

fn int(object: &Object, key: &str) -> Option<i64> {
    object.get(key).and_then(Value::as_i64)
}

fn object<'a>(object: &'a Object, key: &str) -> Option<&'a Object> {
    object.get(key).and_then(Value::as_object)
}

/// `m:ss` (`h:mm:ss` past an hour).
pub fn clock(ms: i64) -> String {
    let seconds = ms.max(0) / 1000;
    if seconds >= 3600 {
        format!("{}:{:02}:{:02}", seconds / 3600, seconds / 60 % 60, seconds % 60)
    } else {
        format!("{}:{:02}", seconds / 60, seconds % 60)
    }
}

/// "12s", "1m 12s", "2h 3m".
pub fn worked(ms: i64) -> String {
    let seconds = (ms.max(0) + 500) / 1000;
    match seconds {
        s if s < 60 => format!("{}s", s.max(1)),
        s if s < 3600 => if s % 60 == 0 { format!("{}m", s / 60) } else { format!("{}m {}s", s / 60, s % 60) },
        s => format!("{}h {}m", s / 3600, s / 60 % 60),
    }
}

/// A settled item's duration: "3.2s" under a minute, else "1m 12s".
fn duration(ms: i64) -> String {
    if ms < 60_000 { format!("{:.1}s", ms.max(0) as f64 / 1000.0) } else { worked(ms) }
}

fn plural(count: usize, one: &str, many: &str) -> String {
    format!("{count} {}", if count == 1 { one } else { many })
}

/// The one status line (§7.4), or none when idle.
pub fn status_line(view: &LiveView, host_now_ms: i64) -> Option<StatusLine> {
    let activity = view.activity();
    let state = text(activity, "state");
    let headline = text(activity, "headline");
    let since = |key: Option<i64>| key.map(|start| format!(" · {}", clock(host_now_ms - start))).unwrap_or_default();
    let turn_started = int(activity, "turn_started_ms");
    let (text, busy) = match state {
        "" | "idle" => return None,
        "tool" => {
            let tool = object(activity, "tool");
            let started = tool.and_then(|t| int(t, "started_at_ms"));
            let label = if headline.is_empty() { tool.map(|t| text(t, "label")).unwrap_or_default().to_owned() } else { headline.to_owned() };
            let more = int(activity, "running_tools").unwrap_or(0) - 1;
            let extra = if more > 0 { format!(" +{more}") } else { String::new() };
            (format!("● {label}{}{extra}", since(started)), true)
        }
        "thinking" => (format!("◌ {}{}", if headline.is_empty() { "Thinking" } else { headline }, since(turn_started)), true),
        "responding" => (format!("◌ Responding{}", since(turn_started)), true),
        "compacting" => (format!("◌ Compacting{}", since(turn_started)), true),
        "limited" => (format!("◌ {}", if headline.is_empty() { "Waiting for the usage limit" } else { headline }), true),
        "waiting" => (format!("◆ {}", if headline.is_empty() { "Waiting for you" } else { headline }), false),
        "interrupted" => ("■ Interrupted".to_owned(), false),
        "background" => (format!("◇ {}", if headline.is_empty() { "Working in the background" } else { headline }), false),
        // An unknown state is ignored.
        _ => return None,
    };
    Some(StatusLine { text, busy, state: state.to_owned() })
}

/// The ids durable rows carry: their own and their tools' and cells'.
struct Durable<'a> {
    by_row: HashMap<&'a str, &'a str>,
    by_call: HashMap<String, &'a str>,
}

fn durable(rows: &[Message]) -> Durable<'_> {
    let mut by_row = HashMap::new();
    let mut by_call = HashMap::new();
    for row in rows.iter().filter(|m| m.kind != "live" && !m.pending && !m.activity && !m.id.is_empty()) {
        by_row.insert(row.id.as_str(), row.id.as_str());
        for value in row.tools.iter().chain(&row.display_cells) {
            if let Some(id) = value.get("id").and_then(Value::as_str).filter(|id| !id.is_empty()) {
                by_call.entry(id.to_owned()).or_insert(row.id.as_str());
            }
        }
    }
    Durable { by_row, by_call }
}

fn known_kind(kind: &str) -> bool {
    matches!(kind, "message" | "reasoning" | "tool" | "plan" | "diff" | "compaction")
}

fn explore_category(item: &Object) -> Option<&str> {
    let tool = object(item, "tool")?;
    let category = text(tool, "category");
    (matches!(category, "read" | "list" | "search") && tool.get("group").and_then(Value::as_str).is_some_and(|g| !g.is_empty())).then_some(category)
}

/// The open turn's items as rows (§7.3, §7.5, §7.6).
pub fn present(view: &LiveView, rows: &[Message], options: &Options) -> Presented {
    let mut presented = Presented::default();
    let turn = view.turn();
    let Some(turn_id) = turn.map(|t| text(t, "turn_id").to_owned()).filter(|t| !t.is_empty()) else { return presented };
    let items: Vec<&Object> =
        view.items().into_iter().filter(|i| text(i, "turn_id") == turn_id && known_kind(text(i, "kind"))).collect();
    let durable = durable(rows);
    let settled = turn.is_some_and(|t| !matches!(text(t, "status"), "running" | ""));
    // Taken over (§7.5): a durable row with the message's row_id, or a
    // tool or cell with the tool's call_id. A message that stays in view
    // (a settled turn folds commentary) is shown by its row, in its place;
    // everything else keeps its item row, so a turn reads the same before
    // and after it lands: one row per tool, the reasoning, the fold.
    let owner_of = |item: &Object| -> Option<&str> {
        match text(item, "kind") {
            "message" => durable.by_row.get(text(item, "row_id")).copied(),
            "tool" => object(item, "tool").and_then(|t| durable.by_call.get(text(t, "call_id"))).copied(),
            _ => None,
        }
    };
    let stays = |item: &Object| !settled || text(item, "phase") != "commentary" || text(item, "status") != "completed";
    // Rows that carry a message staying in view: they show, at its place.
    let giving: HashSet<&str> = items.iter().filter(|i| text(i, "kind") == "message" && stays(i)).filter_map(|i| owner_of(i)).collect();
    let mut placed: HashSet<&str> = HashSet::new();
    let mut rows_of: HashMap<&str, Entry> = HashMap::new();
    let mut shown = Vec::new();
    let mut owned_rows: Vec<&str> = Vec::new();
    for item in &items {
        let Some(row) = owner_of(item) else {
            shown.push(*item);
            continue;
        };
        presented.taken_over.insert(text(item, "id").to_owned(), row.to_owned());
        if !owned_rows.contains(&row) {
            owned_rows.push(row);
        }
        if let Some(tool) = object(item, "tool") {
            presented.stripped_calls.insert(text(tool, "call_id").to_owned());
        }
        if text(item, "kind") == "message" && giving.contains(row) {
            // The row's text holds every message it carries: it shows once.
            if stays(item) && placed.insert(row) {
                let mut entry = item_entry(item, options);
                entry.row = row.to_owned();
                rows_of.insert(text(item, "id"), entry);
                shown.push(*item);
            }
            continue;
        }
        shown.push(*item);
    }
    // A row whose every part an item row shows is not shown again.
    let calls: HashSet<&str> = items.iter().filter_map(|i| object(i, "tool")).map(|t| text(t, "call_id")).collect();
    for id in owned_rows.into_iter().filter(|r| !giving.contains(r)) {
        let Some(row) = rows.iter().find(|m| m.id == id) else { continue };
        let carries_message = items.iter().any(|i| text(i, "kind") == "message" && text(i, "row_id") == id);
        let every_call = row.tools.iter().chain(&row.display_cells).all(|v| v.get("id").and_then(Value::as_str).is_some_and(|c| calls.contains(c)));
        if every_call && (carries_message || row.display_text.trim().is_empty()) {
            presented.absorbed_rows.push(id.to_owned());
        }
    }
    // A live /log row an item shows (its row_id) is hidden.
    let claimed: HashSet<&str> = shown.iter().filter(|i| text(i, "kind") == "message").map(|i| text(i, "row_id")).filter(|r| !r.is_empty()).collect();
    presented.hidden_rows = rows.iter().filter(|m| m.kind == "live" && claimed.contains(m.id.as_str())).map(|m| m.id.clone()).collect();

    let rows_of = rows_of.into_iter().map(|(id, entry)| (id.to_owned(), entry)).collect();
    let entries = arrange(&turn_id, turn.expect("a turn"), settled, &shown, rows_of, options);
    presented.entries = entries;
    presented.turn_id = turn_id;
    presented.started_at_ms = turn.and_then(|t| int(t, "started_at_ms"));
    presented.own_rows = presented.taken_over.values().chain(&presented.absorbed_rows).chain(&presented.hidden_rows).cloned().collect();
    presented
}

/// A turn's item rows: one entry per item (an entry in `rows_of` stands
/// for its item), consecutive explore members as one, and when the turn
/// has settled its work folded (§7.6). The live turn and history's
/// settled turns (`history_fold`) read the same through this.
pub(crate) fn arrange(turn_id: &str, turn: &Object, settled: bool, shown: &[&Object], mut rows_of: HashMap<String, Entry>, options: &Options) -> Vec<Entry> {
    // One entry per item, consecutive explore members as one.
    let mut entries: Vec<Entry> = Vec::new();
    let mut index = 0;
    while index < shown.len() {
        let item = shown[index];
        if explore_category(item).is_some() {
            let group = object(item, "tool").map(|t| text(t, "group")).unwrap_or_default();
            let mut members = vec![item];
            while let Some(next) = shown.get(index + members.len()) {
                if explore_category(next).is_some() && object(next, "tool").map(|t| text(t, "group")) == Some(group) {
                    members.push(next);
                } else {
                    break;
                }
            }
            index += members.len();
            entries.push(explore_entry(group, &members, options));
            continue;
        }
        entries.push(rows_of.remove(text(item, "id")).unwrap_or_else(|| item_entry(item, options)));
        index += 1;
    }

    // A settled turn folds its work (§7.6): what failed or stopped and the
    // final answer stay out of the fold.
    if settled {
        let stays = |entry: &Entry| {
            matches!(entry.status.as_str(), "failed" | "interrupted" | "running" | "pending") || (entry.kind == Kind::Message && entry.phase != "commentary")
        };
        let folded: Vec<usize> = (0..entries.len()).filter(|i| !stays(&entries[*i])).collect();
        if let Some(&first) = folded.first() {
            let key = format!("live:fold:{turn_id}");
            let open = options.expanded.contains(&key);
            let worked_ms = int(turn, "worked_ms").or_else(|| Some(int(turn, "ended_at_ms")? - int(turn, "started_at_ms")?));
            let tools = int(turn, "tool_count").unwrap_or(0).max(0) as usize;
            // A turn whose worked time nobody recorded says what it used.
            let title = match worked_ms {
                Some(ms) if tools > 0 => format!("Worked for {} · {}", worked(ms), plural(tools, "tool", "tools")),
                Some(ms) => format!("Worked for {}", worked(ms)),
                None if tools > 0 => format!("Used {}", plural(tools, "tool", "tools")),
                None => "Worked".to_owned(),
            };
            let fold = Entry {
                items: folded.iter().flat_map(|i| entries[*i].items.clone()).collect(),
                rev: folded.iter().map(|i| entries[*i].rev).sum(),
                key,
                kind: Kind::Fold,
                status: text(turn, "status").to_owned(),
                title,
                expandable: true,
                expanded: open,
                ..Entry::default()
            };
            if open {
                entries.insert(first, fold);
            } else {
                let mut kept = Vec::with_capacity(entries.len());
                for (i, entry) in entries.into_iter().enumerate() {
                    if i == first {
                        kept.push(fold.clone());
                    }
                    if !folded.contains(&i) {
                        kept.push(entry);
                    }
                }
                entries = kept;
            }
        }
    }
    entries
}

fn verb(category: &str, status: &str) -> &'static str {
    let done = is_terminal(status);
    let stopped = status == "interrupted";
    match category {
        "exec" if stopped => "Stopped",
        "exec" => if done { "Ran" } else { "Running" },
        "read" => if done { "Read" } else { "Reading" },
        "list" => if done { "Listed" } else { "Listing" },
        "search" => if done { "Searched" } else { "Searching" },
        "edit" => if done { "Edited" } else { "Editing" },
        "write" => if done { "Wrote" } else { "Writing" },
        "fetch" => if done { "Fetched" } else { "Fetching" },
        "todo" => if done { "Updated the plan" } else { "Updating the plan" },
        "agent" => if done { "Delegated" } else { "Delegating" },
        _ => "",
    }
}

fn elapsed_or_duration(item: &Object, options: &Options) -> String {
    let status = text(item, "status");
    let started = int(item, "started_at_ms").unwrap_or(0);
    match int(item, "ended_at_ms") {
        Some(ended) if is_terminal(status) => duration(ended - started),
        _ if status == "running" && started > 0 && options.host_now_ms > 0 => clock(options.host_now_ms - started),
        _ => String::new(),
    }
}

fn join_meta(parts: &[String]) -> String {
    parts.iter().filter(|p| !p.is_empty()).cloned().collect::<Vec<_>>().join(" · ")
}

pub(crate) fn item_entry(item: &Object, options: &Options) -> Entry {
    let id = text(item, "id");
    let key = format!("live:{id}");
    let status = text(item, "status").to_owned();
    let expanded = options.expanded.contains(&key);
    let mut entry = Entry {
        items: vec![id.to_owned()],
        status: status.clone(),
        rev: int(item, "rev").unwrap_or(0),
        expanded,
        key,
        ..Entry::default()
    };
    match text(item, "kind") {
        "message" => {
            entry.kind = Kind::Message;
            entry.text = text(item, "text").to_owned();
            entry.phase = text(item, "phase").to_owned();
            if status == "interrupted" {
                entry.meta = "interrupted".into();
            }
        }
        "reasoning" => {
            entry.kind = Kind::Reasoning;
            let title = text(item, "title");
            let head = if is_terminal(&status) {
                let ms = int(item, "ended_at_ms").unwrap_or(0) - int(item, "started_at_ms").unwrap_or(0);
                format!("Thought for {}", worked(ms))
            } else {
                "Thinking".to_owned()
            };
            entry.title = if title.is_empty() { head } else { format!("{head}: {title}") };
            let body: Vec<String> = text(item, "text").lines().map(str::to_owned).collect();
            entry.expandable = !body.iter().all(|l| l.trim().is_empty());
            if expanded {
                entry.lines = body;
            }
        }
        "tool" => tool_entry(item, options, &mut entry),
        "plan" => {
            entry.kind = Kind::Plan;
            let steps: Vec<&Object> = object(item, "plan").and_then(|p| p.get("steps")).and_then(Value::as_array).into_iter().flatten().filter_map(Value::as_object).collect();
            let done = steps.iter().filter(|s| text(s, "status") == "completed").count();
            entry.title = format!("Plan · {done} of {} done", steps.len());
            entry.lines = steps
                .iter()
                .map(|s| {
                    let mark = match text(s, "status") {
                        "completed" => "✓",
                        "in_progress" => "▸",
                        _ => "○",
                    };
                    format!("{mark} {}", text(s, "text"))
                })
                .collect();
        }
        "diff" => {
            entry.kind = Kind::Diff;
            let diff = object(item, "diff");
            let files: Vec<&Object> = diff.and_then(|d| d.get("files")).and_then(Value::as_array).into_iter().flatten().filter_map(Value::as_object).collect();
            entry.title = format!("Changed {}", plural(files.len(), "file", "files"));
            entry.meta = diff.map(diff_stats).unwrap_or_default();
            entry.expandable = true;
            if expanded {
                entry.lines = files.iter().map(|f| format!("{} {}", text(f, "path"), diff_stats(f))).collect();
                entry.lines.extend(diff.map(|d| text(d, "preview").lines().map(str::to_owned).collect::<Vec<_>>()).unwrap_or_default());
            }
        }
        _ => {
            entry.kind = Kind::Compaction;
            entry.title = if is_terminal(&status) { "Compacted the context".into() } else { "Compacting the context".into() };
            entry.meta = elapsed_or_duration(item, options);
        }
    }
    entry
}

fn diff_stats(diff: &Object) -> String {
    format!("+{} −{}", int(diff, "added").unwrap_or(0), int(diff, "removed").unwrap_or(0))
}

fn tool_entry(item: &Object, options: &Options, entry: &mut Entry) {
    entry.kind = Kind::Tool;
    let empty = Object::new();
    let tool = object(item, "tool").unwrap_or(&empty);
    let status = entry.status.clone();
    let label = text(tool, "label");
    let verb = verb(text(tool, "category"), &status);
    let verb = if verb.is_empty() { text(tool, "name") } else { verb };
    entry.title = [verb, label].iter().filter(|s| !s.is_empty()).copied().collect::<Vec<_>>().join(" ");
    let output = object(tool, "output");
    let exit = output.and_then(|o| int(o, "exit_code")).filter(|c| *c != 0).map(|c| format!("exit {c}")).unwrap_or_default();
    let diff = object(tool, "diff").filter(|d| d.contains_key("added") || d.contains_key("removed"));
    let stopped = if status == "interrupted" { "interrupted".to_owned() } else { String::new() };
    entry.meta = match diff {
        Some(diff) => join_meta(&[diff_stats(diff), exit.clone(), stopped]),
        None if status == "interrupted" => stopped,
        None => join_meta(&[exit.clone(), elapsed_or_duration(item, options)]),
    };
    // Second line (§6): the explanation when explained, else with
    // explanations off the raw command.
    let explain = object(tool, "explain");
    if options.explanations {
        entry.secondary = explain.map(|e| text(e, "text")).unwrap_or_default().to_owned();
        // A line is kept while an explanation may still come, so it lands
        // without moving the chat; a settled tool without one needs none.
        // Until it lands the line says it is coming.
        let state = explain.map(|e| text(e, "status")).unwrap_or_default();
        let coming = state == "pending" || (explain.is_some() && !matches!(state, "ready" | "failed")) || (!is_terminal(&status) && state != "failed");
        entry.explaining = entry.secondary.is_empty() && coming;
        entry.reserve_secondary = entry.explaining || !is_terminal(&status);
    } else {
        entry.secondary = tool.get("command").and_then(Value::as_str).filter(|c| *c != label).unwrap_or_default().to_owned();
    }
    let tail: Vec<String> = output.and_then(|o| o.get("tail")).and_then(Value::as_array).into_iter().flatten().filter_map(Value::as_str).map(str::to_owned).collect();
    let total = output.and_then(|o| int(o, "total_lines")).unwrap_or(tail.len() as i64).max(tail.len() as i64) as usize;
    let preview: Vec<String> = diff.map(|d| text(d, "preview").lines().map(str::to_owned).collect()).unwrap_or_default();
    entry.expandable = !tail.is_empty() || !preview.is_empty();
    if entry.expanded {
        if !preview.is_empty() {
            entry.lines = preview;
        } else {
            entry.lines = tail.clone();
            if total > tail.len() {
                entry.more = format!("+{} earlier lines", total - tail.len());
            }
        }
    } else if !tail.is_empty() && (status == "running" || status == "failed" || !exit.is_empty()) {
        // A running or failed command shows its last lines; a clean one folds.
        let start = tail.len().saturating_sub(TAIL_COLLAPSED);
        entry.lines = tail[start..].to_vec();
        if total > entry.lines.len() {
            entry.more = format!("+{} lines", total - entry.lines.len());
        }
    }
}

fn explore_entry(group: &str, members: &[&Object], options: &Options) -> Entry {
    // The Host's group ids read `explore:<first member>` already.
    let key = if group.starts_with("explore:") { format!("live:{group}") } else { format!("live:explore:{group}") };
    let expanded = options.expanded.contains(&key);
    let running = members.iter().any(|m| !is_terminal(text(m, "status")));
    let status = if running {
        "running"
    } else if members.iter().any(|m| text(m, "status") == "failed") {
        "failed"
    } else if members.iter().any(|m| text(m, "status") == "interrupted") {
        "interrupted"
    } else {
        "completed"
    };
    let count = |category: &str| {
        members
            .iter()
            .filter(|m| explore_category(m) == Some(category))
            .map(|m| object(m, "tool").map(|t| text(t, "label")).unwrap_or_default())
            .collect::<HashSet<_>>()
            .len()
    };
    let parts: Vec<String> = [(count("read"), "file", "files"), (count("search"), "search", "searches"), (count("list"), "listing", "listings")]
        .into_iter()
        .filter(|(n, _, _)| *n > 0)
        .map(|(n, one, many)| plural(n, one, many))
        .collect();
    let title = if running { "Exploring".to_owned() } else { format!("Explored {}", parts.join(", ")) };
    Entry {
        key,
        kind: Kind::Explore,
        items: members.iter().map(|m| text(m, "id").to_owned()).collect(),
        status: status.to_owned(),
        title,
        lines: if expanded {
            members
                .iter()
                .map(|m| {
                    let tool = object(m, "tool");
                    let category = tool.map(|t| text(t, "category")).unwrap_or_default();
                    format!("{} {}", verb(category, text(m, "status")), tool.map(|t| text(t, "label")).unwrap_or_default())
                })
                .collect()
        } else {
            Vec::new()
        },
        expandable: true,
        expanded,
        rev: members.iter().map(|m| int(m, "rev").unwrap_or(0)).sum(),
        meta: if running { members.iter().filter_map(|m| int(m, "started_at_ms")).min().filter(|_| options.host_now_ms > 0).map(|s| clock(options.host_now_ms - s)).unwrap_or_default() } else { String::new() },
        ..Entry::default()
    }
}
