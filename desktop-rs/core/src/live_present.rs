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
    /// A message's Markdown: its written part (#823).
    pub text: String,
    /// A message's spoken part, drawn as a quiet summary line above its
    /// Markdown; empty when it has none or is only spoken (#823).
    pub spoken_text: String,
    pub phase: String,
    /// A message that carried speech (a `<speak>` block the user heard):
    /// commentary that did stays in view, above the work row (#653).
    pub spoken: bool,
    pub expandable: bool,
    pub expanded: bool,
    /// The items' revisions summed: a row changes only when this does.
    pub rev: i64,
    /// The durable `/log` row that took over this message: it shows here,
    /// in the item's place.
    pub row: String,
    /// When the earliest of its items began (Host time): no row written
    /// before then goes below it.
    pub started_at_ms: Option<i64>,
    /// The key that stops the agent, drawn as Stop inside the row (a
    /// stalled turn's Running box, #702); empty for none.
    pub stop: String,
}

#[derive(Debug, Clone, Default)]
pub struct Options {
    /// The Host explains tool items (`tool_explanations.enabled`).
    pub explanations: bool,
    /// Entry keys the reader opened.
    pub expanded: HashSet<String>,
    /// Host time now (`LiveView::host_now_ms`), for running items.
    pub host_now_ms: i64,
    /// Fold the running turn's work into one `Working · N tools` row
    /// (Tool activity Grouped and Hide when done, #653).
    pub fold_running: bool,
    /// Leave a settled turn's work row out (Hide when done): its messages,
    /// and what failed or stopped, stay.
    pub hide_settled: bool,
    /// Always visible (#653): commentary (the agent's lines between tools,
    /// phase `commentary`) stays in its place instead of folding into the
    /// work row as a working detail. The final reply never folds.
    pub show_commentary: bool,
    /// The chat hides coordination between agents (its `/log` was read
    /// hidden): what a turn wrote for other agents is left out
    /// ([`LiveView::veiled`]).
    pub veil: bool,
    /// Conversation view: the turn's work stays one piece (its Running box
    /// holds all of it), never split at a message read into it.
    pub whole_turn: bool,
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
    /// The `/log` rows of the messages read into the turn as it ran, oldest
    /// first (`read_rows`, feature `live_read_rows`, Rust Host contract
    /// 185): its working line goes below the last of them.
    pub read_rows: Vec<String>,
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
        rows.iter().skip(from).position(|r| self.pushes(r) && written(r).is_some_and(|ms| ms > started)).map_or(rows.len(), |i| from + i)
    }

    /// Where each entry goes among the chat's rows, in entry order: at the
    /// turn's place, and below every row not its own written before the
    /// entry began. A turn the Host never settled (its agent died) whose
    /// items go on under newer prompts splits at them.
    ///
    /// The turn's working line (its running work row or Running box) goes
    /// below the last message read into the turn (`read_rows`): a message
    /// steered into a running turn sits above Working. What the turn did
    /// before that message keeps its place above it, in a work row of its
    /// own (`arrange`), and every step keeps the place its time gives it, so
    /// the turn reads in the same order once it settles and history holds it.
    pub fn anchors(&self, rows: &[&Message]) -> Vec<usize> {
        let mut at = self.anchor(rows);
        let read = self.below_read(rows);
        self.entries
            .iter()
            .map(|entry| {
                if self.started_at_ms.is_some()
                    && let Some(began) = entry.started_at_ms
                {
                    let earlier = rows.iter().rposition(|r| self.pushes(r) && written(r).is_some_and(|ms| ms <= began));
                    at = at.max(earlier.map_or(0, |i| i + 1));
                }
                if working(entry)
                    && let Some(read) = read
                {
                    at = at.max(read);
                }
                at
            })
            .collect()
    }

    /// Just below the last shown row read into the turn; None where none
    /// was read (or none of them is shown).
    fn below_read(&self, rows: &[&Message]) -> Option<usize> {
        let read: HashSet<&str> = self.read_rows.iter().map(String::as_str).collect();
        rows.iter().rposition(|r| read.contains(r.id.as_str())).map(|i| i + 1)
    }

    /// A row that pushes the turn's place: not its own and sent. The turn's
    /// own answer that no item stands for (a backend whose live items carry no
    /// final message) pushes it too: the work goes above its answer.
    fn pushes(&self, row: &Message) -> bool {
        if row.pending || self.own_rows.contains(&row.id) {
            return false;
        }
        let traced = !self.turn_id.is_empty() && row.trace_id == self.turn_id;
        !traced || (row.role == "assistant" && row.phase == "final" && row.kind != "live")
    }
}

