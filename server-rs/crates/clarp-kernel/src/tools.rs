//! What a tool call is, in the words the apps show while it runs.
//!
//! "Running npm test", "Reading src/main.rs", "Searching wake in crates": a tool's name and
//! what it was given become a category, a short label and the command it runs, and those
//! become the line an app shows of a working agent. A command that only looks at things
//! (`rg`, `ls`, `cat` and their kind) is a search, a listing or a reading, whatever shell it
//! was wrapped in.
//!
//! This is the Python Host's `server/lib/live_tools.py` (`classify_tool`) with the shell
//! reading of `codex_tool_cells.py` and the headline of `live_core.py`, held to them by
//! `tests/fixtures/tool-vectors.json`, which `server-rs/tools/tool-vectors.py` writes.

use serde_json::Value;

const LABEL_LIMIT: usize = 120;
const COMMAND_LIMIT: usize = 2_000;

/// What a tool call is.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Described {
    /// `exec`, `read`, `list`, `search`, `edit`, `write`, `fetch`, `mcp`, `todo`, `agent` or
    /// `other`.
    pub category: &'static str,
    /// What it works on, shortly: a command, a path's end, a pattern.
    pub label: String,
    /// The command it runs, when it runs one.
    pub command: Option<String>,
}

/// The category a tool has by its name alone.
fn by_name(key: &str) -> Option<&'static str> {
    Some(match key {
        "read" | "notebookread" | "view" | "read_file" | "image_view" => "read",
        "grep" | "glob" | "search" | "codebase_search" => "search",
        "ls" | "list" | "list_dir" => "list",
        "edit" | "multiedit" | "notebookedit" | "apply_patch" | "patch" | "str_replace"
        | "file_change" | "patch_apply" => "edit",
        "write" | "create" | "write_file" => "write",
        "webfetch" | "websearch" | "web_search" | "fetch" | "web_search_call" => "fetch",
        "todowrite" | "update_plan" | "todoread" => "todo",
        "task" | "agent" | "collab_tool_call" | "collab_agent_tool_call" => "agent",
        "mcp_tool_call" => "mcp",
        "image_generation" | "dynamic_tool_call" => "other",
        _ => return None,
    })
}

const SHELL_NAMES: [&str; 8] = [
    "bash",
    "shell",
    "exec_command",
    "local_shell",
    "run_terminal_command",
    "run_command",
    "command_execution",
    "terminal",
];
const SHELLS: [&str; 3] = ["bash", "sh", "zsh"];
const EXPLORERS: [&str; 11] = [
    "rg", "grep", "git", "ls", "find", "fd", "cat", "sed", "nl", "head", "tail",
];

fn is_true(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(flag) => *flag,
        Value::String(text) => !text.is_empty(),
        Value::Number(number) => number.as_f64() != Some(0.0),
        Value::Array(items) => !items.is_empty(),
        Value::Object(fields) => !fields.is_empty(),
    }
}

/// A value as the text Python makes of it.
fn text(value: &Value) -> String {
    match value {
        Value::String(text) => text.clone(),
        Value::Null => "None".to_string(),
        Value::Bool(true) => "True".to_string(),
        Value::Bool(false) => "False".to_string(),
        other => other.to_string(),
    }
}

/// The first of these fields of what the tool was given that says something.
fn first<'a>(input: &'a Value, fields: &[&str]) -> Option<&'a Value> {
    fields
        .iter()
        .filter_map(|field| input.get(field))
        .find(|value| is_true(value))
}

/// A text without the space around it, and cut where it is longer than `limit`.
fn truncate(text: &str, limit: usize) -> String {
    let text = text.trim();
    if text.chars().count() <= limit {
        return text.to_string();
    }
    let cut: String = text.chars().take(limit).collect();
    format!("{}...", cut.trim_end())
}

/// The last two parts of a path: enough to know a file by.
fn path_tail(path: &str) -> String {
    let text = truncate(path, 220);
    let slashed = text.replace('\\', "/");
    let parts: Vec<&str> = slashed
        .trim_end_matches('/')
        .split('/')
        .filter(|part| !part.is_empty())
        .collect();
    match parts.len() {
        0 => text,
        n => parts[n.saturating_sub(2)..].join("/"),
    }
}

