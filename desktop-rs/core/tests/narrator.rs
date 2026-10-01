//! Ports of the C++ client's `tst_tool_narrator.cpp` for the shared-Host narrator,
//! driving the state machine directly and asserting the effects it returns.

use clarp_core::narrator::*;
use serde_json::{Value, json};

fn obj(value: Value) -> serde_json::Map<String, Value> {
    value.as_object().cloned().unwrap()
}

fn command() -> serde_json::Map<String, Value> {
    obj(json!({"name": "Bash", "command": "cmake --build desktop/build/dev", "_session": "felix-a2e1"}))
}

fn requests(effects: &[Effect]) -> Vec<(String, Value)> {
    effects
        .iter()
        .filter_map(|e| match e {
            Effect::HostRequest { tag, body } => Some((tag.clone(), body.clone())),
            _ => None,
        })
        .collect()
}

fn answer(body: &Value, status: &str, text: &str) -> serde_json::Map<String, Value> {
    let items: Vec<Value> = body["items"]
        .as_array()
        .unwrap()
        .iter()
        .map(|item| json!({"id": item["id"], "status": status, "text": text}))
        .collect();
    obj(json!({"items": items}))
}

#[test]
fn shared_host_polls_until_ready_and_caches() {
    let mut narrator = Narrator::new();
    assert!(narrator.request(&command()).is_empty(), "off: no request");
    narrator.set_detail_level(3);
    let effects = narrator.request(&command());
    assert!(effects.contains(&Effect::Debounce));
    let effects = narrator.start_batch();
    let sent = requests(&effects);
    assert_eq!(sent.len(), 1);
    let (tag, body) = &sent[0];
    assert_eq!(body["session"], "felix-a2e1");
    assert_eq!(body["detail_level"], 3);
    assert!(body["items"][0]["activity"].get("_session").is_none(), "the session travels once, not per item");
    assert!(effects.iter().any(|e| matches!(e, Effect::Timeout { .. })));
    // First answer: still pending, so the narrator polls again.
    let effects = narrator.handle_reply(tag, &answer(body, "pending", ""));
    assert!(effects.iter().any(|e| matches!(e, Effect::Poll { .. })));
    assert!(narrator.status().starts_with("Host translating"));
    let resent = requests(&narrator.poll_for(tag));
    assert_eq!(resent.len(), 1);
    narrator.handle_reply(tag, &answer(body, "ready", "Build the desktop preview."));
    assert_eq!(narrator.explanation(&command()), "Build the desktop preview.");
    assert!(narrator.request(&command()).is_empty(), "a cached explanation is never requested again");
    narrator.reset();
    assert_eq!(narrator.explanation(&command()), "");
}

#[test]
fn payload_redacts_and_never_sends_results() {
    let mut activity = command();
    activity.insert("status".into(), json!("error"));
    activity.insert("result".into(), json!("PRIVATE OUTPUT NEVER SENT"));
    activity.insert("command".into(), json!("curl -H 'Authorization: Bearer very-secret' https://example.test"));
    activity.insert("input".into(), json!({"cmd": "cmake --build", "result": "PRIVATE NESTED OUTPUT", "token": "PRIVATE JSON TOKEN"}));
    let sent = Value::Object(payload(&activity).unwrap()).to_string();
    for secret in ["PRIVATE OUTPUT", "PRIVATE NESTED OUTPUT", "PRIVATE JSON TOKEN", "very-secret"] {
        assert!(!sent.contains(secret), "{secret} leaked: {sent}");
    }
    assert!(sent.contains("[redacted]"));
    assert_eq!(snippet("key sk-abcdefghijklmnop end", 100), "key [redacted] end");
    assert!(payload(&obj(json!({}))).is_none(), "nothing to explain");
    assert!(payload(&obj(json!({"lines": [{"label": "Read", "text": "x", "kind": "output"}]}))).is_none());
}

#[test]
fn identical_requests_are_deduplicated_and_results_do_not_change_the_key() {
    let mut narrator = Narrator::new();
    narrator.set_enabled(true);
    narrator.request(&command());
    narrator.request(&command());
    let mut updated = command();
    updated.insert("status".into(), json!("error"));
    updated.insert("result".into(), json!("streamed output"));
    narrator.request(&updated);
    let sent = requests(&narrator.start_batch());
    assert_eq!(sent[0].1["items"].as_array().unwrap().len(), 1, "one activity despite three requests");
    narrator.handle_reply(&sent[0].0, &answer(&sent[0].1, "ready", "Builds it."));
    assert_eq!(narrator.explanation(&updated), narrator.explanation(&command()));
}

#[test]
fn viewport_owners_share_and_release_queued_activity() {
    let mut narrator = Narrator::new();
    narrator.set_detail_level(3);
    narrator.acquire_view(1, &command());
    narrator.acquire_view(2, &command());
    narrator.release_view(1);
    assert!(narrator.status().contains("1 queued"));
    narrator.release_view(2);
    assert!(!narrator.status().contains("queued"), "{}", narrator.status());
    assert!(!narrator.unavailable());
}

#[test]
fn releasing_an_in_flight_view_tells_the_host() {
    let mut narrator = Narrator::new();
    narrator.set_enabled(true);
    narrator.acquire_view(7, &command());
    let sent = requests(&narrator.start_batch());
    let demand = sent[0].1["items"][0]["demand_id"].as_str().unwrap().to_owned();
    let released = requests(&narrator.release_view(7));
    assert_eq!(released.len(), 1);
    assert_eq!(released[0].0, "explanation-release");
    assert_eq!(released[0].1["release"][0], demand.as_str());
}

