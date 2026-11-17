//! The cells a conversation of Codex shows of what its agent did.
//!
//! Codex has no messages that hold their tool calls: a command, an edit, a search of the
//! web, a tool of a server and an agent it starts itself each come as a thing of their own.
//! A cell is how one of them is shown, as a terminal would show it: "Ran cargo test" with
//! the first and the last lines it printed, "Edited" with the files and the lines that
//! changed, "Explored" for what only looked at things. A cell has an id, a kind, a title, a
//! summary, a status and lines; a line has a label, a text and a kind.
//!
//! This is the Python Host's `server/lib/codex_tool_cells.py`, held to it through the
//! reader that uses it (`clarp-backend`, `history::codex`) by
//! `tests/fixtures/codex-history-vectors.json`, which `server-rs/tools/codex-history-vectors.py`
//! writes. The words of a command and what only looks at things are read by this module's
//! parent, as that file's own functions read them.

use std::sync::LazyLock;

use regex::Regex;
use serde_json::{Map, Value, json};

use super::{exploration, strip_shell_wrapper};
use crate::python;

/// How many lines of what a tool printed a cell shows.
const OUTPUT_LINES: usize = 5;
/// How long a line of what a tool printed may be.
const OUTPUT_LINE_SIGNS: usize = 220;
/// No line of any cell is longer than this.
const LINE_SIGNS_MOST: usize = 2_000;
/// No cell has more lines than this.
const LINES_MOST: usize = 600;
/// What the lines of a cell may weigh together, each counted with 64 beyond its signs.
const WEIGHT_MOST: usize = 64 * 1024;

/// What is no letter, no number, no dash and no underscore, as Python's `str.isalnum` tells
/// letters and numbers: marks and symbols are neither.
static NOT_FOR_AN_ID: LazyLock<Regex> = LazyLock::new(|| pattern(r"[^\p{L}\p{N}_-]"));
static NESTED_TOOL: LazyLock<Regex> =
    LazyLock::new(|| pattern(r"\btools\.([A-Za-z_][A-Za-z0-9_]*)[\s\x1c-\x1f]*\("));
static JSON_COMMAND: LazyLock<Regex> = LazyLock::new(|| {
    pattern(
        r#"(?:"(?:cmd|command)"|\b(?:cmd|command))[\s\x1c-\x1f]*:[\s\x1c-\x1f]*("(?:\\.|[^"\\])*")"#,
    )
});

fn pattern(text: &str) -> Regex {
    #[allow(clippy::expect_used)]
    Regex::new(text).expect("a pattern of this module is well formed")
}

// ---- Python's ways with a value -----------------------------------------------------------

/// A value as Python's `str()` writes it: a text as it is, anything else as it is shown.
pub fn said(value: &Value) -> String {
    match value {
        Value::String(text) => text.clone(),
        other => python::repr(other),
    }
}

/// `a or b or ...`: the first of the values Python holds true, and otherwise the last one
/// that was looked at. `None` is Python's `None`: a field that is not there, or null.
pub fn any_of<'a>(values: &[Option<&'a Value>]) -> Option<&'a Value> {
    let mut last = None;
    for value in values {
        last = value.filter(|value| !value.is_null());
        if last.is_some_and(python::truthy) {
            return last;
        }
    }
    last
}

/// `a or b or ...` where what Python holds false counts as nothing.
pub fn some_of<'a>(values: &[Option<&'a Value>]) -> Option<&'a Value> {
    any_of(values).filter(|value| python::truthy(value))
}

/// `str(a or b or ... or "")`.
pub fn text_of(values: &[Option<&Value>]) -> String {
    some_of(values).map(said).unwrap_or_default()
}

/// A value that names one thing or several, as a list: a text is a list of one, and what
/// Python holds false, or anything else, is an empty one.
fn listed(value: Option<&Value>) -> Vec<Value> {
    match value {
        Some(Value::String(one)) => vec![Value::from(one.as_str())],
        Some(Value::Array(several)) => several.clone(),
        _ => Vec::new(),
    }
}

/// The first `count` signs of a text.
fn head(text: &str, count: usize) -> String {
    text.chars().take(count).collect()
}

fn signs(text: &str) -> usize {
    text.chars().count()
}

/// A text cut to `most` signs, with a sign that it was cut (`text_util.truncate`).
pub fn clip(text: &str, most: usize) -> String {
    if signs(text) <= most {
        text.to_string()
    } else {
        format!("{}…", head(text, most))
    }
}

/// A text with one space wherever it had space of any kind, and none around it.
fn one_line(text: &str) -> String {
    let words: Vec<&str> = text
        .split(python::blank)
        .filter(|word| !word.is_empty())
        .collect();
    words.join(" ")
}

/// A text with its first sign a capital and the rest small (`str.capitalize`).
fn capitalized(text: &str) -> String {
    let mut signs = text.chars();
    match signs.next() {
        Some(first) => first
            .to_uppercase()
            .chain(signs.flat_map(char::to_lowercase))
            .collect(),
        None => String::new(),
    }
}

/// A text with a capital at the start of each of its words (`str.title`): a sign that
/// follows a letter is made small, any other letter a capital.
fn titled(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut after_a_letter = false;
    for sign in text.chars() {
        if after_a_letter {
            out.extend(sign.to_lowercase());
        } else {
            out.extend(sign.to_uppercase());
        }
        after_a_letter = sign.is_uppercase() || sign.is_lowercase();
    }
    out
}

/// A value that is an object, or a text that says one (`_json_obj`).
pub fn object_of(value: Option<&Value>) -> Map<String, Value> {
    match value {
        Some(Value::Object(fields)) => fields.clone(),
        Some(Value::String(text)) => match serde_json::from_str::<Value>(text) {
            Ok(Value::Object(fields)) => fields,
            _ => Map::new(),
        },
        _ => Map::new(),
    }
}

