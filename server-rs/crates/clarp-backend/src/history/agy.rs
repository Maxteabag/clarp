//! What a conversation Antigravity kept says was said and done in it: the turns of its
//! transcript (`brain/<conversation>/.system_generated/logs/transcript.jsonl` under the
//! tool's own directory), for the conversation of an agent that takes it up.
//!
//! The transcript is a file of lines, one step of the conversation each, numbered: what the
//! user wrote (within a mark of the tool's), what the model planned (what it said, what it
//! thought, and the tools it called), what each tool gave back (the steps after the one that
//! called it, one a call), and what the tool itself noted (a task in the background that
//! ended). A turn here is one thing the user or the agent said, with the tools that were
//! used before it and what it thought, as a conversation shows them: a card for each tool,
//! and cells in the order the steps made them, with looking around folded into one.
//!
//! The rule is the Python Host's (`server/lib/agy_transcript.py`, `parse_turns`), held by
//! `tests/fixtures/agy-history-vectors.json`: also where it surprises. Tools of steps that
//! never said anything are shown with the next thing the agent says, and with nothing when
//! the user speaks first; a step of agy 1.1 is a tool by its kind. Two things are not
//! copied, and the vectors say so (`turns_mended`): the names agy gives its file tools
//! today are the tools they are (`crate::agy`), and what a command printed is cut at the
//! words agy writes before it today as well as at the ones it wrote.

use std::collections::HashMap;
use std::sync::LazyLock;

use clarp_kernel::python;
use clarp_kernel::tools::{self, cells};
use regex::Regex;
use serde_json::{Map, Value, json};

use super::rollout::without_preamble;
use crate::agy::tool_name;

fn pattern(text: &str) -> Regex {
    #[allow(clippy::expect_used)]
    Regex::new(text).expect("a pattern of this module is well formed")
}

/// The mark agy puts around what the user wrote.
static ASKED: LazyLock<Regex> = LazyLock::new(|| {
    pattern(r"(?is)<USER_REQUEST>[\s\x1c-\x1f]*(.*?)[\s\x1c-\x1f]*</USER_REQUEST>")
});
/// What agy writes before what a tool gave back: when it began, and when it was done.
static RESULT_HEAD: LazyLock<Regex> =
    LazyLock::new(|| pattern(r"\A(?:(?:Created|Completed) At:[^\n]*\n)*\n?"));
static EXIT: LazyLock<Regex> = LazyLock::new(|| pattern(r"exited with code (-?\d+)"));
/// A task agy went on with in the background, by the file it logs to.
static TASK: LazyLock<Regex> = LazyLock::new(|| pattern(r"tasks/(task-\d+)\.log"));
static TASK_END: LazyLock<Regex> =
    LazyLock::new(|| pattern(r"(?i)\b(finished|was canceled) with result:"));
/// What looks like a secret in what a tool was given.
static SECRET: LazyLock<Regex> = LazyLock::new(|| {
    pattern(
        r"(?i)(authorization|token|password|passwd|secret|api[_-]?key)[\s\x1c-\x1f]*[:=][\s\x1c-\x1f]*[^\s\x1c-\x1f]+",
    )
});

/// The kinds of step agy 1.1 wrote for its tools, by the names a conversation knows tools by.
const STEP_TOOLS: [(&str, &str); 11] = [
    ("RUN_COMMAND", "Bash"),
    ("LIST_DIRECTORY", "LS"),
    ("VIEW_FILE", "Read"),
    ("VIEW_CODE_ITEM", "Read"),
    ("GREP_SEARCH", "Grep"),
    ("FIND_FILES", "Glob"),
    ("EDIT_FILE", "Edit"),
    ("WRITE_FILE", "Write"),
    ("WRITE_TO_FILE", "Write"),
    ("SEARCH_WEB", "WebSearch"),
    ("READ_URL_CONTENT", "WebFetch"),
];
/// The kinds of step that are never a tool, whatever they hold.
const NO_TOOL: [&str; 4] = [
    "CONVERSATION_HISTORY",
    "SYSTEM_MESSAGE",
    "GENERIC",
    "ERROR_MESSAGE",
];
/// The words before what a command printed: the ones agy wrote, and the ones it writes.
const PRINTED_AFTER: [&str; 2] = ["Output:\n", "Command output:\n"];

