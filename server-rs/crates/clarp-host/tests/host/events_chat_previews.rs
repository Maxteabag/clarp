use super::*;

/// The fields of an agent's snapshot row a chat's preview carries.
const PREVIEW_FIELDS: [&str; 9] = [
    "conversation_id",
    "head_revision",
    "last_message_id",
    "last_message",
    "busy",
    "latest_state",
    "latest_state_ts",
    "status_text",
    "last_activity",
];

/// The events a connection was sent, each with the moment it arrived.
type Timed = Arc<Mutex<Vec<(Instant, Value)>>>;

fn listen_timed(host: &Host, path: &str) -> Timed {
    let heard: Timed = Arc::new(Mutex::new(Vec::new()));
    let mut stream = TcpStream::connect(&host.address).unwrap();
    let asked = format!("GET {path} HTTP/1.1\r\nHost: test\r\nAccept: text/event-stream\r\n\r\n");
    stream.write_all(asked.as_bytes()).unwrap();
    let collected = heard.clone();
    std::thread::spawn(move || {
        for line in BufReader::new(stream).lines() {
            let Ok(line) = line else { break };
            if let Some(event) = line
                .strip_prefix("data: ")
                .and_then(|data| serde_json::from_str::<Value>(data).ok())
            {
                collected.lock().unwrap().push((Instant::now(), event));
            }
        }
    });
    heard
}

fn events(heard: &Timed, from: usize) -> Vec<Value> {
    heard.lock().unwrap()[from..]
        .iter()
        .map(|(_, event)| event.clone())
        .collect()
}

fn about<'a>(events: &'a [Value], kind: &str, session: &str) -> Vec<&'a Value> {
    events
        .iter()
        .filter(|event| event["type"] == kind && event["session"] == session)
        .collect()
}

fn previews_of(heard: &Timed, from: usize, session: &str) -> Vec<(Instant, Value)> {
    heard.lock().unwrap()[from..]
        .iter()
        .filter(|(_, event)| {
            event["type"] == "agent-roster"
                && event["kind"] == "preview"
                && event["session"] == session
        })
        .cloned()
        .collect()
}

/// A chat's fields as the previews it was sent so far make them.
fn previewed(heard: &Timed, session: &str) -> serde_json::Map<String, Value> {
    let mut held = serde_json::Map::new();
    for (_, preview) in previews_of(heard, 0, session) {
        for (field, value) in preview["fields"].as_object().unwrap() {
            held.insert(field.clone(), value.clone());
        }
    }
    held
}

fn row_of(host: &Host, session: &str) -> Value {
    host.get("/agents/snapshot")["agents"]
        .as_array()
        .unwrap()
        .iter()
        .find(|row| row["session"] == session)
        .cloned()
        .unwrap()
}

/// The `live` frames a connection was sent of a chat it does not show whole: there are some, each
/// carries ops and every op is a `turn` op; and among them its turn running and its turn
/// settled. Answers the turn statuses, in order.
fn turn_frames_alone(events: &[Value], session: &str) -> Vec<String> {
    let frames = about(events, "live", session);
    assert!(!frames.is_empty(), "no live frame of {session}");
    let mut statuses = Vec::new();
    for frame in &frames {
        let ops = frame["ops"].as_array().unwrap();
        assert!(!ops.is_empty(), "a frame without ops: {frame}");
        for op in ops {
            assert_eq!(op["op"], "turn", "only turn ops: {frame}");
            statuses.push(op["turn"]["status"].as_str().unwrap_or("").to_string());
        }
    }
    assert!(
        statuses.iter().any(|status| status == "running"),
        "a running turn: {statuses:?}"
    );
    assert!(
        statuses.iter().any(|status| status == "completed"),
        "a settled turn: {statuses:?}"
    );
    statuses
}

/// The numbers of the events of these kinds about a chat a connection was sent.
fn numbered_of(events: &[Value], kinds: &[&str], session: &str) -> Vec<u64> {
    events
        .iter()
        .filter(|event| {
            kinds.contains(&event["type"].as_str().unwrap_or("")) && event["session"] == session
        })
        .filter_map(|event| event["event_id"].as_u64())
        .collect()
}

