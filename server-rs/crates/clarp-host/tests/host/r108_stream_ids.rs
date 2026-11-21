//! A native session is read before its new agent starts a turn.
use super::*;

#[test]
fn codex_seed_import_keeps_repeated_provider_items_as_distinct_rows() {
    let place = Place::new("r108-codex");
    let home = place.root.join("home");
    let cwd = place.root.join("work");
    let codex = home.join(".codex");
    let day = codex.join("sessions/2027/02/03");
    std::fs::create_dir_all(&day).unwrap();
    let kept = "0199c0de-1111-7222-8333-444455556666";
    let story = [
        json!({"type":"session_meta","payload":{"id":kept,"cwd":cwd}}),
        json!({"type":"event_msg","payload":{"type":"agent_message","message":"First."}}),
        json!({"type":"item.completed","item":{"type":"agent_message","id":"item_0","text":"First."}}),
        json!({"type":"event_msg","payload":{"type":"user_message","message":"Go on."}}),
        json!({"type":"item.completed","item":{"type":"agent_message","id":"item_0","text":"Second."}}),
    ];
    let jsonl = story
        .iter()
        .map(Value::to_string)
        .collect::<Vec<_>>()
        .join("\n");
    std::fs::write(
        day.join(format!("rollout-2027-02-03T10-00-00-{kept}.jsonl")),
        jsonl + "\n",
    )
    .unwrap();

    let cache = home.join(".cache");
    let config = home.join(".config");
    let data = home.join(".local/share");
    let state = home.join(".local/state");
    let absent = home.join("no-codex");
    let world = [
        ("HOME", home.to_str().unwrap()),
        ("CODEX_HOME", codex.to_str().unwrap()),
        ("XDG_CACHE_HOME", cache.to_str().unwrap()),
        ("XDG_CONFIG_HOME", config.to_str().unwrap()),
        ("XDG_DATA_HOME", data.to_str().unwrap()),
        ("XDG_STATE_HOME", state.to_str().unwrap()),
        ("CLARP_CODEX_PROGRAM", absent.to_str().unwrap()),
    ];
    let host = place.start_in("", &[], &world);
    let made = host.post(
        "/agents",
        json!({"name":"Cody","backend":"codex","cwd":cwd,"resume_session_id":kept}),
    );
    let rows = host.turns(made["session"].as_str().unwrap());
    let text: Vec<_> = rows
        .iter()
        .map(|row| row["text"].as_str().unwrap())
        .collect();
    assert_eq!(text, ["First.", "Go on.", "Second."]);
    let ids: std::collections::BTreeSet<_> =
        rows.iter().map(|row| row["id"].as_str().unwrap()).collect();
    assert_eq!(ids.len(), 3);
    assert!(
        ids.iter()
            .all(|id| id.starts_with("msg-") && id.len() == 24),
        "{ids:?}"
    );
}

const CLAUDE_BLOCKS: &str = r#"#!/usr/bin/env python3
import json
import os
from pathlib import Path
import time

def emit(body):
    print(json.dumps(body), flush=True)

def stream(kind, **fields):
    emit({"type": "stream_event", "event": {"type": kind, **fields}})

def begin(key):
    stream("message_start", message={"id": key, "role": "assistant"})

def part(key, index, content, delta=None):
    stream("content_block_start", index=index, content_block=content)
    whole = content.copy()
    if delta:
        stream("content_block_delta", index=index, delta=delta)
        whole["text"] += delta["text"]
    emit({"type": "assistant", "message": {"id": key, "content": [whole]}})
    stream("content_block_stop", index=index)

emit({"type": "system", "subtype": "init", "session_id": "fixture-claude"})
begin("msg_single")
part("msg_single", 0, {"type":"text","text":"Single."})
stream("message_stop")
begin("msg_grouped")
part("msg_grouped", 0, {"type":"text","text":""}, {"type":"text_delta","text":"First."})
def wait_for_release(name):
    flag = Path(os.environ[name])
    until = time.monotonic() + 20
    while not flag.exists() and time.monotonic() < until:
        time.sleep(0.02)
