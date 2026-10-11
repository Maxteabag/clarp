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

fn profiled() -> serde_json::Map<String, Value> {
    let mut c = catalog();
    c["providers"]["codex"]["provider_profiles"] = json!([
        {"id": "azure-eu", "label": "Codex (EU, Azure)", "detail": "Azure OpenAI in the EU. Use text input, not voice, for real cases with this agent.",
         "operating_rule": "Use text input, not voice, for real cases with this agent.", "available": true, "default_model": "sol61-eu",
         "models": [{"id": "sol61-eu", "label": "sol61-eu", "supported_efforts": ["low", "medium", "high", "xhigh"]}, {"label": "no id"}]},
        {"id": "", "label": "no id"},
        {"id": "off", "models": [{"id": "m1"}]}
    ]);
    c
}

#[test]
fn provider_profiles_parse_with_their_models_and_availability() {
    let profiles = provider_profiles(&profiled());
    assert_eq!(profiles.len(), 2, "a profile without an id is skipped");
    let eu = &profiles[0];
    assert_eq!((eu.id.as_str(), eu.label.as_str(), eu.default_model.as_str()), ("azure-eu", "Codex (EU, Azure)", "sol61-eu"));
    assert!(eu.available);
    assert_eq!(eu.operating_rule, "Use text input, not voice, for real cases with this agent.");
    assert_eq!(eu.models.len(), 1, "a model without an id is skipped");
    let off = &profiles[1];
    assert!(!off.available, "a profile that does not say it is available is not");
    assert_eq!(off.label, "off", "label falls back to the id");
    assert_eq!(off.default_model, "m1", "the first model stands in for a missing default");
    assert!(provider_profiles(&catalog()).is_empty(), "an older Host lists none");
}

#[test]
fn a_profile_choice_splits_into_backend_and_profile() {
    assert_eq!(profile_choice("azure-eu"), "codex@azure-eu");
    assert_eq!(split_choice("codex@azure-eu"), ("codex", Some("azure-eu")));
    assert_eq!(split_choice("codex"), ("codex", None));
    assert_eq!(split_choice("claude@x"), ("claude@x", None), "only Codex has profiles");
    assert_eq!(split_choice("codex@"), ("codex@", None));
}

#[test]
fn launch_options_put_profiles_after_codex_only_when_the_host_has_them() {
    let ids = |options: Vec<Value>| -> Vec<String> { options.iter().map(|o| o["id"].as_str().unwrap().to_owned()).collect() };
    assert_eq!(ids(launch_options(&profiled(), true)), ["claude", "codex", "codex@azure-eu", "codex@off", "grok"]);
    assert_eq!(ids(launch_options(&profiled(), false)), ["claude", "codex", "grok"], "an older Host's contract shows nothing new");
    assert_eq!(ids(launch_options(&catalog(), true)), ["claude", "codex", "grok"]);
    let eu = &launch_options(&profiled(), true)[2];
    assert_eq!(eu["label"], "Codex (EU, Azure)");
    assert_eq!(eu["available"], true);
    assert_eq!(launch_options(&profiled(), true)[3]["available"], false);
}

#[test]
fn a_profile_offers_only_its_models_and_efforts() {
    let c = profiled();
    let models = models_for_backend(&c, "codex@azure-eu");
    assert_eq!(models, vec![json!({"id": "", "label": "Provider default"}), json!({"id": "sol61-eu", "label": "sol61-eu"})]);
    let efforts: Vec<Value> = efforts_for_model(&c, "codex@azure-eu", "sol61-eu").into_iter().map(|e| e["id"].clone()).collect();
    assert_eq!(efforts, [json!(""), json!("low"), json!("medium"), json!("high"), json!("xhigh")]);
    assert_eq!(efforts_for_model(&c, "codex@azure-eu", "").len(), 5, "the default model's efforts");
    assert_eq!(efforts_for_model(&c, "codex@off", "m1").len(), 1, "no Codex efforts leak into a profile");
    assert!(models_for_backend(&c, "codex@gone").len() == 1);
    assert!(!backend_supports(&c, "codex@azure-eu", "supports_resume"));
    assert!(backend_supports(&c, "codex", "supports_resume"));
}

#[test]
fn profile_refusal_codes_read_as_sentences() {
    assert!(profile_refusal_text("provider_profile_boundary").unwrap().starts_with("Message refused: an agent in the EU data zone"));
    assert!(profile_refusal_text("provider_profile_locked").unwrap().contains("start a new agent instead"));
    assert!(profile_refusal_text("provider_profile_mismatch").is_some());
    assert_eq!(profile_refusal_text("An agent's provider profile and backend are fixed"), None, "the Host's own sentence stays");
}
