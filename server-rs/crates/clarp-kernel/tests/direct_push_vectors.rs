//! A push handed to the push service itself, against the Python Host's own module.
//!
//! `fixtures/direct-push-vectors.json` is written by `server-rs/tools/direct-push-vectors.py`
//! from `server/lib/apns.py`: where the push service is, the request for one push of every
//! kind, what an answer of the service means, what a token of the Host's says and for how
//! long one is used, and when a push is handed over a second time. The same must come of
//! the same here.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_kernel::direct_push::{
    self as direct, Came, GONE_FOR_GOOD, Next, Push, Signed, Signer, TOKEN_REFUSED,
    TOKEN_USED_FOR_S, Tries,
};
use clarp_kernel::python;
use serde_json::{Map, Value, json};

fn vectors() -> Value {
    serde_json::from_str(include_str!("fixtures/direct-push-vectors.json")).unwrap()
}

fn text<'a>(value: &'a Value, key: &str) -> &'a str {
    value[key].as_str().unwrap_or("")
}

#[test]
fn the_push_service_is_found_and_asked_as_the_python_host_asks_it() {
    let vectors = vectors();
    for (environment, host) in vectors["hosts"].as_object().unwrap() {
        assert_eq!(json!(direct::host(environment)), *host, "{environment:?}");
    }
    assert_eq!(json!(GONE_FOR_GOOD), vectors["gone_for_good"]);
    assert_eq!(json!(TOKEN_REFUSED), vectors["token_refused"]);
    let mut asked = 0;
    for request in vectors["requests"].as_array().unwrap() {
        let name = text(request, "name");
        let push = Push {
            payload: &request["payload"],
            push_type: text(request, "push_type"),
            priority: text(request, "priority"),
            collapse_id: text(request, "collapse_id"),
            expiration: request["expiration"].as_i64(),
        };
        let ours = direct::request(
            "the-token",
            text(request, "bundle_id"),
            text(request, "token"),
            &push,
        );
        // Where it goes: the service for apps as they are given out, and the phone's token.
        let address = format!("{}{}", direct::PRODUCTION, ours.path);
        assert_eq!(json!(address), request["url"], "{name}");
        // What its head says, each thing once, and nothing else.
        let head: Map<String, Value> = ours
            .headers
            .iter()
            .map(|(name, value)| ((*name).to_string(), json!(value)))
            .collect();
        assert_eq!(head.len(), ours.headers.len(), "{name}");
        assert_eq!(Value::Object(head), request["headers"], "{name}");
        assert_eq!(json!(ours.body), request["body"], "{name}");
        asked += 1;
    }
    assert_eq!(asked, 11);
}

#[test]
fn an_answer_of_the_push_service_means_what_it_means_to_the_python_host() {
    let vectors = vectors();
    let mut read = 0;
    for answer in vectors["answers"].as_array().unwrap() {
        let name = text(answer, "name");
        let status = u16::try_from(answer["status"].as_u64().unwrap()).unwrap();
        let named = text(&answer["headers"], "apns-id");
        let (status, reason, named) = direct::answered(status, text(answer, "body"), named);
        assert_eq!(json!([status, reason, named]), answer["result"], "{name}");
        read += 1;
    }
    assert_eq!(read, 25);
}

#[test]
fn a_token_of_the_hosts_says_what_the_python_hosts_says_and_is_used_as_long() {
    let vectors = vectors();
    let tokens = &vectors["provider_tokens"];
    assert_eq!(json!(TOKEN_USED_FOR_S), tokens["used_for_s"]);
    // The story of one Host asking for its token again and again: with another team, another
    // key id, another file, the same file written anew, and as time passes.
    let mut held: Option<Signed> = None;
    let mut written = 0_i128;
    let (mut signed, mut asked) = (0, 0);
    for step in tokens["steps"].as_array().unwrap() {
        let name = text(step, "name");
        let now = step["now"].as_i64().unwrap();
        if step["written_anew"] == true {
            written += 5_000_000_000;
        }
        let by = Signer {
            team_id: text(step, "team_id").to_string(),
            key_id: text(step, "key_id").to_string(),
            key_file: text(step, "key_file").to_string(),
            written_ns: if text(step, "key_file") == "two" {
                written
            } else {
                0
            },
        };
        let serves = held.as_ref().is_some_and(|held| held.serves(&by, now));
        assert_eq!(json!(!serves), step["minted"], "{name}");
        if !serves {
            let (head, claims) = direct::token(&by.team_id, &by.key_id, now);
            held = Some(Signed {
                token: direct::to_sign(&head, &claims),
                at_s: now,
                by,
            });
            signed += 1;
        }
        // What the token in use says: its head and its claims, as that Host's says them.
        let token = &held.as_ref().unwrap().token;
        let parts: Vec<Value> = token
            .split('.')
            .map(|part| {
                let fitted = part.replace('-', "+").replace('_', "/");
                let filled = format!("{fitted}{}", "=".repeat((4 - fitted.len() % 4) % 4));
                let bytes = python::b64decode(&filled).unwrap();
                serde_json::from_slice(&bytes).unwrap()
            })
            .collect();
        assert_eq!(
            parts,
            [step["header"].clone(), step["claims"].clone()],
            "{name}"
        );
        assert!(!token.contains(['=', '+', '/', ' ']), "{name}: {token}");
        asked += 1;
    }
    assert_eq!((asked, signed), (11, 7));
    // Bytes as a token carries them: every sign of the alphabet, and nothing to fill up.
    assert_eq!(direct::base64url(&[0xfb, 0xff, 0xfe]), "-__-");
    assert_eq!(direct::base64url(b"a"), "YQ");
}

#[test]
fn a_push_is_handed_over_a_second_time_where_the_python_host_does() {
    let vectors = vectors();
    let mut stories = 0;
    for story in vectors["second_tries"].as_array().unwrap() {
        let name = text(story, "name");
        let mut tries = Tries::default();
        let (mut token, mut sent_with) = (1, Vec::new());
        let mut result = Value::Null;
        for answer in story["answers"].as_array().unwrap() {
            sent_with.push(format!("bearer token-{token}"));
            let (came, reason) = match answer.get("status") {
                Some(status) => {
                    let status = u16::try_from(status.as_u64().unwrap()).unwrap();
                    let (status, reason, named) =
                        direct::answered(status, text(answer, "body"), "");
                    result = json!([status, reason, named]);
                    (Came::Answered(status), reason)
                }
                None => {
                    result = json!({ "raised": answer["raised"] });
                    let came = if answer["closed"] == true {
                        Came::Closed
                    } else {
                        Came::Failed
                    };
                    (came, Value::Null)
                }
            };
            match tries.after(came, reason.as_str().unwrap_or("")) {
                Next::Done => break,
                Next::AgainOverANewConnection => {}
                Next::AgainWithANewToken => token += 1,
            }
        }
        assert_eq!(json!(sent_with), story["sent_with"], "{name}");
        assert_eq!(result, story["result"], "{name}");
        stories += 1;
    }
    assert_eq!(stories, 13);
}
