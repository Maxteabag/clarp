//! The relay of a long text to the Oracle's voice, against the Python Host's own modules.
//!
//! `fixtures/oracle-relay-vectors.json` is written by `server-rs/tools/oracle-relay-vectors.py`
//! from `server/lib/oracle_relay.py` and `oracle_context.context_chunks`: how a text is cut,
//! what each part of a relay says for every way it can be handed over, what a relay says of
//! how far it came, what of an agent's answer is relayed, and the relay of a conversation
//! that is read out. The same must come of the same here.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_kernel::oracle::delegations::{Delegation, Status};
use clarp_kernel::oracle::relay::{
    self, APPEND_CHARS, AUTO_PARTS, Handed, PART_BYTES, Relay, Said,
};
use serde_json::{Value, json};

fn vectors() -> Value {
    serde_json::from_str(include_str!("fixtures/oracle-relay-vectors.json")).unwrap()
}

fn text<'a>(value: &'a Value, key: &str) -> &'a str {
    value[key].as_str().unwrap_or("")
}

/// A relay as the vectors make one.
fn made(made: &Value) -> Result<Relay, &'static str> {
    Relay::new(
        (text(made, "key"), text(made, "label"), text(made, "source")),
        text(made, "text"),
        text(made, "request"),
        made["verbatim"] == true,
    )
}

/// What a relay is, as the vectors say it of one.
fn told(relay: &Relay) -> Value {
    json!({
        "key": relay.key, "label": relay.label, "source": relay.source, "text": relay.text,
        "request": relay.request, "verbatim": relay.verbatim, "total": relay.total(),
    })
}

#[test]
fn a_text_is_cut_as_the_python_host_cuts_it_and_nothing_of_it_is_lost() {
    let vectors = vectors();
    assert_eq!(
        json!([PART_BYTES, APPEND_CHARS, AUTO_PARTS]),
        json!([
            vectors["part_bytes"],
            vectors["append_chars"],
            vectors["auto_parts"]
        ])
    );
    let (mut cut, mut refused) = (0, 0);
    for case in vectors["chunkings"].as_array().unwrap() {
        let most = case["max_bytes"].as_i64().unwrap();
        let most = usize::try_from(most).unwrap_or(0);
        match relay::chunks(text(case, "text"), most) {
            Ok(pieces) => {
                assert_eq!(json!(pieces), case["chunks"], "{case}");
                assert_eq!(pieces.concat(), text(case, "text"));
                assert!(
                    pieces
                        .iter()
                        .all(|piece| !piece.is_empty() && piece.len() <= most)
                );
                cut += 1;
            }
            Err(why) => {
                assert_eq!(json!(why), case["raises"], "{case}");
                refused += 1;
            }
        }
    }
    assert_eq!((cut, refused), (171, 3));
}

