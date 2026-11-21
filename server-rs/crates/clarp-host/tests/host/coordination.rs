//! The Host's tests of coordination between agents (the Python Host's PR 482, its contract 108):
//! the turn another agent's message began is for the agents on every row, on the live turn and
//! on the working state; the user's own word never is; a turn's token is the sender of what it
//! sends; no caller takes another message's trace; and the note an agent leaves for its user.
//! One file a piece, declared in `host.rs`.

use std::collections::BTreeMap;

use super::*;

/// What the agents of a test are: their ids by session.
fn ids_of(host: &Host) -> BTreeMap<String, String> {
    let snapshot = host.get("/agents/snapshot");
    let agents = snapshot["agents"].as_array().unwrap();
    agents
        .iter()
        .map(|agent| {
            (
                agent["session"].as_str().unwrap().to_string(),
                agent["agent_id"].as_str().unwrap().to_string(),
            )
        })
        .collect()
}

#[test]
fn the_users_word_precedes_ordinary_work_across_a_hold_and_restart() {
    let place = Place::new("user-word-first");
    let (tokens, release, holds) = stand_in(&place);
    let owners = place.root.join("owner.toml");
    std::fs::write(
        &owners,
        "[agents]\nfront_desk_sessions = [' desk ', '', 'desk']\n",
    )
    .unwrap();
    let start = || start_host(&place, &["--speech-keys", owners.to_str().unwrap()]);
    let host = start();
    let target = host.new_agent("Target");
    assert_eq!(
        host.post(
            "/agents",
            json!({"name":"Desk","session":"desk","backend":"claude"})
        )["session"],
        "desk"
    );
    let heard = host.listen();
    wait_for("the event subscriber", || {
        heard
            .lock()
            .unwrap()
            .iter()
            .any(|event| event["type"] == "agent-roster")
    });
    std::fs::write(holds.join("desk"), "").unwrap();
    host.send("desk", "relay later", "desk-owner");
    let mut token = String::new();
    wait_for("the desk's verified turn", || {
        token = std::fs::read_to_string(tokens.join("desk")).unwrap_or_default();
        !token.is_empty()
    });
    host.send(&target, "sleep 60", "running");
    wait_for("the receiving turn", || host.state(&target) == "thinking");

    let send = |id: &str, claimed: &str, headers: &[(&str, &str)]| {
        let (status, reply) = asked(
            &host,
            "/send",
            headers,
            &json!({
                "session": target, "client_msg_id": id, "text": id, "origin": claimed,
                "queue_if_busy": true, "user_word": true,
                "sender": if claimed == "agent" { json!("desk") } else { Value::Null },
            }),
        );
        assert_eq!(status, 200, "{reply}");
        assert_eq!(reply["queued"], true, "{reply}");
    };
    let queue = |host: &Host| host.get(&format!("/turn-queue?session={target}"));
    let order = |host: &Host| -> Vec<String> {
        queue(host)["items"]
            .as_array()
            .unwrap()
            .iter()
            .map(|item| item["id"].as_str().unwrap().to_owned())
            .collect()
    };
    send("forged", "agent", &[]);
    send("scheduled", "schedule", &[]);
    send("routine", "automation", &[]);
    assert_eq!(order(&host), ["forged", "scheduled", "routine"]);
    let before = queue(&host)["revision"].as_u64().unwrap();
    let plain = host.send(&target, "owner's word", "plain");
    assert_eq!(plain["queued"], true, "{plain}");
    assert_eq!(plain["queue_revision"], before + 1);
    send("relay", "user", &[("X-Clarp-Turn", token.as_str())]);

    let (status, made) = host.ask(
        "POST",
        "/decisions",
        Some(&json!({"session":target,"title":"Approval","question":"Proceed?"})),
    );
    assert_eq!(status, 201, "{made}");
    let id = made["artifact"]["decision"]["decision_id"]
        .as_str()
        .unwrap();
    let path = format!("/decisions/{id}/resolve");
    let (status, resolved) = host.ask(
        "POST",
        &path,
        Some(&json!({"choice":"accepted","expected_revision":1})),
    );
    assert_eq!(status, 200, "{resolved}");
    let card = format!("decision-{id}");
    wait_for("the user's card answer to reach the queue", || {
        order(&host).contains(&card)
    });
    send("oracle", "oracle", &[]);
    let want = [
        "plain",
        "relay",
        card.as_str(),
        "oracle",
        "forged",
        "scheduled",
        "routine",
    ];
    assert_eq!(order(&host), want);
    let revision = queue(&host)["revision"].clone();
    wait_for("the last enqueue's published revision", || {
        heard.lock().unwrap().iter().any(|event| {
            event["type"] == "queue-updated"
                && event["client_msg_id"] == "oracle"
                && event["queue_revision"] == revision
                && event["queue_depth"] == want.len()
        })
    });
    let snapshot = host.get("/agents/snapshot");
    let row = snapshot["agents"]
        .as_array()
        .unwrap()
        .iter()
        .find(|row| row["session"] == target)
        .unwrap();
    assert_eq!(row["queued_turn_count"], want.len());
    assert_eq!(row["queued_turn_revision"], revision);

    host.post("/stop", json!({"session":target}));
    wait_for("the stopped receiving turn", || {
        host.state(&target) == "interrupted"
    });
    assert_eq!(queue(&host)["paused"], true);
    assert_eq!(order(&host), want);
    host.kill();
    let host = start();
    assert_eq!(queue(&host)["paused"], true);
    assert_eq!(order(&host), want);
    host.post("/turn-queue/resume", json!({"session":target}));
    wait_for("the reordered queue to drain", || {
        order(&host).is_empty() && host.state(&target) == "done"
    });
    let rows = host.turns(&target);
    let users: Vec<&str> = rows
        .iter()
        .filter(|row| row["role"] == "user")
        .map(|row| row["id"].as_str().unwrap())
        .collect();
    let expected: Vec<_> = std::iter::once("running")
        .chain(want)
        .map(|id| format!("u-{id}"))
        .collect();
    assert_eq!(users, expected);
    let relay = rows.iter().find(|row| row["id"] == "u-relay").unwrap();
    assert_eq!(relay["origin"], "agent");
    assert_eq!(relay["sender_session"], "desk");
    assert_eq!(relay["audience"], "user");
    let card_row = rows
        .iter()
        .find(|row| row["id"] == format!("u-{card}"))
        .unwrap();
    assert_eq!(card_row["origin"], "automation");
    std::fs::write(release.join("desk"), "").unwrap();
    wait_for("the desk stand-in to end", || host.state("desk") == "done");
}

/// A request with these headers besides the JSON body: its status and answer.
fn asked(host: &Host, path: &str, headers: &[(&str, &str)], body: &Value) -> (u16, Value) {
    let mut all = vec![("Content-Type", "application/json")];
    all.extend_from_slice(headers);
    let got = fetch(
        host,
        &host.token,
        "POST",
        path,
        &all,
        body.to_string().as_bytes(),
    );
    let answer = serde_json::from_slice(&got.body).unwrap_or(Value::Null);
    (got.status, answer)
}

