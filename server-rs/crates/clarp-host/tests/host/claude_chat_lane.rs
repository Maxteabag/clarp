//! Issue 761. With the owner's `claude_chat_lane` on, a Claude agent's first turn runs in a
//! process of its own, and once that process is gone one Claude Code is kept for the agent: the
//! turns after it are written to its input and end at their `result`, a message sent while one
//! runs is steered into it, a Stop is Claude Code's interrupt and keeps the process, and the
//! turns' tools find their turn's identity in the lane's turn file. With the setting off again,
//! the lane leaves and the next turn runs in a process of its own on the same conversation.

use super::*;

/// A stand-in Claude Code that keeps reading stream-json lines until its input closes. A word
/// `ask-<x>` is answered `said-ask-<x>` at once; `hold-<x>` once the file `release-<x>` exists,
/// or at an interrupt. A message that comes while a word is held is read into its turn, and the
/// result that ends the turn names both. What it was started with and asked is noted beside it.
pub(super) const CLAUDE: &str = r#"#!/usr/bin/env python3
import json
import os
import pathlib
import queue
import re
import sys
import threading

if "--version" in sys.argv:
    print("2.1.321 (Claude Code)")
    raise SystemExit(0)

root = pathlib.Path(__file__).resolve().parent
flag = "--resume" if "--resume" in sys.argv else "--session-id"
session = sys.argv[sys.argv.index(flag) + 1]
turn_env = "set" if os.environ.get("CLARP_PROVIDER_TURN") else "absent"
token_file = os.environ.get("CLARP_PROVIDER_TURN_FILE", "")

def note(name, text):
    with (root / name).open("a") as f:
        f.write(text + "\n")

note("launches", f"{flag} turn={turn_env}")

def say(kind, **fields):
    print(json.dumps({"type": kind, "session_id": session, **fields}), flush=True)

def answer(text, uids, error=False):
    if not error:
        say("assistant", message={"id": "msg_" + text, "role": "assistant",
            "content": [{"type": "text", "text": text}]})
    say("result", subtype="error_during_execution" if error else "success", is_error=error,
        result="" if error else text, user_message_uuids=uids)

lines = queue.Queue()
def read():
    for line in sys.stdin:
        lines.put(json.loads(line))
    lines.put(None)
threading.Thread(target=read, daemon=True).start()

held = None
while True:
    try:
        record = lines.get(timeout=0.02)
    except queue.Empty:
        if held and (root / ("release-" + held["word"].removeprefix("hold-"))).exists():
            answer("said-" + held["word"], held["uids"])
            note("answered", held["word"])
            held = None
        continue
    if record is None:
        break
    if record["type"] == "control_request":
        note("interrupts", record["request"]["subtype"])
        say("control_response", response={"subtype": "success",
            "request_id": record["request_id"], "response": {}})
        if held:
            answer("", held["uids"], error=True)
            held = None
        continue
    uid = record["uuid"]
    text = record["message"]["content"][0]["text"]
    if held:
        note("steered", re.search(r"more-[a-z]+", text).group(0))
        say("user", isReplay=True, uuid=uid, message={"role": "user", "content": []})
        held["uids"].append(uid)
        continue
    word = re.search(r"\b(?:ask|hold)-[a-z0-9]+", text).group(0)
    note("turns", word)
    token = pathlib.Path(token_file).read_text() if token_file and pathlib.Path(token_file).exists() else ""
    note("tokens", f"{word} {turn_env} {token}")
    say("system", subtype="init")
    say("user", isReplay=True, uuid=uid, message={"role": "user", "content": []})
    if word.startswith("hold-"):
        held = {"word": word, "uids": [uid]}
        (root / ("holding-" + word.removeprefix("hold-"))).write_text("")
    else:
        answer("said-" + word, [uid])
        note("answered", word)
note("exits", session)
"#;

pub(super) fn noted(place: &Place, name: &str) -> String {
    std::fs::read_to_string(place.root.join(name)).unwrap_or_default()
}

