#![allow(clippy::unwrap_used)]
use clarp_kernel::grok_muse::Tool;
use serde_json::{Value, json};

fn tool(row: &Value) -> Tool {
    match row["tool"].as_str().unwrap() {
        "grok" => Tool::Grok,
        "muse" => Tool::Muse,
        _ => panic!("unknown fixture backend"),
    }
}

#[test]
fn commands_and_answers_follow_the_python_backends() {
    let fixture: Value =
        serde_json::from_str(include_str!("fixtures/backend-grok-vectors.json")).unwrap();
    let mut commands = 0;
    for row in fixture["commands"].as_array().unwrap() {
        let model = row["model"].as_str().unwrap();
        let effort = row["effort"].as_str().unwrap();
        let argv = tool(row).arguments(
            row["session"].as_str().unwrap(),
            row["fresh"].as_bool().unwrap(),
            model,
            effort,
        );
        assert_eq!(json!(argv), row["argv"]);
        assert_eq!(
            json!(tool(row).question(model, effort, "--question")),
            row["once"]
        );
        commands += 1;
    }
    let mut answers = 0;
    for row in fixture["answers"].as_array().unwrap() {
        assert_eq!(
            tool(row).answer(row["printed"].as_str().unwrap()),
            row["answer"].as_str().unwrap()
        );
        answers += 1;
    }
    for row in fixture["histories"].as_array().unwrap() {
        let log = row["log"].as_str().unwrap();
        let turns = match tool(row) {
            Tool::Grok => clarp_kernel::grok_muse_history::grok(log),
            Tool::Muse => clarp_kernel::grok_muse_history::muse(log),
        };
        assert_eq!(json!(turns), row["turns"]);
    }
    assert_eq!((commands, answers), (32, 89));
}