/// The use of a tool, while the transcript is read: its card, and what only the reading
/// needs of it.
struct Used {
    card: Map<String, Value>,
    /// What agy called the tool. None for a step of agy 1.1, which is a tool by its kind.
    raw: Option<String>,
    /// What it gave back, whole, once something did.
    output: Option<String>,
}

impl Used {
    fn set(&mut self, field: &str, value: Value) {
        self.card.insert(field.to_string(), value);
    }

    fn status(&self) -> &str {
        self.card
            .get("status")
            .and_then(Value::as_str)
            .unwrap_or("")
    }
}

/// What a turn shows beside its tools' cards, in the order the steps made it: a thought, or
/// the use of a tool by its place among the uses.
enum Piece {
    Thought(Value),
    Tool(usize),
}

struct Turn {
    by_user: bool,
    text: String,
    at: String,
    tools: Vec<usize>,
    order: Vec<Piece>,
}

/// `str(value or "")`.
fn text(value: Option<&Value>) -> String {
    cells::text_of(&[value])
}

/// The number of a step, where it has one: a whole number, and to Python true and false
/// are the numbers one and zero.
fn step_of(row: &Map<String, Value>) -> Option<i64> {
    match row.get("step_index") {
        Some(Value::Number(number)) if number.is_i64() || number.is_u64() => number.as_i64(),
        Some(Value::Bool(yes)) => Some(i64::from(*yes)),
        _ => None,
    }
}

/// A text with every word begun with a capital and gone on in small letters (`str.title`).
pub(super) fn title(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut within = false;
    for sign in text.chars() {
        let cased = sign.is_lowercase() || sign.is_uppercase();
        if !cased {
            out.push(sign);
        } else if within {
            out.extend(sign.to_lowercase());
        } else {
            out.extend(sign.to_uppercase());
        }
        within = cased;
    }
    out
}

/// What the user wrote: what stands within the tool's mark, where it has one, without what
/// a Host wrote before it.
fn asked(content: Option<&Value>) -> String {
    let Some(Value::String(content)) = content else {
        return String::new();
    };
    let within = ASKED.captures(content).and_then(|found| found.get(1));
    let said = within.map_or(content.as_str(), |found| found.as_str());
    without_preamble(python::strip(said)).to_string()
}

/// A text agy wrote as JSON once more, as the text it is.
fn plain(value: &Value) -> Value {
    match value {
        Value::String(text)
            if text.chars().count() >= 2 && text.starts_with('"') && text.ends_with('"') =>
        {
            serde_json::from_str(text).unwrap_or_else(|_| value.clone())
        }
        other => other.clone(),
    }
}

/// What a tool was given, under the names the conversation's tools have, cut to what a
/// card shows and without what looks like a secret (`_canonical_tool_input`). The file of
/// a tool agy names as it does today stands under `Path`.
fn given(name: &str, raw: &Map<String, Value>) -> Value {
    let clean = |keys: &[&str], most: usize| -> String {
        let named: Vec<Option<&Value>> = keys.iter().map(|key| raw.get(*key)).collect();
        let said = cells::text_of(&named);
        let shown = SECRET.replace_all(python::first(&said, most), "${1}=[redacted]");
        shown.into_owned()
    };
    match name {
        "Bash" => json!({
            "command": clean(&["command", "CommandLine"], 180),
            "description": clean(&["description"], 120),
        }),
        "Read" | "Write" | "Edit" => {
            let places = [
                "file_path",
                "FilePath",
                "AbsolutePath",
                "TargetFile",
                "path",
                "Path",
            ];
            json!({ "file_path": clean(&places, 240) })
        }
        "Grep" | "Glob" => json!({
            "pattern": clean(&["pattern", "Query"], 100),
            "path": clean(&["path", "SearchPath"], 240),
        }),
        "LS" => json!({ "path": clean(&["path", "DirectoryPath"], 240) }),
        "WebSearch" | "WebFetch" => json!({
            "query": clean(&["query", "Query"], 130),
            "url": clean(&["url", "Url"], 180),
        }),
        _ => json!({}),
    }
}