/// The last part of a path, as a path names it: `.` and empty parts are no parts.
fn file_name(path: &str) -> &str {
    path.split('/')
        .rfind(|part| !part.is_empty() && *part != ".")
        .unwrap_or("")
}

fn plain(c: char) -> bool {
    c.is_ascii_alphanumeric() || "_./+:@%=,-".contains(c)
}

fn blank(c: char) -> bool {
    matches!(c, ' ' | '\t' | '\r' | '\n')
}

/// True when the command's first word alone shows it is none of `names`: a plain word,
/// with nothing a shell would read into it.
fn first_word_rules_out(command: &str, names: &[&str]) -> bool {
    let rest = command.trim_start_matches(blank);
    let end = rest.find(|c: char| !plain(c)).unwrap_or(rest.len());
    let word = &rest[..end];
    let ends_as_a_word = rest[end..].chars().next().is_none_or(blank);
    !word.is_empty() && ends_as_a_word && !names.contains(&file_name(word))
}

/// The words of `shell -lc 'script'` when the command is exactly that: the usual wrapper,
/// read without reading the script letter by letter.
fn quoted_wrapper_words(command: &str) -> Option<Vec<String>> {
    let (shell, rest) = command.split_once(' ')?;
    if shell.is_empty() || !shell.chars().all(plain) {
        return None;
    }
    let inner = rest.strip_prefix("-lc '")?.strip_suffix('\'')?;
    let pieces: Vec<&str> = inner.split("'\"'\"'").collect();
    if pieces.iter().any(|piece| piece.contains('\'')) {
        return None;
    }
    Some(vec![shell.to_string(), "-lc".to_string(), pieces.join("'")])
}

/// The words of a command line as a POSIX shell splits it, without expanding anything.
/// `None` when a quotation is not closed or the line ends on a backslash.
pub(crate) fn shell_split(command: &str) -> Option<Vec<String>> {
    #[derive(Clone, Copy, PartialEq)]
    enum In {
        Between,
        Word,
        Single,
        Double,
    }
    let mut words = Vec::new();
    let mut word = String::new();
    let mut state = In::Between;
    let mut chars = command.chars();
    while let Some(c) = chars.next() {
        match (state, c) {
            (In::Between | In::Word, c) if blank(c) => {
                if state == In::Word {
                    words.push(std::mem::take(&mut word));
                }
                state = In::Between;
            }
            (In::Between | In::Word, '\\') => {
                word.push(chars.next()?);
                state = In::Word;
            }
            (In::Between | In::Word, '\'') => state = In::Single,
            (In::Between | In::Word, '"') => state = In::Double,
            (In::Between | In::Word, c) => {
                word.push(c);
                state = In::Word;
            }
            (In::Single, '\'') | (In::Double, '"') => state = In::Word,
            (In::Double, '\\') => {
                // Inside double quotes a backslash only takes the meaning from a quote or
                // another backslash; before anything else it is itself.
                let next = chars.next()?;
                if next != '\\' && next != '"' {
                    word.push('\\');
                }
                word.push(next);
            }
            (In::Single | In::Double, c) => word.push(c),
        }
    }
    match state {
        In::Single | In::Double => None,
        In::Word => {
            words.push(word);
            Some(words)
        }
        In::Between => Some(words),
    }
}

fn shell_words(command: &str) -> Vec<String> {
    quoted_wrapper_words(command)
        .or_else(|| shell_split(command))
        .unwrap_or_else(|| command.split_whitespace().map(str::to_string).collect())
}

/// The script a shell was asked to run, when the command is a shell wrapped around one.
fn strip_shell_wrapper(command: &str) -> String {
    if first_word_rules_out(command, &SHELLS) {
        return command.to_string();
    }
    let mut words = shell_words(command);
    let wrapped = words.len() >= 3 && SHELLS.contains(&file_name(&words[0])) && words[1] == "-lc";
    if wrapped {
        words.swap_remove(2)
    } else {
        command.to_string()
    }
}

