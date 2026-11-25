//! Issue 777: a helper's chat that moves to this Host keeps the turns other agents gave it as
//! its conversation. Its history comes as imported rows, and a chat that was a helper's says so
//! in what the move takes (`helper_chat`), also where the agent is a helper no more.

use super::*;

const SOURCE_TOKEN: &str = "test-helper-source";
const DESTINATION_TOKEN: &str = "test-helper-destination";

/// Two turns a parent gave a helper, as the source Host serves them: for the agents.
fn parent_turns(session: &str) -> Value {
    let mut rows = Vec::new();
    for (n, (asked, answered)) in [
        ("Count the files.", "One file."),
        ("And now?", "Two files."),
    ]
    .into_iter()
    .enumerate()
    {
        let trace = format!("tr-{session}-{n}");
        rows.push(json!({
            "id": format!("u-{session}-{n}"), "role": "user", "text": asked, "origin": "agent",
            "audience": "agents", "trace_id": trace, "sender_agent_id": "boss-id",
        }));
        rows.push(json!({
            "id": format!("r-{session}-{n}"), "role": "assistant", "text": answered,
            "origin": "agent", "audience": "agents", "trace_id": trace, "phase": "final",
            "session_rows": 0,
        }));
    }
    json!({ "turns": rows })
}

#[test]
fn a_helpers_chat_that_moved_here_keeps_the_turns_other_agents_gave_it() {
    let place = Place::new("helper-moved");
    let host = place.start_with(DESTINATION_TOKEN, &["--name", "Destination"]);
    let cwd = place.root.join("work");
    std::fs::create_dir_all(&cwd).unwrap();
    // The Host hides what agents say to each other: a helper's chat shows it all the same.
    host.post(
        "/coordination-settings",
        json!({"coordination_visibility": "hide"}),
    );
    let (source, _) = answers_using(move |path, _| {
        let (route, query) = path.split_once('?').unwrap_or((path, ""));
        let json = match route {
            "/server-info" => json!({"capabilities": {"features": []}}),
            // `hal` is a helper; `dee` was one and is an agent like any other now.
            "/agents/snapshot" => json!({"agents": [
                {"agent_id": "hal-id", "session": "hal", "persona": "Hal", "backend": "scripted",
                 "cwd": cwd, "busy": false, "role": "helper", "parent_agent_id": "boss-id"},
                {"agent_id": "dee-id", "session": "dee", "persona": "Dee", "backend": "scripted",
                 "cwd": cwd, "busy": false, "role": "agent", "helper_chat": true},
            ], "personas": []}),
            "/log" if query.contains("session=hal") => parent_turns("hal"),
            "/log" if query.contains("session=dee") => parent_turns("dee"),
            "/task-plans" => json!({"plans": []}),
            "/artifacts" => json!({"artifacts": []}),
            "/media" => json!({"assets": []}),
            "/agent-portraits" => json!({"portraits": []}),
            "/transcription-providers" => json!({}),
            _ => {
                return (
                    "404 Not Found",
                    json!({"error": "no such fixture"}).to_string(),
                );
            }
        };
        ("200 OK", json.to_string())
    });
    for session in ["hal", "dee"] {
        let output = place
            .host_command()
            .args([
                "import",
                "--from",
                &source,
                "--host",
                &host.address,
                "--session",
                session,
            ])
            .env("CLARP_SOURCE_TOKEN", SOURCE_TOKEN)
            .env("CLARP_HOST_TOKEN", DESTINATION_TOKEN)
            .ended();
        assert!(
            output.status.success(),
            "{session}: {}",
            String::from_utf8_lossy(&output.stderr)
        );
        let chat = host.get(&format!(
            "/log?session={session}&view=conversation&coordination=auto"
        ));
        let texts: Vec<&str> = chat["turns"]
            .as_array()
            .unwrap()
            .iter()
            .map(|row| row["text"].as_str().unwrap_or_default())
            .collect();
        assert_eq!(
            texts,
            ["Count the files.", "One file.", "And now?", "Two files."],
            "{session}: {chat}"
        );
        assert_eq!(
            chat["coordination"],
            json!({"visibility": "show", "hidden": false, "source": "helper"}),
            "{session}: {chat}"
        );
    }
    // What a move takes of it says that it was a helper's, and a helper's own row says that it
    // is one, so an app can tell a helper's chat by this field alone.
    let rows = host.get("/agents/snapshot")["agents"].clone();
    let row = |session: &str| {
        rows.as_array()
            .unwrap()
            .iter()
            .find(|agent| agent["session"] == session)
            .cloned()
            .unwrap()
    };
    let dee = row("dee");
    assert_eq!(dee["helper_chat"], true, "{dee}");
    let hal = row("hal");
    assert_eq!(hal["helper_chat"], true, "{hal}");
}
