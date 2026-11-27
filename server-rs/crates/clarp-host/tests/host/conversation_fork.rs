//! The conversation fork through the edge (issue 718, clarp-ios#139): what the apps read of it.
//! A stand-in of Claude Code holds the agent's own turn (for another agent) and the fork's
//! turn open, so that `/live` and `/stop` are asked while both run.

use super::*;

/// A stand-in of Claude Code. The session it says is the one it was named or resumed. A turn
/// of the fork (its prompt carries the fork's brief) answers at once unless the user's words
/// say `QUIET`; every turn notes its launch in `launches` (whose it is, and the user's words)
/// and holds until `finish-<whose>` or `finish` is there, or until it is stopped, when it
/// leaves `<whose>-gone`.
const CLAUDE: &str = r#"#!/usr/bin/env python3
import json
import pathlib
import signal
import sys
import time

if "--version" in sys.argv:
    print("2.1.320 (Claude Code)")
    raise SystemExit(0)

def after(flag):
    return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else None

sid = after("--session-id") or after("--resume") or "stand-in"
root = pathlib.Path(__file__).resolve().parent
incoming = json.loads(sys.stdin.readline())
prompt = incoming["message"]["content"][0]["text"]
whose = "fork" if "You are a short-lived fork of your own session." in prompt else "main"
said = next((line for line in prompt.splitlines() if line.startswith("U:")), "")
with (root / "launches").open("a") as lines:
    lines.write(json.dumps({"whose": whose, "said": said, "session": sid}) + "\n")

def stopped(*_):
    (root / f"{whose}-gone").write_text("stopped")
    raise SystemExit(0)

signal.signal(signal.SIGTERM, stopped)
# Claude Code is asked to stop with an interrupt first.
signal.signal(signal.SIGINT, stopped)

def send(kind, **fields):
    print(json.dumps({"type": kind, "session_id": sid, **fields}), flush=True)

send("system", subtype="init")
if whose == "main" and 'The fork replied: "Here, from the fork."' in prompt:
    (root / "fork-note-read").write_text(sid)
if whose == "fork" and "QUIET" not in prompt:
    # Streamed, as Claude Code streams: the reply is whole at its message_stop.
    def event(body):
        send("stream_event", event=body)
    event({"type": "message_start", "message": {"id": "msg_fork"}})
    event({"type": "content_block_start", "index": 0,
           "content_block": {"type": "text", "text": "Here, from the fork."}})
    event({"type": "message_stop"})
(root / f"{whose}-running").write_text(sid)
for _ in range(3000):
    if (root / "finish").exists() or (root / f"finish-{whose}").exists():
        break
    time.sleep(0.02)
send("result", subtype="success", is_error=False, result="done")
(root / f"{whose}-gone").write_text("finished")
"#;

const OWNER: &str = "fixture-owner-token";

const CODEX: &str = r#"#!/usr/bin/env python3
import json
import os
import pathlib
import signal
import sys
import time

if '--version' in sys.argv:
    print('codex-cli 0.167.1')
    raise SystemExit(0)

root = pathlib.Path(__file__).resolve().parent
prompt = sys.stdin.read()
side = 'short-lived conversation session' in prompt
thread = 'fresh-side' if side else 'work-thread'

def say(kind, **fields):
    print(json.dumps({'type':kind, **fields}), flush=True)

def reply(text):
    say('item.completed', item={'type':'agent_message', 'id':'words', 'text':text})

def stopped(*_):
    (root / ('side-stopped' if side else 'main-stopped')).touch()
    raise SystemExit(0)

signal.signal(signal.SIGINT, stopped)
signal.signal(signal.SIGTERM, stopped)
say('thread.started',thread_id=thread)
say('turn.started')
if side:
    assert '--ephemeral' in sys.argv and 'read-only' in sys.argv
    assert 'resume' not in sys.argv and '--dangerously-bypass-approvals-and-sandbox' not in sys.argv
    assert not os.environ.get('CLARP_TOOL_TOKEN') and not os.environ.get('CLARP_PROVIDER_TURN')
    assert 'Owner work marker' in prompt and 'Earlier raw work' in prompt
    (root / 'side-prompt').write_text(prompt)
    reply('Reply from the fresh conversation.')
    (root / 'side-running').touch()
    while not ((root / 'finish-side').exists() or (root / 'finish').exists()):
        time.sleep(0.02)