/// The rows of an agent's conversation, by their ids.
fn rows_of(host: &Host, session: &str) -> BTreeMap<String, Value> {
    let page = host.get(&format!("/log?session={session}&limit=500"));
    let rows = page["turns"].as_array().unwrap();
    rows.iter()
        .map(|row| (row["id"].as_str().unwrap().to_string(), row.clone()))
        .collect()
}

/// The rows of one trace of an agent's conversation, in order.
fn rows_of_trace(host: &Host, session: &str, trace: &str) -> Vec<Value> {
    let page = host.get(&format!("/log?session={session}&limit=500"));
    let rows = page["turns"].as_array().unwrap();
    rows.iter()
        .filter(|row| row["trace_id"] == trace)
        .cloned()
        .collect()
}

/// Whether the turn of this trace has answered and the agent is done: its reply is a row.
fn answered(host: &Host, session: &str, trace: &Value) -> bool {
    let trace = trace.as_str().unwrap_or_default();
    let rows = rows_of_trace(host, session, trace);
    let replied = rows.iter().any(|row| {
        row["role"] == "assistant" && row["kind"] != "note" && row.get("turn").is_some()
    });
    replied && host.state(session) == "done"
}

fn dispatched(heard: &Mutex<Vec<Value>>, session: &str, client_msg_id: &str) -> Value {
    let mut dispatched = None;
    wait_for("the send's dispatch on the event stream", || {
        dispatched = heard
            .lock()
            .unwrap()
            .iter()
            .find(|event| {
                event["type"] == "agent-state"
                    && event["kind"] == "thinking"
                    && event["session"] == session
                    && event["client_msg_id"] == client_msg_id
            })
            .cloned();
        dispatched.is_some()
    });
    dispatched.unwrap()
}

/// A stand-in for Claude Code that writes down the token of the turn it runs in,
/// under its agent's session, and runs on, where the test marked its session to hold, until
/// the test lets it go. It answers "on it".
fn stand_in(place: &Place) -> (std::path::PathBuf, std::path::PathBuf, std::path::PathBuf) {
    let (tokens, release, holds) = (
        place.root.join("tokens"),
        place.root.join("release"),
        place.root.join("holds"),
    );
    for dir in [&tokens, &release, &holds] {
        std::fs::create_dir_all(dir).unwrap();
    }
    // Claude's assistant message IDs are unique across turns. Expand the stand-in's PID
    // inside its otherwise single-quoted JSON so a later reply has its own log row.
    let answers = STAND_IN_ANSWERS.replace("msg_1", "msg_'\"$$\"'");
    let script = [
        "#!/bin/sh\nread -r message\n",
        &format!(
            "printf '%s' \"$message\" > \"{}/$CLARP_SESSION.prompt\"\n",
            tokens.display()
        ),
        &format!(
            "printf '%s' \"$CLARP_PROVIDER_TURN\" > \"{}/$CLARP_SESSION\"\n",
            tokens.display()
        ),
        &format!(
            "if [ -e \"{1}/$CLARP_SESSION\" ]; then n=0; while [ ! -e \"{0}/$CLARP_SESSION\" ] \
             && [ -d \"{0}\" ] && [ $n -lt 600 ]; do sleep 0.2; n=$((n+1)); done; \
             rm -f \"{0}/$CLARP_SESSION\" \"{1}/$CLARP_SESSION\"; fi\n",
            release.display(),
            holds.display()
        ),
        &answers,
    ]
    .concat();
    let claude = place.root.join("claude");
    program(&claude, &script);
    program(
        &place.root.join("codex"),
        r#"#!/bin/sh
test "$1" = exec || exit 0
cat >/dev/null
printf '%s\n' '{"type":"thread.started","thread_id":"00000000-0000-7000-8000-00000000c0de"}'
printf '%s\n' '{"type":"turn.started"}'
printf '%s\n' '{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"on it"}}'
printf '%s\n' '{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}'
"#,
    );
    (tokens, release, holds)
}

fn start_host(place: &Place, args: &[&str]) -> Host {
    start_host_as(place, args, "")
}

fn start_host_as(place: &Place, args: &[&str], token: &str) -> Host {
    let home = place.root.join("home");
    let config = home.join(".config");
    let data = home.join(".local/share");
    let cache = home.join(".cache");
    let state = home.join(".local/state");
    let claude = place.root.join("claude");
    let codex = place.root.join("codex");
    place.start_without(
        token,
        args,
        &[
            ("HOME", home.to_str().unwrap()),
            ("XDG_CONFIG_HOME", config.to_str().unwrap()),
            ("XDG_DATA_HOME", data.to_str().unwrap()),
            ("XDG_CACHE_HOME", cache.to_str().unwrap()),
            ("XDG_STATE_HOME", state.to_str().unwrap()),
            ("CLARP_CLAUDE_PROGRAM", claude.to_str().unwrap()),
            ("CLARP_CODEX_PROGRAM", codex.to_str().unwrap()),
        ],
        &[
            "CLAUDE_CONFIG_DIR",
            "CODEX_HOME",
            "CLAUDE_PWA_CONFIG",
            "CLARP_CONFIG_DIR",
            "CLARP_PUSH_CREDENTIAL",
        ],
    )
}

/// Forward two calls to the real Host, dropping the response to the first after it wrote the note.
fn lose_the_first_tool_answer(host: &Host) -> (String, std::thread::JoinHandle<Value>) {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    listener.set_nonblocking(true).unwrap();
    let address = listener.local_addr().unwrap().to_string();
    let (upstream, token) = (host.address.clone(), host.token.clone());
    let worker = std::thread::spawn(move || {
        let mut first_note = Value::Null;
        for first in [true, false] {
            let mut connection = None;
            wait_for("the tool to connect", || {
                connection = listener.accept().ok().map(|(socket, _)| socket);
                connection.is_some()
            });
            let mut connection = connection.unwrap();
            connection.set_nonblocking(false).unwrap();
            connection
                .set_read_timeout(Some(Duration::from_secs(5)))
                .unwrap();
            let request = whole_request(&mut connection);
            let (head, body) = request.split_once("\r\n\r\n").unwrap();
            assert!(head.starts_with("POST /tools/run "));
            let body = serde_json::from_str(body).unwrap();
            let (status, reply) =
                clarp_host::admin::post(&upstream, "/tools/run", &token, &body).unwrap();
            assert_eq!((status, &reply["exit"]), (200, &json!(0)), "{reply}");
            if first {
                first_note = serde_json::from_str(reply["stdout"].as_str().unwrap()).unwrap();
                assert_eq!(first_note["created"], true);
                assert_eq!(first_note["notified"], true);
                continue;
            }
            let reply = reply.to_string();
            write!(
                connection,
                "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{reply}",
                reply.len()
            )
            .unwrap();
        }
        first_note
    });
    (address, worker)
}

