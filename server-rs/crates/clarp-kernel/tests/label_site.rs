#![allow(clippy::unwrap_used)]
use clarp_kernel::janitors::label_audit::{Evidence, JUDGE_SECONDS, batch};
use serde_json::{Value, json};
#[test]
fn one_request_carries_the_reference_questions_and_only_the_packets() {
    let cases: Vec<Value> = serde_json::from_str(include_str!("fixtures/label-site.json")).unwrap();
    for case in &cases {
        let entries: Vec<_> = case["entries"]
            .as_array()
            .unwrap()
            .iter()
            .map(|p| Evidence {
                agent_id: "internal".into(),
                name: "name".into(),
                idle_min: None,
                packet: p.clone(),
            })
            .collect();
        let actual: Vec<_> = batch(&entries)
            .into_iter()
            .map(|body| json!({"body":body,"seconds":JUDGE_SECONDS as f64}))
            .collect();
        assert_eq!(json!(actual), case["requests"]);
    }
    assert_eq!(cases.len(), 4);
}
