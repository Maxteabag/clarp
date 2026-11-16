//! The rules of speech in, against the Python Host's.
//!
//! `fixtures/speech-vectors.json` is written by `server-rs/tools/speech-vectors.py` from the
//! Python Host's own functions: what a job may be called, when two requests are the same job,
//! which engine runs a clip, which of an engine's segments are kept, when a text is something
//! an engine invented on silence, and the small rules around them. The same must be decided
//! here.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_kernel::heard::{
    CLOUD_PROVIDERS, INVALID_ID, Segment, ends_terminal, file_name, fingerprint, invented,
    is_cloud, job_id, joined, plan, provider_of, terms, truthy, wants_cloud_engine, whole,
};
use serde_json::Value;

fn vectors() -> Value {
    serde_json::from_str(include_str!("fixtures/speech-vectors.json")).unwrap()
}

fn bytes(hex: &str) -> Vec<u8> {
    (0..hex.len())
        .step_by(2)
        .map(|at| u8::from_str_radix(&hex[at..at + 2], 16).unwrap())
        .collect()
}

fn texts(list: &Value) -> Vec<String> {
    let list = list.as_array().unwrap().iter();
    list.map(|text| text.as_str().unwrap().to_string())
        .collect()
}

#[test]
fn a_job_is_called_what_the_python_host_lets_it_be_called() {
    let vectors = vectors();
    let cases = vectors["job_ids"].as_array().unwrap();
    assert!(cases.len() >= 30);
    for case in cases {
        let named = job_id(case["given"].as_str());
        match case.get("answer") {
            Some(answer) => assert_eq!(named.as_deref(), Ok(answer.as_str().unwrap()), "{case}"),
            None => {
                assert_eq!(named, Err(INVALID_ID), "{case}");
                assert_eq!(case["message"], INVALID_ID);
            }
        }
    }
}

#[test]
fn two_requests_are_the_same_job_when_the_python_host_says_so() {
    let vectors = vectors();
    let cases = vectors["fingerprints"].as_array().unwrap();
    assert!(cases.len() >= 100);
    for case in cases {
        let made = fingerprint(
            &bytes(case["audio_hex"].as_str().unwrap()),
            case["content_type"].as_str().unwrap(),
            case["model"].as_str().unwrap(),
            case["hands_free"].as_bool().unwrap(),
        );
        assert_eq!(made, case["fingerprint"].as_str().unwrap(), "{case}");
    }
}

#[test]
fn a_clip_goes_to_the_engine_the_python_host_would_send_it_to() {
    let vectors = vectors();
    let cases = vectors["plans"].as_array().unwrap();
    assert!(cases.len() >= 144);
    for case in cases {
        let said = |key: &str| case[key].as_str().unwrap();
        let agent_engines = texts(&case["agent_engines"]);
        let in_call = case["in_call"].as_bool().unwrap();
        let planned = plan(
            said("requested"),
            &agent_engines,
            said("cloud_engine"),
            said("long_model"),
            in_call,
        );
        assert_eq!(planned.model, said("model"), "{case}");
        assert_eq!(
            planned.agent_engine.as_deref(),
            case["agent_engine"].as_str(),
            "{case}"
        );
        assert_eq!(planned.fallbacks, texts(&case["fallbacks"]), "{case}");
        assert_eq!(
            planned.escalated_from.as_deref(),
            case["escalated_from"].as_str(),
            "{case}"
        );
        assert_eq!(
            wants_cloud_engine(said("requested"), &agent_engines, in_call),
            case["wants_cloud_engine"].as_bool().unwrap(),
            "{case}"
        );
    }
}

#[test]
fn what_an_engine_invents_on_silence_is_what_the_python_host_throws_away() {
    let vectors = vectors();
    let cases = vectors["invented"]["texts"].as_array().unwrap();
    assert!(cases.len() >= 120);
    for case in cases {
        let text = case["text"].as_str().unwrap();
        assert_eq!(
            invented(text),
            case["invented"].as_bool().unwrap(),
            "{text:?}"
        );
    }
    // The list itself is the Python Host's: every phrase of it, by itself, is thrown away,
    // and so is nothing else that is one plain word of real speech.
    for phrase in texts(&vectors["invented"]["phrases"]) {
        assert!(invented(&phrase), "{phrase:?}");
    }
    assert!(!invented("status"));
}

#[test]
fn the_pieces_an_engine_is_sure_of_are_what_was_heard() {
    let vectors = vectors();
    let cases = vectors["joined"].as_array().unwrap();
    assert!(cases.len() >= 11);
    for case in cases {
        let segments: Vec<Segment> = case["segments"]
            .as_array()
            .unwrap()
            .iter()
            .map(|segment| Segment {
                text: segment["text"].as_str().unwrap().to_string(),
                // A piece that does not say how sure it is of speech is kept.
                no_speech_prob: segment["no_speech_prob"].as_f64().unwrap_or(0.0),
            })
            .collect();
        assert_eq!(
            joined(&segments),
            case["joined"].as_str().unwrap(),
            "{case}"
        );
    }
}

#[test]
fn the_small_rules_are_the_python_hosts() {
    let vectors = vectors();
    let each = |key: &str| vectors[key].as_array().unwrap().clone();
    for case in each("eleven_file_names") {
        let kind = case["content_type"].as_str().unwrap();
        assert_eq!(file_name(kind), case["name"].as_str().unwrap(), "{kind:?}");
    }
    for case in each("ends_terminal") {
        let text = case["text"].as_str().unwrap();
        assert_eq!(
            ends_terminal(text),
            case["ends"].as_bool().unwrap(),
            "{text:?}"
        );
    }
    for case in each("providers") {
        let model = case["model"].as_str().unwrap();
        assert_eq!(
            provider_of(model),
            case["provider"].as_str().unwrap(),
            "{model:?}"
        );
        assert_eq!(
            is_cloud(model),
            case["cloud"].as_bool().unwrap(),
            "{model:?}"
        );
    }
    for case in each("terms") {
        let payload = case["payload"].as_str().unwrap();
        assert_eq!(terms(payload), texts(&case["terms"]), "{payload:?}");
    }
    for case in each("truthy") {
        let given = case["given"].as_str();
        assert_eq!(truthy(given), case["yes"].as_bool().unwrap(), "{given:?}");
    }
    for case in each("whole") {
        let given = case["given"].as_str();
        assert_eq!(whole(given), case["answer"].as_i64(), "{given:?}");
    }
}

#[test]
fn the_cloud_providers_are_the_ones_the_python_host_lists() {
    let vectors = vectors();
    let listed: Vec<String> = vectors["catalogue"]["cloud"]
        .as_array()
        .unwrap()
        .iter()
        .map(|provider| provider["id"].as_str().unwrap().to_string())
        .collect();
    assert_eq!(listed, CLOUD_PROVIDERS);
}