elif 'Continue with the next work request.' in prompt:
    if 'The fork replied: "Reply from the fresh conversation."' in prompt:
        (root / 'note-read').touch()
    reply('Next work turn.')
else:
    reply('Earlier raw work. <speak>Deliberate work announcement.</speak>')
    (root / 'main-running').touch()
    while not ((root / 'finish-main').exists() or (root / 'finish').exists()):
        time.sleep(0.02)
    (root / 'main-finished').touch()
say('turn.completed', usage={'input_tokens':6,'output_tokens':4})
"#;

fn row(host: &Host, session: &str) -> Value {
    host.get("/agents/snapshot")["agents"]
        .as_array()
        .unwrap()
        .iter()
        .find(|row| row["session"] == session)
        .unwrap()
        .clone()
}

fn live(host: &Host, session: &str) -> Value {
    host.get(&format!("/live?session={session}"))
}

fn log_row(host: &Host, session: &str, id: &str) -> Option<Value> {
    host.get(&format!("/log?session={session}&limit=100"))["turns"]
        .as_array()
        .unwrap()
        .iter()
        .find(|row| row["id"] == id)
        .cloned()
}

/// Follows the Host's events as an app with the chat open does, presenting the owner's token
/// (`Host::listen_at` presents none).
fn follow(host: &Host, path: &str) -> Arc<Mutex<Vec<Value>>> {
    use std::io::{BufRead, Write};
    let heard = Arc::new(Mutex::new(Vec::new()));
    let mut stream = std::net::TcpStream::connect(&host.address).unwrap();
    let asked = format!(
        "GET {path} HTTP/1.1\r\nHost: test\r\nAuthorization: Bearer {}\r\n\
         Accept: text/event-stream\r\n\r\n",
        host.token
    );
    stream.write_all(asked.as_bytes()).unwrap();
    let collected = heard.clone();
    std::thread::spawn(move || {
        for line in std::io::BufReader::new(stream).lines() {
            let Ok(line) = line else { break };
            if let Some(event) = line
                .strip_prefix("data:")
                .and_then(|data| serde_json::from_str::<Value>(data.trim_start()).ok())
            {
                collected.lock().unwrap().push(event);
            }
        }
    });
    heard
}

/// The operations of the agent's `live` events heard so far, in order.
fn live_ops(heard: &Arc<Mutex<Vec<Value>>>, session: &str) -> Vec<Value> {
    heard
        .lock()
        .unwrap()
        .iter()
        .filter(|event| event["type"] == "live" && event["session"] == session)
        .flat_map(|event| event["ops"].as_array().cloned().unwrap_or_default())
        .collect()
}

/// A Host with the stand-in, whose owner lets `oak` answer through a fork, and `oak` in
/// Conversation view; the stand-ins end however the test ends.
struct Forking {
    place: Place,
    host: Host,
    _finish: Finish,
}

/// Lets the stand-ins end however the test ends, so that none outlives it.
struct Finish(std::path::PathBuf);

impl Drop for Finish {
    fn drop(&mut self) {
        let _ = std::fs::write(&self.0, "");
    }
}

impl Forking {
    fn new(name: &str) -> Self {
        Self::new_on(name, "claude", "CLARP_CLAUDE_PROGRAM", CLAUDE)
    }

    fn new_on(name: &str, backend: &str, program_env: &str, fixture: &str) -> Self {
        let place = Place::new(name);
        let tool = place.root.join(backend);
        program(&tool, fixture);
        let xdg = place.root.join("xdg");
        std::fs::create_dir_all(xdg.join("clarp-host")).unwrap();
        std::fs::write(
            xdg.join("clarp-host/config.toml"),
            "[conversation_forks]\nagents = [\"oak\"]\n",
        )
        .unwrap();
        let world = [
            (program_env, tool.to_str().unwrap()),
            ("XDG_CONFIG_HOME", xdg.to_str().unwrap()),
        ];
        let finish = Finish(place.root.join("finish"));
        let host = place.start_in(OWNER, &[], &world);
        host.post(
            "/agents",
            json!({"name": "Oak", "session": "oak", "backend": backend}),
        );
        host.post(
            "/agent-chat-view",
            json!({"session": "oak", "chat_view": "conversation"}),
        );
        Self {
            place,
            host,
            _finish: finish,
        }
    }

