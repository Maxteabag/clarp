//! The Host's tests of what a Host tolerates as it starts: one file a piece, declared in `host.rs`.

use super::*;

/// Bytes put where a stopped Host keeps a table, by the store's own crate: synthetic, nobody's.
fn put_bytes(place: &Place, table: &str, key: &str, bytes: &[u8]) {
    use clarp_ports::kv::KeyValueEngine as _;
    clarp_store_sqlite::SqliteKeyValue
        .open(&place.root.join("data/fleet.db"))
        .unwrap()
        .put(table, key, bytes)
        .unwrap();
}

/// Bytes where a stopped Host keeps them.
fn stored_bytes(place: &Place, table: &str, key: &str) -> Option<Vec<u8>> {
    use clarp_ports::kv::KeyValueEngine as _;
    clarp_store_sqlite::SqliteKeyValue
        .open(&place.root.join("data/fleet.db"))
        .unwrap()
        .get(table, key)
        .unwrap()
}

/// How the Host's start went, once no agent is still being opened.
fn started(host: &Host) -> Value {
    let mut said = Value::Null;
    wait_for("every agent to be opened", || {
        said = host.get("/host/start");
        said["opening"] == 0
    });
    said
}

const NOT_JSON: &[u8] = b"not json at all";
const TOKEN: &str = "0123456789abcdefghijklmnopqrstuv";

#[test]
fn startup_preserves_the_quota_key_and_sets_aside_invalid_bytes() {
    let place = Place::new("start-quota-text");
    drop(place.start());

    let key = b"abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789";
    let invalid = b"\xffquota";
    put_bytes(&place, "kept.quota", "key", key);
    put_bytes(&place, "kept.quota", "invalid", invalid);

    let host = place.start();
    let said = started(&host);
    assert_eq!(said["left_out"]["quota"], 1, "{said}");
    drop(host);

    assert_eq!(
        stored_bytes(&place, "kept.quota", "key").as_deref(),
        Some(key.as_slice())
    );
    assert_eq!(stored_bytes(&place, "kept.quota", "key.unreadable"), None);
    assert_eq!(stored_bytes(&place, "kept.quota", "invalid"), None);
    assert_eq!(
        stored_bytes(&place, "kept.quota", "invalid.unreadable").as_deref(),
        Some(invalid.as_slice())
    );
}

#[test]
fn a_host_starts_past_what_it_cannot_read_and_its_own_door_counts_it() {
    let place = Place::new("start-tolerance");
    let host = place.start();
    let kept = host.new_agent("Kept");
    // A Host in order: nothing left out, nothing unrun, and the reference's shape at its door.
    let first = started(&host);
    // Its agents are Kept and the janitors it installs of its own.
    let agents = first["agents"].as_u64().unwrap();
    assert!(agents >= 1, "{first}");
    assert_eq!(
        (&first["broken"], &first["left_out"], &first["not_run"]),
        (&json!(0), &json!({}), &json!({})),
        "{first}"
    );
    assert!(host.get("/server-info").get("left_out").is_none());
    // What its count covers: the kinds judged when read are counted then, not at a start.
    let when_read = first["judged_when_read"].as_array().unwrap();
    assert!(when_read.contains(&json!("vocabulary-runs")), "{first}");
    assert!(!when_read.contains(&json!("agent")), "{first}");
    drop(host);

    // Written while it was stopped, by the store's own crate: an agent's record that is no JSON,
    // an agent whose tool this Host has not, a push token's row of another shape, a schedules'
    // document that cannot be read, a heartbeat's book with one entry that is not its own.
    put_bytes(&place, "agent", "agent-unreadable", NOT_JSON);
    let elsewhere = json!({
        "agent_id": "agent-elsewhere", "session": "elsewhere", "persona": "Elsewhere",
        "backend": "no-such-tool", "cwd": place.root.join("work"), "conversation_id": "c-1",
        "created_at_ms": 1,
    });
    put_bytes(
        &place,
        "agent",
        "agent-elsewhere",
        &serde_json::to_vec(&elsewhere).unwrap(),
    );
    put_bytes(&place, "push", TOKEN, b"[]");
    put_bytes(&place, "kept.schedule", "all", b"{ not a list");
    let heartbeat = json!({ "agents": { "someone": "not a state" } });
    let heartbeat = serde_json::to_vec(&heartbeat).unwrap();
    put_bytes(&place, "kept.heartbeat", "all", &heartbeat);

    // It starts, runs what it can, and says what it could not, at its own door and in one line.
    let host = place.start();
    let said = started(&host);
    assert_eq!(
        (&said["agents"], &said["broken"]),
        (&json!(agents), &json!(0)),
        "{said}"
    );
    assert_eq!(
        said["left_out"],
        json!({ "agent": 1, "heartbeat": 1, "push": 1, "schedule": 1 }),
        "{said}"
    );
    assert_eq!(said["not_run"], json!({ "no-such-tool": 1 }), "{said}");
    assert!(host.get("/server-info").get("left_out").is_none());
    assert!(
        host.get("/agents/snapshot")["agents"]
            .as_array()
            .unwrap()
            .iter()
            .any(|row| row["session"] == kept.as_str())
    );
    // The journal names each by its kind and key, the token, a secret, by its place alone, and nothing
    // of what any of them holds.
    wait_for("the start to be said", || {
        host.heard
            .lock()
            .unwrap()
            .contains("records read at the start could not be read and are left out")
    });
    let journal = host.heard.lock().unwrap().clone();
    assert!(journal.contains(
        "a record of the kind agent could not be read and is left out: agent-unreadable"
    ));
    assert!(journal.contains(
        "a record of the kind push could not be read and is left out: 1 of this kind so far"
    ));
    assert!(journal.contains("set aside as all.unreadable"));
    assert!(
        !journal.contains(&TOKEN[..4])
            && !journal.contains("not json")
            && !journal.contains("not a state")
    );
    // What could not be read is where it was, byte for byte; the schedules' document is set
    // aside besides, and the heartbeat's entry is carried.
    assert_eq!(
        stored_bytes(&place, "agent", "agent-unreadable").as_deref(),
        Some(NOT_JSON)
    );
    assert_eq!(
        stored_bytes(&place, "push", TOKEN).as_deref(),
        Some(&b"[]"[..])
    );
    assert_eq!(
        stored_bytes(&place, "kept.schedule", "all.unreadable").as_deref(),
        Some(&b"{ not a list"[..])
    );
    assert_eq!(
        stored_bytes(&place, "kept.heartbeat", "all").as_deref(),
        Some(heartbeat.as_slice())
    );
    // The agent it keeps and does not run keeps its name.
    let (status, refused) = host.ask(
        "POST",
        "/agents",
        Some(&json!({ "name": "Elsewhere", "backend": "scripted" })),
    );
    assert_eq!(
        (status, &refused["error"]),
        (409, &json!("contact_occupied")),
        "{refused}"
    );
    drop(host);

    // Mended where it is kept, it is read again, and the door no longer counts it.
    let mended = json!({
        "agent_id": "agent-unreadable", "session": "mended", "persona": "Mended",
        "backend": "scripted", "cwd": place.root.join("work"), "conversation_id": "c-2",
        "created_at_ms": 2,
    });
    put_bytes(
        &place,
        "agent",
        "agent-unreadable",
        &serde_json::to_vec(&mended).unwrap(),
    );
    let host = place.start();
    let said = started(&host);
    assert_eq!(said["agents"], agents + 1, "{said}");
    assert!(said["left_out"].get("agent").is_none(), "{said}");
}

