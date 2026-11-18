//! The record of the Oracle's delegations, against the Python Host's own module.
//!
//! `fixtures/oracle-delegation-vectors.json` is written by
//! `server-rs/tools/oracle-delegation-vectors.py` from `server/lib/oracle_delegations.py`,
//! run as it is on a database of that program's own with a stand-in for what admits a turn:
//! stories of delegations taken, taken again, refused, queued, begun, ended, failed, called
//! off by a stop, shown to a phone and acknowledged, and found again after their turn was
//! lost. Every step is replayed here, and after every step the whole book must be what
//! that Host's table was.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_kernel::oracle::delegations::{
    ADMINISTRATOR, Agent, Asked, Book, Delegation, Fate, Refused, SHOWN_USUALLY, Status, Taken,
    normalize_id,
};
use serde_json::{Value, json};
use std::collections::BTreeMap;

fn vectors() -> Value {
    serde_json::from_str(include_str!("fixtures/oracle-delegation-vectors.json")).unwrap()
}

fn text<'a>(value: &'a Value, key: &str) -> &'a str {
    value[key].as_str().unwrap_or("")
}

fn told(rows: &[&Delegation]) -> Value {
    Value::Array(rows.iter().map(|row| row.told()).collect())
}

/// What a refusal of the book is to the Python Host: the kind of thing it raised.
fn kind(refused: &Refused) -> &'static str {
    match (refused.status, refused.why) {
        (404, _) => "lookup",
        (409, "delegation_id was already used for different work") => "collision",
        (409, _) => "not cancellable",
        _ => "value",
    }
}

/// The step says the same was refused, in the same words.
fn same_refusal(step: &Value, refused: &Refused) {
    assert_eq!(
        json!([kind(refused), refused.why]),
        json!([step["refused"], step["words"]]),
        "{step}"
    );
}

fn by_id(rows: &[Delegation]) -> BTreeMap<&str, &Delegation> {
    let rows = rows.iter().map(|row| (row.delegation_id.as_str(), row));
    rows.collect()
}

/// The whole book, by the ids of its rows, as the Python Host's table is written down.
fn whole(book: &Book) -> Value {
    let mut rows: Vec<&Delegation> = book.rows().iter().collect();
    rows.sort_by(|one, other| one.delegation_id.cmp(&other.delegation_id));
    told(&rows)
}

/// What became of the turn of a delegation, as a step of the vectors has it: the messages
/// the turn left are looked through as that Host's query does, the newest first, and one
/// that is nothing but spaces is none.
fn fate(step: &Value, row: &Delegation, at: i64) -> Fate {
    let named = |list: &str| {
        let ids = step[list].as_array().unwrap().iter();
        ids.filter_map(Value::as_str)
            .any(|id| id == row.delegation_id)
    };
    if !named("admitted") {
        return Fate::NotAdmitted;
    }
    if named("live") {
        return Fate::Live;
    }
    let left = step["said"][&row.delegation_id].as_array();
    let mut left: Vec<(&str, &str, i64)> = left
        .into_iter()
        .flatten()
        .map(|said| {
            (
                said[0].as_str().unwrap(),
                said[1].as_str().unwrap(),
                said[2].as_i64().unwrap(),
            )
        })
        .filter(|(place, text, _)| {
            matches!(*place, "final" | "live") && !text.trim_matches(' ').is_empty()
        })
        .collect();
    left.sort_by_key(|(_, _, revision)| std::cmp::Reverse(*revision));
    match left.first() {
        Some((_, text, revision)) => Fate::Said {
            message_id: format!("m-{}-{revision}", row.delegation_id),
            text: (*text).to_string(),
            at_ms: at,
        },
        None => Fate::Lost,
    }
}

