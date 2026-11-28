//! An agent on Antigravity over ACP (`agy-acp`), end to end: the Host launches its turns
//! through the shim's driver, the driver speaks ACP to a scripted stand-in for `agy acp`
//! (`clarp-shim/tests/fake_acp/agy.py`), and the answer lands in the conversation. The next
//! turn continues the conversation the first one opened. One file a piece, declared in
//! `host.rs`.

use super::*;

pub(super) const FAKE: &str = include_str!("../../../clarp-shim/tests/fake_acp/agy.py");

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

/// The mark the Host's own record of a conversation begins with, where a turn opens with it.
const RECORD: &str = "--- Clarp restored conversation ---";

/// A Host running the ACP peer in a home of the test's own.
fn open_antigravity(place: &Place) -> Host {
    let agy = place.root.join("agy");
    let world = [
        ("HOME", place.root.to_str().unwrap()),
        ("CLARP_AGY_PROGRAM", agy.to_str().unwrap()),
    ];
    place.start_in("", &[], &world)
}

/// The lines of a file the stand-in wrote, each a JSON object.
pub(super) fn written(place: &Place, file: &str) -> Vec<Value> {
    let contents = match std::fs::read_to_string(place.root.join(file)) {
        Ok(contents) => contents,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Vec::new(),
        Err(error) => panic!("reading {file}: {error}"),
    };
    // A concurrent append may be between writing the JSON and its terminating newline.
    contents
        .split_inclusive('\n')
        .filter(|line| line.ends_with('\n'))
        .map(|line| serde_json::from_str(line).unwrap())
        .collect()
}

