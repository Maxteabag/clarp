//! What was said and done in a session Codex kept: the turns of its rollout.
//!
//! A rollout is a file of lines, one JSON object each, and Codex has written the same
//! things in more than one way over time. What a user wrote and what the agent answered
//! come as messages of the model's API and as events, sometimes both: one message is told
//! once. What the agent did comes as items (`item.started`, `item.completed`: what
//! `codex exec --json` prints, and what Codex's app server writes under the items' own
//! names), and in older rollouts as a call and what came back for it (`function_call`,
//! `custom_tool_call`, their outputs, `patch_apply_end`, the events of the agents Codex
//! starts itself). Where a turn of Codex has a thing both ways, the older record is left
//! out.
//!
//! A turn here is one thing a user or the agent said, and for the agent the cells of what
//! it did after saying it (`clarp_kernel::tools::cells`): Codex has no messages that hold
//! their calls, so what is done hangs on the agent's last words, or on a turn with no words
//! where there were none yet. Reads, listings and searches that follow one another are one
//! cell, and no cell grows past its bounds.
//!
//! The rule is the Python Host's (`server/lib/codex_transcript.py`, `parse_turns`), held by
//! `tests/fixtures/codex-history-vectors.json`.

use std::collections::BTreeMap;

use clarp_kernel::python;
use clarp_kernel::tools::cells::{self, any_of, said, some_of, text_of, token};
use clarp_speech::markup::strip_hidden_blocks;
use serde_json::{Map, Value, json};

/// What the Python Host wrote before a user's words for the tools it could tell nothing
/// any other way, and where the user's words begin after it (`voice_preamble.py`).
const PREAMBLE: &str = "[voice-mode]";
const AFTER_PREAMBLE: &str = "\n\n--- user message ---\n";

/// What Codex puts into a conversation by itself as a message of the user's: it begins
/// with one of these.
const PUT_IN_BY_CODEX: [&str; 4] = [
    "# AGENTS.md instructions for ",
    "# AGENTS.md instructions\n",
    "<environment_context>",
    "<recommended_plugins>",
];

/// The marks an older record carries until it is known whether the same turn has the thing
/// as an item too.
const OLDER_TURN: &str = "_legacy_turn_id";
const OLDER_KIND: &str = "_legacy_category";

/// The turns of a rollout, as they are read line by line.
#[derive(Default)]
struct Reader {
    turns: Vec<Value>,
    /// How many items of a kind a turn of Codex has: as many of its older records of that
    /// kind are left out.
    as_items: BTreeMap<(String, String), usize>,
}

/// The agent's last turn: where what it does is hung. One with no words is made where the
/// last turn is not the agent's.
fn last_of_the_agent<'a>(turns: &'a mut Vec<Value>, at: &str) -> &'a mut Value {
    if turns.last().is_none_or(|turn| turn["role"] != "assistant") {
        turns.push(json!({
            "role": "assistant", "text": "", "tools": [], "display_cells": [], "timestamp": at,
        }));
    }
    let last = turns.len() - 1;
    &mut turns[last]
}

/// Something that was said. The two records Codex may write of one message are one turn:
/// the fuller text is kept, what the message is in its conversation, and the id the
/// provider gave it where a record names one (`provider_id`: the key its live row was
/// streamed under, R107).
fn say(
    turns: &mut Vec<Value>,
    role: &str,
    text: &str,
    at: &str,
    phase: Option<&Value>,
    provider_id: &str,
) {
    if text.is_empty() || !matches!(role, "user" | "assistant") {
        return;
    }
    let phase = phase.filter(|phase| python::truthy(phase));
    if let Some(last) = turns.last_mut().filter(|last| last["role"] == role) {
        let held = text_of(&[last.get("text")]);
        let plain = |text: &str| python::strip(&strip_hidden_blocks(text)).to_string();
        if held == text || plain(&held) == plain(text) {
            if text.chars().count() > held.chars().count() {
                last["text"] = Value::from(text);
            }
            if let Some(phase) = phase.filter(|_| !python::truthy(&last["kind"])) {
                last["kind"] = phase.clone();
            }
            if !provider_id.is_empty() && !python::truthy(&last["provider_id"]) {
                last["provider_id"] = Value::from(provider_id);
            }
            return;
        }
    }
    let mut turn = json!({
        "role": role, "text": text, "tools": [], "display_cells": [], "timestamp": at,
    });
    if let Some(phase) = phase {
        turn["kind"] = phase.clone();
    }
    if !provider_id.is_empty() {
        turn["provider_id"] = Value::from(provider_id);
    }
    turns.push(turn);
}

