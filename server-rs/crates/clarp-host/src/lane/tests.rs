#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::fs::File;
use std::io::{BufRead, BufReader, Write};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use clarp_shim::lane::{LOGIN_CHANGED, Status, owner_lock};
use serde_json::{Value, json};

use super::{
    Endpoint, Free, LEAVING_LOOK, LoginChanged, MAX_STATE, Memory, Mode, Place, Room, Settings,
    Tool, held, lanes_in, make_room, make_way_within, mark_owners_turn, mark_owners_view,
    once_more, replied, said_it_goes, shed, start_once_more,
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

/// How long a quiet lane says nothing when asked to leave before it hangs up.
const QUIET_FOR: std::time::Duration = std::time::Duration::from_secs(1);

/// A lane process as the Host sees it: it answers `inspect` with `status`, holds its owner lock
/// until it is retired, and refuses `retire` while it holds a claim. Every op it was asked is
/// kept, in order. With `"refuses": true` in `status` it says it is idle and refuses `retire`
/// all the same (a turn claimed it after it was looked at); with `"quiet": true` it takes
/// `retire`, says nothing for [`QUIET_FOR`], longer than e6a9915e's half second, and hangs up,
/// its socket staying where it is. Asked to leave while idle, it goes as a lane's process does:
/// by default its socket goes before it says so (Codex, Claude Code); with `"answers_after_ms"`
/// it is a Muse or OpenCode lane started before issue 908, which keeps its socket while its tool
/// ends for that long, lets go, says it goes, and only then removes its socket; with
/// `"lingers_ms"` it is one started since, which says it goes at once, its tool ending for that
/// long before it lets go and its socket goes.
struct FakeLane {
    endpoint: Endpoint,
    asked: Arc<Mutex<Vec<String>>>,
}

impl FakeLane {
    fn start(run: &Path, agent: &str, status: Value) -> Self {
        let endpoint = Endpoint::of(Tool::Codex, run, agent);
        let owner = File::create(owner_lock(endpoint.socket())).unwrap();
        owner.try_lock().unwrap();
        Self::start_holding(run, agent, status, owner)
    }

    /// The same for a lane that took its owner lock already and binds its socket only now, as a
    /// lane that is starting does.
    fn start_holding(run: &Path, agent: &str, status: Value, owner: File) -> Self {
        let endpoint = Endpoint::of(Tool::Codex, run, agent);
        let listener = UnixListener::bind(endpoint.socket()).unwrap();
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
                if op == "retire" && status["quiet"] == json!(true) {
                    std::thread::spawn(move || {
                        std::thread::sleep(QUIET_FOR);
                        drop(stream);
                    });
                    continue;
                }
                let busy = !status["claim"].is_null() || status["refuses"] == json!(true);
                let goes = |stream: &mut std::os::unix::net::UnixStream| {
                    let mut frame = json!({"ok": true, "state": status}).to_string();
                    frame.push('\n');
                    stream.write_all(frame.as_bytes()).unwrap();
                };
                let ending = |ms: &Value| std::time::Duration::from_millis(ms.as_u64().unwrap());
                if op == "retire" && !busy && !status["answers_after_ms"].is_null() {
                    std::thread::sleep(ending(&status["answers_after_ms"]));
                    owner.take();
                    goes(&mut stream);
                    let _ = std::fs::remove_file(&socket);
                    return;
                }
                if op == "retire" && !busy && !status["lingers_ms"].is_null() {
                    goes(&mut stream);
                    std::thread::sleep(ending(&status["lingers_ms"]));
                    owner.take();
                    let _ = std::fs::remove_file(&socket);
                    return;
                }
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

/// What a start was given. A place given is given back at once, as it is when a launch fails:
/// the tests that only read the answer start no lane in it.
#[derive(Debug, PartialEq, Eq)]
enum Was {
    Kept,
    Free,
    Full,
}

fn was(place: Place) -> Was {
    match place {
        Place::Kept => Was::Kept,
        Place::Free(_) => Was::Free,
        Place::Full => Was::Full,
    }
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
    let _ = make_room(&run, &own, counted(3), false);
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
    assert_eq!(was(place.unwrap()), Was::Kept);
    assert_eq!(same.asked(), ["inspect"]);

    // What the driver says the tool kept from an earlier turn starts it again.
    let place = same
        .endpoint
        .place(&run, Some("t"), "d", |_| true, ROOMY, false);
    assert_eq!(was(place.unwrap()), Was::Free);
    // Asked what it does before it is asked to leave.
    assert_eq!(same.asked(), ["inspect", "inspect", "inspect", "retire"]);

    let other = FakeLane::start(&run, "b", status("t", "old", 5, false));
    let place = other
        .endpoint
        .place(&run, Some("t"), "new", |_| false, ROOMY, false);
    assert_eq!(was(place.unwrap()), Was::Free);
    assert_eq!(other.asked(), ["inspect", "inspect", "retire"]);
    assert!(other.endpoint.released().unwrap());

    // A lane that still runs work is used as it is for its own thread, and refuses another.
    let busy = FakeLane::start(&run, "c", status("t", "old", 0, true));
    let place = busy
        .endpoint
        .place(&run, Some("t"), "new", |_| false, ROOMY, false);
    assert_eq!(was(place.unwrap()), Was::Kept);
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
    assert_eq!(
        once_more(login(), &spool, &lane, false, || Ok(2)).unwrap(),
        2
    );
    assert_eq!(
        once_more(
            Err(std::io::Error::other("gone")),
            &spool,
            &lane,
            false,
            || Ok(2)
        )
        .unwrap(),
        2
    );
    // A spool: the tool may have it, and it is never offered again.
    std::fs::write(&spool, "").unwrap();
    let kept = once_more(login(), &spool, &lane, false, || panic!("offered twice"));
    assert!(LoginChanged::is(&kept.unwrap_err()));
    assert_eq!(once_more(Ok(1), &spool, &lane, false, || Ok(2)).unwrap(), 1);
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
    let _ = make_room(&run, &own, in_bytes(500 * MIB), false);
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
    let _ = make_room(&run, &own, in_bytes(Settings::of(None).budget), false);
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
    let _ = make_room(&run, &eighth, budget, false);
    assert!(seven.iter().all(|lane| lane.asked() == ["inspect"]));
    // With eight running, a ninth sends the one idle the longest away, and only it.
    let running = FakeLane::start(&run, "eighth", weighed(1));
    let ninth = Endpoint::of(Tool::Codex, &run, "ninth");
    let _ = make_room(&run, &ninth, budget, false);
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
    let _ = make_room(&run, &own, Settings::of(None).room_with(None), false);
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
    let _ = make_room(
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
    let _ = make_room(&run, &own, Settings::of(None).room_with(None), false);
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
        Self::start_leaving(run, agent, status, group, None)
    }

    /// The same for a lane that, once asked to leave, stays in its loop for `leaves` and then
    /// begins leaving (its socket goes) and hangs there, holding its lock.
    fn start_leaving(
        run: &Path,
        agent: &str,
        status: Value,
        group: &str,
        leaves: Option<std::time::Duration>,
    ) -> Self {
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
        let socket = endpoint.socket().to_owned();
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
                    if let Some(after) = leaves {
                        let socket = socket.clone();
                        std::thread::spawn(move || {
                            std::thread::sleep(after);
                            let _ = std::fs::remove_file(socket);
                        });
                    }
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
    assert_eq!(was(place.unwrap()), Was::Free);
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
    assert_eq!(was(place.unwrap()), Was::Free);
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
    assert_eq!(was(place), Was::Full);
    assert!(busy.iter().all(|lane| lane.asked() == ["inspect"]));
    // Nor is a Muse lane, whose start is always optional (issue 762): the busy lanes are only
    // looked at again.
    let muse = Endpoint::of(Tool::Muse, &run, "muse");
    let place = muse
        .place(&run, Some("t"), "d", |_| false, defaults, false)
        .unwrap();
    assert_eq!(was(place), Was::Full);
    assert!(
        busy.iter()
            .all(|lane| lane.asked() == ["inspect", "inspect"])
    );
    // A native goal needs its lane: it starts past the limits, and counts.
    let goal = Endpoint::of(Tool::Codex, &run, "goal");
    let place = goal
        .place(&run, Some("t"), "d", |_| false, defaults, true)
        .unwrap();
    assert_eq!(was(place), Was::Free);
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
    assert_eq!(was(place), Was::Free);
    assert_eq!(lanes[7].asked(), ["inspect", "retire"]);
    assert!(lanes[..7].iter().all(|lane| lane.asked() == ["inspect"]));
    std::fs::remove_dir_all(&run).unwrap();
}

#[test]
fn over_the_limits_a_chat_lane_takes_the_place_of_the_longest_idle_one_alone() {
    // Eleven lanes, three past the count of eight (the owner lowered it, or goals' lanes started
    // anyway): eight busy and three idle. No idle lane leaving brings them within the count, and
    // a chat lane still starts: in the place of the one idle longest, and only that one, so the
    // turn runs on a lane rather than a process of its own as big and started cold (issue 908).
    let run = run_dir("over");
    let mut lanes: Vec<FakeLane> = (0..8u64)
        .map(|n| FakeLane::start(&run, &format!("b{n}"), status("t", "d", 0, true)))
        .collect();
    for (n, idle_ms) in [(0, 60_000), (1, 90_000), (2, 30_000)] {
        lanes.push(FakeLane::start(
            &run,
            &format!("idle{n}"),
            status("t", "d", idle_ms, false),
        ));
    }
    let room = Settings::of(None).room_with(None);
    let chat = Endpoint::of(Tool::Codex, &run, "chat");
    let place = chat
        .place(&run, Some("t"), "d", |_| false, room, false)
        .unwrap();
    assert_eq!(was(place), Was::Free);
    assert_eq!(retired(&lanes), [9], "the longest idle, alone");
    // A goal's lane starts whatever the answer: it sends every idle one away, toward the limits.
    let goal = Endpoint::of(Tool::Codex, &run, "goal");
    let place = goal
        .place(&run, Some("t"), "d", |_| false, room, true)
        .unwrap();
    assert_eq!(was(place), Was::Free);
    assert_eq!(retired(&lanes), [8, 9, 10]);
    // Only busy lanes are left: a chat lane does not start, and none of them is asked to leave.
    let late = Endpoint::of(Tool::Codex, &run, "late");
    let place = late
        .place(&run, Some("t"), "d", |_| false, room, false)
        .unwrap();
    assert_eq!(was(place), Was::Full);
    assert!(
        lanes[..8]
            .iter()
            .all(|lane| !lane.asked().contains(&"retire".to_owned()))
    );
    std::fs::remove_dir_all(&run).unwrap();
}

#[test]
fn a_lane_takes_the_place_of_as_many_idle_ones_as_pay_for_it() {
    // Over the count of one, a busy lane and idle Codex lanes that weighed 110 MiB: no count of
    // them leaving brings the lanes within it. Each idle Codex lane pays at least the 270 a new
    // Codex lane counts at, so a Codex lane takes the place of one, and an OpenCode lane, which
    // counts at 590, of three (two pay 540, short of it).
    let run = run_dir("pays");
    let busy = FakeLane::start(&run, "w", status("t", "d", 0, true));
    let mut idle: Vec<FakeLane> = (0..3u64)
        .map(|at| {
            let mut said = status("t", "d", 100_000 - at, false);
            said["memory_bytes"] = json!(110 * MIB);
            FakeLane::start(&run, &format!("i{at}"), said)
        })
        .collect();
    let codex = Endpoint::of(Tool::Codex, &run, "codex");
    assert!(make_room(&run, &codex, counted(1), false).is_some());
    assert_eq!(retired(&idle), [0]);
    let opencode = Endpoint::of(Tool::OpenCode, &run, "opencode");
    // Two idle lanes are left, 540 MiB of what new lanes count at: short of 590.
    assert!(make_room(&run, &opencode, counted(1), false).is_none());
    assert_eq!(
        retired(&idle),
        [0],
        "a start that will not happen ends no lane"
    );
    // A third idle lane: the three pay for it, and all three leave.
    let mut said = status("t", "d", 50_000, false);
    said["memory_bytes"] = json!(110 * MIB);
    idle.push(FakeLane::start(&run, "i3", said));
    assert!(make_room(&run, &opencode, counted(1), false).is_some());
    assert_eq!(retired(&idle), [0, 1, 2, 3]);
    assert!(busy.asked().iter().all(|op| op == "inspect"));
    std::fs::remove_dir_all(&run).unwrap();
}

#[test]
fn where_the_limits_can_be_met_as_many_leave_as_meet_them_and_a_refusal_starts_nothing() {
    // Two busy lanes and three idle ones, for a count of four: with the new lane six, so two
    // idle lanes must leave. bd4a41ad2 stopped at one, because one idle Codex lane already pays
    // for a new one, and started a fifth lane over the count of four (issue 908).
    let run = run_dir("fits-two");
    let (idle, _busy) = idle_and_busy(&run, 3);
    let own = Endpoint::of(Tool::Codex, &run, "new");
    assert!(make_room(&run, &own, counted(4), false).is_some());
    assert_eq!(
        retired(&idle),
        [0, 1],
        "two leave, the fewest that meet the count"
    );
    std::fs::remove_dir_all(&run).unwrap();

    // The second of the two refuses (a turn claimed it after it was looked at). One left, which
    // pays for a Codex lane; bd4a41ad2 counted that as the new lane's place and started it over
    // the count. Within the limits only: the lane does not start.
    let run = run_dir("fits-refused");
    let mut said = status("t", "d", 100_000, false);
    said["memory_bytes"] = json!(110 * MIB);
    let first = FakeLane::start(&run, "i0", said.clone());
    said["idle_ms"] = json!(90_000);
    said["refuses"] = json!(true);
    let claimed = FakeLane::start(&run, "i1", said);
    let busy: Vec<FakeLane> = (0..2u64)
        .map(|at| FakeLane::start(&run, &format!("w{at}"), status("t", "d", 0, true)))
        .collect();
    assert!(make_room(&run, &own_of(&run), counted(3), false).is_none());
    assert_eq!(first.asked().last().unwrap(), "retire");
    assert_eq!(claimed.asked().last().unwrap(), "retire");
    assert!(claimed.endpoint.socket().exists(), "the claimed lane stays");
    assert!(busy.iter().all(|lane| lane.asked() == ["inspect"]));
    std::fs::remove_dir_all(&run).unwrap();
}

fn own_of(run: &Path) -> Endpoint {
    Endpoint::of(Tool::Codex, run, "new")
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

/// Which of `lanes`, by their place, were asked to leave.
fn retired(lanes: &[FakeLane]) -> Vec<usize> {
    lanes
        .iter()
        .enumerate()
        .filter(|(_, lane)| lane.asked().contains(&"retire".to_owned()))
        .map(|(n, _)| n)
        .collect()
}

/// The instance of a lane a look reaches.
fn seen(lane: &FakeLane) -> super::gate::Instance {
    lane.endpoint.inspect_seen().unwrap().1.unwrap()
}

/// Where a start the test plays itself, whose own lane does not matter, would start its lane.
fn nowhere() -> (&'static Path, u64) {
    (Path::new("/nonexistent/lane"), 0)
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
    assert_eq!(was(place), Was::Full);
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
    assert_eq!(was(place), Was::Free);
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
    assert_eq!(was(place), Was::Free);
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
    assert_eq!(was(place), Was::Free);
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
    assert!(make_room(&run, &own, room, false).is_some());
    assert_eq!(retired(&idle), [0, 1]);
    assert!(retired(&busy).is_empty());
    std::fs::remove_dir_all(&run).unwrap();

    // With plenty free, no count binds: a ceiling of three set by hand sends no idle lane away.
    let run = run_dir("auto-ceiling");
    let (idle, busy) = idle_and_busy(&run, 2);
    let own = Endpoint::of(Tool::Codex, &run, "new");
    let room = of("lane_ceiling = 3\n").room_with(Some(machine(9_000)));
    assert!(make_room(&run, &own, room, false).is_some());
    assert!(retired(&idle).is_empty());
    assert!(retired(&busy).is_empty());
    std::fs::remove_dir_all(&run).unwrap();

    // When even every idle lane leaving would not bring the machine above the reserve (other
    // programs took its memory), a chat lane takes the place of the one idle longest, which
    // pays for it, and starts; a goal's lane sends the rest away and starts (issue 908).
    let run = run_dir("auto-short");
    let (idle, busy) = idle_and_busy(&run, 2);
    let room = Settings::of(None).room_with(Some(machine(1_500)));
    let chat = Endpoint::of(Tool::Codex, &run, "chat");
    let place = chat
        .place(&run, Some("t"), "d", |_| false, room, false)
        .unwrap();
    assert_eq!(was(place), Was::Free);
    assert_eq!(retired(&idle), [0]);
    let goal = Endpoint::of(Tool::Codex, &run, "goal");
    let place = goal
        .place(&run, Some("t"), "d", |_| false, room, true)
        .unwrap();
    assert_eq!(was(place), Was::Free);
    assert_eq!(retired(&idle), [0, 1]);
    // No idle lane is left: the next chat lane does not start, and the busy ones stay.
    let next = Endpoint::of(Tool::Codex, &run, "next");
    let place = next
        .place(&run, Some("t"), "d", |_| false, room, false)
        .unwrap();
    assert_eq!(was(place), Was::Full);
    assert!(retired(&busy).is_empty());
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
    assert_eq!(was(place), Was::Free);
    assert!(retired(&busy).is_empty());
    // A ceiling in the owner's file (the live Hosts carry `lane_ceiling = 64`) bounds nothing in
    // auto: under a ceiling of 3, or of 24, the 25th starts too.
    for ceiling in [3, 24] {
        let set = of(&format!("lane_ceiling = {ceiling}\n"));
        assert_eq!(set.mode, Mode::Auto);
        assert_eq!(set.ceiling, ceiling);
        let room = set.room_with(Some(machine(6_000)));
        assert!(
            make_room(&run, &own, room, false).is_some(),
            "ceiling {ceiling}"
        );
        assert!(retired(&busy).is_empty(), "ceiling {ceiling}");
    }
    // Short of memory, the same 25th does not start: the reserve still binds.
    let short = Settings::of(None).room_with(Some(machine(2_100)));
    assert!(make_room(&run, &own, short, false).is_none());
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
fn a_lane_another_start_has_chosen_to_leave_is_not_asked_again() {
    // Over a count of one, another start has chosen the lane idle longest and not asked it yet:
    // this start takes the place of the next one, never the same (one ask to leave per lane).
    let run = run_dir("chosen-once");
    let (idle, _busy) = idle_and_busy(&run, 2);
    let socket = idle[0].endpoint.socket().to_owned();
    let looked = seen(&idle[0]);
    let mut other = super::gate::of(&run).look(None);
    other.choose(&[], |_| ((), vec![(socket.clone(), looked)]));
    let own = Endpoint::of(Tool::Codex, &run, "new");
    assert!(make_room(&run, &own, counted(1), false).is_some());
    assert_eq!(retired(&idle), [1]);
    let _ = other.settle(
        &[],
        &[(socket, looked, false)],
        |_, _| false,
        nowhere(),
        false,
    );
    std::fs::remove_dir_all(&run).unwrap();
}

#[test]
fn a_lane_that_left_for_one_start_is_no_place_for_another() {
    // Over a count of one, another start chose the lane idle longest and asked it, and it said
    // it goes; its tool is still on its way out, so it still answers as idle. bd4a41ad2
    // unmarked it once asked, so a start whose look came before it left chose it again, and
    // two lanes started in its one place. It stays marked until its socket is gone: this start
    // takes the place of the next one.
    let run = run_dir("left-once");
    let mut said = status("t", "d", 100_000, false);
    said["hangs"] = json!(true);
    let leaving = FakeLane::start(&run, "i0", said);
    let next = FakeLane::start(&run, "i1", status("t", "d", 90_000, false));
    let _busy = FakeLane::start(&run, "w", status("t", "d", 0, true));
    let socket = leaving.endpoint.socket().to_owned();
    let looked = seen(&leaving);
    let mut other = super::gate::of(&run).look(None);
    other.choose(&[], |_| ((), vec![(socket.clone(), looked)]));
    leaving
        .endpoint
        .ask::<Status>(&clarp_shim::lane::Lifecycle::Retire)
        .unwrap();
    let _ = other.settle(
        &[],
        &[(socket, looked, true)],
        |_, _| false,
        nowhere(),
        false,
    );
    assert!(make_room(&run, &own_of(&run), counted(1), false).is_some());
    assert_eq!(next.asked().last().unwrap(), "retire");
    let asked = leaving.asked().iter().filter(|op| *op == "retire").count();
    assert_eq!(asked, 1, "asked to leave once, by the start it left for");
    std::fs::remove_dir_all(&run).unwrap();
}

#[test]
fn a_lane_that_says_nothing_when_asked_to_leave_is_no_place() {
    // Over a count of one, a busy lane and an idle one that takes the ask to leave and says
    // nothing until it hangs up, its socket still there. bd4a41ad2 counted it as gone after
    // 500 ms, and the new lane started beside it. It did not leave: the lane does not start.
    // (One silent until the 30 s bound of a request ends the same way, and is not waited for
    // here.)
    let run = run_dir("quiet");
    let mut said = status("t", "d", 100_000, false);
    said["quiet"] = json!(true);
    let quiet = FakeLane::start(&run, "i0", said);
    let _busy = FakeLane::start(&run, "w", status("t", "d", 0, true));
    assert!(make_room(&run, &own_of(&run), counted(1), false).is_none());
    assert_eq!(quiet.asked().last().unwrap(), "retire");
    assert!(quiet.endpoint.socket().exists());
    std::fs::remove_dir_all(&run).unwrap();
}

#[test]
fn in_a_mixed_rollout_lanes_from_before_and_since_both_leave_and_silence_or_refusal_does_not() {
    // A Host update leaves running lanes with the shim they started with. A Muse or OpenCode lane
    // from before issue 908 keeps its socket while its tool ends, and says it goes only then,
    // past half a second. e6a9915e stopped listening after 500 ms and counted it as not left:
    // with it among the lanes that had to leave, the new lane did not start and its turn ran
    // cold. Its answer is waited for within a request's bound, as before 908, and counts.
    const ENDING: std::time::Duration = std::time::Duration::from_millis(800);
    let ending = json!(800);
    let lane = |run: &Path, agent: &str, idle_ms: u64, key: &str| {
        let mut said = status("t", "d", idle_ms, false);
        said[key] = if key.ends_with("_ms") {
            ending.clone()
        } else {
            json!(true)
        };
        FakeLane::start(run, agent, said)
    };
    // A count of two: the busy lane and the one about to start. Both idle lanes must leave, the
    // one from before asked first.
    let run = run_dir("mixed");
    let lanes = [
        lane(&run, "before", 100_000, "answers_after_ms"),
        lane(&run, "since", 90_000, "lingers_ms"),
    ];
    let _busy = FakeLane::start(&run, "w", status("t", "d", 0, true));
    let began = std::time::Instant::now();
    assert!(make_room(&run, &own_of(&run), counted(2), false).is_some());
    assert!(began.elapsed() >= ENDING, "the late answer was waited for");
    assert_eq!(retired(&lanes), [0, 1]);
    std::fs::remove_dir_all(&run).unwrap();

    // A lane started since counts by its answer, at once, while its tool is still ending and
    // its socket still there.
    let run = run_dir("mixed-early");
    let since = lane(&run, "since", 90_000, "lingers_ms");
    let _busy = FakeLane::start(&run, "w", status("t", "d", 0, true));
    let began = std::time::Instant::now();
    assert!(make_room(&run, &own_of(&run), counted(2), false).is_some());
    assert!(began.elapsed() < ENDING, "{:?}", began.elapsed());
    assert!(since.endpoint.socket().exists());
    std::fs::remove_dir_all(&run).unwrap();

    // Beside a lane from before that left, one that refuses or says nothing past half a second
    // still did not leave: the lane does not start.
    for (case, run) in [
        ("refuses", run_dir("mixed-refuses")),
        ("quiet", run_dir("mixed-quiet")),
    ] {
        let lanes = [
            lane(&run, "before", 100_000, "answers_after_ms"),
            lane(&run, case, 90_000, case),
        ];
        let _busy = FakeLane::start(&run, "w", status("t", "d", 0, true));
        assert!(
            make_room(&run, &own_of(&run), counted(2), false).is_none(),
            "{case}"
        );
        assert_eq!(retired(&lanes), [0, 1], "{case}");
        assert!(lanes[0].endpoint.released().unwrap(), "{case}: it left");
        assert!(lanes[1].endpoint.socket().exists(), "{case}");
        std::fs::remove_dir_all(&run).unwrap();
    }
}

/// What an idle lane asked to leave says when it goes: one line, its line end last.
fn it_goes() -> Vec<u8> {
    let mut frame = json!({"ok": true, "state": status("t", "d", 100_000, false)})
        .to_string()
        .into_bytes();
    frame.push(b'\n');
    frame
}

#[test]
fn a_lane_that_keeps_sending_part_of_its_answer_is_cut_off_at_the_bound_counted_from_the_ask() {
    // An answer that arrives in parts, slower than a look, counts once its line is whole.
    let (mut host, mut lane) = UnixStream::pair().unwrap();
    let answer = it_goes();
    let parts = std::thread::spawn(move || {
        for part in answer.chunks(answer.len() / 3) {
            lane.write_all(part).unwrap();
            std::thread::sleep(LEAVING_LOOK * 2);
        }
        lane
    });
    let far = std::time::Instant::now() + std::time::Duration::from_secs(10);
    assert!(said_it_goes(&mut host, Tool::Codex, far, || false));
    drop(parts.join().unwrap());

    // A lane that keeps sending a byte of it every few milliseconds and never ends its line.
    // 9bd36a19 read it with read_until, to which each byte was progress: no look ever ran out,
    // and the start waited as long as the lane kept sending. The bound does not move for what
    // arrives: the lane is cut off at it, and has not left.
    const BOUND: std::time::Duration = std::time::Duration::from_millis(300);
    let (mut host, mut lane) = UnixStream::pair().unwrap();
    let mut part = it_goes();
    part.pop();
    let trickle = std::thread::spawn(move || {
        // Until the Host hangs up.
        for byte in part.iter().cycle() {
            if lane.write_all(&[*byte]).is_err() {
                return;
            }
            std::thread::sleep(std::time::Duration::from_millis(5));
        }
    });
    let began = std::time::Instant::now();
    assert!(!said_it_goes(&mut host, Tool::Codex, began + BOUND, || {
        false
    }));
    let took = began.elapsed();
    assert!(took >= BOUND, "{took:?}");
    assert!(took < BOUND + std::time::Duration::from_secs(2), "{took:?}");
    drop(host);
    trickle.join().unwrap();
}

#[test]
fn an_answer_longer_than_any_can_be_is_none_and_is_read_no_further() {
    // The answer behind blank space, which a JSON reader skips: exactly as long as an answer
    // may be, line end included, it counts.
    let answer = it_goes();
    let most = usize::try_from(MAX_STATE).unwrap();
    let padded = |blank: usize| {
        let mut frame = vec![b' '; blank];
        frame.extend_from_slice(&answer);
        frame
    };
    let sent = |frame: Vec<u8>| {
        let (mut host, mut lane) = UnixStream::pair().unwrap();
        // The lane keeps its end open: whatever is decided is decided by what it sent.
        let writer = std::thread::spawn(move || {
            let _ = lane.write_all(&frame);
            lane
        });
        let began = std::time::Instant::now();
        let far = began + std::time::Duration::from_secs(20);
        let went = said_it_goes(&mut host, Tool::Codex, far, || false);
        let took = began.elapsed();
        drop(host);
        drop(writer.join().unwrap());
        (went, took)
    };
    assert!(sent(padded(most - answer.len())).0, "the longest answer");
    // One byte more: its first MAX_STATE bytes hold no line end. 9bd36a19 read on with
    // read_until, unbounded, to the line end and counted the answer; it is none, and the lane is
    // cut off once it has sent that much, not at the bound.
    let (went, took) = sent(padded(most - answer.len() + 1));
    assert!(!went);
    assert!(took < std::time::Duration::from_secs(10), "{took:?}");
}

#[test]
fn part_of_an_answer_its_lane_hung_up_after_is_none_whether_or_not_a_look_ran_out_first() {
    // A lane sends its answer but for the line end and hangs up, at once or after a look in
    // which nothing more came. That part is no answer either way: what decides is whether its
    // socket went, which the lane does here, or not, just before it hangs up. 9bd36a19 decided
    // by the socket only where a look ran out first; where it did not, it took the part for a
    // whole answer and found none, so a lane that went did not count.
    let mut part = it_goes();
    part.pop();
    for late in [false, true] {
        for goes in [false, true] {
            let (mut host, mut lane) = UnixStream::pair().unwrap();
            let gone = Arc::new(std::sync::atomic::AtomicBool::new(false));
            let socket = gone.clone();
            let part = part.clone();
            let writer = std::thread::spawn(move || {
                lane.write_all(&part).unwrap();
                if late {
                    std::thread::sleep(LEAVING_LOOK * 3);
                }
                socket.store(goes, std::sync::atomic::Ordering::SeqCst);
            });
            let far = std::time::Instant::now() + std::time::Duration::from_secs(10);
            let left = || gone.load(std::sync::atomic::Ordering::SeqCst);
            assert_eq!(
                said_it_goes(&mut host, Tool::Codex, far, left),
                goes,
                "late {late}, socket gone {goes}"
            );
            writer.join().unwrap();
        }
    }
}

#[test]
fn a_helpers_start_leaves_the_one_place_to_an_owners_start_still_choosing_and_waits_for_none() {
    // Seven busy lanes and one idle lane of the agents' own. A start of the owner's is counted at
    // the gate and still looking at the lanes when a helper's start chooses. bd4a41ad2 had the
    // helper's start wait for it up to 3 s and then choose as if his were not there, so with one
    // place it took the idle lane's, and his turn ran cold. The helper's start now leaves room
    // for his at once: where two places can be made it starts, where one can it does not.
    let run = run_dir("owner-ahead");
    let lanes = eight(&run, 7, &[]);
    let his_start = super::gate::of(&run).look(Some(CODEX_LANE));
    let helper = Endpoint::of(Tool::Codex, &run, "helper");
    assert!(
        make_room(&run, &helper, counted(10), false).is_some(),
        "room for both"
    );
    assert!(retired(&lanes).is_empty());
    assert!(
        make_room(&run, &helper, counted(8), false).is_none(),
        "one place: his"
    );
    assert!(retired(&lanes).is_empty());
    // His start chooses, and takes the place.
    drop(his_start);
    mark_owners_turn(&run, "his");
    let his = Endpoint::of(Tool::Codex, &run, "his");
    assert!(make_room(&run, &his, counted(8), false).is_some());
    assert_eq!(retired(&lanes), [7]);
    std::fs::remove_dir_all(&run).unwrap();
}

#[test]
fn an_owners_start_that_looked_before_a_helper_made_room_still_finds_the_room_left_for_it() {
    // A count of three: one busy lane and two idle ones. His start is counted and has looked
    // (three lanes and his) when a helper's start comes. Leaving room for his, the helper's
    // start sends both idle lanes away and starts. e1a523fa had his start choose by its look,
    // where both idle lanes were still there, both chosen and neither a place, so it came out
    // Full and his turn ran cold while the room left for it stood empty.
    let run = run_dir("stale-look");
    let busy = FakeLane::start(&run, "w", status("t", "d", 0, true));
    let idle = [
        FakeLane::start(&run, "i0", status("t", "d", 100_000, false)),
        FakeLane::start(&run, "i1", status("t", "d", 90_000, false)),
    ];
    mark_owners_turn(&run, "his");
    let his = Endpoint::of(Tool::Codex, &run, "his");
    let his_start = super::Start::look(&run, &his, counted(3));
    let helper = Endpoint::of(Tool::Codex, &run, "helper");
    let helpers = make_room(&run, &helper, counted(3), false);
    assert!(helpers.is_some(), "room for both");
    assert_eq!(retired(&idle), [0, 1]);
    let his_place = his_start.finish(false);
    assert!(his_place.is_some(), "the room left for his start is his");
    // The busy lane, the helper's and his: three, both still starting. A third start finds no
    // place, and nothing more is asked to leave.
    let late = Endpoint::of(Tool::Codex, &run, "late");
    assert!(make_room(&run, &late, counted(3), false).is_none());
    assert!(busy.asked().iter().all(|op| op == "inspect"));
    std::fs::remove_dir_all(&run).unwrap();
}

#[test]
fn a_lane_let_start_counts_until_it_listens_and_gives_its_place_back_when_its_launch_fails() {
    // A count of one and no lane yet. His start is given the place; its lane has not bound its
    // socket yet. e1a523fa saw no lane there, and let a helper's lane start beside it: two
    // lanes over a count of one.
    let run = run_dir("starting");
    mark_owners_turn(&run, "his");
    let his = Endpoint::of(Tool::Codex, &run, "his");
    let helper = Endpoint::of(Tool::Codex, &run, "helper");
    let admitted = make_room(&run, &his, counted(1), false).expect("his place");
    assert!(make_room(&run, &helper, counted(1), false).is_none());
    // His launch fails: the place is given back at once.
    drop(admitted);
    assert!(make_room(&run, &helper, counted(1), false).is_some());

    // Given again, through the start a turn makes. A helper's start that looked before his lane
    // listened still counts it once it does; one that looks after sees it there.
    let place = his
        .place(&run, Some("t"), "d", |_| false, counted(1), false)
        .unwrap();
    let Place::Free(admitted) = place else {
        panic!("his place");
    };
    let early = super::Start::look(&run, &helper, counted(1));
    let lane = FakeLane::start(&run, "his", status("t", "d", 0, true));
    admitted.visible();
    assert!(
        early.finish(false).is_none(),
        "it looked before his lane listened"
    );
    assert!(make_room(&run, &helper, counted(1), false).is_none());
    assert_eq!(lane.asked(), ["inspect"], "a busy lane is only looked at");
    std::fs::remove_dir_all(&run).unwrap();
}

#[test]
fn only_the_lane_looked_at_is_asked_to_leave_and_no_answer_about_it_touches_its_successor() {
    // A lane is looked at, leaves, and a new lane of the same agent takes its socket's name.
    let run = run_dir("instance");
    let old = FakeLane::start(&run, "a", status("t", "d", 100_000, false));
    // A second name keeps the old socket file's inode taken, so the new one cannot reuse it:
    // in this test both lanes are of this process, so their files alone tell them apart. The
    // look comes after it, as a new name changes the file's ctime.
    std::fs::hard_link(old.endpoint.socket(), run.join("a-old.keep")).unwrap();
    let looked = seen(&old);
    old.endpoint
        .ask::<Status>(&clarp_shim::lane::Lifecycle::Retire)
        .unwrap();
    let new = FakeLane::start(&run, "a", status("t", "d", 100_000, false));
    // The ask made for the lane looked at reaches no one: e1a523fa asked whatever listened on
    // the name, and the new lane left in the old one's stead.
    assert!(!new.endpoint.asked_to_leave(looked));
    assert!(new.asked().is_empty(), "{:?}", new.asked());
    // Another start chose the new lane. The old lane's completions, that it did not leave and
    // that it did, leave the new lane's mark alone: e1a523fa unmarked by name, and a third start
    // chose and asked the new lane while it was still another start's.
    let gate = super::gate::of(&run);
    let mut other = gate.look(None);
    let now = seen(&new);
    let socket = new.endpoint.socket().to_owned();
    other.choose(&[], |_| ((), vec![(socket.clone(), now)]));
    for left in [false, true] {
        let old_start = gate.look(None);
        let _ = old_start.settle(
            &[],
            &[(socket.clone(), looked, left)],
            |_, _| false,
            nowhere(),
            false,
        );
    }
    let _busy = FakeLane::start(&run, "w", status("t", "d", 0, true));
    let third = Endpoint::of(Tool::Codex, &run, "third");
    assert!(make_room(&run, &third, counted(1), false).is_none());
    assert!(!new.asked().contains(&"retire".to_owned()));
    let _ = other.settle(&[], &[(socket, now, false)], |_, _| false, nowhere(), false);
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
    assert_eq!(was(place), Was::Full);
    // A minute on, they have weighed what they hold: the same free memory lets the lane start.
    let _old: Vec<FakeLane> = young.into_iter().map(aged).collect();
    let place = chat
        .place(&run, Some("t"), "d", |_| false, room, false)
        .unwrap();
    assert_eq!(was(place), Was::Free);
    // A lane that has not weighed itself at all counts at its typical size however old it is.
    let run2 = run_dir("unweighed-young");
    let _unweighed = aged(FakeLane::start(&run2, "u", status("t", "d", 0, true)));
    let chat2 = Endpoint::of(Tool::Codex, &run2, "chat");
    let tight = Settings::of(None).room_with(Some(machine(2_400)));
    let place = chat2
        .place(&run2, Some("t"), "d", |_| false, tight, false)
        .unwrap();
    assert_eq!(was(place), Was::Full);
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

/// Issue 857. The Host's own looks at a lane's owner lock never refuse each other: one thread
/// looking while another looks still sees a lane that has left as gone. A lane that holds it is
/// there for every look.
#[test]
fn a_look_at_an_owner_lock_does_not_make_a_lane_that_left_seem_there() {
    let run = run_dir("looks");
    let lane = Endpoint::of(Tool::Claude, &run, "a");
    let lock = File::create(owner_lock(lane.socket())).unwrap();
    // Another thread of the Host looking at this moment.
    let looking = File::open(owner_lock(lane.socket())).unwrap();
    looking.try_lock_shared().unwrap();
    assert!(lane.released().unwrap(), "a look is no lane");
    drop(looking);
    // The lane itself. A lock let go of may stay held for a moment by a child that another test
    // of this process forks and has not yet replaced with its program, so the lane waits for
    // it, and the last look is given that moment.
    lock.lock().unwrap();
    assert!(!lane.released().unwrap());
    drop(lock);
    let until = std::time::Instant::now() + std::time::Duration::from_secs(5);
    assert!(lane.released_by(until).unwrap());
    std::fs::remove_dir_all(&run).unwrap();
}

/// Issue 857. A start in place of a lane that has just left, which still found its owner there,
/// is tried once more once the owner has let go, and never a third time.
#[test]
fn a_start_after_a_lane_left_is_tried_once_more_once_its_owner_let_go() {
    let run = run_dir("once-more-start");
    let lane = Endpoint::of(Tool::Claude, &run, "a");
    let owner = File::create(owner_lock(lane.socket())).unwrap();
    owner.lock().unwrap();
    // The owner lets go a moment after the first start found it there.
    let late = std::thread::spawn(move || {
        std::thread::sleep(std::time::Duration::from_millis(300));
        drop(owner);
    });
    let tries = std::cell::Cell::new(0);
    let started = start_once_more(&lane, std::time::Duration::from_secs(5), || {
        tries.set(tries.get() + 1);
        if lane.released()? {
            Ok("started")
        } else {
            Err(std::io::Error::other(
                "the Claude lane owner has not disconnected",
            ))
        }
    });
    late.join().unwrap();
    assert_eq!(started.unwrap(), "started");
    assert_eq!(tries.get(), 2);
    // A start that fails again is not tried a third time.
    let tries = std::cell::Cell::new(0);
    let failed = start_once_more(&lane, std::time::Duration::from_secs(1), || {
        tries.set(tries.get() + 1);
        Err::<(), _>(std::io::Error::other("did not start"))
    });
    assert!(failed.is_err());
    assert_eq!(tries.get(), 2);
    // One whose owner does not let go within the bound is not tried again.
    let held = File::create(owner_lock(lane.socket())).unwrap();
    held.lock().unwrap();
    let tries = std::cell::Cell::new(0);
    let kept = start_once_more(&lane, std::time::Duration::from_millis(200), || {
        tries.set(tries.get() + 1);
        Err::<(), _>(std::io::Error::other(
            "the Claude lane owner has not disconnected",
        ))
    });
    assert!(kept.is_err());
    assert_eq!(tries.get(), 1);
    drop(held);
    std::fs::remove_dir_all(&run).unwrap();
}

/// Issue 860. A lane on its way out (its socket gone, its owner lock still held while it ends its
/// tool) is waited for by a start, within the bound, rather than looked at once and given up on:
/// the start goes ahead once the lock is let go. One whose lock is not let go within the bound
/// still refuses the start.
#[test]
fn a_start_waits_for_a_lane_on_its_way_out_to_let_go() {
    let run = run_dir("on-its-way-out");
    let lane = Endpoint::of(Tool::Claude, &run, "a");
    let owner = File::create(owner_lock(lane.socket())).unwrap();
    owner.lock().unwrap();
    let late = std::thread::spawn(move || {
        std::thread::sleep(std::time::Duration::from_millis(400));
        drop(owner);
    });
    let began = std::time::Instant::now();
    let place = lane.place_within(
        &run,
        (Some("t"), "d"),
        |_| false,
        ROOMY,
        false,
        std::time::Duration::from_secs(5),
    );
    late.join().unwrap();
    assert_eq!(was(place.unwrap()), Was::Free);
    assert!(began.elapsed() >= std::time::Duration::from_millis(300));
    // Held past the bound by a process no lane is (this test's own): not ended, and refused.
    let held = File::create(owner_lock(lane.socket())).unwrap();
    held.lock().unwrap();
    let refused = lane.place_within(
        &run,
        (Some("t"), "d"),
        |_| false,
        ROOMY,
        false,
        std::time::Duration::from_millis(200),
    );
    assert_eq!(
        refused.unwrap_err().to_string(),
        "the Claude lane owner has not disconnected"
    );
    drop(held);
    std::fs::remove_dir_all(&run).unwrap();
}

/// Issue 860. A turn's start in place of a lane on its way out whose owner holds its lock for ten
/// seconds: the turn waits TURN_LEAVE_WAIT, about three seconds, never the ten, and goes on
/// without the lane (its start is refused, once, and the turn runs in a process of its own). The
/// owner is not ended for it, and the next turn's start, once it has let go, finds it gone.
#[test]
fn a_turn_does_not_wait_ten_seconds_for_an_owner_that_holds_its_lock() {
    let run = run_dir("ten-seconds");
    let lane = Endpoint::of(Tool::Claude, &run, "a");
    let owner = File::create(owner_lock(lane.socket())).unwrap();
    owner.lock().unwrap();
    let late = std::thread::spawn(move || {
        std::thread::sleep(std::time::Duration::from_secs(10));
        drop(owner);
    });
    let tries = std::cell::Cell::new(0);
    let began = std::time::Instant::now();
    let started = start_once_more(&lane, super::TURN_LEAVE_WAIT, || {
        tries.set(tries.get() + 1);
        lane.place_within(
            &run,
            (Some("t"), "d"),
            |_| false,
            ROOMY,
            false,
            super::LEAVE_WAIT,
        )
    });
    let waited = began.elapsed();
    assert_eq!(
        started.unwrap_err().to_string(),
        "the Claude lane owner has not disconnected"
    );
    assert_eq!(tries.get(), 1, "no retry while the owner holds");
    assert!(
        waited >= std::time::Duration::from_millis(2500)
            && waited < std::time::Duration::from_secs(5),
        "the turn waited about TURN_LEAVE_WAIT, not the owner's ten seconds: {waited:?}"
    );
    late.join().unwrap();
    let next = lane.place_within(
        &run,
        (Some("t"), "d"),
        |_| false,
        ROOMY,
        false,
        super::LEAVE_WAIT,
    );
    assert_eq!(was(next.unwrap()), Was::Free);
    std::fs::remove_dir_all(&run).unwrap();
}

/// Issue 860. A lane that hangs while leaving (its socket gone, its owner lock never let go)
/// holds a turn's start about TURN_LEAVE_WAIT, never LEAVE_WAIT's fifteen seconds; the turn then
/// runs on its own at once, and the lane is left as it is, not ended on the turn's time.
#[test]
fn a_lane_that_hangs_while_leaving_holds_a_turn_about_three_seconds_never_fifteen() {
    let run = run_dir("hangs-leaving");
    let lane = Endpoint::of(Tool::Claude, &run, "a");
    let owner = File::create(owner_lock(lane.socket())).unwrap();
    owner.lock().unwrap();
    let began = std::time::Instant::now();
    let started = start_once_more(&lane, super::TURN_LEAVE_WAIT, || {
        lane.place_within(
            &run,
            (Some("t"), "d"),
            |_| false,
            ROOMY,
            false,
            super::LEAVE_WAIT,
        )
    });
    let waited = began.elapsed();
    assert!(started.is_err());
    assert!(
        waited >= std::time::Duration::from_millis(2500)
            && waited < std::time::Duration::from_secs(5),
        "{waited:?}"
    );
    assert!(!lane.released().unwrap(), "the hung lane is left as it is");
    drop(owner);
    std::fs::remove_dir_all(&run).unwrap();
}

/// Issue 860. A lane that is starting holds its owner lock before it binds its socket (OpenCode's
/// for about 1.5 s, while it starts its server), and so looks like one on its way out. A start
/// that meets it waits, ends its wait as soon as the socket answers, and uses the lane: it is
/// never ended, and nothing but a look is asked of it.
#[test]
fn a_start_that_meets_a_lane_starting_keeps_it_once_its_socket_answers() {
    let run = run_dir("starting");
    let lane = Endpoint::of(Tool::Codex, &run, "a");
    let owner = File::create(owner_lock(lane.socket())).unwrap();
    owner.lock().unwrap();
    let (sender, started) = std::sync::mpsc::channel();
    let binding = run.clone();
    let binder = std::thread::spawn(move || {
        std::thread::sleep(std::time::Duration::from_millis(500));
        let fake = FakeLane::start_holding(&binding, "a", status("t", "d", 60_000, false), owner);
        let _ = sender.send(fake);
    });
    let began = std::time::Instant::now();
    let place = lane.place_within(
        &run,
        (Some("t"), "d"),
        |_| false,
        ROOMY,
        false,
        super::LEAVE_WAIT,
    );
    let waited = began.elapsed();
    binder.join().unwrap();
    let fake = started.recv().unwrap();
    assert_eq!(
        was(place.unwrap()),
        Was::Kept,
        "the lane that started is the one used"
    );
    assert!(
        waited < std::time::Duration::from_secs(2),
        "the wait ended when the socket answered: {waited:?}"
    );
    assert_eq!(
        fake.asked(),
        ["inspect"],
        "nothing but a look was asked of it"
    );
    assert!(!lane.released().unwrap(), "it was never ended");
    assert!(lane.retire_within(super::LEAVE_WAIT).unwrap());
    std::fs::remove_dir_all(&run).unwrap();
}

/// Sends a lane one request and keeps the connection, unanswered, as the Host's ask to leave is.
fn ask_to_leave(lane: &Endpoint) -> std::os::unix::net::UnixStream {
    let mut stream = std::os::unix::net::UnixStream::connect(lane.socket()).unwrap();
    writeln!(stream, "{}", json!({"op": "retire"})).unwrap();
    stream
}

/// Waits for a lane's socket to go, which is when it begins leaving.
fn began_leaving(lane: &Endpoint) -> std::time::Instant {
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(10);
    while lane.socket().exists() {
        assert!(
            std::time::Instant::now() < deadline,
            "the lane never began leaving"
        );
        std::thread::sleep(std::time::Duration::from_millis(10));
    }
    std::time::Instant::now()
}

/// Issue 861 (1), and the ending's clock. An idle lane that holds the turn's conversation is
/// retired as a turn's must be: the turn waits while the lane is still busy in its loop, and
/// from when its socket goes gives it EXIT_LIMIT and a second (TURN_LEAVE_WAIT) before what is
/// left of it is ended; nothing is ended before that. A lane that runs work refuses at once, is
/// never ended, and the turn queues.
#[test]
fn a_turns_retirement_ends_an_idle_lane_only_three_seconds_after_it_began_leaving() {
    let run = run_dir("turn-retire");
    // Busy in its loop for a second after the ask, then leaving, and hung there.
    let lane = StuckLane::start_leaving(
        &run,
        "a",
        saying(false),
        "tool",
        Some(std::time::Duration::from_secs(1)),
    );
    let endpoint = lane.endpoint.clone();
    let asked = std::time::Instant::now();
    let retiring = std::thread::spawn(move || endpoint.retire_for_a_turn());
    let left = began_leaving(&lane.endpoint);
    assert!(left - asked >= std::time::Duration::from_millis(900));
    // EXIT_LIMIT and a second from when its socket went: nothing is ended before.
    std::thread::sleep(std::time::Duration::from_millis(2500));
    assert!(
        lane.ended.try_recv().is_err(),
        "ended before EXIT_LIMIT and a second after it began leaving"
    );
    assert!(
        retiring.join().unwrap().unwrap(),
        "what was left of it was ended"
    );
    let ended_after = left.elapsed();
    let exit = lane
        .ended
        .recv_timeout(std::time::Duration::from_secs(5))
        .unwrap();
    assert_eq!(
        std::os::unix::process::ExitStatusExt::signal(&exit),
        Some(nix::libc::SIGKILL)
    );
    assert!(ended_after >= super::TURN_LEAVE_WAIT, "{ended_after:?}");
    assert!(
        ended_after < std::time::Duration::from_secs(8),
        "{ended_after:?}"
    );
    // A lane that runs work refuses to leave: an error, so the turn queues, and nothing ended.
    let busy = FakeLane::start(&run, "busy", status("t", "d", 0, true));
    let began = std::time::Instant::now();
    assert!(busy.endpoint.retire_for_a_turn().is_err());
    assert!(began.elapsed() < std::time::Duration::from_secs(3));
    assert_eq!(busy.asked(), ["inspect", "retire"]);
    assert!(!busy.endpoint.released().unwrap(), "it was not ended");
    // One that works and is slow to answer is not ended either, however long it takes.
    let working = StuckLane::start(&run, "working", saying(true), "tool");
    let slow = working.endpoint.retire_for_a_turn_within(
        std::time::Duration::from_millis(500),
        super::TURN_LEAVE_WAIT,
    );
    assert!(slow.is_err());
    assert!(
        working
            .ended
            .recv_timeout(std::time::Duration::from_millis(500))
            .is_err()
    );
    for (id, group) in [(working.group, true), (working.pid, false)] {
        let target = nix::unistd::Pid::from_raw(i32::try_from(id).unwrap());
        if group {
            let _ = nix::sys::signal::killpg(target, nix::sys::signal::Signal::SIGKILL);
        } else {
            let _ = nix::sys::signal::kill(target, nix::sys::signal::Signal::SIGKILL);
        }
    }
    std::fs::remove_dir_all(&run).unwrap();
}

/// Issue 861 (2). A chat turn whose Codex lane refused it for its login waits at most
/// TURN_LEAVE_WAIT for that lane to let go, ends nothing, and gets the refusal back so it runs
/// in a process of its own; the lane is left to leave on its own.
#[test]
fn a_refused_chat_turn_waits_three_seconds_and_runs_on_its_own_ending_nothing() {
    let run = run_dir("refused-chat");
    let lane = Endpoint::of(Tool::Codex, &run, "a");
    let spool = run.join("spool");
    // The refusing lane on its way out: its socket gone, its lock held.
    let owner = File::create(owner_lock(lane.socket())).unwrap();
    owner.lock().unwrap();
    let again = std::cell::Cell::new(false);
    let began = std::time::Instant::now();
    let refused = std::io::Error::other(super::LoginChanged("login".into()));
    let result = once_more(Err::<u8, _>(refused), &spool, &lane, true, || {
        again.set(true);
        Ok(2)
    });
    let waited = began.elapsed();
    assert!(
        LoginChanged::is(&result.unwrap_err()),
        "the refusal comes back"
    );
    assert!(
        !again.get(),
        "no second offer to a lane that has not let go"
    );
    assert!(
        waited >= std::time::Duration::from_millis(2500)
            && waited < std::time::Duration::from_secs(5),
        "{waited:?}"
    );
    assert!(!lane.released().unwrap(), "nothing was ended");
    drop(owner);
    std::fs::remove_dir_all(&run).unwrap();
}

/// Issue 861 (3). A turn's start that must retire an idle lane started otherwise: one that holds
/// the turn's conversation (the same thread) is retired as a turn's must be, and ended only
/// TURN_LEAVE_WAIT after its socket went, never run beside; one of another thread holds the turn
/// at most TURN_LEAVE_WAIT, and its retirement carries on without the turn.
#[test]
fn a_starts_retirement_of_a_lane_started_otherwise_follows_whose_conversation_it_holds() {
    let run = run_dir("place-retire");
    // The same thread: it begins leaving at once and hangs.
    let same = StuckLane::start_leaving(
        &run,
        "a",
        saying(false),
        "tool",
        Some(std::time::Duration::ZERO),
    );
    let began = std::time::Instant::now();
    let place = same.endpoint.place_within(
        &run,
        (Some("t"), "new"),
        |_| false,
        ROOMY,
        false,
        super::LEAVE_WAIT,
    );
    let waited = began.elapsed();
    assert_eq!(
        was(place.unwrap()),
        Was::Free,
        "never beside it: it was ended first"
    );
    assert!(
        waited >= std::time::Duration::from_millis(2900),
        "{waited:?}"
    );
    assert!(waited < std::time::Duration::from_secs(8), "{waited:?}");
    same.ended
        .recv_timeout(std::time::Duration::from_secs(5))
        .unwrap();
    // Another thread: it never leaves; the turn goes on after about TURN_LEAVE_WAIT, and the
    // lane is not ended on the turn's time.
    let other = StuckLane::start(&run, "b", saying(false), "tool");
    let began = std::time::Instant::now();
    let place = other.endpoint.place_within(
        &run,
        (Some("u"), "new"),
        |_| false,
        ROOMY,
        false,
        super::LEAVE_WAIT,
    );
    let waited = began.elapsed();
    assert!(place.is_err(), "{place:?}");
    assert!(
        waited >= std::time::Duration::from_millis(2900)
            && waited < std::time::Duration::from_secs(5),
        "{waited:?}"
    );
    assert!(
        other.ended.try_recv().is_err(),
        "not ended on the turn's time"
    );
    for (id, group) in [(other.group, true), (other.pid, false)] {
        let target = nix::unistd::Pid::from_raw(i32::try_from(id).unwrap());
        if group {
            let _ = nix::sys::signal::killpg(target, nix::sys::signal::Signal::SIGKILL);
        } else {
            let _ = nix::sys::signal::kill(target, nix::sys::signal::Signal::SIGKILL);
        }
    }
    std::fs::remove_dir_all(&run).unwrap();
}

/// Issue 861 (4). A lane that hangs while leaving, which a turn gave up on, is ended in the
/// background once LEAVE_WAIT has passed, only while its socket still does not answer and its
/// lock is still held; the next start then finds room for a fresh lane. A lane whose socket
/// answers is never ended.
#[test]
fn a_lane_hung_while_leaving_is_ended_in_the_background_and_one_that_answers_never() {
    let run = run_dir("hung-end");
    let hung = StuckLane::start_leaving(
        &run,
        "a",
        saying(false),
        "tool",
        Some(std::time::Duration::ZERO),
    );
    let _asked = ask_to_leave(&hung.endpoint);
    began_leaving(&hung.endpoint);
    // LEAVE_WAIT has passed (the clock given is now).
    assert!(hung.endpoint.end_if_hung_at(std::time::Instant::now()));
    hung.ended
        .recv_timeout(std::time::Duration::from_secs(5))
        .unwrap();
    let next = hung.endpoint.place_within(
        &run,
        (Some("t"), "old"),
        |_| false,
        ROOMY,
        false,
        super::LEAVE_WAIT,
    );
    assert_eq!(was(next.unwrap()), Was::Free, "a fresh lane may start");
    // A lane whose socket answers (one that was starting, or never left) is never ended.
    let answers = FakeLane::start(&run, "b", status("t", "d", 50, false));
    assert!(!answers.endpoint.end_if_hung_at(std::time::Instant::now()));
    assert!(!answers.endpoint.released().unwrap(), "it was not ended");
    assert_eq!(answers.asked(), ["inspect"]);
    std::fs::remove_dir_all(&run).unwrap();
}
