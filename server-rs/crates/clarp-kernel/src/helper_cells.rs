//! Claude Code's in-process Agent (formerly Task) is one mutable display cell.
use crate::{python, tools};
use serde_json::{Value, json};
use std::collections::BTreeMap;

pub type Children = BTreeMap<String, String>;

fn text(v: &Value) -> String {
    if !python::truthy(v) {
        String::new()
    } else if let Some(s) = v.as_str() {
        s.to_owned()
    } else {
        python::repr(v)
    }
}
fn one_line(s: &str, n: usize) -> String {
    let joined = s
        .split(python::blank)
        .filter(|w| !w.is_empty())
        .collect::<Vec<_>>()
        .join(" ");
    let first = python::first(&joined, n);
    if first.len() == joined.len() {
        joined
    } else {
        format!("{first}…")
    }
}

pub fn is_helper(name: &str) -> bool {
    matches!(name, "Agent" | "Task")
}

pub fn started(id: &str, input: &Value) -> Value {
    let description = one_line(&text(&input["description"]), 120);
    let role = text(&input["subagent_type"]);
    let role = python::strip(&role);
    let summary = if !description.is_empty() {
        description.as_str()
    } else if !role.is_empty() {
        role
    } else {
        "agent"
    };
    json!({"id":id,"kind":"subagents","title":"Running agent","summary":summary,"status":"running","lines":[],
           "ephemeral":true,"description":description,"subagent_type":role,"background":input["run_in_background"]==true,
           "agent_id":"","transcript_path":"","_task":one_line(&text(&input["prompt"]),240),"_result":"","_stats":""})
}

pub fn returned(cell: &mut Value, block: &Value, meta: &Value) {
    if let Some(id) = meta["agentId"].as_str() {
        cell["agent_id"] = json!(id);
    }
    let result = || one_line(&tools::result_text(&block["content"]), 240);
    if python::truthy(&block["is_error"]) || python::truthy(&block["isError"]) {
        cell["status"] = json!("error");
        cell["_result"] = json!(result());
        return;
    }
    if meta["isAsync"] == true || meta["status"] == "async_launched" {
        cell["background"] = json!(true);
        return;
    }
    cell["status"] = json!(
        if matches!(meta["status"].as_str(), Some("failed" | "error" | "killed")) {
            "error"
        } else {
            "ok"
        }
    );
    cell["_result"] = json!(result());
    let mut stats = Vec::new();
    if meta["totalToolUseCount"].is_i64() || meta["totalToolUseCount"].is_u64() {
        let count = &meta["totalToolUseCount"];
        stats.push(format!(
            "{count} tool call{}",
            if count == 1 { "" } else { "s" }
        ));
    }
    if let Some(ms) = meta["totalDurationMs"].as_f64() {
        let seconds = (ms / 1000.0).floor() as i128;
        stats.push(if seconds >= 60 {
            format!("{}m {}s", seconds / 60, seconds % 60)
        } else {
            format!("{seconds}s")
        });
    }
    cell["_stats"] = json!(stats.join(" · "));
}

fn field<'a>(body: &'a str, name: &str) -> &'a str {
    body.split_once(&format!("<{name}>"))
        .and_then(|(_, tail)| tail.split_once(&format!("</{name}>")))
        .map(|(s, _)| python::strip(s))
        .unwrap_or("")
}
pub fn notifications(mut text: &str, mut apply: impl FnMut(&str, &str, &str, &str)) {
    while let Some((_, rest)) = text.split_once("<task-notification>") {
        let (body, tail) = rest
            .split_once("</task-notification>")
            .unwrap_or((rest, ""));
        text = tail;
        let status = field(body, "status").to_lowercase();
        if !matches!(
            status.as_str(),
            "completed" | "failed" | "killed" | "stopped"
        ) {
            continue;
        }
        let result = field(body, "result");
        apply(
            field(body, "tool-use-id"),
            &status,
            field(body, "task-id"),
            if result.is_empty() {
                field(body, "summary")
            } else {
                result
            },
        );
    }
}
pub fn notified(cell: &mut Value, status: &str, id: &str, result: &str) {
    cell["status"] = json!(if status == "completed" { "ok" } else { "error" });
    if status != "completed" {
        cell["_stopped"] = json!(status == "stopped");
    }
    cell["background"] = json!(true);
    if !id.is_empty() && cell["agent_id"] == "" {
        cell["agent_id"] = json!(id);
    }
    cell["_result"] = json!(one_line(result, 240));
}

pub fn shown(state: &Value, children: &Children) -> Value {
    let mut cell = state.clone();
    let id = cell["id"].as_str().unwrap_or("");
    if let Some(path) = children
        .get(id)
        .or_else(|| children.get(cell["agent_id"].as_str().unwrap_or("")))
    {
        cell["transcript_path"] = json!(path);
        if cell["agent_id"] == ""
            && let Some(id) = path
                .rsplit('/')
                .next()
                .and_then(|n| n.strip_prefix("agent-"))
                .and_then(|n| n.strip_suffix(".jsonl"))
        {
            cell["agent_id"] = json!(id);
        }
    }
    cell["title"] = json!(match cell["status"].as_str() {
        Some("ok") => "Agent finished",
        Some("error") if cell["_stopped"] == true => "Agent stopped",
        Some("error") => "Agent failed",
        _ => "Running agent",
    });
    let mut lines = Vec::new();
    for (label, content, kind) in [
        ("Task", cell["_task"].as_str(), "detail"),
        ("Type", cell["subagent_type"].as_str(), "muted"),
        (
            "Mode",
            Some(if cell["background"] == true {
                "Background"
            } else {
                "Foreground"
            }),
            "muted",
        ),
        (
            if cell["status"] == "ok" {
                "Result"
            } else {
                "Status"
            },
            cell["_result"].as_str(),
            "status",
        ),
        ("Usage", cell["_stats"].as_str(), "muted"),
        ("Transcript", cell["transcript_path"].as_str(), "muted"),
    ] {
        if let Some(text) = content.filter(|s| !s.is_empty()) {
            lines.push(json!({"label":label,"text":text,"kind":kind}));
        }
    }
    cell["lines"] = json!(lines);
    if let Some(fields) = cell.as_object_mut() {
        fields.retain(|name, _| !name.starts_with('_'));
    }
    cell
}