/// A name in small letters with underscores, whatever way it was written (`_token`):
/// `spawnAgent`, `spawn-agent` and `Spawn Agent` are all `spawn_agent`.
pub fn token(value: Option<&Value>) -> String {
    let raw: Vec<char> = text_of(&[value]).chars().collect();
    let mut out = String::new();
    for (at, sign) in raw.iter().enumerate() {
        if matches!(sign, '-' | ' ') {
            out.push('_');
        } else if sign.is_uppercase() && at > 0 && !matches!(raw[at - 1], '_' | '-' | ' ') {
            out.push('_');
            out.extend(sign.to_lowercase());
        } else {
            out.extend(sign.to_lowercase());
        }
    }
    out.trim_matches('_').to_string()
}

/// The same of a name that is a text already.
pub fn token_of(name: &str) -> String {
    token(Some(&Value::from(name)))
}

/// The value of the first of the fields that is there, whatever it holds (`_get_any`).
fn field<'a>(fields: &'a Map<String, Value>, names: &[&str]) -> Option<&'a Value> {
    names.iter().find_map(|name| fields.get(*name))
}

fn object(value: &Value) -> Option<&Map<String, Value>> {
    value.as_object()
}

// ---- a line and a cell ---------------------------------------------------------------------

/// A line of a cell.
pub fn line(text: &str, label: &str, kind: &str) -> Value {
    json!({ "label": label, "text": text, "kind": kind })
}

fn detail(text: &str, label: &str) -> Value {
    line(text, label, "detail")
}

/// A cell. One that is given no id is known by its kind and what it is about.
pub fn cell(
    kind: &str,
    title: &str,
    summary: &str,
    status: &str,
    id: &str,
    lines: Vec<Value>,
) -> Value {
    let id = if id.is_empty() {
        safe_id(kind, if summary.is_empty() { title } else { summary })
    } else {
        id.to_string()
    };
    json!({
        "id": id, "kind": kind, "title": title, "summary": summary, "status": status,
        "lines": lines,
    })
}

/// An id made of a text: what is no letter, number, dash or underscore becomes a dash, and
/// eighty signs of it are kept.
pub fn safe_id(prefix: &str, value: &str) -> String {
    if value.is_empty() {
        return prefix.to_string();
    }
    let clean = NOT_FOR_AN_ID.replace_all(value, "-");
    format!("{prefix}-{}", head(&clean, 80))
}

/// Puts a cell where the one with its id is, or after the others.
pub fn replace_or_append(turn: &mut Value, new: Value) {
    if !turn["display_cells"].is_array() {
        turn["display_cells"] = json!([]);
    }
    let Some(cells) = turn["display_cells"].as_array_mut() else {
        return;
    };
    let id = &new["id"];
    if python::truthy(id)
        && let Some(held) = cells.iter_mut().find(|held| held.get("id") == Some(id))
    {
        *held = new;
        return;
    }
    cells.push(new);
}

// ---- what a tool printed -------------------------------------------------------------------

/// What a tool gave back, as a text: its parts one after the other, or the value written
/// out; without the head a command's answer has before what it printed (`_clean_tool_output`).
pub fn clean_output(output: &Value) -> String {
    let text = match output {
        Value::Array(parts) => {
            let texts: Vec<&str> = parts
                .iter()
                .filter_map(|part| match part {
                    Value::Object(fields) => fields.get("text").and_then(Value::as_str),
                    Value::String(text) => Some(text.as_str()),
                    _ => None,
                })
                .collect();
            if texts.is_empty() {
                python::dumps_ascii(output)
            } else {
                texts.concat()
            }
        }
        Value::String(text) => text.clone(),
        other => python::dumps_ascii(other),
    };
    match text.split_once("\nOutput:\n") {
        Some((_, printed)) => printed.to_string(),
        None => text,
    }
}

/// The code a command ended with, where its answer says one (`_tool_output_exit_code`).
pub fn exit_code_in(output: &Value) -> Option<i64> {
    let (_, after) = output.as_str()?.split_once("\nProcess exited with code ")?;
    let first = python::splitlines(after).into_iter().next()?;
    python::int(first).and_then(|code| i64::try_from(code).ok())
}

fn output_line(text: &str) -> String {
    if signs(text) <= OUTPUT_LINE_SIGNS {
        return text.to_string();
    }
    let cut = head(text, OUTPUT_LINE_SIGNS - 1);
    format!("{}…", cut.trim_end_matches(python::blank))
}

/// The first and the last lines of what a tool printed, with how many are left out between
/// them (`_preview_output`).
pub fn preview(output: &Value, limit: usize) -> Vec<Value> {
    let text = clean_output(output);
    if text.is_empty() {
        return Vec::new();
    }
    let all = python::splitlines(&text);
    let (first, last) = if all.len() <= limit {
        (all.len(), 0)
    } else {
        let first = (limit / 2).max(1);
        (first, limit.saturating_sub(first + 1).max(1))
    };
    let shown = |text: &&str| line(&output_line(text), "", "output");
    let mut lines: Vec<Value> = all[..first].iter().map(shown).collect();
    let omitted = all.len() - first - last;
    if omitted > 0 {
        let said = format!("… +{omitted} lines (full transcript in terminal)");
        lines.push(line(&said, "", "omitted"));
    }
    lines.extend(all[all.len() - last..].iter().map(shown));
    lines
}

/// A cell within its bounds: no line too long, not too many lines, not too much in all
/// (`_cap_cell`).
pub fn capped(cell: &Value) -> Value {
    let Some(lines) = cell["lines"].as_array().filter(|lines| !lines.is_empty()) else {
        return cell.clone();
    };
    let mut kept: Vec<Value> = Vec::new();
    let mut weight = 0;
    let (mut changed, mut cut_short) = (false, false);
    for held in lines {
        if kept.len() >= LINES_MOST {
            cut_short = true;
            break;
        }
        let mut text = text_of(&[held.get("text")]);
        let mut held = held.clone();
        if signs(&text) > LINE_SIGNS_MOST {
            let cut = head(&text, LINE_SIGNS_MOST - 1);
            text = format!("{}…", cut.trim_end_matches(python::blank));
            held["text"] = Value::from(text.clone());
            changed = true;
        }
        weight += signs(&text) + 64;
        if weight > WEIGHT_MOST {
            cut_short = true;
            break;
        }
        kept.push(held);
    }
    if !changed && !cut_short {
        return cell.clone();
    }
    if cut_short {
        let said = "… output truncated (full transcript in terminal)";
        kept.push(line(said, "", "omitted"));
    }
    let mut cell = cell.clone();
    cell["lines"] = Value::Array(kept);
    cell
}

