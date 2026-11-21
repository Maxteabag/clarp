use super::*;

/// Reads what the tool actually got during each launch, before the scoped file is retired.
fn turn(host: &Host, place: &Place, session: &str, label: &str) -> (String, Option<Value>) {
    let receipt = place.root.join("mcp-receipt");
    if receipt.exists() {
        std::fs::remove_file(&receipt).unwrap();
    }
    host.post(
        "/send",
        json!({
            "session": session, "text": "check MCP", "client_msg_id": label,
            "synthesize_audio": false,
        }),
    );
    wait_for("Claude received its MCP configuration and answered", || {
        receipt.is_file() && host.state(session) == "done"
    });
    let told = std::fs::read_to_string(receipt).unwrap();
    let (strict, rest) = told.split_once('\n').unwrap();
    let scoped = if rest.is_empty() {
        None
    } else {
        let (mode, contents) = rest.split_once('\n').unwrap();
        assert_eq!(mode, "600");
        Some(serde_json::from_str(contents).unwrap())
    };
    (strict.to_owned(), scoped)
}

fn stand_in(place: &Place) -> PathBuf {
    let claude = place.root.join("claude");
    let receipt = place.root.join("mcp-receipt");
    program(
        &claude,
        &format!(
            r#"#!/bin/sh
set -e
read -r message
strict=default
scoped=
while [ "$#" -gt 0 ]; do
    case "$1" in
        --strict-mcp-config) strict=strict ;;
        --mcp-config) scoped="$2"; shift ;;
    esac
    shift
done
{{
    printf '%s\n' "$strict"
    if [ -n "$scoped" ]; then
        stat -c '%a' "$scoped"
        cat "$scoped"
    fi
}} > "{}"
{}"#,
            receipt.display(),
            STAND_IN_ANSWERS
        ),
    );
    claude
}

fn make(host: &Host, request: Value) -> Value {
    let (status, answer) = host.ask("POST", "/agents", Some(&request));
    assert_eq!(status, 200, "{answer}");
    answer
}

fn selected(host: &Host, session: &str) -> Value {
    let snapshot = host.get("/agents/snapshot");
    let row = snapshot["agents"]
        .as_array()
        .unwrap()
        .iter()
        .find(|row| row["session"] == session)
        .unwrap();
    row["mcp_servers"].clone()
}

#[test]
fn mcp_selection_survives_restart_and_scopes_only_the_agent_that_chose_it() {
    let place = Place::new("creation-mcp");
    let claude = stand_in(&place);
    let world = [("CLARP_CLAUDE_PROGRAM", claude.to_str().unwrap())];
    let home_config = place.root.join("home/.claude.json");
    let alpha =
        json!({"type": "stdio", "command": "/synthetic/alpha", "env": {"KEY": "alpha-test-key"}});
    let beta = json!({"type": "http", "url": "http://127.0.0.1:9/beta"});
    let withheld = json!({"command": "/synthetic/private"});
    let catalog = json!({"mcpServers": {"alpha": alpha, "beta": beta, "withheld": withheld}});
    std::fs::write(&home_config, catalog.to_string()).unwrap();
    let host = place.start_in("secret", &[], &world);
    assert_eq!(
        host.get("/agents/snapshot")["available_mcp_servers"],
        json!(["alpha", "beta", "withheld"])
    );
    let (status, reply) = host.ask(
        "POST",
        "/agents",
        Some(&json!({ "name": "MCP Codex", "backend": "codex", "mcp_servers": ["alpha"] })),
    );
    assert_eq!(
        (status, reply),
        (
            400,
            json!({
                "error": "mcp servers unsupported for backend",
                "message": "MCP server selection is unavailable for Codex.",
            })
        )
    );
    let made = make(
        &host,
        json!({
            "name": "MCP Choose", "mcp_servers": [" beta ", "alpha", "beta", ""],
        }),
    );
    let session = made["session"].as_str().unwrap();
    let wanted = json!(["beta", "alpha"]);
    assert_eq!(made["agent"]["mcp_servers"], wanted);
    let bare = make(&host, json!({ "name": "MCP Bare" }));
    let bare_session = bare["session"].as_str().unwrap();
    assert_eq!(bare["agent"]["mcp_servers"], json!([]));
    assert_eq!(
        turn(&host, &place, bare_session, "unset"),
        ("strict".into(), None)
    );
    host.kill();

    let host = place.start_in("secret", &[], &world);
    assert_eq!(selected(&host, session), wanted);
    assert_eq!(
        turn(&host, &place, session, "selected"),
        (
            "strict".into(),
            Some(json!({"mcpServers": {"beta": beta, "alpha": alpha}}))
        )
    );
    let made = make(&host, json!({"name": "MCP Choose", "replace_sid": session}));
    assert_eq!(made["agent"]["mcp_servers"], wanted);
    std::fs::write(
        &home_config,
        json!({"mcpServers": {"alpha": alpha}}).to_string(),
    )
    .unwrap();
    assert_eq!(selected(&host, session), wanted);
    assert_eq!(
        turn(&host, &place, session, "catalog-changed"),
        (
            "strict".into(),
            Some(json!({"mcpServers": {"alpha": alpha}}))
        )
    );
    let made = make(
        &host,
        json!({
            "name": "MCP Choose", "replace_sid": session, "mcp_servers": [],
        }),
    );
    assert_eq!(made["agent"]["mcp_servers"], json!([]));
    assert_eq!(selected(&host, session), json!([]));
    assert_eq!(
        turn(&host, &place, session, "cleared"),
        ("strict".into(), None)
    );
}

#[test]
fn mcp_selection_is_kept_when_the_owner_turns_strict_scoping_off() {
    let place = Place::new("creation-mcp-nonstrict");
    let claude = stand_in(&place);
    let world = [("CLARP_CLAUDE_PROGRAM", claude.to_str().unwrap())];
    let owners = place.root.join("owner.toml");
    std::fs::write(&owners, "[mcp]\nstrict = false\n").unwrap();
    std::fs::write(
        place.root.join("home/.claude.json"),
        r#"{"mcpServers":{"alpha":{"command":"/synthetic/alpha"}}}"#,
    )
    .unwrap();
    let host = place.start_in(
        "secret",
        &["--speech-keys", owners.to_str().unwrap()],
        &world,
    );
    let made = make(
        &host,
        json!({ "name": "MCP Choose", "mcp_servers": ["alpha"] }),
    );
    assert_eq!(made["agent"]["mcp_servers"], json!(["alpha"]));
    let session = made["session"].as_str().unwrap();
    assert_eq!(
        turn(&host, &place, session, "unscoped"),
        ("default".into(), None)
    );
}
