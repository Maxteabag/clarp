//! Issue 762. With the owner's `muse_chat_lane` on, a Muse agent's chat turns run on one kept
//! `muse serve`: the first on exec (which names the session), while the lane starts beside it and
//! takes the session once exec is gone, or for the turn queued behind exec, before it (issue
//! 833); the turns after on the lane, which takes steered words and keeps its process through a
//! Stop. With the switch off again, the lane leaves before exec
//! runs the session, which Muse would refuse while the lane holds it.

use super::*;

/// A stand-in Muse. `exec` refuses a session a `serve` holds (`lock-<session>`), as Muse does, and
/// answers `exec-<word>`. `serve` speaks MSP: a word `ask-<x>` is answered `lane-ask-<x>` at once;
/// `hold-<x>` is answered `lane-hold-<x>` once the file `release-<x>` exists, unless it is
/// interrupted first. While the file `slow-end` exists, a `serve` whose input has ended takes 1.8 s
/// to end, as Muse writes its session log to its end. What it was asked is noted in files beside
/// it.
const MUSE: &str = r#"#!/usr/bin/env python3
import json
import os
from pathlib import Path
import re
import sys
import threading
import time

root = Path(__file__).resolve().parent

def note(name, text):
    with (root / name).open("a") as f:
        f.write(text + "\n")

if sys.argv[1] == "exec":
    session = sys.argv[sys.argv.index("--session-id") + 1]
    if (root / ("lock-" + session)).exists():
        note("refused", session)
        print(f"session {session} is already in use", file=sys.stderr, flush=True)
        raise SystemExit(1)
    note("launches", "exec")
    word = re.search(r"\b(?:ask|hold)-[a-z0-9]+", sys.argv[-1]).group(0)
    if word.startswith("hold-"):
        (root / ("holding-" + word.removeprefix("hold-"))).write_text("")
        release = root / ("release-" + word.removeprefix("hold-"))
        end = time.monotonic() + 60
        while not release.exists() and time.monotonic() < end:
            time.sleep(.02)
    for event in (("run.lifecycle.started", {}),
                  ("run.output.delta", {"text": "exec-" + word}),
                  ("run.terminal.completed", {"terminal": "completed"})):
        print(json.dumps({"payload_type": event[0], "payload": event[1]}), flush=True)
    raise SystemExit(0)

assert sys.argv[1] == "serve", sys.argv
note("launches", "serve")
note("serve-args", " ".join(sys.argv[2:]))
# As Muse 1.4.25: its reminder agents run unless the empty roster is set, and only then does it
# say the foreground is done before the turn is.
reminders = "run.reminder_roster=[]" not in sys.argv
note("lane-env", "turn=" + ("set" if os.environ.get("CLARP_PROVIDER_TURN") else "absent"))
token_file = os.environ.get("CLARP_PROVIDER_TURN_FILE", "")
out = threading.Lock()
state = threading.Lock()
session = None
running = {}

def emit(value):
    value["jsonrpc"] = "2.0"
    with out:
        print(json.dumps(value), flush=True)

def notify(method, **fields):
    emit({"method": method, "params": {"sessionId": session, **fields}})

def finish(turn_id, word, terminal="completed"):
    if terminal == "completed":
        notify("item/completed", item={"itemId": "a-" + turn_id, "kind": "agentMessage",
            "turnId": turn_id, "status": "completed", "text": "lane-" + word, "revision": 1})
        note("answered", word)
        if reminders:
            notify("turn/foregroundCompleted", turnId=turn_id, blockingAgents=["goal-reminder"])
            time.sleep(.3)
    notify("turn/completed", turnId=turn_id, terminal=terminal,
        usage={"inputTokens": 7, "outputTokens": 2})

def held(turn_id, word, stopped):
    release = root / ("release-" + word.removeprefix("hold-"))
    end = time.monotonic() + 60
    while not release.exists() and time.monotonic() < end:
        if stopped.wait(.02):
            return
    with state:
        if running.pop(turn_id, None) is None:
            return
    finish(turn_id, word)

