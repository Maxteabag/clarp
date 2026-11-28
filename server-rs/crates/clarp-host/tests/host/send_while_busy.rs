//! What the owner's Send does while the agent works (issue 920, feature `send_while_busy`): one
//! setting of the Host's for every app, and a send's `while_busy`: read into the running turn,
//! wait for it, or stop it and run next; where the turn cannot take words at all, `steer` stops
//! it instead, and what is no words waits. One file a piece, declared in `host.rs`.

use super::*;

use super::queue_steer::CLAUDE;

fn rows_saying(host: &Host, session: &str, role: &str, text: &str) -> usize {
    host.turns(session)
        .iter()
        .filter(|row| row["role"] == role && row["text"] == text)
        .count()
}

fn waiting(host: &Host, session: &str, id: &str) -> bool {
    host.get(&format!("/turn-queue?session={session}"))["items"]
        .as_array()
        .unwrap()
        .iter()
        .any(|item| item["id"] == id)
}

fn send_while_busy(host: &Host, session: &str, text: &str, id: &str, mode: &str) -> Value {
    host.post(
        "/send",
        json!({ "session": session, "text": text, "client_msg_id": id, "while_busy": mode }),
    )
}

/// Claude Code's stand-in, on a Host of its own, with one agent of it.
pub(super) fn claude_place(name: &str) -> (Place, Host, String) {
    let place = Place::new(name);
    let claude = place.root.join("claude");
    program(&claude, CLAUDE);
    let home = place.root.join("home");
    let world = [
        ("HOME", home.to_str().unwrap()),
        ("CLARP_CLAUDE_PROGRAM", claude.to_str().unwrap()),
    ];
    let host = place.start_in("", &[], &world);
    let made = host.post("/agents", json!({ "name": "Reader", "backend": "claude" }));
    let session = made["session"].as_str().unwrap().to_string();
    (place, host, session)
}

#[test]
fn the_owner_sets_what_send_does_while_the_agent_works_and_every_app_is_told() {
    let place = Place::new("send-settings");
    let host = place.start();
    assert_eq!(
        host.get("/server-info")["contract"]["features"]["send_while_busy"],
        208
    );
    assert_eq!(host.get("/send-settings"), json!({"while_busy": "steer"}));
    let events = host.listen();
    wait_for("the event stream", || {
        events
            .lock()
            .unwrap()
            .iter()
            .any(|event| event["type"] == "agent-roster")
    });
    let changed = host.post("/send-settings", json!({"while_busy": "after_turn"}));
    assert_eq!(changed, json!({"while_busy": "after_turn"}));
    assert_eq!(
        host.get("/send-settings"),
        json!({"while_busy": "after_turn"})
    );
    wait_for("the change told to the apps", || {
        events.lock().unwrap().iter().any(|event| {
            event["type"] == "send-settings-updated" && event["while_busy"] == "after_turn"
        })
    });
    // A value that is none of the three, or none at all, changes nothing.
    for body in [
        json!({"while_busy": "later"}),
        json!({"while_busy": 1}),
        json!({}),
    ] {
        let (status, refused) = host.ask("POST", "/send-settings", Some(&body));
        assert_eq!(status, 400, "{body}: {refused}");
    }
    assert_eq!(
        host.get("/send-settings"),
        json!({"while_busy": "after_turn"})
    );
    // A request from an agent's own turn is refused, as for the owner's other settings.
    let body = json!({"while_busy": "take_over"}).to_string();
    let headers = [("X-Clarp-Turn", "a-turn")];
    let answered = fetch(
        &host,
        &host.token,
        "POST",
        "/send-settings",
        &headers,
        body.as_bytes(),
    );
    assert_eq!(answered.status, 403);
    assert_eq!(
        host.get("/send-settings"),
        json!({"while_busy": "after_turn"})
    );
    let changed = host.post("/send-settings", json!({"while_busy": "take_over"}));
    assert_eq!(changed["while_busy"], "take_over");
    // A send names a value of the three, or none.
    let session = host.new_agent("Sender");
    let (status, refused) = host.ask(
        "POST",
        "/send",
        Some(&json!({"session": session, "text": "hi", "while_busy": "soon"})),
    );
    assert_eq!(status, 400, "{refused}");
}

