#![allow(clippy::unwrap_used)]
use clarp_kernel::goals::{
    GoalBook, Owner,
    ledger::{self, Author},
};
use serde_json::{Value, json};

#[test]
fn history_changes_match_the_reference_ledger() {
    let table: Value =
        serde_json::from_str(include_str!("fixtures/goal-ledger-vectors.json")).unwrap();
    let mut count = 0;
    for story in table["stories"].as_array().unwrap() {
        let id = story["identity"].as_str().unwrap();
        let verified = story["verified"].as_bool().unwrap();
        let author = match story["author"].as_str().unwrap() {
            "owner" => Author::Owner(verified.then_some(id)),
            "user" => Author::User,
            "system" => Author::Keeper(id),
            "peer" => Author::Peer(id, verified),
            "unattributed" => Author::Unattributed,
            _ => panic!("unknown fixture actor"),
        };
        let mut prior = None;
        let mut history = Vec::new();
        for step in story["steps"].as_array().unwrap() {
            let mut goal = step["fields"].clone();
            history.push(step["entry"].clone());
            goal["history"] = json!(history);
            let row = json!({"plan_id":story["plan"],"agent_id":"a","session":"one","status":step["prior_status"],"goal":goal});
            let book = GoalBook::new();
            let plan = book
                .taken_over(
                    Owner {
                        agent_id: "a",
                        session: "one",
                    },
                    &row,
                    &[],
                )
                .unwrap()
                .plan;
            if let Some(p) = &mut prior {
                let p: &mut clarp_kernel::goals::Plan = p;
                p.status = step["prior_status"].as_str().unwrap().into();
            }
            let made = ledger::changes(prior.as_ref(), &plan, author);
            assert_eq!(made.len(), 1);
            let (index, event) = &made[0];
            assert_eq!(*index as usize, history.len() - 1);
            let mut expected = step["event"].clone();
            expected["event_id"] = json!(0); // allocated by the log, tested at the storage port.
            assert_eq!(json!(event), expected);
            assert!(ledger::changes(Some(&plan), &plan, author).is_empty());
            prior = Some(plan);
            count += 1;
        }
    }
    assert_eq!(count, 105);
}

#[test]
fn a_delegate_cursor_is_capped_by_its_principals_own_events() {
    let v: Value = serde_json::from_str(include_str!("fixtures/goal-ledger-vectors.json")).unwrap();
    let c = &v["cap"];
    let high = c["principal_high"].as_i64().unwrap();
    assert!(c["other_high"].as_i64().unwrap() > high);
    assert_eq!(c["reference"], c["other_high"]);
    assert_eq!(
        ledger::applied_through(0, c["requested"].as_i64().unwrap(), high),
        high
    );
    assert_eq!(ledger::applied_through(4, 2, high), 4);
}
