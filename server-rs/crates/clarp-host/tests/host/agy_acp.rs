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

/// The mark the Host's own record of a conversation begins with, where a turn opens with it.
const RECORD: &str = "--- Clarp restored conversation ---";

/// A Host whose Antigravity is the stand-in, for both cards, in a home of the test's own.
fn antigravity(name: &str) -> (Place, Host) {
    let place = Place::new(name);
    let agy = place.root.join("agy");
    program(&agy, FAKE);
    let world = [
        ("HOME", place.root.to_str().unwrap()),
        ("CLARP_AGY_PROGRAM", agy.to_str().unwrap()),
    ];
    let host = place.start_in("", &[], &world);
    (place, host)
}

/// The lines of a file the stand-in wrote, each a JSON object.
fn written(place: &Place, file: &str) -> Vec<Value> {
    std::fs::read_to_string(place.root.join(file))
        .unwrap_or_default()
        .lines()
        .map(|line| serde_json::from_str(line).unwrap())
        .collect()
}

/// What the stand-in was sent over ACP of one method.
fn asked(place: &Place, method: &str) -> Vec<Value> {
    written(place, "acp.log")
        .into_iter()
        .filter(|message| message["method"] == method)
        .collect()
}

fn idle(host: &Host, session: &str) -> bool {
    host.get("/agents/snapshot")["agents"]
        .as_array()
        .unwrap()
        .iter()
        .find(|row| row["session"] == session)
        .is_some_and(|row| row["busy"] == false)
}

/// The text of the `n`th prompt the stand-in was sent over ACP.
fn prompt_text(place: &Place, n: usize) -> String {
    asked(place, "session/prompt")[n]["params"]["prompt"][0]["text"]
        .as_str()
        .unwrap()
        .to_string()
}

#[test]
fn a_move_between_antigravity_and_its_acp_card_goes_on_in_the_same_conversation() {
    let (place, host) = antigravity("agy-same-store");
    let options = host.get("/agent-model-options");
    assert_eq!(
        options["providers"]["agy-acp"]["runs_through"], "agy",
        "{options}"
    );
    assert_eq!(options["providers"]["agy-acp"]["supports_steer"], false);
    let made = host.post("/agents", json!({"name": "Mover", "backend": "agy"}));
    let session = made["session"].as_str().unwrap().to_string();
    host.send(&session, "remember the word lantern", "m-1");
    wait_for("the turn on agy", || {
        written(&place, "print.log").len() == 1 && idle(&host, &session)
    });

    // To the ACP card: the same store, so nothing is carried and the conversation is kept.
    let there = moved(&host, &session, "agy-acp", "move-1");
    assert_eq!(there["phase"], "done", "{there}");
    assert_eq!(there["carried"]["turns"], 0, "{there}");
    assert!(
        host.conversation(&session).iter().any(
            |(role, text)| role == "system" && text.ends_with("The same conversation goes on.")
        ),
        "{:?}",
        host.conversation(&session)
    );
    host.send(&session, "what was the word?", "m-2");
    wait_for("the turn over ACP", || {
        asked(&place, "session/prompt").len() == 1 && idle(&host, &session)
    });
    // It resumes agy's own conversation, natively, with no record of the Host's at its head.
    assert_eq!(
        asked(&place, "session/resume")[0]["params"]["sessionId"],
        "conv-print"
    );
    assert!(asked(&place, "session/new").is_empty());
    let text = prompt_text(&place, 0);
    assert!(
        !text.contains(RECORD) && !text.contains("lantern"),
        "{text}"
    );

    // And back: agy continues the same conversation by its id.
    let back = moved(&host, &session, "agy", "move-2");
    assert_eq!(back["phase"], "done", "{back}");
    assert_eq!(back["carried"]["turns"], 0, "{back}");
    host.send(&session, "and once more", "m-3");
    wait_for("the turn on agy again", || {
        written(&place, "print.log").len() == 2 && idle(&host, &session)
    });
    let second = &written(&place, "print.log")[1];
    assert_eq!(second["conversation"], "conv-print", "{second}");
    assert!(
        !second["prompt"].as_str().unwrap().contains(RECORD),
        "{second}"
    );
}