/// The text of a message of the model's API: its parts that are text, one under the other.
fn parts_text(content: Option<&Value>, named: &dyn Fn(&Value) -> bool) -> String {
    let Some(parts) = content.and_then(Value::as_array) else {
        return String::new();
    };
    let texts: Vec<&str> = parts
        .iter()
        .filter(|part| part.is_object() && named(&part["type"]))
        .filter_map(|part| part["text"].as_str())
        .filter(|text| !text.is_empty())
        .collect();
    python::strip(&texts.join("\n")).to_string()
}

/// The names Codex's app server gives its items, and the names they are read by.
fn item_kind(written: &str) -> String {
    let known = [
        ("UserMessage", "user_message"),
        ("AgentMessage", "agent_message"),
        ("CommandExecution", "command_execution"),
        ("FileChange", "file_change"),
        ("McpToolCall", "mcp_tool_call"),
        ("DynamicToolCall", "dynamic_tool_call"),
        ("CollabToolCall", "collab_tool_call"),
        ("CollabAgentToolCall", "collab_agent_tool_call"),
        ("WebSearch", "web_search"),
        ("ImageView", "image_view"),
        ("ImageGeneration", "image_generation"),
        ("Reasoning", "reasoning"),
        ("Plan", "plan"),
    ];
    match known.iter().find(|(name, _)| *name == written) {
        Some((_, read)) => (*read).to_string(),
        None => cells::token_of(written),
    }
}

/// An item with its details beside its own fields and each field under the one name it is
/// read by, whichever way it was written (`_normalize_official_item`).
fn normalized(item: &Map<String, Value>) -> Map<String, Value> {
    let mut out = item.clone();
    if let Some(Value::Object(details)) = item.get("details") {
        for (name, value) in details {
            out.insert(name.clone(), value.clone());
        }
    }
    let kind = item_kind(&text_of(&[out.get("type")]));
    out.insert("type".to_string(), Value::from(kind));
    let both_ways = [
        ("process_id", "processId"),
        ("parsed_cmd", "parsedCmd"),
        ("aggregated_output", "aggregatedOutput"),
        ("exit_code", "exitCode"),
        ("duration_ms", "durationMs"),
        ("command_actions", "commandActions"),
        ("sender_thread_id", "senderThreadId"),
        ("receiver_thread_ids", "receiverThreadIds"),
        ("new_thread_id", "newThreadId"),
        ("agents_states", "agentsStates"),
        ("content_items", "contentItems"),
        ("saved_path", "savedPath"),
        ("summary_text", "summaryText"),
        ("raw_content", "rawContent"),
    ];
    for (snake, camel) in both_ways {
        if !out.contains_key(snake)
            && let Some(value) = out.get(camel).cloned()
        {
            out.insert(snake.to_string(), value);
        }
    }
    out
}

/// What kind of thing an item is, for telling which older records it stands for.
fn kind_of_item(item: &Map<String, Value>) -> &'static str {
    match text_of(&[item.get("type")]).as_str() {
        "command_execution" | "local_shell_call" => "command",
        "file_change" | "patch_apply" => "patch",
        "mcp_tool_call" => "mcp",
        "dynamic_tool_call" => "tool",
        "collab_tool_call" | "collab_agent_tool_call" => "subagents",
        "web_search" | "web_search_call" => "web_search",
        "image_view" | "image_generation" => "image",
        "plan" => "plan",
        "reasoning" => "reasoning",
        _ => "",
    }
}

/// The turn of Codex a record belongs to.
fn turn_of(payload: &Map<String, Value>) -> String {
    let passed = payload.get("internal_chat_message_metadata_passthrough");
    let passed = passed.and_then(Value::as_object);
    text_of(&[
        payload.get("turn_id"),
        passed.and_then(|passed| passed.get("turn_id")),
    ])
}

/// A word as a shell would have to be given it (`shlex.quote`).
fn quoted(word: &str) -> String {
    let plain = |sign: char| sign.is_ascii_alphanumeric() || "_@%+=:,./-".contains(sign);
    if word.is_empty() {
        "''".to_string()
    } else if word.chars().all(plain) {
        word.to_string()
    } else {
        format!("'{}'", word.replace('\'', "'\"'\"'"))
    }
}

