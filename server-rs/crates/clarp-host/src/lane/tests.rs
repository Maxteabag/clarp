#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::fs::File;
use std::io::{BufRead, BufReader, Write};
use std::os::unix::net::UnixListener;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use clarp_shim::lane::{LOGIN_CHANGED, Status, owner_lock};
use serde_json::{Value, json};

use super::{
    Endpoint, Place, Room, Settings, Tool, held, lanes_in, make_room, make_way_within,
    mark_owners_turn, mark_owners_view, once_more, replied,
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
};

/// Only the count limits.
fn counted(most: usize) -> Room {
    Room {
        most,
        budget: u64::MAX,
    }
}

/// Only the budget limits.
fn in_bytes(budget: u64) -> Room {
    Room {
        most: usize::MAX,
        budget,
    }
}

#[test]
fn the_lane_settings_are_off_fifteen_minutes_eight_lanes_and_2200_mb_unless_the_owner_says() {
    let default = Settings {
        chat: [false, false],
        idle_ms: 15 * 60_000,
        most: 8,
        budget: 2_200 * MIB,
    };
    assert_eq!(Settings::of(None), default);
    assert_eq!(of(""), default);
    assert!(!default.chat(Tool::Codex));
    assert!(!default.chat(Tool::Claude));
    let set = of(
        "codex_chat_lane = true\ncodex_lane_idle_minutes = 2\ncodex_lane_most = 3\nlane_memory_mb = 3000\n",
    );
    assert_eq!(
        set,
        Settings {
            chat: [true, false],
            idle_ms: 120_000,
            most: 3,
            budget: 3_000 * MIB,
        }
    );
    assert!(set.chat(Tool::Codex));
    assert_eq!(
        set.room(),
        Room {
            most: 3,
            budget: 3_000 * MIB
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
    make_room(&run, &own, counted(3));
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
    assert_eq!(same.asked(), ["inspect", "inspect", "retire"]);

    let other = FakeLane::start(&run, "b", status("t", "old", 5, false));
    let place = other
        .endpoint
        .place(&run, Some("t"), "new", |_| false, ROOMY, false);
    assert_eq!(place.unwrap(), Place::Free);
    assert_eq!(other.asked(), ["inspect", "retire"]);
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
    make_room(&run, &own, in_bytes(500 * MIB));
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
    make_room(&run, &own, in_bytes(Settings::of(None).budget));
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
    make_room(&run, &eighth, budget);
    assert!(seven.iter().all(|lane| lane.asked() == ["inspect"]));
    // With eight running, a ninth sends the one idle the longest away, and only it.
    let running = FakeLane::start(&run, "eighth", weighed(1));
    let ninth = Endpoint::of(Tool::Codex, &run, "ninth");
    make_room(&run, &ninth, budget);
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
    make_room(&run, &own, Settings::of(None).room());
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
        },
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
    make_room(&run, &own, Settings::of(None).room());
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
fn the_terminal_opens_after_the_bound_when_an_idle_lane_never_lets_go() {
    let run = run_dir("hangs");
    let mut said = status("t", "d", 50, false);
    said["hangs"] = json!(true);
    let hanging = FakeLane::start(&run, "agent", said);
    let started = std::time::Instant::now();
    make_way_within(&run, "agent", std::time::Duration::from_millis(300));
    let waited = started.elapsed();
    // Asked to leave, waited for at most the bound, and the terminal goes on.
    assert_eq!(hanging.asked(), ["retire"]);
    assert!(
        waited >= std::time::Duration::from_millis(300),
        "{waited:?}"
    );
    assert!(waited < std::time::Duration::from_secs(5), "{waited:?}");
    assert!(!hanging.endpoint.released().unwrap());
    // An idle lane that lets go is waited for until it has, and no longer.
    let leaving = FakeLane::start(&run, "other", status("t", "d", 50, false));
    make_way_within(&run, "other", std::time::Duration::from_secs(5));
    assert_eq!(leaving.asked(), ["retire"]);
    assert!(leaving.endpoint.released().unwrap());
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
    let defaults = Settings::of(None).room();
    // A chat turn's lane is optional: it is not started, and nothing is asked to leave.
    let chat = Endpoint::of(Tool::Codex, &run, "chat");
    let place = chat
        .place(&run, Some("t"), "d", |_| false, defaults, false)
        .unwrap();
    assert_eq!(place, Place::Full);
    assert!(busy.iter().all(|lane| lane.asked() == ["inspect"]));
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
    let room = Settings::of(None).room();
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
    let room = Settings::of(None).room();
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
