#![allow(clippy::unwrap_used)]
use clarp_kernel::goals::{GoalBook, Owner};
use serde_json::{Value, json};

#[test]
fn the_owners_subgoals_match_the_reference_commands_and_events() {
    let fixture: Value = serde_json::from_str(include_str!("fixtures/goal-subgoals.json")).unwrap();
    let pid = fixture["plan"].as_str().unwrap();
    let mut book = GoalBook::new();
    let owner = Owner {
        agent_id: "a",
        session: "owner",
    };
    let plan = json!({"agent_id":"a","plan_id":pid,"status":"active","revision":0,"goal":{"outcome":"Deliver"}});
    let change = book.taken_over(owner, &plan, &[]).unwrap();
    book.apply(&book.record(&change, 100));
    let plain = json!({"agent_id":"a","plan_id":fixture["plain"],"status":"active","revision":1});
    let change = book.taken_over(owner, &plain, &[]).unwrap();
    book.apply(&book.record(&change, 100));
    let mut count = 0;
    for (i, case) in fixture["cases"].as_array().unwrap().iter().enumerate() {
        let who = &case["actor"];
        let at = case["at"].as_i64().unwrap();
        let result = book.owner_subgoal(
            case["plan"].as_str().unwrap(),
            case["action"].as_str().unwrap(),
            &case["data"],
            who["agent_id"].as_str().filter(|_| who["verified"] == true),
            at,
        );
        if let Some(error) = case["error"].as_str() {
            let why = result.unwrap_err();
            assert_eq!(
                i64::from(why.tool_exit()),
                case["exit"].as_i64().unwrap(),
                "case {i}"
            );
            assert_eq!(why.to_string(), error, "case {i}");
            continue;
        }
        let (change, event) = result.unwrap();
        count += 1;
        assert_eq!(change.plan.event_count, count);
        assert_eq!(change.plan.revision, 0);
        assert_eq!(event.event_id, 0, "the actor assigns the ID at commit");
        let mut recorded = json!(event);
        let mut expected = case["events"][0].clone();
        expected.as_object_mut().unwrap().remove("actor_session");
        recorded["event_id"] = expected["event_id"].clone();
        assert_eq!(recorded, expected, "case {i}");
        let record = book.record(&change, at);
        assert_eq!(record.subgoals.len(), 1);
        book.apply(&record);
        assert_eq!(
            book.get(pid, at).unwrap()["subgoals"],
            case["answer"]["subgoals"],
            "case {i}"
        );
    }
}

#[test]
fn an_invalid_import_cannot_leave_two_subgoals_with_the_same_id() {
    let fixture: Value = serde_json::from_str(include_str!("fixtures/goal-subgoals.json")).unwrap();
    let row = fixture["cases"]
        .as_array()
        .unwrap()
        .iter()
        .find_map(|r| r["answer"]["subgoals"].as_array())
        .unwrap()[0]
        .clone();
    let owner = Owner {
        agent_id: "a",
        session: "owner",
    };
    let book = GoalBook::new();
    let taken = |rows: Value| {
        book.taken_over(owner,&json!({"agent_id":"a","plan_id":"a:durable","status":"active","goal":{"outcome":"Deliver"},"subgoals":rows}),&[])
    };
    assert_eq!(
        taken(json!([row.clone(), row.clone()])).unwrap_err().0,
        "invalid imported subgoals"
    );
    for (key, bad) in [
        ("revision", json!(0)),
        ("revision", json!(i64::MAX)),
        ("created_at", json!(null)),
        ("created_by", json!({})),
        ("evidence", json!("x")),
        ("subgoal_id", json!("")),
    ] {
        let mut wrong = row.clone();
        wrong[key] = bad;
        assert_eq!(
            taken(json!([wrong])).unwrap_err().0,
            "invalid imported subgoals",
            "{key}"
        );
    }
    let mut second = row.clone();
    second["created_at"] = json!(0);
    second["subgoal_id"] = json!("early");
    let plan = taken(json!([row, second])).unwrap().plan;
    assert_eq!(plan.subgoals[0]["subgoal_id"], "early");
}
