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
        self.start_in(token, more, &[])
    }

    /// Starts the Host with more on its command line and more in its environment.
    fn start_in(&self, token: &str, more: &[&str], world: &[(&str, &str)]) -> Host {
        let mut process = Command::new(env!("CARGO_BIN_EXE_clarp-host"))
            .args(["--data", self.root.join("data").to_str().unwrap()])
            .args(["--run", self.root.join("run").to_str().unwrap()])
            .args(["--cwd", self.root.join("work").to_str().unwrap()])
            .args(["--listen", "127.0.0.1:0"])
            .args(more)
            .envs(world.iter().copied())
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
    // Without a label of its own, what it left running says what goes on: the job that
    // was started again is still there, and the agent is drawn as working, not as idle.
    wait_for("the label to go", || row(&host)["status_text"] == "Watch");
    assert_eq!(row(&host)["latest_state"], "background");
    assert_eq!(
        tool(
            &host,
            &dir,
            None,
            &["clarp-agent-bg", &session, "job-finish", "bg1:2:watch-1"]
        )
        .0,
        0
    );
    wait_for("the agent to rest", || row(&host)["status_text"].is_null());
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
    // A form that takes answers is not here yet, and its verbs say so.
    assert_eq!(
        artifacts(&["create-form", &session, "Form", "form.html", "schema.json"]).0,
        69
    );
    assert_eq!(artifacts(&["form-events", "no-such"]).0, 69);

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

#[test]
fn only_a_person_moves_the_focus_and_a_named_sender_is_who_a_message_is_from() {
    let place = Place::new("focus");
    let host = place.start();
    let (first, second) = (host.new_agent("First"), host.new_agent("Second"));
    let focus = || host.get("/agents/snapshot")["focus"].clone();
    let id_of = |session: &str| -> Value {
        host.get("/agents/snapshot")["agents"]
            .as_array()
            .unwrap()
            .iter()
            .find(|agent| agent["session"] == session)
            .unwrap()["agent_id"]
            .clone()
    };
    let (first_id, second_id) = (id_of(&first), id_of(&second));
    // Sends to the second agent and waits for its answer; gives back the message's row.
    let send = |more: Value, id: &str| -> Value {
        let mut body =
            json!({ "session": second, "text": format!("word {id}"), "client_msg_id": id });
        for (key, value) in more.as_object().unwrap() {
            body[key] = value.clone();
        }
        let sent = host.post("/send", body);
        assert_eq!(sent["ok"], true, "{sent}");
        let mine = format!("u-{id}");
        let mut row = Value::Null;
        wait_for("the answer", || {
            let rows = host.turns(&second);
            let Some(at) = rows.iter().position(|row| row["id"] == mine) else {
                return false;
            };
            row = rows[at].clone();
            rows[at + 1..].iter().any(|row| row["role"] == "assistant")
                && host.state(&second) == "done"
        });
        row
    };
    let open_first = || {
        let (status, _) = host.ask("POST", "/select", Some(&json!({ "session": first })));
        assert_eq!(status, 200);
        assert_eq!(focus(), first_id);
    };

    // The user has the first agent open. A message that names its sender is that agent's,
    // also when it says it is the user's or the Oracle's: the focus stays.
    open_first();
    for (claimed, id) in [("user", "claimed-user"), ("oracle", "claimed-oracle")] {
        let row = send(json!({ "origin": claimed, "sender": first }), id);
        assert_eq!(
            (&row["origin"], &row["sender_session"]),
            (&json!("agent"), &json!(first)),
            "{claimed}"
        );
        assert_eq!(focus(), first_id, "{claimed}");
    }
    // What runs by itself does not move it either.
    let row = send(json!({ "origin": "schedule" }), "by-itself");
    assert_eq!(row["origin"], "schedule");
    assert_eq!(focus(), first_id);
    // The Oracle acts on what the user said: a person. The agent it writes to is the open one.
    let row = send(json!({ "origin": "oracle" }), "oracle");
    assert_eq!(row["origin"], "oracle");
    assert_eq!(focus(), second_id);
    // And so does the user.
    open_first();
    let row = send(json!({}), "user");
    assert_eq!(row["origin"], "user");
    assert_eq!(focus(), second_id);
}

#[test]
fn an_agent_writes_to_another_agent_with_its_own_tool() {
    let place = Place::new("message");
    let host = place.start();
    let (asker, worker) = (host.new_agent("Asker"), host.new_agent("Worker"));
    let dir = place.root.clone();
    let events = host.listen();
    let focus_before = host.get("/agents/snapshot")["focus"].clone();
    let admin = |args: &[&str]| {
        let mut all = vec!["clarp-admin"];
        all.extend_from_slice(args);
        tool(&host, &dir, None, &all)
    };
    let over = |text: &str| {
        let reply = said("assistant", text);
        wait_for("the answer", || host.conversation(&worker).contains(&reply));
        wait_for("the turn to be over", || host.state(&worker) == "done");
    };

    // One agent hands another something, as the skill tells it to.
    let (code, out, err) = admin(&[
        "prompt",
        "--to",
        &worker,
        "--from",
        &asker,
        "--text",
        "please look at the build",
    ]);
    assert_eq!(code, 0, "{err}");
    let sent: Value = serde_json::from_str(&out).unwrap();
    assert_eq!(
        (&sent["ok"], &sent["session"], &sent["queued"]),
        (&json!(true), &json!(worker), &json!(false))
    );
    over("please look at the build");
    let rows = host.turns(&worker);
    assert_eq!(
        (
            &rows[0]["origin"],
            &rows[0]["sender_session"],
            &rows[0]["sender_name"]
        ),
        (&json!("agent"), &json!(asker), &json!("Asker"))
    );
    assert_eq!(rows[1]["origin"], "agent");
    // What agents say to each other tells the user nothing, and the agent the user has
    // open stays the one it was.
    assert!(
        !events
            .lock()
            .unwrap()
            .iter()
            .any(|event| event["type"] == "user-notification")
    );
    assert_eq!(host.get("/agents/snapshot")["focus"], focus_before);

    // Something that runs by itself writes without a sender.
    let (code, _, err) = admin(&[
        "prompt",
        "--to",
        &worker,
        "--origin",
        "watcher",
        "--text",
        "the queue is long",
    ]);
    assert_eq!(code, 0, "{err}");
    over("the queue is long");
    let rows = host.turns(&worker);
    assert_eq!(
        (&rows[2]["origin"], &rows[2]["text"], &rows[2]["automated"]),
        (
            &json!("watcher"),
            &json!("Automated watcher event"),
            &json!(true)
        )
    );

    // What the tool refuses, it refuses as the Python tool does.
    let nobody = admin(&["prompt", "--to", "nobody", "--text", "anyone there"]);
    assert_eq!(
        (nobody.0, nobody.2.as_str()),
        (
            4,
            "clarp-admin prompt: the Host refused it (HTTP 404): unknown forced agent\n"
        )
    );
    assert_eq!(admin(&["prompt", "--to", &worker]).0, 2);
    let both = admin(&[
        "prompt", "--to", &worker, "--from", &asker, "--origin", "watcher", "--text", "x",
    ]);
    assert_eq!(
        (both.0, both.2.as_str()),
        (1, "--origin cannot be combined with --from\n")
    );
    // What is not on this Host says so, and does nothing.
    assert_eq!(admin(&["update"]).0, 69);
    assert_eq!(
        admin(&["prompt", "--to", &worker, "--text", "x", "--delay", "5m"]).0,
        69
    );
    let stranger = admin(&["prompt", "--to", &worker, "--text", "x", "--server", "work"]);
    assert_eq!(
        (stranger.0, stranger.2.as_str()),
        (1, "clarp-admin prompt: unknown or disabled peer: work\n")
    );
    assert_eq!(host.turns(&worker).len(), 4, "nothing else was sent");

    // Another Host, which the owner names: an agent here writes to an agent there.
    let elsewhere = Place::new("message-elsewhere");
    let other = elsewhere.start_with_token("the-other-hosts-token");
    let remote = other.new_agent("Remote");
    assert_eq!(
        host.ask(
            "POST",
            "/peers",
            Some(&json!({ "name": "work", "url": "nowhere", "token": "t" }))
        ),
        (
            400,
            json!({ "error": "peer requires name, HTTP(S) URL, and token" })
        )
    );
    let named = host.post(
        "/peers",
        json!({
            "name": "work", "url": format!("http://{}/", other.address),
            "token": "the-other-hosts-token",
        }),
    );
    assert_eq!(
        named,
        json!({ "name": "work", "url": format!("http://{}", other.address), "enabled": true })
    );
    assert_eq!(host.get("/peers"), json!([named]), "never its token");
    // The owner's own command for it reads the same.
    let listed = Command::new(env!("CARGO_BIN_EXE_clarp-host"))
        .args(["peer", "list", "--host", &host.address])
        .output()
        .unwrap();
    assert_eq!(
        String::from_utf8_lossy(&listed.stdout).trim(),
        format!("work http://{}", other.address)
    );
    let unnamed = Command::new(env!("CARGO_BIN_EXE_clarp-host"))
        .args([
            "peer",
            "add",
            "home",
            "http://127.0.0.1:1",
            "--host",
            &host.address,
        ])
        .env_remove("CLARP_PEER_TOKEN")
        .output()
        .unwrap();
    assert_eq!(unnamed.status.code(), Some(2), "a peer needs its token");
    let (code, out, err) = admin(&[
        "prompt",
        "--server",
        "work",
        "--to",
        &remote,
        "--from",
        &asker,
        "--text",
        "greetings from the other host",
    ]);
    assert_eq!(code, 0, "{err}");
    let sent: Value = serde_json::from_str(&out).unwrap();
    assert_eq!(sent["session"], remote.as_str());
    let reply = said("assistant", "greetings from the other host");
    wait_for("the answer on the other Host", || {
        other.conversation(&remote).contains(&reply)
    });
    let rows = other.turns(&remote);
    assert_eq!(
        (&rows[0]["origin"], &rows[0]["sender_session"]),
        (&json!("agent"), &json!(asker)),
        "it says who wrote, by the name the writer has on its own Host"
    );
    // What the other Host refuses is said as its own refusal.
    let lost = admin(&[
        "prompt", "--server", "work", "--to", "nobody", "--text", "x",
    ]);
    assert_eq!(
        (lost.0, lost.2.as_str()),
        (
            4,
            "clarp-admin prompt: the Host refused it (HTTP 404): unknown forced agent\n"
        )
    );
    // A Host that is forgotten is no longer written to.
    assert_eq!(
        host.ask("DELETE", "/peers/work", None),
        (200, json!({ "ok": true, "removed": "work" }))
    );
    assert_eq!(host.ask("DELETE", "/peers/work", None).0, 404);
    assert_eq!(
        admin(&["prompt", "--server", "work", "--to", &remote, "--text", "x"]).0,
        1
    );
}

#[test]
fn an_agent_asks_another_for_something_that_comes_back_to_its_goal() {
    let place = Place::new("request");
    let host = place.start();
    let (asker, worker) = (host.new_agent("Asker"), host.new_agent("Worker"));
    let dir = place.root.clone();
    let admin = |args: &[&str]| {
        let mut all = vec!["clarp-admin"];
        all.extend_from_slice(args);
        tool(&host, &dir, None, &all)
    };
    let told = |session: &str, begins: &str| {
        host.conversation(session)
            .iter()
            .filter(|(role, text)| role == "user" && text.starts_with(begins))
            .count()
    };

    // The asker has a goal that is being worked, bound to the conversation it has.
    host.send(&asker, r#"{"session": "s1"}"#, "bind");
    wait_for("the first turn to end", || host.state(&asker) == "done");
    let commitment = json!({
        "outcome": "The log is live", "criteria": ["It runs"], "limits": "None", "enroll": true,
    })
    .to_string();
    let plan = goal(
        &host,
        &dir,
        &["create", &asker, "log", "Get the log", "[]", &commitment],
    );
    let plan_id = plan["plan_id"].as_str().unwrap().to_string();

    // What cannot be done is refused before anything is sent.
    let refused = |args: &[&str], why: &str| {
        let said = admin(args);
        assert_eq!(
            (said.0, said.2.as_str()),
            (
                4,
                format!("clarp-admin request: {why}; nothing was sent\n").as_str()
            )
        );
    };
    refused(
        &[
            "request",
            "--to",
            &worker,
            "--from",
            &asker,
            "--text",
            "x",
            "--deadline",
            "60",
        ],
        "--deadline needs --goal: without a goal nothing waits",
    );
    refused(
        &["request", "--to", "nobody", "--from", &asker, "--text", "x"],
        "no such recipient session: nobody",
    );
    refused(
        &[
            "request", "--to", &asker, "--from", &worker, "--goal", &plan_id, "--text", "x",
        ],
        &format!("goal {plan_id} belongs to {asker}, not {worker}"),
    );
    refused(
        &[
            "request",
            "--to",
            &worker,
            "--from",
            &asker,
            "--goal",
            &plan_id,
            "--deadline",
            "0",
            "--text",
            "x",
        ],
        "deadline must be 1 to 2592000 seconds",
    );
    assert_eq!(told(&worker, "[Request "), 0);

    // The request: the worker is asked and told how to answer, and the asker's goal waits
    // for the answer, with a deadline.
    let (code, out, err) = admin(&[
        "request",
        "--to",
        &worker,
        "--from",
        &asker,
        "--goal",
        &plan_id,
        "--deadline",
        "3600",
        "--text",
        "please add an event log",
    ]);
    assert_eq!(code, 0, "{err}");
    let sent: Value = serde_json::from_str(&out).unwrap();
    let request_id = sent["request_id"].as_str().unwrap().to_string();
    assert_eq!(
        (&sent["to"], &sent["goal"]),
        (&json!(worker), &json!(plan_id))
    );
    let asked =
        format!("[Request {request_id} from Asker ({asker}). Your answers in your own chat");
    wait_for("the worker to be asked", || told(&worker, &asked) == 1);
    let waiting = goal(&host, &dir, &["get", &plan_id]);
    let wake = &waiting["goal"]["continuation"];
    assert_eq!(
        (&wake["state"], &wake["dependency_key"]),
        (&json!("waiting"), &json!(format!("peer:{request_id}")))
    );
    assert!(
        waiting["goal"]["checkpoint"]["next_work"]
            .as_str()
            .unwrap()
            .contains(&format!("When woken by request {request_id} to Worker"))
    );

    // Only the agent that was asked answers a request.
    let stranger = admin(&[
        "reply",
        "--request",
        &request_id,
        "--from",
        &asker,
        "--kind",
        "result",
        "--text",
        "x",
    ]);
    assert_eq!(
        (stranger.0, stranger.2.as_str()),
        (
            4,
            format!(
                "clarp-admin reply: request {request_id} was not sent to {asker}; nothing was delivered\n"
            )
            .as_str()
        )
    );

    // An update is a message to the asker. The goal keeps waiting, and the same update
    // sent again is the same message.
    let update = [
        "reply",
        "--request",
        &request_id,
        "--from",
        &worker,
        "--kind",
        "progress",
        "--text",
        "half done",
    ];
    let (code, out, err) = admin(&update);
    assert_eq!(code, 0, "{err}");
    let said: Value = serde_json::from_str(&out).unwrap();
    assert_eq!(
        (&said["via"], &said["to"], &said["kind"]),
        (&json!("message"), &json!(asker), &json!("progress"))
    );
    let progress = format!(
        "[Reply to request {request_id} from Worker ({worker}): progress. The request is still open"
    );
    wait_for("the asker to hear of the progress", || {
        told(&asker, &progress) == 1
    });
    assert_eq!(admin(&update).0, 0);
    wait_for("the asker's turn to be over", || {
        host.state(&asker) == "done"
    });
    assert_eq!(told(&asker, &progress), 1);
    assert_eq!(
        goal(&host, &dir, &["get", &plan_id])["goal"]["continuation"]["state"],
        "waiting"
    );

    // The result is what the goal waited for: the goal takes it, and its owner is woken
    // with it.
    let (code, out, err) = admin(&[
        "reply",
        "--request",
        &request_id,
        "--from",
        &worker,
        "--kind",
        "result",
        "--text",
        "Live: the log is at the new route.",
    ]);
    assert_eq!(code, 0, "{err}");
    let said: Value = serde_json::from_str(&out).unwrap();
    assert_eq!(said["via"], format!("goal {plan_id}"));
    wait_for("the asker to be woken with the answer", || {
        host.conversation(&asker).iter().any(|(role, text)| {
            role == "user"
                && text.starts_with("Continue your durable outcome commitment.")
                && text.contains("Live: the log is at the new route.")
        })
    });
    // The same result again is known to be there already.
    let (code, out, _) = admin(&[
        "reply",
        "--request",
        &request_id,
        "--from",
        &worker,
        "--kind",
        "result",
        "--text",
        "Live: the log is at the new route.",
    ]);
    let said: Value = serde_json::from_str(&out).unwrap();
    assert_eq!(code, 0);
    assert_eq!(said["via"], format!("goal {plan_id} (already recorded)"));

    // Without a goal a request is a message, and so is its answer.
    let (code, out, err) = admin(&[
        "request",
        "--to",
        &worker,
        "--from",
        &asker,
        "--text",
        "and the docs?",
    ]);
    assert_eq!(code, 0);
    assert!(err.contains("no goal waits on it"), "{err}");
    let second = serde_json::from_str::<Value>(&out).unwrap()["request_id"]
        .as_str()
        .unwrap()
        .to_string();
    assert!(sent["goal"].is_string() && second != request_id);
    wait_for("the worker to be asked again", || {
        told(&worker, &format!("[Request {second} from Asker")) == 1
    });
    let (code, out, err) = admin(&[
        "reply",
        "--request",
        &second,
        "--from",
        &worker,
        "--kind",
        "result",
        "--text",
        "The docs are written.",
    ]);
    assert_eq!(code, 0, "{err}");
    assert_eq!(
        serde_json::from_str::<Value>(&out).unwrap()["via"],
        "message"
    );
    wait_for("the asker to be told the second answer", || {
        told(
            &asker,
            &format!("[Reply to request {second} from Worker ({worker}): result.]"),
        ) == 1
    });
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
        made["agent"]["agent_id"].as_str().unwrap().to_string(),
    );
    old_host.send(&session, "what we spoke of", "old-1");
    wait_for("the first Host's answer", || {
        old_host.conversation(&session).len() == 2 && old_host.state(&session) == "done"
    });
    // It published an image and a document there, and its conversation names both, and a
    // file that is no longer anywhere.
    let png: Vec<u8> = [
        &b"\x89PNG\r\n\x1a\n"[..],
        &13u32.to_be_bytes(),
        b"IHDR",
        &8u32.to_be_bytes(),
        &6u32.to_be_bytes(),
        &[8, 6, 0, 0, 0],
    ]
    .concat();
    let pdf = b"%PDF-1.7\n1 0 obj\n".to_vec();
    let publish = |bytes: &[u8], name: &str, kind: &str, caption: &str| -> Value {
        let said = [
            ("X-Session", session.as_str()),
            ("X-File-Name", name),
            ("X-Caption", caption),
            ("Content-Type", kind),
        ];
        let answer = fetch(&old_host, "", "POST", "/media", &said, bytes);
        assert_eq!(answer.status, 200, "{}", answer.text());
        serde_json::from_slice(&answer.body).unwrap()
    };
    let image = publish(&png, "chart.png", "image/png", "A%20chart%20%5Bone%5D");
    let paper = publish(&pdf, "report.pdf", "application/pdf", "");
    let (image_id, paper_id) = (
        image["asset_id"].as_str().unwrap().to_string(),
        paper["asset_id"].as_str().unwrap().to_string(),
    );
    let naming = format!(
        "see {} and the paper at {} and /media/asset_gone_long_ago",
        image["markdown"].as_str().unwrap(),
        paper["url"].as_str().unwrap()
    );
    old_host.send(&session, &naming, "old-2");
    wait_for("the first Host's second answer", || {
        old_host.conversation(&session).len() == 4 && old_host.state(&session) == "done"
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
    let told = String::from_utf8_lossy(&moved.stdout).into_owned();
    assert!(
        told.contains("with the 4 rows of its conversation and 2 files it published (1 more"),
        "{told}"
    );

    // What it published came with it, under the ids its conversation names: the image in
    // its gallery as it was, the bytes of both as they were.
    let gallery = new_host.get(&format!("/media?session={session}"));
    let shown = gallery["assets"].as_array().unwrap();
    assert_eq!(shown.len(), 1, "{gallery}");
    for field in [
        "asset_id",
        "source_name",
        "caption",
        "created_at",
        "width",
        "height",
        "markdown",
    ] {
        assert_eq!(shown[0][field], image[field], "{field}");
    }
    assert_eq!(shown[0]["caption"], "A chart [one]");
    let came = fetch(
        &new_host,
        "",
        "GET",
        &format!("/media/{image_id}"),
        &[],
        b"",
    );
    assert_eq!(
        (came.status, came.header("content-type")),
        (200, "image/png")
    );
    assert!(came.body == png);
    let came = fetch(
        &new_host,
        "",
        "GET",
        &format!("/media/{paper_id}"),
        &[],
        b"",
    );
    assert_eq!(
        (came.status, came.header("content-type")),
        (200, "application/pdf")
    );
    assert!(came.body == pdf);
    // Asked for again, the files are the same files: nothing arrives twice.
    let again = Command::new(env!("CARGO_BIN_EXE_clarp-host"))
        .args(["import", "--files", "--from", &old_host.address])
        .args(["--session", &session, "--host", &new_host.address])
        .env_remove("CLARP_HOST_TOKEN")
        .env_remove("CLARP_SOURCE_TOKEN")
        .output()
        .unwrap();
    let told = String::from_utf8_lossy(&again.stdout).into_owned();
    assert!(
        again.status.success() && told.contains("has 2 files it published"),
        "{told}"
    );
    let gallery = new_host.get(&format!("/media?session={session}"));
    assert_eq!(gallery["assets"].as_array().unwrap().len(), 1);
    assert_eq!(files_under(&new_place.root.join("data").join("media")), 2);
    // Only the administrator says which id a file has, and an id is one file's.
    let taken = [
        ("X-Session", session.as_str()),
        ("X-Asset-Id", image_id.as_str()),
        ("Content-Type", "application/pdf"),
    ];
    let other = fetch(&new_host, "", "POST", "/media", &taken, &pdf);
    assert_eq!(other.status, 409, "{}", other.text());

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
        new_host.conversation(&session).len() == 6 && new_host.state(&session) == "done"
    });
    assert_eq!(
        new_host.conversation(&session)[5],
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
    assert_eq!(new_host.conversation(&session).len(), 6);
    let came = fetch(
        &new_host,
        "",
        "GET",
        &format!("/media/{image_id}"),
        &[],
        b"",
    );
    assert!(came.status == 200 && came.body == png);
}

#[test]
fn the_user_changes_who_an_agent_is_and_the_host_keeps_it() {
    let place = Place::new("roster");
    let host = place.start();
    let heard = host.listen();
    let ada = host.new_agent("Ada");
    let bob = host.new_agent("Bob");
    let row = |host: &Host, session: &str| -> Value {
        host.get("/agents/snapshot")["agents"]
            .as_array()
            .unwrap()
            .iter()
            .find(|agent| agent["session"] == session)
            .cloned()
            .unwrap_or_else(|| panic!("{session} is not in the snapshot"))
    };
    let refused = |path: &str, body: Value| -> (u16, Value) { host.ask("POST", path, Some(&body)) };
    let why = |path: &str, body: Value| -> (u16, String) {
        let (status, answer) = refused(path, body);
        (status, answer["error"].as_str().unwrap_or("").to_string())
    };

    // One agent has a name at a time, whatever the case of its letters.
    let (status, taken) = refused("/agents", json!({ "name": " ada ", "backend": "scripted" }));
    assert_eq!(status, 409);
    assert_eq!(
        taken,
        json!({
            "error": "contact_occupied",
            "message": "Ada already has an active session. Open or release that chat before starting another.",
            "owner": "Ada",
            "session": "ada",
        })
    );

    // A name.
    let (status, taken) = refused("/agent-rename", json!({ "session": bob, "name": "ADA" }));
    assert_eq!(status, 409);
    assert_eq!(
        taken,
        json!({
            "error": "contact_occupied",
            "message": "Ada already has an active session.",
            "owner": "Ada",
            "session": "ada",
        })
    );
    let name_refused = "name required (maximum 60 characters)".to_string();
    assert_eq!(
        why("/agent-rename", json!({ "name": "X" })),
        (400, "session required".to_string())
    );
    assert_eq!(
        why("/agent-rename", json!({ "session": bob, "name": "   " })),
        (400, name_refused.clone())
    );
    assert_eq!(
        why(
            "/agent-rename",
            json!({ "session": bob, "name": "n".repeat(61) })
        ),
        (400, name_refused)
    );
    assert_eq!(
        why("/agent-rename", json!({ "session": "nobody", "name": "X" })),
        (404, "no such agent".to_string())
    );
    assert_eq!(
        why("/agent-rename", json!([1])),
        (400, "bad json".to_string())
    );
    assert_eq!(
        host.post(
            "/agent-rename",
            json!({ "session": bob, "name": "  Robert   the  Second " })
        ),
        json!({ "ok": true, "session": "bob", "name": "Robert the Second" })
    );
    assert_eq!(row(&host, &bob)["persona"], "Robert the Second");
    wait_for("the apps to hear of the new name", || {
        heard.lock().unwrap().iter().any(|event| {
            event["type"] == "agent-roster"
                && event["kind"] == "agent-renamed"
                && event["session"] == "bob"
                && event["name"] == "Robert the Second"
        })
    });
    // Its own name in other letters is its own to take, and an agent is also named by its id.
    host.post(
        "/agent-rename",
        json!({ "session": bob, "name": "robert the second" }),
    );
    let bob_id = row(&host, &bob)["agent_id"].as_str().unwrap().to_string();
    assert_eq!(
        host.post("/agent-rename", json!({ "session": bob_id, "name": "Bob" })),
        json!({ "ok": true, "session": bob_id, "name": "Bob" })
    );
    assert_eq!(row(&host, &bob)["persona"], "Bob");
    // The name that was given up is free again.
    let rob = host.post(
        "/agents",
        json!({ "name": "Robert the Second", "backend": "scripted" }),
    );
    assert_eq!(rob["session"], "robert-the-second");

    // Whether it pages.
    assert_eq!(
        why("/agent-mute", json!({ "session": ada })),
        (400, "muted required".to_string())
    );
    assert_eq!(
        why("/agent-mute", json!({ "muted": true })),
        (400, "session required".to_string())
    );
    assert_eq!(
        why("/agent-mute", json!({ "session": "nobody", "muted": true })),
        (404, "no such agent".to_string())
    );
    assert_eq!(row(&host, &ada)["muted"], false);
    assert_eq!(
        host.post("/agent-mute", json!({ "session": ada, "muted": true })),
        json!({ "ok": true, "session": "ada", "muted": true })
    );
    assert_eq!(row(&host, &ada)["muted"], true);
    host.send(&ada, "anything new?", "m1");
    let told = |id: &str| {
        heard
            .lock()
            .unwrap()
            .iter()
            .find(|event| event["type"] == "user-notification" && event["cause_message_id"] == id)
            .cloned()
    };
    wait_for("the muted agent's turn to be told", || {
        told("u-m1").is_some()
    });
    let quiet = told("u-m1").unwrap();
    assert_eq!(
        (&quiet["push"], &quiet["muted"], &quiet["unread"]),
        (&json!(false), &json!(true), &json!(true)),
        "marked unread, and nobody is paged: {quiet}"
    );
    assert!(quiet["reason"].as_str().unwrap().ends_with("-muted"));
    // Muted is what is plainly true.
    assert_eq!(
        host.post("/agent-mute", json!({ "session": ada, "muted": "yes" }))["muted"],
        false
    );
    host.send(&ada, "and now?", "m2");
    wait_for("the agent's turn to be told", || told("u-m2").is_some());
    let loud = told("u-m2").unwrap();
    assert_eq!(
        (&loud["push"], &loud["muted"]),
        (&json!(true), &json!(false))
    );

    // Put away, and taken out again.
    let both = "session and archived required".to_string();
    assert_eq!(
        why(
            "/agent-archive",
            json!({ "session": ada, "archived": "yes" })
        ),
        (400, both.clone())
    );
    assert_eq!(
        why("/agent-archive", json!({ "archived": true })),
        (400, both)
    );
    assert_eq!(
        why(
            "/agent-archive",
            json!({ "session": "nobody", "archived": true })
        ),
        (404, "no such agent".to_string())
    );
    assert_eq!(row(&host, &ada)["archived_at"], Value::Null);
    assert_eq!(
        host.post(
            "/agent-archive",
            json!({ "session": ada, "archived": true })
        ),
        json!({ "ok": true, "session": "ada", "archived": true })
    );
    let put_away_at = row(&host, &ada)["archived_at"].as_i64().unwrap();
    assert!(put_away_at > 1_600_000_000_000);
    let sessions = |snapshot: &Value| -> Vec<String> {
        snapshot["agents"]
            .as_array()
            .unwrap()
            .iter()
            .map(|agent| agent["session"].as_str().unwrap().to_string())
            .collect()
    };
    let without = host.get("/agents/snapshot?include_archived=0");
    assert_eq!(sessions(&without), ["bob", "robert-the-second"]);
    assert_eq!(without["archived_count"], 1);
    assert_eq!(without["archived_parents"], json!([]));
    assert_eq!(
        without["roster"],
        json!(["Ada", "Bob", "Robert the Second"]),
        "the names are every agent's"
    );
    let whole = host.get("/agents/snapshot");
    assert_eq!(sessions(&whole), ["ada", "bob", "robert-the-second"]);
    assert_eq!(whole.get("archived_count"), None);
    // Any other word for it is the whole list, as it has always been.
    assert_eq!(
        sessions(&host.get("/agents/snapshot?include_archived=false")).len(),
        3
    );
    let page = host.get("/agents/archived");
    assert_eq!(sessions(&page), ["ada"]);
    assert_eq!(
        (&page["total"], &page["next_cursor"]),
        (&json!(1), &Value::Null)
    );
    assert_eq!(
        page["agents"][0],
        row(&host, &ada),
        "the row the snapshot has"
    );
    for query in ["limit=0", "limit=201", "limit=07", "limit=x", "cursor=zzz"] {
        let (status, answer) = host.ask("GET", &format!("/agents/archived?{query}"), None);
        assert_eq!(
            (status, answer["error"].as_str()),
            (400, Some("invalid archived agents query")),
            "{query}"
        );
    }
    // An agent that is put away can still be written to, and stays put away.
    host.send(&ada, "still there?", "m3");
    wait_for("the answer of the agent that is put away", || {
        host.conversation(&ada).len() == 6 && host.state(&ada) == "done"
    });
    assert_eq!(row(&host, &ada)["archived_at"], put_away_at);
    // The list is read a page at a time, the last put away first.
    host.post(
        "/agent-archive",
        json!({ "session": bob, "archived": true }),
    );
    let first = host.get("/agents/archived?limit=1");
    assert_eq!(sessions(&first), ["bob"]);
    assert_eq!(first["total"], 2);
    let cursor = first["next_cursor"].as_str().unwrap().to_string();
    let second = host.get(&format!("/agents/archived?limit=1&cursor={cursor}"));
    assert_eq!(sessions(&second), ["ada"]);
    assert_eq!(
        (&second["total"], &second["next_cursor"]),
        (&json!(2), &Value::Null)
    );
    assert_eq!(
        host.post(
            "/agent-archive",
            json!({ "session": bob, "archived": false })
        ),
        json!({ "ok": true, "session": "bob", "archived": false })
    );
    assert_eq!(row(&host, &bob)["archived_at"], Value::Null);
    assert_eq!(host.get("/agents/archived")["total"], 1);

    // Which model, and how much effort.
    let real = host.post("/agents", json!({ "name": "Real", "backend": "claude" }));
    let real = real["session"].as_str().unwrap().to_string();
    let claude_efforts = json!(["low", "medium", "high", "xhigh", "max"]);
    assert_eq!(
        host.post(
            "/agent-llm",
            json!({ "session": real, "model": " claude-opus-5-5 ", "effort": "HIGH" })
        ),
        json!({
            "ok": true,
            "backend": "claude",
            "model": "claude-opus-5-5",
            "effort": "high",
            "valid_efforts": claude_efforts,
        })
    );
    let tuned = row(&host, &real);
    assert_eq!(
        (&tuned["model"], &tuned["effort"]),
        (&json!("claude-opus-5-5"), &json!("high"))
    );
    // Only what a request has is changed, and saying nothing for one is the tool's choice.
    let eased = host.post("/agent-llm", json!({ "session": real, "effort": null }));
    assert_eq!(
        (&eased["model"], &eased["effort"]),
        (&json!("claude-opus-5-5"), &json!(""))
    );
    let asked = host.post("/agent-llm", json!({ "session": real }));
    assert_eq!(
        (&asked["model"], &asked["effort"]),
        (&json!("claude-opus-5-5"), &json!(""))
    );
    let (status, answer) = refused("/agent-llm", json!({ "session": real, "effort": "ultra" }));
    assert_eq!(status, 400);
    assert_eq!(
        answer,
        json!({
            "error": "invalid effort for backend",
            "backend": "claude",
            "valid_efforts": claude_efforts,
        })
    );
    assert_eq!(
        why("/agent-llm", json!({ "session": real, "model": 5 })),
        (400, "model must be string or null".to_string())
    );
    assert_eq!(
        why("/agent-llm", json!({ "session": real, "effort": ["high"] })),
        (400, "effort must be string or null".to_string())
    );
    assert_eq!(
        why("/agent-llm", json!({ "model": "x" })),
        (400, "session required".to_string())
    );
    assert_eq!(
        why("/agent-llm", json!({ "session": "nobody", "model": "x" })),
        (404, "no such agent".to_string())
    );
    // Another tool has its own efforts.
    let other = host.post("/agents", json!({ "name": "Other", "backend": "codex" }));
    let other = other["session"].as_str().unwrap().to_string();
    assert_eq!(
        host.post("/agent-llm", json!({ "session": other, "effort": "ultra" }))["effort"],
        "ultra"
    );

    // An agent reads the roster and tunes itself with its own tool.
    let admin = |own: Option<&str>, args: &[&str]| -> (i32, String, String) {
        let mut command = Command::new(env!("CARGO_BIN_EXE_clarp-host"));
        command
            .args(["tool", "clarp-admin"])
            .args(args)
            .current_dir(&place.root)
            .env("CLARP_HOST_ADDR", &host.address)
            .env("CLARP_HOST_TOKEN", &host.token)
            .env_remove("CLAUDE_PWA_SESSION");
        if let Some(own) = own {
            command.env("CLAUDE_PWA_SESSION", own);
        }
        let out = command.output().unwrap();
        (
            out.status.code().unwrap_or(-1),
            String::from_utf8_lossy(&out.stdout).into_owned(),
            String::from_utf8_lossy(&out.stderr).trim_end().to_string(),
        )
    };
    let (code, out, err) = admin(None, &["sessions"]);
    assert_eq!(code, 0, "{err}");
    let listed: Value = serde_json::from_str(&out).unwrap();
    let personas: Vec<&str> = listed
        .as_array()
        .unwrap()
        .iter()
        .map(|agent| agent["persona"].as_str().unwrap())
        .collect();
    assert_eq!(
        personas,
        ["Ada", "Bob", "Other", "Real", "Robert the Second"]
    );
    let ada_row = row(&host, &ada);
    assert_eq!(
        listed[0],
        json!({
            "session": "ada",
            "agent_id": ada_row["agent_id"],
            "persona": "Ada",
            "backend": "scripted",
            "cwd": ada_row["cwd"],
        })
    );
    let (code, out, err) = admin(Some(&real), &["model"]);
    assert_eq!(code, 0, "{err}");
    assert_eq!(
        serde_json::from_str::<Value>(&out).unwrap(),
        json!({
            "session": "real",
            "backend": "claude",
            "model": "claude-opus-5-5",
            "effort": "",
            "available": [],
        })
    );
    let (code, out, err) = admin(
        Some(&real),
        &["model", "claude-haiku-5-5", "--effort", "low"],
    );
    assert_eq!(code, 0, "{err}");
    assert_eq!(
        serde_json::from_str::<Value>(&out).unwrap(),
        json!({
            "ok": true,
            "backend": "claude",
            "model": "claude-haiku-5-5",
            "effort": "low",
            "valid_efforts": claude_efforts,
            "session": "real",
            "applies": "next turn",
        })
    );
    assert_eq!(row(&host, &real)["model"], "claude-haiku-5-5");
    // `default` is the tool's own choice again, and another agent is named with --session.
    let (code, out, err) = admin(
        Some(&real),
        &[
            "model",
            "default",
            "--effort",
            "default",
            "--session",
            &other,
        ],
    );
    assert_eq!(code, 0, "{err}");
    let cleared: Value = serde_json::from_str(&out).unwrap();
    assert_eq!(
        (&cleared["session"], &cleared["model"], &cleared["effort"]),
        (&json!("other"), &json!(""), &json!(""))
    );
    assert_eq!(
        admin(None, &["model"]),
        (
            1,
            String::new(),
            "no session: pass --session (CLAUDE_PWA_SESSION is unset)".to_string()
        )
    );
    assert_eq!(
        admin(None, &["model", "--session", "nobody"]),
        (
            1,
            String::new(),
            "no such agent session: nobody".to_string()
        )
    );
    assert_eq!(
        admin(Some(&real), &["model", "--effort", "ultra"]),
        (
            1,
            String::new(),
            "model change refused (HTTP 400): {\"error\": \"invalid effort for backend\", \
             \"backend\": \"claude\", \"valid_efforts\": [\"low\", \"medium\", \"high\", \
             \"xhigh\", \"max\"]}"
                .to_string()
        )
    );
    assert_eq!(admin(Some(&real), &["model", "--bogus"]).0, 2);

    // The Host keeps all of it.
    host.post("/agent-mute", json!({ "session": bob, "muted": true }));
    let before: Vec<Value> = [&ada, &bob, &real, &other]
        .iter()
        .map(|session| row(&host, session))
        .collect();
    host.kill();
    let host = place.start();
    let kept = |session: &str| {
        let row = row(&host, session);
        json!([
            row["persona"],
            row["muted"],
            row["archived_at"],
            row["model"],
            row["effort"]
        ])
    };
    for was in &before {
        let session = was["session"].as_str().unwrap();
        assert_eq!(
            kept(session),
            json!([
                was["persona"],
                was["muted"],
                was["archived_at"],
                was["model"],
                was["effort"]
            ]),
            "{session}"
        );
    }
    assert_eq!(kept(&ada)[2], put_away_at);
    assert_eq!(kept(&bob), json!(["Bob", true, null, "", ""]));
    assert_eq!(
        kept(&real),
        json!(["Real", false, null, "claude-haiku-5-5", "low"])
    );
    assert_eq!(kept(&other), json!(["Other", false, null, "", ""]));
}

#[test]
fn an_agent_makes_a_helper_and_the_helpers_work_is_followed_to_its_end() {
    let place = Place::new("helpers");
    // The fleet looks after its helpers often here, and one that is done is put away at once.
    let fast = [
        "--helpers-checked-every-ms",
        "100",
        "--helper-archive-grace-ms",
        "0",
    ];
    let host = place.start_with("", &fast);
    let heard = host.listen();
    let maker = host.new_agent("Maker");
    let other = host.new_agent("Other");
    let row = |session: &str| -> Value {
        host.get("/agents/snapshot")["agents"]
            .as_array()
            .unwrap()
            .iter()
            .find(|agent| agent["session"] == session)
            .cloned()
            .unwrap_or_else(|| panic!("{session} is not in the snapshot"))
    };
    let lineage = |session: &str| host.get(&format!("/agent-helper-state?session={session}"));
    let refused = |path: &str, body: Value| -> (u16, Value) { host.ask("POST", path, Some(&body)) };
    let maker_id = row(&maker)["agent_id"].as_str().unwrap().to_string();

    // Who makes it, and as what.
    let told = |status: u16, code: &str, message: &str| {
        (status, json!({ "error": code, "message": message }))
    };
    assert_eq!(
        refused(
            "/agents",
            json!({ "name": "H", "backend": "scripted", "role": "boss" })
        ),
        told(400, "invalid_role", "role must be 'agent' or 'helper'")
    );
    assert_eq!(
        refused(
            "/agents",
            json!({ "name": "H", "backend": "scripted", "role": "helper" })
        ),
        told(
            400,
            "helper_requires_parent",
            "A helper needs a parent session."
        )
    );
    assert_eq!(
        refused(
            "/agents",
            json!({ "name": "H", "backend": "scripted", "role": "helper", "parent": "nobody" })
        ),
        told(
            404,
            "parent_not_found",
            "No live agent matches parent 'nobody'."
        )
    );
    assert_eq!(
        refused("/agents", json!({ "backend": "scripted" })),
        told(400, "name required", "name required")
    );
    assert_eq!(
        refused(
            "/agents",
            json!({ "name": "H", "backend": "scripted", "model": 5 })
        ),
        told(
            400,
            "model must be a string or null",
            "model must be a string or null"
        )
    );
    assert_eq!(
        refused(
            "/agents",
            json!({ "name": "H", "backend": "claude", "effort": "extreme" })
        ),
        told(
            400,
            "invalid effort for backend",
            "invalid effort for backend"
        )
    );
    let (status, answer) = host.ask("POST", "/agents", Some(&json!([1])));
    assert_eq!((status, answer["error"].as_str()), (400, Some("bad json")));

    let made = host.post(
        "/agents",
        json!({
            "name": "Slice Helper",
            "backend": "scripted",
            "parent": maker,
            "role": "helper",
            "session": "slice-1",
            "synthesize_audio": false,
            "voice_id": "{}",
        }),
    );
    assert_eq!(
        (&made["ok"], &made["session"], &made["name"]),
        (&json!(true), &json!("slice-1"), &json!("Slice Helper"))
    );
    let agent = &made["agent"];
    assert_eq!(
        (
            &agent["session"],
            &agent["persona"],
            &agent["role"],
            &agent["parent_agent_id"],
            &agent["helper_state"],
            &agent["latest_state"],
            &agent["alive"],
        ),
        (
            &json!("slice-1"),
            &json!("Slice Helper"),
            &json!("helper"),
            &json!(maker_id),
            &json!("running"),
            &json!("spawned"),
            &json!(true),
        )
    );
    assert_eq!(agent.as_object().unwrap().len(), 21);
    let helper = "slice-1";
    let helper_id = agent["agent_id"].as_str().unwrap().to_string();
    // A session name that is taken is not given twice: the next agent gets one of its own.
    let second = host.post(
        "/agents",
        json!({ "name": "Second", "backend": "scripted", "session": "slice-1" }),
    );
    assert_eq!(second["session"], "second");
    assert_eq!(second["agent"]["role"], "agent");
    assert_eq!(second["agent"]["helper_state"], Value::Null);

    assert_eq!(
        lineage(helper),
        json!({
            "agent_id": helper_id,
            "session": "slice-1",
            "role": "helper",
            "parent_agent_id": maker_id,
            "helper_state": "running",
            "helper_completed_at": null,
            "archived_at": null,
        })
    );
    assert_eq!(lineage(&maker)["role"], "agent");
    let (status, answer) = host.ask("GET", "/agent-helper-state?session=nobody", None);
    assert_eq!(
        (status, answer["error"].as_str()),
        (404, Some("agent_not_found"))
    );
    let (status, answer) = host.ask("GET", "/agent-helper-state", None);
    assert_eq!(
        (status, answer["error"].as_str()),
        (400, Some("session required"))
    );

    // The maker's row counts what it made: it is at work while its helper is.
    let makers = row(&maker);
    assert_eq!(
        (
            &makers["child_count"],
            &makers["running_children"],
            &makers["background_jobs"],
            &makers["latest_state"],
            &makers["busy"],
        ),
        (
            &json!(1),
            &json!(1),
            &json!({ "count": 0, "sub_agents": 1 }),
            &json!("background"),
            &json!(false),
        )
    );
    assert!(
        makers["status_text"]
            .as_str()
            .is_some_and(|line| line.starts_with("slice-1")),
        "{}",
        makers["status_text"]
    );
    let helpers = row(helper);
    assert_eq!(
        (
            &helpers["role"],
            &helpers["parent_agent_id"],
            &helpers["helper_state"]
        ),
        (&json!("helper"), &json!(maker_id), &json!("running"))
    );

    let roster_told = |session: &str| {
        heard
            .lock()
            .unwrap()
            .iter()
            .filter(|event| {
                event["type"] == "agent-roster"
                    && event["kind"] == "helper-state"
                    && event["session"] == session
            })
            .count()
    };
    let agent_message = |to: &str, from: &str, text: &str, id: &str| {
        host.post(
            "/send",
            json!({ "session": to, "text": text, "client_msg_id": id, "origin": "agent", "sender": from }),
        )
    };
    let state = |session: &str| lineage(session)["helper_state"].clone();

    // The apps were told of the new agent in order: that it is there, then what an agent
    // that has never run is doing.
    wait_for("the apps to hear of the new agent", || {
        heard.lock().unwrap().iter().any(|event| {
            event["type"] == "agent-activity"
                && event["session"] == "slice-1"
                && event["kind"] == "spawned"
        })
    });
    let about: Vec<Value> = heard
        .lock()
        .unwrap()
        .iter()
        .filter(|event| event["session"] == "slice-1")
        .take(4)
        .cloned()
        .collect();
    assert_eq!(
        about
            .iter()
            .map(|event| (
                event["type"].as_str().unwrap(),
                event["kind"].as_str().unwrap_or("")
            ))
            .collect::<Vec<_>>(),
        [
            ("agent-roster", "created"),
            ("transcript-updated", ""),
            ("agent-state", "spawned"),
            ("agent-activity", "spawned"),
        ]
    );
    assert_eq!(
        (
            &about[0]["persona"],
            &about[0]["backend"],
            &about[0]["voice_id"]
        ),
        (&json!("Slice Helper"), &json!("scripted"), &json!("{}"))
    );
    assert_eq!(about[2]["detail"], json!({ "session": "slice-1" }));

    // The maker hands it work: it is working already, nothing moves. A message from
    // another agent waits its turn like one left for later: it goes through the queue.
    agent_message(helper, &maker, "do the slice", "h1");
    wait_for("the helper's turn", || host.state(helper) == "done");
    assert_eq!(state(helper), "running");
    assert_eq!(roster_told(helper), 0);
    assert_eq!(row(helper)["queued_turn_revision"], 2);
    // The helper's answer says whom it answers, and is its own: it names no sender.
    let rows = host.turns(helper);
    assert_eq!(
        (
            &rows[0]["sender_session"],
            &rows[0]["reply_to_session"],
            &rows[1]["sender_session"],
            &rows[1]["reply_to_session"],
            &rows[1]["reply_to_name"],
            &rows[1]["reply_to_agent_id"],
        ),
        (
            &json!("maker"),
            &json!(""),
            &json!(""),
            &json!("maker"),
            &json!("Maker"),
            &json!(maker_id),
        )
    );
    // What a helper says tells nobody: it reports to the agent that made it.
    host.send(helper, "how is it going?", "h2");
    let told_of = |id: &str| {
        heard
            .lock()
            .unwrap()
            .iter()
            .find(|event| event["type"] == "user-notification" && event["cause_message_id"] == id)
            .cloned()
    };
    wait_for("the helper's answer to the user", || {
        told_of("u-h2").is_some()
    });
    let quiet = told_of("u-h2").unwrap();
    assert_eq!(
        (&quiet["push"], &quiet["muted"]),
        (&json!(false), &json!(true))
    );

    // An agent that is neither its maker nor its helper moves nothing.
    agent_message(&maker, &other, "unrelated", "h3");
    wait_for("the maker's turn", || host.state(&maker) == "background");
    assert_eq!(state(helper), "running");
    // It writes to its maker: it has reported.
    agent_message(&maker, helper, "the slice is done", "h4");
    wait_for("the report to be noted", || state(helper) == "reported");
    wait_for("the apps to hear of the helper's move", || {
        roster_told(helper) == 1
    });
    wait_for("the maker to be at rest", || host.state(&maker) == "done");
    let makers = row(&maker);
    assert_eq!(
        (
            &makers["running_children"],
            &makers["background_jobs"],
            &makers["status_text"]
        ),
        (
            &json!(0),
            &json!({ "count": 0, "sub_agents": 0 }),
            &Value::Null
        )
    );
    assert_eq!(makers["child_count"], 1);
    // The maker writes back: more work.
    agent_message(helper, &maker, "one more thing", "h5");
    wait_for("the helper to be working again", || {
        state(helper) == "running"
    });
    wait_for("the apps to hear of the helper's move", || {
        roster_told(helper) == 2
    });
    wait_for("the helper's turn", || host.state(helper) == "done");

    // Marked by hand: by the agent that made it, or by the user.
    let mark = |body: Value| refused("/agent-helper-state", body);
    assert_eq!(
        mark(json!({ "session": helper })).1["error"],
        "session and state required"
    );
    assert_eq!(
        mark(json!({ "session": helper, "state": "abandoned" })),
        told(
            400,
            "invalid_state",
            "state must be done, failed or running"
        )
    );
    assert_eq!(
        mark(json!({ "session": "nobody", "state": "done" })),
        told(404, "agent_not_found", "No live agent: nobody")
    );
    assert_eq!(
        mark(json!({ "session": maker, "state": "done" })),
        told(409, "not_a_helper", "maker is not a helper")
    );
    assert_eq!(
        mark(json!({ "session": helper, "state": "done", "by": other })),
        told(
            403,
            "not_parent",
            "Only the helper's parent or the user may mark it"
        )
    );
    assert_eq!(
        mark(json!({ "session": helper, "state": "done", "by": "nobody" })),
        told(404, "agent_not_found", "No live agent: nobody")
    );
    assert_eq!(
        mark(json!({ "session": helper, "state": "failed", "by": maker })),
        (
            200,
            json!({ "ok": true, "session": "slice-1", "helper_state": "failed" })
        )
    );
    let failed_at = lineage(helper)["helper_completed_at"].as_i64().unwrap();
    assert!(failed_at > 1_600_000_000_000);
    // The state it has already is no change and no refusal.
    assert_eq!(mark(json!({ "session": helper, "state": "failed" })).0, 200);
    wait_for("the apps to hear of the helper's move", || {
        roster_told(helper) == 3
    });
    // Opened again by the user, and the moment it ended is forgotten.
    assert_eq!(
        mark(json!({ "session": helper, "state": "running" })).1["helper_state"],
        "running"
    );
    assert_eq!(lineage(helper)["helper_completed_at"], Value::Null);

    // The agent's own tool for it.
    let admin = |args: &[&str]| -> (i32, Value, String) {
        let mut all = vec!["clarp-admin", "agent"];
        all.extend_from_slice(args);
        let (code, out, err) = tool(&host, &place.root, None, &all);
        (
            code,
            serde_json::from_str(&out).unwrap_or(Value::Null),
            err.trim_end().to_string(),
        )
    };
    let (code, shown, err) = admin(&["helper-state", helper]);
    assert_eq!(code, 0, "{err}");
    assert_eq!(shown, lineage(helper));
    let work = place.root.join("work-of-two");
    std::fs::create_dir_all(&work).unwrap();
    let (code, made, err) = admin(&[
        "create",
        "tool-helper",
        "--parent",
        &maker,
        "--role",
        "helper",
        "--cwd",
        "work-of-two",
        "--backend",
        "scripted",
    ]);
    assert_eq!(code, 0, "{err}");
    assert_eq!(made["session"], "tool-helper");
    assert_eq!(made["agent"]["helper_state"], "running");
    assert_eq!(
        made["agent"]["cwd"],
        json!(work.canonicalize().unwrap().display().to_string()),
        "a directory is named from where the tool was called"
    );
    let (code, _, err) = admin(&["create", "x", "--role", "helper"]);
    assert_eq!(
        (code, err.as_str()),
        (1, "--role helper needs --parent SESSION")
    );
    let (code, _, err) = admin(&[
        "create",
        "x",
        "--role",
        "helper",
        "--parent",
        "nobody",
        "--backend",
        "scripted",
    ]);
    assert_eq!(code, 1);
    assert_eq!(
        err,
        "clarp-admin: HTTP 404: {\"error\": \"parent_not_found\", \"message\": \"No live agent matches parent 'nobody'.\"}"
    );
    let (code, marked, err) = admin(&["helper-state", "tool-helper", "done", "--from", &maker]);
    assert_eq!(code, 0, "{err}");
    assert_eq!(
        marked,
        json!({ "ok": true, "session": "tool-helper", "helper_state": "done" })
    );
    let (code, _, err) = admin(&["helper-state", "tool-helper", "failed", "--from", &maker]);
    assert_eq!(code, 1);
    assert_eq!(
        err,
        "clarp-admin: HTTP 409: {\"error\": \"illegal_transition\", \"message\": \"tool-helper is done; cannot mark failed\"}"
    );
    assert_eq!(admin(&["helper-state", "tool-helper", "sideways"]).0, 2);
    assert_eq!(admin(&["fork", "x"]).0, 69);

    // A helper that has been done long enough is put away by the fleet.
    wait_for("the finished helper to be put away", || {
        lineage("tool-helper")["archived_at"].as_i64().is_some()
    });
    wait_for("the apps to hear that it is done and put away", || {
        roster_told("tool-helper") == 2
    });
    assert_eq!(
        row(&maker)["child_count"],
        2,
        "it is still one of the two it made"
    );

    // The maker is put away: what it made and has not finished loses it.
    host.post(
        "/agent-archive",
        json!({ "session": maker, "archived": true }),
    );
    assert_eq!(state(helper), "abandoned");
    assert_eq!(
        state("tool-helper"),
        "done",
        "what is finished stays as it ended"
    );
    assert!(lineage(helper)["helper_completed_at"].as_i64().is_some());
    let without = host.get("/agents/snapshot?include_archived=0");
    assert_eq!(without["archived_parents"], json!([maker_id]));
    assert_eq!(without["archived_count"], 2);
    // An abandoned helper can be taken up again by the user.
    assert_eq!(
        mark(json!({ "session": helper, "state": "running" })).1["helper_state"],
        "running"
    );
    // And when its maker is removed, it is abandoned and stays: nothing is removed with it.
    let (status, _) = host.ask("DELETE", &format!("/agents/{maker}"), None);
    assert_eq!(status, 200);
    assert_eq!(state(helper), "abandoned");
    assert_eq!(lineage(helper)["parent_agent_id"], json!(maker_id));

    // The Host keeps all of it.
    let before = lineage(helper);
    host.kill();
    let host = place.start_with("", &fast);
    assert_eq!(
        host.get(&format!("/agent-helper-state?session={helper}")),
        before
    );
}

/// Writes a program a test runs in place of a real one.
fn program(path: &std::path::Path, text: &str) {
    use std::os::unix::fs::PermissionsExt;
    std::fs::write(path, text).unwrap();
    std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o755)).unwrap();
}

