//! A chat's snooze kept on the Host, the same on every app (feature `snooze`, issue 871).

use super::*;

/// The newest `snooze-changed` event of this agent.
fn told(heard: &Arc<Mutex<Vec<Value>>>, session: &str) -> Option<Value> {
    heard
        .lock()
        .unwrap()
        .iter()
        .rev()
        .find(|event| {
            event["type"] == "agent-roster"
                && event["kind"] == "snooze-changed"
                && event["session"] == session
        })
        .cloned()
}

fn row(host: &Host, session: &str) -> Value {
    host.get("/agents/snapshot")["agents"]
        .as_array()
        .unwrap()
        .iter()
        .find(|row| row["session"] == session)
        .cloned()
        .unwrap()
}

/// A POST with headers of its own, as the Host's own token.
fn asked(host: &Host, path: &str, headers: &[(&str, &str)], body: &Value) -> (u16, Value) {
    let mut all = vec![("Content-Type", "application/json")];
    all.extend_from_slice(headers);
    let body = body.to_string();
    let got = fetch(host, &host.token, "POST", path, &all, body.as_bytes());
    (
        got.status,
        serde_json::from_slice(&got.body).unwrap_or(Value::Null),
    )
}

fn now_ms() -> i64 {
    let now = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH);
    i64::try_from(now.unwrap().as_millis()).unwrap()
}

/// The owner snoozes a chat: its row says until when, every app is told with the field, and
/// it ends when the agent answers, when its moment comes, or when the owner ends it. A turn of
/// an agent may not snooze, and a moment that has passed is refused.
#[test]
fn a_snooze_is_the_hosts_and_ends_when_the_agent_writes_or_its_moment_comes() {
    let place = Place::new("snooze");
    let host = place.start();
    let heard = host.listen();
    let session = host.new_agent("Drowse");
    assert_eq!(row(&host, &session)["snoozed_until"], Value::Null);

    // Snoozed for an hour, then the agent answers: the snooze is over.
    let until = now_ms() + 3_600_000;
    let set = host.post(
        "/agent-snooze",
        json!({ "session": session, "until": until }),
    );
    assert_eq!(
        set,
        json!({ "ok": true, "session": session, "snoozed_until": until })
    );
    assert_eq!(row(&host, &session)["snoozed_until"], until);
    wait_for("the snooze told", || told(&heard, &session).is_some());
    let event = told(&heard, &session).unwrap();
    assert_eq!(
        event["fields"],
        json!({ "snoozed_until": until }),
        "{event}"
    );
    host.send(&session, "say something", "drowse-1");
    wait_for("the answer to end the snooze", || {
        row(&host, &session)["snoozed_until"].is_null()
    });
    wait_for("its end told", || {
        told(&heard, &session).is_some_and(|event| event["fields"]["snoozed_until"].is_null())
    });

    // Snoozed for a moment: it ends by itself.
    let soon = now_ms() + 1_500;
    host.post(
        "/agent-snooze",
        json!({ "session": session, "until": soon }),
    );
    assert_eq!(row(&host, &session)["snoozed_until"], soon);
    wait_for("the moment to end the snooze", || {
        row(&host, &session)["snoozed_until"].is_null()
    });

    // The owner ends one; a moment past, a turn of an agent and no agent are refused.
    host.post(
        "/agent-snooze",
        json!({ "session": session, "until": until }),
    );
    let cleared = host.post(
        "/agent-snooze",
        json!({ "session": session, "until": null }),
    );
    assert_eq!(cleared["snoozed_until"], Value::Null, "{cleared}");
    let (status, _) = host.ask(
        "POST",
        "/agent-snooze",
        Some(&json!({ "session": session, "until": now_ms() - 1 })),
    );
    assert_eq!(status, 400);
    let (status, said) = asked(
        &host,
        "/agent-snooze",
        &[("X-Clarp-Turn", "not-the-owner")],
        &json!({ "session": session, "until": until }),
    );
    assert_eq!(status, 403, "{said}");
    assert_eq!(row(&host, &session)["snoozed_until"], Value::Null);
    let (status, _) = host.ask(
        "POST",
        "/agent-snooze",
        Some(&json!({ "session": "nobody", "until": until })),
    );
    assert_eq!(status, 404);
}
