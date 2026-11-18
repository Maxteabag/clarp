//! Stories taken from the R77 and R78 rulings.
#![allow(clippy::unwrap_used)]
use clarp_kernel::rings::binding::{Binding, Kind, expired, key};
use serde_json::{Value, json};
use std::collections::BTreeMap;

#[test]
fn a_clip_belongs_to_the_admitted_agent_and_turn_even_when_traces_repeat() {
    let mut index = BTreeMap::new();
    let mut first = json!({"ring_id":"one","token":"a","call_id":"call-a","epoch":4,
        "greeting":{"trace_id":"same"}});
    let mut second = json!({"ring_id":"two","token":"b","call_id":"call-b","epoch":7,
        "intent":{"admission_id":"intent-two","state":"started","trace_id":"same"}});
    let greeting = Binding {
        turn_id: 1,
        kind: Kind::GreetingReply,
        admission_id: "hello".into(),
        epoch: 4,
    };
    let initiative = Binding {
        turn_id: 1,
        kind: Kind::Initiative,
        admission_id: "intent-two".into(),
        epoch: 7,
    };
    assert!(greeting.clone().record(&mut first));
    assert!(initiative.clone().record(&mut second));
    assert_eq!(
        first["clip_bindings"],
        json!([{"turn_id":1,"kind":"greeting_reply","admission_id":"hello","epoch":4}])
    );
    first["epoch"] = json!(5);
    assert!(
        !Binding {
            epoch: 5,
            ..greeting.clone()
        }
        .record(&mut first)
    );
    assert_eq!(first["clip_bindings"][0]["epoch"], 4);
    first["retired"] = json!({"at":100});
    for (agent, op, b) in [("alice", &first, &greeting), ("bob", &second, &initiative)] {
        index.insert(key(agent, b.turn_id).unwrap(), b.audio(op));
    }
    assert_eq!(
        index["alice:1"],
        json!({"skip":true,"tag":{"call_id":"call-a","opening":{
        "ring_id":"one","token":"a","kind":"greeting_reply","epoch":4,"admission_id":"hello"}}})
    );
    assert_eq!(index["bob:1"]["skip"], false);
    assert_eq!(
        index["bob:1"]["tag"]["opening"]["admission_id"],
        "intent-two"
    );
    assert!(!index.contains_key(&key("alice", 2).unwrap()));
    assert_eq!(key("alice", 0), None);
    assert_eq!(key("", 1), None);
    second["intent"]["admission_id"] = json!("replacement");
    assert_eq!(initiative.audio(&second)["skip"], true);
    assert_eq!(first["clip_bindings"].as_array().unwrap().len(), 1);
    assert_eq!(second["retired"], Value::Null);
}

#[test]
fn retention_requires_retirement_and_a_full_day_after_call_end() {
    let ended = 500;
    let due = ended + 86_400_000;
    for now in [due - 1, due, due + 1] {
        assert!(!expired(None, Some(ended), now));
        assert!(!expired(Some(100), None, now));
        assert_eq!(expired(Some(100), Some(ended), now), now >= due);
        assert!(!expired(Some(now + 1), Some(ended), now));
    }
    assert!(expired(Some(due + 10), Some(ended), due + 10));
}
