//! R182: queue items and their mutation events say who sent them.
use super::*;

#[test]
fn queued_items_and_mutation_events_keep_their_origin_and_resolve_the_current_sender() {
    let place = Place::new("queue-provenance");
    let host = place.start_with_token("admin");
    let target = host.new_agent("Receiver");
    let mike = host.new_agent("Mike");
    let mike_id = row_id(&host, &mike);
    let (device, _) = pair(&host, "Another device", "full");
    let heard = host.listen_at("/events?token=admin");
    host.send(&target, "sleep 60", "running");
    wait_for("the target to be busy", || {
        host.state(&target) == "thinking"
    });

    let enqueue = |id: &str, origin: &str, sender: Value, token: &str| {
        let (status, reply) = host.ask_as(
            token,
            "POST",
            "/send",
            Some(&json!({
                "session": target, "text": id, "client_msg_id": id, "queue_if_busy": true,
                "origin": origin, "sender": sender,
            })),
        );
        assert!((200..300).contains(&status), "{status}: {reply}");
    };
    enqueue("phone", "user", Value::Null, &device);
    enqueue("agent", "agent", json!(mike_id), "admin");
    enqueue("oracle", "oracle", Value::Null, "admin");
    enqueue("automation", "automation", Value::Null, "admin");

    let queue = || host.get(&format!("/turn-queue?session={target}"));
    let item = |id: &str| {
        queue()["items"]
            .as_array()
            .unwrap()
            .iter()
            .find(|item| item["id"] == id)
            .unwrap()
            .clone()
    };
    let provenance = |row: Value| json!({"origin": row.get("origin").unwrap(), "sender": row.get("sender").unwrap()});
    for origin in ["user", "oracle", "automation"] {
        let id = if origin == "user" { "phone" } else { origin };
        assert_eq!(
            provenance(item(id)),
            json!({"origin": origin, "sender": null})
        );
    }
    assert_eq!(item("phone")["client_msg_id"], "phone");
    assert_eq!(item("phone")["text"], "phone");
    assert_eq!(
        item("agent")["sender"],
        json!({"agent_id": mike_id, "name": "Mike"})
    );

    let joined = || {
        heard
            .lock()
            .unwrap()
            .iter()
            .find(|event| {
                event["type"] == "queue-updated"
                    && event["session"] == target
                    && event["client_msg_id"] == "agent"
                    && event["queue_started"] == false
                    && event.get("queue_change").is_none()
            })
            .cloned()
    };
    wait_for("the named sender's joined event", || joined().is_some());
    assert_eq!(
        provenance(joined().unwrap()),
        json!({"origin":"agent","sender":{"agent_id":mike_id,"name":"Mike"}})
    );

    host.post("/agent-rename", json!({"session": mike, "name": "Michael"}));
    assert_eq!(item("agent")["sender"]["name"], "Michael");
    assert_eq!(
        host.ask(
            "PUT",
            "/turn-queue/agent",
            Some(&json!({"text": "changed"}))
        )
        .0,
        200
    );
    let change = |change: &str, id: &str| {
        heard
            .lock()
            .unwrap()
            .iter()
            .find(|event| {
                event["type"] == "queue-updated"
                    && event["session"] == target.as_str()
                    && event["client_msg_id"] == id
                    && event["queue_change"] == change
            })
            .cloned()
    };
    wait_for("the edit event", || change("edited", "agent").is_some());
    let edited = change("edited", "agent").unwrap();
    assert_eq!(edited["text"], "changed");
    assert_eq!(
        provenance(edited),
        json!({"origin": "agent", "sender": {"agent_id": mike_id, "name": "Michael"}})
    );

    assert_eq!(host.ask("DELETE", &format!("/agents/{mike}"), None).0, 200);
    assert_eq!(
        item("agent")["sender"],
        json!({"agent_id": mike_id, "name": null})
    );
    assert_eq!(host.ask("DELETE", "/turn-queue/agent", None).0, 200);
    assert_eq!(host.ask("DELETE", "/turn-queue/phone", None).0, 200);
    wait_for("both deletion events", || {
        change("deleted", "agent").is_some() && change("deleted", "phone").is_some()
    });
    assert_eq!(
        provenance(change("deleted", "agent").unwrap()),
        json!({"origin": "agent", "sender": {"agent_id": mike_id, "name": null}})
    );
    assert_eq!(
        provenance(change("deleted", "phone").unwrap()),
        json!({"origin": "user", "sender": null})
    );
    host.post("/stop", json!({"session": target}));
}

#[test]
fn a_new_oracle_send_after_stop_runs_then_releases_what_waited() {
    a_fresh_send_releases_the_held_queue("oracle", true);
}