/// A set of chats as a `live-changed` and a change's answer say it: sorted, each once.
fn normalized(value: &Value) -> bool {
    let Some(names) = value.as_array() else {
        return false;
    };
    let names: Vec<&str> = names.iter().filter_map(Value::as_str).collect();
    let mut sorted = names.clone();
    sorted.sort_unstable();
    sorted.dedup();
    names == sorted && !names.iter().any(|name| name.trim().is_empty())
}

fn opened(heard: &Timed) -> bool {
    events(heard, 0)
        .iter()
        .any(|event| event["type"] == "agent-roster" && event.get("kind").is_none())
}

/// Issues 905 and 913: a stream opened with `chat_previews=1` sends the chats it does not show
/// whole as previews: no transcript, activity or state of theirs but a send's receipt and their
/// turn ops, and in their place, at most every five seconds, the list fields of their snapshot
/// rows, unnumbered. A chat both followed and on screen is sent everything; a stream without the
/// option is as before.
#[test]
fn a_stream_with_previews_sends_the_chats_it_does_not_show_whole_as_previews() {
    let place = Place::new("chat-previews");
    let host = place.start_with_token("the-owners-token");
    let features = host.get("/server-info")["capabilities"]["features"].clone();
    assert!(
        features
            .as_array()
            .unwrap()
            .contains(&json!("events_chat_previews")),
        "{features}"
    );
    let open = host.new_agent("Open");
    let listed = host.new_agent("Listed");
    let token = "token=the-owners-token";
    let previews = listen_timed(
        &host,
        &format!(
            "/events?{token}&live={open},{listed}&visible={open}&live_change=1&chat_previews=1&compact=1"
        ),
    );
    let plain = listen_timed(&host, &format!("/events?{token}&live={open}"));
    // A device paired with limited access is sent the same previews.
    let (limited, _) = pair(&host, "Watch", "limited");
    let watch = listen_timed(
        &host,
        &format!("/events?token={limited}&live={open}&visible={open}&chat_previews=1"),
    );
    wait_for("the streams to open", || {
        [&previews, &plain, &watch]
            .iter()
            .all(|heard| opened(heard))
    });
    let from = previews.lock().unwrap().len();

    host.send(&open, "for the open chat", "c-open");
    host.send(&listed, "for the listed chat", "c-listed-1");
    wait_for("both answers", || {
        [&open, &listed]
            .iter()
            .all(|agent| host.state(agent) == "done")
    });
    wait_for("the listed chat's preview with its answer", || {
        previewed(&previews, &listed)
            .get("latest_state")
            .is_some_and(|state| state == "done")
    });
    let heard = events(&previews, from);
    // The open chat comes whole.
    assert!(!about(&heard, "transcript-updated", &open).is_empty());
    assert!(
        about(&heard, "live", &open)
            .iter()
            .flat_map(|event| event["ops"].as_array().unwrap())
            .any(|op| op["op"] == "upsert")
    );
    // The listed one: none of its transcript, activity or states, but the receipt of the send.
    assert!(
        about(&heard, "transcript-updated", &listed).is_empty(),
        "{heard:#?}"
    );
    assert!(
        about(&heard, "agent-activity", &listed).is_empty(),
        "{heard:#?}"
    );
    let states = about(&heard, "agent-state", &listed);
    assert!(
        states
            .iter()
            .all(|state| state.get("client_msg_id").is_some()),
        "only the send's receipt: {states:#?}"
    );
    assert!(
        states
            .iter()
            .any(|state| state["client_msg_id"] == "c-listed-1"),
        "{states:#?}"
    );
    // Of its live events, its turn ops alone, though the stream follows it: a running turn and
    // a settled one, and nothing else in those frames.
    turn_frames_alone(&heard, &listed);
    // Previews have no number, and say what the snapshot's row says.
    for (_, preview) in previews_of(&previews, 0, &listed) {
        assert!(preview.get("event_id").is_none(), "{preview}");
        assert_eq!(preview["agent_id"], row_of(&host, &listed)["agent_id"]);
    }
    std::thread::sleep(Duration::from_secs(6));
    let row = row_of(&host, &listed);
    let held = previewed(&previews, &listed);
    for field in PREVIEW_FIELDS {
        assert_eq!(held.get(field), Some(&row[field]), "{field}: {row}");
    }
    assert!(
        held["last_message"]
            .as_str()
            .is_some_and(|text| !text.is_empty())
    );
    let limited_held = previewed(&watch, &listed);
    for field in PREVIEW_FIELDS {
        assert_eq!(
            limited_held.get(field),
            Some(&row[field]),
            "limited: {field}"
        );
    }
    // The stream that did not ask is sent everything as before, and no preview.
    let as_before = events(&plain, 0);
    assert!(!about(&as_before, "transcript-updated", &listed).is_empty());
    assert!(as_before.iter().all(|event| event["kind"] != "preview"));

    // Throttled: a chat that keeps changing is previewed at most every five seconds. Two sends
    // at once queue the second behind the first.
    let from = previews.lock().unwrap().len();
    host.send(&listed, "sleep 2", "c-listed-queued-1");
    host.send(&listed, "two", "c-listed-queued-2");
    for round in 0..4 {
        host.send(&listed, "again", &format!("c-listed-again-{round}"));
        std::thread::sleep(Duration::from_millis(1_500));
    }
    wait_for("the last answer", || host.state(&listed) == "done");
    std::thread::sleep(Duration::from_secs(6));
    let times: Vec<Instant> = previews_of(&previews, from, &listed)
        .into_iter()
        .map(|(at, _)| at)
        .collect();
    assert!(times.len() >= 2, "{} previews", times.len());
    for pair in times.windows(2) {
        let gap = pair[1] - pair[0];
        assert!(
            gap >= Duration::from_millis(4_500),
            "previews {gap:?} apart"
        );
    }
    let held = previewed(&previews, &listed);
    let row = row_of(&host, &listed);
    for field in PREVIEW_FIELDS {
        assert_eq!(held.get(field), Some(&row[field]), "after: {field}");
    }
    // Every send's receipt, and the queue's and delivery's events, reach the stream with previews
    // as they reach the stream without: the same numbered events, of the chat it previews.
    let everything = events(&previews, 0);
    let as_before = events(&plain, 0);
    for id in ["c-listed-1", "c-listed-queued-1", "c-listed-queued-2"] {
        let receipts = |events: &[Value]| {
            about(events, "agent-state", &listed)
                .iter()
                .filter(|state| state["client_msg_id"] == id)
                .count()
        };
        assert_eq!(receipts(&everything), 1, "{id}");
        assert_eq!(receipts(&as_before), 1, "{id}");
    }
    let kinds = ["queue-updated", "delivery-updated"];
    let kept = numbered_of(&everything, &kinds, &listed);
    assert_eq!(kept, numbered_of(&as_before, &kinds, &listed));
    assert!(
        !numbered_of(&everything, &["queue-updated"], &listed).is_empty(),
        "the queue was exercised: {kept:?}"
    );
}

