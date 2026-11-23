//! The Host's tests of a question card an agent asks its user with. One file a piece,
//! declared in `host.rs`.

use super::*;

#[test]
fn a_question_can_offer_only_the_listed_options() {
    let place = Place::new("question-no-custom");
    let host = place.start();
    let session = host.new_agent("Asker");
    let options = r#"[{"id":"a","label":"First"},{"id":"b","label":"Second"}]"#;
    let question = |flag: &str| {
        tool(
            &host,
            &place.root,
            None,
            &[
                "clarp-agent-artifacts",
                "question",
                &session,
                "Choose",
                "Which?",
                options,
                flag,
            ],
        )
    };
    let (exit, body, stderr) = question("--dry-run");
    assert_eq!(exit, 0, "{stderr}");
    let dry: Value = serde_json::from_str(&body).unwrap();
    assert_eq!(dry["body"]["allow_custom_text"], true);

    let (exit, _, stderr) = question("--no-custom=true");
    assert_ne!(exit, 0);
    assert_eq!(
        stderr.trim(),
        "agent_artifacts: argument --no-custom: ignored explicit argument 'true'"
    );

    let (exit, body, stderr) = question("--no-custom");
    assert_eq!(exit, 0, "{stderr}");
    let artifact: Value = serde_json::from_str(&body).unwrap();
    assert_eq!(artifact["decision"]["allow_custom_text"], false);
    let id = artifact["decision"]["decision_id"].as_str().unwrap();
    let resolve = format!("/decisions/{id}/resolve");
    let (status, refusal) = host.ask(
        "POST",
        &resolve,
        Some(&json!({"answer":{"text":"Something else"}, "expected_revision":1})),
    );
    assert_eq!(status, 409, "{refusal}");
    assert_eq!(refusal["error"], "custom text answers are not enabled");
    let (status, selected) = host.ask(
        "POST",
        &resolve,
        Some(&json!({"answer":{"option_id":"b"}, "expected_revision":1})),
    );
    assert_eq!(status, 200, "{selected}");
    assert_eq!(
        selected["artifact"]["decision"]["answer"]["label"],
        "Second"
    );
}
