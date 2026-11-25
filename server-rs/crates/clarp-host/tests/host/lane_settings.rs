//! Issue 779. The owner reads and changes the settings of lanes from his apps: `GET
//! /lane-settings` reads the Host's `config.toml` and asks the lanes that run what they hold;
//! `POST /lane-settings` writes the same keys into the same file, keeps the rest of it, tells
//! every app, and the next turn runs by it. An agent may read it and never change it.

use super::codex_chat_lane::{Lanes, asked, lane_agent, lane_lives, lane_place, noted};
use super::*;

const FILE: &str = "# The owner's own notes.\n\
    codex_chat_lane = false  # off for now\n\
    codex_lane_most = 6\n\
    \n\
    [server]\n\
    kind = \"clarp-host\"\n\
    \n\
    # kept as it is\n\
    [agents]\n\
    codex_reset_max_per_day = 2\n";

#[test]
fn the_owner_changes_the_lane_settings_from_an_app_and_the_next_turn_runs_by_them() {
    let (place, world) = lane_place("lane-settings", "");
    let file = place.root.join("config/clarp-host/config.toml");
    std::fs::write(&file, FILE).unwrap();
    let _lanes = Lanes(place.root.clone());
    let token = "the-owners-token";
    let world: Vec<(&str, &str)> = world
        .iter()
        .map(|(key, value)| (key.as_str(), value.as_str()))
        .collect();
    let host = place.start_in(token, &[], &world);
    let unchanged = || assert_eq!(std::fs::read_to_string(&file).unwrap(), FILE);

    // What the file holds, the defaults where it holds nothing, and no lane yet.
    let read = host.get("/lane-settings");
    assert_eq!(read["codex_chat_lane"], false, "{read}");
    assert_eq!(read["claude_chat_lane"], false, "{read}");
    assert_eq!(read["muse_chat_lane"], false, "{read}");
    assert_eq!(read["opencode_chat_lane"], false, "{read}");
    assert_eq!(read["most"], 6, "{read}");
    assert_eq!(read["memory_mb"], 2200, "{read}");
    assert_eq!(read["idle_minutes"], 15, "{read}");
    // A test's Host runs the limits fixed (the harness says so: its lanes must not follow the
    // test machine's free memory), and still says what the machine has free and what auto keeps.
    assert_eq!(read["limits"], "fixed", "{read}");
    assert_eq!(read["reserve_percent"], 20, "{read}");
    assert_eq!(read["ceiling"], 24, "{read}");
    assert!(read["held"]["available_mb"].as_u64().is_some(), "{read}");
    assert!(read["held"]["reserve_mb"].as_u64().is_some(), "{read}");
    assert_eq!(
        read["ranges"]["memory_mb"],
        json!({"min": 256, "max": 16384})
    );
    assert_eq!(read["held"]["lanes"], 0, "{read}");
    assert_eq!(read["held"]["tools"]["codex"]["lanes"], 0, "{read}");
    assert_eq!(read["held"]["tools"]["opencode"]["lanes"], 0, "{read}");

    // An agent's tools read it, and may not change it; nor may a request that names a turn.
    let tools = clarp_host::tool_token(token);
    let (status, read_by_an_agent) = host.ask_as(&tools, "GET", "/lane-settings", None);
    assert_eq!(status, 200, "{read_by_an_agent}");
    assert_eq!(read_by_an_agent["most"], 6);
    let (status, refused) =
        host.ask_as(&tools, "POST", "/lane-settings", Some(&json!({"most": 3})));
    assert_eq!(status, 403, "{refused}");
    let refused = fetch(
        &host,
        token,
        "POST",
        "/lane-settings",
        &[
            ("X-Clarp-Turn", "a-turn"),
            ("Content-Type", "application/json"),
        ],
        br#"{"most": 3}"#,
    );
    assert_eq!(refused.status, 403, "{}", refused.text());
    unchanged();

    // A value out of its range, or of the wrong kind, is refused with its range, and nothing
    // of the file changes, not even what the same request named rightly.
    for (body, code, words) in [
        (
            json!({"most": 33, "codex_chat_lane": true}),
            "out_of_range",
            "most must be between 1 and 32",
        ),
        (
            json!({"memory_mb": 100}),
            "out_of_range",
            "memory_mb must be between 256 and 16384",
        ),
        (
            json!({"idle_minutes": 0}),
            "out_of_range",
            "idle_minutes must be between 1 and 240",
        ),
        (
            json!({"idle_minutes": "5"}),
            "wrong_type",
            "idle_minutes must be a whole number",
        ),
        (
            json!({"claude_chat_lane": 1}),
            "wrong_type",
            "claude_chat_lane must be true or false",
        ),
        (
            json!({"muse_chat_lane": "on"}),
            "wrong_type",
            "muse_chat_lane must be true or false",
        ),
        (
            json!({"opencode_chat_lane": "yes"}),
            "wrong_type",
            "opencode_chat_lane must be true or false",
        ),
    ] {
        let (status, answer) = host.ask("POST", "/lane-settings", Some(&body));
        assert_eq!(status, 400, "{body}: {answer}");
        assert_eq!(answer["code"], code, "{body}: {answer}");
        assert_eq!(answer["error"], words, "{body}: {answer}");
        let setting = answer["setting"].as_str().unwrap();
        if let Some(range) = read["ranges"].get(setting) {
            assert_eq!(answer["range"], *range, "{body}: {answer}");
        }
    }
    unchanged();

    // The first turn runs as the file said: by exec.
    let socket = lane_agent(&host, &place, "settled");
    host.send("settled", "ask-one", "one");
    let busy = || {
        host.get("/agents/snapshot")["agents"]
            .as_array()
            .unwrap()
            .iter()
            .find(|agent| agent["session"] == "settled")
            .unwrap()["busy"]
            == true
    };
    wait_for("the exec turn to end", || {
        noted(&place, "launches").starts_with("exec\n") && !busy()
    });
    assert!(!noted(&place, "launches").contains("app-server"));

    // The owner switches the Codex lane on and lowers the count: the file has the new values
    // and every other byte it had, and every app is told.
    let events = host.listen_at(&format!("/events?token={token}"));
    let changed = host.post(
        "/lane-settings",
        json!({"codex_chat_lane": true, "most": 4, "held": {"lanes": 9}}),
    );
    assert_eq!(changed["codex_chat_lane"], true, "{changed}");
    assert_eq!(changed["most"], 4, "{changed}");
    assert_eq!(changed["memory_mb"], 2200, "{changed}");
    assert_eq!(
        std::fs::read_to_string(&file).unwrap(),
        FILE.replace(
            "codex_chat_lane = false  # off",
            "codex_chat_lane = true  # off"
        )
        .replace("codex_lane_most = 6", "codex_lane_most = 4")
    );
    wait_for("the change told to the apps", || {
        events.lock().unwrap().iter().any(|event| {
            event["type"] == "lane-settings-updated"
                && event["codex_chat_lane"] == true
                && event["most"] == 4
                && event["idle_minutes"] == 15
        })
    });

    // The next turn runs on a lane, and the lane is counted with what it weighed itself.
    asked(&host, "settled", "ask-two");
    let launches = noted(&place, "launches");
    assert!(launches.ends_with("exec\napp-server\n"), "{launches}");
    assert_eq!(launches.matches("app-server").count(), 1, "{launches}");
    assert!(lane_lives(&socket));
    let read = host.get("/lane-settings");
    assert_eq!(read["held"]["lanes"], 1, "{read}");
    let codex = &read["held"]["tools"]["codex"];
    assert_eq!(codex["lanes"], 1, "{read}");
    assert_eq!(codex["busy"], 0, "{read}");
    assert!(codex["memory_mb"].as_u64().unwrap() > 0, "{read}");
    assert_eq!(read["held"]["tools"]["claude"]["lanes"], 0, "{read}");
    assert_eq!(read["held"]["tools"]["muse"]["lanes"], 0, "{read}");
    assert_eq!(read["held"]["tools"]["opencode"]["lanes"], 0, "{read}");

    // A key the file did not have is added where the Host reads it.
    let changed = host.post("/lane-settings", json!({"memory_mb": 3000}));
    assert_eq!(changed["memory_mb"], 3000, "{changed}");
    let text = std::fs::read_to_string(&file).unwrap();
    assert!(
        text.find("lane_memory_mb = 3000").unwrap() < text.find("[server]").unwrap(),
        "{text}"
    );
    assert!(text.contains("# kept as it is\n[agents]\ncodex_reset_max_per_day = 2\n"));

    // What an app read, sent back as it is, changes nothing: the file keeps every byte, and the
    // idle time it never set is not written into it.
    let read = host.get("/lane-settings");
    assert_eq!(read["held"]["unanswered"], 0, "{read}");
    let sent_back = host.post("/lane-settings", read.clone());
    assert_eq!(sent_back["idle_minutes"], 15, "{sent_back}");
    assert_eq!(std::fs::read_to_string(&file).unwrap(), text);
    assert!(!text.contains("codex_lane_idle_minutes"), "{text}");

    // OpenCode's switch (issue 763) is one of them: written where the Host reads it, said back,
    // and told to the apps.
    let changed = host.post("/lane-settings", json!({"opencode_chat_lane": true}));
    assert_eq!(changed["opencode_chat_lane"], true, "{changed}");
    let written = std::fs::read_to_string(&file).unwrap();
    assert!(
        written.find("opencode_chat_lane = true").unwrap() < written.find("[server]").unwrap(),
        "{written}"
    );
    assert_eq!(host.get("/lane-settings")["opencode_chat_lane"], true);
    wait_for("OpenCode's switch told to the apps", || {
        events.lock().unwrap().iter().any(|event| {
            event["type"] == "lane-settings-updated" && event["opencode_chat_lane"] == true
        })
    });
    let changed = host.post("/lane-settings", json!({"opencode_chat_lane": false}));
    assert_eq!(changed["opencode_chat_lane"], false, "{changed}");
}