// ---- a command -----------------------------------------------------------------------------

/// The command a call was given, where its arguments name one (`_exec_command_str`).
pub fn command_of(arguments: Option<&Value>) -> String {
    let parsed;
    let arguments = match arguments {
        Some(Value::String(text)) => match serde_json::from_str::<Value>(text) {
            Ok(value) => {
                parsed = value;
                &parsed
            }
            Err(_) => return clip(text, 400),
        },
        Some(other) => other,
        None => return String::new(),
    };
    let Some(fields) = arguments.as_object() else {
        return String::new();
    };
    match any_of(&[fields.get("cmd"), fields.get("command")]) {
        Some(Value::Array(words)) => {
            let words: Vec<String> = words.iter().map(said).collect();
            clip(&words.join(" "), 400)
        }
        Some(Value::String(command)) => clip(command, 400),
        _ => String::new(),
    }
}

/// The cell of a command: what ran, and what it printed (`_command_cell`). `output` is
/// `None` where nothing is known of what it printed yet.
pub fn command(
    command: &str,
    id: &str,
    output: Option<&Value>,
    exit_code: Option<i64>,
    running: bool,
) -> Value {
    let display = strip_shell_wrapper(command);
    let status = match (running, exit_code) {
        (true, _) => "running",
        (false, None | Some(0)) => "ok",
        (false, Some(_)) => "error",
    };
    let mut lines = match output {
        Some(output) if python::truthy(output) => preview(output, OUTPUT_LINES),
        _ => Vec::new(),
    };
    if output.is_some_and(|output| output == "") && !running {
        lines = vec![line("(no output)", "", "muted")];
    }
    let id = if id.is_empty() {
        safe_id("command", &display)
    } else {
        id.to_string()
    };
    cell(
        "command",
        if running { "Running" } else { "Ran" },
        &display,
        status,
        &id,
        lines,
    )
}

/// What a command looks at, when looking is all it does: a line with `Read`, `List` or
/// `Search` for its label (`_classify_exploration`).
pub fn explored(command: &str) -> Option<(&'static str, String)> {
    exploration(command)
}

// ---- an edit -------------------------------------------------------------------------------

fn change_label(kind: &str) -> &'static str {
    match kind.to_lowercase().as_str() {
        "add" | "create" => "Add",
        "delete" | "remove" => "Delete",
        _ => "Edit",
    }
}

/// A line of a diff as a line of a cell, where it is one a reader is shown.
fn diff_line(raw: &str) -> Option<Value> {
    let kind = if raw.starts_with("@@") {
        "diff_header"
    } else if raw.starts_with('+') && !raw.starts_with("+++") {
        "diff_new"
    } else if raw.starts_with('-') && !raw.starts_with("---") {
        "diff_old"
    } else if raw.starts_with(' ') {
        "diff_context"
    } else {
        return None;
    };
    Some(line(raw, "", kind))
}

/// The cell of a change to files, in each form Codex writes one: the files by their paths,
/// a list of changes, or the text of a patch (`_patch_cell`).
pub fn patch(id: &str, given: &Value, status: &str) -> Value {
    let edited =
        |id: String, lines: Vec<Value>| cell("patch", "Edited", "apply_patch", status, &id, lines);
    let or_made = |of: &str| {
        if id.is_empty() {
            safe_id("patch", of)
        } else {
            id.to_string()
        }
    };
    let mut given = given;
    if let Value::Object(fields) = given {
        let nested = any_of(&[
            fields.get("changes"),
            fields.get("patch"),
            fields.get("input"),
        ]);
        if let Some(nested) = nested {
            given = nested;
        } else {
            // The files by their paths, each with how it changed and its diff.
            let mut lines = Vec::new();
            for (path, change) in fields {
                let Some(change) = change.as_object() else {
                    continue;
                };
                let kind = text_of(&[change.get("type"), change.get("kind")]);
                let kind = if kind.is_empty() { "update" } else { &kind };
                lines.push(detail(path, change_label(kind)));
                let diff = text_of(&[change.get("unified_diff"), change.get("diff")]);
                lines.extend(python::splitlines(&diff).into_iter().filter_map(diff_line));
            }
            return edited(or_made("changes"), lines);
        }
    }
    if let Value::Array(changes) = given {
        let mut lines = Vec::new();
        for change in changes.iter().filter_map(object) {
            let path = text_of(&[change.get("path")]);
            let kind = text_of(&[change.get("kind")]);
            let kind = if kind.is_empty() { "update" } else { &kind };
            if !path.is_empty() {
                lines.push(detail(&path, change_label(kind)));
            }
        }
        return edited(or_made(&head(&python::dumps_ascii(given), 80)), lines);
    }
    let text = given.as_str().unwrap_or_default();
    let mut lines = Vec::new();
    for raw in python::splitlines(text) {
        let named = [
            ("*** Add File: ", "Add"),
            ("*** Update File: ", "Edit"),
            ("*** Delete File: ", "Delete"),
        ];
        let file = named
            .iter()
            .find_map(|(mark, label)| Some((raw.strip_prefix(mark)?, *label)));
        match file {
            Some((path, label)) => lines.push(detail(path, label)),
            None => lines.extend(diff_line(raw)),
        }
    }
    if lines.is_empty() && !text.is_empty() {
        lines = preview(given, 3);
    }
    edited(or_made(&head(text, 80)), lines)
}

/// The files a patch names, each once (`_patch_file_paths`).
fn patch_paths(text: &str) -> Vec<&str> {
    let mut paths: Vec<&str> = Vec::new();
    for raw in python::splitlines(text) {
        let marks = ["*** Add File: ", "*** Update File: ", "*** Delete File: "];
        if let Some(path) = marks.iter().find_map(|mark| raw.strip_prefix(mark))
            && !paths.contains(&path)
        {
            paths.push(path);
        }
    }
    paths
}