#[test]
fn a_conversation_lost_after_a_same_store_move_goes_on_with_the_hosts_record() {
    // From agy to the ACP card, where agy answers that it no longer knows the conversation: that
    // turn fails and marks it, and the next one opens a new conversation with the Host's record.
    let (place, host) = antigravity("agy-same-store-lost");
    let made = host.post("/agents", json!({"name": "Forgetful", "backend": "agy"}));
    let session = made["session"].as_str().unwrap().to_string();
    host.send(&session, "remember the word lantern", "m-1");
    wait_for("the turn on agy", || {
        written(&place, "print.log").len() == 1 && idle(&host, &session)
    });
    assert_eq!(moved(&host, &session, "agy-acp", "move-1")["phase"], "done");
    std::fs::write(place.root.join("scenario"), "resume-lost").unwrap();
    host.send(&session, "what was the word?", "m-2");
    wait_for("the resume agy refuses", || {
        !asked(&place, "session/resume").is_empty() && idle(&host, &session)
    });
    if asked(&place, "session/prompt").is_empty() {
        host.send(&session, "the word, please", "m-3");
    }
    wait_for("a turn in a new conversation", || {
        asked(&place, "session/prompt").len() == 1 && idle(&host, &session)
    });
    assert_eq!(asked(&place, "session/new").len(), 1);
    let text = prompt_text(&place, 0);
    assert!(
        text.contains(RECORD) && text.contains("remember the word lantern"),
        "{text}"
    );

    // From the ACP card to agy, where agy has let the conversation go: agy is not told its id,
    // and its turn opens with the Host's record.
    let (place, host) = antigravity("agy-same-store-lost-back");
    let made = host.post(
        "/agents",
        json!({"name": "Forgetful", "backend": "agy-acp"}),
    );
    let session = made["session"].as_str().unwrap().to_string();
    host.send(&session, "remember the word lantern", "m-1");
    wait_for("the turn over ACP", || {
        asked(&place, "session/prompt").len() == 1 && idle(&host, &session)
    });
    assert_eq!(moved(&host, &session, "agy", "move-1")["phase"], "done");
    let kept = place
        .root
        .join(".gemini/antigravity-cli/conversations/conv-new.db");
    std::fs::remove_file(&kept).unwrap();
    host.send(&session, "what was the word?", "m-2");
    wait_for("the turn on agy", || {
        written(&place, "print.log").len() == 1 && idle(&host, &session)
    });
    let turn = &written(&place, "print.log")[0];
    assert_eq!(turn["conversation"], false, "{turn}");
    let text = turn["prompt"].as_str().unwrap();
    assert!(
        text.contains(RECORD) && text.contains("remember the word lantern"),
        "{text}"
    );
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
        replies
            .last()
            .is_some_and(|text| text.to_lowercase().contains("acp-ember")),
        "the second turn continued the conversation: {replies:?}"
    );
}

#[test]
fn an_agent_on_antigravity_over_acp_is_made_with_a_model_and_an_effort() {
    let place = Place::new("agy-acp-effort");
    let agy = place.root.join("agy");
    program(&agy, FAKE);
    let world = [
        ("HOME", place.root.to_str().unwrap()),
        ("CLARP_AGY_PROGRAM", agy.to_str().unwrap()),
    ];
    let host = place.start_in("", &[], &world);
    let ask =
        json!({"name": "Thinker", "backend": "agy-acp", "model": "claude-b", "effort": "high"});
    let (status, made) = host.ask("POST", "/agents", Some(&ask));
    assert_eq!(status, 200, "{made}");
    let session = made["session"].as_str().unwrap().to_string();
    host.send(&session, "think hard", "m-1");
    wait_for("the turn to finish", || host.state(&session) == "done");
    let set: Vec<Value> = std::fs::read_to_string(place.root.join("acp.log"))
        .unwrap()
        .lines()
        .map(|line| serde_json::from_str::<Value>(line).unwrap())
        .filter(|message| message["method"] == "session/set_config_option")
        .map(|message| json!([message["params"]["configId"], message["params"]["value"]]))
        .collect();
    assert_eq!(
        set,
        [
            json!(["model", "claude-b"]),
            json!(["thought_level", "high"])
        ]
    );

    // The `agy` card folds an effort into its model's name, and still refuses both apart.
    let (status, refused) = host.ask(
        "POST",
        "/agents",
        Some(&json!({"name": "Folded", "backend": "agy", "model": "gemini-a-high", "effort": "high"})),
    );
    assert_eq!(status, 400, "{refused}");
    assert!(
        refused.to_string().contains("effort compatibility"),
        "{refused}"
    );
}