/// The screen changes without reopening the stream: both sets take effect at one
/// `live-changed`, the answer says them, the newest of rapid changes wins, a chat that comes on
/// screen is sent whole from there and previewed no more, one that leaves it is previewed, and
/// an empty screen previews every chat. Only the opener's token changes it.
#[test]
fn a_change_of_screen_moves_chats_between_previews_and_whole_without_reopening() {
    let place = Place::new("chat-previews-moved");
    let host = place.start_with_token("the-owners-token");
    let first = host.new_agent("First");
    let second = host.new_agent("Second");
    let heard = listen_timed(
        &host,
        &format!(
            "/events?token=the-owners-token&live={first},{second}&visible={first}&live_change=1&chat_previews=1"
        ),
    );
    wait_for("the stream to open", || opened(&heard));
    let id = events(&heard, 0)[0]["stream_id"]
        .as_str()
        .unwrap()
        .to_string();
    let path = format!("/events/{id}/live");

    // Only its opener changes it, and only with lists.
    let (other, _) = pair(&host, "Other", "full");
    let body = json!({ "live": [first, second], "visible": [second] });
    let (status, _) = host.ask_as(&other, "POST", &path, Some(&body));
    assert_eq!(status, 404);
    // Whose stream it is comes first: another token is told no such stream, whatever its
    // `visible` says.
    for visible in [json!("nope"), Value::Null] {
        let (status, refused) = host.ask_as(
            &other,
            "POST",
            &path,
            Some(&json!({ "live": [first], "visible": visible })),
        );
        assert_eq!(
            (status, &refused["error"]),
            (404, &json!("stream not found"))
        );
    }
    let (status, refused) = host.ask(
        "POST",
        &path,
        Some(&json!({ "live": [first], "visible": "nope" })),
    );
    assert_eq!(
        (status, &refused["error"]),
        (
            400,
            &json!("visible must be a list of sessions or agent ids")
        )
    );
    // `null` is no list either: only a body without `visible` keeps the screen.
    let (status, refused) = host.ask(
        "POST",
        &path,
        Some(&json!({ "live": [first], "visible": null })),
    );
    assert_eq!(
        (status, &refused["error"]),
        (
            400,
            &json!("visible must be a list of sessions or agent ids")
        )
    );
    // The refused changes changed nothing: no acknowledgement, and the screen is the opening's.
    std::thread::sleep(Duration::from_millis(300));
    assert!(
        events(&heard, 0)
            .iter()
            .all(|event| event["type"] != "live-changed"),
        "a refused change is not taken"
    );
    let kept = host.post(&path, json!({ "live": [first, second] }));
    assert_eq!(kept["visible"], json!([first]), "{kept}");

    // Rapid changes: the answers say each, the stream takes the newest.
    let from = heard.lock().unwrap().len();
    let mut both = [first.clone(), second.clone()];
    both.sort();
    let answered = host.post(&path, json!({ "live": [first], "visible": [] }));
    assert_eq!(answered["visible"], json!([]));
    let answered = host.post(
        &path,
        json!({ "live": [second, " ", first, second], "visible": [second, second, ""] }),
    );
    assert_eq!(
        answered,
        json!({ "ok": true, "stream_id": id, "live": both, "visible": [second] })
    );
    wait_for("the change to take effect", || {
        events(&heard, from)
            .iter()
            .any(|event| event["type"] == "live-changed" && event["visible"] == json!([second]))
    });
    let said: Vec<Value> = events(&heard, from)
        .into_iter()
        .filter(|event| event["type"] == "live-changed")
        .collect();
    let last = said.last().unwrap();
    assert_eq!(last["live"], json!(both));
    assert!(last.get("event_id").is_none());
    let boundary = heard
        .lock()
        .unwrap()
        .iter()
        .rposition(|(_, event)| event["type"] == "live-changed")
        .unwrap()
        + 1;

    // From there the second chat comes whole, and the first, followed but off screen, as
    // previews and turn ops.
    host.send(&second, "now on screen", "c-2");
    host.send(&first, "now off screen", "c-1");
    wait_for("both answers", || {
        [&first, &second]
            .iter()
            .all(|agent| host.state(agent) == "done")
    });
    wait_for("the first chat's preview", || {
        previews_of(&heard, boundary, &first)
            .iter()
            .any(|(_, preview)| preview["fields"]["latest_state"] == "done")
    });
    let after = events(&heard, boundary);
    assert!(!about(&after, "transcript-updated", &second).is_empty());
    assert!(
        about(&after, "live", &second)
            .iter()
            .flat_map(|event| event["ops"].as_array().unwrap())
            .any(|op| op["op"] == "upsert")
    );
    assert!(
        previews_of(&heard, boundary, &second).is_empty(),
        "{after:#?}"
    );
    assert!(about(&after, "transcript-updated", &first).is_empty());
    // The first, followed and off screen: a running and a settled turn op, nothing else.
    turn_frames_alone(&after, &first);
    assert!(
        about(&after, "agent-state", &first)
            .iter()
            .any(|state| state["client_msg_id"] == "c-1"),
        "the send's receipt"
    );

    // The screen cleared while the stream still follows both: its acknowledgement says both sets,
    // and is where every chat starts being previewed.
    let from = heard.lock().unwrap().len();
    let cleared = host.post(&path, json!({ "live": both, "visible": [] }));
    assert_eq!(
        cleared,
        json!({ "ok": true, "stream_id": id, "live": both, "visible": [] })
    );
    wait_for("the cleared screen's acknowledgement", || {
        events(&heard, from).iter().any(|event| {
            event["type"] == "live-changed"
                && event["visible"] == json!([])
                && event["live"] == json!(both)
        })
    });
    let boundary = from
        + events(&heard, from)
            .iter()
            .position(|event| {
                event["type"] == "live-changed"
                    && event["visible"] == json!([])
                    && event["live"] == json!(both)
            })
            .unwrap()
        + 1;
    // A change that names no screen keeps it.
    let kept = host.post(&path, json!({ "live": both }));
    assert_eq!(kept["visible"], json!([]));
    host.send(&second, "off screen now", "c-3");
    wait_for("its answer", || host.state(&second) == "done");
    wait_for("the second chat's preview", || {
        previews_of(&heard, boundary, &second)
            .iter()
            .any(|(_, preview)| preview["fields"]["latest_state"] == "done")
    });
    let after = events(&heard, boundary);
    assert!(
        about(&after, "transcript-updated", &second).is_empty(),
        "{after:#?}"
    );
    assert!(about(&after, "agent-activity", &second).is_empty());
    turn_frames_alone(&after, &second);
    // The first preview after it left the screen says all of it.
    let (_, first_after) = previews_of(&heard, boundary, &second).remove(0);
    for field in PREVIEW_FIELDS {
        assert!(
            first_after["fields"].get(field).is_some(),
            "{field}: {first_after}"
        );
    }
    // Every acknowledgement this stream sent carries both sets, normalized.
    let acks: Vec<Value> = events(&heard, 0)
        .into_iter()
        .filter(|event| event["type"] == "live-changed")
        .collect();
    assert!(acks.len() >= 3, "{acks:#?}");
    for ack in &acks {
        assert!(normalized(&ack["live"]), "{ack}");
        assert!(normalized(&ack["visible"]), "{ack}");
        assert!(ack.get("event_id").is_none(), "{ack}");
    }
}

