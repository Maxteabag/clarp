//! What was said in a session a tool kept: the turns of its transcript, for the
//! conversation of an agent that takes the session up.
//!
//! A transcript of Claude Code is a file of lines, one JSON object each: what the user
//! wrote, what the agent answered and which tools it used, what the tools gave back, and
//! much the tool writes for itself. A turn here is one thing a user or the agent said: its
//! words, and for the agent the tools it used with them, each as the card a conversation
//! shows of it (`clarp_kernel::tools::card`) with what came back. What nobody said is no
//! turn: what the tool put into the conversation by itself (reminders, notifications, the
//! echo of a command), the line it writes when a session is taken up again and the answer to
//! that, and an answer the tool made up when its provider failed.
//!
//! The rule is the Python Host's (`server/lib/claude_transcript.py`, `parse_turns`), held
//! by `tests/fixtures/claude-history-vectors.json`. One thing is not copied: a tool that
//! starts a helper inside the agent's own process (`Agent`, `Task`) is a card like any
//! other here, where the Python Host shows a cell of its own (`docs/differences.md`).

use std::collections::BTreeSet;

use clarp_kernel::python;
use clarp_kernel::tools;
use serde_json::{Value, json};

/// What the tool writes into a conversation by itself, as a message of the user's: it
/// begins with one of these marks.
const WRITTEN_BY_THE_TOOL: [&str; 7] = [
    "task-notification",
    "system-reminder",
    "local-command-stdout",
    "command-name",
    "command-message",
    "command-args",
    "clarp-account-recovery",
];

/// Whether a text is something the tool wrote by itself: a mark of its own at its
/// beginning, whole, in letters of either size.
fn by_the_tool(text: &str) -> bool {
    let Some(inside) = text.strip_prefix('<') else {
        return false;
    };
    WRITTEN_BY_THE_TOOL.iter().any(|mark| {
        let begins = inside.get(..mark.len());
        let after = inside[mark.len().min(inside.len())..].chars().next();
        let ends = after.is_none_or(|sign| !sign.is_alphanumeric() && sign != '_');
        begins.is_some_and(|begins| begins.eq_ignore_ascii_case(mark)) && ends
    })
}

/// Whether an answer was made up by the tool itself: when its provider failed, it writes
/// the failure down as if the agent had said it.
fn made_up(record: &Value) -> bool {
    let message = match &record["message"] {
        message @ Value::Object(_) => message,
        _ => record,
    };
    record["isApiErrorMessage"] == true
        || message["isApiErrorMessage"] == true
        || message["model"] == "<synthetic>"
}

/// What came back from a tool settles the use it answers: the newest use that is still
/// open, of that id where the answer names one.
fn settle(turns: &mut [Value], answer: &Value) {
    let said = |keys: [&str; 2]| {
        keys.iter()
            .map(|key| &answer[*key])
            .find(|value| !value.is_null())
    };
    let id = said(["tool_use_id", "toolUseId"]).and_then(Value::as_str);
    let id = id.filter(|id| !id.is_empty());
    let failed = [&answer["is_error"], &answer["isError"]];
    let failed = failed.iter().any(|flag| match flag {
        Value::Bool(yes) => *yes,
        Value::Null => false,
        Value::Number(number) => number.as_f64() != Some(0.0),
        Value::String(text) => !text.is_empty(),
        Value::Array(items) => !items.is_empty(),
        Value::Object(fields) => !fields.is_empty(),
    });
    for turn in turns.iter_mut().rev() {
        let Some(cards) = turn["tools"].as_array_mut() else {
            continue;
        };
        for card in cards.iter_mut().rev() {
            let another = id.is_some_and(|id| card["id"] != id);
            let settled = card["status"] == "ok" || card["status"] == "error";
            if another || settled {
                continue;
            }
            tools::returned(card, &answer["content"], failed);
            return;
        }
    }
}

/// The turns of a transcript of Claude Code, the oldest first: each with who said it
/// (`role`: `user` or `assistant`), its `text`, the `tools` used with it and its
/// `timestamp` as the transcript writes it.
pub fn claude(transcript: &str) -> Vec<Value> {
    let mut turns: Vec<Value> = Vec::new();
    // The lines the tool writes when a session is taken up again: neither they nor the
    // answers to them were said by anybody.
    let mut taken_up: BTreeSet<String> = BTreeSet::new();
    for line in transcript.lines() {
        let Ok(Value::Object(record)) = serde_json::from_str::<Value>(line) else {
            continue;
        };
        let record = Value::Object(record);
        let role = match record["type"].as_str() {
            Some(role @ ("user" | "assistant")) => role,
            _ => continue,
        };
        if record["isMeta"] == true {
            let id = record["uuid"].as_str().filter(|id| !id.is_empty());
            taken_up.extend(id.map(str::to_string));
            continue;
        }
        let answers = record["parentUuid"].as_str();
        if role == "assistant"
            && (answers.is_some_and(|id| taken_up.contains(id)) || made_up(&record))
        {
            continue;
        }
        let (mut said, mut cards) = (Vec::new(), Vec::new());
        match &record["message"]["content"] {
            Value::String(text) => said.push(text.as_str()),
            Value::Array(parts) => {
                for part in parts {
                    match part["type"].as_str() {
                        Some("text") => said.extend(part["text"].as_str()),
                        Some("tool_use") => {
                            let name = part.get("name").and_then(Value::as_str).unwrap_or("tool");
                            let id = part["id"].as_str().unwrap_or_default();
                            cards.push(tools::card(name, &part["input"], id));
                        }
                        Some("tool_result") => settle(&mut turns, part),
                        _ => {}
                    }
                }
            }
            _ => {}
        }
        let said: Vec<&str> = said.into_iter().filter(|text| !text.is_empty()).collect();
        let text = python::strip(&said.join("\n")).to_string();
        if text.is_empty() && cards.is_empty() {
            continue;
        }
        if role == "user" && cards.is_empty() && by_the_tool(&text) {
            continue;
        }
        let at = record
            .get("timestamp")
            .cloned()
            .unwrap_or_else(|| json!(""));
        turns.push(json!({ "role": role, "text": text, "tools": cards, "timestamp": at }));
    }
    turns
}