/// What a patch takes out and puts in, hunk by hunk; a hunk that changes nothing is left
/// out (`_patch_edits`).
fn patch_edits(text: &str) -> Vec<Value> {
    let mut edits = Vec::new();
    let (mut old, mut new): (Vec<&str>, Vec<&str>) = (Vec::new(), Vec::new());
    let mut flush = |old: &mut Vec<&str>, new: &mut Vec<&str>| {
        if !old.is_empty() || !new.is_empty() {
            let (was, is) = (old.join("\n"), new.join("\n"));
            if was != is {
                edits.push(json!({ "old": was, "new": is }));
            }
        }
        old.clear();
        new.clear();
    };
    for raw in python::splitlines(text) {
        if raw.starts_with("*** ") || raw.starts_with("@@") {
            flush(&mut old, &mut new);
        } else if raw.starts_with('+') && !raw.starts_with("+++") {
            new.push(&raw[1..]);
        } else if raw.starts_with('-') && !raw.starts_with("---") {
            old.push(&raw[1..]);
        } else if let Some(context) = raw.strip_prefix(' ') {
            old.push(context);
            new.push(context);
        }
    }
    flush(&mut old, &mut new);
    edits
}

// ---- other tools ---------------------------------------------------------------------------

/// The cell of a tool nothing more is known of than its name (`_generic_tool_cell`).
pub fn generic(name: &str, payload: &Map<String, Value>, status: &str) -> Value {
    let id = text_of(&[payload.get("call_id"), payload.get("id")]);
    let id = if id.is_empty() {
        safe_id("tool", name)
    } else {
        id
    };
    let title = if status == "running" {
        "Calling"
    } else {
        "Called"
    };
    cell("tool", title, name, status, &id, Vec::new())
}

/// The cell of a tool of a server: how it was called, and what came back (`_mcp_cell`).
pub fn mcp(item: &Map<String, Value>, running: bool) -> Value {
    let id = text_of(&[item.get("id"), item.get("call_id")]);
    let server = text_of(&[item.get("server")]);
    let tool = text_of(&[item.get("tool"), item.get("name")]);
    let tool = if tool.is_empty() { "tool" } else { &tool };
    let given = match item.get("arguments") {
        None | Some(Value::Null) => String::new(),
        Some(Value::String(text)) if text.is_empty() => String::new(),
        Some(Value::Object(fields)) if fields.is_empty() => String::new(),
        Some(arguments) => python::compact_ascii(arguments),
    };
    let dot = if server.is_empty() { "" } else { "." };
    let invocation = format!("{server}{dot}{tool}({given})");
    let error = item.get("error").filter(|error| python::truthy(error));
    let lines = match (error, item.get("result")) {
        (Some(Value::Object(error)), _) => {
            vec![line(&text_of(&[error.get("message")]), "", "error")]
        }
        (Some(error), _) => vec![line(&said(error), "", "error")],
        (None, None | Some(Value::Null)) => Vec::new(),
        (None, Some(Value::String(text))) if text.is_empty() => Vec::new(),
        (None, Some(result)) => preview(result, OUTPUT_LINES),
    };
    let status = match (running, error) {
        (true, _) => "running",
        (false, Some(_)) => "error",
        (false, None) => "ok",
    };
    let id = if id.is_empty() {
        safe_id("mcp", &invocation)
    } else {
        id
    };
    cell(
        "mcp",
        if running { "Calling" } else { "Called" },
        &invocation,
        status,
        &id,
        lines,
    )
}

/// The cell of a search of the web (`_web_search_cell`). `query` is what was asked, of any
/// kind: what is nothing is shown as "web".
pub fn web_search(id: &str, query: Option<&Value>, running: bool) -> Value {
    let asked = query.filter(|query| python::truthy(query)).map(said);
    let summary = asked.unwrap_or_else(|| "web".to_string());
    let id = match (id.is_empty(), query) {
        (false, _) => id.to_string(),
        (true, Some(query)) => safe_id("web", &said(query)),
        (true, None) => safe_id("web", "None"),
    };
    let (title, status) = if running {
        ("Searching", "running")
    } else {
        ("Searched", "ok")
    };
    cell("web_search", title, &summary, status, &id, Vec::new())
}

// ---- the agents Codex starts itself --------------------------------------------------------

/// A status as a word and what it says, in each way Codex writes one: a name, or a name
/// with a message (`_status_text`).
fn status_text(status: Option<&Value>) -> (String, String) {
    match status {
        Some(Value::Object(fields)) if !fields.is_empty() => {
            let Some((name, message)) = fields.iter().next() else {
                return ("Recorded".to_string(), String::new());
            };
            let word = capitalized(&name.replace('_', " "));
            match message
                .as_str()
                .filter(|message| !python::strip(message).is_empty())
            {
                Some(message) => (word, clip(&one_line(message), 240)),
                None => (word, String::new()),
            }
        }
        Some(Value::String(_)) => (capitalized(&token(status).replace('_', " ")), String::new()),
        _ => ("Recorded".to_string(), String::new()),
    }
}

/// What a status makes of a cell: `error`, `running`, `ok` or `recorded` (`_status_cell_state`).
fn status_state(status: Option<&Value>, call_status: Option<&Value>) -> &'static str {
    let key = match status {
        Some(Value::Object(fields)) => fields
            .keys()
            .next()
            .map(|name| token_of(name))
            .unwrap_or_default(),
        Some(Value::String(_)) => token(status),
        _ => String::new(),
    };
    let call = token(call_status);
    if matches!(key.as_str(), "errored" | "error" | "failed" | "not_found") || call == "failed" {
        "error"
    } else if matches!(key.as_str(), "running" | "pending_init")
        || matches!(call.as_str(), "in_progress" | "running")
    {
        "running"
    } else if matches!(key.as_str(), "completed" | "shutdown" | "interrupted")
        || call == "completed"
    {
        "ok"
    } else {
        "recorded"
    }
}