/// The turn's working line: its work row or Running box while the turn runs.
/// A work row of what the turn did before a message it read is not one
/// (its key names that message), and a step keeps the place its time gives
/// it, as it will in history.
fn working(entry: &Entry) -> bool {
    entry.kind == Kind::Fold && !entry.key.contains(PART) && matches!(entry.status.as_str(), "running" | "pending" | crate::chat_view::STALLED)
}

/// Between a turn's fold key and the message a part of its work ran before:
/// `live:fold:<turn>@<row id>`.
pub const PART: char = '@';

/// A message read into a turn while it ran: the turn's work splits at it.
#[derive(Debug, Clone, PartialEq)]
pub struct Cut {
    /// Its `/log` row.
    pub row: String,
    /// When it was written (Host time).
    pub at_ms: i64,
}

/// A row's Host time; rows without one (unsent ones) never push the turn.
fn written(row: &Message) -> Option<i64> {
    crate::time_format::epoch_ms(&row.timestamp)
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
        // Held for the machine: an ordinary working turn (no words of its own).
        "limited" if crate::chat_view::held_for_the_machine(activity) => (format!("◌ Working{}", since(turn_started)), true),
        "limited" => (format!("◌ {}", if headline.is_empty() { crate::chat_view::limited_words(activity) } else { headline }), true),
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
    let items: Vec<&Object> = view
        .items()
        .into_iter()
        .filter(|i| text(i, "turn_id") == turn_id && known_kind(text(i, "kind")))
        .filter(|i| !(options.veil && turn.is_some_and(|t| view.veiled(t, i))))
        .collect();
    let durable = durable(rows);
    let settled = turn.is_some_and(|t| !matches!(text(t, "status"), "running" | ""));
    // Taken over (§7.5): a durable row with the message's row_id, or a
    // tool or cell with the tool's call_id. A message (it never folds) is
    // shown by its row, in its place; everything else keeps its item row,
    // so a turn reads the same before and after it lands: one row per
    // tool, the reasoning, the fold.
    let owner_of = |item: &Object| -> Option<&str> {
        match text(item, "kind") {
            "message" => durable.by_row.get(text(item, "row_id")).copied(),
            "tool" => object(item, "tool").and_then(|t| durable.by_call.get(text(t, "call_id"))).copied(),
            _ => None,
        }
    };
    // Rows that carry a message: they show, at its place.
    let giving: HashSet<&str> = items.iter().filter(|i| text(i, "kind") == "message").filter_map(|i| owner_of(i)).collect();
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
            // The row's text holds every message it carries: it shows once. Where it
            // holds the answer it stands in the answer's place (and keeps the answer's
            // row there when it lands): it is the answer, it never folds (#653);
            // otherwise in its first message's place, spoken when one of them was.
            let carried: Vec<&Object> = items.iter().copied()
                .filter(|other| text(other, "kind") == "message" && owner_of(*other) == Some(row))
                .collect();
            let stands_for = carried.iter().copied()
                .find(|other| item_entry(*other, options).phase != "commentary")
                .or_else(|| carried.first().copied());
            if stands_for.is_some_and(|chosen| text(chosen, "id") == text(item, "id")) && placed.insert(row) {
                let mut entry = item_entry(item, options);
                entry.row = row.to_owned();
                entry.spoken = entry.spoken || carried.iter().any(|other| item_entry(*other, options).spoken);
                rows_of.insert(text(item, "id"), entry);
                shown.push(*item);
            }
            continue;
        }
        shown.push(*item);
    }
    // A cell of the turn's own rows that groups tools the items show (an
    // exploration cell whose id joins its tools' call ids) is the fold's
    // work too: it is not shown again on its row beside the fold.
    let tool_calls: Vec<&str> = items.iter().filter_map(|i| object(i, "tool")).map(|t| text(t, "call_id")).filter(|c| !c.is_empty()).collect();
    for row in rows.iter().filter(|m| !turn_id.is_empty() && m.trace_id == turn_id) {
        for cell in &row.display_cells {
            let id = cell.get("id").and_then(Value::as_str).unwrap_or_default();
            if !id.is_empty() && !tool_calls.contains(&id) && tool_calls.iter().any(|call| id.contains(call)) {
                presented.stripped_calls.insert(id.to_owned());
            }
        }
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
    // A settled turn's work row takes its time from the Host's /log summary
    // (the turn's last row) once it has landed (#653), and from the live
    // turn until then, so the row does not lose its time meanwhile.
    let live_turn = turn.expect("a turn");
    let summary = if settled { logged_summary(rows, &turn_id) } else { None };
    let read_rows: Vec<String> = live_turn
        .get("read_rows")
        .and_then(Value::as_array)
        .map(|ids| ids.iter().filter_map(Value::as_str).map(str::to_owned).collect())
        .unwrap_or_default();
    // The work splits at each message read into the turn, by when it was
    // written, as history splits it at that row (`history_fold`).
    let cuts: Vec<Cut> = if options.whole_turn {
        Vec::new()
    } else {
        let mut cuts: Vec<Cut> = rows
            .iter()
            .filter(|r| read_rows.contains(&r.id))
            .filter_map(|r| Some(Cut { row: r.id.clone(), at_ms: written(r)? }))
            .collect();
        cuts.sort_by_key(|c| c.at_ms);
        cuts
    };
    let began = |item: &Object| int(item, "started_at_ms");
    presented.entries = arrange_parts(&turn_id, summary.unwrap_or(live_turn), settled, &shown, &began, &cuts, rows_of, options).into_iter().flatten().collect();
    // A durable row whose commentary the work row folds is not shown in
    // the chat either; opened, it shows in its place again.
    let placed_rows: HashSet<&str> = presented.entries.iter().map(|e| e.row.as_str()).collect();
    let folded_rows: Vec<String> = giving.iter().filter(|r| !placed_rows.contains(*r)).map(|r| (*r).to_owned()).collect();
    presented.hidden_rows.extend(folded_rows);
    presented.turn_id = turn_id;
    presented.started_at_ms = turn.and_then(|t| int(t, "started_at_ms"));
    presented.read_rows = read_rows;
    presented.own_rows = presented.taken_over.values().chain(&presented.absorbed_rows).chain(&presented.hidden_rows).cloned().collect();
    presented
}

