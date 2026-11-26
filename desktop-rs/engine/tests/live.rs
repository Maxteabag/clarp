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
    // Once on opening, once more when the stream reopened listing the chat
    // (what happened between the two was not sent).
    d.until("the snapshot after the stream reopened", |_| host.requests("GET", "/live").len() == 2);
    assert!(host.requests("GET", "/live").iter().all(|r| r["query"]["session"] == "rachel"));
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
    assert_eq!(host.requests("GET", "/live").len(), 2, "a whole stream in order needs no further snapshot");
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
    d.until("the snapshot after the stream reopened", |_| host.requests("GET", "/live").len() >= 2);
    d.settle(std::time::Duration::from_millis(300));
    let fetched = host.requests("GET", "/live").len();
    // The turn moves on while the stream is down: nothing arrives as events.
    host.control("/__control/outage", json!({"seconds": 0.5}));
    d.until("the drop", |e| !e.connected());
    host.control("/__control/live-replay", json!({"fixture": "turn-full", "through": 6}));
    d.until("live again", |e| e.connected() && e.connection_state() == "live");
    d.until("the turn so far", |e| e.live_view("rachel").is_some_and(|v| v.lseq() == Some(5)));
    assert_eq!(host.requests("GET", "/live").len(), fetched + 1);
    assert_eq!(d.engine.live_view("rachel").unwrap().item("cl:msg_01:0").unwrap()["status"], "completed");
}

#[test]
fn turning_the_feature_on_and_reconnecting_follows_the_open_chat() {
    let host = Host::start("live-later");
    let mut d = Driver::new(&host.base);
    d.connect();
    d.engine.select("rachel");
    d.until("rachel's log", |e| e.conversation("rachel").is_some_and(|c| c.len() == 2));
    host.control("/__control/live", json!({"on": true}));
    d.engine.reconnect();
    d.until("the snapshot", |e| e.live_items() && e.live_view("rachel").is_some_and(|v| v.lseq() == Some(0)));
    d.until("the stream asks for rachel", |_| events_queries(&host).iter().any(|q| q.get("live") == Some(&json!("rachel"))));
    d.settle(std::time::Duration::from_millis(300));
    host.control("/__control/live-replay", json!({"fixture": "turn-full", "through": 5}));
    d.until("the thinking title", |e| e.live_view("rachel").is_some_and(|v| v.lseq() == Some(4)));
}

/// A Host that lists `live_items` but sends no live events yet (its live hub
/// not serving) must not leave chats looking idle: until a live event (or a
/// snapshot with something in it) arrives for a chat, the old activity rows
/// and typing indicator stay in charge.
#[test]
fn a_flag_without_live_events_keeps_the_old_activity() {
    let host = Host::start("live-silent");
    host.control("/__control/live", json!({"on": true}));
    let mut d = Driver::new(&host.base);
    d.connect();
    assert!(d.engine.live_items());
    d.engine.select("rachel");
    d.until("the empty snapshot", |e| e.live_view("rachel").is_some_and(|v| v.lseq() == Some(0)));
    assert!(!d.engine.live_active("rachel"), "an idle, empty snapshot does not take the chat over");
    // Events sent while the stream reopens to list the chat are not sent.
    d.until("the stream asks for rachel", |_| events_queries(&host).iter().any(|q| q.get("live") == Some(&json!("rachel"))));
    d.settle(std::time::Duration::from_millis(300));
    host.control("/__control/event", json!({"type": "agent-activity", "session": "rachel", "activity_status": "ok",
        "activity_action": "Bash", "activity_summary": "npm test", "state": "tool"}));
    d.until("the old activity row", |e| e.conversation("rachel").is_some_and(|c| c.rows().iter().any(|m| m.activity)));

    // The first live event for the chat hands it to live items.
    host.control("/__control/live-replay", json!({"fixture": "turn-full"}));
    d.until("live takes over", |e| e.live_active("rachel"));
}

/// Whether a stream was opened, from the `skip`-th on, for exactly `sessions`. Asked for by
/// arrival, not by the last one: a reopen that was cut off can reach the Host after the next.
fn asked_for(host: &Host, skip: usize, sessions: &[&str]) -> bool {
    let mut wanted: Vec<&str> = sessions.to_vec();
    wanted.sort_unstable();
    let wanted = wanted.join(",");
    events_queries(host).iter().skip(skip).any(|q| q.get("live").and_then(Value::as_str) == Some(wanted.as_str()))
}

