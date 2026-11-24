#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::fs::File;
use std::io::{BufRead, BufReader, Write};
use std::os::unix::net::UnixListener;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use clarp_shim::lane::{LOGIN_CHANGED, Status, owner_lock};
use serde_json::{Value, json};

use super::{Endpoint, Place, Settings, Tool, make_room, once_more, replied};

const MIB: u64 = 1024 * 1024;
/// Room for everything the tests' lanes hold.
const ROOMY: u64 = u64::MAX;
/// What a Codex lane counts at when it says nothing and cannot be read.
const CODEX_LANE: u64 = 270 * MIB;

fn of(text: &str) -> Settings {
    Settings::of(Some(&text.parse::<toml::Table>().unwrap()))
}

#[test]
fn the_lane_settings_are_off_and_fifteen_minutes_and_2200_mb_unless_the_owner_says() {
    let default = Settings {
        chat: [false],
        idle_ms: 15 * 60_000,
        budget: 2_200 * MIB,
    };
    assert_eq!(Settings::of(None), default);
    assert_eq!(of(""), default);
    assert!(!default.chat(Tool::Codex));
    let set = of("codex_chat_lane = true\ncodex_lane_idle_minutes = 2\nlane_memory_mb = 3000\n");
    assert_eq!(
        set,
        Settings {
            chat: [true],
            idle_ms: 120_000,
            budget: 3_000 * MIB,
        }
    );
    assert!(set.chat(Tool::Codex));
    // What is no setting keeps the default: a word, zero or less, or a key inside a table.
    assert_eq!(
        of("codex_chat_lane = \"yes\"\ncodex_lane_idle_minutes = 0\nlane_memory_mb = -1\n"),
        default
    );
    assert_eq!(
        of("[other]\ncodex_chat_lane = true\nlane_memory_mb = 9\n"),
        default
    );
    // The default budget holds what the count of eight held: eight Codex lanes.
    assert_eq!(default.budget / CODEX_LANE, 8);
}

