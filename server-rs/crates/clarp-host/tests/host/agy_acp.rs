//! An agent on Antigravity over ACP (`agy-acp`), end to end: the Host launches its turns
//! through the shim's driver, the driver speaks ACP to a scripted stand-in for `agy acp`
//! (`clarp-shim/tests/fake_acp/agy.py`), and the answer lands in the conversation. The next
//! turn continues the conversation the first one opened. One file a piece, declared in
//! `host.rs`.

use super::*;

const FAKE: &str = include_str!("../../../clarp-shim/tests/fake_acp/agy.py");

#[test]
fn an_agent_on_antigravity_over_acp_answers_and_continues_its_conversation() {
    let place = Place::new("agy-acp");
    let agy = place.root.join("agy");
    program(&agy, FAKE);
    let world = [
        ("HOME", place.root.to_str().unwrap()),
        ("CLARP_AGY_PROGRAM", agy.to_str().unwrap()),
    ];
    let host = place.start_in("", &[], &world);
    let made = host.post("/agents", json!({"name": "Ada", "backend": "agy-acp"}));
    let session = made["session"].as_str().unwrap().to_string();

    host.send(&session, "hello there", "m-1");
    wait_for("the first turn to finish", || {
        host.state(&session) == "done"
    });
    let conversation = host.conversation(&session);
    assert_eq!(
        conversation.last().unwrap(),
        &("assistant".to_string(), "Hello".to_string()),
        "{conversation:?}"
    );
    let sent = || -> Vec<Value> {
        std::fs::read_to_string(place.root.join("acp.log"))
            .unwrap_or_default()
            .lines()
            .map(|line| serde_json::from_str(line).unwrap())
            .collect()
    };
    let of = |method: &str| -> Vec<Value> {
        sent()
            .into_iter()
            .filter(|message| message["method"] == method)
            .collect()
    };
    assert_eq!(of("session/new").len(), 1);
    let blocks = &of("session/prompt")[0]["params"]["prompt"];
    assert!(
        blocks[0]["text"].as_str().unwrap().contains("hello there"),
        "{blocks}"
    );

    // The second turn continues the conversation Antigravity opened, without a new one.
    host.send(&session, "and again", "m-2");
    wait_for("the second turn to finish", || {
        host.state(&session) == "done" && of("session/prompt").len() == 2
    });
    assert_eq!(of("session/new").len(), 1);
    assert_eq!(of("session/resume")[0]["params"]["sessionId"], "conv-new");
    let replies: Vec<_> = host
        .conversation(&session)
        .into_iter()
        .filter(|(role, _)| role == "assistant")
        .collect();
    assert_eq!(replies.len(), 2, "{replies:?}");
}