#[test]
fn a_ledger_read_by_key_is_judged_when_read_and_not_numbered_anew() {
    let place = Place::new("start-tolerance-ledger");
    let host = place.start();
    let maker = host.new_agent("FormMaker");
    let form = json!({
        "session": maker, "artifact_id": "f", "type": "html_form", "title": "Test form",
        "payload": { "version": "v1", "content": "<p>Test</p>", "answer_schema": { "type": "object" } },
    });
    assert_eq!(host.ask("POST", "/artifacts", Some(&form)).0, 201);
    let event = |id: &str, n: u64| {
        json!({ "version": "v1", "events": [
            { "event_id": id, "client_seq": n, "client_at": n, "event": { "number": n } },
        ] })
    };
    let first = event("first-0000000000", 1);
    assert_eq!(host.ask("POST", "/artifacts/f/events", Some(&first)).0, 200);
    drop(host);

    // The form's tail, written while it was stopped, is no tail; its first row is kept.
    put_bytes(&place, "kept.form-event-tails", "f", NOT_JSON);
    let row = stored_bytes(&place, "kept.form-event-rows", "1:f1");
    assert!(row.is_some());
    // A start does not read a ledger: nothing of it is counted there.
    let host = place.start();
    let said = started(&host);
    assert!(said["left_out"].get("form-event-tails").is_none(), "{said}");
    // Read when an event comes: set aside and counted, and the ledger is not numbered anew over
    // the row it kept.
    let second = event("second-000000000", 2);
    assert_eq!(
        host.ask("POST", "/artifacts/f/events", Some(&second)).0,
        500
    );
    assert_eq!(stored_bytes(&place, "kept.form-event-rows", "1:f1"), row);
    assert_eq!(
        stored_bytes(&place, "kept.form-event-tails", "f.unreadable").as_deref(),
        Some(NOT_JSON)
    );
    // Moved aside, its key free: the next event is not numbered anew over the row either.
    let third = event("third-0000000000", 3);
    assert_eq!(host.ask("POST", "/artifacts/f/events", Some(&third)).0, 500);
    assert_eq!(stored_bytes(&place, "kept.form-event-rows", "1:f1"), row);
    assert_eq!(host.get("/host/start")["left_out"]["form-event-tails"], 1);
}

#[test]
fn a_place_that_is_none_is_set_aside_when_it_is_asked_for() {
    let place = Place::new("start-tolerance-place");
    drop(place.start());
    let north: &[u8] = br#"{"lat":"north"}"#;
    put_bytes(&place, "kept.place", "s", north);
    // Not read at a start; read when asked for: no place is told, and it is set aside and counted.
    let host = place.start();
    let said = started(&host);
    assert!(said["left_out"].get("place").is_none(), "{said}");
    assert_eq!(host.get("/location?session=s"), json!({}));
    assert_eq!(host.get("/host/start")["left_out"]["place"], 1);
    assert_eq!(
        stored_bytes(&place, "kept.place", "s.unreadable").as_deref(),
        Some(north)
    );
}

#[test]
fn the_hosts_key_moved_aside_is_never_drawn_anew() {
    let place = Place::new("start-tolerance-key");
    drop(place.start());
    put_bytes(&place, "kept.turn-identity", "key", NOT_JSON);
    // The key cannot be read: no turn is given a token, and the start moves it aside.
    drop(place.start());
    assert_eq!(stored_bytes(&place, "kept.turn-identity", "key"), None);
    assert_eq!(
        stored_bytes(&place, "kept.turn-identity", "key.unreadable").as_deref(),
        Some(NOT_JSON)
    );
    // Its key free and its copy standing, the next start does not draw a new one: every token
    // given with the old would name nobody.
    drop(place.start());
    assert_eq!(stored_bytes(&place, "kept.turn-identity", "key"), None);
}
