#![allow(clippy::unwrap_used)]
use clarp_kernel::goals::listener::{Activity, Decision, Pending, State};
use serde_json::{Value, json};

#[test]
fn bookkeeping_wakes_follow_the_reference_clock_and_both_cursors() {
    let fixture: Value = serde_json::from_str(include_str!("fixtures/goal-listener.json")).unwrap();
    for case in fixture["busy"].as_array().unwrap() {
        let why = clarp_kernel::goals::listener::busy_reason(
            case["queued"].as_bool().unwrap(),
            case["started"].as_i64(),
            case["now"].as_i64().unwrap(),
        );
        assert_eq!(case["why"], why, "{}", case["name"]);
    }
    let id = fixture["delegation_id"].as_str().unwrap();
    for case in fixture["cases"].as_array().unwrap() {
        let before = &case["before"];
        let mut state: State = serde_json::from_value(before.clone()).unwrap();
        let mut pending: Pending = serde_json::from_value(case["start"].clone()).unwrap();
        let now = case["now"].as_i64().unwrap();
        let activity = Activity {
            marker: (
                case["source"][0].as_i64().unwrap(),
                case["source"][1].as_i64().unwrap(),
            ),
            newest: case["source"][2].as_i64(),
            oldest: Some(1000),
        };
        let mut sent = vec![];
        let status = match pending.decide(
            &mut state,
            id,
            (
                before["message_through"].as_i64().unwrap(),
                before["goal_event_through"].as_i64().unwrap(),
            ),
            &activity,
            case["busy"].as_str().unwrap(),
            now,
        ) {
            Decision::Idle => "idle",
            Decision::Waiting => "waiting",
            Decision::Coalescing => "coalescing",
            Decision::Backoff => "backoff",
            Decision::Send(wake) => {
                sent.push(
                    json!({"session":"delegate","text":wake.text(id,"principal"),"wake":wake.id}),
                );
                let why = case["failure"].as_str().unwrap();
                if why.is_empty() {
                    pending.sent(&mut state, &wake, now);
                    if wake.retry { "retried" } else { "dispatched" }
                } else {
                    pending.failed(&mut state, why, now);
                    "error"
                }
            }
        };
        assert_eq!(case["outcome"], status, "{}", case["name"]);
        assert_eq!(case["end"], json!(pending));
        assert_eq!(case["sent"], json!(sent));
        let mut want = case["after"].clone();
        want.as_object_mut().unwrap().remove("unapplied_since");
        assert_eq!(want, json!(state));
    }
}
