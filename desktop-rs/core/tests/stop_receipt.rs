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
    StopReceipt::from_response(&object(response)).said(&names)
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
    assert_eq!(said(response), line("Stopped by Theo (verified): Pebble busy with no process · queue paused · 1 goal paused"));
}

#[test]
fn a_claimed_actor_says_it_is_a_claim_and_unknown_goals_are_left_out() {
    let response = json!({"ok": true, "terminated": 0, "stop_actor": "user", "stop_actor_verified": false, "stop_reason": "", "queue_paused": true});
    assert_eq!(said(response), line("Stopped by you (claimed) · queue paused"));
}

#[test]
fn a_paired_user_is_you_and_no_goals_paused_is_said() {
    let response = json!({"ok": true, "terminated": 1, "stop_actor": "user", "stop_actor_verified": true, "stop_reason": "", "queue_paused": true, "goals_paused": 0});
    assert_eq!(said(response), line("Stopped by you · queue paused · no goals paused"));
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
    assert_eq!(said(response), line("Stopped by nora-1 (claimed) · effects unknown"));
    let response = json!({"ok": true, "terminated": 1, "stop_actor": "ops-script", "queue_paused": false, "goals_paused": 3});
    assert_eq!(said(response), line("Stopped by ops-script · queue not paused · 3 goals paused"));
}

#[test]
fn the_interrupted_detail_says_who_stopped_and_why_without_effects() {
    let detail = object(json!({"source": "user_stop", "message": "Turn stopped", "stop_actor": "agent:theo-97e5",
        "stop_actor_verified": true, "stop_reason": "Pebble busy with no process"}));
    let receipt = StopReceipt::from_detail(&detail).expect("a Stop's detail");
    assert_eq!(receipt.origin, Origin::Detail);
    assert_eq!(receipt.said(&names), line("Stopped by Theo (verified): Pebble busy with no process"));
    let older = object(json!({"source": "user_stop", "message": "Turn stopped"}));
    assert_eq!(StopReceipt::from_detail(&older).unwrap().said(&names), line("Stopped"), "an older Host's detail names no one");
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