/// The first program of this name on the path the tests run with.
fn on_path(name: &str) -> Option<PathBuf> {
    std::env::var_os("PATH").and_then(|path| {
        std::env::split_paths(&path)
            .map(|dir| dir.join(name))
            .find(|candidate| candidate.is_file())
    })
}

/// What a stand-in for Claude Code prints for a turn: it began, it answered one line, it ended.
const STAND_IN_ANSWERS: &str = "\
printf '%s\\n' '{\"type\":\"system\",\"subtype\":\"init\",\"session_id\":\"stand-in\",\"model\":\"stand-in\"}'\n\
printf '%s\\n' '{\"type\":\"assistant\",\"session_id\":\"stand-in\",\"message\":{\"id\":\"msg_1\",\"role\":\"assistant\",\"model\":\"stand-in\",\"content\":[{\"type\":\"text\",\"text\":\"on it\"}]}}'\n\
printf '%s\\n' '{\"type\":\"result\",\"subtype\":\"success\",\"is_error\":false,\"session_id\":\"stand-in\",\"result\":\"on it\",\"num_turns\":1}'\n";

#[test]
fn a_turn_is_given_a_token_for_its_agents_tools_and_never_the_hosts_own() {
    let place = Place::new("tool-token");
    let token = "the-administrators-token";
    // A stand-in for Claude Code that writes down what it was given, calls one of its
    // agent's tools the way a command of the agent would, and answers.
    let seen = place.root.join("seen");
    let claude = place.root.join("claude");
    let script = [
        "#!/bin/sh\nread message\n{\n",
        "echo \"host_token=${CLARP_HOST_TOKEN+given}\"\n",
        "echo \"push_credential=${CLARP_PUSH_CREDENTIAL+given}\"\n",
        "echo \"tool_token=$CLARP_TOOL_TOKEN\"\n",
        &format!(
            "\"{}\" tool clarp-admin sessions > /dev/null 2>&1\n",
            env!("CARGO_BIN_EXE_clarp-host")
        ),
        "echo \"tool_exit=$?\"\n",
        &format!("}} > \"{}\"\n", seen.display()),
        STAND_IN_ANSWERS,
    ]
    .concat();
    program(&claude, &script);
    let host = place.start_in(
        token,
        &[],
        &[
            ("CLARP_CLAUDE_PROGRAM", claude.to_str().unwrap()),
            ("CLARP_PUSH_CREDENTIAL", "the-relays-credential"),
        ],
    );
    // An agent that Claude Code runs: its turns are the stand-in above.
    let made = host.post("/agents", json!({ "name": "Worker", "backend": "claude" }));
    let agent = made["session"].as_str().unwrap().to_string();
    host.send(&agent, "what were you given?", "m1");
    wait_for("the turn's answer", || {
        host.conversation(&agent).len() == 2 && host.state(&agent) == "done"
    });

    let seen = std::fs::read_to_string(&seen).unwrap();
    let said = |key: &str| -> String {
        seen.lines()
            .find_map(|line| line.strip_prefix(&format!("{key}=")))
            .unwrap_or("not said")
            .to_string()
    };
    // Nothing of the Host's own is in what a turn inherits.
    assert_eq!(said("host_token"), "", "{seen}");
    assert_eq!(said("push_credential"), "", "{seen}");
    // What it has instead follows from the administrator's token and is not it, and the
    // agent's tools reach their Host with it.
    let tools = clarp_host::tool_token(token);
    assert_ne!(tools, token);
    assert_eq!(said("tool_token"), tools, "{seen}");
    assert_eq!(said("tool_exit"), "0", "{seen}");

    // That token opens the tools, from this machine, and nothing the apps read or do.
    let by_hand = Command::new(env!("CARGO_BIN_EXE_clarp-host"))
        .args(["tool", "clarp-admin", "sessions"])
        .current_dir(&place.root)
        .env("CLARP_HOST_ADDR", &host.address)
        .env("CLARP_TOOL_TOKEN", &tools)
        .env_remove("CLARP_HOST_TOKEN")
        .output()
        .unwrap();
    assert_eq!(
        by_hand.status.code(),
        Some(0),
        "{}",
        String::from_utf8_lossy(&by_hand.stderr)
    );
    let log = format!("/log?session={agent}");
    for (method, path) in [
        ("GET", "/agents/snapshot"),
        ("GET", log.as_str()),
        ("POST", "/send"),
        ("POST", "/agents"),
        ("POST", "/pairing/issue"),
        ("GET", "/paired-devices"),
        ("GET", "/peers"),
    ] {
        let (status, refused) = host.ask_as(&tools, method, path, None);
        assert_eq!(status, 403, "{method} {path}: {refused}");
    }
    let (_, refused) = host.ask_as(&tools, "GET", "/agents/snapshot", None);
    assert_eq!(refused["error"], "an agent's tools may ask for tools only");
    // The administrator still calls a tool by hand with the administrator's token.
    let (code, _, err) = tool(&host, &place.root, None, &["clarp-admin", "sessions"]);
    assert_eq!(code, 0, "{err}");
    // And a token that is neither opens nothing.
    let (status, _) = host.ask_as("neither", "GET", "/agents/snapshot", None);
    assert_eq!(status, 401);
}