/// A reconnect of a stream with previews is replayed nothing a preview stands for, keeps the
/// numbered events it is given in order, and ends with the plain `agent-roster` on which the
/// app reads the snapshot; a stream without previews is replayed as before.
#[test]
fn a_reconnect_with_previews_is_replayed_nothing_a_preview_stands_for() {
    let place = Place::new("chat-previews-resume");
    let host = place.start();
    let open = host.new_agent("Open");
    let listed = host.new_agent("Listed");
    let first = host.listen();
    // Watches every chat whole, live ops too: whether the listed chat changed at all.
    let watcher = listen_timed(&host, "/events?live=*");
    wait_for("the streams to open", || {
        first
            .lock()
            .unwrap()
            .iter()
            .any(|event| event["type"] == "agent-roster")
            && opened(&watcher)
    });
    let cursor = first
        .lock()
        .unwrap()
        .iter()
        .filter_map(|event| event["event_id"].as_u64())
        .max()
        .unwrap_or(0);
    host.send(&listed, "while away", "c-away");
    host.send(&open, "while away too", "c-away-2");
    wait_for("both answers", || {
        [&open, &listed]
            .iter()
            .all(|agent| host.state(agent) == "done")
    });
    // When the watcher last heard anything of the listed chat.
    let last_heard = || {
        watcher
            .lock()
            .unwrap()
            .iter()
            .filter(|(_, event)| event["session"] == listed)
            .map(|(at, _)| *at)
            .max()
    };
    for compact in ["", "&compact=1"] {
        // The listed chat at rest: nothing of it heard for two seconds.
        wait_for("the listed chat to be quiet", || {
            last_heard().is_none_or(|at| at.elapsed() >= Duration::from_secs(2))
        });
        let quiet_since = last_heard();
        let resumed = listen_timed(
            &host,
            &format!(
                "/events?last_event_id={cursor}&live={open}&visible={open}&chat_previews=1{compact}"
            ),
        );
        wait_for("the replay", || opened(&resumed));
        std::thread::sleep(Duration::from_millis(500));
        let replay = events(&resumed, 0);
        let end = replay
            .iter()
            .position(|event| {
                event["type"] == "agent-roster"
                    && event.get("kind").is_none()
                    && event.get("resync").is_none()
            })
            .unwrap();
        let replay = &replay[..end];
        assert!(
            about(replay, "transcript-updated", &listed).is_empty(),
            "{compact}: {replay:#?}"
        );
        assert!(
            about(replay, "agent-activity", &listed).is_empty(),
            "{compact}"
        );
        assert!(
            !about(replay, "transcript-updated", &open).is_empty(),
            "{compact}"
        );
        let receipts = about(replay, "agent-state", &listed)
            .iter()
            .filter(|state| state["client_msg_id"] == "c-away")
            .count();
        assert_eq!(receipts, 1, "{compact}: the receipt is replayed once");
        // A replayed receipt makes no preview: the snapshot read after the replay says the chat.
        // The watcher shows the chat did not change meanwhile, so a preview could only come from
        // the replay.
        std::thread::sleep(Duration::from_millis(1_000));
        assert_eq!(
            last_heard(),
            quiet_since,
            "{compact}: the listed chat changed during the check: {:#?}",
            events(&watcher, 0)
        );
        assert!(
            previews_of(&resumed, 0, &listed).is_empty(),
            "{compact}: no preview of what was replayed: {:#?}",
            events(&resumed, 0)
        );
        let numbers: Vec<u64> = replay
            .iter()
            .filter_map(|event| event["event_id"].as_u64())
            .collect();
        assert!(
            numbers.windows(2).all(|pair| pair[0] < pair[1]),
            "{numbers:?}"
        );
        assert!(replay.iter().all(|event| event["kind"] != "preview"));
        if !compact.is_empty() {
            assert_eq!(replay[0]["resync"], true, "{replay:#?}");
            assert!(replay[0]["skipped"].as_u64().unwrap() > 0);
        }
    }
    let as_before = host.listen_at(&format!("/events?last_event_id={cursor}"));
    wait_for("the plain replay", || {
        as_before
            .lock()
            .unwrap()
            .iter()
            .any(|event| event["type"] == "transcript-updated" && event["session"] == listed)
    });
}

