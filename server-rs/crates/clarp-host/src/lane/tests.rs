#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::fs::File;
use std::io::{BufRead, BufReader, Write};
use std::os::unix::net::UnixListener;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use clarp_shim::lane::{LOGIN_CHANGED, Status, owner_lock};
use serde_json::{Value, json};

use super::{
    Endpoint, Free, LoginChanged, Memory, Mode, Place, Room, Settings, Tool, held, lanes_in,
    make_room, make_way_within, mark_owners_turn, mark_owners_view, once_more, replied, shed,
};

fn of(text: &str) -> Settings {
    Settings::of(Some(&text.parse::<toml::Table>().unwrap()))
}

const MIB: u64 = 1024 * 1024;
/// What a Codex lane counts at before it has weighed itself.
const CODEX_LANE: u64 = 270 * MIB;
/// Room for everything the tests' lanes hold, by count and by bytes.
const ROOMY: Room = Room {
    most: usize::MAX,
    budget: u64::MAX,
    free: None,
};

/// Only the count limits.
fn counted(most: usize) -> Room {
    Room {
        most,
        budget: u64::MAX,
        free: None,
    }
}

/// Only the budget limits.
fn in_bytes(budget: u64) -> Room {
    Room {
        most: usize::MAX,
        budget,
        free: None,
    }
}

#[test]
fn the_lane_settings_are_off_fifteen_minutes_eight_lanes_and_2200_mb_unless_the_owner_says() {
    let default = Settings {
        chat: [false, false, false, false],
        idle_ms: 15 * 60_000,
        most: 8,
        budget: 2_200 * MIB,
        mode: Mode::Auto,
        reserve_percent: 20,
        ceiling: 24,
        pressure: crate::pressure::Thresholds::DEFAULT,
    };
    assert_eq!(Settings::of(None), default);
    assert_eq!(of(""), default);
    assert!(!default.chat(Tool::Codex));
    assert!(!default.chat(Tool::Claude));
    assert!(!default.chat(Tool::Muse));
    assert!(!default.chat(Tool::OpenCode));
    let set = of(
        "codex_chat_lane = true\ncodex_lane_idle_minutes = 2\ncodex_lane_most = 3\nlane_memory_mb = 3000\n",
    );
    assert_eq!(
        set,
        Settings {
            chat: [true, false, false, false],
            idle_ms: 120_000,
            most: 3,
            budget: 3_000 * MIB,
            ..default
        }
    );
    assert!(set.chat(Tool::Codex));
    // Each tool has its own switch.
    let muse = of("muse_chat_lane = true\n");
    assert!(muse.chat(Tool::Muse) && !muse.chat(Tool::Codex));
    assert!(!set.chat(Tool::OpenCode));
    let opencode = of("opencode_chat_lane = true\n");
    assert!(opencode.chat(Tool::OpenCode) && !opencode.chat(Tool::Codex));
    // Where the machine's memory cannot be read, auto keeps the fixed limits.
    assert_eq!(
        set.room_with(None),
        Room {
            most: 3,
            budget: 3_000 * MIB,
            free: None,
        }
    );
    // Each tool's switch is its own.
    assert!(!set.chat(Tool::Claude));
    let claude = of("claude_chat_lane = true\n");
    assert!(claude.chat(Tool::Claude));
    assert!(!claude.chat(Tool::Codex));
    // What is no setting keeps the default: a word, zero or less, or a key inside a table.
    assert_eq!(
        of(
            "codex_chat_lane = \"yes\"\ncodex_lane_idle_minutes = 0\ncodex_lane_most = -1\nlane_memory_mb = -1\n"
        ),
        default
    );
    assert_eq!(
        of("[other]\ncodex_chat_lane = true\ncodex_lane_most = 2\nlane_memory_mb = 9\n"),
        default
    );
    // Unweighed Codex lanes: the budget alone holds eight, as the count does.
    assert_eq!(default.budget / CODEX_LANE, 8);
}