/// What the stand-in was sent over ACP of one method.
pub(super) fn asked(place: &Place, method: &str) -> Vec<Value> {
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
pub(super) fn prompt_text(place: &Place, n: usize) -> String {
    asked(place, "session/prompt")[n]["params"]["prompt"][0]["text"]
        .as_str()
        .unwrap()
        .to_string()
}

/// Seed a stopped fleet with a legacy backend and a pinned model, keeping its real
/// journal and native conversation. Exercise each side of the durable switch.
fn legacy_fleet(name: &str, archived: bool) -> (Place, Host, String) {
    let place = Place::new(name);
    program(&place.root.join("agy"), FAKE);
    let host = open_antigravity(&place);
    let made = host.post("/agents", json!({"name":"Mover","backend":"agy"}));
    let session = made["session"].as_str().unwrap().to_string();
    let row = |host: &Host| {
        host.get("/agents/snapshot?include_archived=1")["agents"]
            .as_array()
            .unwrap()
            .iter()
            .find(|r| r["session"] == session)
            .unwrap()
            .clone()
    };
    assert_eq!(row(&host)["backend"], "agy-acp");
    host.send(&session, "remember the word lantern", "old-message");
    wait_for("the seed conversation", || host.state(&session) == "done");
    if archived {
        host.post("/agent-archive", json!({"session":session,"archived":true}));
    }
    let before = row(&host);
    assert!(
        before["conversation_id"]
            .as_str()
            .is_some_and(|id| !id.is_empty())
    );
    let history = host.conversation(&session);
    assert_eq!(before["backend_session_id"], "conv-new");
    assert_eq!(before["archived_at"].is_number(), archived);
    assert!(
        history
            .iter()
            .any(|(role, text)| role == "user" && text == "remember the word lantern")
    );
    assert!(
        place
            .root
            .join(".gemini/antigravity-cli/conversations/conv-new.db")
            .is_file()
    );
    host.kill();
    let id = before["agent_id"].as_str().unwrap();
    let mut record = kept_records(&place, "agent").remove(id).unwrap();
    record["backend"] = json!("agy");
    record["model"] = json!("claude-b");
    keep_record(&place, "agent", id, &record);
    std::fs::remove_file(place.root.join("acp.log")).unwrap();
    let migrated = |host: &Host| {
        wait_for("legacy fleet record to move", || {
            let lineage = host.get(&format!("/agent-helper-state?session={session}"));
            let current = row(host);
            lineage["backend"] == "agy-acp"
                && current["backend"] == "agy-acp"
                && current["busy"] == false
        });
    };
    // The catalog sees another model during startup. The same-store
    // retirement must still carry the exact selection to the ACP actor.
    let partial_models = place.root.join("partial-models");
    std::fs::write(&partial_models, "").unwrap();
    let host = open_antigravity(&place);
    migrated(&host);
    std::fs::remove_file(&partial_models).unwrap();
    let after = row(&host);
    for key in [
        "agent_id",
        "session",
        "conversation_id",
        "backend_session_id",
        "archived_at",
        "archived_by",
    ] {
        assert_eq!(before[key], after[key], "{key}");
    }
    let current = host.conversation(&session);
    assert!(current.starts_with(&history), "{current:?}");
    assert!(
        current.iter().any(
            |(role, text)| role == "system" && text.ends_with("The same conversation goes on.")
        ),
        "{current:?}"
    );
    if !archived {
        assert!(!place.root.join("print.log").exists());
        return (place, host, session);
    }

    // A crash can leave the agent journal switched while the fleet still names the old
    // backend. Reopen this precise combination and verify the repair is idempotent.
    host.kill();
    let switched = kept_records(&place, "agent").remove(id).unwrap();
    assert_eq!(switched["backend"], "agy-acp");
    assert_eq!(switched["model"], record["model"]);
    assert_eq!(switched["archived_at_ms"], record["archived_at_ms"]);
    assert_eq!(switched["archived_by"], record["archived_by"]);
    keep_record(&place, "agent", id, &record);
    let host = open_antigravity(&place);
    migrated(&host);
    assert_eq!(
        host.conversation(&session),
        current,
        "a replayed switch added a chat row"
    );
    let visible = host.get("/agents/snapshot?include_archived=0");
    assert!(
        !visible["agents"]
            .as_array()
            .unwrap()
            .iter()
            .any(|agent| agent["agent_id"] == id)
    );
    assert_eq!(row(&host)["archived_at"], before["archived_at"]);
    assert_eq!(
        row(&host)["backend_session_id"],
        before["backend_session_id"]
    );
    assert!(!place.root.join("print.log").exists());
    (place, host, session)
}

#[test]
fn an_archived_legacy_agent_resumes_its_own_conversation_over_acp() {
    let (place, host, session) = legacy_fleet("agy-same-store", true);
    // A Transit request using the retired spelling resolves to the current tool.
    let (status, refused) = host.ask(
        "POST",
        "/agent-transit",
        Some(&json!({
            "session":session, "backend":"agy", "model":"claude-b"
        })),
    );
    assert_eq!(status, 400, "{refused}");
    assert_eq!(refused["error"], "same_backend_and_model");
    host.post(
        "/agent-archive",
        json!({"session":session,"archived":false}),
    );
    let opened = asked(&place, "session/new").len();
    host.send(&session, "what was the word?", "m-2");
    wait_for("the resumed answer", || {
        asked(&place, "session/prompt").len() == 1 && idle(&host, &session)
    });
    assert_eq!(
        asked(&place, "session/resume")[0]["params"]["sessionId"],
        "conv-new"
    );
    assert_eq!(asked(&place, "session/new").len(), opened);
    assert!(asked(&place, "session/set_config_option").iter().any(
        |message| message["params"]["sessionId"] == "conv-new"
            && message["params"]["configId"] == "model"
            && message["params"]["value"] == "claude-b"
    ));
    let text = prompt_text(&place, 0);
    assert!(
        text.contains("what was the word?") && !text.contains(RECORD) && !text.contains("lantern"),
        "{text}"
    );
    assert_eq!(host.conversation(&session).last().unwrap().1, "Hello");
    let id = row_id(&host, &session);
    host.kill();
    let persisted = &kept_records(&place, "agent")[&id];
    assert_eq!(persisted["backend"], "agy-acp");
    assert_eq!(persisted["session"], session);
    assert_eq!(persisted["model"], "claude-b");
    assert!(!place.root.join("print.log").exists());
}

#[test]
fn a_conversation_lost_after_a_same_store_move_goes_on_with_the_hosts_record() {
    let (place, host, session) = legacy_fleet("agy-same-store-lost", false);
    let opened = asked(&place, "session/new").len();
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
    assert_eq!(asked(&place, "session/new").len(), opened + 1);
    assert_eq!(
        asked(&place, "session/prompt")[0]["params"]["sessionId"],
        "conv-replacement"
    );
    let text = prompt_text(&place, 0);
    assert!(
        text.contains(RECORD) && text.contains("remember the word lantern"),
        "{text}"
    );

    assert!(!place.root.join("print.log").exists());
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
    let ask = json!({
        "name": "Thinker", "backend": "agy-acp", "model": "gemini-4-neon-medium"
    });
    let (status, made) = host.ask("POST", "/agents", Some(&ask));
    assert_eq!(status, 200, "{made}");
    let options = host.get("/agent-model-options");
    assert!(options["providers"].get("agy").is_none());
    assert_eq!(options["providers"]["agy-acp"]["runs_through"], "agy");
    assert!(
        options["providers"]["agy-acp"]["models"]
            .as_array()
            .unwrap()
            .iter()
            .any(|model| model["id"] == "gemini-4-neon-medium")
    );
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
            json!(["model", "gemini-4-neon"]),
            json!(["thought_level", "medium"])
        ]
    );

    let (status, alias) = host.ask(
        "POST",
        "/agents",
        Some(&json!({
            "name":"Alias", "backend":"agy","model":"claude-b","effort":"high"
        })),
    );
    assert_eq!(status, 200, "{alias}");
    let snapshot = host.get("/agents/snapshot");
    for created in [&made, &alias] {
        let row = snapshot["agents"]
            .as_array()
            .unwrap()
            .iter()
            .find(|row| row["session"] == created["session"])
            .unwrap();
        assert_eq!(row["backend"], "agy-acp");
    }
}