for line in sys.stdin:
    request = json.loads(line)
    assert request.get("jsonrpc") == "2.0", request
    method = request["method"]
    params = request.get("params", {})
    if method == "initialized":
        continue
    request_id = request["id"]
    result = {}
    after = None
    if method == "initialize":
        assert params["capabilities"]["userInputDialogs"] is False, params
    elif method == "session/resume":
        session = params["sessionId"]
        lock = root / ("lock-" + session)
        if lock.exists():
            emit({"id": request_id, "error": {"code": -32021, "message": "session in use"}})
            continue
        lock.write_text(str(os.getpid()))
        note("resumed", session)
        result = {"session": {"sessionId": session, "modelId": "muse-model"}}
    elif method == "session/setApprovalMode":
        note("approval", params["mode"])
    elif method == "session/setModel":
        note("models", params["model"]["modelId"])
    elif method == "turn/start":
        text = params["input"][0]["text"]
        word = re.search(r"\b(?:ask|hold)-[a-z0-9]+", text).group(0)
        turn_id = params["commandId"]
        note("turns", word)
        note("tokens", Path(token_file).read_text() if token_file else "")
        result = {"turnId": turn_id, "disposition": "started", "startedNewTurn": True,
            "status": "accepted", "commandId": turn_id}
        stopped = threading.Event()
        with state:
            running[turn_id] = stopped
        def after():
            notify("turn/started", turnId=turn_id, commandId=turn_id)
            if word.startswith("hold-"):
                (root / ("holding-" + word.removeprefix("hold-"))).write_text("")
                threading.Thread(target=held, args=(turn_id, word, stopped), daemon=True).start()
            else:
                with state:
                    running.pop(turn_id, None)
                finish(turn_id, word)
    elif method == "turn/steer":
        with state:
            live = params["expectedTurnId"] in running
        if not live:
            emit({"id": request_id, "error": {"code": -32030, "message": "no running turn"}})
            continue
        note("steered", params["input"][0]["text"])
        result = {"turnId": params["expectedTurnId"], "commandId": params["commandId"],
            "status": "accepted"}
        steered = (params["expectedTurnId"], params["commandId"], params["input"][0]["text"])
        def after():
            notify("item/started", item={"itemId": "u-" + steered[1], "kind": "userMessage",
                "turnId": steered[0], "status": "completed", "steered": True,
                "commandId": steered[1], "text": steered[2], "revision": 1})
    elif method == "turn/unqueue":
        note("unqueued", params["turnId"])
    elif method == "turn/interrupt":
        turn_id = params["turnId"]
        note("interrupts", "x")
        with state:
            stopped = running.pop(turn_id, None)
        if stopped is not None:
            stopped.set()
            def after():
                finish(turn_id, "", "cancelled")
    else:
        raise SystemExit("unexpected " + method)
    emit({"id": request_id, "result": result})
    if after:
        after()
if (root / "slow-end").exists():
    note("slow-end", "serve")
    time.sleep(1.8)
if session:
    (root / ("lock-" + session)).unlink(missing_ok=True)
"#;

fn noted(place: &Place, name: &str) -> String {
    std::fs::read_to_string(place.root.join(name)).unwrap_or_default()
}

/// How many times the stand-in was started as each of its commands, in the order of their names:
/// the lane starts beside an exec turn, so the order of the two is not fixed.
fn launched(place: &Place) -> Vec<(&'static str, usize)> {
    let said = noted(place, "launches");
    ["exec", "serve"]
        .into_iter()
        .map(|what| (what, said.lines().filter(|line| *line == what).count()))
        .collect()
}

fn answers(host: &Host, session: &str, text: &str) -> usize {
    host.conversation(session)
        .iter()
        .filter(|(role, said)| role == "assistant" && said == text)
        .count()
}

fn answered(host: &Host, session: &str, text: &str) {
    wait_for(text, || answers(host, session, text) == 1);
    wait_for("the turn to end", || host.state(session) == "done");
}

/// Asks every Muse lane of a place to leave when the test ends.
struct Lanes(std::path::PathBuf);