/// Previews change what a stream is sent, never which chats count as open on the owner's device
/// for their agents' lanes: those are the chats it follows (`live`), named as before, whatever
/// `visible` says, at the opening and at each `live-changed`. `*` marks no agent.
#[test]
fn previews_leave_the_owners_lane_marks_to_what_the_stream_follows() {
    let place = Place::new("chat-previews-marks");
    let host = place.start_with_token("the-owners-token");
    let first = host.new_agent("First");
    let second = host.new_agent("Second");
    let id_of = |session: &str| {
        row_of(&host, session)["agent_id"]
            .as_str()
            .unwrap()
            .to_string()
    };
    let run = place.root.join("run");
    let mark = |agent: &str| run.join(format!("{agent}-owner-view.mark"));
    let (first_id, second_id) = (id_of(&first), id_of(&second));
    let clear = || {
        for agent in [&first_id, &second_id] {
            let _ = std::fs::remove_file(mark(agent));
        }
    };
    clear();
    // Every chat followed and on screen by `*`: no agent is marked, as `live=*` never was.
    let every = listen_timed(
        &host,
        "/events?token=the-owners-token&live=*&visible=*&live_change=1&chat_previews=1",
    );
    wait_for("the stream to open", || opened(&every));
    std::thread::sleep(Duration::from_millis(1_500));
    assert!(!mark(&first_id).exists() && !mark(&second_id).exists());
    // Both followed and none on screen: both are marked, as a stream without previews marks them.
    let heard = listen_timed(
        &host,
        &format!(
            "/events?token=the-owners-token&live={first},{second}&visible=&live_change=1&chat_previews=1"
        ),
    );
    wait_for("the stream to open", || opened(&heard));
    wait_for("both followed chats marked", || {
        mark(&first_id).exists() && mark(&second_id).exists()
    });
    // At a change, what it follows from then on: the first, though nothing is on screen.
    let stream = events(&heard, 0)[0]["stream_id"]
        .as_str()
        .unwrap()
        .to_string();
    clear();
    host.post(
        &format!("/events/{stream}/live"),
        json!({ "live": [first], "visible": [] }),
    );
    wait_for("the change", || {
        events(&heard, 0)
            .iter()
            .any(|event| event["type"] == "live-changed" && event["live"] == json!([first]))
    });
    wait_for("the followed chat marked", || mark(&first_id).exists());
    std::thread::sleep(Duration::from_millis(1_000));
    assert!(!mark(&second_id).exists(), "no longer followed, not marked");
}

