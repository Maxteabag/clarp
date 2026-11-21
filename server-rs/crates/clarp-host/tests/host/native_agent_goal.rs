//! The native goal lane through the real Host, actor, supervisor and spool.

use super::*;

const CODEX: &str = r#"#!/usr/bin/env python3
import json
from pathlib import Path
import sys
import threading

root = Path(__file__).resolve().parent
thread = "native-thread"
if sys.argv[1] == "exec":
    sys.stdin.read()
    with (root / "launches").open("a") as f:
        f.write("exec\n")
    print(json.dumps({"type": "thread.started", "thread_id": thread}), flush=True)
    print(json.dumps({"type": "item.completed", "item": {
        "id": "answer", "type": "agent_message", "text": "Ordinary exec reply"
    }}), flush=True)
    print('{"type":"turn.completed"}', flush=True)
    raise SystemExit(0)
assert sys.argv[1:] == ["app-server", "--stdio"], sys.argv
with (root / "launches").open("a") as f:
    f.write("app-server\n")
lock = threading.Lock()
goal = None
sequence = 0
turn = None
cancel = None

def emit(value):
    with lock:
        print(json.dumps(value), flush=True)

def notify(method, **fields):
    emit({"method": method, "params": {"threadId": thread, **fields}})

def finish(turn_id, stopped):
    for _ in range(1000):
        if stopped.wait(.03):
            return
        if (root / "finish-goal").exists():
            break
    else:
        return
    notify("item/completed", turnId=turn_id, item={
        "id": "goal-reply", "type": "agentMessage", "text": "Native goal result"
    })
    goal["status"] = "complete"
    goal["updatedAt"] += 1
    notify("thread/goal/updated", goal=goal)
    notify("turn/completed", turn={"id":turn_id, "status":"completed"})

for line in sys.stdin:
    request = json.loads(line)
    method = request["method"]
    params = request.get("params", {})
    if method == "initialized":
        continue
    request_id = request["id"]
    result = {}
    if method == "thread/resume":
        assert params["threadId"] == thread
        result = {"thread": {"id": thread}}
    elif method == "thread/goal/set":
        if goal is None:
            goal = {"objective": params["objective"], "createdAt": 1795000000, "updatedAt": 1795000000}
        goal["status"] = params["status"]
        goal["updatedAt"] += 1
        result = {"goal": dict(goal)}
    elif method == "thread/goal/get":
        result = {"goal": goal}
    elif method == "turn/interrupt":
        assert params["turnId"] == turn and goal["status"] == "paused"
        cancel.set()
        notify("turn/completed", turn={"id":turn, "status":"interrupted"})
        turn = None
    else:
        assert method == "initialize", method
    emit({"id": request_id, "result": result})
    if method == "thread/goal/set" and goal["status"] == "active":
        assert turn is None, turn
        sequence += 1
        turn = f"goal-attempt-{sequence}"
        cancel = threading.Event()
        (root / "native-turns").write_text(str(sequence))
        notify("turn/started", turn={"id": turn})
        threading.Thread(target=finish, args=(turn, cancel), daemon=True).start()
"#;

