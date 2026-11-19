//! What a managed Claude background Bash call is told before it starts.
use serde_json::{Value, json};

pub const TURN_CONTEXT: &str = concat!(
    "Clarp: this background command belongs to the current turn. Clarp runs one ",
    "Claude process per turn, and when your reply ends, that process exits and ",
    "this task stops with it: you will not be notified when it finishes. ",
    "A process you detach with setsid can outlive the turn and a Host restart; ",
    "nothing reports its result to you. When the result must reach you, launch the work ",
    "only with the clarp-background-jobs skill's run_detached_job.sh --goal ",
    "<your goal> (\"Completion that must wake you\"): it exits 0 only once the ",
    "job, its worker and your goal's wake are stored, and starts nothing ",
    "otherwise. Wakes follow the goal: if it or your queue is paused, or wakes ",
    "are disabled on this Host, tell the user you will not be woken."
);

pub fn valid_id(id: &str) -> bool {
    (1..=160).contains(&id.len())
        && id
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'-' | b'_'))
}

/// The reference emits origin and turn advice independently. A missing or
/// malformed identity can never supply provenance to a registered job.
pub fn background_hook(agent: &str, has_turn: bool, payload: &Value) -> Option<Value> {
    let tool = match &payload["tool_name"] {
        Value::Null => payload["tool"]["name"].as_str(),
        Value::String(name) if name.is_empty() => payload["tool"]["name"].as_str(),
        value => value.as_str(),
    };
    let input = payload["tool_input"]
        .as_object()
        .or_else(|| payload["tool"]["input"].as_object())?;
    if tool != Some("Bash") || input.get("run_in_background") != Some(&Value::Bool(true)) {
        return None;
    }
    let native = payload["session_id"].as_str().unwrap_or_default().trim();
    let call = payload["tool_use_id"].as_str().unwrap_or_default();
    let mut result = json!({"hookEventName":"PreToolUse"});
    if let Some(command) = input.get("command").and_then(Value::as_str)
        && [agent, native, call].into_iter().all(valid_id)
    {
        // All three identifiers use the grammar above. The JSON order is
        // that of the Python hook, including when checking an existing export.
        let prefix = format!(
            "export CLARP_BACKGROUND_ORIGIN='{{\"agent_id\":\"{agent}\",\"provider\":\"claude\",\"native_session_id\":\"{native}\",\"tool_use_id\":\"{call}\"}}'\n"
        );
        let mut updated = input.clone();
        if !command.starts_with(&prefix) {
            updated.insert("command".into(), Value::String(prefix + command));
        }
        result["updatedInput"] = Value::Object(updated);
    }
    if has_turn {
        result["additionalContext"] = json!(TURN_CONTEXT);
    }
    if result.get("updatedInput").is_none() && !has_turn {
        return None;
    }
    Some(json!({"hookSpecificOutput":result}))
}
