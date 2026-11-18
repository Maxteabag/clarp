//! Conversation rows imported from Grok's chat history and Muse's session log.
use serde_json::{Value,json};

use crate::{grok_muse::{Tool,content}, python, told, tools};

fn text(v: &Value) -> &str { v.as_str().unwrap_or("") }

pub fn tool_name(tool: Tool, raw: &str) -> &str {
    match (tool,raw) {
        (Tool::Grok,"run_terminal_command") | (Tool::Muse,"shell" | "exec_command") => "Bash",
        (_,"read_file") => "Read",
        (Tool::Grok,"write") | (Tool::Muse,"write_file") => "Write",
        (Tool::Grok,"search_replace") | (Tool::Muse,"edit_file" | "apply_patch") => "Edit",
        (_,"grep") => "Grep",
        (Tool::Muse,"glob") | (_,"list_dir") => "Glob",
        (Tool::Grok,"todo_write") => "TodoWrite",
        (_,"web_search") => "WebSearch",
        (_,"web_fetch") => "WebFetch",
        _ => raw,
    }
}

pub fn arguments(tool: Tool, raw: &Value) -> Value {
    let args = if let Some(s) = raw.as_str() {
        serde_json::from_str(s).unwrap_or_else(|_| json!({"arguments":s}))
    } else { raw.clone() };
    let mut output = serde_json::Map::new();
    if let Some(args) = args.as_object() {
        for (key,value) in args {
            let key = match (tool,key.as_str()) {
                (Tool::Grok,"target_file") | (Tool::Muse,"path") => "file_path",
                (Tool::Grok,"target_directory") => "path",
                (Tool::Muse,"cmd") => "command",
                _ => key,
            };
            output.insert(key.to_string(),value.clone());
        }
    }
    Value::Object(output)
}

fn card(tool: Tool, call: &Value) -> Value {
    let (key,id) = match tool {
        Tool::Grok => ("arguments", text(&call["id"])),
        Tool::Muse => ("args",call["call_id"].as_str().filter(|id| !id.is_empty()).unwrap_or(text(&call["id"]))),
    };
    let mut row = tools::card(tool_name(tool,call["name"].as_str().unwrap_or("tool")), &arguments(tool,&call[key]),id);
    row["status"] = json!("pending");
    row
}

fn row(role:&str,text:&str,tools:Vec<Value>,stamp:&str) -> Value {
    json!({"role":role,"text":text,"tools":tools,"timestamp":stamp})
}

fn grok_result(turns: &mut [Value],event: &Value) {
    let id = event["tool_call_id"].as_str().or_else(|| event["tool_use_id"].as_str()).unwrap_or("");
    let raw = &event["content"];
    let result = match raw.as_array() {
        None => text(raw).into(),
        Some(parts) => parts.iter().filter_map(|p| p.as_str().or(p["text"].as_str()).or(p["content"].as_str())).filter(|s| !s.is_empty()).collect::<Vec<_>>().join("\n"),
    };
    let mut failed = python::truthy(&event["error"]) || python::truthy(&event["is_error"]);
    let mut body = result.as_str();
    if let Some(rest) = body.strip_prefix("exit: ") {
        let minus = usize::from(rest.starts_with('-'));
        let digits = rest[minus..].bytes().take_while(u8::is_ascii_digit).count();
        if digits > 0 {
            failed |= &rest[..digits+minus] != "0";
            body = rest[digits+minus..].strip_prefix('\n').unwrap_or(&rest[digits+minus..]);
        }
    }
    for turn in turns.iter_mut().rev() {
        if let Some(cards) = turn["tools"].as_array_mut()
            && let Some(card) = cards.iter_mut().rev().find(|c| (id.is_empty() || c["id"] == id) && c["status"] != "ok" && c["status"] != "error") {
            tools::returned(card,&json!(body),failed);
            return;
        }
    }
}

fn append_tools(turn: Option<&mut Value>, pending: &mut Vec<Value>) {
    if let Some(turn) = turn && turn["role"] == "assistant" && let Some(tools) = turn["tools"].as_array_mut() {
        tools.append(pending);
    }
}