/// The recipe: previews take effect only on a stream that names `live` when it opens (`live=`
/// empty: the list alone). A stream opened without `live` stays an ordinary #892 stream: its
/// changes carry `live` alone, it ignores `visible` (well formed, `null` or malformed), and it
/// is sent every chat's numbered events and no preview, also after a change gives it chats.
#[test]
fn previews_need_live_at_the_opening_and_an_ordinary_stream_ignores_visible() {
    let place = Place::new("chat-previews-recipe");
    let host = place.start_with_token("the-owners-token");
    let listed = host.new_agent("Listed");
    let token = "token=the-owners-token";
    // Asked for previews, but named no `live`: ordinary.
    let unnamed = listen_timed(
        &host,
        &format!("/events?{token}&live_change=1&chat_previews=1&visible={listed}"),
    );
    // Ordinary from the start.
    let ordinary = listen_timed(
        &host,
        &format!("/events?{token}&live_change=1&live={listed}"),
    );
    // The list alone: previews of every chat.
    let list = listen_timed(
        &host,
        &format!("/events?{token}&live_change=1&chat_previews=1&live="),
    );
    wait_for("the streams to open", || {
        [&unnamed, &ordinary, &list]
            .iter()
            .all(|heard| opened(heard))
    });
    let id = |heard: &Timed| {
        events(heard, 0)[0]["stream_id"]
            .as_str()
            .unwrap()
            .to_string()
    };
    let (unnamed_id, ordinary_id, list_id) = (id(&unnamed), id(&ordinary), id(&list));

    // An ordinary stream ignores `visible` whatever it is, and answers and acknowledges `live`
    // alone, as #892 does.
    for (stream, heard, live, visible) in [
        (&unnamed_id, &unnamed, json!([listed]), Value::Null),
        (&unnamed_id, &unnamed, json!([]), json!("nope")),
        (&ordinary_id, &ordinary, json!([listed]), json!([listed])),
        (
            &ordinary_id,
            &ordinary,
            json!([listed]),
            json!({"not": "a list"}),
        ),
    ] {
        let from = heard.lock().unwrap().len();
        let answered = host.post(
            &format!("/events/{stream}/live"),
            json!({ "live": live, "visible": visible }),
        );
        assert_eq!(
            answered,
            json!({ "ok": true, "stream_id": stream, "live": live }),
            "{visible}"
        );
        wait_for("its acknowledgement", || {
            events(heard, from)
                .iter()
                .any(|event| event["type"] == "live-changed")
        });
        let ack = events(heard, from)
            .into_iter()
            .find(|event| event["type"] == "live-changed")
            .unwrap();
        assert_eq!(
            ack,
            json!({ "type": "live-changed", "live": live }),
            "{visible}"
        );
    }
    // A preview stream answers with both sets; its list-only screen is empty.
    let answered = host.post(&format!("/events/{list_id}/live"), json!({ "live": [] }));
    assert_eq!(
        answered,
        json!({ "ok": true, "stream_id": list_id, "live": [], "visible": [] })
    );

    // Every chat's changes reach the ordinary streams, numbered, with no preview; the list-only
    // stream is sent the chat's preview in their place.
    let marks = [&unnamed, &ordinary, &list].map(|heard| heard.lock().unwrap().len());
    host.send(&listed, "hello", "c-recipe");
    wait_for("its answer", || host.state(&listed) == "done");
    wait_for("the list's preview", || {
        previews_of(&list, marks[2], &listed)
            .iter()
            .any(|(_, preview)| preview["fields"]["latest_state"] == "done")
    });
    for (heard, from) in [(&unnamed, marks[0]), (&ordinary, marks[1])] {
        wait_for("the transcript's change", || {
            about(&events(heard, from), "transcript-updated", &listed)
                .iter()
                .any(|event| event["event_id"].as_u64().is_some())
        });
        assert!(
            events(heard, 0)
                .iter()
                .all(|event| event["kind"] != "preview"),
            "an ordinary stream is sent no preview"
        );
    }
    let listed_events = events(&list, marks[2]);
    assert!(about(&listed_events, "transcript-updated", &listed).is_empty());
    assert!(about(&listed_events, "agent-activity", &listed).is_empty());
}