#[test]
fn a_busy_send_waits_or_takes_over_as_it_asks_and_without_asking_waits_as_always() {
    let place = Place::new("send-while-busy");
    let host = place.start();
    let session = host.new_agent("Busy");
    host.send(&session, "sleep 60", "long");
    wait_for("the turn to run", || host.state(&session) == "thinking");

    // Without `while_busy`, as always: it waits, and the answer says nothing of it.
    let plain = host.send(&session, "plain", "plain");
    assert_eq!(plain["queued"], true, "{plain}");
    assert!(plain.get("while_busy").is_none(), "{plain}");
    assert!(plain.get("fallback").is_none(), "{plain}");
    // Another sender's send never preempts, whatever it names.
    let scheduled = host.post(
        "/send",
        json!({ "session": session, "text": "scheduled", "client_msg_id": "sched",
                "origin": "schedule", "while_busy": "take_over" }),
    );
    assert_eq!(scheduled["queued"], true, "{scheduled}");
    assert!(scheduled.get("while_busy").is_none(), "{scheduled}");
    // After the turn: it waits.
    let after = send_while_busy(&host, &session, "after", "after", "after_turn");
    assert_eq!(after["queued"], true, "{after}");
    assert_eq!(after["while_busy"], "after_turn", "{after}");
    assert!(after.get("fallback").is_none(), "{after}");
    assert_eq!(host.state(&session), "thinking", "nothing was cut");
    assert!(waiting(&host, &session, "plain") && waiting(&host, &session, "after"));

    // Steer, on a tool that takes no words during a turn at all: the turn is stopped and the
    // send runs next, and the answer says so with the chip's line.
    let steer = send_while_busy(&host, &session, "steered", "steered", "steer");
    assert_eq!(steer["while_busy"], "take_over", "{steer}");
    assert_eq!(
        steer["fallback"],
        json!({"from": "steer", "to": "take_over", "reason": "not_steerable",
               "text": "This agent can't take a message mid-turn: stopped and sent"}),
        "{steer}"
    );
    // The cut turn ends long before its sixty seconds (a wait gives up after thirty), and the
    // send runs before what waited. The user's waiting messages may be read together into one
    // turn, so what is looked at is the order of their rows.
    wait_for("every waiting send run", || {
        ["steered", "plain", "after", "scheduled"]
            .iter()
            .all(|text| rows_saying(&host, &session, "user", text) == 1)
    });
    let rows = host.turns(&session);
    let at = |text: &str| {
        rows.iter()
            .position(|row| row["role"] == "user" && row["text"] == text)
            .unwrap_or_else(|| panic!("{text}: {rows:?}"))
    };
    assert!(at("steered") < at("plain"), "{rows:?}");
    assert!(at("plain") < at("after"), "{rows:?}");
    wait_for("the agent at rest", || host.state(&session) == "done");
    let queue = host.get(&format!("/turn-queue?session={session}"));
    assert_eq!(queue["paused"], false, "a take-over holds nothing: {queue}");

    // Take over: the running turn stops now and the send runs next.
    host.send(&session, "sleep 60", "long-again");
    wait_for("the turn to run again", || {
        host.state(&session) == "thinking"
    });
    let taken = send_while_busy(&host, &session, "taken", "taken", "take_over");
    assert_eq!(taken["while_busy"], "take_over", "{taken}");
    assert!(taken.get("fallback").is_none(), "{taken}");
    wait_for("the taking send's answer", || {
        rows_saying(&host, &session, "assistant", "taken") == 1
    });
    wait_for("the agent at rest again", || host.state(&session) == "done");
    assert_eq!(rows_saying(&host, &session, "user", "taken"), 1);

    // An idle agent: the send starts at once, and nothing is said of a busy agent.
    let idle = send_while_busy(&host, &session, "idle", "idle", "take_over");
    assert_eq!(idle["queued"], false, "{idle}");
    assert!(idle.get("while_busy").is_none(), "{idle}");
}

#[test]
fn a_steer_the_turn_cannot_take_yet_waits_and_is_read_in_once_it_can() {
    let (place, host, session) = claude_place("steer-not-yet");
    host.send(&session, "work", "work");
    wait_for("the turn's tool to start", || {
        place.root.join("started").exists()
    });
    // Its tool has not said it is at work: it takes nothing yet. The send waits, and says it
    // is to be read in.
    let sent = send_while_busy(&host, &session, "more", "more", "steer");
    assert_eq!(sent["queued"], true, "{sent}");
    assert_eq!(sent["while_busy"], "steer", "{sent}");
    assert!(sent.get("fallback").is_none(), "{sent}");
    assert!(waiting(&host, &session, "more"));
    // Once at work the turn takes it, with no one asking again.
    std::fs::write(place.root.join("go"), "").unwrap();
    wait_for("the answer to both", || {
        rows_saying(&host, &session, "assistant", "answered both") == 1
    });
    let more = host
        .turns(&session)
        .into_iter()
        .find(|row| row["role"] == "user" && row["text"] == "more")
        .unwrap();
    assert_eq!(more["read_into"]["at"], "steer", "{more}");
    wait_for("the agent at rest", || host.state(&session) == "done");
    assert_eq!(
        rows_saying(&host, &session, "assistant", "answered: more"),
        0,
        "it did not run again as a turn of its own"
    );
}