wait_for_release("R108_RELEASE")
part("msg_grouped", 1, {"type":"tool_use","id":"toolu_read","name":"Read","input":{"file_path":"/fixture"}})
part("msg_grouped", 2, {"type":"thinking","thinking":"A model thought."})
part("msg_grouped", 3, {"type":"text","text":""}, {"type":"text_delta","text":"Second."})
part("msg_grouped", 4, {"type":"server_tool_use","id":"srvtoolu_search","name":"WebSearch","input":{}})
part("msg_grouped", 5, {"type":"web_search_tool_result","tool_use_id":"srvtoolu_search","content":[]})
part("msg_grouped", 6, {"type":"text","text":"Third."})
stream("message_stop")
wait_for_release("R108_FINISH")
emit({"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"toolu_read","content":"fixture"}]}})
emit({"type":"result","subtype":"success","result":"Third.","is_error":False})
"#;

#[test]
fn claude_text_blocks_grow_one_item_and_one_durable_reply() {
    let place = Place::new("r108-claude");
    let home = place.root.join("home");
    let cache = home.join(".cache");
    let config = home.join(".config");
    let data = home.join(".local/share");
    let state = home.join(".local/state");
    let claude = place.root.join("claude");
    program(&claude, CLAUDE_BLOCKS);
    let release = place.root.join("continue");
    let finish = place.root.join("finish");
    let world = [
        ("HOME", home.to_str().unwrap()),
        ("XDG_CACHE_HOME", cache.to_str().unwrap()),
        ("XDG_CONFIG_HOME", config.to_str().unwrap()),
        ("XDG_DATA_HOME", data.to_str().unwrap()),
        ("XDG_STATE_HOME", state.to_str().unwrap()),
        ("CLARP_CLAUDE_PROGRAM", claude.to_str().unwrap()),
        ("R108_RELEASE", release.to_str().unwrap()),
        ("R108_FINISH", finish.to_str().unwrap()),
    ];
    let host = place.start_in("", &[], &world);
    let made = host.post("/agents", json!({"name":"Claude","backend":"claude"}));
    let agent = made["session"].as_str().unwrap();
    let heard = host.listen_at(&format!("/events?live={agent}"));
    wait_for("the live subscription is open", || {
        !heard.lock().unwrap().is_empty()
    });
    let shown = || host.get(&format!("/live?session={agent}"));
    let item = |view: &Value, row: &str| {
        view["items"]
            .as_array()
            .unwrap()
            .iter()
            .find(|item| item["row_id"] == row)
            .cloned()
    };
    host.send(agent, "A fixture turn", "r108");
    wait_for("the first block is visible", || {
        item(&shown(), "r-grouped").is_some_and(|item| item["text"] == "First.")
    });
    let first = item(&shown(), "r-grouped").unwrap();
    assert_eq!(first["status"], "running");
    assert_eq!(item(&shown(), "r-single").unwrap()["text"], "Single.");

    std::fs::write(&release, "").unwrap();
    wait_for(
        "the grouped reply takes over its live item before the turn ends",
        || {
            let view = shown();
            view["turn"]["status"] == "running"
                && item(&view, "r-grouped").is_some_and(|item| item["status"] == "completed")
                && host.turns(agent).iter().any(|row| {
                    row["id"] == "r-grouped" && row["text"] == "First.\n\nSecond.\n\nThird."
                })
        },
    );
    std::fs::write(&finish, "").unwrap();
    wait_for("the grouped reply is complete", || {
        shown()["turn"]["status"] == "completed"
    });
    let after = shown();
    let messages: Vec<_> = after["items"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|item| item["kind"] == "message")
        .collect();
    assert_eq!(messages.len(), 2, "{messages:?}");
    let grouped = item(&after, "r-grouped").unwrap();
    assert_eq!(grouped["id"], first["id"]);
    assert_eq!(grouped["status"], "completed");
    assert_eq!(grouped["text"], "First.\n\nSecond.\n\nThird.");
    assert_eq!(grouped["phase"], "commentary");
    for item in messages {
        let rows = host.turns(agent);
        let durable: Vec<_> = rows
            .iter()
            .filter(|row| row["id"] == item["row_id"])
            .collect();
        assert_eq!(durable.len(), 1, "{item}");
        assert_eq!(durable[0]["text"], item["text"]);
        if item["row_id"] == "r-grouped" {
            assert_eq!(durable[0]["tools"].as_array().unwrap().len(), 2);
        }
    }
    let messages_in_sse = || {
        let heard = heard.lock().unwrap();
        heard
            .iter()
            .filter(|event| event["type"] == "live")
            .flat_map(|event| event["ops"].as_array().into_iter().flatten())
            .filter_map(|op| op.get("item"))
            .filter(|item| item["kind"] == "message" && item["row_id"] == "r-grouped")
            .cloned()
            .collect::<Vec<_>>()
    };
    wait_for("the grouped item was sent", || {
        !messages_in_sse().is_empty()
    });
    assert!(
        messages_in_sse()
            .iter()
            .all(|item| item["id"] == first["id"])
    );
}
