#![allow(clippy::unwrap_used)]
use clarp_kernel::janitors::{
    adaptive,
    label_audit::{self, Evidence},
    quota::Lane,
};
use serde_json::{Value, json};
#[test]
fn consecutive_checks_keep_the_reference_outcome_and_interval() {
    let cases: Vec<Value> = serde_json::from_str(include_str!("fixtures/label-lane.json")).unwrap();
    let mut lane = Lane::default();
    let mut previous = String::new();
    let mut runs = 0;
    let mut now = 1_000_000;
    let entries = [Evidence {
        agent_id: "sample".into(),
        name: "Example".into(),
        packet: json!({"label":"Reviewing"}),
        idle_min: Some(100),
    }];
    for (i, c) in cases.iter().enumerate() {
        let bounds = (3600, 86400);
        lane.start(bounds, now);
        let choice = c["choice"].as_str().unwrap();
        let (activity, summary) = if i == 0 {
            (adaptive::IDLE, "Nothing shown as working".into())
        } else {
            runs += 1;
            if choice == "timeout" {
                (
                    adaptive::FAILED,
                    "Jev did not answer; nothing reported".into(),
                )
            } else {
                let found = label_audit::findings(
                    &entries,
                    &json!({"l_a1":{"choice":choice,"probabilities":{(choice):1}}}),
                )
                .unwrap();
                let (mark, doc) =
                    label_audit::report("checker", &i.to_string(), 1, &found, &previous);
                previous = mark;
                (
                    if doc.is_some() {
                        adaptive::WORKED
                    } else {
                        adaptive::IDLE
                    },
                    label_audit::summary(1, found.len(), doc.is_some()),
                )
            }
        };
        lane.record(bounds, (activity, &summary), now);
        assert_eq!(json!(activity), c["activity"]);
        assert_eq!(summary, c["summary"]);
        assert_eq!(json!(runs), c["runs"]);
        let next = lane.next_run(bounds.0);
        assert_eq!(json!((next - now) / 1000), c["interval"]);
        now = next;
    }
    assert_eq!(cases.len(), 6);
}
