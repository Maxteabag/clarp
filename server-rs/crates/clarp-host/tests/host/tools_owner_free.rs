//! The Host's tests of what an agent's tools do without the owner's token: HTTP routes through
//! bound privacy; the contacts, pictures and helpers an agent
//! manages for itself from its own turn; and the lines that name who still uses the owner's
//! token. One file a piece, declared in `host.rs`.

use super::bound_privacy::{CODEX, SECRET, agent_id, bound_agent, first_turn_token, own_home};
use super::*;

/// One of an agent's own tools with only the tools' token, as a worker of this change runs:
/// with the turn it was started from, or none.
fn as_tools(
    host: &Host,
    dir: &std::path::Path,
    tools: &str,
    turn: Option<&str>,
    args: &[&str],
) -> (i32, String, String) {
    let mut command = sealed_command(dir, env!("CARGO_BIN_EXE_clarp-host"));
    command
        .arg("tool")
        .args(args)
        .current_dir(dir)
        .env("CLARP_HOST_ADDR", &host.address)
        .env("CLARP_TOOL_TOKEN", tools)
        .env_remove("CLARP_HOST_TOKEN")
        .env_remove("CLARP_PROVIDER_TURN")
        .env_remove("CLARP_SESSION")
        .env_remove("CLAUDE_PWA_SESSION");
    if let Some(turn) = turn {
        command.env("CLARP_PROVIDER_TURN", turn);
    }
    let out = command.ended();
    (
        out.status.code().unwrap_or(-1),
        String::from_utf8_lossy(&out.stdout).into_owned(),
        String::from_utf8_lossy(&out.stderr).into_owned(),
    )
}

#[test]
fn an_agents_tools_read_the_fleet_through_bound_privacy() {
    let place = Place::new("tools-read-fleet");
    let codex = place.root.join("codex");
    program(&codex, CODEX);
    let admin = "tools-read-owner";
    let host = place.start_in(
        admin,
        &[],
        &[("CLARP_CODEX_PROGRAM", codex.to_str().unwrap())],
    );
    let home = own_home(&place, "home-a");
    let ada = bound_agent(&host, "Ada", &home);
    let ben = host.new_agent("Ben");
    for (session, id) in [(&ada, "u-1"), (&ben, "u-2")] {
        host.send(session, "hello", id);
    }
    for session in [&ada, &ben] {
        wait_for("each first turn", || host.state(session) == "done");
    }
    let tools = clarp_host::tool_token(admin);

    // The agents, the cards and an agent's waiting messages are read with the tools' token.
    let (status, snapshot) = host.ask_as(&tools, "GET", "/agents/snapshot", None);
    assert_eq!(status, 200, "{snapshot}");
    let rows = snapshot["agents"].as_array().unwrap();
    assert!(rows.iter().any(|row| row["session"] == ada), "{snapshot}");
    assert!(rows.iter().any(|row| row["session"] == ben), "{snapshot}");
    // A tool that says no turn is an agent that cannot say whose it is: the bound agent's
    // words are not in what it reads.
    assert!(!snapshot.to_string().contains(SECRET), "{snapshot}");
    let (status, inbox) = host.ask_as(&tools, "GET", "/attention/inbox?limit=10", None);
    assert_eq!(status, 200, "{inbox}");
    let (status, queue) = host.ask_as(&tools, "GET", &format!("/turn-queue?session={ben}"), None);
    assert_eq!(status, 200, "{queue}");
    let (status, refused) = host.ask_as(&tools, "GET", &format!("/turn-queue?session={ada}"), None);
    assert_eq!(status, 403, "{refused}");
    assert_eq!(refused["error"], "bound_conversation", "{refused}");
    // Ada's own turn reads her queue.
    let ada_turn = first_turn_token(&place, &agent_id(&host, &ada));
    let answered = fetch(
        &host,
        &tools,
        "GET",
        &format!("/turn-queue?session={ada}"),
        &[("X-Clarp-Turn", ada_turn.as_str())],
        b"",
    );
    assert_eq!(answered.status, 200);

    let (status, _) = host.ask_as(&tools, "GET", &format!("/log?session={ben}"), None);
    assert_eq!(status, 200);
    let (status, refused) = host.ask_as(&tools, "GET", &format!("/log?session={ada}"), None);
    assert_eq!(status, 403, "{refused}");
    assert_eq!(refused["error"], "bound_conversation", "{refused}");
}