    fn at(&self, name: &str) -> std::path::PathBuf {
        self.place.root.join(name)
    }

    /// The agent works for another agent, its own session known: what `/live` shows of it.
    fn busy_for_an_agent(&self) -> Value {
        self.host.post(
            "/send",
            json!({
                "session": "oak", "text": "U: work on the build.", "client_msg_id": "work-1",
                "sender": "remote-helper", "origin": "user",
            }),
        );
        let running = self.at("main-running");
        wait_for("the agent's own turn", || running.exists());
        wait_for("the agent's own session to be known", || {
            !row(&self.host, "oak")["backend_session_id"]
                .as_str()
                .unwrap_or("")
                .is_empty()
        });
        let shown = live(&self.host, "oak");
        assert_eq!(shown["turn"]["audience"], "agents", "{shown:#}");
        shown
    }

    /// The launches the stand-in saw: whose each was, and the user's words it was given.
    fn launches(&self) -> Vec<(String, String)> {
        std::fs::read_to_string(self.at("launches"))
            .unwrap_or_default()
            .lines()
            .map(|line| {
                let told: Value = serde_json::from_str(line).unwrap();
                (
                    told["whose"].as_str().unwrap().to_string(),
                    told["said"].as_str().unwrap().to_string(),
                )
            })
            .collect()
    }

    /// Waits out the Stop's grace: a stopped turn's shim keeps its tool's group to its end
    /// (D26, the Host's five seconds) before it leaves, so nothing outlives the test.
    fn grace(&self) {
        std::thread::sleep(Duration::from_millis(5_500));
    }
}

#[test]
fn codex_conversation_reads_committed_context_and_leaves_work_and_projection_intact() {
    let forking = Forking::new_on("codex-conversation", "codex", "CLARP_CODEX_PROGRAM", CODEX);
    let host = &forking.host;
    host.send("oak", "Owner work marker", "work");
    waited(host, "the separate work turn", || {
        forking.at("main-running").exists()
    });
    let main = live(host, "oak");
    assert_ne!(main["conversation_turn"], true);
    let main_id = main["turn"]["turn_id"].clone();
    assert!(main_id.is_string());
    host.post("/send", json!({
        "session":"oak", "text":"Hello, how are you?", "client_msg_id":"greeting", "while_busy":"take_over",
    }));
    waited(host, "the fresh Codex conversation reply", || {
        forking.at("side-running").exists()
            && host
                .turns("oak")
                .iter()
                .any(|row| row["text"] == "Reply from the fresh conversation.")
    });
    let shown = live(host, "oak");
    assert_eq!(shown["conversation_turn"], true, "{shown}");
    assert_eq!(shown["main_turn"]["turn_id"], main_id, "{shown}");
    assert!(!forking.at("main-stopped").exists());
    let rows = host.turns("oak");
    let work = rows
        .iter()
        .find(|row| {
            row["text"]
                .as_str()
                .is_some_and(|v| v.starts_with("Earlier raw work."))
        })
        .unwrap();
    assert_eq!(
        work["conversation_text"], "Deliberate work announcement.",
        "{work}"
    );
    let answer = rows
        .iter()
        .find(|row| row["text"] == "Reply from the fresh conversation.")
        .unwrap();
    assert_eq!(
        answer["conversation_text"],
        "Reply from the fresh conversation."
    );
    std::fs::write(forking.at("finish-side"), "").unwrap();
    waited(host, "the work live again", || {
        live(host, "oak")["turn"]["turn_id"] == main_id
    });
    std::fs::write(forking.at("finish-main"), "").unwrap();
    waited(host, "work finishes without stop", || {
        forking.at("main-finished").exists() && row(host, "oak")["busy"] == false
    });
    host.send("oak", "Continue with the next work request.", "continue");
    waited(
        host,
        "the next work prompt includes the conversation note",
        || forking.at("note-read").exists(),
    );
}