impl Drop for Lanes {
    fn drop(&mut self) {
        let Ok(entries) = std::fs::read_dir(self.0.join("run")) else {
            return;
        };
        for entry in entries.flatten() {
            let path = entry.path();
            if path.to_string_lossy().ends_with("-mus.sock")
                && let Ok(mut stream) = std::os::unix::net::UnixStream::connect(&path)
            {
                let _ = writeln!(stream, "{}", json!({"op": "retire"}));
            }
        }
    }
}

#[test]
fn muse_chat_turns_run_on_one_kept_serve_after_exec_and_take_steered_words_and_a_stop() {
    let place = Place::new("muse-chat-lane");
    let _lanes = Lanes(place.root.clone());
    let muse = place.root.join("muse");
    program(&muse, MUSE);
    let config = place.root.join("config");
    std::fs::create_dir_all(config.join("clarp-host")).unwrap();
    let setting = |on: bool| {
        std::fs::write(
            config.join("clarp-host/config.toml"),
            format!("muse_chat_lane = {on}\ncodex_lane_idle_minutes = 15\n"),
        )
        .unwrap();
    };
    setting(false);
    let world = [
        ("CLARP_MUSE_PROGRAM", muse.to_str().unwrap()),
        ("XDG_CONFIG_HOME", config.to_str().unwrap()),
    ];
    let host = place.start_in("the-owners-token", &[], &world);
    // The owner switches the Muse lane on from his apps' "Kept processes" settings.
    let read = host.get("/lane-settings");
    assert_eq!(read["muse_chat_lane"], false, "{read}");
    assert_eq!(read["held"]["tools"]["muse"]["lanes"], 0, "{read}");
    let switched = host.post("/lane-settings", json!({"muse_chat_lane": true}));
    assert_eq!(switched["muse_chat_lane"], true, "{switched}");
    let file = std::fs::read_to_string(config.join("clarp-host/config.toml")).unwrap();
    assert!(file.starts_with("muse_chat_lane = true\n"), "{file}");
    let made = host.post(
        "/agents",
        json!({"name": "Muse", "session": "muser", "backend": "muse"}),
    );
    assert_eq!(made["session"], "muser", "{made}");
    let users = || {
        host.turns("muser")
            .into_iter()
            .filter(|row| row["role"] == "user")
            .map(|row| row["id"].as_str().unwrap().to_owned())
            .collect::<Vec<_>>()
    };

    // The first turn runs on exec, which names the session, while the lane starts beside it.
    // A second message, sent while the first runs, is launched the moment exec ends: the lane
    // takes the session for it and runs it, rather than a second exec (issue 833). Exec never
    // meets a session the lane holds.
    host.send("muser", "hold-one", "one");
    wait_for("the first exec turn", || {
        place.root.join("holding-one").exists()
    });
    let sent = host.send("muser", "ask-onemore", "onemore");
    assert_eq!(
        sent["queued"], true,
        "an exec turn takes no steered words: {sent}"
    );
    std::fs::write(place.root.join("release-one"), "").unwrap();
    answered(&host, "muser", "exec-hold-one");
    answered(&host, "muser", "lane-ask-onemore");
    assert_eq!(answers(&host, "muser", "exec-ask-onemore"), 0);
    wait_for("the lane to take the session", || {
        noted(&place, "resumed").lines().count() == 1
    });
    assert_eq!(
        noted(&place, "refused"),
        "",
        "exec never met a held session"
    );
    assert_eq!(launched(&place), [("exec", 1), ("serve", 1)]);
    assert_eq!(noted(&place, "approval"), "allowAll\n");
    assert_eq!(noted(&place, "lane-env"), "turn=absent\n");
    assert!(
        noted(&place, "serve-args").contains("-c run.reminder_roster=[]"),
        "Muse's reminder agents are off on the lane, as on exec: {}",
        noted(&place, "serve-args")
    );

    // The next turn runs on the lane, which the settings count with what it weighed itself.
    host.send("muser", "ask-two", "two");
    answered(&host, "muser", "lane-ask-two");
    let read = host.get("/lane-settings");
    assert_eq!(read["held"]["tools"]["muse"]["lanes"], 1, "{read}");
    assert!(
        read["held"]["tools"]["muse"]["memory_mb"].as_u64().unwrap() > 0,
        "{read}"
    );

    // Words sent while a lane turn runs reach it, and enter the conversation when Muse reads them.
    host.send("muser", "hold-three", "three");
    wait_for("the held turn", || {
        place.root.join("holding-three").exists()
    });
    let sent = host.send("muser", "more-words", "more");
    assert_eq!(sent["queued"], true, "{sent}");
    wait_for("the steer", || {
        noted(&place, "steered").contains("more-words")
    });
    wait_for("the steered words in the conversation", || {
        users() == ["u-one", "u-onemore", "u-two", "u-three", "u-more"]
    });
    std::fs::write(place.root.join("release-three"), "").unwrap();
    answered(&host, "muser", "lane-hold-three");
    assert_eq!(
        noted(&place, "turns"),
        "ask-onemore\nask-two\nhold-three\n",
        "the steered words never ran as a turn of their own"
    );

    // A Stop interrupts the lane's turn at once and keeps the lane: the next turn runs on it.
    host.send("muser", "hold-four", "four");
    wait_for("the held turn", || place.root.join("holding-four").exists());
    let began = Instant::now();
    host.post("/stop", json!({"session": "muser"}));
    wait_for("the turn to end interrupted", || {
        host.state("muser") == "interrupted"
    });
    assert!(
        began.elapsed() < Duration::from_secs(4),
        "{:?}",
        began.elapsed()
    );
    assert_eq!(answers(&host, "muser", "lane-hold-four"), 0);
    host.send("muser", "ask-five", "five");
    answered(&host, "muser", "lane-ask-five");
    assert_eq!(launched(&place), [("exec", 1), ("serve", 1)]);
    let tokens: Vec<String> = noted(&place, "tokens").lines().map(str::to_owned).collect();
    assert_eq!(tokens.len(), 5, "{tokens:?}");
    assert!(tokens.iter().all(|token| !token.is_empty()), "{tokens:?}");
    assert_ne!(tokens[0], tokens[1], "each turn has its own identity");

    // Switched off: the lane leaves before exec runs the session, which Muse would refuse while
    // the lane holds it.
    let switched = host.post("/lane-settings", json!({"muse_chat_lane": false}));
    assert_eq!(switched["muse_chat_lane"], false, "{switched}");
    host.send("muser", "ask-six", "six");
    answered(&host, "muser", "exec-ask-six");
    assert_eq!(launched(&place), [("exec", 2), ("serve", 1)]);
    assert_eq!(
        noted(&place, "refused"),
        "",
        "exec never met a held session"
    );
}

