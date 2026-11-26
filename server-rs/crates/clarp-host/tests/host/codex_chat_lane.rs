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
    elif method == "thread/backgroundTerminals/clean":
        note("cleaned", thread)
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

    // The next turn reuses the lane. Each turn's tools found their own identity, and what
    // Codex kept for each turn ended with it.
    host.send("lane", "next-message", "next");
    answered("reply-next-message");
    wait_for("the third turn to end", || host.state("lane") == "done");
    wait_for("each turn's end to clean its commands", || {
        read("cleaned").lines().count() == 3
    });
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

/// A stand-in Codex for the lane's life: Stop, a Host restart, idle time, the cap, a changed
/// login and a changed model. A word `ask-<x>` is answered `said-ask-<x>` at once; a word
/// `hold-<x>` is answered `said-hold-<x>` once the file `release-<x>` exists, unless the turn
/// is interrupted first. A word `hold-cmd<x>` also starts a long command in a session of its
/// own, as Codex runs a terminal command: an interrupt leaves it running, as Codex leaves a
/// background terminal, and `thread/backgroundTerminals/clean` ends it. While the file `deaf`
/// exists, Codex answers an interrupt but neither ends the turn nor cleans anything. What it
/// was asked is noted in files beside it.
const LANE_CODEX: &str = r#"#!/usr/bin/env python3
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time

root = Path(__file__).resolve().parent

def note(name, text):
    with (root / name).open("a") as f:
        f.write(text + "\n")

if sys.argv[1] == "exec":
    note("launches", "exec")
    sys.stdin.read()
    print('{"type":"turn.completed"}', flush=True)
    raise SystemExit(0)

assert sys.argv[1:] == ["app-server", "--stdio"], sys.argv
note("launches", "app-server")
# A test of the memory budget gives each app-server a known weight, in memory it touched, before
# its lane weighs itself.
weight = root / "weight-mb"
ballast = b"\x01" * (int(weight.read_text()) << 20) if weight.exists() else b""
out = threading.Lock()
state = threading.Lock()
thread = None
count = 0
running = {}
terminals = []

def emit(value):
    with out:
        print(json.dumps(value), flush=True)

def notify(method, **fields):
    emit({"method": method, "params": {"threadId": thread, **fields}})

def finish(turn_id, word):
    notify("item/completed", turnId=turn_id, item={
        "id": "a-" + turn_id, "type": "agentMessage", "text": "said-" + word})
    notify("turn/completed", turn={"id": turn_id, "status": "completed"})
    note("answered", word)

def held(turn_id, word, stopped):
    end = time.monotonic() + 60
    release = root / ("release-" + word.removeprefix("hold-"))
    while not release.exists() and time.monotonic() < end:
        if stopped.wait(.02):
            return
    with state:
        if running.pop(turn_id, None) is None:
            return
    finish(turn_id, word)

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
        thread = "thread-" + str(os.getpid())
        note("threads", f"start {thread} model={params.get('model')}")
        result = {"thread": {"id": thread}}
    elif method == "thread/resume":
        thread = params["threadId"]
        note("threads", f"resume {thread} model={params.get('model')}")
        result = {"thread": {"id": thread}}
    elif method == "turn/start":
        text = params["input"][0]["text"]
        word = re.search(r"\b(?:ask|hold)-[a-z0-9]+", text).group(0)
        count += 1
        turn_id = f"t{count}"
        note("turns", word)
        note("turn-models", f"{word} {params.get('model')} {params.get('effort')}")
        result = {"turn": {"id": turn_id}}
        stopped = threading.Event()
        with state:
            running[turn_id] = stopped
        def after():
            notify("turn/started", turn={"id": turn_id})
            if word.startswith("hold-cmd"):
                command = subprocess.Popen(["sleep", "30"], start_new_session=True)
                terminals.append(command)
                (root / ("command-" + word.removeprefix("hold-"))).write_text(str(command.pid))
            if word.startswith("hold-"):
                (root / ("holding-" + word.removeprefix("hold-"))).write_text("")
                threading.Thread(target=held, args=(turn_id, word, stopped), daemon=True).start()
            else:
                with state:
                    running.pop(turn_id, None)
                finish(turn_id, word)
    elif method == "turn/interrupt":
        turn_id = params["turnId"]
        note("interrupts", turn_id)
        if (root / "deaf").exists():
            emit({"id": request_id, "result": {}})
            continue
        with state:
            stopped = running.pop(turn_id, None)
        if stopped is not None:
            stopped.set()
            def after():
                notify("turn/completed", turn={"id": turn_id, "status": "interrupted"})
    elif method == "turn/steer":
        emit({"id": request_id, "error": {"code": -32600, "message": "no active turn to steer"}})
        continue
    elif method == "thread/turns/list":
        result = {"data": []}
    elif method == "thread/backgroundTerminals/clean":
        note("cleaned", params["threadId"])
        if not (root / "deaf").exists():
            for command in terminals:
                os.killpg(command.pid, signal.SIGKILL)
                command.wait()
            terminals.clear()
    else:
        raise SystemExit("unexpected " + method)
    emit({"id": request_id, "result": result})
    if after:
        after()
