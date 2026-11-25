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

def carried(tool, name, message):
    # The whole message that carries a call, as Claude sends it after the streamed start.
    say("assistant", message={"id": message, "role": "assistant", "content": [
        {"type": "tool_use", "id": tool, "name": name, "input": {}}]})

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
assert has_reminder == (text == "case-relayed")
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
if mode == "answered":
    # The first message is answered whole; the follow-up comes after that answer, and the
    # work it asks for begins with a tool.
    say("assistant", message={"id": "msg_first_answer", "role": "assistant",
        "content": [{"type": "text", "text": "first-answered"}]})
    say("stream_event", event={"type": "message_stop"})
    (root / "answered-first").touch()
    uid, follow = prompt()
    (root / ("received-" + follow)).write_text(uid)
    echo(uid)
    start("tool-answered", "Bash")
    carried("tool-answered", "Bash", "msg_answered_call")
    (root / "tool-answered").touch()
    release("release-answered")
    returned("tool-answered")
    answer("joined-answered", first, uid)
    raise SystemExit(0)
if mode in ("answered-separate", "answered-restart"):
    # The first message is answered, and the answer's result comes after the follow-up was
    # written but before it is read: the follow-up is a turn of its own in the same process.
    # In the second the Host is started again between the follow-up's read and its first tool.
    tag = mode.removeprefix("answered-")
    say("assistant", message={"id": "msg_first_" + tag, "role": "assistant",
        "content": [{"type": "text", "text": "first-" + tag}]})
    say("stream_event", event={"type": "message_stop"})
    (root / ("answered-first-" + tag)).touch()
    uid, follow = prompt()
    (root / ("received-" + follow)).write_text(uid)
    say("result", subtype="success", is_error=False, result="first-" + tag,
        user_message_uuids=[first])
    echo(uid)
    if tag == "restart":
        (root / "echoed-restart").touch()
        release("go-restart")
    start("tool-" + tag, "Bash")
    carried("tool-" + tag, "Bash", "msg_" + tag + "_call")
    (root / ("tool-answered-" + tag)).touch()
    release("release-answered-" + tag)
    returned("tool-" + tag)
    answer("second-" + tag, uid)
    raise SystemExit(0)
