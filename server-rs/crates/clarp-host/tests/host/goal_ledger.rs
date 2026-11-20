//! The Host's tests of the goal ledger: one file a piece, declared in `host.rs`.

use super::*;

#[test]
fn owner_subgoal_edits_survive_restart_with_their_evidence_and_events() {
    let place = Place::new("owner-subgoal");
    let host = place.start();
    let session = host.new_agent("Planner");
    let plan = goal(
        &host,
        &place.root,
        &[
            "create",
            &session,
            "build",
            "Fixture",
            "[]",
            r#"{"outcome":"Deliver","limits":"Local","criteria":["Done"]}"#,
        ],
    );
    let id = plan["plan_id"].as_str().unwrap();
    let cmd = |host: &Host, action: &str, data: Value| {
        goal(
            host,
            &place.root,
            &["subgoal", id, action, &data.to_string()],
        )
    };
    let added = cmd(&host, "add", json!({"subgoal_id":"probe","title":"Probe"}));
    assert_eq!(added["subgoals"][0]["revision"], 1);
    let update = |revision: i64, evidence: &str| json!({"subgoal_id":"probe","expected_revision":revision,"fields":{"evidence":[{"text":evidence}]}});
    cmd(&host, "update", update(1, "first"));
    let held = host.get(&format!("/goal-ledger?plan_id={id}"));
    let stale = tool(
        &host,
        &place.root,
        None,
        &[
            "clarp-goal",
            "subgoal",
            id,
            "update",
            &update(1, "stale").to_string(),
        ],
    );
    assert_eq!(stale.0, 1);
    assert!(stale.2.contains("changed (revision 2)"));
    assert_eq!(host.get(&format!("/goal-ledger?plan_id={id}")), held);
    host.kill();
    let host = place.start();
    let updated = cmd(&host, "update", update(2, "second"));
    let evidence = &updated["subgoals"][0]["evidence"];
    assert_eq!(evidence.as_array().unwrap().len(), 2);
    assert_eq!(evidence[0]["text"], "first");
    assert_eq!(evidence[1]["text"], "second");
    let retired = cmd(
        &host,
        "update",
        json!({"subgoal_id":"probe","expected_revision":3,"fields":{"status":"retired"},"reason":"done"}),
    );
    assert_eq!(retired["subgoals"][0]["status"], "retired");
    let plan = host.get(&format!("/task-plans?session={session}"))["plans"][0].clone();
    assert_eq!(plan["subgoals"], retired["subgoals"]);
    assert_eq!(plan["revision"], 0);
    assert_eq!(plan["ledger"]["event_count"], 5);
    let events = host.get(&format!("/goal-ledger?plan_id={id}"))["events"].clone();
    assert_eq!(events.as_array().unwrap().len(), 5);
    assert_eq!(events[1]["kind"], "subgoal_added");
    assert_eq!(events[4]["kind"], "subgoal_updated");
    assert_eq!(events[4]["subject"], "subgoal:probe");
    assert_eq!(events[4]["actor_agent_id"], plan["agent_id"]);
    assert_eq!(events[4]["prior"], json!({"status":"active"}));
    assert_eq!(events[4]["new"], json!({"status":"retired"}));
    host.kill();
    let host = place.start();
    assert_eq!(
        goal(&host, &place.root, &["get", id])["subgoals"],
        retired["subgoals"]
    );
    // Both commands name the same revision. The owner's actor admits one and
    // rejects the other, including after it has been opened again.
    let write = |text: &str| {
        tool(&host,&place.root,None,&[
        "clarp-goal","subgoal",id,"update",&json!({"subgoal_id":"probe","expected_revision":4,"fields":{"current_action":text}}).to_string()
    ])
    };
    let (one, two) = std::thread::scope(|scope| {
        let first = scope.spawn(|| write("left"));
        let second = scope.spawn(|| write("right"));
        (first.join().unwrap(), second.join().unwrap())
    });
    let mut exits = [one.0, two.0];
    exits.sort();
    assert_eq!(exits, [0, 1]);
    let rejected = if one.0 == 1 { one.2 } else { two.2 };
    assert!(rejected.contains("revision 5"), "{rejected}");
    let final_plan = goal(&host, &place.root, &["get", id]);
    let event_rows = goal(&host, &place.root, &["ledger", id])["events"].clone();
    assert_eq!(final_plan["ledger"]["event_count"], 6);
    assert_eq!(final_plan["subgoals"][0]["status"], "retired");
    assert_eq!(final_plan["subgoals"][0]["revision"], 5);

    let other = Place::new("subgoal-import");
    let target = other.start();
    let imported=target.post("/agents/import",json!({
        "from":"fixture","agent":{"agent_id":final_plan["agent_id"],"session":session,"persona":"Planner","backend":"scripted","cwd":other.root.join("work")},
        "turns":[],"plans":[{"plan":final_plan,"events":event_rows}]
    }));
    assert_eq!(imported["ok"], true, "{imported}");
    let moved = goal(&target, &other.root, &["get", id]);
    assert_eq!(moved["subgoals"], final_plan["subgoals"]);
    assert_eq!(moved["ledger"], final_plan["ledger"]);
    let reopened = goal(
        &target,
        &other.root,
        &[
            "subgoal",
            id,
            "update",
            &json!({"subgoal_id":"probe","expected_revision":5,"fields":{"status":"active"}})
                .to_string(),
        ],
    );
    assert_eq!(reopened["subgoals"][0]["revision"], 6);
    assert_eq!(reopened["subgoals"][0]["evidence"], *evidence);
}

