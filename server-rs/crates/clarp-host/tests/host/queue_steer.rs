//! A waiting message of the owner's is read into the running turn, or takes over one that
//! cannot take it, and the live turn says whether it can (issue 867, feature `queue_steer`).
//! One file a piece, declared in `host.rs`.

use super::*;

/// A Claude Code stand-in. Its turn on "work" says nothing until the file `go` exists (so it
/// is not bound to its session, and takes nothing on its input yet), then reads one more message
/// on its input, says it read it, and answers both. Anything else is answered at once. While it
/// waits it has printed a line on its stderr, so its turn is not a quiet one a waiting message takes over in
/// the tool's own way (`Actor::takeover_quiet`): the message waits until it is steered.
pub(super) const CLAUDE: &str = r#"#!/usr/bin/env python3
import json, pathlib, select, sys, time

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

def answer(text, *uids):
    say("assistant", message={"id": "msg_" + uids[-1], "role": "assistant",
        "content": [{"type": "text", "text": text}]})
    say("result", subtype="success", is_error=False, result=text, user_message_uuids=list(uids))

uid, text = prompt()
if "work" not in text:
    say("system", subtype="init")
    say("user", isReplay=True, uuid=uid, message={"role": "user", "content": []})
    answer("answered: " + text.strip().splitlines()[-1], uid)
    raise SystemExit(0)
print("warming up", file=sys.stderr, flush=True)
(root / "started").touch()
while not (root / "go").exists():
    time.sleep(0.05)
say("system", subtype="init")
say("user", isReplay=True, uuid=uid, message={"role": "user", "content": []})
say("assistant", message={"id": "msg_work", "role": "assistant",
    "content": [{"type": "text", "text": "working"}]})
(root / "working").write_text(text)
while not select.select([sys.stdin], [], [], 0.05)[0]:
    if (root / "probe").exists():
        (root / "still-waiting").touch()
steered, words = prompt()
(root / "read-words").write_text(words)
say("user", isReplay=True, uuid=steered, message={"role": "user", "content": []})
answer("answered both", uid, steered)
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
        host.get("/server-info")["contract"]["features"]["queue_steer"],
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
    assert_eq!(
        (status, &refused["code"]),
        (404, &json!("not_waiting")),
        "{refused}"
    );
    // Nor is it read into a turn whose tool takes nothing on its input: it waits on.
    let (status, refused) = host.ask("POST", "/turn-queue/now/steer", None);
    assert_eq!(
        (status, &refused["code"]),
        (409, &json!("not_steerable")),
        "{refused}"
    );
    assert_eq!(host.state(&session), "thinking", "nothing was cut");

    let (status, taken) = host.ask("POST", "/turn-queue/now/take-over", None);
    assert_eq!(status, 202, "{taken}");
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
    // It no longer waits: taking over again, or steering it, finds nothing.
    let (status, again) = host.ask("POST", "/turn-queue/now/take-over", None);
    assert_eq!(status, 404, "{again}");
    let (status, again) = host.ask("POST", "/turn-queue/now/steer", None);
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
    let guard = json!({"expected_turn":{"server_id":"foreign","agent_id":"foreign","turn_id":1}});
    for action in ["take-over", "steer"] {
        for supplied in [&guard, &json!({"expected_turn": null})] {
            let (status, refused) = host.ask(
                "POST",
                &format!("/turn-queue/sched/{action}"),
                Some(supplied),
            );
            assert_eq!((status, &refused["code"]), (409, &json!("not_the_owners")));
        }
    }
    let (status, refused) = host.ask("POST", "/turn-queue/sched/take-over", None);
    assert_eq!(
        (status, &refused["code"]),
        (409, &json!("not_the_owners")),
        "{refused}"
    );
    assert_eq!(host.state(&session), "thinking", "nothing was cut");
    host.post("/stop", json!({ "session": session }));
}

#[test]
fn a_waiting_message_is_read_into_the_running_turn_once_it_can_take_it() {
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
    wait_for("the turn's tool to start", || {
        place.root.join("started").exists()
    });
    // Its tool has not said it is at work: it takes nothing yet, and what is sent waits.
    assert_eq!(steerable(&host, &session).0, false);
    let waits = host.send(&session, "more", "more");
    assert_eq!(waits["queued"], true, "{waits}");
    let (status, refused) = host.ask("POST", "/turn-queue/more/steer", None);
    assert_eq!(
        (status, &refused["code"]),
        (409, &json!("not_steerable")),
        "{refused}"
    );
    // A message the owner left for later is not read in, however the turn stands.
    let later = host.post(
        "/send",
        json!({ "session": session, "text": "later", "client_msg_id": "later",
                "queue_if_busy": true }),
    );
    assert_eq!(later["queued"], true, "{later}");
    let (status, refused) = host.ask("POST", "/turn-queue/later/steer", None);
    assert_eq!(
        (status, &refused["code"]),
        (409, &json!("left_for_later")),
        "{refused}"
    );
    let (status, refused) = host.ask("POST", "/turn-queue/nothing-such/steer", None);
    assert_eq!(
        (status, &refused["code"]),
        (404, &json!("not_waiting")),
        "{refused}"
    );

    // The tool is at work, and says it can be steered: the waiting message is read in on
    // request, and the turn ends with the answer to both.
    std::fs::write(place.root.join("go"), "").unwrap();
    wait_for("the turn at work", || place.root.join("working").exists());
    wait_for("the turn to say it can be steered", || {
        steerable(&host, &session) == (json!(true), json!(true))
    });
    let (status, steered) = host.ask("POST", "/turn-queue/more/steer", None);
    assert_eq!(status, 202, "{steered}");
    assert_eq!(steered["queued"], false, "{steered}");
    wait_for("the answer to both", || {
        rows_saying(&host, &session, "assistant", "answered both") == 1
    });
    let more = host
        .turns(&session)
        .into_iter()
        .find(|row| row["role"] == "user" && row["text"] == "more")
        .unwrap();
    assert_eq!(more["read_into"]["at"], "steer", "{more}");
    // The message left for later runs as a turn of its own after it.
    wait_for("the later message's own turn", || {
        rows_saying(&host, &session, "assistant", "answered: later") == 1
    });
    wait_for("the agent at rest", || host.state(&session) == "done");
    assert_eq!(steerable(&host, &session).0, false, "no turn runs");
}