/// The Host's summary of turn `turn_id` (`log_turn_summary`): the `turn`
/// of its last `/log` row.
pub(crate) fn logged_summary<'a>(rows: &'a [Message], turn_id: &str) -> Option<&'a Object> {
    rows.iter().rev().filter(|m| !turn_id.is_empty() && m.trace_id == turn_id).find_map(|m| m.turn.as_ref())
}

/// The work row's title (#653, Arnie's ruling for every client): the count
/// is the calls the row folds (failed or killed ones keep their own rows
/// and are not counted), the time the /log summary's worked_ms.
/// "Worked for 11s · 3 tools", "Worked for 11s" with no folded call,
/// "3 tools" without a worked time, "Working · 2 tools" while it runs.
/// None: neither a worked time nor a folded call, so no work row.
pub fn work_title(running: bool, worked_ms: Option<i64>, calls: usize) -> Option<String> {
    let count = (calls > 0).then(|| plural(calls, "tool", "tools"));
    Some(match (running, worked_ms, count) {
        (true, _, Some(count)) => format!("Working · {count}"),
        (true, _, None) => "Working".to_owned(),
        (false, Some(ms), Some(count)) => format!("Worked for {} · {count}", worked(ms)),
        (false, Some(ms), None) => format!("Worked for {}", worked(ms)),
        (false, None, Some(count)) => count,
        (false, None, None) => return None,
    })
}

/// A turn's item rows in parts, one more than `cuts`: what ran before the
/// first message read into the turn, between each two, and after the last
/// (#843). An item goes in the part its time (`at`) says; one without a time
/// stays in the part of the item before it. Each part folds on its own: the
/// last under the turn's key (`live:fold:<turn>`, Working while it runs),
/// each earlier one under the turn's key and the message it ran before
/// (`live:fold:<turn>@<row>`), counted only. Settled, the turn's worked time
/// goes on the last part that has a work row. The live turn and history's
/// settled turns (`history_fold`) read the same through this.
#[allow(clippy::too_many_arguments)]
pub(crate) fn arrange_parts(
    turn_id: &str,
    turn: &Object,
    settled: bool,
    shown: &[&Object],
    at: &dyn Fn(&Object) -> Option<i64>,
    cuts: &[Cut],
    mut rows_of: HashMap<String, Entry>,
    options: &Options,
) -> Vec<Vec<Entry>> {
    let mut parts: Vec<Vec<&Object>> = vec![Vec::new(); cuts.len() + 1];
    let mut part = 0;
    for &item in shown {
        if let Some(began) = at(item) {
            part = part.max(cuts.iter().filter(|c| c.at_ms <= began).count());
        }
        parts[part].push(item);
    }
    let last = cuts.len();
    let mut timed = true;
    let mut arranged: Vec<Vec<Entry>> = Vec::with_capacity(parts.len());
    for (index, items) in parts.iter().enumerate().rev() {
        let key = match cuts.get(index) {
            Some(cut) => format!("live:fold:{turn_id}{PART}{}", cut.row),
            None => format!("live:fold:{turn_id}"),
        };
        // The working line goes on below the last message read while the
        // turn runs, even before anything runs after it.
        let working = !settled && index == last && last > 0;
        let entries = arrange_part(&key, turn, settled, index == last, timed, working, items, &mut rows_of, options);
        timed &= !entries.iter().any(|e| e.kind == Kind::Fold);
        arranged.push(entries);
    }
    arranged.reverse();
    arranged
}