/// Issue 849. A changed login makes the Muse lane refuse the next turn and leave. That turn has a
/// lane start under the new login and take the session, as a turn no lane holds does, and runs on
/// it, instead of running on exec: one start, and no turn without a lane.
#[test]
fn a_muse_turn_refused_for_a_changed_login_runs_on_a_lane_under_the_new_one() {
    let place = Place::new("muse-seat-switch");
    let _lanes = Lanes(place.root.clone());
    let muse = place.root.join("muse");
    program(&muse, MUSE);
    let config = place.root.join("config");
    std::fs::create_dir_all(config.join("clarp-host")).unwrap();
    std::fs::create_dir_all(config.join("muse")).unwrap();
    std::fs::write(
        config.join("clarp-host/config.toml"),
        "muse_chat_lane = true\ncodex_lane_idle_minutes = 15\n",
    )
    .unwrap();
    let login = |email: &str| {
        let said = json!({"providers": {"meta": {"user_email": email, "token": "t"}}});
        std::fs::write(config.join("muse/auth.json"), said.to_string()).unwrap();
    };
    login("one@example.org");
    let world = [
        ("CLARP_MUSE_PROGRAM", muse.to_str().unwrap()),
        ("XDG_CONFIG_HOME", config.to_str().unwrap()),
    ];
    let host = place.start_in("the-owners-token", &[], &world);
    let made = host.post(
        "/agents",
        json!({"name": "Muse", "session": "muser", "backend": "muse"}),
    );
    assert_eq!(made["session"], "muser", "{made}");
    // The first turn runs on exec, which names the session; the lane takes it after.
    host.send("muser", "ask-one", "one");
    answered(&host, "muser", "exec-ask-one");
    wait_for("the lane to take the session", || {
        noted(&place, "resumed").lines().count() == 1
    });
    host.send("muser", "ask-two", "two");
    answered(&host, "muser", "lane-ask-two");
    assert_eq!(launched(&place), [("exec", 1), ("serve", 1)]);
    let cold = || {
        host.get("/lane-settings")["cold_starts_last_hour"]["tools"]["muse"]
            .as_u64()
            .unwrap_or(0)
    };
    let before = cold();

    // Another account: the lane refuses the next turn and leaves, and a lane on the new login
    // takes the session and runs it.
    login("two@example.org");
    host.send("muser", "ask-three", "three");
    answered(&host, "muser", "lane-ask-three");
    assert_eq!(answers(&host, "muser", "exec-ask-three"), 0);
    assert_eq!(launched(&place), [("exec", 1), ("serve", 2)]);
    assert_eq!(noted(&place, "resumed").lines().count(), 2);
    assert_eq!(noted(&place, "turns"), "ask-two\nask-three\n");
    assert_eq!(
        noted(&place, "refused"),
        "",
        "exec never met a held session"
    );
    assert_eq!(cold(), before, "no turn started without a lane");
}

