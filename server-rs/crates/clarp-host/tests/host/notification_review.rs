//! What the owner's notifications keep to (feature `notification_events`), held through the
//! relay and the event stream: a bound agent's failed turn and request carry none of its words
//! and offer no answer from the banner, a failed turn the owner switched off is no popup for any
//! app, and every route that changes the choices changes them whole, with a version each.

use super::*;

/// A Host that pushes through a stand-in relay, with a phone paired and registered by its
/// grant. Dropped in reverse: the Host before its place and its relay.
fn pushing_host(name: &str) -> (StandInRelay, Place, Host) {
    let relay = StandInRelay::start();
    let place = Place::new(name);
    let host = place.start_with(
        "the-administrators-token",
        &["--push-relay", &format!("http://{}", relay.address)],
    );
    let (phone, _) = pair(&host, "Phone", "full");
    let grant = format!(
        "pg1.{}.{}.{}",
        "1".repeat(64),
        "2".repeat(16),
        "3".repeat(64)
    );
    let registered = host.ask_as(
        &phone,
        "POST",
        "/devices",
        Some(&json!({ "token": "c3".repeat(32), "push_grant": grant })),
    );
    assert_eq!(registered, (200, json!({ "ok": true })));
    (relay, place, host)
}

/// The alerts the relay was handed whose payload says this.
fn alerts_where(relay: &StandInRelay, key: &str, value: &str) -> Vec<Value> {
    relay
        .taken()
        .into_iter()
        .map(|(_, _, body)| body["notifications"][0].clone())
        .filter(|notification| {
            notification["pushType"] == "alert" && notification["payload"][key] == value
        })
        .collect()
}

/// An agent bound to a model provider of its own, working in this folder.
fn bound_agent_in(host: &Host, place: &Place, name: &str, folder: &std::path::Path) -> String {
    let home = crate::bound_privacy::own_home(place, &format!("{name}-home"));
    let made = host.post(
        "/agents",
        json!({
            "name": name, "backend": "codex", "model": "a-deployment-name",
            "codex_home": home, "codex_provider": "stand-in", "cwd": folder,
        }),
    );
    made["session"]
        .as_str()
        .unwrap_or_else(|| panic!("{made}"))
        .to_string()
}

#[test]
fn a_bound_agents_failed_turn_is_pushed_without_its_folder_or_words() {
    let (relay, place, host) = pushing_host("bound-failed");
    let folder = place.root.join("a-private-worktree-name");
    std::fs::create_dir_all(&folder).unwrap();
    let session = bound_agent_in(&host, &place, "Ada", &folder);
    std::fs::remove_dir_all(&folder).unwrap();

    host.send(&session, "hello", "b1");
    wait_for("the push of the failed turn", || {
        !alerts_where(&relay, "event", "failed").is_empty()
    });
    let pushed = alerts_where(&relay, "event", "failed").remove(0);
    let payload = &pushed["payload"];
    assert_eq!(payload["session"], session.as_str());
    // Nothing of what the folder is called, anywhere in what the push service is handed.
    let whole = pushed.to_string();
    assert!(
        !whole.contains("a-private-worktree-name"),
        "the folder went to the push service: {whole}"
    );
    assert_eq!(payload["aps"]["alert"]["body"], "Open Clarp to see why.");
    assert_eq!(payload["aps"]["alert"]["title"], "Ada couldn't finish");
    assert_eq!(payload["body_loc_key"], "notify.body.failed_withheld");
    assert_eq!(payload["body_loc_args"], json!([]));
    // What is safe to say is said: what kind of failure, as a code.
    assert!(payload["error_code"].is_string(), "{payload}");
    assert_eq!(payload["aps"]["category"], "turn-failed");
}

#[test]
fn a_bound_agents_request_is_opened_from_its_notification_not_answered_there() {
    let (relay, place, host) = pushing_host("bound-request");
    let folder = place.root.join("work");
    std::fs::create_dir_all(&folder).unwrap();
    let session = bound_agent_in(&host, &place, "Ada", &folder);

    let (status, asked) = host.ask(
        "POST",
        "/decisions",
        Some(&json!({
            "session": session, "title": "Release", "question": "Merge the private branch?",
            "response_type": "approval", "yes_label": "Merge it",
        })),
    );
    assert_eq!(status, 201, "{asked}");
    let decision_id = asked["artifact"]["decision"]["decision_id"]
        .as_str()
        .unwrap()
        .to_string();
    wait_for("the push of the request", || {
        !alerts_where(&relay, "decision_id", &decision_id).is_empty()
    });
    let pushed = alerts_where(&relay, "decision_id", &decision_id).remove(0);
    let payload = &pushed["payload"];
    let whole = pushed.to_string();
    for words in ["Merge the private branch", "Merge it", "Release"] {
        assert!(
            !whole.contains(words),
            "{words} went to the push service: {whole}"
        );
    }
    assert_eq!(payload["aps"]["alert"]["title"], "Ada asks");
    assert_eq!(payload["aps"]["alert"]["body"], "Open Clarp to answer.");
    // Nothing answers it from the banner: a tap opens the card.
    assert!(payload["aps"].get("category").is_none(), "{payload}");
    assert!(payload.get("options").is_none(), "{payload}");
    assert!(payload.get("response_type").is_none(), "{payload}");
    assert_eq!(payload["revision"], 1);
}