fn looks_like_path(value: &str) -> bool {
    value.contains('/')
        || value.starts_with('.')
        || file_name(&value.replace('\\', "/")).contains('.')
}

/// The values of a list, each once, in the order they first come.
fn each_once<'a>(values: impl Iterator<Item = &'a str>) -> Vec<&'a str> {
    let mut seen: Vec<&str> = Vec::new();
    for value in values {
        if !seen.contains(&value) {
            seen.push(value);
        }
    }
    seen
}

/// What a command looks at, when looking is all it does: `Read`, `List` or `Search`, and
/// what.
fn exploration(command: &str) -> Option<(&'static str, String)> {
    let stripped = strip_shell_wrapper(command);
    let mut display = stripped.trim().to_string();
    if display.is_empty() {
        return None;
    }
    if display.contains("&&") || display.contains(';') {
        // A chain is named by its explorers. When every step reads files, each file is
        // named once; otherwise the last explorer stands for the chain.
        let chain = display.replace("&&", ";");
        let parts: Vec<&str> = chain
            .split(';')
            .map(str::trim)
            .filter(|part| !part.is_empty() && !matches!(*part, "pwd" | "true"))
            .collect();
        if parts.len() > 1 {
            let lines: Vec<Option<(&'static str, String)>> =
                parts.iter().map(|part| exploration(part)).collect();
            let all_read = lines.iter().all(|line| matches!(line, Some(("Read", _))));
            if all_read {
                let texts = lines
                    .iter()
                    .flatten()
                    .flat_map(|(_, text)| text.split(", "));
                return Some(("Read", each_once(texts).join(", ")));
            }
            return lines.into_iter().rev().flatten().next();
        }
        display = parts.first().map(|part| (*part).to_string())?;
    }
    if first_word_rules_out(&display, &EXPLORERS) {
        return None;
    }
    let words = shell_words(&display);
    let (base, args) = words.split_first()?;
    let args: Vec<&str> = args.iter().map(String::as_str).collect();
    let no_flag = |arg: &&&str| !arg.starts_with('-');
    let scope = || args.iter().rev().find(|arg| looks_like_path(arg)).copied();
    match file_name(base) {
        "git" if !matches!(args.first(), Some(&"grep" | &"ls-files")) => None,
        "git" if args.first() == Some(&"ls-files") => Some(("List", args.join(" "))),
        "rg" if args.contains(&"--files") => {
            Some(("List", scope().unwrap_or("rg --files").to_string()))
        }
        "rg" | "grep" | "git" => {
            let query = args.iter().find(no_flag).copied().unwrap_or("");
            let text = if query.is_empty() {
                display.as_str()
            } else {
                query
            };
            Some(match scope().filter(|scope| *scope != query) {
                Some(scope) => ("Search", format!("{text} in {scope}")),
                None => ("Search", text.to_string()),
            })
        }
        "ls" | "find" | "fd" => {
            let target = args.iter().rev().find(no_flag).copied();
            let target = target.filter(|target| !target.is_empty());
            Some(("List", target.unwrap_or(&display).to_string()))
        }
        "cat" | "sed" | "nl" | "head" | "tail" => {
            let paths = args
                .iter()
                .filter(|arg| !arg.starts_with('-') && looks_like_path(arg))
                .copied();
            let target = each_once(paths).join(", ");
            Some(("Read", if target.is_empty() { display } else { target }))
        }
        _ => None,
    }
}

/// The first line of a text, as Python splits lines.
fn first_line(text: &str) -> &str {
    let ends = |c: char| {
        matches!(
            c,
            '\n' | '\r'
                | '\x0b'
                | '\x0c'
                | '\x1c'
                | '\x1d'
                | '\x1e'
                | '\u{85}'
                | '\u{2028}'
                | '\u{2029}'
        )
    };
    text.split(ends).next().unwrap_or("")
}

/// The category and the label of a command line.
fn shell_category(command: &str) -> (&'static str, String) {
    if let Some((kind, text)) = exploration(command) {
        let category = match kind {
            "Read" => "read",
            "List" => "list",
            _ => "search",
        };
        return (category, truncate(&text, LABEL_LIMIT));
    }
    let shown = strip_shell_wrapper(command);
    let line = match shown.trim() {
        "" => command,
        shown => first_line(shown),
    };
    ("exec", truncate(line, LABEL_LIMIT))
}

/// What a tool call is: its category, a short label, and the command when it runs one.
pub fn describe(name: &str, input: &Value) -> Described {
    let raw_name = name.trim();
    let key = raw_name.to_lowercase();
    let command = first(input, &["command", "cmd"]).map(|command| match command {
        Value::Array(parts) => parts.iter().map(text).collect::<Vec<_>>().join(" "),
        other => text(other),
    });
    let labelled = |category, label: String| Described {
        category,
        label,
        command: None,
    };
    if key.starts_with("mcp__") || key.starts_with("mcp:") {
        let tool = raw_name.rsplit("__").next().unwrap_or(raw_name);
        return labelled("mcp", truncate(tool, LABEL_LIMIT));
    }
    let known = by_name(&key);
    // A command line: under a shell's name, under a name nobody knows, or as the name itself.
    let line = if SHELL_NAMES.contains(&key.as_str()) || (command.is_some() && known.is_none()) {
        Some(command.unwrap_or_else(|| raw_name.to_string()))
    } else if known.is_none() && (raw_name.contains(' ') || raw_name.contains('/')) {
        Some(raw_name.to_string())
    } else {
        None
    };
    if let Some(line) = line {
        let (category, label) = shell_category(&line);
        return Described {
            category,
            label,
            command: Some(truncate(&line, COMMAND_LIMIT)),
        };
    }
    let path = first(input, &["file_path", "filePath", "path", "notebook_path"]).map(text);
    match (known, path) {
        (Some(category @ ("read" | "edit" | "write" | "list")), Some(path)) => {
            labelled(category, path_tail(&path))
        }
        (Some("search"), path) => {
            let pattern = first(input, &["pattern", "query"]).map(text).or(path);
            labelled(
                "search",
                truncate(&pattern.unwrap_or_default(), LABEL_LIMIT),
            )
        }
        (Some("fetch"), _) => {
            let target = first(input, &["url", "query"])
                .map(text)
                .unwrap_or_default();
            labelled("fetch", truncate(&target, LABEL_LIMIT))
        }
        (Some("agent"), _) => {
            let described = first(input, &["description"]).map(text);
            let label = described.unwrap_or_else(|| raw_name.to_string());
            labelled("agent", truncate(&label, LABEL_LIMIT))
        }
        (Some("todo"), _) => labelled("todo", "plan".to_string()),
        (Some(category), Some(path)) => labelled(category, path_tail(&path)),
        (Some(category), None) => labelled(category, raw_name.to_string()),
        (None, _) => labelled("other", truncate(raw_name, LABEL_LIMIT)),
    }
}

/// A tool call as the event of a tool state sums it up: what is being done, to what, and
/// the file when it is about one.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Summary {
    pub action: String,
    pub summary: String,
    pub file_path: String,
}

