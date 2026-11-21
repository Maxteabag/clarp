//! Issue 491. The Host, shim, and a disposable Claude stream talk through their actual ports.

use super::*;

const CLAUDE: &str = r#"#!/usr/bin/env python3
import json
import pathlib
import sys
import time

if "--version" in sys.argv:
    print("2.1.321 (Claude Code)")
    raise SystemExit(0)

root = pathlib.Path(__file__).resolve().parent
if "--replay-user-messages" not in sys.argv:
    raise SystemExit("replay receipts were not requested")
flag = "--resume" if "--resume" in sys.argv else "--session-id"
session = sys.argv[sys.argv.index(flag) + 1]

def say(kind, **fields):
    print(json.dumps({"type": kind, "session_id": session, **fields}), flush=True)

def prompt():
    record = json.loads(sys.stdin.readline())
    assert record.get("uuid")
    return record["uuid"], record["message"]["content"][0]["text"]

def echo(uid):
    say("user", isReplay=True, uuid=uid, message={"role": "user", "content": []})

def start(tool, name, parent=None):
    say("stream_event", parent_tool_use_id=parent, event={
        "type": "content_block_start", "index": 0,
        "content_block": {"type": "tool_use", "id": tool, "name": name, "input": {}},
    })

def returned(tool, parent=None):
    say("user", parent_tool_use_id=parent,
        message={"content": [{"type": "tool_result", "tool_use_id": tool, "content": "tick"}]})

def answer(text, *uids):
    say("assistant", message={"id": "msg_" + text, "role": "assistant",
        "content": [{"type": "text", "text": text}]})
    say("result", subtype="success", is_error=False, result=text, user_message_uuids=uids)

def release(name):
    end = time.monotonic() + 40
    while not (root / name).exists():
        if time.monotonic() >= end:
            raise SystemExit("stand-in release expired")
        time.sleep(.02)

first, text = prompt()
with (root / "launches").open("a") as rows:
    rows.write(text + "\n")
say("system", subtype="init")
echo(first)
if text.startswith("add-"):
    answer("separate-" + text, first)
    raise SystemExit(0)

mode = text.removeprefix("case-")
tool = "tool-" + mode
start(tool, "Task" if mode == "task" else "Bash")
if mode == "task":
    start("child", "Bash", tool)
    returned("child", tool)
(root / ("tool-" + mode)).touch()
more = []
for index in range(2 if mode == "double" else 1):
    uid, follow = prompt()
    more.append((uid, follow))
    (root / ("received-" + follow)).write_text(uid)
release("release-" + mode)
if mode == "race":
    answer("first-race", first)
    (root / "result-race").touch()
    release("echo-race")
    echo(more[0][0])
    answer("second-race", more[0][0])
elif mode == "unread":
    answer("first-unread", first)
else:
    returned(tool)
    for uid, _ in more:
        echo(uid)
    (root / ("echoed-" + mode)).touch()
    release("answer-" + mode)
    answer("joined-" + mode, first, *(uid for uid, _ in more))
"#;