"#;

/// A place whose Host runs Codex chat turns on lanes with `settings` added to the switch, and
/// whose Codex is [`LANE_CODEX`]. Gives the place and the environment its Host starts with.
pub(super) fn lane_place(name: &str, settings: &str) -> (Place, Vec<(String, String)>) {
    let place = Place::new(name);
    let codex = place.root.join("codex");
    program(&codex, LANE_CODEX);
    let config = place.root.join("config");
    std::fs::create_dir_all(config.join("clarp-host")).unwrap();
    std::fs::write(
        config.join("clarp-host/config.toml"),
        format!("codex_chat_lane = true\n{settings}"),
    )
    .unwrap();
    let world = vec![
        (
            "CLARP_CODEX_PROGRAM".to_owned(),
            codex.to_string_lossy().into_owned(),
        ),
        (
            "XDG_CONFIG_HOME".to_owned(),
            config.to_string_lossy().into_owned(),
        ),
    ];
    (place, world)
}

fn start_lane_host(place: &Place, world: &[(String, String)]) -> Host {
    let world: Vec<(&str, &str)> = world
        .iter()
        .map(|(key, value)| (key.as_str(), value.as_str()))
        .collect();
    place.start_in("", &[], &world)
}

pub(super) fn noted(place: &Place, name: &str) -> String {
    std::fs::read_to_string(place.root.join(name)).unwrap_or_default()
}

/// A new Codex agent, and the socket its lane answers on.
pub(super) fn lane_agent(host: &Host, place: &Place, session: &str) -> std::path::PathBuf {
    let made = host.post(
        "/agents",
        json!({"name": session, "session": session, "backend": "codex"}),
    );
    assert_eq!(made["session"], session, "{made}");
    let id = host.get("/agents/snapshot")["agents"]
        .as_array()
        .unwrap()
        .iter()
        .find(|agent| agent["session"] == session)
        .unwrap()["agent_id"]
        .as_str()
        .unwrap()
        .to_owned();
    place.root.join("run").join(format!("{id}-cxg.sock"))
}

pub(super) fn lane_lives(socket: &std::path::Path) -> bool {
    lane_said(socket, "inspect").is_some_and(|said| said["ok"] == true)
}

/// Asks every lane of a place to leave when the test ends, so none outlives it by its idle
/// time. A lane that still runs a turn refuses, and its stand-in ends the turn within a minute.
pub(super) struct Lanes(pub(super) std::path::PathBuf);