#[test]
fn queue_actions_bind_to_the_displayed_journal_turn_across_replacement() {
    let (place, host, session) = super::send_while_busy::claude_place("queue-action-binding");
    let session = session.as_str();
    assert_eq!(
        host.get("/server-info")["contract"]["features"]["queue_action_expected_turn"],
        225
    );
    let live =
        || host.get(&format!("/live?session={session}"))["turn"]["queue_action_binding"].clone();
    let queue = || host.get(&format!("/turn-queue?session={session}"));
    host.send(session, "work A", "a");
    wait_for("A warming up", || place.root.join("started").exists());
    let a = live();
    assert!(
        a["turn_id"].is_u64() && a["server_id"].is_string() && a["agent_id"].is_string(),
        "{a}"
    );
    let other = host.new_agent("Other");
    host.send(&other, "sleep 60", "another-first-turn");
    wait_for("the other agent working", || {
        host.state(&other) == "thinking"
    });
    let other_binding =
        host.get(&format!("/live?session={other}"))["turn"]["queue_action_binding"].clone();
    assert_eq!(a["turn_id"], other_binding["turn_id"]);
    assert_ne!(a["agent_id"], other_binding["agent_id"]);
    for (id, words) in [
        ("older", "older words"),
        ("q", "selected words"),
        ("b", "work B"),
    ] {
        assert_eq!(host.send(session, words, id)["queued"], true);
    }
    for action in ["steer", "take-over"] {
        let before = queue();
        for bad in [
            json!({"agent_id":a["agent_id"],"server_id":a["server_id"],"turn_id":1u64 << 63}),
            json!({"agent_id":a["agent_id"],"server_id":a["server_id"],"turn_id":0}),
            Value::Null,
            json!(false),
            json!({}),
            json!(["host", "agent", 1]),
            json!({"server_id":"x","agent_id":"y","turn_id":"1"}),
        ] {
            assert_eq!(
                host.ask(
                    "POST",
                    &format!("/turn-queue/q/{action}"),
                    Some(&json!({"expected_turn":bad}))
                )
                .0,
                400
            );
        }
        let mut other_host = a.clone();
        other_host["server_id"] = json!("somewhere-else");
        let mut max = a.clone();
        max["turn_id"] = json!(i64::MAX);
        for wrong in [&other_binding, &other_host, &max] {
            let (status, refused) = host.ask(
                "POST",
                &format!("/turn-queue/q/{action}"),
                Some(&json!({"expected_turn":wrong})),
            );
            assert_eq!((status, &refused["code"]), (409, &json!("stale_turn")));
        }
        assert_eq!(queue(), before);
    }
    let (status, taken) = host.ask(
        "POST",
        "/turn-queue/b/take-over",
        Some(&json!({"expected_turn":a})),
    );
    assert_eq!(status, 202, "{taken}");
    std::fs::write(place.root.join("go"), "").unwrap();
    wait_for("B ready for a steer", || {
        std::fs::read_to_string(place.root.join("working"))
            .is_ok_and(|words| words.contains("work B"))
            && live()["turn_id"].is_u64()
            && live() != a
            && queue()["steerable"] == true
    });
    let b = live();
    assert_eq!(b["agent_id"], a["agent_id"]);
    assert_ne!(b["turn_id"], a["turn_id"]);
    let before = queue();
    let ids: Vec<_> = before["items"]
        .as_array()
        .unwrap()
        .iter()
        .map(|item| &item["id"])
        .collect();
    assert_eq!(ids, vec![&json!("older"), &json!("q")]);
    for action in ["steer", "take-over"] {
        let (status, refused) = host.ask(
            "POST",
            &format!("/turn-queue/q/{action}"),
            Some(&json!({"expected_turn":a})),
        );
        eprintln!("POST /turn-queue/q/{action} HTTP {status} {refused}");
        assert_eq!(
            (status, &refused["code"]),
            (409, &json!("stale_turn")),
            "{refused}"
        );
        assert_eq!(queue(), before);
        assert_eq!(live(), b);
    }
    std::fs::write(place.root.join("probe"), "").unwrap();
    wait_for("B's provider still waiting for its first input", || {
        place.root.join("still-waiting").exists()
    });
    assert!(!place.root.join("read-words").exists());
    assert_eq!(host.ask("DELETE", "/turn-queue/older", None).0, 200);
    assert_eq!(
        host.ask(
            "POST",
            "/turn-queue/q/steer",
            Some(&json!({"expected_turn":b}))
        )
        .0,
        202
    );
    wait_for("the selected answer", || {
        rows_saying(&host, session, "assistant", "answered both") == 1
    });
    assert_eq!(rows_saying(&host, session, "user", "selected words"), 1);
    assert!(
        std::fs::read_to_string(place.root.join("read-words"))
            .unwrap()
            .contains("selected words")
    );
    wait_for("the binding cleared at turn end", || live().is_null());
    assert_eq!(
        host.ask(
            "POST",
            "/turn-queue/q/steer",
            Some(&json!({"expected_turn":b}))
        )
        .0,
        404
    );
}
