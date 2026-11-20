//! Coordination between agents, held to the Python Host's (`tests/fixtures/coordination-vectors.json`, written by
//! `server-rs/tools/coordination-vectors.py` from its `lib/coordination.py`, `policies/notifications.py` and
//! `send_request.py`, its PR 482): what is coordination, a row's audience, who can leave a note, whether a note
//! pages the user, and what a send's origin and sender are from what it claims and who made it.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_kernel::coordination::{
    self, AGENTS, DELEGATE_REFUSED, USER, USER_WORD_ORIGINS, can_leave_note, is_coordination,
    note_decision, row_audience,
};
use clarp_kernel::notify::{Capabilities, Decision};
use serde_json::{Value, json};

fn vectors() -> Value {
    serde_json::from_str(include_str!("fixtures/coordination-vectors.json")).unwrap()
}

fn text(value: &Value) -> Option<&str> {
    value.as_str()
}

#[test]
fn what_is_coordination_is_the_python_hosts() {
    let vectors = vectors();
    let mut words: Vec<&str> = USER_WORD_ORIGINS.to_vec();
    words.sort_unstable();
    assert_eq!(vectors["user_word_origins"], json!(words));
    let cases = vectors["coordination"].as_array().unwrap();
    let mut held = 0;
    for case in cases {
        let ours = is_coordination(
            case["agent_id"].as_str().unwrap(),
            text(&case["origin"]),
            text(&case["sender_agent_id"]),
        );
        assert_eq!(json!(ours), case["coordination"], "{case}");
        held += 1;
    }
    assert_eq!(held, 126);
}

#[test]
fn a_rows_audience_is_the_python_hosts_and_never_mixed_here() {
    let vectors = vectors();
    // The one audience of the reference that only a message steered into a running turn makes:
    // this Host steers none, and gives it to no row.
    assert_eq!(vectors["steer_only"], json!(["mixed"]));
    let cases = vectors["audience"].as_array().unwrap();
    let mut held = 0;
    for case in cases {
        let ours = row_audience(text(&case["kind"]), text(&case["trace_audience"]));
        assert!(ours == AGENTS || ours == USER, "{case}");
        assert_eq!(json!(ours), case["audience"], "{case}");
        held += 1;
    }
    assert_eq!(held, 56);
}

#[test]
fn who_can_leave_a_note_is_the_python_hosts_and_codex_here() {
    let vectors = vectors();
    let me = vectors["agent_id"].as_str().unwrap();
    let cases = vectors["can_leave_note"].as_array().unwrap();
    let (mut held, mut codex) = (0, 0);
    for case in cases {
        // The Python Host's agent of no record reads as one on its default backend; here the Host asks
        // only of an agent it has.
        let agent = case["agent_id"].as_str().unwrap_or(me);
        let backend = text(&case["backend"]);
        let ours = can_leave_note(agent, backend, true);
        let mut theirs = case["can_leave_note"].as_bool().unwrap();
        if !agent.is_empty() && coordination::backend(backend) == "codex" {
            // Registered difference: every turn of this Host carries its token, Codex's too.
            assert!(!theirs, "{case}");
            theirs = true;
            codex += 1;
        }
        assert_eq!(ours, theirs, "{case}");
        // A Host that cannot read its key gives no turn a token: no agent can leave a note, and none is hidden.
        assert!(!can_leave_note(agent, backend, false), "{case}");
        held += 1;
    }
    assert_eq!((held, codex), (21, 2));
}

#[test]
fn whether_a_note_pages_the_user_is_the_python_hosts() {
    let vectors = vectors();
    let cases = vectors["note_decision"].as_array().unwrap();
    let mut held = 0;
    for case in cases {
        let flag = |name: &str| case[name].as_bool().unwrap();
        let agent = Capabilities {
            present: flag("present"),
            can_chat: flag("can_chat"),
            muted: flag("muted"),
            is_team_leader: flag("is_team_leader"),
            quiet: flag("quiet"),
        };
        let ours = match note_decision(&agent) {
            Decision::Notify {
                reason,
                kind,
                push,
                muted,
            } => {
                json!({"notify": true, "reason": reason, "kind": kind, "push": push, "muted": muted})
            }
            Decision::Suppress { reason } => json!({"notify": false, "reason": reason}),
        };
        assert_eq!(ours, case["decision"], "{case}");
        held += 1;
    }
    assert_eq!(held, 32);
}

