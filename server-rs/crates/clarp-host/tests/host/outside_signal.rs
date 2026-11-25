//! Issue 829: a turn whose process something outside the Host ended with a signal (an
//! operator's `kill` of its shim) settles as ended from outside, in its own kind and words, not
//! as the tool's failure. A Stop through the Host still settles as stopped. One file a piece,
//! declared in `host.rs`.

use super::*;

/// A stand-in of Claude Code: a turn whose words say `HOLD-829` works until it is asked to
/// stop, and then does what Claude Code does on SIGINT: writes a result that is an error with
/// no words, and exits. Its shim's process id is left in `shim-<words>`.
const CLAUDE: &str = r#"#!/usr/bin/env python3
import json
import os
import pathlib
import signal
import sys
import time

if "--version" in sys.argv:
    print("2.1.321 (Claude Code)")
    raise SystemExit(0)

root = pathlib.Path(__file__).resolve().parent
flag = "--resume" if "--resume" in sys.argv else "--session-id"
session = sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else "stand-in"

def say(kind, **fields):
    print(json.dumps({"type": kind, "session_id": session, **fields}), flush=True)

def interrupted(*_):
    say("result", subtype="error_during_execution", is_error=True, result="")
    sys.exit(0)

signal.signal(signal.SIGINT, interrupted)
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
record = json.loads(sys.stdin.readline())
uid = record.get("uuid", "")
text = record["message"]["content"][0]["text"]
say("system", subtype="init")
if "HOLD-829" in text:
    (root / f"shim-{text}").write_text(str(os.getppid()))
    while True:
        time.sleep(0.05)
say("assistant", message={"id": "msg-" + uid, "role": "assistant",
    "content": [{"type": "text", "text": "answered"}]})
say("result", subtype="success", is_error=False, result="answered",
    user_message_uuids=[uid] if uid else [])
"#;

const OUTSIDE: &str = "The turn's process was ended from outside (SIGTERM)";

/// Starts a turn that works until it is ended, and gives the process id of its shim.
fn hold(place: &Place, host: &Host, session: &str, words: &str) -> i32 {
    let sent = host.send(session, words, &format!("id-{words}"));
    assert_eq!(sent["queued"], false, "{sent}");
    let file = place.root.join(format!("shim-{words}"));
    wait_for("the turn's tool to work", || file.exists());
    wait_for("the turn to show as working", || {
        matches!(host.state(session).as_str(), "thinking" | "tool")
    });
    std::fs::read_to_string(&file)
        .unwrap()
        .trim()
        .parse()
        .unwrap()
}

/// The `/log` row that closed the turn of the message with this id.
fn closing(host: &Host, session: &str, id: &str) -> Value {
    let rows = host.turns(session);
    let at = rows
        .iter()
        .position(|row| row["id"] == format!("u-{id}"))
        .unwrap_or_else(|| panic!("no row of {id}: {rows:?}"));
    rows[at..]
        .iter()
        .find(|row| row["turn"].is_object())
        .cloned()
        .unwrap_or_else(|| panic!("no end of {id}: {rows:?}"))
}

#[test]
fn a_turn_ended_from_outside_says_so_and_a_stop_is_still_a_stop() {
    let place = Place::new("outside-signal");
    let claude = place.root.join("claude");
    program(&claude, CLAUDE);
    let host = place.start_in(
        "",
        &[],
        &[("CLARP_CLAUDE_PROGRAM", claude.to_str().unwrap())],
    );
    let session = "outside";
    host.post(
        "/agents",
        json!({"name": session, "session": session, "backend": "claude"}),
    );

    // Something outside the Host sends the turn's shim SIGTERM.
    let shim = hold(&place, &host, session, "HOLD-829-a");
    nix::sys::signal::kill(
        nix::unistd::Pid::from_raw(shim),
        nix::sys::signal::Signal::SIGTERM,
    )
    .unwrap();
    wait_for("the turn ended from outside to settle", || {
        host.get(&format!("/log?session={session}&limit=200"))["turns"]
            .as_array()
            .unwrap()
            .iter()
            .any(|row| row["turn"].is_object())
    });
    let end = closing(&host, session, "id-HOLD-829-a");
    assert_eq!(end["turn"]["status"], "failed", "{end}");
    // Its kind is on the turn's end and on the row that says why it failed, so an app needs
    // no words to tell it (`failure_kind`).
    assert_eq!(end["turn"]["kind"], "ended_from_outside", "{end}");
    let rows = host.turns(session);
    let failed = rows
        .iter()
        .find(|row| row["id"] == "marker-u-id-HOLD-829-a")
        .unwrap_or_else(|| panic!("no failure row: {rows:?}"));
    assert_eq!(failed["failure_kind"], "ended_from_outside", "{failed}");
    assert!(
        failed["kind"].is_null(),
        "the row's own kind is untouched: {failed}"
    );
    let said = host
        .turns(session)
        .iter()
        .map(Value::to_string)
        .collect::<String>();
    assert!(said.contains(OUTSIDE), "its own words: {said}");
    assert!(
        !said.contains("claude reported an error"),
        "not the tool's failure: {said}"
    );

    // A Stop through the Host is a stop, not a signal from outside.
    hold(&place, &host, session, "HOLD-829-b");
    let stopped = host.post("/stop", json!({ "session": session }));
    assert_eq!(stopped["terminated"], 1, "{stopped}");
    wait_for("the stopped turn to settle", || {
        let rows = host.turns(session);
        let at = rows.iter().position(|row| row["id"] == "u-id-HOLD-829-b");
        at.is_some_and(|at| rows[at..].iter().any(|row| row["turn"].is_object()))
    });
    let end = closing(&host, session, "id-HOLD-829-b");
    assert_eq!(end["turn"]["status"], "interrupted", "{end}");
    assert!(
        end["turn"].get("kind").is_none(),
        "a stop has no kind: {end}"
    );
    let rows = host.turns(session);
    let at = rows
        .iter()
        .position(|row| row["id"] == "u-id-HOLD-829-b")
        .unwrap();
    let after: String = rows[at..].iter().map(Value::to_string).collect();
    assert!(
        !after.contains(OUTSIDE),
        "a stop says nothing of outside: {after}"
    );
}
