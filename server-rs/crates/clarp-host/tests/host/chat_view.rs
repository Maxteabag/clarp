use super::*;

const CLAUDE: &str = r#"#!/usr/bin/env python3
import json
import pathlib
import sys
import time

if "--version" in sys.argv:
    print("2.1.320 (Claude Code)")
    raise SystemExit(0)

root = pathlib.Path(__file__).resolve().parent
incoming = json.loads(sys.stdin.readline())
prompt = incoming["message"]["content"][0]["text"]
with (root / "prompts.jsonl").open("a") as lines:
    lines.write(json.dumps(prompt) + "\n")

def send(kind, **fields):
    print(json.dumps({"type": kind, "session_id": "view-fixture", **fields}), flush=True)

send("system", subtype="init")
send("assistant", message={"id": "msg_work", "role": "assistant", "content": [
    {"type": "text", "text": "I am reading."},
    {"type": "tool_use", "id": "toolu_view", "name": "Bash", "input": {"command": "ls"}}
]})
send("user", message={"role": "user", "content": [
    {"type": "tool_result", "tool_use_id": "toolu_view", "content": "a.txt"}
]})
send("assistant", message={"id": "msg_final", "role": "assistant", "content": [
    {"type": "text", "text": "One file."}
]})
for _ in range(500):
    if (root / "finish").exists():
        break
    time.sleep(0.02)
send("result", subtype="success", is_error=False, result="One file.")
"#;

#[test]
fn chat_views_keep_the_settled_reply_and_remember_how_the_user_reads() {
    let place = Place::new("chat-view");
    let claude = place.root.join("claude");
    program(&claude, CLAUDE);
    let world = [("CLARP_CLAUDE_PROGRAM", claude.to_str().unwrap())];
    let start = || place.start_in("", &[], &world);
    let host = start();
    let made = host.post("/agents", json!({"name": "Reader", "backend": "claude"}));
    let session = made["session"].as_str().unwrap();
    let view = |host: &Host| {
        host.get("/agents/snapshot")["agents"]
            .as_array()
            .unwrap()
            .iter()
            .find(|row| row["session"] == session)
            .unwrap()["chat_view"]
            .clone()
    };
    assert_eq!(view(&host), "session");
    let set = json!({"session": session, "chat_view": "Conversation"});
    for header in ["X-Clarp-Turn", "X-Clarp-Peer"] {
        let refused = fetch(
            &host,
            "",
            "POST",
            "/agent-chat-view",
            &[(header, "named"), ("Content-Type", "application/json")],
            set.to_string().as_bytes(),
        );
        assert_eq!(refused.status, 403);
        assert_eq!(
            serde_json::from_slice::<Value>(&refused.body).unwrap()["error"],
            "agents_may_not"
        );
    }
    let events = host.listen();
    assert_eq!(
        host.post("/agent-chat-view", set),
        json!({"ok": true, "session": session, "chat_view": "conversation"})
    );
    assert_eq!(view(&host), "conversation");
    wait_for("the view change on the roster", || {
        events.lock().unwrap().iter().any(|event| {
            event["type"] == "agent-roster"
                && event["kind"] == "chat_view"
                && event["session"] == session
                && event["chat_view"] == "conversation"
        })
    });

    let path = format!("/log?session={session}");
    host.send(session, "What is here?", "chat-view");
    wait_for("assistant work while the turn is open", || {
        host.turns(session)
            .iter()
            .any(|row| row["text"] == "One file.")
    });
    let running = host.get(&path);
    let shown = running["turns"].as_array().unwrap();
    assert!(
        shown
            .iter()
            .any(|row| row["text"] == "One file." && row["view"] == "session")
    );
    let rev = running["latest_revision"].as_u64().unwrap();
    std::fs::write(place.root.join("finish"), []).unwrap();
    wait_for("the finished chat turn", || host.state(session) == "done");

    let session_log = host.get(&format!("{path}&view=session"));
    let rows = session_log["turns"].as_array().unwrap();
    let omitted = rows.iter().filter(|row| row["view"] == "session").count();
    assert!(omitted > 0);
    let chat = host.get(&format!("{path}&view=conversation&limit=2"));
    assert_eq!(chat["view"], "conversation");
    let rows = chat["turns"].as_array().unwrap();
    assert_eq!(rows.len(), 2);
    assert!(
        rows.iter()
            .all(|row| row["view"] == "conversation" && row["audience"] == "user")
    );
    assert_eq!(rows[0]["text"], "What is here?");
    assert_eq!(rows[1]["text"], "One file.");
    assert_eq!(rows[1]["session_rows"], omitted);
    let delta = host.get(&format!("{path}&view=conversation&after_revision={rev}"));
    assert!(delta["turns"].as_array().unwrap().iter().any(|row| {
        row["id"] == rows[1]["id"]
            && row["view"] == "conversation"
            && row["session_rows"] == omitted
    }));
    let prompts = std::fs::read_to_string(place.root.join("prompts.jsonl")).unwrap();
    let prompt: String = serde_json::from_str(prompts.lines().next().unwrap()).unwrap();
    assert!(prompt.contains("Your final reply reaches them"));
    assert!(prompt.contains(&format!("clarp-admin tell-user --from {session}")));
    host.kill();
    let restarted = start();
    assert_eq!(view(&restarted), "conversation");
}
