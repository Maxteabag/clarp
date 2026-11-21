#![allow(clippy::unwrap_used)]
use clarp_kernel::seat_switch::{Demand, Seat, codex_reset_quota_ready, decide, parse};
use serde_json::{Value, json};

#[test]
fn reads_and_choices_match_the_python_switcher() {
    let data: Value =
        serde_json::from_str(include_str!("fixtures/accounts-switch-vectors.json")).unwrap();
    for c in data["reads"].as_array().unwrap() {
        assert_eq!(
            json!(parse(c["provider"].as_str().unwrap(), &c["body"])),
            c["parsed"],
            "{c}"
        );
    }
    for c in data["decisions"].as_array().unwrap() {
        let accounts: Vec<Seat> = serde_json::from_value(c["accounts"].clone()).unwrap();
        let required: Vec<String> = serde_json::from_value(c["required_models"].clone()).unwrap();
        let running: Vec<String> = serde_json::from_value(c["active_models"].clone()).unwrap();
        let demand = Demand {
            floor: c["floor"].as_i64().unwrap(),
            required: &required,
            running: &running,
            last: &c["last_switch"],
            now: c["now_ms"].as_i64().unwrap(),
            cooldown: c["cooldown_ms"].as_i64().unwrap(),
        };
        assert_eq!(decide(&accounts, demand), c["result"], "{c}");
    }
    for c in data["reports"].as_array().unwrap() {
        let report = clarp_kernel::seat_switch::report(
            c["provider"].as_str().unwrap(),
            &c["decision"],
            c["mode"].as_str().unwrap(),
            c["switched"].as_bool().unwrap(),
        );
        assert_eq!(json!(report), c["expected"], "{c}");
    }
}

#[test]
fn codex_reset_quota_evidence_matches_the_python_switcher() {
    let data: Value =
        serde_json::from_str(include_str!("fixtures/accounts-switch-vectors.json")).unwrap();
    let cases = &data["codex_reset"];
    for case in cases["weekly"].as_array().unwrap() {
        let seats = parse("codex", &case["body"]);
        assert_eq!(json!(&seats), case["parsed"], "{}", case["name"]);
        let round_trip: Vec<Seat> = serde_json::from_value(case["parsed"].clone()).unwrap();
        assert_eq!(json!(round_trip), case["parsed"], "{}", case["name"]);
        assert_eq!(
            codex_reset_quota_ready(&seats, 1, Some(0)),
            case["eligible"].as_bool().unwrap(),
            "{}",
            case["name"]
        );
    }
    let seats = parse(
        "codex",
        &json!({"accounts": [{"alias": "saved", "usage": {
            "windows": [{"label": "Codex weekly", "used": 1}]
        }}]}),
    );
    for case in cases["ledger"].as_array().unwrap() {
        let cap: usize = serde_json::from_value(case["cap"].clone()).unwrap();
        let count: Option<usize> = serde_json::from_value(case["count"].clone()).unwrap();
        assert_eq!(
            codex_reset_quota_ready(&seats, cap, count),
            case["eligible"].as_bool().unwrap(),
            "{}: {}",
            case["name"],
            case["blocked"]
        );
    }
}
