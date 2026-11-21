use clarp_kernel::{chip, hidden_coordination};
use serde_json::{Value, json};

#[test]
fn working_state_projections_match_the_python_host() {
    let cases: Vec<Value> = serde_json::from_str(include_str!(
        "fixtures/coordination-working-state-vectors.json"
    ))
    .unwrap();
    for case in &cases {
        let hides = case["hide"] == true;
        let headline = if hides {
            hidden_coordination::working_headline(case["name"].as_str())
        } else {
            String::new()
        };
        assert_eq!(json!(headline), case["headline"]);
        let mut activity = case["activity"].clone();
        hidden_coordination::veil_activity(&mut activity, &headline);
        assert_eq!(activity, case["veiled_activity"]);
        let mut live = case["live"].clone();
        hidden_coordination::veil_live(&mut live, &headline);
        assert_eq!(live, case["veiled_live"]);
        let mut detail = case["detail"].clone();
        hidden_coordination::veil_detail(&mut detail, &headline);
        assert_eq!(detail, case["veiled_detail"]);
        if hides {
            live["veiled"] = json!(true);
        }
        assert_eq!(json!(chip::headline(&live)), case["pushed_headline"]);
    }
    assert_eq!(cases.len(), 5);
}