#[test]
fn a_failed_turn_switched_off_marks_its_chat_unread_and_pops_up_nothing() {
    let place = Place::new("failed-off");
    let host = place.start();
    let folder = place.root.join("gone");
    std::fs::create_dir_all(&folder).unwrap();
    let made = host.post(
        "/agents",
        json!({ "name": "Helper", "backend": "scripted", "cwd": folder }),
    );
    let session = made["session"].as_str().unwrap().to_string();
    let heard = host.listen();
    std::fs::remove_dir_all(&folder).unwrap();
    let failed_events = |kind: &str| {
        heard
            .lock()
            .unwrap()
            .iter()
            .filter(|event| {
                event["type"] == kind
                    && event["session"] == session.as_str()
                    && event["event"] == "failed"
            })
            .cloned()
            .collect::<Vec<Value>>()
    };

    // On, as it is by default: a notification every app shows.
    host.send(&session, "hello", "f1");
    wait_for("the failed turn's notification", || {
        !failed_events("user-notification").is_empty()
    });
    let on = failed_events("user-notification").remove(0);
    assert_eq!((&on["alert"], &on["unread"]), (&json!(true), &json!(true)));

    // Switched off: the chat is still marked unread, by an event no app pops up.
    host.post(
        "/notification-settings",
        json!({ "events": { "failed": false } }),
    );
    host.send(&session, "again", "f2");
    wait_for("the second failed turn's mark", || {
        !failed_events("chat-unread").is_empty()
    });
    let off = failed_events("chat-unread").remove(0);
    assert_eq!(
        (&off["alert"], &off["unread"]),
        (&json!(false), &json!(true))
    );
    assert_ne!(off["notification_id"], on["notification_id"]);
    assert_eq!(
        failed_events("user-notification").len(),
        1,
        "the turn switched off is no user-notification"
    );
}

#[test]
fn every_route_that_changes_the_choices_changes_them_whole_with_a_version_each() {
    let place = Place::new("settings-writes");
    let host = place.start();
    let session = host.new_agent("Echo");
    let heard = host.listen();
    let document = || host.get("/notification-settings");
    let call_on = |document: &Value| {
        document["events"]
            .as_array()
            .unwrap()
            .iter()
            .find(|event| event["id"] == "call")
            .unwrap()["on"]
            .clone()
    };
    let rings_enabled = || host.get("/calls/ring/settings")["settings"]["enabled"].clone();
    let first = document()["version"].as_u64().unwrap();

    // The ring switch is the global `call` choice: one change, one version.
    host.post("/calls/ring/settings", json!({ "enabled": false }));
    let after_ring = document();
    assert_eq!(after_ring["version"], first + 1);
    assert_eq!(call_on(&after_ring), false);

    // A mute is part of the choices too.
    host.post("/agent-mute", json!({ "session": session, "muted": true }));
    let after_mute = document();
    assert_eq!(after_mute["version"], first + 2);
    let muted = after_mute["agents"]
        .as_object()
        .unwrap()
        .values()
        .any(|agent| agent["session"] == session.as_str() && agent["muted"] == true);
    assert!(muted, "{after_mute}");

    // A change of the ring switch and another choice at once is one change.
    let both = host.post(
        "/notification-settings",
        json!({ "events": { "call": true, "failed": false } }),
    );
    assert_eq!(both["version"], first + 3);
    assert_eq!(call_on(&both), true);
    assert_eq!(rings_enabled(), true);

    // A refused change changes nothing, the ring switch included.
    let (status, refused) = host.ask(
        "POST",
        "/notification-settings",
        Some(&json!({ "events": { "call": false, "nope": true } })),
    );
    assert_eq!((status, &refused["error"]), (400, &json!("unknown_event")));
    assert_eq!(document()["version"], first + 3);
    assert_eq!(rings_enabled(), true);

    // Every app was told each change once, each under its own version.
    wait_for("the third change to be told", || {
        heard.lock().unwrap().iter().any(|event| {
            event["type"] == "notification-settings-changed" && event["version"] == first + 3
        })
    });
    let told: Vec<u64> = heard
        .lock()
        .unwrap()
        .iter()
        .filter(|event| event["type"] == "notification-settings-changed")
        .filter_map(|event| event["version"].as_u64())
        .collect();
    assert_eq!(told, vec![first + 1, first + 2, first + 3]);
}
