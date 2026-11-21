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
