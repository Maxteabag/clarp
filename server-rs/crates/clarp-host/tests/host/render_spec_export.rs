//! The Host's tests of the export of a render spec (R137, the Python Host's PR 474): one file a
//! piece, declared in `host.rs`.

use super::*;

/// What the Python Host exports, case by case (`tools/render-spec-export-vectors.py`).
fn vectors() -> Value {
    serde_json::from_str(include_str!(
        "../../../clarp-kernel/tests/fixtures/render-spec-export-vectors.json"
    ))
    .unwrap()
}

/// The keys of an object, sorted.
fn keys(value: &Value) -> Vec<String> {
    let mut keys: Vec<String> = value.as_object().unwrap().keys().cloned().collect();
    keys.sort();
    keys
}

/// A spec's export gives the administrator what a move to another Host needs, as the Python
/// Host gives it: the row as it is listed, the payload as kept, and the accepted answer or none,
/// whatever the caller says it can draw. Every device is refused, limited or full, and the
/// administrator's token anywhere but in the Authorization header; another kind is refused with
/// its kind; an id nobody has is not found.
#[test]
fn the_administrator_exports_a_render_spec_with_its_accepted_answer_as_the_python_host_does() {
    let vectors = vectors();
    let theirs = |name: &str| -> Value {
        let found = vectors["exports"]
            .as_array()
            .unwrap()
            .iter()
            .find(|one| one["name"] == name);
        found.unwrap().clone()
    };
    let place = Place::new("spec-export");
    let host = place.start_with_token("secret");
    let made = host.post(
        "/agents",
        json!({ "name": "Theo", "session": "theo", "backend": "scripted" }),
    );
    assert_eq!(made["session"], "theo", "{made}");
    let client = [
        ("Content-Type", "application/json"),
        ("X-Clarp-Client", "ios/12 contract=74"),
    ];
    let publish = |body: &Value| {
        let got = fetch(
            &host,
            &host.token,
            "POST",
            "/artifacts",
            &client,
            body.to_string().as_bytes(),
        );
        assert_eq!(got.status, 201, "{}", String::from_utf8_lossy(&got.body));
    };
    publish(
        &json!({ "session": "theo", "type": "render_spec", "title": "Decide",
                     "artifact_id": "spec-1", "payload": vectors["spec"] }),
    );

    // As the Python Host gives it, before an answer and after one.
    let exported = |id: &str| host.ask("GET", &format!("/artifacts/{id}/export"), None);
    let before = theirs("a spec without an answer");
    let (status, ours) = exported("spec-1");
    assert_eq!(status, 200, "{ours}");
    assert_eq!(keys(&ours), keys(&before["export"]));
    assert_eq!(ours["kind"], "render_spec");
    assert_eq!(ours["render_spec"]["submission"], Value::Null);
    let answer = theirs("a spec with its accepted answer");
    let body = answer["answer"].to_string();
    let said = fetch(
        &host,
        &host.token,
        "POST",
        "/artifacts/spec-1/submit",
        &client,
        body.as_bytes(),
    );
    assert_eq!(said.status, 200, "{}", String::from_utf8_lossy(&said.body));
    let (status, ours) = exported("spec-1");
    assert_eq!(status, 200, "{ours}");
    let (ours, theirs_now) = (&ours["render_spec"], &answer["export"]["render_spec"]);
    assert_eq!(keys(ours), keys(theirs_now));
    // The row: every field the Python Host gives, and the same where neither an id nor a
    // time is in it.
    assert_eq!(
        keys(&ours["artifact"]),
        keys(&theirs_now["artifact"]),
        "{ours}"
    );
    for field in [
        "artifact_id",
        "session",
        "type",
        "title",
        "summary",
        "status",
        "reference_id",
        "payload_version",
        "pinned",
        "pinned_at",
        "archived_at",
        "deleted_at",
        "source_message_id",
        "source_trace_id",
        "agent_name",
    ] {
        assert_eq!(
            ours["artifact"][field], theirs_now["artifact"][field],
            "the row's {field}"
        );
    }
    for field in ["created_at", "updated_at", "completed_at"] {
        assert!(
            ours["artifact"][field].is_i64(),
            "the row's {field}: {ours}"
        );
    }
    // The payload as kept, whole.
    assert_eq!(ours["stored"], theirs_now["stored"]);
    // The answer: the same, but for when it was taken and whether its agent was told yet.
    let (mine, his) = (&ours["submission"], &theirs_now["submission"]);
    assert_eq!(keys(mine), keys(his), "{mine}");
    for field in [
        "submission_id",
        "version",
        "answers",
        "payload_hash",
        "synthesize_audio",
    ] {
        assert_eq!(mine[field], his[field], "the answer's {field}");
    }
    assert!(mine["accepted_at"].is_i64());
    match mine["delivery_status"].as_str() {
        Some("pending") => assert_eq!(mine["delivered_at"], Value::Null),
        Some("delivered") => assert!(mine["delivered_at"].is_i64()),
        other => panic!("a delivery status of no kind: {other:?}"),
    }

    // Whatever a device says it can draw, it is refused: limited or full.
    for scope in ["limited", "full"] {
        let (device, _) = pair(&host, &format!("Phone {scope}"), scope);
        let (status, said) = host.ask_as(&device, "GET", "/artifacts/spec-1/export", None);
        assert_eq!(
            (status, said),
            (403, json!({ "error": "administrator only" }))
        );
    }
    // The administrator's token from the Authorization header only: not in a browser's
    // cookie, nor in the address, though this Host takes it there for other routes (the
    // Python Host's PR 474 at 01ceed2d).
    let cookie = format!("claude_pwa_token={}", host.token);
    let by_cookie = [("Cookie", cookie.as_str())];
    let in_address = format!("/artifacts/spec-1/export?token={}", host.token);
    let read = fetch(&host, "", "GET", "/artifacts/spec-1", &by_cookie, b"");
    assert_eq!(read.status, 200, "a cookie lets the administrator read");
    let read = fetch(
        &host,
        "",
        "GET",
        &format!("/artifacts/spec-1?token={}", host.token),
        &[],
        b"",
    );
    assert_eq!(read.status, 200, "so does the address, from this machine");
    for (how, got) in [
        (
            "cookie",
            fetch(
                &host,
                "",
                "GET",
                "/artifacts/spec-1/export",
                &by_cookie,
                b"",
            ),
        ),
        ("address", fetch(&host, "", "GET", &in_address, &[], b"")),
    ] {
        let said: Value = serde_json::from_slice(&got.body).unwrap_or(Value::Null);
        assert_eq!(
            (got.status, said),
            (403, json!({ "error": "administrator only" })),
            "the token in the {how}"
        );
    }
    // Another kind, with its kind; an id nobody has.
    publish(
        &json!({ "session": "theo", "type": "document", "title": "Notes",
                     "artifact_id": "doc-1", "payload": { "content": "words" } }),
    );
    let other = theirs("another kind");
    let kind = other["refused_kind"].as_str().unwrap();
    assert_eq!(
        exported("doc-1"),
        (
            409,
            json!({ "error": format!("export is not available for {kind} artifacts"), "kind": kind })
        )
    );
    assert_eq!(theirs("an id nobody has")["export"], Value::Null);
    assert_eq!(
        exported("no-such-artifact"),
        (404, json!({ "error": "not found" }))
    );
}