impl Drop for Lanes {
    fn drop(&mut self) {
        let Ok(entries) = std::fs::read_dir(self.0.join("run")) else {
            return;
        };
        for entry in entries.flatten() {
            let path = entry.path();
            if path.to_string_lossy().ends_with("-cxg.sock") {
                let _ = lane_said(&path, "retire");
            }
        }
    }
}

/// How many times an agent's conversation holds an answer.
fn answers(host: &Host, session: &str, text: &str) -> usize {
    host.conversation(session)
        .iter()
        .filter(|(role, said)| role == "assistant" && said == text)
        .count()
}

/// Sends `word` and waits for its answer and the end of its turn.
pub(super) fn asked(host: &Host, session: &str, word: &str) {
    host.send(session, word, word);
    let answer = format!("said-{word}");
    wait_for(&answer, || answers(host, session, &answer) == 1);
    wait_for("the turn to end", || host.state(session) == "done");
}

#[test]
fn a_stop_interrupts_the_lane_turn_at_once_and_the_next_message_runs_on_the_same_lane() {
    let (place, world) = lane_place("codex-lane-stop", "codex_lane_idle_minutes = 5\n");
    let _lanes = Lanes(place.root.clone());
    let host = start_lane_host(&place, &world);
    let socket = lane_agent(&host, &place, "stopper");
    asked(&host, "stopper", "ask-first");

    // Every turn's end cleans what Codex kept for it: the first turn's clean is in before
    // the Stop's is counted.
    wait_for("the first turn's end to clean its commands", || {
        noted(&place, "cleaned").lines().count() == 1
    });
    let cleaned = noted(&place, "cleaned").lines().count();
    host.send("stopper", "hold-stop", "hold");
    wait_for("the held turn", || place.root.join("holding-stop").exists());
    let began = Instant::now();
    host.post("/stop", json!({"session": "stopper"}));
    wait_for("the turn to end interrupted", || {
        host.state("stopper") == "interrupted"
    });
    assert!(
        began.elapsed() < Duration::from_secs(4),
        "Codex answered the interrupt at once, so the turn ended at once: {:?}",
        began.elapsed()
    );
    assert_eq!(
        noted(&place, "interrupts"),
        "t2\n",
        "the held turn was interrupted"
    );
    assert_eq!(answers(&host, "stopper", "said-hold-stop"), 0);
    wait_for("the stopped turn's end to clean its commands", || {
        noted(&place, "cleaned").lines().count() == cleaned + 1
    });
    assert!(
        lane_lives(&socket),
        "a Codex that ended its turn keeps its lane"
    );

    // The next message runs on the same lane.
    asked(&host, "stopper", "ask-after");
    assert_eq!(noted(&place, "launches"), "app-server\n");
    assert_eq!(noted(&place, "turns"), "ask-first\nhold-stop\nask-after\n");
    assert_eq!(noted(&place, "answered"), "ask-first\nask-after\n");
}