#[test]
fn a_turn_that_fails_is_told_under_its_message_in_the_python_hosts_words() {
    let place = Place::new("failed");
    // A stand-in for Claude Code whose account is out when the message says so, that dies
    // when the message says so, and that answers otherwise.
    let init = "printf '%s\\n' '{\"type\":\"system\",\"subtype\":\"init\",\"session_id\":\"stand-in\",\"model\":\"stand-in\"}'\n";
    let script = [
        "#!/bin/sh\nread message\ncase \"$message\" in\n*limit*)\n",
        init,
        "printf '%s\\n' '{\"type\":\"rate_limit_event\",\"rate_limit_info\":{\"status\":\"rejected\"}}'\n",
        "printf '%s\\n' '{\"type\":\"result\",\"subtype\":\"success\",\"is_error\":true,\"session_id\":\"stand-in\",\"result\":\"out for now\"}'\n",
        "exit 1 ;;\n*crash*)\n",
        init,
        "echo 'Error: the tool broke' >&2\nexit 1 ;;\n*count*)\n",
        init,
        "printf '%s\\n' '{\"type\":\"assistant\",\"session_id\":\"stand-in\",\"message\":{\"id\":\"msg_9\",\"role\":\"assistant\",\"model\":\"stand-in\",\"content\":[{\"type\":\"text\",\"text\":\"counted\"}]}}'\n",
        "printf '%s\\n' '{\"type\":\"result\",\"subtype\":\"success\",\"is_error\":false,\"session_id\":\"stand-in\",\"result\":\"counted\",\"usage\":{\"input_tokens\":9,\"output_tokens\":2}}'\n",
        "exit 0 ;;\nesac\n",
        STAND_IN_ANSWERS,
    ]
    .concat();
    let claude = place.root.join("claude");
    program(&claude, &script);
    let host = place.start_in(
        "",
        &[],
        &[("CLARP_CLAUDE_PROGRAM", claude.to_str().unwrap())],
    );
    let made = host.post("/agents", json!({ "name": "Worker", "backend": "claude" }));
    let agent = made["session"].as_str().unwrap().to_string();
    let events = host.listen();
    let row = || -> Value {
        host.get("/agents/snapshot")["agents"]
            .as_array()
            .unwrap()
            .iter()
            .find(|row| row["session"] == agent)
            .cloned()
            .unwrap()
    };
    let failed = |rows: usize| {
        wait_for("the turn to fail", || {
            host.turns(&agent).len() == rows && host.state(&agent) == "interrupted"
        });
    };

    // The account is out.
    host.send(&agent, "reach the limit", "kilo");
    failed(2);
    let rows = host.turns(&agent);
    // The turn said nothing: its end is told on the message itself.
    assert_eq!(
        (&rows[0]["id"], &rows[0]["turn"]["status"]),
        (&json!("u-kilo"), &json!("failed"))
    );
    // And under the message the Host says why, in a row of its own that closes nothing.
    assert_eq!(
        (
            &rows[1]["id"],
            &rows[1]["origin"],
            &rows[1]["role"],
            &rows[1]["kind"],
            &rows[1]["text"],
            &rows[1]["turn"],
        ),
        (
            &json!("marker-u-kilo"),
            &json!("system"),
            &json!("assistant"),
            &Value::Null,
            &json!("Usage limit reached.\n\nClaude usage limit reached"),
            &Value::Null,
        )
    );
    assert_eq!(rows[1]["trace_id"], rows[0]["trace_id"]);
    assert_ne!(rows[1]["trace_id"], "");
    let shown = row();
    assert_eq!(
        (
            &shown["latest_state"],
            &shown["activity"]["summary"],
            &shown["live_activity"]["headline"],
            &shown["last_message"],
            &shown["last_message_id"],
        ),
        (
            &json!("interrupted"),
            &json!("Usage limit reached. Your message is saved."),
            &json!("Usage limit reached"),
            &json!("Usage limit reached. Claude usage limit reached"),
            &json!("marker-u-kilo"),
        )
    );
    let detail = events
        .lock()
        .unwrap()
        .iter()
        .rev()
        .find(|event| event["type"] == "agent-state" && event["kind"] == "interrupted")
        .map(|event| event["detail"].clone())
        .expect("the apps were told the turn failed");
    assert_eq!(
        detail,
        json!({
            "attempts": 1,
            "dispatch": "claude",
            "error": "Claude usage limit reached",
            "message": "Usage limit reached",
            "provider_message": "Claude usage limit reached",
            "reason": "usage_limit",
            "summary": "Usage limit reached. Your message is saved.",
        })
    );

    // The tool dies: nothing reads its words as a kind of failure, and they are given whole.
    host.send(&agent, "crash now", "lima");
    failed(4);
    let rows = host.turns(&agent);
    assert_eq!(
        (&rows[2]["id"], &rows[2]["turn"]["status"]),
        (&json!("u-lima"), &json!("failed"))
    );
    assert_eq!(
        (&rows[3]["id"], &rows[3]["text"]),
        (
            &json!("marker-u-lima"),
            &json!("Turn failed.\n\nError: the tool broke")
        )
    );
    assert_eq!(
        row()["activity"]["summary"],
        "Turn failed. Your message is saved."
    );

    // And the agent works on.
    host.send(&agent, "hello", "mike");
    wait_for("the answer", || {
        host.turns(&agent).len() == 6 && host.state(&agent) == "done"
    });
    let rows = host.turns(&agent);
    assert_eq!(
        (&rows[5]["role"], &rows[5]["turn"]["status"]),
        (&json!("assistant"), &json!("completed"))
    );
    assert_eq!(rows[4]["turn"], Value::Null);

    // How full the conversation is, is what a turn last said of it. A tool that dies says
    // nothing: the gauge stays, and the Host still knows it after it was started again.
    host.send(&agent, "count this", "november");
    wait_for("the count", || {
        host.turns(&agent).len() == 8 && host.state(&agent) == "done"
    });
    assert_eq!(row()["context_tokens"], 9);
    host.send(&agent, "crash now", "oscar");
    failed(10);
    assert_eq!(row()["context_tokens"], 9);
    host.kill();
    let host = place.start_in(
        "",
        &[],
        &[("CLARP_CLAUDE_PROGRAM", claude.to_str().unwrap())],
    );
    let again = host.get("/agents/snapshot")["agents"]
        .as_array()
        .unwrap()
        .iter()
        .find(|row| row["session"] == agent)
        .cloned()
        .unwrap();
    assert_eq!(
        (&again["context_tokens"], &again["latest_state"]),
        (&json!(9), &json!("interrupted"))
    );
}