#[test]
fn every_part_of_a_relay_says_what_the_python_hosts_says() {
    let vectors = vectors();
    let (mut parts, mut statuses, mut refused) = (0, 0, 0);
    for case in vectors["relays"].as_array().unwrap() {
        let mut relay = match made(&case["made"]) {
            Ok(relay) => relay,
            Err(why) => {
                assert_eq!(json!(why), case["raises"], "{}", case["made"]["key"]);
                refused += 1;
                continue;
            }
        };
        let name = text(&case["made"], "key");
        assert_eq!(json!(relay.total()), case["total"], "{name}");
        assert_eq!(json!(relay.chunks()), case["chunks"], "{name}");
        assert_eq!(relay.chunks().concat(), relay.text, "{name}");
        for part in case["parts"].as_array().unwrap() {
            let index = usize::try_from(part["index"].as_u64().unwrap()).unwrap();
            let handed = Handed {
                served: part["served"] == true,
                resumed: part["resumed"] == true,
                pause_after: part["pause_after"] == true,
            };
            assert_eq!(
                json!(relay.part(index, handed)),
                part["part"],
                "{name} {part}"
            );
            parts += 1;
        }
        // How it is to be read is said as it stands when a part is handed over.
        relay.verbatim = !relay.verbatim;
        for part in case["turned"].as_array().unwrap() {
            let index = usize::try_from(part["index"].as_u64().unwrap()).unwrap();
            assert_eq!(
                json!(relay.part(index, Handed::default())),
                part["part"],
                "{name}"
            );
        }
        relay.verbatim = !relay.verbatim;
        for at in case["status"].as_array().unwrap() {
            relay.next = usize::try_from(at["next"].as_u64().unwrap()).unwrap();
            assert_eq!(json!(relay.status()), at["status"], "{name} {at}");
            assert_eq!(json!(relay.remaining()), at["remaining"], "{name} {at}");
            statuses += 1;
        }
        // The longest a part of it can be handed over as.
        let every = Handed {
            served: true,
            resumed: true,
            pause_after: true,
        };
        let longest = (0..relay.total()).map(|index| relay.part(index, every).chars().count());
        assert_eq!(json!(longest.max()), case["longest"], "{name}");
    }
    assert_eq!((parts, statuses, refused), (144, 24, 1));
}

#[test]
fn what_of_an_answer_is_relayed_and_what_of_a_conversation() {
    let vectors = vectors();
    let (mut answers, mut marked) = (0, 0);
    for case in vectors["results"].as_array().unwrap() {
        let row = &case["row"];
        let said = relay::result_text(text(row, "result_text"), text(row, "error"));
        assert_eq!(json!(said), case["text"], "{row}");
        assert_eq!(
            json!(relay::request_excerpt(text(row, "request_text"))),
            case["excerpt"],
            "{row}"
        );
        let status = match text(row, "status") {
            "completed" => Status::Completed,
            "failed" => Status::Failed,
            _ => Status::Cancelled,
        };
        let kept = Delegation {
            delegation_id: text(row, "delegation_id").to_string(),
            owner_principal: "administrator".to_string(),
            trace_id: String::new(),
            client_msg_id: String::new(),
            agent_id: String::new(),
            session: text(row, "session").to_string(),
            backend_session_id: String::new(),
            request_text: text(row, "request_text").to_string(),
            status,
            result_message_id: None,
            result_text: text(row, "result_text").to_string(),
            error: text(row, "error").to_string(),
            created_at: 0,
            updated_at: 0,
            delivered_at: None,
        };
        let relay = relay::result_relay(&kept, case["agent"].as_str()).unwrap();
        let mut ours = told(&relay);
        ours["first"] = json!(relay.part(0, Handed::default()));
        assert_eq!(ours, case["relay"], "{row}");
        answers += 1;
        marked += usize::from(
            text(row, "result_text") != text(case, "text") && !text(row, "result_text").is_empty(),
        );
    }
    let mut conversations = 0;
    for case in vectors["transcripts"].as_array().unwrap() {
        let output = &case["output"];
        let messages: Vec<Said> = output["messages"]
            .as_array()
            .unwrap()
            .iter()
            .map(|said| Said {
                by_the_agent: said["role"] == "assistant",
                timestamp: text(said, "timestamp"),
                text: text(said, "text"),
                truncated: said["truncated"] == true,
            })
            .collect();
        let age = (
            text(output, "newest_message_age"),
            output["truncated"] == true,
        );
        let relay = relay::transcript_relay(
            text(output, "agent"),
            &messages,
            age,
            case["verbatim"] == true,
        );
        let relay = relay.unwrap();
        let mut ours = told(&relay);
        ours["first"] = json!(relay.part(0, Handed::default()));
        ours["last"] = json!(relay.part(relay.total() - 1, Handed::default()));
        assert_eq!(ours, case["relay"], "{output}");
        conversations += 1;
    }
    assert_eq!((answers, marked, conversations), (52, 16, 10));
}
