#![allow(clippy::unwrap_used)]
use clarp_kernel::seat_switch::{Demand, Seat, decide, parse};
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
}