/// The use of a tool a step called, before anything is known of what it gave back
/// (`_call_tool`). `answered_by` is the step that answers it, which is also what the tool's
/// stream names the use while it runs.
fn called(call: &Map<String, Value>, args: &Map<String, Value>, answered_by: i64) -> Used {
    let raw = match text(call.get("name")) {
        name if name.is_empty() => "tool".to_string(),
        name => name,
    };
    let args: Map<String, Value> = args
        .iter()
        .map(|(key, value)| (key.clone(), plain(value)))
        .collect();
    let mut name = tool_name(&raw);
    if name == raw {
        name = title(&raw.replace('_', " "));
    }
    let safe = given(&name, &args);
    let card = tools::card(&name, &safe, &format!("step-{answered_by}"));
    let mut used = Used {
        card: card.as_object().cloned().unwrap_or_default(),
        raw: Some(raw),
        output: None,
    };
    // agy's own words for the call, where it has any.
    if let Some(Value::String(own)) = args.get("toolSummary")
        && !python::strip(own).is_empty()
    {
        used.set("summary", json!(cells::clip(python::strip(own), 200)));
    }
    used.set("status", json!("running"));
    used.set("input", safe);
    used
}

/// A step of agy 1.1 that is a tool by its kind (`_tool_from`).
fn stepped(row: &Map<String, Value>, kind: &str) -> Used {
    let kind = if kind.is_empty() { "tool" } else { kind };
    let spaced = kind.replace('_', " ");
    let known = STEP_TOOLS.iter().find(|(theirs, _)| *theirs == kind);
    let name = known.map_or_else(|| title(&spaced), |(_, ours)| (*ours).to_string());
    let step = row
        .get("step_index")
        .map_or_else(|| "None".to_string(), cells::said);
    let failed = row.get("error").is_some_and(python::truthy);
    let mut card = Map::new();
    card.insert("id".to_string(), json!(format!("step-{step}")));
    card.insert("name".to_string(), json!(name));
    card.insert("summary".to_string(), json!(spaced.to_lowercase()));
    card.insert("action".to_string(), json!(name.to_lowercase()));
    card.insert("file_path".to_string(), json!(""));
    card.insert(
        "status".to_string(),
        json!(if failed { "error" } else { "ok" }),
    );
    if let Some(Value::String(content)) = row.get("content")
        && !content.is_empty()
    {
        card.insert("result".to_string(), json!(cells::clip(content, 300)));
    }
    Used {
        card,
        raw: None,
        output: None,
    }
}

/// What a tool gave back is put on its use: how the command ended, what it printed, and
/// whether it is still at work (`_settle`, cut at either of the words agy writes).
fn settle(used: &mut Used, content: &str, status: &str) {
    let body = RESULT_HEAD.replace(content, "");
    let mut body: &str = &body;
    let code = EXIT
        .captures(python::first(body, 300))
        .and_then(|found| python::int(&found[1]))
        .and_then(|code| i64::try_from(code).ok());
    used.set("exit_code", json!(code));
    let marks = PRINTED_AFTER
        .iter()
        .filter_map(|mark| body.find(mark).map(|at| (at, mark.len())));
    if let Some((at, size)) = marks.min() {
        body = &body[at + size..];
    }
    used.set("result", json!(cells::clip(python::strip(body), 300)));
    used.output = Some(body.to_string());
    let status = if status.to_uppercase() == "RUNNING" {
        "running"
    } else if code.is_some_and(|code| code != 0) {
        "error"
    } else {
        "ok"
    };
    used.set("status", json!(status));
}

/// What the model thought in a step, as the cell a conversation shows of reasoning
/// (`_thinking_cell`): eight paragraphs of it at most.
fn thought(said: Option<&Value>, step: Option<&Value>) -> Option<Value> {
    let Some(Value::String(said)) = said else {
        return None;
    };
    if python::strip(said).is_empty() {
        return None;
    }
    let shown = clarp_speech::markup::clean_for_display(said, false);
    let paragraphs = shown.split("\n\n").map(python::strip);
    let lines: Vec<Value> = paragraphs
        .filter(|paragraph| !paragraph.is_empty())
        .take(8)
        .map(|paragraph| cells::line(&cells::clip(paragraph, 600), "", "status"))
        .collect();
    if lines.is_empty() {
        return None;
    }
    let step = step.map_or_else(|| "0".to_string(), cells::said);
    let id = format!("agy-think-{step}");
    Some(cells::cell("reasoning", "Reasoned", "", "ok", &id, lines))
}