#[test]
fn a_connection_that_drops_is_tried_again_and_a_turn_that_never_gets_through_says_so() {
    let place = Place::new("retried");
    // A stand-in for Claude Code whose connection drops as often as the message says: it
    // counts, in a file named after the message, how often it was given it.
    let counts = place.root.join("counts");
    std::fs::create_dir_all(&counts).unwrap();
    let init = "printf '%s\\n' '{\"type\":\"system\",\"subtype\":\"init\",\"session_id\":\"stand-in\",\"model\":\"stand-in\"}'\n";
    let script = [
        "#!/bin/sh\nread message\n",
        "key=$(printf '%s' \"$message\" | cksum | cut -d' ' -f1)\n",
        &format!("file=\"{}/$key\"\n", counts.display()),
        "before=$(cat \"$file\" 2>/dev/null || echo 0)\n",
        "echo $((before + 1)) > \"$file\"\n",
        "case \"$message\" in\n*once*) drops=1 ;;\n*always*) drops=9 ;;\n*) drops=0 ;;\nesac\n",
        "if [ \"$before\" -lt \"$drops\" ]; then\n",
        init,
        "echo 'API Error: socket connection was closed unexpectedly' >&2\nexit 1\nfi\n",
        // It answers, under an id no other answer has: the message's, and which attempt.
        init,
        "printf '{\"type\":\"assistant\",\"session_id\":\"stand-in\",\"message\":{\"id\":\"msg_%s_%s\",\"role\":\"assistant\",\"model\":\"stand-in\",\"content\":[{\"type\":\"text\",\"text\":\"on it\"}]}}\\n' \"$key\" \"$before\"\n",
        "printf '%s\\n' '{\"type\":\"result\",\"subtype\":\"success\",\"is_error\":false,\"session_id\":\"stand-in\",\"result\":\"on it\",\"num_turns\":1}'\n",
    ]
    .concat();
    let claude = place.root.join("claude");
    program(&claude, &script);
    let host = place.start_in(
        "",
        &[],
        &[("CLARP_CLAUDE_PROGRAM", claude.to_str().unwrap())],
    );
    let made = host.post("/agents", json!({ "name": "Worker", "backend": "claude" }));
    let agent = made["session"].as_str().unwrap().to_string();
    let events = host.listen();
    // How often the stand-in was started, for all messages together.
    let started = || -> u64 {
        std::fs::read_dir(&counts)
            .unwrap()
            .map(|file| {
                std::fs::read_to_string(file.unwrap().path())
                    .unwrap()
                    .trim()
                    .parse::<u64>()
                    .unwrap()
            })
            .sum()
    };

    // The connection drops once. The turn is tried again, and answers: one turn, one answer,
    // and nothing in the conversation says that it took two attempts.
    host.send(&agent, "drop once", "quebec");
    wait_for("the answer", || {
        host.turns(&agent).len() == 2 && host.state(&agent) == "done"
    });
    assert_eq!(started(), 2, "the tool was started twice");
    let rows = host.turns(&agent);
    assert_eq!(
        (&rows[0]["id"], &rows[0]["turn"]),
        (&json!("u-quebec"), &Value::Null)
    );
    assert_eq!(
        (&rows[1]["role"], &rows[1]["turn"]["status"]),
        (&json!("assistant"), &json!("completed"))
    );
    // While it waited for its second attempt, the apps were told that it reconnects: as busy
    // as before, and which attempt comes after how long.
    let told = |kind: &str| -> Vec<Value> {
        events
            .lock()
            .unwrap()
            .iter()
            .filter(|event| event["type"] == kind)
            .cloned()
            .collect()
    };
    let reconnects: Vec<Value> = told("agent-state")
        .into_iter()
        .filter(|event| event["detail"]["action"] == "reconnecting")
        .collect();
    assert_eq!(reconnects.len(), 1, "{reconnects:?}");
    assert_eq!(
        (&reconnects[0]["kind"], &reconnects[0]["detail"]),
        (
            &json!("thinking"),
            &json!({
                "action": "reconnecting",
                "after_ms": 1000,
                "dispatch": "claude",
                "of": 3,
                "origin": "user",
                "reconnect": 2,
                "summary": "Reconnecting… Your message is saved.",
            })
        )
    );
    assert!(told("agent-activity").iter().any(|event| {
        event["action"] == "reconnecting"
            && event["status"] == "running"
            && event["summary"] == "Reconnecting… Your message is saved."
    }));
    // The apps never saw the agent at rest between the two attempts.
    assert!(
        !events
            .lock()
            .unwrap()
            .iter()
            .any(|event| event["type"] == "agent-state" && event["kind"] == "interrupted"),
        "a turn that is tried again has not failed"
    );

    // It drops every time: three attempts, and then the turn says so.
    host.send(&agent, "drop always", "romeo");
    wait_for("the turn to give up", || {
        host.turns(&agent).len() == 4 && host.state(&agent) == "interrupted"
    });
    assert_eq!(started(), 5, "three attempts and no more");
    let rows = host.turns(&agent);
    assert_eq!(
        (&rows[2]["id"], &rows[2]["turn"]["status"]),
        (&json!("u-romeo"), &json!("failed"))
    );
    assert_eq!(
        (&rows[3]["id"], &rows[3]["text"]),
        (
            &json!("marker-u-romeo"),
            &json!(
                "Connection lost — retries exhausted.\n\nAPI Error: socket connection was closed unexpectedly"
            )
        )
    );
    let detail = events
        .lock()
        .unwrap()
        .iter()
        .rev()
        .find(|event| event["type"] == "agent-state" && event["kind"] == "interrupted")
        .map(|event| event["detail"].clone())
        .expect("the apps were told the turn failed");
    assert_eq!(
        (&detail["reason"], &detail["attempts"], &detail["message"]),
        (
            &json!("connection"),
            &json!(3),
            &json!("Connection lost — retries exhausted")
        )
    );

    // A turn that waits for its next attempt and is stopped ends there: nothing is started
    // again. (The next attempt is a second away.)
    host.send(&agent, "drop always, again", "sierra");
    wait_for("the first attempt to fail", || started() == 6);
    let (status, _) = host.ask("POST", "/stop", Some(&json!({ "session": agent })));
    assert_eq!(status, 200);
    wait_for("the stop", || host.state(&agent) == "interrupted");
    std::thread::sleep(Duration::from_millis(1_500));
    assert_eq!(started(), 6, "a stopped turn is not tried again");

    // And a Host that is started again while a turn waits takes the wait up.
    let (status, _) = host.ask(
        "POST",
        "/turn-queue/resume",
        Some(&json!({ "session": agent })),
    );
    assert_eq!(status, 200);
    host.send(&agent, "drop once more", "tango");
    wait_for("the first attempt to fail", || started() == 7);
    host.kill();
    let host = place.start_in(
        "",
        &[],
        &[("CLARP_CLAUDE_PROGRAM", claude.to_str().unwrap())],
    );
    wait_for("the second attempt after the restart", || started() >= 8);
    let deadline = Instant::now() + Duration::from_secs(20);
    loop {
        let (rows, state) = (host.turns(&agent), host.state(&agent));
        let answered = rows
            .last()
            .is_some_and(|row| row["turn"]["status"] == "completed");
        if answered && state == "done" {
            break;
        }
        assert!(
            Instant::now() < deadline,
            "no answer after the restart: the tool was started {} times, the agent is {state}, \
             and the conversation ends with {:#?}",
            started(),
            &rows[rows.len().saturating_sub(3)..]
        );
        std::thread::sleep(Duration::from_millis(50));
    }
    assert_eq!(
        started(),
        8,
        "the second attempt ran after the restart, once"
    );
}

