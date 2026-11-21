//! The scheduler's settlement of peer waits from the recipient's journal (R136).

use super::*;
use clarp_kernel::{
    agent::Command as AgentCommand,
    conversation::{Entry, Notice, TurnEnd},
    journal::Recorder,
    turn::{RunOutcome, Turn, TurnId},
};
use clarp_ports::log::LogEngine;

fn request_admin(host: &Host, place: &Place, args: &[&str]) -> Value {
    let mut argv = vec!["clarp-admin"];
    argv.extend_from_slice(args);
    let (code, out, err) = tool(host, &place.root, None, &argv);
    assert_eq!(code, 0, "{err}");
    serde_json::from_str(&out).unwrap()
}

// Write through the same journal transitions as the actor, in a stopped Host's test
// store. This avoids waiting for the launch-refusal backoff during the scheduler test.
fn end_request(place: &Place, agent_id: &str, message_id: &str, ran: bool) {
    let path = place.root.join("data/agents").join(agent_id);
    let log = clarp_store_sqlite::SqliteEngine::new()
        .open(&path, clarp_codec::standard_set("zstd").unwrap())
        .unwrap();
    let turn = TurnId(log.find(message_id).unwrap().unwrap().turn.unwrap());
    let mut recorder = Recorder::open(log).unwrap();
    let now = now_ms();
    let mut batch = recorder.batch();
    if matches!(batch.agent().live(), Some(Turn::Parked(_))) {
        batch
            .command(AgentCommand::TurnRetried { turn }, now)
            .unwrap();
    }
    let reason = "fixture tool failed".to_string();
    if ran {
        batch
            .command(AgentCommand::TurnStarted { turn }, now)
            .unwrap();
        batch
            .command(
                AgentCommand::TurnEnded {
                    turn,
                    outcome: RunOutcome::Failed { reason },
                },
                now,
            )
            .unwrap();
    } else {
        batch
            .command(
                AgentCommand::StartFailed {
                    turn,
                    reason: reason.clone(),
                },
                now,
            )
            .unwrap();
        let marker = Entry::Notice(Notice {
            id: format!("marker-{message_id}"),
            turn: Some(turn.0),
            code: Notice::TURN_FAILED.to_owned(),
            text: clarp_kernel::launch_refused::notice(8, &reason),
            trace_id: None,
        });
        batch.item(marker.to_item(now).unwrap());
    }
    let end = TurnEnd::of(batch.agent().last().unwrap(), message_id).unwrap();
    batch.item(Entry::TurnEnd(end).to_item(now).unwrap());
    batch.commit().unwrap();
}

#[test]
fn the_tick_wakes_only_the_unrun_peer_wait_and_keeps_an_answer_already_given() {
    let place = Place::new("r136-peer-wait");
    let slow = ["--schedules-checked-every-ms", "120000"];
    let host = place.start_with("", &slow);
    let commitment = json!({
        "outcome": "Get their result", "criteria": ["It arrives"], "limits": "None", "enroll": true,
    })
    .to_string();
    let rows = ["Unrun", "Ran", "Answered"].map(|label| {
        let asker = host.new_agent(&format!("{label} Asker"));
        let worker = host.new_agent(&format!("{label} Worker"));
        host.send(&asker, &json!({"session": asker}).to_string(), "bind");
        wait_for("the asker to bind its session", || {
            host.state(&asker) == "done"
        });
        let goal = goal(
            &host,
            &place.root,
            &["create", &asker, "work", "Peer work", "[]", &commitment],
        );
        let worker_id = row_id(&host, &worker);
        (
            asker,
            worker,
            worker_id,
            goal["plan_id"].as_str().unwrap().to_owned(),
        )
    });
    host.kill();

    let missing = place.root.join("missing-shim");
    let host = place.start_with("", &[slow[0], slow[1], "--shim", missing.to_str().unwrap()]);
    let read = |host: &Host, plan: &str| {
        goal(host, &place.root, &["get", plan])["goal"]["continuation"].clone()
    };
    let ids = rows.each_ref().map(|(asker, worker, _, plan)| {
        let req = request_admin(
            &host,
            &place,
            &[
                "request",
                "--to",
                worker,
                "--from",
                asker,
                "--goal",
                plan,
                "--deadline",
                "3600",
                "--text",
                "send the findings",
            ],
        );
        assert_eq!(read(&host, plan)["state"], "waiting");
        req["request_id"].as_str().unwrap().to_owned()
    });
    let sent = |id: &str| format!("u-{}", clarp_kernel::requests::request_client_id(id));
    host.kill();

    for (index, (_, _, worker_id, _)) in rows.iter().enumerate() {
        end_request(&place, worker_id, &sent(&ids[index]), index == 1);
    }
    let host = place.start_with("", &slow);
    request_admin(
        &host,
        &place,
        &[
            "reply",
            "--request",
            &ids[2],
            "--from",
            &rows[2].1,
            "--kind",
            "result",
            "--text",
            "The findings are here.",
        ],
    );
    assert_eq!(
        read(&host, &rows[2].3)["dependency_result"]["outcome"],
        "succeeded"
    );
    host.kill();

    let host = place.start_with("", &["--schedules-checked-every-ms", "50"]);
    wait_for("the next tick to settle the unrun request", || {
        read(&host, &rows[0].3)["dependency_result"]["outcome"] == "failed"
    });
    let wake = read(&host, &rows[0].3);
    assert_eq!(wake["dependency_result"]["key"], format!("peer:{}", ids[0]));
    let why = wake["dependency_result"]["evidence"].as_str().unwrap();
    assert!(
        why.starts_with(&format!(
            "request {} was not delivered: The agent could not be started",
            ids[0]
        )),
        "{why}"
    );
    assert!(why.contains("fixture tool failed"), "{why}");
    assert_eq!(read(&host, &rows[1].3)["state"], "waiting");
    let answered = read(&host, &rows[2].3);
    assert_eq!(answered["dependency_result"]["outcome"], "succeeded");
    assert!(
        answered["dependency_result"]["evidence"]
            .as_str()
            .unwrap()
            .contains("The findings are here.")
    );
}
