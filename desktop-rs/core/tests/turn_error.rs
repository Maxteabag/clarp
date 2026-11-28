//! The Host's turn errors (docs/turn-errors.md, contract 217) against the
//! contract fixture (`tests/fixtures/turn-errors.json`, a copy of
//! `contract/fixtures/turn-errors.json`): decoding, the language, the
//! retry bodies and the collapse of repeats.

use clarp_core::protocol::Message;
use clarp_core::turn_error::{self, TurnError};
use serde_json::{Value, json};

fn fixture() -> Value {
    serde_json::from_str(include_str!("../../tests/fixtures/turn-errors.json")).expect("the fixture is JSON")
}

fn rows() -> Vec<Message> {
    fixture()["log_rows"].as_array().unwrap().iter().map(|r| Message::from_json(r.as_object().unwrap())).collect()
}

fn live() -> (String, TurnError) {
    let op = fixture()["live_turn_op"].clone();
    let turn = op["turn"].as_object().unwrap();
    (turn["turn_id"].as_str().unwrap().to_owned(), turn_error::of_live_turn(turn).expect("the failed live turn explains itself"))
}

#[test]
fn failure_rows_decode_and_user_rows_do_not() {
    let rows = rows();
    let first = rows.iter().find(|r| r.id == "marker-u1").unwrap().turn_error.clone().expect("the failure row carries its explanation");
    assert_eq!(first.code, "invalid_request");
    assert_eq!(first.severity, "error");
    assert_eq!(first.sentence(true), "Codex avviste samtalehistorikken. Start en ny økt med samme samtale og prøv igjen.");
    assert!(first.sentence(false).starts_with("Codex rejected the conversation history."));
    assert_eq!(first.actions.iter().map(|a| a.id.as_str()).collect::<Vec<_>>(), ["fresh_retry", "switch_model", "copy_details"]);
    assert_eq!(first.actions[0].label.get(true), "Ny økt, prøv igjen");
    assert_eq!(first.meta(), "codex · gpt-6.1 · t1");
    assert!(first.clipboard().starts_with("A Clarp agent turn failed."));
    let usage = rows.iter().find(|r| r.id == "marker-u4").unwrap().turn_error.clone().unwrap();
    assert_eq!((usage.severity.as_str(), usage.actions[0].at_ms), ("warning", Some(1_795_850_820_000)));
    let auth = rows.iter().find(|r| r.id == "marker-u5").unwrap().turn_error.clone().unwrap();
    assert_eq!(auth.actions[0].backend, "claude", "sign_in names its backend");
    assert!(rows.iter().filter(|r| r.role == "user").all(|r| r.turn_error.is_none()));
    assert!(rows.iter().find(|r| r.id == "q1").unwrap().turn_error.is_none(), "a notice without the object keeps today's look");
}

#[test]
fn unknown_actions_and_an_older_hosts_error_are_left_out() {
    let error = TurnError::from_json(&json!({"code": "x", "text": {"en": "E"}, "severity": "loud",
        "actions": [{"id": "dance", "label": {"en": "Dance"}}, {"id": "retry", "label": {"en": "Try again"}}]})).unwrap();
    assert_eq!(error.actions.len(), 1);
    assert_eq!(error.severity, "error", "an unknown severity reads as an error");
    assert_eq!(error.sentence(true), "E", "a missing language falls back to the other");
    assert!(TurnError::from_json(&json!({"reason": "provider_busy", "message": "API unavailable"})).is_none(), "no sentence, no card");
}

#[test]
fn retry_bodies_match_the_contract() {
    let fixture = fixture();
    let rows = rows();
    let error = |id: &str| rows.iter().find(|r| r.id == id).unwrap().turn_error.clone().unwrap();
    let action = |e: &TurnError, id: &str| e.actions.iter().find(|a| a.id == id).cloned().unwrap();
    let fresh = error("marker-u3");
    assert_eq!(fresh.retry_body("tt-codex", &action(&fresh, "fresh_retry")).unwrap(), fixture["turn_retry"]["request_fresh"]);
    let usage = error("marker-u4");
    assert_eq!(usage.retry_body("tt-codex", &action(&usage, "retry_at")).unwrap(), fixture["turn_retry"]["request_at"]);
    let long = error("marker-u6");
    assert_eq!(long.retry_body("tt-claude", &action(&long, "compact_retry")).unwrap(), fixture["turn_retry"]["request_compact"]);
    let auth = error("marker-u5");
    assert_eq!(auth.retry_body("tt-claude", &action(&auth, "retry")).unwrap(), json!({"session": "tt-claude", "message_id": "u5"}));
    assert!(usage.retry_body("tt-codex", &action(&usage, "switch_model")).is_none(), "only retries send");
    let mut orphan = auth.clone();
    orphan.message_id.clear();
    assert!(orphan.retry_body("tt-claude", &action(&auth, "retry")).is_none(), "no message, nothing to send again");
}

