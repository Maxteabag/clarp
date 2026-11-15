//! The shim against a real process: it records, it relays, it stops, and it does not need a server.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::io::{BufRead, BufReader, Write};
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

use clarp_shim::{Reply, Request};
use clarp_spool::{Entry, Record, ShimEvent, SpoolReader};

struct Running {
    shim: Child,
    spool: PathBuf,
    socket: PathBuf,
    dir: PathBuf,
}

fn start(name: &str, script: &str) -> Running {
    let dir = std::env::temp_dir().join(format!("clarp-shim-{name}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let (spool, socket) = (dir.join("spool/turn-1.jsonl"), dir.join("shim.sock"));
    let shim = Command::new(env!("CARGO_BIN_EXE_clarp-shim"))
        .args([
            "--spool",
            spool.to_str().unwrap(),
            "--socket",
            socket.to_str().unwrap(),
            "--",
            "sh",
            "-c",
            script,
        ])
        .stdin(Stdio::null())
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
    assert_eq!(status.code(), Some(127));
    let all = records(&spool);
    assert!(
        matches!(&all.last().unwrap().entry, Entry::Shim(ShimEvent::SpawnFailed { error }) if error.contains("/no/such/program"))
    );
    assert!(all.last().unwrap().is_final());
    std::fs::remove_dir_all(&dir).unwrap();
}