#[test]
fn an_agents_issued_token_can_read_and_change_voice_with_its_own_attribution() {
    let place = Place::new("agent-token-voice");
    let claude = place.root.join("claude");
    let codex = place.root.join("codex");
    program(&codex, CODEX);
    program(
        &claude,
        r#"#!/bin/sh
read -r line
umask 077
printf '%s\n%s\n' "$CLARP_TOOL_TOKEN" "$CLARP_PROVIDER_TURN" > "$0.credentials"
printf '%s\n' '{"type":"system","subtype":"init","session_id":"voice-fixture"}'
printf '%s\n' '{"type":"assistant","session_id":"voice-fixture","message":{"id":"voice-1","role":"assistant","content":[{"type":"text","text":"Ready"}]}}'
printf '%s\n' '{"type":"result","subtype":"success","session_id":"voice-fixture","result":"done"}'
"#,
    );
    let owners = place.root.join("speech.toml");
    std::fs::write(&owners, "[tts]\nprovider='cartesia'\n").unwrap();
    let (endpoint, requests) = speech_reply_stand_in(
        "200 OK",
        r#"{"data":[{"id":"f0000000-0000-4000-8000-000000000011","name":"Fixture Voice"}]}"#,
    );
    let endpoint = endpoint.replace("/v1/speech-to-text", "/tts/bytes");
    let admin = "agent-token-fixture-owner";
    let host = place.start_in(
        admin,
        &["--speech-keys", owners.to_str().unwrap()],
        &[
            ("CLARP_CLAUDE_PROGRAM", claude.to_str().unwrap()),
            ("CLARP_CODEX_PROGRAM", codex.to_str().unwrap()),
            ("CARTESIA_API_KEY", "fixture-cartesia-key"),
            ("CLARP_CARTESIA_ENDPOINT", &endpoint),
        ],
    );
    let made = host.post(
        "/agents",
        json!({"name":"Voice Worker","session":"voice-worker","backend":"claude"}),
    );
    let private = bound_agent(&host, "Private Voice", &own_home(&place, "voice-private"));
    assert_eq!(
        host.ask(
            "POST",
            "/personas",
            Some(&json!({"name":"Voice Contact","voice_id":"prior-voice","generate_avatar":false})),
        )
        .0,
        201
    );
    host.send("voice-worker", "Choose a voice.", "voice-task");
    let kept = claude.with_extension("credentials");
    wait_for("the fixture credentials written by the turn", || {
        kept.exists()
    });
    wait_for("the fixture turn to finish", || {
        host.state("voice-worker") == "done"
    });
    let issued = std::fs::read_to_string(kept).unwrap();
    let (tools, turn) = issued.trim_end().split_once('\n').unwrap();
    assert!(!tools.is_empty() && tools != admin && !turn.is_empty());

    let row = "/agents/voice-worker";
    let voice = "f0000000-0000-4000-8000-000000000011";
    let contact_voice = r#"{"elevenlabs":"new-voice"}"#;
    let set_voice = json!({"session":"voice-worker","voice_id":voice,"provider":"cartesia"});
    let set_contact = json!({
        "original_name":"Voice Contact","name":"Voice Contact","voice_id":contact_voice,
    });
    for (path, body) in [
        ("/agent-voice", &set_voice),
        ("/personas/update", &set_contact),
    ] {
        let (status, refused) = host.ask_as(tools, "POST", path, Some(body));
        assert_eq!(status, 403, "{refused}");
        assert_eq!(refused["error"], "verified turn identity required");
    }
    let private_path = format!("/agents/{private}");
    let previous_voice = host.get(&private_path)["voice_id"].clone();
    let denied = fetch(
        &host,
        tools,
        "POST",
        "/agent-voice",
        &[("X-Clarp-Turn", turn), ("Content-Type", "application/json")],
        json!({"session":private,"voice_id":voice,"provider":"cartesia"})
            .to_string()
            .as_bytes(),
    );
    assert_eq!(denied.status, 403, "{}", denied.text());
    assert!(denied.text().contains("bound_conversation"));
    assert_eq!(host.get(&private_path)["voice_id"], previous_voice);

    let ask = |method, path, body: Value| {
        let text = if body.is_null() {
            String::new()
        } else {
            body.to_string()
        };
        let answer = fetch(
            &host,
            tools,
            method,
            path,
            &[("X-Clarp-Turn", turn), ("Content-Type", "application/json")],
            text.as_bytes(),
        );
        assert_eq!(answer.status, 200, "{method} {path}: {}", answer.text());
        serde_json::from_slice::<Value>(&answer.body).unwrap()
    };
    let listed = ask("GET", "/cartesia-voices", Value::Null);
    assert_eq!(listed["voices"][0]["id"], voice, "{listed}");
    assert!(
        requests
            .lock()
            .unwrap()
            .iter()
            .any(|request| request.starts_with("GET /voices"))
    );
    assert_eq!(
        ask("GET", row, Value::Null)["agent_id"],
        made["agent"]["agent_id"]
    );
    assert_eq!(ask("POST", "/agent-voice", set_voice)["ok"], true);
    assert_eq!(
        ask("GET", row, Value::Null)["voice_id"],
        json!({"cartesia":voice}).to_string()
    );
    let edited = ask("POST", "/personas/update", set_contact);
    let voices: Value =
        serde_json::from_str(edited["persona"]["voice_id"].as_str().unwrap()).unwrap();
    assert_eq!(voices["elevenlabs"], "new-voice");
    let agent = made["agent"]["agent_id"].as_str().unwrap();
    wait_for("agent-attributed audit of all four routes", || {
        let seen = host.heard.lock().unwrap();
        [
            "GET /cartesia-voices",
            "GET /agents/{session}",
            "POST /agent-voice",
            "POST /personas/update",
        ]
        .iter()
        .all(|route| {
            seen.contains(&format!(
                "agent token (agent {agent} turn 1): {route} -> 200"
            ))
        })
    });
    let seen = host.heard.lock().unwrap();
    assert!(!seen.contains(tools) && !seen.contains(turn));
}

