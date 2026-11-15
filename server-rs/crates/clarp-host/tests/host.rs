//! The Host as a process: the real binary, real shims, a real tool, and a kill in the middle.
//!
//! These tests need `clarp-shim` built beside `clarp-host`, which is where a build of the
//! workspace puts it.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::io::{BufRead, BufReader, Read, Write};
use std::net::TcpStream;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use serde_json::{Value, json};

static HOSTS: AtomicU64 = AtomicU64::new(0);

/// A Host's place on disk. It outlives the Host's process: that is the point.
struct Place {
    root: PathBuf,
}

impl Place {
    fn new(name: &str) -> Self {
        let n = HOSTS.fetch_add(1, Ordering::SeqCst);
        let root =
            std::env::temp_dir().join(format!("clarp-host-{name}-{}-{n}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(root.join("work")).unwrap();
        Self { root }
    }

    /// Starts a Host process in this place and waits until it listens.
    fn start(&self) -> Host {
        self.start_with_token("")
    }

    /// Starts a Host that asks for this token (none when it is empty).
    fn start_with_token(&self, token: &str) -> Host {
        let mut process = Command::new(env!("CARGO_BIN_EXE_clarp-host"))
            .args(["--data", self.root.join("data").to_str().unwrap()])
            .args(["--run", self.root.join("run").to_str().unwrap()])
            .args(["--cwd", self.root.join("work").to_str().unwrap()])
            .args(["--listen", "127.0.0.1:0"])
            .env("CLARP_HOST_TOKEN", token)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        let mut said = BufReader::new(process.stderr.take().unwrap());
        let mut first = String::new();
        said.read_line(&mut first).unwrap();
        let address = first
            .trim()
            .strip_prefix("clarp-host listens on ")
            .unwrap_or_else(|| panic!("the Host did not say where it listens, it said: {first}"))
            .to_string();
        // Whatever else it says is kept out of the way.
        std::thread::spawn(move || {
            let mut rest = String::new();
            let _ = said.read_to_string(&mut rest);
        });
        Host {
            process,
            address,
            token: token.to_string(),
        }
    }
}

impl Drop for Place {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.root);
    }
}

struct Host {
    process: Child,
    address: String,
    /// The token this test's own requests present, when the Host asks for one.
    token: String,
}

impl Host {
    /// One request, one answer: the status and the body as JSON.
    fn ask(&self, method: &str, path: &str, body: Option<&Value>) -> (u16, Value) {
        self.ask_as(&self.token, method, path, body)
    }

    /// A request that presents this token, or none when it is empty.
    fn ask_as(&self, token: &str, method: &str, path: &str, body: Option<&Value>) -> (u16, Value) {
        let mut stream = TcpStream::connect(&self.address).unwrap();
        stream
            .set_read_timeout(Some(Duration::from_secs(20)))
            .unwrap();
        let body = body.map(Value::to_string).unwrap_or_default();
        let authorization = if token.is_empty() {
            String::new()
        } else {
            format!("Authorization: Bearer {token}\r\n")
        };
        let request = format!(
            "{method} {path} HTTP/1.1\r\nHost: test\r\nConnection: close\r\n{authorization}Content-Type: application/json\r\nContent-Length: {}\r\n\r\n{body}",
            body.len()
        );
        stream.write_all(request.as_bytes()).unwrap();
        let mut answer = String::new();
        stream.read_to_string(&mut answer).unwrap();
        let (head, body) = answer.split_once("\r\n\r\n").unwrap_or((&answer, ""));
        let status = head
            .split_whitespace()
            .nth(1)
            .and_then(|code| code.parse().ok())
            .unwrap_or_else(|| panic!("no status in: {head}"));
        (status, serde_json::from_str(body).unwrap_or(Value::Null))
    }

    fn get(&self, path: &str) -> Value {
        let (status, body) = self.ask("GET", path, None);
        assert_eq!(status, 200, "GET {path}: {body}");
        body
    }

    fn post(&self, path: &str, body: Value) -> Value {
        let (status, answer) = self.ask("POST", path, Some(&body));
        assert_eq!(status, 200, "POST {path} {body}: {answer}");
        answer
    }

