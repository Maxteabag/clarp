#![allow(clippy::unwrap_used)]
use clarp_kernel::janitors::label_evidence::{evidence, selected};
use serde_json::{Value, json};
#[test]
fn reference_labels_are_selected_before_history_and_clipped_for_the_judge() {
    let cases: Vec<Value> =
        serde_json::from_str(include_str!("fixtures/label-evidence.json")).unwrap();
    for case in &cases {
        let now = case["now"].as_i64().unwrap();
        let facts = serde_json::from_value(case["candidates"].clone()).unwrap();
        let mut reads = Vec::new();
        let entries: Vec<_> = selected(facts, now)
            .into_iter()
            .map(|c| {
                reads.push(c.agent_id.clone());
                evidence(c, now, case["messages"].as_array().unwrap())
            })
            .collect();
        assert_eq!(json!(reads), case["reads"]);
        assert_eq!(json!(entries), case["result"]);
    }
    assert_eq!(cases.len(), 40);
}