/// Waits for `ready`; when it does not come, says what the agent's chat, its live view, its
/// row and its queue were.
fn waited(host: &Host, what: &str, mut ready: impl FnMut() -> bool) {
    let deadline = std::time::Instant::now() + Duration::from_secs(30);
    while !ready() {
        assert!(
            std::time::Instant::now() < deadline,
            "timed out waiting for {what}:\nlog {:#}\nlive {:#}\nrow {:#}\nqueue {:#}",
            host.get("/log?session=oak&limit=100")["turns"],
            live(host, "oak"),
            row(host, "oak"),
            host.get("/turn-queue?session=oak"),
        );
        std::thread::sleep(Duration::from_millis(50));
    }
}

#[test]
fn the_users_message_during_an_agents_turn_is_answered_by_a_fork_that_a_stop_ends_alone() {
    let forking = Forking::new("conversation-fork");
    let host = &forking.host;
    host.post(
        "/agents",
        json!({"name": "Elm", "session": "elm", "backend": "claude"}),
    );

    // The gates the apps read: the feature, and the flag on the agent the setting names.
    assert_eq!(
        host.get("/server-info")["contract"]["features"]["conversation_fork"],
        153
    );
    assert_eq!(row(host, "oak")["conversation_fork"], true);
    assert_eq!(row(host, "elm")["conversation_fork"], false);

    let main_id = forking.busy_for_an_agent()["turn"]["turn_id"].clone();
    let main_session = std::fs::read_to_string(forking.at("main-running")).unwrap();

    // An app with the chat open follows the agent's live events.
    let heard = follow(host, "/events?live=oak");
    wait_for("the live subscription to open", || {
        !heard.lock().unwrap().is_empty()
    });
    let main_turns = || -> Vec<Value> {
        live_ops(&heard, "oak")
            .into_iter()
            .filter(|op| op["op"] == "main_turn")
            .map(|op| op["main_turn"].clone())
            .collect()
    };

    // The user writes: the fork answers at once, in the same chat.
    host.send("oak", "U: are you there?", "u-1");
    let fork_running = forking.at("fork-running");
    waited(host, "the fork's turn", || fork_running.exists());
    let fork_session = std::fs::read_to_string(&fork_running).unwrap();
    assert_ne!(fork_session, main_session);
    let reply = || {
        host.get("/log?session=oak&limit=100")["turns"]
            .as_array()
            .unwrap()
            .iter()
            .find(|row| row["text"] == "Here, from the fork.")
            .cloned()
    };
    waited(host, "the fork's reply in the chat", || reply().is_some());
    let reply = reply().unwrap();
    assert_eq!(reply["session_id"], fork_session.as_str());
    assert_eq!(reply["conversation_id"], main_session.as_str());
    assert_eq!(reply["audience"], "user");

    // `/live` shows the fork's turn, with the agent's own beside it.
    let mut shown = Value::Null;
    wait_for("the fork's turn to be shown", || {
        shown = live(host, "oak");
        shown["turn"]["audience"] == "user"
    });
    let main = &shown["main_turn"];
    assert_eq!(main["turn_id"], main_id, "{shown:#}");
    assert_eq!(main["audience"], "agents");
    assert_eq!(main["status"], "running");
    assert!(main["activity"]["state"].is_string());
    assert!(main.get("headline").is_none());

    // The events told the same: the agent's own turn beside the fork's, as the app applies it.
    wait_for("main_turn on the live events", || {
        main_turns().iter().any(|told| told["turn_id"] == main_id)
    });
    let told = main_turns().last().cloned().unwrap();
    assert_eq!(told["audience"], "agents", "{told:#}");
    assert_eq!(told["status"], "running");
    assert!(told["activity"]["state"].is_string());
    assert!(told.get("headline").is_none());

    // A Stop without a target ends the fork's turn, and nothing of the agent's own.
    let stopped = host.post("/stop", json!({"session": "oak"}));
    assert_eq!(stopped["terminated"], 1, "{stopped}");
    assert_eq!(stopped["queue_paused"], false, "{stopped}");
    wait_for("the fork's turn to end", || {
        live(host, "oak").get("main_turn").is_none()
    });
    let shown = live(host, "oak");
    assert_eq!(shown["turn"]["turn_id"], main_id, "{shown:#}");
    wait_for("main_turn null on the live events", || {
        main_turns().last().is_some_and(Value::is_null)
    });
    assert_eq!(
        row(host, "oak")["busy"],
        true,
        "the agent's own turn runs on"
    );

    // A Stop that names the agent's own turn ends it.
    let stopped = host.post("/stop", json!({"session": "oak", "target": "main"}));
    assert_eq!(stopped["terminated"], 1, "{stopped}");
    assert_eq!(stopped["queue_paused"], true, "{stopped}");
    wait_for("the agent to be free", || row(host, "oak")["busy"] == false);
    // Both tools were stopped, not let go.
    for (tool, gone) in [
        ("the fork's", "fork-gone"),
        ("the agent's own", "main-gone"),
    ] {
        let gone = forking.at(gone);
        wait_for(&format!("{tool} tool to end"), || gone.exists());
        assert_eq!(std::fs::read_to_string(&gone).unwrap(), "stopped");
    }
    forking.grace();
}