#[test]
fn direct_queue_controls_record_the_signed_agent_and_keep_owner_work_held_on_stop() {
    use clarp_kernel::agent::AgentEvent;
    use clarp_kernel::journal::kind;
    use clarp_kernel::turn::{Outcome, TurnEvent};
    use clarp_ports::log::{Kinds, LogEngine};

    let place = Place::new("agent-token-queue-controls");
    let owner = "agent-queue-owner";
    let host = place.start_with_token(owner);
    let from = host.new_agent("Queue Caller");
    let target = host.new_agent("Queue Target");
    host.send(&from, "Ready", "first");
    wait_for("the caller has a turn", || host.state(&from) == "done");
    let token = first_turn_token(&place, &agent_id(&host, &from));
    let tools = clarp_host::tool_token(owner);
    let signed = |method: &str, path: &str, body: Value| {
        fetch(
            &host,
            &tools,
            method,
            path,
            &[
                ("X-Clarp-Turn", &token),
                ("Content-Type", "application/json"),
            ],
            body.to_string().as_bytes(),
        )
    };
    host.send(&target, "sleep 60", "live");
    wait_for("the target is working", || {
        host.state(&target) == "thinking"
    });
    for (id, text) in [
        ("interrupt", "sleep 60"),
        ("withdraw", "Never said"),
        ("by-hand", "Sent explicitly"),
        ("resume", "Resumed normally"),
    ] {
        assert_eq!(
            host.post(
                "/send",
                json!({"session":target,"text":text,"client_msg_id":id,"queue_if_busy":true}),
            )["queued"],
            true
        );
    }
    let (status, refused) = host.ask_as(&tools, "DELETE", "/turn-queue/withdraw", None);
    assert_eq!(status, 403, "{refused}");
    assert_eq!(refused["error"], "verified turn identity required");
    let cut = signed("POST", "/turn-queue/interrupt/take-over", json!({}));
    assert_eq!(cut.status, 202, "{}", cut.text());
    wait_for("the selected replacement starts", || {
        host.turns(&target)
            .iter()
            .any(|row| row["id"] == "u-interrupt")
            && host.state(&target) == "thinking"
    });

    let stopped = signed("POST", "/stop", json!({"session":target,"actor":"user"}));
    assert_eq!(stopped.status, 200, "{}", stopped.text());
    wait_for("the replacement stops", || {
        host.state(&target) == "interrupted"
    });
    let queue = host.get(&format!("/turn-queue?session={target}"));
    assert_eq!(queue["paused"], true);
    assert_eq!(queue["items"].as_array().unwrap().len(), 3);
    assert_eq!(
        signed("DELETE", "/turn-queue/withdraw", json!({})).status,
        200
    );
    let sent = signed("POST", "/turn-queue/by-hand/send", json!({}));
    assert_eq!(sent.status, 202, "{}", sent.text());
    wait_for("the hand-selected message finishes", || {
        host.state(&target) == "done"
    });
    let resumed = signed("POST", "/turn-queue/resume", json!({"session":target}));
    assert_eq!(resumed.status, 200, "{}", resumed.text());
    wait_for("the remainder runs after resume", || {
        host.turns(&target)
            .iter()
            .any(|row| row["text"] == "Resumed normally" && row["role"] == "assistant")
    });

    let id = agent_id(&host, &target);
    let log = clarp_store_sqlite::SqliteEngine::new()
        .read_committed(
            &place.root.join("data/agents").join(id),
            clarp_codec::standard_set("zstd").unwrap(),
        )
        .unwrap();
    let by = format!("agent:{from}");
    let mut recorded: Vec<_> = log
        .read_after(0, Kinds::only(kind::AGENT_EVENT), 100)
        .unwrap()
        .iter()
        .filter_map(|item| {
            let event = serde_json::from_slice::<AgentEvent>(&item.body).unwrap();
            match event {
                AgentEvent::SentNow {
                    turn, by: actor, ..
                } if actor == by => Some(("sent", Some(turn))),
                AgentEvent::Paused {
                    by: actor,
                    verified: true,
                    ..
                } if actor == by => Some(("stop", None)),
                AgentEvent::Turn {
                    change:
                        TurnEvent::Settled {
                            outcome: Outcome::Withdrawn { by: actor },
                            ..
                        },
                    ..
                } if actor == by => Some(("withdraw", None)),
                AgentEvent::Resumed { by: actor, .. } if actor == by => Some(("resume", None)),
                _ => None,
            }
        })
        .collect();
    // A takeover can record the same selection when it cuts the old turn and when its
    // replacement starts. Both records carry the same signed caller and queued turn.
    recorded.dedup();
    assert_eq!(
        recorded
            .iter()
            .map(|(action, _)| *action)
            .collect::<Vec<_>>(),
        ["sent", "stop", "withdraw", "sent", "resume"]
    );
    assert_ne!(recorded[0].1, recorded[3].1);
}

