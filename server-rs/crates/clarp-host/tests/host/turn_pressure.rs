//! The Host's tests of turns held while the machine is under pressure (`crate::pressure`,
//! feature `turn_pressure_admission`). The pressure is the place's own files, written as the
//! kernel writes `/proc/pressure`. One file a piece, declared in `host.rs`.

use super::*;

/// The Host keeps its turns' spools: one there says a shim started for that turn, also after the
/// turn has ended.
const KEEP: &[&str] = &["--keep-spools"];

/// Writes the place's pressure files: io `some` avg60 at `io` percent, the rest calm.
fn pressure(place: &Place, io: f64) {
    let dir = place.pressure();
    std::fs::create_dir_all(&dir).unwrap();
    let calm = "some avg10=0.00 avg60=0.00 avg300=0.00 total=0\n\
                full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n";
    for (name, text) in [
        ("cpu", calm.to_owned()),
        (
            "io",
            format!(
                "some avg10={io:.2} avg60={io:.2} avg300=0.00 total=1\n\
                 full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"
            ),
        ),
        ("memory", calm.to_owned()),
    ] {
        let staged = dir.join(format!("{name}.next"));
        std::fs::write(&staged, text).unwrap();
        std::fs::rename(staged, dir.join(name)).unwrap();
    }
}

/// A scripted agent, and the folder its turns' spools are made in: one is there once a turn's
/// shim has started.
fn agent(place: &Place, host: &Host, name: &str) -> (String, PathBuf) {
    let session = host.new_agent(name);
    let agent_id = host.get("/agents/snapshot")["agents"]
        .as_array()
        .unwrap()
        .iter()
        .find(|agent| agent["session"] == session)
        .map(|agent| agent["agent_id"].as_str().unwrap().to_string())
        .unwrap();
    let spools = place.root.join("data/agents").join(agent_id).join("spool");
    (session, spools)
}

/// How many turns of the agent a shim has started for: the spools it writes (`<turn>.jsonl`).
/// The Host writes a turn's prompt beside them (`<turn>.stdin`) before it decides whether the
/// turn starts, so that is no start.
fn shims(spools: &std::path::Path) -> usize {
    std::fs::read_dir(spools).map_or(0, |entries| {
        entries
            .flatten()
            .filter(|entry| entry.path().extension().is_some_and(|ext| ext == "jsonl"))
            .count()
    })
}

/// Whether a shim has started for a turn of the agent.
fn started(spools: &std::path::Path) -> bool {
    shims(spools) > 0
}

/// What the spool folder holds, and what the Hosts said: for a failure's words.
fn seen(spools: &std::path::Path) -> String {
    let names: Vec<String> = std::fs::read_dir(spools)
        .map(|entries| {
            entries
                .flatten()
                .map(|entry| entry.file_name().to_string_lossy().into_owned())
                .collect()
        })
        .unwrap_or_default();
    format!("spools {names:?}{}", what_the_hosts_said())
}

/// A send of the given origin, as an app or a tool names it.
fn send_as(host: &Host, session: &str, text: &str, id: &str, origin: &str) {
    host.post(
        "/send",
        json!({"session": session, "text": text, "client_msg_id": id, "origin": origin}),
    );
}

fn held(host: &Host) -> u64 {
    host.get("/lane-settings")["held_for_pressure"]
        .as_u64()
        .unwrap()
}

#[test]
fn a_turn_nobody_waits_on_waits_under_pressure_and_starts_when_it_falls() {
    let place = Place::new("pressure-hold");
    let host = place.start_with("", KEEP);
    let (session, spools) = agent(&place, &host, "Helper");
    pressure(&place, 45.0);
    let settings = host.get("/lane-settings");
    assert_eq!(settings["pressure"]["high"], true, "{settings}");
    assert_eq!(settings["pressure"]["io"]["some"]["avg60"], 45.0);
    assert_eq!(settings["pressure"]["thresholds"]["io_some_avg60"], 30);

    send_as(&host, &session, "a scheduled chore", "s1", "schedule");
    wait_for("the turn to wait for the machine", || held(&host) == 1);
    std::thread::sleep(Duration::from_millis(1500));
    assert!(
        !started(&spools),
        "a turn nobody waits on started under pressure: {}",
        seen(&spools)
    );

    // Pressure that falls only to 25 is not below 80% of 30: the turn still waits.
    pressure(&place, 25.0);
    std::thread::sleep(Duration::from_secs(6));
    assert!(
        !started(&spools),
        "the turn started while the machine was still under pressure"
    );
    assert_eq!(held(&host), 1);

    pressure(&place, 5.0);
    wait_for("the turn to start once the pressure fell", || {
        started(&spools)
    });
    wait_for("the turn to end", || host.state(&session) == "done");
    assert_eq!(held(&host), 0);
}

