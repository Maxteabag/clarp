//! The Host as a process: the real binary, real shims, a real tool, and a kill in the middle.
//!
//! These tests need `clarp-shim` built beside `clarp-host`, which is where a build of the
//! workspace puts it.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::io::{BufRead, BufReader, Read, Write};
use std::net::{TcpListener, TcpStream};
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
        self.start_with(token, &[])
    }

    /// Starts the Host with more on its command line.
    fn start_with(&self, token: &str, more: &[&str]) -> Host {
        let mut process = Command::new(env!("CARGO_BIN_EXE_clarp-host"))
            .args(["--data", self.root.join("data").to_str().unwrap()])
            .args(["--run", self.root.join("run").to_str().unwrap()])
            .args(["--cwd", self.root.join("work").to_str().unwrap()])
            .args(["--listen", "127.0.0.1:0"])
            .args(more)
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
    assert_eq!(
        host.state(&session),
        "spawned",
        "an agent that has never run a turn"
    );
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
    // An agent the Host does not have has no conversation, and cannot be chosen or written to.
    assert_eq!(host.get("/log?session=nobody")["missing"], true);
    let select = host.ask("POST", "/select", Some(&json!({ "session": "nobody" })));
    assert_eq!(
        (select.0, &select.1["error"]),
        (404, &json!("no such session"))
    );
    let lost = host.ask(
        "POST",
        "/send",
        Some(&json!({ "session": "nobody", "text": "x" })),
    );
    assert_eq!(lost.0, 404);
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
    assert_eq!(held["paused"], true);
    assert_eq!(held["items"].as_array().unwrap().len(), 1);
    assert_eq!(
        (&held["items"][0]["id"], &held["items"][0]["text"]),
        (&json!("m2"), &json!("later")),
        "what waits is named as the client named it"
    );
    assert!(
        !host.conversation(&session).contains(&said("user", "later")),
        "what was left for later is in the queue, not yet in the conversation"
    );

    // The user says something new. It runs although the queue is held, and the hold stays.
    let now = host.send(&session, "this instead", "m3");
    assert_eq!(
        (&now["queued"], &now["queue_depth"], &now["queue_revision"]),
        (&json!(false), &json!(0), &json!(0))
    );
    wait_for("the new message to be answered", || {
        host.conversation(&session)
            .contains(&said("assistant", "this instead"))
    });
    let held = host.get(&format!("/turn-queue?session={session}"));
    assert_eq!(
        (&held["paused"], held["items"].as_array().unwrap().len()),
        (&json!(true), 1),
        "what was held is still held"
    );

    let resumed = host.post("/turn-queue/resume", json!({ "session": session }));
    assert_eq!(
        (&resumed["paused"], &resumed["count"]),
        (&json!(false), &json!(0))
    );
    assert!(resumed["revision"].as_u64().unwrap() > held["revision"].as_u64().unwrap());
    wait_for("the held message to be answered", || {
        host.conversation(&session)
            .contains(&said("assistant", "later"))
    });
    // It entered the conversation when its turn started: after what was said in between.
    let conversation = host.conversation(&session);
    let place = |row: (String, String)| conversation.iter().position(|known| *known == row);
    assert!(place(said("user", "this instead")) < place(said("user", "later")));
    wait_for("the agent to be done", || host.state(&session) == "done");
}

/// Runs one of an agent's own tools the way a worker would: the Host's binary under the tool's
/// name, told where the Host is. Answers the exit code and what was printed.
fn tool(
    host: &Host,
    dir: &std::path::Path,
    worker: Option<u32>,
    args: &[&str],
) -> (i32, String, String) {
    let mut command = Command::new(env!("CARGO_BIN_EXE_clarp-host"));
    command
        .arg("tool")
        .args(args)
        .current_dir(dir)
        .env("CLARP_HOST_ADDR", &host.address)
        .env("CLARP_HOST_TOKEN", &host.token)
        .env_remove("CLARP_BACKGROUND_WORKER_PID");
    if let Some(pid) = worker {
        command.env("CLARP_BACKGROUND_WORKER_PID", pid.to_string());
    }
    let out = command.output().unwrap();
    (
        out.status.code().unwrap_or(-1),
        String::from_utf8_lossy(&out.stdout).into_owned(),
        String::from_utf8_lossy(&out.stderr).into_owned(),
    )
}