/// A value of what a tool was given, as a short text. Nothing is no text.
fn short(value: Option<&Value>, limit: usize) -> String {
    match value {
        None | Some(Value::Null) => String::new(),
        Some(value) => truncate(&text(value), limit),
    }
}

/// Sums a tool call up for the event of a tool state. This is the older of the two ways the
/// Python Host words a tool call (`activity.summarize_tool_activity`); the apps that show an
/// agent's activity line read it.
pub fn summarize(name: &str, input: &Value) -> Summary {
    let name = if name.is_empty() { "tool" } else { name };
    let file_path = first(input, &["file_path", "filePath", "path"])
        .map(text)
        .unwrap_or_default();
    let short_path = path_tail(&file_path);
    let or = |said: String, otherwise: &str| {
        if said.is_empty() {
            otherwise.to_string()
        } else {
            said
        }
    };
    let about_file = |action: &str, otherwise: &str| Summary {
        action: action.to_string(),
        summary: or(short_path.clone(), otherwise),
        file_path: file_path.clone(),
    };
    let about = |action: &str, summary: String| Summary {
        action: action.to_string(),
        summary,
        file_path: String::new(),
    };
    let listed = |field: &str| {
        input
            .get(field)
            .and_then(Value::as_array)
            .map_or(0, Vec::len)
    };
    // Where a search looks: the end of the path it was given, or here.
    let scope = || {
        let path = input.get("path").filter(|path| !path.is_null()).map(text);
        or(path_tail(&path.unwrap_or_default()), ".")
    };
    match name {
        "Bash" | "bash" => {
            let described = short(input.get("description"), 120);
            let command = short(input.get("command"), 170);
            about(
                "running command",
                or(or(described, &command), "Running shell command"),
            )
        }
        "Read" | "read_file" => about_file("reading file", "Reading file"),
        "Write" | "write_file" => about_file("writing file", "Writing file"),
        "Edit" | "edit_file" => about_file("editing file", "Editing file"),
        "MultiEdit" => {
            let edits = listed("edits");
            let summary = if short_path.is_empty() {
                format!("{edits} edits")
            } else {
                format!("{short_path} · {edits} edits")
            };
            Summary {
                action: "editing file".to_string(),
                summary,
                file_path,
            }
        }
        "Glob" | "glob_search" => {
            let pattern = short(first(input, &["pattern", "glob"]), 100);
            Summary {
                action: "finding files".to_string(),
                summary: format!("{} in {}", or(pattern, "files"), scope()),
                file_path,
            }
        }
        "Grep" | "grep_search" => {
            let pattern = short(input.get("pattern"), 100);
            Summary {
                action: "searching code".to_string(),
                summary: format!("{} in {}", or(pattern, "code"), scope()),
                file_path,
            }
        }
        "TodoWrite" => {
            let todos = input.get("todos").and_then(Value::as_array);
            let current = todos.into_iter().flatten().find(|item| {
                item.is_object()
                    && item.get("status").and_then(Value::as_str) == Some("in_progress")
            });
            let current = short(current.and_then(|item| item.get("content")), 120);
            about(
                "updating plan",
                or(current, &format!("{} items", listed("todos"))),
            )
        }
        "WebSearch" | "web_search" => about(
            "searching web",
            or(short(input.get("query"), 130), "the web"),
        ),
        "WebFetch" | "web_fetch" => about(
            "reading web page",
            or(short(input.get("url"), 130), "web page"),
        ),
        _ => {
            // The first plain value it was given, in the order given.
            let mut values = input
                .as_object()
                .into_iter()
                .flat_map(|fields| fields.values());
            let plain = values.find(|value| !value.is_object() && !value.is_array());
            Summary {
                action: name.to_string(),
                summary: or(short(plain, 100), &format!("Using {name}")),
                file_path,
            }
        }
    }
}

