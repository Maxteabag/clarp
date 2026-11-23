//! A message sent while a turn of Antigravity over ACP runs reaches that turn, as one sent to
//! Claude Code does (`steer_c.rs`): the Host writes it on the turn's input, the driver sends it
//! to Antigravity as a prompt of its own, and the message enters the conversation when the
//! driver says Antigravity took it up, never on the write alone. The stand-in for `agy acp`
//! (`clarp-shim/tests/fake_acp/agy.py`, scenario `steer`) meets a second prompt as agy 1.5 does:
//! it answers the running one at once and goes on under the new one.

use super::*;

const FAKE: &str = include_str!("../../../clarp-shim/tests/fake_acp/agy.py");

/// The revisions at which the Host said this message's turn started.
fn started(heard: &Mutex<Vec<Value>>, session: &str, id: &str) -> Vec<u64> {
    heard
        .lock()
        .unwrap()
        .iter()
        .filter(|event| {
            event["type"] == "queue-updated"
                && event["session"] == session
                && event["client_msg_id"] == id
                && event["queue_started"] == true
        })
        .map(|event| event["queue_revision"].as_u64().unwrap_or(0))
        .collect()
}

fn answered(host: &Host, session: &str, answer: &str) {
    wait_for(answer, || {
        host.conversation(session)
            .iter()
            .any(|(role, text)| role == "assistant" && text == answer)
    });
}

/// How many prompts Antigravity was sent with exactly this text.
fn prompted(root: &std::path::Path, text: &str) -> usize {
    std::fs::read_to_string(root.join("acp.log"))
        .unwrap_or_default()
        .lines()
        .filter_map(|line| serde_json::from_str::<Value>(line).ok())
        .filter(|message| message["method"] == "session/prompt")
        .filter(|message| message["params"]["prompt"][0]["text"] == text)
        .count()
}

