#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::fs::File;
use std::io::{BufRead, BufReader, Write};
use std::os::unix::net::UnixListener;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use clarp_shim::lane::{LOGIN_CHANGED, Status, owner_lock};
use serde_json::{Value, json};

use super::{Endpoint, Place, Room, Settings, Tool, make_room, once_more, replied};

const EIGHT: Room = Room {
    most: 8,
    memory: None,
};

fn of(text: &str) -> Settings {
    Settings::of(Some(&text.parse::<toml::Table>().unwrap()))
}

#[test]
fn the_lane_settings_are_off_and_fifteen_minutes_and_eight_unless_the_owner_says() {
    let default = Settings {
        chat: [false],
        idle_ms: 15 * 60_000,
        room: EIGHT,
    };
    assert_eq!(Settings::of(None), default);
    assert_eq!(of(""), default);
    assert!(!default.chat(Tool::Codex));
    let set = of("codex_chat_lane = true\ncodex_lane_idle_minutes = 2\ncodex_lane_most = 3\n");
    assert_eq!(
        set,
        Settings {
            chat: [true],
            idle_ms: 120_000,
            room: Room {
                most: 3,
                memory: None
            }
        }
    );
    assert!(set.chat(Tool::Codex));
    // What is no setting keeps the default: a word, zero or less, or a key inside a table.
    assert_eq!(
        of("codex_chat_lane = \"yes\"\ncodex_lane_idle_minutes = 0\ncodex_lane_most = -1\n"),
        default
    );
    assert_eq!(of("[other]\ncodex_chat_lane = true\n"), default);
    // One memory budget for every lane of the Host, in MiB; none unless the owner sets it.
    assert_eq!(
        of("lane_memory_mb = 2048\n").room,
        Room {
            most: 8,
            memory: Some(2048 * 1024 * 1024)
        }
    );
    assert_eq!(of("lane_memory_mb = 0\n").room, EIGHT);
}

#[test]
fn every_tool_with_a_lane_has_its_own_switch_and_socket_name() {
    for tool in Tool::ALL {
        assert!(tool.switch().ends_with("_chat_lane"), "{tool:?}");
        assert!(tool.suffix().ends_with(".sock"), "{tool:?}");
    }
    assert_eq!(Tool::of("codex"), Some(Tool::Codex));
    assert_eq!(Tool::of("claude"), None);
}

#[test]
fn a_lane_is_named_by_its_agent_and_tool_in_the_run_folder() {
    let run = Path::new("/run/clarp");
    let lane = Endpoint::of(Tool::Codex, run, "a1");
    // The name an installed Host and its running lanes already use.
    assert_eq!(lane.socket(), Path::new("/run/clarp/a1-cxg.sock"));
    assert_eq!(lane.turn_file(run), Path::new("/run/clarp/a1-turn.token"));
    assert_eq!(
        Endpoint::found(PathBuf::from("/run/clarp/a1-cxg.sock")),
        Some(lane)
    );
    assert_eq!(Endpoint::found(PathBuf::from("/run/clarp/a1.sock")), None);
    assert_eq!(
        Endpoint::found(PathBuf::from("/run/clarp/a1-cxg.lock")),
        None
    );
}

/// A lane process as the Host sees it: it answers `inspect` with `status`, holds its owner lock
/// until it is retired, and refuses `retire` while it holds a claim. Every op it was asked is
/// kept, in order.
struct FakeLane {
    endpoint: Endpoint,
    asked: Arc<Mutex<Vec<String>>>,
}