/// A value as Python's `int()` makes a whole number of it, where it can.
fn whole(value: Option<&Value>) -> Option<i64> {
    match value? {
        Value::Bool(truth) => Some(i64::from(*truth)),
        Value::Number(number) => number
            .as_i64()
            .or_else(|| number.as_f64().map(|real| real.trunc() as i64)),
        Value::String(text) => python::int(text).and_then(|number| i64::try_from(number).ok()),
        _ => None,
    }
}

/// A cell that carries the marks of an older record.
fn marked(mut value: Value, turn: &str, kind: &str) -> Value {
    value[OLDER_TURN] = Value::from(turn);
    value[OLDER_KIND] = Value::from(kind);
    value
}

impl Reader {
    /// An item, as `codex exec --json` prints it and as Codex's app server writes it
    /// (`_handle_modern_item`).
    fn item(&mut self, step: &str, item: &Map<String, Value>, at: &str) {
        let item = &normalized(item);
        let of = |name: &str| item.get(name);
        let kind = text_of(&[of("type")]);
        let over = step == "item.completed";
        match kind.as_str() {
            "user_message" | "agent_message" => {
                if !over {
                    return;
                }
                let text = match some_of(&[of("text"), of("message")]) {
                    Some(text) => python::strip(&said(text)).to_string(),
                    None => parts_text(of("content"), &|kind| {
                        matches!(
                            token(Some(kind)).as_str(),
                            "text" | "input_text" | "output_text"
                        )
                    }),
                };
                let role = if kind == "user_message" {
                    "user"
                } else {
                    "assistant"
                };
                // Only the agent's message names the id its live row was streamed under.
                let provider_id = if kind == "agent_message" {
                    text_of(&[of("id")])
                } else {
                    String::new()
                };
                say(&mut self.turns, role, &text, at, of("phase"), &provider_id);
                return;
            }
            "reasoning" => {
                let summed = match some_of(&[of("summary"), of("summary_text")]) {
                    Some(Value::String(one)) => vec![Value::from(one.as_str())],
                    Some(Value::Array(parts)) => parts.clone(),
                    _ => Vec::new(),
                };
                let parts = summed.iter().filter_map(Value::as_str);
                let lines: Vec<Value> = parts
                    .filter(|part| !python::strip(part).is_empty())
                    .map(|part| cells::line(part, "", "status"))
                    .collect();
                if !lines.is_empty() {
                    let cell = cells::cell(
                        "reasoning",
                        "Reasoned",
                        "",
                        "ok",
                        &text_of(&[of("id")]),
                        lines,
                    );
                    cells::replace_or_append(last_of_the_agent(&mut self.turns, at), cell);
                }
                return;
            }
            "plan" => {
                let text = text_of(&[of("text")]);
                let text = python::strip(&text);
                let lines = python::splitlines(text);
                if let Some(first) = lines.first() {
                    let shown = lines
                        .iter()
                        .map(|line| cells::line(line, "", "detail"))
                        .collect();
                    let cell =
                        cells::cell("plan", "Plan", first, "ok", &text_of(&[of("id")]), shown);
                    cells::replace_or_append(last_of_the_agent(&mut self.turns, at), cell);
                }
                return;
            }
            "error" => return,
            _ => {}
        }

        let running = !over;
        let id = text_of(&[of("id"), of("call_id")]);
        let status = text_of(&[of("status")]).to_lowercase();
        let ended = |failed: bool| match (running, failed) {
            (true, _) => "running",
            (false, true) => "error",
            (false, false) => "ok",
        };
        let or_made = |prefix: &str, about: &str| {
            if id.is_empty() {
                cells::safe_id(prefix, about)
            } else {
                id.clone()
            }
        };
        let cell = match kind.as_str() {
            "command_execution" | "local_shell_call" => {
                let command = match some_of(&[of("command")]) {
                    Some(Value::Array(words)) => {
                        let words: Vec<String> =
                            words.iter().map(|word| quoted(&said(word))).collect();
                        words.join(" ")
                    }
                    Some(command) => said(command),
                    None => String::new(),
                };
                let nothing = Value::from("");
                let output = match of("aggregated_output").filter(|output| !output.is_null()) {
                    Some(output) => output,
                    None => {
                        some_of(&[of("output"), of("stdout"), of("stderr")]).unwrap_or(&nothing)
                    }
                };
                let code = whole(of("exit_code").filter(|code| !code.is_null()));
                Some(cells::command(&command, &id, Some(output), code, running))
            }
            "mcp_tool_call" => Some(cells::mcp(item, running)),
            "dynamic_tool_call" => {
                let name = text_of(&[of("tool"), of("name")]);
                let name = if name.is_empty() {
                    "dynamic tool"
                } else {
                    &name
                };
                let failed = of("success") == Some(&Value::Bool(false)) || status == "failed";
                let mut cell = cells::generic(name, item, ended(failed));
                if let Some(result) = some_of(&[of("content_items"), of("result")]) {
                    cell["lines"] = Value::Array(cells::preview(result, 5));
                }
                Some(cell)
            }
            "web_search" | "web_search_call" => {
                let nothing = Value::from("");
                let query = some_of(&[of("query"), of("action")]).unwrap_or(&nothing);
                Some(cells::web_search(&id, Some(query), running))
            }
            "file_change" | "patch_apply" => {
                let nothing = Value::from("");
                let given = some_of(&[of("patch"), of("changes"), of("input")]).unwrap_or(&nothing);
                Some(cells::patch(&id, given, ended(status == "failed")))
            }
            "image_view" => {
                let path = text_of(&[of("path")]);
                Some(cells::cell(
                    "image",
                    "Viewed image",
                    &path,
                    "ok",
                    &or_made("image", &path),
                    Vec::new(),
                ))
            }
            "image_generation" => {
                let path = text_of(&[of("saved_path")]);
                let status = if status == "failed" {
                    "error"
                } else {
                    ended(false)
                };
                let id = or_made("image-generation", &path);
                Some(cells::cell(
                    "image",
                    "Generated image",
                    &path,
                    status,
                    &id,
                    Vec::new(),
                ))
            }
            _ => cells::collab_item(item, running)
                .or_else(|| {
                    let activity = cells::token_of(&kind) == "sub_agent_activity";
                    activity
                        .then(|| cells::collab_event("sub_agent_activity", item))
                        .flatten()
                })
                .or_else(|| {
                    if !matches!(kind.as_str(), "function_call" | "tool_call") {
                        return None;
                    }
                    let name = text_of(&[of("name"), of("tool")]);
                    let name = if name.is_empty() { "tool" } else { &name };
                    let helper = cells::subagent_call(name, item, running);
                    Some(helper.unwrap_or_else(|| cells::generic(name, item, ended(false))))
                }),
        };
        // What the agent does is hung on its last turn, also where nothing is shown of it.
        let turn = last_of_the_agent(&mut self.turns, at);
        if let Some(cell) = cell {
            cells::replace_or_append(turn, cell);
        }
    }

