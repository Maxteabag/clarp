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
    put_agent(
        &place,
        &agent("agent-gone", "gone", json!({ "deleted_at_ms": 5 })),
    );
    put_agent(
        &place,
        &agent("agent-left", "left", helper_of("agent-gone")),
    );
    put_agent(&place, &agent("agent-here", "here", json!({})));
    put_agent(
        &place,
        &agent("agent-kept", "kept", helper_of("agent-here")),
    );

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
    assert_eq!(
        stored_agent(&place, "agent-kept")["helper_state"],
        "running"
    );
    assert_eq!(stored_agent(&place, "agent-gone")["ledger_retired"], true);

    // The next start finds it abandoned already: nothing is moved or written twice.
    drop(place.start());
    let again = stored_agent(&place, "agent-left");
    assert_eq!(again["helper_state"], "abandoned");
    assert_eq!(again["helper_completed_at_ms"], json!(completed));
}

/// Asks the Host to stop as systemd does.
fn terminate(host: &Host) {
    use nix::sys::signal::{Signal, kill};
    use nix::unistd::Pid;
    let pid = Pid::from_raw(i32::try_from(host.process.id()).unwrap());
    kill(pid, Signal::SIGTERM).unwrap();
}

/// Issue 880: an event stream open when the Host is told to stop ends at once, the first step
/// of the stop, so its app reconnects to the next Host without waiting out the drain (6.8 s
/// after a stop, against 1.9 s after a kill, before).
#[test]
fn an_event_stream_open_when_the_host_is_told_to_stop_ends_at_once() {
    let place = Place::new("shutdown-ends-streams");
    let mut host = place.start();
    let mut stream = TcpStream::connect(&host.address).unwrap();
    stream
        .write_all(b"GET /events HTTP/1.1\r\nHost: test\r\nAccept: text/event-stream\r\n\r\n")
        .unwrap();
    stream
        .set_read_timeout(Some(Duration::from_secs(20)))
        .unwrap();
    let mut heard = Vec::new();
    let mut chunk = [0u8; 4096];
    while !String::from_utf8_lossy(&heard).contains("agent-roster") {
        let count = stream.read(&mut chunk).unwrap();
        assert!(count > 0, "the stream ended before it began");
        heard.extend_from_slice(&chunk[..count]);
    }

    // A FIFO without a reader stalls the optional restart timestamp write.
    let mark = place.root.join("data/host-stopped-at-ms");
    nix::unistd::mkfifo(
        &mark,
        nix::sys::stat::Mode::S_IRUSR | nix::sys::stat::Mode::S_IWUSR,
    )
    .unwrap();
    let stopping = Instant::now();
    terminate(&host);
    // The stream's last chunk comes, and its connection is closed.
    stream
        .set_read_timeout(Some(Duration::from_secs(4)))
        .unwrap();
    loop {
        match stream.read(&mut chunk) {
            Ok(0) => break,
            Ok(count) => {
                heard.extend_from_slice(&chunk[..count]);
                if heard.ends_with(b"0\r\n\r\n") {
                    break;
                }
            }
            Err(error) => panic!("the stream did not end after the stop: {error}"),
        }
    }
    assert_eq!(
        String::from_utf8_lossy(&heard)
            .matches("host-updating")
            .count(),
        1,
        "the stream received one update frame before closing"
    );
    let ended = stopping.elapsed();
    // A few hundred milliseconds; a second allows for a loaded build machine.
    assert!(
        ended < Duration::from_secs(1),
        "the stream ended {ended:?} after the stop"
    );
    // Nothing else holds the Host: it is gone long before the drain's five seconds.
    wait_within("the Host to exit", Duration::from_secs(4), || {
        host.process.try_wait().unwrap().is_some()
    });
    assert!(
        !host
            .heard
            .lock()
            .unwrap()
            .contains("HTTP shutdown deadline reached")
    );
}

/// A connection that never ends still cannot keep the Host from exiting: a request whose body
/// never comes is waited for to the drain's deadline, and no longer.
#[test]
fn a_connection_that_never_ends_cannot_keep_the_host_from_exiting() {
    let place = Place::new("shutdown-open-request");
    let mut host = place.start();
    let mut stream = TcpStream::connect(&host.address).unwrap();
    stream
        .write_all(
            b"POST /send HTTP/1.1\r\nHost: test\r\nContent-Type: application/json\r\nContent-Length: 1000\r\n\r\n{\"session\"",
        )
        .unwrap();
    wait_within(
        "the request to be at the Host",
        Duration::from_secs(5),
        || host.get("/server-info")["contract"].is_object(),
    );

    let stopping = Instant::now();
    terminate(&host);
    // Allow for scheduling the signal and observing the exit on a shared build machine.
    wait_within(
        "the Host to exit with an open request",
        Duration::from_secs(7),
        || host.process.try_wait().unwrap().is_some(),
    );
    let status = host.process.wait().unwrap();
    assert!(status.success(), "{status}");
    assert!(stopping.elapsed() >= Duration::from_secs(5));
    wait_within(
        "the count of open connections",
        Duration::from_secs(1),
        || {
            host.heard
                .lock()
                .unwrap()
                .contains("HTTP shutdown deadline reached with 1 open connections")
        },
    );
    drop(stream);
}