impl FakeLane {
    fn start(run: &Path, agent: &str, status: Value) -> Self {
        let endpoint = Endpoint::of(Tool::Codex, run, agent);
        let listener = UnixListener::bind(endpoint.socket()).unwrap();
        let owner = File::create(owner_lock(endpoint.socket())).unwrap();
        owner.try_lock().unwrap();
        let asked = Arc::new(Mutex::new(Vec::new()));
        let seen = asked.clone();
        let socket = endpoint.socket().to_owned();
        std::thread::spawn(move || {
            let mut owner = Some(owner);
            for stream in listener.incoming() {
                let mut stream = stream.unwrap();
                let mut line = String::new();
                BufReader::new(&stream).read_line(&mut line).unwrap();
                let request: Value = serde_json::from_str(&line).unwrap();
                let op = request["op"].as_str().unwrap().to_owned();
                seen.lock().unwrap().push(op.clone());
                let busy = !status["claim"].is_null();
                let reply = match op.as_str() {
                    "retire" if busy => {
                        json!({"ok": false, "error": "the Codex lane still owns live work"})
                    }
                    "retire" => {
                        // Gone before it says so, as a lane's process is.
                        owner.take();
                        let _ = std::fs::remove_file(&socket);
                        json!({"ok": true, "state": status})
                    }
                    _ => json!({"ok": true, "state": status}),
                };
                let mut frame = reply.to_string();
                frame.push('\n');
                stream.write_all(frame.as_bytes()).unwrap();
                if op == "retire" && !busy {
                    return;
                }
            }
        });
        Self { endpoint, asked }
    }

    fn asked(&self) -> Vec<String> {
        self.asked.lock().unwrap().clone()
    }
}

fn status(thread: &str, options: &str, idle_ms: u64, busy: bool) -> Value {
    status_of(0, thread, options, idle_ms, busy)
}

/// A status whose lane process is `instance`: its memory is that process tree's.
fn status_of(instance: u32, thread: &str, options: &str, idle_ms: u64, busy: bool) -> Value {
    let claim = busy.then(|| json!({"turn": 1, "attempt": 1, "spool": "/s", "socket": "/t"}));
    json!({
        "thread": thread,
        "available": !busy,
        "claim": claim,
        "options": options,
        "model": null,
        "effort": null,
        "idle_ms": idle_ms,
        "instance": instance,
        // A driver's own fields are not the layer's.
        "known_goal": false,
    })
}