#[test]
fn a_lane_turn_held_through_a_host_restart_is_answered_once() {
    let (place, world) = lane_place("codex-lane-restart", "codex_lane_idle_minutes = 5\n");
    let _lanes = Lanes(place.root.clone());
    let host = start_lane_host(&place, &world);
    let socket = lane_agent(&host, &place, "keeper");
    asked(&host, "keeper", "ask-first");
    let user_rows = |host: &Host| {
        host.turns("keeper")
            .into_iter()
            .filter(|row| row["role"] == "user")
            .map(|row| row["id"].as_str().unwrap().to_owned())
            .collect::<Vec<_>>()
    };

    // Codex answers while the Host is away: the answer waits in the spool for the next Host.
    host.send("keeper", "hold-down", "down");
    wait_for("the first held turn", || {
        place.root.join("holding-down").exists()
    });
    host.kill();
    std::fs::write(place.root.join("release-down"), "").unwrap();
    wait_for("Codex to answer while the Host is away", || {
        noted(&place, "answered").contains("hold-down")
    });
    let host = start_lane_host(&place, &world);
    wait_for("the answer given while the Host was away", || {
        answers(&host, "keeper", "said-hold-down") == 1
    });
    wait_for("that turn to end", || host.state("keeper") == "done");

    // Codex answers after the Host is back: the Host rejoined the running turn.
    host.send("keeper", "hold-up", "up");
    wait_for("the second held turn", || {
        place.root.join("holding-up").exists()
    });
    host.kill();
    let host = start_lane_host(&place, &world);
    wait_for("the second turn rejoined", || {
        host.state("keeper") == "thinking"
    });
    std::fs::write(place.root.join("release-up"), "").unwrap();
    wait_for("the answer given after the restart", || {
        answers(&host, "keeper", "said-hold-up") == 1
    });
    wait_for("that turn to end", || host.state("keeper") == "done");

    // The next turn runs on the same lane, and nothing came twice.
    asked(&host, "keeper", "ask-last");
    assert_eq!(
        answers(&host, "keeper", "said-hold-down"),
        1,
        "{:?}",
        host.conversation("keeper")
    );
    assert_eq!(
        answers(&host, "keeper", "said-hold-up"),
        1,
        "{:?}",
        host.conversation("keeper")
    );
    assert_eq!(
        user_rows(&host),
        ["u-ask-first", "u-down", "u-up", "u-ask-last"]
    );
    assert_eq!(
        noted(&place, "turns"),
        "ask-first\nhold-down\nhold-up\nask-last\n",
        "no turn reached Codex twice"
    );
    assert_eq!(
        noted(&place, "launches"),
        "app-server\n",
        "the lane outlived both Hosts and served the next turn"
    );
    assert!(lane_lives(&socket));
}

#[test]
fn an_idle_lane_leaves_after_its_time_and_the_next_turn_starts_another_on_the_same_thread() {
    let (place, world) = lane_place("codex-lane-idle", "codex_lane_idle_minutes = 1\n");
    let _lanes = Lanes(place.root.clone());
    let host = start_lane_host(&place, &world);
    let socket = lane_agent(&host, &place, "idler");
    asked(&host, "idler", "ask-first");
    let seen = lane_said(&socket, "inspect").expect("the lane answers after its turn");
    assert!(seen["state"]["claim"].is_null(), "{seen}");
    // The lane went idle when its turn ended, before the Host showed the turn done: it says
    // for how long.
    let idle_ms = seen["state"]["idle_ms"]
        .as_u64()
        .expect("the lane says how long it is idle");
    let idle_from = Instant::now() - Duration::from_millis(idle_ms);

    wait_within("the idle lane to leave", Duration::from_secs(120), || {
        !lane_lives(&socket)
    });
    assert!(
        idle_from.elapsed() >= Duration::from_secs(59),
        "the lane stayed for its idle time: it left after {:?}",
        idle_from.elapsed()
    );

    asked(&host, "idler", "ask-next");
    assert_eq!(noted(&place, "launches"), "app-server\napp-server\n");
    let threads: Vec<String> = noted(&place, "threads")
        .lines()
        .map(str::to_owned)
        .collect();
    assert_eq!(threads.len(), 2, "{threads:?}");
    let started = threads[0]
        .strip_prefix("start ")
        .and_then(|rest| rest.split(' ').next())
        .unwrap();
    assert!(
        threads[1].starts_with(&format!("resume {started} ")),
        "the new lane continues the conversation's thread: {threads:?}"
    );
}

