//! Issue 747: the user stops a turn while two messages of theirs wait, and does it again and
//! again, as an app does. Each Stop tells the turn's `interrupted` state once, and leaves the
//! queue as it answered: held, also after the messages it let through have run, until the user
//! sends again. The same on every backend, however soon the stopped process exits. One file a
//! piece, declared in `host.rs`.

use super::*;

/// A stand-in of Claude Code new enough that a message sent while its turn runs is written to
/// the running CLI. A turn whose words say `HOLD-747` works until it is stopped and reads
/// nothing more; any other is answered at once.
const CLAUDE: &str = r#"#!/usr/bin/env python3
import json
import signal
import sys
import time

if "--version" in sys.argv:
    print("2.1.321 (Claude Code)")
    raise SystemExit(0)

flag = "--resume" if "--resume" in sys.argv else "--session-id"
session = sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else "stand-in"
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

def say(kind, **fields):
    print(json.dumps({"type": kind, "session_id": session, **fields}), flush=True)

record = json.loads(sys.stdin.readline())
uid = record.get("uuid", "")
text = record["message"]["content"][0]["text"]
say("system", subtype="init")
if "--replay-user-messages" in sys.argv and uid:
    say("user", isReplay=True, uuid=uid, message={"role": "user", "content": []})
if "HOLD-747" in text:
    while True:
        time.sleep(0.05)
say("assistant", message={"id": "msg-" + uid, "role": "assistant",
    "content": [{"type": "text", "text": "answered"}]})
say("result", subtype="success", is_error=False, result="answered",
    user_message_uuids=[uid] if uid else [])
"#;

/// A stand-in of `codex exec`: a turn whose words say `HOLD-747` works until it is stopped; any
/// other is answered at once.
const CODEX: &str = r#"#!/usr/bin/env python3
import json
import signal
import sys
import time

if sys.argv[1:2] != ["exec"]:
    sys.exit(0)
prompt = sys.stdin.read()
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
def say(value):
    print(json.dumps(value), flush=True)
say({"type": "thread.started", "thread_id": "00000000-0000-7000-8000-000000000747"})
say({"type": "turn.started"})
if "HOLD-747" in prompt:
    while True:
        time.sleep(0.05)
say({"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": "answered"}})
say({"type": "turn.completed", "usage": {"input_tokens": 1, "cached_input_tokens": 0,
                                         "output_tokens": 1, "reasoning_output_tokens": 0}})
"#;

/// The stand-in for `agy acp`, in its scenario `steer`: `case-hold` works until it is stopped
/// and takes none of the prompts it is sent meanwhile; `add-...` is answered at once.
const AGY: &str = include_str!("../../../clarp-shim/tests/fake_acp/agy.py");