fn run_dir(name: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("lane-{name}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

#[test]
fn room_is_made_by_the_longest_idle_lane_and_a_busy_one_counts() {
    let run = run_dir("room");
    let shorter = FakeLane::start(&run, "a", status("ta", "d", 10, false));
    let longer = FakeLane::start(&run, "b", status("tb", "d", 30, false));
    let busy = FakeLane::start(&run, "c", status("tc", "d", 0, true));
    std::fs::write(run.join("d-turn.token"), "not a lane").unwrap();
    let own = Endpoint::of(Tool::Codex, &run, "new");
    // Three live, the new one makes four: one leaves for a cap of three.
    make_room(
        &run,
        own.socket(),
        Room {
            most: 3,
            memory: None,
        },
    );
    assert_eq!(longer.asked(), ["inspect", "retire"]);
    assert_eq!(shorter.asked(), ["inspect"]);
    assert_eq!(busy.asked(), ["inspect"]);
    assert!(longer.endpoint.released().unwrap());
    assert!(!shorter.endpoint.released().unwrap());
    std::fs::remove_dir_all(&run).unwrap();
}

#[test]
fn a_running_lane_is_kept_for_its_thread_and_options_and_otherwise_makes_way() {
    let run = run_dir("place");
    let same = FakeLane::start(&run, "a", status("t", "d", 5, false));
    let place = same.endpoint.place(&run, Some("t"), "d", |_| false, EIGHT);
    assert_eq!(place.unwrap(), Place::Kept);
    assert_eq!(same.asked(), ["inspect"]);

    // What the driver says the tool kept from an earlier turn starts it again.
    let place = same.endpoint.place(&run, Some("t"), "d", |_| true, EIGHT);
    assert_eq!(place.unwrap(), Place::Free);
    assert_eq!(same.asked(), ["inspect", "inspect", "retire"]);

    let other = FakeLane::start(&run, "b", status("t", "old", 5, false));
    let place = other
        .endpoint
        .place(&run, Some("t"), "new", |_| false, EIGHT);
    assert_eq!(place.unwrap(), Place::Free);
    assert_eq!(other.asked(), ["inspect", "retire"]);
    assert!(other.endpoint.released().unwrap());

    // A lane that still runs work is used as it is for its own thread, and refuses another.
    let busy = FakeLane::start(&run, "c", status("t", "old", 0, true));
    let place = busy
        .endpoint
        .place(&run, Some("t"), "new", |_| false, EIGHT);
    assert_eq!(place.unwrap(), Place::Kept);
    let refused = busy
        .endpoint
        .place(&run, Some("u"), "new", |_| false, EIGHT);
    assert_eq!(
        refused.unwrap_err().to_string(),
        "the Codex lane owns another thread"
    );
    std::fs::remove_dir_all(&run).unwrap();
}

#[test]
fn an_attempt_that_reached_nothing_is_offered_once_more() {
    let run = run_dir("again");
    let lane = Endpoint::of(Tool::Codex, &run, "a");
    let spool = run.join("spool");
    let login = || {
        Err::<u8, _>(std::io::Error::new(
            std::io::ErrorKind::PermissionDenied,
            "login",
        ))
    };
    // No spool: nothing reached the tool, and the lane that left is not waited on forever.
    assert_eq!(once_more(login(), &spool, &lane, || Ok(2)).unwrap(), 2);
    assert_eq!(
        once_more(Err(std::io::Error::other("gone")), &spool, &lane, || Ok(2)).unwrap(),
        2
    );
    // A spool: the tool may have it, and it is never offered again.
    std::fs::write(&spool, "").unwrap();
    let kept = once_more(login(), &spool, &lane, || panic!("offered twice"));
    assert_eq!(
        kept.unwrap_err().kind(),
        std::io::ErrorKind::PermissionDenied
    );
    assert_eq!(once_more(Ok(1), &spool, &lane, || Ok(2)).unwrap(), 1);
    std::fs::remove_dir_all(&run).unwrap();
}

#[test]
fn a_changed_login_is_told_by_its_kind_and_a_reply_without_state_by_its_words() {
    let read = |line: Value| {
        let text = format!("{line}\n");
        replied::<Status>(&mut text.as_bytes(), Tool::Codex)
    };
    let changed =
        read(json!({"ok": false, "error": "the Codex login changed", "code": LOGIN_CHANGED}));
    assert_eq!(
        changed.unwrap_err().kind(),
        std::io::ErrorKind::PermissionDenied
    );
    let empty = read(json!({"ok": true})).unwrap_err();
    assert_eq!(empty.kind(), std::io::ErrorKind::Other);
    assert_eq!(empty.to_string(), "Codex lane supplied no state");
    let cut = replied::<Status>(&mut &b"{\"ok\":true"[..], Tool::Codex).unwrap_err();
    assert_eq!(cut.to_string(), "Codex lane connection closed");
}

#[test]
fn one_memory_budget_holds_every_lane_and_the_longest_idle_leaves_first() {
    let run = run_dir("memory");
    // Each says it is the same idle process: what it holds is real, steady, and more than a byte.
    let mut sleeper = std::process::Command::new("sleep")
        .arg("60")
        .spawn()
        .unwrap();
    let me = sleeper.id();
    let shorter = FakeLane::start(&run, "a", status_of(me, "ta", "d", 10, false));
    let longer = FakeLane::start(&run, "b", status_of(me, "tb", "d", 30, false));
    let busy = FakeLane::start(&run, "c", status_of(me, "tc", "d", 0, true));
    let own = Endpoint::of(Tool::Codex, &run, "new");
    let held = Endpoint::memory(&serde_json::from_value(status_of(me, "", "", 0, false)).unwrap())
        .unwrap();
    // Room for all three by their number, and for two of them by their memory: the longest
    // idle leaves, the busy one counts and stays.
    make_room(
        &run,
        own.socket(),
        Room {
            most: 8,
            memory: Some(held * 2 + held / 2),
        },
    );
    assert_eq!(longer.asked(), ["inspect", "retire"]);
    assert_eq!(shorter.asked(), ["inspect"]);
    assert_eq!(busy.asked(), ["inspect"]);
    // No budget: memory is not read, and the number alone decides.
    make_room(&run, own.socket(), EIGHT);
    assert_eq!(shorter.asked(), ["inspect", "inspect"]);
    sleeper.kill().unwrap();
    sleeper.wait().unwrap();
    std::fs::remove_dir_all(&run).unwrap();
}