#[test]
fn a_delegation_is_reserved_started_enforced_stopped_and_read_back() {
    use std::os::unix::fs::PermissionsExt;
    let place = Place::new("books");
    let cli = place.root.join("fake-claude");
    let token_file = place.root.join("signed-turn");
    let script = format!(
        "#!/bin/sh\nread -r line\nprintf '%s' \"$CLARP_PROVIDER_TURN\" > '{}'\n{}",
        token_file.display(),
        STAND_IN_ANSWERS
    );
    program(&cli, &script);
    let boot = || {
        place.start_in(
            "",
            &[],
            &[
                ("HOME", place.root.to_str().unwrap()),
                ("CLARP_CLAUDE_PROGRAM", cli.to_str().unwrap()),
            ],
        )
    };
    let host = boot();
    let agent = |name: &str, parent: Option<&str>| {
        let mut data = json!({"name":name,"session":name,"backend":"claude"});
        if let Some(p) = parent {
            data["role"] = json!("helper");
            data["parent"] = json!(p);
        }
        assert_eq!(host.post("/agents", data)["session"], name);
    };
    agent("principal", None);
    agent("helper", Some("principal"));
    agent("child", Some("helper"));
    let own = row_id(&host, "principal");
    let helper = row_id(&host, "helper");
    let mut tokens = std::collections::BTreeMap::new();
    for session in ["principal", "helper", "child"] {
        if token_file.exists() {
            std::fs::remove_file(&token_file).unwrap();
        }
        host.send(session, "begin", &format!("bind-{session}"));
        wait_for(&format!("the {session} turn to publish its token"), || {
            let present = std::fs::read_to_string(&token_file).is_ok_and(|t| !t.is_empty());
            assert!(
                present || host.state(session) != "interrupted",
                "{session} interrupted before token delivery: {}",
                host.heard.lock().unwrap()
            );
            present
        });
        tokens.insert(session, std::fs::read_to_string(&token_file).unwrap());
    }
    let call = |host: &Host, tool: &str, argv: &[&str], who: Option<&str>| {
        let env = who.map_or_else(|| json!({}), |s| json!({"CLARP_PROVIDER_TURN":tokens[s]}));
        host.post("/tools/run", json!({"tool":tool,"argv":argv,"env":env}))
    };
    let commitment = r#"{"outcome":"Deliver","criteria":["done"],"limits":"local"}"#;
    let made = goal(
        &host,
        &place.root,
        &["create", "principal", "work", "Fixture", "[]", commitment],
    );
    let plan = made["plan_id"].as_str().unwrap();
    let route = "/goal-ledger/delegations";
    let outstanding = call(
        &host,
        "clarp-admin",
        &[
            "request",
            "--from",
            "principal",
            "--to",
            "helper",
            "--text",
            "pending",
        ],
        Some("principal"),
    );
    assert_eq!(outstanding["exit"], 0, "{outstanding}");
    let request: Value = serde_json::from_str(outstanding["stdout"].as_str().unwrap()).unwrap();
    let request_id = request["request_id"].as_str().unwrap();
    let prefix = format!("[Request {request_id} ");
    wait_for("the helper to receive the request", || {
        host.conversation("helper")
            .iter()
            .any(|(role, text)| role == "user" && text.starts_with(&prefix))
    });
    let create = json!({"principal":"principal","delegate":"helper","reason":"  keep  "});
    let (one, two) = std::thread::scope(|scope| {
        let a = scope.spawn(|| host.ask("POST", route, Some(&create)));
        let b = scope.spawn(|| host.ask("POST", route, Some(&create)));
        (a.join().unwrap(), b.join().unwrap())
    });
    let (ok, no) = if one.0 == 200 { (one, two) } else { (two, one) };
    assert_eq!(ok.0, 200, "{}", ok.1);
    assert_eq!(
        no,
        (
            409,
            json!({"error":"an active delegation already exists for this principal or delegate"})
        )
    );
    let row = ok.1;
    let id = row["delegation_id"].as_str().unwrap();
    assert_eq!(row["listener"]["started"], true);
    assert_eq!(row["listener"]["error"], "");
    let oracle: Value = serde_json::from_str(include_str!(
        "../../../clarp-kernel/tests/fixtures/goal-delegation.json"
    ))
    .unwrap();
    // The listener can tick between any two HTTP reads. Its changing fields
    // have a separate end-to-end test. Compare the whole creation response
    // against the static oracle before the listener has had any work.
    let lifecycle = |mut row: Value| {
        for key in [
            "last_heartbeat_at",
            "last_source_at",
            "job_handle",
            "unapplied_since",
            "lag_ms",
            "last_wake_at",
            "last_wake_id",
            "dispatched_message_through",
            "dispatched_goal_event_through",
            "in_flight",
            "last_error",
        ] {
            row.as_object_mut().unwrap().remove(key);
        }
        row
    };
    let expected = |status: &str, at: &Value| {
        let mut result = oracle[status].clone();
        result["delegation_id"] = json!(id);
        result["principal_agent_id"] = json!(own);
        result["delegate_agent_id"] = json!(helper);
        result["principal_session"] = json!("principal");
        result["delegate_session"] = json!("helper");
        result["created_at"] = row["created_at"].clone();
        result["created_reason"] = json!("keep");
        if status == "stopped" {
            result["stopped_reason"] = json!("done");
            result["stopped_at"] = at.clone();
        }
        result
    };
    let mut initial = row.clone();
    for k in ["baseline_message_revision", "listener", "token_path"] {
        initial.as_object_mut().unwrap().remove(k);
    }
    assert_eq!(initial, expected("active", &Value::Null));
    assert_eq!(row["created_reason"], "keep");
    assert_eq!(row["principal_agent_id"], own);
    assert_eq!(row["delegate_agent_id"], helper);
    assert!(row.get("token_hash").is_none());
    let path = PathBuf::from(row["token_path"].as_str().unwrap());
    assert!(path.starts_with(place.root.join("data/delegations")));
    assert_eq!(path.metadata().unwrap().permissions().mode() & 0o777, 0o600);
    assert_eq!(
        path.parent()
            .unwrap()
            .metadata()
            .unwrap()
            .permissions()
            .mode()
            & 0o777,
        0o700
    );
    for who in ["principal", "helper"] {
        let records = host.get(&format!("{route}?session={who}"));
        assert_eq!(records["delegations"].as_array().unwrap().len(), 1);
        assert_eq!(records["delegations"][0]["delegation_id"], id);
        assert_eq!(
            lifecycle(records["delegations"][0].clone()),
            lifecycle(expected("active", &Value::Null))
        );
    }
    let cmd = ["pause", plan, "0", r#"{"reason":"review"}"#];
    for who in [None, Some("helper"), Some("child")] {
        let denied = call(&host, "clarp-goal", &cmd, who);
        assert_eq!(denied["exit"], 4, "{denied}");
    }
    let allowed = call(&host, "clarp-goal", &cmd, Some("principal"));
    assert_eq!(allowed["exit"], 0, "{allowed}");
    let response = call(
        &host,
        "clarp-admin",
        &[
            "reply",
            "--from",
            "helper",
            "--request",
            request_id,
            "--kind",
            "result",
            "--text",
            "correction",
        ],
        None,
    );
    assert_eq!(response["exit"], 4);
    assert!(
        response["stderr"]
            .as_str()
            .unwrap()
            .contains("a bookkeeping delegate does not reply into its principal"),
        "{response}"
    );
    let message = host.ask(
        "POST",
        "/send",
        Some(&json!({"session":"principal","sender":"helper","text":"steer"})),
    );
    assert_eq!(message.0, 403);
    // The Host checks both the named fork source and the caller's signed turn.
    // Naming the principal cannot let either the delegate or its child fork.
    for session in ["helper", "child"] {
        let expected = json!({"error":"a bookkeeping delegate does not start helpers or forks"});
        let named = format!("/agents/{session}/fork");
        assert_eq!(
            host.ask("POST", &named, Some(&json!({}))),
            (409, expected.clone())
        );
        let headers = [
            ("Content-Type", "application/json"),
            ("X-Clarp-Turn", tokens[session].as_str()),
        ];
        let reply = fetch(
            &host,
            &host.token,
            "POST",
            "/agents/principal/fork",
            &headers,
            b"{}",
        );
        assert_eq!(reply.status, 409, "{session}: {}", reply.text());
        assert_eq!(
            serde_json::from_slice::<Value>(&reply.body).unwrap(),
            expected
        );
    }
    let refused = host.ask(
        "POST",
        "/agents",
        Some(&json!({"name":"next","parent":"child"})),
    );
    assert_eq!(refused.0, 409);
    host.kill();

    let host = boot();
    assert_eq!(
        goal(&host, &place.root, &["get", plan])["ledger"]["delegation"]["delegation_id"],
        id
    );
    let stopped = call(
        &host,
        "clarp-admin",
        &[
            "bookkeeping",
            "stop",
            "--delegation",
            id,
            "--reason",
            "done",
        ],
        None,
    );
    assert_eq!(stopped["exit"], 0, "{stopped}");
    let stopped: Value = serde_json::from_str(stopped["stdout"].as_str().unwrap()).unwrap();
    assert_eq!(stopped["status"], "stopped");
    assert_eq!(stopped["stopped_reason"], "done");
    assert!(stopped["stopped_at"].is_i64());
    assert_eq!(
        lifecycle(host.get(&format!("{route}?session=principal"))["delegations"][0].clone()),
        lifecycle(expected("stopped", &stopped["stopped_at"]))
    );
    let resumed = call(
        &host,
        "clarp-goal",
        &["resume", plan, "1", r#"{"reason":"resume"}"#],
        None,
    );
    assert_eq!(resumed["exit"], 0, "{resumed}");
    let another = host.post(route, create.clone());
    assert_ne!(another["delegation_id"], id);
    // The administrative CLI makes the same checks as the HTTP doors. An
    // archived participant cannot create authority even through a valid name.
    host.post(
        "/agent-archive",
        json!({"session":"principal","archived":true}),
    );
    let archived = host.ask("POST", route, Some(&create));
    assert_eq!(
        archived,
        (409, json!({"error":"the principal is archived"}))
    );
    host.post(
        "/agent-archive",
        json!({"session":"principal","archived":false}),
    );
    let stopped_by_archive = host.get(&format!("{route}?session=helper"));
    assert!(
        stopped_by_archive["delegations"]
            .as_array()
            .unwrap()
            .iter()
            .any(|r| r["stopped_reason"] == "agent archived")
    );
    host.post(route, create.clone());
    let overlap = host.post(
        route,
        json!({"principal":"helper","delegate":"child","reason":"their own books"}),
    );
    assert_eq!(overlap["principal_agent_id"], helper);
    assert_eq!(host.ask("DELETE", "/agents/helper", None).0, 200);
    for session in ["child", "principal"] {
        let rows = host.get(&format!("{route}?session={session}"));
        assert!(
            rows["delegations"]
                .as_array()
                .unwrap()
                .iter()
                .all(|r| r["status"] == "stopped"),
            "{rows}"
        );
        assert!(
            rows["delegations"]
                .as_array()
                .unwrap()
                .iter()
                .any(|r| r["stopped_reason"] == "agent deleted"),
            "{rows}"
        );
    }
}

#[test]
fn a_delegate_observes_records_and_replays_without_changing_the_owners_checkpoint() {
    let place = Place::new("books-apply");
    let cli = place.root.join("claude");
    let signed = place.root.join("token");
    program(
        &cli,
        &format!(
            "#!/bin/sh\nread -r line\nprintf '%s' \"$CLARP_PROVIDER_TURN\" > '{}'\n{}",
            signed.display(),
            STAND_IN_ANSWERS
        ),
    );
    let boot = || {
        place.start_in(
            "",
            &[],
            &[
                ("HOME", place.root.to_str().unwrap()),
                ("CLARP_CLAUDE_PROGRAM", cli.to_str().unwrap()),
            ],
        )
    };
    let host = boot();
    host.post(
        "/agents",
        json!({"name":"Owner","session":"owner","backend":"claude"}),
    );
    host.post("/agents",json!({"name":"Helper","session":"helper","parent":"owner","role":"helper","backend":"claude"}));
    let turn = |session: &str| {
        if signed.exists() {
            std::fs::remove_file(&signed).unwrap();
        }
        host.send(session, "initial evidence", &format!("first-{session}"));
        wait_for("the test turn to receive its identity", || {
            std::fs::read_to_string(&signed).is_ok_and(|s| !s.is_empty())
        });
        let identity = std::fs::read_to_string(&signed).unwrap();
        wait_for(
            "the fixture turn to bind and its evidence to settle",
            || {
                host.get(&format!("/log?session={session}"))["turns"]
                    .as_array()
                    .is_some_and(|rows| {
                        rows.iter()
                            .any(|row| row["role"] == "assistant" && row["phase"] == "final")
                    })
            },
        );
        identity
    };
    let owner = turn("owner");
    let helper = turn("helper");
    let plan = goal(
        &host,
        &place.root,
        &[
            "create",
            "owner",
            "work",
            "Fixture",
            "[]",
            r#"{"outcome":"Deliver","limits":"Local","criteria":["Done"]}"#,
        ],
    );
    let pid = plan["plan_id"].as_str().unwrap();
    let grant = host.post(
        "/goal-ledger/delegations",
        json!({"principal":"owner","delegate":"helper","reason":"Keep"}),
    );
    let did = grant["delegation_id"].as_str().unwrap();
    let credential = std::fs::read_to_string(grant["token_path"].as_str().unwrap()).unwrap();
    let real = place.host_command()
        .arg("tool")
        .args(["clarp-goal", "bookkeeping", "observe", did])
        .current_dir(&place.root)
        .env("CLARP_HOST_ADDR", &host.address)
        .env("CLARP_HOST_TOKEN", &host.token)
        .env("CLARP_PROVIDER_TURN", &helper)
        .env(
            "CLARP_DELEGATION_TOKEN_FILE",
            grant["token_path"].as_str().unwrap(),
        )
        .ended();
    assert!(
        real.status.success(),
        "{}",
        String::from_utf8_lossy(&real.stderr)
    );
    let observed: Value = serde_json::from_slice(&real.stdout).unwrap();
    assert_eq!(observed["delegation_id"], did);
    let call = |h: &Host, who: &str, key: &str, argv: Value| {
        let env = if who.is_empty() {
            json!({})
        } else {
            json!({"CLARP_PROVIDER_TURN":who})
        };
        h.post(
            "/tools/run",
            json!({"tool":"clarp-goal","argv":argv,"bookkeeping_credential":key,"env":env}),
        )
    };
    let observe = json!(["bookkeeping", "observe", did]);
    for (who, key, reason) in [
        (
            helper.as_str(),
            "wrong",
            "unknown delegation or wrong credential",
        ),
        (
            owner.as_str(),
            credential.as_str(),
            "only the delegate's own Clarp turn can keep these books",
        ),
        (
            "",
            credential.as_str(),
            "only the delegate's own Clarp turn can keep these books",
        ),
    ] {
        let refused = call(&host, who, key, observe.clone());
        assert_eq!(refused["exit"], 4);
        assert_eq!(
            refused["stderr"].as_str().unwrap().trim(),
            format!("agent_tasks: {reason}")
        );
    }
    let from_start = json!(["bookkeeping", "observe", did, r#"{"after_message":0}"#]);
    let read = call(&host, &helper, &credential, from_start);
    assert_eq!(read["exit"], 0, "{read}");
    let read: Value = serde_json::from_str(read["stdout"].as_str().unwrap()).unwrap();
    assert_eq!(read["goals"][0]["plan_id"], pid);
    let event = &read["goal_events"][0];
    assert_eq!(event["kind"], "created");
    let msg = read["messages"]
        .as_array()
        .unwrap()
        .iter()
        .find(|m| m["text"] == "initial evidence")
        .unwrap();
    assert_eq!(msg["backend_session_id"], "stand-in");
    let reference = format!(
        "message:{}@{}",
        msg["message_id"].as_str().unwrap(),
        msg["revision"]
    );
    let entry =
        json!({"type":"claim","subject":"goal","text":"evidence seen","source_refs":[reference]});
    let apply = |h: &Host, wake: &str, entries: Value| {
        let batch = json!({"entries":entries,"goal_event_through":read["goal_event_through"],"message_through":read["message_through"]});
        call(
            h,
            &helper,
            &credential,
            json!(["bookkeeping", "record", did, pid, wake, batch.to_string()]),
        )
    };
    let rejected = apply(&host, "bad", json!([entry,{"type":"cancel"}]));
    assert_eq!(rejected["exit"], 4, "{rejected}");
    assert_eq!(
        goal(&host, &place.root, &["ledger", pid])["events"]
            .as_array()
            .unwrap()
            .len(),
        1
    );
    let entries =
        json!([entry,{"type":"subgoal_propose","subgoal_id":"inspect","title":"Inspect"}]);
    let mut counts = std::thread::scope(|scope| {
        let first = scope.spawn(|| apply(&host, "once", entries.clone()));
        let second = scope.spawn(|| apply(&host, "once", entries.clone()));
        [first.join().unwrap(), second.join().unwrap()].map(|result| {
            assert_eq!(result["exit"], 0, "{result}");
            let applied: Value = serde_json::from_str(result["stdout"].as_str().unwrap()).unwrap();
            applied["applied"].as_u64().unwrap()
        })
    });
    counts.sort_unstable();
    assert_eq!(counts, [0, 2]);
    let whole = goal(&host, &place.root, &["get", pid]);
    assert_eq!(whole["revision"], 0);
    assert_eq!(whole["subgoals"][0]["status"], "proposed");
    assert_eq!(whole["ledger"]["accounting"][0]["text"], "evidence seen");
    let recorded = goal(&host, &place.root, &["ledger", pid])["events"].clone();
    assert_eq!(recorded[1]["basis"], "claimed");
    assert_eq!(recorded[1]["new"]["sources"][0]["status"], "exact");
    assert_eq!(
        recorded[1]["new"]["sources"][0]["excerpt"],
        "initial evidence"
    );
    host.kill();
    let host = boot();
    let repeat = apply(&host, "once", json!([entry]));
    assert_eq!(repeat["exit"], 0, "{repeat}");
    let repeat: Value = serde_json::from_str(repeat["stdout"].as_str().unwrap()).unwrap();
    assert_eq!(repeat["applied"], 0);
    let summary = &goal(&host, &place.root, &["get", pid])["ledger"]["delegation"];
    assert_eq!(summary, &repeat["delegation"]);
    assert_eq!(
        host.get("/goal-ledger/delegations?session=helper")["delegations"][0],
        *summary
    );
    assert_eq!(
        goal(&host, &place.root, &["ledger", pid])["events"],
        recorded
    );
    let since = call(&host, &helper, &credential, observe.clone());
    let since: Value = serde_json::from_str(since["stdout"].as_str().unwrap()).unwrap();
    assert_eq!(since["goal_events"], json!([]));
    host.post(
        "/goal-ledger/delegations/stop",
        json!({"delegation_id":did,"reason":"finished"}),
    );
    assert_eq!(call(&host, &helper, &credential, observe)["exit"], 4);
}

#[test]
fn the_listener_wakes_the_delegate_once_and_cancelling_its_job_stops_the_grant() {
    let place = Place::new("listener-wake");
    let cli = place.root.join("stand-in");
    program(
        &cli,
        &format!("#!/bin/sh\nread -r line\n{STAND_IN_ANSWERS}"),
    );
    let boot = || {
        place.start_in(
            "",
            &[],
            &[
                ("HOME", place.root.to_str().unwrap()),
                ("CLARP_CLAUDE_PROGRAM", cli.to_str().unwrap()),
            ],
        )
    };
    let host = boot();
    assert_eq!(
        host.get("/server-info")["contract"]["features"]["goal_ledger"],
        52
    );
    host.post(
        "/agents",
        json!({"session":"principal","name":"Principal","backend":"claude"}),
    );
    host.post("/agents",json!({"session":"delegate","name":"Delegate","backend":"claude","parent":"principal","role":"helper"}));
    let route = "/goal-ledger/delegations";
    let grant = host.post(
        route,
        json!({"principal":"principal","delegate":"delegate","reason":"check"}),
    );
    assert_eq!(grant["listener"]["started"], true);
    let id = grant["delegation_id"].as_str().unwrap();
    let public =
        |host: &Host| host.get(&format!("{route}?session=principal"))["delegations"][0].clone();
    host.send(
        "principal",
        "Please keep this accounted.",
        "listener-source",
    );
    wait_for("the listener to dispatch the first wake", || {
        public(&host)["in_flight"] == true
    });
    let first = public(&host);
    let wake = first["last_wake_id"].as_str().unwrap();
    assert!(wake.starts_with(&format!("bookkeeping-{id}-m")));
    assert!(first["dispatched_message_through"].as_i64().unwrap() > 0);
    assert!(first["last_heartbeat_at"].is_i64());
    assert!(first["unapplied_since"].is_i64());
    let handle = first["job_handle"].as_str().unwrap();
    let job_id = clarp_kernel::jobs::parse_handle(handle).0;
    let message = || {
        host.conversation("delegate")
            .iter()
            .any(|(role, text)| role == "user" && text.contains(wake))
    };
    wait_for(
        "the delegate to receive the bookkeeping instructions",
        message,
    );
    assert!(
        host.turns("delegate")
            .iter()
            .any(|r| r["id"] == format!("u-{wake}"))
    );
    host.kill();
    let host = boot();
    assert_eq!(public(&host)["last_wake_id"], wake);
    assert_eq!(public(&host)["job_handle"], handle);
    assert_eq!(
        tool(
            &host,
            &place.root,
            None,
            &[
                "clarp-agent-bg",
                "delegate",
                "job-fail",
                handle,
                "stand-in heartbeat lost"
            ]
        )
        .0,
        0
    );
    let restarted = format!("bg1:2:{job_id}");
    wait_for(
        "a failed listener job to resume with the same id in its next generation",
        || public(&host)["job_handle"] == restarted,
    );
    assert_eq!(
        host.ask("DELETE", &format!("/background-jobs/{job_id}"), None)
            .0,
        200
    );
    wait_for("the listener cancellation to stop authority", || {
        public(&host)["status"] == "stopped"
    });
    assert_eq!(public(&host)["stopped_reason"], "listener job cancelled");
    assert_eq!(
        host.conversation("delegate")
            .iter()
            .filter(|(role, text)| role == "user" && text.contains(wake))
            .count(),
        1
    );
    let next = host.post(
        route,
        json!({"principal":"principal","delegate":"delegate","reason":"second pilot"}),
    );
    let next_id = next["delegation_id"].as_str().unwrap();
    let next_row = || {
        host.get(&format!("{route}?session=delegate"))["delegations"]
            .as_array()
            .unwrap()
            .iter()
            .find(|r| r["delegation_id"] == next_id)
            .unwrap()
            .clone()
    };
    wait_for("the second listener to register", || {
        !next_row()["job_handle"].as_str().unwrap_or("").is_empty()
    });
    let second_job = clarp_kernel::jobs::parse_handle(next_row()["job_handle"].as_str().unwrap()).0;
    host.post(
        "/goal-ledger/delegations/stop",
        json!({"delegation_id":next_id,"reason":"pilot ended"}),
    );
    wait_for("a stopped listener to finish its own job", || {
        host.get(&format!("/background-jobs/{second_job}"))["job"]["status"] == "succeeded"
    });
}
