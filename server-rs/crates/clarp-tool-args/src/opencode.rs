use serde_json::{Value, json};

/// The arguments after the executable for a run or a single routing question.
pub fn command(session: &str, model: &str, effort: &str, thinking: bool) -> Vec<String> {
    let mut args = ["run", "--format", "json", "--auto"]
        .map(String::from)
        .to_vec();
    if thinking {
        args.push("--thinking".into());
    }
    for (flag, value) in [
        ("--model", model),
        ("--variant", effort),
        ("--session", session),
    ] {
        if !value.is_empty() {
            args.extend([flag.to_owned(), value.to_owned()]);
        }
    }
    args
}

pub fn model_pair(model: &str) -> Option<Value> {
    let (provider, model) = model.split_once('/')?;
    (!provider.is_empty() && !model.is_empty())
        .then(|| json!({"providerID": provider, "modelID": model}))
}

/// Whether an assistant message's `info` is OpenCode's own compaction: the summary of the
/// conversation it writes for itself when it compacts its context (`session/compaction.ts`:
/// `mode` and `agent` `compaction`, `summary: true`). It is no answer to anyone: no reply of the
/// turn, and no reading of a message the turn was given (issue 874).
pub fn compaction(info: &Value) -> bool {
    info["role"] == "assistant"
        && (info["summary"] == true
            || info["mode"] == "compaction"
            || info["agent"] == "compaction")
}

/// A retry OpenCode announces (`session.status` of type `retry`) that no retry gets past: the
/// provider refused the account itself (a free tier's limit, credits, a plan to buy), so OpenCode
/// would retry without end. Answers the provider's words. A retry of a passing trouble (a rate
/// limit, an overloaded provider) is none: OpenCode's own retry gets past it.
///
/// OpenCode 1.18 says such a refusal with an `action` for the user (`free_tier_limit`, measured
/// for issue 721); its words name the usage, the credits or what to subscribe to.
pub fn refused(status: &Value) -> Option<String> {
    if status["type"] != "retry" {
        return None;
    }
    let message = status["message"].as_str().unwrap_or("").trim();
    let lower = message.to_lowercase();
    let about_the_account = [
        "usage",
        "quota",
        "credit",
        "subscribe",
        "billing",
        "payment",
    ]
    .iter()
    .any(|word| lower.contains(word));
    if !status["action"].is_object() && !about_the_account {
        return None;
    }
    let said = if message.is_empty() {
        status["action"]["title"].as_str().unwrap_or("").trim()
    } else {
        message
    };
    Some(if said.is_empty() {
        "OpenCode's provider refused the turn".to_string()
    } else {
        said.to_string()
    })
}
