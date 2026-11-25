//! Issue 839: the owner's message never waits behind a turn that is not a person's.
use super::*;

/// A Claude Code stand-in: the peer's work runs until it is stopped, whatever else its prompt
/// carries; anything else is answered at once with its last line.
const CLAUDE: &str = r#"#!/usr/bin/env python3
import json, pathlib, sys, time

if "--version" in sys.argv:
    print("2.1.321 (Claude Code)")
    raise SystemExit(0)

root = pathlib.Path(__file__).resolve().parent
flag = "--resume" if "--resume" in sys.argv else "--session-id"
session = sys.argv[sys.argv.index(flag) + 1]

def say(kind, **fields):
    print(json.dumps({"type": kind, "session_id": session, **fields}), flush=True)

record = json.loads(sys.stdin.readline())
uid = record["uuid"]
text = record["message"]["content"][0]["text"]
say("system", subtype="init")
say("user", isReplay=True, uuid=uid, message={"role": "user", "content": []})
if "work for the peer" in text:
    (root / "peer-working").touch()
    time.sleep(120)
    raise SystemExit(0)
answer = "answered: " + text.strip().splitlines()[-1]
say("assistant", message={"id": "msg_" + uid, "role": "assistant",
    "content": [{"type": "text", "text": answer}]})
say("result", subtype="success", is_error=False, result=answer, user_message_uuids=[uid])
"#;

#[test]
fn the_owners_message_ends_another_agents_turn_and_the_agent_is_told_to_take_it_up() {
    let place = Place::new("owner-preempts");
    let claude = place.root.join("claude");
    program(&claude, CLAUDE);
    let home = place.root.join("home");
    let world = [
        ("HOME", home.to_str().unwrap()),
        ("CLARP_CLAUDE_PROGRAM", claude.to_str().unwrap()),
    ];
    let host = place.start_in("", &[], &world);
    let target = "desk";
    host.post(
        "/agents",
        json!({"name": "Desk", "session": target, "backend": "claude"}),
    );
    let peer = host.new_agent("Peer");
    let peer_id = row_id(&host, &peer);
    let sent = host.post(
        "/send",
        json!({
            "session": target, "text": "work for the peer", "client_msg_id": "from-peer",
            "origin": "agent", "sender": peer_id,
        }),
    );
    assert_eq!(sent["queued"], false, "{sent}");
    wait_for("the peer's turn at work", || {
        place.root.join("peer-working").exists()
    });

    host.send(target, "owner says hi", "owner");
    let text_of = |row: &Value| row["text"].as_str().unwrap_or("").to_owned();
    // Long before the peer's work would have ended by itself.
    let answered =
        |row: &Value| row["role"] == "assistant" && text_of(row) == "answered: owner says hi";
    wait_for("the owner's answer", || {
        host.turns(target).iter().any(answered)
    });
    // The agent is told to take the cut work up again, after the owner's turn: in the
    // conversation once its turn starts, in the queue until then.
    let cut_short = "was cut short because the owner wrote to you";
    let note_text = || {
        let queue = host.get(&format!("/turn-queue?session={target}"));
        let queued = queue["items"].as_array().cloned().unwrap_or_default();
        let turns = host.turns(target);
        let found = |rows: &[Value]| {
            rows.iter()
                .map(text_of)
                .find(|text| text.contains(cut_short))
        };
        found(&turns).or_else(|| found(&queued))
    };
    wait_for("the note to take the peer's message up again", || {
        note_text().is_some()
    });
    let note = note_text().unwrap();
    assert!(note.contains("Peer"), "{note}");
    assert!(note.contains("work for the peer"), "{note}");
    let turns = host.turns(target);
    let owner = turns.iter().position(answered).unwrap();
    if let Some(noted) = turns
        .iter()
        .position(|row| text_of(row).contains(cut_short))
    {
        assert!(owner < noted, "{turns:#?}");
    }
}

#[test]
fn behind_the_owners_own_turn_the_owners_next_message_waits_as_before() {
    let place = Place::new("owner-waits");
    let host = place.start();
    let target = host.new_agent("Desk");
    assert_eq!(host.send(&target, "sleep 60", "first")["queued"], false);
    wait_for("the owner's turn to run", || {
        host.state(&target) == "thinking"
    });
    let second = host.send(&target, "second", "second");
    assert_eq!(second["queued"], true, "{second}");
    let queue = host.get(&format!("/turn-queue?session={target}"));
    assert!(
        queue["items"]
            .as_array()
            .unwrap()
            .iter()
            .any(|item| item["id"] == "second"),
        "{queue}"
    );
    assert_eq!(host.state(&target), "thinking", "the owner's turn runs on");
    host.post("/stop", json!({"session": target}));
}