/// A send that another agent's turn makes (by its token), that names a sender, or that comes
/// from a peer Host: the turn it begins is for the agents, on every row of it, on the live
/// turn and on the working state, with the agent that sent it where this Host has it. The
/// user's own word is never coordination, whatever sender it names; a turn's token is the
/// sender of what it sends, and never speaks the user's word; a turn of one agent may not name
/// another as its sender; an agent waking itself is automation. A trace another message has is
/// refused, but a retry of the same message is that message. An agent on Codex hides here
/// (every turn of this Host carries its token).
///
/// Then the note: from the agent's own running turn only (no token, another agent's token, a
/// turn that ended: refused in the Python Host's words), every refusal of the body in its
/// order, the note asked again (the same note, nobody told again) and with other words
/// (refused); the user is told of it at once, whatever turn wrote it, and a muted agent's note
/// pages nobody. A note is always the user's row, and the turn's summary counts its notes.
/// `clarp-admin tell-user`, an agent's own tool on this Host, leaves one the same way.
#[test]
fn a_turn_another_agent_began_is_for_the_agents_and_an_agent_leaves_its_user_a_note_from_it() {
    let place = Place::new("coordination");
    let (tokens, release, holds) = stand_in(&place);
    let relay = StandInRelay::start();
    let host = start_host(
        &place,
        &["--push-relay", &format!("http://{}", relay.address)],
    );
    for (name, session, backend) in [
        ("Ivy", "ivy", "claude"),
        ("Oak", "oak", "claude"),
        ("Cody", "cody", "codex"),
        ("Hush", "hush", "claude"),
    ] {
        let made = host.post(
            "/agents",
            json!({ "name": name, "session": session, "backend": backend }),
        );
        assert_eq!(made["session"], session, "{made}");
    }
    let (phone, _) = pair(&host, "Note phone", "full");
    let grant = format!(
        "pg1.{}.{}.{}",
        "1".repeat(64),
        "2".repeat(16),
        "3".repeat(64)
    );
    let device = json!({ "token": "a1".repeat(32), "session": "oak", "push_grant": grant });
    assert_eq!(
        host.ask_as(&phone, "POST", "/devices", Some(&device)),
        (200, json!({ "ok": true }))
    );
    let ids = ids_of(&host);
    let heard = host.listen();
    let token_of =
        |session: &str| std::fs::read_to_string(tokens.join(session)).unwrap_or_default();
    let hold = |session: &str| std::fs::write(holds.join(session), "").unwrap();
    let let_go = |session: &str| {
        assert_ne!(host.state(session), "done", "{session}'s turn was held");
        std::fs::write(release.join(session), "").unwrap();
        wait_for("the turn to end", || host.state(session) == "done");
    };
    // Waits for a turn of the agent to begin and gives its token.
    let begun = |session: &str, before: &str| -> String {
        wait_for("the turn's token", || {
            let now = token_of(session);
            !now.is_empty() && now != before
        });
        token_of(session)
    };
    let send = |headers: &[(&str, &str)], body: Value| asked(&host, "/send", headers, &body);
    let note = |headers: &[(&str, &str)], body: Value| asked(&host, "/agent-notes", headers, &body);
    let told_of = |session: &str| -> Vec<Value> {
        let heard = heard.lock().unwrap();
        heard
            .iter()
            .filter(|event| event["type"] == "user-notification" && event["session"] == session)
            .cloned()
            .collect()
    };

    // Ivy works: its turn runs, and its token is what it presents.
    hold("ivy");
    host.send("ivy", "plan the work", "i-1");
    let ivy = begun("ivy", "");
    let ivy_turn = [("X-Clarp-Turn", ivy.as_str())];

    // From its turn Ivy asks Oak, naming no sender and no origin: Oak's turn is Ivy's, for the
    // agents. Its prompt row says who sent it.
    hold("oak");
    let (status, sent) = send(
        &ivy_turn,
        json!({ "session": "oak", "text": "check the build", "client_msg_id": "c-1" }),
    );
    assert_eq!(status, 200, "{sent}");
    let oak = begun("oak", "");
    let oak_turn = [("X-Clarp-Turn", oak.as_str())];
    let trace = sent["trace_id"].as_str().unwrap().to_string();
    let rows = rows_of(&host, "oak");
    let asked_row = &rows["u-c-1"];
    assert_eq!(
        (
            &asked_row["audience"],
            &asked_row["origin"],
            &asked_row["sender_agent_id"],
            &asked_row["sender_name"],
        ),
        (
            &json!("agents"),
            &json!("agent"),
            &json!(ids["ivy"]),
            &json!("Ivy")
        ),
        "{asked_row}"
    );
    // The live turn says whom it is for, and the working state names the agent it works with.
    let live = host.get("/live?session=oak");
    assert_eq!(live["turn"]["audience"], "agents", "{live}");
    let working = dispatched(&heard, "oak", "c-1");
    assert_eq!(
        (
            &working["audience"],
            &working["sender_agent_id"],
            &working["sender_name"]
        ),
        (&json!("agents"), &json!(ids["ivy"]), &json!("Ivy")),
        "{working}"
    );
    // Ivy's own turn, the user's, is for the user everywhere.
    assert_eq!(rows_of(&host, "ivy")["u-i-1"]["audience"], "user");
    assert_eq!(host.get("/live?session=ivy")["turn"]["audience"], "user");
    let working = dispatched(&heard, "ivy", "i-1");
    assert_eq!(working["audience"], "user", "{working}");
    assert!(working.get("sender_agent_id").is_none(), "{working}");

    // THE NOTE. Oak's turn runs. A request that presents no turn is refused for every caller,
    // and so is another agent's turn, in the Python Host's words.
    let green = json!({ "session": "oak", "text": "The build is green.", "note_id": "Green" });
    assert_eq!(
        note(&[], green.clone()),
        (
            403,
            json!({ "error": "clarp-admin tell-user works only from a turn of oak: this call \
                              carries no turn of an agent" })
        )
    );
    assert_eq!(
        note(&ivy_turn, green.clone()),
        (
            403,
            json!({ "error": "this turn belongs to ivy, not to oak; run clarp-admin tell-user \
                              --from ivy" })
        )
    );
    // Nor does a peer Host that names itself: without a turn's token no caller leaves a note
    // (the release owner's docs line of PR 492).
    assert_eq!(
        note(&[("X-Clarp-Peer", "beacon")], green.clone()).1,
        note(&[], green.clone()).1
    );
    assert_eq!(note(&[("X-Clarp-Peer", "beacon")], green.clone()).0, 403);
    // A text that is no token is no turn.
    assert_eq!(
        note(&[("X-Clarp-Turn", "not-a-token")], green.clone()).0,
        403
    );
    // The body, in the Python Host's order, before whose turn it is.
    for (body, status, words) in [
        (json!([1]), 400, "bad json".to_string()),
        (json!({ "text": "x" }), 400, "session required".to_string()),
        (
            json!({ "session": "oak", "text": "  " }),
            400,
            "a note needs words: clarp-admin tell-user --from oak --text '...'".to_string(),
        ),
        (
            json!({ "session": "oak", "text": "x".repeat(8001) }),
            413,
            "a note is at most 8000 characters".to_string(),
        ),
        (
            json!({ "session": "oak", "text": "x", "note_id": "not an id" }),
            400,
            "note_id: up to 64 of a-z, 0-9, '-' and '_'".to_string(),
        ),
        (
            json!({ "session": "nobody", "text": "x" }),
            404,
            "no such agent".to_string(),
        ),
    ] {
        assert_eq!(
            note(&ivy_turn, body.clone()),
            (status, json!({ "error": words })),
            "{body}"
        );
    }
    // A raw body that is no JSON is refused the same way.
    let got = fetch(
        &host,
        &host.token,
        "POST",
        "/agent-notes",
        &[("Content-Type", "application/json"), ("X-Clarp-Turn", &oak)],
        b"{not json",
    );
    assert_eq!(got.status, 400);
    // A note at the most it may be is taken.
    let longest = "y".repeat(8000);
    let (status, _) = note(
        &oak_turn,
        json!({ "session": "oak", "text": longest, "note_id": "longest" }),
    );
    assert_eq!(status, 200);
    // From Oak's own running turn: written into the turn's trace, and the user is told at once
    // (lower-cased id), although the turn is for the agents.
    let written_after = now_ms();
    let (status, written) = note(&oak_turn, green.clone());
    let green_id = format!("note-{}-green", ids["oak"]);
    assert_eq!(
        (status, written.clone()),
        (
            200,
            json!({ "ok": true, "session": "oak", "message_id": green_id, "trace_id": trace,
                    "created": true, "notified": true, "reason": "agent-note" })
        )
    );
    let notification = || {
        told_of("oak")
            .into_iter()
            .find(|event| event["source_message_id"] == green_id)
    };
    wait_for("the user to be told of the note", || {
        notification().is_some()
    });
    let told = notification().unwrap();
    wait_for("the note to reach the push relay", || {
        relay.taken().iter().any(|(path, _, body)| {
            path == "/v1/push/send"
                && body["notifications"][0]["payload"]["notification_id"] == told["notification_id"]
        })
    });
    assert!(
        (written_after..=now_ms()).contains(&told["done_ts"].as_i64().unwrap()),
        "{told}"
    );
    assert_eq!(
        (
            &told["notification_id"],
            &told["source_message_id"],
            &told["cause_message_id"],
            &told["origin"],
            &told["reason"],
            &told["push"],
            &told["muted"],
            &told["preview"],
            &told["badge"],
            &told["unread"],
        ),
        (
            &json!(clarp_kernel::coordination::note_notification_id(&green_id)),
            &json!(green_id),
            &json!(""),
            &json!("note"),
            &json!("agent-note"),
            &json!(true),
            &json!(false),
            &json!("The build is green."),
            &json!(true),
            &json!(true),
        ),
        "{told}"
    );
    // Asked again with the same words: the same note, and nobody is told again.
    let told_now = told_of("oak").len();
    let (status, again) = note(&oak_turn, green.clone());
    assert_eq!(
        (status, again),
        (
            200,
            json!({ "ok": true, "session": "oak", "message_id": green_id, "trace_id": trace,
                    "created": false, "notified": false, "reason": "duplicate-completion" })
        )
    );
    // With other words under the same id: refused.
    assert_eq!(
        note(
            &oak_turn,
            json!({ "session": "oak", "text": "Red.", "note_id": "green" })
        ),
        (
            409,
            json!({ "error": "note_id green is already a note of oak with other words; send \
                              the new words with a new note_id" })
        )
    );
    // The command itself retries after losing the response to a note the Host already wrote.
    let (address, first) = lose_the_first_tool_answer(&host);
    let told = place
        .host_command()
        .args([
            "tool",
            "clarp-admin",
            "tell-user",
            "--from",
            "oak",
            "--text",
            "Blue.",
        ])
        .env("HOME", &place.root)
        .env("CLARP_HOST_ADDR", address)
        .env("CLARP_PROVIDER_TURN", &oak)
        .ended();
    assert!(
        told.status.success(),
        "{}",
        String::from_utf8_lossy(&told.stderr)
    );
    let by_tool: Value = serde_json::from_slice(&told.stdout).unwrap();
    let first = first.join().unwrap();
    assert_eq!(by_tool["message_id"], first["message_id"]);
    assert_eq!(
        (
            &by_tool["created"],
            &by_tool["notified"],
            &by_tool["trace_id"]
        ),
        (&json!(false), &json!(false), &json!(trace))
    );
    assert!(
        by_tool["message_id"]
            .as_str()
            .unwrap()
            .starts_with(&format!("note-{}-tell-", ids["oak"]))
    );
    let refused = host.post(
        "/tools/run",
        json!({ "tool": "clarp-admin", "argv": ["tell-user", "--from", "oak", "--text", "Blue."],
                "env": {} }),
    );
    assert_eq!(refused["exit"], 4, "{refused}");
    assert!(
        refused["stderr"]
            .as_str()
            .unwrap()
            .starts_with("clarp-admin tell-user: the Host refused it (HTTP 403): "),
        "{refused}"
    );
    wait_for("the user to be told of the tool's note", || {
        told_of("oak").len() == told_now + 1
    });
    // Oak ends its turn. Its rows are for the agents, but its notes, each a row of the turn;
    // the turn's summary counts them (three).
    let_go("oak");
    let turn_rows = rows_of_trace(&host, "oak", &trace);
    let notes: Vec<&Value> = turn_rows
        .iter()
        .filter(|row| row["kind"] == "note")
        .collect();
    assert_eq!(notes.len(), 3, "{turn_rows:?}");
    for row in &notes {
        assert_eq!(
            (&row["audience"], &row["origin"], &row["role"]),
            (&json!("user"), &json!("note"), &json!("assistant")),
            "{row}"
        );
    }
    let green_row = notes.iter().find(|row| row["id"] == green_id).unwrap();
    assert_eq!(green_row["text"], "The build is green.");
    let reply = turn_rows
        .iter()
        .find(|row| row["role"] == "assistant" && row["kind"] != "note")
        .unwrap();
    assert_eq!(
        (&reply["audience"], &reply["turn"]["note_count"]),
        (&json!("agents"), &json!(3)),
        "{reply}"
    );
    // A turn that ended leaves no note, nor does an older turn's token.
    assert_eq!(
        note(
            &oak_turn,
            json!({ "session": "oak", "text": "Late.", "note_id": "late" })
        ),
        (
            409,
            json!({ "error": "clarp-admin tell-user works only inside a running turn of oak; \
                              this call is not in one" })
        )
    );

    // THE SEND. The user's own word is never coordination, whatever sender it names.
    // An empty peer header still leaves it the user's word.
    let (status, sent) = send(
        &[("X-Clarp-Peer", "")],
        json!({ "session": "oak", "text": "from me", "client_msg_id": "c-2", "sender": "ivy",
                "origin": "user" }),
    );
    assert_eq!(status, 200, "{sent}");
    wait_for("Oak's second turn", || {
        answered(&host, "oak", &sent["trace_id"])
    });
    for row in rows_of_trace(&host, "oak", sent["trace_id"].as_str().unwrap()) {
        assert_eq!(row["audience"], "user", "{row}");
    }
    let mine = dispatched(&heard, "oak", "c-2");
    assert_eq!(mine["audience"], "user", "{mine}");
    // A sender the Host does not know cannot mark another kind of message as coordination.
    let (status, scheduled) = send(
        &[],
        json!({ "session": "oak", "text": "scheduled", "client_msg_id": "unknown-sender",
                "sender": "absent-agent", "origin": "schedule" }),
    );
    assert_eq!(status, 200, "{scheduled}");
    wait_for("the scheduled turn", || {
        answered(&host, "oak", &scheduled["trace_id"])
    });
    let scheduled_row = &rows_of(&host, "oak")["u-unknown-sender"];
    assert_eq!(scheduled_row["audience"], "user", "{scheduled_row}");
    // A sender named by whoever may name one, with an agent's origin: another agent's message.
    let (_, named) = send(
        &[],
        json!({ "session": "oak", "text": "named", "client_msg_id": "c-3", "sender": "ivy",
                "origin": "agent" }),
    );
    wait_for("Oak's third turn", || {
        answered(&host, "oak", &named["trace_id"])
    });
    let named_rows = rows_of_trace(&host, "oak", named["trace_id"].as_str().unwrap());
    assert!(named_rows.len() >= 2, "{named_rows:?}");
    for row in &named_rows {
        assert_eq!(row["audience"], "agents", "{row}");
    }
    // A turn's token never speaks the user's word: what it sends claiming it is an agent's,
    // that agent's (the sender it did not name).
    let (_, claimed) = send(
        &ivy_turn,
        json!({ "session": "oak", "text": "as if the user", "client_msg_id": "c-4",
                "origin": "user" }),
    );
    wait_for("Oak's fourth turn", || {
        answered(&host, "oak", &claimed["trace_id"])
    });
    let claimed_row = &rows_of(&host, "oak")["u-c-4"];
    assert_eq!(
        (
            &claimed_row["origin"],
            &claimed_row["sender_agent_id"],
            &claimed_row["audience"]
        ),
        (&json!("agent"), &json!(ids["ivy"]), &json!("agents")),
        "{claimed} {claimed_row}"
    );
    // A turn of Ivy's may not name Oak as its sender: a fork speaking as its source.
    assert_eq!(
        send(
            &ivy_turn,
            json!({ "session": "cody", "text": "x", "sender": "oak" })
        ),
        (
            403,
            json!({ "error": "this turn belongs to ivy, not to the sender it named; send as \
                              --from ivy" })
        )
    );
    // A peer Host forwards only its agents' messages: its own claim of the user's word is an
    // agent's, of an agent this Host does not know, and its working state names nobody.
    let (status, from_peer) = send(
        &[("X-Clarp-Peer", "beacon")],
        json!({ "session": "oak", "text": "from afar", "client_msg_id": "c-5", "origin": "user" }),
    );
    assert_eq!(status, 200, "{from_peer}");
    wait_for("Oak's fifth turn", || {
        answered(&host, "oak", &from_peer["trace_id"])
    });
    let peer_row = &rows_of(&host, "oak")["u-c-5"];
    assert_eq!(
        (&peer_row["origin"], &peer_row["audience"]),
        (&json!("agent"), &json!("agents")),
        "{peer_row}"
    );
    let afar = dispatched(&heard, "oak", "c-5");
    assert_eq!(afar["audience"], "agents", "{afar}");
    assert!(afar.get("sender_agent_id").is_none(), "{afar}");
    // An agent on Codex is hidden here too: its turns carry this Host's token.
    let (status, to_cody) = send(
        &ivy_turn,
        json!({ "session": "cody", "text": "review", "client_msg_id": "c-6" }),
    );
    assert_eq!(status, 200, "{to_cody}");
    wait_for("Cody's turn", || {
        answered(&host, "cody", &to_cody["trace_id"])
    });
    for row in rows_of_trace(&host, "cody", to_cody["trace_id"].as_str().unwrap()) {
        assert_eq!(row["audience"], "agents", "{row}");
    }
    // An agent waking itself from its own turn with the user's word: automation, the user's.
    let (_, woken) = send(
        &ivy_turn,
        json!({ "session": "ivy", "text": "remind me", "client_msg_id": "i-2", "origin": "user" }),
    );
    std::fs::write(release.join("ivy"), "").unwrap();
    wait_for("Ivy's second turn", || {
        answered(&host, "ivy", &woken["trace_id"])
    });
    let self_row = &rows_of(&host, "ivy")["u-i-2"];
    assert_eq!(
        (&self_row["origin"], &self_row["audience"]),
        (&json!("automation"), &json!("user")),
        "{woken} {self_row}"
    );

    // THE TRACE. A trace the caller names belongs to the first message that has it: another
    // message naming it is refused; the same message sent again is that message.
    let first = json!({ "session": "hush", "text": "one", "client_msg_id": "h-1",
                        "trace_id": "shared-trace" });
    let (status, one) = send(&[], first.clone());
    assert_eq!(
        (status, &one["trace_id"]),
        (200, &json!("shared-trace")),
        "{one}"
    );
    wait_for("Hush's turn", || answered(&host, "hush", &one["trace_id"]));
    let other = json!({ "session": "hush", "text": "two", "client_msg_id": "h-2",
                        "trace_id": "shared-trace" });
    assert_eq!(
        send(&[], other),
        (
            409,
            json!({ "error": "trace_id already belongs to another message; send without \
                              trace_id (the Host names one) or with a new one" })
        )
    );
    let (status, again) = send(&[], first);
    assert_eq!((status, &again["trace_id"]), (200, &json!("shared-trace")));
    // The same id from another sender is another message.
    let posing = json!({ "session": "hush", "text": "one", "client_msg_id": "h-1",
                         "trace_id": "shared-trace", "sender": "ivy", "origin": "agent" });
    assert_eq!(send(&[], posing).0, 409);
    let hush_rows = rows_of(&host, "hush");
    assert!(!hush_rows.contains_key("u-h-2"), "{hush_rows:?}");
    // Two agents' messages with one id, one trace and one origin, from two senders: the second
    // is no retry of the first (the release owner's wall W5). And a page read after a revision
    // (a delta, the wall W3's way in) says whom each turn is for as the whole page does.
    let cursor = host.get("/log?session=hush")["latest_revision"]
        .as_u64()
        .unwrap();
    assert!(cursor > 0);
    let from_ivy = json!({ "session": "hush", "text": "from ivy", "client_msg_id": "h-4",
                           "trace_id": "agents-trace", "sender": "ivy", "origin": "agent" });
    let (status, by_ivy) = send(&[], from_ivy.clone());
    assert_eq!(status, 200, "{by_ivy}");
    wait_for("Hush's turn for Ivy", || {
        answered(&host, "hush", &json!("agents-trace"))
    });
    let from_oak = json!({ "session": "hush", "text": "from ivy", "client_msg_id": "h-4",
                           "trace_id": "agents-trace", "sender": "oak", "origin": "agent" });
    assert_eq!(send(&[], from_oak).0, 409);
    assert_eq!(send(&[], from_ivy).0, 200);
    let (_, mine) = send(
        &[],
        json!({ "session": "hush", "text": "and mine", "client_msg_id": "h-5" }),
    );
    wait_for("Hush's turn for the user", || {
        answered(&host, "hush", &mine["trace_id"])
    });
    let delta = host.get(&format!(
        "/log?session=hush&after_revision={cursor}&limit=500"
    ));
    let delta = delta["turns"].as_array().unwrap();
    assert!(
        delta.iter().all(|row| row["trace_id"] != "shared-trace"),
        "{delta:?}"
    );
    let of = |trace: &str| -> Vec<&Value> {
        delta
            .iter()
            .filter(|row| row["trace_id"] == trace)
            .collect()
    };
    assert!(of("agents-trace").len() >= 2, "{delta:?}");
    for row in of("agents-trace") {
        assert_eq!(row["audience"], "agents", "{row}");
    }
    let users = of(mine["trace_id"].as_str().unwrap());
    assert!(users.len() >= 2, "{delta:?}");
    for row in users {
        assert_eq!(row["audience"], "user", "{row}");
    }

    // A muted agent's note pages nobody, and is still told: Hush is muted, and works.
    let muted = host.post("/agent-mute", json!({ "session": "hush", "muted": true }));
    assert_eq!(muted["ok"], true, "{muted}");
    hold("hush");
    let before = token_of("hush");
    host.send("hush", "work quietly", "h-3");
    let hush = begun("hush", &before);
    let (status, quiet) = note(
        &[("X-Clarp-Turn", hush.as_str())],
        json!({ "session": "hush", "text": "Done, quietly.", "note_id": "quiet" }),
    );
    assert_eq!(
        (status, &quiet["notified"], &quiet["reason"]),
        (200, &json!(true), &json!("agent-note-muted")),
        "{quiet}"
    );
    let notification = || {
        told_of("hush")
            .into_iter()
            .find(|event| event["source_message_id"] == quiet["message_id"])
    };
    wait_for("the user to be told of the muted note", || {
        notification().is_some()
    });
    let told = notification().unwrap();
    assert_eq!(
        (&told["push"], &told["muted"], &told["reason"]),
        (&json!(false), &json!(true), &json!("agent-note-muted")),
        "{told}"
    );
    let_go("hush");
}

