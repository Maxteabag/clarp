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
    for invalid in [
        Binding {
            turn_id: 0,
            ..greeting.clone()
        },
        Binding {
            admission_id: String::new(),
            ..greeting.clone()
        },
    ] {
        assert!(!invalid.record(&mut first));
        assert!(first.get("clip_bindings").is_none());
    }
    assert!(greeting.clone().record(&mut first));
    assert!(initiative.clone().record(&mut second));
    assert_eq!(
        first["clip_bindings"],
        json!([{"turn_id":1,"kind":"greeting_reply","admission_id":"hello","epoch":4}])
    );
    assert!(
        !Binding {
            admission_id: "impostor".into(),
            ..greeting.clone()
        }
        .record(&mut first)
    );
    assert!(
        !Binding {
            turn_id: 8,
            ..greeting.clone()
        }
        .record(&mut first)
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
    for state in ["unknown", "cancelled"] {
        second["intent"]["state"] = json!(state);
        assert_eq!(initiative.audio(&second)["skip"], true);
    }
    second["intent"]["state"] = json!("started");
    second["late_greeting"] = json!({"client_msg_id":"late"});
    assert_eq!(initiative.audio(&second)["skip"], true);
    second["late_greeting"] = Value::Null;
    second["intent"]["admission_id"] = json!("replacement");
    assert_eq!(initiative.audio(&second)["skip"], true);
    assert_eq!(first["clip_bindings"].as_array().unwrap().len(), 1);
    assert_eq!(second["retired"], Value::Null);
}

#[test]
fn retention_starts_at_the_later_event_or_at_retirement_if_the_end_is_unknown() {
    use clarp_kernel::rings::binding::CallEnd::{Ended, Live, Unknown};
    const DAY: i64 = 86_400_000;
    for (story, retired, call, since) in [
        ("retirement before call end", 100, Ended(5_000), 5_000),
        (
            "retirement long after call end",
            2 * DAY,
            Ended(100),
            2 * DAY,
        ),
        ("missing call end", 7_000, Unknown, 7_000),
    ] {
        assert!(!expired(Some(retired), call, retired), "{story}");
        for (offset, due) in [
            (-1000, false),
            (-1, false),
            (0, true),
            (1, true),
            (1000, true),
        ] {
            let now = since + DAY + offset;
            assert_eq!(
                expired(Some(retired), call, now),
                due,
                "{story}, offset {offset}"
            );
            assert!(!expired(None, call, now), "{story} still unretired");
            assert!(!expired(Some(retired), Live, now), "call still live");
        }
    }
}
