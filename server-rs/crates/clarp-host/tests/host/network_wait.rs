//! A turn whose tool cannot reach its provider because the machine has no network waits for
//! the network, says so, and goes on when it is back (issue 752).

use super::*;

#[test]
fn a_turn_whose_network_is_gone_waits_for_it_and_goes_on_on_the_same_account() {
    let place = Place::new("network");
    // A stand-in for Claude Code that fails as Claude Code does when the machine's DNS is
    // down, after it began its work, for as long as the test says there is no network. It
    // writes down what it was given, each time.
    let given = place.root.join("given");
    let down = place.network_down();
    let init = "printf '%s\\n' '{\"type\":\"system\",\"subtype\":\"init\",\"session_id\":\"stand-in\",\"model\":\"stand-in\"}'\n";
    let script = [
        "#!/bin/sh\nread message\n",
        &format!("printf '%s\\n' \"$message\" >> \"{}\"\n", given.display()),
        init,
        &format!("if [ -e \"{}\" ]; then\n", down.display()),
        "printf '%s\\n' '{\"type\":\"assistant\",\"session_id\":\"stand-in\",\"message\":{\"id\":\"msg_1\",\"role\":\"assistant\",\"model\":\"stand-in\",\"content\":[{\"type\":\"text\",\"text\":\"Counting.\"}]}}'\n",
        "echo \"API Error: Can't reach the API server. Check your internet connection (EAI_AGAIN)\" >&2\nexit 1\nfi\n",
        "printf '%s\\n' '{\"type\":\"assistant\",\"session_id\":\"stand-in\",\"message\":{\"id\":\"msg_2\",\"role\":\"assistant\",\"model\":\"stand-in\",\"content\":[{\"type\":\"text\",\"text\":\"Seven files.\"}]}}'\n",
        "printf '%s\\n' '{\"type\":\"result\",\"subtype\":\"success\",\"is_error\":false,\"session_id\":\"stand-in\",\"result\":\"Seven files.\",\"num_turns\":1}'\n",
    ]
    .concat();
    let claude = place.root.join("claude");
    program(&claude, &script);
    let home = place.root.join("home");
    let login = home.join(".claude").join(".credentials.json");
    std::fs::create_dir_all(login.parent().unwrap()).unwrap();
    std::fs::write(&login, "the only account").unwrap();
    let host = place.start_in(
        "",
        &["--accounts", "follow", "--logins-checked-every-ms", "100"],
        &[
            ("CLARP_CLAUDE_PROGRAM", claude.to_str().unwrap()),
            ("HOME", home.to_str().unwrap()),
        ],
    );
    let made = host.post("/agents", json!({ "name": "Worker", "backend": "claude" }));
    let agent = made["session"].as_str().unwrap().to_string();
    let events = host.listen();
    let row = || -> Value {
        host.get("/agents/snapshot")["agents"]
            .as_array()
            .unwrap()
            .iter()
            .find(|row| row["session"] == agent)
            .cloned()
            .unwrap()
    };
    let started = || -> usize {
        std::fs::read_to_string(&given)
            .map(|said| said.lines().count())
            .unwrap_or(0)
    };

    // The network is gone: the turn waits for it and says so, as a turn that waits for an
    // account does, and nothing says that it failed.
    std::fs::write(&down, "").unwrap();
    host.send(&agent, "count the files", "xray");
    wait_for("the turn to wait for the network", || {
        row()["live_activity"]["headline"] == "Waiting for the network"
    });
    let shown = row();
    assert_eq!(shown["live_activity"]["state"], "limited");
    assert_eq!(shown["activity"]["summary"], "Waiting for the network");
    assert_eq!(shown["activity"]["state"], "limited");
    // The word an app keys on, rather than the words (issue 834).
    assert_eq!(shown["live_activity"]["waits_for"], "network");
    assert_eq!(shown["activity"]["waits_for"], "network");
    assert_eq!(started(), 1);
    // It is checked again and again, and the tool is not started again meanwhile.
    std::thread::sleep(Duration::from_millis(6_000));
    assert_eq!(started(), 1, "no attempt while the network is gone");
    assert_eq!(
        row()["live_activity"]["headline"],
        "Waiting for the network"
    );
    assert!(
        !host
            .turns(&agent)
            .iter()
            .any(|row| row["id"] == "marker-u-xray"),
        "nothing says the turn failed"
    );

    // The network is back: the turn goes on from where it stood, on the same account, and
    // answers.
    std::fs::remove_file(&down).unwrap();
    wait_for("the answer", || {
        host.state(&agent) == "done"
            && host
                .turns(&agent)
                .last()
                .is_some_and(|row| row["turn"]["status"] == "completed")
    });
    assert_eq!(started(), 2, "one attempt after the network came back");
    let said = std::fs::read_to_string(&given).unwrap();
    let again = said.lines().nth(1).unwrap();
    assert!(
        again.contains("<clarp-turn-resumed>") && again.contains("count the files"),
        "{again}"
    );
    assert_eq!(
        std::fs::read_to_string(&login).unwrap(),
        "the only account",
        "the login was never changed"
    );
    let told = events.lock().unwrap().clone();
    assert!(
        !told.iter().any(|event| {
            event.to_string().contains("Waiting for a claude account")
                || (event["type"] == "agent-state" && event["kind"] == "interrupted")
        }),
        "it never waited for an account, nor failed"
    );
}
