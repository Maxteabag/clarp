//! A goal's wire fields, recorded from the Python Host's own mirror.

#![allow(clippy::unwrap_used)]

use clarp_kernel::agent_goal::Goal;
use serde_json::Value;

#[test]
fn a_codex_goal_projects_the_reference_hosts_status_counters_and_times() {
    let cases: Vec<Value> =
        serde_json::from_str(include_str!("fixtures/agent-goal-vectors.json")).unwrap();
    let mut previous = None;
    for case in cases {
        let next = Goal::from_codex(
            &case["raw"],
            previous.as_ref(),
            case["now"].as_i64().unwrap(),
        );
        let public = next.as_ref().map_or(Value::Null, Goal::public);
        assert_eq!(public, case["public"], "{}", case["raw"]);
        if next.is_some() {
            previous = next;
        }
    }
}
