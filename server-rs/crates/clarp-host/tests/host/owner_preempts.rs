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
    // The agent is told to take the cut work up again, after the owner's turn.
    let cut_short = "was cut short because the owner wrote to you";
    let log = |query: &str| -> Vec<Value> {
        host.get(&format!("/log?session={target}&limit=200{query}"))["turns"]
            .as_array()
            .cloned()
            .unwrap_or_default()
    };
    let is_note = |row: &Value| text_of(row).contains(cut_short);
    wait_for("the note to take the peer's message up again", || {
        log("&coordination=show").iter().any(is_note)
    });
    let all = log("&coordination=show");
    let note = all.iter().find(|row| is_note(row)).unwrap();
    let text = text_of(note);
    assert!(text.contains("Peer"), "{text}");
    assert!(text.contains("work for the peer"), "{text}");
    assert!(
        text.contains(&format!(
            "answer Peer ({peer}): `clarp-admin reply` for a request"
        )),
        "the note names whom to answer, with the session: {text}"
    );
    let owner = all.iter().position(answered).unwrap();
    let noted = all.iter().position(is_note).unwrap();
    assert!(owner < noted, "{all:#?}");
    // The peer's turn was the agents' (hidden coordination): the note that takes it up again
    // is too, and never reads as the owner's own words in his chat.
    let peer_row = all.iter().find(|row| row["id"] == "u-from-peer").unwrap();
    assert_eq!(peer_row["audience"], "agents", "{peer_row}");
    assert_eq!(note["audience"], "agents", "{note}");
    assert!(!log("&coordination=hide").iter().any(is_note));
    // A chat read without automation leaves the note out whatever its audience.
    assert!(
        !log("&coordination=show&include_automated=0")
            .iter()
            .any(is_note)
    );
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
