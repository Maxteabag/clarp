//! Issue 491. The Host, shim, and a disposable Claude stream talk through their actual ports.

use super::*;

const CLAUDE: &str = r#"#!/usr/bin/env python3
import json
import os
import pathlib
import re
import sys
import time

if "--version" in sys.argv:
    print("2.1.321 (Claude Code)")
    raise SystemExit(0)

root = pathlib.Path(__file__).resolve().parent
replay = "--replay-user-messages" in sys.argv
flag = "--resume" if "--resume" in sys.argv else "--session-id"
session = sys.argv[sys.argv.index(flag) + 1]

def say(kind, **fields):
    print(json.dumps({"type": kind, "session_id": session, **fields}), flush=True)

def prompt():
    record = json.loads(sys.stdin.readline())
    assert record.get("uuid")
    return record["uuid"], record["message"]["content"][0]["text"]

def echo(uid):
    if replay:
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
if text == "case-race":
    (root / "steering-argv.json").write_text(json.dumps(sys.argv[1:]))
say("system", subtype="init")
echo(first)
if text.startswith("add-"):
    answer("separate-" + text, first)
    raise SystemExit(0)

mode = text.removeprefix("case-")
if mode == "desk":
    (root / "desk-token").write_text(os.environ["CLARP_PROVIDER_TURN"])
    release("release-desk")
    answer("desk-finished", first)
    raise SystemExit(0)
tool = "tool-" + mode
start(tool, "Task" if mode == "task" else "Bash")
if mode == "task":
    start("child", "Bash", tool)
    returned("child", tool)
(root / ("tool-" + mode)).touch()
if mode == "large":
    release("flood-large")
    for _ in range(96):
        say("system", subtype="fixture-padding", padding="x" * 1024)
    start("large-producer-advanced", "LargeProducer")
    release("read-large")