    /// A call as an older record has it: its card, and the cell of a call that still runs.
    fn older_call(&mut self, payload: &Map<String, Value>, at: &str) {
        let name = text_of(&[payload.get("name")]);
        let name = if name.is_empty() { "tool" } else { &name };
        let (kind, stands_for) = cells::older_kind(name, payload);
        let turn_id = turn_of(payload);
        let runs_others = cells::token_of(name) == "exec";
        let mut shown = payload.clone();
        if runs_others && stands_for != "exec" {
            shown.insert("name".to_string(), Value::from(stands_for.as_str()));
        }
        let card = cells::card(if runs_others { &stands_for } else { name }, &shown);
        let id = text_of(&[payload.get("call_id"), payload.get("id")]);
        let cell = match name {
            "exec_command" | "shell" | "local_shell" => {
                let command = cells::command_of(payload.get("arguments"));
                Some(cells::command(&command, &id, None, None, true))
            }
            "apply_patch" => None,
            "web_search" | "web_search_call" => {
                let web = Value::from("web");
                let query = some_of(&[payload.get("query"), payload.get("input")]).unwrap_or(&web);
                Some(cells::web_search(&id, Some(query), true))
            }
            _ => Some(match cells::subagent_call(name, payload, true) {
                Some(helper) => helper,
                None if runs_others => cells::nested(payload, &id),
                None => cells::older(name, payload, &id),
            }),
        };
        let turn = last_of_the_agent(&mut self.turns, at);
        if let Some(tools) = turn["tools"].as_array_mut() {
            tools.push(marked(card, &turn_id, kind));
        }
        if let Some(cell) = cell {
            cells::replace_or_append(turn, marked(cell, &turn_id, kind));
        }
    }