#[test]
fn the_book_of_delegations_is_kept_as_the_python_host_keeps_it() {
    let vectors = vectors();
    assert_eq!(json!(ADMINISTRATOR), vectors["administrator"]);
    assert_eq!(json!(SHOWN_USUALLY), vectors["usual_shown"]);
    let (mut steps, mut refusals, mut admitted, mut ended) = (0, 0, 0, 0);
    for story in vectors["stories"].as_array().unwrap() {
        let mut book = Book::default();
        let mut before: Vec<Delegation> = Vec::new();
        for step in story["steps"].as_array().unwrap() {
            let at = step["at"].as_i64().unwrap();
            let owner = step["owner"].as_str().unwrap_or(ADMINISTRATOR);
            let limit = step["limit"].as_i64().unwrap_or(SHOWN_USUALLY);
            match text(step, "op") {
                "dispatch" => {
                    let asked = Asked {
                        delegation_id: text(step, "delegation_id"),
                        session: text(step, "session"),
                        request: text(step, "request"),
                        owner,
                    };
                    let agent = step["agent"].as_object().map(|agent| Agent {
                        agent_id: agent["session"].as_str().unwrap(),
                        janitor: agent["janitor"] == true,
                    });
                    let has_message = step["has_message"] == true;
                    match book.take(&asked, agent, has_message, at) {
                        Err(refused) => {
                            same_refusal(step, &refused);
                            assert_eq!(step["admitted"], Value::Null, "{step}");
                            refusals += 1;
                        }
                        Ok(Taken::AsItIs(row)) => {
                            assert_eq!(row.told(), step["answer"], "{step}");
                            assert_eq!(step["admitted"], Value::Null, "{step}");
                        }
                        Ok(Taken::ToAdmit(admit)) => {
                            // The turn as the Python Host hands it to what admits one.
                            let sent = json!({
                                "text": admit.text, "session": admit.session,
                                "trace": admit.trace_id, "client_msg_id": admit.client_msg_id,
                                "origin": "oracle", "speaks": false, "waits_if_busy": true,
                            });
                            assert_eq!(sent, step["admitted"], "{step}");
                            admitted += 1;
                            if let Some(why) = step["raises"].as_str() {
                                book.fail(&admit.delegation_id, why, at);
                                assert_eq!(
                                    json!(["raised", why]),
                                    json!([step["refused"], step["words"]])
                                );
                                refusals += 1;
                            } else {
                                let waits = step["queued"] == true && step["queue"] == "queued";
                                let backend = text(step, "backend");
                                let row = book.dispatched(&admit.delegation_id, backend, waits, at);
                                assert_eq!(row.unwrap().told(), step["answer"], "{step}");
                            }
                        }
                    }
                }
                "dispatched" => {
                    let waits = step["queued"] == true && step["queue"] == "queued";
                    let id = text(step, "delegation_id");
                    let row = book.dispatched(id, text(step, "backend"), waits, at);
                    let row = row.map_or(Value::Null, Delegation::told);
                    assert_eq!(row, step["answer"], "{step}");
                }
                "completed" => {
                    let done = book.completed(
                        text(step, "trace"),
                        text(step, "message"),
                        text(step, "text"),
                        at,
                    );
                    assert_eq!(json!(done), step["answer"], "{step}");
                    ended += usize::from(done);
                }
                "same_moment" => {
                    for id in step["ids"].as_array().unwrap() {
                        let trace = format!("oracle-{}", id.as_str().unwrap());
                        assert!(book.completed(
                            &trace,
                            text(step, "message"),
                            text(step, "text"),
                            at
                        ));
                        ended += 1;
                    }
                }
                "failed" => {
                    let row = book.fail(text(step, "delegation_id"), text(step, "reason"), at);
                    assert_eq!(
                        row.map_or(Value::Null, Delegation::told),
                        step["answer"],
                        "{step}"
                    );
                }
                "failed_for_trace" => {
                    let failed =
                        book.failed_for_trace(text(step, "trace"), text(step, "reason"), at);
                    assert_eq!(json!(failed), step["answer"], "{step}");
                }
                "started" => {
                    assert_eq!(
                        json!(book.started(text(step, "trace"), at)),
                        step["answer"],
                        "{step}"
                    );
                }
                "cancel" => match book.cancel(text(step, "delegation_id"), owner) {
                    Ok(row) => {
                        assert_eq!(
                            row.map_or(Value::Null, Delegation::told),
                            step["answer"],
                            "{step}"
                        );
                    }
                    Err(refused) => {
                        same_refusal(step, &refused);
                        refusals += 1;
                    }
                },
                "acknowledge" => {
                    let has = book.acknowledge(text(step, "delegation_id"), owner, at);
                    assert_eq!(json!(has), step["answer"], "{step}");
                }
                "undelivered" => {
                    assert_eq!(
                        told(&book.undelivered(owner, limit)),
                        step["answer"],
                        "{step}"
                    );
                }
                "recent" => {
                    let whose = step["all_of"].as_str().unwrap_or(owner);
                    assert_eq!(told(&book.recent(whose, limit)), step["answer"], "{step}");
                }
                "active" => {
                    let owed = book.owed(text(step, "agent"), text(step, "trace"));
                    assert_eq!(Value::Array(owed), step["answer"], "{step}");
                }
                "stop" => match book.to_call_off(text(step, "session"), owner) {
                    Err(refused) => {
                        same_refusal(step, &refused);
                        assert_eq!(step["released"], json!([]), "{step}");
                        refusals += 1;
                    }
                    Ok(_) if step["does"] == "raises" => {
                        // The agent could not be stopped: nothing is called off.
                        assert_eq!(step["refused"], "raised", "{step}");
                        assert_eq!(step["released"], json!([]), "{step}");
                        refusals += 1;
                    }
                    Ok(found) => {
                        // What the stop itself does to the turn that runs.
                        match text(step, "does") {
                            "fails" => {
                                assert!(book.failed_for_trace(text(step, "trace"), "Stopped.", at));
                            }
                            "completes" => {
                                let said = "Done as it was stopped.";
                                assert!(book.completed(text(step, "trace"), "m-race", said, at));
                            }
                            _ => {}
                        }
                        let ids: Vec<String> =
                            found.iter().map(|one| one.delegation_id.clone()).collect();
                        let called_off = book.called_off(&ids, at);
                        let called_off: Vec<&Delegation> = called_off.iter().collect();
                        assert_eq!(told(&called_off), step["answer"], "{step}");
                        let waited = found.iter().filter(|one| one.waited);
                        let waited: Vec<&str> =
                            waited.map(|one| one.client_msg_id.as_str()).collect();
                        assert_eq!(json!(waited), step["taken_from_queue"], "{step}");
                        let mut traces: Vec<&str> =
                            found.iter().map(|one| one.trace_id.as_str()).collect();
                        traces.sort_unstable();
                        assert_eq!(json!([traces]), step["released"], "{step}");
                    }
                },
                "orphans" => {
                    let mut asked = Vec::new();
                    let count = book.orphans(
                        |row| {
                            let fate = fate(step, row, at);
                            if fate != Fate::NotAdmitted {
                                asked.push(row.trace_id.clone());
                            }
                            fate
                        },
                        at,
                    );
                    assert_eq!(json!(count), step["answer"], "{step}");
                    assert_eq!(json!(asked), step["asked_whether_live"], "{step}");
                }
                other => panic!("a step of a kind this test does not know: {other}"),
            }
            assert_eq!(whole(&book), step["rows"], "after {step}");
            // What the Host is given to write is exactly what changed in this step, each
            // row as it is now, and nothing is given twice.
            let written = book.written();
            assert!(book.written().is_empty());
            let now: BTreeMap<&str, &Delegation> = by_id(book.rows());
            let was: BTreeMap<&str, &Delegation> = by_id(&before);
            let changed = now.iter().filter(|(id, row)| was.get(*id) != Some(*row));
            let changed: Vec<&Delegation> = changed.map(|(_, row)| *row).collect();
            let mut given: Vec<&Delegation> = written.iter().collect();
            given.sort_by(|one, other| one.delegation_id.cmp(&other.delegation_id));
            assert_eq!(given, changed, "after {step}");
            before = book.rows().to_vec();
            steps += 1;
        }
        // A book that is kept and read again, its rows in whatever order they come back in,
        // is the same book in the same order.
        let mut kept = book.rows().to_vec();
        kept.reverse();
        assert_eq!(Book::kept(kept).rows(), book.rows(), "{}", story["name"]);
    }
    assert_eq!((steps, refusals, admitted, ended), (191, 28, 57, 18));
}

