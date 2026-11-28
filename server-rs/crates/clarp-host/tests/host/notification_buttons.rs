//! A request's push offers its answers as buttons (feature `notification_events`): an approval
//! its yes and no with the card's words, a choice its options in their order, each with what a
//! button needs to answer without reading the card (`decision_id`, `revision`, `response_type`)
//! and the category whose actions the app registered. Taken from what the relay is handed.

use super::*;

#[test]
fn an_approval_and_a_choice_are_pushed_with_their_answers_as_buttons() {
    let relay = StandInRelay::start();
    let place = Place::new("push-buttons");
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
        Some(&json!({ "token": "b2".repeat(32), "session": "echo", "push_grant": grant })),
    );
    assert_eq!(registered, (200, json!({ "ok": true })));
    let session = host.new_agent("Echo");
    let server_id = host.get("/server-info")["server_id"].clone();
    assert!(server_id.as_str().is_some_and(|id| !id.is_empty()));

    // The alert the relay was handed for a request, by the request's id.
    let pushed_for = |decision_id: &str| {
        relay.taken().into_iter().find_map(|(_, _, body)| {
            let notification = &body["notifications"][0];
            (notification["pushType"] == "alert"
                && notification["payload"]["decision_id"] == decision_id)
                .then(|| notification.clone())
        })
    };
    let ask = |asked: Value| {
        let (status, answer) = host.ask("POST", "/decisions", Some(&asked));
        assert_eq!(status, 201, "{answer}");
        let decision = answer["artifact"]["decision"].clone();
        let id = decision["decision_id"].as_str().unwrap().to_string();
        wait_for("the push of the request", || pushed_for(&id).is_some());
        (answer["artifact"].clone(), pushed_for(&id).unwrap())
    };

    let (approval, approval_push) = ask(json!({
        "session": session, "title": "Release", "question": "Merge the release branch?",
        "response_type": "approval", "yes_label": "Merge",
    }));
    let (choice, choice_push) = ask(json!({
        "session": session, "title": "Release", "question": "Which way?",
        "response_type": "single_choice",
        "options": [
            {"id": "ship", "label": "Ship"},
            {"id": "wait", "label": "Wait"},
            {"id": "drop", "label": "Drop"},
        ],
    }));

    // What a phone is handed, whole: printed so its pair can be kept as a fixture.
    for (name, artifact, notification) in [
        ("approval", &approval, &approval_push),
        ("single_choice", &choice, &choice_push),
    ] {
        println!(
            "PUSH-RECEIPT {name} {}",
            json!({ "server_id": server_id, "artifact_id": artifact["artifact_id"],
                    "decision": artifact["decision"], "notification": notification })
        );
    }

    for (artifact, notification, response_type, category, options) in [
        (
            &approval,
            &approval_push,
            "approval",
            "decision-approval",
            json!([{"id": "accepted", "label": "Merge"}, {"id": "rejected", "label": "No"}]),
        ),
        (
            &choice,
            &choice_push,
            "single_choice",
            "decision-choice-3",
            json!([
                {"id": "ship", "label": "Ship"},
                {"id": "wait", "label": "Wait"},
                {"id": "drop", "label": "Drop"},
            ]),
        ),
    ] {
        let decision = &artifact["decision"];
        let decision_id = decision["decision_id"].as_str().unwrap();
        // The envelope: an alert at once, under the request's own name, so a later push of
        // the same request takes its place.
        assert_eq!(
            (
                &notification["pushType"],
                &notification["priority"],
                &notification["collapseId"]
            ),
            (
                &json!("alert"),
                &json!("10"),
                &json!(format!("decision-{decision_id}"))
            )
        );
        let payload = &notification["payload"];
        assert_eq!(payload["notification_id"], notification["collapseId"]);
        assert_eq!(payload["server_instance_id"], server_id);
        assert_eq!(payload["event"], "needs_you");
        assert_eq!(payload["agent_id"], artifact["agent_id"]);
        assert_eq!(payload["decision_id"], decision_id);
        assert_eq!(payload["artifact_id"], artifact["artifact_id"]);
        assert_eq!(payload["response_type"], response_type);
        // A request is pushed when it is made: its first revision, the one a button answers.
        assert_eq!(payload["revision"], decision["revision"]);
        assert_eq!(payload["revision"], 1);
        assert_eq!(payload["options"], options, "in the card's order");
        assert_eq!(payload["aps"]["category"], category);
        assert_eq!(payload["aps"]["sound"], "default");
        assert_eq!(payload["aps"]["thread-id"], session.as_str());
        assert_eq!(payload["aps"]["alert"]["title"], "Echo");
        assert_eq!(payload["aps"]["alert"]["subtitle"], "Release");
    }
    assert_eq!(
        approval_push["payload"]["aps"]["alert"]["body"],
        "Merge the release branch?"
    );
    assert_eq!(choice_push["payload"]["aps"]["alert"]["body"], "Which way?");
}
