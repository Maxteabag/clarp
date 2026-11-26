//! An agent's Codex lane runs its chat turns: it starts a new conversation's thread, takes
//! steered words into a running turn and says when they were read, ends a turn that does not
//! answer a Stop, and leaves by itself when it has been idle for its time.

#![allow(clippy::unwrap_used)]

use std::fs;
use std::io::{BufRead, BufReader, Write};
use std::os::unix::fs::PermissionsExt;
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, ExitStatus, Stdio};
use std::time::{Duration, Instant};

use clarp_shim::codex_lane::{Claim, Request, thread_lock};
use clarp_spool::{Entry, ShimEvent, SpoolReader};
use serde_json::{Value, json};

struct Lane {
    root: PathBuf,
    socket: PathBuf,
    child: Child,
}

impl Drop for Lane {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
        let _ = fs::remove_dir_all(&self.root);
    }
}

impl Lane {
    fn start(name: &str, fake: &str, options: impl FnOnce(&Path) -> Value) -> Self {
        let root = std::env::temp_dir().join(format!("clarp-{name}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir(&root).unwrap();
        let program = root.join("codex");
        fs::write(&program, fake).unwrap();
        fs::set_permissions(&program, fs::Permissions::from_mode(0o700)).unwrap();
        let file = root.join("options.json");
        fs::write(&file, options(&root).to_string()).unwrap();
        let socket = root.join("lane.sock");
        let mut child = Command::new(env!("CARGO_BIN_EXE_clarp-shim"))
            .env_clear()
            .env("PATH", "/usr/bin:/bin")
            .current_dir(&root)
            .arg("codex-goal-lane")
            .arg(&socket)
            .arg(&program)
            .arg(&file)
            .stdout(Stdio::piped())
            .spawn()
            .unwrap();
        let mut ready = String::new();
        BufReader::new(child.stdout.take().unwrap())
            .read_line(&mut ready)
            .unwrap();
        assert_eq!(ready.trim(), "ready");
        Self {
            root,
            socket,
            child,
        }
    }

    fn claim(&self) -> Claim {
        Claim {
            turn: 3,
            attempt: 1,
            spool: self.root.join("turn.jsonl"),
            socket: self.root.join("turn.sock"),
        }
    }

    fn ended(&mut self, within: Duration) -> ExitStatus {
        let until = Instant::now() + within;
        loop {
            if let Some(status) = self.child.try_wait().unwrap() {
                return status;
            }
            assert!(Instant::now() < until, "the lane did not leave");
            std::thread::sleep(Duration::from_millis(50));
        }
    }
}

fn ask(socket: &Path, request: &impl serde::Serialize) -> Value {
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

/// What the spool holds once its turn has ended: Codex's lines, and how the turn ended.
fn finished(spool: &Path) -> (Vec<Value>, (Option<i32>, Option<i32>)) {
    let until = Instant::now() + Duration::from_secs(10);
    loop {
        let records = SpoolReader::new(spool.to_owned())
            .read_from(0)
            .unwrap()
            .records;
        if let Some(Entry::Shim(ShimEvent::Exited { code, signal })) =
            records.last().map(|record| &record.entry)
        {
            let output = records
                .iter()
                .filter_map(|record| match &record.entry {
                    Entry::Out(line) => Some(serde_json::from_str(line).unwrap()),
                    _ => None,
                })
                .collect();
            return (output, (*code, *signal));
        }
        assert!(Instant::now() < until, "the turn did not end");
        std::thread::sleep(Duration::from_millis(50));
    }
}

#[test]
fn a_new_conversation_is_started_steered_and_its_readings_kept() {
    let fake = r#"#!/bin/sh
set -eu
read -r init
case "$init" in (*'"method":"initialize"'*) ;; (*) exit 1 ;; esac
printf '%s\n' '{"id":1,"result":{}}'
read -r initialized
read -r start
case "$start" in (*'"method":"thread/start"'*) ;; (*) exit 1 ;; esac
case "$start" in (*'"threadId"'*|*_clarp*) exit 1 ;; (*) ;; esac
printf '%s\n' '{"id":2,"result":{"thread":{"id":"new-thread"}}}'
read -r turn
case "$turn" in (*'"method":"turn/start"'*'"effort":"high"'*) ;; (*) exit 1 ;; esac
printf '%s\n' \
    '{"method":"turn/started","params":{"threadId":"new-thread","turn":{"id":"t1"}}}' \
    '{"id":3,"result":{"turn":{"id":"t1"}}}' \
    '{"method":"item/started","params":{"threadId":"new-thread","turnId":"t1","item":{"type":"userMessage","id":"own","content":[]}}}'
read -r steer
case "$steer" in (*'"method":"turn/steer"'*'"clientUserMessageId":"s-1"'*) ;; (*) exit 1 ;; esac
case "$steer" in (*'"expectedTurnId":"t1"'*) ;; (*) exit 1 ;; esac
printf '%s\n' \
    '{"id":4,"result":{"turnId":"t1"}}' \
    '{"method":"item/started","params":{"threadId":"new-thread","turnId":"t1","item":{"type":"userMessage","id":"u1","clientId":"s-1","content":[]}}}' \
    '{"method":"item/completed","params":{"threadId":"new-thread","turnId":"t1","item":{"type":"userMessage","id":"u1","clientId":"s-1","content":[]}}}'
read -r steer
case "$steer" in (*'"method":"turn/steer"'*'"clientUserMessageId":"s-2"'*) ;; (*) exit 1 ;; esac
printf '%s\n' \
    '{"id":5,"result":{"turnId":"t1"}}' \
    '{"method":"item/completed","params":{"threadId":"new-thread","turnId":"t1","item":{"type":"agentMessage","id":"a1","text":"done"}}}' \
    '{"method":"turn/completed","params":{"threadId":"new-thread","turn":{"id":"t1","status":"completed"}}}'
read -r listed
case "$listed" in (*'"method":"thread/turns/list"'*) ;; (*) exit 1 ;; esac
printf '%s\n' '{"id":6,"result":{"data":[{"id":"t1","items":[{"type":"userMessage","clientId":"s-2"},{"type":"userMessage","clientId":"never-sent"}]}]}}'
read -r clean
case "$clean" in (*'"method":"thread/backgroundTerminals/clean"'*) ;; (*) exit 1 ;; esac
printf '%s\n' '{"id":7,"result":{}}'
# The lane leaves in order after its idle time: it asks once more first.
read -r leaving
case "$leaving" in (*'"method":"thread/backgroundTerminals/clean"'*) ;; (*) exit 1 ;; esac
touch "$(dirname "$0")/cleaned-on-leaving"
printf '%s\n' '{"id":8,"result":{}}'
read -r end || :
"#;
    let mut lane = Lane::start("codex-chat-lane", fake, |root| {
        json!({
            "cwd": root,
            "_clarpThreadLockDir": root.join("threads"),
            "_clarpIdleMs": 1500,
            "_clarpTurnFile": root.join("turn.token"),
            "_clarpOptionsDigest": "first-options",
            "model": "chosen",
        })
    });
    let state = ask(&lane.socket, &Request::Inspect);
    assert_eq!(state["state"]["thread"], "");
    assert_eq!(state["state"]["options"], "first-options");
    assert_eq!(state["state"]["model"], "chosen");

    let claim = lane.claim();
    let prompt = Request::Prompt {
        claim: claim.clone(),
        text: "hello".into(),
        model: None,
        effort: Some("high".into()),
        token: Some("turn-token".into()),
    };
    let started = ask(&lane.socket, &prompt);
    assert_eq!(started["ok"], true, "{started}");
    assert_eq!(started["state"]["thread"], "new-thread");
    assert_eq!(started["state"]["native_turn"], "t1");
    assert_eq!(started["state"]["effort"], "high");
    assert_eq!(started["state"]["model"], "chosen");

    let token = lane.root.join("turn.token");
    assert_eq!(fs::read_to_string(&token).unwrap(), "turn-token");
    assert_eq!(
        fs::metadata(&token).unwrap().permissions().mode() & 0o777,
        0o600
    );
    let locked = thread_lock(&lane.root.join("threads"), "new-thread");
    let held = fs::File::open(&locked).unwrap();
    assert!(held.try_lock_shared().is_err(), "the lane holds its thread");

    let steer = |id: &str| {
        ask(
            &claim.socket,
            &clarp_shim::Request::Steer {
                id: id.into(),
                text: "and also this".into(),
            },
        )
    };
    assert_eq!(steer("s-1")["ok"], true);
    assert_eq!(steer("s-2")["ok"], true);

    let (output, exit) = finished(&claim.spool);
    assert_eq!(
        output,
        vec![
            json!({"type":"thread.started","thread_id":"new-thread"}),
            json!({"type":"turn.started"}),
            json!({"type":"steer.open"}),
            // Read as Codex said it, once.
            json!({"type":"steer.read","id":"s-1","confirmed":true}),
            json!({"type":"item.completed","item":{"type":"agent_message","id":"a1","text":"done"}}),
            // Read as the turn's own record says, before its end: only what was sent.
            json!({"type":"steer.read","id":"s-2","confirmed":true}),
            json!({"type":"turn.completed","usage":null}),
        ]
    );
    assert_eq!(exit, (Some(0), None));

    // Idle past its time with nothing that may continue: the lane leaves, and lets go of the
    // thread only after Codex is gone.
    assert!(lane.ended(Duration::from_secs(10)).success());
    assert!(held.try_lock_shared().is_ok());
    assert!(
        lane.root.join("cleaned-on-leaving").exists(),
        "a lane that leaves in order asks Codex to end its terminals first"
    );
}

#[test]
fn a_turn_that_does_not_end_after_a_stop_is_ended_with_its_group_however_much_it_says() {
    let fake = r#"#!/bin/sh
set -eu
read -r init
printf '%s\n' '{"id":1,"result":{}}'
read -r initialized
read -r resume
case "$resume" in (*'"method":"thread/resume"'*'"threadId":"old-thread"'*) ;; (*) exit 1 ;; esac
printf '%s\n' '{"id":2,"result":{"thread":{"id":"old-thread"}}}'
read -r turn
case "$turn" in (*'"method":"turn/start"'*) ;; (*) exit 1 ;; esac
printf '%s\n' \
    '{"id":3,"result":{"turn":{"id":"t9"}}}' \
    '{"method":"turn/started","params":{"threadId":"old-thread","turn":{"id":"t9"}}}'
# Codex runs a command in a group of its own: the kill must find it below Codex.
setsid sleep 60 &
echo $! > left-behind
read -r interrupt
case "$interrupt" in (*'"method":"turn/interrupt"'*'"turnId":"t9"'*) ;; (*) exit 1 ;; esac
printf '%s\n' '{"id":4,"result":{}}'
# It answers, and talks on without ending the turn: the Stop's time still runs out.
while :; do
    printf '%s\n' '{"method":"item/updated","params":{"threadId":"old-thread","turnId":"t9","item":{"type":"reasoning","id":"r"}}}'
    sleep 0.02
done
"#;
    let mut lane = Lane::start("codex-chat-stop", fake, |root| {
        json!({
            "threadId": "old-thread",
            "cwd": root,
            "_clarpThreadLockDir": root.join("threads"),
        })
    });
    let claim = lane.claim();
    let prompt = Request::Prompt {
        claim: claim.clone(),
        text: "work".into(),
        model: None,
        effort: None,
        token: None,
    };
    assert_eq!(ask(&lane.socket, &prompt)["ok"], true);
    assert_eq!(
        ask(
            &claim.socket,
            &clarp_shim::Request::Steer {
                id: String::new(),
                text: "no id".into()
            }
        )["ok"],
        false,
        "steered words need an id"
    );
    let stopped = ask(&claim.socket, &clarp_shim::Request::Stop { grace_ms: 300 });
    assert_eq!(stopped["ok"], true);

    let (_, exit) = finished(&claim.spool);
    assert_eq!(exit, (None, Some(9)), "ended where it stood");
    assert!(!lane.ended(Duration::from_secs(10)).success());
    let left = fs::read_to_string(lane.root.join("left-behind")).unwrap();
    let stat = fs::read_to_string(format!("/proc/{}/stat", left.trim())).unwrap_or_default();
    let state = stat
        .rfind(')')
        .and_then(|end| stat[end + 1..].split_whitespace().next());
    assert!(
        matches!(state, None | Some("Z")),
        "what Codex started went with it"
    );
}

#[test]
fn steered_words_codex_dropped_at_an_interrupt_are_not_read_and_the_turns_commands_end() {
    let fake = r#"#!/bin/sh
set -eu
read -r init
printf '%s\n' '{"id":1,"result":{}}'
read -r initialized
read -r resume
printf '%s\n' '{"id":2,"result":{"thread":{"id":"old-thread"}}}'
read -r turn
printf '%s\n' \
    '{"id":3,"result":{"turn":{"id":"t5"}}}' \
    '{"method":"turn/started","params":{"threadId":"old-thread","turn":{"id":"t5"}}}'
read -r steer
case "$steer" in (*'"method":"turn/steer"'*'"clientUserMessageId":"s-9"'*) ;; (*) exit 1 ;; esac
printf '%s\n' '{"id":4,"result":{"turnId":"t5"}}'
read -r interrupt
case "$interrupt" in (*'"method":"turn/interrupt"'*) ;; (*) exit 1 ;; esac
printf '%s\n' \
    '{"id":5,"result":{}}' \
    '{"method":"turn/completed","params":{"threadId":"old-thread","turn":{"id":"t5","status":"interrupted"}}}'
read -r listed
case "$listed" in (*'"method":"thread/turns/list"'*) ;; (*) exit 1 ;; esac
printf '%s\n' '{"id":6,"result":{"data":[{"id":"t5","items":[{"type":"userMessage","clientId":"someone-else"}]}]}}'
read -r clean
case "$clean" in (*'"method":"thread/backgroundTerminals/clean"'*'"threadId":"old-thread"'*) ;; (*) exit 1 ;; esac
printf '%s\n' '{"id":7,"result":{}}'
read -r end || :
"#;
    let lane = Lane::start(
        "codex-chat-dropped",
        fake,
        |root| json!({"threadId": "old-thread", "cwd": root}),
    );
    let claim = lane.claim();
    let prompt = Request::Prompt {
        claim: claim.clone(),
        text: "work".into(),
        model: None,
        effort: None,
        token: None,
    };
    assert_eq!(ask(&lane.socket, &prompt)["ok"], true);
    let steered = ask(
        &claim.socket,
        &clarp_shim::Request::Steer {
            id: "s-9".into(),
            text: "and this".into(),
        },
    );
    assert_eq!(steered["ok"], true, "Codex accepted the words");
    let stopped = ask(
        &claim.socket,
        &clarp_shim::Request::Stop { grace_ms: 5_000 },
    );
    assert_eq!(stopped["ok"], true);

    let (output, exit) = finished(&claim.spool);
    assert_eq!(
        output,
        vec![
            // A resumed thread is not named again.
            json!({"type":"turn.started"}),
            json!({"type":"steer.open"}),
        ],
        "no reading: the words wait for a turn of their own"
    );
    assert_eq!(exit, (None, Some(2)));
}

#[test]
fn a_lane_whose_login_changed_refuses_with_its_code_and_gives_codex_nothing() {
    let fake = r#"#!/bin/sh
set -eu
read -r init
printf '%s\n' '{"id":1,"result":{}}'
read -r initialized
if read -r anything; then
    echo "$anything" > "$(dirname "$0")/reached-codex"
fi
"#;
    let mut lane = Lane::start("codex-chat-login", fake, |root| {
        fs::write(
            root.join("auth.json"),
            r#"{"tokens":{"account_id":"first"}}"#,
        )
        .unwrap();
        json!({"threadId": "old-thread", "cwd": root, "_clarpLoginFile": root.join("auth.json")})
    });
    fs::write(
        lane.root.join("auth.json"),
        r#"{"tokens":{"account_id":"second"}}"#,
    )
    .unwrap();
    let claim = lane.claim();
    let refused = ask(
        &lane.socket,
        &Request::Prompt {
            claim: claim.clone(),
            text: "work".into(),
            model: None,
            effort: None,
            token: None,
        },
    );
    assert_eq!(refused["ok"], false, "{refused}");
    assert_eq!(refused["code"], clarp_shim::codex_lane::LOGIN_CHANGED);
    assert!(
        lane.ended(Duration::from_secs(10)).success(),
        "the lane leaves"
    );
    assert!(!claim.spool.exists(), "nothing of the attempt was taken");
    assert!(!lane.root.join("reached-codex").exists());
}

#[test]
fn a_stop_ends_the_turns_long_command_and_the_lane_answers_the_next_turn() {
    // Codex 0.167.1 ends the turn on turn/interrupt and leaves the command running as the
    // thread's background terminal; thread/backgroundTerminals/clean ends it (23 Nov 2026).
    // The stand-in does the same.
    let fake = r#"#!/bin/sh
set -eu
here="$(dirname "$0")"
read -r init
printf '%s\n' '{"id":1,"result":{}}'
read -r initialized
read -r resume
printf '%s\n' '{"id":2,"result":{"thread":{"id":"old-thread"}}}'
read -r turn
printf '%s\n' \
    '{"id":3,"result":{"turn":{"id":"t1"}}}' \
    '{"method":"turn/started","params":{"threadId":"old-thread","turn":{"id":"t1"}}}'
setsid sleep 63 &
echo $! > "$here/command"
read -r interrupt
case "$interrupt" in (*'"method":"turn/interrupt"'*'"turnId":"t1"'*) ;; (*) exit 1 ;; esac
printf '%s\n' \
    '{"id":4,"result":{}}' \
    '{"method":"turn/completed","params":{"threadId":"old-thread","turn":{"id":"t1","status":"interrupted"}}}'
read -r clean
case "$clean" in (*'"method":"thread/backgroundTerminals/clean"'*'"threadId":"old-thread"'*) ;; (*) exit 1 ;; esac
kill "$(cat "$here/command")"
printf '%s\n' '{"id":5,"result":{}}'
read -r next
case "$next" in (*'"method":"turn/start"'*'after the stop'*) ;; (*) exit 1 ;; esac
printf '%s\n' \
    '{"id":6,"result":{"turn":{"id":"t2"}}}' \
    '{"method":"turn/started","params":{"threadId":"old-thread","turn":{"id":"t2"}}}' \
    '{"method":"item/completed","params":{"threadId":"old-thread","turnId":"t2","item":{"type":"agentMessage","id":"a2","text":"still here"}}}' \
    '{"method":"turn/completed","params":{"threadId":"old-thread","turn":{"id":"t2","status":"completed"}}}'
read -r end || :
"#;
    let lane = Lane::start(
        "codex-chat-command",
        fake,
        |root| json!({"threadId": "old-thread", "cwd": root}),
    );
    let claim = lane.claim();
    let prompt = |claim: &Claim, text: &str| {
        ask(
            &lane.socket,
            &Request::Prompt {
                claim: claim.clone(),
                text: text.into(),
                model: None,
                effort: None,
                token: None,
            },
        )
    };
    assert_eq!(prompt(&claim, "run something long")["ok"], true);
    let command = lane.root.join("command");
    let until = Instant::now() + Duration::from_secs(5);
    while !command.exists() {
        assert!(Instant::now() < until, "the command did not start");
        std::thread::sleep(Duration::from_millis(20));
    }
    let pid = fs::read_to_string(&command).unwrap().trim().to_owned();

    let grace = Duration::from_millis(2_000);
    let stopped_at = Instant::now();
    let stopped = ask(
        &claim.socket,
        &clarp_shim::Request::Stop {
            grace_ms: u64::try_from(grace.as_millis()).unwrap(),
        },
    );
    assert_eq!(stopped["ok"], true);
    let (_, exit) = finished(&claim.spool);
    assert_eq!(exit, (None, Some(2)), "the turn ended by its interrupt");
    let alive = || {
        let stat = fs::read_to_string(format!("/proc/{pid}/stat")).unwrap_or_default();
        stat.rfind(')')
            .and_then(|end| stat[end + 1..].split_whitespace().next())
            .is_some_and(|state| state != "Z")
    };
    while alive() {
        assert!(
            stopped_at.elapsed() < grace,
            "the stopped turn's command outlived the Stop's grace"
        );
        std::thread::sleep(Duration::from_millis(20));
    }

    let next = Claim {
        turn: 4,
        attempt: 1,
        spool: lane.root.join("next.jsonl"),
        socket: lane.root.join("next.sock"),
    };
    assert_eq!(prompt(&next, "after the stop")["ok"], true);
    let (output, exit) = finished(&next.spool);
    assert!(
        output.contains(&json!({"type":"item.completed","item":{"type":"agent_message","id":"a2","text":"still here"}})),
        "{output:?}"
    );
    assert_eq!(exit, (Some(0), None));
}

#[test]
fn a_completed_turns_commands_end_with_it_and_the_next_turn_runs_on_the_lane() {
    // What Codex keeps for a turn (its background terminals) ends with the turn, however it
    // ended, as with exec (the lead's rule of 23 Nov 2026). The stand-in ends its command on
    // the clean, as Codex 0.167.1 does.
    let fake = r#"#!/bin/sh
set -eu
here="$(dirname "$0")"
read -r init
printf '%s\n' '{"id":1,"result":{}}'
read -r initialized
read -r resume
printf '%s\n' '{"id":2,"result":{"thread":{"id":"old-thread"}}}'
read -r turn
setsid sleep 64 &
echo $! > "$here/command"
printf '%s\n' \
    '{"id":3,"result":{"turn":{"id":"t1"}}}' \
    '{"method":"turn/started","params":{"threadId":"old-thread","turn":{"id":"t1"}}}' \
    '{"method":"item/completed","params":{"threadId":"old-thread","turnId":"t1","item":{"type":"agentMessage","id":"a1","text":"started a server"}}}' \
    '{"method":"turn/completed","params":{"threadId":"old-thread","turn":{"id":"t1","status":"completed"}}}'
read -r clean
case "$clean" in (*'"method":"thread/backgroundTerminals/clean"'*'"threadId":"old-thread"'*) ;; (*) exit 1 ;; esac
kill "$(cat "$here/command")"
printf '%s\n' '{"id":4,"result":{}}'
read -r next
case "$next" in (*'"method":"turn/start"'*'the next one'*) ;; (*) exit 1 ;; esac
printf '%s\n' \
    '{"id":5,"result":{"turn":{"id":"t2"}}}' \
    '{"method":"turn/started","params":{"threadId":"old-thread","turn":{"id":"t2"}}}' \
    '{"method":"item/completed","params":{"threadId":"old-thread","turnId":"t2","item":{"type":"agentMessage","id":"a2","text":"next answered"}}}' \
    '{"method":"turn/completed","params":{"threadId":"old-thread","turn":{"id":"t2","status":"completed"}}}'
read -r clean
case "$clean" in (*'"method":"thread/backgroundTerminals/clean"'*) ;; (*) exit 1 ;; esac
touch "$here/second-clean"
printf '%s\n' '{"id":6,"result":{}}'
read -r end || :
"#;
    let lane = Lane::start(
        "codex-chat-turn-end",
        fake,
        |root| json!({"threadId": "old-thread", "cwd": root}),
    );
    let prompt = |claim: &Claim, text: &str| {
        ask(
            &lane.socket,
            &Request::Prompt {
                claim: claim.clone(),
                text: text.into(),
                model: None,
                effort: None,
                token: None,
            },
        )
    };
    let first = lane.claim();
    assert_eq!(prompt(&first, "start a server")["ok"], true);
    let (_, exit) = finished(&first.spool);
    assert_eq!(exit, (Some(0), None));
    let pid = fs::read_to_string(lane.root.join("command"))
        .unwrap()
        .trim()
        .to_owned();
    let gone = || {
        let stat = fs::read_to_string(format!("/proc/{pid}/stat")).unwrap_or_default();
        !stat
            .rfind(')')
            .and_then(|end| stat[end + 1..].split_whitespace().next())
            .is_some_and(|state| state != "Z")
    };
    let until = Instant::now() + Duration::from_secs(5);
    while !gone() {
        assert!(
            Instant::now() < until,
            "the completed turn's command lived on"
        );
        std::thread::sleep(Duration::from_millis(20));
    }

    let next = Claim {
        turn: 4,
        attempt: 1,
        spool: lane.root.join("next.jsonl"),
        socket: lane.root.join("next.sock"),
    };
    assert_eq!(prompt(&next, "the next one")["ok"], true);
    let (output, exit) = finished(&next.spool);
    assert!(
        output.contains(&json!({"type":"item.completed","item":{"type":"agent_message","id":"a2","text":"next answered"}})),
        "{output:?}"
    );
    assert_eq!(exit, (Some(0), None));
    let until = Instant::now() + Duration::from_secs(5);
    while !lane.root.join("second-clean").exists() {
        assert!(Instant::now() < until, "every turn's end cleans");
        std::thread::sleep(Duration::from_millis(20));
    }
}