/// Three rounds: a turn that works until it is stopped, two messages of the user's sent while it
/// works, the user's Stop. `hold` is what makes a turn work; `quick` the words of a waiting
/// message, by its round and its place.
fn stops_alike(host: &Host, session: &str, hold: &str, quick: impl Fn(usize, usize) -> String) {
    let heard = host.listen();
    wait_for("the event stream", || {
        heard
            .lock()
            .unwrap()
            .iter()
            .any(|event| event["type"] == "agent-roster")
    });
    let queue = || host.get(&format!("/turn-queue?session={session}"));
    let interrupted = || -> Vec<Value> {
        heard
            .lock()
            .unwrap()
            .iter()
            .filter(|event| {
                event["type"] == "agent-state"
                    && event["session"] == session
                    && event["kind"] == "interrupted"
            })
            .cloned()
            .collect()
    };
    let users = || -> Vec<String> {
        host.turns(session)
            .iter()
            .filter(|row| row["role"] == "user")
            .filter_map(|row| row["id"].as_str().map(str::to_owned))
            .collect()
    };
    for round in 0..3 {
        // The user's fresh message runs past the hold the last Stop left, and ends it.
        let held = host.send(session, hold, &format!("hold-{round}"));
        assert_eq!(held["queued"], false, "round {round}: {held}");
        wait_for("the turn to work", || {
            matches!(host.state(session).as_str(), "thinking" | "tool")
        });
        assert_eq!(queue()["paused"], false, "round {round}: {}", queue());
        let waiting = [0, 1].map(|n| format!("wait-{round}-{n}"));
        for (n, id) in waiting.iter().enumerate() {
            let sent = host.send(session, &quick(round, n), id);
            assert_eq!(sent["queued"], true, "round {round}: {sent}");
        }

        let before = interrupted().len();
        let stopped = host.post("/stop", json!({ "session": session }));
        assert_eq!(stopped["terminated"], 1, "round {round}: {stopped}");
        let paused = &stopped["queue_paused"];
        assert_eq!(paused, &json!(true), "round {round}: {stopped}");
        let now = queue();
        assert_eq!(&now["paused"], paused, "round {round}, right after the Stop: {now}");

        // The Stop let both messages run, the oldest first; the hold stays after them.
        wait_for("the messages the Stop let through to be answered", || {
            let read = users();
            host.state(session) == "done"
                && waiting.iter().all(|id| read.contains(&format!("u-{id}")))
                && queue()["items"] == json!([])
        });
        let read = users();
        let at = |id: &str| read.iter().position(|row| *row == format!("u-{id}"));
        assert!(at(&waiting[0]) < at(&waiting[1]), "round {round}: {read:?}");
        let after = queue();
        assert_eq!(&after["paused"], paused, "round {round}, after they ran: {after}");

        // A moment longer for anything more of this Stop: one `interrupted`, for its turn.
        std::thread::sleep(Duration::from_millis(500));
        let told = interrupted();
        assert_eq!(
            told.len() - before,
            1,
            "round {round}: {:#?}",
            &told[before..]
        );
        assert_eq!(told[before]["detail"]["source"], "user_stop", "{}", told[before]);
    }
}

#[test]
fn issue_747_each_stop_of_a_stand_in_turn_is_told_once_and_holds_the_queue_alike() {
    let place = Place::new("stop-events");
    let host = place.start();
    let session = host.new_agent("Stopper");
    stops_alike(&host, &session, "sleep 60", |round, n| {
        format!("quick {round} {n}")
    });
}

#[test]
fn issue_747_each_stop_of_a_claude_turn_is_told_once_and_holds_the_queue_alike() {
    let place = Place::new("stop-events-claude");
    let claude = place.root.join("claude");
    program(&claude, CLAUDE);
    let host = place.start_in(
        "",
        &[],
        &[("CLARP_CLAUDE_PROGRAM", claude.to_str().unwrap())],
    );
    let made = host.post("/agents", json!({ "name": "Clay", "backend": "claude" }));
    let session = made["session"].as_str().unwrap().to_string();
    stops_alike(&host, &session, "HOLD-747", |round, n| {
        format!("quick {round} {n}")
    });
}

#[test]
fn issue_747_each_stop_of_an_antigravity_turn_is_told_once_and_holds_the_queue_alike() {
    let place = Place::new("stop-events-agy");
    let agy = place.root.join("agy");
    program(&agy, AGY);
    std::fs::write(place.root.join("scenario"), "steer").unwrap();
    let world = [
        ("HOME", place.root.to_str().unwrap()),
        ("CLARP_AGY_PROGRAM", agy.to_str().unwrap()),
    ];
    let host = place.start_in("", &[], &world);
    let made = host.post("/agents", json!({ "name": "Ada", "backend": "agy-acp" }));
    let session = made["session"].as_str().unwrap().to_string();
    stops_alike(&host, &session, "case-hold", |round, n| {
        format!("add-q{round}-{n}")
    });
}

#[test]
fn issue_747_each_stop_of_a_codex_turn_is_told_once_and_holds_the_queue_alike() {
    let place = Place::new("stop-events-codex");
    let codex = place.root.join("codex");
    program(&codex, CODEX);
    let host = place.start_in(
        "",
        &[],
        &[("CLARP_CODEX_PROGRAM", codex.to_str().unwrap())],
    );
    let made = host.post("/agents", json!({ "name": "Cody", "backend": "codex" }));
    let session = made["session"].as_str().unwrap().to_string();
    stops_alike(&host, &session, "HOLD-747", |round, n| {
        format!("quick {round} {n}")
    });
}
