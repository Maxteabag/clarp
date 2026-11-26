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
        206
    );
    // The meaning below is contract 213's: no feature of its own.
    let contract = host.get("/server-info")["contract"]["host"].as_u64();
    assert!(contract >= Some(213), "{contract:?}");
    let heard = host.listen();
    let session = host.new_agent("Avail");
    // A new agent is never shown as restarting while it is being opened.
    let idle = availability_of(&host, &session);
    assert_ne!(idle["state"], "restarting", "{idle}");
    wait_for("the new agent to be opened", || {
        availability_of(&host, &session)["state"] == "ok"
    });
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

    // Ordinary healthy work keeps nobody waiting. The stand-in's own turn takes no words, so
    // the owner's send as his apps make it (`while_busy` from the Host's setting, `steer` by
    // default) stops it and runs now: nothing replaces the preview.
    host.send(&session, "sleep 60", "long");
    wait_for("the turn to run", || host.state(&session) == "thinking");
    let counted = || {
        heard
            .lock()
            .unwrap()
            .iter()
            .filter(|event| event["kind"] == "availability" && event["session"] == session.as_str())
            .count()
    };
    std::thread::sleep(Duration::from_secs(1));
    let ordinary = json!(["ok", "", null, 0]);
    let working = availability_of(&host, &session);
    assert_eq!(shown(&working), ordinary, "ordinary work: {working}");
    // Under `after_turn` his send waits for the turn because he chose that order: still
    // ordinary work, not a reason the agent cannot answer.
    host.post("/send-settings", json!({ "while_busy": "after_turn" }));
    std::thread::sleep(Duration::from_secs(1));
    assert_eq!(shown(&availability_of(&host, &session)), ordinary);
    host.post("/send-settings", json!({ "while_busy": "steer" }));
    assert_eq!(counted(), 0, "nothing is told of ordinary work");
    // A plain `POST /send` without `while_busy` keeps its admission: behind his own turn whose
    // tool takes no words it queues, as always. The row describes his Send in an app, not this.
    let plain = host.send(&session, "plain", "plain");
    assert_eq!(plain["queued"], true, "{plain}");
    assert!(plain.get("while_busy").is_none(), "{plain}");
    std::thread::sleep(Duration::from_millis(500));
    assert_eq!(host.state(&session), "thinking", "nothing was cut");
    assert_eq!(shown(&availability_of(&host, &session)), ordinary);
    // What the row said is what his send does: it takes the turn's place at once.
    let steer = host.post(
        "/send",
        json!({ "session": session, "text": "now", "client_msg_id": "now",
                "while_busy": "steer" }),
    );
    assert_eq!(steer["while_busy"], "take_over", "{steer}");
    wait_for("his send to run", || {
        host.conversation(&session)
            .iter()
            .any(|(role, text)| role == "user" && text == "now")
    });
    assert_eq!(shown(&availability_of(&host, &session)), ordinary);
    host.post("/stop", json!({ "session": session }));
}