#[test]
fn a_turn_whose_account_is_out_waits_and_is_taken_up_when_the_login_has_changed() {
    let place = Place::new("account");
    // A stand-in for Claude Code whose account is out for as long as a file says so. It
    // writes down what it was given, each time.
    let (out_of_usage, given) = (place.root.join("out"), place.root.join("given"));
    let init = "printf '%s\\n' '{\"type\":\"system\",\"subtype\":\"init\",\"session_id\":\"stand-in\",\"model\":\"stand-in\"}'\n";
    let script = [
        "#!/bin/sh\nread message\n",
        &format!("printf '%s\\n' \"$message\" >> \"{}\"\n", given.display()),
        &format!("times=$(wc -l < \"{}\")\n", given.display()),
        &format!("if [ -e \"{}\" ]; then\n", out_of_usage.display()),
        init,
        "printf '%s\\n' '{\"type\":\"rate_limit_event\",\"rate_limit_info\":{\"status\":\"rejected\"}}'\n",
        "printf '%s\\n' '{\"type\":\"result\",\"subtype\":\"success\",\"is_error\":true,\"session_id\":\"stand-in\",\"result\":\"out for now\"}'\n",
        "exit 1\nfi\n",
        init,
        "printf '{\"type\":\"assistant\",\"session_id\":\"stand-in\",\"message\":{\"id\":\"msg_%s\",\"role\":\"assistant\",\"model\":\"stand-in\",\"content\":[{\"type\":\"text\",\"text\":\"on it\"}]}}\\n' \"$times\"\n",
        "printf '%s\\n' '{\"type\":\"result\",\"subtype\":\"success\",\"is_error\":false,\"session_id\":\"stand-in\",\"result\":\"on it\",\"num_turns\":1}'\n",
    ]
    .concat();
    let claude = place.root.join("claude");
    program(&claude, &script);
    // The login of the tool, where the Host looks for it: in its user's home.
    let home = place.root.join("home");
    let login = home.join(".claude").join(".credentials.json");
    std::fs::create_dir_all(login.parent().unwrap()).unwrap();
    std::fs::write(&login, "the first account").unwrap();
    let host = place.start_in(
        "",
        &["--accounts", "follow", "--logins-checked-every-ms", "100"],
        &[
            ("CLARP_CLAUDE_PROGRAM", claude.to_str().unwrap()),
            ("HOME", home.to_str().unwrap()),
        ],
    );
    let made = host.post("/agents", json!({ "name": "Worker", "backend": "claude" }));
    let agent = made["session"].as_str().unwrap().to_string();
    let events = host.listen();
    let row = || -> Value {
        host.get("/agents/snapshot")["agents"]
            .as_array()
            .unwrap()
            .iter()
            .find(|row| row["session"] == agent)
            .cloned()
            .unwrap()
    };
    let started = || -> usize {
        std::fs::read_to_string(&given)
            .map(|said| said.lines().count())
            .unwrap_or(0)
    };
    let waits = || row()["live_activity"]["state"] == "limited";

    // The account is out. The turn does not end: it keeps its place and says what it
    // waits for.
    std::fs::write(&out_of_usage, "").unwrap();
    host.send(&agent, "work on this", "uniform");
    wait_for("the turn to wait for an account", waits);
    let shown = row();
    assert_eq!(
        (
            &shown["busy"],
            &shown["latest_state"],
            &shown["activity"]["action"],
            &shown["activity"]["phase"],
            &shown["activity"]["state"],
            &shown["activity"]["summary"],
            &shown["live_activity"]["headline"],
        ),
        (
            &json!(true),
            &json!("thinking"),
            &json!("waiting for account"),
            &json!("account_recovery"),
            &json!("limited"),
            &json!("Waiting for a claude account with available usage"),
            &json!("Waiting for a claude account with available usage"),
        )
    );
    let told = events
        .lock()
        .unwrap()
        .iter()
        .rev()
        .find(|event| {
            event["type"] == "agent-state" && event["detail"]["account_recovery"] == "waiting"
        })
        .cloned()
        .expect("the apps were told the turn waits");
    assert_eq!(
        (
            &told["kind"],
            &told["detail"]["dispatch"],
            &told["detail"]["message"]
        ),
        (
            &json!("thinking"),
            &json!("claude"),
            &json!("Waiting for a claude account with available usage")
        )
    );
    // Nothing in the conversation says the turn failed: it has not.
    assert_eq!(host.turns(&agent).len(), 1);
    assert_eq!(started(), 1);

    // Someone puts another account in place. The turn is taken up on it: the tool, which
    // has the turn in its session, is told so and is not given the message a second time.
    std::fs::remove_file(&out_of_usage).unwrap();
    std::fs::write(&login, "another account").unwrap();
    wait_for("the answer", || {
        host.turns(&agent).len() == 2 && host.state(&agent) == "done"
    });
    assert_eq!(started(), 2);
    let said = std::fs::read_to_string(&given).unwrap();
    let again = said.lines().nth(1).unwrap();
    assert!(
        again.contains("<clarp-account-recovery>")
            && again.contains("Clarp recovered from a claude account usage limit")
            && again.contains("Original request:")
            && again.contains("work on this"),
        "{again}"
    );
    let rows = host.turns(&agent);
    assert_eq!(
        (&rows[0]["id"], &rows[0]["text"], &rows[1]["turn"]["status"]),
        (
            &json!("u-uniform"),
            &json!("work on this"),
            &json!("completed")
        )
    );
    assert!(!waits());

    // A turn that waits for an account and is stopped ends there, and a login that changes
    // afterwards starts nothing.
    std::fs::write(&out_of_usage, "").unwrap();
    host.send(&agent, "and this", "victor");
    wait_for("the turn to wait for an account", waits);
    let (status, _) = host.ask("POST", "/stop", Some(&json!({ "session": agent })));
    assert_eq!(status, 200);
    wait_for("the stop", || host.state(&agent) == "interrupted");
    std::fs::write(&login, "a third account").unwrap();
    std::thread::sleep(Duration::from_millis(600));
    assert_eq!(started(), 3, "a stopped turn is not taken up");
}

