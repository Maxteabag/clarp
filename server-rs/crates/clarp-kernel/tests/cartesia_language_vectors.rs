//! The Python Host's Cartesia catalogue, request languages and agent-language resolution.

#![allow(clippy::unwrap_used)]

use clarp_kernel::cartesia_language;
use clarp_kernel::languages;
use clarp_kernel::said::Who;
use serde_json::{Value, json};

#[test]
fn cartesia_languages_are_bounded_by_the_configured_model() {
    let fixture: Value =
        serde_json::from_str(include_str!("fixtures/cartesia-language-vectors.json")).unwrap();
    let models = fixture["model_languages"].as_object().unwrap();
    for (model, expected) in models {
        let mut found: Vec<_> = cartesia_language::model_languages(model).collect();
        found.sort_unstable();
        assert_eq!(json!(found), *expected, "{model}");
    }
    let requests = fixture["requests"].as_array().unwrap();
    for case in requests {
        let model = case["model"].as_str().unwrap();
        let code = case["code"].as_str().unwrap_or("");
        let actual = cartesia_language::language_for(code, model);
        assert_eq!(actual.code, case["language"], "{case}");
        let log = actual.unsupported.map_or_else(
            || json!([]),
            |primary| {
                let model = if model.is_empty() { "-" } else { model };
                json!([[
                    "cartesiaLanguageUnsupported",
                    format!("{primary} on {model} -> en")
                ]])
            },
        );
        assert_eq!(log, case["logs"], "{case}");
    }
    assert_eq!((models.len(), requests.len()), (20, 400));
}

#[test]
fn cartesia_uses_the_first_language_matched_for_hearing() {
    let fixture: Value =
        serde_json::from_str(include_str!("fixtures/cartesia-language-vectors.json")).unwrap();
    let cases = fixture["agents"].as_array().unwrap();
    for case in cases {
        let agent = &case["agent"];
        let who = agent.is_object().then(|| Who {
            agent_id: "",
            name: agent["name"].as_str().unwrap_or(""),
            persona: agent["persona"].as_str().unwrap_or(""),
            session: agent["session"].as_str().unwrap_or(""),
        });
        // Rust uses the settings snapshot that was already read. A failed source read is
        // represented by the missing value in that snapshot.
        let map = (case["read_error"] != "agent")
            .then_some(case["map"].as_str())
            .flatten();
        let default = (case["read_error"] == "")
            .then_some(case["default"].as_str())
            .flatten();
        let overrides = languages::languages(map);
        let selected = languages::selected(default, "");
        assert_eq!(
            cartesia_language::agent_speech_language(&overrides, who, &selected),
            case["language"],
            "{case}"
        );
    }
    assert_eq!(cases.len(), 13);
}