/// The cell of a tool's use, and what it looked at where looking is all it did and it did
/// not fail (`_cell`).
fn cell_of(used: &Used) -> (Value, Option<(String, String)>) {
    let id = text(used.card.get("id"));
    let status = used.status();
    let input = |field: &str| used.card.get("input").and_then(|given| given.get(field));
    match used.raw.as_deref() {
        Some("run_command") => {
            let command = text(input("command"));
            let output = json!(used.output.clone().unwrap_or_default());
            let code = used.card.get("exit_code").and_then(Value::as_i64);
            let cell = cells::command(&command, &id, Some(&output), code, status == "running");
            let looked = cells::explored(&command).filter(|_| status != "error");
            (cell, looked.map(|(label, at)| (label.to_string(), at)))
        }
        Some(raw @ ("view_file" | "list_dir" | "grep_search" | "find_by_name")) => {
            let target = cells::text_of(&[input("file_path"), input("path")]);
            let label = match raw {
                "view_file" => "Read",
                "list_dir" => "List",
                _ => "Search",
            };
            let cell = cells::cell("exploration", "Explored", "", status, &id, Vec::new());
            let looked = (status != "error").then(|| (label.to_string(), target));
            (cell, looked)
        }
        _ => {
            let mut payload = Map::new();
            payload.insert("call_id".to_string(), json!(id));
            let name = text(used.card.get("name"));
            (cells::generic(&name, &payload, status), None)
        }
    }
}

/// The cells of a turn with every run of looking around as one cell, a line for each thing
/// that was looked at (`opencode_transcript._coalesce_exploration`).
pub(super) fn gathered(cells_of_turn: Vec<(Value, Option<(String, String)>)>) -> Vec<Value> {
    fn one(run: &mut Vec<(Value, (String, String))>, out: &mut Vec<Value>) {
        if run.is_empty() {
            return;
        }
        let running = run.iter().any(|(cell, _)| cell["status"] == "running");
        let ids: Vec<String> = run.iter().map(|(cell, _)| text(cell.get("id"))).collect();
        let id = match ids.as_slice() {
            [only] => only.clone(),
            several => cells::safe_id("exploration", &several.join("|")),
        };
        let lines = run
            .iter()
            .map(|(_, (label, at))| cells::line(at, label, "detail"))
            .collect();
        let (title, status) = if running {
            ("Exploring", "running")
        } else {
            ("Explored", "ok")
        };
        out.push(cells::cell("exploration", title, "", status, &id, lines));
        run.clear();
    }
    let mut out = Vec::new();
    let mut run: Vec<(Value, (String, String))> = Vec::new();
    for (cell, looked) in cells_of_turn {
        match looked {
            Some(looked) => run.push((cell, looked)),
            None => {
                one(&mut run, &mut out);
                out.push(cell);
            }
        }
    }
    one(&mut run, &mut out);
    out
}

/// Whether a message names a task: by its place, and not as the beginning of the name of
/// another task.
fn names(content: &str, task: &str) -> bool {
    let wanted = format!("/{task}");
    content.match_indices(&wanted).any(|(at, _)| {
        let next = content[at + wanted.len()..].chars().next();
        !next.is_some_and(|sign| sign.is_alphanumeric() || sign == '_')
    })
}

/// A transcript, while it is read.
#[derive(Default)]
struct Reading {
    turns: Vec<Turn>,
    used: Vec<Used>,
    /// The tools and the thoughts of steps that said nothing yet: they ride on the next
    /// thing the agent says.
    waiting: Vec<usize>,
    waiting_order: Vec<Piece>,
    /// The commands that went on in the background, by their tasks, until agy says how each
    /// ended.
    background: Vec<(String, usize)>,
}