#[test]
fn memoized_lookups_follow_every_field_that_is_sent() {
    let mut narrator = Narrator::new();
    narrator.set_enabled(true);
    let activity = obj(json!({"name": "Bash", "input": {"command": "cmake --build desktop/build/dev"}, "_session": "s"}));
    narrator.request(&activity);
    let sent = requests(&narrator.start_batch());
    narrator.handle_reply(&sent[0].0, &answer(&sent[0].1, "ready", "Builds."));
    assert_eq!(narrator.explanation(&activity), "Builds.");
    let mut other_input = activity.clone();
    other_input.insert("input".into(), json!({"command": "ctest --preset dev"}));
    assert_eq!(narrator.explanation(&other_input), "");
    let mut as_text = activity.clone();
    as_text.insert("input".into(), json!("cmake --build desktop/build/dev"));
    assert_eq!(narrator.explanation(&as_text), "");
    let mut with_lines = activity.clone();
    with_lines.insert("lines".into(), json!([{"label": "Read", "text": "CMakeLists.txt"}]));
    assert_eq!(narrator.explanation(&with_lines), "");
    let mut other_session = activity.clone();
    other_session.insert("_session".into(), json!("other"));
    assert_eq!(narrator.explanation(&other_session), "");
    let mut with_result = activity.clone();
    with_result.insert("result".into(), json!("PRIVATE OUTPUT"));
    assert_eq!(narrator.explanation(&with_result), "Builds.", "fields never sent share the explanation");
}

#[test]
fn failures_fall_back_without_a_retry_storm() {
    for failure in ["host-error", "invalid", "timeout", "row-failed"] {
        let mut narrator = Narrator::new();
        narrator.set_enabled(true);
        narrator.request(&command());
        let effects = narrator.start_batch();
        let (tag, body) = requests(&effects)[0].clone();
        match failure {
            "host-error" => {
                narrator.handle_failure(&tag, 500);
            }
            "invalid" => {
                narrator.handle_reply(&tag, &obj(json!({"items": []})));
            }
            "timeout" => {
                let generation = effects.iter().find_map(|e| match e {
                    Effect::Timeout { generation } => Some(*generation),
                    _ => None,
                }).unwrap();
                narrator.timed_out(generation);
            }
            _ => {
                narrator.handle_reply(&tag, &answer(&body, "failed", ""));
                assert!(narrator.failed(&command()), "a failed row falls back to the original call");
                assert!(!narrator.unavailable(), "one failed row does not disable the narrator");
            }
        }
        assert_eq!(narrator.explanation(&command()), "", "{failure}");
        if failure != "row-failed" {
            // A whole-batch failure is reported; a single failed row is not.
            assert!(!narrator.status().starts_with("Shared Host"), "{failure}: {}", narrator.status());
        }
        assert!(requests(&narrator.request(&command())).is_empty(), "{failure}: no retry storm");
        assert!(narrator.start_batch().is_empty(), "{failure}: nothing queued to retry");
    }
    let mut narrator = Narrator::new();
    narrator.set_enabled(true);
    narrator.request(&command());
    let (tag, _) = requests(&narrator.start_batch())[0].clone();
    narrator.handle_failure(&tag, 404);
    assert_eq!(narrator.status(), "Update this Host to enable shared explanations.");
}

#[test]
fn late_replies_after_disable_are_rejected() {
    let mut narrator = Narrator::new();
    narrator.set_enabled(true);
    narrator.request(&command());
    let (tag, body) = requests(&narrator.start_batch())[0].clone();
    narrator.set_enabled(false);
    assert!(narrator.status().starts_with("Off"));
    narrator.handle_reply(&tag, &answer(&body, "ready", "Late."));
    narrator.set_enabled(true);
    assert_eq!(narrator.explanation(&command()), "", "a reply to a cancelled batch is ignored");
}

#[test]
fn detail_levels_discard_previous_translations() {
    let mut narrator = Narrator::new();
    assert_eq!((narrator.detail_level(), DETAIL_LEVELS.len()), (0, 5));
    for level in 1..=4 {
        let effects = narrator.set_detail_level(level);
        assert!(effects.contains(&Effect::DetailLevelChanged));
        assert!(narrator.enabled());
        assert_eq!(narrator.explanation(&command()), "", "level {level} starts without old wording");
        narrator.request(&command());
        let (tag, body) = requests(&narrator.start_batch())[0].clone();
        assert_eq!(body["detail_level"], level);
        narrator.handle_reply(&tag, &answer(&body, "ready", &format!("level {level}")));
        assert_eq!(narrator.explanation(&command()), format!("level {level}"));
    }
    let effects = narrator.set_detail_level(0);
    assert!(effects.contains(&Effect::EnabledChanged) && !narrator.enabled());
    assert!(narrator.request(&command()).is_empty());
    narrator.set_enabled(true);
    assert_eq!(narrator.detail_level(), 4, "re-enabling restores the last audience");
    assert_eq!(narrator.explanation(&command()), "level 4", "and keeps its wording");
}

#[test]
fn a_batch_holds_one_session_and_at_most_eight_items() {
    let mut narrator = Narrator::new();
    narrator.set_enabled(true);
    for i in 0..10 {
        narrator.request(&obj(json!({"name": "Bash", "command": format!("ls {i}"), "_session": "a"})));
    }
    narrator.request(&obj(json!({"name": "Bash", "command": "ls", "_session": "b"})));
    let (tag, body) = requests(&narrator.start_batch())[0].clone();
    assert_eq!(body["items"].as_array().unwrap().len(), 8);
    assert_eq!(body["session"], "a");
    narrator.handle_reply(&tag, &answer(&body, "ready", "Lists."));
    let (_, next) = requests(&narrator.start_batch())[0].clone();
    assert_eq!(next["session"], "a", "the rest of session a goes next");
    assert_eq!(next["items"].as_array().unwrap().len(), 2);
}
