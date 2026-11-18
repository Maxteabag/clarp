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
