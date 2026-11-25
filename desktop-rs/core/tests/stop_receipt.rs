//! A Stop's receipt (Host contract 61): every newer field is optional and
//! missing means unknown, and the chat's line says only what the Host
//! committed. The cases are the contract's.

use clarp_core::json::Object;
use clarp_core::stop_receipt::{Origin, Said, StopReceipt};
use serde_json::{Value, json};

fn object(value: Value) -> Object {
    value.as_object().cloned().unwrap()
}

fn names(session: &str) -> Option<String> {
    (session == "theo-97e5").then(|| "Theo".to_owned())
}

fn said(response: Value) -> Said {
    said_waiting(response, true)
}

fn said_waiting(response: Value, waiting: bool) -> Said {
    StopReceipt::from_response(&object(response)).said(&names, waiting)
}

fn line(text: &str) -> Said {
    Said::Line(text.into())
}

#[test]
fn an_older_host_leaves_every_newer_field_unknown() {
    let receipt = StopReceipt::from_response(&object(json!({"ok": true, "terminated": 1})));
    assert_eq!(receipt.origin, Origin::Response);
    assert_eq!((receipt.ok, receipt.terminated), (Some(true), Some(1)));
    assert_eq!(receipt.actor, None);
    assert_eq!(receipt.actor_verified, None, "not false");
    assert_eq!(receipt.reason, None);
    assert_eq!(receipt.queue_paused, None, "not false: terminated says nothing about the queue");
    assert_eq!(receipt.goals_paused, None, "not 0");
}

#[test]
fn a_field_of_the_wrong_type_is_unknown_too() {
    let receipt = StopReceipt::from_response(&object(json!({
        "ok": true, "terminated": 1, "stop_actor_verified": "yes", "queue_paused": 1, "goals_paused": -2, "stop_reason": "  "})));
    assert_eq!((receipt.actor_verified, receipt.queue_paused, receipt.goals_paused, receipt.reason), (None, None, None, None));
}

#[test]
fn a_verified_agent_names_itself_its_reason_and_the_effects() {
    let response = json!({"ok": true, "terminated": 1, "stop_actor": "agent:theo-97e5", "stop_actor_verified": true,
        "stop_reason": "Pebble busy with no process", "queue_paused": true, "goals_paused": 1});
    let receipt = StopReceipt::from_response(&object(response.clone()));
    assert_eq!(receipt.actor.as_deref(), Some("agent:theo-97e5"));
    assert_eq!((receipt.actor_verified, receipt.queue_paused, receipt.goals_paused), (Some(true), Some(true), Some(1)));
    assert_eq!(said(response), line("Stopped by Theo: Pebble busy with no process · queue paused · 1 goal paused"));
}

#[test]
fn your_own_stop_leaves_the_stopping_to_the_transcript_and_names_only_waiting_messages() {
    let response = json!({"ok": true, "terminated": 0, "stop_actor": "user", "stop_actor_verified": false, "stop_reason": "", "queue_paused": true});
    assert_eq!(said(response.clone()), line("Queue paused"), "no 'Stopped by you', no 'claimed'");
    assert_eq!(said_waiting(response, false), line(""), "nothing waits: the transcript's line says it all");
}

#[test]
fn no_goals_paused_is_left_out_and_paused_goals_are_said() {
    let response = json!({"ok": true, "terminated": 1, "stop_actor": "user", "stop_actor_verified": true, "stop_reason": "", "queue_paused": true, "goals_paused": 0});
    assert_eq!(said(response), line("Queue paused"));
    let response = json!({"ok": true, "terminated": 1, "stop_actor": "user", "stop_actor_verified": true, "queue_paused": true, "goals_paused": 2});
    assert_eq!(said_waiting(response, false), line("2 goals paused"));
}

#[test]
fn an_older_host_says_its_effects_are_unknown() {
    assert_eq!(said(json!({"ok": true, "terminated": 1})), line("Stopped · effects unknown"));
    assert_eq!(said(json!({"ok": true, "terminated": 0})), line("Stop sent · effects unknown"));
}

#[test]
fn a_stop_the_runtime_refused_is_only_an_error() {
    let refused = json!({"ok": true, "terminated": 0, "stop_actor": "user", "stop_actor_verified": true, "stop_reason": ""});
    assert_eq!(said(refused), Said::Error("Stop did not take effect".into()));
    assert_eq!(said(json!({"ok": false, "terminated": 0})), Said::Error("Stop did not take effect".into()));
}

