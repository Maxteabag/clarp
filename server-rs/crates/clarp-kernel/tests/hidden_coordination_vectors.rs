#![allow(clippy::unwrap_used)]

use clarp_kernel::hidden_coordination::{self as rules, Mode, Visibility};
use serde_json::{Value, json};

#[test]
fn coordination_choices_and_policy_follow_the_reference() {
    let vectors: Value =
        serde_json::from_str(include_str!("fixtures/hidden-coordination-vectors.json")).unwrap();
    let choices = vectors["choices"].as_array().unwrap();
    for case in choices {
        let text = |field: &str| case[field].as_str().unwrap();
        let global = case["global_choice"]
            .as_str()
            .and_then(Visibility::read)
            .unwrap_or_default();
        let chat = rules::chat_choice(text("own")).unwrap();
        let mode = Mode::parse(text("mode")).unwrap().unwrap();
        assert_eq!(json!(mode.policy(global, chat)), case["policy"]);
        assert_eq!(
            json!({"coordination_visibility": chat.unwrap_or(global), "coordination_override": chat}),
            case["snapshot"]
        );
    }
    let inputs = vectors["inputs"].as_array().unwrap();
    for input in inputs {
        let raw = input["raw"].as_str().unwrap_or("");
        let normalized = Visibility::read(raw).map_or("", Visibility::word);
        assert_eq!(normalized, input["normalized"]);
        let global = if normalized.is_empty() {
            json!({"error":rules::GLOBAL_ERROR})
        } else {
            json!(normalized)
        };
        assert_eq!(global, input["global"]);
        let chat = rules::chat_choice(raw)
            .map(|choice| json!(choice.map_or("", Visibility::word)))
            .unwrap_or_else(|error| json!({"error":error}));
        assert_eq!(chat, input["chat"]);
    }
    assert_eq!(choices.len(), 27);
    assert_eq!(inputs.len(), 6);
    assert_eq!(rules::reminder("ivy", Some("Boss")), vectors["reminder"]);
}