/// How an agent is named: by its nickname and its role, or by the start of its thread
/// (`_agent_label`).
fn agent_label(thread: Option<&Value>, nickname: Option<&Value>, role: Option<&Value>) -> String {
    let nickname = text_of(&[nickname]);
    let role = text_of(&[role]);
    let (nickname, role) = (python::strip(&nickname), python::strip(&role));
    match (nickname.is_empty(), role.is_empty()) {
        (false, false) => format!("{nickname} [{role}]"),
        (false, true) => nickname.to_string(),
        (true, false) => format!("[{role}]"),
        (true, true) => {
            let thread = text_of(&[thread]);
            let thread = python::strip(&thread);
            if thread.is_empty() {
                "Agent".to_string()
            } else {
                head(thread, 8)
            }
        }
    }
}

/// A line for each agent of a map of threads to their states (`_agent_state_lines`).
fn agent_state_lines(states: Option<&Value>) -> Vec<Value> {
    let Some(states) = states.and_then(Value::as_object) else {
        return Vec::new();
    };
    let mut threads: Vec<(&String, &Value)> = states.iter().collect();
    threads.sort_by(|left, right| left.0.cmp(right.0));
    let mut lines = Vec::new();
    for (thread, state) in threads {
        let alone;
        let state = match state.as_object() {
            Some(state) => state,
            None => {
                alone = Map::from_iter([("status".to_string(), state.clone())]);
                &alone
            }
        };
        let (word, mut message) = status_text(field(state, &["status"]));
        let own = field(state, &["message"]).and_then(Value::as_str);
        if let Some(own) = own.filter(|own| message.is_empty() && !python::strip(own).is_empty()) {
            message = clip(&one_line(own), 240);
        }
        let label = agent_label(
            Some(&Value::from(thread.as_str())),
            field(state, &["agent_nickname", "agentNickname", "nickname"]),
            field(
                state,
                &["agent_role", "agentRole", "agent_type", "agentType", "role"],
            ),
        );
        let text = if message.is_empty() {
            word
        } else {
            format!("{word} - {message}")
        };
        lines.push(line(&text, &label, "status"));
    }
    lines
}

fn counted_agents(count: usize) -> String {
    match count {
        0 => "agents".to_string(),
        1 => "1 agent".to_string(),
        count => format!("{count} agents"),
    }
}

/// The cell of a call that starts, waits for, writes to, resumes or closes an agent, by the
/// name of the call (`_subagent_tool_cell`). `None`: the call is none of those.
pub fn subagent_call(name: &str, payload: &Map<String, Value>, running: bool) -> Option<Value> {
    let given = object_of(payload.get("arguments"));
    let id = text_of(&[payload.get("call_id"), payload.get("id")]);
    let status = if running { "running" } else { "recorded" };
    if name == "spawn_agent" {
        let role = text_of(&[given.get("agent_type"), given.get("agent_role")]);
        let role = python::strip(&role);
        let task = text_of(&[given.get("message"), given.get("prompt")]);
        let task = python::strip(&task);
        let mut lines = Vec::new();
        if !task.is_empty() {
            lines.push(detail(&clip(&one_line(task), 240), "Task"));
        }
        let title = if running {
            "Spawning agent"
        } else {
            "Spawned agent"
        };
        let summary = if role.is_empty() { "agent" } else { role };
        return Some(cell("subagents", title, summary, status, &id, lines));
    }
    // `wait_agent` waits for agents. The outer `wait` takes up a command that was left
    // running, and names no agent.
    let waits = name == "wait" && (given.contains_key("target") || given.contains_key("targets"));
    if name == "wait_agent" || waits {
        let targets = listed(some_of(&[given.get("targets"), given.get("target")]));
        let lines = targets
            .iter()
            .map(|target| line(&head(&said(target), 36), "Agent", "muted"));
        let title = if running {
            "Waiting for agents"
        } else {
            "Finished waiting"
        };
        let summary = counted_agents(targets.len());
        return Some(cell(
            "subagents",
            title,
            &summary,
            status,
            &id,
            lines.collect(),
        ));
    }
    let action = match name {
        "send_input" => "Sent input to agent",
        "resume_agent" => "Resumed agent",
        "close_agent" => "Closed agent",
        _ => return None,
    };
    let target = some_of(&[given.get("target"), given.get("id")]);
    let task = text_of(&[given.get("message")]);
    let task = python::strip(&task);
    let mut lines = Vec::new();
    if let Some(target) = target {
        lines.push(line(&head(&said(target), 36), "Agent", "muted"));
    }
    if !task.is_empty() {
        lines.push(detail(&clip(&one_line(task), 240), "Input"));
    }
    let summary = target.map_or("agent".to_string(), |target| head(&said(target), 8));
    Some(cell("subagents", action, &summary, status, &id, lines))
}

/// The cell of such a call with what came back for it (`_subagent_output_cell`).
fn subagent_answered(held: &Value, output: &Value) -> Value {
    let title = text_of(&[held.get("title")]);
    let data = object_of(Some(output));
    let mut cell = held.clone();
    let mut lines = match held.get("lines") {
        Some(Value::Array(lines)) => lines.clone(),
        _ => Vec::new(),
    };
    if data.contains_key("agent_id") || data.contains_key("nickname") {
        let role = any_of(&[data.get("agent_role"), data.get("agent_type")]);
        let label = agent_label(data.get("agent_id"), data.get("nickname"), role);
        cell["title"] = Value::from("Spawned agent");
        cell["summary"] = Value::from(label);
        cell["status"] = Value::from("ok");
        if let Some(thread) = data.get("agent_id").filter(|thread| python::truthy(thread)) {
            lines.push(line(&said(thread), "Thread", "muted"));
        }
    } else if data.contains_key("status") {
        let (word, message) = status_text(data.get("status"));
        cell["status"] = Value::from(status_state(data.get("status"), None));
        if title.contains("Waiting") {
            cell["title"] = Value::from("Finished waiting");
        }
        let text = if message.is_empty() {
            word
        } else {
            format!("{word} - {message}")
        };
        lines.push(line(&text, "Status", "status"));
    } else if let Some(result) = data.get("completed") {
        cell["status"] = Value::from("ok");
        lines.push(line(&clip(&said(result), 240), "Result", "status"));
    } else {
        cell["status"] = Value::from("ok");
        if let Some(text) = output
            .as_str()
            .filter(|text| !python::strip(text).is_empty())
        {
            lines.push(line(&clip(&one_line(text), 240), "Result", "status"));
        }
    }
    cell["lines"] = Value::Array(lines);
    cell
}