fn answers(host: &Host, session: &str, text: &str) -> usize {
    host.conversation(session)
        .iter()
        .filter(|(role, said)| role == "assistant" && said == text)
        .count()
}

/// Sends `word` and waits for its answer and the end of its turn.
fn asked(host: &Host, session: &str, word: &str) {
    host.send(session, word, word);
    let answer = format!("said-{word}");
    wait_for(&answer, || answers(host, session, &answer) == 1);
    wait_for("the turn to end", || host.state(session) == "done");
}

/// What a lane answers one request with, or `None` when no lane answers on the socket.
pub(super) fn lane_said(socket: &std::path::Path, op: &str) -> Option<Value> {
    let mut stream = std::os::unix::net::UnixStream::connect(socket).ok()?;
    stream.set_read_timeout(Some(Duration::from_secs(5))).ok()?;
    writeln!(stream, "{}", json!({ "op": op })).ok()?;
    let mut line = String::new();
    BufReader::new(stream).read_line(&mut line).ok()?;
    serde_json::from_str(&line).ok()
}

/// Asks the place's lanes to leave when the test ends, so none outlives it by its idle time.
pub(super) struct Lanes(pub(super) std::path::PathBuf);

impl Drop for Lanes {
    fn drop(&mut self) {
        let Ok(entries) = std::fs::read_dir(self.0.join("run")) else {
            return;
        };
        for entry in entries.flatten() {
            let path = entry.path();
            if path.to_string_lossy().ends_with("-cll.sock") {
                let _ = lane_said(&path, "retire");
            }
        }
    }
}