/// The line an app shows of an agent that runs this tool.
pub fn headline(name: &str, category: &str, label: &str) -> String {
    let verb = match category {
        "exec" => "Running",
        "read" => "Reading",
        "list" => "Listing",
        "search" => "Searching",
        "edit" => "Editing",
        "write" => "Writing",
        "fetch" => "Fetching",
        "mcp" => "Calling",
        "agent" => "Delegating",
        "todo" => return "Updating plan".to_string(),
        _ => name,
    };
    format!("{verb} {label}").trim().to_string()
}

// ---- the card a conversation shows of a tool's use -----------------------------------------
//
// The Python Host makes a card of every use of a tool when it reads the tool's transcript
// (`claude_transcript.summarise_tool`): the name, the words of `summarize`, and a few fields
// of what the tool was given, by its name: an edit's old and new text, a command and what it
// is for, a pattern. What the tool returned is put on the card when it comes
// (`_apply_tool_result`). The apps draw a card from these fields. Held to the Python Host by
// the `cards` and `results` of `tests/fixtures/tool-vectors.json`.

/// A text of what a tool was given, cut to `most` signs with an ellipsis. What is no text
/// is nothing.
fn clipped(value: Option<&Value>, most: usize) -> String {
    let Some(text) = value.and_then(Value::as_str) else {
        return String::new();
    };
    if text.chars().count() <= most {
        return text.to_string();
    }
    let cut: String = text.chars().take(most).collect();
    format!("{cut}…")
}