/// A Host that cannot read its key gives no turn a token: no agent can leave a note, so no
/// turn is hidden. Turns marked before key loss and turns accepted after it remain visible.
/// Their history, live turn and working state are the user's. A note is refused without a token
/// (403) and answered that it cannot be said whose turn it is with one (503, which may be
/// asked again).
#[test]
fn a_host_that_cannot_read_its_key_hides_nothing_and_takes_no_note() {
    let place = Place::new("coordination-no-key");
    let (tokens, release, holds) = stand_in(&place);
    let host = start_host(&place, &[]);
    for (name, session) in [("Ivy", "ivy"), ("Oak", "oak")] {
        let made = host.post(
            "/agents",
            json!({ "name": name, "session": session, "backend": "claude" }),
        );
        assert_eq!(made["session"], session, "{made}");
    }
    std::fs::write(holds.join("oak"), "").unwrap();
    let saved = host.post(
        "/send",
        json!({ "session": "oak", "text": "remember this", "client_msg_id": "saved",
                "sender": "ivy", "origin": "agent" }),
    );
    wait_for("the marked turn to get its token", || {
        std::fs::read_to_string(tokens.join("oak")).is_ok_and(|token| !token.is_empty())
    });
    assert_eq!(rows_of(&host, "oak")["u-saved"]["audience"], "agents");
    assert_eq!(host.get("/live?session=oak")["turn"]["audience"], "agents");
    let queued = host.post(
        "/send",
        json!({ "session": "oak", "text": "wait your turn", "client_msg_id": "saved-queued",
                "sender": "ivy", "origin": "agent" }),
    );
    assert_eq!(queued["queued"], true, "{queued}");
    drop(host);
    keep_record(
        &place,
        "turn-identity",
        "key",
        &json!({ "key": "not a key" }),
    );
    let host = start_host(&place, &[]);
    // The rejoined turn and past rows use the current capability before any send can set it.
    wait_for("the marked turn to rejoin", || {
        host.get("/live?session=oak")["turn"].is_object()
    });
    assert_eq!(rows_of(&host, "oak")["u-saved"]["audience"], "user");
    assert_eq!(host.get("/live?session=oak")["turn"]["audience"], "user");
    let heard = host.listen();
    wait_for("the event stream to subscribe", || {
        heard
            .lock()
            .unwrap()
            .iter()
            .any(|event| event["type"] == "agent-roster")
    });
    std::fs::write(release.join("oak"), "").unwrap();
    let working = dispatched(&heard, "oak", "saved-queued");
    assert_eq!(working["audience"], "user", "{working}");
    assert!(working.get("sender_agent_id").is_none(), "{working}");
    wait_for("the rejoined and previously queued turns to finish", || {
        answered(&host, "oak", &saved["trace_id"]) && answered(&host, "oak", &queued["trace_id"])
    });
    std::fs::remove_file(tokens.join("oak")).unwrap();
    std::fs::write(holds.join("oak"), "").unwrap();
    let (status, sent) = asked(
        &host,
        "/send",
        &[],
        &json!({ "session": "oak", "text": "check", "client_msg_id": "c-1", "sender": "ivy",
                 "origin": "agent" }),
    );
    assert_eq!(status, 200, "{sent}");
    // Its turn runs, and was given no token.
    wait_for("Oak's turn", || tokens.join("oak").exists());
    assert_eq!(std::fs::read_to_string(tokens.join("oak")).unwrap(), "");
    let row = &rows_of(&host, "oak")["u-c-1"];
    assert_eq!(
        (&row["audience"], &row["sender_agent_id"]),
        (&json!("user"), &json!(ids_of(&host)["ivy"])),
        "{row}"
    );
    assert_eq!(host.get("/live?session=oak")["turn"]["audience"], "user");
    let snapshot = host.get("/agents/snapshot");
    let oak = snapshot["agents"]
        .as_array()
        .unwrap()
        .iter()
        .find(|agent| agent["session"] == "oak")
        .unwrap();
    assert_eq!(oak["working_audience"], "user");
    assert!(oak["working_with"].is_null());
    let hidden = host.get("/log?session=oak&coordination=hide");
    assert_eq!(hidden["coordination"]["visibility"], "hide");
    for id in ["u-saved", "u-c-1"] {
        assert!(
            hidden["turns"]
                .as_array()
                .unwrap()
                .iter()
                .any(|row| row["id"] == id),
            "{hidden}"
        );
    }
    assert!(hidden.get("hidden_after").is_none());
    let working = dispatched(&heard, "oak", "c-1");
    assert_eq!(working["audience"], "user", "{working}");
    assert!(working.get("sender_agent_id").is_none(), "{working}");
    let body = json!({ "session": "oak", "text": "A note." });
    assert_eq!(asked(&host, "/agent-notes", &[], &body).0, 403);
    for body in [body, json!([])] {
        assert_eq!(
            asked(
                &host,
                "/agent-notes",
                &[("X-Clarp-Turn", "any-token")],
                &body
            ),
            (
                503,
                json!({ "error": "turn identity unavailable", "retryable": true })
            )
        );
    }
    std::fs::write(release.join("oak"), "").unwrap();
    wait_for("Oak's turn to end", || host.state("oak") == "done");
    let reply = rows_of_trace(&host, "oak", sent["trace_id"].as_str().unwrap());
    assert!(reply.len() >= 2, "{reply:?}");
    for row in reply {
        assert_eq!(row["audience"], "user", "{row}");
    }
}

