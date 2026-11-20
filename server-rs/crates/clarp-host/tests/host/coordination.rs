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

/// A stand-in for Claude Code (and Codex) that writes down the token of the turn it runs in,
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
    let script = [
        "#!/bin/sh\nread -r message\n",
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
        STAND_IN_ANSWERS,
    ]
    .concat();
    let claude = place.root.join("claude");
    program(&claude, &script);
    (tokens, release, holds)
}

fn start_host(place: &Place) -> Host {
    let home = place.root.join("home");
    let config = home.join(".config");
    let data = home.join(".local/share");
    let cache = home.join(".cache");
    let state = home.join(".local/state");
    let claude = place.root.join("claude");
    place.start_without(
        "",
        &[],
        &[
            ("HOME", home.to_str().unwrap()),
            ("XDG_CONFIG_HOME", config.to_str().unwrap()),
            ("XDG_DATA_HOME", data.to_str().unwrap()),
            ("XDG_CACHE_HOME", cache.to_str().unwrap()),
            ("XDG_STATE_HOME", state.to_str().unwrap()),
            ("CLARP_CLAUDE_PROGRAM", claude.to_str().unwrap()),
            ("CLARP_CODEX_PROGRAM", claude.to_str().unwrap()),
        ],
        &[
            "CLAUDE_CONFIG_DIR",
            "CODEX_HOME",
            "CLAUDE_PWA_CONFIG",
            "CLARP_CONFIG_DIR",
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
    let host = start_host(&place);
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
    let told_before = told_of("oak").len();
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
    wait_for("the user to be told of the note", || {
        told_of("oak").len() > told_before
    });
    let told = told_of("oak").last().cloned().unwrap();
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
    let told = Command::new(env!("CARGO_BIN_EXE_clarp-host"))
        .args([
            "tool",
            "clarp-admin",
            "tell-user",
            "--from",
            "oak",
            "--text",
            "Blue.",
        ])
        .env_clear()
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
    let (status, sent) = send(
        &[],
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
    let (_, to_cody) = send(
        &ivy_turn,
        json!({ "session": "cody", "text": "review", "client_msg_id": "c-6" }),
    );
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
    let told_before = told_of("hush").len();
    let (status, quiet) = note(
        &[("X-Clarp-Turn", hush.as_str())],
        json!({ "session": "hush", "text": "Done, quietly.", "note_id": "quiet" }),
    );
    assert_eq!(
        (status, &quiet["notified"], &quiet["reason"]),
        (200, &json!(true), &json!("agent-note-muted")),
        "{quiet}"
    );
    wait_for("the user to be told of the muted note", || {
        told_of("hush").len() > told_before
    });
    let told = told_of("hush").last().cloned().unwrap();
    assert_eq!(
        (&told["push"], &told["muted"], &told["reason"]),
        (&json!(false), &json!(true), &json!("agent-note-muted")),
        "{told}"
    );
    let_go("hush");
}

/// A Host that cannot read its key gives no turn a token: no agent can leave a note, so no
/// turn is hidden. A turn another agent's message began is still marked, and every row of it,
/// its live turn and its working state are the user's. A note is refused without a token
/// (403) and answered that it cannot be said whose turn it is with one (503, which may be
/// asked again).
#[test]
fn a_host_that_cannot_read_its_key_hides_nothing_and_takes_no_note() {
    let place = Place::new("coordination-no-key");
    let (tokens, release, holds) = stand_in(&place);
    keep_record(
        &place,
        "turn-identity",
        "key",
        &json!({ "key": "not a key" }),
    );
    let host = start_host(&place);
    for (name, session) in [("Ivy", "ivy"), ("Oak", "oak")] {
        let made = host.post(
            "/agents",
            json!({ "name": name, "session": session, "backend": "claude" }),
        );
        assert_eq!(made["session"], session, "{made}");
    }
    let heard = host.listen();
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
    let working = dispatched(&heard, "oak", "c-1");
    assert_eq!(working["audience"], "user", "{working}");
    assert!(working.get("sender_agent_id").is_none(), "{working}");
    let body = json!({ "session": "oak", "text": "A note." });
    assert_eq!(asked(&host, "/agent-notes", &[], &body).0, 403);
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
    std::fs::write(release.join("oak"), "").unwrap();
    wait_for("Oak's turn to end", || host.state("oak") == "done");
    let reply = rows_of_trace(&host, "oak", sent["trace_id"].as_str().unwrap());
    assert!(reply.len() >= 2, "{reply:?}");
    for row in reply {
        assert_eq!(row["audience"], "user", "{row}");
    }
}