/// A turn's item rows as one part, never split (`arrange_parts` without a
/// message read into it). Older `history_fold` builds call it this way.
#[allow(dead_code)]
pub(crate) fn arrange(turn_id: &str, turn: &Object, settled: bool, shown: &[&Object], rows_of: HashMap<String, Entry>, options: &Options) -> Vec<Entry> {
    let began = |item: &Object| int(item, "started_at_ms");
    arrange_parts(turn_id, turn, settled, shown, &began, &[], rows_of, options).into_iter().flatten().collect()
}

/// One part of a turn's item rows: one entry per item (an entry in `rows_of`
/// stands for its item), consecutive explore members as one, and when the
/// turn has settled its work folded (§7.6) under `key`. `last`: the part
/// that runs now (a running turn's Working row); `timed`: the part that
/// shows the worked time; `working`: a Working row even with nothing folded.
#[allow(clippy::too_many_arguments)]
fn arrange_part(key: &str, turn: &Object, settled: bool, last: bool, timed: bool, working: bool, shown: &[&Object], rows_of: &mut HashMap<String, Entry>, options: &Options) -> Vec<Entry> {
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

    // A settled turn explains nothing more: no Explaining… and no line
    // held for one.
    if settled {
        for entry in entries.iter_mut().filter(|e| e.secondary.is_empty()) {
            entry.explaining = false;
            entry.reserve_secondary = false;
        }
    }

    // A settled turn folds its work (§7.6, Arnie's ruling for every client, #653): its
    // reasoning, its tools and the commentary between them, opened in place with the
    // rest. What failed or stopped stays in its row, and the answer never folds. A
    // commentary that carried speech (it held a <speak> block: the user heard it) stays
    // too, above the work row. The row counts the calls it opens to, never the
    // summary's tool count; with no time and no folded call there is no row. A running
    // turn folds the same way when the reader groups it: `Working · N tools`, the
    // calls so far. Always visible keeps commentary in its place.
    if settled || options.fold_running {
        let spoken = |entry: &Entry| entry.kind == Kind::Message && entry.spoken && entry.phase == "commentary";
        let stays = |entry: &Entry| {
            matches!(entry.status.as_str(), "failed" | "interrupted")
                || (settled && matches!(entry.status.as_str(), "running" | "pending"))
                || (entry.kind == Kind::Message && (entry.phase != "commentary" || entry.spoken || options.show_commentary))
        };
        let folded: Vec<usize> = (0..entries.len()).filter(|i| !stays(&entries[*i])).collect();
        let calls: usize = folded.iter().map(|&i| match entries[i].kind {
            Kind::Tool => 1,
            Kind::Explore => entries[i].items.len(),
            _ => 0,
        }).sum();
        let had_calls = entries.iter().any(|e| matches!(e.kind, Kind::Tool | Kind::Explore));
        let worked_ms = if settled && timed { int(turn, "worked_ms").or_else(|| Some(int(turn, "ended_at_ms")? - int(turn, "started_at_ms")?)) } else { None };
        // A row when there is folded work with a time or a call, or when every call
        // failed and the time is known; never for a turn that used nothing. A
        // running turn has one while it has folded work. What ran before a
        // message the running turn read is counted, not timed: it is not Working.
        let running = !settled && last;
        let has_work = !folded.is_empty() || (settled && had_calls) || (working && options.fold_running);
        if let Some(title) = work_title(running, worked_ms, calls).filter(|_| has_work) {
            let key = key.to_owned();
            let open = options.expanded.contains(&key) && !folded.is_empty() && !(settled && options.hide_settled);
            let started_at_ms = folded.iter().filter_map(|i| entries[*i].started_at_ms).min();
            // While it runs the row says for how long, as the explorer's
            // "working 15s" does, from the turn's start by the Host's clock.
            let meta = match (running, int(turn, "started_at_ms").or(started_at_ms)) {
                (true, Some(started)) if options.host_now_ms > 0 => crate::time_format::turn_elapsed(options.host_now_ms - started),
                _ => String::new(),
            };
            let fold = Entry {
                items: folded.iter().flat_map(|i| entries[*i].items.clone()).collect(),
                rev: folded.iter().map(|i| entries[*i].rev).sum(),
                started_at_ms,
                meta,
                key,
                kind: Kind::Fold,
                status: if settled { text(turn, "status").to_owned() } else if running { "running".to_owned() } else { "completed".to_owned() },
                title,
                expandable: !folded.is_empty(),
                expanded: open,
                ..Entry::default()
            };
            // Hide when done: a finished turn's work row goes, and what it
            // stood for with it.
            let shows_fold = !(settled && options.hide_settled);
            // Above the row: what came before the work and every spoken commentary.
            // Below it, in their places: what stays and, opened, the folded rows too.
            let first_work = folded.first().copied()
                .or_else(|| entries.iter().position(|e| matches!(e.kind, Kind::Tool | Kind::Explore)))
                .unwrap_or(entries.len());
            let mut kept: Vec<Entry> = Vec::with_capacity(entries.len() + 1);
            let mut below: Vec<Entry> = Vec::new();
            for (i, entry) in entries.into_iter().enumerate() {
                let folds = folded.contains(&i);
                if i < first_work || (!folds && spoken(&entry)) {
                    kept.push(entry);
                } else if open || !folds {
                    below.push(entry);
                }
            }
            if shows_fold {
                kept.push(fold);
            }
            kept.append(&mut below);
            entries = kept;
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
        started_at_ms: int(item, "started_at_ms"),
        expanded,
        key,
        ..Entry::default()
    };
    match text(item, "kind") {
        "message" => {
            entry.kind = Kind::Message;
            // As `/log` rows show it: no voice markup, while it streams no
            // tag that is still arriving, and the spoken gist apart (#823).
            let streaming = matches!(status.as_str(), "running" | "pending" | "");
            (entry.spoken_text, entry.text) = crate::text::reply_parts(text(item, "text"), streaming);
            entry.phase = text(item, "phase").to_owned();
            entry.spoken = item.get("spoken").and_then(Value::as_bool).unwrap_or(false)
                || crate::text::has_speech(text(item, "text"));
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
        // A failed explanation (contract 55: with a `reason`) shows nothing.
        let state = explain.map(|e| text(e, "status")).unwrap_or_default();
        entry.secondary = explain.filter(|_| state != "failed").map(|e| text(e, "text")).unwrap_or_default().to_owned();
        // The line says Explaining… only while the Host has one pending;
        // none requested (null), failed or skipped shows nothing. A running
        // tool keeps the line so a late explanation lands without moving
        // the chat. A settled turn clears it (`present`).
        entry.explaining = entry.secondary.is_empty() && state == "pending";
        entry.reserve_secondary = entry.explaining || (!is_terminal(&status) && state != "failed");
    } else {
        entry.secondary = tool.get("command").and_then(Value::as_str).filter(|c| *c != label).unwrap_or_default().to_owned();
    }
    let tail: Vec<String> = output.and_then(|o| o.get("tail")).and_then(Value::as_array).into_iter().flatten().filter_map(Value::as_str).map(str::to_owned).collect();
    // A tool the agent's runtime refused: its reason is one quiet line
    // under the title, in plain words; the output box stays folded.
    if let Some(reason) = refusal(&tail.join("\n")) {
        entry.secondary = reason;
        entry.explaining = false;
        entry.reserve_secondary = false;
        entry.expandable = false;
        return;
    }
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

/// The reason a tool was refused, when `output` is the refusal the agent's
/// runtime writes in its tool result (`<tool_use_error>reason</tool_use_error>`):
/// the words inside, without the tags, on one line.
pub fn refusal(output: &str) -> Option<String> {
    let rest = output.trim().strip_prefix("<tool_use_error>")?;
    let inner = rest.trim_end().strip_suffix("</tool_use_error>").unwrap_or(rest);
    Some(inner.split_whitespace().collect::<Vec<_>>().join(" "))
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
        started_at_ms: members.iter().filter_map(|m| int(m, "started_at_ms")).min(),
        meta: if running { members.iter().filter_map(|m| int(m, "started_at_ms")).min().filter(|_| options.host_now_ms > 0).map(|s| clock(options.host_now_ms - s)).unwrap_or_default() } else { String::new() },
        ..Entry::default()
    }
}
