//! The REWRITE_PLAN behaviours the desktop must keep, against the fake Host,
//! through the engine the Slint app runs on (SLINT_PARITY.md's behaviour
//! table points here).

mod common;

use std::time::Duration;

use clarp_engine::Change;
use common::{Driver, Host};
use serde_json::json;

#[test]
fn the_roster_comes_from_the_snapshot_and_follows_roster_events() {
    let host = Host::start("roster");
    let mut d = Driver::new(&host.base);
    d.connect();
    let names: Vec<String> = d.engine.roster().agents().iter().map(|a| a.session.clone()).collect();
    assert!(names.contains(&"rachel".into()) && names.contains(&"mike".into()), "{names:?}");
    assert!(!host.requests("GET", "/agents/snapshot").is_empty());
    host.control("/__control/add-agent", json!({"session": "nina", "persona": "Nina"}));
    d.until("a created agent appears", |e| e.roster().find("nina").is_some());
    assert!(d.changes.contains(&Change::Roster));
}

#[test]
fn selecting_focuses_the_host_and_server_focus_moves_with_other_clients() {
    let host = Host::start("focus");
    let mut d = Driver::new(&host.base);
    d.connect();
    d.engine.select("mike");
    d.until("mike opens", |e| e.conversation("mike").is_some_and(|c| !c.rows().is_empty()));
    assert_eq!(host.requests("POST", "/select").last().unwrap()["body"]["session"], "mike");
    // Another client focuses Rachel: the roster says so.
    host.control("/__control/event", json!({"type": "agent-focus", "session": "rachel"}));
    d.until("focus follows the Host", |e| e.roster().find("rachel").is_some_and(|a| a.focused) && e.roster().find("mike").is_some_and(|a| !a.focused));
}

#[test]
fn a_new_conversation_id_replaces_the_transcript() {
    let host = Host::start("replace");
    let mut d = Driver::new(&host.base);
    d.connect();
    d.until("rachel open", |e| e.conversation("rachel").is_some_and(|c| c.rows().len() == 2));
    host.control("/__control/fill", json!({"session": "rachel", "count": 3, "prefix": "Fresh", "conversation_id": "c-rachel-2"}));
    d.until("replaced, not appended", |e| {
        e.conversation("rachel").is_some_and(|c| c.rows().len() == 3 && c.rows().iter().all(|r| r.text.starts_with("Fresh")))
    });
}

#[test]
fn a_growing_message_is_merged_by_id_and_revision() {
    let host = Host::start("merge");
    let mut d = Driver::new(&host.base);
    d.connect();
    d.until("rachel open", |e| e.conversation("rachel").is_some_and(|c| c.rows().len() == 2));
    for text in ["Partial", "Partial answer", "Partial answer, complete."] {
        host.control("/__control/turns", json!({"session": "rachel", "turns": [
            {"id": "r1", "role": "user", "text": "Hello"},
            {"id": "r2", "role": "assistant", "text": text},
        ]}));
        d.until(text, |e| e.conversation("rachel").is_some_and(|c| c.rows().last().is_some_and(|r| r.text == text)));
        let rows = d.engine.conversation("rachel").unwrap().rows();
        assert_eq!(rows.len(), 2, "no duplicated turns while it grows: {:?}", rows.iter().map(|r| &r.text).collect::<Vec<_>>());
    }
}

#[test]
fn one_event_stream_resumes_after_an_outage_and_ignores_unknown_events() {
    let host = Host::start("stream");
    let mut d = Driver::new(&host.base);
    d.connect();
    // An event the desktop does not know is ignored, not an error.
    host.control("/__control/event", json!({"type": "something-new", "session": "rachel", "extra": {"x": 1}}));
    host.control("/__control/event", json!({"type": "agent-focus", "session": "mike", "new_field": true}));
    d.until("a known event with an extra field applies", |e| e.roster().find("mike").is_some_and(|a| a.focused));
    assert!(d.engine.error().is_empty(), "{:?}", d.engine.error());
    assert_eq!(host.requests("GET", "/events").len(), 1, "one stream");
    host.control("/__control/outage", json!({"seconds": 1.0}));
    d.until("the stream drops", |e| e.connection_state() != "live");
    d.until("and comes back", |e| e.connection_state() == "live");
    d.settle(Duration::from_millis(300));
    let streams = host.requests("GET", "/events");
    let resumed = streams.last().unwrap();
    assert!(streams.len() >= 2 && resumed["last_event_id"].as_str().is_some_and(|id| !id.is_empty()), "it resumes where it left: {streams:?}");
}

#[test]
fn stopping_a_turn_and_the_states_a_chat_shows() {
    let host = Host::start("states");
    let mut d = Driver::new(&host.base);
    d.connect();
    for state in ["thinking", "waiting", "interrupted", "tool", "idle"] {
        let now = chrono_now();
        host.control("/__control/event", json!({"type": "agent-state", "session": "rachel", "kind": state, "ts": now}));
        d.until(state, |e| e.roster().display_state("rachel").as_deref() == Some(state));
    }
    host.control("/__control/event", json!({"type": "queue-updated", "session": "rachel", "queue_depth": 2, "queue_revision": 5}));
    d.until("the queue shows", |e| e.queue_count("rachel") == 2);
    d.engine.stop_session("rachel");
    d.until("stop sent", |_| !host.requests("POST", "/stop").is_empty());
    assert_eq!(host.requests("POST", "/stop")[0]["body"]["session"], "rachel");
}

fn chrono_now() -> i64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_millis() as i64).unwrap_or(0)
}