#[test]
fn the_count_of_lanes_is_no_longer_read_and_a_config_that_has_it_is_told() {
    // An installed config with the old count: the budget alone decides.
    let old = of("codex_chat_lane = true\ncodex_lane_most = 2\n");
    assert_eq!(old.budget, 2_200 * MIB);
    let said = Settings::replaced(Some(&"codex_lane_most = 2\n".parse().unwrap())).unwrap();
    assert!(
        said.starts_with("codex_lane_most = 2 in config.toml is no longer read"),
        "{said}"
    );
    assert!(said.contains("lane_memory_mb"), "{said}");
    assert_eq!(
        Settings::replaced(Some(&"lane_memory_mb = 9\n".parse().unwrap())),
        None
    );
    assert_eq!(Settings::replaced(None), None);
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
        Self::start_into(run, agent, status, Arc::new(Mutex::new(Vec::new())))
    }

    /// As [`FakeLane::start`], and every op it is asked is also kept in `all`, as `agent:op`,
    /// in the order the lanes were asked.
    fn start_into(run: &Path, agent: &str, status: Value, all: Arc<Mutex<Vec<String>>>) -> Self {
        let name = agent.to_owned();
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
                all.lock().unwrap().push(format!("{name}:{op}"));
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
    // Three live lanes, none read, each at a Codex lane's typical size; the new one makes four,
    // and a budget of three Codex lanes: one leaves.
    make_room(&run, &own, 3 * CODEX_LANE);
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
    let place = same.endpoint.place(&run, Some("t"), "d", |_| false, ROOMY);
    assert_eq!(place.unwrap(), Place::Kept);
    assert_eq!(same.asked(), ["inspect"]);

    // What the driver says the tool kept from an earlier turn starts it again.
    let place = same.endpoint.place(&run, Some("t"), "d", |_| true, ROOMY);
    assert_eq!(place.unwrap(), Place::Free);
    assert_eq!(same.asked(), ["inspect", "inspect", "retire"]);

    let other = FakeLane::start(&run, "b", status("t", "old", 5, false));
    let place = other
        .endpoint
        .place(&run, Some("t"), "new", |_| false, ROOMY);
    assert_eq!(place.unwrap(), Place::Free);
    assert_eq!(other.asked(), ["inspect", "retire"]);
    assert!(other.endpoint.released().unwrap());

    // A lane that still runs work is used as it is for its own thread, and refuses another.
    let busy = FakeLane::start(&run, "c", status("t", "old", 0, true));
    let place = busy
        .endpoint
        .place(&run, Some("t"), "new", |_| false, ROOMY);
    assert_eq!(place.unwrap(), Place::Kept);
    let refused = busy
        .endpoint
        .place(&run, Some("u"), "new", |_| false, ROOMY);
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
    // Each says it is the same live process: what it holds is real, and more than a byte.
    let mut sleeper = std::process::Command::new("sleep")
        .arg("60")
        .spawn()
        .unwrap();
    let me = sleeper.id();
    let all = Arc::new(Mutex::new(Vec::new()));
    let shorter = FakeLane::start_into(&run, "a", status_of(me, "ta", "d", 10, false), all.clone());
    let longer = FakeLane::start_into(&run, "b", status_of(me, "tb", "d", 30, false), all.clone());
    let busy = FakeLane::start_into(&run, "c", status_of(me, "tc", "d", 0, true), all.clone());
    let own = Endpoint::of(Tool::Codex, &run, "new");
    // A budget nothing fits and room for all by their number: every idle lane leaves, the
    // longest idle first, and the busy one counts and stays.
    make_room(&run, &own, 1);
    let retired: Vec<String> = all
        .lock()
        .unwrap()
        .iter()
        .filter(|op| op.ends_with(":retire"))
        .cloned()
        .collect();
    assert_eq!(retired, ["b:retire", "a:retire"]);
    assert_eq!(busy.asked(), ["inspect"]);
    assert!(shorter.endpoint.released().unwrap() && longer.endpoint.released().unwrap());

    // A budget all fit in: nobody leaves.
    let kept = FakeLane::start(&run, "d", status_of(me, "td", "d", 50, false));
    make_room(&run, &own, ROOMY);
    assert_eq!(kept.asked(), ["inspect"]);
    sleeper.kill().unwrap();
    sleeper.wait().unwrap();
    std::fs::remove_dir_all(&run).unwrap();
}

#[test]
fn the_budget_counts_each_lanes_own_reading_and_the_new_lane_at_its_typical_size() {
    let run = run_dir("bytes");
    let said = |idle_ms: u64, bytes: u64| {
        let mut status = status("t", "d", idle_ms, false);
        status["memory_bytes"] = json!(bytes);
        status
    };
    let all = Arc::new(Mutex::new(Vec::new()));
    let _shorter = FakeLane::start_into(&run, "a", said(10, 100 * MIB), all.clone());
    let _longer = FakeLane::start_into(&run, "b", said(30, 100 * MIB), all.clone());
    let own = Endpoint::of(Tool::Codex, &run, "new");
    // 100 + 100 said by the lanes, and the new Codex lane at 270: over 400, so the longest idle
    // leaves, and 370 fits.
    make_room(&run, &own, 400 * MIB);
    let retired: Vec<String> = all
        .lock()
        .unwrap()
        .iter()
        .filter(|op| op.ends_with(":retire"))
        .cloned()
        .collect();
    assert_eq!(retired, ["b:retire"]);
    std::fs::remove_dir_all(&run).unwrap();
}