if mode in ("narrated", "narrated-restart"):
    # Words that announce a call, then the call: the follow-up is read when the call returns,
    # and more work follows for it. In the second the Host is started again between the
    # follow-up's read and the next call.
    say("assistant", message={"id": "msg_narration", "role": "assistant", "content": [
        {"type": "text", "text": "narrated-before"},
        {"type": "tool_use", "id": "tool-narrated-1", "name": "Bash", "input": {}}]})
    (root / "narrated-first").touch()
    uid, follow = prompt()
    (root / ("received-" + follow)).write_text(uid)
    returned("tool-narrated-1")
    echo(uid)
    if mode == "narrated-restart":
        (root / "echoed-narrated").touch()
        release("go-narrated")
    start("tool-narrated-2", "Bash")
    carried("tool-narrated-2", "Bash", "msg_narrated_call")
    (root / "tool-narrated").touch()
    release("release-narrated")
    returned("tool-narrated-2")
    answer("joined-narrated", first, uid)
    raise SystemExit(0)
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
        "race", "task", "double", "unread", "stop", "restart", "relayed", "large", "ledger",
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
        // The user's own message never reaches another agent's turn this way: it takes that
        // turn's place (issue 839, `owner_preempts.rs`). A front desk's relay of it does.
        let first = if mode == "relayed" {
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
        let hidden = (mode == "relayed").then(|| {
            let filtered = host.get(&format!("/log?session={session}&coordination=hide"));
            assert_eq!(filtered["turns"], json!([]), "{filtered}");
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
            // The unread user follow-up now starts when the stopped process ends.
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
                assert_eq!(followup["origin"], "agent");
                assert_eq!(followup["sender_name"], "Desk");
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

#[test]
fn an_answer_given_before_a_steered_message_is_never_hidden_by_the_work_for_it() {
    // The answer, the follow-up, the file that says the follow-up's tool runs, and what the
    // turn says last. In the second the answer ends with a result before the follow-up is
    // read, so the follow-up is a turn of its own in the same process. The third is the second
    // with the Host started again between that read and the follow-up's first tool (issue
    // 743): the Host that follows the turn from its spool closes the answer as the one that
    // read the follow-up did.
    for (mode, answer, tool, last) in [
        (
            "answered",
            "first-answered",
            "tool-answered",
            "joined-answered",
        ),
        (
            "answered-separate",
            "first-separate",
            "tool-answered-separate",
            "second-separate",
        ),
        (
            "answered-restart",
            "first-restart",
            "tool-answered-restart",
            "second-restart",
        ),
    ] {
        let place = Place::new(mode);
        let claude = place.root.join("claude");
        program(&claude, CLAUDE);
        let world = [("CLARP_CLAUDE_PROGRAM", claude.to_str().unwrap())];
        let mut host = place.start_in("", &[], &world);
        let session = "answered";
        host.post(
            "/agents",
            json!({"name":session,"session":session,"backend":"claude"}),
        );
        host.send(session, &format!("case-{mode}"), "first");
        wait_for("the answer", || {
            live_phase_of(&host, session, answer).is_some()
                || stored_phase_of(&host, session, answer).is_some()
        });
        let follow = format!("add-{mode}");
        assert_eq!(host.send(session, &follow, &follow)["queued"], true);
        if mode == "answered-restart" {
            // The read of the follow-up is in the log, and the Host goes down before the tool
            // it asks for begins. The turn's process goes on; the next Host takes it up.
            let row_id = format!("u-{follow}");
            wait_for("the follow-up read into the turn's process", || {
                place.root.join("echoed-restart").exists()
                    && host.turns(session).iter().any(|row| row["id"] == row_id)
            });
            host.kill();
            host = place.start_in("", &[], &world);
            std::fs::write(place.root.join("go-restart"), "").unwrap();
        }
        wait_for("the follow-up read, and the tool it asked for", || {
            place.root.join(tool).exists()
        });
        wait_for("the tool in the live projection", || {
            host.state(session) == "tool"
        });
        // The user was shown the answer: the work for the new message makes it no narration,
        // neither for those who watch the turn nor in what is kept.
        assert_eq!(
            live_phase_of(&host, session, answer).as_deref(),
            Some("final"),
            "{mode}: live, while the tool runs"
        );
        assert_eq!(
            stored_phase_of(&host, session, answer).as_deref(),
            Some("final"),
            "{mode}: kept, while the tool runs"
        );
        // And it is the answer to the first message in the conversation view from the moment
        // it was written: the work for the follow-up neither hides it now nor at the end.
        assert_eq!(
            view_of(&host, session, answer),
            Some(json!("conversation")),
            "{mode}: in the conversation, while the tool runs"
        );
        // The turn the follow-up was read into says so: an app puts its working line below
        // the follow-up's row (issue 808).
        if mode == "answered" {
            let live = host.get(&format!("/live?session={session}"));
            let read = live["turn"]["read_rows"]
                .as_array()
                .cloned()
                .unwrap_or_default();
            assert!(
                read.iter().any(|row| row == &json!(format!("u-{follow}"))),
                "{mode}: {live}"
            );
            // And the follow-up's own row says so, without the live turn (issue 845): the
            // running turn's id, which its own message's row has as its trace. The message the
            // turn began for names no turn.
            let rows = host.turns(session);
            let row = |id: &str| rows.iter().find(|row| row["id"] == id).cloned();
            let turn_id = live["turn"]["turn_id"].clone();
            let own = row("u-first").unwrap();
            assert_eq!(own["trace_id"], turn_id, "{mode}: {own}");
            assert!(own.get("read_into").is_none(), "{mode}: {own}");
            let steered = row(&format!("u-{follow}")).unwrap();
            assert_eq!(
                steered["read_into"]["trace_id"], turn_id,
                "{mode}: {steered}"
            );
            assert!(steered["read_into"]["turn"].is_u64(), "{mode}: {steered}");
        }
        std::fs::write(place.root.join(format!("release-{mode}")), "").unwrap();
        collect_answer(&host, session, last);
        wait_for("the follow-up's answer in the conversation", || {
            view_of(&host, session, last) == Some(json!("conversation"))
        });
        assert_eq!(
            stored_phase_of(&host, session, answer).as_deref(),
            Some("final"),
            "{mode}: after the turn"
        );
        wait_for("the turn ended", || host.state(session) == "done");
        let rows = host.get(&format!("/log?session={session}&view=conversation"))["turns"]
            .as_array()
            .unwrap()
            .iter()
            .map(|row| row["text"].as_str().unwrap_or_default().to_owned())
            .collect::<Vec<_>>();
        assert_eq!(
            rows,
            [
                format!("case-{mode}"),
                answer.to_owned(),
                follow,
                last.to_owned()
            ],
            "{mode}: each message with its own answer, after the turn"
        );
    }
}

#[test]
fn words_that_announce_a_call_are_said_along_the_way_also_when_a_message_is_steered_in() {
    // In the second the Host is started again between the follow-up's read and the next call:
    // the Host that follows the turn from its spool still has the words as said along the way,
    // as the one that read the follow-up had them, and the next call makes them so.
    for mode in ["narrated", "narrated-restart"] {
        let place = Place::new(mode);
        let claude = place.root.join("claude");
        program(&claude, CLAUDE);
        let world = [("CLARP_CLAUDE_PROGRAM", claude.to_str().unwrap())];
        let mut host = place.start_in("", &[], &world);
        let session = "narrated";
        host.post(
            "/agents",
            json!({"name":session,"session":session,"backend":"claude"}),
        );
        host.send(session, &format!("case-{mode}"), "first");
        wait_for("the words and their call", || {
            place.root.join("narrated-first").exists()
        });
        wait_for("the call in the live projection", || {
            host.state(session) == "tool"
        });
        let follow = format!("add-{mode}");
        assert_eq!(host.send(session, &follow, &follow)["queued"], true);
        if mode == "narrated-restart" {
            // The read of the follow-up is in the log, and the Host goes down before the next
            // call begins. The turn's process goes on; the next Host takes it up.
            let row_id = format!("u-{follow}");
            wait_for("the follow-up read into the turn's process", || {
                place.root.join("echoed-narrated").exists()
                    && host.turns(session).iter().any(|row| row["id"] == row_id)
            });
            host.kill();
            host = place.start_in("", &[], &world);
            std::fs::write(place.root.join("go-narrated"), "").unwrap();
        }
        wait_for("the follow-up read, and the next call", || {
            place.root.join("tool-narrated").exists()
        });
        // Those who watch the turn see the same: the next call makes the words narration.
        wait_for("the words said along the way, live", || {
            live_phase_of(&host, session, "narrated-before").as_deref() == Some("commentary")
        });
        std::fs::write(place.root.join("release-narrated"), "").unwrap();
        collect_answer(&host, session, "joined-narrated");
        // What announced the first call was said along the way: the work after it makes it
        // so, whatever was read in between, and whether or not the Host was started again
        // before that work. (The reply that carries the next call is written once its message
        // is whole, after the call returns: what is kept says so from then on.)
        wait_for("the turn's answer in the conversation", || {
            view_of(&host, session, "joined-narrated") == Some(json!("conversation"))
        });
        assert_eq!(
            stored_phase_of(&host, session, "narrated-before").as_deref(),
            Some("commentary"),
            "{mode}"
        );
    }
}

/// The row with this text as the conversation keeps it: its view, if it is there at all.
fn view_of(host: &Host, session: &str, text: &str) -> Option<Value> {
    host.get(&format!("/log?session={session}"))["turns"]
        .as_array()
        .unwrap()
        .iter()
        .find(|row| row["text"] == text)
        .map(|row| row["view"].clone())
}

/// Whether the kept row with this text was said along the way or at the end.
fn stored_phase_of(host: &Host, session: &str, text: &str) -> Option<String> {
    host.get(&format!("/log?session={session}"))["turns"]
        .as_array()
        .unwrap()
        .iter()
        .find(|row| row["text"] == text)
        .and_then(|row| row["phase"].as_str().map(str::to_string))
}

/// The phase of the message with this text, as those who watch the running turn are shown it.
fn live_phase_of(host: &Host, session: &str, text: &str) -> Option<String> {
    host.get(&format!("/live?session={session}"))["items"]
        .as_array()?
        .iter()
        .find(|item| item["kind"] == "message" && item["text"] == text)
        .and_then(|item| item["phase"].as_str().map(str::to_string))
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

/// A stand-in for Claude Code for rule FU: a turn for another agent, into which the user's
/// follow-up is read; after the test leaves a note in the turn, the agent writes its reply and
/// waits before it ends.
const CLAUDE_FU: &str = r#"#!/usr/bin/env python3
import json, os, pathlib, sys, time

if "--version" in sys.argv:
    print("2.1.321 (Claude Code)")
    raise SystemExit(0)

root = pathlib.Path(__file__).resolve().parent
flag = "--resume" if "--resume" in sys.argv else "--session-id"
session = sys.argv[sys.argv.index(flag) + 1]

def say(kind, **fields):
    print(json.dumps({"type": kind, "session_id": session, **fields}), flush=True)

def prompt():
    record = json.loads(sys.stdin.readline())
    return record["uuid"], record["message"]["content"][0]["text"]

def release(name):
    end = time.monotonic() + 40
    while not (root / name).exists():
        if time.monotonic() >= end:
            raise SystemExit("stand-in release expired")
        time.sleep(.02)

first, text = prompt()
if text == "case-desk":
    # The front desk: its turn relays the user's words, and ends when told.
    (root / "desk-token").write_text(os.environ["CLARP_PROVIDER_TURN"])
    say("system", subtype="init")
    say("user", isReplay=True, uuid=first, message={"role": "user", "content": []})
    release("release-desk")
    say("assistant", message={"id": "msg_desk", "role": "assistant",
        "content": [{"type": "text", "text": "desk-finished"}]})
    say("result", subtype="success", is_error=False, result="desk-finished",
        user_message_uuids=[first])
    raise SystemExit(0)
(root / "fu-token").write_text(os.environ["CLARP_PROVIDER_TURN"])
say("system", subtype="init")
say("user", isReplay=True, uuid=first, message={"role": "user", "content": []})
say("stream_event", event={"type": "content_block_start", "index": 0,
    "content_block": {"type": "tool_use", "id": "fu-tool", "name": "Bash", "input": {}}})
(root / "fu-tool").touch()
followup, _ = prompt()
say("user", isReplay=True, uuid=followup, message={"role": "user", "content": []})
say("user", message={"content": [{"type": "tool_result", "tool_use_id": "fu-tool", "content": "ok"}]})
(root / "fu-echoed").touch()
release("fu-noted")
say("assistant", message={"id": "msg_after_note", "role": "assistant",
    "content": [{"type": "text", "text": "after-note"}]})
say("stream_event", event={"type": "message_stop"})
(root / "fu-replied").touch()
release("fu-end")
say("result", subtype="success", is_error=False, result="after-note",
    user_message_uuids=[first, followup])
"#;

/// Rule FU. The user's words, relayed by the front desk, reach a turn the agent runs for
/// another agent, and the agent answers the user in a note. (The user's own message takes such
/// a turn's place instead, issue 839.) What it says after the note is the agents' again: it is
/// hidden from the user while it is written and after the turn ends alike. Before, the reply
/// was shown while the turn ran and hidden once it ended: the user's chat lost the message it
/// had just shown.
#[test]
fn a_reply_after_the_note_that_answered_the_user_is_never_shown_then_hidden() {
    let place = Place::new("steer-fu");
    let claude = place.root.join("claude");
    program(&claude, CLAUDE_FU);
    let home = place.root.join("home");
    let world = [
        ("HOME", home.to_str().unwrap()),
        ("CLARP_CLAUDE_PROGRAM", claude.to_str().unwrap()),
    ];
    let owners = place.root.join("owner.toml");
    std::fs::write(&owners, "[agents]\nfront_desk_sessions = ['desk']\n").unwrap();
    let host = place.start_in("", &["--speech-keys", owners.to_str().unwrap()], &world);
    let sender = host.new_agent("sender");
    let session = "steer-fu";
    host.post(
        "/agents",
        json!({"name": "Desk", "session": "desk", "backend": "claude"}),
    );
    host.send("desk", "case-desk", "desk-owner");
    host.post(
        "/agents",
        json!({"name": session, "session": session, "backend": "claude"}),
    );
    let at = |name: &str| place.root.join(name);
    let first = host.post(
        "/send",
        json!({
            "session": session, "text": "case-fu", "client_msg_id": "first",
            "origin": "agent", "sender": sender,
        }),
    );
    assert_eq!(first["queued"], false, "{first}");
    wait_for("the tool", || at("fu-tool").exists());
    wait_for("the tool in the live projection", || {
        host.state(session) == "tool"
    });
    wait_for("the desk's turn", || place.root.join("desk-token").exists());
    let desk = std::fs::read_to_string(place.root.join("desk-token")).unwrap();
    let body =
        json!({"session": session, "text": "a question of the user's", "client_msg_id": "steer"});
    let relayed = fetch(
        &host,
        &host.token,
        "POST",
        "/send",
        &[
            ("Content-Type", "application/json"),
            ("X-Clarp-Turn", &desk),
        ],
        body.to_string().as_bytes(),
    );
    assert_eq!(relayed.status, 200);
    std::fs::write(place.root.join("release-desk"), "").unwrap();
    wait_for("the follow-up read into the turn", || {
        at("fu-echoed").exists()
    });
    wait_for("the user's live turn", || {
        host.get(&format!("/live?session={session}"))["turn"]["audience"] == "user"
    });
    let shown = |text: &str| {
        host.get(&format!("/log?session={session}&coordination=hide"))["turns"]
            .as_array()
            .unwrap()
            .iter()
            .any(|row| row["text"] == text)
    };
    assert!(shown("a question of the user's"));

    // The agent answers the user in a note: the rest of the turn is the agents' again, in the
    // live projection too.
    let token = std::fs::read_to_string(at("fu-token")).unwrap();
    let body = json!({"session": session, "text": "the answer for the user", "note_id": "answer"});
    let noted = fetch(
        &host,
        &host.token,
        "POST",
        "/agent-notes",
        &[
            ("Content-Type", "application/json"),
            ("X-Clarp-Turn", &token),
        ],
        body.to_string().as_bytes(),
    );
    assert_eq!(
        noted.status,
        200,
        "{}",
        String::from_utf8_lossy(&noted.body)
    );
    assert!(shown("the answer for the user"));
    wait_for("the live turn given back to the agents", || {
        host.get(&format!("/live?session={session}"))["turn"]["audience"] == "agents"
    });

    // The reply comes while the turn still runs: hidden from the user already.
    std::fs::write(at("fu-noted"), "").unwrap();
    wait_for("the reply in the conversation", || {
        host.get(&format!("/log?session={session}&coordination=show"))["turns"]
            .as_array()
            .unwrap()
            .iter()
            .any(|row| row["text"] == "after-note")
    });
    assert!(
        !shown("after-note"),
        "the reply is the agents' while the turn runs"
    );

    // The turn ends: what the user was shown stays, and what was hidden stays hidden.
    std::fs::write(at("fu-end"), "").unwrap();
    wait_for("the turn to end", || host.state(session) == "done");
    assert!(!shown("after-note"), "and after it ended");
    assert!(shown("a question of the user's"));
    assert!(shown("the answer for the user"));
}