#[test]
fn a_user_can_hide_coordination_globally_or_per_chat_without_rewriting_it() {
    let place = Place::new("hidden-coordination");
    let (tokens, release, holds) = stand_in(&place);
    let admin = "fixture-admin";
    let host = start_host_as(&place, &[], admin);
    for (name, session) in [("Ivy", "ivy"), ("Oak", "oak")] {
        assert_eq!(
            host.post(
                "/agents",
                json!({"name": name, "session": session, "backend": "claude"})
            )["session"],
            session
        );
    }
    let (limited, _) = pair(&host, "Gallery", "limited");
    let declared = json!({"coordination_visibility":"hide"});
    let per = json!({"session":"oak","visibility":"show"});
    for (path, payload) in [
        ("/coordination-settings", &declared),
        ("/agent-coordination", &per),
    ] {
        assert_eq!(host.ask_as(&limited, "POST", path, Some(payload)).0, 403);
        for header in ["X-Clarp-Turn", "X-Clarp-Peer"] {
            let (status, body) = asked(&host, path, &[(header, "fixture-agent")], payload);
            assert_eq!((status, body), (403, json!({"error":"agents_may_not"})));
        }
    }
    assert_eq!(
        host.get("/coordination-settings"),
        json!({"coordination_visibility":"hide"})
    );
    assert_eq!(
        host.get("/log?session=oak&coordination=auto")["coordination"],
        json!({"visibility":"hide","hidden":true,"source":"global"})
    );
    assert_eq!(
        host.ask_as(&limited, "GET", "/coordination-settings", None)
            .0,
        403
    );
    assert_eq!(
        host.ask(
            "POST",
            "/agent-coordination",
            Some(&json!({"session":"missing","visibility":"show"}))
        )
        .0,
        404
    );
    assert_eq!(
        host.ask(
            "POST",
            "/coordination-settings",
            Some(&json!({"coordination_visibility": true}))
        )
        .0,
        400
    );
    assert_eq!(
        host.ask("GET", "/log?session=oak&coordination=wrong", None)
            .0,
        400
    );
    let heard = host.listen_at(&format!("/events?token={admin}"));
    assert_eq!(
        host.post(
            "/coordination-settings",
            json!({"coordination_visibility":"show"})
        ),
        json!({"coordination_visibility":"show"})
    );
    host.send("oak", "visible before", "visible-first");
    wait_for("the first visible reply", || host.state("oak") == "done");
    let path = "/log?session=oak";
    let cached = host.get(&format!("{path}&coordination=auto"));
    let cursor = cached["latest_revision"].as_u64().unwrap();
    assert_eq!(
        cached["coordination"],
        json!({"visibility":"show","hidden":false,"source":"global"})
    );
    assert_eq!(
        host.post("/coordination-settings", declared),
        json!({"coordination_visibility":"hide"})
    );
    let reloaded = host.get(&format!(
        "{path}&after_revision={cursor}&coordination=auto&coordination_read=show"
    ));
    assert_eq!(reloaded["replace_required"], true);
    assert_eq!(reloaded["latest_revision"], cursor);
    assert_eq!(
        host.post("/agent-coordination", per)["coordination_override"],
        "show"
    );
    let own = host.get(&format!("{path}&coordination=auto"));
    assert_eq!(
        own["coordination"],
        json!({"visibility":"show","hidden":false,"source":"chat"})
    );
    let snapshot = host.get("/agents/snapshot");
    let oak = snapshot["agents"]
        .as_array()
        .unwrap()
        .iter()
        .find(|agent| agent["session"] == "oak")
        .unwrap();
    assert_eq!(oak["coordination_visibility"], "show");
    assert_eq!(oak["coordination_override"], "show");
    assert!(
        host.post(
            "/agent-coordination",
            json!({"session":"oak","visibility":"default"})
        )["coordination_override"]
            .is_null()
    );
    let rows = host.get("/agents/snapshot");
    assert!(
        rows["agents"]
            .as_array()
            .unwrap()
            .iter()
            .all(|row| row["coordination_visibility"] == "hide"
                && row["coordination_override"].is_null())
    );
    wait_for("the roster setting event", || {
        heard.lock().unwrap().iter().any(|event| {
            event["type"] == "agent-roster"
                && event["kind"] == "coordination-changed"
                && event["coordination_visibility"] == "hide"
        })
    });

    std::fs::write(holds.join("oak"), b"").unwrap();
    let before = std::fs::read_to_string(tokens.join("oak")).unwrap_or_default();
    let sent = host.post(
        "/send",
        json!({
            "session":"oak","text":"coordination prompt","client_msg_id":"only-agents",
            "sender":"ivy","origin":"agent"
        }),
    );
    wait_for("the marked turn to begin", || {
        std::fs::read_to_string(tokens.join("oak")).is_ok_and(|t| !t.is_empty() && t != before)
    });
    let prompt = std::fs::read_to_string(tokens.join("oak.prompt")).unwrap();
    assert!(
        prompt.contains("Coordination: this turn answers a message from another agent"),
        "{prompt}"
    );
    let token = std::fs::read_to_string(tokens.join("oak")).unwrap();
    let delta = host.get(&format!(
        "{path}&after_revision={cursor}&coordination=hide&coordination_read=hide"
    ));
    assert_eq!(delta["turns"], json!([]));
    assert!(delta["latest_revision"].as_u64().unwrap() > cursor);
    assert_eq!(delta["replace_required"], false);
    let (status, note) = asked(
        &host,
        "/agent-notes",
        &[("X-Clarp-Turn", &token)],
        &json!({"session":"oak","text":"Keep my note","note_id":"n1"}),
    );
    assert_eq!(status, 200, "{note}");
    std::fs::write(release.join("oak"), b"").unwrap();
    wait_for("marked turn to end", || {
        answered(&host, "oak", &sent["trace_id"])
    });
    let hidden = host.get(&format!("{path}&coordination=hide&limit=1"));
    assert_eq!(hidden["turns"].as_array().unwrap().len(), 1);
    assert_eq!(hidden["turns"][0]["id"], note["message_id"]);
    assert_eq!(hidden["turns"][0]["audience"], "user");
    assert_eq!(hidden["turns"][0]["hidden_before"]["messages"], 1);
    assert_eq!(hidden["turns"][0]["hidden_before"]["exchanges"], 1);
    assert_eq!(hidden["hidden_after"]["messages"], 1);
    let older = host.get(&format!(
        "{path}&coordination=hide&limit=1&before={}",
        note["message_id"].as_str().unwrap()
    ));
    assert_eq!(older["coordination"]["source"], "request");
    assert_eq!(older["turns"].as_array().unwrap().len(), 1);
    assert!(older.get("hidden_after").is_none());

    let show = host.get(&format!("{path}&coordination=show"));
    assert!(
        show["turns"]
            .as_array()
            .unwrap()
            .iter()
            .any(|row| row["audience"] == "agents")
    );
    let plain = host.get(path);
    assert_eq!(plain["turns"], show["turns"]);
    assert!(plain.get("coordination").is_none());
}

