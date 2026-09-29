use clarp_core::catalog::*;
use serde_json::{Value, json};

fn catalog() -> serde_json::Map<String, Value> {
    json!({"providers": {
        "codex": {"label": "Codex", "sort_index": 2, "supports_resume": true,
                  "supported_efforts": ["low", "high"],
                  "models": [{"id": "gpt-5.5", "label": "GPT-5.5", "default_effort": "high", "supported_efforts": ["medium"]},
                             {"id": "mini"}, {"label": "no id"}]},
        "claude": {"label": "Claude", "sort_index": 1},
        "hidden": {"hidden": true},
        "missing": {"installed": false},
        "grok": {}
    }}).as_object().cloned().unwrap()
}

#[test]
fn backends_sort_by_index_and_skip_hidden_or_missing() {
    let ids: Vec<String> = backend_options(&catalog()).iter().map(|o| o["id"].as_str().unwrap().to_owned()).collect();
    assert_eq!(ids, ["claude", "codex", "grok"]);
    assert_eq!(backend_options(&catalog())[2]["label"], "grok", "label falls back to the id");
    let fallback: Vec<Value> = backend_options(&Default::default()).into_iter().map(|o| o["id"].clone()).collect();
    assert_eq!(fallback, [json!("claude"), json!("codex"), json!("agy")]);
}

#[test]
fn models_and_efforts_come_from_the_catalog() {
    let c = catalog();
    let models = models_for_backend(&c, "codex");
    assert_eq!(models.len(), 3, "provider default + two models with ids");
    assert_eq!(models[0], json!({"id": "", "label": "Provider default"}));
    assert_eq!(models[2]["label"], "mini");
    assert_eq!(default_effort_for_model(&c, "codex", "gpt-5.5"), "high");
    assert_eq!(default_effort_for_model(&c, "codex", "mini"), "");
    let efforts = efforts_for_model(&c, "codex", "gpt-5.5");
    assert_eq!(efforts[1], json!({"id": "medium", "label": "Medium"}));
    let inherited = efforts_for_model(&c, "codex", "mini");
    assert_eq!(inherited.len(), 3, "a model without efforts inherits the provider's");
    assert!(backend_supports(&c, "codex", "supports_resume"));
    assert!(!backend_supports(&c, "claude", "supports_fork"));
}
