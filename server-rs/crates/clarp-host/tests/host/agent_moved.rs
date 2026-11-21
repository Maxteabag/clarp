use super::*;

const ADMIN: &str = "test-move-administrator";

fn target() -> Value {
    json!({"host":"Destination","id":"other-host-id"})
}

fn change(host: &Host, session: &str, to: Value) -> (u16, Value) {
    host.ask(
        "POST",
        "/agent-moved",
        Some(&json!({"session":session,"moved_to":to})),
    )
}

fn row(host: &Host, session: &str) -> Value {
    host.get("/agents/snapshot")["agents"]
        .as_array()
        .unwrap()
        .iter()
        .find(|agent| agent["session"] == session)
        .unwrap()
        .clone()
}

fn refused(mark: &Value) -> (u16, Value) {
    (
        409,
        json!({
            "ok":false,"error":"agent_moved","moved_to":mark["to"],"moved_at":mark["at"]
        }),
    )
}

#[test]
fn agent_moved_fences_turn_entry_and_releases_the_waiting_queue_in_order_on_return() {
    let place = Place::new("moved-queue");
    let host = place.start_with_token(ADMIN);
    let session = host.new_agent("Mover");
    assert_eq!(row(&host, &session).get("moved"), Some(&Value::Null));
    let events = host.listen_at(&format!("/events?token={ADMIN}"));
    let queue_path = format!("/turn-queue?session={session}");
    host.send(&session, "sleep 2", "active");
    wait_for("the turn to start", || row(&host, &session)["busy"] == true);
    for (id, text) in [("two", "Second"), ("three", "Third")] {
        let (status, answer) = host.ask(
            "POST",
            "/send",
            Some(&json!({
                "session":session,"text":text,"client_msg_id":id,"queue_if_busy":true,
            })),
        );
        assert_eq!((status, &answer["queued"]), (200, &json!(true)));
    }

    let (status, mark) = change(&host, &session, target());
    let moved = mark["moved"].clone();
    assert_eq!(status, 200);
    assert_eq!(
        mark,
        json!({"ok":true,"session":session,"changed":true,"moved":moved})
    );
    assert_eq!(moved["to"], target());
    assert!(moved["at"].as_i64().unwrap() > 0);
    assert!(
        row(&host, &session)["busy"] == true,
        "the mark lets the live turn finish"
    );
    assert_eq!(row(&host, &session)["moved"], moved);
    let (status, again) = change(&host, &session, target());
    assert_eq!(
        (status, &again["changed"], &again["moved"]),
        (200, &json!(false), &moved)
    );
    let (status, conflict) = change(&host, &session, json!({"host":"Elsewhere","id":"another"}));
    assert_eq!(
        (status, &conflict["error"], &conflict["moved"]),
        (409, &json!("already_moved"), &moved)
    );

    for (origin, later) in [
        ("user", false),
        ("user", true),
        ("agent", true),
        ("automation", true),
        ("heartbeat", true),
    ] {
        let send = json!({"session":session, "text":"Never admitted",
            "client_msg_id":format!("no-{origin}-{later}"),"queue_if_busy":later,"origin":origin});
        assert_eq!(host.ask("POST", "/send", Some(&send)), refused(&moved));
    }
    for (path, body) in [
        ("/turn-queue/two/send", json!({})),
        ("/turn-queue/resume", json!({"session":session})),
        ("/compact", json!({"session":session})),
        (
            "/oracle/delegations",
            json!({"session":session,"delegation_id":"moved-1","request":"Never admitted"}),
        ),
    ] {
        assert_eq!(
            host.ask("POST", path, Some(&body)),
            refused(&moved),
            "{path}"
        );
    }
    let terminal = asked_for_a_socket(&host, ADMIN, &format!("/terminal/{session}"));
    assert_eq!(
        (
            terminal.status,
            serde_json::from_slice::<Value>(&terminal.body).unwrap()
        ),
        refused(&moved)
    );

    wait_for("the first turn to finish on this Host", || {
        host.state(&session) == "done"
    });
    let queue = host.get(&queue_path);
    assert_eq!(
        (queue["items"].as_array().map(Vec::len), &queue["paused"]),
        (Some(2), &json!(true))
    );
    wait_for("only the new mark to reach the roster stream", || {
        events.lock().unwrap().iter().any(|event| {
            event["type"] == "agent-roster"
                && event["kind"] == "agent-moved"
                && event["session"] == session
        })
    });
    assert_eq!(
        events
            .lock()
            .unwrap()
            .iter()
            .filter(|event| event["kind"] == "agent-moved")
            .count(),
        1
    );
    assert!(
        host.conversation(&session)
            .iter()
            .all(|(_, text)| !matches!(text.as_str(), "Second" | "Third" | "Never admitted"))
    );

    host.kill();
    let host = place.start_with_token(ADMIN);
    wait_for("the agent to recover its mark and held queue", || {
        row(&host, &session)["moved"] == moved && host.get(&queue_path) == queue
    });
    assert_eq!(
        host.ask("POST", "/turn-queue/two/send", Some(&json!({}))),
        refused(&moved)
    );
    assert_eq!(
        change(&host, &session, Value::Null),
        (
            200,
            json!({"ok":true,"session":session,"changed":true,"moved":null})
        )
    );
    assert_eq!(
        change(&host, &session, Value::Null),
        (
            200,
            json!({"ok":true,"session":session,"changed":false,"moved":null})
        )
    );
    wait_for("the held work to finish, once and in order", || {
        let conversation = host.conversation(&session);
        conversation.ends_with(&[
            said("user", "Second"),
            said("assistant", "Second"),
            said("user", "Third"),
            said("assistant", "Third"),
        ]) && host.state(&session) == "done"
    });
    assert_eq!(row(&host, &session)["moved"], Value::Null);
    assert_eq!(host.get(&queue_path)["items"], json!([]));
}

