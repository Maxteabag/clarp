//! The Host's tests of answers the user takes back (`decision_undo`), approvals answered in the
//! user's own words (`approval_custom_text`) and spoken answers (`decision_voice_match`).

use super::*;

/// An answer held with `undo_ms` says so and can be taken back until it goes; a second undo
/// changes nothing; once the agent has the answer, it is too late. An approval takes the user's
/// own words, and the tools' token may not take an answer back.
#[test]
fn a_held_answer_is_taken_back_until_the_agent_has_it() {
    let place = Place::new("decision-undo");
    let host = place.start_with_token("undo-owner");
    let made = host.post(
        "/agents",
        json!({ "name": "Undo Fixture", "session": "undo", "backend": "scripted" }),
    );
    assert_eq!(made["session"], "undo", "{made}");
    let ask = |more: Value| -> (String, String) {
        let mut body = json!({ "session": "undo", "title": "Approval", "question": "Ship it?" });
        body.as_object_mut()
            .unwrap()
            .extend(more.as_object().unwrap().clone());
        let (status, made) = host.ask("POST", "/decisions", Some(&body));
        assert_eq!(status, 201, "{made}");
        let id = made["artifact"]["decision"]["decision_id"]
            .as_str()
            .unwrap();
        let artifact = made["artifact"]["artifact_id"].as_str().unwrap();
        (id.to_string(), artifact.to_string())
    };
    let resolve =
        |id: &str, body: Value| host.ask("POST", &format!("/decisions/{id}/resolve"), Some(&body));
    let undo = |id: &str, revision: i64| {
        host.ask(
            "POST",
            &format!("/decisions/{id}/undo"),
            Some(&json!({ "expected_revision": revision })),
        )
    };

    let (id, artifact_id) = ask(json!({}));
    // Approval rows offer the user's own words on a Host with the feature.
    let shown = &host.get(&format!("/artifacts/{artifact_id}"))["artifact"];
    assert_eq!(shown["decision"]["allow_custom_text"], true, "{shown}");
    let refused = resolve(
        &id,
        json!({ "choice": "accepted", "expected_revision": 1, "undo_ms": 20_000 }),
    );
    assert_eq!(refused.0, 400, "{refused:?}");
    let before = host_now();
    let (status, said) = resolve(
        &id,
        json!({ "choice": "accepted", "expected_revision": 1, "undo_ms": 15_000 }),
    );
    assert_eq!(status, 200, "{said}");
    let decision = &said["artifact"]["decision"];
    assert_eq!(decision["status"], "accepted");
    assert_eq!(decision["undoable"], true, "{said}");
    let until = decision["undo_until"].as_i64().unwrap();
    assert!(
        until >= before + 14_000 && until <= host_now() + 15_000,
        "{until}"
    );
    let revision = decision["revision"].as_i64().unwrap();
    let shown = &host.get(&format!("/artifacts/{artifact_id}"))["artifact"];
    assert_eq!(shown["decision"]["undoable"], true, "{shown}");

    // The tools' token never takes the user's answer back.
    let tools = clarp_host::tool_token("undo-owner");
    let body = json!({ "expected_revision": revision }).to_string();
    let got = fetch(
        &host,
        &tools,
        "POST",
        &format!("/decisions/{id}/undo"),
        &[("Content-Type", "application/json")],
        body.as_bytes(),
    );
    assert_eq!(got.status, 403, "{}", String::from_utf8_lossy(&got.body));

    let (status, back) = undo(&id, revision);
    assert_eq!(status, 200, "{back}");
    assert_eq!(back["undone"], true);
    let decision = &back["artifact"]["decision"];
    assert_eq!(decision["status"], "pending", "{back}");
    assert_eq!(decision["answer"], Value::Null);
    assert_eq!(decision["revision"], revision + 1);
    assert!(decision.get("undoable").is_none(), "{back}");
    assert_eq!(back["artifact"]["status"], "active");
    let (status, again) = undo(&id, revision + 1);
    assert_eq!(status, 200, "{again}");
    assert_eq!(again["undone"], false);
    // Nothing of the answer taken back reached the agent.
    assert!(!host.turns("undo").iter().any(|row| {
        row["text"]
            .as_str()
            .is_some_and(|text| text.contains("[Clarp decision resolved]"))
    }),);

    // Answered again with a short hold, the answer goes to the agent when the hold ends, and
    // then it is too late.
    let (status, said) = resolve(
        &id,
        json!({ "answer": { "text": "Not today, ask me tomorrow" }, "expected_revision": revision + 1, "undo_ms": 300 }),
    );
    assert_eq!(status, 200, "{said}");
    assert_eq!(said["artifact"]["decision"]["status"], "answered");
    let after = said["artifact"]["decision"]["revision"].as_i64().unwrap();
    wait_for("the held answer to go", || {
        let shown = &host.get(&format!("/artifacts/{artifact_id}"))["artifact"];
        shown["decision"]["delivery_pending"] == false
            && shown["decision"].get("undoable").is_none()
    });
    wait_for("the agent to be told", || {
        host.turns("undo").iter().any(|row| {
            row["text"].as_str().is_some_and(|text| {
                text.contains("neither approved nor rejected")
                    && text.contains("Not today, ask me tomorrow")
            })
        })
    });
    let (status, late) = undo(&id, after);
    assert_eq!(
        (status, late["error"].as_str()),
        (409, Some("too_late")),
        "{late}"
    );

    // An answer without `undo_ms` is as it always was.
    let (other, _) = ask(json!({ "question": "Ship the other?" }));
    let (status, said) = resolve(
        &other,
        json!({ "choice": "rejected", "expected_revision": 1 }),
    );
    assert_eq!(status, 200, "{said}");
    assert!(
        said["artifact"]["decision"].get("undoable").is_none(),
        "{said}"
    );
}