#[test]
fn the_users_stop_starts_waiting_user_and_oracle_sends_in_order() {
    let place = Place::new("stop-starts-user-waiting");
    let host = place.start();
    let target = host.new_agent("Receiver");
    let sender = host.new_agent("Sender");
    let sender_id = row_id(&host, &sender);
    let events = host.listen();
    host.send(&target, "sleep 60", "running");
    wait_for("the target to be busy", || {
        host.state(&target) == "thinking"
    });
    for (id, origin, sender) in [
        ("agent", "agent", json!(sender_id)),
        ("first", "user", Value::Null),
        ("second", "oracle", Value::Null),
        ("third", "user", Value::Null),
    ] {
        let queued = host.post(
            "/send",
            json!({
                "session": target, "text": id, "client_msg_id": id,
                "queue_if_busy": true, "origin": origin, "sender": sender,
            }),
        );
        assert_eq!(queued["queued"], true, "{queued}");
    }

    let stopped = host.post("/stop", json!({"session": target, "actor": "agent:sender"}));
    assert_eq!(stopped["terminated"], 1);
    wait_for("the target to be stopped", || {
        host.state(&target) == "interrupted"
    });
    let queue = || host.get(&format!("/turn-queue?session={target}"));
    let held = queue();
    assert_eq!(held["paused"], true);
    assert_eq!(held["items"].as_array().unwrap().len(), 4);
    assert_eq!(host.turns(&target).len(), 1);

    // From an already-paused idle agent. Neither a new send nor Resume intervenes.
    let stopped = host.post("/stop", json!({"session": target}));
    assert_eq!(stopped["queue_paused"], true);
    wait_for("the third released message to be answered", || {
        host.conversation(&target)
            .contains(&said("assistant", "third"))
            && host.state(&target) == "done"
    });
    let rows = host.turns(&target);
    let prompts: Vec<_> = rows
        .iter()
        .filter(|row| row["role"] == "user")
        .map(|row| row["id"].as_str().unwrap())
        .collect();
    assert_eq!(prompts, ["u-running", "u-first", "u-second", "u-third"]);
    let held = queue();
    assert_eq!(held["paused"], true);
    assert_eq!(held["items"].as_array().unwrap().len(), 1);
    assert_eq!(held["items"][0]["id"], "agent");
    wait_for(
        "the queue start to be broadcast with its remaining pause",
        || {
            events.lock().unwrap().iter().any(|event| {
                event["type"] == "queue-updated"
                    && event["session"] == target
                    && event["client_msg_id"] == "third"
                    && event["queue_started"] == true
                    && event["queue_paused"] == true
                    && event["queue_depth"] == 1
            })
        },
    );
}

/// Issue 732: a goal's check-in is told by what waits (origin `automation` under a wake's id),
/// not by an id alone. Its words are not changed; a person's message sent under such an id
/// keeps the right to new words; and such an id that names nothing waiting is not there.
#[test]
fn only_a_waiting_goal_check_in_refuses_new_words() {
    let place = Place::new("queue-goal-reword");
    let host = place.start_with_token("admin");
    let target = host.new_agent("Receiver");
    let (device, _) = pair(&host, "A phone", "full");
    host.send(&target, "sleep 60", "running");
    wait_for("the target to be busy", || {
        host.state(&target) == "thinking"
    });
    let enqueue = |id: &str, origin: &str, token: &str| {
        let (status, reply) = host.ask_as(
            token,
            "POST",
            "/send",
            Some(&json!({
                "session": target, "text": id, "client_msg_id": id, "queue_if_busy": true,
                "origin": origin,
            })),
        );
        assert!((200..300).contains(&status), "{status}: {reply}");
    };
    enqueue("task-goal-wake", "automation", "admin");
    enqueue("task-goal-typed", "user", &device);
    let put = |id: &str| {
        host.ask(
            "PUT",
            &format!("/turn-queue/{id}"),
            Some(&json!({ "text": "new words" })),
        )
    };
    assert_eq!(
        put("task-goal-wake"),
        (
            409,
            json!({ "error": "a goal's check-in is the Host's own and cannot be edited" })
        )
    );
    let (status, reply) = put("task-goal-typed");
    assert_eq!(status, 200, "{reply}");
    assert_eq!(
        put("task-goal-gone"),
        (404, json!({ "error": "queued message not found" }))
    );
    let items = host.get(&format!("/turn-queue?session={target}"))["items"].clone();
    let text = |id: &str| {
        items
            .as_array()
            .unwrap()
            .iter()
            .find(|item| item["id"] == id)
            .map(|item| item["text"].clone())
    };
    assert_eq!(text("task-goal-wake"), Some(json!("Goal check-in")));
    assert_eq!(text("task-goal-typed"), Some(json!("new words")));
}