pub fn grok(log: &str) -> Vec<Value> {
    let mut turns = Vec::new();
    let mut pending = Vec::new();
    for e in log.lines().filter_map(|s| serde_json::from_str::<Value>(s).ok()) {
        let stamp = text(&e["ts"]);
        match e["type"].as_str().or(e["role"].as_str()).unwrap_or("") {
            "user" => {
                append_tools(turns.last_mut(),&mut pending);
                let input = content(&e["content"]);
                let input = input.trim();
                if python::truthy(&e["synthetic_reason"]) || input.starts_with("<user_info>") || input.starts_with("<system-reminder>") { continue; }
                let input = input.strip_prefix("<user_query>").and_then(|s| s.strip_suffix("</user_query>")).unwrap_or(input).trim();
                let input = told::unheaded(input).trim();
                if !input.is_empty() { turns.push(row("user",input,vec![],stamp)); }
            }
            "assistant" => {
                pending.extend(e["tool_calls"].as_array().into_iter().flatten().filter(|v| v.is_object()).map(|v| card(Tool::Grok,v)));
                turns.push(row("assistant",&content(&e["content"]),std::mem::take(&mut pending),stamp));
            }
            "backend_tool_call" | "tool_use" => {
                let name = if e["kind"].is_object() {e["kind"]["tool_type"].as_str().unwrap_or("None")} else { e["name"].as_str().unwrap_or("tool") };
                pending.push(json!({"name":name,"status":"ok","input":{}}));
            }
            "tool_result" => grok_result(&mut turns,&e),
            _ => {}
        }
    }
    append_tools(turns.last_mut(),&mut pending);
    turns
}

pub fn muse(log: &str) -> Vec<Value> {
    let mut turns = Vec::new();
    let mut answers: Vec<usize> = Vec::new();
    let mut pending = Vec::new();
    for r in log.lines().filter_map(|s| serde_json::from_str::<Value>(s).ok()) {
        if r["payload_type"] != "runtime.session" || r["payload"]["kind"] != "run" { continue; }
        let event = &r["payload"]["event"];
        let micros = r["recorded_at"].as_i64().or_else(||text(&r["recorded_at"]).parse().ok());
        let stamp = micros.and_then(|us| jiff::Timestamp::from_microsecond(us).ok()).map(|t| format!("{t:.6}")).unwrap_or_default();
        match text(&event["kind"]) {
            "started" => {
                pending.clear();
                answers.clear();
                let input = told::unheaded(text(&event["prompt"])).trim();
                if !input.is_empty() { turns.push(row("user",input,vec![],&stamp)); }
            }
            "assistant_tool_calls_committed" => pending.extend(event["tool_calls"].as_array().into_iter().flatten().filter(|v| v.is_object()).map(|v|card(Tool::Muse,v))),
            "tool_result_batch_committed" => {
                for result in event["results"].as_array().into_iter().flatten() {
                    if let Some(c) = pending.iter_mut().rev().find(|c| c["id"] == result["tool_call_id"]) {
                        tools::returned(c,&result["text"],python::truthy(&result["error"]) || python::truthy(&result["is_error"]));
                    }
                }
            }
            "assistant_message_committed" => {
                let answer = text(&event["text"]);
                if pending.is_empty() && answer.is_empty() { continue; }
                if let Some(prev) = answers.last() { turns[*prev]["kind"] = json!("commentary"); }
                let mut r = row("assistant",answer,std::mem::take(&mut pending),&stamp);
                r["kind"] = json!("final_answer");
                answers.push(turns.len());
                turns.push(r);
            }
            "terminal" => {
                let end = text(&event["terminal"]);
                if !pending.is_empty() {
                    if let Some(prev) = answers.last() { append_tools(turns.get_mut(*prev),&mut pending); }
                    else { turns.push(row("assistant","",std::mem::take(&mut pending),&stamp)); }
                }
                if answers.is_empty() && !matches!(end,""|"completed"|"cancelled"|"interrupted") {
                    let mut why = format!("Muse Code {end}");
                    let reason = text(&event["reason"]).trim();
                    if !reason.is_empty() { why.push_str(": "); why.push_str(reason); }
                    let mut failed = row("assistant",&why,vec![],&stamp);
                    failed["failed"] = json!(true);
                    turns.push(failed);
                }
                answers.clear();
            }
            _ => {}
        }
    }
    if !pending.is_empty() {
        let stamp = turns.last().and_then(|v| v["timestamp"].as_str()).unwrap_or("");
        turns.push(row("assistant","",pending,stamp));
    }
    turns
}
