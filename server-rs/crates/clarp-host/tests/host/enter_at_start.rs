//! The Host's tests of where a message stands in the conversation (issue 491): one file a piece,
//! declared in `host.rs`.

use super::*;

/// Issue 491: a message sent while its agent is busy waits in the queue and enters the
/// conversation in the write that starts its turn, whoever sent it: the user, a trigger, an
/// agent without a name. So the conversation reads m1, its answer, m2, its answer, and never two
/// messages above an answer that read only the first. While it waits it is no row: it is in the
/// queue the apps read, it can be reworded and taken back there, and a user's send is shown
/// pending in the delivery feed. Sent twice, it is one item and later one row. A kill of the Host
/// between the acceptance and the start loses nothing and writes no row; the turn starts after
/// the restart, with its row. A rewording that comes after the start finds nothing waiting. A
/// Stop holds what waits and writes no row of it; what the user says after the Stop runs first,
/// and each enters when its turn starts.
#[test]
fn a_message_sent_while_its_agent_is_busy_enters_the_conversation_when_its_turn_starts() {
    fn queue_event(
        heard: &Mutex<Vec<Value>>,
        id: &str,
        started: bool,
        change: Option<&str>,
    ) -> Option<Value> {
        heard
            .lock()
            .unwrap()
            .iter()
            .find(|event| {
                event["type"] == "queue-updated"
                    && event["client_msg_id"] == id
                    && event["queue_started"] == started
                    && event["queue_change"].as_str() == change
            })
            .cloned()
    }

    let place = Place::new("enter-at-start");
    // Give each stand-in its own gate so a stopped turn cannot take the next turn's signal.
    let took = place.root.join("took");
    let script = [
        "#!/bin/sh\nread -r message\n",
        &format!(
            "printf '%s %s\\n' \"$$\" \"$message\" >> \"{took}\"\n\
             while [ -d \"{root}\" ] && [ ! -f \"{root}/go-$$\" ]; do sleep 0.05; done\n",
            took = took.display(),
            root = place.root.display(),
        ),
        // The backend uses the provider's message ID as the row ID. A shared ID
        // for these separate replies would address the first turn's row again.
        &STAND_IN_ANSWERS.replace("msg_1", r#"msg_'"$$"'"#),
    ]
    .concat();
    let claude = place.root.join("claude");
    program(&claude, &script);
    let world = [("CLARP_CLAUDE_PROGRAM", claude.to_str().unwrap())];
    let host = place.start_in("", &[], &world);
    let made = host.post(
        "/agents",
        json!({ "name": "Ada", "session": "ada", "backend": "claude" }),
    );
    assert_eq!(made["session"], "ada", "{made}");
    let session = "ada";
    let listen = |host: &Host| {
        let heard = host.listen();
        wait_for("the event stream's roster", || {
            heard
                .lock()
                .unwrap()
                .iter()
                .any(|event| event["type"] == "agent-roster")
        });
        heard
    };
    let heard = listen(&host);
    let taken = || -> Vec<(u32, String)> {
        std::fs::read_to_string(&took)
            .unwrap_or_default()
            .lines()
            .map(|line| {
                let (pid, message) = line.split_once(' ').unwrap();
                (pid.parse().unwrap(), message.to_string())
            })
            .collect()
    };
    let answer = |index: usize| {
        let (pid, _) = &taken()[index];
        std::fs::write(place.root.join(format!("go-{pid}")), "").unwrap();
    };
    let rows = |host: &Host| -> Vec<(String, String)> {
        host.turns(session)
            .iter()
            .map(|turn| {
                (
                    turn["id"].as_str().unwrap().to_string(),
                    turn["role"].as_str().unwrap().to_string(),
                )
            })
            .collect()
    };
    let users = |host: &Host| -> Vec<String> {
        rows(host)
            .into_iter()
            .filter(|(_, role)| role == "user")
            .map(|(id, _)| id)
            .collect()
    };
    let queue_items = |host: &Host| {
        host.get(&format!("/turn-queue?session={session}"))["items"]
            .as_array()
            .unwrap()
            .clone()
    };
    let queue = |host: &Host| -> Vec<(String, String)> {
        queue_items(host)
            .iter()
            .map(|item| {
                (
                    item["id"].as_str().unwrap().to_string(),
                    item["text"].as_str().unwrap().to_string(),
                )
            })
            .collect()
    };
    let pending = |host: &Host, id: &str| -> Value {
        let feed = host.get(&format!("/log?session={session}&delivery_since=0"));
        let updates = feed["delivery_updates"].as_array().unwrap().clone();
        let update = updates.iter().find(|update| update["message_id"] == id);
        update.map_or(Value::Null, |update| update["delivery"]["state"].clone())
    };

    // The first message starts at once, and is in the conversation at once.
    let first = host.send(session, "one", "m1");
    assert_eq!(first["queued"], false, "{first}");
    wait_for("the first turn to run", || taken().len() == 1);
    assert_eq!(users(&host), ["u-m1"]);
    assert!(host.turns(session)[0].get("sent_at").is_none());

    // The user, a trigger and an agent that names nobody each send while it runs: each waits.
    let sends = [
        ("m2", "two", json!({})),
        ("t3", "three", json!({ "origin": "schedule" })),
        ("a4", "four", json!({ "origin": "agent" })),
    ];
    for (depth, (id, text, more)) in sends.iter().enumerate() {
        let mut body = json!({ "session": session, "text": text, "client_msg_id": id });
        body.as_object_mut()
            .unwrap()
            .extend(more.as_object().unwrap().clone());
        let sent = host.post("/send", body);
        assert_eq!(
            (&sent["queued"], &sent["queue_depth"]),
            (&json!(true), &json!(depth + 1)),
            "{sent}"
        );
    }
    let waiting = |list: &[(&str, &str)]| -> Vec<(String, String)> {
        list.iter()
            .map(|(id, text)| (id.to_string(), text.to_string()))
            .collect()
    };
    assert_eq!(
        queue(&host),
        waiting(&[("m2", "two"), ("t3", "three"), ("a4", "four")])
    );
    assert_eq!(
        rows(&host),
        [("u-m1".to_string(), "user".to_string())],
        "nothing that waits is a row"
    );
    assert_eq!(
        pending(&host, "u-m2"),
        "pending_launch",
        "the user's is shown pending"
    );
    assert_eq!(
        pending(&host, "u-t3"),
        Value::Null,
        "a trigger's has no receipt"
    );

    // The same id again: one item.
    let again = host.send(session, "two", "m2");
    assert_eq!(again["ok"], true, "{again}");
    assert_eq!(queue(&host).len(), 3);

    let later = host.post(
        "/send",
        json!({ "session": session, "text": "later", "client_msg_id": "l6", "queue_if_busy": true }),
    );
    assert_eq!(later["queued"], true, "{later}");
    let items = queue_items(&host);
    let waiting_item = &items[0];
    let later_item = &items[3];
    let sent_at = waiting_item["sent_at"].clone();
    for (item, id, kind) in [(waiting_item, "m2", "waiting"), (later_item, "l6", "later")] {
        assert_eq!(item["id"], id);
        assert_eq!(item["client_msg_id"], id);
        assert_eq!(item["kind"], kind);
        let sent_at_ms = item["enqueued_at"].as_i64().unwrap();
        assert_eq!(item["sent_at"], clarp_host::wire::iso(sent_at_ms));
        wait_for("queued item in SSE", || {
            queue_event(&heard, id, false, None).is_some()
        });
        let event = queue_event(&heard, id, false, None).unwrap();
        assert_eq!(event["client_msg_id"], item["client_msg_id"]);
        for key in ["text", "sent_at", "kind"] {
            assert!(event.get(key).is_none(), "{key}: {event}");
        }
    }
    assert_eq!(host.ask("DELETE", "/turn-queue/l6", None).0, 200);

    // Reworded and taken back while it waits.
    let (status, reworded) = host.ask(
        "PUT",
        "/turn-queue/m2",
        Some(&json!({ "text": "two, better" })),
    );
    assert_eq!((status, &reworded["ok"]), (200, &json!(true)), "{reworded}");
    wait_for("edit in SSE", || {
        queue_event(&heard, "m2", false, Some("edited")).is_some()
    });
    let edit = queue_event(&heard, "m2", false, Some("edited")).unwrap();
    assert_eq!(edit["text"], "two, better");
    assert!(edit.get("kind").is_none(), "{edit}");
    assert!(edit.get("sent_at").is_none(), "{edit}");
    assert_eq!(queue_items(&host)[0]["sent_at"], sent_at);
    let (status, withdrawn) = host.ask("DELETE", "/turn-queue/a4", None);
    assert_eq!(
        (status, &withdrawn["ok"]),
        (200, &json!(true)),
        "{withdrawn}"
    );
    wait_for("withdrawal in SSE", || {
        queue_event(&heard, "a4", false, Some("deleted")).is_some()
    });
    let deletion = queue_event(&heard, "a4", false, Some("deleted")).unwrap();
    assert!(deletion.get("text").is_none(), "{deletion}");
    assert_eq!(
        queue(&host),
        waiting(&[("m2", "two, better"), ("t3", "three")])
    );
    assert_eq!(users(&host), ["u-m1"]);

    // Killed between the acceptance and the start: what waits waits on, and is no row.
    host.kill();
    let host = place.start_in("", &[], &world);
    let heard = listen(&host);
    assert_eq!(
        queue(&host),
        waiting(&[("m2", "two, better"), ("t3", "three")])
    );
    assert_eq!(users(&host), ["u-m1"]);
    assert_eq!(queue_items(&host)[0]["sent_at"], sent_at);

    // The first answers: the second starts, on its new words, and only now is a row, below
    // the answer that did not read it.
    answer(0);
    wait_for("the second turn to run", || taken().len() == 2);
    assert!(taken()[1].1.contains("two, better"), "{:?}", taken());
    wait_for("the second message's row", || {
        users(&host) == ["u-m1", "u-m2"]
    });
    let now = rows(&host);
    assert_eq!(
        now.iter()
            .map(|(_, role)| role.as_str())
            .collect::<Vec<_>>(),
        ["user", "assistant", "user"],
        "{now:?}"
    );
    let row = host
        .turns(session)
        .into_iter()
        .find(|turn| turn["id"] == "u-m2")
        .unwrap();
    assert_eq!(row["text"], "two, better", "{row}");
    assert_eq!(row["sent_at"], sent_at, "{row}");
    assert!(
        row["timestamp"].as_str().unwrap() > sent_at.as_str().unwrap(),
        "{row}"
    );
    wait_for("started item in SSE", || {
        queue_event(&heard, "m2", true, None).is_some()
    });
    let started = queue_event(&heard, "m2", true, None).unwrap();
    assert_eq!(started["client_msg_id"], "m2");
    assert_eq!(started["queue_depth"], 1);
    assert!(started.get("text").is_none(), "{started}");
    assert_eq!(queue(&host), waiting(&[("t3", "three")]));
    assert_eq!(pending(&host, "u-m2"), "handed_over");

    // Too late to reword it or take it back: it runs.
    let (status, _) = host.ask("PUT", "/turn-queue/m2", Some(&json!({ "text": "late" })));
    assert_eq!(status, 404);
    let (status, _) = host.ask("DELETE", "/turn-queue/m2", None);
    assert_eq!(status, 404);

    // The user stops it: the trigger's waits on, held, and is no row. What the user says
    // after the Stop runs ahead of it, and enters when its turn starts.
    host.post("/stop", json!({ "session": session }));
    wait_for("the second turn to end", || {
        host.state(session) == "interrupted"
    });
    assert_eq!(queue(&host), waiting(&[("t3", "three")]));
    let after = host.send(session, "five", "m5");
    assert_eq!(after["ok"], true, "{after}");
    wait_for("the fifth to run", || taken().len() == 3);
    assert!(taken()[2].1.contains("five"), "{:?}", taken());
    assert_eq!(users(&host), ["u-m1", "u-m2", "u-m5"]);
    assert_eq!(queue(&host), waiting(&[("t3", "three")]));
    answer(2);
    let deadline = Instant::now() + Duration::from_secs(30);
    loop {
        let all = rows(&host);
        if all.last().is_some_and(|(_, role)| role == "assistant") {
            break;
        }
        assert!(
            Instant::now() < deadline,
            "timed out waiting for the fifth's answer: {all:?}{}",
            what_the_hosts_said()
        );
        std::thread::sleep(Duration::from_millis(50));
    }
    assert_eq!(users(&host), ["u-m1", "u-m2", "u-m5"], "held, it is no row");

    // Let go: the trigger's runs, and enters at its start, below the answer before it.
    host.post("/turn-queue/resume", json!({ "session": session }));
    wait_for("the trigger's turn to run", || taken().len() == 4);
    wait_for("its row", || users(&host).len() == 4);
    answer(3);
    wait_for("its answer", || {
        let all = rows(&host);
        all.len() >= 2 && all[all.len() - 2].0 == "u-t3" && all[all.len() - 1].1 == "assistant"
    });
    let all = rows(&host);
    assert_eq!(users(&host), ["u-m1", "u-m2", "u-m5", "u-t3"], "{all:?}");
    for id in ["u-m1", "u-m5", "u-t3"] {
        let at = all.iter().position(|(row, _)| row == id).unwrap();
        assert_eq!(
            all[at + 1].1,
            "assistant",
            "{id} is answered below it: {all:?}"
        );
    }
    assert!(queue(&host).is_empty());
    assert_eq!(taken().len(), 4, "the taken back never ran");
}
