//! Exercise a real stdio child and read its turn back through the ordinary spool.

#![allow(clippy::unwrap_used)]

use std::path::PathBuf;
use std::process::Command;
use std::time::Duration;

use clarp_shim::codex_app::{AppServer, Frame};
use clarp_shim::codex_turn::TurnSpool;
use clarp_spool::{Entry, ShimEvent, SpoolReader};
use serde_json::{Value, json};

struct Scratch(PathBuf);

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

#[test]
fn codex_goal_transport_keeps_notifications_before_the_reply_and_spools_only_its_turn() {
    let root = Scratch(
        std::env::temp_dir().join(format!("clarp-codex-goal-transport-{}", std::process::id())),
    );
    std::fs::create_dir(&root.0).unwrap();
    let mut command = Command::new("/bin/sh");
    command.env_clear().current_dir(&root.0).args([
        "-c",
        r#"
read -r init
case "$init" in
    *'"experimentalApi":true'*'"method":"initialize"'*) ;;
    *'"method":"initialize"'*'"experimentalApi":true'*) ;;
    *) exit 1 ;;
esac
printf '%s\n' '{"id":1,"result":{}}'
read -r initialized
case "$initialized" in (*'"method":"initialized"'*) ;; (*) exit 1 ;; esac
read -r goal
case "$goal" in (*'"method":"thread/goal/set"'*) ;; (*) exit 1 ;; esac
printf '%s\n' \
  '{"method":"turn/started","params":{"threadId":"thread","turn":{"id":"ours"}}}' \
  '{"id":2,"result":{"goal":{"status":"active"}}}' \
  '{"method":"item/agentMessage/delta","params":{"threadId":"thread","turnId":"ours","itemId":"item","delta":"working"}}' \
  '{"method":"item/completed","params":{"threadId":"thread","turnId":"other","item":{"id":"wrong","type":"agentMessage","text":"other turn"}}}' \
  '{"method":"item/completed","params":{"threadId":"thread","turnId":"ours","item":{"id":"item","type":"agentMessage","text":"done"}}}' \
  '{"method":"turn/completed","params":{"threadId":"thread","turn":{"id":"ours","status":"completed"}}}'
"#,
    ]);
    let (mut server, frames) = AppServer::spawn(&mut command).unwrap();
    let receive = || {
        frames
            .recv_timeout(Duration::from_secs(5))
            .unwrap()
            .unwrap()
    };
    assert_eq!(server.initialize().unwrap(), 1);
    assert_eq!(
        receive(),
        Frame::Reply {
            id: 1,
            result: Ok(json!({}))
        }
    );
    server.initialized().unwrap();
    assert_eq!(
        server
            .request(
                "thread/goal/set",
                json!({"threadId":"thread","status":"active"})
            )
            .unwrap(),
        2,
    );
    let Frame::Notification { method, params } = receive() else {
        panic!("expected native turn start")
    };
    assert_eq!(method, "turn/started");
    let path = root.0.join("turn.jsonl");
    let mut spool = TurnSpool::begin(
        &path,
        params["threadId"].as_str().unwrap().to_owned(),
        params["turn"]["id"].as_str().unwrap().to_owned(),
        std::process::id(),
        1,
    )
    .unwrap();
    assert_eq!(
        receive(),
        Frame::Reply {
            id: 2,
            result: Ok(json!({"goal":{"status":"active"}}))
        }
    );
    loop {
        let Frame::Notification { method, params } = receive() else {
            panic!("expected turn output")
        };
        if spool.notification(&method, &params, 2).unwrap() {
            break;
        }
    }
    let records = SpoolReader::new(path).read_from(0).unwrap().records;
    let output: Vec<Value> = records
        .iter()
        .filter_map(|record| match &record.entry {
            Entry::Out(line) => Some(serde_json::from_str(line).unwrap()),
            _ => None,
        })
        .collect();
    assert_eq!(
        output,
        vec![
            json!({"type":"turn.started"}),
            json!({"type":"item.updated","item":{"type":"agent_message","id":"item","text":"working"}}),
            json!({"type":"item.completed","item":{"type":"agent_message","id":"item","text":"done"}}),
            json!({"type":"turn.completed","usage":null}),
        ]
    );
    assert!(matches!(
        records.last().unwrap().entry,
        Entry::Shim(ShimEvent::Exited {
            code: Some(0),
            signal: None
        })
    ));
}