#[test]
fn an_id_of_a_delegation_is_short_and_of_plain_signs() {
    assert_eq!(normalize_id("  d-1._A  ").unwrap(), "d-1._A");
    assert_eq!(normalize_id(&"d".repeat(80)).unwrap().len(), 80);
    for none in [
        "",
        "   ",
        "a b",
        "semi;colon",
        "d-\u{e9}",
        "a/b",
        &"d".repeat(81),
    ] {
        let refused = normalize_id(none).unwrap_err();
        assert_eq!(
            (refused.status, refused.why),
            (400, "invalid delegation_id"),
            "{none:?}"
        );
    }
    // Two hundred are shown at most, and one at least, whatever number is asked for.
    let mut book = Book::default();
    let theo = Agent {
        agent_id: "a-theo",
        janitor: false,
    };
    for number in 0..205_i64 {
        let id = format!("d-{number}");
        let asked = Asked {
            delegation_id: &id,
            session: "theo",
            request: "Look.",
            owner: ADMINISTRATOR,
        };
        assert!(matches!(
            book.take(&asked, Some(theo), false, number),
            Ok(Taken::ToAdmit(_))
        ));
        assert!(book.completed(&format!("oracle-{id}"), "m-1", "Done.", 1_000 + number));
    }
    for (asked, shown) in [
        (500, 200),
        (200, 200),
        (201, 200),
        (199, 199),
        (1, 1),
        (0, 1),
        (-7, 1),
    ] {
        assert_eq!(
            book.undelivered(ADMINISTRATOR, asked).len(),
            shown,
            "{asked}"
        );
        assert_eq!(book.recent(ADMINISTRATOR, asked).len(), shown, "{asked}");
    }
    // What is over is told as over, in the words a phone reads.
    for (status, word, active) in [
        (Status::Accepted, "accepted", true),
        (Status::Queued, "queued", true),
        (Status::Completed, "completed", false),
        (Status::Failed, "failed", false),
        (Status::Cancelled, "cancelled", false),
    ] {
        assert_eq!((status.word(), status.active()), (word, active));
        assert_eq!(serde_json::to_value(status).unwrap(), json!(word));
    }
}