#[test]
fn a_native_codex_goal_owns_its_thread_across_host_restart_and_returns_to_exec() {
    let place = Place::new("native-codex-goal");
    let codex = place.root.join("codex");
    program(&codex, CODEX);
    let world = [("CLARP_CODEX_PROGRAM", codex.to_str().unwrap())];
    let host = place.start_in("", &[], &world);
    let agent = host.post(
        "/agents",
        json!({"name":"Ada", "session":"ada", "backend":"codex"}),
    );
    assert_eq!(agent["session"], "ada", "{agent}");
    assert_eq!(host.get("/agent-goal?session=ada")["goal"], Value::Null);
    assert_eq!(
        host.ask(
            "POST",
            "/agent-goal",
            Some(&json!({"session":"ada","objective":"learn"}))
        ),
        (
            400,
            json!({"error":"Send the agent a message first; a goal needs a conversation to run in."})
        )
    );
    assert_eq!(
        host.ask(
            "POST",
            "/agent-goal",
            Some(&json!({"session":"ada","objective":"  "}))
        ),
        (400, json!({"error":"objective required"}))
    );
    host.send("ada", "open a Codex thread", "open");
    wait_for("the opener to finish", || host.state("ada") == "done");

    let started = host.post(
        "/agent-goal",
        json!({"session":"ada", "objective":"Finish the proof"}),
    );
    assert_eq!(
        started["goal"]["objective"], "Finish the proof",
        "{started}"
    );
    assert_eq!(started["goal"]["status"], "active", "{started}");
    wait_for("the native goal turn to appear busy", || {
        host.state("ada") == "thinking"
    });
    assert_eq!(
        std::fs::read_to_string(place.root.join("launches")).unwrap(),
        "exec\napp-server\n"
    );

    host.post("/stop", json!({"session":"ada"}));
    wait_for(
        "Stop to pause the native goal and interrupt its turn",
        || {
            host.get("/agent-goal?session=ada")["goal"]["status"] == "paused"
                && host.state("ada") == "interrupted"
        },
    );
    assert_eq!(
        std::fs::read_to_string(place.root.join("native-turns")).unwrap(),
        "1"
    );
    host.post("/turn-queue/resume", json!({"session":"ada"}));
    let resumed = host.post("/agent-goal/resume", json!({"session":"ada"}));
    assert_eq!(resumed["goal"]["status"], "active", "{resumed}");
    wait_for("Codex's second native turn", || {
        std::fs::read_to_string(place.root.join("native-turns")).unwrap() == "2"
            && host.state("ada") == "thinking"
    });

    host.kill();
    let host = place.start_in("", &[], &world);
    let snapshot = host.get("/agents/snapshot");
    let row = snapshot["agents"]
        .as_array()
        .unwrap()
        .iter()
        .find(|row| row["session"] == "ada")
        .unwrap();
    assert_eq!(row["goal"]["status"], "active");
    std::fs::write(place.root.join("finish-goal"), "").unwrap();
    let deadline = Instant::now() + Duration::from_secs(30);
    loop {
        let goal = host.get("/agent-goal?session=ada");
        let state = host.state("ada");
        if goal["goal"]["status"] == "complete" && state == "done" {
            break;
        }
        assert!(
            Instant::now() < deadline,
            "the goal did not finish: goal={goal}, state={state}, turns={:?}, lane={}{}",
            host.turns("ada"),
            lane_at(&place, row["agent_id"].as_str().unwrap()),
            what_the_hosts_said(),
        );
        std::thread::sleep(Duration::from_millis(50));
    }
    let conversation = host.conversation("ada");
    let user: Vec<_> = conversation
        .iter()
        .filter(|(role, _)| role == "user")
        .map(|(_, text)| text.as_str())
        .collect();
    assert_eq!(user, ["open a Codex thread"], "{conversation:?}");
    assert!(conversation.contains(&said("assistant", "Ordinary exec reply")));
    assert_eq!(
        conversation
            .iter()
            .filter(|entry| **entry == said("assistant", "Native goal result"))
            .count(),
        1
    );
    host.send("ada", "ordinary turn after completion", "after");
    wait_for("exec to regain the thread", || {
        std::fs::read_to_string(place.root.join("launches")).unwrap() == "exec\napp-server\nexec\n"
            && host.state("ada") == "done"
    });
}

fn lane_at(place: &Place, agent: &str) -> String {
    use std::os::unix::net::UnixStream;

    let socket = place.root.join("run").join(format!("{agent}-cxg.sock"));
    let inspect = || -> std::io::Result<String> {
        let mut stream = UnixStream::connect(socket)?;
        stream.set_read_timeout(Some(Duration::from_secs(1)))?;
        stream.write_all(b"{\"op\":\"inspect\"}\n")?;
        let mut answer = String::new();
        BufReader::new(stream).read_line(&mut answer)?;
        Ok(answer)
    };
    format!("{:?}", inspect())
}