#[test]
fn an_agent_tokens_http_access_cannot_answer_an_owner_decision() {
    let place = Place::new("agent-token-decision-owner");
    let owner = "fixture-owner";
    let host = place.start_with_token(owner);
    let session = host.new_agent("Approval Fixture");
    host.send(&session, "Ready", "first-turn");
    wait_for("the fixture turn to finish", || {
        host.state(&session) == "done"
    });
    let (status, created) = host.ask(
        "POST",
        "/decisions",
        Some(&json!({
            "session": session, "title": "Fixture decision", "question": "Approve?",
        })),
    );
    assert_eq!(status, 201, "{created}");
    let choice = &created["artifact"]["decision"];
    let id = choice["decision_id"].as_str().unwrap();
    let path = format!("/decisions/{id}/resolve");
    let body = json!({"choice":"accepted","expected_revision":choice["revision"]});
    let tools = clarp_host::tool_token(owner);
    let (status, refused) = host.ask_as(&tools, "POST", &path, Some(&body));
    assert_eq!(status, 403, "{refused}");
    assert_eq!(refused["error"], "verified turn identity required");
    let turn = first_turn_token(&place, &agent_id(&host, &session));
    let stop = json!({"session":session,"actor":"user"});
    let (status, refused) = host.ask_as(&tools, "POST", "/stop", Some(&stop));
    assert_eq!(status, 403, "{refused}");
    assert_eq!(refused["error"], "verified turn identity required");
    let stood = fetch(
        &host,
        &tools,
        "POST",
        "/stop",
        &[
            ("X-Clarp-Turn", &turn),
            ("Content-Type", "application/json"),
        ],
        stop.to_string().as_bytes(),
    );
    assert_eq!(stood.status, 200, "{}", stood.text());
    let stood: Value = serde_json::from_slice(&stood.body).unwrap();
    assert_eq!(stood["stop_actor"], format!("agent:{session}"));
    assert_eq!(stood["stop_actor_verified"], true);
    let signed = fetch(
        &host,
        &tools,
        "POST",
        &path,
        &[
            ("X-Clarp-Turn", &turn),
            ("Content-Type", "application/json"),
        ],
        body.to_string().as_bytes(),
    );
    assert_eq!(signed.status, 403, "{}", signed.text());
    assert!(
        signed
            .text()
            .contains("agent resolution requires on_behalf_session")
    );
    assert_eq!(host.get("/attention")["items"][0]["decision_id"], id);
}

