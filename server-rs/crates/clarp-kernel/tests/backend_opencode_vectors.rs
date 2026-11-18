#![allow(clippy::unwrap_used)]
use clarp_kernel::opencode;
use serde_json::{Value, json};

#[test]
fn opencode_and_deepseek_use_the_recorded_command_and_event_rules() {
    let cases: Value =
        serde_json::from_str(include_str!("fixtures/backend-opencode-vectors.json")).unwrap();
    let mut count = 0;
    for row in cases["commands"].as_array().unwrap() {
        let args = opencode::command(
            row[0].as_str().unwrap(),
            row[1].as_str().unwrap(),
            row[2].as_str().unwrap(),
            row[3].as_bool().unwrap(),
        );
        let mut argv = vec![String::from("opencode")];
        argv.extend(args);
        assert_eq!(json!(argv), row[4]);
        count += 1;
    }
    for row in cases["permissions"].as_array().unwrap() {
        assert_eq!(opencode::permissions(row[0].as_str().unwrap()), row[1]);
        count += 1;
    }
    for row in cases["models"].as_array().unwrap() {
        assert_eq!(
            json!(opencode::model_pair(row[0].as_str().unwrap())),
            row[1]
        );
        count += 1;
    }
    for row in cases["texts"].as_array().unwrap() {
        assert_eq!(opencode::answer(&row[0].to_string()), row[1]);
        count += 1;
    }
    for row in cases["sessions"].as_array().unwrap() {
        assert_eq!(opencode::session(&row[0]).unwrap_or_default(), row[1]);
        count += 1;
    }
    for row in cases["listings"].as_array().unwrap() {
        for (deepseek, column) in [(false, 1usize), (true, 2usize)] {
            assert_eq!(
                json!(opencode::models(row[0].as_str().unwrap(), deepseek, "at")),
                row[column]
            );
            count += 1;
        }
    }
    assert_eq!(count, 180);
}