#[test]
fn the_program_an_agent_starts_helpers_with_works_against_this_host() {
    // `clarp-sub-agent` is the repository's own program (skills/clarp-sub-agents). It asks
    // no Host anything itself: it calls clarp-admin and clarp-agent-bg by name. Here those
    // names are this Host's, as an installation puts them on an agent's path.
    let script = concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../../skills/clarp-sub-agents/scripts/sub_agent.py"
    );
    assert!(
        std::path::Path::new(script).is_file(),
        "the program is not beside server-rs: {script}"
    );
    let python = on_path("python3").expect("the program needs python3");
    let place = Place::new("sub-agent");
    let token = "sub-agent-token";
    // A stand-in for Claude Code: it takes the message and answers one line.
    let claude = place.root.join("claude");
    program(
        &claude,
        &format!("#!/bin/sh\nread message\n{STAND_IN_ANSWERS}"),
    );
    let host = place.start_in(
        token,
        &[],
        &[("CLARP_CLAUDE_PROGRAM", claude.to_str().unwrap())],
    );
    let maker = host.new_agent("Maker");

    // An agent's path, as an installation makes it: this Host's tools, and python. Nothing
    // of systemd is on it, so the program starts no watcher of its own here.
    let tools = place.root.join("tools");
    std::fs::create_dir_all(&tools).unwrap();
    for name in ["clarp-admin", "clarp-agent-bg"] {
        program(
            &tools.join(name),
            &format!(
                "#!/bin/sh\nexec \"{}\" tool {name} \"$@\"\n",
                env!("CARGO_BIN_EXE_clarp-host")
            ),
        );
    }
    std::os::unix::fs::symlink(&python, tools.join("python3")).unwrap();
    // What a worker an agent left running finds its Host by: the file an installation
    // writes. No turn tells this program where the Host is.
    let config = place.root.join("config");
    std::fs::create_dir_all(&config).unwrap();
    let (address, port) = host.address.rsplit_once(':').unwrap();
    std::fs::write(
        config.join("config.toml"),
        format!(
            "[server]\nkind = \"clarp-host\"\nbind_addr = \"{address}\"\nport = {port}\nauth_token = \"{token}\"\n"
        ),
    )
    .unwrap();
    let kept = place.root.join("sub-agents");
    let run = |args: &[&str]| -> (i32, String, String) {
        let out = Command::new(&python)
            .arg(script)
            .args(args)
            .current_dir(&place.root)
            .env_clear()
            .env("PATH", &tools)
            .env("HOME", &place.root)
            .env("CLARP_CONFIG_DIR", &config)
            .env("CLARP_SUB_AGENT_DIR", &kept)
            .env("CLAUDE_PWA_SESSION", &maker)
            .output()
            .unwrap();
        (
            out.status.code().unwrap_or(-1),
            String::from_utf8_lossy(&out.stdout).into_owned(),
            String::from_utf8_lossy(&out.stderr).into_owned(),
        )
    };
    let lineage = |session: &str| host.get(&format!("/agent-helper-state?session={session}"));
    let row = |session: &str| -> Value {
        host.get("/agents/snapshot")["agents"]
            .as_array()
            .unwrap()
            .iter()
            .find(|agent| agent["session"] == session)
            .cloned()
            .unwrap_or_else(|| panic!("{session} is not in the snapshot"))
    };

    // The agent starts a helper from a written brief.
    let work = place.root.join("work");
    std::fs::write(
        place.root.join("brief.md"),
        "Count the files here and report.\n",
    )
    .unwrap();
    let (code, out, err) = run(&[
        "start",
        "counter",
        work.to_str().unwrap(),
        "brief.md",
        "--clarp-agent",
        "--title",
        "Counting",
    ]);
    assert_eq!(code, 0, "{out}{err}");
    assert!(
        out.contains("started helper counter (parent=maker)"),
        "{out}{err}"
    );
    let made = lineage("counter");
    assert_eq!(
        (
            &made["role"],
            &made["helper_state"],
            &made["parent_agent_id"]
        ),
        (
            &json!("helper"),
            &json!("running"),
            &row(&maker)["agent_id"]
        )
    );
    assert_eq!(
        row("counter")["cwd"],
        json!(work.canonicalize().unwrap().display().to_string())
    );
    assert_eq!(row("counter")["model"], "claude-opus-5-5");
    // It was handed its brief as a message from its maker, and worked on it.
    wait_for("the helper's answer", || {
        host.conversation("counter").len() == 2 && host.state("counter") == "done"
    });
    let rows = host.turns("counter");
    assert_eq!(rows[0]["origin"], "agent");
    assert_eq!(rows[0]["sender_session"], "maker");
    let brief = rows[0]["text"].as_str().unwrap();
    assert!(
        brief
            .starts_with("You are the Clarp helper agent `counter` (counter), working for `maker`"),
        "{brief}"
    );
    assert!(brief.contains("Count the files here and report."));
    assert!(brief.contains("clarp-admin prompt --to maker --from counter"));
    assert_eq!(rows[1]["text"], "on it");
    assert_eq!(
        std::fs::read_to_string(kept.join("counter.session")).unwrap(),
        "counter\n"
    );

    // The program's watcher holds a job on the maker until the helper has reported. The job
    // only mirrors the helper: the maker's row counts one sub-agent and no process.
    let (code, handle, err) = tool(
        &host,
        &place.root,
        None,
        &[
            "clarp-agent-bg",
            &maker,
            "job-upsert",
            "sub-agent-counter",
            "sub-agent",
            "Counting",
            "counter",
        ],
    );
    assert_eq!(code, 0, "{err}");
    let active = tool(
        &host,
        &place.root,
        None,
        &["clarp-agent-bg", &maker, "job-active", handle.trim()],
    );
    assert_eq!(active.0, 0, "{}", active.2);
    wait_for("the maker's row to count its helper once", || {
        row(&maker)["background_jobs"] == json!({ "count": 0, "sub_agents": 1 })
    });
    assert_eq!(row(&maker)["latest_state"], "background");

    // The helper reports, as its brief told it to. The watcher then closes its job, from a
    // process no turn started: it finds the Host by the installation's file.
    let (code, _, err) = tool(
        &host,
        &place.root,
        None,
        &[
            "clarp-admin",
            "prompt",
            "--to",
            &maker,
            "--from",
            "counter",
            "--text",
            "There are no files here.",
        ],
    );
    assert_eq!(code, 0, "{err}");
    assert_eq!(lineage("counter")["helper_state"], "reported");
    let (code, out, err) = run(&["__watch", "counter", "counter", &maker]);
    assert_eq!(code, 0, "{out}{err}");
    assert_eq!(
        std::fs::read_to_string(kept.join("counter.exit")).unwrap(),
        "0\n"
    );
    let job = host.get("/background-jobs")["jobs"]
        .as_array()
        .unwrap()
        .iter()
        .find(|job| job["job_id"] == "sub-agent-counter")
        .cloned()
        .unwrap_or(Value::Null);
    assert_eq!(job["status"], "succeeded", "{job}");
    wait_for("the maker to be at rest", || {
        row(&maker)["background_jobs"] == json!({ "count": 0, "sub_agents": 0 })
            && host.state(&maker) == "done"
    });

    // The same name again takes the helper up again instead of making a second one.
    let (code, out, err) = run(&[
        "start",
        "counter",
        work.to_str().unwrap(),
        "brief.md",
        "--clarp-agent",
    ]);
    assert_eq!(code, 0, "{out}{err}");
    assert_eq!(lineage("counter")["helper_state"], "running");
    assert_eq!(row(&maker)["child_count"], 1);
}