#[test]
fn conversation_words_answer_beside_the_owners_work_and_return_as_a_note() {
    for busy in ["steer", "take_over"] {
        let forking = Forking::new(busy);
        let host = &forking.host;
        host.send("oak", "U: work on my build.", "owner-work");
        waited(host, "the owner's running work", || {
            forking.at("main-running").exists()
                && row(host, "oak")["backend_session_id"]
                    .as_str()
                    .is_some_and(|key| !key.is_empty())
        });
        let shown = live(host, "oak");
        assert_eq!(shown["turn"]["audience"], "user");
        let main_id = shown["turn"]["turn_id"].clone();

        let sent = std::time::Instant::now();
        host.post(
            "/send",
            json!({
                "session": "oak", "text": "U: hello, how are you",
                "client_msg_id": "owner-greeting", "while_busy": busy,
            }),
        );
        waited(host, "the fork's reply before the work ends", || {
            let rows = host.get("/log?session=oak&limit=100");
            rows["turns"]
                .as_array()
                .unwrap()
                .iter()
                .any(|row| row["text"] == "Here, from the fork.")
        });
        eprintln!(
            "owner-fork {busy} durable reply {} ms (fixture)",
            sent.elapsed().as_millis()
        );
        let alongside = live(host, "oak");
        assert_eq!(alongside["main_turn"]["turn_id"], main_id, "{alongside:#}");
        assert_eq!(alongside["main_turn"]["status"], "running");
        assert!(!forking.at("main-gone").exists());

        std::fs::write(forking.at("finish-fork"), "").unwrap();
        waited(host, "the fork to yield the chat", || {
            live(host, "oak")["turn"]["turn_id"] == main_id
        });
        std::fs::write(forking.at("finish-main"), "").unwrap();
        waited(host, "the same work to finish", || {
            row(host, "oak")["busy"] == false
        });
        assert_eq!(
            std::fs::read_to_string(forking.at("main-gone")).unwrap(),
            "finished"
        );

        host.send("oak", "U: continue with my next task.", "owner-next");
        waited(host, "the main session to read the fork's note", || {
            forking.at("fork-note-read").exists()
        });
        assert_eq!(
            forking.launches(),
            [
                ("main".into(), "U: work on my build.".into()),
                ("fork".into(), "U: hello, how are you".into()),
                ("main".into(), "U: continue with my next task.".into())
            ]
        );
    }
}