#[test]
fn a_claude_follow_up_enters_at_the_echo_and_is_answered_once() {
    let place = Place::new("steer-c");
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
    let mut host = place.start_in("", &[], &world);
    let subscribe = |host: &Host| {
        let heard = host.listen();
        wait_for("the event stream", || {
            heard
                .lock()
                .unwrap()
                .iter()
                .any(|event| event["type"] == "agent-roster")
        });
        heard
    };
    let mut heard = subscribe(&host);
    let sender = host.new_agent("sender");
    let await_file = |name: &str| {
        wait_for(name, || place.root.join(name).exists());
    };
    let mark = |name: &str| std::fs::write(place.root.join(name), "").unwrap();
    let launches = || std::fs::read_to_string(place.root.join("launches")).unwrap();
    let ids = |host: &Host, session: &str| {
        host.turns(session)
            .into_iter()
            .filter(|row| row["role"] == "user")
            .map(|row| row["id"].as_str().unwrap().to_owned())
            .collect::<Vec<_>>()
    };

    for mode in [
        "race", "task", "double", "unread", "stop", "restart", "mixed",
    ] {
        let session = format!("steer-{mode}");
        let made = host.post(
            "/agents",
            json!({
                "name": session, "session": session, "backend": "claude",
            }),
        );
        assert_eq!(made["session"], session);
        let case = format!("case-{mode}");
        let first = if mode == "mixed" {
            host.post(
                "/send",
                json!({
                    "session": session, "text": case, "client_msg_id": "first",
                    "origin": "agent", "sender": sender,
                }),
            )
        } else {
            host.send(&session, &case, "first")
        };
        assert_eq!(first["queued"], false, "{first}");
        await_file(&format!("tool-{mode}"));
        // Let the real Host observe that the tool is active before the follow-up comes.
        wait_for("the tool in the live projection", || {
            host.state(&session) == "tool"
        });
        let mut expected = vec!["u-first".to_owned()];
        let mut traces = Vec::new();
        for index in 0..if mode == "double" { 2 } else { 1 } {
            let id = format!("add-{mode}-{index}");
            let sent = host.send(&session, &id, &id);
            assert_eq!(sent["queued"], true, "{sent}");
            traces.push(sent["trace_id"].clone());
            await_file(&format!("received-{id}"));
            assert_eq!(
                ids(&host, &session),
                ["u-first"],
                "the pipe write is no read"
            );
            assert!(reads(&heard, &session, &id).is_empty());
            assert_eq!(
                host.ask("PUT", &format!("/turn-queue/{id}"), Some(&json!({"text":"new"}))).0,
                404
            );
            assert_eq!(host.ask("DELETE", &format!("/turn-queue/{id}"), None).0, 404);
            expected.push(format!("u-{id}"));
        }
        if mode == "stop" {
            host.post("/stop", json!({"session":session}));
            wait_for("the unread follow-up to be held", || {
                let queue = host.get(&format!("/turn-queue?session={session}"));
                queue["paused"] == true && queue["items"][0]["id"] == "add-stop-0"
            });
            assert_eq!(ids(&host, &session), ["u-first"]);
            host.post("/turn-queue/resume", json!({"session":session}));
        } else {
            if mode == "restart" {
                host.kill();
                host = place.start_in("", &[], &world);
                heard = subscribe(&host);
                assert_eq!(ids(&host, &session), ["u-first"]);
            }
            mark(&format!("release-{mode}"));
        }
        if mode == "race" {
            await_file("result-race");
            collect_answer(&host, &session, "first-race");
            assert_eq!(ids(&host, &session), ["u-first"]);
            mark("echo-race");
            collect_answer(&host, &session, "second-race");
            let rows = host.turns(&session);
            let trace_for =
                |text: &str| &rows.iter().find(|row| row["text"] == text).unwrap()["trace_id"];
            assert_eq!(trace_for("first-race"), &first["trace_id"]);
            assert_eq!(trace_for("second-race"), &traces[0]);
        } else if matches!(mode, "stop" | "unread") {
            let text = format!("separate-add-{mode}-0");
            collect_answer(&host, &session, &text);
        } else {
            await_file(&format!("echoed-{mode}"));
            wait_for("each read in the conversation", || {
                ids(&host, &session) == expected
            });
            if mode == "mixed" {
                let live = host.get(&format!("/live?session={session}"));
                assert_eq!(live["turn"]["audience"], "mixed", "{live}");
            }
            mark(&format!("answer-{mode}"));
            collect_answer(&host, &session, &format!("joined-{mode}"));
        }
        wait_for("the agent to finish", || host.state(&session) == "done");
        assert_eq!(ids(&host, &session), expected);
        assert_eq!(launches().lines().filter(|text| *text == case).count(), 1);
        let mut revision = 0;
        for index in 0..if mode == "double" { 2 } else { 1 } {
            let id = format!("add-{mode}-{index}");
            wait_for("the queue-start for the read follow-up", || {
                !reads(&heard, &session, &id).is_empty()
            });
            let started = reads(&heard, &session, &id);
            assert_eq!(started.len(), 1);
            assert!(started[0] > revision);
            revision = started[0];
            assert_eq!(
                launches().lines().filter(|text| *text == id).count(),
                usize::from(matches!(mode, "stop" | "unread"))
            );
        }
    }
}

fn reads(heard: &Mutex<Vec<Value>>, session: &str, id: &str) -> Vec<u64> {
    heard
        .lock()
        .unwrap()
        .iter()
        .filter(|event| {
            event["type"] == "queue-updated"
                && event["session"] == session
                && event["client_msg_id"] == id
                && event["queue_started"] == true
        })
        .map(|event| event["queue_revision"].as_u64().unwrap())
        .collect()
}

fn collect_answer(host: &Host, session: &str, answer: &str) {
    wait_for(answer, || {
        host.conversation(session)
            .iter()
            .any(|(role, text)| role == "assistant" && text == answer)
    });
}