#[test]
fn a_sends_origin_and_sender_are_the_python_hosts_from_what_it_claims_and_who_made_it() {
    let vectors = vectors();
    let roster: Vec<(String, String)> = vectors["roster"]
        .as_array()
        .unwrap()
        .iter()
        .map(|row| {
            (
                row[0].as_str().unwrap().to_string(),
                row[1].as_str().unwrap().to_string(),
            )
        })
        .collect();
    // An agent by its session or its id, as the reference's `identity.lookup` finds it.
    let lookup = |name: &str| {
        let name = name.trim();
        roster
            .iter()
            .find(|(session, id)| name == session || name == id)
            .cloned()
    };
    let delegates: Vec<(String, String)> = vectors["delegates"]
        .as_array()
        .unwrap()
        .iter()
        .map(|pair| {
            (
                pair[0].as_str().unwrap().to_string(),
                pair[1].as_str().unwrap().to_string(),
            )
        })
        .collect();
    let cases = vectors["sends"].as_array().unwrap();
    let (mut made, mut refused, mut marked) = (0, 0, 0);
    for case in cases {
        let target = lookup(case["target"].as_str().unwrap()).unwrap().1;
        let sender = text(&case["sender"])
            .and_then(lookup)
            .map(|(_, id)| id)
            .unwrap_or_default();
        let caller = case["caller_agent_id"].as_str().unwrap();
        let caller_session = lookup(caller).map(|(session, _)| session);
        let peer = case["peer"].as_bool().unwrap();
        let ours = coordination::principal(&target, &sender, caller, caller_session.as_deref())
            .and_then(|principal| {
                let delegate = delegates
                    .iter()
                    .any(|(helper, of)| *helper == principal.sender_agent_id && *of == target);
                if !principal.sender_agent_id.is_empty() && delegate {
                    return Err(coordination::Refusal {
                        status: 403,
                        words: DELEGATE_REFUSED.to_string(),
                    });
                }
                Ok(principal)
            });
        match ours {
            Ok(principal) => {
                let origin = coordination::origin(text(&case["origin"]), &principal, peer);
                let coordination =
                    is_coordination(&target, Some(&origin), Some(&principal.sender_agent_id));
                let ours = json!({
                    "origin": origin,
                    "sender_agent_id": principal.sender_agent_id,
                    "synthesize_audio": coordination::synthesize_audio(&origin, None),
                    "coordination": coordination,
                });
                assert_eq!(ours, case["made"], "{case}");
                made += 1;
                marked += usize::from(coordination);
            }
            Err(refusal) => {
                let ours = json!({"status": refusal.status, "error": refusal.words});
                assert_eq!(ours, case["refused"], "{case}");
                refused += 1;
            }
        }
    }
    assert_eq!((cases.len(), made, refused, marked), (2240, 1536, 704, 932));
}

#[test]
fn a_send_is_spoken_as_it_asks_where_the_user_sent_it_and_otherwise_only_when_it_asks() {
    use coordination::synthesize_audio as spoken;
    assert!(spoken("user", None));
    assert!(!spoken("agent", None));
    assert!(spoken("user", Some(&json!(true))));
    assert!(spoken("user", Some(&json!(null))));
    assert!(spoken("user", Some(&json!(0))));
    assert!(!spoken("user", Some(&json!(false))));
    assert!(spoken("agent", Some(&json!(true))));
    assert!(!spoken("agent", Some(&json!(1))));
    assert!(!spoken("agent", Some(&json!("true"))));
}