/// One answer as it came: its status, its headers under their names in small letters, its
/// bytes.
struct Fetched {
    status: u16,
    headers: Vec<(String, String)>,
    body: Vec<u8>,
}

impl Fetched {
    fn header(&self, name: &str) -> &str {
        let named = self.headers.iter().find(|(key, _)| key == name);
        named.map_or("", |(_, value)| value.as_str())
    }

    fn text(&self) -> String {
        String::from_utf8_lossy(&self.body).into_owned()
    }
}

/// One request with headers of its own and a body of bytes, answered in bytes.
fn fetch(
    host: &Host,
    token: &str,
    method: &str,
    path: &str,
    headers: &[(&str, &str)],
    body: &[u8],
) -> Fetched {
    let mut stream = TcpStream::connect(&host.address).unwrap();
    stream
        .set_read_timeout(Some(Duration::from_secs(20)))
        .unwrap();
    // No token: nothing is presented in a header.
    let authorization = if token.is_empty() {
        String::new()
    } else {
        format!("Authorization: Bearer {token}\r\n")
    };
    let mut request = format!(
        "{method} {path} HTTP/1.1\r\nHost: test\r\nConnection: close\r\n{authorization}Content-Length: {}\r\n",
        body.len()
    );
    for (name, value) in headers {
        request.push_str(&format!("{name}: {value}\r\n"));
    }
    request.push_str("\r\n");
    stream.write_all(request.as_bytes()).unwrap();
    stream.write_all(body).unwrap();
    let mut answer = Vec::new();
    stream.read_to_end(&mut answer).unwrap();
    let end = answer
        .windows(4)
        .position(|window| window == b"\r\n\r\n")
        .unwrap_or(answer.len());
    let head = String::from_utf8_lossy(&answer[..end]).into_owned();
    let mut lines = head.lines();
    let status = lines
        .next()
        .and_then(|line| line.split_whitespace().nth(1))
        .and_then(|code| code.parse().ok())
        .unwrap_or_else(|| panic!("no status in: {head}"));
    let headers = lines
        .filter_map(|line| line.split_once(':'))
        .map(|(name, value)| (name.trim().to_lowercase(), value.trim().to_string()))
        .collect();
    Fetched {
        status,
        headers,
        body: answer.get(end + 4..).unwrap_or_default().to_vec(),
    }
}

/// How many files lie under a directory, however deep.
fn files_under(dir: &std::path::Path) -> usize {
    let Ok(entries) = std::fs::read_dir(dir) else {
        return 0;
    };
    entries
        .flatten()
        .map(|entry| {
            if entry.path().is_dir() {
                files_under(&entry.path())
            } else {
                1
            }
        })
        .sum()
}

#[test]
fn an_agent_publishes_a_file_with_its_own_tool_and_the_apps_fetch_it() {
    // `clarp-media-publish` is the repository's own program (scripts/): it reads a file and
    // sends its bytes to the Host. Here it runs as an installation runs it for a turn: told
    // where the Host's door for tools is, with the token of the agent's tools.
    let script = concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../../scripts/clarp-media-publish.py"
    );
    assert!(
        std::path::Path::new(script).is_file(),
        "the program is not beside server-rs: {script}"
    );
    let python = on_path("python3").expect("the program needs python3");
    let place = Place::new("media");
    let token = "media-token";
    let host = place.start_with_token(token);
    let maker = host.new_agent("Maker");
    let other = host.new_agent("Other");
    let tools = clarp_host::tool_token(token);

    // What an agent made: an image, notes, and a program.
    let work = place.root.join("work");
    let png: Vec<u8> = [
        &b"\x89PNG\r\n\x1a\n"[..],
        &13u32.to_be_bytes(),
        b"IHDR",
        &640u32.to_be_bytes(),
        &480u32.to_be_bytes(),
        &[8, 6, 0, 0, 0],
    ]
    .concat();
    std::fs::write(work.join("My chart.png"), &png).unwrap();
    std::fs::write(work.join("notes.md"), "# Notes\n\nSome *text*.\n").unwrap();
    std::fs::write(work.join("run.sh"), "#!/bin/sh\necho hello\n").unwrap();
    let door = format!("http://{}/tools", host.address);
    let run = |world: &[(String, String)], args: &[&str]| -> (i32, String, String) {
        let out = Command::new(&python)
            .arg(script)
            .args(args)
            .current_dir(&work)
            .env_clear()
            .env("HOME", &place.root)
            .envs(world.iter().map(|(name, value)| (name, value)))
            .output()
            .unwrap();
        (
            out.status.code().unwrap_or(-1),
            String::from_utf8_lossy(&out.stdout).into_owned(),
            String::from_utf8_lossy(&out.stderr).into_owned(),
        )
    };
    // What an installation's wrapper gives the program in a turn of this agent.
    let in_a_turn = |session: &str| -> [(String, String); 3] {
        [
            ("CLAUDE_PWA_SESSION".to_string(), session.to_string()),
            ("CLAUDE_PWA_BASE_URL".to_string(), door.clone()),
            ("CLAUDE_PWA_TOKEN".to_string(), tools.clone()),
        ]
    };

    // An image is answered with what the agent writes into its reply to show it.
    let (code, out, err) = run(
        &in_a_turn(&maker),
        &["My chart.png", "--caption", "A chart [one]"],
    );
    assert_eq!(code, 0, "{out}{err}");
    let id = out
        .trim()
        .strip_prefix("![A chart \\[one\\]](clarp-media://asset/")
        .and_then(|rest| rest.strip_suffix(')'))
        .unwrap_or_else(|| panic!("not the Markdown of an image: {out}"))
        .to_string();
    assert!(id.starts_with("asset_") && id.len() == 30, "{id}");

    // With --json it prints the whole answer: what the skills for audio, video and files
    // read. A file that is no image is linked, not shown.
    let (code, out, err) = run(&in_a_turn(&maker), &["--json", "notes.md"]);
    assert_eq!(code, 0, "{out}{err}");
    let notes: Value = serde_json::from_str(&out).unwrap();
    let notes_id = notes["asset_id"].as_str().unwrap().to_string();
    assert_eq!(notes["ok"], true);
    assert_eq!(notes["asset"]["asset_id"], notes_id.as_str());
    assert_eq!(
        (&notes["mime_type"], &notes["file_name"], &notes["bytes"]),
        (&json!("text/markdown"), &json!("notes.md"), &json!(22))
    );
    assert_eq!(notes["markdown"], "");
    assert_eq!(notes["url"], format!("/media/{notes_id}"));
    assert_eq!(notes["uri"], format!("clarp-media://asset/{notes_id}"));
    assert_eq!(
        (&notes["width"], &notes["height"]),
        (&Value::Null, &Value::Null)
    );
    assert_eq!(notes["created_by"], "agent");
    assert_eq!(notes["session"], maker.as_str());

    // A program is not taken, and the tool says what the Host said. Nor is anything taken
    // for an agent the Host does not have.
    let (code, _, err) = run(&in_a_turn(&maker), &["run.sh"]);
    assert_eq!(code, 1, "{err}");
    assert!(
        err.contains("server returned 400") && err.contains("unsupported media type"),
        "{err}"
    );
    let (code, _, err) = run(&in_a_turn("nobody"), &["My chart.png"]);
    assert_eq!(code, 1, "{err}");
    assert!(
        err.contains("server returned 404") && err.contains("unknown session"),
        "{err}"
    );

    // The token of an agent's tools opens the door for tools, and not the one beside it.
    let said = [("X-Session", maker.as_str()), ("Content-Type", "image/png")];
    let beside = fetch(&host, &tools, "POST", "/media", &said, &png);
    assert_eq!(beside.status, 403, "{}", beside.text());
    // Nothing at all is no file, at either door.
    let nothing = fetch(&host, token, "POST", "/media", &said, b"");
    assert_eq!(
        (nothing.status, nothing.text().contains("bad size")),
        (400, true)
    );
    let nobody = fetch(
        &host,
        token,
        "POST",
        "/media",
        &[("Content-Type", "image/png")],
        &png,
    );
    assert_eq!(
        (nobody.status, nobody.text().contains("session required")),
        (400, true)
    );

    // A worker an agent left running was told nothing by a turn: it finds its Host in the
    // file an installation wrote, and publishes with what stands there. The same image, for
    // another agent.
    let config = place.root.join("config.toml");
    let (address, port) = host.address.rsplit_once(':').unwrap();
    std::fs::write(
        &config,
        format!(
            "[server]\nkind = \"clarp-host\"\nbind_addr = \"{address}\"\nport = {port}\nauth_token = \"{token}\"\n"
        ),
    )
    .unwrap();
    let left_running = [
        ("CLAUDE_PWA_SESSION".to_string(), other.clone()),
        (
            "CLAUDE_PWA_CONFIG".to_string(),
            config.to_str().unwrap().to_string(),
        ),
    ];
    let (code, out, err) = run(&left_running, &["--json", "My chart.png"]);
    assert_eq!(code, 0, "{out}{err}");
    let again: Value = serde_json::from_str(&out).unwrap();
    assert_ne!(again["asset_id"], id.as_str());
    assert_eq!(again["session"], other.as_str());
    assert_eq!(
        again["markdown"],
        format!(
            "![My_chart.png](clarp-media://asset/{})",
            again["asset_id"].as_str().unwrap()
        )
    );
    // The same bytes published twice are kept once: the image and the notes are two files.
    assert_eq!(files_under(&place.root.join("data").join("media")), 2);

    // What the apps read. An agent's gallery is its images, the newest first: the notes are
    // not in it, and neither is the other agent's image.
    let gallery = |host: &Host, session: &str| -> Vec<Value> {
        let listed = host.get(&format!("/media?session={session}"));
        assert_eq!(listed["session"], session);
        listed["assets"].as_array().unwrap().clone()
    };
    let shown = gallery(&host, &maker);
    assert_eq!(shown.len(), 1, "{shown:?}");
    assert_eq!(shown[0]["asset_id"], id.as_str());
    assert_eq!(
        (
            &shown[0]["source_name"],
            &shown[0]["mime_type"],
            &shown[0]["caption"]
        ),
        (
            &json!("My_chart.png"),
            &json!("image/png"),
            &json!("A chart [one]")
        )
    );
    assert_eq!(
        (&shown[0]["width"], &shown[0]["height"]),
        (&json!(640), &json!(480))
    );
    assert_eq!(shown[0]["bytes"], png.len());
    assert_eq!(gallery(&host, &other).len(), 1);
    assert!(gallery(&host, "nobody").is_empty());

    // The bytes as they were handed in, with what keeps a browser from running anything
    // in them.
    let whole = fetch(&host, token, "GET", &format!("/media/{id}"), &[], b"");
    assert_eq!(whole.status, 200);
    assert!(whole.body == png, "the image came back as it was published");
    assert_eq!(whole.header("content-type"), "image/png");
    assert_eq!(whole.header("content-length"), png.len().to_string());
    assert_eq!(whole.header("accept-ranges"), "bytes");
    assert_eq!(whole.header("x-content-type-options"), "nosniff");
    assert_eq!(
        whole.header("content-security-policy"),
        "sandbox; default-src 'none'"
    );
    assert_eq!(whole.header("cache-control"), "no-store");
    let text = fetch(&host, token, "GET", &format!("/media/{notes_id}"), &[], b"");
    assert_eq!(
        (
            text.status,
            text.header("content-type"),
            text.text().as_str()
        ),
        (200, "text/markdown", "# Notes\n\nSome *text*.\n")
    );

    // A player asks for a part, for the end, and for what is not there.
    let total = png.len();
    let part = fetch(
        &host,
        token,
        "GET",
        &format!("/media/{id}"),
        &[("Range", "bytes=1-3")],
        b"",
    );
    assert_eq!(part.status, 206);
    assert_eq!(part.body, &png[1..=3]);
    assert_eq!(part.header("content-range"), format!("bytes 1-3/{total}"));
    assert_eq!(part.header("content-length"), "3");
    assert_eq!(part.header("accept-ranges"), "bytes");
    let end = fetch(
        &host,
        token,
        "GET",
        &format!("/media/{id}"),
        &[("Range", "bytes=-4")],
        b"",
    );
    assert_eq!(end.status, 206);
    assert_eq!(end.body, &png[total - 4..]);
    assert_eq!(
        end.header("content-range"),
        format!("bytes {}-{}/{total}", total - 4, total - 1)
    );
    let past = fetch(
        &host,
        token,
        "GET",
        &format!("/media/{id}"),
        &[("Range", "bytes=5000-")],
        b"",
    );
    assert_eq!(past.status, 416);
    assert_eq!(past.header("content-range"), format!("bytes */{total}"));
    assert_eq!(past.header("x-content-type-options"), "nosniff");
    assert!(past.body.is_empty());
    // A range nobody can read is no range.
    let odd = fetch(
        &host,
        token,
        "GET",
        &format!("/media/{id}"),
        &[("Range", "bytes=3-1")],
        b"",
    );
    assert_eq!((odd.status, odd.body.len()), (200, total));

    // What is not there, and what is no id.
    let missing = fetch(&host, token, "GET", "/media/asset_nothing", &[], b"");
    assert_eq!(
        (
            missing.status,
            missing.header("content-type"),
            missing.text().as_str()
        ),
        (404, "text/plain", "not found")
    );
    for no_id in ["/media/", "/media/a/b"] {
        let refused = fetch(&host, token, "GET", no_id, &[], b"");
        assert_eq!(
            (refused.status, refused.text().as_str()),
            (400, "bad asset id"),
            "{no_id}"
        );
    }

    // What the skills for recordings, films and files do next: an artifact that names the
    // file, made with what the tool answered. The agent's own artifact tool makes it.
    let payload = json!({
        "url": notes["url"], "mime_type": notes["mime_type"], "file_name": notes["file_name"],
    })
    .to_string();
    let create = |session: &str, kind: &str, payload: &str| {
        let args = [
            "clarp-agent-artifacts",
            "create",
            session,
            kind,
            "The notes",
            "As a file",
            payload,
        ];
        tool(&host, &work, None, &args)
    };
    let (code, out, err) = create(&maker, "file", &payload);
    assert_eq!(code, 0, "{out}{err}");
    let made: Value = serde_json::from_str(&out).unwrap();
    assert_eq!(
        (&made["type"], &made["url"], &made["file_name"]),
        (&json!("file"), &notes["url"], &json!("notes.md"))
    );
    let library = host.get(&format!("/artifacts?session={maker}"));
    assert_eq!(library["artifacts"][0]["artifact_id"], made["artifact_id"]);
    // Notes are no recording; and an agent's artifact names a file of its own, not one
    // another agent published, and not one elsewhere.
    let (code, _, err) = create(&maker, "audio", &payload);
    assert_ne!(code, 0);
    assert!(
        err.contains("audio artifact mime_type must start with audio/"),
        "{err}"
    );
    let (code, _, err) = create(&other, "file", &payload);
    assert_ne!(code, 0);
    assert!(
        err.contains("file artifact media asset not found for this session"),
        "{err}"
    );
    let elsewhere = payload.replace("/media/", "https://example.org/");
    let (code, _, err) = create(&maker, "file", &elsewhere);
    assert_ne!(code, 0);
    assert!(
        err.contains("file artifact url must be a same-origin media path"),
        "{err}"
    );

    // A phone that may only read fetches a file and lists a gallery, and publishes nothing.
    let (phone, _) = pair(&host, "phone", "limited");
    let read = fetch(&host, &phone, "GET", &format!("/media/{id}"), &[], b"");
    assert_eq!((read.status, read.body.len()), (200, total));
    let listed = fetch(
        &host,
        &phone,
        "GET",
        &format!("/media?session={maker}"),
        &[],
        b"",
    );
    assert_eq!(listed.status, 200);
    let refused = fetch(&host, &phone, "POST", "/media", &said, &png);
    assert_eq!(refused.status, 403, "{}", refused.text());
    // And nobody without a token reads anything.
    let unknown = fetch(
        &host,
        "not-a-token",
        "GET",
        &format!("/media/{id}"),
        &[],
        b"",
    );
    assert_eq!(unknown.status, 401);

    // The Host is killed. What was published is there when it is back: what is known of
    // it from the agent's log, its bytes from the disk.
    host.kill();
    let host = place.start_with_token(token);
    let shown = gallery(&host, &maker);
    assert_eq!(shown.len(), 1);
    assert_eq!(shown[0]["asset_id"], id.as_str());
    let whole = fetch(&host, token, "GET", &format!("/media/{id}"), &[], b"");
    assert_eq!(whole.status, 200);
    assert!(
        whole.body == png,
        "the image is the same after the Host was killed"
    );
    let text = fetch(&host, token, "GET", &format!("/media/{notes_id}"), &[], b"");
    assert_eq!(text.text(), "# Notes\n\nSome *text*.\n");
}

