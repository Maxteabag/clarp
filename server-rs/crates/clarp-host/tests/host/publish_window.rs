//! Issue 831. A finished reply reached the apps 16 seconds late on a disk where one sync took
//! seconds: the agent's thread read the turn's whole spool, a commit for each window, and
//! started the turn of the message that waited, before it told the apps anything. Here each
//! commit to an agent's log takes a second, and a Codex lane turn runs as the issue's run did:
//! a turn that runs a command and answers SKUNK, a message held behind it, and words sent
//! while it runs that are steered into it. Once the turn goes on, its command, the command's
//! end, its answer and its end are in the spool together: several windows for one reading.
//! Each window is told to the apps once its own commit is made, the command's a commit or more
//! before the answer's, where before all of them went out together once the reading was over;
//! and the turn of the message that waited is told after the answer.

use super::*;

/// A stand-in Codex lane. The turn of `[h1]` starts, says so in the file `started`, then waits
/// for the file `release`; then it
/// runs a command, answers SKUNK and ends, all at once. Words steered into it are read. Any
/// other turn waits for the file `release-next`, and answers TAPIR.
const CODEX: &str = r#"#!/usr/bin/env python3
import json
from pathlib import Path
import sys
import threading
import time

root = Path(__file__).resolve().parent
thread = "lane-thread"
lock = threading.Lock()
current = None
count = 0

def emit(value):
    with lock:
        print(json.dumps(value), flush=True)

def notify(method, **fields):
    emit({"method": method, "params": {"threadId": thread, **fields}})

def wait_for(name):
    end = time.monotonic() + 90
    while not (root / name).exists() and time.monotonic() < end:
        time.sleep(.02)

def first(turn_id):
    global current
    (root / "started").write_text(turn_id)
    wait_for("release")
    command = {"id": "cmd-1", "type": "commandExecution", "command": "sleep 15",
               "status": "inProgress"}
    notify("item/started", turnId=turn_id, item=command)
    notify("item/completed", turnId=turn_id, item={**command, "status": "completed",
           "aggregatedOutput": "", "exitCode": 0})
    current = None
    notify("item/completed", turnId=turn_id, item={
        "id": "a-" + turn_id, "type": "agentMessage", "text": "SKUNK"})
    notify("turn/completed", turn={"id": turn_id, "status": "completed"})

def next_turn(turn_id):
    global current
    wait_for("release-next")
    current = None
    notify("item/completed", turnId=turn_id, item={
        "id": "a-" + turn_id, "type": "agentMessage", "text": "TAPIR"})
    notify("turn/completed", turn={"id": turn_id, "status": "completed"})

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
    elif method in ("thread/start", "thread/resume"):
        result = {"thread": {"id": thread}}
    elif method == "turn/start":
        text = params["input"][0]["text"]
        count += 1
        turn_id = f"t{count}"
        current = turn_id
        result = {"turn": {"id": turn_id}}
        def after():
            notify("turn/started", turn={"id": turn_id})
            run = first if "[h1]" in text else next_turn
            threading.Thread(target=run, args=(turn_id,), daemon=True).start()
    elif method == "thread/backgroundTerminals/clean":
        pass
    elif method == "turn/steer":
        if current is None or params["expectedTurnId"] != current:
            emit({"id": request_id, "error": {"code": -32600, "message": "no active turn to steer"}})
            continue
        (root / "steered").write_text(params["clientUserMessageId"])
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

/// How long each commit to an agent's log takes here.
const COMMIT_MS: u64 = 1_000;

/// The Host's events as they come, each with when it came.
fn listen_timed(host: &Host) -> Arc<Mutex<Vec<(Instant, Value)>>> {
    let heard = Arc::new(Mutex::new(Vec::new()));
    let mut stream = TcpStream::connect(&host.address).unwrap();
    stream
        .write_all(b"GET /events HTTP/1.1\r\nHost: test\r\nAccept: text/event-stream\r\n\r\n")
        .unwrap();
    let collected = heard.clone();
    std::thread::spawn(move || {
        for line in BufReader::new(stream).lines() {
            let Ok(line) = line else { break };
            if let Some(event) = line
                .strip_prefix("data: ")
                .or_else(|| line.strip_prefix("data:"))
                .and_then(|data| serde_json::from_str::<Value>(data).ok())
            {
                collected.lock().unwrap().push((Instant::now(), event));
            }
        }
    });
    heard
}

