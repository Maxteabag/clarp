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
    assert_eq!(row["listener"]["started"], false);
    assert_eq!(row["listener"]["error"], "bookkeeping listener unavailable");
    let oracle: Value = serde_json::from_str(include_str!(
        "../../../clarp-kernel/tests/fixtures/goal-delegation.json"
    ))
    .unwrap();
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
        assert_eq!(records["delegations"][0], expected("active", &Value::Null));
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
    let fork = host.ask("POST", "/agents/child/fork", Some(&json!({})));
    assert_eq!(
        fork,
        (
            409,
            json!({"error":"a bookkeeping delegate does not start helpers or forks"})
        )
    );
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
        host.get(&format!("{route}?session=principal"))["delegations"][0],
        expected("stopped", &stopped["stopped_at"])
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