/// A held answer that became a message waiting behind the agent's running turn is taken back
/// from the queue; the next answer reaches the agent as a message of its own.
#[test]
fn an_answer_waiting_in_the_queue_is_taken_back_from_it() {
    let place = Place::new("decision-undo-queue");
    let host = place.start();
    let made = host.post(
        "/agents",
        json!({ "name": "Queue Fixture", "session": "queued", "backend": "scripted" }),
    );
    assert_eq!(made["session"], "queued", "{made}");
    host.post(
        "/send",
        json!({ "session": "queued", "text": "sleep 30", "client_msg_id": "busy" }),
    );
    wait_for("the turn to run", || host.state("queued") != "done");
    let (status, made) = host.ask(
        "POST",
        "/decisions",
        Some(&json!({
            "session": "queued", "title": "Pick", "question": "Which one?",
            "response_type": "single_choice",
            "options": [{"id": "a", "label": "Pebble"}, {"id": "b", "label": "Granite"}],
        })),
    );
    assert_eq!(status, 201, "{made}");
    let id = made["artifact"]["decision"]["decision_id"]
        .as_str()
        .unwrap()
        .to_string();
    let waiting = |item: &str| {
        let queue = host.get("/turn-queue?session=queued");
        queue["items"]
            .as_array()
            .unwrap()
            .iter()
            .any(|row| row["id"] == item)
    };
    let (status, said) = host.ask(
        "POST",
        &format!("/decisions/{id}/resolve"),
        Some(&json!({ "answer": { "option_id": "b" }, "expected_revision": 1, "undo_ms": 300 })),
    );
    assert_eq!(status, 200, "{said}");
    let first = format!("decision-{id}");
    // The hold ends while the turn runs: the answer waits in the queue, and can still go.
    wait_for("the answer in the queue", || waiting(&first));
    let (status, back) = host.ask(
        "POST",
        &format!("/decisions/{id}/undo"),
        Some(&json!({ "expected_revision": 2 })),
    );
    assert_eq!(status, 200, "{back}");
    assert_eq!(back["undone"], true, "{back}");
    assert!(!waiting(&first));
    let (status, said) = host.ask(
        "POST",
        &format!("/decisions/{id}/resolve"),
        Some(&json!({ "answer": { "option_id": "a" }, "expected_revision": 3, "undo_ms": 300 })),
    );
    assert_eq!(status, 200, "{said}");
    let second = format!("decision-{id}-u1");
    wait_for("the new answer in the queue", || waiting(&second));
}