/// Issue 834. `lane_ceiling` bounds nothing since contract 186: a file that carries one (the live
/// Hosts' config had `lane_ceiling = 64`) still reads, and no POST writes it, nor a change of it.
#[test]
fn a_ceiling_in_the_file_still_reads_and_no_post_writes_one() {
    let (place, world) = lane_place("lane-ceiling", "");
    let file = place.root.join("config/clarp-host/config.toml");
    let old = "lane_ceiling = 64\n[server]\nkind = \"clarp-host\"\n";
    std::fs::write(&file, old).unwrap();
    let token = "the-owners-token";
    let world: Vec<(&str, &str)> = world
        .iter()
        .map(|(key, value)| (key.as_str(), value.as_str()))
        .collect();
    let host = place.start_in(token, &[], &world);
    let read = host.get("/lane-settings");
    assert_eq!(read["ceiling"], 64, "{read}");
    assert_eq!(read["limits"], "fixed", "{read}");
    // A ceiling sent alone changes nothing, and is no error.
    let sent = host.post("/lane-settings", json!({"ceiling": 10}));
    assert_eq!(sent["ceiling"], 64, "{sent}");
    assert_eq!(std::fs::read_to_string(&file).unwrap(), old);
    // Sent with a change, only the change is written.
    let sent = host.post(
        "/lane-settings",
        json!({"ceiling": 10, "reserve_percent": 25}),
    );
    assert_eq!(sent["reserve_percent"], 25, "{sent}");
    let text = std::fs::read_to_string(&file).unwrap();
    assert!(text.contains("lane_ceiling = 64\n"), "{text}");
    assert!(text.contains("lane_reserve_percent = 25\n"), "{text}");
    assert_eq!(text.matches("lane_ceiling").count(), 1, "{text}");
}
