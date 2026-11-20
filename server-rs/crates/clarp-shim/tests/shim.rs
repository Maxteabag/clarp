//! The shim against a real process: it records, it relays, it stops, and it does not need a server.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::io::{BufRead, BufReader, Read, Write};
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

use clarp_shim::{EXIT_NOT_STARTED, EXIT_TURN_TAKEN, Reply, Request};
use clarp_spool::{Entry, Record, ShimEvent, SpoolReader};

/// The shim as a test starts it with `dir` for its place: nothing of the machine the tests run on
/// is in its environment, by the one rule every test that starts a process keeps
/// (`clarp-sealed`). It starts what it is told by its path, so no program is pinned for it.
fn shim_in(dir: &Path) -> Command {
    std::fs::create_dir_all(dir.join("tmp")).unwrap();
    clarp_sealed::command(
        env!("CARGO_BIN_EXE_clarp-shim"),
        clarp_sealed::sealed(&clarp_sealed::runner(), dir, &[], &[]),
    )
}

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
    let mut command = shim_in(&dir);
    command.args(["--spool", spool.to_str().unwrap()]);
    command.args(["--socket", socket.to_str().unwrap()]);
    if let Some(message) = message {
        let file = dir.join("turn-1.stdin");
        std::fs::write(&file, message).unwrap();
        command.args(["--stdin-file", file.to_str().unwrap()]);
    }
    // The shim's own log, to be read by the tests of the hold.
    let log = std::fs::File::create(dir.join("shim.log")).unwrap();
    let shim = command
        .args(["--", "sh", "-c", script])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(log)
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

/// A shim a test starts gives its tool nothing of the machine the tests run on: the names its
/// tool sees are those of the rule [`shim_in`] keeps, what the shim adds for its hooks, and what
/// `sh` sets of itself; none of cargo's, none of the runner's.
#[test]
fn a_shim_a_test_starts_gives_its_tool_nothing_of_the_machine() {
    let mut running = start("sealed", "env");
    assert_eq!(running.shim.wait().unwrap().code(), Some(0));
    let all = records(&running.spool);
    let names: Vec<&str> = outs(&all)
        .into_iter()
        .filter_map(|line| line.split_once('=').map(|(name, _)| name))
        .collect();
    assert!(names.contains(&"PATH") && names.contains(&"HOME"), "{names:?}");
    let rule = clarp_sealed::sealed(&[], &running.dir, &[], &[]);
    let own = [clarp_shim::SOCKET_ENV, "PWD", "OLDPWD", "SHLVL", "_"];
    let more: Vec<&&str> = names
        .iter()
        .filter(|name| !own.contains(name) && !rule.contains_key(std::ffi::OsStr::new(name)))
        .collect();
    assert!(more.is_empty(), "the tool saw more than the test gave: {more:?}");
    std::fs::remove_dir_all(&running.dir).unwrap();
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
    let status = shim_in(&dir)
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
    let status = shim_in(&first.dir)
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
    let mut shim = shim_in(&dir)
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

#[test]
fn prompt_delivery_is_selected_by_the_shim_flag() {
    let root = std::env::temp_dir().join(format!("clarp-shim-prompt-{}", std::process::id()));
    std::fs::create_dir_all(&root).unwrap();
    for as_file in [false, true] {
        for named in [false, true] {
            let dir = root.join(format!("{as_file}-{named}"));
            std::fs::create_dir_all(&dir).unwrap();
            let input = dir.join("turn input");
            let spool = dir.join("turn.jsonl");
            std::fs::write(&input, "turn bytes").unwrap();
            let mut shim = shim_in(&dir);
            shim.arg("--spool")
                .arg(&spool)
                .arg("--socket")
                .arg(dir.join("s"))
                .arg("--stdin-file")
                .arg(&input);
            if as_file {
                shim.arg("--prompt-as-file");
            }
            if named {
                shim.args(["--named-session", "session"]);
            }
            shim.args([
                "--",
                "python3",
                "-c",
                r#"import json, os, select, sys
fd = os.readlink("/proc/self/fd/0")
data = sys.stdin.read() if fd.startswith("pipe:") and select.select([0], [], [], 1)[0] else ""
print(json.dumps([sys.argv[1:], fd == "/dev/null", data]))
"#,
            ]);
            // These are ordinary child arguments, even for a named session.
            shim.args(["--prompt-file", "/dev/stdin"]);
            assert!(shim.stdout(Stdio::null()).status().unwrap().success());
            let entries = records(&spool);
            let said: serde_json::Value = serde_json::from_str(outs(&entries)[0]).unwrap();
            let mut expected = vec!["--prompt-file", "/dev/stdin"];
            if as_file {
                expected.push(input.to_str().unwrap());
            }
            assert_eq!(
                said,
                serde_json::json!([expected, as_file, if as_file { "" } else { "turn bytes" }])
            );
        }
    }
    std::fs::remove_dir_all(root).unwrap();
}

// Compare the request with the writer's timestamp. The time when the test
// thread next gets scheduled to read the spool is not the publication time.
fn request_time_ms() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_millis() as i64
}

fn assert_fast_end(spool: &Path, requested_ms: i64) {
    let all = records(spool);
    let elapsed = all.iter().find(|r| r.is_final()).unwrap().t - requested_ms;
    assert!(
        (0..500).contains(&elapsed),
        "Exited was written after {elapsed} ms: {all:?}"
    );
}