#[test]
fn every_tool_with_a_lane_has_its_own_switch_and_socket_name() {
    for tool in Tool::ALL {
        assert!(tool.switch().ends_with("_chat_lane"), "{tool:?}");
        assert!(tool.suffix().ends_with(".sock"), "{tool:?}");
    }
    assert_eq!(Tool::of("codex"), Some(Tool::Codex));
    assert_eq!(Tool::of("claude"), Some(Tool::Claude));
    assert_eq!(Tool::of("grok"), None);
    // Two tools' lanes of one agent never share a socket.
    let names: std::collections::BTreeSet<_> = Tool::ALL.map(Tool::suffix).into_iter().collect();
    assert_eq!(names.len(), Tool::ALL.len());
    assert_eq!(Tool::of("muse"), Some(Tool::Muse));
    let run = Path::new("/run/clarp");
    let muse = Endpoint::of(Tool::Muse, run, "a1");
    assert_eq!(muse.socket(), Path::new("/run/clarp/a1-mus.sock"));
    assert_eq!(
        Endpoint::found(PathBuf::from("/run/clarp/a1-mus.sock")),
        Some(muse)
    );
    // DeepSeek runs through OpenCode: one program, one session store, one lane.
    assert_eq!(Tool::of("opencode"), Some(Tool::OpenCode));
    assert_eq!(Tool::of("deepseek"), Some(Tool::OpenCode));
    assert_eq!(
        Endpoint::of(Tool::OpenCode, run, "a1").socket(),
        Path::new("/run/clarp/a1-ocl.sock")
    );
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
                    // A lane whose tool hangs on its way out: it says it goes, and never lets go.
                    "retire" if status["hangs"] == json!(true) => {
                        json!({"ok": true, "state": status})
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
                if op == "retire" && !busy && status["hangs"] != json!(true) {
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
    let claim = busy.then(|| json!({"turn": 1, "attempt": 1, "spool": "/s", "socket": "/t"}));
    json!({
        "thread": thread,
        "available": !busy,
        "claim": claim,
        "options": options,
        "model": null,
        "effort": null,
        "idle_ms": idle_ms,
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
    // Three live lanes and the new one, for a count of three: the longest idle leaves.
    make_room(&run, &own, counted(3), false);
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
    let place = same
        .endpoint
        .place(&run, Some("t"), "d", |_| false, ROOMY, false);
    assert_eq!(place.unwrap(), Place::Kept);
    assert_eq!(same.asked(), ["inspect"]);

    // What the driver says the tool kept from an earlier turn starts it again.
    let place = same
        .endpoint
        .place(&run, Some("t"), "d", |_| true, ROOMY, false);
    assert_eq!(place.unwrap(), Place::Free);
    // Asked what it does before it is asked to leave.
    assert_eq!(same.asked(), ["inspect", "inspect", "inspect", "retire"]);

    let other = FakeLane::start(&run, "b", status("t", "old", 5, false));
    let place = other
        .endpoint
        .place(&run, Some("t"), "new", |_| false, ROOMY, false);
    assert_eq!(place.unwrap(), Place::Free);
    assert_eq!(other.asked(), ["inspect", "inspect", "retire"]);
    assert!(other.endpoint.released().unwrap());

    // A lane that still runs work is used as it is for its own thread, and refuses another.
    let busy = FakeLane::start(&run, "c", status("t", "old", 0, true));
    let place = busy
        .endpoint
        .place(&run, Some("t"), "new", |_| false, ROOMY, false);
    assert_eq!(place.unwrap(), Place::Kept);
    let refused = busy
        .endpoint
        .place(&run, Some("u"), "new", |_| false, ROOMY, false);
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
    let login = || Err::<u8, _>(std::io::Error::other(LoginChanged("login".into())));
    // No spool: nothing reached the tool, and the lane that left is not waited on forever.
    assert_eq!(once_more(login(), &spool, &lane, || Ok(2)).unwrap(), 2);
    assert_eq!(
        once_more(Err(std::io::Error::other("gone")), &spool, &lane, || Ok(2)).unwrap(),
        2
    );
    // A spool: the tool may have it, and it is never offered again.
    std::fs::write(&spool, "").unwrap();
    let kept = once_more(login(), &spool, &lane, || panic!("offered twice"));
    assert!(LoginChanged::is(&kept.unwrap_err()));
    assert_eq!(once_more(Ok(1), &spool, &lane, || Ok(2)).unwrap(), 1);
    std::fs::remove_dir_all(&run).unwrap();
}

#[test]
fn a_changed_login_is_told_by_its_type_and_a_reply_without_state_by_its_words() {
    let read = |line: Value| {
        let text = format!("{line}\n");
        replied::<Status>(&mut text.as_bytes(), Tool::Codex)
    };
    let changed =
        read(json!({"ok": false, "error": "the Codex login changed", "code": LOGIN_CHANGED}))
            .unwrap_err();
    assert!(LoginChanged::is(&changed));
    assert_eq!(changed.to_string(), "the Codex login changed");
    // Another refusal of the lane's, with a code of its own or none, is no changed login.
    let other = read(json!({"ok": false, "error": "busy", "code": "lane-busy"})).unwrap_err();
    assert!(!LoginChanged::is(&other));
    let empty = read(json!({"ok": true})).unwrap_err();
    assert!(!LoginChanged::is(&empty));
    assert_eq!(empty.kind(), std::io::ErrorKind::Other);
    assert_eq!(empty.to_string(), "Codex lane supplied no state");
    let cut = replied::<Status>(&mut &b"{\"ok\":true"[..], Tool::Codex).unwrap_err();
    assert_eq!(cut.to_string(), "Codex lane connection closed");
}

#[test]
fn the_budget_counts_what_each_lane_weighed_and_the_new_one_at_its_typical_size() {
    let run = run_dir("bytes");
    let weighed = |idle_ms: u64, busy: bool| {
        let mut said = status("t", "d", idle_ms, busy);
        said["memory_bytes"] = json!(100 * MIB);
        said
    };
    let shorter = FakeLane::start(&run, "a", weighed(10, false));
    let longer = FakeLane::start(&run, "b", weighed(30, false));
    let busy = FakeLane::start(&run, "c", weighed(0, true));
    let own = Endpoint::of(Tool::Codex, &run, "new");
    // 3 x 100 weighed and 270 for the new lane: over 500, so the longest idle leaves and 470
    // fits; the busy one counts and stays.
    make_room(&run, &own, in_bytes(500 * MIB), false);
    assert_eq!(longer.asked(), ["inspect", "retire"]);
    assert_eq!(shorter.asked(), ["inspect"]);
    assert_eq!(busy.asked(), ["inspect"]);
    std::fs::remove_dir_all(&run).unwrap();
}

#[test]
fn lanes_that_could_not_weigh_themselves_count_at_their_estimate_and_stay_bounded() {
    let run = run_dir("unweighed");
    // Ten idle lanes whose `/proc` could not be read: half say no weight, half say 0.
    let lanes: Vec<FakeLane> = (0..10u64)
        .map(|n| {
            let mut said = status("t", "d", 100 - n, false);
            if n % 2 == 0 {
                said["memory_bytes"] = json!(0);
            }
            FakeLane::start(&run, &format!("a{n}"), said)
        })
        .collect();
    let own = Endpoint::of(Tool::Codex, &run, "new");
    // The default budget: each counts at 270 MiB, the new one too, so eight fit, and the three
    // idle the longest (a0, a1, a2) leave.
    make_room(&run, &own, in_bytes(Settings::of(None).budget), false);
    let left: Vec<usize> = lanes
        .iter()
        .enumerate()
        .filter(|(_, lane)| lane.asked().contains(&"retire".to_owned()))
        .map(|(n, _)| n)
        .collect();
    assert_eq!(left, [0, 1, 2]);
    std::fs::remove_dir_all(&run).unwrap();
}

#[test]
fn the_default_budget_keeps_eight_codex_lanes_and_a_ninth_sends_the_longest_idle_away() {
    let run = run_dir("eight");
    let budget = in_bytes(Settings::of(None).budget);
    let weighed = |idle_ms: u64| {
        let mut said = status("t", "d", idle_ms, false);
        said["memory_bytes"] = json!(CODEX_LANE);
        said
    };
    // Seven lanes of about 270 MB and the one about to start: eight fit, nobody leaves.
    let seven: Vec<FakeLane> = (0..7u64)
        .map(|n| FakeLane::start(&run, &format!("a{n}"), weighed(100 - n)))
        .collect();
    let eighth = Endpoint::of(Tool::Codex, &run, "eighth");
    make_room(&run, &eighth, budget, false);
    assert!(seven.iter().all(|lane| lane.asked() == ["inspect"]));
    // With eight running, a ninth sends the one idle the longest away, and only it.
    let running = FakeLane::start(&run, "eighth", weighed(1));
    let ninth = Endpoint::of(Tool::Codex, &run, "ninth");
    make_room(&run, &ninth, budget, false);
    assert_eq!(seven[0].asked(), ["inspect", "inspect", "retire"]);
    assert!(
        seven[1..]
            .iter()
            .all(|lane| lane.asked() == ["inspect", "inspect"])
    );
    assert_eq!(running.asked(), ["inspect"]);
    std::fs::remove_dir_all(&run).unwrap();
}

#[test]
fn a_lane_starts_only_within_both_limits_and_the_tighter_one_decides() {
    let run = run_dir("both");
    // Ten idle Codex lanes that weighed 110 MiB each, as idle app-servers measure: the budget
    // alone would keep them all and the new one (270 + 10 x 110 = 1,370 MiB of 2,200), but the count
    // of eight sends the three idle the longest away.
    let lanes: Vec<FakeLane> = (0..10u64)
        .map(|n| {
            let mut said = status("t", "d", 100 - n, false);
            said["memory_bytes"] = json!(110 * MIB);
            FakeLane::start(&run, &format!("a{n}"), said)
        })
        .collect();
    let own = Endpoint::of(Tool::Codex, &run, "new");
    make_room(&run, &own, Settings::of(None).room_with(None), false);
    let left = |lanes: &[FakeLane]| -> Vec<usize> {
        lanes
            .iter()
            .enumerate()
            .filter(|(_, lane)| lane.asked().contains(&"retire".to_owned()))
            .map(|(n, _)| n)
            .collect()
    };
    assert_eq!(left(&lanes), [0, 1, 2]);
    // The same lanes under a budget of 600 MiB and the count of eight: the budget is the
    // tighter one, and the lanes go until 270 MiB for the new one and three of 110 fit.
    let run2 = run_dir("both-bytes");
    let more: Vec<FakeLane> = (0..7u64)
        .map(|n| {
            let mut said = status("t", "d", 100 - n, false);
            said["memory_bytes"] = json!(110 * MIB);
            FakeLane::start(&run2, &format!("b{n}"), said)
        })
        .collect();
    let own2 = Endpoint::of(Tool::Codex, &run2, "new");
    make_room(
        &run2,
        &own2,
        Room {
            most: 8,
            budget: 600 * MIB,
            free: None,
        },
        false,
    );
    assert_eq!(left(&more), [0, 1, 2, 3]);
    std::fs::remove_dir_all(&run).unwrap();
    std::fs::remove_dir_all(&run2).unwrap();
}

#[test]
fn under_the_defaults_heavy_lanes_stop_by_their_bytes_before_the_count() {
    let run = run_dir("heavy");
    // Six idle lanes of a heavy tool's weight (an OpenCode serve measured 571-590 MiB): the
    // count of eight would keep them all, but 270 MiB for the new one and six of 590 is over
    // 2,200, so the three idle the longest leave and 270 + 3 x 590 = 2,040 MiB fits.
    let lanes: Vec<FakeLane> = (0..6u64)
        .map(|n| {
            let mut said = status("t", "d", 100 - n, false);
            said["memory_bytes"] = json!(590 * MIB);
            FakeLane::start(&run, &format!("h{n}"), said)
        })
        .collect();
    let own = Endpoint::of(Tool::Codex, &run, "new");
    make_room(&run, &own, Settings::of(None).room_with(None), false);
    let left: Vec<usize> = lanes
        .iter()
        .enumerate()
        .filter(|(_, lane)| lane.asked().contains(&"retire".to_owned()))
        .map(|(n, _)| n)
        .collect();
    assert_eq!(left, [0, 1, 2]);
    std::fs::remove_dir_all(&run).unwrap();
}

/// A lane's process as the tests start it: it takes its owner lock itself (so the kernel names
/// it as the holder), starts its tool where `group` is `tool` (a process that leads a group of
/// its own and ignores SIGTERM and the end of its input, as a hung tool does), writes `<pid>
/// <group>` into the lock as a lane does, says the group in `ready`, and stands still. Started
/// outside systemd, it runs in no scope of its own.
const LANE_PROCESS: &str = r#"
import fcntl, os, subprocess, sys, time
lock, ready, group = sys.argv[1], sys.argv[2], sys.argv[3]
held = open(lock, "a+")
fcntl.flock(held, fcntl.LOCK_EX)
if group == "tool":
    tool = subprocess.Popen(["sh", "-c", "trap '' TERM; exec sleep 600"],
                            stdin=subprocess.PIPE, start_new_session=True)
    group = str(tool.pid)
held.seek(0)
held.truncate()
held.write(f"{os.getpid()} {group}\n")
held.flush()
with open(ready + ".part", "w") as said:
    said.write(group)
os.rename(ready + ".part", ready)
time.sleep(600)
"#;

/// A lane whose process never lets go when asked: its control socket answers `inspect` with
/// `status`, takes `retire` and never answers it. Its process ([`LANE_PROCESS`]) holds the owner
/// lock until it ends; `ended` says how it ended.
struct StuckLane {
    endpoint: Endpoint,
    asked: Arc<Mutex<Vec<String>>>,
    ended: std::sync::mpsc::Receiver<std::process::ExitStatus>,
    /// The lane's process id, captured when it was started.
    pid: u32,
    /// The group its lock names: its tool's, or what the test gave.
    group: u32,
}

impl StuckLane {
    fn start(run: &Path, agent: &str, status: Value, group: &str) -> Self {
        let endpoint = Endpoint::of(Tool::Codex, run, agent);
        let listener = UnixListener::bind(endpoint.socket()).unwrap();
        let ready = run.join(format!("{agent}.ready"));
        let mut process = crate::child::command("python3")
            .args(["-c", LANE_PROCESS])
            .arg(owner_lock(endpoint.socket()))
            .arg(&ready)
            .arg(group)
            .spawn()
            .unwrap();
        let pid = process.id();
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(10);
        let group = loop {
            if let Ok(said) = std::fs::read_to_string(&ready) {
                break said.trim().parse().unwrap();
            }
            assert!(
                std::time::Instant::now() < deadline,
                "the lane did not start"
            );
            std::thread::sleep(std::time::Duration::from_millis(20));
        };
        let (tell, ended) = std::sync::mpsc::channel();
        std::thread::spawn(move || {
            let _ = tell.send(process.wait().unwrap());
        });
        let asked = Arc::new(Mutex::new(Vec::new()));
        let seen = asked.clone();
        std::thread::spawn(move || {
            let mut unanswered = Vec::new();
            for stream in listener.incoming() {
                let mut stream = stream.unwrap();
                let mut line = String::new();
                BufReader::new(&stream).read_line(&mut line).unwrap();
                let request: Value = serde_json::from_str(&line).unwrap();
                let op = request["op"].as_str().unwrap().to_owned();
                seen.lock().unwrap().push(op.clone());
                if op == "retire" {
                    unanswered.push(stream);
                    continue;
                }
                let frame = format!("{}\n", json!({"ok": true, "state": status}));
                let _ = stream.write_all(frame.as_bytes());
            }
        });
        Self {
            endpoint,
            asked,
            ended,
            pid,
            group,
        }
    }

    fn asked(&self) -> Vec<String> {
        self.asked.lock().unwrap().clone()
    }
}

/// What a lane says of itself while it is idle, or while it works.
fn saying(working: bool) -> Value {
    let mut said = status("t", "old", 50, false);
    said["working"] = json!(working);
    said
}

/// Whether the process `pid` is gone, or ended and only waits to be reaped.
fn is_ended(pid: u32) -> bool {
    let Ok(stat) = std::fs::read_to_string(format!("/proc/{pid}/stat")) else {
        return true;
    };
    stat.rsplit_once(')')
        .is_some_and(|(_, after)| after.trim_start().starts_with('Z'))
}

fn sleeper() -> std::process::Child {
    crate::child::command("sleep").arg("600").spawn().unwrap()
}

#[test]
fn an_idle_lane_that_never_lets_go_is_ended_with_its_tool_within_the_bound() {
    use std::os::unix::process::ExitStatusExt;
    let run = run_dir("stuck");
    let mut beside = sleeper();
    let stuck = StuckLane::start(&run, "a", saying(false), "tool");
    let tool = stuck.group;
    assert!(!is_ended(tool));
    let bound = std::time::Duration::from_millis(300);
    let started = std::time::Instant::now();
    let place =
        stuck
            .endpoint
            .place_within(&run, (Some("t"), "new"), |_| false, ROOMY, false, bound);
    let waited = started.elapsed();
    // Asked what it does, asked to leave, waited for the bound, then ended with its tool: its
    // lock is free and a new lane may start, with no turn held on the lane's exit.
    assert_eq!(place.unwrap(), Place::Free);
    assert_eq!(stuck.asked(), ["inspect", "inspect", "retire"]);
    assert!(waited >= bound, "{waited:?}");
    assert!(waited < std::time::Duration::from_secs(5), "{waited:?}");
    let exit = stuck
        .ended
        .recv_timeout(std::time::Duration::from_secs(5))
        .unwrap();
    assert_eq!(exit.signal(), Some(nix::libc::SIGKILL));
    // The tool is gone too: it ignored SIGTERM and the end of its input, and no process of it
    // writes the conversation beside the next lane.
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(5);
    while !is_ended(tool) {
        assert!(
            std::time::Instant::now() < deadline,
            "the lane's tool lives"
        );
        std::thread::sleep(std::time::Duration::from_millis(20));
    }

    // A group whose leader is no child of the lane is not its tool: only the lane goes.
    let elsewhere = StuckLane::start(&run, "b", saying(false), &beside.id().to_string());
    let place =
        elsewhere
            .endpoint
            .place_within(&run, (Some("t"), "new"), |_| false, ROOMY, false, bound);
    assert_eq!(place.unwrap(), Place::Free);
    let exit = elsewhere
        .ended
        .recv_timeout(std::time::Duration::from_secs(5))
        .unwrap();
    assert_eq!(exit.signal(), Some(nix::libc::SIGKILL));
    // Nothing beside the lane and its tool is signalled.
    assert!(beside.try_wait().unwrap().is_none());
    beside.kill().unwrap();
    beside.wait().unwrap();
    std::fs::remove_dir_all(&run).unwrap();
}

#[test]
fn a_lane_that_works_or_does_not_say_is_never_ended_however_slow_it_answers() {
    let run = run_dir("working");
    let bound = std::time::Duration::from_millis(300);
    for (agent, said) in [
        ("working", saying(true)),
        ("unsaid", status("t", "old", 50, false)),
    ] {
        let lane = StuckLane::start(&run, agent, said, "tool");
        // It does not answer the retirement in time, as a lane loading a long conversation in
        // a turn may not: it is waited for, and the turn goes on without that lane's room.
        let place =
            lane.endpoint
                .place_within(&run, (Some("u"), "new"), |_| false, ROOMY, false, bound);
        assert!(place.is_err(), "{agent}: {place:?}");
        assert_eq!(lane.asked(), ["inspect", "inspect", "retire"], "{agent}");
        assert!(
            lane.ended
                .recv_timeout(std::time::Duration::from_millis(500))
                .is_err(),
            "{agent}: the lane was ended"
        );
        assert!(!is_ended(lane.group), "{agent}: its tool was ended");
        // The test ends what it started, by the ids it captured.
        for (id, group) in [(lane.group, true), (lane.pid, false)] {
            let target = nix::unistd::Pid::from_raw(i32::try_from(id).unwrap());
            let kill = nix::sys::signal::Signal::SIGKILL;
            if group {
                nix::sys::signal::killpg(target, kill).unwrap();
            } else {
                nix::sys::signal::kill(target, kill).unwrap();
            }
        }
        lane.ended
            .recv_timeout(std::time::Duration::from_secs(5))
            .unwrap();
    }
    std::fs::remove_dir_all(&run).unwrap();
}

#[test]
fn the_terminal_opens_after_the_bound_when_an_idle_lane_never_lets_go() {
    let run = run_dir("hangs");
    let mut said = status("t", "d", 50, false);
    said["hangs"] = json!(true);
    let hanging = FakeLane::start(&run, "agent", said);
    let started = std::time::Instant::now();
    assert!(make_way_within(
        &run,
        "agent",
        std::time::Duration::from_millis(300)
    ));
    let waited = started.elapsed();
    // Asked to leave, waited for at most the bound, and the terminal goes on.
    assert_eq!(hanging.asked(), ["inspect", "retire"]);
    assert!(
        waited >= std::time::Duration::from_millis(300),
        "{waited:?}"
    );
    assert!(waited < std::time::Duration::from_secs(5), "{waited:?}");
    assert!(!hanging.endpoint.released().unwrap());
    // An idle lane that lets go is waited for until it has, and no longer.
    let leaving = FakeLane::start(&run, "other", status("t", "d", 50, false));
    assert!(make_way_within(
        &run,
        "other",
        std::time::Duration::from_secs(5)
    ));
    assert_eq!(leaving.asked(), ["inspect", "retire"]);
    assert!(leaving.endpoint.released().unwrap());
    // A lane that runs work of its own refuses to leave, and the terminal is refused.
    let busy = FakeLane::start(&run, "busy", status("t", "d", 0, true));
    assert!(!make_way_within(
        &run,
        "busy",
        std::time::Duration::from_secs(5)
    ));
    assert_eq!(busy.asked(), ["inspect", "retire"]);
    assert!(!busy.endpoint.released().unwrap());
    std::fs::remove_dir_all(&run).unwrap();
}

#[tokio::test]
async fn what_the_lanes_hold_is_asked_of_all_at_once_within_one_deadline() {
    let run = run_dir("held");
    let mut weighed = status("t", "d", 5, true);
    weighed["memory_bytes"] = json!(100 * MIB);
    let _answers = FakeLane::start(&run, "a", weighed);
    let _unweighed = FakeLane::start(&run, "b", status("t", "d", 5, false));
    // Three lanes that take the connection and never answer.
    let silent: Vec<UnixListener> = ["c", "d", "e"]
        .iter()
        .map(|agent| UnixListener::bind(Endpoint::of(Tool::Codex, &run, agent).socket()).unwrap())
        .collect();
    // And the socket of a lane that has left: nothing listens on it.
    drop(UnixListener::bind(Endpoint::of(Tool::Claude, &run, "f").socket()).unwrap());
    let lanes = tokio::task::spawn_blocking({
        let run = run.clone();
        move || lanes_in(&run)
    })
    .await
    .unwrap();
    assert_eq!(lanes.len(), 6);
    let bound = std::time::Duration::from_millis(400);
    let began = std::time::Instant::now();
    let holding = held(lanes, tokio::time::Instant::now() + bound).await;
    let took = began.elapsed();
    assert!(
        took >= bound && took < bound * 3,
        "{took:?}: the silent lanes were asked at once"
    );
    assert_eq!(holding.unanswered, 3);
    assert_eq!(holding.tools[0].lanes, 2);
    assert_eq!(holding.tools[0].busy, 1);
    assert_eq!(holding.tools[0].bytes, 100 * MIB + CODEX_LANE);
    assert_eq!(holding.tools[1], super::Held::default());
    drop(silent);
    std::fs::remove_dir_all(&run).unwrap();
}

#[test]
fn with_every_place_busy_a_chat_lane_is_not_started_and_a_goals_lane_is() {
    // Eight lanes, each running a turn: no place can be made (issue 786).
    let run = run_dir("full");
    let busy: Vec<FakeLane> = (0..8u64)
        .map(|n| FakeLane::start(&run, &format!("b{n}"), status("t", "d", 0, true)))
        .collect();
    let defaults = Settings::of(None).room_with(None);
    // A chat turn's lane is optional: it is not started, and nothing is asked to leave.
    let chat = Endpoint::of(Tool::Codex, &run, "chat");
    let place = chat
        .place(&run, Some("t"), "d", |_| false, defaults, false)
        .unwrap();
    assert_eq!(place, Place::Full);
    assert!(busy.iter().all(|lane| lane.asked() == ["inspect"]));
    // Nor is a Muse lane, whose start is always optional (issue 762): the busy lanes are only
    // looked at again.
    let muse = Endpoint::of(Tool::Muse, &run, "muse");
    let place = muse
        .place(&run, Some("t"), "d", |_| false, defaults, false)
        .unwrap();
    assert_eq!(place, Place::Full);
    assert!(
        busy.iter()
            .all(|lane| lane.asked() == ["inspect", "inspect"])
    );
    // A native goal needs its lane: it starts past the limits, and counts.
    let goal = Endpoint::of(Tool::Codex, &run, "goal");
    let place = goal
        .place(&run, Some("t"), "d", |_| false, defaults, true)
        .unwrap();
    assert_eq!(place, Place::Free);
    std::fs::remove_dir_all(&run).unwrap();

    // Once one of the eight is idle, the next start takes its place: the idle one leaves.
    let run = run_dir("full-then-idle");
    let mut lanes: Vec<FakeLane> = (0..7u64)
        .map(|n| FakeLane::start(&run, &format!("b{n}"), status("t", "d", 0, true)))
        .collect();
    lanes.push(FakeLane::start(
        &run,
        "idle",
        status("t", "d", 60_000, false),
    ));
    let next = Endpoint::of(Tool::Codex, &run, "next");
    let place = next
        .place(&run, Some("t"), "d", |_| false, defaults, false)
        .unwrap();
    assert_eq!(place, Place::Free);
    assert_eq!(lanes[7].asked(), ["inspect", "retire"]);
    assert!(lanes[..7].iter().all(|lane| lane.asked() == ["inspect"]));
    std::fs::remove_dir_all(&run).unwrap();
}

#[test]
fn a_start_that_cannot_be_made_room_for_sends_nothing_away_unless_it_is_needed() {
    // Nine lanes, one past the count of eight because a goal's lane started anyway: eight busy
    // and one idle. The idle one leaving would still leave nine of eight, so a chat lane does
    // not start, and it ends nothing.
    let run = run_dir("over");
    let mut lanes: Vec<FakeLane> = (0..8u64)
        .map(|n| FakeLane::start(&run, &format!("b{n}"), status("t", "d", 0, true)))
        .collect();
    lanes.push(FakeLane::start(
        &run,
        "idle",
        status("t", "d", 60_000, false),
    ));
    let room = Settings::of(None).room_with(None);
    let chat = Endpoint::of(Tool::Codex, &run, "chat");
    let place = chat
        .place(&run, Some("t"), "d", |_| false, room, false)
        .unwrap();
    assert_eq!(place, Place::Full);
    assert!(retired(&lanes).is_empty());
    // A goal's lane starts whatever the answer: it sends the idle one away, toward the limits.
    let goal = Endpoint::of(Tool::Codex, &run, "goal");
    let place = goal
        .place(&run, Some("t"), "d", |_| false, room, true)
        .unwrap();
    assert_eq!(place, Place::Free);
    assert_eq!(retired(&lanes), [8]);
    std::fs::remove_dir_all(&run).unwrap();
}

/// Eight lanes in a run folder: `busy` running turns, the rest idle (the lower the index, the
/// longer idle), each of an agent named by its place; `his` names which agents are the owner's.
fn eight(run: &Path, busy: usize, his: &[usize]) -> Vec<FakeLane> {
    (0..8usize)
        .map(|n| {
            if his.contains(&n) {
                mark_owners_view(run, &format!("a{n}"));
            }
            let idle_ms = if n < busy {
                0
            } else {
                100_000 - u64::try_from(n).unwrap()
            };
            FakeLane::start(run, &format!("a{n}"), status("t", "d", idle_ms, n < busy))
        })
        .collect()
}

fn retired(lanes: &[FakeLane]) -> Vec<usize> {
    lanes
        .iter()
        .enumerate()
        .filter(|(_, lane)| lane.asked().contains(&"retire".to_owned()))
        .map(|(n, _)| n)
        .collect()
}

#[test]
fn a_lane_of_the_agents_own_never_takes_an_owners_place() {
    // Eight lanes, the six idle ones all of agents the owner talks with: a helper's lane may
    // send none of them away, so it does not start and nothing leaves (the owner's rule).
    let run = run_dir("owners-kept");
    let lanes = eight(&run, 2, &[2, 3, 4, 5, 6, 7]);
    let helper = Endpoint::of(Tool::Codex, &run, "helper");
    let room = Settings::of(None).room_with(None);
    let place = helper
        .place(&run, Some("t"), "d", |_| false, room, false)
        .unwrap();
    assert_eq!(place, Place::Full);
    assert!(retired(&lanes).is_empty());
    std::fs::remove_dir_all(&run).unwrap();
}

#[test]
fn the_owners_turn_sends_an_idle_helper_lane_away_before_an_idle_lane_of_his() {
    // Seven busy lanes and one idle helper lane: a lane for an agent the owner just wrote to
    // starts, and the helper's idle lane is the one that leaves.
    let run = run_dir("owners-first");
    let lanes = eight(&run, 7, &[]);
    mark_owners_turn(&run, "his");
    let his = Endpoint::of(Tool::Codex, &run, "his");
    let room = Settings::of(None).room_with(None);
    let place = his
        .place(&run, Some("t"), "d", |_| false, room, false)
        .unwrap();
    assert_eq!(place, Place::Free);
    assert_eq!(retired(&lanes), [7]);
    std::fs::remove_dir_all(&run).unwrap();

    // Six busy, an idle lane of his that has waited the longest and an idle helper lane: the
    // helper's leaves first, though the owner's has been idle longer.
    let run = run_dir("owners-order");
    let lanes = eight(&run, 6, &[6]);
    mark_owners_turn(&run, "his");
    let his = Endpoint::of(Tool::Codex, &run, "his");
    let place = his
        .place(&run, Some("t"), "d", |_| false, room, false)
        .unwrap();
    assert_eq!(place, Place::Free);
    assert_eq!(retired(&lanes), [7]);
    std::fs::remove_dir_all(&run).unwrap();

    // With no helper lane idle, a lane of his may take the place of another idle lane of his.
    let run = run_dir("owners-own");
    let lanes = eight(&run, 7, &[7]);
    mark_owners_view(&run, "his");
    let his = Endpoint::of(Tool::Codex, &run, "his");
    let place = his
        .place(&run, Some("t"), "d", |_| false, room, false)
        .unwrap();
    assert_eq!(place, Place::Free);
    assert_eq!(retired(&lanes), [7]);
    std::fs::remove_dir_all(&run).unwrap();
}

/// A machine of 10 GiB with `available` MiB free: auto keeps 2 GiB (20%) free.
fn machine(available: u64) -> Memory {
    Memory {
        available: available * MIB,
        total: 10_240 * MIB,
    }
}

/// A lane started two minutes ago: no longer taken to grow beyond what it weighed.
fn aged(lane: FakeLane) -> FakeLane {
    use nix::sys::stat::{UtimensatFlags, utimensat};
    use nix::sys::time::TimeSpec;
    let then = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .saturating_sub(std::time::Duration::from_secs(120));
    let then = TimeSpec::from_duration(then);
    utimensat(
        None,
        lane.endpoint.socket(),
        &then,
        &then,
        UtimensatFlags::NoFollowSymlink,
    )
    .unwrap();
    lane
}

/// `n` idle Codex lanes that weighed 110 MiB each, the one idle longest first, and two busy ones,
/// all started two minutes ago.
fn idle_and_busy(run: &Path, n: u64) -> (Vec<FakeLane>, Vec<FakeLane>) {
    let idle = (0..n)
        .map(|at| {
            let mut said = status("t", "d", 100_000 - at, false);
            said["memory_bytes"] = json!(110 * MIB);
            aged(FakeLane::start(run, &format!("i{at}"), said))
        })
        .collect();
    let busy = (0..2u64)
        .map(|at| {
            let mut said = status("t", "d", 0, true);
            said["memory_bytes"] = json!(500 * MIB);
            aged(FakeLane::start(run, &format!("w{at}"), said))
        })
        .collect();
    (idle, busy)
}

#[test]
fn the_limits_are_auto_from_the_free_memory_unless_the_owner_fixes_them() {
    let default = Settings::of(None);
    assert_eq!(default.mode, Mode::Auto);
    let text = "MemTotal:       16303836 kB\nMemFree:  1000 kB\nMemAvailable:    9011696 kB\n";
    let memory = Memory::of(text).unwrap();
    assert_eq!(memory.total, 16_303_836 * 1024);
    assert_eq!(memory.available, 9_011_696 * 1024);
    assert_eq!(Memory::of("MemTotal: 1 kB\n"), None);
    // Auto: no count and no budget; a fifth of the machine stays free.
    assert_eq!(
        default.room_with(Some(machine(4_000))),
        Room {
            most: usize::MAX,
            budget: u64::MAX,
            free: Some(Free {
                available: 4_000 * MIB,
                reserve: 10_240 * MIB / 100 * 20,
            }),
        }
    );
    let tuned = of("lane_reserve_percent = 30\nlane_ceiling = 10\n");
    // The ceiling is read, and bounds nothing in auto.
    assert_eq!(tuned.ceiling, 10);
    assert_eq!(tuned.room_with(Some(machine(4_000))).most, usize::MAX);
    assert_eq!(tuned.reserve_of(machine(4_000)), 10_240 * MIB / 100 * 30);
    // Fixed: the count and the budget as before, whatever is free.
    let fixed = of("lane_limits = \"fixed\"\ncodex_lane_most = 12\nlane_memory_mb = 3000\n");
    assert_eq!(fixed.mode, Mode::Fixed);
    assert_eq!(
        fixed.room_with(Some(machine(100))),
        Room {
            most: 12,
            budget: 3_000 * MIB,
            free: None,
        }
    );
    // A word the Host does not know is no setting.
    assert_eq!(of("lane_limits = \"some\"\n").mode, Mode::Auto);
}

#[test]
fn in_auto_idle_lanes_leave_until_what_is_free_after_the_new_one_stays_above_the_reserve() {
    // 2,048 MiB must stay free; 2,200 are, and the new lane counts at 270: 118 MiB short, so
    // two idle lanes of 110 leave, the two idle the longest, and no busy one.
    let run = run_dir("auto-reserve");
    let (idle, busy) = idle_and_busy(&run, 5);
    let own = Endpoint::of(Tool::Codex, &run, "new");
    let room = Settings::of(None).room_with(Some(machine(2_200)));
    assert!(make_room(&run, &own, room, false));
    assert_eq!(retired(&idle), [0, 1]);
    assert!(retired(&busy).is_empty());
    std::fs::remove_dir_all(&run).unwrap();

    // With plenty free, no count binds: a ceiling of three set by hand sends no idle lane away.
    let run = run_dir("auto-ceiling");
    let (idle, busy) = idle_and_busy(&run, 2);
    let own = Endpoint::of(Tool::Codex, &run, "new");
    let room = of("lane_ceiling = 3\n").room_with(Some(machine(9_000)));
    assert!(make_room(&run, &own, room, false));
    assert!(retired(&idle).is_empty());
    assert!(retired(&busy).is_empty());
    std::fs::remove_dir_all(&run).unwrap();

    // When even every idle lane leaving would not be enough, a chat lane sends none away and
    // does not start; a goal's lane sends them away and starts.
    let run = run_dir("auto-short");
    let (idle, _busy) = idle_and_busy(&run, 2);
    let room = Settings::of(None).room_with(Some(machine(1_500)));
    let chat = Endpoint::of(Tool::Codex, &run, "chat");
    let place = chat
        .place(&run, Some("t"), "d", |_| false, room, false)
        .unwrap();
    assert_eq!(place, Place::Full);
    assert!(retired(&idle).is_empty());
    let goal = Endpoint::of(Tool::Codex, &run, "goal");
    let place = goal
        .place(&run, Some("t"), "d", |_| false, room, true)
        .unwrap();
    assert_eq!(place, Place::Free);
    assert_eq!(retired(&idle), [0, 1]);
    std::fs::remove_dir_all(&run).unwrap();
}

#[test]
fn auto_starts_a_25th_lane_when_memory_allows() {
    // Twenty-four lanes run work, each weighed small, and the machine has plenty free. The
    // ceiling of 24 once refused a 25th; auto now counts the memory alone.
    let run = run_dir("auto-25th");
    let busy: Vec<FakeLane> = (0..24u64)
        .map(|at| {
            let mut said = status("t", "d", 0, true);
            said["memory_bytes"] = json!(60 * MIB);
            aged(FakeLane::start(&run, &format!("b{at}"), said))
        })
        .collect();
    let own = Endpoint::of(Tool::Codex, &run, "new");
    let room = Settings::of(None).room_with(Some(machine(6_000)));
    let place = own
        .place(&run, Some("t"), "d", |_| false, room, false)
        .unwrap();
    assert_eq!(place, Place::Free);
    assert!(retired(&busy).is_empty());
    // A ceiling in the owner's file (the live Hosts carry `lane_ceiling = 64`) bounds nothing in
    // auto: under a ceiling of 3, or of 24, the 25th starts too.
    for ceiling in [3, 24] {
        let set = of(&format!("lane_ceiling = {ceiling}\n"));
        assert_eq!(set.mode, Mode::Auto);
        assert_eq!(set.ceiling, ceiling);
        let room = set.room_with(Some(machine(6_000)));
        assert!(make_room(&run, &own, room, false), "ceiling {ceiling}");
        assert!(retired(&busy).is_empty(), "ceiling {ceiling}");
    }
    // Short of memory, the same 25th does not start: the reserve still binds.
    let short = Settings::of(None).room_with(Some(machine(2_100)));
    assert!(!make_room(&run, &own, short, false));
    std::fs::remove_dir_all(&run).unwrap();
}

#[test]
fn a_machine_short_of_memory_sheds_idle_lanes_in_order_and_never_a_busy_one() {
    let run = run_dir("shed");
    let (idle, busy) = idle_and_busy(&run, 4);
    let settings = Settings::of(None);
    // Above the reserve: nothing leaves.
    assert_eq!(shed(&run, &settings, Some(machine(3_000))), 0);
    assert!(retired(&idle).is_empty());
    // 2,000 free of the 2,048 kept: one idle lane of 110 brings it back.
    assert_eq!(shed(&run, &settings, Some(machine(2_000))), 1);
    assert_eq!(retired(&idle), [0]);
    // Far below: every idle lane leaves, and the busy ones stay.
    assert_eq!(shed(&run, &settings, Some(machine(500))), 3);
    assert_eq!(retired(&idle), [0, 1, 2, 3]);
    assert!(retired(&busy).is_empty());
    std::fs::remove_dir_all(&run).unwrap();
    // Fixed limits, or a machine whose memory cannot be read, shed nothing.
    let run = run_dir("shed-fixed");
    let (idle, _busy) = idle_and_busy(&run, 2);
    assert_eq!(
        shed(&run, &of("lane_limits = \"fixed\"\n"), Some(machine(100))),
        0
    );
    assert_eq!(shed(&run, &settings, None), 0);
    assert!(retired(&idle).is_empty());
    std::fs::remove_dir_all(&run).unwrap();
}

#[test]
fn lanes_started_moments_ago_count_at_their_typical_size_until_they_have_grown() {
    // 2,048 MiB kept free, 2,600 free, and the new lane counts at 270: it fits (2,318). But two
    // lanes started seconds ago, which weighed 100 MiB each before their first turn, may still
    // grow to 270 each: 340 MiB of what looks free is theirs, and a chat lane does not start.
    let run = run_dir("young");
    let young: Vec<FakeLane> = (0..2u64)
        .map(|at| {
            let mut said = status("t", "d", 0, true);
            said["memory_bytes"] = json!(100 * MIB);
            FakeLane::start(&run, &format!("y{at}"), said)
        })
        .collect();
    let room = Settings::of(None).room_with(Some(machine(2_600)));
    let chat = Endpoint::of(Tool::Codex, &run, "chat");
    let place = chat
        .place(&run, Some("t"), "d", |_| false, room, false)
        .unwrap();
    assert_eq!(place, Place::Full);
    // A minute on, they have weighed what they hold: the same free memory lets the lane start.
    let _old: Vec<FakeLane> = young.into_iter().map(aged).collect();
    let place = chat
        .place(&run, Some("t"), "d", |_| false, room, false)
        .unwrap();
    assert_eq!(place, Place::Free);
    // A lane that has not weighed itself at all counts at its typical size however old it is.
    let run2 = run_dir("unweighed-young");
    let _unweighed = aged(FakeLane::start(&run2, "u", status("t", "d", 0, true)));
    let chat2 = Endpoint::of(Tool::Codex, &run2, "chat");
    let tight = Settings::of(None).room_with(Some(machine(2_400)));
    let place = chat2
        .place(&run2, Some("t"), "d", |_| false, tight, false)
        .unwrap();
    assert_eq!(place, Place::Full);
    std::fs::remove_dir_all(&run).unwrap();
    std::fs::remove_dir_all(&run2).unwrap();
}

#[test]
fn the_reserve_is_kept_between_five_and_fifty_percent_however_it_was_written() {
    assert_eq!(of("lane_reserve_percent = 70\n").reserve_percent, 50);
    assert_eq!(of("lane_reserve_percent = 2\n").reserve_percent, 5);
    assert_eq!(of("lane_reserve_percent = 30\n").reserve_percent, 30);
}

#[test]
fn no_two_tests_share_a_run_folder() {
    // Tests run in parallel in one process, and `run_dir` empties its folder first: two tests that
    // name the same folder remove each other's lanes under load (issue 814).
    let source = include_str!("tests.rs");
    let mut names: Vec<&str> = source
        .split("run_dir(\"")
        .skip(1)
        .filter_map(|rest| rest.split('"').next())
        .collect();
    let all = names.len();
    names.sort_unstable();
    names.dedup();
    assert_eq!(names.len(), all, "a run folder is named twice: {names:?}");
}

/// Issue 854. A permission error, as the lane's socket may give one (EACCES), has the kind a
/// changed login once had, and is no changed login: a turn never waits for a lane to let go for
/// it, nor starts a new one.
#[test]
fn a_permission_error_is_no_changed_login() {
    let denied = std::io::Error::from_raw_os_error(13);
    assert_eq!(denied.kind(), std::io::ErrorKind::PermissionDenied);
    assert!(!LoginChanged::is(&denied));
    let said = std::io::Error::new(std::io::ErrorKind::PermissionDenied, "login");
    assert!(!LoginChanged::is(&said));
    assert!(LoginChanged::is(&std::io::Error::other(LoginChanged(
        "login".into()
    ))));
}