#[test]
fn a_finished_reply_is_told_before_the_turn_after_it_starts_on_a_slow_disk() {
    let place = Place::new("publish-window");
    let codex = place.root.join("codex");
    program(&codex, CODEX);
    let config = place.root.join("config");
    std::fs::create_dir_all(config.join("clarp-host")).unwrap();
    std::fs::write(
        config.join("clarp-host/config.toml"),
        "codex_chat_lane = true\ncodex_lane_idle_minutes = 15\n",
    )
    .unwrap();
    let commit_ms = COMMIT_MS.to_string();
    let world = [
        ("CLARP_CODEX_PROGRAM", codex.to_str().unwrap()),
        ("XDG_CONFIG_HOME", config.to_str().unwrap()),
        ("CLARP_TEST_APPEND_DELAY_MS", commit_ms.as_str()),
    ];
    let host = place.start_in("", &[], &world);
    let made = host.post(
        "/agents",
        json!({"name":"Lane", "session":"lane", "backend":"codex"}),
    );
    assert_eq!(made["session"], "lane", "{made}");
    let heard = listen_timed(&host);
    let users = || {
        host.turns("lane")
            .into_iter()
            .filter(|row| row["role"] == "user")
            .map(|row| row["id"].as_str().unwrap().to_owned())
            .collect::<Vec<_>>()
    };

    // [h1] runs; [h2] is held behind it; [h3], sent while it runs, is steered into it.
    host.send("lane", "[h1] run sleep 15, then reply SKUNK", "h1");
    wait_long(&host, "lane", "the first turn to run", || {
        place.root.join("started").exists() && users() == ["u-h1"]
    });
    let held = host.post(
        "/send",
        json!({"session": "lane", "text": "[h2] reply TAPIR", "client_msg_id": "h2",
               "queue_if_busy": true}),
    );
    assert_eq!(held["queued"], true, "{held}");
    host.send("lane", "[h3] also put VIPER in your reply", "h3");
    wait_long(&host, "lane", "the steered words", || {
        place.root.join("steered").exists() && users() == ["u-h1", "u-h3"]
    });

    // The turn goes on: its command, the command's end, its answer and its end, at once.
    let released = Instant::now();
    std::fs::write(place.root.join("release"), "").unwrap();
    wait_long(&host, "lane", "the held message's turn", || {
        users() == ["u-h1", "u-h3", "u-h2"]
    });
    std::fs::write(place.root.join("release-next"), "").unwrap();
    wait_long(&host, "lane", "the held message's answer", || {
        host.conversation("lane")
            .iter()
            .any(|(role, said)| role == "assistant" && said == "TAPIR")
    });

    // What the apps were told after the turn went on, in order, each with when.
    let heard = heard.lock().unwrap();
    let told_after: Vec<&(Instant, Value)> =
        heard.iter().filter(|(at, _)| *at >= released).collect();
    let told = || {
        told_after
            .iter()
            .map(|(at, event)| {
                let cut: String = event.to_string().chars().take(200).collect();
                format!("{:>6} ms {cut}", at.duration_since(released).as_millis())
            })
            .collect::<Vec<_>>()
            .join("\n")
    };
    let first = |what: &str, is: &dyn Fn(&Value) -> bool| {
        told_after
            .iter()
            .position(|(_, event)| is(event))
            .unwrap_or_else(|| panic!("{what} was told:\n{}", told()))
    };
    let command = first("the command", &|event| {
        event["type"] == "agent-activity" && event["kind"] == "tool"
    });
    let ended = first("the end of the first turn", &|event| {
        event["type"] == "agent-state" && event["kind"] == "done"
    });
    // The command's window was told once it was on disk, while the windows after it (the
    // command's end, the answer and the turn's end) were still to be committed: a commit or more
    // before the turn's end, where before all of it went out together once the reading was over.
    let ahead = told_after[ended]
        .0
        .saturating_duration_since(told_after[command].0);
    assert!(
        ahead >= Duration::from_millis(COMMIT_MS / 2),
        "the command was told {} ms before the turn's end, not a commit ahead of it:\n{}",
        ahead.as_millis(),
        told()
    );
    // The turn of the message that waited was told after the end of the turn before it.
    let next = told_after
        .iter()
        .skip(ended)
        .position(|(_, event)| event["type"] == "agent-state" && event["kind"] == "thinking");
    assert!(
        next.is_some(),
        "the next turn was told after the end:\n{}",
        told()
    );
    // The answer is the first turn's, and the held message's turn answered it after.
    let said: Vec<String> = host
        .conversation("lane")
        .into_iter()
        .filter(|(role, _)| role == "assistant")
        .map(|(_, said)| said)
        .collect();
    let at = |word: &str| said.iter().position(|said| said == word);
    assert!(
        matches!((at("SKUNK"), at("TAPIR")), (Some(skunk), Some(tapir)) if skunk < tapir),
        "{said:?}"
    );
}
