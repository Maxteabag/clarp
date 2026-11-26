//! Whether an agent can answer now (feature `agent_availability`, issue 918): every agent row
//! carries `availability`, an `agent-roster` event of kind `availability` carries it as the row
//! has it when what an app shows changes, and an event stream open when the Host is told to stop
//! is sent `host-updating` as its last frame.

use super::*;

fn availability_of(host: &Host, session: &str) -> Value {
    host.get(&format!("/agents/{session}"))["availability"].clone()
}

/// The newest `availability` event about this agent.
fn told(heard: &Arc<Mutex<Vec<Value>>>, session: &str) -> Option<Value> {
    heard
        .lock()
        .unwrap()
        .iter()
        .rev()
        .find(|event| {
            event["type"] == "agent-roster"
                && event["kind"] == "availability"
                && event["session"] == session
        })
        .cloned()
}

/// What an app shows of it: not the times that only follow along.
fn shown(availability: &Value) -> Value {
    json!([
        availability["state"],
        availability["text"],
        availability["back_at_ms"],
        availability["queue_ahead"],
    ])
}

#[test]
fn an_agent_at_work_says_how_many_messages_wait_and_the_event_carries_its_row() {
    let place = Place::new("agent-availability");
    let host = place.start();
    assert_eq!(
        host.get("/server-info")["contract"]["features"]["agent_availability"],
        201
    );
    let heard = host.listen();
    let session = host.new_agent("Avail");
    let idle = availability_of(&host, &session);
    assert_eq!(
        shown(&idle),
        json!(["ok", "", null, 0]),
        "an agent that can answer: {idle}"
    );
    assert!(idle["since_ms"].as_i64().is_some(), "{idle}");
    let snapshot = host.get("/agents/snapshot");
    let row = snapshot["agents"]
        .as_array()
        .unwrap()
        .iter()
        .find(|row| row["session"] == session.as_str())
        .unwrap();
    assert_eq!(shown(&row["availability"]), shown(&idle), "{row}");

    host.send(&session, "sleep 60", "long");
    wait_for("the agent to be busy", || {
        told(&heard, &session)
            .is_some_and(|event| event["fields"]["availability"]["state"] == "busy")
    });
    let event = told(&heard, &session).unwrap();
    assert_eq!(event["agent_id"], row["agent_id"], "{event}");
    assert_eq!(
        shown(&event["fields"]["availability"]),
        json!(["busy", "Busy", null, 0]),
        "{event}"
    );

    // A message that waits behind the turn: one ahead of the next.
    host.send(&session, "after it", "second");
    wait_for("one message ahead", || {
        told(&heard, &session)
            .is_some_and(|event| event["fields"]["availability"]["queue_ahead"] == 1)
    });
    let event = told(&heard, &session).unwrap();
    let now = availability_of(&host, &session);
    assert_eq!(
        shown(&event["fields"]["availability"]),
        json!(["busy", "Busy · 1 message ahead of you", null, 1]),
        "{event}"
    );
    assert_eq!(
        shown(&event["fields"]["availability"]),
        shown(&now),
        "{now}"
    );
    assert_eq!(
        now["queue_ahead"],
        host.get(&format!("/agents/{session}"))["queued_turn_count"]
    );
    assert!(now["last_alive_ms"].as_i64().is_some(), "{now}");
    // Nothing more is told while nothing an app shows changes.
    let count = || {
        heard
            .lock()
            .unwrap()
            .iter()
            .filter(|event| event["kind"] == "availability" && event["session"] == session.as_str())
            .count()
    };
    let before = count();
    std::thread::sleep(Duration::from_secs(1));
    assert_eq!(count(), before);
}

#[test]
fn an_event_stream_is_told_the_host_is_updating_before_it_stops() {
    use nix::sys::signal::{Signal, kill};
    use nix::unistd::Pid;
    let place = Place::new("host-updating");
    let mut host = place.start();
    let heard = host.listen();
    wait_for("the stream to begin", || {
        heard
            .lock()
            .unwrap()
            .iter()
            .any(|event| event["type"] == "agent-roster")
    });
    let before = host.get("/server-info");
    assert!(before["contract"].is_object());
    let pid = Pid::from_raw(i32::try_from(host.process.id()).unwrap());
    kill(pid, Signal::SIGTERM).unwrap();
    wait_for("the Host's last word", || {
        heard
            .lock()
            .unwrap()
            .iter()
            .any(|event| event["type"] == "host-updating")
    });
    let events = heard.lock().unwrap().clone();
    let last = events.last().unwrap();
    assert_eq!(last["type"], "host-updating", "the last frame: {events:?}");
    assert_eq!(last["text"], "Host updating", "{last}");
    let since = last["since_ms"].as_i64().unwrap();
    // No restart measured yet: the default.
    assert_eq!(
        last["back_at_ms"].as_i64().unwrap(),
        since + 45_000,
        "{last}"
    );
    assert!(last.get("event_id").is_none(), "{last}");
    wait_within("the Host to exit", Duration::from_secs(7), || {
        host.process.try_wait().unwrap().is_some()
    });
    // Its next start measures how long it was away (a second or more), for the next stop.
    let data = place.root.join("data");
    assert!(data.join("host-stopped-at-ms").exists());
    let host = place.start();
    assert!(host.get("/server-info")["contract"].is_object());
    assert!(!data.join("host-stopped-at-ms").exists());
    if let Ok(took) = std::fs::read_to_string(data.join("host-restart-ms")) {
        let took: i64 = took.trim().parse().unwrap();
        assert!((1_000..=900_000).contains(&took), "{took}");
    }
}