    /// One line of a rollout. What is no record is passed over.
    fn line(&mut self, line: &str) {
        let line = python::strip(line);
        if line.is_empty() {
            return;
        }
        let Ok(Value::Object(record)) = serde_json::from_str::<Value>(line) else {
            return;
        };
        let at = text_of(&[record.get("timestamp")]);
        let nothing = Map::new();
        let payload = record
            .get("payload")
            .and_then(Value::as_object)
            .unwrap_or(&nothing);
        let kind = match some_of(&[payload.get("type"), record.get("type")]) {
            Some(Value::String(kind)) => kind.as_str(),
            _ => return,
        };
        let of = |name: &str| payload.get(name);
        let output = || {
            let nothing = Value::from("");
            some_of(&[of("output"), of("stdout")])
                .cloned()
                .unwrap_or(nothing)
        };
        match kind {
            "message" => {
                let role = text_of(&[of("role")]);
                let text = parts_text(of("content"), &|kind| {
                    matches!(kind.as_str(), Some("input_text" | "output_text" | "text"))
                });
                // What Codex tells the model of the machine and its rules is no message of
                // the user's.
                if role == "user" && PUT_IN_BY_CODEX.iter().any(|mark| text.starts_with(mark)) {
                    return;
                }
                say(&mut self.turns, &role, &text, &at, of("phase"), "");
            }
            "item.started" | "item.updated" | "item.completed" => {
                let item = any_of(&[record.get("item")]).filter(|item| python::truthy(item));
                if let Some(Value::Object(item)) = item.or(of("item")) {
                    self.item(kind, item, &at);
                }
            }
            "item_started" | "item_updated" | "item_completed" => {
                let item = any_of(&[of("item")]).filter(|item| python::truthy(item));
                if let Some(Value::Object(item)) = item.or(record.get("item")) {
                    let read = normalized(item);
                    let (what, turn) = (kind_of_item(&read), turn_of(payload));
                    if kind == "item_completed" && !what.is_empty() && !turn.is_empty() {
                        *self.as_items.entry((turn, what.to_string())).or_default() += 1;
                    }
                    self.item(&kind.replace('_', "."), &read, &at);
                }
            }
            "collab_waiting_begin"
            | "collab_waiting_end"
            | "collab_close_begin"
            | "collab_close_end"
            | "collab_resume_begin"
            | "collab_resume_end"
            | "sub_agent_activity" => self.helper(kind, payload, &record, &at),
            kind if kind.starts_with("collab_agent_") => self.helper(kind, payload, &record, &at),
            "user_message" => {
                let text = of("message").and_then(Value::as_str).unwrap_or_default();
                let text = python::strip(text.trim_start_matches('›'));
                say(
                    &mut self.turns,
                    "user",
                    without_preamble(text),
                    &at,
                    None,
                    "",
                );
            }
            "agent_message" => {
                let text = text_of(&[of("message")]);
                say(
                    &mut self.turns,
                    "assistant",
                    python::strip(&text),
                    &at,
                    of("phase"),
                    "",
                );
            }
            "function_call" | "custom_tool_call" => self.older_call(payload, &at),
            "patch_apply_end" => {
                let turn = turn_of(payload);
                if !turn.is_empty() {
                    *self
                        .as_items
                        .entry((turn, "patch".to_string()))
                        .or_default() += 1;
                }
                let id = text_of(&[of("call_id")]);
                let holder = last_of_the_agent(&mut self.turns, &at);
                if let Some(changes @ Value::Object(files)) = of("changes")
                    && !files.is_empty()
                {
                    let went = of("success").is_none_or(python::truthy);
                    let cell = cells::patch(&id, changes, if went { "ok" } else { "error" });
                    cells::replace_or_append(holder, cell);
                }
                cells::attach_output(&mut self.turns, &id, &output());
            }
            "function_call_output" | "custom_tool_call_output" => {
                cells::attach_output(&mut self.turns, &text_of(&[of("call_id")]), &output());
            }
            _ => {}
        }
    }

    /// An event of an agent Codex started itself.
    fn helper(
        &mut self,
        kind: &str,
        payload: &Map<String, Value>,
        record: &Map<String, Value>,
        at: &str,
    ) {
        let told = if payload.is_empty() { record } else { payload };
        if let Some(cell) = cells::collab_event(kind, told) {
            cells::replace_or_append(last_of_the_agent(&mut self.turns, at), cell);
        }
    }