/// Issue 908. An idle Muse lane asked to leave says it goes as soon as it has taken the ask,
/// while Muse still ends in order (here in 1.8 s of the 2 it is given): a Host making room counts
/// its place then. Before, the answer came only after Muse had ended, and a start making room
/// waited that long for it (a lane from before still answers so, and is still waited for). The
/// lane still lets go of its owner lock only once Muse has been reaped.
#[test]
fn an_idle_muse_lane_asked_to_leave_says_it_goes_before_muse_has_ended() {
    let place = Place::new("muse-lane-slow-end");
    let _lanes = Lanes(place.root.clone());
    let muse = place.root.join("muse");
    program(&muse, MUSE);
    let config = place.root.join("config");
    std::fs::create_dir_all(config.join("clarp-host")).unwrap();
    std::fs::write(
        config.join("clarp-host/config.toml"),
        "muse_chat_lane = true\ncodex_lane_idle_minutes = 15\n",
    )
    .unwrap();
    let world = [
        ("CLARP_MUSE_PROGRAM", muse.to_str().unwrap()),
        ("XDG_CONFIG_HOME", config.to_str().unwrap()),
    ];
    let host = place.start_in("the-owners-token", &[], &world);
    let made = host.post(
        "/agents",
        json!({"name": "Muse", "session": "muser", "backend": "muse"}),
    );
    assert_eq!(made["session"], "muser", "{made}");
    // The first turn runs on exec, and the lane starts beside it.
    host.send("muser", "ask-one", "one");
    answered(&host, "muser", "exec-ask-one");
    let run = place.root.join("run");
    let mut socket = None;
    wait_for("the lane to be up and idle", || {
        socket = std::fs::read_dir(&run)
            .into_iter()
            .flatten()
            .flatten()
            .map(|entry| entry.path())
            .find(|path| path.to_string_lossy().ends_with("-mus.sock"))
            .filter(|path| {
                lane_said(path, "inspect")
                    .is_some_and(|said| said["ok"] == true && said["state"]["available"] == true)
            });
        socket.is_some()
    });
    let socket = socket.unwrap();
    std::fs::write(place.root.join("slow-end"), "").unwrap();
    let began = Instant::now();
    let said = lane_said(&socket, "retire").expect("the lane answers the ask");
    let answered_in = began.elapsed();
    assert_eq!(said["ok"], true, "{said}");
    assert!(
        answered_in < Duration::from_secs(1),
        "answered after {answered_in:?}"
    );
    let lock = std::fs::OpenOptions::new()
        .read(true)
        .write(true)
        .open(socket.with_extension("lock"))
        .unwrap();
    assert!(
        lock.try_lock_shared().is_err(),
        "Muse still ends, and the lane holds its owner lock"
    );
    wait_for("the lane to let go once Muse has ended", || {
        lock.try_lock_shared().is_ok()
    });
    assert_eq!(noted(&place, "slow-end"), "serve\n");
}
