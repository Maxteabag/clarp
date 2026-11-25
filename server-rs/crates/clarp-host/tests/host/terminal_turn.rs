use super::*;

const ADMIN: &str = "the-administrators-token";

/// A stand-in for Claude Code whose turn holds until the file `go` is there: with a terminal
/// before it, it says how it was started and does what it is typed; as a turn it notes that it
/// started and answers once let go.
fn tool_whose_turn_holds(place: &Place) -> (PathBuf, PathBuf, PathBuf) {
    let started = place.root.join("turn-started");
    let go = place.root.join("go");
    let tool = place.root.join("claude-that-holds");
    let script = [
        "#!/bin/sh\nif [ -t 0 ]; then\n",
        "  echo \"terminal: args=[$*] .\"\n",
        "  while IFS= read -r line; do\n",
        "    case \"$line\" in bye) echo leaving; exit 0 ;; *) echo \"typed: $line\" ;; esac\n",
        "  done\n  exit 0\nfi\nread message\n",
        &format!("echo started >> \"{}\"\n", started.display()),
        &format!("while [ ! -e \"{}\" ]; do sleep 0.05; done\n", go.display()),
        STAND_IN_ANSWERS,
    ]
    .concat();
    program(&tool, &script);
    (tool, started, go)
}

#[test]
fn an_agents_terminal_is_refused_while_a_turn_of_it_runs_and_opens_after() {
    let place = Place::new("terminal-turn");
    let home = place.root.join("home");
    std::fs::create_dir_all(&home).unwrap();
    let (tool, started, go) = tool_whose_turn_holds(&place);
    let world = [
        ("CLARP_CLAUDE_PROGRAM", tool.to_str().unwrap()),
        ("HOME", home.to_str().unwrap()),
    ];
    let host = place.start_in(ADMIN, &[], &world);
    let made = host.post("/agents", json!({ "name": "Worker", "backend": "claude" }));
    let agent = made["session"].as_str().unwrap().to_string();
    let path = format!("/terminal/{agent}");

    // A turn's tool writes the conversation: the terminal, a second writer, is refused in
    // words the terminal shows, and nothing of it is started.
    host.send(&agent, "hold on", "m1");
    wait_for("the turn's tool to start", || started.exists());
    let refused = asked_for_a_socket(&host, ADMIN, &path);
    // The words and the stable code in the body, which the apps read a refused terminal's
    // sentence from, and the code again in a header.
    assert_eq!(
        (
            refused.status,
            serde_json::from_slice::<Value>(&refused.body).unwrap(),
            refused.header("content-type"),
            refused.header("x-clarp-code"),
        ),
        (
            409,
            json!({
                "error": "A turn of this agent is running. Stop it or wait until it ends, \
                          then open the terminal.",
                "code": "agent_turn_running",
            }),
            "application/json",
            "agent_turn_running",
        )
    );
    // A plain read of the address, as an app that cannot read a refused upgrade's body makes,
    // is told the same, and takes nothing: the turn runs on, no terminal is counted.
    let plain = fetch(&host, ADMIN, "GET", &path, &[], b"");
    assert_eq!(
        (
            plain.status,
            serde_json::from_slice::<Value>(&plain.body).unwrap(),
            plain.header("x-clarp-code"),
        ),
        (
            409,
            json!({
                "error": "A turn of this agent is running. Stop it or wait until it ends, \
                          then open the terminal.",
                "code": "agent_turn_running",
            }),
            "agent_turn_running",
        )
    );
    // The refusal held nothing: the turn goes on and ends as ever.
    std::fs::write(&go, "").unwrap();
    wait_for("the turn to end", || host.state(&agent) == "done");
    assert_eq!(host.conversation(&agent).len(), 2);

    // A plain read of an idle agent's address is told only that it asked for no socket, and
    // holds nothing back: the next turn starts at once.
    let plain = fetch(&host, ADMIN, "GET", &path, &[], b"");
    assert_eq!(
        (plain.status, plain.text().as_str()),
        (426, "upgrade required (websocket)")
    );
    host.send(&agent, "after a plain read", "m0");
    wait_for("the turn after a plain read", || {
        std::fs::read_to_string(&started).is_ok_and(|ran| ran.lines().count() == 2)
    });
    wait_for("that turn to end", || host.state(&agent) == "done");

    // With no turn running, the terminal opens on the agent's conversation, and a turn sent
    // meanwhile waits for it to be closed, as before.
    let terminal = ToolScreen::open(&host, ADMIN, &agent);
    host.send(&agent, "after the terminal", "m2");
    std::thread::sleep(Duration::from_millis(300));
    assert_eq!(
        std::fs::read_to_string(&started).unwrap().lines().count(),
        2
    );
    // A second terminal while that turn waits for the first is no second writer beside a
    // tool: it opens.
    let second = ToolScreen::open(&host, ADMIN, &agent);
    assert_eq!(second.leave(), Some(1000));
    assert_eq!(terminal.leave(), Some(1000));
    wait_for("the turn that waited", || host.state(&agent) == "done");
    assert_eq!(
        std::fs::read_to_string(&started).unwrap().lines().count(),
        3
    );
}