#[test]
fn a_lane_beyond_the_cap_retires_the_one_idle_longest() {
    // The cap is bytes (`lane_memory_mb`, with the limits fixed). A lane about to start counts at a Codex lane's
    // typical 270 MiB; each running one at what it weighed: its shim, and an app-server holding
    // 50 MiB of ballast (with the interpreters, about 65 to 85 MiB). 270 and one lane fit in
    // 370 MiB, 270 and two do not, whatever the interpreters weigh within 50 MiB of each other.
    let (place, world) = lane_place(
        "codex-lane-cap",
        "codex_lane_idle_minutes = 5\nlane_limits = \"fixed\"\nlane_memory_mb = 370\n",
    );
    std::fs::write(place.root.join("weight-mb"), "50").unwrap();
    let _lanes = Lanes(place.root.clone());
    let host = start_lane_host(&place, &world);
    let a = lane_agent(&host, &place, "cap-a");
    let b = lane_agent(&host, &place, "cap-b");
    let c = lane_agent(&host, &place, "cap-c");

    asked(&host, "cap-a", "ask-a1");
    asked(&host, "cap-b", "ask-b1");
    assert!(
        lane_lives(&a) && lane_lives(&b),
        "two lanes are within the budget"
    );

    // A third: the lane idle longest (a's) leaves, b's stays.
    asked(&host, "cap-c", "ask-c1");
    wait_for("a's lane to leave", || !lane_lives(&a));
    assert!(lane_lives(&b), "b's lane was idle for less time and stays");
    assert!(lane_lives(&c));

    // a again: now b's lane is the one idle longest.
    asked(&host, "cap-a", "ask-a2");
    wait_for("b's lane to leave", || !lane_lives(&b));
    assert!(lane_lives(&a) && lane_lives(&c));
    assert_eq!(noted(&place, "launches").lines().count(), 4);
}

#[test]
fn a_changed_login_sends_the_same_attempt_to_a_new_lane_once() {
    let (place, world) = lane_place("codex-lane-login", "codex_lane_idle_minutes = 5\n");
    let auth = sealed::home(&place.root).join(".codex/auth.json");
    std::fs::create_dir_all(auth.parent().unwrap()).unwrap();
    let account = |id: &str| {
        std::fs::write(&auth, json!({"tokens": {"account_id": id}}).to_string()).unwrap();
    };
    account("account-one");
    let _lanes = Lanes(place.root.clone());
    let host = start_lane_host(&place, &world);
    let socket = lane_agent(&host, &place, "switcher");
    asked(&host, "switcher", "ask-one");
    let first = lane_said(&socket, "inspect").unwrap()["state"]["instance"].clone();

    // The account changes between turns: the lane refuses the next attempt before Codex
    // receives it and leaves, and the Host gives that attempt to a new lane.
    account("account-two");
    asked(&host, "switcher", "ask-two");
    let second = lane_said(&socket, "inspect").unwrap()["state"]["instance"].clone();
    assert_ne!(first, second, "a new lane runs the turn");
    assert_eq!(noted(&place, "launches"), "app-server\napp-server\n");
    assert_eq!(
        noted(&place, "turns"),
        "ask-one\nask-two\n",
        "the attempt reached Codex once"
    );
    assert_eq!(answers(&host, "switcher", "said-ask-two"), 1);
    let failed = host
        .turns("switcher")
        .into_iter()
        .filter(|row| row["turn"]["status"] == "failed")
        .count();
    assert_eq!(failed, 0, "{:?}", host.turns("switcher"));
}