#[test]
fn a_worker_registers_a_background_job_with_the_agents_own_tool_and_the_apps_see_it() {
    let place = Place::new("jobs");
    let fast = ["--jobs-checked-every-ms", "200"];
    let host = place.start_with("", &fast);
    let session = host.new_agent("Builder");
    let dir = place.root.clone();
    let job = |host: &Host, id: &str| {
        host.get("/background-jobs")["jobs"]
            .as_array()
            .unwrap()
            .iter()
            .find(|job| job["job_id"] == id)
            .cloned()
            .unwrap_or(Value::Null)
    };
    let count = |host: &Host| {
        host.get("/agents/snapshot")["agents"][0]["background_jobs"]["count"]
            .as_u64()
            .unwrap()
    };

    // A worker is a process the agent left running. It names itself, and registers its job.
    let mut worker = Command::new("sleep").arg("60").spawn().unwrap();
    let pid = worker.id();
    let (code, handle, _) = tool(
        &host,
        &dir,
        Some(pid),
        &[
            "clarp-agent-bg",
            &session,
            "job-upsert",
            "build-1",
            "build",
            "Build it",
            "the whole thing",
        ],
    );
    assert_eq!((code, handle.as_str()), (0, "bg1:1:build-1\n"));
    let seen = job(&host, "build-1");
    assert_eq!(
        (
            &seen["status"],
            &seen["session"],
            &seen["worker_pid"],
            &seen["generation"]
        ),
        (&json!("running"), &json!(session), &json!(pid), &json!(1))
    );
    assert_eq!(
        (
            &seen["worker_freshness"],
            &seen["can_cancel"],
            &seen["outcome_state"],
            &seen["agent_name"]
        ),
        (
            &json!("fresh"),
            &json!(true),
            &json!("pending"),
            &json!("Builder")
        )
    );
    assert_eq!(count(&host), 1);

    // It says how far it is and where its log is; whoever looks at the job is shown both.
    std::fs::write(dir.join("build.log"), "compiling\nlinking\n").unwrap();
    let log = dir.join("build.log");
    let said = |args: &[&str]| tool(&host, &dir, Some(pid), args).0;
    assert_eq!(
        said(&[
            "clarp-agent-bg",
            &session,
            "job-progress",
            "bg1:1:build-1",
            "step  1/2"
        ]),
        0
    );
    assert_eq!(
        said(&[
            "clarp-agent-bg",
            &session,
            "job-log",
            "bg1:1:build-1",
            log.to_str().unwrap()
        ]),
        0
    );
    assert_eq!(
        said(&[
            "clarp-agent-bg",
            &session,
            "job-log",
            "bg1:1:build-1",
            "build.log"
        ]),
        2
    );
    assert_eq!(
        said(&["clarp-agent-bg", &session, "job-heartbeat", "bg1:1:build-1"]),
        0
    );
    assert_eq!(
        said(&["clarp-agent-bg", &session, "job-active", "bg1:1:build-1"]),
        0
    );
    let detail = host.get("/background-jobs/build-1");
    assert_eq!(
        (
            &detail["handle"],
            &detail["owner_session"],
            &detail["progress"]["text"]
        ),
        (&json!("bg1:1:build-1"), &json!(session), &json!("step 1/2"))
    );
    assert_eq!(
        (&detail["log"]["available"], &detail["log"]["text"]),
        (&json!(true), &json!("compiling\nlinking\n"))
    );
    let changes: Vec<(&str, &str)> = detail["timeline"]
        .as_array()
        .unwrap()
        .iter()
        .map(|entry| {
            (
                entry["change"].as_str().unwrap(),
                entry["note"].as_str().unwrap(),
            )
        })
        .collect();
    assert_eq!(changes, vec![("status", ""), ("progress", "step 1/2")]);

    // Another process does not speak for this run, and neither does another run.
    assert_eq!(
        tool(
            &host,
            &dir,
            None,
            &["clarp-agent-bg", &session, "job-finish", "bg1:1:build-1"]
        )
        .0,
        1
    );
    assert_eq!(
        said(&["clarp-agent-bg", &session, "job-finish", "bg1:2:build-1"]),
        1
    );
    assert_eq!(job(&host, "build-1")["status"], "running");

    // The worker dies without a word. The Host notices that its process is gone.
    worker.kill().unwrap();
    worker.wait().unwrap();
    wait_for("the job to be given up", || {
        job(&host, "build-1")["status"] == "failed"
    });
    let lost = job(&host, "build-1");
    assert_eq!(
        (
            &lost["terminal_reason"],
            &lost["outcome_state"],
            &lost["worker_freshness"]
        ),
        (
            &json!("worker_vanished"),
            &json!("unknown"),
            &json!("stale")
        ),
        "lost, which is not the same as failed"
    );
    assert_eq!(
        said(&["clarp-agent-bg", &session, "job-active", "bg1:1:build-1"]),
        1
    );
    assert_eq!(count(&host), 0);

    // The id is used again: a new run, and the Host that is started again still knows it.
    let mut second = Command::new("sleep").arg("60").spawn().unwrap();
    let (code, handle, _) = tool(
        &host,
        &dir,
        Some(second.id()),
        &[
            "clarp-agent-bg",
            &session,
            "job-upsert",
            "build-1",
            "build",
            "Build it again",
        ],
    );
    assert_eq!((code, handle.as_str()), (0, "bg1:2:build-1\n"));
    host.kill();
    let host = place.start_with("", &fast);
    assert_eq!(
        (
            &job(&host, "build-1")["status"],
            &job(&host, "build-1")["generation"]
        ),
        (&json!("running"), &json!(2))
    );
    assert_eq!(
        tool(
            &host,
            &dir,
            Some(second.id()),
            &["clarp-agent-bg", &session, "job-finish", "bg1:2:build-1"]
        )
        .0,
        0
    );
    assert_eq!(job(&host, "build-1")["status"], "succeeded");
    second.kill().unwrap();
    second.wait().unwrap();
    // What is over is not cancelled, and a cancel names the run it means.
    let (status, refused) = host.ask(
        "DELETE",
        "/background-jobs/build-1?expected_generation=1",
        None,
    );
    assert_eq!(
        (status, &refused["error"]),
        (409, &json!("generation_mismatch"))
    );
    assert_eq!(refused["current_generation"], 2);
    let (status, refused) = host.ask("DELETE", "/background-jobs/build-1", None);
    assert_eq!(
        (status, &refused["error"]),
        (409, &json!("job already succeeded"))
    );
    assert_eq!(host.ask("DELETE", "/background-jobs/nobody", None).0, 404);

    // The user cancels a job that runs: it is closed for its worker at once, and its owner
    // is told, so that it can clean up.
    let (code, handle, _) = tool(
        &host,
        &dir,
        None,
        &[
            "clarp-agent-bg",
            &session,
            "job-upsert",
            "watch-1",
            "watch",
            "Watch the build",
        ],
    );
    assert_eq!((code, handle.as_str()), (0, "bg1:1:watch-1\n"));
    let (status, receipt) = host.ask(
        "DELETE",
        "/background-jobs/watch-1?expected_generation=1",
        None,
    );
    assert_eq!(
        (
            status,
            &receipt["ok"],
            &receipt["changed"],
            &receipt["job"]["status"]
        ),
        (200, &json!(true), &json!(true), &json!("cancelled"))
    );
    assert_eq!(
        tool(
            &host,
            &dir,
            None,
            &["clarp-agent-bg", &session, "job-cancelled", "bg1:1:watch-1"]
        )
        .0,
        0
    );
    assert_eq!(
        tool(
            &host,
            &dir,
            None,
            &["clarp-agent-bg", &session, "job-active", "bg1:1:watch-1"]
        )
        .0,
        1
    );
    wait_for("the owner to be told", || {
        host.conversation(&session).iter().any(|(role, text)| {
            role == "user" && text.starts_with("the user cancelled background job watch-1")
        })
    });
    // A cancelled job stays cancelled when its worker registers again, until it is restarted.
    let again = tool(
        &host,
        &dir,
        None,
        &[
            "clarp-agent-bg",
            &session,
            "job-upsert",
            "watch-1",
            "watch",
            "Watch",
        ],
    );
    assert_eq!((again.0, again.1.as_str()), (1, "bg1:1:watch-1\n"));
    let again = tool(
        &host,
        &dir,
        None,
        &[
            "clarp-agent-bg",
            &session,
            "job-restart",
            "watch-1",
            "watch",
            "Watch",
        ],
    );
    assert_eq!((again.0, again.1.as_str()), (0, "bg1:2:watch-1\n"));

    // A job's id is one name on the whole Host.
    let other = host.new_agent("Other");
    let taken = tool(
        &host,
        &dir,
        None,
        &[
            "clarp-agent-bg",
            &other,
            "job-upsert",
            "watch-1",
            "watch",
            "Mine",
        ],
    );
    assert_eq!(taken.0, 1);
    assert!(
        taken.2.contains("already belongs to another agent"),
        "{}",
        taken.2
    );
    let unknown = tool(
        &host,
        &dir,
        None,
        &["clarp-agent-bg", "nobody", "job-upsert", "x", "k", "t"],
    );
    assert_eq!(
        (unknown.0, unknown.2.as_str()),
        (1, "agent_bg: unknown session: nobody\n")
    );
    assert_eq!(tool(&host, &dir, None, &["clarp-agent-bg", &session]).0, 2);

    // The agent says what it left running: shown with it while it rests, in a few words.
    let row = |host: &Host| host.get("/agents/snapshot")["agents"][0].clone();
    let on = tool(
        &host,
        &dir,
        None,
        &[
            "clarp-agent-bg",
            &session,
            "on",
            "Watching the long build go by",
        ],
    );
    assert_eq!(
        (on.0, on.2.as_str()),
        (0, "agent_bg: shortened status to 'Watching the long'\n")
    );
    wait_for("the label to show", || {
        row(&host)["status_text"] == "Watching the long"
    });
    assert_eq!(row(&host)["latest_state"], "background");
    assert_eq!(
        tool(&host, &dir, None, &["clarp-agent-bg", &session, "off"]).0,
        0
    );
    wait_for("the label to go", || row(&host)["status_text"].is_null());
    assert_eq!(row(&host)["latest_state"], "idle");

    // A tool this Host does not have yet says so, and does nothing.
    let absent = tool(&host, &dir, None, &["clarp-sub-agent", "list"]);
    assert_eq!(absent.0, 69);
    assert!(
        absent.2.contains("not available on this Host yet"),
        "{}",
        absent.2
    );
}

/// Calls the goal tool and reads the plan it prints.
fn goal(host: &Host, dir: &std::path::Path, args: &[&str]) -> Value {
    let mut all = vec!["clarp-goal"];
    all.extend_from_slice(args);
    let (code, out, err) = tool(host, dir, None, &all);
    assert_eq!(code, 0, "clarp-goal {args:?} said: {err}");
    serde_json::from_str(&out).unwrap()
}

#[test]
fn an_agent_keeps_a_goal_with_its_own_tool_and_still_has_it_after_the_host_is_killed() {
    let place = Place::new("goal");
    let host = place.start();
    let session = host.new_agent("Planner");
    let dir = place.root.clone();
    let items = json!([
        { "id": "probe", "title": "Probe the behaviour" },
        { "id": "build", "title": "Build it" },
    ])
    .to_string();
    let commitment = json!({
        "outcome": "The thing is shipped",
        "criteria": ["It runs"],
        "limits": "No publication",
    })
    .to_string();
    let plan = goal(
        &host,
        &dir,
        &[
            "create",
            &session,
            "ship",
            "Ship the thing",
            &items,
            &commitment,
        ],
    );
    let plan_id = plan["plan_id"].as_str().unwrap().to_string();
    assert_eq!(plan["revision"], 0);
    assert_eq!(plan["session"], session.as_str());
    assert_eq!(plan["goal"]["continuation"]["state"], "not_enrolled");
    assert!(plan_id.contains(":ship:"), "{plan_id}");

    let plan = goal(
        &host,
        &dir,
        &["step", &plan_id, "0", "probe", "completed", "It behaves"],
    );
    assert_eq!(plan["counts"]["completed"], 1);
    let note = json!({
        "name": "working-notes.md", "format": "markdown", "content": "# Notes\nFirst.",
        "document_revision": 0, "reason": "Initial findings",
    })
    .to_string();
    let plan = goal(&host, &dir, &["document", &plan_id, "1", &note]);
    assert_eq!(plan["goal"]["documents"][0]["name"], "working-notes.md");
    let checkpoint = json!({
        "progress": "Probe done", "next_work": "Build it",
        "evidence": { "criterion-1": "ran it" },
    })
    .to_string();
    let plan = goal(&host, &dir, &["checkpoint", &plan_id, "2", &checkpoint]);
    assert_eq!(plan["revision"], 3);

    // What the tool refuses, it refuses in the Python tool's words and with its exit code.
    let stale = tool(
        &host,
        &dir,
        None,
        &["clarp-goal", "step", &plan_id, "0", "build", "in_progress"],
    );
    assert_eq!(
        (stale.0, stale.2.as_str()),
        (1, "agent_tasks: plan changed; reload before editing\n")
    );
    let nobody = tool(
        &host,
        &dir,
        None,
        &[
            "clarp-goal",
            "create",
            "nobody",
            "ship",
            "Ship",
            "[]",
            &commitment,
        ],
    );
    assert_eq!(
        (nobody.0, nobody.2.as_str()),
        (1, "agent_tasks: agent session not found\n")
    );
    assert_eq!(tool(&host, &dir, None, &["clarp-goal", "frobnicate"]).0, 2);
    // The other name the skills know the tool by.
    let listed = tool(&host, &dir, None, &["clarp-agent-tasks", "list", &session]);
    let listed: Value = serde_json::from_str(&listed.1).unwrap();
    assert_eq!(listed[0]["plan_id"], plan_id.as_str());

    // The Host is killed. What the tool said was written is there when it is back.
    let before = goal(&host, &dir, &["get", &plan_id]);
    host.kill();
    let host = place.start();
    let mut after = goal(&host, &dir, &["get", &plan_id]);
    after["server_now"] = before["server_now"].clone();
    assert_eq!(after, before);
    let read = goal(&host, &dir, &["read", &plan_id, "working-notes.md"]);
    assert_eq!(read["text"], "# Notes\nFirst.");
    assert_eq!(
        goal(&host, &dir, &["show", &session])["plan_id"],
        plan_id.as_str()
    );

    // And the work goes on from there, to its end.
    let plan = goal(&host, &dir, &["step", &plan_id, "3", "build", "completed"]);
    let revision = plan["revision"].to_string();
    let plan = goal(&host, &dir, &["complete", &plan_id, &revision]);
    assert_eq!(plan["status"], "completed");
    assert_eq!(plan["completion_verified"], true);
    assert_eq!(goal(&host, &dir, &["show", &session]), json!({}));
}

