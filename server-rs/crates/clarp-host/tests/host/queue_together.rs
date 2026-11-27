//! The owner, on 23 Nov 2026, of an agent that answered his waiting messages one turn at a
//! time: the user's messages that wait behind a turn its tool cannot take them into are one
//! turn when it ends, with all of them in its prompt.

use super::*;

/// Antigravity, as it prints a turn. Every prompt it is given is kept, one JSON string a line,
/// in `prompts` beside it. A prompt that ends with `hold` waits for the file `release`.
const AGY: &str = r#"#!/usr/bin/env python3
import json, os, pathlib, sys, time
root = pathlib.Path(__file__).resolve().parent
args = sys.argv[1:]
prompt = next((a[len("--print="):] for a in args if a.startswith("--print=")), "")
with (root / "prompts").open("a") as kept:
    kept.write(json.dumps(prompt) + "\n")
number = sum(1 for _ in (root / "prompts").open())
conversation = "11111111-2222-3333-4444-555555555555"
def say(event, **body):
    print(json.dumps({"event": event, **body}), flush=True)
def step(index, state, kind, **more):
    update = {"conversation_id": conversation, "step_index": index, "state": state,
              "step_type": kind, **more}
    say("step_update", step_update=update)
say("init", conversation_id=conversation,
    init={"model": "gemini-stand-in-low", "cwd": os.getcwd(), "tools": []})
step(0, "DONE", "user_input", text_delta=prompt)
if prompt.endswith("hold"):
    (root / "holding").touch()
    end = time.monotonic() + 60
    while not (root / "release").exists():
        if time.monotonic() >= end:
            raise SystemExit("stand-in release expired")
        time.sleep(0.05)
answer = "agy answer %d" % number
step(1, "DONE", "agent_response", text_delta=answer)
say("result", result={"conversation_id": conversation, "status": "SUCCESS", "response": answer})
"#;

#[test]
fn three_messages_that_wait_behind_a_turn_go_to_the_tool_in_one_prompt_and_are_answered_once() {
    let place = Place::new("queue-together");
    let agy = place.root.join("agy");
    program(&agy, AGY);
    let nowhere = place.root.join("absent");
    let world = [
        ("CLARP_AGY_PROGRAM", agy.to_str().unwrap()),
        ("CLARP_CLAUDE_PROGRAM", nowhere.to_str().unwrap()),
        ("CLARP_CODEX_PROGRAM", nowhere.to_str().unwrap()),
    ];
    let host = place.start_in("", &[], &world);
    let heard = host.listen();
    let made = host.post("/agents", json!({ "name": "Gatherer", "backend": "agy" }));
    let session = made["session"].as_str().unwrap().to_string();

    host.send(&session, "hold", "m1");
    wait_for("the first turn to run", || {
        place.root.join("holding").exists()
    });
    let said = [
        ("m2", "first words"),
        ("m3", "second words"),
        ("m4", "third words"),
    ];
    for (id, text) in said {
        let sent = host.send(&session, text, id);
        assert_eq!(sent["queued"], true, "{sent}");
    }
    // Antigravity cannot be steered: each waits, to be read with the others.
    let queue = host.get(&format!("/turn-queue?session={session}"));
    let items: Vec<(Value, Value)> = queue["items"]
        .as_array()
        .unwrap()
        .iter()
        .map(|item| (item["id"].clone(), item["read_together"].clone()))
        .collect();
    assert_eq!(
        items,
        ["m2", "m3", "m4"].map(|id| (json!(id), json!(true))),
        "{queue}"
    );
    let told = |id: &str, started: bool| {
        heard.lock().unwrap().iter().any(|event| {
            event["type"] == "queue-updated"
                && event["session"] == session
                && event["client_msg_id"] == id
                && event["queue_started"] == started
                && (started || event["read_together"] == true)
        })
    };
    wait_for("each to be told waiting to be read together", || {
        ["m2", "m3", "m4"].iter().all(|id| told(id, false))
    });

    std::fs::write(place.root.join("release"), "").unwrap();
    let answers = |host: &Host| {
        host.conversation(&session)
            .into_iter()
            .filter(|(role, _)| role == "assistant")
            .count()
    };
    wait_for("the agent to answer the three", || {
        answers(&host) == 2 && host.state(&session) == "done"
    });

    let prompts: Vec<String> = std::fs::read_to_string(place.root.join("prompts"))
        .unwrap()
        .lines()
        .map(|line| serde_json::from_str(line).unwrap())
        .collect();
    assert_eq!(
        prompts.len(),
        2,
        "the first message's launch, and one for the three: {prompts:?}"
    );
    let together = clarp_kernel::told::together(&["first words", "second words", "third words"]);
    assert!(prompts[1].contains(&together), "{prompts:?}");
    wait_for("the queue to tell each started", || {
        ["m2", "m3", "m4"].iter().all(|id| told(id, true))
    });
    let queue = host.get(&format!("/turn-queue?session={session}"));
    assert_eq!(queue["items"], json!([]), "{queue}");

    // Three rows of the user's, in order, and one answer after them.
    let rows: Vec<(String, String, String)> = host
        .turns(&session)
        .into_iter()
        .filter(|row| row["role"] == "user" || row["role"] == "assistant")
        .map(|row| {
            (
                row["role"].as_str().unwrap().to_string(),
                row["id"].as_str().unwrap().to_string(),
                row["text"].as_str().unwrap().to_string(),
            )
        })
        .collect();
    let texts: Vec<(&str, &str)> = rows
        .iter()
        .map(|(role, _, text)| (role.as_str(), text.as_str()))
        .collect();
    assert_eq!(
        texts,
        vec![
            ("user", "hold"),
            ("assistant", "agy answer 1"),
            ("user", "first words"),
            ("user", "second words"),
            ("user", "third words"),
            ("assistant", "agy answer 2"),
        ],
        "{rows:?}"
    );
    let ids: Vec<&str> = rows
        .iter()
        .filter(|(role, _, _)| role == "user")
        .map(|(_, id, _)| id.as_str())
        .collect();
    assert_eq!(ids, ["u-m1", "u-m2", "u-m3", "u-m4"]);
}