/// A value as Python writes it into a text: a text as it is, anything else as it is shown.
fn written(value: &Value) -> String {
    match value {
        Value::String(text) => text.clone(),
        other => crate::python::repr(other),
    }
}

/// Whether Python takes a value for a yes.
fn truthy(value: Option<&Value>) -> bool {
    match value {
        None | Some(Value::Null) => false,
        Some(Value::Bool(truth)) => *truth,
        Some(Value::Number(number)) => number.as_f64() != Some(0.0),
        Some(Value::String(text)) => !text.is_empty(),
        Some(Value::Array(items)) => !items.is_empty(),
        Some(Value::Object(fields)) => !fields.is_empty(),
    }
}

/// A whole number a tool was given, as a number or as a text that is one. The model can
/// write anything where a number belongs, and an app reads a number or nothing there.
fn whole(value: Option<&Value>) -> Value {
    let parsed = match value {
        Some(Value::Number(number)) if !number.is_f64() => number.as_i64(),
        Some(Value::String(text)) => {
            let text = text.trim();
            let digits = text.strip_prefix(['+', '-']).unwrap_or(text);
            // Digits, with single underscores between them, as Python reads a whole number.
            let groups: Vec<&str> = digits.split('_').collect();
            let plain =
                |group: &&str| !group.is_empty() && group.bytes().all(|b| b.is_ascii_digit());
            match groups.iter().all(plain) {
                true => text.replace('_', "").parse::<i64>().ok(),
                false => None,
            }
        }
        _ => None,
    };
    parsed.map_or(Value::Null, Value::from)
}

/// The card a conversation shows of one use of a tool: `recorded` until the tool returns.
/// `id` is the id the tool's use has; a use without one has none on its card.
pub fn card(name: &str, input: &Value, id: &str) -> Value {
    let nothing = serde_json::Map::new();
    let given = input.as_object().unwrap_or(&nothing);
    let asked = Value::Object(given.clone());
    let words = summarize(name, &asked);
    let raw = |field: &str, otherwise: Value| given.get(field).cloned().unwrap_or(otherwise);
    let mut card = serde_json::json!({
        "name": name, "summary": words.summary, "action": words.action,
        "file_path": words.file_path, "status": "recorded",
    });
    if !id.is_empty() {
        card["id"] = Value::from(id);
    }
    match name {
        "Edit" => {
            card["file_path"] = raw("file_path", Value::from(""));
            card["old"] = Value::from(clipped(given.get("old_string"), 600));
            card["new"] = Value::from(clipped(given.get("new_string"), 600));
            card["replace_all"] = Value::from(truthy(given.get("replace_all")));
        }
        "MultiEdit" => {
            let edits = given.get("edits").and_then(Value::as_array);
            let edits = edits.map_or(&[][..], Vec::as_slice);
            let shown: Vec<Value> = edits
                .iter()
                .take(10)
                .map(|edit| {
                    serde_json::json!({
                        "old": clipped(edit.get("old_string"), 200),
                        "new": clipped(edit.get("new_string"), 200),
                    })
                })
                .collect();
            card["file_path"] = raw("file_path", Value::from(""));
            card["edits"] = Value::from(shown);
            card["edit_count"] = Value::from(edits.len());
        }
        "Write" => {
            card["file_path"] = raw("file_path", Value::from(""));
            card["content"] = Value::from(clipped(given.get("content"), 600));
        }
        "Read" => {
            card["file_path"] = raw("file_path", Value::from(""));
            card["offset"] = whole(given.get("offset"));
            card["limit"] = whole(given.get("limit"));
        }
        "Bash" => {
            card["command"] = Value::from(clipped(given.get("command"), 400));
            card["description"] = raw("description", Value::from(""));
        }
        "Glob" | "Grep" => {
            for field in ["pattern", "path", "glob", "output_mode"] {
                card[field] = raw(field, Value::Null);
            }
        }
        "TodoWrite" => {
            let todos = given.get("todos").and_then(Value::as_array);
            let todos = todos.map_or(&[][..], Vec::as_slice);
            let of =
                |todo: &Value, field: &str| todo.get(field).cloned().unwrap_or(Value::from(""));
            let shown: Vec<Value> = todos
                .iter()
                .map(|todo| serde_json::json!({ "content": of(todo, "content"), "status": of(todo, "status") }))
                .collect();
            card["todos"] = Value::from(shown);
        }
        // Any other tool: what it was given, as far as that is short and plain.
        _ => {
            let plain = given
                .iter()
                .filter(|(_, value)| !value.is_array() && !value.is_object())
                .map(|(key, value)| {
                    let text = Value::from(written(value));
                    (key.clone(), Value::from(clipped(Some(&text), 200)))
                });
            card["input"] = Value::Object(plain.collect());
        }
    }
    card
}

