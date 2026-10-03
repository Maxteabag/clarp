//! Live items against the fake Host (docs/live-items.md §7): gated on the
//! `live_items` feature, subscribed per open chat with `/events?live=`, a
//! `GET /live` on opening a chat and after a gap, the recorded streams
//! replayed as `live` events; without the feature, `/log` as before.

mod common;

use clarp_engine::Change;
use common::{Driver, Host};
use serde_json::{Value, json};

fn events_queries(host: &Host) -> Vec<Value> {
    host.requests("GET", "/events").into_iter().map(|r| r["query"].clone()).collect()
}

fn item_text(d: &Driver, session: &str, id: &str) -> String {
    d.engine.live_view(session).and_then(|v| v.item(id)).and_then(|i| i.get("text")).and_then(Value::as_str).unwrap_or_default().to_owned()
}

#[test]
fn without_the_feature_the_desktop_keeps_to_the_log() {
    let host = Host::start("live-off");
    let mut d = Driver::new(&host.base);
    d.connect();
    assert!(!d.engine.live_items());
    d.engine.select("rachel");
    d.until("rachel's log", |e| e.conversation("rachel").is_some_and(|c| c.len() == 2));
    // A live event the Host would not send to an old client changes nothing.
    host.control("/__control/live-replay", json!({"fixture": "turn-full"}));
    host.control("/__control/event", json!({"type": "agent-activity", "session": "rachel", "activity_status": "ok",
        "activity_action": "Bash", "activity_summary": "npm test", "state": "tool"}));
    d.until("the activity row", |e| e.conversation("rachel").is_some_and(|c| c.rows().iter().any(|m| m.activity)));
    assert!(d.engine.live_view("rachel").is_none());
    assert!(host.requests("GET", "/live").is_empty(), "no GET /live without the feature");
    assert!(events_queries(&host).iter().all(|q| q.get("live").is_none()), "no ?live= without the feature: {:?}", events_queries(&host));
}

#[test]
fn an_open_chat_subscribes_fetches_its_snapshot_and_follows_the_stream() {
    let host = Host::start("live-turn");
    host.control("/__control/live", json!({"on": true}));
    let mut d = Driver::new(&host.base);
    d.connect();
    assert!(d.engine.live_items());
    assert!(events_queries(&host).iter().all(|q| q.get("live").is_some()), "every stream asks for live ops: {:?}", events_queries(&host));
    d.engine.select("rachel");
    d.until("the snapshot", |e| e.live_view("rachel").is_some_and(|v| v.lseq() == Some(0)));
    assert_eq!(host.requests("GET", "/live").len(), 1, "{:?}", host.requests("GET", "/live"));
    assert_eq!(host.requests("GET", "/live")[0]["query"]["session"], "rachel");
    d.until("the stream asks for rachel", |_| events_queries(&host).iter().any(|q| q.get("live") == Some(&json!("rachel"))));
    d.settle(std::time::Duration::from_millis(300));
    host.control("/__control/live-replay", json!({"fixture": "turn-full"}));
    d.until("the whole turn", |e| e.live_view("rachel").is_some_and(|v| v.lseq() == Some(23)));
    assert!(d.changes.contains(&Change::Live("rachel".into())));
    let view = d.engine.live_view("rachel").unwrap();
    assert_eq!(view.activity()["state"], "interrupted");
    assert_eq!(view.turn().unwrap()["worked_ms"], 12000);
    assert_eq!(view.items().len(), 8);
    assert_eq!(item_text(&d, "rachel", "cl:msg_01:1"), "Let me look at the parser and its tests.");
    assert_eq!(host.requests("GET", "/live").len(), 1, "a whole stream in order needs no second snapshot");
    // Live items replace the old activity rows for this chat.
    host.control("/__control/event", json!({"type": "agent-activity", "session": "rachel", "activity_status": "ok",
        "activity_action": "Bash", "activity_summary": "npm test", "state": "tool"}));
    d.settle(std::time::Duration::from_millis(300));
    assert!(!d.engine.conversation("rachel").unwrap().rows().iter().any(|m| m.activity), "no activity rows beside live items");

    // A second chat joins the subscription: the stream reopens for the new
    // set, resuming the other events where it was, with no disconnect.
    let snapshots = host.requests("GET", "/agents/snapshot").len();
    host.control("/__control/event", json!({"type": "agent-roster", "session": "rachel"}));
    d.until("the event", |_| host.requests("GET", "/agents/snapshot").len() > snapshots);
    let connection_changes = d.changes.iter().filter(|c| **c == Change::Connection).count();
    d.engine.select("mike");
    d.until("the stream asks for both", |_| events_queries(&host).iter().any(|q| q.get("live") == Some(&json!("mike,rachel"))));
    let reopened = host.requests("GET", "/events").last().cloned().unwrap();
    assert!(!reopened["last_event_id"].as_str().unwrap_or_default().is_empty(), "resumed by event id: {reopened}");
    d.settle(std::time::Duration::from_millis(300));
    assert_eq!(d.changes.iter().filter(|c| **c == Change::Connection).count(), connection_changes, "no disconnect for a new set");
    assert_eq!(d.engine.connection_state(), "live");
}

