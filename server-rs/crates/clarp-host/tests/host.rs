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

    // A tool this Host does not have yet says so, and does nothing.
    let absent = tool(&host, &dir, None, &["clarp-goal", "get", "some-plan"]);
    assert_eq!(absent.0, 69);
    assert!(
        absent.2.contains("not available on this Host yet"),
        "{}",
        absent.2
    );
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
