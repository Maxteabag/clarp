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
# An initial hidden coordination prompt carries the reminder inside the team envelope.
opening = "--- Clarp team context ---\n"
closing = "\n--- End Clarp team context ---\n\n"
context, separator, request = text.partition(closing)
has_reminder = bool(separator) and context.startswith(
    opening + "Coordination: this turn answers a message from another agent")
if has_reminder:
    text = request
assert has_reminder == (text in ("case-mixed", "case-relayed"))
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
if mode == "mixed":
    say("assistant", message={"id": "msg_before_steer", "role": "assistant",
        "content": [{"type": "text", "text": "before-steer"}]})
    say("stream_event", event={"type": "message_stop"})
tool = "tool-" + mode
if mode in ("writing", "between-tools"):
    if mode == "between-tools":
        start("previous-tool", "Bash")
        returned("previous-tool")
    say("stream_event", event={
        "type": "content_block_start", "index": 0,
        "content_block": {"type": "text", "text": ""},
    })
    (root / ("writing-" + mode)).touch()
else:
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
    assert "Coordination: this turn answers a message from another agent" not in follow
    if mode == "large":
        identifier, _, content = follow.partition(" ")
        assert len(content) == 256 * 1024
        follow = identifier
    more.append((uid, follow))
    (root / ("received-" + follow)).write_text(uid)
release("release-" + mode)
if mode in ("race", "writing"):
    answer("first-" + mode, first)
    (root / ("result-" + mode)).touch()
    release("echo-" + mode)
    echo(more[0][0])
    answer("second-" + mode, more[0][0])
elif mode == "between-tools":
    start("next-tool", "Bash")
    returned("next-tool")
    echo(more[0][0])
    answer("joined-between-tools", first, more[0][0])
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
    if mode != "mixed":
        returned(tool)
    for uid, _ in more:
        echo(uid)
    if mode == "mixed":
        returned(tool)
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
            assert_eq!(filtered["turns"], json!([]), "{filtered}");
            if mode == "mixed" {
                assert_eq!(filtered["hidden_after"]["messages"], 2, "{filtered}");
            }
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
            page["latest_revision"].as_u64().unwrap()
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
            if let Some(cursor) = hidden {
                wait_for("the live audience to include the user", || {
                    host.get(&format!("/live?session={session}"))["turn"]["audience"] == "user"
                });
                let delta = host.get(&format!(
                    "/log?session={session}&after_revision={cursor}&coordination=hide"
                ));
                let rows = delta["turns"].as_array().unwrap();
                assert!(rows.iter().all(|row| row["audience"] == "user"), "{delta}");
                let followup_id = format!("u-add-{mode}-0");
                let followup = rows.iter().find(|row| row["id"] == followup_id).unwrap();
                assert!(rows.iter().all(|row| row["id"] != "u-first"), "{delta}");
                if mode == "mixed" {
                    assert_eq!(followup["hidden_before"]["messages"], 2, "{delta}");
                } else {
                    assert_eq!(followup["origin"], "agent");
                    assert_eq!(followup["sender_name"], "Desk");
                }
            }
            mark(&format!("answer-{mode}"));
            collect_answer(&host, &session, &format!("joined-{mode}"));
        }
        wait_for("the agent to finish", || host.state(&session) == "done");
        if mode == "mixed" {
            let all = host.get(&format!("/log?session={session}&coordination=show"));
            let rows = all["turns"].as_array().unwrap();
            let first = rows.iter().find(|row| row["id"] == "u-first").unwrap();
            assert_eq!(first["audience"], "agents");
            assert_eq!(first["origin"], "agent");
            assert_eq!(first["sender_name"], "sender");
            let early = rows
                .iter()
                .find(|row| row["text"] == "before-steer")
                .unwrap();
            assert_eq!(early["audience"], "agents");
            assert!(
                rows.iter()
                    .filter(|row| row["tool_name"] == "Bash")
                    .all(|row| row["audience"] == "agents"),
                "{all}"
            );
            let visible = host.get(&format!("/log?session={session}&coordination=hide"));
            let rows = visible["turns"].as_array().unwrap();
            assert_eq!(rows.len(), 2, "{visible}");
            assert_eq!(rows[0]["id"], "u-add-mixed-0");
            assert_eq!(rows[0]["hidden_before"]["messages"], 2);
            assert_eq!(rows[1]["text"], "joined-mixed");
            assert!(
                rows.iter().all(|row| row["audience"] == "user"),
                "{visible}"
            );
        }
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

