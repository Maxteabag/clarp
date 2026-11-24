//! A Codex goal whose agent's folder is gone (issue 758): it does not go on by itself, and
//! starts no turn while the folder is gone, so nothing fails over and over. It goes on by
//! itself once the folder is back.

use super::*;

/// A stand-in Codex. Exec answers at once. On the app-server, a goal set active starts a
/// turn, numbered in `native-turns`; the turn completes when the file `finish-turn` is there,
/// which it takes away, and the goal stays active, so the Host goes on with it.
const CODEX: &str = r#"#!/usr/bin/env python3
import json
from pathlib import Path
import sys
import threading

root = Path(__file__).resolve().parent
thread = "native-thread"
if sys.argv[1] == "exec":
    sys.stdin.read()
    print(json.dumps({"type": "thread.started", "thread_id": thread}), flush=True)
    print(json.dumps({"type": "item.completed", "item": {
        "id": "answer", "type": "agent_message", "text": "Opened"
    }}), flush=True)
    print('{"type":"turn.completed"}', flush=True)
    raise SystemExit(0)
assert sys.argv[1:] == ["app-server", "--stdio"], sys.argv
lock = threading.Lock()
state = {"goal": None, "turn": None, "sequence": 0}

def emit(value):
    with lock:
        print(json.dumps(value), flush=True)

def notify(method, **fields):
    emit({"method": method, "params": {"threadId": thread, **fields}})

def finish(turn_id, stopped):
    done = root / "finish-turn"
    while not stopped.wait(.03):
        try:
            done.unlink()
        except FileNotFoundError:
            continue
        state["turn"] = None
        notify("item/completed", turnId=turn_id, item={
            "id": turn_id + "-reply", "type": "agentMessage", "text": "Worked on " + turn_id
        })
        notify("turn/completed", turn={"id": turn_id, "status": "completed"})
        return

cancel = None
for line in sys.stdin:
    request = json.loads(line)
    method = request["method"]
    params = request.get("params", {})
    if method == "initialized":
        continue
    request_id = request["id"]
    result = {}
    goal = state["goal"]
    if method == "thread/resume":
        result = {"thread": {"id": thread}}
    elif method == "thread/goal/set":
        if goal is None:
            goal = {"objective": params["objective"], "createdAt": 1795000000, "updatedAt": 1795000000}
            state["goal"] = goal
        goal["status"] = params["status"]
        goal["updatedAt"] += 1
        result = {"goal": dict(goal)}
    elif method == "thread/goal/get":
        result = {"goal": goal}
    elif method == "thread/backgroundTerminals/clean":
        pass
    elif method == "turn/interrupt":
        if cancel is not None:
            cancel.set()
        notify("turn/completed", turn={"id": state["turn"], "status": "interrupted"})
        state["turn"] = None
    else:
        assert method == "initialize", method
    emit({"id": request_id, "result": result})
    if method == "thread/goal/set" and goal["status"] == "active" and state["turn"] is None:
        state["sequence"] += 1
        turn = "goal-turn-" + str(state["sequence"])
        state["turn"] = turn
        cancel = threading.Event()
        (root / "native-turns").write_text(str(state["sequence"]))
        notify("turn/started", turn={"id": turn})
        threading.Thread(target=finish, args=(turn, cancel), daemon=True).start()
"#;

#[test]
fn a_goal_whose_folder_is_gone_waits_without_a_turn_and_goes_on_once_it_is_back() {
    let place = Place::new("native-goal-folder");
    let codex = place.root.join("codex");
    program(&codex, CODEX);
    let world = [("CLARP_CODEX_PROGRAM", codex.to_str().unwrap())];
    let host = place.start_in("", &[], &world);
    let folder = place.root.join("helper-worktree");
    std::fs::create_dir_all(&folder).unwrap();
    let made = host.post(
        "/agents",
        json!({"name": "Ada", "session": "ada", "backend": "codex", "cwd": folder}),
    );
    assert_eq!(made["session"], "ada", "{made}");
    host.send("ada", "open a Codex thread", "open");
    wait_for("the opener to finish", || host.state("ada") == "done");
    let turns_of_the_goal =
        || std::fs::read_to_string(place.root.join("native-turns")).unwrap_or_default();

    let started = host.post(
        "/agent-goal",
        json!({"session": "ada", "objective": "Go on"}),
    );
    assert_eq!(started["goal"]["status"], "active", "{started}");
    wait_for("the goal's first turn", || {
        turns_of_the_goal() == "1" && host.state("ada") == "thinking"
    });

    // The folder goes, and then the goal's turn completes with the goal still active. The
    // goal does not go on: no turn starts, and none fails.
    std::fs::remove_dir_all(&folder).unwrap();
    std::fs::write(place.root.join("finish-turn"), "").unwrap();
    wait_for("the first turn to complete", || {
        host.conversation("ada")
            .contains(&said("assistant", "Worked on goal-turn-1"))
            && host.state("ada") == "done"
    });
    let rows_before = host.turns("ada").len();
    std::thread::sleep(Duration::from_secs(8));
    assert_eq!(turns_of_the_goal(), "1", "no turn while the folder is gone");
    let rows = host.turns("ada");
    assert_eq!(rows.len(), rows_before, "nothing was tried: {rows:#?}");
    assert!(
        !rows.iter().any(|row| row["text"]
            .as_str()
            .is_some_and(|text| text.contains("never run"))),
        "{rows:#?}"
    );
    assert_eq!(
        host.get("/agent-goal?session=ada")["goal"]["status"],
        "active"
    );

    // The folder is back: the goal goes on by itself.
    std::fs::create_dir_all(&folder).unwrap();
    wait_for("the goal's next turn", || turns_of_the_goal() == "2");
}
