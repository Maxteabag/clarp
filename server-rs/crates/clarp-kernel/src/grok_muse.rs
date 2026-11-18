//! Command lines and stream text for Grok Build and Muse Code.
use serde_json::Value;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Tool {
    Grok,
    Muse,
}

impl Tool {
    pub fn id(self) -> &'static str {
        match self {
            Self::Grok => "grok",
            Self::Muse => "muse",
        }
    }

    pub fn arguments(self, session: &str, fresh: bool, model: &str, effort: &str) -> Vec<String> {
        let flags: &[&str] = match self {
            Self::Grok => &["--always-approve", "--no-alt-screen", "--output-format", "streaming-json"],
            Self::Muse => &["exec", "--json", "--yolo"],
        };
        let mut args: Vec<_> = flags.iter().map(|s| s.to_string()).collect();
        let session_flag = if self == Self::Grok && !fresh { "--resume" } else { "--session-id" };
        for (flag, value) in [("--model", model), ("--reasoning-effort", effort), (session_flag, session)] {
            if !value.is_empty() {
                args.extend([flag.to_string(), value.to_string()]);
            }
        }
        args
    }

    pub fn prompt_flag(self) -> &'static str {
        match self {
            Self::Grok => "-p",
            Self::Muse => "--",
        }
    }

    pub fn question(self, model: &str, effort: &str, prompt: &str) -> Vec<String> {
        let mut args = self.arguments("", false, model, effort);
        if self == Self::Muse {
            args.push("--no-session-log".into());
        }
        args.extend([self.prompt_flag().into(), prompt.into()]);
        args
    }

    pub fn answer(self, printed: &str) -> String {
        let mut text = String::new();
        for line in printed.lines() {
            let Ok(event) = serde_json::from_str::<Value>(line) else { continue };
            if self == Self::Muse && event["payload_type"] == "run.terminal.completed"
                && let Some(answer) = event["payload"]["text"].as_str().filter(|s| !s.is_empty()) {
                return answer.into();
            }
            text.push_str(&self.delta(&event));
        }
        text
    }

    pub fn delta(self, event: &Value) -> String {
        if self == Self::Muse {
            return if event["payload_type"] == "run.output.delta" {
                word(&event["payload"]["text"]).into()
            } else { String::new() };
        }
        if event["method"] == "session/update" {
            let update = &event["params"]["update"];
            return if matches!(word(&update["sessionUpdate"]), "agent_message_chunk" | "agent_message") {
                content(&update["content"])
            } else { String::new() };
        }
        let kind = event_kind(event);
        if kind == "text" && let Some(data) = event["data"].as_str() {
            return data.into();
        }
        if matches!(kind, "assistant" | "agent_message" | "message" | "text" | "output_text")
            && matches!(word(&event["role"]), "" | "assistant" | "model") {
            return ["content", "text", "message", "delta"].iter()
                .map(|key| content(&event[*key])).find(|s| !s.is_empty()).unwrap_or_default();
        }
        String::new()
    }
}

pub fn event_kind(event: &Value) -> &str {
    event["type"].as_str().filter(|s| !s.is_empty()).unwrap_or_else(|| word(&event["event"]))
}

fn word(value: &Value) -> &str {
    value.as_str().unwrap_or("")
}

pub fn content(value: &Value) -> String {
    match value {
        Value::String(text) => text.clone(),
        Value::Object(_) => match value["text"].as_str() {
            Some(text) => text.to_string(),
            None if value["content"].is_string() || value["content"].is_array() => content(&value["content"]),
            None => String::new(),
        },
        Value::Array(parts) => parts.iter().map(content).filter(|s| !s.is_empty()).collect::<Vec<_>>().join("\n"),
        _ => String::new(),
    }
}