#[test]
fn after_the_turn_waits_where_the_turn_could_take_it_and_steer_is_read_in_at_once() {
    let (place, host, session) = claude_place("after-turn-steerable");
    std::fs::write(place.root.join("go"), "").unwrap();
    host.send(&session, "work", "work");
    wait_for("the turn at work", || place.root.join("working").exists());
    wait_for("the turn to say it can be steered", || {
        host.get(&format!("/turn-queue?session={session}"))["steerable"] == true
    });
    let after = send_while_busy(&host, &session, "later", "later", "after_turn");
    assert_eq!(after["queued"], true, "{after}");
    assert_eq!(after["while_busy"], "after_turn", "{after}");
    // A newer send cannot pull the older message into this turn.
    let newer = send_while_busy(&host, &session, "newer", "newer", "steer");
    assert_eq!(newer["while_busy"], "steer", "{newer}");
    assert!(waiting(&host, &session, "newer"));
    // It is not read in: it waits while the turn goes on, and can still be steered by hand.
    std::thread::sleep(Duration::from_millis(700));
    assert!(waiting(&host, &session, "later"));
    assert_eq!(
        rows_saying(&host, &session, "assistant", "answered both"),
        0
    );
    let (status, steered) = host.ask("POST", "/turn-queue/later/steer", None);
    assert_eq!(status, 202, "{steered}");
    wait_for("the answer to both", || {
        rows_saying(&host, &session, "assistant", "answered both") == 1
    });
    wait_for("the agent at rest", || host.state(&session) == "done");

    // Steer, where the turn takes words now: read in, nothing cut.
    host.send(&session, "work again", "work-again");
    wait_for("the turn to say it can be steered again", || {
        host.get(&format!("/turn-queue?session={session}"))["steerable"] == true
    });
    let steer = send_while_busy(&host, &session, "now", "now", "steer");
    assert_eq!(steer["while_busy"], "steer", "{steer}");
    assert!(steer.get("fallback").is_none(), "{steer}");
    wait_for("the second answer to both", || {
        rows_saying(&host, &session, "assistant", "answered both") == 2
    });
    let now = host
        .turns(&session)
        .into_iter()
        .find(|row| row["role"] == "user" && row["text"] == "now")
        .unwrap();
    assert_eq!(now["read_into"]["at"], "steer", "{now}");
}

#[test]
fn a_steer_that_is_no_words_waits_for_the_turn_to_end() {
    let place = Place::new("steer-not-words");
    let agy = place.root.join("agy");
    program(
        &agy,
        include_str!("../../../clarp-shim/tests/fake_acp/agy.py"),
    );
    std::fs::write(place.root.join("scenario"), "steer").unwrap();
    let world = [
        ("HOME", place.root.to_str().unwrap()),
        ("CLARP_AGY_PROGRAM", agy.to_str().unwrap()),
    ];
    let host = place.start_in("", &[], &world);
    let session = "acp-not-words";
    host.post(
        "/agents",
        json!({"name": session, "session": session, "backend": "agy-acp"}),
    );
    host.send(session, "case-hold", "first");
    wait_for("the tool at work", || host.state(session) == "tool");
    let sent = send_while_busy(&host, session, "/add-command", "command", "steer");
    assert_eq!(sent["queued"], true, "{sent}");
    assert_eq!(sent["while_busy"], "after_turn", "{sent}");
    assert_eq!(
        sent["fallback"],
        json!({"from": "steer", "to": "after_turn", "reason": "not_words",
               "text": "Files and / commands wait for the turn to end"}),
        "{sent}"
    );
    std::thread::sleep(Duration::from_millis(500));
    assert_eq!(host.state(session), "tool", "nothing was cut");
    assert!(waiting(&host, session, "command"));
    host.post("/stop", json!({"session": session}));
}

#[test]
fn a_steer_or_take_over_meets_a_turn_that_cannot_be_stopped_and_waits_for_it() {
    let place = Place::new("steer-not-cuttable");
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
    let compacting = || {
        let snapshot = host.get("/agents/snapshot");
        snapshot["agents"]
            .as_array()
            .unwrap()
            .iter()
            .any(|row| row["session"] == session && row["compacting"] == true)
    };
    host.send(&session, "first turn", "first");
    wait_for("the first turn to establish a session", || {
        host.state(&session) == "done"
    });
    host.post("/compact", json!({ "session": session }));
    wait_for("the compaction to run", compacting);

    let steer = send_while_busy(&host, &session, "steered", "steered", "steer");
    assert_eq!(steer["queued"], true, "{steer}");
    assert_eq!(steer["while_busy"], "after_turn", "{steer}");
    assert_eq!(
        steer["fallback"],
        json!({"from": "steer", "to": "after_turn", "reason": "turn_not_cuttable",
               "text": "This turn can't be stopped: your message waits for it"}),
        "{steer}"
    );
    let (status, refused) = host.ask("POST", "/turn-queue/steered/take-over", None);
    assert_eq!(status, 409, "{refused}");
    assert_eq!(refused["code"], "turn_not_cuttable", "{refused}");
    let taken = send_while_busy(&host, &session, "taken", "taken", "take_over");
    assert_eq!(taken["while_busy"], "after_turn", "{taken}");
    assert_eq!(taken["fallback"]["from"], "take_over", "{taken}");
    assert_eq!(taken["fallback"]["reason"], "turn_not_cuttable", "{taken}");
    // Nothing was cut: the compaction goes on, and both wait for it.
    std::thread::sleep(Duration::from_millis(500));
    assert!(compacting(), "the compaction was not cut");
    assert!(waiting(&host, &session, "steered") && waiting(&host, &session, "taken"));
    host.post("/stop", json!({ "session": session }));
}