/// The cell of an item that is such a call (`_collab_tool_cell`). `None`: the item is none.
pub fn collab_item(item: &Map<String, Value>, running: bool) -> Option<Value> {
    let details = item
        .get("details")
        .and_then(Value::as_object)
        .unwrap_or(item);
    let kind = token(any_of(&[details.get("type"), item.get("type")]));
    if !matches!(kind.as_str(), "collab_tool_call" | "collab_agent_tool_call") {
        return None;
    }
    let tool = token(field(details, &["tool"]));
    let receivers = listed(some_of(&[field(
        details,
        &["receiver_thread_ids", "receiverThreadIds"],
    )]));
    let first = receivers.first().filter(|first| python::truthy(first));
    let status = if running {
        "running"
    } else {
        status_state(None, field(details, &["status"]))
    };
    let prompt = text_of(&[field(details, &["prompt"])]);
    let prompt = python::strip(&prompt);
    let title = match (tool.as_str(), running) {
        ("spawn_agent", true) => "Spawning agent",
        ("spawn_agent", false) => "Spawned agent",
        ("send_input", true) => "Sending input to agent",
        ("send_input", false) => "Sent input to agent",
        ("wait", true) => "Waiting for agents",
        ("wait", false) => "Finished waiting",
        ("resume_agent", true) => "Resuming agent",
        ("resume_agent", false) => "Resumed agent",
        ("close_agent", true) => "Closing agent",
        ("close_agent", false) => "Closed agent",
        _ => "Agent activity",
    };
    let mut lines = Vec::new();
    if !prompt.is_empty() {
        lines.push(detail(&clip(&one_line(prompt), 240), "Task"));
    }
    lines.extend(agent_state_lines(field(
        details,
        &["agents_states", "agentsStates"],
    )));
    if let Some(first) = first.filter(|_| lines.is_empty()) {
        lines.push(line(&said(first), "Agent", "muted"));
    }
    let summary = match first {
        _ if tool == "wait" && receivers.len() > 1 => format!("{} agents", receivers.len()),
        Some(first) => head(&said(first), 8),
        None => String::new(),
    };
    let id = text_of(&[
        field(item, &["id"]),
        field(details, &["id", "call_id", "callId"]),
    ]);
    Some(cell("subagents", title, &summary, status, &id, lines))
}

/// The cell of an event Codex writes of an agent it started (`_collab_protocol_cell`).
/// `None`: the event is none of those.
pub fn collab_event(event: &str, payload: &Map<String, Value>) -> Option<Value> {
    let id = text_of(&[payload.get("call_id")]);
    let begins = event.ends_with("_begin");
    let ended = |status: Option<&Value>| {
        if begins {
            "running"
        } else {
            status_state(status, None)
        }
    };
    let of = |name: &str| payload.get(name);
    let prompt = text_of(&[of("prompt")]);
    let prompt = clip(&one_line(python::strip(&prompt)), 240);
    match event {
        "collab_agent_spawn_begin" | "collab_agent_spawn_end" => {
            let thread = of("new_thread_id").filter(|thread| python::truthy(thread));
            let role = any_of(&[of("new_agent_role"), of("agent_type")]);
            let mut lines = Vec::new();
            if !prompt.is_empty() {
                lines.push(detail(&prompt, "Task"));
            }
            if let Some(thread) = thread {
                lines.push(line(&said(thread), "Thread", "muted"));
            }
            let title = if begins {
                "Spawning agent"
            } else {
                "Spawned agent"
            };
            let summary = agent_label(of("new_thread_id"), of("new_agent_nickname"), role);
            Some(cell(
                "subagents",
                title,
                &summary,
                ended(of("status")),
                &id,
                lines,
            ))
        }
        "collab_agent_interaction_begin" | "collab_agent_interaction_end" => {
            let role = any_of(&[of("receiver_agent_role"), of("agent_type")]);
            let mut lines = Vec::new();
            if !prompt.is_empty() {
                lines.push(detail(&prompt, "Input"));
            }
            let title = if begins {
                "Sending input to agent"
            } else {
                "Sent input to agent"
            };
            let summary = agent_label(
                of("receiver_thread_id"),
                of("receiver_agent_nickname"),
                role,
            );
            Some(cell(
                "subagents",
                title,
                &summary,
                ended(of("status")),
                &id,
                lines,
            ))
        }
        "collab_waiting_begin" | "collab_waiting_end" => {
            let receivers = listed(some_of(&[of("receiver_thread_ids")]));
            let mut lines = Vec::new();
            let status = if begins {
                let known = of("receiver_agents").and_then(Value::as_array);
                for (at, thread) in receivers.iter().enumerate() {
                    let nothing = Map::new();
                    let meta = known
                        .and_then(|known| known.get(at))
                        .and_then(Value::as_object);
                    let meta = meta.unwrap_or(&nothing);
                    let nickname = any_of(&[meta.get("agent_nickname"), meta.get("agentNickname")]);
                    let role = any_of(&[
                        meta.get("agent_role"),
                        meta.get("agentRole"),
                        meta.get("agent_type"),
                    ]);
                    let label = agent_label(Some(thread), nickname, role);
                    lines.push(line(&said(thread), &label, "muted"));
                }
                "running"
            } else {
                let listed = any_of(&[of("agent_statuses"), of("agentStatuses")]);
                match listed
                    .and_then(Value::as_array)
                    .filter(|listed| !listed.is_empty())
                {
                    Some(listed) => {
                        for entry in listed.iter().filter_map(object) {
                            let thread = any_of(&[entry.get("thread_id"), entry.get("threadId")]);
                            let (word, message) = status_text(entry.get("status"));
                            let text = if message.is_empty() {
                                word
                            } else {
                                format!("{word} - {message}")
                            };
                            let nickname =
                                any_of(&[entry.get("agent_nickname"), entry.get("agentNickname")]);
                            let role = any_of(&[
                                entry.get("agent_role"),
                                entry.get("agentRole"),
                                entry.get("agent_type"),
                            ]);
                            lines.push(line(&text, &agent_label(thread, nickname, role), "status"));
                        }
                    }
                    None => {
                        if let Some(Value::Object(statuses)) = any_of(&[of("statuses")]) {
                            let each = statuses.iter().map(|(thread, state)| {
                                (thread.clone(), json!({ "status": state }))
                            });
                            let states = Value::Object(each.collect());
                            lines.extend(agent_state_lines(Some(&states)));
                        }
                    }
                }
                let failed = lines.iter().any(|line| {
                    let text = text_of(&[line.get("text")]).to_lowercase();
                    ["errored", "not found", "error"]
                        .iter()
                        .any(|start| text.starts_with(start))
                });
                if failed { "error" } else { "ok" }
            };
            let count = if receivers.is_empty() {
                lines.len()
            } else {
                receivers.len()
            };
            let title = if begins {
                "Waiting for agents"
            } else {
                "Finished waiting"
            };
            Some(cell(
                "subagents",
                title,
                &counted_agents(count),
                status,
                &id,
                lines,
            ))
        }
        "collab_close_begin" | "collab_close_end" | "collab_resume_begin" | "collab_resume_end" => {
            let thread = of("receiver_thread_id").filter(|thread| python::truthy(thread));
            let role = any_of(&[of("receiver_agent_role"), of("agent_type")]);
            let title = match (event.starts_with("collab_close"), begins) {
                (true, true) => "Closing agent",
                (true, false) => "Closed agent",
                (false, true) => "Resuming agent",
                (false, false) => "Resumed agent",
            };
            let lines = thread.map(|thread| line(&said(thread), "Thread", "muted"));
            let summary = agent_label(
                of("receiver_thread_id"),
                of("receiver_agent_nickname"),
                role,
            );
            Some(cell(
                "subagents",
                title,
                &summary,
                ended(of("status")),
                &id,
                lines.into_iter().collect(),
            ))
        }
        "sub_agent_activity" => {
            let kind = token(of("kind"));
            let path = text_of(&[of("agent_path"), of("agentPath")]);
            let title = match kind.as_str() {
                "started" => "Started subagent",
                "interacted" => "Interacted with subagent",
                "interrupted" => "Interrupted subagent",
                _ => "Subagent activity",
            };
            let status = if kind == "interrupted" { "error" } else { "ok" };
            let own = text_of(&[of("event_id"), of("eventId")]);
            let id = if own.is_empty() { id } else { own };
            Some(cell(
                "subagents",
                title,
                python::strip(&path),
                status,
                &id,
                Vec::new(),
            ))
        }
        _ => None,
    }
}