#[test]
fn a_delegated_agent_reads_the_decision_it_resolves_only_with_object_access() {
    let place = Place::new("agent-token-private-decision");
    let owners = place.root.join("owners.toml");
    std::fs::write(
        &owners,
        "[agents]\ndecision_on_behalf_sessions = [\"decision-desk\"]\n",
    )
    .unwrap();
    let admin = "fixture-private-decision-owner";
    let codex = place.root.join("codex");
    program(&codex, CODEX);
    let host = place.start_in(
        admin,
        &["--speech-keys", owners.to_str().unwrap()],
        &[("CLARP_CODEX_PROGRAM", codex.to_str().unwrap())],
    );
    let bound = bound_agent(&host, "Private case", &own_home(&place, "private-home"));
    let desk = "decision-desk";
    host.post(
        "/agents",
        json!({"name":"Decision desk","session":desk,"backend":"scripted"}),
    );
    host.send(desk, "Ready", "desk-first-turn");
    wait_for("the desk to finish", || host.state(desk) == "done");
    let turn = first_turn_token(&place, &agent_id(&host, desk));
    let marker = "private-fixture-question";
    let (status, asked) = host.ask(
        "POST",
        "/decisions",
        Some(&json!({"session":bound,"title":"Private case","question":marker})),
    );
    assert_eq!(status, 201, "{asked}");
    let artifact = &asked["artifact"];
    let id = artifact["decision"]["decision_id"].as_str().unwrap();
    let artifact_path = format!("/artifacts/{}", artifact["artifact_id"].as_str().unwrap());
    let path = format!("/decisions/{id}/resolve");
    let body = json!({"choice":"accepted","expected_revision":1,"on_behalf_session":desk});
    let tools = clarp_host::tool_token(admin);
    let resolve = || {
        fetch(
            &host,
            &tools,
            "POST",
            &path,
            &[
                ("X-Clarp-Turn", &turn),
                ("Content-Type", "application/json"),
            ],
            body.to_string().as_bytes(),
        )
    };

    let blocked = resolve();
    assert_eq!(blocked.status, 403, "{}", blocked.text());
    assert!(!blocked.text().contains(marker));
    let refusal: Value = serde_json::from_slice(&blocked.body).unwrap();
    assert_eq!(refusal["error"], "bound_conversation");
    assert!(refusal.get("artifact").is_none());
    assert_eq!(
        host.get(&artifact_path)["artifact"]["decision"]["status"],
        "pending"
    );

    host.post(
        "/agent-provider",
        json!({"session":bound,"add_readers":[desk]}),
    );
    let granted = resolve();
    assert_eq!(granted.status, 200, "{}", granted.text());
    let result: Value = serde_json::from_slice(&granted.body).unwrap();
    assert_eq!(result["artifact"]["artifact_id"], artifact["artifact_id"]);
    assert_eq!(result["artifact"]["decision"]["question"], marker);
    assert_eq!(
        result["artifact"]["decision"]["resolved_by"],
        "agent:decision-desk"
    );
}

