//! A real shim and a disposable Host, with Claude standing in as a local process.

use super::*;

#[test]
fn a_thinking_only_turn_is_replaced_and_the_third_message_waits() {
    let place = Place::new("takeover-b");
    let claude = place.root.join("claude");
    program(
        &claude,
        r#"#!/usr/bin/env python3
import json, os, pathlib, sys, time
root = pathlib.Path(__file__).parent
if "--version" in sys.argv:
    print("2.1.321 (Claude Code)")
    raise SystemExit(0)
arg = "--resume" if "--resume" in sys.argv else "--session-id"
session = sys.argv[sys.argv.index(arg) + 1]
line = json.loads(sys.stdin.readline())
parts = [block["text"] for block in line["message"]["content"]
         if not block["text"].startswith("<system-reminder>")]
pid = os.getpid()
def say(kind, **fields):
    print(json.dumps(dict(type=kind, **fields)), flush=True)
say("system", subtype="init", session_id=session)
if parts == ["first"]:
    say("stream_event", event={"type": "content_block_start", "index": 0,
                              "content_block": {"type": "thinking", "thinking": ""}})
    say("stream_event", event={"type": "content_block_delta", "index": 0,
                              "delta": {"type": "thinking_delta", "thinking": "hmm"}})
    say("stream_event", event={"type": "content_block_delta", "index": 0,
                              "delta": {"type": "signature_delta", "signature": "s"}})
with (root / "took.jsonl").open("a") as file:
    file.write(json.dumps(dict(pid=pid, parts=parts, session=session, argv=sys.argv[1:],
                               uuid=line["uuid"])) + "\n")
while root.exists() and not (root / f"go-{pid}").exists():
    time.sleep(.01)
answer = " / ".join(parts)
say("assistant", message={"id": f"reply-{pid}", "role": "assistant",
                         "content": [{"type": "text", "text": answer}]})
say("result", subtype="success", result=answer)
"#,
    );
    let env = [("CLARP_CLAUDE_PROGRAM", claude.to_str().unwrap())];
    let host = place.start_in("", &[], &env);
    host.post(
        "/agents",
        json!({"name":"Ada", "session":"ada", "backend":"claude"}),
    );
    let launches = || {
        std::fs::read_to_string(place.root.join("took.jsonl"))
            .unwrap_or_default()
            .lines()
            .filter_map(|line| serde_json::from_str::<Value>(line).ok())
            .collect::<Vec<_>>()
    };
    let release = |launch: &Value| {
        std::fs::write(place.root.join(format!("go-{}", launch["pid"])), "").unwrap();
    };

    host.send("ada", "first", "first");
    wait_for("the first tool to report its thinking", || {
        launches().len() == 1
    });
    host.send("ada", "second", "second");
    wait_for("the replacement tool", || launches().len() == 2);
    let saw = launches();
    assert_eq!(saw[0]["parts"], json!(["first"]));
    assert_eq!(saw[1]["parts"], json!(["first", "second"]));
    assert_ne!(saw[0]["session"], saw[1]["session"]);
    assert_ne!(saw[0]["uuid"], saw[1]["uuid"]);
    assert!(
        saw[1]["argv"]
            .as_array()
            .unwrap()
            .contains(&json!("--session-id"))
    );

    let third = host.send("ada", "third", "third");
    assert_eq!(third["queued"], true);
    assert_eq!(launches().len(), 2);
    let rows = host.turns("ada");
    let first = rows.iter().find(|row| row["id"] == "u-first").unwrap();
    assert_eq!(first["turn"]["status"], "replaced", "{rows:?}");
    assert!(first["turn"]["replaced_by"].is_string());
    let visible = rows
        .iter()
        .filter_map(|row| row["id"].as_str())
        .collect::<Vec<_>>();
    assert_eq!(visible, ["u-first", "u-second"]);

    release(&saw[1]);
    wait_for(
        "the third message to start after the combined answer",
        || launches().len() == 3,
    );
    let rows = host.turns("ada");
    let answers = rows
        .iter()
        .filter(|row| row["role"] == "assistant")
        .filter_map(|row| row["text"].as_str())
        .collect::<Vec<_>>();
    assert_eq!(answers, ["first / second"], "{rows:?}");
    release(&launches()[2]);
}
