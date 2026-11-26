use super::*;

/// The events a connection was sent up to, and without, the first plain `agent-roster`.
fn before_settled(heard: &Arc<Mutex<Vec<Value>>>) -> Vec<Value> {
    heard.lock().unwrap().clone()
}

/// Issue 882: a resume that missed events is told so first and given only the newest word of
/// each conversation; a connection without a cursor is given no replay.
#[test]
fn a_resume_is_told_so_first_and_given_the_newest_of_what_it_missed() {
    let place = Place::new("compact-resume");
    let host = place.start();
    let features = host.get("/server-info")["capabilities"]["features"].clone();
    assert!(
        features
            .as_array()
            .unwrap()
            .contains(&json!("compact_resume")),
        "{features}"
    );
    let first = host.new_agent("First");
    let second = host.new_agent("Second");
    let watching = host.listen();
    wait_for("the connection to be open", || {
        watching
            .lock()
            .unwrap()
            .iter()
            .any(|event| event["type"] == "agent-roster")
    });
    let numbered = |heard: &Arc<Mutex<Vec<Value>>>| -> Vec<u64> {
        heard
            .lock()
            .unwrap()
            .iter()
            .filter_map(|event| event["event_id"].as_u64())
            .collect()
    };
    // Where a client that drops now stands.
    host.send(&first, "one first", "c0");
    wait_for("the answer", || {
        host.turns(&first)
            .iter()
            .any(|row| row["role"] == "assistant")
    });
    std::thread::sleep(Duration::from_millis(500));
    let cursor = *numbered(&watching).iter().max().unwrap();
    // Then it misses three answers in one chat and one in another.
    for n in 1..=3 {
        host.post(
            "/send",
            json!({ "session": first, "text": format!("more {n}"), "client_msg_id": format!("c{n}"),
                    "queue_if_busy": true }),
        );
    }
    host.send(&second, "one second", "d1");
    wait_for("every answer", || {
        host.turns(&first)
            .iter()
            .filter(|row| row["role"] == "assistant")
            .count()
            == 4
            && host
                .turns(&second)
                .iter()
                .any(|row| row["role"] == "assistant")
    });
    std::thread::sleep(Duration::from_millis(800));
    let missed: Vec<Value> = watching
        .lock()
        .unwrap()
        .iter()
        .filter(|event| event["event_id"].as_u64().is_some_and(|id| id > cursor))
        .cloned()
        .collect();
    let updates = |events: &[Value], session: &str| {
        events
            .iter()
            .filter(|event| event["type"] == "transcript-updated" && event["session"] == session)
            .count()
    };
    assert!(updates(&missed, &first) > 1, "{missed:#?}");

    let resumed = host.listen_at(&format!("/events?compact=1&last_event_id={cursor}"));
    wait_for("the catch-up", || {
        let heard = resumed.lock().unwrap();
        let ids: Vec<u64> = heard
            .iter()
            .filter_map(|e| e["event_id"].as_u64())
            .collect();
        ids.last()
            == missed
                .iter()
                .filter_map(|e| e["event_id"].as_u64())
                .max()
                .as_ref()
            || ids.len() > missed.len()
    });
    std::thread::sleep(Duration::from_millis(300));
    let given = before_settled(&resumed);
    // Told so first, without a number.
    let marker = &given[0];
    assert_eq!(marker["type"], "agent-roster", "{given:#?}");
    assert_eq!(marker["resync"], true);
    assert_eq!(marker["truncated"], false);
    assert!(marker.get("event_id").is_none());
    // What the catch-up gave: the stretch up to the newest event missed. Live events may follow.
    let upto = missed
        .iter()
        .filter_map(|event| event["event_id"].as_u64())
        .max()
        .unwrap();
    let in_gap = |event: &&Value| {
        event["event_id"]
            .as_u64()
            .is_some_and(|id| id > cursor && id <= upto)
    };
    let sent: Vec<&Value> = given[1..].iter().filter(in_gap).collect();
    let caught: Vec<Value> = sent.iter().map(|event| (*event).clone()).collect();
    // The newest word of each conversation, everything else as it was, in order.
    assert_eq!(updates(&caught, &first), 1, "{caught:#?}");
    assert_eq!(updates(&caught, &second), 1);
    let newest = missed
        .iter()
        .rfind(|event| event["type"] == "transcript-updated" && event["session"] == first)
        .unwrap();
    assert!(
        sent.iter()
            .any(|event| event["event_id"] == newest["event_id"])
    );
    let states = |events: &[&Value]| {
        events
            .iter()
            .filter(|event| event["type"] == "agent-state")
            .count()
    };
    let missed_refs: Vec<&Value> = missed.iter().collect();
    assert_eq!(states(&sent), states(&missed_refs), "every state as before");
    let ids: Vec<u64> = sent
        .iter()
        .map(|e| e["event_id"].as_u64().unwrap())
        .collect();
    assert!(ids.windows(2).all(|pair| pair[0] < pair[1]), "{ids:?}");
    assert_eq!(
        marker["skipped"].as_u64().unwrap() as usize,
        missed.len() - sent.len(),
        "skipped counts what was not sent"
    );
    // A catch-up starts with the marker and ends with the plain word, as every replay does:
    // between them only what it was given.
    let plain_at = given
        .iter()
        .position(|event| event == &json!({ "type": "agent-roster" }))
        .expect("the catch-up ends with the plain agent-roster");
    let last_caught = given
        .iter()
        .rposition(|event| event["event_id"].as_u64() == ids.last().copied())
        .unwrap();
    assert!(plain_at > last_caught, "{given:#?}");
    assert!(
        given[1..plain_at]
            .iter()
            .all(|event| event["event_id"].as_u64().is_some()),
        "only what it was given between the marker and the word: {given:#?}"
    );

    // A cursor this run never gave: told the gap is not whole.
    let old = host.listen_at("/events?compact=1&last_event_id=5");
    wait_for("the marker", || !old.lock().unwrap().is_empty());
    let told = old.lock().unwrap()[0].clone();
    assert_eq!(
        (&told["resync"], &told["truncated"]),
        (&json!(true), &json!(true))
    );

    // A connection without a cursor is given no replay, only the word to read the snapshot.
    let fresh = host.listen_at("/events?compact=1");
    wait_for("the word", || !fresh.lock().unwrap().is_empty());
    std::thread::sleep(Duration::from_millis(300));
    let heard = before_settled(&fresh);
    assert_eq!(heard[0], json!({ "type": "agent-roster" }), "{heard:#?}");
    assert!(
        heard.iter().all(|event| event.get("event_id").is_none()),
        "{heard:#?}"
    );

    // A resume that missed nothing is as before.
    let now = *numbered(&watching).iter().max().unwrap();
    let even = host.listen_at(&format!("/events?compact=1&last_event_id={now}"));
    wait_for("the word", || !even.lock().unwrap().is_empty());
    assert_eq!(even.lock().unwrap()[0], json!({ "type": "agent-roster" }));

    // A stream that did not ask is as before: every missed event, in order, then the plain
    // word, and no marker; without a cursor, what was kept of the last minutes.
    let as_before = host.listen_at(&format!("/events?last_event_id={cursor}"));
    wait_for("the replay and its word", || {
        as_before
            .lock()
            .unwrap()
            .iter()
            .any(|event| event["type"] == "agent-roster" && event.get("event_id").is_none())
    });
    let replayed: Vec<Value> = {
        let heard = as_before.lock().unwrap();
        let end = heard
            .iter()
            .position(|event| event["type"] == "agent-roster" && event.get("event_id").is_none())
            .unwrap();
        assert!(
            heard.iter().all(|event| event.get("resync").is_none()),
            "{heard:#?}"
        );
        heard[..end].to_vec()
    };
    let replayed_ids: Vec<u64> = replayed
        .iter()
        .filter_map(|e| e["event_id"].as_u64())
        .collect();
    let missed_ids: Vec<u64> = missed
        .iter()
        .filter_map(|e| e["event_id"].as_u64())
        .collect();
    assert!(
        missed_ids.iter().all(|id| replayed_ids.contains(id)),
        "every missed event is replayed"
    );
    assert_eq!(updates(&replayed, &first), updates(&missed, &first));
    let kept = host.listen();
    wait_for("the replay and its word", || {
        kept.lock()
            .unwrap()
            .iter()
            .any(|event| event["type"] == "agent-roster" && event.get("event_id").is_none())
    });
    assert!(
        kept.lock()
            .unwrap()
            .iter()
            .any(|event| event["event_id"].as_u64().is_some()),
        "without a cursor the last minutes are replayed"
    );
}