#[test]
fn a_users_turn_held_behind_the_forks_turn_ends_at_once_on_a_stop_of_the_agents_own() {
    let forking = Forking::new("fork-held-turn");
    let host = &forking.host;
    forking.busy_for_an_agent();
    host.send("oak", "U: are you there?", "u-1");
    waited(host, "the fork's reply in the chat", || {
        log_row(host, "oak", "u-1").is_some()
    });

    // The agent's own turn ends while the fork still answers; the user writes again. That
    // turn waits for the fork's: no second session answers the user beside it.
    std::fs::write(forking.at("finish-main"), "").unwrap();
    waited(host, "the agent's own turn to end", || {
        forking.at("main-gone").exists()
    });
    host.send("oak", "U: and the plan?", "u-2");
    waited(host, "the user's turn to wait for the fork's", || {
        row(host, "oak")["busy"] == true
    });
    let launched_for = |said: &str| {
        forking
            .launches()
            .iter()
            .filter(|(whose, told)| whose == "main" && told == said)
            .count()
    };
    assert_eq!(
        launched_for("U: and the plan?"),
        0,
        "{:?}",
        forking.launches()
    );

    // A Stop of the agent's own turn ends the waiting turn at once: it never ran, and the
    // fork answers on.
    let stopped = host.post("/stop", json!({"session": "oak", "target": "main"}));
    assert_eq!(stopped["queue_paused"], true, "{stopped}");
    waited(host, "the waiting turn to end", || {
        row(host, "oak")["busy"] == false
    });
    assert!(!forking.at("fork-gone").exists(), "the fork answers on");
    assert_eq!(live(host, "oak")["turn"]["audience"], "user");

    // The fork's turn ends: the stopped turn does not run after it.
    std::fs::write(forking.at("finish-fork"), "").unwrap();
    waited(host, "the fork's turn to end", || {
        forking.at("fork-gone").exists() && live(host, "oak").get("main_turn").is_none()
    });
    std::thread::sleep(Duration::from_millis(500));
    assert_eq!(
        launched_for("U: and the plan?"),
        0,
        "{:?}",
        forking.launches()
    );
    assert_eq!(row(host, "oak")["busy"], false);
}

#[test]
fn a_stopped_fork_that_had_not_answered_settles_its_message_as_stopped_in_what_apps_read() {
    let forking = Forking::new("fork-stopped-unread");
    let host = &forking.host;
    let main_id = forking.busy_for_an_agent()["turn"]["turn_id"].clone();
    let heard = follow(host, "/events?live=oak");
    wait_for("the live subscription to open", || {
        !heard.lock().unwrap().is_empty()
    });

    // The fork takes the message and says nothing yet; the user stops it.
    host.send("oak", "U: QUIET, never mind", "u-1");
    waited(host, "the fork's turn", || {
        forking.at("fork-running").exists()
    });
    let mut fork_id = Value::Null;
    waited(host, "the fork's turn to be shown", || {
        let shown = live(host, "oak");
        fork_id = shown["turn"]["turn_id"].clone();
        shown["turn"]["audience"] == "user"
    });
    let stopped = host.post("/stop", json!({"session": "oak"}));
    assert_eq!(stopped["terminated"], 1, "{stopped}");
    assert_eq!(stopped["queue_paused"], false, "{stopped}");

    // The chat has the message, settled as stopped; it no longer waits.
    waited(host, "the stopped message in the chat", || {
        log_row(host, "oak", "u-1").is_some_and(|row| row["turn"]["status"] == "stopped")
    });
    let queue = host.get("/turn-queue?session=oak");
    assert!(queue["items"].as_array().unwrap().is_empty(), "{queue:#}");

    // `/live` ended the fork's turn as interrupted and shows the agent's own turn again.
    waited(host, "the agent's own turn to be shown again", || {
        live(host, "oak")["turn"]["turn_id"] == main_id
    });
    assert!(live(host, "oak").get("main_turn").is_none());
    let ops = live_ops(&heard, "oak");
    assert!(
        ops.iter().any(|op| op["op"] == "turn"
            && op["turn"]["turn_id"] == fork_id
            && op["turn"]["status"] == "interrupted"),
        "{ops:#?}"
    );

    // The agent's own turn ends: the stopped message does not run then either.
    std::fs::write(forking.at("finish-main"), "").unwrap();
    waited(host, "the agent to be free", || {
        row(host, "oak")["busy"] == false
    });
    std::thread::sleep(Duration::from_millis(500));
    let runs = forking
        .launches()
        .iter()
        .filter(|(_, said)| said == "U: QUIET, never mind")
        .count();
    assert_eq!(runs, 1, "only the fork's: {:?}", forking.launches());
    assert!(log_row(host, "oak", "u-1").is_some());
    forking.grace();
}
