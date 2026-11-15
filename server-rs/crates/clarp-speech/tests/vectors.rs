//! The port against the Python Host's own answers, and against the repository's contract for
//! what is shown.
//!
//! `fixtures/markup-vectors.json` is written by `server-rs/tools/markup-vectors.py` from
//! `server/lib/voice_markup.py`: several hundred texts through every function. When the Python
//! module changes, write the vectors again and make this pass.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_speech::markup;
use serde_json::Value;

fn vectors() -> Value {
    let path = format!(
        "{}/tests/fixtures/markup-vectors.json",
        env!("CARGO_MANIFEST_DIR")
    );
    serde_json::from_str(&std::fs::read_to_string(&path).unwrap_or_else(|e| panic!("{path}: {e}")))
        .unwrap()
}

/// Runs one function over its vectors and says how many answers differ, with the first few.
fn check(name: &str, port: impl Fn(&[Value]) -> Value) {
    let all = vectors();
    let rows = all[name]
        .as_array()
        .unwrap_or_else(|| panic!("no vectors for {name}"));
    assert!(rows.len() > 100, "{name}: the vectors are there");
    let mut wrong = Vec::new();
    for row in rows {
        let row = row.as_array().unwrap();
        let (given, want) = row.split_at(row.len() - 1);
        let got = port(given);
        if got != want[0] {
            wrong.push(format!(
                "  given {given:?}\n   want {}\n    got {got}",
                want[0]
            ));
        }
    }
    assert!(
        wrong.is_empty(),
        "{name}: {} of {} differ from the Python Host\n{}",
        wrong.len(),
        rows.len(),
        wrong.iter().take(8).cloned().collect::<Vec<_>>().join("\n")
    );
}

fn text(given: &[Value]) -> &str {
    given[0].as_str().unwrap()
}

#[test]
fn what_is_shown_is_what_the_python_host_shows() {
    check("clean_for_display", |given| {
        markup::clean_for_display(text(given), false).into()
    });
    check("clean_for_display_oneline", |given| {
        markup::clean_for_display(text(given), true).into()
    });
    check("strip_hidden_blocks", |given| {
        markup::strip_hidden_blocks(text(given)).into()
    });
}

#[test]
fn what_is_spoken_is_what_the_python_host_speaks() {
    check("spoken_for_tts", |given| {
        markup::spoken_for_tts(text(given)).into()
    });
    check("strip_emotion_tags", |given| {
        markup::strip_emotion_tags(text(given)).into()
    });
    check("strip_ssml_for_plain_tts", |given| {
        markup::strip_ssml_for_plain_tts(text(given)).into()
    });
    check("speak_blocks_bare", |given| {
        markup::speak_blocks(text(given), false).into()
    });
    check("speak_blocks_attributes", |given| {
        markup::speak_blocks(text(given), true).into()
    });
    check("spoken_chunks", |given| {
        let limit = usize::try_from(given[1].as_u64().unwrap()).unwrap();
        markup::spoken_chunks_for_tts(text(given), limit).into()
    });
}

/// `contract/fixtures/voice-display.json` is the contract every client runs. The Host never
/// shows a reply that is still arriving, so it skips those cases, as the Python Host does.
#[test]
fn the_contract_for_what_is_shown_holds() {
    let path = format!(
        "{}/../../../contract/fixtures/voice-display.json",
        env!("CARGO_MANIFEST_DIR")
    );
    let fixture: Value = serde_json::from_str(
        &std::fs::read_to_string(&path).unwrap_or_else(|e| panic!("{path}: {e}")),
    )
    .unwrap();
    let cases = fixture["cases"].as_array().unwrap();
    let mut run = 0;
    for case in cases {
        let name = case["name"].as_str().unwrap();
        if case["streaming"] == true || name.starts_with("streaming") {
            continue;
        }
        run += 1;
        assert_eq!(
            markup::clean_for_display(case["input"].as_str().unwrap(), false),
            case["expect"].as_str().unwrap(),
            "{name}"
        );
    }
    assert!(run >= 40, "the contract's cases were run: {run}");
}