#[test]
fn r7_plain_automatic_sends_wait_and_a_joined_oracle_keeps_the_user_notification() {
    let place = Place::new("r7");
    let claude = place.root.join("claude");
    program(&claude, CLAUDE);
    let host = place.start_in(
        "",
        &[],
        &[("CLARP_CLAUDE_PROGRAM", claude.to_str().unwrap())],
    );
    let session = "r7";
    host.post(
        "/agents",
        json!({"name":session, "session":session,"backend":"claude"}),
    );
    let events = host.listen();
    wait_for("the event stream", || {
        events
            .lock()
            .unwrap()
            .iter()
            .any(|event| event["type"] == "agent-roster")
    });
    let sent = host.send(session, "case-r7", "first");
    assert_eq!(sent["queued"], false);
    wait_for("the user's active tool", || {
        place.root.join("tool-r7").exists() && host.state(session) == "tool"
    });

    let background = ["automation", "schedule", "watcher", "heartbeat", "dreaming"];
    for origin in background.into_iter().chain(["oracle"]) {
        let id = format!("add-{origin}");
        let sent = host.post(
            "/send",
            json!({
                "session":session, "text":id, "client_msg_id":id, "origin":origin,
                "synthesize_audio":false,
            }),
        );
        assert_eq!(sent["queued"], true, "{origin}: {sent}");
    }
    wait_for("the Oracle follow-up to reach the running process", || {
        place.root.join("received-add-oracle").exists()
    });
    let launches = place.root.join("launches");
    assert_eq!(std::fs::read_to_string(&launches).unwrap(), "case-r7\n");
    let queue_path = format!("/turn-queue?session={session}");
    let queue = host.get(&queue_path);
    let waiting = queue["items"].as_array().unwrap();
    for origin in background {
        let id = format!("add-{origin}");
        assert!(
            waiting
                .iter()
                .any(|row| row["id"] == id && row["status"] == "queued"),
            "{queue}"
        );
    }
    assert!(
        waiting
            .iter()
            .any(|row| row["id"] == "add-oracle" && row["status"] == "started"),
        "{queue}"
    );

    std::fs::write(place.root.join("release-r7"), "").unwrap();
    wait_for("the Oracle echo in the original turn", || {
        host.turns(session)
            .iter()
            .any(|row| row["id"] == "u-add-oracle")
    });
    assert_eq!(std::fs::read_to_string(&launches).unwrap(), "case-r7\n");
    std::fs::write(place.root.join("answer-r7"), "").unwrap();
    wait_for("the original user's notification", || {
        events.lock().unwrap().iter().any(|event| {
            event["type"] == "user-notification"
                && event["session"] == session
                && event["cause_message_id"] == "u-first"
                && event["origin"] == "user"
                && event["preview"] == "joined-r7"
        })
    });
    wait_for("the waiting messages to finish", || {
        let turns = host.turns(session);
        turns
            .iter()
            .any(|row| row["role"] == "assistant" && row["text"] == "separate-add-dreaming")
            && host.get(&queue_path)["items"] == json!([])
    });
    let launched = std::fs::read_to_string(&launches).unwrap();
    assert_eq!(
        launched.lines().collect::<Vec<_>>(),
        [
            "case-r7",
            "add-automation",
            "add-schedule",
            "add-watcher",
            "add-heartbeat",
            "add-dreaming"
        ]
    );
}

#[test]
fn queue_status_distinguishes_a_steered_message_from_one_still_waiting() {
    let place = Place::new("queue-item-status");
    let claude = place.root.join("claude");
    program(&claude, CLAUDE);
    let host = place.start_in(
        "",
        &[],
        &[("CLARP_CLAUDE_PROGRAM", claude.to_str().unwrap())],
    );
    let session = "steer-status";
    let heard = host.listen();
    wait_for("the event stream", || {
        heard
            .lock()
            .unwrap()
            .iter()
            .any(|event| event["type"] == "agent-roster")
    });
    host.post(
        "/agents",
        json!({"name":session, "session":session, "backend":"claude"}),
    );
    host.send(session, "case-race", "first");
    wait_for("the stand-in tool to be active", || {
        place.root.join("tool-race").exists() && host.state(session) == "tool"
    });

    let joined = "add-race-0";
    host.send(session, joined, joined);
    wait_for("the follow-up on stdin", || {
        place.root.join(format!("received-{joined}")).exists()
    });
    host.post(
        "/send",
        json!({
            "session":session, "text":"Later", "client_msg_id":"later", "queue_if_busy":true,
        }),
    );

    let queue = || host.get(&format!("/turn-queue?session={session}"));
    let read = queue();
    let status = |id: &str| {
        read["items"]
            .as_array()
            .unwrap()
            .iter()
            .find(|row| row["id"] == id)
            .and_then(|row| row["status"].as_str())
    };
    assert_eq!(status(joined), Some("started"), "{read}");
    assert_eq!(status("later"), Some("queued"), "{read}");
    assert!(
        !host
            .turns(session)
            .iter()
            .any(|row| row["id"] == "u-add-race-0")
    );
    assert_eq!(host.ask("DELETE", "/turn-queue/later", None).0, 200);

    let before_release = heard.lock().unwrap().len();
    std::fs::write(place.root.join("release-race"), "").unwrap();
    collect_answer(&host, session, "first-race");
    wait_for("the first reply's transcript event", || {
        heard.lock().unwrap()[before_release..]
            .iter()
            .any(|event| event["session"] == session && event["type"] == "transcript-updated")
    });
    assert!(
        reads(&heard, session, joined).is_empty(),
        "no read has committed"
    );
    let before_echo = heard.lock().unwrap().len();
    std::fs::write(place.root.join("echo-race"), "").unwrap();
    collect_answer(&host, session, "second-race");
    assert!(
        host.turns(session)
            .iter()
            .any(|row| row["id"] == "u-add-race-0")
    );
    assert_eq!(queue()["items"], json!([]));
    wait_for("the committed follow-up's started event", || {
        !reads(&heard, session, joined).is_empty()
    });
    let heard = heard.lock().unwrap();
    let events = &heard[before_echo..];
    let started: Vec<_> = events
        .iter()
        .enumerate()
        .filter(|(_, event)| {
            event["type"] == "queue-updated"
                && event["session"] == session
                && event["client_msg_id"] == joined
                && event["queue_started"] == true
        })
        .collect();
    assert_eq!(started.len(), 1);
    let (at, event) = started[0];
    assert_eq!(event["origin"], "user");
    assert_eq!(event.get("sender"), Some(&Value::Null));
    assert!(
        events[..at]
            .iter()
            .any(|event| { event["type"] == "transcript-updated" && event["session"] == session }),
        "the transcript notification must precede the started event: {events:?}"
    );
}