impl Reading {
    /// What waits is put on the last thing the agent said, where that is the last thing
    /// said at all. Where the user spoke last, only the tools of agy 1.1 make a turn of
    /// their own; a call of a later agy never does, and is not shown
    /// (`_flush_tools_onto_assistant`).
    fn flush(&mut self, at: &str) {
        if self.waiting.is_empty() && self.waiting_order.is_empty() {
            return;
        }
        match self.turns.last_mut().filter(|last| !last.by_user) {
            Some(last) => {
                last.tools.append(&mut self.waiting);
                last.order.append(&mut self.waiting_order);
            }
            None => {
                let used = &self.used;
                let older = self.waiting.iter().copied();
                let older: Vec<usize> = older.filter(|at| used[*at].raw.is_none()).collect();
                if !older.is_empty() {
                    self.turns.push(Turn {
                        by_user: false,
                        text: String::new(),
                        at: at.to_string(),
                        tools: older,
                        order: Vec::new(),
                    });
                }
            }
        }
        self.waiting.clear();
        self.waiting_order.clear();
    }

    /// A message of the tool itself says how tasks in the background ended: one finished or
    /// was cancelled, or all of them stopped when the tool was started again
    /// (`_settle_background`).
    fn ended_in_the_background(&mut self, content: &str) {
        if content.contains("background tasks have been stopped") {
            for (_, at) in std::mem::take(&mut self.background) {
                self.used[at].set("status", json!("error"));
                self.used[at].set("result", json!("Stopped when Antigravity restarted"));
            }
            return;
        }
        let Some(end) = TASK_END.captures(content) else {
            return;
        };
        let finished = end[1].to_lowercase() == "finished";
        let after = &content[end.get(0).map_or(content.len(), |whole| whole.end())..];
        let cuts = ["\nLog: ", "</SYSTEM_MESSAGE>"];
        let cut = cuts.iter().filter_map(|mark| after.find(mark)).min();
        let body = &after[..cut.unwrap_or(after.len())];
        let mut left = Vec::new();
        for (task, at) in std::mem::take(&mut self.background) {
            if !names(content, &task) {
                left.push((task, at));
            } else if finished {
                settle(&mut self.used[at], body, "");
            } else {
                let said = match python::strip(body) {
                    "" => "Canceled",
                    said => said,
                };
                self.used[at].set("status", json!("error"));
                self.used[at].set("result", json!(cells::clip(said, 300)));
            }
        }
        self.background = left;
    }

    /// A step in which the model planned: the tools it called, each with the step that
    /// answers it, what it thought, and what it said.
    fn planned(
        &mut self,
        row: &Map<String, Value>,
        rows: &[Map<String, Value>],
        by_step: &HashMap<i64, usize>,
        last_asked: i64,
        at: String,
    ) {
        let said = match row.get("content") {
            Some(Value::String(said)) => python::strip(said).to_string(),
            _ => String::new(),
        };
        let step = step_of(row).unwrap_or(0);
        let calls = row.get("tool_calls").and_then(Value::as_array);
        let mut called_here = Vec::new();
        for (number, call) in calls.map_or(&[][..], Vec::as_slice).iter().enumerate() {
            let answered_by = step + 1 + i64::try_from(number).unwrap_or(i64::MAX);
            let answer = by_step.get(&answered_by).map(|at| &rows[*at]);
            let answer_kind = answer.map(|answer| text(answer.get("type")));
            let answer_kind = answer_kind.unwrap_or_default();
            let args = call.get("args").and_then(Value::as_object);
            // agy 1.1 wrote each call as a step of its own kind as well, with or without
            // what it was given: such a call is not counted twice.
            let (Some(call), Some(args)) = (call.as_object(), args) else {
                continue;
            };
            if STEP_TOOLS.iter().any(|(theirs, _)| *theirs == answer_kind) {
                continue;
            }
            let mut used = called(call, args, answered_by);
            let place = self.used.len();
            match answer.filter(|_| answer_kind == "GENERIC") {
                Some(answer) => {
                    let gave = text(answer.get("content"));
                    settle(&mut used, &gave, &text(answer.get("status")));
                    // Handed to a task in the background: how it ends is said later.
                    let output = used.output.as_deref().unwrap_or("");
                    if used.status() == "running"
                        && let Some(task) = TASK.captures(output)
                    {
                        let task = task[1].to_string();
                        match self.background.iter_mut().find(|(known, _)| *known == task) {
                            Some(known) => known.1 = place,
                            None => self.background.push((task, place)),
                        }
                    }
                }
                // The conversation went on without an answer: a turn that was cancelled.
                None if answered_by < last_asked => {
                    used.set("status", json!("error"));
                    used.set("result", json!("No result was recorded"));
                }
                None => {}
            }
            self.used.push(used);
            called_here.push(place);
        }
        if let Some(cell) = thought(row.get("thinking"), row.get("step_index")) {
            self.waiting_order.push(Piece::Thought(cell));
        }
        self.waiting_order
            .extend(called_here.iter().map(|place| Piece::Tool(*place)));
        self.waiting.append(&mut called_here);
        // One turn of the agent for each step that says something; what steps without
        // words thought and called rides on the next step that has some.
        if !said.is_empty() {
            self.turns.push(Turn {
                by_user: false,
                text: said,
                at,
                tools: std::mem::take(&mut self.waiting),
                order: std::mem::take(&mut self.waiting_order),
            });
        }
    }

