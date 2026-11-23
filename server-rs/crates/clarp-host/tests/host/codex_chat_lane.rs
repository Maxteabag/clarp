//! Issue 742. With the owner's setting on, a Codex agent's chat turns run on one lane: a
//! message sent while a turn runs is steered into it and enters the conversation when Codex
//! reads it, the next turn reuses the lane, and its tools find their turn's identity in the
//! lane's turn file. With the setting off again, the lane leaves and exec runs the same thread.

use super::*;

const CODEX: &str = r#"#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys
import threading
import time

root = Path(__file__).resolve().parent
thread = "lane-thread"

def note(name, text):
    with (root / name).open("a") as f:
        f.write(text + "\n")

if sys.argv[1] == "exec":
    prompt = sys.stdin.read()
    note("launches", "exec")
    asked = sys.argv[sys.argv.index("resume") + 1] if "resume" in sys.argv else "none"
    note("exec-threads", asked)
    word = "after-rollback" if "after-rollback" in prompt else "other"
    print(json.dumps({"type": "thread.started", "thread_id": asked}), flush=True)
    print(json.dumps({"type": "item.completed", "item": {
        "id": "answer", "type": "agent_message", "text": "exec-" + word
    }}), flush=True)
    print('{"type":"turn.completed"}', flush=True)
    raise SystemExit(0)

assert sys.argv[1:] == ["app-server", "--stdio"], sys.argv
note("launches", "app-server")
note("lane-env", "turn=" + ("set" if os.environ.get("CLARP_PROVIDER_TURN") else "absent"))
token_file = os.environ.get("CLARP_PROVIDER_TURN_FILE", "")
lock = threading.Lock()
current = None
count = 0

def emit(value):
    with lock:
        print(json.dumps(value), flush=True)

def notify(method, **fields):
    emit({"method": method, "params": {"threadId": thread, **fields}})

def answer(turn_id, text):
    notify("item/completed", turnId=turn_id, item={
        "id": "a-" + turn_id, "type": "agentMessage", "text": text})
    notify("turn/completed", turn={"id": turn_id, "status": "completed"})

def held(turn_id):
    global current
    end = time.monotonic() + 40
    while not (root / "release").exists() and time.monotonic() < end:
        time.sleep(.02)
    current = None
    answer(turn_id, "answered-hold")

for line in sys.stdin:
    request = json.loads(line)
    method = request["method"]
    params = request.get("params", {})
    if method == "initialized":
        continue
    request_id = request["id"]
    result = {}
    after = None
    if method == "initialize":
        pass
    elif method == "thread/start":
        assert "threadId" not in params, params
        note("threads", "start")
        result = {"thread": {"id": thread}}
    elif method == "thread/resume":
        assert params["threadId"] == thread, params
        note("threads", "resume")
        result = {"thread": {"id": thread}}
    elif method == "turn/start":
        text = params["input"][0]["text"]
        count += 1
        turn_id = f"t{count}"
        current = turn_id
        word = next(w for w in ("first-message", "hold-message", "next-message") if w in text)
        note("turns", word)
        note("tokens", Path(token_file).read_text() if token_file else "")
        result = {"turn": {"id": turn_id}}
        def after():
            global current
            notify("turn/started", turn={"id": turn_id})
            if word == "hold-message":
                (root / "holding").write_text("")
                threading.Thread(target=held, args=(turn_id,), daemon=True).start()
            else:
                current = None
                answer(turn_id, "reply-" + word)
    elif method == "turn/steer":
        if current is None or params["expectedTurnId"] != current:
            emit({"id": request_id, "error": {"code": -32600, "message": "no active turn to steer"}})
            continue
        note("steered", params["clientUserMessageId"] + " " + params["input"][0]["text"])
        result = {"turnId": current}
        steered = (current, params["clientUserMessageId"])
        def after():
            notify("item/started", turnId=steered[0], item={
                "id": "u-" + steered[1], "type": "userMessage",
                "clientId": steered[1], "content": []})
    else:
        raise SystemExit("unexpected " + method)
    emit({"id": request_id, "result": result})
    if after:
        after()
"#;

#[test]
fn codex_chat_turns_run_on_one_lane_and_take_steered_words() {
    let place = Place::new("codex-chat-lane");
    let codex = place.root.join("codex");
    program(&codex, CODEX);
    let config = place.root.join("config");
    std::fs::create_dir_all(config.join("clarp-host")).unwrap();
    let setting = |on: bool| {
        std::fs::write(
            config.join("clarp-host/config.toml"),
            format!("codex_chat_lane = {on}\ncodex_lane_idle_minutes = 15\n"),
        )
        .unwrap();
    };
    setting(true);
    let world = [
        ("CLARP_CODEX_PROGRAM", codex.to_str().unwrap()),
        ("XDG_CONFIG_HOME", config.to_str().unwrap()),
    ];
    let host = place.start_in("", &[], &world);
    let made = host.post(
        "/agents",
        json!({"name":"Lane", "session":"lane", "backend":"codex"}),
    );
    assert_eq!(made["session"], "lane", "{made}");
    let read = |name: &str| std::fs::read_to_string(place.root.join(name)).unwrap_or_default();
    let answered = |text: &str| {
        wait_for(text, || {
            host.conversation("lane")
                .iter()
                .any(|(role, said)| role == "assistant" && said == text)
        });
    };
    let users = || {
        host.turns("lane")
            .into_iter()
            .filter(|row| row["role"] == "user")
            .map(|row| row["id"].as_str().unwrap().to_owned())
            .collect::<Vec<_>>()
    };

    // A new conversation: the lane starts its thread, and no exec runs.
    host.send("lane", "first-message", "first");
    answered("reply-first-message");
    wait_for("the first turn to end", || host.state("lane") == "done");
    assert_eq!(read("launches"), "app-server\n");
    assert_eq!(read("threads"), "start\n");
    assert_eq!(read("lane-env"), "turn=absent\n");

    // Words sent while a turn runs reach it, and enter the conversation when Codex reads them.
    host.send("lane", "hold-message", "hold");
    wait_for("the held turn", || place.root.join("holding").exists());
    let sent = host.send("lane", "more-words", "more");
    assert_eq!(sent["queued"], true, "{sent}");
    wait_for("the steer", || read("steered").contains("more-words"));
    wait_for("the steered words in the conversation", || {
        users() == ["u-first", "u-hold", "u-more"]
    });
    std::fs::write(place.root.join("release"), "").unwrap();
    answered("answered-hold");
    wait_for("the held turn to end", || host.state("lane") == "done");
    assert_eq!(users(), ["u-first", "u-hold", "u-more"]);
    assert_eq!(
        read("turns"),
        "first-message\nhold-message\n",
        "the steered words never ran as a turn of their own"
    );

    // The next turn reuses the lane. Each turn's tools found their own identity.
    host.send("lane", "next-message", "next");
    answered("reply-next-message");
    wait_for("the third turn to end", || host.state("lane") == "done");
    assert_eq!(read("launches"), "app-server\n");
    let tokens: Vec<String> = read("tokens").lines().map(str::to_owned).collect();
    assert_eq!(tokens.len(), 3, "{tokens:?}");
    assert!(tokens.iter().all(|token| !token.is_empty()), "{tokens:?}");
    assert_ne!(tokens[0], tokens[1], "each turn has its own identity");

    // Rolled back: the lane leaves, and exec continues the same thread.
    setting(false);
    host.send("lane", "after-rollback", "after");
    answered("exec-after-rollback");
    assert_eq!(read("launches"), "app-server\nexec\n");
    assert_eq!(read("exec-threads"), "lane-thread\n");
}
