use super::*;

/// The ops of the `live` events a connection was sent for one chat, after `from`.
fn ops_for(heard: &Arc<Mutex<Vec<Value>>>, from: usize, session: &str) -> Vec<String> {
    heard.lock().unwrap()[from..]
        .iter()
        .filter(|event| event["type"] == "live" && event["session"] == session)
        .flat_map(|event| event["ops"].as_array().cloned().unwrap_or_default())
        .map(|op| op["op"].as_str().unwrap_or("").to_string())
        .collect()
}

/// Issue 892: a stream opened with `live_change=1` says its id first; its opener changes what it
/// follows without reopening it, and the stream says where the change takes effect. Another
/// token finds no such stream; a stream opened without the option is as before.
#[test]
fn the_chats_an_open_stream_follows_change_without_reopening_it() {
    let place = Place::new("live-change");
    // A Host with a token: who opened a stream is told apart from another device (on a Host
    // without one every caller is the administrator).
    let host = place.start_with_token("the-owners-token");
    let features = host.get("/server-info")["capabilities"]["features"].clone();
    assert!(
        features
            .as_array()
            .unwrap()
            .contains(&json!("events_live_change")),
        "{features}"
    );
    let first = host.new_agent("First");
    let second = host.new_agent("Second");
    let changing = host.listen_at(&format!(
        "/events?token=the-owners-token&live={first}&live_change=1"
    ));
    let plain = host.listen_at(&format!("/events?token=the-owners-token&live={first}"));
    wait_for("the streams to open", || {
        [&changing, &plain].iter().all(|heard| {
            heard
                .lock()
                .unwrap()
                .iter()
                .any(|event| event["type"] == "agent-roster")
        })
    });
    // Its id is the stream's first word, and only a stream that asked has one.
    let told = changing.lock().unwrap()[0].clone();
    assert_eq!(told["type"], "stream", "{told}");
    let id = told["stream_id"].as_str().unwrap().to_string();
    assert_eq!(id.len(), 32);
    assert!(
        plain
            .lock()
            .unwrap()
            .iter()
            .all(|event| event["type"] != "stream")
    );

    // Only the token that opened it may change it.
    let (device, _) = pair(&host, "Other", "full");
    let path = format!("/events/{id}/live");
    let body = json!({ "live": [second] });
    let (status, refused) = host.ask_as(&device, "POST", &path, Some(&body));
    assert_eq!(
        (status, &refused["error"]),
        (404, &json!("stream not found"))
    );
    let (status, _) = host.ask(
        "POST",
        "/events/0123456789abcdef0123456789abcdef/live",
        Some(&body),
    );
    assert_eq!(status, 404);
    let (status, refused) = host.ask("POST", &path, Some(&json!({ "live": "nope" })));
    assert_eq!(
        (status, &refused["error"]),
        (400, &json!("live must be a list of sessions or agent ids"))
    );

    let from = changing.lock().unwrap().len();
    let changed = host.post(&path, json!({ "live": [second, " ", second] }));
    assert_eq!(
        changed,
        json!({ "ok": true, "stream_id": id, "live": [second] })
    );
    wait_for("the stream to say where the change takes effect", || {
        changing.lock().unwrap()[from..]
            .iter()
            .any(|event| event["type"] == "live-changed")
    });
    let said = changing.lock().unwrap()[from..]
        .iter()
        .find(|event| event["type"] == "live-changed")
        .cloned()
        .unwrap();
    assert_eq!(said["live"], json!([second]));
    assert!(said.get("event_id").is_none(), "it is never replayed");
    let after = changing.lock().unwrap().len();

    // From there on the new chat's items come whole, and the old one's status lines alone.
    host.send(&second, "for the second", "c1");
    host.send(&first, "for the first", "c2");
    wait_for("both answers", || {
        [&first, &second].iter().all(|agent| {
            host.turns(agent)
                .iter()
                .any(|row| row["role"] == "assistant")
        })
    });
    wait_for("the second chat's items", || {
        ops_for(&changing, after, &second)
            .iter()
            .any(|op| op == "upsert")
    });
    std::thread::sleep(Duration::from_millis(500));
    let old = ops_for(&changing, after, &first);
    assert!(!old.is_empty(), "the old chat's status still comes");
    assert!(
        old.iter().all(|op| op == "status" || op == "turn"),
        "the old chat's items no longer come: {old:?}"
    );
    // The stream that did not ask follows what it always did.
    assert!(ops_for(&plain, 0, &first).iter().any(|op| op == "upsert"));
    assert!(
        ops_for(&plain, 0, &second)
            .iter()
            .all(|op| op == "status" || op == "turn")
    );

    // A device paired with limited access reads the stream, and changes what its own stream
    // follows; another limited device finds no such stream.
    let (limited, _) = pair(&host, "Watch", "limited");
    let (other_limited, _) = pair(&host, "Other watch", "limited");
    let watching = host.listen_at(&format!(
        "/events?token={limited}&live={first}&live_change=1&compact=1"
    ));
    wait_for("the limited stream to open", || {
        !watching.lock().unwrap().is_empty()
    });
    let its = watching.lock().unwrap()[0]["stream_id"]
        .as_str()
        .expect("a limited stream says its id first")
        .to_string();
    let its_path = format!("/events/{its}/live");
    let (status, _) = host.ask_as(
        &other_limited,
        "POST",
        &its_path,
        Some(&json!({ "live": [second] })),
    );
    assert_eq!(status, 404, "another device finds no such stream");
    let (status, changed) = host.ask_as(
        &limited,
        "POST",
        &its_path,
        Some(&json!({ "live": [second] })),
    );
    assert_eq!(
        (status, &changed),
        (
            200,
            &json!({ "ok": true, "stream_id": its, "live": [second] })
        )
    );
    wait_for(
        "the limited stream to say where the change takes effect",
        || {
            watching
                .lock()
                .unwrap()
                .iter()
                .any(|event| event["type"] == "live-changed" && event["live"] == json!([second]))
        },
    );
}