    /// Listens to the Host's events on a thread and collects them as they come.
    fn listen(&self) -> Arc<Mutex<Vec<Value>>> {
        let heard = Arc::new(Mutex::new(Vec::new()));
        let mut stream = TcpStream::connect(&self.address).unwrap();
        stream
            .write_all(b"GET /events HTTP/1.1\r\nHost: test\r\nAccept: text/event-stream\r\n\r\n")
            .unwrap();
        let collected = heard.clone();
        std::thread::spawn(move || {
            for line in BufReader::new(stream).lines() {
                let Ok(line) = line else { break };
                if let Some(event) = line
                    .strip_prefix("data: ")
                    .or_else(|| line.strip_prefix("data:"))
                    .and_then(|data| serde_json::from_str::<Value>(data).ok())
                {
                    collected.lock().unwrap().push(event);
                }
            }
        });
        heard
    }

    /// Everything in the conversation, as the Host answers it.
    fn turns(&self, session: &str) -> Vec<Value> {
        self.get(&format!("/log?session={session}&limit=200"))["turns"]
            .as_array()
            .unwrap()
            .clone()
    }

    fn new_agent(&self, name: &str) -> String {
        let made = self.post("/agents", json!({ "name": name, "backend": "scripted" }));
        made["session"].as_str().unwrap().to_string()
    }

    fn send(&self, session: &str, text: &str, id: &str) -> Value {
        self.post(
            "/send",
            json!({ "session": session, "text": text, "client_msg_id": id }),
        )
    }

    fn state(&self, session: &str) -> String {
        self.get("/agents/snapshot")["agents"]
            .as_array()
            .unwrap()
            .iter()
            .find(|agent| agent["session"] == session)
            .map(|agent| agent["latest_state"].as_str().unwrap().to_string())
            .unwrap_or_else(|| panic!("{session} is not in the snapshot"))
    }

    /// The conversation as (role, text) pairs.
    fn conversation(&self, session: &str) -> Vec<(String, String)> {
        self.get(&format!("/log?session={session}&limit=100"))["turns"]
            .as_array()
            .unwrap()
            .iter()
            .map(|turn| {
                (
                    turn["role"].as_str().unwrap().to_string(),
                    turn["text"].as_str().unwrap().to_string(),
                )
            })
            .collect()
    }

    /// Kills the Host's process without a word, as a crash or a `kill -9` would.
    fn kill(mut self) {
        self.process.kill().unwrap();
        self.process.wait().unwrap();
    }
}

impl Drop for Host {
    fn drop(&mut self) {
        let _ = self.process.kill();
        let _ = self.process.wait();
    }
}

fn wait_for(what: &str, mut ready: impl FnMut() -> bool) {
    let deadline = Instant::now() + Duration::from_secs(30);
    while !ready() {
        assert!(Instant::now() < deadline, "timed out waiting for {what}");
        std::thread::sleep(Duration::from_millis(50));
    }
}

/// Waits for something a real tool does, which can take a while. Says what it saw when it
/// gives up, and gives up at once when the turn has ended badly.
fn wait_long(host: &Host, session: &str, what: &str, mut ready: impl FnMut() -> bool) {
    let deadline = Instant::now() + Duration::from_secs(180);
    while !ready() {
        let state = host.state(session);
        assert!(
            Instant::now() < deadline && state != "interrupted",
            "waiting for {what}: the agent is {state} and its conversation is {:#?}",
            host.turns(session)
        );
        std::thread::sleep(Duration::from_millis(200));
    }
}

fn said(role: &str, text: &str) -> (String, String) {
    (role.to_string(), text.to_string())
}