more = []
for index in range(2 if mode == "double" else 1):
    uid, follow = prompt()
    if mode == "large":
        identifier, _, content = follow.partition(" ")
        assert len(content) == 256 * 1024
        follow = identifier
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
elif mode == "ledger":
    project = re.sub(r"[^a-zA-Z0-9]", "-", os.getcwd())
    transcript = pathlib.Path.home() / ".claude" / "projects" / project / (session + ".jsonl")
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text(json.dumps({"type": "attachment", "attachment": {
        "type": "queued_command", "source_uuid": more[0][0]}}) + "\n")
    answer("read-in-the-session-file", first, more[0][0])
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
    let owners = place.root.join("owner.toml");
    std::fs::write(&owners, "[agents]\nfront_desk_sessions = ['desk']\n").unwrap();
    let args = ["--speech-keys", owners.to_str().unwrap()];
    let mut host = place.start_in("", &args, &world);
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
        "race", "task", "double", "unread", "stop", "restart", "mixed", "relayed", "large",
        "ledger",
    ] {
        let session = format!("steer-{mode}");
        let made = host.post(
            "/agents",
            json!({
                "name": session, "session": session, "backend": "claude",
            }),
        );
        assert_eq!(made["session"], session);
        let relay_token = if mode == "relayed" {
            host.post(
                "/agents",
                json!({"name":"Desk","session":"desk","backend":"claude"}),
            );
            host.send("desk", "case-desk", "desk-owner");
            await_file("desk-token");
            Some(std::fs::read_to_string(place.root.join("desk-token")).unwrap())
        } else {
            None
        };
        let case = format!("case-{mode}");
        let first = if matches!(mode, "mixed" | "relayed") {
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
        let hidden = matches!(mode, "mixed" | "relayed").then(|| {
            let filtered = host.get(&format!("/log?session={session}&coordination=hide"));
            assert!(
                !filtered["turns"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .any(|row| row["id"] == "u-first")
            );
            let page = host.get(&format!("/log?session={session}"));
            let first = page["turns"]
                .as_array()
                .unwrap()
                .iter()
                .find(|row| row["id"] == "u-first")
                .unwrap();
            assert_eq!(first["audience"], "agents");
            assert_eq!(
                host.get(&format!("/live?session={session}"))["turn"]["audience"],
                "agents"
            );
            (
                page["latest_revision"].as_u64().unwrap(),
                first["sender_agent_id"].clone(),
            )
        });
        let mut expected = vec!["u-first".to_owned()];
        let mut traces = Vec::new();
        for index in 0..if mode == "double" { 2 } else { 1 } {
            let id = format!("add-{mode}-{index}");
            let text = if mode == "large" {
                format!("{id} {}", "x".repeat(256 * 1024))
            } else {
                id.clone()
            };
            let sent = if let Some(token) = &relay_token {
                let body = json!({"session":session,"text":text,"client_msg_id":id});
                let reply = fetch(
                    &host,
                    &host.token,
                    "POST",
                    "/send",
                    &[
                        ("Content-Type", "application/json"),
                        ("X-Clarp-Turn", token),
                    ],
                    body.to_string().as_bytes(),
                );
                assert_eq!(reply.status, 200);
                mark("release-desk");
                serde_json::from_slice(&reply.body).unwrap()
            } else {
                host.send(&session, &text, &id)
            };
            assert_eq!(sent["queued"], true, "{sent}");
            traces.push(sent["trace_id"].clone());
            if mode == "large" {
                mark("flood-large");
                wait_for(
                    "stdout made progress while the stdin reader was still held",
                    || {
                        host.get("/agents/snapshot")["agents"]
                            .as_array()
                            .unwrap()
                            .iter()
                            .any(|row| {
                                row["session"] == session
                                    && row["activity"]["tool"] == "LargeProducer"
                            })
                    },
                );
                mark("read-large");
            }
            await_file(&format!("received-{id}"));
            assert_eq!(
                ids(&host, &session),
                ["u-first"],
                "the pipe write is no read"
            );
            assert!(reads(&heard, &session, &id).is_empty());
            assert_eq!(
                host.ask(
                    "PUT",
                    &format!("/turn-queue/{id}"),
                    Some(&json!({"text":"new"}))
                )
                .0,
                404
            );
            assert_eq!(
                host.ask("DELETE", &format!("/turn-queue/{id}"), None).0,
                404
            );
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
            if matches!(mode, "restart" | "ledger") {
                host.kill();
                host = place.start_in("", &args, &world);
                heard = subscribe(&host);
                assert_eq!(ids(&host, &session), ["u-first"]);
            }
            mark(&format!("release-{mode}"));
        }
        if mode == "race" {
            let argv: Vec<String> = serde_json::from_slice(
                &std::fs::read(place.root.join("steering-argv.json")).unwrap(),
            )
            .unwrap();
            assert!(argv.iter().any(|arg| arg == "--replay-user-messages"));
            assert!(
                argv.windows(2)
                    .any(|pair| pair == ["--input-format", "stream-json"])
            );
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
        } else if mode == "ledger" {
            collect_answer(&host, &session, "read-in-the-session-file");
        } else if matches!(mode, "stop" | "unread") {
            let text = format!("separate-add-{mode}-0");
            collect_answer(&host, &session, &text);
        } else {
            await_file(&format!("echoed-{mode}"));
            wait_for("each read in the conversation", || {
                ids(&host, &session) == expected
            });
            if let Some((cursor, sender)) = &hidden {
                wait_for("the live audience to include the user", || {
                    host.get(&format!("/live?session={session}"))["turn"]["audience"] == "user"
                });
                let delta = host.get(&format!(
                    "/log?session={session}&after_revision={cursor}&coordination=hide"
                ));
                let prior = delta["turns"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .find(|row| row["id"] == "u-first")
                    .unwrap();
                assert_eq!(prior["audience"], "user");
                assert_eq!(&prior["sender_agent_id"], sender);
                assert_eq!(prior["origin"], "agent");
                if relay_token.is_some() {
                    let relay = delta["turns"]
                        .as_array()
                        .unwrap()
                        .iter()
                        .find(|row| row["id"] == "u-add-relayed-0")
                        .unwrap();
                    assert_eq!(relay["origin"], "agent");
                    assert_eq!(relay["sender_name"], "Desk");
                    assert_eq!(relay["audience"], "user");
                }
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