    /// The turns as they are shown: without the older records a turn of Codex has as items
    /// too, with what only looked at things in one cell, and every cell within its bounds.
    fn finish(mut self) -> Vec<Value> {
        for turn in &mut self.turns {
            for held in ["display_cells", "tools"] {
                let Some(values) = turn[held].as_array_mut() else {
                    continue;
                };
                let mut left = self.as_items.clone();
                values.retain(|value| {
                    let key = (
                        text_of(&[value.get(OLDER_TURN)]),
                        text_of(&[value.get(OLDER_KIND)]),
                    );
                    let stands_in =
                        !key.0.is_empty() && left.get(&key).is_some_and(|count| *count > 0);
                    if stands_in && let Some(count) = left.get_mut(&key) {
                        *count -= 1;
                    }
                    !stands_in
                });
                for value in values.iter_mut().filter_map(Value::as_object_mut) {
                    value.shift_remove(OLDER_TURN);
                    value.shift_remove(OLDER_KIND);
                }
            }
            let Some(held) = turn["display_cells"]
                .as_array()
                .filter(|held| !held.is_empty())
            else {
                continue;
            };
            let shown: Vec<Value> = gathered(held).iter().map(cells::capped).collect();
            turn["display_cells"] = Value::Array(shown);
        }
        self.turns
    }
}

/// A user's words without what the Python Host wrote before them.
pub(super) fn without_preamble(text: &str) -> &str {
    let inside = text
        .strip_prefix('"')
        .and_then(|rest| rest.strip_suffix('"'));
    let body = inside
        .filter(|inside| inside.starts_with(PREAMBLE))
        .unwrap_or(text);
    match body.find(AFTER_PREAMBLE) {
        Some(at) if body.starts_with(PREAMBLE) => &body[at + AFTER_PREAMBLE.len()..],
        _ => text,
    }
}

/// The cells of a turn with the commands that only look at things, where they follow one
/// another, as one cell: a line for each (`_coalesce_exploration_cells`).
fn gathered(held: &[Value]) -> Vec<Value> {
    let mut out = Vec::new();
    let mut looked: Vec<(&'static str, String, bool)> = Vec::new();
    let flush = |looked: &mut Vec<(&'static str, String, bool)>, out: &mut Vec<Value>| {
        if looked.is_empty() {
            return;
        }
        let running = looked.iter().any(|(_, _, running)| *running);
        let texts: Vec<&str> = looked.iter().map(|(_, text, _)| text.as_str()).collect();
        let lines = looked
            .iter()
            .map(|(label, text, _)| cells::line(text, label, "detail"));
        let (title, status) = if running {
            ("Exploring", "running")
        } else {
            ("Explored", "ok")
        };
        let id = cells::safe_id("exploration", &texts.join("|"));
        out.push(cells::cell(
            "exploration",
            title,
            "",
            status,
            &id,
            lines.collect(),
        ));
        looked.clear();
    };
    for cell in held {
        if cell["kind"] == "command"
            && let Some((label, text)) = cells::explored(&text_of(&[cell.get("summary")]))
        {
            looked.push((label, text, cell["status"] == "running"));
            continue;
        }
        flush(&mut looked, &mut out);
        out.push(cell.clone());
    }
    flush(&mut looked, &mut out);
    out
}

/// What a rollout of Codex says was said and done: its turns, each with the cells of what
/// the agent did.
pub fn codex(rollout: &str) -> Vec<Value> {
    let mut reader = Reader::default();
    for line in rollout.split(['\n', '\r']) {
        reader.line(line);
    }
    reader.finish()
}

/// The cell of one item by itself, as a conversation shows it when the item is all its
/// agent did: what `codex exec --json` prints of a command, an edit, a search or a tool
/// when it starts (`item.started`) and when it is over (`item.completed`). `None`: nothing
/// is shown of such an item.
pub fn codex_cell(step: &str, item: &Value) -> Option<Value> {
    let mut reader = Reader::default();
    reader.item(step, item.as_object()?, "");
    let mut turns = reader.finish();
    let shown = turns.first_mut()?["display_cells"].as_array_mut()?;
    if shown.is_empty() {
        None
    } else {
        Some(shown.swap_remove(0))
    }
}