#[test]
fn the_owners_turns_and_the_oracles_start_at_once_under_pressure() {
    let place = Place::new("pressure-owner");
    let host = place.start_with("", KEEP);
    pressure(&place, 80.0);
    let (owner, owner_spools) = agent(&place, &host, "Owners");
    host.send(&owner, "my own words", "u1");
    wait_for("the owner's turn to start", || started(&owner_spools));
    let (oracle, oracle_spools) = agent(&place, &host, "Oracles");
    send_as(&host, &oracle, "the owner's words, carried", "o1", "oracle");
    wait_for("the Oracle's turn to start", || started(&oracle_spools));
    wait_for("both turns to end", || {
        host.state(&owner) == "done" && host.state(&oracle) == "done"
    });
    assert_eq!(held(&host), 0);
}

#[test]
fn a_turn_that_waits_starts_at_the_bound_however_high_the_pressure() {
    let place = Place::new("pressure-bound");
    let offset = place.root.join("clock-offset");
    std::fs::write(&offset, "0").unwrap();
    let host = place.start_in(
        "",
        KEEP,
        &[("CLARP_TEST_CLOCK_OFFSET_FILE", offset.to_str().unwrap())],
    );
    let (session, spools) = agent(&place, &host, "Waiter");
    pressure(&place, 90.0);
    send_as(&host, &session, "a goal's wake", "w1", "automation");
    wait_for("the turn to wait for the machine", || held(&host) == 1);
    // Nine minutes on the Host's clock: still waiting.
    std::fs::write(&offset, (9 * 60_000).to_string()).unwrap();
    std::thread::sleep(Duration::from_secs(6));
    assert!(
        !started(&spools),
        "the turn started before its ten minutes: {}",
        seen(&spools)
    );
    // Ten minutes and more: it goes, the pressure as high as ever.
    std::fs::write(&offset, (10 * 60_000 + 1_000).to_string()).unwrap();
    wait_for("the turn to start at the bound", || started(&spools));
    wait_for("the turn to end", || host.state(&session) == "done");
    assert_eq!(held(&host), 0);
}

#[test]
fn a_stop_ends_a_turn_that_waits_for_the_machine_at_once() {
    let place = Place::new("pressure-stop");
    let host = place.start_with("", KEEP);
    let (session, spools) = agent(&place, &host, "Stopped");
    pressure(&place, 60.0);
    send_as(&host, &session, "another agent's ask", "a1", "agent");
    wait_for("the turn to wait for the machine", || held(&host) == 1);
    host.post("/stop", json!({ "session": session }));
    wait_within(
        "the waiting turn to end at the Stop",
        Duration::from_secs(10),
        || host.state(&session) == "interrupted",
    );
    assert_eq!(held(&host), 0);
    // The machine calms: nothing of the stopped turn starts after it.
    pressure(&place, 0.0);
    std::thread::sleep(Duration::from_secs(6));
    assert!(
        !started(&spools),
        "a stopped turn started once the pressure fell"
    );
}

#[test]
fn a_message_of_the_owners_does_not_wait_behind_a_turn_held_for_the_machine() {
    let place = Place::new("pressure-behind");
    let host = place.start_with("", KEEP);
    let (session, spools) = agent(&place, &host, "Busy");
    pressure(&place, 70.0);
    send_as(&host, &session, "a janitor's round", "j1", "schedule");
    wait_for("the turn to wait for the machine", || held(&host) == 1);
    // The owner writes: the held turn goes at once, and his after it, under pressure still.
    host.post(
        "/send",
        json!({"session": session, "text": "mine", "client_msg_id": "u1", "queue_if_busy": true}),
    );
    wait_for("the held turn to start for the owner's sake", || {
        started(&spools)
    });
    wait_for("the owner's turn to start after it", || shims(&spools) >= 2);
    wait_for("both turns to end", || host.state(&session) == "done");
    assert_eq!(held(&host), 0);
}

#[test]
fn the_turns_that_start_without_a_lane_are_counted_for_the_last_hour() {
    let place = Place::new("pressure-cold");
    let host = place.start_with("", KEEP);
    let cold = |host: &Host| host.get("/lane-settings")["cold_starts_last_hour"].clone();
    let before = cold(&host);
    let count = |said: &Value, tool: &str| said["tools"][tool].as_u64().unwrap_or(0);
    let (session, spools) = agent(&place, &host, "Cold");
    host.send(&session, "one", "c1");
    wait_for("the first turn to end", || host.state(&session) == "done");
    host.send(&session, "two", "c2");
    wait_for("the second turn", || shims(&spools) == 2);
    wait_for("the second turn to end", || host.state(&session) == "done");
    let after = cold(&host);
    assert_eq!(
        after["total"].as_u64().unwrap() - before["total"].as_u64().unwrap(),
        2,
        "{after}"
    );
    assert_eq!(
        count(&after, "scripted") - count(&before, "scripted"),
        2,
        "{after}"
    );
    // The live processes are told apart from them: a scripted agent keeps none.
    let settings = host.get("/lane-settings");
    assert_eq!(settings["live"]["lanes"], 0, "{settings}");
    assert_eq!(settings["live"]["tools"]["claude"]["idle"], 0, "{settings}");
}