#[test]
fn a_message_is_answered_and_the_apps_are_told() {
    let place = Place::new("answer");
    let host = place.start();
    let info = host.get("/server-info");
    assert_eq!(info["contract"]["host"], 99);
    assert!(info["server_id"].as_str().is_some_and(|id| id.len() == 36));

    let session = host.new_agent("Echo");
    assert_eq!(session, "echo");
    assert_eq!(host.state(&session), "idle");
    let heard = host.listen();

    let sent = host.send(&session, "hello there", "m1");
    assert_eq!(
        (&sent["ok"], &sent["queued"], &sent["dispatch"]),
        (&json!(true), &json!(false), &json!("scripted"))
    );
    wait_for("the reply", || {
        host.conversation(&session)
            == vec![
                said("user", "hello there"),
                said("assistant", "hello there"),
            ]
    });
    wait_for("the agent to be done", || host.state(&session) == "done");

    // The message is filed under the id the app gave it, and sending it again changes nothing.
    let log = host.get(&format!("/log?session={session}"));
    assert_eq!(log["turns"][0]["id"], "u-m1");
    host.send(&session, "hello there", "m1");
    std::thread::sleep(Duration::from_millis(300));
    assert_eq!(host.conversation(&session).len(), 2);

    // A client that has it all is given nothing new; one that is behind is given the rest.
    let head = log["latest_revision"].as_u64().unwrap();
    let nothing = host.get(&format!("/log?session={session}&after_revision={head}"));
    assert_eq!(nothing["turns"].as_array().unwrap().len(), 0);
    let first = log["turns"][0]["revision"].as_u64().unwrap();
    let rest = host.get(&format!("/log?session={session}&after_revision={first}"));
    assert_eq!(rest["turns"].as_array().unwrap().len(), 1);
    assert_eq!(rest["turns"][0]["role"], "assistant");

    // The apps were told as it happened.
    wait_for("the events", || {
        let heard = heard.lock().unwrap();
        let kinds = |kind: &str| heard.iter().filter(|event| event["type"] == kind).count();
        kinds("transcript-updated") >= 1
            && heard
                .iter()
                .any(|event| event["type"] == "agent-state" && event["kind"] == "thinking")
            && heard
                .iter()
                .any(|event| event["type"] == "agent-state" && event["kind"] == "done")
    });
    let heard = heard.lock().unwrap();
    assert_eq!(heard[0]["type"], "server-version");
    assert!(heard.iter().any(|event| event["type"] == "agent-roster"));
    assert!(
        heard
            .iter()
            .filter(|event| event["type"] == "agent-state")
            .all(|event| event["session"] == "echo" && event["event_id"].is_u64())
    );

    // What the Host does not know, it says so.
    assert_eq!(host.ask("GET", "/log?session=nobody", None).0, 404);
    assert_eq!(
        host.ask(
            "POST",
            "/agents",
            Some(&json!({"name": "X", "backend": "abacus"}))
        )
        .0,
        400
    );
}

#[test]
fn a_stop_ends_the_turn_and_holds_what_waits_until_a_resume() {
    let place = Place::new("stop");
    let host = place.start();
    let session = host.new_agent("Stopper");
    host.send(&session, "sleep 60", "m1");
    wait_for("the turn to run", || host.state(&session) == "thinking");
    let queued = host.post(
        "/send",
        json!({ "session": session, "text": "later", "client_msg_id": "m2", "queue_if_busy": true }),
    );
    assert_eq!(queued["queued"], true);

    let began = Instant::now();
    let stopped = host.post("/stop", json!({ "session": session }));
    assert_eq!(
        (&stopped["terminated"], &stopped["queue_paused"]),
        (&json!(1), &json!(true))
    );
    wait_for("the turn to end", || host.state(&session) == "interrupted");
    assert!(
        began.elapsed() < Duration::from_secs(20),
        "the stop did not wait for the sleep"
    );
    let held = host.get(&format!("/turn-queue?session={session}"));
    assert_eq!(
        (
            held["queued"].as_array().unwrap().len(),
            &held["queue_paused"]
        ),
        (1, &json!(true))
    );

    let resumed = host.post("/turn-queue/resume", json!({ "session": session }));
    assert_eq!(resumed["resumed"], true);
    wait_for("the held message to be answered", || {
        host.conversation(&session)
            .contains(&said("assistant", "later"))
    });
}

#[test]
fn a_host_that_is_killed_in_the_middle_of_turns_comes_back_and_every_turn_finishes() {
    let place = Place::new("kill");
    let host = place.start();
    let agents: Vec<String> = (0..10)
        .map(|n| host.new_agent(&format!("Worker {n}")))
        .collect();
    for (n, session) in agents.iter().enumerate() {
        // Half of them are still running when the Host is back; half end while it is away.
        let seconds = if n % 2 == 0 { 4 } else { 1 };
        host.send(session, &format!("sleep {seconds}"), &format!("m-{n}"));
    }
    wait_for("all ten turns to run", || {
        agents
            .iter()
            .all(|session| host.state(session) == "thinking")
    });

    host.kill();
    std::thread::sleep(Duration::from_millis(2_000));
    let host = place.start();

    for (n, session) in agents.iter().enumerate() {
        let seconds = if n % 2 == 0 { 4 } else { 1 };
        let message = format!("sleep {seconds}");
        wait_for("a turn that ran through the restart to be answered", || {
            host.conversation(session) == vec![said("user", &message), said("assistant", &message)]
        });
        wait_for("its agent to be done", || host.state(session) == "done");
    }
    // Every agent takes its next message as if nothing had happened.
    host.send(&agents[0], "and now?", "after");
    wait_for("the next answer", || {
        host.conversation(&agents[0])
            .contains(&said("assistant", "and now?"))
    });
}

