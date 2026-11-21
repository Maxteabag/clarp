#![allow(clippy::unwrap_used)]

use clarp_kernel::user_word;
use serde_json::Value;

#[test]
fn the_user_word_and_its_slot_follow_the_reference() {
    let data: Value =
        serde_json::from_str(include_str!("fixtures/user-word-first-vectors.json")).unwrap();
    let classification = data["classification"].as_array().unwrap();
    for case in classification {
        let said = |index: usize| case[index].as_str().unwrap_or("");
        assert_eq!(
            user_word::is_user_word(said(0), said(1), said(2)),
            case[3].as_bool().unwrap(),
            "{case}"
        );
    }
    let slots = data["slots"].as_array().unwrap();
    for case in slots {
        let protected = case[0]
            .as_array()
            .unwrap()
            .iter()
            .map(|item| item[0].as_bool().unwrap() || item[1].as_bool().unwrap());
        assert_eq!(
            user_word::slot(protected) as u64,
            case[1].as_u64().unwrap(),
            "{case}"
        );
    }
    assert_eq!((classification.len(), slots.len()), (270, 85));
}