#[test]
fn three_repeats_collapse_to_the_newest_and_the_others_stay() {
    let plan = turn_error::plan(&rows(), None);
    assert_eq!(plan.hidden, ["marker-u1", "marker-u2"].map(String::from).into(), "the older repeats hide");
    assert_eq!(plan.count("marker-u3"), 3);
    for id in ["marker-u4", "marker-u5", "marker-u6"] {
        assert_eq!(plan.count(id), 1, "{id} is a card of its own");
        assert!(!plan.hidden.contains(id));
    }
    assert_eq!(plan.actionable.as_deref(), Some("marker-u6"), "only the newest failure offers a retry");
    assert!(plan.shows("marker-u6", "compact_retry"));
    assert!(!plan.shows("marker-u3", "fresh_retry"), "an older card hides its retry");
    assert!(plan.shows("marker-u3", "copy_details"), "but keeps the rest");
    assert!(plan.traces.contains("t1") && plan.traces.contains("t6"));
}

#[test]
fn a_reply_between_repeats_keeps_them_apart_and_a_reply_after_ends_the_retry() {
    let mut rows = rows();
    let reply = Message::from_json(json!({"id": "a1", "role": "assistant", "text": "Fixed."}).as_object().unwrap());
    rows.insert(3, reply.clone());
    let plan = turn_error::plan(&rows, None);
    assert_eq!(plan.hidden, ["marker-u2"].map(String::from).into(), "the first stands alone before the reply");
    assert_eq!(plan.count("marker-u1"), 1);
    assert_eq!(plan.count("marker-u3"), 2);
    rows.push(reply);
    let plan = turn_error::plan(&rows, None);
    assert_eq!(plan.actionable, None, "a reply after the newest failure takes its retry away");
}

#[test]
fn the_live_failed_turn_shows_once() {
    let rows = rows();
    let (turn_id, error) = live();
    let plan = turn_error::plan(&rows, Some((&turn_id, error.clone())));
    let (key, live) = plan.live.clone().expect("no durable row says it yet: the live card shows");
    assert_eq!(key, format!("{}7", turn_error::LIVE_PREFIX));
    assert_eq!(live.code, "provider_busy");
    assert_eq!(plan.actionable.as_deref(), Some(key.as_str()));
    // Its durable notice lands (same trace): the live card goes.
    let mut landed = rows.clone();
    landed.push(Message::from_json(json!({"id": "marker-u7", "role": "assistant", "origin": "system", "trace_id": "7",
        "failure_kind": "provider_busy", "text": "Turn failed.", "error": fixture()["live_turn_op"]["turn"]["error"]}).as_object().unwrap()));
    let plan = turn_error::plan(&landed, Some((&turn_id, error)));
    assert!(plan.live.is_none());
    assert_eq!(plan.actionable.as_deref(), Some("marker-u7"));
}

#[test]
fn norwegian_locales() {
    for name in ["nb_NO.UTF-8", "nn", "no-NO", "nb"] {
        assert!(turn_error::norwegian_locale(name), "{name}");
    }
    for name in ["en_US.UTF-8", "C", "", "sv_SE"] {
        assert!(!turn_error::norwegian_locale(name), "{name}");
    }
}

#[test]
fn a_retry_at_whose_time_has_passed_is_a_plain_retry() {
    let rows = rows();
    let usage = rows.iter().find(|r| r.id == "marker-u4").unwrap().turn_error.clone().unwrap();
    let at = usage.actions[0].at_ms.unwrap();
    let before = usage.actions_at(at - 1);
    assert_eq!((before[0].id.as_str(), before[0].label.get(true)), ("retry_at", "Prøv igjen kl. 18:27"));
    assert_eq!(usage.next_change_ms(at - 1), Some(at));
    let after = usage.actions_at(at);
    assert_eq!((after[0].id.as_str(), after[0].label.get(true), after[0].label.get(false)), ("retry", "Prøv igjen", "Try again"));
    assert_eq!(usage.retry_body("tt-codex", &after[0]).unwrap(), json!({"session": "tt-codex", "message_id": "u4"}), "no at_ms in the past");
    assert_eq!(usage.next_change_ms(at), None);
}

#[test]
fn interrupted_is_a_quiet_card_with_try_again() {
    let notice = Message::from_json(json!({"id": "n1", "role": "system", "kind": "notice", "origin": "system", "text": "Turn interrupted",
        "error": {"code": "interrupted", "severity": "info", "text": {"en": "The turn was cut off before it finished. Send the message again.", "nb": "Turen ble avbrutt før den var ferdig. Send meldingen på nytt."},
            "actions": [{"id": "retry", "label": {"en": "Try again", "nb": "Prøv igjen"}}], "message_id": "u9", "fingerprint": "interrupted:claude:1"}}).as_object().unwrap());
    let error = notice.turn_error.clone().expect("a turn-interrupted notice carries it too");
    assert_eq!((error.code.as_str(), error.severity.as_str()), ("interrupted", "info"));
    assert_eq!(error.actions_at(0)[0].id, "retry");
    assert_eq!(turn_error::plan(&[notice], None).actionable.as_deref(), Some("n1"));
}

#[test]
fn prompt_too_long_off_claude_offers_a_fresh_session() {
    let error = TurnError::from_json(&json!({"code": "prompt_too_long", "severity": "warning", "text": {"en": "Too long."}, "message_id": "u1",
        "actions": [{"id": "fresh_retry", "label": {"en": "New session, try again"}}, {"id": "switch_model", "label": {"en": "Switch"}}]})).unwrap();
    assert_eq!(error.retry_body("s", &error.actions[0]).unwrap(), json!({"session": "s", "message_id": "u1", "fresh": true}));
}
