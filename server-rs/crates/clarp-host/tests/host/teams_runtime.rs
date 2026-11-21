use super::*;

const CLAUDE: &str = r#"#!/usr/bin/env python3
import json
import pathlib
import sys

if "--version" in sys.argv:
    print("2.1.321 (Claude Code)")
    raise SystemExit(0)

root = pathlib.Path(__file__).resolve().parent
flag = "--resume" if "--resume" in sys.argv else "--session-id"
session = sys.argv[sys.argv.index(flag) + 1]
entry = json.loads(sys.stdin.readline())
prompt = entry["message"]["content"][0]["text"]
with (root / "received.jsonl").open("a") as rows:
    rows.write(json.dumps(prompt) + "\n")

def send(kind, **fields):
    print(json.dumps({"type": kind, "session_id": session, **fields}), flush=True)

send("system", subtype="init")
if "--replay-user-messages" in sys.argv:
    send("user", isReplay=True, uuid=entry["uuid"], message={"role":"user","content":[]})
if prompt.endswith("Publish updates"):
    response = "<speak>Visible result.</speak>" + "".join(
        f"<team><speak>Private update {i}</speak></team>" for i in range(1, 9)
    )
else:
    response = "Turn answered."
send("assistant", message={"id": "msg_" + entry["uuid"], "role":"assistant",
    "content":[{"type":"text","text":response}]})
send("result", subtype="success", is_error=False, result=response, user_message_uuids=[entry["uuid"]])
"#;

fn agent(host: &Host, name: &str) -> (String, String) {
    let made = host.post("/agents", json!({"name":name,"backend":"claude"}));
    let session = made["session"].as_str().unwrap();
    let roster = host.get("/agents/snapshot");
    let row = roster["agents"]
        .as_array()
        .unwrap()
        .iter()
        .find(|row| row["session"] == session)
        .unwrap();
    (
        session.to_owned(),
        row["agent_id"].as_str().unwrap().to_owned(),
    )
}

fn team(host: &Host, name: &str, communication_enabled: bool, members: &[&str]) -> String {
    let made = host.post("/teams", json!({"name":name}));
    let id = made["team"]["team_id"].as_str().unwrap();
    if communication_enabled {
        host.post(
            &format!("/teams/{id}"),
            json!({"communication_enabled":true}),
        );
    }
    for member in members {
        host.post(&format!("/teams/{id}/members"), json!({"agent_id":member}));
    }
    id.to_owned()
}

#[test]
fn teams_runtime_broadcasts_durable_team_blocks_and_acknowledges_the_exact_prompt_on_launch() {
    let place = Place::new("teams-feed");
    let shim = place.root.join("missing-shim");
    let tool = place.root.join("claude");
    program(&tool, CLAUDE);
    let world = [("CLARP_CLAUDE_PROGRAM", tool.to_str().unwrap())];
    let start = |args: &[&str]| place.start_in("", args, &world);
    let send = |host: &Host, session: &str, text: &str, id: &str| {
        host.post(
            "/send",
            json!({"session":session,"text":text,"client_msg_id":id,"synthesize_audio":false}),
        )
    };
    let received = || -> Vec<String> {
        std::fs::read_to_string(place.root.join("received.jsonl"))
            .unwrap_or_default()
            .lines()
            .map(|line| serde_json::from_str(line).unwrap())
            .collect()
    };
    let host = start(&[]);
    let (sender, sender_id) = agent(&host, "Worker");
    let (listener, listener_id) = agent(&host, "Listener");
    let (solo, _) = agent(&host, "Solo");
    let ops = team(&host, "Ops", true, &[&sender_id, &listener_id]);
    let silent = team(&host, "Silent", false, &[&sender_id, &listener_id]);
    let messages = |host: &Host| host.get(&format!("/teams/{ops}/messages"))["messages"].clone();
    send(&host, &sender, "Publish updates", "publishes");
    wait_for("the reply and broadcasts to commit", || {
        host.state(&sender) == "done"
            && messages(&host)
                .as_array()
                .is_some_and(|items| items.len() == 8)
    });
    let feed = messages(&host);
    assert_eq!(
        host.get(&format!("/teams/{silent}/messages"))["messages"],
        json!([])
    );
    assert!(feed.as_array().unwrap().iter().all(|row| {
        row["source_agent_id"] == sender_id
            && row["source_session"] == sender
            && row["text"]
                .as_str()
                .is_some_and(|text| text.starts_with("Private update "))
    }));
    let chat = host.conversation(&sender);
    assert_eq!(
        chat,
        [
            said("user", "Publish updates"),
            said("assistant", "<speak>Visible result.</speak>")
        ]
    );
    send(&host, &solo, "Solo input", "solo-input");
    wait_for("the solo turn", || host.state(&solo) == "done");
    assert!(received().iter().any(|prompt| prompt == "Solo input"));
    host.kill();

    let unavailable = start(&["--shim", shim.to_str().unwrap()]);
    let events = unavailable.listen();
    send(&unavailable, &listener, "Read feed", "listener-first");
    wait_for("the launch refusal", || {
        events.lock().unwrap().iter().any(|event| {
            event["session"] == listener && event["detail"]["action"] == "reconnecting"
        })
    });
    assert!(
        received()
            .iter()
            .all(|prompt| !prompt.ends_with("Read feed"))
    );
    unavailable.kill();

    let host = start(&[]);
    wait_for("the retried turn to receive its team brief", || {
        received()
            .iter()
            .any(|prompt| prompt.ends_with("Read feed"))
            && host.state(&listener) == "done"
    });
    let got = received();
    let injected = got
        .iter()
        .find(|prompt| prompt.ends_with("Read feed"))
        .unwrap();
    assert!(injected.starts_with("--- Clarp team context ---\nTeam feed: you are on Ops."));
    assert!(injected.contains("+3 earlier updates skipped"));
    for i in 1..=8 {
        assert_eq!(
            injected.contains(&format!("[Ops] Worker: Private update {i}")),
            i >= 4
        );
    }
    assert!(!injected.contains("Silent"));
    assert_eq!(messages(&host), feed);
    assert_eq!(
        host.conversation(&listener),
        [
            said("user", "Read feed"),
            said("assistant", "Turn answered.")
        ]
    );

    send(&host, &listener, "Next turn", "listener-next");
    wait_for("the next turn to run", || {
        received()
            .iter()
            .any(|prompt| prompt.ends_with("Next turn"))
    });
    let got = received();
    let next = got
        .iter()
        .find(|prompt| prompt.ends_with("Next turn"))
        .unwrap();
    assert!(next.contains("Team feed: you are on Ops."));
    assert!(!next.contains("Team updates since your last turn:"));
}
