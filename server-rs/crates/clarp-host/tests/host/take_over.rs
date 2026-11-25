//! A waiting message of the owner's takes over a running turn that cannot be steered (issue
//! 867, feature `turn_take_over`), and the live turn says whether it can be. One file a piece,
//! declared in `host.rs`.

use super::*;

/// A Claude Code stand-in whose first turn reads one more message on its input: it says it
/// read it, and answers both.
const CLAUDE: &str = r#"#!/usr/bin/env python3
import json, pathlib, sys

if "--version" in sys.argv:
    print("2.1.321 (Claude Code)")
    raise SystemExit(0)

root = pathlib.Path(__file__).resolve().parent
flag = "--resume" if "--resume" in sys.argv else "--session-id"
session = sys.argv[sys.argv.index(flag) + 1]

def say(kind, **fields):
    print(json.dumps({"type": kind, "session_id": session, **fields}), flush=True)

def prompt():
    record = json.loads(sys.stdin.readline())
    return record["uuid"], record["message"]["content"][0]["text"]

uid, text = prompt()
say("system", subtype="init")
say("user", isReplay=True, uuid=uid, message={"role": "user", "content": []})
say("assistant", message={"id": "msg_work", "role": "assistant",
    "content": [{"type": "text", "text": "working"}]})
(root / "working").touch()
steered, words = prompt()
say("user", isReplay=True, uuid=steered, message={"role": "user", "content": []})
say("assistant", message={"id": "msg_" + steered, "role": "assistant",
    "content": [{"type": "text", "text": "answered both"}]})
say("result", subtype="success", is_error=False, result="answered both",
    user_message_uuids=[uid, steered])
"#;

fn steerable(host: &Host, session: &str) -> (Value, Value) {
    let queue = host.get(&format!("/turn-queue?session={session}"));
    let live = host.get(&format!("/live?session={session}"));
    (
        queue["steerable"].clone(),
        live["turn"]["steerable"].clone(),
    )
}

fn rows_saying(host: &Host, session: &str, role: &str, text: &str) -> usize {
    host.turns(session)
        .iter()
        .filter(|row| row["role"] == role && row["text"] == text)
        .count()
}

#[test]
fn a_waiting_message_takes_over_a_turn_that_cannot_be_steered_and_runs_next_once() {
    let place = Place::new("take-over");
    let host = place.start();
    assert_eq!(
        host.get("/server-info")["contract"]["features"]["turn_take_over"],
        197
    );
    let session = host.new_agent("Taker");
    host.send(&session, "sleep 60", "long");
    wait_for("the turn to run", || host.state(&session) == "thinking");

    // The scripted tool takes nothing on its input: a message sent now waits, and the turn
    // and the queue say so.
    let (queue, live) = steerable(&host, &session);
    assert_eq!(queue, false);
    assert_ne!(live, true);
    let waits = host.send(&session, "now this", "now");
    assert_eq!(waits["queued"], true, "{waits}");

    // A message that does not wait cannot take over.
    let (status, refused) = host.ask("POST", "/turn-queue/nothing-such/take-over", None);
    assert_eq!(status, 404, "{refused}");
    assert_eq!(host.state(&session), "thinking", "nothing was cut");

    let taken = host.post("/turn-queue/now/take-over", json!({}));
    assert_eq!(taken["taken_over"], true, "{taken}");
    // The cut turn ends as stopped, long before its sixty seconds (a wait gives up after
    // thirty), and the message runs next.
    wait_for("the message's answer", || {
        rows_saying(&host, &session, "assistant", "now this") == 1
    });
    let queue = host.get(&format!("/turn-queue?session={session}"));
    assert_eq!(queue["paused"], false, "a take-over holds nothing: {queue}");
    wait_for("the turn to end", || host.state(&session) == "done");
    // It ran once, as one message.
    assert_eq!(rows_saying(&host, &session, "user", "now this"), 1);
    assert_eq!(rows_saying(&host, &session, "assistant", "now this"), 1);
    // It no longer waits: taking over again finds nothing.
    let (status, again) = host.ask("POST", "/turn-queue/now/take-over", None);
    assert_eq!(status, 404, "{again}");
    // The queue goes on by itself: the next message runs at once.
    let next = host.send(&session, "and on", "on");
    assert_eq!(next["queued"], false, "{next}");
    wait_for("the next answer", || {
        rows_saying(&host, &session, "assistant", "and on") == 1
    });
}

#[test]
fn only_the_owners_waiting_message_takes_over() {
    let place = Place::new("take-over-not-his");
    let host = place.start();
    let session = host.new_agent("Taker");
    host.send(&session, "sleep 60", "long");
    wait_for("the turn to run", || host.state(&session) == "thinking");
    host.post(
        "/send",
        json!({ "session": session, "text": "scheduled", "client_msg_id": "sched",
                "origin": "schedule", "queue_if_busy": true }),
    );
    let (status, refused) = host.ask("POST", "/turn-queue/sched/take-over", None);
    assert_eq!(status, 409, "{refused}");
    assert_eq!(host.state(&session), "thinking", "nothing was cut");
    host.post("/stop", json!({ "session": session }));
}

#[test]
fn a_turn_whose_tool_reads_its_input_says_it_is_steerable_while_it_can_be() {
    let place = Place::new("steerable");
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
    // At rest nothing runs, and nothing is steerable.
    assert_eq!(steerable(&host, &session).0, false);
    host.send(&session, "work", "work");
    wait_for("the turn at work", || place.root.join("working").exists());
    wait_for("the turn to say it can be steered", || {
        steerable(&host, &session) == (json!(true), json!(true))
    });
    // A message sent now is read into it, and the turn ends with the answer to both.
    host.send(&session, "more", "more");
    wait_for("the turn to end", || host.state(&session) == "done");
    assert_eq!(
        rows_saying(&host, &session, "assistant", "answered both"),
        1
    );
    assert_eq!(steerable(&host, &session).0, false, "no turn runs");
}