#[test]
fn agent_moved_is_an_administrator_or_full_device_write() {
    let place = Place::new("moved-authority");
    let host = place.start_with_token(ADMIN);
    let session = host.new_agent("Mover");
    let issue = |scope| {
        let code = host.post(
            "/pairing/issue",
            json!({"scope":scope,"device_name":"Phone","url":"http://test"}),
        );
        let (status, paired) = host.ask(
            "POST",
            "/pairing/exchange",
            Some(&json!({"code":code["code"],"device_name":"Phone"})),
        );
        assert_eq!(status, 201);
        assert_eq!(paired["device"]["scope"], scope);
        paired["device"]["token"].as_str().unwrap().to_string()
    };
    let limited = issue("limited");
    let full = issue("full");
    let body = json!({"session":session,"moved_to":target()});
    assert_eq!(
        host.ask_as(&limited, "POST", "/agent-moved", Some(&body)).0,
        403
    );
    let bytes = body.to_string();
    for name in ["X-Clarp-Turn", "X-Clarp-Peer"] {
        let denied = fetch(
            &host,
            ADMIN,
            "POST",
            "/agent-moved",
            &[(name, "some-agent"), ("Content-Type", "application/json")],
            bytes.as_bytes(),
        );
        assert_eq!(denied.status, 403);
        assert_eq!(
            serde_json::from_slice::<Value>(&denied.body).unwrap()["error"],
            "agents_may_not"
        );
    }
    assert_eq!(row(&host, &session)["moved"], Value::Null);
    for malformed in [
        json!({"session":session}),
        json!({"session":session,"moved_to":"Destination"}),
        json!({"session":session,"moved_to":{"host":" ","id":"valid"}}),
        json!({"session":session,"moved_to":{"host":"Destination","id":"x".repeat(201)}}),
    ] {
        assert_eq!(host.ask("POST", "/agent-moved", Some(&malformed)).0, 400);
    }
    assert_eq!(change(&host, "nobody", Value::Null).0, 404);
    assert_eq!(
        host.ask_as(&full, "POST", "/agent-moved", Some(&body)).0,
        200
    );
    assert_eq!(change(&host, &session, Value::Null).1["changed"], true);
}

