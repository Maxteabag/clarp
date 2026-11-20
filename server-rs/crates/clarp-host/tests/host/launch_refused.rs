//! The Host's tests of a send whose tool could not be started (R121): one file a piece,
//! declared in `host.rs`.

use super::*;

/// A send whose launch is refused before its tool is given anything (here the shim cannot be
/// started) is tried again as the Python Host tries it: a second after its own try, then after
/// waits of 1, 2, 4 ... s. Meanwhile it is the agent's turn: the apps are told it reconnects
/// (which try comes after how long, of the nine it can have), its row shows it handed over (its
/// launch was attempted, as the Python Host's receipt says from then on), and a send behind it
/// waits, pending its launch. The user's Stop ends it at once.
#[test]
fn a_send_whose_shim_cannot_be_started_waits_reconnecting_and_a_stop_ends_it() {
    let place = Place::new("launch-refused");
    let nowhere = place.root.join("no-shim-here");
    let world = sealed(&place);
    let world: Vec<(&str, &str)> = world.iter().map(|(k, v)| (*k, v.as_str())).collect();
    let host = place.start_in("secret", &["--shim", nowhere.to_str().unwrap()], &world);
    let session = host.new_agent("Refused");
    let heard = host.listen_at("/events?token=secret");
    host.send(&session, "go", "r1");
    let reconnects = || -> Vec<Value> {
        heard
            .lock()
            .unwrap()
            .iter()
            .filter(|event| event["type"] == "agent-state" && event["session"] == session.as_str())
            .filter(|event| event["detail"]["action"] == "reconnecting")
            .map(|event| event["detail"].clone())
            .collect()
    };
    wait_for("its third refusal", || {
        reconnects().iter().any(|detail| detail["reconnect"] == 4)
    });
    let told: Vec<(Value, Value, Value)> = reconnects()
        .iter()
        .map(|detail| {
            (
                detail["reconnect"].clone(),
                detail["after_ms"].clone(),
                detail["of"].clone(),
            )
        })
        .collect();
    assert_eq!(
        told[..3],
        [
            (json!(2), json!(1000), json!(9)),
            (json!(3), json!(1000), json!(9)),
            (json!(4), json!(2000), json!(9)),
        ],
        "{told:?}"
    );
    assert_eq!(
        reconnects()[0]["summary"],
        "Reconnecting… Your message is saved."
    );

    // Its row shows it handed over; a send behind it waits for its launch.
    host.send(&session, "next", "r2");
    let log = host.get(&format!("/log?session={session}"));
    let delivery = |id: &str| {
        let turns = log["turns"].as_array().unwrap();
        let row = turns.iter().find(|turn| turn["id"] == id);
        row.map_or(Value::Null, |row| row["delivery"]["state"].clone())
    };
    assert_eq!(
        (delivery("u-r1"), delivery("u-r2")),
        (json!("handed_over"), json!("pending_launch")),
        "{log}"
    );

    // The user's Stop ends the wait at once, and nothing of it is tried again.
    host.post("/stop", json!({ "session": session }));
    wait_for("the agent to be free", || {
        host.state(&session) != "thinking"
    });
    let tries = reconnects().len();
    std::thread::sleep(std::time::Duration::from_millis(2_500));
    assert_eq!(reconnects().len(), tries, "nothing is tried after the Stop");
}

/// What a Host of these tests sees of the machine: a home of its own in its place, and every
/// tool a person installs pinned to a path where there is none. Its agent's tool is the
/// scripted stand-in, built into the Host.
fn sealed(place: &Place) -> Vec<(&'static str, String)> {
    let home = place.root.join("home");
    std::fs::create_dir_all(&home).unwrap();
    let at = |path: PathBuf| path.to_str().unwrap().to_string();
    let absent = at(place.root.join("no-such-tool"));
    let mut world = vec![
        ("HOME", at(home.clone())),
        ("XDG_CONFIG_HOME", at(home.join(".config"))),
        ("XDG_DATA_HOME", at(home.join(".local/share"))),
        ("XDG_CACHE_HOME", at(home.join(".cache"))),
        ("XDG_STATE_HOME", at(home.join(".local/state"))),
    ];
    for tool in [
        "CLARP_CLAUDE_PROGRAM",
        "CLARP_CODEX_PROGRAM",
        "CLARP_AGY_PROGRAM",
        "CLARP_GROK_PROGRAM",
        "CLARP_MUSE_PROGRAM",
        "CLARP_OPENCODE_PROGRAM",
        "CLARP_ZOXIDE_PROGRAM",
    ] {
        world.push((tool, absent.clone()));
    }
    world
}