#[test]
fn an_agent_makes_contacts_and_puts_away_its_own_helpers_from_its_own_turn() {
    let place = Place::new("tools-own-helpers");
    let admin = "tools-helpers-owner";
    let host = place.start_with_token(admin);
    let dir = place.root.clone();
    let tools = clarp_host::tool_token(admin);
    let lead = host.new_agent("Lead");
    let peer = host.new_agent("Peer");
    for (session, id) in [(&lead, "u-1"), (&peer, "u-2")] {
        host.send(session, "hello", id);
    }
    for session in [&lead, &peer] {
        wait_for("each first turn", || host.state(session) == "done");
    }
    let lead_turn = first_turn_token(&place, &agent_id(&host, &lead));
    let turn = Some(lead_turn.as_str());
    let (code, made, error) = as_tools(
        &host,
        &dir,
        &tools,
        turn,
        &[
            "clarp-admin",
            "agent",
            "create",
            "Aide",
            "--parent",
            &lead,
            "--role",
            "helper",
        ],
    );
    assert_eq!(code, 0, "{error}");
    let made: Value = serde_json::from_str(&made).unwrap();
    let helper = made["session"].as_str().unwrap().to_string();
    let listed = |session: &str| {
        host.get("/agents/snapshot?include_archived=0")["agents"]
            .as_array()
            .unwrap()
            .iter()
            .any(|row| row["session"] == session)
    };

    // Without a turn nobody can be said to ask: nothing is done.
    let archive = |turn, session: &str, more: &[&str]| {
        let mut args = vec!["clarp-admin", "agent", "archive", "--session", session];
        args.extend_from_slice(more);
        as_tools(&host, &dir, &tools, turn, &args)
    };
    let (code, _, said) = archive(None, &helper, &[]);
    assert_eq!(code, 4, "{said}");
    assert!(said.contains("verified turn identity required"), "{said}");
    // Its own helper is put away, and is taken back.
    let (code, answer, said) = archive(turn, &helper, &[]);
    assert_eq!(code, 0, "{said}");
    assert!(answer.contains("\"archived\": true"), "{answer}");
    assert!(!listed(&helper));
    let (code, _, said) = archive(turn, &helper, &["--restore"]);
    assert_eq!(code, 0, "{said}");
    assert!(listed(&helper));

    // A contact is made from the lead's turn, and from no turn is not.
    let persona = |turn| {
        as_tools(
            &host,
            &dir,
            &tools,
            turn,
            &[
                "clarp-admin",
                "persona",
                "create",
                "Quillmere",
                "--personality",
                "Calm.",
                "--no-picture",
            ],
        )
    };
    let (code, _, said) = persona(None);
    assert_eq!(code, 4, "{said}");
    let (code, answer, said) = persona(turn);
    assert_eq!(code, 0, "{said}");
    let answer: Value = serde_json::from_str(&answer).unwrap();
    assert_eq!(answer["persona"]["name"], "Quillmere", "{answer}");

    // A picture: the peer's is not the lead's to change; for its helper the request reaches
    // the Host's own rules (here: there is no such picture).
    let portrait = |session: &str| {
        as_tools(
            &host,
            &dir,
            &tools,
            turn,
            &[
                "clarp-admin",
                "portrait",
                "add",
                "--session",
                session,
                "--asset",
                "no-such-asset",
            ],
        )
    };
    let (code, _, said) = portrait(&peer);
    assert_eq!(code, 4, "{said}");
    assert!(said.contains("neither"), "{said}");
    let (code, _, said) = portrait(&helper);
    assert_eq!(code, 4, "{said}");
    assert!(said.contains("the Host refused it"), "{said}");
}