#[test]
fn a_claude_follow_up_can_join_while_no_tool_call_is_open() {
    for mode in ["writing", "between-tools"] {
        let place = Place::new(mode);
        let claude = place.root.join("claude");
        program(&claude, CLAUDE);
        let world = [("CLARP_CLAUDE_PROGRAM", claude.to_str().unwrap())];
        let host = place.start_in("", &[], &world);
        let session = "open-input";
        host.post(
            "/agents",
            json!({"name":session,"session":session,"backend":"claude"}),
        );

        let initial = format!("case-{mode}");
        host.send(session, &initial, "first");
        wait_for("the model to generate with no tool open", || {
            place.root.join(format!("writing-{mode}")).exists()
        });
        let follow = format!("add-{mode}");
        assert_eq!(host.send(session, &follow, &follow)["queued"], true);
        wait_for("the follow-up on the running process's stdin", || {
            place.root.join(format!("received-{follow}")).exists()
        });
        let queue = host.get(&format!("/turn-queue?session={session}"));
        assert_eq!(queue["items"][0]["id"], follow);
        assert_eq!(queue["items"][0]["status"], "started");
        let row_id = format!("u-{follow}");
        assert!(!host.turns(session).iter().any(|row| row["id"] == row_id));

        std::fs::write(place.root.join(format!("release-{mode}")), "").unwrap();
        if mode == "writing" {
            collect_answer(&host, session, "first-writing");
            assert!(!host.turns(session).iter().any(|row| row["id"] == row_id));
            std::fs::write(place.root.join("echo-writing"), "").unwrap();
            collect_answer(&host, session, "second-writing");
        } else {
            collect_answer(&host, session, "joined-between-tools");
        }
        let rows = host.turns(session);
        assert_eq!(rows.iter().filter(|row| row["id"] == row_id).count(), 1);
        assert_eq!(
            std::fs::read_to_string(place.root.join("launches")).unwrap(),
            format!("{initial}\n")
        );
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

#[test]
fn claude_attempt_fails_when_its_input_is_completed_without_starting() {
    let place = Place::new("claude-skipped-input");
    let claude = place.root.join("claude");
    program(
        &claude,
        r#"#!/usr/bin/env python3
import json, sys

if "--version" in sys.argv:
    print("2.1.999")
    raise SystemExit(0)

def say(**fields):
    print(json.dumps(fields), flush=True)

prompt = json.loads(sys.stdin.readline())
uid = prompt["uuid"]
text = prompt["message"]["content"][0]["text"]
if text == "skip the input":
    say(type="user", isReplay=True, uuid=uid, message=prompt["message"])
    say(type="command_lifecycle", command_uuid=uid, state="completed")
    sys.stdin.read()
else:
    say(type="command_lifecycle", command_uuid="unrelated", state="completed")
    say(type="command_lifecycle", command_uuid=uid, state="started")
    say(type="command_lifecycle", command_uuid=uid, state="completed")
    say(type="assistant", message={"id":"msg_next", "role":"assistant",
                                   "content":[{"type":"text","text":"next ran"}]})
    say(type="result", subtype="success", is_error=False, result="next ran")
"#,
    );
    let host = place.start_in(
        "",
        &[],
        &[("CLARP_CLAUDE_PROGRAM", claude.to_str().unwrap())],
    );
    let session = "skip-check";
    host.post(
        "/agents",
        json!({"name":"Skip check","session":session,"backend":"claude"}),
    );
    host.send(session, "skip the input", "skipped");
    wait_for("the skipped input to fail", || {
        let rows = host.turns(session);
        rows.iter()
            .any(|row| row["id"] == "u-skipped" && row["turn"]["status"] == "failed")
            && rows.iter().any(|row| {
                row["text"]
                    .as_str()
                    .is_some_and(|text| text.contains("without starting it"))
            })
    });
    host.send(session, "try another", "next");
    collect_answer(&host, session, "next ran");
}
