//! A Stop told in one sentence for people (issue 811, feature `stop_receipt_text`): on its
//! answer, and wherever an app learns of it without having sent it. One file a piece,
//! declared in `host.rs`.

use super::*;

#[test]
fn a_stop_is_said_in_one_sentence_on_its_answer_its_events_and_the_snapshot() {
    let place = Place::new("stop-text");
    let host = place.start();
    assert_eq!(
        host.get("/server-info")["contract"]["features"]["stop_receipt_text"],
        184
    );
    let session = host.new_agent("Stopper");

    // Nothing runs and nothing waits.
    let quiet = host.post("/stop", json!({ "session": session }));
    assert_eq!(quiet["text"], "Stopped.", "{quiet}");

    // A turn runs, and two messages of a schedule wait behind it: a Stop holds them.
    let heard = host.listen();
    host.send(&session, "sleep 30", "long");
    wait_for("the turn to run", || host.state(&session) == "thinking");
    for id in ["later-1", "later-2"] {
        host.post(
            "/send",
            json!({ "session": session, "text": format!("then {id}"), "client_msg_id": id,
                    "origin": "schedule", "queue_if_busy": true }),
        );
    }
    let queue = format!("/turn-queue?session={session}");
    wait_for("both to wait", || {
        host.get(&queue)["items"]
            .as_array()
            .is_some_and(|items| items.len() == 2)
    });
    let stopped = host.post("/stop", json!({ "session": session }));
    let said = "Stopped. 2 messages wait.";
    assert_eq!(stopped["queue_paused"], true, "{stopped}");
    assert_eq!(stopped["text"], said, "{stopped}");
    // The fields it is built from stay.
    assert_eq!(stopped["stop_actor"], "user");

    // Another screen reads the same line in the snapshot, while the Stop is the latest state.
    let activity = || {
        host.get("/agents/snapshot")["agents"]
            .as_array()
            .unwrap()
            .iter()
            .find(|row| row["session"] == session.as_str())
            .unwrap()["activity"]
            .clone()
    };
    wait_for("the snapshot to tell the Stop", || {
        activity()["stop_text"] == said
    });
    // And on the events an app gets.
    wait_for("the events to tell the Stop", || {
        let events = heard.lock().unwrap();
        let ours = |kind: &str| {
            events
                .iter()
                .filter(|event| event["type"] == kind && event["session"] == session.as_str())
                .cloned()
                .collect::<Vec<Value>>()
        };
        let state = ours("agent-state").iter().any(|event| {
            event["detail"]["source"] == "user_stop" && event["detail"]["stop_text"] == said
        });
        state
            && ours("agent-activity")
                .iter()
                .any(|event| event["stop_text"] == said)
    });
}