#[test]
fn any_agent_puts_away_any_agent_and_the_host_keeps_and_logs_who() {
    let place = Place::new("tools-any-archive");
    let admin = "tools-any-archive-owner";
    let host = place.start_with_token(admin);
    let dir = place.root.clone();
    let tools = clarp_host::tool_token(admin);
    let ada = host.new_agent("Ada");
    let ben = host.new_agent("Ben");
    for (session, id) in [(&ada, "u-1"), (&ben, "u-2")] {
        host.send(session, "hello", id);
    }
    for session in [&ada, &ben] {
        wait_for("each first turn", || host.state(session) == "done");
    }
    let ada_id = agent_id(&host, &ada);
    let ben_id = agent_id(&host, &ben);
    let ada_turn = first_turn_token(&place, &ada_id);
    let row = |session: &str| {
        host.get("/agents/snapshot")["agents"]
            .as_array()
            .unwrap()
            .iter()
            .find(|row| row["session"] == session)
            .cloned()
            .unwrap()
    };
    let archive = |turn: Option<&str>, session: &str, more: &[&str]| {
        let mut args = vec!["clarp-admin", "agent", "archive", "--session", session];
        args.extend_from_slice(more);
        as_tools(&host, &dir, &tools, turn, &args)
    };
    let events = host.listen_at(&format!("/events?token={admin}"));
    wait_for("the authenticated event stream", || {
        events
            .lock()
            .unwrap()
            .iter()
            .any(|event| event["type"] == "server-version")
    });

    // Without a verified turn nobody can be said to ask: nothing is done, as before.
    let (code, _, said) = archive(None, &ben, &[]);
    assert_eq!(code, 4, "{said}");
    assert!(said.contains("verified turn identity required"), "{said}");
    assert!(row(&ben)["archived_at"].is_null());
    // An agent that does not exist is no agent.
    let (code, _, said) = archive(Some(&ada_turn), "no-such-agent", &[]);
    assert_eq!(code, 1, "{said}");
    assert!(said.contains("no such agent"), "{said}");

    // Ada puts Ben away, who is neither she nor her helper; the row and the event say she did.
    let by = json!({ "kind": "agent", "session": ada, "agent_id": ada_id });
    let (code, answer, said) = archive(Some(&ada_turn), &ben, &[]);
    assert_eq!(code, 0, "{said}");
    let answer: Value = serde_json::from_str(&answer).unwrap();
    assert_eq!(answer["archived"], true, "{answer}");
    assert_eq!(answer["archived_by"], by, "{answer}");
    let put_away = row(&ben);
    assert!(put_away["archived_at"].is_i64(), "{put_away}");
    assert_eq!(put_away["archived_by"], by, "{put_away}");
    let told = |archived: bool| {
        events
            .lock()
            .unwrap()
            .iter()
            .find(|event| {
                event["type"] == "agent-roster"
                    && event["kind"] == "archive-changed"
                    && event["session"] == ben
                    && event["archived"] == archived
            })
            .cloned()
    };
    wait_for("the archive event", || told(true).is_some());
    assert_eq!(told(true).unwrap()["archived_by"], by);

    // And takes him back: he is no longer away, so no one put him away.
    let (code, _, said) = archive(Some(&ada_turn), &ben, &["--restore"]);
    assert_eq!(code, 0, "{said}");
    let back = row(&ben);
    assert!(back["archived_at"].is_null(), "{back}");
    assert!(back["archived_by"].is_null(), "{back}");
    wait_for("the restore event", || told(false).is_some());
    assert!(told(false).unwrap().get("archived_by").is_none());

    // The Host's token from no app (a script, clarp-admin with it) says so.
    assert_eq!(
        host.post(
            "/agent-archive",
            json!({ "session": ben, "archived": true })
        )["archived_by"],
        json!({ "kind": "host-token" })
    );
    assert_eq!(row(&ben)["archived_by"], json!({ "kind": "host-token" }));
    // An app of the owner's on this machine (the Host's token with X-Clarp-Client) is the owner.
    let body = json!({ "session": ben, "archived": true }).to_string();
    let answered = fetch(
        &host,
        admin,
        "POST",
        "/agent-archive",
        &[
            ("X-Clarp-Client", "desktop/1 contract=1"),
            ("Content-Type", "application/json"),
        ],
        body.as_bytes(),
    );
    assert_eq!(answered.status, 200);
    let answered: Value = serde_json::from_slice(&answered.body).unwrap();
    assert_eq!(
        answered["archived_by"],
        json!({ "kind": "owner" }),
        "{answered}"
    );
    assert_eq!(row(&ben)["archived_by"], json!({ "kind": "owner" }));

    // Each one is in the Host's log: what, which agent, and who asked.
    let heard = || host.heard.lock().unwrap().clone();
    let owners = format!("clarp-host: agent archived: {ben} ({ben_id}) by owner at ");
    wait_for("the lines", || heard().contains(&owners));
    let script = format!("clarp-host: agent archived: {ben} ({ben_id}) by host-token at ");
    assert!(heard().contains(&script), "{}", heard());
    let heard = heard();
    for what in ["archived", "restored"] {
        let line = format!("clarp-host: agent {what}: {ben} ({ben_id}) by {ada} ({ada_id}) at ");
        assert!(heard.contains(&line), "{line}\n{heard}");
    }
    // What was refused was not done, and is not written as done.
    assert!(!heard.contains("no-such-agent ("), "{heard}");
}