#[test]
fn the_apps_read_an_agents_plans_and_the_user_sets_a_criterion_aside() {
    let place = Place::new("plans");
    let host = place.start();
    let session = host.new_agent("Planner");
    let dir = place.root.clone();
    let events = host.listen();

    // Nothing is planned yet, and the routes say so the way the Python Host does.
    assert_eq!(
        host.get(&format!("/task-plan?session={session}")),
        json!({ "plan": null })
    );
    assert_eq!(
        host.get(&format!("/task-plans?session={session}")),
        json!({ "plans": [] })
    );
    assert_eq!(
        host.get("/task-plan?session=nobody"),
        json!({ "plan": null })
    );
    assert_eq!(
        host.ask("GET", "/task-plan", None),
        (400, json!({ "error": "session required" }))
    );
    assert_eq!(
        host.ask("GET", "/task-plans?session=", None),
        (400, json!({ "error": "session required" }))
    );

    // The agent makes a goal with its own tool. The apps read it.
    let items = json!([{ "id": "build", "title": "Build it" }]).to_string();
    let commitment = json!({
        "outcome": "The thing is shipped",
        "criteria": ["It runs", "It is documented"],
        "limits": "No publication",
    })
    .to_string();
    let made = goal(
        &host,
        &dir,
        &[
            "create",
            &session,
            "ship",
            "Ship the thing",
            &items,
            &commitment,
        ],
    );
    let plan_id = made["plan_id"].as_str().unwrap().to_string();
    let read = host.get(&format!("/task-plan?session={session}"));
    assert_eq!(read["plan"]["plan_id"], plan_id.as_str());
    assert_eq!(
        read["plan"]["goal"]["criteria"][1]["text"],
        "It is documented"
    );
    let all = host.get(&format!("/task-plans?session={session}"));
    assert_eq!(all["plans"].as_array().unwrap().len(), 1);
    assert_eq!(all["plans"][0]["plan_id"], plan_id.as_str());

    // The user sets a criterion aside. The agent cannot: it could complete anything so.
    let waive = json!({ "criterion_id": "criterion-2", "reason": "Not needed after all" });
    let own = tool(
        &host,
        &dir,
        None,
        &[
            "clarp-goal",
            "act",
            &plan_id,
            "0",
            "waive",
            &waive.to_string(),
        ],
    );
    assert_eq!(
        (own.0, own.2.as_str()),
        (
            1,
            "agent_tasks: only the user can waive or restore a criterion\n"
        )
    );
    let act = |revision: Value, action: &str, data: Value| {
        host.ask(
            "POST",
            "/task-plan/action",
            Some(&json!({
                "plan_id": plan_id, "revision": revision, "action": action, "data": data,
            })),
        )
    };
    let heard_before = events.lock().unwrap().len();
    let (status, answer) = act(json!(0), "waive", waive.clone());
    assert_eq!(status, 200, "{answer}");
    assert_eq!(answer["plan"]["revision"], 1);
    let waived = &answer["plan"]["goal"]["criteria"][1]["waived"];
    assert_eq!(
        (&waived["by"], &waived["reason"]),
        (&json!("user"), &json!("Not needed after all"))
    );
    // The plan is something the agent made: the apps hear that it changed.
    wait_for("the apps to hear of the changed plan", || {
        events.lock().unwrap()[heard_before..]
            .iter()
            .any(|event| event["type"] == "artifact-updated")
    });

    // What the rules refuse is answered 409 with the rule, as the Python Host answers it.
    assert_eq!(
        act(json!(0), "waive", waive.clone()),
        (
            409,
            json!({ "error": "plan changed; reload before editing" })
        )
    );
    assert_eq!(
        act(json!(1), "waive", json!({ "criterion_id": "criterion-2" })),
        (
            409,
            json!({ "error": "waiving a criterion requires a reason" })
        )
    );
    assert_eq!(
        act(
            json!(1),
            "unwaive",
            json!({ "criterion_id": "criterion-1" })
        ),
        (409, json!({ "error": "criterion is not waived" }))
    );
    assert_eq!(
        act(json!(null), "pause", json!({ "reason": "x" })),
        (409, json!({ "error": "revision required" }))
    );
    assert_eq!(
        host.ask(
            "POST",
            "/task-plan/action",
            Some(&json!({
                "plan_id": "nobody:ship:01", "revision": 0, "action": "pause",
                "data": { "reason": "x" },
            })),
        ),
        (409, json!({ "error": "task plan not found" }))
    );

    // A document of the goal, as the plan screen opens it.
    let note = json!({
        "name": "working-notes.md", "format": "markdown", "content": "# Notes\nFirst.",
        "document_revision": 0, "reason": "Initial findings",
    })
    .to_string();
    goal(&host, &dir, &["document", &plan_id, "1", &note]);
    let document = host.get(&format!(
        "/task-plan/document?plan_id={plan_id}&name=working-notes.md"
    ));
    assert_eq!(document["document"]["text"], "# Notes\nFirst.");
    assert_eq!(document["document"]["revision"], 1);
    assert_eq!(
        host.ask(
            "GET",
            &format!("/task-plan/document?plan_id={plan_id}&name=nothing.md"),
            None
        ),
        (404, json!({ "error": "goal document not found" }))
    );
    assert_eq!(
        host.ask(
            "GET",
            &format!("/task-plan/document?plan_id={plan_id}&name=working-notes.md&revision=two"),
            None
        ),
        (
            404,
            json!({ "error": "invalid literal for int() with base 10: 'two'" })
        )
    );

    // The Host is killed. What the user decided is there when it is back, and the goal can
    // end with one criterion shown and the other set aside.
    host.kill();
    let host = place.start();
    let read = host.get(&format!("/task-plan?session={session}"));
    assert_eq!(read["plan"]["goal"]["criteria"][1]["waived"]["by"], "user");
    let revision = read["plan"]["revision"].clone();
    let (status, answer) = host.ask(
        "POST",
        "/task-plan/action",
        Some(&json!({
            "plan_id": plan_id, "revision": revision, "action": "checkpoint",
            "data": {
                "progress": "Seen by the user", "next_work": "Finish",
                "evidence": { "criterion-1": "ran it" },
            },
        })),
    );
    assert_eq!(status, 200, "{answer}");
    let revision = answer["plan"]["revision"].to_string();
    let plan = goal(
        &host,
        &dir,
        &["step", &plan_id, &revision, "build", "completed"],
    );
    let revision = plan["revision"].to_string();
    let plan = goal(&host, &dir, &["complete", &plan_id, &revision]);
    assert_eq!(plan["status"], "completed");
    assert_eq!(
        host.get(&format!("/task-plan?session={session}")),
        json!({ "plan": null }),
        "a plan that ended is not the one being worked"
    );
    assert_eq!(
        host.get(&format!("/task-plans?session={session}"))["plans"][0]["status"],
        "completed"
    );
}

