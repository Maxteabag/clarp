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

/// Begins a move and waits until it is done or failed: its last state.
fn moved(host: &Host, session: &str, backend: &str, id: &str) -> Value {
    let ask = json!({"session": session, "backend": backend, "model": "", "request_id": id});
    let (status, began) = host.ask("POST", "/agent-transit", Some(&ask));
    assert_eq!(status, 202, "{began}");
    let transit = began["transit_id"].as_str().unwrap().to_string();
    let mut last = Value::Null;
    wait_for("the move to end", || {
        last = host.get(&format!(
            "/agent-transit?session={session}&transit_id={transit}"
        ));
        matches!(last["phase"].as_str(), Some("done" | "failed"))
    });
    last
}

#[test]
fn an_agent_moves_from_antigravity_over_acp_and_back_with_its_conversation() {
    let place = Place::new("agy-acp-move");
    let agy = place.root.join("agy");
    program(&agy, FAKE);
    let world = [
        ("HOME", place.root.to_str().unwrap()),
        ("CLARP_AGY_PROGRAM", agy.to_str().unwrap()),
    ];
    let host = place.start_in("", &[], &world);
    let options = host.get("/agent-model-options");
    assert_eq!(options["providers"]["agy-acp"]["runs_through"], "agy", "{options}");
    assert_eq!(options["providers"]["agy-acp"]["supports_steer"], false);
    let made = host.post("/agents", json!({"name": "Mover", "backend": "agy-acp"}));
    let session = made["session"].as_str().unwrap().to_string();
    host.send(&session, "remember the word lantern", "m-1");
    wait_for("the first turn to finish", || host.state(&session) == "done");

    // To Antigravity as it runs today: it has no writer, so its next turn would begin with the
    // Host's record. And back, the same way.
    let away = moved(&host, &session, "agy", "move-1");
    assert_eq!(away["phase"], "done", "{away}");
    assert_eq!(away["previous_backend"], "agy-acp");
    let back = moved(&host, &session, "agy-acp", "move-2");
    assert_eq!(back["phase"], "done", "{back}");
    assert_eq!(back["backend"], "agy-acp");

    // The next turn opens a new conversation that begins with what was said before.
    host.send(&session, "what was the word?", "m-2");
    wait_for("the turn after the move to finish", || {
        host.state(&session) == "done"
            && std::fs::read_to_string(place.root.join("acp.log"))
                .unwrap_or_default()
                .lines()
                .filter(|line| line.contains("session/prompt"))
                .count()
                == 2
    });
    let prompts: Vec<Value> = std::fs::read_to_string(place.root.join("acp.log"))
        .unwrap()
        .lines()
        .map(|line| serde_json::from_str::<Value>(line).unwrap())
        .filter(|message| message["method"] == "session/prompt")
        .collect();
    let text = prompts[1]["params"]["prompt"][0]["text"].as_str().unwrap();
    assert!(text.contains("remember the word lantern"), "{text}");
    assert!(text.contains("what was the word?"), "{text}");
    let opened = std::fs::read_to_string(place.root.join("acp.log"))
        .unwrap()
        .lines()
        .filter(|line| line.contains("\"session/new\""))
        .count();
    assert_eq!(opened, 2, "the move back begins a new conversation");
}

/// The names of the machine's environment a real Antigravity needs: its program, its home and
/// the session bus its keyring login is reached by.
const REAL_AGY: &[&str] = &[
    "HOME",
    "PATH",
    "USER",
    "LANG",
    "CLARP_AGY_PROGRAM",
    "XDG_CONFIG_HOME",
    "XDG_RUNTIME_DIR",
    "DBUS_SESSION_BUS_ADDRESS",
];

/// A real Antigravity turn over ACP through a disposable Host, with a test agent: it answers,
/// and the next turn continues its conversation.
///
/// It uses the machine's own agy login and its quota, so it runs only when asked, and only on a
/// machine whose Host is disposable (never a live Host):
/// `CLARP_TEST_AGY=1 cargo test --test host agy_acp -- --ignored`. Set `CLARP_AGY_PROGRAM` when
/// Antigravity is not `agy` on the path.
#[test]
#[ignore = "runs a real Antigravity turn: needs a signed-in agy and CLARP_TEST_AGY=1"]
fn a_real_antigravity_turn_over_acp_answers_and_is_continued() {
    if std::env::var("CLARP_TEST_AGY").as_deref() != Ok("1") {
        eprintln!("skipped: CLARP_TEST_AGY is not 1");
        return;
    }
    let place = Place::new("agy-acp-live");
    let host = place.start_on_the_machine(&[], REAL_AGY);
    let made = host.post(
        "/agents",
        json!({"name": "AcpLive", "backend": "agy-acp", "test": true}),
    );
    let session = made["session"].as_str().unwrap().to_string();
    host.send(
        &session,
        "Reply with exactly this word and nothing else: acp-ember",
        "live-1",
    );
    wait_long(&host, &session, "the first answer", || {
        host.state(&session) == "done"
    });
    let answered = |word: &str| {
        host.conversation(&session)
            .iter()
            .any(|(role, text)| role == "assistant" && text.to_lowercase().contains(word))
    };
    assert!(answered("acp-ember"), "{:?}", host.conversation(&session));
    host.send(
        &session,
        "Which word did you just reply with? Answer with that word only.",
        "live-2",
    );
    wait_long(&host, &session, "the second answer", || {
        host.state(&session) == "done"
            && host
                .conversation(&session)
                .iter()
                .filter(|(role, _)| role == "assistant")
                .count()
                >= 2
    });
    let replies: Vec<String> = host
        .conversation(&session)
        .into_iter()
        .filter(|(role, _)| role == "assistant")
        .map(|(_, text)| text)
        .collect();
    assert!(
        replies.last().is_some_and(|text| text.to_lowercase().contains("acp-ember")),
        "the second turn continued the conversation: {replies:?}"
    );
}