#[test]
fn queued_utterances_share_one_answer_only_within_their_call() {
    let place = Place::new("call-utterances-together");
    let agy = place.root.join("agy");
    program(&agy, AGY);
    let host = place.start_in("", &[], &[("CLARP_AGY_PROGRAM", agy.to_str().unwrap())]);
    let agent = host.post("/agents", json!({"name":"Caller","backend":"agy"}));
    let session = agent["session"].as_str().unwrap();
    host.send(session, "hold", "occupied");
    wait_for("the previous turn to run", || {
        place.root.join("holding").exists()
    });

    let principal = "fixture-caller";
    let call = || {
        host.post("/calls", json!({"agents":[session],"principal":principal}))["call"]["call_id"]
            .clone()
    };
    let send = |call: &Value, id: &str, text: &str| {
        let sent = host.post(
            "/send",
            json!({"session":session,"call_id":call,"transcription_id":id,
                "synthesize_audio":false,"text":text,"client_msg_id":id,"trace_id":id}),
        );
        assert_eq!(sent["queued"], true, "{sent}");
    };
    let first = call();
    assert!(first.is_string(), "{first}");
    send(&first, "call-first", "first captured words");
    send(&first, "call-second", "second captured words");
    host.post("/calls/end", json!({"call_id":first,"principal":principal}));
    let second = call();
    assert!(second.is_string() && second != first, "{second}");
    send(&second, "other-call", "separate call words");
    let typed = host.send(session, "typed follow-up", "typed");
    assert_eq!(typed["queued"], true, "{typed}");

    std::fs::write(place.root.join("release"), "").unwrap();
    wait_for("every message to be answered", || {
        let rows = host.turns(session);
        rows.iter()
            .any(|row| row["role"] == "assistant" && row["text"] == "agy answer 4")
            && host.state(session) == "done"
    });
    let prompts: Vec<String> = std::fs::read_to_string(place.root.join("prompts"))
        .unwrap()
        .lines()
        .map(|line| serde_json::from_str(line).unwrap())
        .collect();
    assert_eq!(prompts.len(), 4, "{prompts:?}");
    let joined = clarp_kernel::told::together(&["first captured words", "second captured words"]);
    assert!(
        prompts[1].contains(&joined),
        "{prompt:?}",
        prompt = prompts[1]
    );
    assert!(prompts[2].contains("separate call words"), "{prompts:?}");
    assert!(prompts[3].contains("typed follow-up"), "{prompts:?}");
    let rows = host.turns(session);
    for id in ["u-call-first", "u-call-second", "u-other-call", "u-typed"] {
        assert_eq!(rows.iter().filter(|row| row["id"] == id).count(), 1, "{id}");
    }
    let reply = rows
        .iter()
        .find(|row| row["text"] == "agy answer 2")
        .unwrap();
    assert_eq!(reply["trace_id"], "call-second", "{reply}");
}