#[test]
fn a_held_helper_turn_does_not_use_or_start_a_lane() {
    use super::claude_chat_lane::{CLAUDE, Lanes, lane_said, noted};
    let place = Place::new("pressure-lane");
    let _lanes = Lanes(place.root.clone());
    let claude = place.root.join("claude");
    program(&claude, CLAUDE);
    let config = place.root.join("config");
    std::fs::create_dir_all(config.join("clarp-host")).unwrap();
    std::fs::write(
        config.join("clarp-host/config.toml"),
        "claude_chat_lane = true\ncodex_lane_idle_minutes = 15\n",
    )
    .unwrap();
    let home = place.root.join("home");
    std::fs::create_dir_all(home.join(".claude")).unwrap();
    std::fs::write(
        home.join(".claude/.credentials.json"),
        json!({"claudeAiOauth": {"refreshToken": "r1", "accessToken": "a"}}).to_string(),
    )
    .unwrap();
    let offset = place.root.join("clock-offset");
    std::fs::write(&offset, "0").unwrap();
    let world = [
        ("CLARP_CLAUDE_PROGRAM", claude.to_str().unwrap()),
        ("XDG_CONFIG_HOME", config.to_str().unwrap()),
        ("HOME", home.to_str().unwrap()),
        ("CLARP_TEST_CLOCK_OFFSET_FILE", offset.to_str().unwrap()),
    ];
    let host = place.start_in("", KEEP, &world);
    let socket = |session: &str| {
        let made = host.post(
            "/agents",
            json!({"name": session, "session": session, "backend": "claude"}),
        );
        assert_eq!(made["session"], session, "{made}");
        let id = host.get("/agents/snapshot")["agents"]
            .as_array()
            .unwrap()
            .iter()
            .find(|agent| agent["session"] == session)
            .unwrap()["agent_id"]
            .as_str()
            .unwrap()
            .to_owned();
        place.root.join("run").join(format!("{id}-cll.sock"))
    };
    let (kept, cold) = (socket("kept"), socket("cold"));
    let launches = || noted(&place, "launches").lines().count();
    let answered = |word: &str| noted(&place, "answered").lines().any(|line| line == word);

    // Calm: the owner's first turn starts its Claude Code as the lane (issue 833).
    host.send("kept", "ask-one", "u1");
    wait_for("the first answer", || answered("ask-one"));
    assert_eq!(launches(), 1);
    wait_for("the lane to answer", || {
        lane_said(&kept, "inspect").is_some_and(|said| said["state"]["available"] == true)
    });

    // Under pressure, a helper's turn waits: the lane that runs is not given it.
    pressure(&place, 70.0);
    send_as(&host, "kept", "ask-two", "s2", "schedule");
    wait_for("the turn to wait for the machine", || held(&host) == 1);
    send_as(&host, "cold", "ask-three", "s3", "schedule");
    wait_for("the second turn to wait", || held(&host) == 2);
    std::thread::sleep(Duration::from_millis(1500));
    assert!(
        !noted(&place, "turns").contains("ask-two"),
        "a held turn used the lane"
    );
    assert!(!noted(&place, "turns").contains("ask-three"));
    assert_eq!(launches(), 1);

    // At the bound, the pressure as high: the one with a lane uses it; the one without runs in
    // a process of its own, and no lane starts after it.
    std::fs::write(&offset, (10 * 60_000 + 1_000).to_string()).unwrap();
    wait_for("the turns let go at the bound", || {
        answered("ask-two") && answered("ask-three")
    });
    wait_for("the turns to end", || {
        host.state("kept") == "done" && host.state("cold") == "done"
    });
    std::thread::sleep(Duration::from_secs(3));
    assert_eq!(launches(), 2, "{}", noted(&place, "launches"));
    assert!(
        noted(&place, "launches").ends_with("--session-id turn=set\n"),
        "the turn without a lane ran in a process of its own: {}",
        noted(&place, "launches")
    );
    assert!(
        lane_said(&cold, "inspect").is_none(),
        "a lane started for a turn nobody waited on"
    );

    // The owner's turn starts its Claude Code as the lane whatever the pressure.
    host.send("cold", "ask-four", "u4");
    wait_for("the owner's answer", || answered("ask-four"));
    assert_eq!(launches(), 3, "{}", noted(&place, "launches"));
    wait_for("the owner's lane to answer", || {
        lane_said(&cold, "inspect").is_some()
    });
    assert_eq!(held(&host), 0);
}