#[test]
fn a_changed_model_restarts_the_idle_lane_and_the_next_turn_runs_with_it() {
    let (place, world) = lane_place("codex-lane-model", "codex_lane_idle_minutes = 5\n");
    let _lanes = Lanes(place.root.clone());
    let host = start_lane_host(&place, &world);
    let socket = lane_agent(&host, &place, "tuner");
    asked(&host, "tuner", "ask-one");
    let first = lane_said(&socket, "inspect").unwrap()["state"]["instance"].clone();
    // What each turn asked Codex for: its word, model and effort, as the stand-in noted them.
    let asked_for = |field: usize| -> Vec<String> {
        noted(&place, "turn-models")
            .lines()
            .map(|line| line.split(' ').nth(field).unwrap().to_owned())
            .collect()
    };
    let models = || asked_for(1);
    let efforts = || asked_for(2);

    let tuned = host.post(
        "/agent-llm",
        json!({"session": "tuner", "model": "gpt-5.5"}),
    );
    assert_eq!(tuned["model"], "gpt-5.5", "{tuned}");
    asked(&host, "tuner", "ask-two");
    let second = lane_said(&socket, "inspect").unwrap()["state"]["instance"].clone();
    assert_ne!(first, second, "the lane was started again for the model");
    assert_eq!(noted(&place, "launches"), "app-server\napp-server\n");
    assert_eq!(models()[1], "gpt-5.5");
    assert!(
        noted(&place, "threads")
            .lines()
            .nth(1)
            .unwrap()
            .ends_with("model=gpt-5.5"),
        "the new lane resumed the thread with the model: {}",
        noted(&place, "threads")
    );

    // Back to the default: Codex would keep the model it was given, so the lane starts again.
    let reset = host.post("/agent-llm", json!({"session": "tuner", "model": null}));
    assert_ne!(reset["model"], "gpt-5.5", "{reset}");
    asked(&host, "tuner", "ask-three");
    let third = lane_said(&socket, "inspect").unwrap()["state"]["instance"].clone();
    assert_ne!(second, third, "the lane was started again for the default");
    assert_eq!(
        noted(&place, "launches"),
        "app-server\napp-server\napp-server\n"
    );
    assert_eq!(
        models()[2],
        models()[0],
        "the turn runs with the default again: {:?}",
        models()
    );
    assert_eq!(noted(&place, "turns"), "ask-one\nask-two\nask-three\n");

    // An effort is given with each turn, so setting one keeps the lane. Clearing it does not:
    // the options are the same, but Codex would keep the effort an earlier turn gave it, so
    // the lane starts again for the turn that asks for the default.
    let effort = host.post("/agent-llm", json!({"session": "tuner", "effort": "high"}));
    assert_eq!(effort["effort"], "high", "{effort}");
    asked(&host, "tuner", "ask-four");
    assert_eq!(
        lane_said(&socket, "inspect").unwrap()["state"]["instance"],
        third,
        "a turn's effort needs no new lane"
    );
    assert_eq!(efforts()[3], "high");
    let cleared = host.post("/agent-llm", json!({"session": "tuner", "effort": null}));
    assert_ne!(cleared["effort"], "high", "{cleared}");
    asked(&host, "tuner", "ask-five");
    assert_ne!(
        lane_said(&socket, "inspect").unwrap()["state"]["instance"],
        third,
        "the lane was started again for the default effort"
    );
    assert_eq!(noted(&place, "launches").lines().count(), 4);
    assert_eq!(efforts()[4], efforts()[0], "{:?}", efforts());
    assert_eq!(
        noted(&place, "turns"),
        "ask-one\nask-two\nask-three\nask-four\nask-five\n"
    );
}

/// Whether a process runs: it is there and has not ended (an ended child its parent has not
/// reaped yet is still listed).
fn runs(pid: &str) -> bool {
    std::fs::read_to_string(format!("/proc/{pid}/stat")).is_ok_and(|stat| {
        stat.rsplit_once(')')
            .is_some_and(|(_, after)| !after.trim_start().starts_with('Z'))
    })
}