/// What a spoken answer means, read from the request and changing nothing.
#[test]
fn a_spoken_answer_is_matched_against_the_request() {
    let place = Place::new("decision-match");
    let host = place.start();
    host.post(
        "/agents",
        json!({ "name": "Match Fixture", "session": "matcher", "backend": "scripted" }),
    );
    let (status, made) = host.ask(
        "POST",
        "/decisions",
        Some(&json!({
            "session": "matcher", "title": "Pick", "question": "Which one?",
            "response_type": "single_choice",
            "options": [{"id": "a", "label": "Pebble"}, {"id": "b", "label": "Granite"}],
        })),
    );
    assert_eq!(status, 201, "{made}");
    let id = made["artifact"]["decision"]["decision_id"]
        .as_str()
        .unwrap()
        .to_string();
    let artifact_id = made["artifact"]["artifact_id"]
        .as_str()
        .unwrap()
        .to_string();
    let matched = |body: Value| host.ask("POST", &format!("/decisions/{id}/match"), Some(&body));
    assert_eq!(
        matched(json!({ "text": "velg B" })),
        (
            200,
            json!({ "kind": "option", "option_id": "b", "label": "Granite" })
        )
    );
    assert_eq!(
        matched(json!({ "text": "videre", "commands": { "next": ["videre"] } })),
        (200, json!({ "kind": "command", "command": "next" }))
    );
    assert_eq!(
        matched(json!({ "text": "B men bare for prod" })),
        (
            200,
            json!({ "kind": "custom", "text": "B men bare for prod" })
        )
    );
    assert_eq!(matched(json!({ "words": "B" })).0, 400);
    let shown = &host.get(&format!("/artifacts/{artifact_id}"))["artifact"];
    assert_eq!(shown["decision"]["status"], "pending");
    assert_eq!(shown["decision"]["revision"], 1);
}

/// Answers to a native form held with `undo_ms` are taken back and the form opens again; their
/// id is never taken again.
#[test]
fn a_held_form_answer_is_taken_back_and_the_form_opens_again() {
    let vectors: Value = serde_json::from_str(include_str!(
        "../../../clarp-kernel/tests/fixtures/render-spec-export-vectors.json"
    ))
    .unwrap();
    let place = Place::new("form-undo");
    let host = place.start_with_token("form-owner");
    host.post(
        "/agents",
        json!({ "name": "Form Fixture", "session": "former", "backend": "scripted" }),
    );
    let client = [
        ("Content-Type", "application/json"),
        ("X-Clarp-Client", "ios/12 contract=74"),
    ];
    let send = |method: &str, path: &str, body: &Value| {
        let got = fetch(
            &host,
            &host.token,
            method,
            path,
            &client,
            body.to_string().as_bytes(),
        );
        let said: Value = serde_json::from_slice(&got.body).unwrap_or(Value::Null);
        (got.status, said)
    };
    let (status, made) = send(
        "POST",
        "/artifacts",
        &json!({ "session": "former", "type": "render_spec", "title": "Decide",
                 "artifact_id": "spec-undo", "payload": vectors["spec"] }),
    );
    assert_eq!(status, 201, "{made}");
    let open_status = made["artifact"]["status"].clone();
    let answer = vectors["exports"]
        .as_array()
        .unwrap()
        .iter()
        .find(|one| one["name"] == "a spec with its accepted answer")
        .unwrap()["answer"]
        .clone();
    let mut held = answer.clone();
    held["submission_id"] = json!("undo-answer-0001");
    held["undo_ms"] = json!(15_000);
    let (status, receipt) = send("POST", "/artifacts/spec-undo/submit", &held);
    assert_eq!(status, 200, "{receipt}");
    assert_eq!(receipt["undoable"], true, "{receipt}");
    assert!(receipt["undo_until"].is_i64(), "{receipt}");
    let (status, row) = send("GET", "/artifacts/spec-undo", &Value::Null);
    assert_eq!(status, 200, "{row}");
    let row = &row["artifact"];
    assert_eq!(row["status"], "completed");
    assert_eq!(row["undoable"], true, "{row}");

    let (status, back) = send(
        "POST",
        "/artifacts/spec-undo/submissions/undo-answer-0001/undo",
        &json!({}),
    );
    assert_eq!(status, 200, "{back}");
    assert_eq!(back["undone"], true, "{back}");
    assert_eq!(back["artifact"]["status"], open_status, "{back}");
    let (status, again) = send(
        "POST",
        "/artifacts/spec-undo/submissions/undo-answer-0001/undo",
        &json!({}),
    );
    assert_eq!(
        (status, again["undone"].clone()),
        (200, json!(false)),
        "{again}"
    );
    // The id taken back is not taken again; new answers under a new id are.
    let (status, refused) = send("POST", "/artifacts/spec-undo/submit", &held);
    assert_eq!(status, 409, "{refused}");
    let mut anew = answer;
    anew["submission_id"] = json!("undo-answer-0002");
    let (status, receipt) = send("POST", "/artifacts/spec-undo/submit", &anew);
    assert_eq!(status, 200, "{receipt}");
    assert!(receipt.get("undoable").is_none(), "{receipt}");
}

/// The Host's clock as the tests read it: the wall clock.
fn host_now() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_millis() as i64
}