#[test]
fn agent_moved_keeps_an_unsent_continuation_ahead_of_the_other_waiting_messages() {
    let place = Place::new("moved-unsent");
    let shim = place.root.join("shim");
    let built = std::path::Path::new(env!("CARGO_BIN_EXE_clarp-host")).with_file_name("clarp-shim");
    std::os::unix::fs::symlink(&built, &shim).unwrap();
    let host = place.start_with("", &["--shim", shim.to_str().unwrap()]);
    let session = host.new_agent("Mover");
    let me = row(&host, &session);
    let agent_id = me["agent_id"].as_str().unwrap();
    let spool = place
        .root
        .join("data/agents")
        .join(agent_id)
        .join("spool/1.jsonl");
    let events = host.listen();
    host.send(&session, "sleep 3", "blocker");
    wait_for("the shim to start the blocking turn", || spool.is_file());
    assert_eq!(host.post("/send", json!({
        "session":session,"text":"First","client_msg_id":"unsent-first","queue_if_busy":true,
    }))["queued"], true);
    std::fs::remove_file(&shim).unwrap();
    wait_for("a refused launch with nothing sent to a tool", || {
        events
            .lock()
            .unwrap()
            .iter()
            .any(|event| event["detail"]["action"] == "reconnecting")
    });
    assert_eq!(host.post("/send", json!({
        "session":session,"text":"Second","client_msg_id":"unsent-second","queue_if_busy":true,
    }))["queued"], true);
    let (status, marked) = change(&host, &session, target());
    assert_eq!(status, 200);
    let moved = marked["moved"].clone();
    assert_eq!(row(&host, &session)["busy"], false);
    let queue = format!("/turn-queue?session={session}");
    let waiting = host.get(&queue);
    assert_eq!(waiting["paused"], true);
    assert_eq!(waiting["items"].as_array().unwrap().len(), 1);
    assert_eq!(waiting["items"][0]["text"], "Second");
    host.kill();

    let host = place.start();
    wait_for(
        "the unsent continuation to recover in the held queue",
        || row(&host, &session)["moved"] == moved && host.get(&queue) == waiting,
    );
    let before = host.conversation(&session);
    assert_eq!(
        before
            .iter()
            .filter(|row| **row == said("user", "First"))
            .count(),
        1
    );
    assert_eq!(change(&host, &session, Value::Null).0, 200);
    let mut expected = before;
    expected.extend([
        said("assistant", "First"),
        said("user", "Second"),
        said("assistant", "Second"),
    ]);
    wait_for("the original messages to answer once each", || {
        host.conversation(&session) == expected
    });
}

#[test]
fn agent_moved_records_decision_and_form_answers_without_delivering_until_return() {
    let place = Place::new("moved-deliveries");
    let host = place.start();
    let session = host.new_agent("Mover");
    let (status, decision) = host.ask(
        "POST",
        "/decisions",
        Some(&json!({
            "session":session,"title":"Pick a route","question":"Take the bridge?",
        })),
    );
    assert_eq!(status, 201);
    let id = decision["artifact"]["decision"]["decision_id"]
        .as_str()
        .unwrap();
    let work = place.root.join("work");
    std::fs::write(work.join("form.html"), "<form><input name=route></form>").unwrap();
    std::fs::write(
        work.join("schema.json"),
        r#"{"type":"object","properties":{"route":{"type":"string"}},"required":["route"]}"#,
    )
    .unwrap();
    let (code, _, err) = tool(
        &host,
        &work,
        None,
        &[
            "clarp-agent-artifacts",
            "create-form",
            &session,
            "Route",
            "form.html",
            "schema.json",
            "--version",
            "1",
            "--artifact-id",
            "move-form",
        ],
    );
    assert_eq!(code, 0, "{err}");
    assert_eq!(change(&host, &session, target()).0, 200);
    let (status, answer) = host.ask(
        "POST",
        &format!("/decisions/{id}/resolve"),
        Some(&json!({
            "choice":"accepted","expected_revision":1,
        })),
    );
    assert_eq!((status, &answer["delivery_pending"]), (200, &json!(true)));
    let (status, receipt) = host.ask(
        "POST",
        "/artifacts/move-form/submit",
        Some(&json!({
            "submission_id":"0a1b2c3d-0000-4000-8000-000000000001","version":"1",
            "answers":{"route":"Bridge"},
        })),
    );
    assert!(matches!(status, 200 | 201 | 202), "{receipt}");
    assert!(host.conversation(&session).is_empty());
    assert_eq!(
        host.get(&format!("/turn-queue?session={session}"))["items"],
        json!([])
    );
    assert_eq!(change(&host, &session, Value::Null).0, 200);
    let requests = || {
        host.conversation(&session)
            .into_iter()
            .filter_map(|(role, text)| (role == "user").then_some(text))
            .collect::<Vec<_>>()
    };
    wait_for("both saved answers to be delivered on return", || {
        requests().len() == 2
    });
    assert!(
        requests()
            .iter()
            .any(|text| text.contains("[Clarp decision resolved]"))
    );
    assert!(requests().iter().any(|text| text.contains("Bridge")));
}