#[test]
fn a_stop_ends_the_long_command_of_a_lane_turn_within_its_grace_and_the_lane_answers_next() {
    let (place, world) = lane_place("codex-lane-stop-command", "codex_lane_idle_minutes = 5\n");
    let _lanes = Lanes(place.root.clone());
    let host = start_lane_host(&place, &world);
    let socket = lane_agent(&host, &place, "sleeper");
    asked(&host, "sleeper", "ask-first");
    let instance = lane_said(&socket, "inspect").unwrap()["state"]["instance"].clone();

    host.send("sleeper", "hold-cmdstop", "cmd");
    wait_for("the turn's command", || {
        place.root.join("holding-cmdstop").exists()
    });
    let command = noted(&place, "command-cmdstop");
    assert!(runs(&command), "the command runs while the turn runs");
    let began = Instant::now();
    host.post("/stop", json!({"session": "sleeper"}));
    wait_within("the command to be ended", Duration::from_secs(5), || {
        !runs(&command)
    });
    assert!(
        began.elapsed() < Duration::from_secs(5),
        "within the Stop's grace"
    );
    wait_for("the turn to end interrupted", || {
        host.state("sleeper") == "interrupted"
    });
    assert_eq!(answers(&host, "sleeper", "said-hold-cmdstop"), 0);

    // The lane that ended it answers the next turn.
    asked(&host, "sleeper", "ask-after");
    assert_eq!(noted(&place, "launches"), "app-server\n");
    assert_eq!(
        lane_said(&socket, "inspect").unwrap()["state"]["instance"],
        instance
    );
}

#[test]
fn a_stop_codex_does_not_answer_ends_codex_and_its_long_command_and_a_new_lane_answers_next() {
    let (place, world) = lane_place("codex-lane-stop-deaf", "codex_lane_idle_minutes = 5\n");
    let _lanes = Lanes(place.root.clone());
    let host = start_lane_host(&place, &world);
    let socket = lane_agent(&host, &place, "deafened");
    asked(&host, "deafened", "ask-first");
    let first = lane_said(&socket, "inspect").unwrap()["state"]["instance"].clone();

    // This Codex takes the interrupt and goes on: the lane ends it, and what runs below it,
    // when the Stop's grace (5 s) is over.
    std::fs::write(place.root.join("deaf"), "").unwrap();
    host.send("deafened", "hold-cmddeaf", "cmd");
    wait_for("the turn's command", || {
        place.root.join("holding-cmddeaf").exists()
    });
    let command = noted(&place, "command-cmddeaf");
    assert!(runs(&command), "the command runs while the turn runs");
    let began = Instant::now();
    host.post("/stop", json!({"session": "deafened"}));
    wait_within("the command to be ended", Duration::from_secs(10), || {
        !runs(&command)
    });
    let took = began.elapsed();
    assert!(
        took >= Duration::from_secs(4) && took < Duration::from_secs(10),
        "Codex had the Stop's grace, and then it and its command were ended: {took:?}"
    );
    wait_for("the turn to end interrupted", || {
        host.state("deafened") == "interrupted"
    });
    assert_eq!(answers(&host, "deafened", "said-hold-cmddeaf"), 0);

    // The next turn gets a new lane on the same thread.
    std::fs::remove_file(place.root.join("deaf")).unwrap();
    asked(&host, "deafened", "ask-after");
    assert_eq!(noted(&place, "launches"), "app-server\napp-server\n");
    assert_ne!(
        lane_said(&socket, "inspect").unwrap()["state"]["instance"],
        first
    );
    assert_eq!(
        noted(&place, "turns"),
        "ask-first\nhold-cmddeaf\nask-after\n"
    );
}

#[test]
fn the_terminal_of_an_idle_codex_agent_retires_its_lane_before_it_opens() {
    let (place, world) = lane_place("codex-lane-terminal", "codex_lane_idle_minutes = 5\n");
    let _lanes = Lanes(place.root.clone());
    let host = start_lane_host(&place, &world);
    let socket = lane_agent(&host, &place, "by-hand");
    asked(&host, "by-hand", "ask-first");
    assert!(lane_lives(&socket), "the turn left its lane idle");

    // The terminal is opened only once the idle lane has left: the tool by hand is the
    // conversation's one writer.
    let terminal = socket_at(&host, "", "/terminal/by-hand");
    assert!(terminal.is_ok(), "the terminal opens: {:?}", terminal.err());
    assert!(
        !lane_lives(&socket),
        "the idle lane left before the terminal opened"
    );
    drop(terminal);
}