    /// The turns as a conversation has them (`_finish_tools`).
    fn turns(self) -> Vec<Value> {
        let used = &self.used;
        let shown = |turn: Turn| {
            let role = if turn.by_user { "user" } else { "assistant" };
            let cards: Vec<Value> = turn
                .tools
                .iter()
                .map(|at| Value::Object(used[*at].card.clone()))
                .collect();
            let mut out = json!({
                "role": role, "text": turn.text, "tools": cards, "timestamp": turn.at,
            });
            if !turn.order.is_empty() {
                let cells_of_turn = turn.order.into_iter().map(|piece| match piece {
                    Piece::Thought(cell) => (cell, None),
                    Piece::Tool(at) => cell_of(&used[at]),
                });
                out["display_cells"] = Value::Array(gathered(cells_of_turn.collect()));
            }
            out
        };
        self.turns.into_iter().map(shown).collect()
    }
}

/// The turns a transcript of Antigravity holds, the oldest first: for each who spoke, what
/// was said, the tools that were used with it, each as a card, when, and the cells a
/// conversation shows of what was thought and done.
pub fn agy(transcript: &str) -> Vec<Value> {
    let mut rows: Vec<Map<String, Value>> = Vec::new();
    for line in transcript.lines() {
        if let Ok(Value::Object(row)) = serde_json::from_str::<Value>(python::strip(line)) {
            rows.push(row);
        }
    }
    // By the numbers of the steps, whatever order they were written in; a step without a
    // number counts as the first, after what is there.
    rows.sort_by_key(|row| step_of(row).unwrap_or(0));
    let mut by_step: HashMap<i64, usize> = HashMap::new();
    for (at, row) in rows.iter().enumerate() {
        if let Some(step) = step_of(row) {
            by_step.insert(step, at);
        }
    }
    let asked_at = by_step
        .iter()
        .filter(|(_, at)| rows[**at].get("type") == Some(&json!("USER_INPUT")));
    let last_asked = asked_at.map(|(step, _)| *step).max().unwrap_or(-1);

    let mut reading = Reading::default();
    for row in &rows {
        let kind = text(row.get("type"));
        let at = text(row.get("created_at"));
        match kind.as_str() {
            "USER_INPUT" => {
                reading.flush(&at);
                let said = asked(row.get("content"));
                if !said.is_empty() {
                    reading.turns.push(Turn {
                        by_user: true,
                        text: said,
                        at,
                        tools: Vec::new(),
                        order: Vec::new(),
                    });
                }
            }
            "PLANNER_RESPONSE" => reading.planned(row, &rows, &by_step, last_asked, at),
            "SYSTEM_MESSAGE" if !reading.background.is_empty() => {
                reading.ended_in_the_background(&text(row.get("content")));
            }
            kind => {
                let content = row.get("content").filter(|content| python::truthy(content));
                let headed = !NO_TOOL.contains(&kind)
                    && content.is_some_and(|content| cells::said(content).contains("Created At:"));
                if headed || STEP_TOOLS.iter().any(|(theirs, _)| *theirs == kind) {
                    let place = reading.used.len();
                    reading.used.push(stepped(row, kind));
                    reading.waiting.push(place);
                }
            }
        }
    }
    let last = rows.last().map(|row| text(row.get("created_at")));
    reading.flush(&last.unwrap_or_default());
    reading.turns()
}