#[test]
fn a_direct_child_can_abort_before_its_group_is_terminated() {
    for exits in [false, true] {
        let dir = std::env::temp_dir().join(format!("clarp-abort-{}-{exits}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let socket = dir.join("shim.sock");
        let script = r#"import os,signal,sys
def note(s):
    with open("events","a") as f: f.write(s+"\n")
watched = {signal.SIGUSR1, signal.SIGTERM}
# Queue signals before publishing readiness. sigwait consumes even a signal
# that arrived between the readiness write and entering the wait.
signal.pthread_sigmask(signal.SIG_BLOCK, watched)
if os.fork()==0:
    for fd in ((0,1,2) if sys.argv[1]=="true" else (0,1)): os.close(fd)
    with open("stubborn","w") as f: f.write(str(os.getpid()))
    signal.alarm(12)
    while True:
        if signal.sigwait(watched)==signal.SIGUSR1: note("wrong child")
if os.fork()==0:
    note("ready")
    while signal.sigwait(watched)!=signal.SIGTERM: note("wrong child")
    note("group")
    os._exit(0)
while True:
    if signal.sigwait(watched)==signal.SIGTERM:
        signal.pthread_sigmask(signal.SIG_UNBLOCK,{signal.SIGTERM})
        os.kill(os.getpid(),signal.SIGTERM)
    note("direct")
    if sys.argv[1]=="true": os._exit(0)
"#;
        let mut shim = shim_in(&dir)
            .current_dir(&dir)
            .args([
                "--spool",
                "spool/turn.jsonl",
                "--socket",
                "shim.sock",
                "--before-stop",
                "SIGUSR1",
                "1500",
                "--",
                "python3",
                "-c",
                script,
                if exits { "true" } else { "false" },
            ])
            .spawn()
            .unwrap();
        let read = || std::fs::read_to_string(dir.join("events")).unwrap_or_default();
        wait_for("both signal handlers", || {
            read().contains("ready") && dir.join("stubborn").is_file()
        });
        let mut follower = exits.then(|| {
            let mut stream = UnixStream::connect(&socket).unwrap();
            stream
                .set_read_timeout(Some(Duration::from_secs(5)))
                .unwrap();
            stream.write_all(b"{\"op\":\"follow\"}\n").unwrap();
            let mut reader = BufReader::new(stream);
            let mut reply = String::new();
            reader.read_line(&mut reply).unwrap();
            assert!(serde_json::from_str::<Reply>(&reply).unwrap().ok);
            // The first hint confirms the writer has taken ownership of the subscription.
            let mut hint = [0];
            reader.read_exact(&mut hint).unwrap();
            assert_eq!(&hint, b"\n");
            reader
        });
        let start = Instant::now();
        let stop = Request::Stop { grace_ms: 2000 };
        let requested_ms = request_time_ms();
        assert!(ask(&socket, &stop).ok);
        if !exits {
            wait_for("direct abort", || read().contains("direct"));
            assert!(ask(&socket, &stop).ok);
        }
        wait_for("group termination", || read().contains("group"));
        assert_eq!(read(), "ready\ndirect\ngroup\n");
        if exits {
            wait_for("exited in the spool", || {
                records(&dir.join("spool/turn.jsonl")).iter().any(|r| {
                    matches!(
                        r.entry,
                        Entry::Shim(ShimEvent::Exited {
                            code: Some(0),
                            signal: None
                        })
                    )
                })
            });
            assert_fast_end(&dir.join("spool/turn.jsonl"), requested_ms);
            assert!(shim.try_wait().unwrap().is_none());
            let reader = follower.as_mut().unwrap();
            reader
                .get_ref()
                .set_read_timeout(Some(Duration::from_millis(500)))
                .unwrap();
            let mut hints = Vec::new();
            reader
                .read_to_end(&mut hints)
                .expect("the spool writer closes its followers at Exited");
            assert!(
                shim.try_wait().unwrap().is_none(),
                "the group grace is still running"
            );
        } else {
            assert!(
                (Duration::from_millis(1450)..Duration::from_secs(3)).contains(&start.elapsed())
            );
        }
        shim.wait().unwrap();
        let pid = std::fs::read_to_string(dir.join("stubborn")).unwrap();
        wait_for("the remaining descendant to be killed", || {
            let stat = std::fs::read_to_string(format!("/proc/{pid}/stat")).unwrap_or_default();
            stat.is_empty() || stat.contains(") Z ")
        });
        let all = records(&dir.join("spool/turn.jsonl"));
        assert_eq!(all.iter().any(|r| matches!(&r.entry, Entry::Shim(ShimEvent::Stopping{signal}) if signal=="SIGKILL")), !exits);

        assert!(
            matches!(
                all.last().unwrap().entry,
                Entry::Shim(ShimEvent::Exited { .. })
            ),
            "late record: {all:?}"
        );
        assert_eq!(
            all.iter()
                .filter(|r| matches!(r.entry, Entry::Shim(ShimEvent::Exited { .. })))
                .count(),
            1
        );
        assert!(start.elapsed() >= Duration::from_secs(2));
        assert!(start.elapsed() < Duration::from_secs(5));
        std::fs::remove_dir_all(dir).unwrap();
    }
}

/// The leader exits with a status of its own. Its background child keeps stdout,
/// reports TERM without exiting, and only finishes voluntarily on USR2.
#[test]
fn plain_stop_owns_the_group_until_the_end_is_written() {
    for mode in ["running", "already-exited", "unstopped", "fast"] {
        let code = r#"import os,signal,sys
mode=sys.argv[1]
signal.alarm(10)
if mode!="fast" and os.fork()==0:
    signal.alarm(10)
    watched = {signal.SIGTERM,signal.SIGUSR2}
    signal.pthread_sigmask(signal.SIG_BLOCK, watched)
    print("child "+str(os.getpid()),flush=True)
    while signal.sigwait(watched)==signal.SIGTERM:
        print("descendant TERM",flush=True)
    print("background output",flush=True)
    os._exit(0)
print("leader "+str(os.getpid()),flush=True)
if mode in ("unstopped","already-exited"):
    sys.exit(23)
while True: signal.pause()
"#;
        let mut run = start(
            &format!("plain-stop-{mode}"),
            &format!("exec python3 -c '{code}' {mode}"),
        );
        let pid = |who: &str| {
            records(&run.spool).iter().find_map(|r| match &r.entry {
                Entry::Out(line) => line.strip_prefix(who)?.parse::<i32>().ok(),
                _ => None,
            })
        };
        wait_for("owned processes", || {
            pid("leader ").is_some() && (mode == "fast" || pid("child ").is_some())
        });
        let leader = pid("leader ").unwrap();
        if matches!(mode, "unstopped" | "already-exited") {
            wait_for("leader exit without reaping", || {
                std::fs::read_to_string(format!("/proc/{leader}/stat"))
                    .is_ok_and(|s| s.contains(") Z "))
            });
            assert!(!records(&run.spool).iter().any(Record::is_final));
        }
        let began = Instant::now();
        let requested_ms = request_time_ms();
        if mode == "unstopped" {
            nix::sys::signal::kill(
                nix::unistd::Pid::from_raw(pid("child ").unwrap()),
                nix::sys::signal::Signal::SIGUSR2,
            )
            .unwrap();
        } else {
            assert!(ask(&run.socket, &Request::Stop { grace_ms: 1_000 }).ok);
            if mode != "fast" {
                wait_for("descendant TERM", || {
                    outs(&records(&run.spool)).contains(&"descendant TERM")
                });
                assert!(!records(&run.spool).iter().any(Record::is_final));
                assert!(ask(&run.socket, &Request::Stop { grace_ms: 0 }).ok);
            }
        }
        wait_for("Exited after both pipes close", || {
            records(&run.spool).iter().any(Record::is_final)
        });
        if mode == "fast" {
            assert_fast_end(&run.spool, requested_ms);
            assert!(run.shim.try_wait().unwrap().is_none());
        } else if mode != "unstopped" {
            assert!(
                (Duration::from_millis(970)..Duration::from_secs(4)).contains(&began.elapsed())
            );
        }
        let status = run.shim.wait().unwrap();
        let natural = matches!(mode, "unstopped" | "already-exited");
        assert_eq!(status.code(), Some(if natural { 23 } else { 1 }));
        let all = records(&run.spool);
        let signals: Vec<_> = all
            .iter()
            .filter_map(|r| match &r.entry {
                Entry::Shim(ShimEvent::Stopping { signal }) => Some(signal.as_str()),
                _ => None,
            })
            .collect();
        let expected: &[&str] = match mode {
            "unstopped" => &[],
            "fast" => &["SIGTERM"],
            _ => &["SIGTERM", "SIGKILL"],
        };
        assert_eq!(signals, expected);
        assert_eq!(
            outs(&all).contains(&"background output"),
            mode == "unstopped"
        );
        assert_eq!(
            all.last().unwrap().entry,
            Entry::Shim(ShimEvent::Exited {
                code: natural.then_some(23),
                signal: (!natural).then_some(15),
            })
        );
        assert_eq!(all.iter().filter(|r| r.is_final()).count(), 1);
        std::fs::remove_dir_all(run.dir).unwrap();
    }
}

// A hold: the tool stands still, and the answer says how far the spool goes with all it printed.

/// Waits for the shim to end, twenty seconds at most.
fn ends(shim: &mut Child) -> std::process::ExitStatus {
    let deadline = Instant::now() + Duration::from_secs(20);
    loop {
        if let Some(status) = shim.try_wait().unwrap() {
            return status;
        }
        assert!(Instant::now() < deadline, "the shim did not end");
        std::thread::sleep(Duration::from_millis(20));
    }
}

/// The lines the shim wrote in its log of its holds, one a hold.
fn hold_lines(dir: &Path) -> Vec<String> {
    std::fs::read_to_string(dir.join("shim.log"))
        .unwrap_or_default()
        .lines()
        .filter(|line| line.starts_with("clarp-shim: hold "))
        .map(str::to_string)
        .collect()
}

/// A number in a hold's line, as `held_ms=250`; nothing for `-`.
fn hold_ms(line: &str, key: &str) -> Option<u64> {
    line.split(&format!(" {key}="))
        .nth(1)?
        .split(' ')
        .next()?
        .parse()
        .ok()
}

fn hold_end(line: &str) -> &str {
    line.split(" end=").nth(1).unwrap()
}

fn hold(socket: &Path, ms: u64) -> Reply {
    ask(socket, &Request::Hold { ms })
}

fn release(socket: &Path) -> Reply {
    ask(socket, &Request::Release)
}

fn spool_len(spool: &Path) -> u64 {
    std::fs::metadata(spool).unwrap().len()
}

/// The records from a byte offset on.
fn after(spool: &Path, offset: u64) -> Vec<Record> {
    SpoolReader::new(spool).read_from(offset).unwrap().records
}

/// A process's state as `/proc` says it (`T`: stopped), or nothing when it is gone.
fn state(pid: i32) -> Option<char> {
    let stat = std::fs::read_to_string(format!("/proc/{pid}/stat")).ok()?;
    stat[stat.rfind(')')? + 1..]
        .split_whitespace()
        .next()?
        .chars()
        .next()
}

fn gone(pid: i32) -> bool {
    matches!(state(pid), None | Some('Z'))
}

/// Nothing is added to the spool for this long.
fn stays(spool: &Path, len: u64, how_long: Duration) {
    let until = Instant::now() + how_long;
    while Instant::now() < until {
        assert_eq!(
            spool_len(spool),
            len,
            "the spool grew while the tool was held"
        );
        std::thread::sleep(Duration::from_millis(10));
    }
}

/// The number a tool printed after a word, as `child 123`.
fn printed_pid(spool: &Path, who: &str) -> Option<i32> {
    outs(&records(spool))
        .iter()
        .find_map(|line| line.strip_prefix(who)?.trim().parse().ok())
}

#[test]
fn a_hold_counts_everything_printed_before_the_tool_stood_still() {
    // Each burst can fill the pipe. Keep the producer finite even when a planted rule
    // makes the test panic before it can stop the shim.
    let script = r#"exec python3 -c 'import sys,time
for _ in range(120):
    sys.stdout.write("abcdefgh\n"*4096)
    sys.stdout.flush()
    time.sleep(0.025)'"#;
    let mut running = start("hold-count", script);
    wait_for("output", || {
        running.spool.exists() && spool_len(&running.spool) > 100_000
    });
    let reply = hold(&running.socket, 1_000);
    assert!(reply.ok, "{reply:?}");
    let pid = i32::try_from(reply.pid.unwrap()).unwrap();
    let held = reply.held.unwrap();
    assert_eq!(state(pid), Some('T'), "the tool stands still");
    let upto = SpoolReader::new(&running.spool)
        .read_window(0, held.spool, usize::MAX)
        .unwrap();
    assert_eq!(upto.next_offset, held.spool, "the count ends with a record");
    assert!(upto.damaged.is_empty());
    assert!(outs(&upto.records).iter().all(|line| *line == "abcdefgh"));
    assert!(held.partial_out <= 8, "{held:?}");
    assert_eq!(held.partial_err, 0);
    assert!((1..=1_000).contains(&held.left_ms), "{held:?}");
    stays(&running.spool, held.spool, Duration::from_millis(300));

    let let_go = release(&running.socket);
    assert!(let_go.ok);
    assert_eq!(let_go.let_go, Some(true), "this release let it go");
    wait_for("the tool to print again", || {
        spool_len(&running.spool) > held.spool
    });
    assert_ne!(state(pid), Some('T'));
    assert!(ask(&running.socket, &Request::Stop { grace_ms: 1_000 }).ok);
    ends(&mut running.shim);
    let lines = hold_lines(&running.dir);
    assert_eq!(lines.len(), 1, "one line a hold: {lines:?}");
    assert_eq!(hold_end(&lines[0]), "let go by request");
    assert!(hold_ms(&lines[0], "still_ms").is_some() && hold_ms(&lines[0], "drain_ms").is_some());
    assert!(hold_ms(&lines[0], "held_ms").unwrap() >= 300, "{lines:?}");
    // What it printed after it was let go comes after the count, and the line it had begun is the
    // first of it, whole.
    let then = SpoolReader::new(&running.spool)
        .read_window(held.spool, u64::MAX, 3)
        .unwrap();
    assert!(then.damaged.is_empty());
    assert_eq!(outs(&then.records).first(), Some(&"abcdefgh"));
    std::fs::remove_dir_all(&running.dir).unwrap();
}

/// The directory `start` makes for a test of this name.
fn dir_of(name: &str) -> PathBuf {
    std::env::temp_dir().join(format!("clarp-shim-{name}-{}", std::process::id()))
}

#[test]
fn a_half_line_at_the_hold_is_said_and_whole_lines_are_counted() {
    let marker = dir_of("hold-half").join("printed");
    let mut running = start(
        "hold-half",
        &format!(
            "printf 'whole\\n'; printf 'half'; printf 'e' >&2; : > '{}'; exec sleep 300",
            marker.display()
        ),
    );
    wait_for("all it prints", || marker.exists());
    let reply = hold(&running.socket, 2_000);
    assert!(reply.ok, "{reply:?}");
    let held = reply.held.unwrap();
    assert_eq!(held.partial_out, 4, "it had begun to print a line");
    assert_eq!(held.partial_err, 1, "and one on its standard error");
    assert_eq!(held.spool, spool_len(&running.spool));
    let upto = SpoolReader::new(&running.spool)
        .read_window(0, held.spool, usize::MAX)
        .unwrap();
    assert_eq!(outs(&upto.records), ["whole"], "the whole line is counted");
    assert!(ask(&running.socket, &Request::Stop { grace_ms: 500 }).ok);
    ends(&mut running.shim);
    // The half line is recorded when the tool is gone, after the count.
    let then = after(&running.spool, held.spool);
    assert_eq!(outs(&then), ["half"]);
    assert!(then.iter().any(|r| r.entry == Entry::Err("e".to_string())));
    std::fs::remove_dir_all(&running.dir).unwrap();

    // A tool that closed its output after half a line: the half line is a record, and nothing
    // is begun.
    let marker = dir_of("hold-closed").join("closed");
    let mut running = start(
        "hold-closed",
        &format!(
            "printf 'half'; exec >&-; : > '{}'; exec sleep 300",
            marker.display()
        ),
    );
    wait_for("the output to be closed", || marker.exists());
    wait_for("the half line", || {
        outs(&records(&running.spool)).contains(&"half")
    });
    let held = hold(&running.socket, 2_000).held.unwrap();
    assert_eq!(held.partial_out, 0);
    assert_eq!(held.spool, spool_len(&running.spool));
    assert!(ask(&running.socket, &Request::Stop { grace_ms: 500 }).ok);
    ends(&mut running.shim);
    std::fs::remove_dir_all(&running.dir).unwrap();
}

#[test]
fn a_child_of_the_tool_is_held_and_let_go_with_it() {
    let mut running = start(
        "hold-child",
        "sh -c 'echo \"child $$\"; while :; do echo child; sleep 0.01; done' & \
         echo \"leader $$\"; while :; do echo leader; sleep 0.01; done",
    );
    wait_for("both", || {
        printed_pid(&running.spool, "child ").is_some()
            && printed_pid(&running.spool, "leader ").is_some()
    });
    let (child, leader) = (
        printed_pid(&running.spool, "child ").unwrap(),
        printed_pid(&running.spool, "leader ").unwrap(),
    );
    let reply = hold(&running.socket, 2_000);
    assert!(reply.ok, "{reply:?}");
    let held = reply.held.unwrap();
    assert_eq!(state(child), Some('T'), "the child stands still");
    assert_eq!(state(leader), Some('T'), "and the tool");
    stays(&running.spool, held.spool, Duration::from_millis(300));
    assert_eq!(release(&running.socket).let_go, Some(true));
    wait_for("both to print again", || {
        let then = after(&running.spool, held.spool);
        let said = outs(&then);
        said.contains(&"child") && said.contains(&"leader")
    });
    assert_ne!(state(child), Some('T'));
    assert_ne!(state(leader), Some('T'));
    assert!(ask(&running.socket, &Request::Stop { grace_ms: 500 }).ok);
    ends(&mut running.shim);
    std::fs::remove_dir_all(&running.dir).unwrap();
}

#[test]
fn the_bound_lets_the_tool_go_when_the_host_is_gone_and_the_turn_completes_whole() {
    let mut running = start(
        "hold-bound",
        "i=0; while [ $i -lt 200 ]; do echo $i; i=$((i+1)); sleep 0.005; done; echo done",
    );
    wait_for("the counting", || {
        outs(&records(&running.spool)).contains(&"5")
    });
    let pid = i32::try_from(ask(&running.socket, &Request::Ping).pid.unwrap()).unwrap();
    // Sixty seconds are asked, and the connection goes with its answer: nobody lets it go.
    let requested_ms = request_time_ms();
    let reply = hold(&running.socket, 60_000);
    assert!(reply.ok, "{reply:?}");
    let held = reply.held.unwrap();
    assert!(
        (1_500..=2_000).contains(&held.left_ms),
        "the bound is two seconds: {held:?}"
    );
    stays(&running.spool, held.spool, Duration::from_millis(1_400));
    assert_eq!(state(pid), Some('T'), "still held");
    assert_eq!(ends(&mut running.shim).code(), Some(0));
    let all = records(&running.spool);
    let mut expected: Vec<String> = (0..200).map(|i| i.to_string()).collect();
    expected.push("done".to_string());
    assert_eq!(outs(&all), expected, "nothing lost, nothing twice");
    let first_after = after(&running.spool, held.spool)
        .into_iter()
        .find(|r| matches!(r.entry, Entry::Out(_)))
        .unwrap();
    let waited = first_after.t - requested_ms;
    assert!(
        (1_900..3_000).contains(&waited),
        "let go by the bound after {waited} ms"
    );
    assert_eq!(
        all.last().unwrap().entry,
        Entry::Shim(ShimEvent::Exited {
            code: Some(0),
            signal: None
        })
    );
    let lines = hold_lines(&running.dir);
    assert_eq!(lines.len(), 1, "{lines:?}");
    assert_eq!(hold_end(&lines[0]), "let go at the bound");
    let held = hold_ms(&lines[0], "held_ms").unwrap();
    assert!((1_900..2_600).contains(&held), "{lines:?}");
    std::fs::remove_dir_all(&running.dir).unwrap();
}

#[cfg(target_os = "linux")]
#[test]
fn the_hold_retries_when_a_vfork_parent_cannot_stop() {
    let fifo = dir_of("hold-vfork").join("before-exec");
    let script = r#"import os,sys
os.mkfifo(sys.argv[1])
print("spawning",flush=True)
child=os.posix_spawn("/bin/sleep",["sleep","5"],os.environ,
    file_actions=[(os.POSIX_SPAWN_OPEN,3,sys.argv[1],os.O_RDONLY,0)])
print("spawn completed",flush=True)
os.waitpid(child,0)"#;
    let mut running = start(
        "hold-vfork",
        &format!("exec python3 -c '{script}' '{}'", fifo.display()),
    );
    wait_for("the spawn fixture to start", || {
        outs(&records(&running.spool)).contains(&"spawning")
    });
    let parent = i32::try_from(ask(&running.socket, &Request::Ping).pid.unwrap()).unwrap();
    let children = format!("/proc/{parent}/task/{parent}/children");
    let mut child = None;
    wait_for(
        "the child to wait in open and its parent to wait in vfork",
        || {
            child = std::fs::read_to_string(&children)
                .ok()
                .and_then(|pids| pids.split_whitespace().next()?.parse::<i32>().ok());
            state(parent) == Some('D') && child.is_some_and(|pid| state(pid) == Some('S'))
        },
    );
    let child = child.unwrap();
    nix::sys::signal::kill(
        nix::unistd::Pid::from_raw(child),
        nix::sys::signal::Signal::SIGSTOP,
    )
    .unwrap();
    wait_for("the child to stop before exec", || {
        state(child) == Some('T')
    });
    // Opening a Linux FIFO read/write gives the child a writer when it can resume.
    let _writer = std::fs::OpenOptions::new()
        .read(true)
        .write(true)
        .open(&fifo)
        .unwrap();
    assert_eq!(state(parent), Some('D'));
    assert_eq!(state(child), Some('T'));

    let reply = hold(&running.socket, 2_000);
    assert!(reply.ok, "{reply:?}");
    assert_eq!(state(parent), Some('T'));
    assert_eq!(state(child), Some('T'));
    assert_eq!(release(&running.socket).let_go, Some(true));
    wait_for("the parent's spawn to finish", || {
        outs(&records(&running.spool)).contains(&"spawn completed")
    });
    assert!(ask(&running.socket, &Request::Stop { grace_ms: 100 }).ok);
    ends(&mut running.shim);
    std::fs::remove_dir_all(&running.dir).unwrap();
}

#[test]
fn a_tool_ended_while_it_is_held_is_killed_without_running_again() {
    for how in ["stop", "signal"] {
        let name = format!("hold-end-{how}");
        let mut running = start(
            &name,
            "trap 'echo term' TERM; \
             sh -c 'trap \"echo child term\" TERM; echo \"child $$\"; while :; do echo child; sleep 0.01; done' & \
             echo \"leader $$\"; while :; do echo leader; sleep 0.01; done",
        );
        wait_for("both", || {
            printed_pid(&running.spool, "child ").is_some()
                && printed_pid(&running.spool, "leader ").is_some()
        });
        let child = printed_pid(&running.spool, "child ").unwrap();
        let reply = hold(&running.socket, 2_000);
        assert!(reply.ok, "{reply:?}");
        let held = reply.held.unwrap();
        let began = Instant::now();
        if how == "stop" {
            assert!(ask(&running.socket, &Request::Stop { grace_ms: 5_000 }).ok);
        } else {
            let shim = nix::unistd::Pid::from_raw(i32::try_from(running.shim.id()).unwrap());
            nix::sys::signal::kill(shim, nix::sys::signal::Signal::SIGTERM).unwrap();
        }
        let status = ends(&mut running.shim);
        assert!(
            began.elapsed() < Duration::from_secs(2),
            "no grace is waited for: {how}"
        );
        assert_eq!(status.code(), Some(1));
        let then: Vec<Entry> = after(&running.spool, held.spool)
            .into_iter()
            .map(|r| r.entry)
            .collect();
        assert_eq!(
            then,
            [
                Entry::Shim(ShimEvent::Stopping {
                    signal: "SIGKILL".into()
                }),
                Entry::Shim(ShimEvent::Exited {
                    code: None,
                    signal: Some(9)
                }),
            ],
            "nothing more is printed, and the end is a stopped turn's: {how}"
        );
        wait_for("the child to be gone", || gone(child));
        let lines = hold_lines(&running.dir);
        assert_eq!(lines.len(), 1, "{lines:?}");
        assert_eq!(hold_end(&lines[0]), "ended while held");
        std::fs::remove_dir_all(&running.dir).unwrap();
    }
}

#[test]
fn what_is_written_to_a_held_tool_is_read_when_it_is_let_go() {
    let mut running = start("hold-stdin", "while read line; do echo \"got:$line\"; done");
    wait_for("the socket", || running.socket.exists());
    assert!(ask(&running.socket, &Request::Ping).ok);
    let reply = hold(&running.socket, 2_000);
    assert!(reply.ok, "{reply:?}");
    let held = reply.held.unwrap();
    let stdin = Request::Stdin {
        data: "while held\n".to_string(),
    };
    assert!(ask(&running.socket, &stdin).ok, "it is taken");
    stays(&running.spool, held.spool, Duration::from_millis(300));
    assert_eq!(release(&running.socket).let_go, Some(true));
    wait_for("the line to be read", || {
        outs(&after(&running.spool, held.spool)).contains(&"got:while held")
    });
    assert!(ask(&running.socket, &Request::CloseStdin).ok);
    assert_eq!(ends(&mut running.shim).code(), Some(0));
    std::fs::remove_dir_all(&running.dir).unwrap();
}

#[test]
fn a_hold_once_held_is_refused_and_a_release_does_no_harm() {
    let mut running = start("hold-once", "echo up; exec sleep 300");
    wait_for("the tool", || {
        outs(&records(&running.spool)).contains(&"up")
    });
    let pid = i32::try_from(ask(&running.socket, &Request::Ping).pid.unwrap()).unwrap();
    // A release of a tool that is not held lets nothing go and stops nothing.
    let free = release(&running.socket);
    assert!(free.ok);
    assert_eq!(free.let_go, Some(false));
    assert_eq!(free.error, None);
    assert_ne!(state(pid), Some('T'));

    let first = hold(&running.socket, 2_000);
    assert!(first.ok, "{first:?}");
    let second = hold(&running.socket, 2_000);
    assert!(!second.ok);
    assert_eq!(second.held, None);
    assert_eq!(
        second.error.as_deref(),
        Some("the turn has been held once already")
    );
    assert_eq!(state(pid), Some('T'), "still held by the first");
    assert_eq!(release(&running.socket).let_go, Some(true));
    assert_eq!(
        release(&running.socket).let_go,
        Some(false),
        "a second release is no harm"
    );
    wait_for("the tool to run", || state(pid) != Some('T'));
    let third = hold(&running.socket, 2_000);
    assert_eq!(
        third.error.as_deref(),
        Some("the turn has been held once already")
    );
    std::thread::sleep(Duration::from_millis(100));
    assert_ne!(state(pid), Some('T'), "a refused hold signals nothing");
    let lines = hold_lines(&running.dir);
    let endings: Vec<&str> = lines.iter().map(|line| hold_end(line)).collect();
    assert_eq!(
        endings,
        [
            "refused: the turn has been held once already",
            "let go by request",
            "refused: the turn has been held once already"
        ],
        "a line a hold, written when it ends"
    );
    assert_eq!(hold_ms(&lines[0], "held_ms"), None);
    assert!(ask(&running.socket, &Request::Stop { grace_ms: 500 }).ok);
    ends(&mut running.shim);
    std::fs::remove_dir_all(&running.dir).unwrap();
}

#[test]
fn a_hold_whose_bound_is_spent_lets_go_and_counts_as_the_one() {
    let mut running = start("hold-zero", "echo up; exec sleep 300");
    wait_for("the tool", || {
        outs(&records(&running.spool)).contains(&"up")
    });
    let reply = hold(&running.socket, 0);
    assert!(!reply.ok, "{reply:?}");
    assert_eq!(reply.held, None);
    // The bound is spent before the tool is first looked at.
    assert_eq!(
        reply.error.as_deref(),
        Some("the tool did not stand still within the bound")
    );
    let pid = i32::try_from(ask(&running.socket, &Request::Ping).pid.unwrap()).unwrap();
    wait_for("the tool to be let go", || state(pid) != Some('T'));
    assert_eq!(
        hold(&running.socket, 2_000).error.as_deref(),
        Some("the turn has been held once already")
    );
    assert!(ask(&running.socket, &Request::Stop { grace_ms: 500 }).ok);
    ends(&mut running.shim);
    let lines = hold_lines(&running.dir);
    assert_eq!(
        hold_end(&lines[0]),
        "refused: the tool did not stand still within the bound"
    );
    assert_eq!(hold_ms(&lines[0], "still_ms"), None);
    std::fs::remove_dir_all(&running.dir).unwrap();
}

#[test]
fn a_tool_that_is_gone_is_not_held() {
    // The tool ends; what it started keeps its output open, and the shim with it.
    let mut running = start("hold-gone", "sleep 300 & echo \"child $!\"; exit 0");
    wait_for("the child", || {
        printed_pid(&running.spool, "child ").is_some()
    });
    let child = printed_pid(&running.spool, "child ").unwrap();
    let leader = i32::try_from(ask(&running.socket, &Request::Ping).pid.unwrap()).unwrap();
    wait_for("the tool to have ended", || state(leader) == Some('Z'));
    // The shim sees the end a moment after the kernel does.
    wait_for("the hold to be refused", || {
        let reply = hold(&running.socket, 2_000);
        assert!(!reply.ok, "a tool that has ended was held: {reply:?}");
        reply.error.as_deref() == Some("the tool is gone")
    });
    std::thread::sleep(Duration::from_millis(100));
    assert_eq!(state(child), Some('S'), "nothing is signalled");
    assert!(
        hold_lines(&running.dir)
            .iter()
            .all(|l| hold_end(l) == "refused: the tool is gone")
    );
    assert!(ask(&running.socket, &Request::Stop { grace_ms: 500 }).ok);
    ends(&mut running.shim);
    std::fs::remove_dir_all(&running.dir).unwrap();

    // A tool that is being stopped, and takes its time, is not held either.
    let mut running = start(
        "hold-stopping",
        "trap '' TERM; echo up; while :; do sleep 0.05; done",
    );
    wait_for("the tool", || {
        outs(&records(&running.spool)).contains(&"up")
    });
    let leader = i32::try_from(ask(&running.socket, &Request::Ping).pid.unwrap()).unwrap();
    assert!(ask(&running.socket, &Request::Stop { grace_ms: 3_000 }).ok);
    wait_for("the polite signal", || {
        records(&running.spool).iter().any(|r| {
            matches!(&r.entry, Entry::Shim(ShimEvent::Stopping { signal }) if signal == "SIGTERM")
        })
    });
    let reply = hold(&running.socket, 2_000);
    assert!(!reply.ok);
    assert_eq!(reply.error.as_deref(), Some("the tool is being stopped"));
    assert_eq!(
        hold_lines(&running.dir)
            .iter()
            .map(|l| hold_end(l))
            .collect::<Vec<_>>(),
        ["refused: the tool is being stopped"]
    );
    std::thread::sleep(Duration::from_millis(100));
    assert_ne!(state(leader), Some('T'), "nothing is signalled");
    ends(&mut running.shim);
    std::fs::remove_dir_all(&running.dir).unwrap();
}

#[test]
fn a_tool_killed_from_outside_while_it_is_held_leaves_nothing_standing() {
    // The child holds neither pipe: when the tool is killed, the shim's end does not wait for it.
    let mut running = start(
        "hold-outside",
        "sh -c 'echo \"child $$\"; exec >/dev/null 2>&1; exec sleep 300' & \
         echo \"leader $$\"; exec sleep 300",
    );
    wait_for("both", || {
        printed_pid(&running.spool, "child ").is_some()
            && printed_pid(&running.spool, "leader ").is_some()
    });
    let child = printed_pid(&running.spool, "child ").unwrap();
    let leader = printed_pid(&running.spool, "leader ").unwrap();
    wait_for("the child to hold neither pipe", || {
        std::fs::read_link(format!("/proc/{child}/fd/1"))
            .is_ok_and(|to| to == Path::new("/dev/null"))
    });
    assert!(hold(&running.socket, 2_000).ok);
    assert_eq!(state(child), Some('T'));
    nix::sys::signal::kill(
        nix::unistd::Pid::from_raw(leader),
        nix::sys::signal::Signal::SIGKILL,
    )
    .unwrap();
    let began = Instant::now();
    ends(&mut running.shim);
    assert!(
        began.elapsed() < Duration::from_millis(1_500),
        "the shim does not wait for the bound"
    );
    // The shim is gone, and its tool's child was let go before it went.
    std::thread::sleep(Duration::from_millis(100));
    assert_ne!(state(child), Some('T'), "a process left stopped forever");
    let lines = hold_lines(&running.dir);
    assert_eq!(lines.len(), 1, "{lines:?}");
    assert_eq!(hold_end(&lines[0]), "let go when the tool ended");
    // It may be gone by now: what is left of it goes.
    let _ = nix::sys::signal::kill(
        nix::unistd::Pid::from_raw(child),
        nix::sys::signal::Signal::SIGKILL,
    );
    std::fs::remove_dir_all(&running.dir).unwrap();
}

#[test]
fn a_hooks_report_under_way_at_the_hold_is_counted() {
    // The marker proves that the tool sent the complete request. The shim can still be
    // draining an earlier burst or handling the request when the hold arrives.
    let script = r#"exec python3 -c 'import os,socket
os.write(1,b"abcdefgh\n"*1024)
s=socket.socket(socket.AF_UNIX)
s.connect(os.environ["CLARP_SHIM_SOCKET"])
s.sendall(b"{\"op\":\"hook\",\"name\":\"Step\",\"data\":{\"n\":1}}\n")
print("report sent",flush=True)
s.recv(4096)
input()'"#;
    let mut running = start("hold-hook", script);
    wait_for("the tool to send its report", || {
        outs(&records(&running.spool)).contains(&"report sent")
    });
    let reply = hold(&running.socket, 2_000);
    assert!(reply.ok, "{reply:?}");
    let held = reply.held.unwrap();
    let window = SpoolReader::new(&running.spool)
        .read_window(0, held.spool, usize::MAX)
        .unwrap();
    let reports = window
        .records
        .iter()
        .filter(|r| matches!(&r.entry, Entry::Hook { name, .. } if name == "Step"))
        .count();
    assert_eq!(reports, 1);
    assert_eq!(held.partial_reports, 0);
    // A report taken before the count and recorded after it would grow the spool here.
    stays(&running.spool, held.spool, Duration::from_millis(300));
    assert_eq!(release(&running.socket).let_go, Some(true));
    assert!(ask(&running.socket, &Request::Stop { grace_ms: 500 }).ok);
    ends(&mut running.shim);
    std::fs::remove_dir_all(&running.dir).unwrap();
}

#[test]
fn a_served_tool_is_not_held() {
    // The shim's own OpenCode driver with a stand-in server that never says where it listens.
    let dir = dir_of("hold-served");
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let server = dir.join("stand-in-server");
    std::fs::write(
        &server,
        "#!/bin/sh\necho \"server $$\" >&2\nexec sleep 300\n",
    )
    .unwrap();
    std::fs::set_permissions(&server, std::os::unix::fs::PermissionsExt::from_mode(0o755)).unwrap();
    let message = dir.join("turn-1.stdin");
    std::fs::write(
        &message,
        r#"{"model":"","effort":"","prompt":"hi","session":""}"#,
    )
    .unwrap();
    let (spool, socket) = (dir.join("spool/turn-1.jsonl"), dir.join("shim.sock"));
    let shim_path = env!("CARGO_BIN_EXE_clarp-shim");
    let mut shim = Command::new(shim_path)
        .args(["--spool", spool.to_str().unwrap()])
        .args(["--socket", socket.to_str().unwrap()])
        .args(["--stdin-file", message.to_str().unwrap()])
        .args(["--", shim_path, "opencode-turn", server.to_str().unwrap()])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(std::fs::File::create(dir.join("shim.log")).unwrap())
        .spawn()
        .unwrap();
    let server_pid = || {
        records(&spool).iter().find_map(|r| match &r.entry {
            Entry::Err(line) => line.strip_prefix("server ")?.parse::<i32>().ok(),
            _ => None,
        })
    };
    wait_for("the stand-in server", || server_pid().is_some());
    let driver = i32::try_from(ask(&socket, &Request::Ping).pid.unwrap()).unwrap();
    let reply = hold(&socket, 2_000);
    assert!(!reply.ok);
    assert_eq!(reply.held, None);
    assert_eq!(reply.error.as_deref(), Some("a served tool cannot be held"));
    assert_eq!(
        hold_lines(&dir)
            .iter()
            .map(|l| hold_end(l))
            .collect::<Vec<_>>(),
        ["refused: a served tool cannot be held"]
    );
    std::thread::sleep(Duration::from_millis(100));
    assert_ne!(state(driver), Some('T'), "nothing is signalled");
    assert_ne!(state(server_pid().unwrap()), Some('T'));
    assert!(ask(&socket, &Request::Stop { grace_ms: 500 }).ok);
    ends(&mut shim);
    std::fs::remove_dir_all(&dir).unwrap();
}

#[test]
fn a_request_ends_with_its_line_or_with_the_connection() {
    let mut running = start("requests", "exec sleep 300");
    wait_for("the socket", || running.socket.exists());
    let replies = |sent: &[u8], close: bool| {
        let mut stream = UnixStream::connect(&running.socket).unwrap();
        stream
            .set_read_timeout(Some(Duration::from_secs(5)))
            .unwrap();
        stream.write_all(sent).unwrap();
        if close {
            stream.shutdown(std::net::Shutdown::Write).unwrap();
        }
        let mut reader = BufReader::new(stream);
        let mut said = Vec::new();
        loop {
            let mut line = String::new();
            match reader.read_line(&mut line) {
                Ok(0) | Err(_) => break,
                Ok(_) => said.push(serde_json::from_str::<Reply>(&line).unwrap().ok),
            }
            if !close && said.len() == 2 {
                break;
            }
        }
        said
    };
    // A line may end in a carriage return.
    assert_eq!(replies(b"{\"op\":\"ping\"}\r\n", true), [true]);
    // The last request may end with the connection instead of a line.
    assert_eq!(replies(b"{\"op\":\"ping\"}", true), [true]);
    // Two requests in one write are both answered, without a third to push them.
    assert_eq!(
        replies(b"{\"op\":\"ping\"}\n{\"op\":\"ping\"}\n", false),
        [true, true]
    );
    assert!(ask(&running.socket, &Request::Stop { grace_ms: 500 }).ok);
    ends(&mut running.shim);
    std::fs::remove_dir_all(&running.dir).unwrap();
}

#[test]
fn a_hooks_report_begun_at_the_hold_is_said_and_not_waited_for() {
    // A process of the tool sends half a report and stands still with it: it cannot finish.
    let script = r#"exec python3 -c 'import os,socket,time
s=socket.socket(socket.AF_UNIX)
s.connect(os.environ["CLARP_SHIM_SOCKET"])
s.sendall(b"{\"op\":\"hook\",\"name\":\"Step\"")
print("sent",flush=True)
time.sleep(300)'"#;
    let mut running = start("hold-report-begun", script);
    wait_for("half a report", || {
        outs(&records(&running.spool)).contains(&"sent")
    });
    let began = Instant::now();
    let reply = hold(&running.socket, 2_000);
    assert!(reply.ok, "{reply:?}");
    assert!(
        began.elapsed() < Duration::from_secs(1),
        "the half report does not hold the count up"
    );
    let held = reply.held.unwrap();
    assert_eq!(held.partial_reports, 1, "it had begun to act");
    assert_eq!((held.partial_out, held.partial_err), (0, 0));
    assert!(held.left_ms > 1_000);
    assert_eq!(release(&running.socket).let_go, Some(true));
    assert!(ask(&running.socket, &Request::Stop { grace_ms: 500 }).ok);
    ends(&mut running.shim);
    std::fs::remove_dir_all(&running.dir).unwrap();
}