#[test]
fn a_hiding_chat_shows_who_its_helper_works_with_and_keeps_the_visible_preview() {
    let place = Place::new("coordination-working-state");
    let release = place.root.join("release-working");
    let tokens = place.root.join("working-token");
    let script = STAND_IN_STREAMS
        .replace(
            "read message\n",
            &format!(
                "read message\nprintf '%s' \"$message\" > '{0}/'\"$CLARP_SESSION.prompt\"\nprintf '%s' \"$CLARP_PROVIDER_TURN\" > '{0}/'\"$CLARP_SESSION\"\n",
                tokens.display()
            )
        )
        .replace(
            "sleep \"$pause\"",
            &format!(
                "if [ \"$tag\" = two ]; then n=0; while [ ! -e '{}' ] && [ $n -lt 100 ]; do sleep 0.1; n=$((n+1)); done; fi",
                release.display()
            ),
        )
        .replace("sleep 2; echo found", "cat secret-plan.md")
        .replace("sleep 2; ", "cat ")
        .replace("echo found", "secret-plan.md");
    std::fs::create_dir(&tokens).unwrap();
    program(&place.root.join("claude"), &script);
    let admin = "fixture-admin";
    let host = start_host_as(&place, &[], admin);
    host.post(
        "/agents",
        json!({"name":"Boss","session":"boss","backend":"claude"}),
    );
    host.post(
        "/agents",
        json!({"name":"Oak","session":"oak","backend":"claude","role":"helper","parent":"boss"}),
    );
    let row = |session| {
        host.get("/agents/snapshot")["agents"]
            .as_array()
            .unwrap()
            .iter()
            .find(|row| row["session"] == session)
            .cloned()
            .unwrap()
    };
    host.send("oak", "one visible turn", "first-visible");
    wait_for("the visible turn to finish", || host.state("oak") == "done");
    let before = row("oak");
    assert!(before["working_audience"].is_null());
    assert_eq!(before["coordination_visibility"], "hide");
    assert!(before["coordination_override"].is_null());
    let heard = host.listen_at(&format!("/events?token={admin}"));
    wait_for("the event stream to subscribe", || {
        heard
            .lock()
            .unwrap()
            .iter()
            .any(|event| event["type"] == "agent-roster")
    });
    let sent = host.post(
        "/send",
        json!({
            "session":"oak","text":"two while the tool is held","origin":"agent","sender":"boss",
            "client_msg_id":"private-working",
        }),
    );
    wait_for("the hidden turn to reach its tool", || {
        host.state("oak") == "tool"
    });
    let hidden = row("oak");
    assert_eq!(hidden["last_message_id"], before["last_message_id"]);
    let page = host.get("/log?session=oak&coordination=auto");
    assert_eq!(
        page["coordination"],
        json!({"visibility":"hide","hidden":true,"source":"global"})
    );
    assert!(
        page["hidden_after"]["messages"]
            .as_u64()
            .is_some_and(|count| count > 0)
    );
    assert_eq!(
        hidden["last_completed_message"],
        before["last_completed_message"]
    );
    assert_eq!(hidden["last_activity"], before["last_activity"]);
    assert_eq!(hidden["working_audience"], "agents");
    let prompt = std::fs::read_to_string(tokens.join("oak.prompt")).unwrap();
    assert!(prompt.contains("Coordination: this turn answers a message from another agent"));

    assert_eq!(
        hidden["working_with"],
        json!({"agent_id":ids_of(&host)["boss"],"name":"Boss"})
    );
    assert_eq!(hidden["activity"]["summary"], "Working with Boss");
    assert_eq!(hidden["live_activity"]["headline"], "Working with Boss");
    assert!(hidden["live_activity"]["tool"].is_null());
    assert!(
        row("boss")["status_text"]
            .as_str()
            .unwrap()
            .contains("oak: Working with Boss")
    );
    wait_for("the late working event", || {
        heard.lock().unwrap().iter().any(|event| {
            event["type"] == "agent-state"
                && event["session"] == "oak"
                && event["kind"] == "tool"
                && event["audience"] == "agents"
                && event["sender_name"] == "Boss"
                && event["detail"]["summary"] == "Working with Boss"
                && event["detail"].get("tool").is_none()
        })
    });
    assert!(heard.lock().unwrap().iter().any(|event| {
        event["type"] == "agent-activity"
            && event["session"] == "oak"
            && event["kind"] == "tool"
            && event["tool"] == ""
            && event["summary"] == "Working with Boss"
    }));

    let token = std::fs::read_to_string(tokens.join("oak")).unwrap();
    let (status, note) = asked(
        &host,
        "/agent-notes",
        &[("X-Clarp-Turn", &token)],
        &json!({"session":"oak","text":"An update for you","note_id":"working-note"}),
    );
    assert_eq!(status, 200, "{note}");
    assert_eq!(row("oak")["last_message"], "An update for you");
    std::fs::write(release, "").unwrap();
    wait_for("the hidden turn to finish", || {
        answered(&host, "oak", &sent["trace_id"])
    });
    let idle = row("oak");
    assert!(idle["working_audience"].is_null() && idle["working_with"].is_null());
    assert_eq!(idle["last_message"], "An update for you");
    host.post(
        "/agent-coordination",
        json!({"session":"oak","visibility":"show"}),
    );
    assert_ne!(row("oak")["last_message_id"], idle["last_message_id"]);
}
