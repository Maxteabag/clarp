use super::*;

/// Issue 882: the word after a replay says which run of the Host answered and whether the
/// replay was cut, and the snapshot carries what waits for the owner.
#[test]
fn a_resume_says_when_its_replay_was_cut_and_the_snapshot_carries_what_waits() {
    let place = Place::new("resume-state");
    let host = place.start();
    let features = host.get("/server-info")["capabilities"]["features"].clone();
    assert!(
        features
            .as_array()
            .unwrap()
            .contains(&json!("resume_state")),
        "{features}"
    );
    let session = host.new_agent("Asker");
    let look = |heard: &Arc<Mutex<Vec<Value>>>| {
        let mut found = None;
        wait_for("the word after the replay", || {
            found = heard
                .lock()
                .unwrap()
                .iter()
                .find(|event| event["type"] == "agent-roster" && event.get("replay").is_some())
                .cloned();
            found.is_some()
        });
        found.unwrap()
    };
    // Without a cursor: no resume, nothing cut. The last few minutes are replayed all the
    // same: the new agent's events among them.
    let heard = host.listen();
    let fresh = look(&heard);
    let replay = &fresh["replay"];
    let epoch = replay["epoch"].as_str().unwrap().to_string();
    assert_eq!(
        (&replay["after"], &replay["truncated"]),
        (&Value::Null, &json!(false))
    );
    assert!(
        fresh.get("event_id").is_none(),
        "the word itself has no number"
    );
    // What was replayed: the numbered events before the word.
    let ids: Vec<u64> = heard
        .lock()
        .unwrap()
        .iter()
        .take_while(|event| event.get("replay").is_none())
        .filter_map(|event| event["event_id"].as_u64())
        .collect();
    assert_eq!(replay["count"], json!(ids.len()));
    let newest = *ids.iter().max().expect("the Host told of the new agent");
    // From the newest event: nothing missed, nothing cut.
    let whole = look(&host.listen_at(&format!("/events?last_event_id={newest}")));
    assert_eq!(
        (
            &whole["replay"]["epoch"],
            &whole["replay"]["after"],
            &whole["replay"]["truncated"]
        ),
        (&json!(epoch), &json!(newest), &json!(false))
    );
    // From one this run never gave: cut, so the client reads everything anew.
    let cut = look(&host.listen_at("/events?last_event_id=5"));
    assert_eq!(
        (&cut["replay"]["after"], &cut["replay"]["truncated"]),
        (&json!(5), &json!(true))
    );

    // The snapshot carries what waits for the owner, and loses it once answered.
    let snapshot = host.get("/agents/snapshot");
    assert_eq!(
        (
            &snapshot["attention"]["count"],
            &snapshot["attention"]["items"]
        ),
        (&json!(0), &json!([]))
    );
    let ask = json!({"session": session, "title": "Deploy?", "question": "Deploy version 1.2?"});
    let (status, asked) = host.ask("POST", "/decisions", Some(&ask));
    assert_eq!(status, 201);
    let decision_id = asked["artifact"]["decision"]["decision_id"]
        .as_str()
        .unwrap()
        .to_string();
    let snapshot = host.get("/agents/snapshot");
    let attention = &snapshot["attention"];
    assert_eq!(
        (&attention["count"], &attention["decision_format"]),
        (&json!(1), &json!(2))
    );
    assert_eq!(attention["items"][0]["decision_id"], decision_id.as_str());
    let inbox = host.get("/attention?decision_format=2");
    assert_eq!(
        attention["items"], inbox["items"],
        "as the inbox answers it"
    );
    let accept = json!({ "choice": "accepted", "expected_revision": 1 });
    let (status, _) = host.ask(
        "POST",
        &format!("/decisions/{decision_id}/resolve"),
        Some(&accept),
    );
    assert_eq!(status, 200);
    assert_eq!(host.get("/agents/snapshot")["attention"]["count"], 0);
}
