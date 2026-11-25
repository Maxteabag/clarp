use clarp_core::protocol::{Message, automated_row};
use serde_json::json;

#[test]
fn the_note_left_after_a_cut_turn_is_never_drawn() {
    let note = json!({"id": "u-made-way-t42", "role": "user", "origin": "made-way", "text": "Your turn on a request was cut short"});
    let note = note.as_object().unwrap();
    assert!(automated_row(note) && Message::from_json(note).automated);
    let by_id = json!({"id": "u-made-way-t43", "role": "user", "text": "x"});
    assert!(automated_row(by_id.as_object().unwrap()));
    let plain = json!({"id": "u-123", "role": "user", "origin": "owner", "text": "hello"});
    assert!(!automated_row(plain.as_object().unwrap()) && !Message::from_json(plain.as_object().unwrap()).automated);
}

#[test]
fn a_failure_notice_carries_why_the_turn_failed() {
    let notice = json!({"id": "marker-m1", "role": "assistant", "origin": "system", "failure_kind": "ended_from_outside", "text": "The turn's process was ended from outside (SIGTERM)"});
    assert_eq!(Message::from_json(notice.as_object().unwrap()).failure_kind, clarp_core::protocol::ENDED_FROM_OUTSIDE);
    let older = json!({"id": "marker-m2", "role": "assistant", "origin": "system", "text": "Failed"});
    assert!(Message::from_json(older.as_object().unwrap()).failure_kind.is_empty());
}