/// A real Claude Code turn through the Host, with the Host killed while Claude's tool runs.
///
/// It spends a few cents of a real account and needs a Claude Code that is logged in, so it runs
/// only when asked: `CLARP_TEST_CLAUDE=1 cargo test --test host -- --ignored`. Set
/// `CLARP_CLAUDE_PROGRAM` when Claude Code is not `claude` on the path.
#[test]
#[ignore = "runs a real Claude Code turn: needs a logged-in account and CLARP_TEST_CLAUDE=1"]
fn a_real_claude_turn_survives_a_kill_of_the_host_and_the_next_turn_remembers_it() {
    if std::env::var("CLARP_TEST_CLAUDE").as_deref() != Ok("1") {
        eprintln!("skipped: CLARP_TEST_CLAUDE is not 1");
        return;
    }
    let place = Place::new("claude");
    let host = place.start();
    let made = host.post(
        "/agents",
        json!({ "name": "Real", "backend": "claude", "model": "claude-haiku-5-5" }),
    );
    let session = made["session"].as_str().unwrap().to_string();
    host.send(
        &session,
        "Use the Bash tool once to run exactly: sleep 12; echo spool-survived. Then reply with one short sentence that says what it printed.",
        "real-1",
    );
    wait_long(&host, &session, "Claude to start its tool", || {
        host.state(&session) == "tool"
    });

    // The Host dies while Claude's command sleeps, and is away for a moment.
    host.kill();
    std::thread::sleep(Duration::from_secs(2));
    let host = place.start();

    wait_long(&host, &session, "the turn to finish", || {
        host.state(&session) == "done"
    });
    let turns = host.turns(&session);
    let tool = turns
        .iter()
        .find(|turn| turn["kind"] == "tool")
        .expect("the tool call is in the conversation");
    assert_eq!(tool["tools"][0]["status"], "ok", "{tool}");
    assert!(
        tool["tools"][0]["output"]
            .as_str()
            .unwrap()
            .contains("spool-survived")
    );
    let answers: Vec<&str> = turns
        .iter()
        .filter(|turn| turn["role"] == "assistant" && turn["kind"].is_null())
        .map(|turn| turn["text"].as_str().unwrap())
        .collect();
    assert!(
        answers.iter().any(|text| text.contains("spool-survived")),
        "Claude's answer came through the restart: {answers:?}"
    );
    assert_eq!(
        turns.iter().filter(|turn| turn["role"] == "user").count(),
        1,
        "the message is there once"
    );

    // The next turn continues Claude's own session: it knows what the last one did.
    host.send(
        &session,
        "In one word or one hyphenated word: what did that command print?",
        "real-2",
    );
    wait_long(&host, &session, "the second answer", || {
        host.state(&session) == "done"
            && host
                .turns(&session)
                .iter()
                .filter(|turn| turn["role"] == "user")
                .count()
                == 2
            && host.turns(&session).last().is_some_and(|turn| {
                turn["role"] == "assistant"
                    && turn["text"]
                        .as_str()
                        .is_some_and(|text| text.contains("spool-survived"))
            })
    });
}

