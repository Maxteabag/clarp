//! The Host's tests of what an agent's tools do without the owner's token (#795): the fleet's
//! reads they are given, through bound privacy; the contacts, pictures and helpers an agent
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
fn an_agents_tools_read_the_fleet_through_bound_privacy_and_change_nothing_else() {
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

    // Nothing else outside the tools opens to it.
    for (method, path) in [
        ("POST", "/turn-queue/resume"),
        ("GET", "/attention"),
        ("GET", &format!("/log?session={ben}") as &str),
        ("POST", "/personas"),
        ("POST", "/agent-archive"),
        ("POST", "/agent-portraits"),
    ] {
        let body = json!({ "session": ben });
        assert_eq!(
            host.ask_as(&tools, method, path, Some(&body)).0,
            403,
            "{method} {path}"
        );
    }
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
    let events = host.listen();

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

    // The owner's own archive says the owner.
    assert_eq!(
        host.post(
            "/agent-archive",
            json!({ "session": ben, "archived": true })
        )["archived_by"],
        json!({ "kind": "owner" })
    );
    assert_eq!(row(&ben)["archived_by"], json!({ "kind": "owner" }));

    // Each one is in the Host's log: what, which agent, and who asked.
    let heard = || host.heard.lock().unwrap().clone();
    let owners = format!("clarp-host: agent archived: {ben} ({ben_id}) by owner at ");
    wait_for("the lines", || heard().contains(&owners));
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
