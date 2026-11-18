#![allow(clippy::unwrap_used)]
use clarp_kernel::live_call::receipts;
use serde_json::{Value, json};

#[test]
fn the_voice_is_told_exactly_what_the_director_confirmed() {
    let fixture:Value=serde_json::from_str(include_str!("fixtures/live-call-tools-vectors.json")).unwrap();
    let mut answers=0;
    for row in fixture["outcomes"].as_array().unwrap(){
        assert_eq!(receipts::director(&row["answer"],row["status"].as_i64()),row["receipt"],"{row}");
        answers+=1;
    }
    let mut notes=0;
    for row in fixture["notes"].as_array().unwrap(){
        assert_eq!(json!(receipts::notes(row["receipts"].as_array().unwrap())),row["notes"],"note case {notes}");
        notes+=1;
    }
    assert_eq!((answers,notes),(129,102));
}
