//! The Host's tests of the turn an artifact names (`source_trace_id`, R102): one file a piece,
//! declared in `host.rs`.

use super::*;

/// What the Python Host's artifacts name, case by case (`tools/source-trace-vectors.py`, from
/// its `_publishing_trace`): `the trace` of the publishing turn, `none`, or `unavailable`.
fn vectors() -> Vec<Value> {
    let read: Value = serde_json::from_str(include_str!(
        "../../../clarp-kernel/tests/fixtures/source-trace-vectors.json"
    ))
    .unwrap();
    read["vectors"].as_array().unwrap().clone()
}

/// An artifact published by `POST /artifacts` names the turn that published it: the trace its
/// rows carry, when the request presents that turn's token and the turn is the artifact's own
/// agent's running turn; nothing otherwise (no token, a text that is no token, another agent's,
/// a turn that ended, an older turn while a newer one runs). A later revision keeps what the
/// first had; a request to the user does not pass that route and names none. Each case, for a
/// document, a render spec and a read-only report, as the Python Host answers it.
///
/// Not staged here: a running turn without a trace (this Host gives every message one; the
/// actor's own answer for such a turn is held in the actor's tests), and a lookup that fails
/// (the 503 every reader of a turn's token answers when its key cannot be read).
#[test]
fn an_artifact_names_the_running_turn_that_published_it_as_the_python_host_names_it() {
    let place = Place::new("source-trace");
    let (tokens, release) = (place.root.join("tokens"), place.root.join("release"));
    std::fs::create_dir_all(&tokens).unwrap();
    std::fs::create_dir_all(&release).unwrap();
    // A stand-in for Claude Code that writes down the token of the turn it runs in, under its
    // agent's session. Told to hold, its turn runs until the test lets it go.
    let script = [
        "#!/bin/sh\nread -r message\n",
        &format!(
            "printf '%s' \"$CLARP_PROVIDER_TURN\" > \"{}/$CLARP_SESSION\"\n",
            tokens.display()
        ),
        &format!(
            "case \"$message\" in hold*) n=0; while [ ! -e \"{0}/$CLARP_SESSION\" ] && [ -d \"{0}\" ] \
             && [ $n -lt 600 ]; do sleep 0.2; n=$((n+1)); done; rm -f \"{0}/$CLARP_SESSION\";; esac\n",
            release.display()
        ),
        STAND_IN_ANSWERS,
    ]
    .concat();
    let claude = place.root.join("claude");
    program(&claude, &script);
    // The same stand-in for Codex: what it says is no matter, only the token it is given.
    let world = [
        ("CLARP_CLAUDE_PROGRAM", claude.to_str().unwrap()),
        ("CLARP_CODEX_PROGRAM", claude.to_str().unwrap()),
    ];
    let host = place.start_in("", &[], &world);
    for (name, session, backend) in [
        ("Ivy", "ivy", "claude"),
        ("Oak", "oak", "claude"),
        ("Gone", "gone", "claude"),
        ("Cody", "cody", "codex"),
    ] {
        let made = host.post(
            "/agents",
            json!({ "name": name, "session": session, "backend": backend }),
        );
        assert_eq!(made["session"], session, "{made}");
    }
    let token_of =
        |session: &str| std::fs::read_to_string(tokens.join(session)).unwrap_or_default();
    // A turn of the agent, held running or let to end; the token it was given, and the trace
    // its rows carry.
    let turn = |session: &str, id: &str, hold: bool| -> (String, String) {
        let before = token_of(session);
        let text = if hold { "hold on" } else { "done soon" };
        host.send(session, text, id);
        wait_for("the turn's token", || {
            let now = token_of(session);
            !now.is_empty() && now != before
        });
        // The token is written by the turn's own tool: the turn runs. One not held ends.
        if !hold {
            wait_for("the turn to end", || host.state(session) == "done");
        }
        let rows = host.get(&format!("/log?session={session}"));
        let row = rows["turns"]
            .as_array()
            .unwrap()
            .iter()
            .find(|row| row["id"] == format!("u-{id}"));
        let trace = row.map(|row| row["trace_id"].as_str().unwrap_or("").to_string());
        (token_of(session), trace.unwrap_or_default())
    };
    let let_go = |session: &str| {
        std::fs::write(release.join(session), "").unwrap();
        wait_for("the turn to end", || host.state(session) == "done");
    };
    // An artifact of a kind, published for Ivy with a token or none; its row.
    let ids = std::cell::Cell::new(0);
    let publish = |kind: &str, token: Option<&str>, id: Option<&str>, version: &str| -> Value {
        ids.set(ids.get() + 1);
        let id = id.map_or_else(|| format!("card-{}", ids.get()), str::to_string);
        let body = match kind {
            "document" => json!({ "session": "ivy", "type": "document", "title": "A card",
                                  "artifact_id": id, "payload": { "content": "words" } }),
            "render_spec" => json!({ "session": "ivy", "type": "render_spec", "title": "Build",
                                     "artifact_id": id, "payload": build_form(version),
                                     "expected_version": (version != "build-1").then_some("build-1") }),
            _ => {
                json!({ "session": "ivy", "type": "html_form", "title": "A report", "artifact_id": id,
                         "payload": { "content": format!("<p>{version}</p>"), "version": version,
                                      "read_only": true },
                         "expected_version": (version != "1").then_some("1") })
            }
        };
        let mut body = body;
        if body["expected_version"].is_null()
            && let Some(fields) = body.as_object_mut()
        {
            fields.remove("expected_version");
        }
        let mut headers = vec![
            ("Content-Type", "application/json"),
            ("X-Clarp-Client", "ios/12 contract=106"),
        ];
        if let Some(token) = token {
            headers.push(("X-Clarp-Turn", token));
        }
        let got = fetch(
            &host,
            &host.token,
            "POST",
            "/artifacts",
            &headers,
            body.to_string().as_bytes(),
        );
        let answer: Value = serde_json::from_slice(&got.body).unwrap_or(Value::Null);
        assert!(
            got.status == 201 || got.status == 200,
            "{kind}: {} {answer}",
            got.status
        );
        answer["artifact"].clone()
    };
    let named = |row: &Value, trace: &str| match row["source_trace_id"].as_str().unwrap_or("?") {
        "" => "none".to_string(),
        shown if shown == trace => "the trace".to_string(),
        shown => format!("another: {shown}"),
    };
    let first = |kind: &str| {
        if kind == "render_spec" {
            "build-1"
        } else {
            "1"
        }
    };
    let kinds = ["document", "render_spec", "report"];
    let mut found: Vec<(String, String, String)> = Vec::new();
    let mut note = |case: &str, kind: &str, shown: String| {
        found.push((case.to_string(), kind.to_string(), shown));
    };

    // A token of a turn nobody has: one this Host gave, of an agent it no longer has.
    let (gone, _) = turn("gone", "g-1", false);
    assert_eq!(host.ask("DELETE", "/agents/gone", None).0, 200);

    // Ivy's turn runs: what it publishes with its token names it; without one, or with a text
    // that is no token, nothing. Oak's turn runs too: its token names nothing of Ivy's.
    let (ivy, ivy_trace) = turn("ivy", "i-1", true);
    assert!(
        !ivy_trace.is_empty(),
        "every message of this Host has a trace"
    );
    let (oak, _) = turn("oak", "o-1", true);
    let mut published = Vec::new();
    for kind in kinds {
        let row = publish(kind, Some(&ivy), None, first(kind));
        assert_eq!(row["source_message_id"], "", "{row}");
        note("the running turn", kind, named(&row, &ivy_trace));
        published.push((kind, row["artifact_id"].as_str().unwrap().to_string()));
        note(
            "no token",
            kind,
            named(&publish(kind, None, None, first(kind)), &ivy_trace),
        );
        let none = publish(kind, Some("not-a-token-of-this-host"), None, first(kind));
        note("a token that is none", kind, named(&none, &ivy_trace));
        let nobody = publish(kind, Some(&gone), None, first(kind));
        note(
            "a token of a turn nobody has",
            kind,
            named(&nobody, &ivy_trace),
        );
        note(
            "another agent's token",
            kind,
            named(&publish(kind, Some(&oak), None, first(kind)), &ivy_trace),
        );
    }
    let_go("oak");
    // Ivy's turn ends: its token names nothing any more (a job it started, publishing later).
    let_go("ivy");
    for kind in kinds {
        note(
            "the turn ended",
            kind,
            named(&publish(kind, Some(&ivy), None, first(kind)), &ivy_trace),
        );
    }
    // A newer turn of Ivy's runs: the older turn's token names nothing; what was published in
    // the older turn and is revised now keeps its trace.
    let (later, later_trace) = turn("ivy", "i-2", true);
    assert_ne!(later_trace, ivy_trace);
    for kind in kinds {
        note(
            "an older turn",
            kind,
            named(&publish(kind, Some(&ivy), None, first(kind)), &ivy_trace),
        );
    }
    for (kind, id) in &published {
        let revised = match *kind {
            "document" => {
                let (status, answer) = host.ask(
                    "POST",
                    &format!("/artifacts/{id}"),
                    Some(&json!({ "payload_patch": { "content": "more words" } })),
                );
                assert_eq!(status, 200, "{answer}");
                answer["artifact"].clone()
            }
            "render_spec" => publish(kind, Some(&later), Some(id), "build-2"),
            _ => publish(kind, Some(&later), Some(id), "2"),
        };
        assert_eq!(revised["artifact_id"], id.as_str(), "{revised}");
        note(
            "a revision by a later turn",
            kind,
            named(&revised, &ivy_trace),
        );
    }
    // The Host's own tool, called in the turn, presents that turn's token, as the Python Host's
    // tool sends it: what it publishes names the turn.
    let tool = Command::new(env!("CARGO_BIN_EXE_clarp-host"))
        .args([
            "tool",
            "clarp-agent-artifacts",
            "create",
            "ivy",
            "document",
            "Tool card",
        ])
        .args(["by the tool", r#"{"content":"words"}"#])
        .current_dir(&place.root)
        .env("CLARP_HOST_ADDR", &host.address)
        .env("CLARP_HOST_TOKEN", &host.token)
        .env("CLARP_PROVIDER_TURN", &later)
        .env_remove("CLARP_SESSION")
        .env_remove("CLAUDE_PWA_SESSION")
        .ended();
    let printed: Value = serde_json::from_slice(&tool.stdout).unwrap_or(Value::Null);
    let row = printed.get("artifact").cloned().unwrap_or(printed);
    assert_eq!(named(&row, &later_trace), "the trace", "{row}");

    // A turn of Codex has its token on this Host too, and names what it publishes: the rule is
    // the token's (the Python Host's Codex agents have none: a registered difference).
    let (cody, cody_trace) = turn("cody", "c-1", true);
    let body = json!({ "session": "cody", "type": "document", "title": "Codex's card",
                       "payload": { "content": "words" } });
    let headers = [
        ("Content-Type", "application/json"),
        ("X-Clarp-Turn", cody.as_str()),
    ];
    let got = fetch(
        &host,
        &host.token,
        "POST",
        "/artifacts",
        &headers,
        body.to_string().as_bytes(),
    );
    let got: Value = serde_json::from_slice(&got.body).unwrap();
    assert_eq!(named(&got["artifact"], &cody_trace), "the trace", "{got}");
    std::fs::write(release.join("cody"), "").unwrap();

    // A request to the user, made by its own route in a running turn, names none.
    let headers = [
        ("Content-Type", "application/json"),
        ("X-Clarp-Turn", later.as_str()),
    ];
    let body = json!({ "session": "ivy", "title": "Ship it?", "question": "Ship it?" });
    let asked = fetch(
        &host,
        &host.token,
        "POST",
        "/decisions",
        &headers,
        body.to_string().as_bytes(),
    );
    let asked: Value = serde_json::from_slice(&asked.body).unwrap();
    note(
        "a request to the user",
        "decision",
        named(&asked["artifact"], &later_trace),
    );
    let_go("ivy");

    let staged: Vec<&(String, String, String)> = found.iter().collect();
    let mut compared = 0;
    for vector in vectors() {
        let (case, kind) = (
            vector["case"].as_str().unwrap(),
            vector["kind"].as_str().unwrap(),
        );
        let Some((_, _, shown)) = staged.iter().find(|(c, k, _)| c == case && k == kind) else {
            assert!(
                ["a running turn without a trace", "a lookup that fails"].contains(&case),
                "a case of the Python Host's that is not staged here: {case} ({kind})"
            );
            continue;
        };
        assert_eq!(shown.as_str(), vector["shown"], "{case} ({kind})");
        compared += 1;
    }
    assert_eq!(
        compared,
        found.len(),
        "every case staged here is one of the Python Host's"
    );
}

/// The Python Host's own form (`tests/fixtures/render_spec/build-decision.json`), at a version.
fn build_form(version: &str) -> Value {
    json!({
        "version": version, "catalog": "clarp.form", "catalog_version": 1,
        "spec": { "root": "root", "elements": {
            "root": { "type": "stack", "props": {},
                      "children": ["topic", "instructions", "selection", "comment", "notify"] },
            "topic": { "type": "heading", "props": { "text": "Build decision", "level": 1 }, "children": [] },
            "selection": { "type": "choice", "props": {
                "value": { "$bindState": "/choice" }, "label": "Next step",
                "options": [{ "value": "send", "label": "Send this build" },
                            { "value": "review", "label": "Review first" }] }, "children": [] },
            "comment": { "type": "text_field",
                         "props": { "label": "Comment", "value": { "$bindState": "/answer" } }, "children": [] },
            "notify": { "type": "toggle",
                        "props": { "label": "Notify", "value": { "$bindState": "/notify" } }, "children": [] },
            "instructions": { "type": "text", "props": { "text": "Choose what should happen to this build." } } },
          "state": {} },
        "submit": { "label": "Submit", "intent": "submit_decision" },
        "answer_schema": { "type": "object", "properties": {
            "choice": { "type": "string", "enum": ["send", "review"] },
            "answer": { "type": "string", "maxLength": 2000 },
            "notify": { "type": "boolean" } }, "required": ["choice"] },
    })
}