#[test]
fn a_gap_recovers_from_get_live() {
    let host = Host::start("live-gap");
    host.control("/__control/live", json!({"on": true}));
    let mut d = Driver::new(&host.base);
    d.connect();
    d.engine.select("rachel");
    d.until("the subscription", |e| e.live_view("rachel").is_some_and(|v| v.lseq() == Some(0)) && events_queries(&host).iter().any(|q| q.get("live") == Some(&json!("rachel"))));
    d.settle(std::time::Duration::from_millis(300));
    // lseq 3 never arrives: the desktop asks for the snapshot.
    host.control("/__control/live-replay", json!({"fixture": "gap-needs-snapshot"}));
    d.until("a second snapshot", |_| host.requests("GET", "/live").len() >= 2);
    d.until("the Host's state", |e| e.live_view("rachel").is_some_and(|v| v.lseq() == Some(2) && !v.awaiting_snapshot()));
    assert_eq!(item_text(&d, "rachel", "cx:msg_a"), "Hello");

    // The recovering stream: after the snapshot the events above it apply;
    // a new epoch asks again.
    host.control("/__control/live-replay", json!({"fixture": "gap-recovers-from-snapshot", "restart": true}));
    d.until("the finished message", |e| item_text_of(e, "cx:msg_a") == "Hello world!");
    d.until("the new epoch's state", |e| e.live_view("rachel").is_some_and(|v| v.epoch() == Some("boot-b") && !v.awaiting_snapshot()));
    let view = d.engine.live_view("rachel").unwrap();
    assert_eq!(view.item("cx:msg_a").unwrap()["status"], "completed");
}

fn item_text_of(engine: &clarp_engine::Engine, id: &str) -> String {
    engine.live_view("rachel").and_then(|v| v.item(id)).and_then(|i| i.get("text")).and_then(Value::as_str).unwrap_or_default().to_owned()
}

#[test]
fn a_dropped_stream_asks_for_the_snapshot_again() {
    let host = Host::start("live-drop");
    host.control("/__control/live", json!({"on": true}));
    let mut d = Driver::new(&host.base);
    d.connect();
    d.engine.select("rachel");
    d.until("the snapshot", |e| e.live_view("rachel").is_some_and(|v| v.lseq() == Some(0)));
    let fetched = host.requests("GET", "/live").len();
    // The turn moves on while the stream is down: nothing arrives as events.
    host.control("/__control/outage", json!({"seconds": 0.5}));
    d.until("the drop", |e| e.connection_state() == "reconnecting");
    host.control("/__control/live-replay", json!({"fixture": "turn-full", "through": 6}));
    d.until("live again", |e| e.connection_state() == "live");
    d.until("the turn so far", |e| e.live_view("rachel").is_some_and(|v| v.lseq() == Some(5)));
    assert_eq!(host.requests("GET", "/live").len(), fetched + 1);
    assert_eq!(d.engine.live_view("rachel").unwrap().item("cl:msg_01:0").unwrap()["status"], "completed");
}