// ---- the older records of a call -----------------------------------------------------------

/// The card of a call as an older record has it (`_codex_tool`).
pub fn card(name: &str, payload: &Map<String, Value>) -> Value {
    let mut out = json!({
        "name": name, "summary": name, "action": name, "file_path": "", "status": "recorded",
    });
    if let Some(id) = some_of(&[payload.get("call_id"), payload.get("id")]) {
        out["id"] = id.clone();
    }
    match name {
        "exec_command" | "shell" | "local_shell" => {
            let command = command_of(payload.get("arguments"));
            out["name"] = Value::from("Bash");
            out["command"] = Value::from(command.as_str());
            out["summary"] = Value::from(head(&command, 80));
            out["action"] = Value::from("run");
        }
        "apply_patch" => {
            if let Some(Value::String(text)) = payload.get("input") {
                let paths = patch_paths(text);
                let edits = patch_edits(text);
                out["name"] = Value::from("Edit");
                out["summary"] = Value::from("apply_patch");
                out["action"] = Value::from("edit");
                out["content"] = Value::from(clip(text, 600));
                if let Some(first) = paths.first() {
                    out["file_path"] = Value::from(*first);
                    let named: Vec<&str> = paths.iter().take(3).copied().collect();
                    out["summary"] = Value::from(named.join(", "));
                }
                match edits.as_slice() {
                    [] => {}
                    [only] => {
                        out["old"] = only["old"].clone();
                        out["new"] = only["new"].clone();
                    }
                    several => {
                        out["name"] = Value::from("MultiEdit");
                        out["edits"] = Value::from(several.to_vec());
                        out["edit_count"] = Value::from(several.len());
                    }
                }
            }
        }
        "web_search" | "web_search_call" => {
            out["name"] = Value::from("WebSearch");
            out["action"] = Value::from("search");
        }
        _ => {
            let words = super::summarize(name, &json!({}));
            let or_name = |said: String| {
                if said.is_empty() {
                    name.to_string()
                } else {
                    said
                }
            };
            out["summary"] = Value::from(or_name(words.summary));
            out["action"] = Value::from(or_name(words.action));
        }
    }
    out
}

/// Gives a call what came back for it: its card is over and says a little of it, and its
/// cell shows it (`_attach_output`). A call is known by its id; with none, the last that
/// still waits is meant.
pub fn attach_output(turns: &mut [Value], id: &str, output: &Value) {
    let text = clean_output(output);
    let code = exit_code_in(output);
    let named = |held: &Value| id.is_empty() || held.get("id").and_then(Value::as_str) == Some(id);
    for turn in turns.iter_mut().rev() {
        if let Some(tools) = turn["tools"].as_array_mut() {
            let waits = |tool: &&mut Value| {
                named(tool) && !matches!(tool["status"].as_str(), Some("ok" | "error"))
            };
            if let Some(tool) = tools.iter_mut().rev().find(waits) {
                tool["status"] = Value::from("ok");
                tool["result"] = Value::from(clip(&text, 300));
            }
        }
        let Some(cells) = turn["display_cells"].as_array_mut() else {
            continue;
        };
        let Some(held) = cells.iter_mut().rev().find(|held| named(held)) else {
            continue;
        };
        let own = text_of(&[held.get("id")]);
        *held = match held["kind"].as_str() {
            Some("command") => {
                let ran = text_of(&[held.get("summary")]);
                let id = if own.is_empty() { id } else { &own };
                command(&ran, id, Some(&Value::from(text.as_str())), code, false)
            }
            Some("subagents") => subagent_answered(held, output),
            Some("patch") => {
                let mut cell = held.clone();
                cell["status"] = Value::from("ok");
                cell
            }
            _ => {
                let mut cell = held.clone();
                cell["status"] = Value::from("ok");
                let title = match held.get("title") {
                    Some(Value::String(title)) if title == "Calling" => Value::from("Called"),
                    Some(title) => title.clone(),
                    None => Value::from("Called"),
                };
                cell["title"] = title;
                cell["lines"] = Value::Array(preview(&Value::from(text.as_str()), OUTPUT_LINES));
                cell
            }
        };
        return;
    }
}