/// What the parts of what a tool returned say, one under the other: a part says its `text`,
/// or its `content`; a plain text is a part; one that says nothing is left out.
pub fn result_parts(content: &Value) -> String {
    let Some(parts) = content.as_array() else {
        return String::new();
    };
    let said = |part: &Value| -> Option<String> {
        match part {
            Value::String(text) => Some(text.clone()),
            Value::Object(fields) => {
                let first = [fields.get("text"), fields.get("content")]
                    .into_iter()
                    .find(|value| truthy(*value))
                    .flatten();
                Some(first.map(written).unwrap_or_default())
            }
            _ => None,
        }
    };
    let texts: Vec<String> = parts
        .iter()
        .filter_map(said)
        .filter(|text| !text.is_empty())
        .collect();
    texts.join("\n")
}

/// What is shown of what a tool returned: its text, or the texts of its parts one under the
/// other, cut to 300 signs.
pub fn result_text(content: &Value) -> String {
    match content {
        Value::String(_) => clipped(Some(content), 300),
        Value::Array(_) => clipped(Some(&Value::from(result_parts(content))), 300),
        _ => String::new(),
    }
}

/// A card after its tool returned: `ok` or `error`, with what is shown of what came back.
pub fn returned(card: &mut Value, content: &Value, failed: bool) {
    card["status"] = Value::from(if failed { "error" } else { "ok" });
    let text = result_text(content);
    if !text.is_empty() {
        card["result"] = Value::from(text);
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn a_tool_call_is_said_in_the_words_an_app_shows() {
        let said = |name: &str, input: Value| {
            let described = describe(name, &input);
            headline(name, described.category, &described.label)
        };
        assert_eq!(
            said("Bash", json!({ "command": "npm test" })),
            "Running npm test"
        );
        assert_eq!(
            said(
                "Bash",
                json!({ "command": "bash -lc 'cd server-rs && rg wake crates'" })
            ),
            "Searching wake"
        );
        assert_eq!(
            said("Bash", json!({ "command": "rg -n needle src/" })),
            "Searching needle in src/"
        );
        assert_eq!(
            said(
                "Read",
                json!({ "file_path": "/home/user/project/src/main.rs" })
            ),
            "Reading src/main.rs"
        );
        assert_eq!(said("TodoWrite", json!({})), "Updating plan");
        assert_eq!(
            said("mcp__github__create_issue", json!({})),
            "Calling create_issue"
        );
        assert_eq!(said("Unknown", Value::Null), "Unknown Unknown");
    }

    #[test]
    fn a_command_line_is_split_as_a_shell_splits_it() {
        let words = |line: &str| shell_split(line);
        assert_eq!(words("a  b\tc").unwrap(), ["a", "b", "c"]);
        assert_eq!(
            words(r#"cat 'a b' "c d" e\ f"#).unwrap(),
            ["cat", "a b", "c d", "e f"]
        );
        assert_eq!(
            words(r#"echo "a \"b\" \n" ''"#).unwrap(),
            ["echo", r#"a "b" \n"#, ""]
        );
        assert_eq!(words("echo 'open"), None);
        assert_eq!(words("echo end\\"), None);
        assert_eq!(words("").unwrap(), Vec::<String>::new());
        assert_eq!(
            quoted_wrapper_words(r#"bash -lc 'echo '"'"'x'"'"' && ls'"#).unwrap(),
            ["bash", "-lc", "echo 'x' && ls"]
        );
        assert_eq!(quoted_wrapper_words("bash -lc 'a' b"), None);
    }
}
