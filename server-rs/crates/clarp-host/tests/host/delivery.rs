//! The Host's tests of what becomes of a message on its way to an agent: one file a piece, declared in `host.rs`.

use super::*;

/// What a user's send is shown of its delivery (send_delivery_state), as the Python Host shows
/// it with its PR 462 (R120): handed over once its tool is launched; a plain send behind a busy
/// turn pending until then; the user's Stop fences it (stopped) and moves the generation; the
/// same id sent again changes nothing; a resume runs it, and its launch replaces the fence. The
/// apps are told of each fence and each launch by the send's id alone, and the feed gives the
/// sends from its start. A number that is not plain digits is the Python Host's 400.
#[test]
fn a_held_send_is_shown_stopped_then_handed_over_and_each_change_is_hinted() {
    let place = Place::new("delivery");
    // Desk, a stand-in for Claude Code, writes down the token of the turn it runs in: a Stop it
    // makes with it is an agent's, and the Host knows it.
    let token_at = place.root.join("desk-token");
    let script = [
        "#!/bin/sh\nread -r message\n",
        &format!(
            "printf '%s' \"$CLARP_PROVIDER_TURN\" > \"{}\"\n",
            token_at.display()
        ),
        STAND_IN_ANSWERS,
    ]
    .concat();
    let claude = place.root.join("claude");
    program(&claude, &script);
    let world = [("CLARP_CLAUDE_PROGRAM", claude.to_str().unwrap())];
    let host = place.start_in("secret", &[], &world);
    let made = host.post(
        "/agents",
        json!({ "name": "Desk", "session": "desk", "backend": "claude" }),
    );
    assert_eq!(made["session"], "desk", "{made}");
    host.send("desk", "hello", "d-1");
    wait_for("Desk's turn to hold its token", || {
        std::fs::read_to_string(&token_at).is_ok_and(|token| !token.is_empty())
    });
    let desk_token = std::fs::read_to_string(&token_at).unwrap();
    let session = host.new_agent("Deliverer");
    let heard = host.listen_at("/events?token=secret");
    host.send(&session, "sleep 60", "m1");
    wait_for("the turn to run", || host.state(&session) == "thinking");
    host.send(&session, "later", "m2");
    // One left for later has no delivery at any step, as the Python Host keeps no receipt of it.
    let left = host.post(
        "/send",
        json!({ "session": session, "text": "left for later", "client_msg_id": "m3", "queue_if_busy": true }),
    );
    assert_eq!(left["queued"], true, "{left}");
    let log = |query: &str| host.get(&format!("/log?session={session}{query}"));
    let delivery = |log: &Value, id: &str| {
        let turns = log["turns"].as_array().unwrap();
        let row = turns.iter().find(|turn| turn["id"] == id);
        row.map_or(Value::Null, |row| row["delivery"].clone())
    };
    // What waits is no row yet (issue 491): its delivery is read in the feed, by its id.
    let waiting = |id: &str| {
        let feed = log("&delivery_since=0");
        let updates = feed["delivery_updates"].as_array().unwrap().clone();
        let update = updates.iter().find(|update| update["message_id"] == id);
        update.map_or(Value::Null, |update| update["delivery"].clone())
    };
    let before = log("");
    assert_eq!(
        delivery(&before, "u-m1")["state"],
        "handed_over",
        "{before}"
    );
    let rows = |log: &Value| -> Vec<String> {
        let turns = log["turns"].as_array().unwrap();
        turns
            .iter()
            .map(|turn| turn["id"].as_str().unwrap().to_string())
            .collect()
    };
    assert_eq!(rows(&before), ["u-m1"], "{before}");
    let queued = waiting("u-m2");
    assert_eq!(
        (
            &queued["state"],
            &queued["rev"],
            &queued["for"]["client_msg_id"]
        ),
        (&json!("pending_launch"), &json!(1), &json!("m2")),
        "{before}"
    );
    assert_eq!(
        (
            &before["delivery_stop_generation"],
            &before["delivery_scope"]["runtime_release"],
        ),
        (&json!(0), &json!("in-process"))
    );
    assert_ne!(before["delivery_scope"]["agent_id"], "");

    // Desk stops it, in its own turn: an agent's Stop the Host knows. The turn ends and what
    // waits is held, but nothing of the user's is withdrawn: still pending, the generation as
    // it was.
    let body = json!({ "session": session, "actor": "agent:desk" }).to_string();
    let headers = [
        ("X-Clarp-Turn", desk_token.as_str()),
        ("Content-Type", "application/json"),
    ];
    let answered = fetch(
        &host,
        &host.token,
        "POST",
        "/stop",
        &headers,
        body.as_bytes(),
    );
    let by_desk: Value = serde_json::from_slice(&answered.body).unwrap();
    assert_eq!(
        (
            answered.status,
            &by_desk["stop_actor"],
            &by_desk["stop_actor_verified"]
        ),
        (200, &json!("agent:desk"), &json!(true)),
        "{by_desk}"
    );
    wait_for("the turn to end", || host.state(&session) == "interrupted");
    let after_desk = log("");
    assert_eq!(waiting("u-m2"), queued, "{after_desk}");
    assert_eq!(after_desk["delivery_stop_generation"], 0);

    // The user's Stop: the one that waits is fenced, and the generation moves.
    host.post("/stop", json!({ "session": session }));
    let held = log("&delivery_since=0");
    assert_eq!(waiting("u-m3"), Value::Null, "{held}");
    let stopped = waiting("u-m2");
    assert_eq!(
        (&stopped["state"], &stopped["rev"]),
        (&json!("stopped"), &json!(2)),
        "{held}"
    );
    assert_eq!(held["delivery_stop_generation"], 1);
    let updates = held["delivery_updates"].as_array().unwrap();
    let ids: Vec<&str> = updates
        .iter()
        .map(|update| update["message_id"].as_str().unwrap())
        .collect();
    assert_eq!(ids, ["u-m1", "u-m2"], "{held}");
    assert_eq!(held["delivery_bootstrap"], true);

    // The same id again changes nothing of it.
    host.send(&session, "later", "m2");
    assert_eq!(waiting("u-m2"), stopped);
    assert_eq!(rows(&log("")), ["u-m1"], "held, it is still no row");

    host.post("/turn-queue/resume", json!({ "session": session }));
    wait_for("the held one to be answered", || {
        host.conversation(&session)
            .contains(&said("assistant", "later"))
    });
    let after = delivery(&log(""), "u-m2");
    assert_eq!(
        (&after["state"], &after["rev"]),
        (&json!("handed_over"), &json!(3)),
        "{after}"
    );

    // Each held one enters the conversation when its turn starts, above its own answer.
    wait_for("the one left for later to be answered", || {
        host.conversation(&session)
            .contains(&said("assistant", "left for later"))
    });
    let answered = log("");
    let roles: Vec<(String, String)> = answered["turns"]
        .as_array()
        .unwrap()
        .iter()
        .map(|turn| (turn["id"].as_str().unwrap().to_string(), turn["role"].as_str().unwrap().to_string()))
        .collect();
    let at = |id: &str| roles.iter().position(|(row, _)| row == id).unwrap();
    assert!(
        at("u-m2") < at("u-m3") && roles[at("u-m2") + 1].1 == "assistant",
        "{answered}"
    );

    // Told by id alone: the first's launch, the second's fence, and its launch.
    let hints = || -> Vec<Value> {
        heard
            .lock()
            .unwrap()
            .iter()
            .filter(|event| {
                event["type"] == "delivery-updated" && event["session"] == session.as_str()
            })
            .cloned()
            .collect()
    };
    wait_for("the hints", || hints().len() >= 3);
    let told: Vec<String> = hints()
        .iter()
        .map(|hint| hint["message_id"].as_str().unwrap().to_string())
        .collect();
    assert_eq!(told, ["u-m1", "u-m2", "u-m2"], "{:?}", hints());
    for hint in hints() {
        assert!(
            hint.get("delivery").is_none() && hint["session"] == session,
            "{hint}"
        );
    }

    let (status, refused) = host.ask(
        "GET",
        &format!("/log?session={session}&delivery_since=x"),
        None,
    );
    assert_eq!(
        (status, &refused["error"]),
        (400, &json!("bad delivery_since"))
    );
}
