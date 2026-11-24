//! Issue 745: a helper's chat holds the turns other agents give it as its conversation. The
//! message its parent sent and the helper's answer are in the Conversation view, its work is
//! not, and the turn's audience stays the agents'. Another agent's chat keeps such turns out.

use super::*;

/// A stand-in of Claude Code: it reads, says so with a call, and answers once `finish` is there.
const CLAUDE: &str = r#"#!/usr/bin/env python3
import json
import pathlib
import sys
import time

if "--version" in sys.argv:
    print("2.1.320 (Claude Code)")
    raise SystemExit(0)

root = pathlib.Path(__file__).resolve().parent
sys.stdin.readline()

def send(kind, **fields):
    print(json.dumps({"type": kind, "session_id": "helper-fixture", **fields}), flush=True)

send("system", subtype="init")
send("assistant", message={"id": "msg_work", "role": "assistant", "content": [
    {"type": "text", "text": "I am reading."},
    {"type": "tool_use", "id": "toolu_helper", "name": "Bash", "input": {"command": "ls"}}
]})
send("user", message={"role": "user", "content": [
    {"type": "tool_result", "tool_use_id": "toolu_helper", "content": "a.txt"}
]})
(root / "worked").touch()
for _ in range(1500):
    if (root / "finish").exists():
        break
    time.sleep(0.02)
send("assistant", message={"id": "msg_final", "role": "assistant", "content": [
    {"type": "text", "text": "One file."}
]})
send("stream_event", event={"type": "message_stop"})
send("result", subtype="success", is_error=False, result="One file.")
"#;

/// The view of the row of a chat with this text.
fn view_of(rows: &[Value], text: &str) -> Value {
    let row = rows
        .iter()
        .find(|row| row["text"] == text)
        .unwrap_or_else(|| panic!("no row says {text}: {rows:?}"));
    row["view"].clone()
}

fn texts(page: &Value) -> Vec<String> {
    page["turns"]
        .as_array()
        .unwrap()
        .iter()
        .map(|row| row["text"].as_str().unwrap_or_default().to_owned())
        .collect()
}

#[test]
fn a_helpers_chat_holds_the_turns_other_agents_give_it_and_another_agents_does_not() {
    let place = Place::new("helper-conversation");
    let claude = place.root.join("claude");
    program(&claude, CLAUDE);
    let host = place.start_in("", &[], &[("CLARP_CLAUDE_PROGRAM", claude.to_str().unwrap())]);
    host.post(
        "/agents",
        json!({"name": "Boss", "session": "boss", "backend": "claude"}),
    );
    host.post(
        "/agents",
        json!({"name": "Hal", "session": "hal", "backend": "claude",
               "role": "helper", "parent": "boss"}),
    );
    host.post(
        "/agents",
        json!({"name": "Plain", "session": "plain", "backend": "claude"}),
    );
    let ask = |session: &str| {
        host.post(
            "/send",
            json!({"session": session, "text": "Count the files.",
                   "client_msg_id": format!("ask-{session}"),
                   "origin": "agent", "sender": "boss"}),
        )
    };

    // The helper: its parent asks, and it works.
    ask("hal");
    wait_for("the helper's work", || place.root.join("worked").exists());
    wait_for("the call in the helper's chat", || {
        host.turns("hal")
            .iter()
            .any(|row| row["text"] == "I am reading.")
    });
    let held = host.get("/log?session=hal&view=conversation");
    let cursor = held["latest_revision"].as_u64().unwrap();
    assert_eq!(texts(&held), ["Count the files."], "{held}");
    std::fs::write(place.root.join("finish"), []).unwrap();
    wait_for("the helper's answer", || {
        host.state("hal") == "done"
            && host
                .turns("hal")
                .iter()
                .any(|row| row["text"] == "One file." && row["view"] == "conversation")
    });
    let rows = host.turns("hal");
    // The parent's message and the answer are the conversation; the work is not.
    assert_eq!(view_of(&rows, "Count the files."), json!("conversation"));
    assert_eq!(view_of(&rows, "One file."), json!("conversation"));
    assert_eq!(view_of(&rows, "I am reading."), json!("session"));
    let answer = rows.iter().find(|row| row["text"] == "One file.").unwrap();
    assert!(answer["session_rows"].as_u64().unwrap() >= 1, "{answer}");
    // A reader of the Conversation view that held the message is given the answer.
    let delta = host.get(&format!(
        "/log?session=hal&view=conversation&after_revision={cursor}"
    ));
    assert_eq!(texts(&delta), ["One file."], "{delta}");
    let chat = host.get("/log?session=hal&view=conversation");
    assert_eq!(texts(&chat), ["Count the files.", "One file."], "{chat}");

    // Another agent's chat: the message an agent sent is not the user's conversation.
    std::fs::remove_file(place.root.join("worked")).unwrap();
    ask("plain");
    wait_for("the plain agent's answer", || {
        host.state("plain") == "done"
            && host
                .turns("plain")
                .iter()
                .any(|row| row["text"] == "One file.")
    });
    assert_eq!(
        view_of(&host.turns("plain"), "Count the files."),
        json!("session")
    );
    let chat = host.get("/log?session=plain&view=conversation");
    assert!(
        !texts(&chat).contains(&"Count the files.".to_owned()),
        "{chat}"
    );
}
