//! The Host's tests of closing an agent: what a start does for a removal that did not come. One
//! file a piece, declared in `host.rs`. The cases of a close that waits are the fleet's own
//! (`src/fleet/closing_tests.rs`): only there can an agent's thread be held.

use super::*;

/// An agent's record where a stopped Host keeps it, by the store's own crate.
fn put_agent(place: &Place, record: &Value) {
    use clarp_ports::kv::KeyValueEngine as _;
    clarp_store_sqlite::SqliteKeyValue
        .open(&place.root.join("data/fleet.db"))
        .unwrap()
        .put(
            "agent",
            record["agent_id"].as_str().unwrap(),
            &serde_json::to_vec(record).unwrap(),
        )
        .unwrap();
}

/// An agent's record as a stopped Host keeps it.
fn stored_agent(place: &Place, id: &str) -> Value {
    use clarp_ports::kv::KeyValueEngine as _;
    let bytes = clarp_store_sqlite::SqliteKeyValue
        .open(&place.root.join("data/fleet.db"))
        .unwrap()
        .get("agent", id)
        .unwrap()
        .unwrap();
    serde_json::from_slice(&bytes).unwrap()
}

/// A Host stopped after an agent's tombstone was kept and before the rest of its removal came (its
/// thread did not answer its close in time): at the next start the agent is gone and its helper
/// has lost it, as the removal would have left it; a helper of an agent that is there is not
/// touched, and a start after that moves nothing again.
#[test]
fn a_helper_whose_parent_was_deleted_before_its_removal_came_is_abandoned_at_the_next_start() {
    let place = Place::new("bounded-close");
    drop(place.start());
    let work = place.root.join("work");
    let agent = |id: &str, session: &str, extra: Value| {
        let mut record = json!({
            "agent_id": id, "session": session, "persona": session, "backend": "scripted",
            "cwd": work, "conversation_id": format!("c-{id}"), "created_at_ms": 1,
        });
        for (key, value) in extra.as_object().unwrap() {
            record[key] = value.clone();
        }
        record
    };
    let helper_of = |parent: &str| json!({ "parent_agent_id": parent, "role": "helper", "helper_state": "running" });
    put_agent(&place, &agent("agent-gone", "gone", json!({ "deleted_at_ms": 5 })));
    put_agent(&place, &agent("agent-left", "left", helper_of("agent-gone")));
    put_agent(&place, &agent("agent-here", "here", json!({})));
    put_agent(&place, &agent("agent-kept", "kept", helper_of("agent-here")));

    let host = place.start();
    let rows = host.get("/agents/snapshot")["agents"].clone();
    let row = |session: &str| {
        rows.as_array()
            .unwrap()
            .iter()
            .find(|row| row["session"] == session)
            .cloned()
    };
    assert!(row("gone").is_none(), "{rows}");
    assert_eq!(row("left").unwrap()["helper_state"], "abandoned", "{rows}");
    assert_eq!(row("kept").unwrap()["helper_state"], "running", "{rows}");
    assert!(row("here").is_some(), "{rows}");
    drop(host);

    let left = stored_agent(&place, "agent-left");
    assert_eq!(left["helper_state"], "abandoned");
    let completed = left["helper_completed_at_ms"].as_i64().unwrap();
    assert!(completed > 5, "{left}");
    assert_eq!(stored_agent(&place, "agent-kept")["helper_state"], "running");
    assert_eq!(stored_agent(&place, "agent-gone")["ledger_retired"], true);

    // The next start finds it abandoned already: nothing is moved or written twice.
    drop(place.start());
    let again = stored_agent(&place, "agent-left");
    assert_eq!(again["helper_state"], "abandoned");
    assert_eq!(again["helper_completed_at_ms"], json!(completed));
}
