#![allow(clippy::unwrap_used)]

use super::*;
use clarp_spool::{Entry, SpoolReader};

fn opened(name: &str) -> (std::path::PathBuf, TurnSpool) {
    let dir = std::env::temp_dir().join(format!("clarp-muse-turn-{name}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let path = dir.join("turn.jsonl");
    let claim = ClaimSpool::create(&path, 0).unwrap();
    let spool = TurnSpool::begin(claim, "s".into(), "t".into(), 42, Some("m".into()), 0).unwrap();
    (path, spool)
}

fn lines(path: &std::path::Path) -> Vec<String> {
    SpoolReader::new(path.to_owned())
        .read_from(0)
        .unwrap()
        .records
        .iter()
        .map(|record| match &record.entry {
            Entry::Out(line) => line.clone(),
            Entry::Shim(event) => format!("shim {event:?}"),
            other => format!("{other:?}"),
        })
        .collect()
}

fn item(kind: &str, extra: Value) -> Value {
    let mut item = json!({"kind": kind, "turnId": "t", "itemId": "i", "status": "completed"});
    for (key, value) in extra.as_object().unwrap() {
        item[key] = value.clone();
    }
    json!({"sessionId": "s", "item": item})
}

#[test]
fn a_turn_is_written_as_exec_writes_it_with_the_lane_lines() {
    let (path, mut spool) = opened("whole");
    let message = item("agentMessage", json!({"text": "Hello"}));
    let tool = item(
        "toolCall",
        json!({"tool": "shell", "callId": "c1", "visibleOutput": "ok"}),
    );
    let failed = item(
        "toolCall",
        json!({"tool": "read", "callId": "c2", "status": "failed", "failureReason": "no file"}),
    );
    assert!(!spool.notification("item/completed", &tool, 1).unwrap());
    assert!(!spool.notification("item/completed", &failed, 2).unwrap());
    assert!(!spool.notification("item/completed", &message, 3).unwrap());
    spool.allowance(&json!({"observedAtMs": 5, "tier": "pro",
        "weekly": {"resetsAtMs": 9, "usedPercent": 6},
        "window": {"resetsAtMs": 8, "usedPercent": 2, "windowDurationMins": 300}}));
    let end = json!({"sessionId": "s", "turnId": "t", "terminal": "completed",
        "usage": {"inputTokens": 10, "outputTokens": 3}});
    assert!(spool.notification("turn/completed", &end, 4).unwrap());
    let said = lines(&path);
    assert_eq!(said[0], r#"shim Session { id: "s" }"#);
    assert_eq!(said[1], "shim Spawned { pid: 42 }");
    let events: Vec<Value> = said[2..said.len() - 1]
        .iter()
        .map(|line| serde_json::from_str(line).unwrap())
        .collect();
    let kinds: Vec<&str> = events
        .iter()
        .map(|e| e["payload_type"].as_str().or(e["type"].as_str()).unwrap())
        .collect();
    assert_eq!(
        kinds,
        [
            "run.lifecycle.started",
            "run.model.configured",
            "steer.open",
            "tool.result",
            "tool.result",
            "run.output.delta",
            "run.usage",
            "run.terminal.completed",
            "turn.completed",
        ]
    );
    assert_eq!(events[1]["payload"]["model_id"], "m");
    assert_eq!(
        events[3]["payload"]["correlation_facts"]["outcome"],
        "success"
    );
    assert_eq!(events[3]["payload"]["text"], "ok");
    assert_eq!(
        events[4]["payload"]["correlation_facts"]["outcome"],
        "failed"
    );
    assert_eq!(events[4]["payload"]["text"], "no file");
    assert_eq!(events[5]["payload"]["text"], "Hello");
    assert_eq!(events[6]["payload"]["models"][0]["input_tokens"], 10);
    assert_eq!(
        events[6]["payload"]["allowance"]["weekly"]["used_percent"],
        6
    );
    assert_eq!(events[6]["payload"]["allowance"]["as_of_ms"], 5);
    assert_eq!(
        said.last().unwrap(),
        "shim Exited { code: Some(0), signal: None }"
    );
}

#[test]
fn another_turn_or_session_is_not_this_turns() {
    let (path, mut spool) = opened("other");
    let mut other_turn = item("agentMessage", json!({"text": "x"}));
    other_turn["item"]["turnId"] = json!("u");
    let mut other_session = item("agentMessage", json!({"text": "y"}));
    other_session["sessionId"] = json!("r");
    spool
        .notification("item/completed", &other_turn, 1)
        .unwrap();
    spool
        .notification("item/completed", &other_session, 1)
        .unwrap();
    let end = json!({"sessionId": "s", "turnId": "u", "terminal": "completed"});
    assert!(!spool.notification("turn/completed", &end, 2).unwrap());
    assert_eq!(lines(&path).len(), 5);
}

#[test]
fn steered_words_are_read_once_by_their_command_and_the_rest_are_unread() {
    let (path, mut spool) = opened("steer");
    spool.steered("c-1", "words-1");
    spool.steered("c-2", "words-2");
    let read = item(
        "userMessage",
        json!({"steered": true, "commandId": "c-1", "text": "go"}),
    );
    spool.notification("item/started", &read, 1).unwrap();
    spool.notification("item/completed", &read, 2).unwrap();
    // The prompt's own message is no reading.
    let prompt = item("userMessage", json!({"commandId": "t", "text": "hi"}));
    spool.notification("item/completed", &prompt, 3).unwrap();
    assert_eq!(spool.unread(), ["c-2"]);
    spool.given_back("c-2");
    assert!(spool.unread().is_empty());
    let reads: Vec<String> = lines(&path)
        .into_iter()
        .filter(|line| line.contains("steer.read"))
        .collect();
    assert_eq!(reads.len(), 1);
    let read: Value = serde_json::from_str(&reads[0]).unwrap();
    assert_eq!(
        read,
        json!({"type": "steer.read", "id": "words-1", "confirmed": true})
    );
}

#[test]
fn a_stopped_turn_ends_as_exec_on_sigint_and_muses_own_cancel_as_stopped() {
    let (path, mut spool) = opened("stop");
    spool.stopping();
    let end = json!({"sessionId": "s", "turnId": "t", "terminal": "cancelled"});
    assert!(spool.notification("turn/completed", &end, 1).unwrap());
    assert_eq!(
        lines(&path).last().unwrap(),
        &format!(
            "shim Exited {{ code: None, signal: Some({}) }}",
            nix::libc::SIGINT
        )
    );

    let (path, mut spool) = opened("own-cancel");
    assert!(spool.notification("turn/completed", &end, 1).unwrap());
    let said = lines(&path);
    assert!(said[said.len() - 2].contains("run.terminal.cancelled"));
    assert_eq!(
        said.last().unwrap(),
        "shim Exited { code: Some(0), signal: None }"
    );
}

#[test]
fn a_failed_turn_says_why_and_a_lost_serve_fails_once() {
    let (path, mut spool) = opened("fail");
    let end = json!({"sessionId": "s", "turnId": "t", "terminal": "failed",
        "error": {"kind": "quota", "message": "weekly limit reached", "retryable": false}});
    assert!(spool.notification("turn/completed", &end, 1).unwrap());
    let said = lines(&path);
    assert!(
        said.iter()
            .any(|line| line.contains(r#""reason":"weekly limit reached""#))
    );
    assert!(
        said.iter()
            .any(|line| line.contains(r#""type":"turn.failed""#))
    );
    assert_eq!(
        said.last().unwrap(),
        "shim Exited { code: Some(1), signal: None }"
    );

    let (path, mut spool) = opened("lost");
    spool.lost("Muse serve disconnected", 1).unwrap();
    let said = lines(&path);
    assert!(said[said.len() - 3].contains("Muse serve disconnected"));
    assert_eq!(
        said.last().unwrap(),
        "shim Exited { code: Some(1), signal: None }"
    );
}

#[test]
fn a_long_reply_says_it_is_still_written_at_most_every_thirty_seconds() {
    let (path, mut spool) = opened("progress");
    let delta = json!({"sessionId": "s", "turnId": "t", "itemId": "m", "delta": "x"});
    for at in [1_000, 10_000, 31_500, 40_000, 62_000] {
        spool.notification("item/delta", &delta, at).unwrap();
    }
    let progress = lines(&path)
        .iter()
        .filter(|line| line.contains("turn.progress"))
        .count();
    assert_eq!(progress, 2);
}

#[test]
fn the_answer_is_whole_when_muse_says_its_foreground_is_done() {
    let (path, mut spool) = opened("answered");
    let done = json!({"sessionId": "s", "turnId": "t", "blockingAgents": ["goal-reminder"]});
    assert!(
        !spool
            .notification("turn/foregroundCompleted", &done, 1)
            .unwrap()
    );
    let said = lines(&path);
    assert_eq!(said.last().unwrap(), r#"{"type":"turn.answered"}"#);
    let end = json!({"sessionId": "s", "turnId": "t", "terminal": "completed"});
    assert!(spool.notification("turn/completed", &end, 2).unwrap());
}

#[test]
fn with_the_reminder_agents_off_no_answered_line_is_written_and_the_turn_ends_at_completed() {
    // Muse 1.4.25 with `run.reminder_roster=[]` sends no `turn/foregroundCompleted`: the answer
    // and the turn's end come together, and nothing waits for an answered line.
    let (path, mut spool) = opened("no-reminders");
    let message = item("agentMessage", json!({"text": "Hello"}));
    assert!(!spool.notification("item/completed", &message, 1).unwrap());
    let end = json!({"sessionId": "s", "turnId": "t", "terminal": "completed"});
    assert!(spool.notification("turn/completed", &end, 2).unwrap());
    let said = lines(&path);
    assert!(
        !said.iter().any(|line| line.contains("turn.answered")),
        "{said:?}"
    );
    assert!(
        said.iter()
            .any(|line| line.contains("run.terminal.completed"))
    );
    assert_eq!(
        said.last().unwrap(),
        "shim Exited { code: Some(0), signal: None }"
    );
}
