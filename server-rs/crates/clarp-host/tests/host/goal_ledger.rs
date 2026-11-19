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