#[test]
fn a_device_is_paired_with_a_code_and_speaks_with_a_token_that_can_be_taken_away() {
    let place = Place::new("pair");
    let host = place.start_with_token("the-administrators-token");
    // Without a token nothing answers; with the administrator's, from this machine, all does.
    assert_eq!(host.ask_as("", "GET", "/agents/snapshot", None).0, 401);
    assert_eq!(host.ask_as("wrong", "GET", "/agents/snapshot", None).0, 401);
    assert_eq!(host.ask("GET", "/agents/snapshot", None).0, 200);
    host.new_agent("Echo");

    // The administrator has a code made; the answer carries the link a phone opens.
    let issued = host.post(
        "/pairing/issue",
        json!({ "device_name": "Test phone", "scope": "limited", "url": "https://host.example:7690" }),
    );
    let code = issued["code"].as_str().unwrap().to_string();
    assert!(code.starts_with("clp_"));
    assert!(
        issued["uri"]
            .as_str()
            .unwrap()
            .starts_with("clarp://pair?name=")
            && issued["uri"]
                .as_str()
                .unwrap()
                .ends_with(&format!("&code={code}"))
    );

    // The command line asks the same Host for the same thing.
    let shown = Command::new(env!("CARGO_BIN_EXE_clarp-host"))
        .args([
            "pair",
            "--url",
            "https://host.example:7690",
            "--host",
            &host.address,
        ])
        .env("CLARP_HOST_TOKEN", "the-administrators-token")
        .output()
        .unwrap();
    assert!(
        shown.status.success(),
        "{}",
        String::from_utf8_lossy(&shown.stderr)
    );
    assert!(String::from_utf8_lossy(&shown.stdout).starts_with("clarp://pair?name="));
    let refused = Command::new(env!("CARGO_BIN_EXE_clarp-host"))
        .args([
            "pair",
            "--url",
            "https://host.example:7690",
            "--host",
            &host.address,
        ])
        .env("CLARP_HOST_TOKEN", "not-the-token")
        .output()
        .unwrap();
    assert_eq!(refused.status.code(), Some(2));

    // The device hands the code in, with no token, and is given one of its own.
    let (status, paired) = host.ask_as(
        "",
        "POST",
        "/pairing/exchange",
        Some(&json!({ "code": code, "device_name": "" })),
    );
    assert_eq!(status, 201, "{paired}");
    let token = paired["device"]["token"].as_str().unwrap().to_string();
    let device_id = paired["device"]["device_id"].as_str().unwrap().to_string();
    assert!(token.starts_with("cld_"));
    assert_eq!(paired["device"]["device_name"], "Test phone");
    assert_eq!(paired["device"]["scope"], "limited");
    assert_eq!(
        paired["server"]["server_id"],
        host.get("/server-info")["server_id"]
    );
    // A code works once.
    let again = host.ask_as(
        "",
        "POST",
        "/pairing/exchange",
        Some(&json!({ "code": code })),
    );
    assert_eq!(again.0, 409);
    assert_eq!(
        host.ask_as(
            "",
            "POST",
            "/pairing/exchange",
            Some(&json!({ "code": "clp_nonsense" }))
        )
        .0,
        409
    );

    // With its token the device reads and sends, and its limited scope keeps it from the rest.
    assert_eq!(host.ask_as(&token, "GET", "/agents/snapshot", None).0, 200);
    let sent = host.ask_as(
        &token,
        "POST",
        "/send",
        Some(&json!({ "session": "echo", "text": "from the phone", "client_msg_id": "p1" })),
    );
    assert_eq!(sent.0, 200, "{}", sent.1);
    assert_eq!(
        host.ask_as(&token, "POST", "/agents", Some(&json!({ "name": "X" })))
            .0,
        403
    );
    assert_eq!(host.ask_as(&token, "GET", "/paired-devices", None).0, 403);
    assert_eq!(
        host.ask_as(&token, "POST", "/stop", Some(&json!({ "session": "echo" })))
            .0,
        403
    );

    // The administrator sees the device, without anything that could speak as it.
    let devices = host.get("/paired-devices");
    assert_eq!(devices["devices"].as_array().unwrap().len(), 1);
    assert_eq!(devices["devices"][0]["device_id"], device_id);
    assert!(devices["devices"][0].get("token_hash").is_none());
    assert!(!devices.to_string().contains(&token));

    // A restart forgets nothing: the device is still paired.
    host.kill();
    let host = place.start_with_token("the-administrators-token");
    assert_eq!(host.ask_as(&token, "GET", "/agents/snapshot", None).0, 200);

    // Revoked, the token opens nothing, at once and after another restart.
    let revoked = host.post("/paired-devices/revoke", json!({ "device_id": device_id }));
    assert_eq!(revoked["ok"], true);
    assert_eq!(host.ask_as(&token, "GET", "/agents/snapshot", None).0, 401);
    assert_eq!(
        host.ask(
            "POST",
            "/paired-devices/revoke",
            Some(&json!({ "device_id": device_id }))
        )
        .0,
        404
    );
    host.kill();
    let host = place.start_with_token("the-administrators-token");
    assert_eq!(host.ask_as(&token, "GET", "/agents/snapshot", None).0, 401);
    assert_eq!(
        host.get("/paired-devices")["devices"]
            .as_array()
            .unwrap()
            .len(),
        0
    );
}