/// What kind of call an older record is, and the tool it stands for: a call that runs
/// other tools inside it (`exec`) stands for the first of them that counts
/// (`_legacy_tool_category`).
pub fn older_kind(name: &str, payload: &Map<String, Value>) -> (&'static str, String) {
    let named = token_of(name);
    let kind = match named.as_str() {
        "exec_command" | "shell" | "local_shell" => "command",
        "apply_patch" => "patch",
        "web_search" | "web_search_call" | "web__run" => "web_search",
        "view_image" | "image_gen__imagegen" => "image",
        "wait" | "write_stdin" => "status",
        "spawn_agent" | "send_message" | "followup_task" | "wait_agent" | "list_agents"
        | "interrupt_agent" => "subagents",
        "exec" => "",
        _ => "tool",
    };
    if !kind.is_empty() {
        return (kind, named);
    }
    let source = text_of(&[payload.get("input"), payload.get("arguments")]);
    let nested: Vec<String> = NESTED_TOOL
        .captures_iter(&source)
        .map(|found| token_of(&found[1]))
        .collect();
    let by_weight = [
        ("apply_patch", "patch"),
        ("exec_command", "command"),
        ("web__run", "web_search"),
        ("view_image", "image"),
        ("image_gen__imagegen", "image"),
        ("write_stdin", "status"),
        ("wait", "status"),
        ("spawn_agent", "subagents"),
        ("send_message", "subagents"),
        ("followup_task", "subagents"),
        ("wait_agent", "subagents"),
    ];
    for (tool, kind) in by_weight {
        if nested.iter().any(|name| name == tool) {
            return (kind, tool.to_string());
        }
    }
    (
        "tool",
        nested
            .into_iter()
            .next()
            .unwrap_or_else(|| "exec".to_string()),
    )
}

/// The cell of a call that runs other tools inside it, while nothing came back yet
/// (`_nested_exec_cell`).
pub fn nested(payload: &Map<String, Value>, id: &str) -> Value {
    let (kind, tool) = older_kind("exec", payload);
    let source = text_of(&[payload.get("input"), payload.get("arguments")]);
    let running = |kind: &str, title: &str, summary: &str| {
        cell(kind, title, summary, "running", id, Vec::new())
    };
    match kind {
        "command" => {
            let found = JSON_COMMAND.captures(&source);
            let written = found.and_then(|found| serde_json::from_str::<Value>(&found[1]).ok());
            let ran = written
                .as_ref()
                .and_then(Value::as_str)
                .filter(|ran| !ran.is_empty());
            command(ran.unwrap_or("command"), id, None, None, true)
        }
        "patch" => running("patch", "Editing", "apply_patch"),
        "web_search" => web_search(id, Some(&Value::from("web")), true),
        "image" => running("image", "Viewing image", &tool),
        "subagents" => running("subagents", "Agent activity", &tool),
        "status" => {
            let title = if tool == "write_stdin" {
                "Continuing command"
            } else {
                "Waiting"
            };
            running("status", title, "background command")
        }
        _ => generic(&tool, payload, "running"),
    }
}

/// The cell of any other call of an older record, by its name (`_legacy_function_cell`).
pub fn older(name: &str, payload: &Map<String, Value>, id: &str) -> Value {
    let named = token_of(name);
    let namespace = token(payload.get("namespace"));
    let given = object_of(payload.get("arguments"));
    let running = |kind: &str, title: &str, summary: &str| {
        cell(kind, title, summary, "running", id, Vec::new())
    };
    match named.as_str() {
        "update_plan" => {
            let steps = any_of(&[given.get("plan")]).and_then(Value::as_array);
            let lines = steps.into_iter().flatten().filter_map(object).map(|step| {
                let status = text_of(&[step.get("status")]);
                line(
                    &text_of(&[step.get("step")]),
                    &titled(&status.replace('_', " ")),
                    "status",
                )
            });
            cell("plan", "Updated plan", "", "ok", id, lines.collect())
        }
        "get_goal" | "create_goal" | "update_goal" => {
            let title = match named.as_str() {
                "get_goal" => "Checked goal",
                "create_goal" => "Created goal",
                _ => "Updated goal",
            };
            let summary = text_of(&[given.get("objective"), given.get("status")]);
            cell("plan", title, &summary, "ok", id, Vec::new())
        }
        "wait" => running("status", "Waiting", "background work"),
        "write_stdin" => {
            let summary = text_of(&[given.get("session_id"), given.get("cell_id")]);
            running("status", "Continuing command", &summary)
        }
        "run" | "web__run" if matches!(namespace.as_str(), "web" | "") => {
            let asked = if given.is_empty() {
                object_of(payload.get("input"))
            } else {
                given
            };
            let first = asked
                .get("search_query")
                .and_then(Value::as_array)
                .and_then(|all| all.first());
            let query = first
                .and_then(Value::as_object)
                .map(|first| text_of(&[first.get("q")]));
            let query = query
                .filter(|query| !query.is_empty())
                .unwrap_or_else(|| "web".to_string());
            web_search(id, Some(&Value::from(query)), true)
        }
        "view_image" | "image_gen__imagegen" => {
            let title = if named == "view_image" {
                "Viewing image"
            } else {
                "Generating image"
            };
            let summary = text_of(&[given.get("path"), given.get("prompt")]);
            running("image", title, &head(&summary, 240))
        }
        "spawn_agent" | "send_message" | "followup_task" | "wait_agent" | "list_agents"
        | "interrupt_agent" => {
            subagent_call(name, payload, true).unwrap_or_else(|| generic(name, payload, "recorded"))
        }
        _ => generic(name, payload, "recorded"),
    }
}
