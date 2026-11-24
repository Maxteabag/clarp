//! Issue 750: a turn is over for the user at its tool's own end line, while the process runs a
//! moment longer, and the agent's next turn is launched only once it is gone. One file a piece,
//! declared in `host.rs`.

use super::*;

/// Claude Code as it ends a turn: its result first, and its exit only when the test lets it go
/// (the real one sends what it counted in between). Asked to stop with SIGINT, it says so in a
/// result of its own at once, and exits later too.
const CLAUDE: &str = r#"#!/usr/bin/env python3
import json
import pathlib
import signal
import sys
import time

if "--version" in sys.argv:
    print("2.1.321 (Claude Code)")
    raise SystemExit(0)

root = pathlib.Path(__file__).resolve().parent
flag = "--resume" if "--resume" in sys.argv else "--session-id"
session = sys.argv[sys.argv.index(flag) + 1]

def say(kind, **fields):
    print(json.dumps({"type": kind, "session_id": session, **fields}), flush=True)

def release(name):
    end = time.monotonic() + 40
    while not (root / name).exists():
        if time.monotonic() >= end:
            raise SystemExit("stand-in release expired")
        time.sleep(.02)

record = json.loads(sys.stdin.readline())
text = record["message"]["content"][0]["text"]
with (root / "launches").open("a") as rows:
    rows.write(text + "\n")
say("system", subtype="init")
if text.startswith("stop-"):
    def interrupted(*_):
        say("result", subtype="error_during_execution", is_error=True, result="")
        (root / ("interrupted-" + text)).touch()
        release("exit-" + text)
        raise SystemExit(0)
    signal.signal(signal.SIGINT, interrupted)
    say("assistant", message={"id": "msg_" + text, "role": "assistant",
        "content": [{"type": "text", "text": "working-" + text}]})
    (root / ("working-" + text)).touch()
    release("never-" + text)
    raise SystemExit(1)
say("assistant", message={"id": "msg_" + text, "role": "assistant",
    "content": [{"type": "text", "text": "answer-" + text}]})
say("result", subtype="success", is_error=False, result="answer-" + text,
    user_message_uuids=[record["uuid"]])
(root / ("answered-" + text)).touch()
release("exit-" + text)
"#;

#[test]
fn a_claude_turn_is_over_at_its_result_and_the_next_waits_for_its_exit() {
    let place = Place::new("end-at-answer");
    let claude = place.root.join("claude");
    program(&claude, CLAUDE);
    let home = place.root.join("home");
    let xdg = [".config", ".local/share", ".cache", ".local/state"].map(|name| home.join(name));
    let nowhere = place.root.join("absent");
    let world = [
        ("HOME", home.to_str().unwrap()),
        ("XDG_CONFIG_HOME", xdg[0].to_str().unwrap()),
        ("XDG_DATA_HOME", xdg[1].to_str().unwrap()),
        ("XDG_CACHE_HOME", xdg[2].to_str().unwrap()),
        ("XDG_STATE_HOME", xdg[3].to_str().unwrap()),
        ("CLARP_CLAUDE_PROGRAM", claude.to_str().unwrap()),
        ("CLARP_CODEX_PROGRAM", nowhere.to_str().unwrap()),
        ("CLARP_AGY_PROGRAM", nowhere.to_str().unwrap()),
        ("CLARP_GROK_PROGRAM", nowhere.to_str().unwrap()),
        ("CLARP_MUSE_PROGRAM", nowhere.to_str().unwrap()),
        ("CLARP_OPENCODE_PROGRAM", nowhere.to_str().unwrap()),
    ];
    let host = place.start_in("", &[], &world);
    let made = host.post(
        "/agents",
        json!({"name": "Quick", "session": "quick", "backend": "claude"}),
    );
    assert_eq!(made["session"], "quick");
    let await_file = |name: &str| wait_for(name, || place.root.join(name).exists());
    let mark = |name: &str| std::fs::write(place.root.join(name), "").unwrap();
    let launches = || {
        std::fs::read_to_string(place.root.join("launches"))
            .unwrap_or_default()
            .lines()
            .map(str::to_owned)
            .collect::<Vec<_>>()
    };
    // Long enough for a launch that should not come to have come.
    let settle = || std::thread::sleep(Duration::from_millis(700));

    // Its answer is whole: the agent rests while its process still runs.
    host.send("quick", "one", "m1");
    await_file("answered-one");
    wait_for("the turn to be over before its process", || {
        host.state("quick") == "done"
    });
    let answered = host.turns("quick");
    let last = answered.last().unwrap();
    assert_eq!(last["text"], "answer-one");
    assert_eq!(last["turn"]["status"], "completed", "{last}");

    // A message now begins its turn, and its tool waits for the process before it.
    host.send("quick", "two", "m2");
    settle();
    assert_eq!(
        launches(),
        ["one"],
        "two processes never work in one conversation"
    );
    mark("exit-one");
    await_file("answered-two");
    assert_eq!(launches(), ["one", "two"]);
    mark("exit-two");
    wait_for("the second turn to be over", || {
        host.state("quick") == "done"
    });

    // A Stop: the interrupted tool says at once that its turn is over, and the turn is
    // stopped there, while its process still runs.
    host.send("quick", "stop-three", "m3");
    await_file("working-stop-three");
    let stopped = host.post("/stop", json!({ "session": "quick" }));
    assert_eq!(stopped["terminated"], 1, "{stopped}");
    await_file("interrupted-stop-three");
    wait_for("the stopped turn to end at its result", || {
        host.state("quick") == "interrupted"
    });
    host.send("quick", "four", "m4");
    settle();
    assert_eq!(launches(), ["one", "two", "stop-three"]);
    mark("exit-stop-three");
    await_file("answered-four");
    assert_eq!(launches(), ["one", "two", "stop-three", "four"]);
    mark("exit-four");
    wait_for("the last turn to be over", || host.state("quick") == "done");
}