#[test]
fn the_host_names_who_uses_the_owners_token_and_never_the_token() {
    let place = Place::new("owner-token-lines");
    let admin = "owner-token-lines-owner";
    let host = place.start_with_token(admin);
    let dir = place.root.clone();
    let session = host.new_agent("Worker");

    // A request with the owner's token, from no app and no turn: one line, with its route.
    host.get("/agents/snapshot");
    // The router's route is written, never the path a caller sent, which may carry a token.
    let odd = fetch(
        &host,
        admin,
        "GET",
        "/agents/snapshot&token=SECRET_PATH_MARKER",
        &[],
        b"",
    );
    assert_ne!(odd.status, 0);
    // An app's own request is not counted.
    let app = fetch(
        &host,
        admin,
        "GET",
        "/server-info",
        &[("X-Clarp-Client", "desktop/1 contract=1")],
        b"",
    );
    assert_eq!(app.status, 200);
    // The owner's own command, said so: one line with its tool and verb.
    let (code, _, said) = tool(
        &host,
        &dir,
        None,
        &["clarp-admin", "agent", "helper-state", &session],
    );
    assert_eq!(code, 0, "{said}");
    // The tools' token is no owner's: no line.
    let tools = clarp_host::tool_token(admin);
    let (code, _, said) = as_tools(&host, &dir, &tools, None, &["clarp-admin", "sessions"]);
    assert_eq!(code, 0, "{said}");

    let heard = || host.heard.lock().unwrap().clone();
    wait_for("the lines", || {
        heard().contains("clarp-host: owner mode: tool clarp-admin agent helper-state")
    });
    let heard = heard();
    assert!(
        heard.contains("clarp-host: admin token without a turn: GET /agents/snapshot ("),
        "{heard}"
    );
    assert!(
        !heard.contains("without a turn: GET /server-info"),
        "{heard}"
    );
    assert!(!heard.contains("tool clarp-admin sessions"), "{heard}");
    assert!(!heard.contains(admin), "a token was written: {heard}");
    assert!(
        !heard.contains("SECRET_PATH_MARKER"),
        "a path was written: {heard}"
    );
    assert!(!heard.contains(&tools), "a token was written: {heard}");
}