#[test]
fn claude_turns_after_the_first_run_on_one_kept_process_and_its_interrupt_keeps_it() {
    let place = Place::new("claude-chat-lane");
    let _lanes = Lanes(place.root.clone());
    let claude = place.root.join("claude");
    program(&claude, CLAUDE);
    let config = place.root.join("config");
    std::fs::create_dir_all(config.join("clarp-host")).unwrap();
    let setting = |on: bool| {
        std::fs::write(
            config.join("clarp-host/config.toml"),
            format!("claude_chat_lane = {on}\ncodex_lane_idle_minutes = 15\n"),
        )
        .unwrap();
    };
    setting(true);
    // Claude Code's credentials, which a switch of login may be the only file to change.
    let home = place.root.join("home");
    std::fs::create_dir_all(home.join(".claude")).unwrap();
    let credentials = |token: &str| {
        let said = json!({"claudeAiOauth": {"refreshToken": token, "accessToken": "a"}});
        std::fs::write(home.join(".claude/.credentials.json"), said.to_string()).unwrap();
    };
    credentials("r1");
    let world = [
        ("CLARP_CLAUDE_PROGRAM", claude.to_str().unwrap()),
        ("XDG_CONFIG_HOME", config.to_str().unwrap()),
        ("HOME", home.to_str().unwrap()),
    ];
    let host = place.start_in("", &[], &world);
    let made = host.post(
        "/agents",
        json!({"name": "Kept", "session": "kept", "backend": "claude"}),
    );
    assert_eq!(made["session"], "kept", "{made}");
    let id = host.get("/agents/snapshot")["agents"]
        .as_array()
        .unwrap()
        .iter()
        .find(|agent| agent["session"] == "kept")
        .unwrap()["agent_id"]
        .as_str()
        .unwrap()
        .to_owned();
    let socket = place.root.join("run").join(format!("{id}-cll.sock"));
    let users = || {
        host.turns("kept")
            .into_iter()
            .filter(|row| row["role"] == "user")
            .map(|row| row["id"].as_str().unwrap().to_owned())
            .collect::<Vec<_>>()
    };

    // The first turn never waits for a lane: it runs in a process of its own, which names its
    // turn in its environment. Once it is gone, the lane resumes the conversation.
    asked(&host, "kept", "ask-one");
    wait_for("the lane to start after the first turn", || {
        noted(&place, "launches").lines().count() == 2
    });
    assert_eq!(
        noted(&place, "launches"),
        "--session-id turn=set\n--resume turn=absent\n"
    );
    wait_for("the lane to answer", || {
        lane_said(&socket, "inspect").is_some_and(|said| said["state"]["available"] == true)
    });

    // The next turn is written to the kept process and ends at its result.
    asked(&host, "kept", "ask-two");
    assert_eq!(noted(&place, "launches").lines().count(), 2);

    // Words sent while a turn runs reach it on the same input, and are answered with it.
    host.send("kept", "hold-three", "hold");
    wait_for("the held turn", || {
        place.root.join("holding-three").exists()
    });
    let sent = host.send("kept", "more-words", "more");
    assert_eq!(sent["queued"], true, "{sent}");
    wait_for("the steer", || noted(&place, "steered") == "more-words\n");
    std::fs::write(place.root.join("release-three"), "").unwrap();
    wait_for("the held answer", || {
        answers(&host, "kept", "said-hold-three") == 1
    });
    wait_for("the held turn to end", || host.state("kept") == "done");
    assert_eq!(users(), ["u-ask-one", "u-ask-two", "u-hold", "u-more"]);

    // A Stop interrupts the turn on the process's input: the turn ends at once, and the
    // process is kept for the next.
    host.send("kept", "hold-four", "four");
    wait_for("the fourth held turn", || {
        place.root.join("holding-four").exists()
    });
    let began = Instant::now();
    host.post("/stop", json!({"session": "kept"}));
    wait_for("the turn to end interrupted", || {
        host.state("kept") == "interrupted"
    });
    assert!(
        began.elapsed() < Duration::from_secs(4),
        "the interrupt's result ends the turn at once: {:?}",
        began.elapsed()
    );
    assert_eq!(noted(&place, "interrupts"), "interrupt\n");
    asked(&host, "kept", "ask-five");
    assert_eq!(
        noted(&place, "launches").lines().count(),
        2,
        "the interrupt kept the process"
    );
    assert_eq!(
        noted(&place, "turns"),
        "ask-one\nask-two\nhold-three\nhold-four\nask-five\n",
        "the steered words never ran as a turn of their own"
    );

    // Each lane turn's tools found their own identity in the lane's turn file, and none in
    // the environment.
    let tokens: Vec<String> = noted(&place, "tokens").lines().map(str::to_owned).collect();
    assert_eq!(tokens.len(), 5, "{tokens:?}");
    let lane_tokens: Vec<&str> = tokens[1..]
        .iter()
        .map(|line| {
            let mut words = line.splitn(3, ' ');
            let (_, env, token) = (words.next(), words.next(), words.next());
            assert_eq!(env, Some("absent"), "{line}");
            token.unwrap_or_default()
        })
        .collect();
    assert!(
        lane_tokens.iter().all(|token| !token.is_empty()),
        "{tokens:?}"
    );
    assert_ne!(
        lane_tokens[0], lane_tokens[1],
        "each turn has its own identity"
    );

    // Another login, written to the credentials file alone: the lane on the old one serves
    // no more turns. The next turn runs in a process of its own, and a lane starts after it.
    credentials("r2");
    asked(&host, "kept", "ask-login");
    wait_for("a lane on the new login", || {
        noted(&place, "launches").lines().count() == 4
    });
    assert_eq!(
        noted(&place, "launches"),
        "--session-id turn=set\n--resume turn=absent\n--resume turn=set\n--resume turn=absent\n"
    );

    // Turned off: the lane leaves before the next turn, which runs in a process of its own on
    // the same conversation.
    setting(false);
    asked(&host, "kept", "ask-six");
    assert_eq!(
        noted(&place, "launches").lines().last(),
        Some("--resume turn=set")
    );
    assert_eq!(noted(&place, "launches").lines().count(), 5);
    assert!(lane_said(&socket, "inspect").is_none(), "the lane left");
}