#[test]
fn an_agent_asks_its_user_and_is_told_the_answer_and_what_it_made_is_in_the_library() {
    let place = Place::new("library");
    let host = place.start();
    let session = host.new_agent("Asker");
    let dir = place.root.clone();
    let events = host.listen();
    let told = |host: &Host, begins: &str| {
        let all = host.conversation(&session);
        all.iter()
            .any(|(role, text)| role == "user" && text.starts_with(begins))
    };
    let error = |answer: &(u16, Value)| (answer.0, answer.1["error"].as_str().unwrap().to_string());

    // What an agent made: a document, with what its kind shows on the artifact itself.
    let document = json!({
        "session": session, "type": "document", "title": "Notes", "summary": "What was found",
        "payload": { "content": "# Notes" },
    });
    let (status, made) = host.ask("POST", "/artifacts", Some(&document));
    assert_eq!(status, 201);
    let document = made["artifact"].clone();
    let document_id = document["artifact_id"].as_str().unwrap().to_string();
    assert_eq!(
        (
            &document["content"],
            &document["agent_name"],
            &document["status"]
        ),
        (&json!("# Notes"), &json!("Asker"), &json!("ready"))
    );
    // What is refused is refused in the Python Host's words, as a conflict.
    let plan = json!({ "session": session, "type": "plan", "title": "A plan" });
    let refused = host.ask("POST", "/artifacts", Some(&plan));
    assert_eq!(error(&refused), (409, "reserved artifact type".to_string()));
    let nobody = json!({ "session": "nobody", "type": "document", "title": "x", "payload": { "content": "x" } });
    let refused = host.ask("POST", "/artifacts", Some(&nobody));
    assert_eq!(error(&refused), (409, "unknown agent session".to_string()));
    let empty = json!({ "session": session, "type": "document", "title": "x" });
    let refused = host.ask("POST", "/artifacts", Some(&empty));
    assert_eq!(
        error(&refused),
        (
            409,
            "document artifact requires payload field(s): content".to_string()
        )
    );
    let refused = host.ask("GET", "/artifacts/no-such", None);
    assert_eq!(error(&refused), (404, "artifact not found".to_string()));

    // A plan the agent makes with its goal tool is in the library, as an artifact that
    // follows the plan. A list leaves the plan's history out and says what it left out.
    let commitment = json!({
        "outcome": "The thing is shipped", "criteria": ["It runs"], "limits": "No publication",
    })
    .to_string();
    let plan = goal(
        &host,
        &dir,
        &[
            "create",
            &session,
            "ship",
            "Ship the thing",
            "[]",
            &commitment,
        ],
    );
    let plan_id = plan["plan_id"].as_str().unwrap().to_string();
    let library = |host: &Host, how: &str| {
        host.get(&format!("/artifacts?session={session}{how}"))["artifacts"]
            .as_array()
            .unwrap()
            .clone()
    };
    let listed = library(&host, "&representation=summary-v1");
    assert_eq!(listed.len(), 2);
    let shown = listed.iter().find(|row| row["type"] == "plan").unwrap();
    assert_eq!(shown["reference_id"], plan_id.as_str());
    assert_eq!(
        (&shown["status"], &shown["title"]),
        (&json!("active"), &json!("Ship the thing"))
    );
    assert!(shown.get("payload").is_none(), "a summary has no payload");
    let left_out = &shown["plan"]["goal"]["history_omitted"];
    assert_eq!(
        (&left_out["count"], &left_out["reason"]),
        (&json!(1), &json!("summary"))
    );
    assert!(shown["plan"]["goal"].get("history").is_none());
    let whole = library(&host, "&type=plan");
    assert_eq!(
        whole[0]["plan"]["goal"]["history"]
            .as_array()
            .unwrap()
            .len(),
        1
    );
    goal(
        &host,
        &dir,
        &["pause", &plan_id, "0", r#"{"reason": "Later"}"#],
    );
    goal(
        &host,
        &dir,
        &["cancel", &plan_id, "1", r#"{"reason": "Not needed"}"#],
    );
    let shown = library(&host, "&type=plan")[0].clone();
    assert_eq!(
        (&shown["status"], &shown["plan"]["status"]),
        (&json!("cancelled"), &json!("cancelled"))
    );

    // The agent asks for an approval. It is in the user's inbox.
    let ask = json!({
        "session": session, "title": "Deploy?", "question": "Deploy version 1.2?",
        "context": "Only the web service.", "reference_id": "deploy-1.2",
    });
    let (status, asked) = host.ask("POST", "/decisions", Some(&ask));
    assert_eq!(status, 201);
    let request = asked["artifact"]["decision"].clone();
    let request_id = request["decision_id"].as_str().unwrap().to_string();
    assert_eq!(
        (&request["status"], &request["revision"]),
        (&json!("pending"), &json!(1))
    );
    let inbox = host.get("/attention");
    assert_eq!(
        (&inbox["count"], &inbox["decision_format"]),
        (&json!(1), &json!(2))
    );
    assert_eq!(inbox["items"][0]["decision_id"], request_id.as_str());
    assert_eq!(inbox["items"][0]["kind"], "decision");

    // The user answers. The agent is told, once, and the request leaves the inbox.
    let resolve = format!("/decisions/{request_id}/resolve");
    let stale = host.ask(
        "POST",
        &resolve,
        Some(&json!({ "choice": "accepted", "expected_revision": 2 })),
    );
    assert_eq!(
        error(&stale),
        (409, "decision revision changed".to_string())
    );
    let neither = host.ask("POST", &resolve, Some(&json!({ "expected_revision": 1 })));
    assert_eq!(
        error(&neither),
        (
            400,
            "exactly one of choice or answer is required".to_string()
        )
    );
    let accept = json!({ "choice": "accepted", "expected_revision": 1 });
    let (status, resolved) = host.ask("POST", &resolve, Some(&accept));
    assert_eq!(status, 200);
    assert_eq!(
        (&resolved["changed"], &resolved["delivery_pending"]),
        (&json!(true), &json!(false))
    );
    assert_eq!(resolved["artifact"]["decision"]["status"], "accepted");
    wait_for("the agent to be told the answer", || {
        told(&host, "[Clarp decision resolved]")
    });
    let answer = host
        .conversation(&session)
        .into_iter()
        .find(|(_, text)| text.contains("approved this exact action"))
        .unwrap()
        .1;
    assert!(
        answer.contains("The user approved this exact action: Deploy version 1.2? Approved scope/context: Only the web service."),
        "{answer}"
    );
    assert!(
        answer.contains(&format!("Decision ID: {request_id}\n")),
        "{answer}"
    );
    assert_eq!(host.get("/attention")["count"], 0);
    let (status, again) = host.ask("POST", &resolve, Some(&accept));
    assert_eq!((status, &again["changed"]), (200, &json!(false)));
    let other = host.ask(
        "POST",
        &resolve,
        Some(&json!({ "choice": "rejected", "expected_revision": 2 })),
    );
    assert_eq!(
        error(&other),
        (
            409,
            "decision already resolved with a different choice or answer".to_string()
        )
    );
    wait_for("the apps to be told", || {
        let seen = events.lock().unwrap();
        let of = |kind: &str| seen.iter().filter(|event| event["type"] == kind).count();
        let counts: Vec<&Value> = seen
            .iter()
            .filter(|event| event["type"] == "attention-updated")
            .map(|event| &event["attention_count"])
            .collect();
        of("artifact-updated") >= 5
            && counts.contains(&&json!(1))
            && counts.last() == Some(&&json!(0))
    });
    wait_for("the agent to rest", || host.state(&session) == "done");

    // A question nobody answers runs out, and the agent is told that it did.
    let question = json!({
        "session": session, "title": "Which?", "question": "Which way?",
        "response_type": "single_choice",
        "options": [{ "id": "a", "label": "The first" }, { "id": "b", "label": "The second" }],
        "expires_at": now_ms() + 500,
    });
    let (status, asked) = host.ask("POST", "/decisions", Some(&question));
    assert_eq!(status, 201);
    let question_id = asked["artifact"]["artifact_id"]
        .as_str()
        .unwrap()
        .to_string();
    assert_eq!(host.get("/attention?decision_format=2")["count"], 1);
    assert_eq!(
        host.get("/attention")["count"],
        0,
        "a question is not shown as an approval"
    );
    wait_for("the question to run out", || {
        host.get(&format!("/artifacts/{question_id}"))["artifact"]["decision"]["status"]
            == "expired"
    });
    wait_for("the agent to be told it ran out", || {
        host.conversation(&session)
            .iter()
            .any(|(_, text)| text.contains("The request expired without an answer or approval."))
    });

    // The same thing is not asked twice: the reference is taken for this agent and kind.
    let twice = host.ask("POST", "/decisions", Some(&ask));
    assert_eq!(
        error(&twice),
        (
            409,
            "UNIQUE constraint failed: artifacts.agent_id, artifacts.type, artifacts.reference_id"
                .to_string()
        )
    );
    // A request the agent takes back is not told to it.
    let mut ask = ask;
    ask["reference_id"] = json!("deploy-1.3");
    let (_, asked) = host.ask("POST", "/decisions", Some(&ask));
    let taken_back = asked["artifact"]["decision"]["decision_id"]
        .as_str()
        .unwrap()
        .to_string();
    let withdraw = format!("/decisions/{taken_back}/withdraw");
    let not_mine = host.ask(
        "POST",
        &withdraw,
        Some(&json!({ "session": "someone-else" })),
    );
    assert_eq!(
        error(&not_mine),
        (
            403,
            "only the asking agent may withdraw this request".to_string()
        )
    );
    let (status, withdrawn) = host.ask("POST", &withdraw, Some(&json!({ "session": session })));
    assert_eq!((status, &withdrawn["changed"]), (200, &json!(true)));
    assert_eq!(
        withdrawn["artifact"]["decision"]["resolved_choice"],
        "withdrawn"
    );

    // The agent's own tool asks the same things of the same Host: a question, with the
    // Python tool's command line. It waits for the answer while the user gives it.
    let artifacts = |args: &[&str]| {
        let mut all = vec!["clarp-agent-artifacts"];
        all.extend_from_slice(args);
        tool(&host, &dir, None, &all)
    };
    let options = r#"[{"id":"keep","label":"Keep it"},{"id":"new","label":"Make it new"}]"#;
    let asked = artifacts(&[
        "question",
        &session,
        "Choose",
        "Which one?",
        options,
        "--recommend",
        "keep",
        "--effort",
        "short",
    ]);
    assert_eq!(asked.0, 0, "{}", asked.2);
    let asked: Value = serde_json::from_str(&asked.1).unwrap();
    assert_eq!(
        (&asked["type"], &asked["decision"]["response_type"]),
        (&json!("question"), &json!("single_choice"))
    );
    assert_eq!(asked["decision"]["recommended_option_id"], "keep");
    let asked_id = asked["artifact_id"].as_str().unwrap().to_string();
    let request_id = asked["decision"]["decision_id"]
        .as_str()
        .unwrap()
        .to_string();
    let inbox = artifacts(&["attention", "--session", &session]);
    let inbox: Value = serde_json::from_str(&inbox.1).unwrap();
    assert_eq!(
        (&inbox["count"], &inbox["items"][0]["decision_id"]),
        (&json!(1), &json!(request_id))
    );
    let waiting = {
        let (address, token, dir, id) = (
            host.address.clone(),
            host.token.clone(),
            dir.clone(),
            asked_id.clone(),
        );
        std::thread::spawn(move || {
            Command::new(env!("CARGO_BIN_EXE_clarp-host"))
                .args([
                    "tool",
                    "clarp-agent-artifacts",
                    "wait",
                    &id,
                    "--timeout",
                    "20",
                    "--interval",
                    "0.5",
                ])
                .current_dir(dir)
                .env("CLARP_HOST_ADDR", address)
                .env("CLARP_HOST_TOKEN", token)
                .output()
                .unwrap()
        })
    };
    std::thread::sleep(Duration::from_millis(700));
    let answer = json!({ "answer": { "option_id": "new" }, "expected_revision": 1 });
    let (status, _) = host.ask(
        "POST",
        &format!("/decisions/{request_id}/resolve"),
        Some(&answer),
    );
    assert_eq!(status, 200);
    let waited = waiting.join().unwrap();
    assert_eq!(
        waited.status.code(),
        Some(0),
        "{}",
        String::from_utf8_lossy(&waited.stderr)
    );
    let outcome: Value = serde_json::from_slice(&waited.stdout).unwrap();
    assert_eq!(
        (&outcome["status"], &outcome["answer"]),
        (
            &json!("answered"),
            &json!({ "option_id": "new", "label": "Make it new" })
        )
    );
    wait_for("the agent to be told what was chosen", || {
        host.conversation(&session).iter().any(|(_, text)| {
            text.contains(
                r#"answered this clarification: {"option_id": "new", "label": "Make it new"}"#,
            )
        })
    });
    // A wait whose time is up says so, with its own exit code; so does one for nothing.
    let nothing = artifacts(&["wait", &document_id, "--timeout", "1", "--interval", "0.5"]);
    assert_eq!(nothing.0, 4, "{}", nothing.1);
    let missing = artifacts(&["wait", "no-such"]);
    assert_eq!(
        (missing.0, missing.2.as_str()),
        (
            1,
            "agent_artifacts: HTTP Error 404: Not Found: artifact not found\n"
        )
    );
    // The other verbs: an approval that is only looked at, a made artifact, its progress.
    let dry = artifacts(&[
        "decision",
        &session,
        "Deploy?",
        "Deploy 1.4?",
        "Deploy",
        "Hold",
        "--dry-run",
    ]);
    let dry: Value = serde_json::from_str(&dry.1).unwrap();
    assert_eq!(
        (&dry["path"], &dry["body"]["yes_label"]),
        (&json!("/decisions"), &json!("Deploy"))
    );
    let run = r#"{"provider":"github","run_id":"7","run_url":"https://github.com/o/r/actions/runs/7","workflow_name":"Tests"}"#;
    let made = artifacts(&["create", &session, "workflow_run", "CI", "The tests", run]);
    assert_eq!(made.0, 0, "{}", made.2);
    let made: Value = serde_json::from_str(&made.1).unwrap();
    let run_id = made["artifact_id"].as_str().unwrap().to_string();
    let progressed = artifacts(&["progress", &run_id, "0.5", "half way"]);
    let progressed: Value = serde_json::from_str(&progressed.1).unwrap();
    assert_eq!(
        (&progressed["progress"], &progressed["status"]),
        (&json!(0.5), &json!("active"))
    );
    let finished = artifacts(&["update", &run_id, "completed"]);
    let finished: Value = serde_json::from_str(&finished.1).unwrap();
    assert_eq!(finished["status"], "completed");
    let listed = artifacts(&["list", &session]);
    let listed: Value = serde_json::from_str(&listed.1).unwrap();
    assert_eq!(listed["artifacts"].as_array().unwrap().len(), 7);
    let refused = artifacts(&["create", &session, "poem", "A poem"]);
    assert_eq!(
        (refused.0, refused.2.as_str()),
        (
            1,
            "agent_artifacts: HTTP Error 409: Conflict: unsupported artifact type\n"
        )
    );
    assert_eq!(artifacts(&["frobnicate"]).0, 2);
    assert_eq!(artifacts(&[]).0, 1);
    assert_eq!(
        artifacts(&["create-report", &session, "Report", "report.html"]).0,
        69
    );

    // What the user does with an artifact: archive it, pin it, discard it.
    let at = document["updated_at"].as_i64().unwrap();
    let archive = format!("/artifacts/{document_id}/archive");
    let (status, archived) = host.ask(
        "POST",
        &archive,
        Some(&json!({ "archived": true, "expected_updated_at": at })),
    );
    assert_eq!((status, &archived["changed"]), (200, &json!(true)));
    assert!(archived["artifact"]["archived_at"].is_i64());
    let (status, pinned) = host.ask(
        "POST",
        &format!("/artifacts/{document_id}/pin"),
        Some(&json!({ "pinned": true })),
    );
    assert_eq!((status, &pinned["artifact"]["pinned"]), (200, &json!(true)));
    assert_eq!(
        host.get("/pinned-artifacts")["artifacts"][0]["artifact_id"],
        document_id.as_str()
    );
    let (status, updated) = host.ask(
        "POST",
        &format!("/artifacts/{document_id}"),
        Some(&json!({ "payload_patch": { "content": "# Notes, again" } })),
    );
    assert_eq!(
        (status, &updated["artifact"]["content"]),
        (200, &json!("# Notes, again"))
    );

    // The Host is killed. What was made, asked and answered is there when it is back, and
    // nothing is told a second time.
    let before = host.get(&format!("/artifacts?session={session}&order=created"));
    let told_before = host.conversation(&session).len();
    host.kill();
    let host = place.start();
    let mut after = host.get(&format!("/artifacts?session={session}&order=created"));
    for row in after["artifacts"].as_array_mut().unwrap() {
        if !row["plan"].is_null() {
            row["plan"]["server_now"] = json!(0);
        }
    }
    let mut before = before;
    for row in before["artifacts"].as_array_mut().unwrap() {
        if !row["plan"].is_null() {
            row["plan"]["server_now"] = json!(0);
        }
    }
    assert_eq!(after, before);
    assert_eq!(before["artifacts"].as_array().unwrap().len(), 7);
    assert_eq!(host.conversation(&session).len(), told_before);
    let at = updated["artifact"]["updated_at"].as_i64().unwrap();
    let discard = format!("/artifacts/{document_id}/discard");
    let (status, discarded) = host.ask(
        "POST",
        &discard,
        Some(&json!({ "expected_updated_at": at })),
    );
    assert_eq!((status, &discarded["changed"]), (200, &json!(true)));
    let gone = host.ask("GET", &format!("/artifacts/{document_id}"), None);
    assert_eq!(error(&gone), (404, "artifact not found".to_string()));
}

fn now_ms() -> i64 {
    let since = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH);
    i64::try_from(since.unwrap().as_millis()).unwrap()
}

#[test]
fn an_agent_is_woken_for_a_goal_that_is_due_and_a_stop_holds_its_goals_too() {
    let place = Place::new("wake");
    let host = place.start();
    let session = host.new_agent("Keeper");
    let dir = place.root.clone();
    let wakes = |host: &Host| {
        let begins = "Continue your durable outcome commitment.";
        let all = host.conversation(&session);
        all.iter()
            .filter(|(role, text)| role == "user" && text.starts_with(begins))
            .count()
    };
    let state_of = |host: &Host, plan_id: &str| goal(host, &dir, &["get", plan_id]);

    // A goal is bound to the conversation its owner has with its tool: the agent has one.
    host.send(&session, r#"{"session": "s1"}"#, "bind");
    wait_for("the first turn to end", || host.state(&session) == "done");
    let commitment = json!({
        "outcome": "It is kept", "criteria": ["It runs"], "limits": "None", "enroll": true,
    })
    .to_string();
    let plan = goal(
        &host,
        &dir,
        &["create", &session, "keep", "Keep it", "[]", &commitment],
    );
    let plan_id = plan["plan_id"].as_str().unwrap().to_string();
    assert_eq!(plan["recovery_enabled"], 1);
    assert_eq!(plan["goal"]["continuation"]["state"], "ready");
    assert_eq!(wakes(&host), 0);

    // The agent says when it wants to go on: in a moment. It is woken then, and answers.
    let checkpoint = json!({
        "progress": "Started", "next_work": "Go on",
        "continuation": { "kind": "timer", "due_at": now_ms() + 300, "reason": "Go on in a moment" },
    })
    .to_string();
    goal(&host, &dir, &["checkpoint", &plan_id, "0", &checkpoint]);
    wait_for("the agent to be woken", || wakes(&host) == 1);
    let woken_with = host
        .conversation(&session)
        .into_iter()
        .find(|(role, text)| role == "user" && text.contains("clarp-goal get"))
        .unwrap()
        .1;
    assert!(
        woken_with.contains(&format!("clarp-goal get {plan_id}\n")),
        "{woken_with}"
    );
    assert!(
        woken_with.contains("Continuation: Go on in a moment\n"),
        "{woken_with}"
    );
    wait_for("what the wake's turn came to", || {
        state_of(&host, &plan_id)["goal"]["continuation"]["last_execution"]["kind"] == "done"
    });
    let plan = state_of(&host, &plan_id);
    let wake = &plan["goal"]["continuation"];
    assert_eq!(
        (&wake["state"], &wake["attempts"]),
        (&json!("admitted"), &json!(1))
    );
    assert!(
        wake["request_id"]
            .as_str()
            .unwrap()
            .starts_with("task-goal-")
    );
    assert_eq!(
        plan["revision"], 1,
        "a wake does not make the agent's copy of its goal stale"
    );
    assert_eq!(
        wakes(&host),
        1,
        "it is not woken again before the goal is due again"
    );

    // A Stop holds the agent's goals too: nothing wakes an agent its user has just stopped.
    let stopped = host.post("/stop", json!({ "session": session }));
    assert_eq!(stopped["goals_paused"], 1);
    let plan = state_of(&host, &plan_id);
    assert_eq!(plan["status"], "paused");
    assert_eq!(
        plan["goal"]["continuation"]["reason"],
        "User stopped this agent"
    );

    // Resumed, the goal waits for a background job, and its owner is woken when the job ends.
    let revision = plan["revision"].to_string();
    let plan = goal(
        &host,
        &dir,
        &[
            "resume",
            &plan_id,
            &revision,
            r#"{"reason": "The user said go on"}"#,
        ],
    );
    let handle = tool(
        &host,
        &dir,
        None,
        &[
            "clarp-agent-bg",
            &session,
            "job-upsert",
            "build-1",
            "build",
            "The build",
        ],
    )
    .1;
    assert_eq!(handle.trim(), "bg1:1:build-1");
    let waiting = json!({
        "progress": "Build launched", "next_work": "Read its result",
        "continuation": {
            "kind": "dependency", "key": "build-1", "reason": "Waiting for the build",
            "due_at": now_ms() + 600_000, "job_handle": "bg1:1:build-1",
        },
    })
    .to_string();
    let revision = plan["revision"].to_string();
    let plan = goal(&host, &dir, &["checkpoint", &plan_id, &revision, &waiting]);
    assert_eq!(plan["goal"]["continuation"]["state"], "waiting");
    assert_eq!(wakes(&host), 1);
    let finished = tool(
        &host,
        &dir,
        None,
        &["clarp-agent-bg", &session, "job-finish", "bg1:1:build-1"],
    );
    assert_eq!(finished.0, 0, "{}", finished.2);
    wait_for("the agent to be woken for the job's end", || {
        wakes(&host) == 2
    });
    let plan = state_of(&host, &plan_id);
    let result = &plan["goal"]["continuation"]["dependency_result"];
    assert_eq!(
        (&result["key"], &result["outcome"]),
        (&json!("build-1"), &json!("succeeded"))
    );
    wait_for("the agent to rest", || host.state(&session) == "done");

    // The Host is killed. A goal that comes due while nobody looks wakes its owner when the
    // Host is back.
    let revision = state_of(&host, &plan_id)["revision"].to_string();
    let soon = json!({
        "progress": "Result read", "next_work": "Close",
        "continuation": { "kind": "timer", "due_at": now_ms() + 1_500, "reason": "Close it" },
    })
    .to_string();
    goal(&host, &dir, &["checkpoint", &plan_id, &revision, &soon]);
    host.kill();
    std::thread::sleep(Duration::from_millis(2_000));
    let host = place.start();
    wait_for("the agent to be woken after the restart", || {
        wakes(&host) == 3
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

/// A turn under the launcher a Host that runs as a systemd unit uses: the shim in a scope of its
/// own. Runs only where there is a user session of systemd to ask, and only when asked:
/// `CLARP_TEST_SYSTEMD=1 cargo test --test host -- --ignored a_turn_in_a_systemd_scope`.
#[test]
#[ignore = "starts transient systemd scopes: needs a systemd user session and CLARP_TEST_SYSTEMD=1"]
fn a_turn_in_a_systemd_scope_gets_its_tool_unchanged_and_outlives_the_host() {
    if std::env::var("CLARP_TEST_SYSTEMD").as_deref() != Ok("1") {
        eprintln!("skipped: CLARP_TEST_SYSTEMD is not 1");
        return;
    }
    let place = Place::new("systemd");
    let host = place.start_with("", &["--launcher", "systemd"]);
    let session = host.new_agent("Scoped");

    // The scripted tool's own script has `${...}` in it. If systemd-run rewrote the tool's
    // arguments, the sleep would be over at once.
    let began = Instant::now();
    host.send(&session, "sleep 3", "m1");
    wait_for("the turn to run", || host.state(&session) == "thinking");
    wait_for("the turn to end", || host.state(&session) == "done");
    assert!(
        began.elapsed() >= Duration::from_secs(3),
        "the tool ran as it was written: it slept"
    );
    assert!(
        host.conversation(&session)
            .contains(&said("assistant", "sleep 3"))
    );

    // The Host goes away in the middle of a turn. The turn is in a scope of its own.
    host.send(&session, "sleep 6", "m2");
    wait_for("the second turn to run", || {
        host.state(&session) == "thinking"
    });
    host.kill();
    std::thread::sleep(Duration::from_secs(1));
    let host = place.start_with("", &["--launcher", "systemd"]);
    wait_for("the second turn to end", || host.state(&session) == "done");
    let conversation = host.conversation(&session);
    assert_eq!(
        conversation
            .iter()
            .filter(|row| **row == said("assistant", "sleep 6"))
            .count(),
        1,
        "answered once: {conversation:?}"
    );
}

/// The same with a real Codex: `CLARP_TEST_CODEX=1 cargo test --test host -- --ignored`. Set
/// `CLARP_CODEX_PROGRAM` when Codex is not `codex` on the path.
#[test]
#[ignore = "runs a real Codex turn: needs a logged-in account and CLARP_TEST_CODEX=1"]
fn a_real_codex_turn_survives_a_kill_of_the_host_and_the_next_turn_remembers_it() {
    if std::env::var("CLARP_TEST_CODEX").as_deref() != Ok("1") {
        eprintln!("skipped: CLARP_TEST_CODEX is not 1");
        return;
    }
    let place = Place::new("codex");
    let host = place.start();
    let made = host.post(
        "/agents",
        json!({ "name": "Realx", "backend": "codex", "effort": "low" }),
    );
    let session = made["session"].as_str().unwrap().to_string();
    host.send(
        &session,
        "Run the shell command `sleep 12; echo spool-survived` exactly once. Then reply with one short sentence that says what it printed.",
        "real-1",
    );
    wait_long(&host, &session, "Codex to start its command", || {
        host.state(&session) == "tool"
    });

    // The Host dies while Codex's command sleeps, and is away for a moment.
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
        .expect("the command is in the conversation");
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
        "Codex's answer came through the restart: {answers:?}"
    );
    assert_eq!(
        turns.iter().filter(|turn| turn["role"] == "user").count(),
        1,
        "the message is there once"
    );
    let named = host.get(&format!("/log?session={session}"))["conversation_id"].clone();

    // The next turn resumes Codex's own thread: it knows what the last one did. The
    // conversation is still called what it was called before Codex had named its thread.
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
    assert_eq!(
        host.get(&format!("/log?session={session}"))["conversation_id"],
        named
    );
}

/// Pairs a device with this Host. Answers the device's token and its id.
fn pair(host: &Host, name: &str, scope: &str) -> (String, String) {
    let issued = host.post(
        "/pairing/issue",
        json!({ "device_name": name, "scope": scope, "url": "https://host.example:7690" }),
    );
    let (status, paired) = host.ask_as(
        "",
        "POST",
        "/pairing/exchange",
        Some(&json!({ "code": issued["code"], "device_name": "" })),
    );
    assert_eq!(status, 201, "{paired}");
    (
        paired["device"]["token"].as_str().unwrap().to_string(),
        paired["device"]["device_id"].as_str().unwrap().to_string(),
    )
}

#[test]
fn a_phone_registers_its_push_token_and_a_revoked_phone_is_sent_nothing() {
    let place = Place::new("push-register");
    let host = place.start_with_token("the-administrators-token");
    // The Host says that it takes a phone's grant and lets a phone take its token back:
    // an app registers this way only with a Host that says so.
    let info = host.get("/server-info");
    let features = info["capabilities"]["features"].as_array().unwrap();
    assert!(features.contains(&json!("push_grants")) && features.contains(&json!("pairing")));
    assert_eq!(info["contract"]["features"]["push_unregister"], 93);
    assert!(
        !features.contains(&json!("push_fcm")),
        "what is not built is not said to be there"
    );
    let (phone, phone_id) = pair(&host, "Phone", "full");
    let (tablet, _) = pair(&host, "Tablet", "full");
    let (reader, _) = pair(&host, "Reader", "limited");
    let (a, b) = ("a1".repeat(32), "b2".repeat(32));
    let grant = format!(
        "pg1.{}.{}.{}",
        "1".repeat(64),
        "2".repeat(16),
        "3".repeat(64)
    );
    let rows = |host: &Host, token: &str| {
        let (status, answer) = host.ask_as(token, "GET", "/devices/push-tokens", None);
        assert_eq!(status, 200, "{answer}");
        answer["tokens"].as_array().unwrap().clone()
    };
    let live = |rows: &[Value]| {
        rows.iter()
            .filter(|row| row["disabled_at"].is_null())
            .map(|row| row["token_prefix"].as_str().unwrap().to_string())
            .collect::<Vec<_>>()
    };
    let owned_elsewhere = (
        409,
        json!({ "ok": false, "error": "device_token_owned_elsewhere" }),
    );
    let not_found = (
        404,
        json!({ "ok": false, "error": "device_token_not_found" }),
    );

    // The phone registers its token. It is the phone's from then on: nobody takes it.
    let registered = host.ask_as(
        &phone,
        "POST",
        "/devices",
        Some(&json!({
            "token": a.to_uppercase(), "session": "vera", "push_grant": grant,
            "base_url": "https://host.example:7690/",
        })),
    );
    assert_eq!(registered, (200, json!({ "ok": true })));
    assert_eq!(
        host.ask_as(&tablet, "POST", "/devices", Some(&json!({ "token": a }))),
        owned_elsewhere
    );
    assert_eq!(
        host.ask("POST", "/devices", Some(&json!({ "token": a }))),
        owned_elsewhere,
        "nor the administrator"
    );
    assert_eq!(
        host.ask_as(
            &phone,
            "POST",
            "/devices",
            Some(&json!({ "token": "too-short" }))
        ),
        (400, json!({ "ok": false, "error": "device_token_invalid" }))
    );
    assert_eq!(
        host.ask_as(&phone, "POST", "/devices", Some(&json!({}))),
        (400, json!({ "error": "token required" }))
    );
    assert_eq!(
        host.ask_as(&reader, "POST", "/devices", Some(&json!({ "token": b })))
            .0,
        403,
        "a device with limited scope registers nothing"
    );

    // What the Host has: the administrator sees every row, a device its own, and nobody
    // the whole token.
    let all = rows(&host, "the-administrators-token");
    assert_eq!(all.len(), 1);
    assert_eq!(
        (
            &all[0]["token_prefix"],
            &all[0]["owner"],
            &all[0]["owner_can_receive"],
            &all[0]["environment"],
            &all[0]["provider"]
        ),
        (
            &json!("a1a1a1a1a1a1"),
            &json!(phone_id),
            &json!(true),
            &json!("production"),
            &json!("apns")
        )
    );
    assert!(all[0].get("token").is_none());
    assert_eq!(rows(&host, &tablet), Vec::<Value>::new());
    assert_eq!(
        host.ask_as(&reader, "GET", "/devices/push-tokens", None).0,
        403
    );

    // A new token of the same phone retires the old one.
    assert_eq!(
        host.ask_as(&phone, "POST", "/devices", Some(&json!({ "token": b })))
            .0,
        200
    );
    let mine = rows(&host, &phone);
    assert_eq!(mine.len(), 2);
    assert_eq!(live(&mine), vec!["b2b2b2b2b2b2"]);

    // Taking a token back: a device only its own, the administrator any one it names.
    assert_eq!(
        host.ask_as(&tablet, "DELETE", "/devices", Some(&json!({ "token": b }))),
        not_found
    );
    assert_eq!(
        host.ask_as(
            &phone,
            "DELETE",
            "/devices",
            Some(&json!({ "token_prefix": "b2b2b2b2b2b2" }))
        ),
        (400, json!({ "ok": false, "error": "token_required" }))
    );
    assert_eq!(
        host.ask("DELETE", "/devices", Some(&json!({}))),
        (
            400,
            json!({ "ok": false, "error": "one_of_token_token_prefix_device_id" })
        )
    );
    assert_eq!(
        host.ask(
            "DELETE",
            "/devices",
            Some(&json!({ "token_prefix": "b2b2" }))
        ),
        (
            400,
            json!({ "ok": false, "error": "token_prefix_too_short" })
        )
    );
    assert_eq!(
        host.ask(
            "DELETE",
            "/devices",
            Some(&json!({ "token_prefix": "c3c3c3c3c3c3" }))
        ),
        not_found
    );
    assert_eq!(
        host.ask_as(&phone, "DELETE", "/devices", Some(&json!({ "token": b }))),
        (200, json!({ "ok": true, "disabled": 1 }))
    );
    assert_eq!(live(&rows(&host, &phone)), Vec::<String>::new());

    // Registered again, it lives again. The Host is killed and still has it.
    assert_eq!(
        host.ask_as(&phone, "POST", "/devices", Some(&json!({ "token": b })))
            .0,
        200
    );
    host.kill();
    let host = place.start_with_token("the-administrators-token");
    assert_eq!(
        live(&rows(&host, "the-administrators-token")),
        vec!["b2b2b2b2b2b2"]
    );

    // The phone is revoked: its rows stop at once, and are shown as rows nothing goes to.
    host.post("/paired-devices/revoke", json!({ "device_id": phone_id }));
    let after = rows(&host, "the-administrators-token");
    assert_eq!(live(&after), Vec::<String>::new());
    assert!(after.iter().all(|row| row["owner_can_receive"] == false));
    // The administrator, who proves no device, cannot bring the token back to life.
    assert_eq!(
        host.ask("POST", "/devices", Some(&json!({ "token": b }))),
        owned_elsewhere
    );
    // The same phone, paired again, registers its token: it is the new pairing's, and lives.
    let (again, again_id) = pair(&host, "Phone", "full");
    assert_eq!(
        host.ask_as(&again, "POST", "/devices", Some(&json!({ "token": b })))
            .0,
        200
    );
    let mine = rows(&host, &again);
    assert_eq!(live(&mine), vec!["b2b2b2b2b2b2"]);
    assert!(mine.iter().all(|row| row["owner"] == again_id.as_str()));
}

/// What the relay was sent: the path, who it was sent as, and the body.
type Relayed = (String, String, Value);

/// A stand-in for the relay that carries pushes: it takes what a Host hands it, keeps it to
/// be looked at, and answers what the test told it to.
struct StandInRelay {
    address: String,
    taken: Arc<Mutex<Vec<Relayed>>>,
    answer: Arc<Mutex<(u16, Value)>>,
}

impl StandInRelay {
    fn start() -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap().to_string();
        let taken: Arc<Mutex<Vec<Relayed>>> = Arc::new(Mutex::new(Vec::new()));
        let delivered = json!({ "results": [{ "status": 200, "reason": "", "apnsId": "apns-1" }] });
        let answer = Arc::new(Mutex::new((200, delivered)));
        let (keeps, says) = (taken.clone(), answer.clone());
        std::thread::spawn(move || {
            for stream in listener.incoming() {
                let Ok(mut stream) = stream else { break };
                let mut reader = BufReader::new(stream.try_clone().unwrap());
                let mut first = String::new();
                if reader.read_line(&mut first).unwrap_or(0) == 0 {
                    continue;
                }
                let path = first.split_whitespace().nth(1).unwrap_or("").to_string();
                let (mut length, mut chunked, mut sent_as) = (0usize, false, String::new());
                loop {
                    let mut line = String::new();
                    if reader.read_line(&mut line).unwrap_or(0) == 0 || line.trim().is_empty() {
                        break;
                    }
                    let (name, value) = line.split_once(':').unwrap_or((&line, ""));
                    match name.to_ascii_lowercase().as_str() {
                        "content-length" => length = value.trim().parse().unwrap_or(0),
                        "transfer-encoding" => chunked = value.contains("chunked"),
                        "authorization" => sent_as = value.trim().to_string(),
                        _ => {}
                    }
                }
                let mut body = Vec::new();
                if chunked {
                    loop {
                        let mut size = String::new();
                        reader.read_line(&mut size).unwrap();
                        let size = usize::from_str_radix(size.trim(), 16).unwrap_or(0);
                        let mut chunk = vec![0u8; size + 2];
                        reader.read_exact(&mut chunk).unwrap();
                        if size == 0 {
                            break;
                        }
                        body.extend_from_slice(&chunk[..size]);
                    }
                } else {
                    body = vec![0u8; length];
                    reader.read_exact(&mut body).unwrap();
                }
                let body = serde_json::from_slice(&body).unwrap_or(Value::Null);
                keeps.lock().unwrap().push((path, sent_as, body));
                let (status, said) = says.lock().unwrap().clone();
                let said = said.to_string();
                let _ = write!(
                    stream,
                    "HTTP/1.1 {status} Answer\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{said}",
                    said.len()
                );
            }
        });
        Self {
            address,
            taken,
            answer,
        }
    }

    fn taken(&self) -> Vec<Relayed> {
        self.taken.lock().unwrap().clone()
    }

    /// From now on the relay answers this.
    fn answers(&self, status: u16, said: Value) {
        *self.answer.lock().unwrap() = (status, said);
    }
}

#[test]
fn a_finished_turn_and_a_request_are_pushed_to_the_phone_that_registered() {
    let relay = StandInRelay::start();
    let place = Place::new("push");
    let host = place.start_with(
        "the-administrators-token",
        &["--push-relay", &format!("http://{}", relay.address)],
    );
    let (phone, _) = pair(&host, "Phone", "full");
    let token = "a1".repeat(32);
    let grant = format!(
        "pg1.{}.{}.{}",
        "1".repeat(64),
        "2".repeat(16),
        "3".repeat(64)
    );
    let register = |host: &Host| {
        let registered = host.ask_as(
            &phone,
            "POST",
            "/devices",
            Some(&json!({ "token": token, "session": "echo", "push_grant": grant })),
        );
        assert_eq!(registered, (200, json!({ "ok": true })));
    };
    let row = |host: &Host| {
        let (_, answer) = host.ask("GET", "/devices/push-tokens", None);
        answer["tokens"][0].clone()
    };
    register(&host);
    let session = host.new_agent("Echo");
    let server_id = host.get("/server-info")["server_id"].clone();
    // The push of a turn that ended on this, when it is the newest the relay was sent.
    let pushed = |text: &str| {
        relay.taken().last().is_some_and(|(_, _, body)| {
            body["notifications"][0]["payload"]["aps"]["alert"]["body"] == text
        })
    };
    // The turn that ended on this is over, and what the pusher was handed for it is dealt
    // with: the pusher takes one thing after another, and needs no time worth naming.
    let answered = |host: &Host, text: &str| {
        let reply = said("assistant", text);
        wait_for("the reply", || host.conversation(&session).contains(&reply));
        wait_for("the turn to be over", || host.state(&session) == "done");
        std::thread::sleep(Duration::from_millis(300));
    };

    // A turn ends with something for the user: the phone is pushed to, by its own grant.
    host.send(&session, "the build is green", "m1");
    wait_for("the push for the first turn", || {
        pushed("the build is green")
    });
    let taken = relay.taken();
    assert_eq!(taken.len(), 1);
    let (path, sent_as, body) = &taken[0];
    assert_eq!(path, "/v1/push/send");
    assert_eq!(sent_as, &format!("Bearer {grant}"));
    let notification = &body["notifications"][0];
    assert_eq!(
        (&notification["pushType"], &notification["priority"]),
        (&json!("alert"), &json!("10"))
    );
    assert!(
        notification.get("deviceToken").is_none(),
        "a grant names the phone: no token is sent"
    );
    let payload = &notification["payload"];
    assert_eq!(payload["aps"]["alert"]["title"], "Echo");
    assert_eq!(payload["aps"]["thread-id"], session.as_str());
    assert_eq!(payload["aps"]["interruption-level"], "active");
    assert!(
        payload["aps"].get("sound").is_none(),
        "an ordinary reply is quiet"
    );
    assert_eq!(payload["kind"], "user-notification");
    assert_eq!(payload["session"], session.as_str());
    assert_eq!(payload["server_instance_id"], server_id);
    assert_eq!(payload["needs_response"], false);
    let notification_id = payload["notification_id"].as_str().unwrap();
    assert!(notification_id.starts_with("pn-"));
    assert_eq!(notification["collapseId"], notification_id);
    wait_for("the row to say when it was last pushed to", || {
        row(&host)["last_push_at"].is_number()
    });

    // A user who asked at the desk is told in the app, not paged; and while a desktop app
    // says its user sits in front of it, nobody is paged. Neither is pushed: the next push
    // the relay is sent is the one after them.
    host.post(
        "/send",
        json!({ "session": session, "text": "asked at the desk", "client_msg_id": "m2", "client": "desktop" }),
    );
    answered(&host, "asked at the desk");
    let desk = |sequence: u64, active: bool| {
        host.ask(
            "POST",
            "/desktop-presence",
            Some(&json!({
                "instance_id": "8f6f0d6a-3a9a-4f37-9d6b-0c2f8f3b7a11", "sequence": sequence,
                "active": active, "sent_at_ms": now_ms(),
            })),
        )
    };
    assert_eq!(
        desk(1, true),
        (200, json!({ "accepted": true, "lease_ms": 45_000 }))
    );
    host.send(&session, "while at the desk", "m3");
    answered(&host, "while at the desk");
    assert_eq!(
        desk(1, false),
        (200, json!({ "accepted": false, "lease_ms": 45_000 })),
        "an older report changes nothing"
    );
    assert_eq!(desk(2, false).1["accepted"], true);
    assert_eq!(
        host.ask(
            "POST",
            "/desktop-presence",
            Some(&json!({ "instance_id": "no", "sequence": 3, "active": true, "sent_at_ms": now_ms() })),
        ),
        (400, json!({ "error": "instance_id must be a UUID" }))
    );
    host.send(&session, "the desk is left", "m4");
    wait_for("the push after the desk was left", || {
        pushed("the desk is left")
    });
    assert_eq!(relay.taken().len(), 2, "nothing was pushed in between");

    // An agent asks its user something: the phone is told at once, with a sound, and the
    // answer can be typed from the banner.
    let (status, asked) = host.ask(
        "POST",
        "/decisions",
        Some(&json!({
            "session": session, "title": "The code", "question": "Which code did the bank send?",
            "response_type": "text_input",
        })),
    );
    assert_eq!(status, 201, "{asked}");
    let decision_id = asked["artifact"]["decision"]["decision_id"]
        .as_str()
        .unwrap()
        .to_string();
    wait_for("the push for the request", || {
        pushed("Which code did the bank send?")
    });
    let request = relay.taken().last().unwrap().2["notifications"][0].clone();
    assert_eq!(request["collapseId"], format!("decision-{decision_id}"));
    let payload = &request["payload"];
    assert_eq!(payload["aps"]["sound"], "default");
    assert_eq!(payload["aps"]["category"], "input-request");
    assert_eq!(payload["aps"]["alert"]["subtitle"], "The code");
    assert_eq!(
        (
            &payload["reason"],
            &payload["decision_id"],
            &payload["response_type"]
        ),
        (
            &json!("decision"),
            &json!(decision_id),
            &json!("text_input")
        )
    );
    // While the request is open, a reply that ends a turn makes a sound too.
    host.send(&session, "still waiting for the code", "m5");
    wait_for("the push for a reply while a request is open", || {
        pushed("still waiting for the code")
    });
    let payload = relay.taken().last().unwrap().2["notifications"][0]["payload"].clone();
    assert_eq!(
        (&payload["needs_response"], &payload["aps"]["sound"]),
        (&json!(true), &json!("default"))
    );

    // The push service says the token is gone: nothing is sent to it again.
    relay.answers(
        200,
        json!({ "results": [{ "status": 410, "reason": "Unregistered" }] }),
    );
    host.send(&session, "to a phone that is gone", "m6");
    wait_for("the row to be stopped", || {
        !row(&host)["disabled_at"].is_null()
    });
    let sent_so_far = relay.taken().len();
    host.send(&session, "to nobody", "m7");
    answered(&host, "to nobody");

    // The phone registers again and lives again. Then it takes its grant back: this Host
    // has no other way to it, and stops.
    register(&host);
    assert!(row(&host)["disabled_at"].is_null());
    relay.answers(401, json!({ "error": { "code": "grant_revoked" } }));
    host.send(&session, "to a phone that took its grant back", "m8");
    wait_for("the row to be stopped again", || {
        !row(&host)["disabled_at"].is_null()
    });
    assert_eq!(
        relay.taken().len(),
        sent_so_far + 1,
        "one more push was tried, and none for the turn in between"
    );
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

#[test]
fn an_agent_moves_from_one_host_to_another_with_its_conversation() {
    // Two Hosts. The agent lives on the first and has said something there.
    let (old_place, new_place) = (Place::new("move-from"), Place::new("move-to"));
    let (old_host, new_host) = (old_place.start(), new_place.start());
    let made = old_host.post(
        "/agents",
        json!({ "name": "Mover", "backend": "scripted", "cwd": old_place.root.join("work").to_str().unwrap() }),
    );
    let (session, agent_id) = (
        made["session"].as_str().unwrap().to_string(),
        made["agent_id"].as_str().unwrap().to_string(),
    );
    old_host.send(&session, "what we spoke of", "old-1");
    wait_for("the first Host's answer", || {
        old_host.conversation(&session).len() == 2 && old_host.state(&session) == "done"
    });
    let before = old_host.conversation(&session);

    // The move, as its owner would run it.
    let moved = Command::new(env!("CARGO_BIN_EXE_clarp-host"))
        .args(["import", "--from", &old_host.address, "--session", &session])
        .args(["--host", &new_host.address])
        .env_remove("CLARP_HOST_TOKEN")
        .env_remove("CLARP_SOURCE_TOKEN")
        .output()
        .unwrap();
    assert!(
        moved.status.success(),
        "{}",
        String::from_utf8_lossy(&moved.stderr)
    );
    assert!(String::from_utf8_lossy(&moved.stdout).contains("with the 2 rows"));

    // On the second Host it is the same agent, with the conversation it had, row for row.
    let snapshot = new_host.get("/agents/snapshot");
    assert_eq!(snapshot["agents"][0]["agent_id"], agent_id);
    assert_eq!(snapshot["agents"][0]["session"], session);
    assert_eq!(new_host.conversation(&session), before);
    let came = new_host.turns(&session);
    let was = old_host.turns(&session);
    assert_eq!(
        came.iter().map(|row| &row["id"]).collect::<Vec<_>>(),
        was.iter().map(|row| &row["id"]).collect::<Vec<_>>(),
        "the rows keep the ids the apps know them by"
    );
    // And it goes on there.
    new_host.send(&session, "and now?", "new-1");
    wait_for("the second Host's answer", || {
        new_host.conversation(&session).len() == 4 && new_host.state(&session) == "done"
    });
    assert_eq!(
        new_host.conversation(&session)[3],
        said("assistant", "and now?")
    );
    // The first Host was read, not changed; and an agent arrives once.
    assert_eq!(old_host.conversation(&session), before);
    let again = Command::new(env!("CARGO_BIN_EXE_clarp-host"))
        .args(["import", "--from", &old_host.address, "--session", &session])
        .args(["--host", &new_host.address])
        .env_remove("CLARP_HOST_TOKEN")
        .output()
        .unwrap();
    assert_eq!(again.status.code(), Some(2));
    assert!(String::from_utf8_lossy(&again.stderr).contains("already has the agent"));
    // After a restart of the second Host it is all still there.
    new_host.kill();
    let new_host = new_place.start();
    assert_eq!(new_host.conversation(&session).len(), 4);
}
