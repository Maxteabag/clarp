use super::*;

const TOOL: &str = r#"#!/usr/bin/env python3
import json
import pathlib
import subprocess
import sys

if "--version" in sys.argv:
    print("2.1.321 (Claude Code)")
    raise SystemExit(0)

flag = "--resume" if "--resume" in sys.argv else "--session-id"
session = sys.argv[sys.argv.index(flag) + 1]
entry = json.loads(sys.stdin.readline())
prompt = entry["message"]["content"][0]["text"]
who = "leader" if prompt.endswith("Probe leader") else "worker"
root = pathlib.Path(__file__).resolve().parent

def send(kind, **fields):
    print(json.dumps({"type":kind,"session_id":session,**fields}),flush=True)

send("system",subtype="init")
if "--replay-user-messages" in sys.argv:
    send("user",isReplay=True,uuid=entry["uuid"],message={"role":"user","content":[]})
out = subprocess.run(
    ["__HOST__", "tool", "clarp-admin", "leader-decision", "search", "--question", ""],
    capture_output=True, text=True, timeout=10,
)
(root / (who + ".json")).write_text(json.dumps(
    {"prompt":prompt,"code":out.returncode,"stdout":out.stdout}
))
send("assistant",message={"id":"answer-"+entry["uuid"],"role":"assistant",
    "content":[{"type":"text","text":"Probe done."}]})
send("result",subtype="success",is_error=False,result="Probe done.",user_message_uuids=[entry["uuid"]])
"#;

#[test]
fn leader_memory_only_a_leaders_turn_gets_the_values_and_can_call_its_tool() {
    let place = Place::new("leader-memory-turn");
    let cfg = place.root.join("xdg");
    std::fs::create_dir_all(cfg.join("clarp")).unwrap();
    let file = cfg.join("clarp/user-values.md");
    let seed = "# Values\n\n## Current Value Model\nUse a scratch proof.\n\n## Recent Promotions\nA held promotion.";
    std::fs::write(&file, &seed).unwrap();
    let fake = place.root.join("claude");
    program(
        &fake,
        &TOOL.replace("__HOST__", env!("CARGO_BIN_EXE_clarp-host")),
    );
    let host = place.start_in(
        "",
        &[],
        &[
            ("XDG_CONFIG_HOME", cfg.to_str().unwrap()),
            ("CLARP_USER_VALUES_DOC", ""),
            ("CLARP_CLAUDE_PROGRAM", fake.to_str().unwrap()),
        ],
    );
    let new = |name: &str| {
        let made = host.post("/agents", json!({"name":name,"backend":"claude"}));
        let session = made["session"].as_str().unwrap().to_owned();
        let snapshot = host.get("/agents/snapshot");
        let row = snapshot["agents"]
            .as_array()
            .unwrap()
            .iter()
            .find(|r| r["session"] == session)
            .unwrap();
        (session, row["agent_id"].as_str().unwrap().to_owned())
    };
    let (leader, leader_id) = new("Leader");
    let (worker, worker_id) = new("Worker");
    let created = host.post("/teams", json!({"name":"Ops"}));
    let team = created["team"]["team_id"].as_str().unwrap();
    for id in [&leader_id, &worker_id] {
        host.post(&format!("/teams/{team}/members"), json!({"agent_id":id}));
    }
    host.post(
        &format!("/teams/{team}"),
        json!({"leader":leader_id,"communication_enabled":true}),
    );
    for (session, text) in [(&leader, "Probe leader"), (&worker, "Probe worker")] {
        host.post(
            "/send",
            json!({"session":session,"text":text,"synthesize_audio":false}),
        );
        wait_for("the stand-in turn to finish", || {
            host.state(session) == "done"
        });
    }

    let read = |who: &str| -> Value {
        serde_json::from_slice(&std::fs::read(place.root.join(format!("{who}.json"))).unwrap())
            .unwrap()
    };
    let lead = read("leader");
    let prompt = lead["prompt"].as_str().unwrap();
    assert!(prompt.contains("clarp-admin leader-decision search"));
    assert!(prompt.contains("## Compact User Values"));
    assert!(prompt.contains("Use a scratch proof."));
    assert!(!prompt.contains("A held promotion."));
    assert_eq!(lead["code"], 0);
    let memory: Value = serde_json::from_str(lead["stdout"].as_str().unwrap()).unwrap();
    assert_eq!(memory, json!({"decisions":[],"user_value_facts":[]}));
    let worker = read("worker");
    assert!(
        !worker["prompt"]
            .as_str()
            .unwrap()
            .contains("Use a scratch proof.")
    );
    assert_eq!(worker["code"], 1);
    assert_eq!(std::fs::read_to_string(&file).unwrap(), seed);
}