/// A send another agent makes from its running turn, as `clarp-admin prompt` makes it: the
/// turn's token in `X-Clarp-Turn`, the sender named.
fn relayed(host: &Host, token: &str, from: &str, to: &str, id: &str) {
    let got = fetch(
        host,
        &host.token,
        "POST",
        "/send",
        &[
            ("Content-Type", "application/json"),
            ("X-Clarp-Turn", token),
        ],
        json!({"session": to, "text": id, "client_msg_id": id, "sender": from,
               "queue_if_busy": true})
        .to_string()
        .as_bytes(),
    );
    assert_eq!(got.status, 200, "{}", String::from_utf8_lossy(&got.body));
}

#[test]
fn work_relayed_from_an_owners_turn_is_his_for_one_hop() {
    use super::coordination::{stand_in, start_host_as};
    let place = Place::new("pressure-hop");
    let (tokens, release, holds) = stand_in(&place);
    let host = start_host_as(&place, KEEP, "");
    for name in ["desk", "first", "second", "helper", "third"] {
        let made = host.post(
            "/agents",
            json!({"name": name, "session": name, "backend": "claude"}),
        );
        assert_eq!(made["session"], name, "{made}");
        // Each turn of the stand-in holds until it is released, so its token stays live.
        std::fs::write(holds.join(name), "").unwrap();
    }
    let token = |name: &str| {
        let mut said = String::new();
        wait_for(&format!("{name}'s turn to start"), || {
            said = std::fs::read_to_string(tokens.join(name)).unwrap_or_default();
            !said.is_empty()
        });
        said
    };
    let started = |name: &str| tokens.join(name).exists();

    // Calm: the owner writes the desk, and a helper is at its own work.
    host.send("desk", "relay to first", "d1");
    let desk = token("desk");
    send_as(&host, "helper", "a chore", "h1", "schedule");
    let helper = token("helper");

    pressure(&place, 70.0);
    // One hop from the owner's turn: it starts at once.
    relayed(&host, &desk, "desk", "first", "hop-1");
    let first = token("first");
    // Two hops: the relay's own onward prompt waits.
    relayed(&host, &first, "first", "second", "hop-2");
    // From a helper's own turn: it waits.
    relayed(&host, &helper, "helper", "third", "from-helper");
    wait_for("the two turns to wait for the machine", || held(&host) == 2);
    std::thread::sleep(Duration::from_millis(1500));
    assert!(
        !started("second"),
        "a turn two hops from the owner started under pressure"
    );
    assert!(!started("third"), "a helper's relay started under pressure");

    // Calm again: they start, and every held turn ends.
    pressure(&place, 0.0);
    wait_for("the waiting turns to start", || {
        started("second") && started("third")
    });
    for name in ["desk", "first", "second", "helper", "third"] {
        std::fs::write(release.join(name), "").unwrap();
    }
    assert_eq!(held(&host), 0);
}

#[test]
fn a_busy_machine_with_a_calm_host_holds_no_work() {
    let place = Place::new("pressure-cgroup");
    let host = place.start_with("", KEEP);
    let (session, spools) = agent(&place, &host, "Helper");
    // The agents' own work keeps the machine's processors busy; the Host's own cgroup is calm.
    pressure(&place, 0.0);
    let busy = "some avg10=90.00 avg60=90.00 avg300=50.00 total=1\n";
    std::fs::write(place.pressure().join("cpu"), busy).unwrap();
    std::fs::write(
        place.pressure().join("host-cpu"),
        "some avg10=3.00 avg60=4.00 avg300=1.00 total=1\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=0\n",
    )
    .unwrap();
    let settings = host.get("/lane-settings");
    assert_eq!(
        settings["pressure"]["cpu_source"], "host-cgroup",
        "{settings}"
    );
    assert_eq!(settings["pressure"]["cpu"]["some"]["avg60"], 4.0);
    assert_eq!(settings["pressure"]["high"], false, "{settings}");
    send_as(&host, &session, "a scheduled chore", "s1", "schedule");
    wait_for("the turn to start at once", || started(&spools));
    assert_eq!(held(&host), 0);
    // Without the Host's own figure, the machine's counts.
    std::fs::remove_file(place.pressure().join("host-cpu")).unwrap();
    let settings = host.get("/lane-settings");
    assert_eq!(settings["pressure"]["cpu_source"], "system", "{settings}");
    assert_eq!(settings["pressure"]["high"], true, "{settings}");
}
