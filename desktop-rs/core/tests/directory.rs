use std::collections::HashSet;

use clarp_core::directory::*;
use serde_json::{Value, json};

fn obj(value: Value) -> serde_json::Map<String, Value> {
    value.as_object().cloned().unwrap()
}

// Port of tst_native_core::contactsExcludeActivePersonas.
#[test]
fn contacts_exclude_active_personas() {
    let contacts = contacts_from_snapshot(
        &obj(json!({"personas": [{"id": "one", "name": "Rachel"},
                                  {"id": "two", "name": "Bella", "personality": "Personality: Thoughtful"}]})),
        &HashSet::from(["rachel".to_owned()]),
    );
    assert_eq!(contacts.len(), 1);
    assert_eq!(contacts[0].name, "Bella");
    assert_eq!(contacts[0].description, "Thoughtful");
}

#[test]
fn contacts_sort_by_name_and_skip_malformed_rows() {
    let contacts = contacts_from_snapshot(
        &obj(json!({"personas": [{"name": "zed", "avatar_url": "/a/z.png"}, 7, {"name": ""},
                                  {"name": "Alice", "builtin": true, "personality": "Kind"}]})),
        &HashSet::new(),
    );
    let names: Vec<&str> = contacts.iter().map(|c| c.name.as_str()).collect();
    assert_eq!(names, ["Alice", "zed"]);
    assert!(contacts[0].builtin);
    assert_eq!(contacts[0].description, "Kind");
    assert_eq!(avatar_urls_by_name(&contacts).get("zed").map(String::as_str), Some("/a/z.png"));
}

#[test]
fn voices_mark_current_and_hide_its_owner() {
    let voices = voices_from_response(
        &obj(json!({"voices": [{"id": "v1", "label": "Warm", "taken_by": "Rachel"},
                                {"id": "v2", "taken_by": "Bella"}, {"label": "no id"}]})),
        "v1",
    );
    assert_eq!(voices.len(), 2);
    assert!(voices[0].current && voices[0].taken_by.is_empty());
    assert_eq!(voices[1].label, "v2", "label falls back to the id");
    assert_eq!(voices[1].taken_by, "Bella");
}
