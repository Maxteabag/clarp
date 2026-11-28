//! The shared vectors of what a spoken answer to a request means
//! (`contract/fixtures/decision-voice-match.json`): every case is answered as it says.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_kernel::artifacts::decisions::Decision;
use clarp_kernel::artifacts::voice_match::{Commands, voice_match};
use serde_json::{Value, json};

fn request(shape: &Value) -> Decision {
    let mut fields = json!({
        "decision_id": "decision-voice",
        "question": "Which one?",
        "context": "",
        "status": "pending",
        "resolved_choice": "",
        "resolved_by": "",
        "revision": 1,
        "allow_custom_text": true,
        "blocks_progress": false,
        "priority_reason": "",
        "urgency": "normal",
        "response_effort": "quick",
    });
    for (key, value) in shape.as_object().unwrap() {
        fields[key] = value.clone();
    }
    serde_json::from_value(fields).unwrap()
}

#[test]
fn every_spoken_answer_means_what_the_vectors_say() {
    let path = concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../../contract/fixtures/decision-voice-match.json"
    );
    let vectors: Value = serde_json::from_slice(&std::fs::read(path).unwrap()).unwrap();
    let mut wrong = Vec::new();
    let cases = vectors["cases"].as_array().unwrap();
    assert!(cases.len() > 40);
    for case in cases {
        let decision = request(&vectors["requests"][case["request"].as_str().unwrap()]);
        let commands = Commands::of(case.get("commands"));
        let said = voice_match(&decision, case["text"].as_str().unwrap(), &commands);
        if said != case["expect"] {
            wrong.push(format!(
                "{} {:?}: {said}, expected {}",
                case["request"], case["text"], case["expect"]
            ));
        }
    }
    assert!(wrong.is_empty(), "{}", wrong.join("\n"));
}
