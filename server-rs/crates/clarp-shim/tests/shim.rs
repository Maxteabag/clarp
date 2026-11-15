//! The shim against a real process: it records, it relays, it stops, and it does not need a server.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::io::{BufRead, BufReader, Read, Write};
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

use clarp_shim::{EXIT_NOT_STARTED, EXIT_TURN_TAKEN, Reply, Request};
use clarp_spool::{Entry, Record, ShimEvent, SpoolReader};

struct Running {
    shim: Child,
    spool: PathBuf,
    socket: PathBuf,
    dir: PathBuf,
}

fn start(name: &str, script: &str) -> Running {
    start_with(name, None, script)
}

/// Starts a shim for `sh -c script`, with a message to feed the script when one is given.
fn start_with(name: &str, message: Option<&str>, script: &str) -> Running {
    let dir = std::env::temp_dir().join(format!("clarp-shim-{name}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let (spool, socket) = (dir.join("spool/turn-1.jsonl"), dir.join("shim.sock"));
    let mut command = Command::new(env!("CARGO_BIN_EXE_clarp-shim"));
    command.args(["--spool", spool.to_str().unwrap()]);
    command.args(["--socket", socket.to_str().unwrap()]);
    if let Some(message) = message {
        let file = dir.join("turn-1.stdin");
        std::fs::write(&file, message).unwrap();
        command.args(["--stdin-file", file.to_str().unwrap()]);
    }
    let shim = command
        .args(["--", "sh", "-c", script])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .spawn()
        .unwrap();
    Running {
        shim,
        spool,
        socket,
        dir,
    }
}

fn wait_for(what: &str, mut ready: impl FnMut() -> bool) {
    let deadline = Instant::now() + Duration::from_secs(20);
    while !ready() {
        assert!(Instant::now() < deadline, "timed out waiting for {what}");
        std::thread::sleep(Duration::from_millis(20));
    }
}

fn records(spool: &Path) -> Vec<Record> {
    SpoolReader::new(spool).read_from(0).unwrap().records
}

fn ask(socket: &Path, request: &Request) -> Reply {
    let mut stream = UnixStream::connect(socket).unwrap();
    let mut line = serde_json::to_string(request).unwrap();
    line.push('\n');
    stream.write_all(line.as_bytes()).unwrap();
    let mut reply = String::new();
    BufReader::new(stream).read_line(&mut reply).unwrap();
    serde_json::from_str(&reply).unwrap()
}

fn outs(records: &[Record]) -> Vec<&str> {
    records
        .iter()
        .filter_map(|r| {
            if let Entry::Out(line) = &r.entry {
                Some(line.as_str())
            } else {
                None
            }
        })
        .collect()
}

#[test]
fn it_records_output_relays_input_and_reports_the_end() {
    let mut running = start(
        "io",
        "echo one; echo oops >&2; read line; echo \"got:$line\"; exit 3",
    );
    wait_for("the socket", || running.socket.exists());
    wait_for("the first line", || {
        outs(&records(&running.spool)).contains(&"one")
    });
    let pong = ask(&running.socket, &Request::Ping);
    assert!(pong.ok && pong.pid.is_some());
    assert!(
        ask(
            &running.socket,
            &Request::Stdin {
                data: "hello\n".to_string()
            }
        )
        .ok
    );
    let status = running.shim.wait().unwrap();
    assert_eq!(
        status.code(),
        Some(3),
        "the shim ends with the tool's own code"
    );

    let all = records(&running.spool);
    assert!(
        matches!(
            all.first().unwrap().entry,
            Entry::Shim(ShimEvent::Spawned { .. })
        ),
        "the first record is the spawn"
    );
    assert_eq!(outs(&all), vec!["one", "got:hello"]);
    assert!(
        all.iter()
            .any(|r| r.entry == Entry::Err("oops".to_string()))
    );
    assert_eq!(
        all.last().unwrap().entry,
        Entry::Shim(ShimEvent::Exited {
            code: Some(3),
            signal: None
        }),
        "the last record is the end"
    );
    assert!(!running.socket.exists(), "the socket is gone with the shim");
    std::fs::remove_dir_all(&running.dir).unwrap();
}

#[test]
fn a_stop_ends_the_tool_and_what_it_started() {
    let mut running = start("stop", "sleep 300 & echo started; wait");
    wait_for("the tool to start", || {
        outs(&records(&running.spool)).contains(&"started")
    });
    assert!(ask(&running.socket, &Request::Stop { grace_ms: 2_000 }).ok);
    let began = Instant::now();
    running.shim.wait().unwrap();
    assert!(
        began.elapsed() < Duration::from_secs(15),
        "a stop does not wait for the sleep"
    );
    let all = records(&running.spool);
    assert!(all.iter().any(
        |r| matches!(&r.entry, Entry::Shim(ShimEvent::Stopping { signal }) if signal == "SIGTERM")
    ));
    assert!(matches!(
        all.last().unwrap().entry,
        Entry::Shim(ShimEvent::Exited { .. })
    ));
    std::fs::remove_dir_all(&running.dir).unwrap();
}

#[test]
fn a_hook_reports_through_the_shim_in_order_with_the_output() {
    let hook = env!("CARGO_BIN_EXE_clarp-hook");
    let script =
        format!("echo before; echo '{{\"tool_name\":\"Bash\"}}' | {hook} PreToolUse; echo after");
    let mut running = start("hook", &script);
    running.shim.wait().unwrap();
    let all = records(&running.spool);
    let order: Vec<String> = all
        .iter()
        .filter_map(|r| match &r.entry {
            Entry::Out(line) => Some(line.clone()),
            Entry::Hook { name, data } => Some(format!(
                "hook:{name}:{}",
                data["tool_name"].as_str().unwrap_or("?")
            )),
            _ => None,
        })
        .collect();
    assert_eq!(order, vec!["before", "hook:PreToolUse:Bash", "after"]);
    std::fs::remove_dir_all(&running.dir).unwrap();
}

#[test]
fn every_hook_is_recorded_after_what_the_tool_printed_before_it() {
    // Output and a hook's report reach the shim on two ways. A hundred times in one turn
    // the tool prints and then calls a hook: each report is recorded after the line before
    // it and before the line after it.
    let hook = env!("CARGO_BIN_EXE_clarp-hook");
    let script = format!(
        "i=0; while [ $i -lt 100 ]; do echo \"out $i\"; \
         echo \"{{\\\"n\\\":$i}}\" | {hook} Step; i=$((i+1)); done; echo end"
    );
    let mut running = start("hooks", &script);
    running.shim.wait().unwrap();
    let all = records(&running.spool);
    let order: Vec<String> = all
        .iter()
        .filter_map(|r| match &r.entry {
            Entry::Out(line) => Some(line.clone()),
            Entry::Hook { name, data } => Some(format!("{name} {}", data["n"])),
            _ => None,
        })
        .collect();
    let mut expected: Vec<String> = (0..100)
        .flat_map(|i| [format!("out {i}"), format!("Step {i}")])
        .collect();
    expected.push("end".to_string());
    assert_eq!(order, expected);
    std::fs::remove_dir_all(&running.dir).unwrap();
}

#[test]
fn a_program_that_cannot_start_is_recorded_and_the_shim_says_so() {
    let dir = std::env::temp_dir().join(format!("clarp-shim-nostart-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let (spool, socket) = (dir.join("spool/turn-1.jsonl"), dir.join("shim.sock"));
    let status = Command::new(env!("CARGO_BIN_EXE_clarp-shim"))
        .args([
            "--spool",
            spool.to_str().unwrap(),
            "--socket",
            socket.to_str().unwrap(),
            "--",
            "/no/such/program",
        ])
        .status()
        .unwrap();
    assert_eq!(status.code(), Some(i32::from(EXIT_NOT_STARTED)));
    let all = records(&spool);
    assert!(
        matches!(&all.last().unwrap().entry, Entry::Shim(ShimEvent::SpawnFailed { error }) if error.contains("/no/such/program"))
    );
    assert!(all.last().unwrap().is_final());
    std::fs::remove_dir_all(&dir).unwrap();
}

#[test]
fn the_message_is_given_to_the_tool_after_its_start_is_on_record() {
    let mut running = start_with(
        "message",
        Some("hello there\n"),
        "read line; echo \"got:$line\"",
    );
    assert_eq!(running.shim.wait().unwrap().code(), Some(0));
    let all = records(&running.spool);
    assert!(
        matches!(
            all.first().unwrap().entry,
            Entry::Shim(ShimEvent::Spawned { .. })
        ),
        "the start is the first thing in the spool"
    );
    assert_eq!(
        outs(&all),
        vec!["got:hello there"],
        "and the tool read the message, then the end of its input"
    );
    std::fs::remove_dir_all(&running.dir).unwrap();
}

#[test]
fn a_second_shim_for_the_same_turn_starts_nothing() {
    let mut first = start("taken", "echo once");
    assert_eq!(first.shim.wait().unwrap().code(), Some(0));
    let status = Command::new(env!("CARGO_BIN_EXE_clarp-shim"))
        .args(["--spool", first.spool.to_str().unwrap()])
        .args(["--socket", first.socket.to_str().unwrap()])
        .args(["--", "sh", "-c", "echo twice"])
        .stderr(Stdio::null())
        .status()
        .unwrap();
    assert_eq!(status.code(), Some(i32::from(EXIT_TURN_TAKEN)));
    assert_eq!(
        outs(&records(&first.spool)),
        vec!["once"],
        "the turn ran once"
    );
    std::fs::remove_dir_all(&first.dir).unwrap();
}

#[test]
fn a_follower_is_told_when_the_spool_grows_and_when_the_shim_is_gone() {
    let mut running = start("follow", "read line; echo \"got:$line\"; echo bye");
    wait_for("the socket", || running.socket.exists());
    let mut follower = UnixStream::connect(&running.socket).unwrap();
    follower
        .set_read_timeout(Some(Duration::from_secs(20)))
        .unwrap();
    follower.write_all(b"{\"op\":\"follow\"}\n").unwrap();
    let mut reader = BufReader::new(follower);
    let mut reply = String::new();
    reader.read_line(&mut reply).unwrap();
    assert!(serde_json::from_str::<Reply>(&reply).unwrap().ok);
    // A hint arrives at once: there may be things in the spool from before the follower came.
    let mut hint = [0u8; 1];
    reader.read_exact(&mut hint).unwrap();
    assert_eq!(&hint, b"\n");

    assert!(
        ask(
            &running.socket,
            &Request::Stdin {
                data: "x\n".to_string()
            }
        )
        .ok
    );
    // The rest of the connection is hints and then the end, which is the shim going.
    let mut rest = Vec::new();
    reader.read_to_end(&mut rest).unwrap();
    assert!(
        !rest.is_empty() && rest.iter().all(|byte| *byte == b'\n'),
        "a follower hears nothing but hints"
    );
    assert_eq!(running.shim.wait().unwrap().code(), Some(0));
    assert_eq!(outs(&records(&running.spool)), vec!["got:x", "bye"]);
    std::fs::remove_dir_all(&running.dir).unwrap();
}

#[test]
fn a_shim_that_is_told_to_go_ends_its_tool_first_and_says_so() {
    let mut running = start("sigterm", "sleep 300 & echo started; wait");
    wait_for("the tool to start", || {
        outs(&records(&running.spool)).contains(&"started")
    });
    let shim = nix::unistd::Pid::from_raw(i32::try_from(running.shim.id()).unwrap());
    nix::sys::signal::kill(shim, nix::sys::signal::Signal::SIGTERM).unwrap();
    let began = Instant::now();
    running.shim.wait().unwrap();
    assert!(
        began.elapsed() < Duration::from_secs(15),
        "the shim does not wait for the sleep"
    );
    let all = records(&running.spool);
    assert!(
        all.iter().any(
            |r| matches!(&r.entry, Entry::Shim(ShimEvent::Stopping { signal }) if signal == "SIGTERM")
        ),
        "it told the tool to end"
    );
    assert!(
        matches!(
            all.last().unwrap().entry,
            Entry::Shim(ShimEvent::Exited { .. })
        ),
        "and wrote how the tool went before going itself"
    );
    std::fs::remove_dir_all(&running.dir).unwrap();
}

#[test]
fn a_shim_says_ready_when_the_turn_is_taken_and_the_tool_is_up() {
    let dir = std::env::temp_dir().join(format!("clarp-shim-ready-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let (spool, socket) = (dir.join("spool/turn-1.jsonl"), dir.join("shim.sock"));
    let mut shim = Command::new(env!("CARGO_BIN_EXE_clarp-shim"))
        .args(["--spool", spool.to_str().unwrap()])
        .args(["--socket", socket.to_str().unwrap()])
        .args(["--", "sh", "-c", "read line; echo done"])
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    let mut said = String::new();
    BufReader::new(shim.stdout.take().unwrap())
        .read_line(&mut said)
        .unwrap();
    assert_eq!(said, "ready\n");
    // By then the start is on record and the socket answers.
    assert!(matches!(
        records(&spool).first().unwrap().entry,
        Entry::Shim(ShimEvent::Spawned { .. })
    ));
    assert!(ask(&socket, &Request::CloseStdin).ok);
    assert_eq!(shim.wait().unwrap().code(), Some(0));
    std::fs::remove_dir_all(&dir).unwrap();
}
