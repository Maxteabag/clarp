//! Requests between agents against the Python Host's.
//!
//! `fixtures/request-vectors.json` is written by `server-rs/tools/request-vectors.py` from
//! `server/lib/peer_requests.py`: what an agent that is asked is told, what the asker is told
//! of a reply, and the names the messages and the goal's dependency go by. The same inputs
//! must come to the same words here.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_kernel::requests::{
    DEFAULT_DEADLINE_S, KINDS, MAX_DEADLINE_S, MAX_EVIDENCE, MAX_UPDATES, NOT_DELIVERED, Who,
    dependency_key, evidence, is_request_id, reply_client_id, reply_text, request_client_id,
    request_text,
};
use serde_json::{Value, json};

fn who(agent: &Value) -> Who<'_> {
    Who {
        persona: agent["persona"].as_str().unwrap(),
        session: agent["session"].as_str().unwrap(),
    }
}

#[test]
fn a_request_and_its_replies_are_worded_as_the_python_host_words_them() {
    let vectors: Value =
        serde_json::from_str(include_str!("fixtures/request-vectors.json")).unwrap();
    assert_eq!(
        vectors["limits"],
        json!({
            "kinds": KINDS, "max_updates": MAX_UPDATES, "max_deadline_s": MAX_DEADLINE_S,
            "default_deadline_s": DEFAULT_DEADLINE_S, "max_evidence": MAX_EVIDENCE,
            "not_delivered": NOT_DELIVERED,
        })
    );
    let text = |vector: &Value, key: &str| vector[key].as_str().unwrap().to_string();
    let requests = vectors["requests"].as_array().unwrap();
    for vector in requests {
        let id = text(vector, "request_id");
        assert_eq!(
            request_text(&id, &who(&vector["requester"]), &text(vector, "text")),
            text(vector, "said")
        );
        assert_eq!(request_client_id(&id), text(vector, "client_id"));
        assert_eq!(dependency_key(&id), text(vector, "dependency"));
    }
    let replies = vectors["replies"].as_array().unwrap();
    for vector in replies {
        let (id, kind, said) = (
            text(vector, "request_id"),
            text(vector, "kind"),
            text(vector, "text"),
        );
        let told = reply_text(&id, &who(&vector["replier"]), &kind, &said);
        assert_eq!(told, text(vector, "said"));
        assert_eq!(evidence(&told), text(vector, "evidence"));
        assert_eq!(
            reply_client_id(&id, &kind, &said, false),
            text(vector, "client_id")
        );
        assert_eq!(
            reply_client_id(&id, &kind, &said, true),
            text(vector, "client_id_when_another_came_first")
        );
    }
    for vector in vectors["ids"].as_array().unwrap() {
        assert_eq!(
            is_request_id(vector["given"].as_str().unwrap()),
            vector["is_one"] == true,
            "{}",
            vector["given"]
        );
    }
    assert!(requests.len() >= 20 && replies.len() >= 70);
}

#[test]
fn only_proven_unrun_requests_are_undelivered() {
    use clarp_kernel::requests::unrun_reason;

    let vectors: Vec<Value> =
        serde_json::from_str(include_str!("fixtures/request-unrun-vectors.json")).unwrap();
    for row in &vectors {
        let notice = ["launch_refused", "held_send_not_run"]
            .into_iter()
            .filter_map(|key| row["notices"][key].as_str())
            .find(|text| !text.is_empty());
        assert_eq!(
            unrun_reason(
                row["status"].as_str().unwrap_or_default(),
                !row["started_at"].is_null(),
                notice,
            )
            .unwrap_or_default(),
            row["reason"],
            "{}",
            row["case"]
        );
    }
    assert_eq!(vectors.len(), 11);
}