#[test]
fn agent_moved_holds_a_due_goal_and_refuses_the_heartbeat_and_schedule_dispatches() {
    let place = Place::new("moved-wakes");
    let launch = place.root.join("janitor-launched");
    let claude = janitors_tool(&place, &format!(r#"touch "{}""#, launch.display()));
    let host = place.start_in(
        "",
        &[
            "--heartbeats-checked-every-ms",
            "50",
            "--schedules-checked-every-ms",
            "50",
            "--janitors-checked-every-ms",
            "50",
        ],
        &[("CLARP_CLAUDE_PROGRAM", claude.to_str().unwrap())],
    );
    let janitor = host.post("/agents", json!({"name":"Sweeper","backend":"claude"}));
    let janitor = janitor["session"].as_str().unwrap();
    assert_eq!(
        host.ask("POST", "/janitors", Some(&every_minute(janitor, 120)))
            .0,
        200
    );
    assert_eq!(change(&host, janitor, target()).0, 200);
    assert_eq!(
        host.ask(
            "POST",
            &format!("/janitors/{janitor}/enabled"),
            Some(&json!({
                "expected_revision":1,"enabled":true,
            }))
        )
        .0,
        200
    );
    let session = host.new_agent("Mover");
    host.send(&session, r#"{"session":"moved-wakes-native"}"#, "bind");
    wait_for("the native session", || host.state(&session) == "done");
    let plan = goal(
        &host,
        &place.root,
        &[
            "create",
            &session,
            "walk",
            "Walk",
            "[]",
            r#"{"outcome":"Reached","criteria":["Arrived"],"limits":"None","enroll":true}"#,
        ],
    );
    let plan_id = plan["plan_id"].as_str().unwrap();
    let before = host.conversation(&session);
    assert_eq!(change(&host, &session, target()).0, 200);
    let due = now_ms() + 200;
    let checkpoint = json!({
        "progress":"Started","next_work":"Go on",
        "continuation":{"kind":"timer","due_at":due,"reason":"Go on"}
    });
    goal(
        &host,
        &place.root,
        &["checkpoint", plan_id, "0", &checkpoint.to_string()],
    );
    let (status, _) = host.ask(
        "POST",
        "/agent-heartbeat",
        Some(&json!({
            "session":session,"heartbeat_enabled":true
        })),
    );
    assert_eq!(status, 200);
    let (status, scheduled) = host.ask("POST", "/agent-schedules", Some(&json!({
        "session":session,"name":"Every minute","cron":"* * * * *","prompt":"Schedule should stay silent",
    })));
    assert_eq!(status, 201, "{scheduled}");
    let schedule_id = &scheduled["schedule"]["schedule_id"];
    wait_within(
        "the scheduled time to pass",
        Duration::from_secs(65),
        || {
            host.get("/agent-schedules")["schedules"]
                .as_array()
                .unwrap()
                .iter()
                .any(|row| &row["schedule_id"] == schedule_id && row["last_run_at"].is_number())
        },
    );
    wait_within(
        "the janitor schedule to offer its run",
        Duration::from_secs(65),
        || {
            host.get(&format!("/janitors/{janitor}/runs"))["runs"]
                .as_array()
                .is_some_and(|runs| !runs.is_empty())
        },
    );
    assert!(!launch.exists());
    assert!(host.conversation(janitor).is_empty());
    assert_eq!(
        host.get(&format!("/turn-queue?session={janitor}"))["items"],
        json!([])
    );
    assert_eq!(host.conversation(&session), before);
    assert_eq!(
        host.get(&format!("/turn-queue?session={session}"))["items"],
        json!([])
    );
    let held = goal(&host, &place.root, &["get", plan_id]);
    assert_eq!(held["goal"]["continuation"]["attempts"], 0);
    host.post(
        "/agent-heartbeat",
        json!({"session":session,"heartbeat_enabled":false}),
    );
    host.post(
        "/agent-schedules/toggle",
        json!({"schedule_id":schedule_id,"enabled":false}),
    );
    assert_eq!(change(&host, &session, Value::Null).0, 200);
    wait_for("the original goal wake to run on return", || {
        host.conversation(&session).iter().any(|(role, text)| {
            role == "user" && text.starts_with("Continue your durable outcome commitment.")
        })
    });
}
