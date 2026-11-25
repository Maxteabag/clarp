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