/// A turn the owner can neither steer into nor take over (the tool's compaction) keeps his
/// message waiting: the row says why, and how many truly run before a new one; the event an
/// app patches its row with says the same as a fresh read of the row.
#[test]
fn a_turn_that_cannot_take_the_owners_word_says_why_and_what_runs_before_it() {
    let place = Place::new("availability-not-cuttable");
    let claude = place.root.join("claude");
    program(&claude, super::compaction_outcome::CLAUDE);
    std::fs::write(place.root.join("modes"), r#"["stall"]"#).unwrap();
    let home = place.root.join("home");
    let world = [
        ("HOME", home.to_str().unwrap()),
        ("CLARP_CLAUDE_PROGRAM", claude.to_str().unwrap()),
    ];
    let host = place.start_in("", &[], &world);
    let made = host.post("/agents", json!({ "name": "Shorty", "backend": "claude" }));
    let session = made["session"].as_str().unwrap().to_string();
    host.send(&session, "first turn", "first");
    wait_for("the first turn to establish a session", || {
        host.state(&session) == "done"
    });
    let heard = host.listen();
    host.post("/compact", json!({ "session": session }));
    let compacting = json!(["busy", "Compacting", null, 0]);
    wait_for("the compaction to keep the owner waiting", || {
        shown(&availability_of(&host, &session)) == compacting
    });
    let same_as_the_row = |expected: &Value| {
        wait_for("the event to say what the row says", || {
            told(&heard, &session).is_some_and(|event| {
                let told = &event["fields"]["availability"];
                let row = availability_of(&host, &session);
                shown(told) == *expected
                    && shown(&row) == *expected
                    && told["since_ms"] == row["since_ms"]
            })
        });
    };
    same_as_the_row(&compacting);
    // The owner's own message waits for it and runs first: one ahead of his next.
    host.post(
        "/send",
        json!({ "session": session, "text": "after it", "client_msg_id": "mine",
                "while_busy": "steer" }),
    );
    let one = json!(["busy", "Compacting · 1 message ahead of you", null, 1]);
    same_as_the_row(&one);
    let snapshot = host.get("/agents/snapshot");
    let row = snapshot["agents"]
        .as_array()
        .unwrap()
        .iter()
        .find(|row| row["session"] == session.as_str())
        .unwrap();
    assert_eq!(shown(&row["availability"]), one, "{row}");
    // The setting alone moves the row and its event: under `after_turn` his message waits for
    // the compaction by his own choice (ok), under `steer` again because it cannot be cut. The
    // compaction runs on and his message waits throughout: admission is untouched.
    host.post("/send-settings", json!({ "while_busy": "after_turn" }));
    same_as_the_row(&json!(["ok", "", null, 0]));
    host.post("/send-settings", json!({ "while_busy": "steer" }));
    same_as_the_row(&one);
    let snapshot = host.get("/agents/snapshot");
    let row = snapshot["agents"]
        .as_array()
        .unwrap()
        .iter()
        .find(|row| row["session"] == session.as_str())
        .unwrap();
    assert_eq!(row["compacting"], true, "the compaction was not cut: {row}");
    assert_eq!(
        row["queued_turn_count"], 1,
        "his message still waits: {row}"
    );
    // Automation left for later waits behind his word: it is in the queue, not ahead of him.
    let later = host.post(
        "/send",
        json!({ "session": session, "text": "routine", "client_msg_id": "routine",
                "queue_if_busy": true, "origin": "automation" }),
    );
    assert_eq!(later["queued"], true, "{later}");
    wait_for("both to wait", || {
        host.get(&format!("/agents/{session}"))["queued_turn_count"] == 2
    });
    std::thread::sleep(Duration::from_millis(500));
    assert_eq!(shown(&availability_of(&host, &session)), one);
    host.post("/stop", json!({ "session": session }));
}

#[test]
fn an_event_stream_is_told_the_host_is_updating_before_it_stops() {
    use nix::sys::signal::{Signal, kill};
    use nix::unistd::Pid;
    let place = Place::new("host-updating");
    let mut host = place.start();
    let heard = host.listen();
    // A stream with chat previews (feature `events_chat_previews`) is told the same last word.
    let previewing = host.listen_at("/events?live_change=1&chat_previews=1&compact=1&gzip=0");
    wait_for("the streams to begin", || {
        [&heard, &previewing].iter().all(|heard| {
            heard
                .lock()
                .unwrap()
                .iter()
                .any(|event| event["type"] == "agent-roster")
        })
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
    wait_for("the previewing stream's last word", || {
        previewing
            .lock()
            .unwrap()
            .last()
            .is_some_and(|event| event["type"] == "host-updating")
    });
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

#[test]
fn an_initial_launch_waits_for_an_account_without_guessing_a_switch() {
    let place = Place::new("availability-account-hold");
    let selector = place.root.join("selector");
    program(
        &selector,
        "#!/bin/sh\ncat >/dev/null\nprintf '%s' '{\"available\":false}'\n",
    );
    let seats = seat_tool(&place);
    std::fs::write(
        place.root.join("list.said"),
        r#"{"accounts":[{"alias":"current","is_active":true},{"alias":"spare"}]}"#,
    )
    .unwrap();
    std::fs::write(place.root.join("switch.said"), r#"{"switched":true}"#).unwrap();

    let owners = place.root.join("owners.toml");
    std::fs::write(
        &owners,
        format!(
            "[agents]\nclaude_account_switch_command = [\"{}\"]\n",
            selector.display()
        ),
    )
    .unwrap();
    let backend = place.root.join("backend");
    program(
        &backend,
        &format!(
            "#!/bin/sh\nread message\nroot=\"$(dirname \"$0\")\"\nprintf '%s\\n' launch >> \"$root/launched\"\n\
             if [ ! -f \"$root/ready\" ]; then echo 'Not logged in. Please run /login' >&2; exit 1; fi\n\
             {STAND_IN_ANSWERS}"
        ),
    );
    let host = place.start_in(
        "",
        &[
            "--accounts",
            "select",
            "--seats-tool",
            seats.to_str().unwrap(),
            "--speech-keys",
            owners.to_str().unwrap(),
        ],
        &[
            ("CLARP_CLAUDE_PROGRAM", backend.to_str().unwrap()),
            ("CLARP_CODEX_PROGRAM", "/bin/false"),
        ],
    );
    let create = |name: &str| {
        host.post("/agents", json!({"name":name, "backend":"claude"}))["session"]
            .as_str()
            .unwrap()
            .to_owned()
    };
    let trigger = create("Trigger");
    let held = create("Held");
    let path = format!("/agents/{held}");
    let heard = host.listen();
    host.send(&trigger, "start account recovery", "trigger-recovery");
    wait_for("the selector to report that no account serves", || {
        host.heard.lock().unwrap().contains("no account serves")
    });

    // The recovery is still open. A different agent's first owner turn is accepted but
    // has never started its backend and therefore has no failure or reset of its own.
    host.send(&held, "first turn", "first-held");
    wait_for("the initial launch to wait for an account", || {
        host.get(&path)["live_activity"]["waits_for"] == "quota"
    });
    let snapshot = host.get("/agents/snapshot");
    let row = snapshot["agents"]
        .as_array()
        .unwrap()
        .iter()
        .find(|row| row["session"] == held.as_str())
        .unwrap();
    assert_eq!(row["live_activity"]["state"], "limited", "{row}");
    assert!(row["backend_quota"].is_null(), "{row}");
    assert_eq!(
        std::fs::read_to_string(place.root.join("launched")).unwrap(),
        "launch\n",
        "only the recovery-triggering turn reached the backend"
    );
    let expected = json!(["usage_limit", "Waiting for account", null, 0]);
    assert_eq!(shown(&row["availability"]), expected, "{row}");
    wait_for("the held launch's roster update", || {
        told(&heard, &held).is_some_and(|event| {
            event["agent_id"] == row["agent_id"]
                && event["fields"]["availability"] == row["availability"]
        })
    });

    // A confirmed switch releases the held launch via the ordinary pool path.
    std::fs::write(place.root.join("ready"), "").unwrap();
    let (status, switched) = host.ask(
        "POST",
        "/hotseat/switch",
        Some(&json!({"provider":"claude","alias":"spare"})),
    );
    assert_eq!(
        (status, &switched["switched"]),
        (200, &json!(true)),
        "{switched}"
    );
    wait_for(
        "the held agent to bind and answer its original turn",
        || {
            let row = host.get(&path);
            row["backend_session_id"] == "stand-in"
                && row["availability"]["state"] == "ok"
                && host
                    .conversation(&held)
                    .iter()
                    .any(|(role, text)| role == "assistant" && text == "on it")
        },
    );
    let cleared = json!(["ok", "", null, 0]);
    assert_eq!(shown(&availability_of(&host, &held)), cleared);
    wait_for("the cleared availability roster update", || {
        told(&heard, &held).is_some_and(|event| shown(&event["fields"]["availability"]) == cleared)
    });
}