/// Agents of the fake Host by these names, listed in the roster before the
/// test opens them: a selected chat the roster does not list is replaced by
/// its first agent when a snapshot arrives, which would move the selection.
fn add_agents(host: &Host, d: &mut Driver, names: &[&str]) {
    for name in names {
        host.control("/__control/add-agent", json!({"session": name, "persona": name}));
    }
    let names: Vec<String> = names.iter().map(|s| (*s).to_owned()).collect();
    d.until("the roster lists the test agents", |e| names.iter().all(|n| e.roster().find(n).is_some()));
}

#[test]
fn every_shown_pane_and_the_selected_chat_stay_live_and_only_the_rest_is_capped() {
    let host = Host::start("live-panes");
    host.control("/__control/live", json!({"on": true}));
    let mut d = Driver::new(&host.base);
    d.connect();
    add_agents(&host, &mut d, &["p1", "p2", "p3", "p4", "b1", "b2", "b3", "b4", "b5", "b6"]);
    // Four panes shown, then a fifth to browse in.
    d.engine.select("p1");
    for session in ["p2", "p3", "p4", "b1"] {
        d.engine.with_panes(|p| p.split_active("vertical", session));
    }
    for session in ["b2", "b3", "b4", "b5", "b6"] {
        d.engine.select(session);
    }
    // Every shown pane and the selected chat keep their live view; of the
    // chats browsed past, only the four opened last stay on the stream.
    let watched = ["p1", "p2", "p3", "p4", "b6", "b5", "b4", "b3", "b2"];
    d.until("the stream carries the shown chats and the four opened last", |_| asked_for(&host, 0, &watched));
    for session in watched {
        assert!(d.engine.live_view(session).is_some(), "{session} keeps its live view");
    }
    assert!(d.engine.live_view("b1").is_none(), "b1 was let go");
    // Opened again, it is watched again, and the one opened longest ago goes.
    d.engine.select("b1");
    d.until("b1 is watched again", |_| asked_for(&host, 0, &["p1", "p2", "p3", "p4", "b1", "b6", "b5", "b4", "b3"]));
    assert!(d.engine.live_view("b2").is_none(), "b2 was let go");
    for session in ["p1", "p2", "p3", "p4"] {
        assert!(d.engine.live_view(session).is_some(), "{session} keeps its live view");
    }
}

#[test]
fn after_a_reconnect_the_selected_chat_and_the_ones_opened_last_stay_live() {
    let host = Host::start("live-reconnect");
    host.control("/__control/live", json!({"on": true}));
    let mut d = Driver::new(&host.base);
    d.connect();
    add_agents(&host, &mut d, &["c1", "c2", "c3", "c4", "c5", "c6"]);
    for session in ["c1", "c2", "c3", "c4", "c5", "c6", "c2"] {
        d.engine.select(session);
    }
    let watched = ["c2", "c3", "c4", "c5", "c6"];
    let held = |d: &Driver| -> Vec<&str> { ["c1", "c2", "c3", "c4", "c5", "c6"].into_iter().filter(|s| d.engine.live_view(s).is_some()).collect() };
    for _ in 0..50 {
        if held(&d) == watched && asked_for(&host, 0, &watched) {
            break;
        }
        d.settle(std::time::Duration::from_millis(200));
    }
    assert_eq!(held(&d), watched, "live views held; streams asked: {:?}", events_queries(&host));
    assert!(asked_for(&host, 0, &watched), "streams asked: {:?}", events_queries(&host));
    let streams = events_queries(&host).len();
    // Six chats are loaded; reconnecting opens them again in the order they
    // were opened, never in the order they are stored in.
    d.engine.reconnect();
    for _ in 0..50 {
        if asked_for(&host, streams, &watched) {
            break;
        }
        d.settle(std::time::Duration::from_millis(200));
    }
    assert!(asked_for(&host, streams, &watched), "live views held: {:?}; streams asked after the reconnect: {:?}", held(&d), &events_queries(&host)[streams.min(events_queries(&host).len())..]);
    assert!(d.engine.live_view("c2").is_some(), "the selected chat keeps its live view");
    assert!(d.engine.live_view("c1").is_none(), "c1 was let go");
}