#[test]
fn a_driver_says_which_attempts_it_takes_and_what_starts_it_again() {
    use super::Driver;
    use super::codex::Lane;
    use clarp_actor::{OpeningOrder, ShimOrder};
    use clarp_kernel::turn::TurnId;

    let mut order = ShimOrder {
        opening: None,
        named_session: None,
        model: None,
        effort: None,
        prompt_as_file: false,
        streams_input: false,
        before_stop: None,
        turn: TurnId(7),
        attempt: 1,
        spool: PathBuf::from("/s"),
        socket: PathBuf::from("/t"),
        stdin_file: PathBuf::from("/i"),
        cwd: PathBuf::from("/w"),
        program: "codex".into(),
        args: Vec::new(),
        env: Vec::new(),
    };
    // Codex takes every attempt but an opening turn, which keeps its own gated launch.
    assert!(Tool::Codex.takes(&order));
    order.opening = Some(Box::new(OpeningOrder {
        id: "o".into(),
        trace: "t".into(),
        previous_unsent: false,
    }));
    assert!(!Tool::Codex.takes(&order));

    // Codex takes a model and effort with a turn, in place; only asking for its defaults after
    // it was given its own starts it again.
    let state = |model: Option<&str>, effort: Option<&str>| -> Status {
        let mut value = status("t", "d", 0, false);
        value["model"] = json!(model);
        value["effort"] = json!(effort);
        serde_json::from_value(value).unwrap()
    };
    let m = Some("m".to_owned());
    assert!(!Lane::restart_for(&state(None, None), &m, &None));
    assert!(!Lane::restart_for(&state(Some("x"), None), &m, &None));
    assert!(Lane::restart_for(&state(Some("x"), None), &None, &None));
    assert!(Lane::restart_for(&state(None, Some("high")), &m, &None));
}

#[test]
fn before_anything_else_opens_the_conversation_its_lanes_leave_or_it_does_not_go_ahead() {
    use super::{Touch, make_way};
    let run = run_dir("way");
    let idle = FakeLane::start(&run, "a", status("t", "d", 5, false));
    make_way(&run, "a", Touch::Exec).unwrap();
    assert_eq!(idle.asked(), ["retire"]);
    assert!(idle.endpoint.released().unwrap());
    // No lane at all: nothing to wait for.
    make_way(&run, "nobody", Touch::Fork).unwrap();
    // A lane that still runs a turn refuses, and says who waited for it.
    let busy = FakeLane::start(&run, "b", status("t", "d", 0, true));
    let refused = make_way(&run, "b", Touch::Terminal).unwrap_err();
    assert!(
        refused
            .to_string()
            .starts_with("Terminal waits for the Codex lane:"),
        "{refused}"
    );
    assert!(!busy.endpoint.released().unwrap());
    std::fs::remove_dir_all(&run).unwrap();
}

#[test]
fn a_turn_waits_for_a_cold_codex_lane_and_a_warm_lane_is_one_that_answers_free() {
    use super::Cold;
    // Codex as before the layer: its turn waits while a new lane starts.
    assert_eq!(Tool::Codex.cold(), Cold::Waits);
    let run = run_dir("warm");
    let free = FakeLane::start(&run, "a", status("t", "d", 5, false));
    let busy = FakeLane::start(&run, "b", status("t", "d", 0, true));
    assert!(free.endpoint.warm());
    assert!(!busy.endpoint.warm());
    assert!(!Endpoint::of(Tool::Codex, &run, "none").warm());
    std::fs::remove_dir_all(&run).unwrap();
}

#[test]
fn a_per_turn_process_holds_its_conversation_shared_and_never_waits() {
    use clarp_actor::ShimOrder;
    use clarp_kernel::turn::TurnId;
    let run = run_dir("guard");
    let mut order = ShimOrder {
        opening: None,
        named_session: None,
        model: None,
        effort: None,
        prompt_as_file: false,
        streams_input: false,
        before_stop: None,
        turn: TurnId(7),
        attempt: 1,
        spool: PathBuf::from("/s"),
        socket: PathBuf::from("/t"),
        stdin_file: PathBuf::from("/i"),
        cwd: PathBuf::from("/w"),
        program: "codex".into(),
        args: vec!["exec".into()],
        env: Vec::new(),
    };
    super::guard_exec(&mut order, &run, Tool::Codex, "thread-1").unwrap();
    let lock = clarp_shim::lane::thread_lock(&run.join("codex-threads"), "thread-1");
    // The words Codex's exec turns have been started with since before the layer.
    assert_eq!(order.program, "flock");
    assert_eq!(
        order.args,
        [
            "--shared",
            "--nonblock",
            "--no-fork",
            lock.to_str().unwrap(),
            "codex",
            "exec"
        ]
    );
    std::fs::remove_dir_all(&run).unwrap();
}