#[test]
fn an_unknown_agent_or_other_actor_reads_as_given_and_effects_unknown_once_terminated() {
    let response = json!({"ok": true, "terminated": 2, "stop_actor": "agent:nora-1", "stop_actor_verified": false});
    assert_eq!(said(response), line("Stopped by nora-1 (unverified)"));
    let response = json!({"ok": true, "terminated": 1, "stop_actor": "ops-script", "queue_paused": false, "goals_paused": 3});
    assert_eq!(said(response), line("Stopped by ops-script · queue not paused · 3 goals paused"));
}

#[test]
fn the_interrupted_detail_says_who_stopped_and_why_without_effects() {
    let detail = object(json!({"source": "user_stop", "message": "Turn stopped", "stop_actor": "agent:theo-97e5",
        "stop_actor_verified": true, "stop_reason": "Pebble busy with no process"}));
    let receipt = StopReceipt::from_detail(&detail).expect("a Stop's detail");
    assert_eq!(receipt.origin, Origin::Detail);
    assert_eq!(receipt.said(&names, false), line("Stopped by Theo: Pebble busy with no process"));
    let older = object(json!({"source": "user_stop", "message": "Turn stopped"}));
    assert_eq!(StopReceipt::from_detail(&older).unwrap().said(&names, false), line("Stopped"), "an older Host's detail names no one");
    assert_eq!(StopReceipt::from_detail(&object(json!({"source": "server_restart"}))), None);
}

#[test]
fn no_line_ever_promises_a_resume() {
    for response in [
        json!({"ok": true, "terminated": 1}),
        json!({"ok": true, "terminated": 1, "stop_actor": "user", "stop_actor_verified": true, "queue_paused": true, "goals_paused": 4}),
    ] {
        let Said::Line(text) = said(response) else { panic!("a line") };
        assert!(!text.to_lowercase().contains("resume"), "{text}");
    }
}

#[test]
fn a_host_with_the_sentence_is_shown_in_place_of_the_built_line() {
    let answer = json!({"ok": true, "terminated": 1, "stop_actor": "user", "stop_actor_verified": true,
        "queue_paused": true, "goals_paused": 1, "text": "Stopped. 2 messages wait; send one to go on."});
    assert_eq!(said(answer), line("Stopped. 2 messages wait; send one to go on."));
    let nothing_waits = json!({"ok": true, "terminated": 1, "stop_actor": "user", "queue_paused": false,
        "goals_paused": 0, "text": "Stopped. You can keep chatting."});
    assert_eq!(said_waiting(nothing_waits, false), line("Stopped. You can keep chatting."), "also where the built line would be empty");
}

#[test]
fn a_stop_from_elsewhere_shows_the_hosts_sentence_from_its_detail() {
    let detail = object(json!({"source": "user_stop", "stop_actor": "agent:theo-97e5", "stop_actor_verified": true,
        "stop_reason": "Pebble busy", "stop_text": "Stopped. The message that waits goes on next."}));
    let receipt = StopReceipt::from_detail(&detail).unwrap();
    assert_eq!(receipt.said(&names, true), line("Stopped. The message that waits goes on next."));
    // The response's own field is `text`; a detail's `text` is not the sentence.
    let other = object(json!({"source": "user_stop", "stop_actor": "agent:theo-97e5", "text": "ignored"}));
    assert_eq!(StopReceipt::from_detail(&other).unwrap().said(&names, false), line("Stopped by Theo"));
}

#[test]
fn without_the_feature_the_desktop_keeps_its_own_line() {
    let answer = json!({"ok": true, "terminated": 1, "stop_actor": "agent:theo-97e5", "stop_actor_verified": true,
        "stop_reason": "Pebble busy", "queue_paused": true, "goals_paused": 1, "text": "Stopped. 1 message waits; send one to go on."});
    let receipt = StopReceipt::from_response(&object(answer)).without_host_text();
    assert_eq!(receipt.said(&names, true), line("Stopped by Theo: Pebble busy · queue paused · 1 goal paused"));
}

#[test]
fn a_stop_that_did_nothing_is_still_an_error_with_the_sentence() {
    let refused = json!({"ok": false, "text": "Stopped. You can keep chatting."});
    assert_eq!(said(refused), Said::Error("Stop did not take effect".into()));
}