#[test]
fn a_message_reaches_a_running_antigravity_turn_when_antigravity_takes_it_up() {
    let place = Place::new("agy-acp-steer");
    let agy = place.root.join("agy");
    program(&agy, FAKE);
    std::fs::write(place.root.join("scenario"), "steer").unwrap();
    let world = [
        ("HOME", place.root.to_str().unwrap()),
        ("CLARP_AGY_PROGRAM", agy.to_str().unwrap()),
    ];
    let host = place.start_in("", &[], &world);
    let features = host.get("/server-info");
    assert!(
        features.to_string().contains("agy_acp_steering"),
        "{features}"
    );
    let heard = host.listen();
    wait_for("the event stream", || {
        heard
            .lock()
            .unwrap()
            .iter()
            .any(|event| event["type"] == "agent-roster")
    });
    let mark = |name: &str| std::fs::write(place.root.join(name), "").unwrap();
    let await_file = |name: &str| wait_for(name, || place.root.join(name).exists());
    let ids = |session: &str| {
        host.turns(session)
            .into_iter()
            .filter(|row| row["role"] == "user")
            .map(|row| row["id"].as_str().unwrap().to_owned())
            .collect::<Vec<_>>()
    };
    let queue_status = |session: &str, id: &str| {
        host.get(&format!("/turn-queue?session={session}"))["items"]
            .as_array()
            .unwrap()
            .iter()
            .find(|row| row["id"] == id)
            .and_then(|row| row["status"].as_str().map(str::to_owned))
    };

    for mode in ["tool", "writing", "sentence", "refused", "stop"] {
        let session = format!("acp-steer-{mode}");
        host.post(
            "/agents",
            json!({"name": session, "session": session, "backend": "agy-acp"}),
        );
        let case = format!("case-{mode}");
        let first = host.send(&session, &case, "first");
        assert_eq!(first["queued"], false, "{first}");
        await_file(&format!("working-{mode}"));
        match mode {
            // What it writes before the message comes is shown as it is written.
            "writing" => shown_live(&host, &session, "before-steer"),
            "sentence" => shown_live(&host, &session, "5 plus"),
            _ => wait_for("the tool in the live projection", || {
                host.state(&session) == "tool"
            }),
        }

        let add = if mode == "stop" {
            "add-stop-1".to_string()
        } else {
            format!("add-{mode}")
        };
        let sent = host.send(&session, &add, &add);
        assert_eq!(sent["queued"], true, "{sent}");
        await_file(&format!("received-{add}"));
        // Written to the turn, not read: it is in the queue as started, and nowhere else.
        assert_eq!(ids(&session), ["u-first"], "the write is no read");
        assert!(started(&heard, &session, &add).is_empty());
        assert_eq!(queue_status(&session, &add).as_deref(), Some("started"));

        match mode {
            "refused" => {
                // Antigravity would not take it: the turn answers alone, and the message
                // runs as a turn of its own after it.
                mark("release-refused");
                answered(&host, &session, "first-refused");
                answered(&host, &session, "separate-add-refused");
                wait_for("the agent to finish", || host.state(&session) == "done");
                assert_eq!(ids(&session), ["u-first", "u-add-refused"]);
                assert_eq!(prompted(&place.root, "add-refused"), 2);
            }
            "stop" => {
                mark("taken-stop");
                wait_for("the first message read", || {
                    ids(&session) == ["u-first", "u-add-stop-1"]
                });
                let unread = host.send(&session, "add-stop-2", "add-stop-2");
                assert_eq!(unread["queued"], true, "{unread}");
                await_file("received-add-stop-2");
                assert_eq!(ids(&session), ["u-first", "u-add-stop-1"]);
                host.post("/stop", json!({"session": session}));
                // The one never read runs when the stopped process has ended, as a turn of
                // its own; the one read is not run again.
                answered(&host, &session, "separate-add-stop-2");
                wait_for("the agent to finish", || host.state(&session) == "done");
                assert_eq!(ids(&session), ["u-first", "u-add-stop-1", "u-add-stop-2"]);
                assert_eq!(prompted(&place.root, "add-stop-1"), 1);
                assert_eq!(prompted(&place.root, "add-stop-2"), 2);
                wait_for("the unread message's own start", || {
                    started(&heard, &session, "add-stop-2").len() == 1
                });
            }
            _ => {
                mark(&format!("taken-{mode}"));
                let read = vec!["u-first".to_string(), format!("u-{add}")];
                wait_for("the message read into the turn", || ids(&session) == read);
                assert_eq!(queue_status(&session, &add), None, "it left the queue");
                if mode == "sentence" {
                    // agy took the message in the middle of a sentence, and went on with it:
                    // the sentence is one whole row, before the message, never cut by it.
                    let rows = host.turns(&session);
                    let whole = rows
                        .iter()
                        .position(|row| row["text"] == "5 plus 4 is 9")
                        .unwrap_or_else(|| panic!("{rows:?}"));
                    let read = rows
                        .iter()
                        .position(|row| row["id"] == "u-add-sentence")
                        .unwrap();
                    assert!(whole < read, "{rows:?}");
                    assert!(
                        rows.iter().all(|row| row["text"] != "5 plus"
                            && row["text"] != " 4 is 9"),
                        "{rows:?}"
                    );
                }
                if mode == "writing" {
                    // An answer given before the message was read stays an answer: stored
                    // before the message, and still there while the turn goes on.
                    let rows = host.turns(&session);
                    let before = rows
                        .iter()
                        .position(|row| row["text"] == "before-steer")
                        .unwrap_or_else(|| panic!("{rows:?}"));
                    let read = rows
                        .iter()
                        .position(|row| row["id"] == "u-add-writing")
                        .unwrap();
                    assert!(before < read, "{rows:?}");
                }
                mark(&format!("release-{mode}"));
                answered(&host, &session, &format!("after-{mode}"));
                wait_for("the agent to finish", || host.state(&session) == "done");
                let rows = host.turns(&session);
                let at = |id: &str| rows.iter().position(|row| row["id"] == id).unwrap();
                let after = rows
                    .iter()
                    .position(|row| row["text"] == format!("after-{mode}"))
                    .unwrap();
                assert!(at(&format!("u-{add}")) < after, "{rows:?}");
                assert_eq!(
                    rows.iter()
                        .filter(|row| row["text"] == "before-steer")
                        .count(),
                    usize::from(mode == "writing"),
                    "{rows:?}"
                );
                // Read once, in the one process: never launched again.
                assert_eq!(prompted(&place.root, &add), 1);
                assert_eq!(prompted(&place.root, &case), 1);
            }
        }
        wait_for("the queue-start of the message", || {
            started(&heard, &session, &add).len() == 1
        });
        assert_eq!(started(&heard, &session, &add).len(), 1);
        assert_eq!(
            host.get(&format!("/turn-queue?session={session}"))["items"],
            json!([])
        );
    }
}

/// Waits until the live projection of the agent's turn shows this text.
fn shown_live(host: &Host, session: &str, text: &str) {
    wait_for(text, || {
        host.get(&format!("/live?session={session}"))
            .to_string()
            .contains(text)
    });
}
