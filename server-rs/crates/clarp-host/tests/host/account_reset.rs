//! A turn whose account ran out waits until the time its tool said the account resets.

use super::*;

#[test]
fn a_turn_whose_account_resets_at_a_time_its_tool_said_waits_until_then_and_goes_on_by_itself() {
    let place = Place::new("resets");
    // A stand-in for Claude Code whose account is out once: it says that the window began
    // anew 50 seconds ago by its clock, so the Host tries the turn again ten seconds from
    // then, without a change of login. It writes down what it was given, each time.
    let (out_of_usage, given) = (place.root.join("out"), place.root.join("given"));
    let init = "printf '%s\\n' '{\"type\":\"system\",\"subtype\":\"init\",\"session_id\":\"stand-in\",\"model\":\"stand-in\"}'\n";
    let script = [
        "#!/bin/sh\nread message\n",
        &format!("printf '%s\\n' \"$message\" >> \"{}\"\n", given.display()),
        &format!("if [ -e \"{0}\" ]; then\nrm -f \"{0}\"\n", out_of_usage.display()),
        init,
        "printf '{\"type\":\"rate_limit_event\",\"rate_limit_info\":{\"status\":\"rejected\",\"rateLimitType\":\"five_hour\",\"resetsAt\":%s,\"overageStatus\":\"rejected\",\"isUsingOverage\":false}}\\n' \"$(( $(date +%s) - 50 ))\"\n",
        "printf '%s\\n' '{\"type\":\"result\",\"subtype\":\"success\",\"is_error\":true,\"session_id\":\"stand-in\",\"result\":\"You have hit your 5-hour limit, resets soon\"}'\n",
        "exit 1\nfi\n",
        init,
        "printf '%s\\n' '{\"type\":\"assistant\",\"session_id\":\"stand-in\",\"message\":{\"id\":\"msg_1\",\"role\":\"assistant\",\"model\":\"stand-in\",\"content\":[{\"type\":\"text\",\"text\":\"on it\"}]}}'\n",
        "printf '%s\\n' '{\"type\":\"result\",\"subtype\":\"success\",\"is_error\":false,\"session_id\":\"stand-in\",\"result\":\"on it\",\"num_turns\":1}'\n",
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

    // The account is out: the turn waits, and says until when.
    std::fs::write(&out_of_usage, "").unwrap();
    let sent = Instant::now();
    host.send(&agent, "work on this", "xray");
    wait_for("the turn to wait for an account", || {
        row()["live_activity"]["state"] == "limited"
    });
    let shown = row();
    let waits = "Waiting for a claude account with available usage · resets ";
    for said in [
        &shown["activity"]["summary"],
        &shown["live_activity"]["headline"],
    ] {
        assert!(
            said.as_str().is_some_and(|said| said.starts_with(waits)),
            "{said}"
        );
    }
    assert_eq!(host.turns(&agent).len(), 1, "nothing says the turn failed");

    // A minute after the reset it is taken up by itself, though the login never changed,
    // and sooner than a turn that was told no reset is tried again (a minute).
    wait_for("the answer", || {
        host.turns(&agent).len() == 2 && host.state(&agent) == "done"
    });
    assert!(
        sent.elapsed() < Duration::from_secs(55),
        "{:?}",
        sent.elapsed()
    );
    assert_eq!(started(), 2);
    let said = std::fs::read_to_string(&given).unwrap();
    let again = said.lines().nth(1).unwrap();
    assert!(
        again.contains("<clarp-account-recovery>") && again.contains("work on this"),
        "{again}"
    );
}
