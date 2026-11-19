//! OpenCode and DeepSeek use the same command and stream vocabulary.
use serde_json::{Value, json};

use crate::python;

pub use clarp_tool_args::opencode::{command, model_pair};

pub fn permissions(raw: &str) -> Value {
    let mut rules = serde_json::from_str::<serde_json::Map<String, Value>>(raw).unwrap_or_default();
    rules.entry("question").or_insert(json!("deny"));
    Value::Object(rules)
}

pub fn session(event: &Value) -> Option<&str> {
    ["sessionID", "session_id", "sessionId"]
        .iter()
        .filter_map(|k| event[k].as_str().map(python::strip))
        .find(|id| !id.is_empty())
        .or_else(|| {
            ["properties", "session", "data", "part"]
                .iter()
                .filter(|key| event[key].is_object())
                .find_map(|key| session(&event[key]))
        })
}

fn text(value: &Value) -> String {
    match value {
        Value::String(s) => s.clone(),
        Value::Array(parts) => parts
            .iter()
            .map(text)
            .filter(|s| !s.is_empty())
            .collect::<Vec<_>>()
            .join("\n"),
        Value::Object(_) => {
            for key in ["text", "content", "delta", "message"] {
                if let Some(s) = value[key].as_str() {
                    return s.to_owned();
                }
            }
            if value["part"].is_object() {
                return text(&value["part"]);
            }
            if value["content"].is_array() {
                return text(&value["content"]);
            }
            String::new()
        }
        _ => String::new(),
    }
}

pub fn event_kind(event: &Value) -> &str {
    let kind = if python::truthy(&event["type"]) {
        &event["type"]
    } else {
        &event["event"]
    };
    kind.as_str().unwrap_or_default()
}

pub fn assistant_text(event: &Value) -> String {
    if !matches!(
        event_kind(event),
        "text" | "message" | "assistant" | "output"
    ) || matches!(event["role"].as_str(), Some("user" | "system"))
    {
        return String::new();
    }
    ["part", "text", "content", "delta"]
        .iter()
        .map(|key| text(&event[key]))
        .find(|s| !s.is_empty())
        .unwrap_or_else(|| text(event))
}

pub fn answer(stdout: &str) -> String {
    stdout
        .lines()
        .filter_map(|line| serde_json::from_str::<Value>(line.trim()).ok())
        .map(|event| assistant_text(&event))
        .collect()
}

pub const EFFORTS: &[&str] = &["none", "minimal", "low", "medium", "high", "xhigh", "max"];

// In a verbose listing each optional JSON detail object opens and closes at column zero.
fn listing(raw: &str) -> Vec<(&str, Option<Value>)> {
    let mut entries: Vec<(&str, Option<Value>)> = Vec::new();
    let mut lines = python::splitlines(raw).into_iter();
    while let Some(line) = lines.next() {
        if line.trim_end_matches(python::blank) == "{" {
            let mut block = String::from("{");
            for line in lines.by_ref() {
                block.push('\n');
                block.push_str(line);
                if line.trim_end_matches(python::blank) == "}" {
                    if let Some((_, detail @ None)) = entries.last_mut() {
                        *detail = serde_json::from_str::<Value>(&block)
                            .ok()
                            .filter(Value::is_object);
                    }
                    break;
                }
            }
            continue;
        }
        let id = python::strip(line);
        if id.starts_with('-') || id.split('/').next().unwrap_or_default().contains(' ') {
            continue;
        }
        if !id.contains('/')
            && (!id.chars().any(char::is_lowercase) || id.chars().any(char::is_uppercase))
        {
            continue;
        }
        entries.push((id, None));
    }
    entries
}

pub fn models(raw: &str, deepseek: bool, at: &str) -> Vec<Value> {
    let mut seen = std::collections::BTreeSet::new();
    let source = if deepseek {
        "opencode models (deepseek)"
    } else {
        "opencode models"
    };
    let mut rows = Vec::new();
    for (id, detail) in listing(raw) {
        if (deepseek && !id.to_lowercase().contains("deepseek")) || !seen.insert(id) {
            continue;
        }
        let detail = detail.unwrap_or_default();
        let label = if deepseek {
            let (provider, rest) = id.split_once('/').unwrap_or((id, ""));
            let provider = match provider {
                "fireworks-ai" => "Fireworks",
                "huggingface" => "Hugging Face",
                "deepseek" => "DeepSeek",
                "opencode" => "OpenCode",
                "openrouter" => "OpenRouter",
                other => other,
            };
            let tail = rest
                .rsplit('/')
                .next()
                .filter(|s| !s.is_empty())
                .unwrap_or(id);
            format!("{tail} ({provider})")
        } else {
            let name = &detail["name"];
            let label = name.as_str().map(str::to_owned).unwrap_or_else(|| {
                if python::truthy(name) {
                    python::repr(name)
                } else {
                    String::new()
                }
            });
            let label = python::strip(&label);
            if label.is_empty() {
                id.to_string()
            } else {
                label.to_string()
            }
        };
        let variants = detail["variants"].as_object();
        let efforts: Vec<_> = EFFORTS
            .iter()
            .copied()
            .filter(|e| variants.is_none_or(|v| v.contains_key(*e)))
            .collect();
        rows.push(
            json!({"id":id,"label":label,"default_effort":null,"supported_efforts":efforts,
            "source":{"kind":"cli_probe","detail":source,"observed_at":at,"freshness":"fresh"}}),
        );
    }
    rows
}