#[test]
fn an_agent_hands_its_user_a_report_and_revises_it() {
    use sha2::{Digest, Sha256};
    let place = Place::new("report");
    let token = "report-token";
    let host = place.start_with_token(token);
    let maker = host.new_agent("Maker");
    let other = host.new_agent("Other");
    let work = place.root.join("work");
    let page_one = "<h1>Findings</h1><p>Day one: æøå.</p><script>document.title = 'one'</script>";
    let page_two = "<h1>Findings</h1><p>Day two.</p>";
    std::fs::write(work.join("report.html"), page_one).unwrap();
    let artifacts = |args: &[&str]| {
        let mut all = vec!["clarp-agent-artifacts"];
        all.extend_from_slice(args);
        tool(&host, &work, None, &all)
    };
    let library = |host: &Host, how: &str| -> Vec<Value> {
        let listed = host.get(&format!("/artifacts?session={maker}{how}"));
        listed["artifacts"].as_array().unwrap().clone()
    };

    // What would be sent, without sending it. A report that is given no id gets one made
    // of its title and its page.
    let (code, out, err) = artifacts(&[
        "create-report",
        &maker,
        "The findings: day 1!",
        "report.html",
        "--dry-run",
    ]);
    assert_eq!(code, 0, "{out}{err}");
    let dry: Value = serde_json::from_str(&out).unwrap();
    assert_eq!(
        (&dry["method"], &dry["path"]),
        (&json!("POST"), &json!("/artifacts"))
    );
    let body = &dry["body"];
    assert_eq!(
        (
            &body["type"],
            &body["payload"]["read_only"],
            &body["payload"]["version"]
        ),
        (&json!("html_form"), &json!(true), &json!("1"))
    );
    let id = body["artifact_id"].as_str().unwrap();
    let digest = id.strip_prefix("report-the-findings-day-1-").unwrap_or("");
    assert!(
        digest.len() == 12 && digest.bytes().all(|byte| byte.is_ascii_hexdigit()),
        "{id}"
    );
    assert!(library(&host, "").is_empty(), "a dry run makes nothing");

    // The report, under an id of the agent's own, with what its page may reach.
    let (code, out, err) = artifacts(&[
        "create-report",
        &maker,
        "The findings",
        "report.html",
        "--summary",
        "Day one",
        "--artifact-id",
        "findings",
        "--connect",
        "https://API.example.org:443",
    ]);
    assert_eq!(code, 0, "{out}{err}");
    let made: Value = serde_json::from_str(&out).unwrap();
    assert_eq!(
        (
            &made["type"],
            &made["version"],
            &made["read_only"],
            &made["summary"]
        ),
        (
            &json!("html_form"),
            &json!("1"),
            &json!(true),
            &json!("Day one")
        )
    );
    assert_eq!(made["content"], page_one);
    assert_eq!(made["connect_origins"], json!(["https://api.example.org"]));
    assert!(
        !out.contains("$page"),
        "nothing of the Host's own keeping is answered: {out}"
    );

    // What the apps read: the report whole, and in a list a summary that names its page
    // instead of carrying it.
    let whole = host.get("/artifacts/findings")["artifact"].clone();
    assert_eq!(whole["content"], page_one);
    assert_eq!(whole["payload"]["content"], page_one);
    assert_eq!(whole["payload"]["answer_schema"]["type"], "object");
    let flat = host.get("/artifacts/findings?representation=flat-v1")["artifact"].clone();
    assert_eq!(flat["content"], page_one);
    assert!(flat.get("payload").is_none());
    assert_eq!(library(&host, "")[0]["content"], page_one);
    let summed = library(&host, "&representation=summary-v1");
    assert!(summed[0].get("content").is_none() && summed[0].get("payload").is_none());
    let digest: String = Sha256::digest(page_one.as_bytes())
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect();
    assert_eq!(
        summed[0]["content_omitted"],
        json!({
            "reason": "summary", "bytes": page_one.len(), "sha256": format!("c1:{digest}"),
            "version": "1", "updated_at": whole["updated_at"],
        })
    );
    for answered in [&whole, &flat, &summed[0]] {
        assert!(answered.get("$page").is_none(), "{answered}");
    }

    // A browser is given the page behind the policy that keeps it from reaching anything
    // the report does not name.
    let page = fetch(&host, token, "GET", "/artifacts/findings/html", &[], b"");
    assert_eq!(page.status, 200, "{}", page.text());
    assert_eq!(page.header("content-type"), "text/html; charset=utf-8");
    let policy = page.header("content-security-policy");
    assert!(
        policy.starts_with("sandbox allow-scripts; default-src 'none';")
            && policy.contains("connect-src https://api.example.org;"),
        "{policy}"
    );
    assert_eq!(page.header("x-content-type-options"), "nosniff");
    assert_eq!(page.header("referrer-policy"), "no-referrer");
    assert_eq!(page.header("cache-control"), "no-store");
    let served = page.text();
    assert!(
        served.starts_with("<!doctype html>") && served.contains(page_one),
        "{served}"
    );
    assert!(served.contains("<summary>Network access</summary>https://api.example.org</details>"));
    // The page's own scripts can read the address it was fetched by: a token there is
    // refused. And a device that may only read and send is given no page that runs.
    let in_the_address = fetch(
        &host,
        "",
        "GET",
        &format!("/artifacts/findings/html?token={token}"),
        &[],
        b"",
    );
    assert_eq!(in_the_address.status, 403, "{}", in_the_address.text());
    assert!(in_the_address.text().contains("not the URL"));
    let (phone, _) = pair(&host, "phone", "limited");
    let kept_out = fetch(&host, &phone, "GET", "/artifacts/findings/html", &[], b"");
    assert_eq!(kept_out.status, 403);
    assert!(kept_out.text().contains("full device access required"));
    assert_eq!(
        fetch(&host, &phone, "GET", "/artifacts/findings", &[], b"").status,
        200
    );
    let (tablet, _) = pair(&host, "tablet", "full");
    assert_eq!(
        fetch(&host, &tablet, "GET", "/artifacts/findings/html", &[], b"").status,
        200
    );
    let nothing = fetch(&host, token, "GET", "/artifacts/no-such/html", &[], b"");
    assert_eq!(nothing.status, 404);

    // The agent revises its report with the same command: the tool finds the version the
    // new one follows.
    std::fs::write(work.join("report.html"), page_two).unwrap();
    let revise = |version: &str| {
        artifacts(&[
            "create-report",
            &maker,
            "The findings",
            "report.html",
            "--summary",
            "Day two",
            "--artifact-id",
            "findings",
            "--version",
            version,
        ])
    };
    let (code, out, err) = revise("2");
    assert_eq!(code, 0, "{out}{err}");
    let revised: Value = serde_json::from_str(&out).unwrap();
    assert_eq!(
        (
            &revised["version"],
            &revised["summary"],
            &revised["content"]
        ),
        (&json!("2"), &json!("Day two"), &json!(page_two))
    );
    // What it may reach was not said this time: the new version reaches nothing.
    assert!(revised.get("connect_origins").is_none(), "{revised}");
    // The same again is answered with the report as it is.
    let (code, out, err) = revise("2");
    assert_eq!(code, 0, "{out}{err}");
    let again: Value = serde_json::from_str(&out).unwrap();
    assert_eq!(again["updated_at"], revised["updated_at"]);
    // Another page under the version it has is refused, and the tool says why.
    std::fs::write(work.join("report.html"), "<p>Another page.</p>").unwrap();
    let (code, _, err) = revise("2");
    assert_eq!(code, 1, "{err}");
    assert!(
        err.contains("409") && err.contains("report version already used for different content"),
        "{err}"
    );
    // And another agent's report is not revised under its id.
    let (code, _, err) = artifacts(&[
        "create-report",
        &other,
        "Theirs",
        "report.html",
        "--artifact-id",
        "findings",
        "--version",
        "9",
    ]);
    assert_eq!(code, 1, "{err}");
    assert!(
        err.contains("does not identify this agent's read-only report"),
        "{err}"
    );
    // A page that is not there, and one that is empty, are said so before anything is asked.
    let (code, _, err) = artifacts(&["create-report", &maker, "Gone", "no-such.html"]);
    assert_eq!(code, 1);
    assert!(
        err.contains("No such file or directory: 'no-such.html'"),
        "{err}"
    );
    std::fs::write(work.join("empty.html"), "  \n").unwrap();
    let (code, _, err) = artifacts(&["create-report", &maker, "Empty", "empty.html"]);
    assert_eq!(
        (code, err.trim()),
        (1, "agent_artifacts: report HTML is empty")
    );

    // What the report was, version by version.
    let (code, out, err) = artifacts(&["report-history", "findings"]);
    assert_eq!(code, 0, "{err}");
    let history: Value = serde_json::from_str(&out).unwrap();
    let versions: Vec<&str> = history["revisions"]
        .as_array()
        .unwrap()
        .iter()
        .map(|revision| revision["version"].as_str().unwrap())
        .collect();
    assert_eq!(versions, ["2", "1"]);
    let (code, out, err) = artifacts(&["report-history", "findings", "--version", "1"]);
    assert_eq!(code, 0, "{err}");
    let first: Value = serde_json::from_str(&out).unwrap();
    assert_eq!(first["revision"]["payload"]["content"], page_one);
    assert_eq!(first["revision"]["summary"], "Day one");
    assert!(!out.contains("$page"), "{out}");
    let second = host.get("/artifacts/findings/revisions?version=2");
    assert_eq!(second["revision"]["payload"]["content"], page_two);
    let (status, none) = host.ask("GET", "/artifacts/findings/revisions?version=7", None);
    assert_eq!(
        (status, &none["error"]),
        (404, &json!("report revision not found"))
    );
    let (status, none) = host.ask("GET", "/artifacts/no-such/revisions", None);
    assert_eq!(
        (status, &none["error"]),
        (404, &json!("read-only report not found"))
    );

    // The two pages lie beside the log, each once. The page that was refused was not kept.
    assert_eq!(files_under(&place.root.join("data").join("media")), 2);

    // The Host is killed. The report is there when it is back: what is known of it from
    // the agent's log, its pages from the disk.
    host.kill();
    let host = place.start_with_token(token);
    let whole = host.get("/artifacts/findings")["artifact"].clone();
    assert_eq!(
        (&whole["version"], &whole["content"]),
        (&json!("2"), &json!(page_two))
    );
    let first = host.get("/artifacts/findings/revisions?version=1");
    assert_eq!(first["revision"]["payload"]["content"], page_one);
    let page = fetch(&host, token, "GET", "/artifacts/findings/html", &[], b"");
    assert!(page.status == 200 && page.text().contains(page_two));
    assert!(
        page.header("content-security-policy")
            .contains("connect-src 'none';")
    );
}
