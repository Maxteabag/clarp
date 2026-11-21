//! A persistent native-goal process can be reconnected and stopped at its provider.

#![allow(clippy::unwrap_used)]

use std::fs;
use std::io::{BufRead, BufReader, Write};
use std::os::unix::fs::PermissionsExt;
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::time::Duration;

use clarp_shim::codex_lane::{Claim, GoalAction, Request};
use clarp_spool::{Entry, ShimEvent, SpoolReader};
use serde_json::{Value, json};

struct Lane {
    root: PathBuf,
    child: Child,
}

impl Drop for Lane {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
        let _ = fs::remove_dir_all(&self.root);
    }
}

fn ask(socket: &Path, request: &Request) -> Value {
    let mut stream = UnixStream::connect(socket).unwrap();
    stream
        .set_read_timeout(Some(Duration::from_secs(5)))
        .unwrap();
    serde_json::to_writer(&mut stream, request).unwrap();
    stream.write_all(b"\n").unwrap();
    let mut answer = String::new();
    BufReader::new(stream).read_line(&mut answer).unwrap();
    serde_json::from_str(&answer).unwrap()
}

#[test]
fn an_admitted_native_goal_turn_reconnects_and_stop_pauses_before_interrupting() {
    let root = std::env::temp_dir().join(format!("clarp-goal-lane-{}", std::process::id()));
    fs::create_dir(&root).unwrap();
    let fake = root.join("codex");
    fs::write(
        &fake,
        r#"#!/bin/sh
set -eu
read -r init
case "$init" in (*'"method":"initialize"'*) ;; (*) exit 1 ;; esac
printf '%s\n' '{"id":1,"result":{}}'
read -r initialized
read -r resume
case "$resume" in (*'"method":"thread/resume"'*) ;; (*) exit 1 ;; esac
printf '%s\n' '{"id":2,"result":{"thread":{"id":"fixture-thread"}}}'
read -r start
case "$start" in (*'"method":"thread/goal/set"'*'"status":"active"'*) ;; (*) exit 1 ;; esac
printf '%s\n' \
    '{"method":"turn/started","params":{"threadId":"fixture-thread","turn":{"id":"goal-turn"}}}' \
    '{"id":3,"result":{"goal":{"objective":"finish the proof","status":"active"}}}'
read -r pause
case "$pause" in (*'"method":"thread/goal/set"'*'"status":"paused"'*) ;; (*) exit 1 ;; esac
printf '%s\n' '{"id":4,"result":{"goal":{"objective":"finish the proof","status":"paused"}}}'
read -r interrupt
case "$interrupt" in (*'"method":"turn/interrupt"'*'"turnId":"goal-turn"'*) ;; (*) exit 1 ;; esac
printf '%s\n' \
    '{"method":"turn/completed","params":{"threadId":"fixture-thread","turn":{"id":"goal-turn","status":"interrupted"}}}' \
    '{"id":5,"result":{}}'
read -r resume_goal
case "$resume_goal" in (*'"method":"thread/goal/set"'*'"status":"active"'*) ;; (*) exit 1 ;; esac
printf '%s\n' \
    '{"id":6,"result":{"goal":{"objective":"finish the proof","status":"active"}}}' \
    '{"method":"turn/started","params":{"threadId":"fixture-thread","turn":{"id":"second-turn"}}}' \
    '{"method":"turn/completed","params":{"threadId":"fixture-thread","turn":{"id":"second-turn","status":"completed"}}}' \
    '{"method":"turn/started","params":{"threadId":"fixture-thread","turn":{"id":"too-early"}}}' \
    '{"method":"item/completed","params":{"threadId":"fixture-thread","turnId":"too-early","item":{"type":"agentMessage","id":"rogue","text":"must stay out"}}}'
read -r defer
case "$defer" in (*'"method":"thread/goal/set"'*'"status":"paused"'*) ;; (*) exit 1 ;; esac
printf '%s\n' '{"id":7,"result":{"goal":{"objective":"finish the proof","status":"paused"}}}'
read -r reject
case "$reject" in (*'"method":"turn/interrupt"'*'"turnId":"too-early"'*) ;; (*) exit 1 ;; esac
printf '%s\n' \
    '{"method":"turn/completed","params":{"threadId":"fixture-thread","turn":{"id":"too-early","status":"interrupted"}}}' \
    '{"id":8,"result":{}}'
read -r probe
case "$probe" in (*'"method":"thread/goal/get"'*) ;; (*) exit 1 ;; esac
printf '%s\n' '{"id":9,"result":{"goal":{"objective":"finish the proof","status":"paused"}}}'
read -r recover
case "$recover" in (*'"method":"thread/goal/set"'*'"status":"active"'*) ;; (*) exit 1 ;; esac
printf '%s\n' \
    '{"method":"turn/started","params":{"threadId":"fixture-thread","turn":{"id":"third-turn"}}}' \
    '{"id":10,"result":{"goal":{"objective":"finish the proof","status":"active"}}}' \
    '{"method":"thread/goal/updated","params":{"threadId":"fixture-thread","goal":{"objective":"finish the proof","status":"complete"}}}' \
    '{"method":"turn/completed","params":{"threadId":"fixture-thread","turn":{"id":"third-turn","status":"completed"}}}'
read -r clear
case "$clear" in (*'"method":"thread/goal/clear"'*) ;; (*) exit 1 ;; esac
printf '%s\n' '{"id":11,"result":{"cleared":true}}'
read -r end || :
"#,
    )
    .unwrap();
    fs::set_permissions(&fake, fs::Permissions::from_mode(0o700)).unwrap();
    let options = root.join("resume.json");
    let login_path = root.join("login.lock");
    fs::write(&login_path, "").unwrap();
    fs::write(
        &options,
        json!({"threadId":"fixture-thread", "_clarpLoginLock": login_path}).to_string(),
    )
    .unwrap();
    let selector = fs::OpenOptions::new()
        .read(true)
        .write(true)
        .open(login_path)
        .unwrap();
    let socket = root.join("lane.sock");
    let child = Command::new(env!("CARGO_BIN_EXE_clarp-shim"))
        .env_clear()
        .current_dir(&root)
        .arg("codex-goal-lane")
        .arg(&socket)
        .arg(&fake)
        .arg(&options)
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    let mut lane = Lane { root, child };
    let mut ready = String::new();
    BufReader::new(lane.child.stdout.take().unwrap())
        .read_line(&mut ready)
        .unwrap();
    assert_eq!(ready.trim(), "ready");

    let start = Request::Goal {
        action: GoalAction::Start,
        objective: "finish the proof".to_owned(),
    };
    assert_eq!(
        ask(&socket, &start)["ok"],
        false,
        "no actor reservation yet"
    );
    let claim = Claim {
        turn: 7,
        attempt: 1,
        spool: lane.root.join("turn.jsonl"),
        socket: lane.root.join("turn.sock"),
    };
    assert_eq!(
        ask(
            &socket,
            &Request::Reserve {
                claim: claim.clone()
            }
        )["ok"],
        true
    );
    assert_eq!(ask(&socket, &start)["state"]["goal"]["status"], "active");
    let state = ask(&socket, &Request::Inspect);
    assert_eq!(state["state"]["claim"], json!(claim));
    assert_eq!(state["state"]["native_turn"], "goal-turn");
    assert!(matches!(
        selector.try_lock(),
        Err(std::fs::TryLockError::WouldBlock)
    ));

    let stopped = ask(
        &socket,
        &Request::Stop {
            turn: 7,
            attempt: 1,
        },
    );
    assert_eq!(stopped["state"]["goal"]["status"], "paused", "{stopped}");
    assert!(stopped["state"]["claim"].is_null(), "{stopped}");
    let logs = SpoolReader::new(&claim.spool).read_from(0).unwrap().records;
    assert!(matches!(
        logs.last().unwrap().entry,
        Entry::Shim(ShimEvent::Exited {
            code: None,
            signal: Some(_),
        })
    ));

    selector.try_lock().unwrap();
    selector.unlock().unwrap();

    let reserve = |turn| {
        let claim = Claim {
            turn,
            attempt: 1,
            spool: lane.root.join(format!("{turn}.jsonl")),
            socket: lane.root.join(format!("{turn}.sock")),
        };
        let answer = ask(
            &socket,
            &Request::Reserve {
                claim: claim.clone(),
            },
        );
        assert_eq!(answer["ok"], true, "{answer}");
        claim
    };
    let second = reserve(8);
    let resume = ask(
        &socket,
        &Request::Goal {
            action: GoalAction::Resume,
            objective: String::new(),
        },
    );
    assert_eq!(resume["ok"], true, "{resume}");
    let deadline = std::time::Instant::now() + Duration::from_secs(5);
    loop {
        let state = ask(&socket, &Request::Inspect);
        if state["state"]["available"] == true && state["state"]["deferred_goal"] == true {
            assert_eq!(state["state"]["goal"]["status"], "paused");
            break;
        }
        assert!(std::time::Instant::now() < deadline, "{state}");
        std::thread::sleep(Duration::from_millis(10));
    }
    assert!(
        !fs::read_to_string(&second.spool)
            .unwrap()
            .contains("must stay out")
    );
    let third = reserve(9);
    let recovered = ask(&socket, &Request::Recover);
    assert_eq!(
        recovered["state"]["native_turn"], "third-turn",
        "{recovered}"
    );
    let deadline = std::time::Instant::now() + Duration::from_secs(5);
    loop {
        let state = ask(&socket, &Request::Inspect);
        if state["state"]["available"] == true && state["state"]["goal"]["status"] == "complete" {
            break;
        }
        assert!(std::time::Instant::now() < deadline, "{state}");
        std::thread::sleep(Duration::from_millis(10));
    }
    assert!(
        !fs::read_to_string(&third.spool)
            .unwrap()
            .contains("must stay out")
    );

    let clear = ask(
        &socket,
        &Request::Goal {
            action: GoalAction::Clear,
            objective: String::new(),
        },
    );
    assert_eq!(clear["ok"], true, "{clear}");
    assert!(clear["state"]["goal"].is_null(), "{clear}");
    assert_eq!(ask(&socket, &Request::Retire)["ok"], true);
    assert!(lane.child.wait().unwrap().success());
    assert!(!socket.exists());
}
