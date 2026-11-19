//! The memory of the Oracle's threads, against the Python Host's own module.
//!
//! `fixtures/oracle-memory-vectors.json` is written by `server-rs/tools/oracle-memory-vectors.py`
//! from `server/lib/oracle_memory.py` (with `oracle_attention.claim_context` and
//! `oracle_result_context.result_payload`) on a database of that program's own: stories of
//! what is asked of the memory, with what was answered and what the tables have after every
//! step. The same must come of the same here.
//!
//! Two things of that module are not this Host's and are passed over: the log of everything
//! a voice heard and said (nothing reads it), and adding to what a user gave a thread (the
//! conversation this Host carries only shows it).

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::collections::BTreeMap;

use clarp_kernel::oracle::delegations::{Delegation, Status};
use clarp_kernel::oracle::threads::{self, Excerpt, Memory, Opened, Opening};
use clarp_kernel::python;
use jiff::tz::TimeZone;
use serde_json::{Value, json};

fn vectors() -> Value {
    serde_json::from_str(include_str!("fixtures/oracle-memory-vectors.json")).unwrap()
}

/// What the file has written short is written out again: one sign many times over.
fn whole(value: &Value) -> Value {
    match value {
        Value::Object(fields) if fields.len() == 1 && fields.contains_key("runs") => {
            let mut text = String::new();
            for part in fields["runs"].as_array().unwrap() {
                match part {
                    Value::String(as_it_is) => text += as_it_is,
                    run => {
                        let times = usize::try_from(run[1].as_u64().unwrap()).unwrap();
                        text += &run[0].as_str().unwrap().repeat(times);
                    }
                }
            }
            json!(text)
        }
        Value::Object(fields) => {
            let fields = fields
                .iter()
                .map(|(name, value)| (name.clone(), whole(value)));
            Value::Object(fields.collect())
        }
        Value::Array(items) => Value::Array(items.iter().map(whole).collect()),
        plain => plain.clone(),
    }
}

/// A delegation as the book has it, of a row of the file.
fn delegation(row: &Value, now_ms: i64) -> Delegation {
    let text = |name: &str| row[name].as_str().unwrap_or("").to_string();
    Delegation {
        delegation_id: text("delegation_id"),
        owner_principal: text("owner_principal"),
        trace_id: String::new(),
        client_msg_id: String::new(),
        agent_id: "a-theo".to_string(),
        session: text("session"),
        backend_session_id: String::new(),
        request_text: text("request_text"),
        status: match row["status"].as_str().unwrap() {
            "accepted" => Status::Accepted,
            "queued" => Status::Queued,
            "completed" => Status::Completed,
            "failed" => Status::Failed,
            "cancelled" => Status::Cancelled,
            other => panic!("no such status: {other}"),
        },
        result_message_id: None,
        result_text: text("result_text"),
        error: text("error"),
        created_at: row["created_at"].as_i64().unwrap_or(now_ms),
        updated_at: row["updated_at"].as_i64().unwrap_or(now_ms),
        delivered_at: None,
    }
}

/// The tables of the memory, as the file writes them.
fn kept(memory: &Memory) -> BTreeMap<&'static str, Value> {
    let threads: Vec<Value> = memory
        .threads()
        .iter()
        .map(|thread| {
            json!({
                "thread_id": thread.thread_id, "owner_principal": thread.owner_principal,
                "contact": thread.contact, "active_connection": thread.active_connection,
                "revision": thread.revision, "state_json": python::canonical(&thread.state),
                "created_at": thread.created_at, "updated_at": thread.updated_at,
            })
        })
        .collect();
    let admissions: Vec<Value> = memory.begun().into_iter().map(begun).collect();
    let mut work: Vec<Value> = memory
        .linked()
        .iter()
        .map(|row| {
            json!({
                "thread_id": row.thread_id, "operation_id": row.operation_id,
                "created_at": row.created_at,
            })
        })
        .collect();
    work.sort_by_key(|row| {
        (
            row["thread_id"].to_string(),
            row["operation_id"].to_string(),
        )
    });
    let mut told: Vec<Value> = memory
        .told()
        .iter()
        .map(|row| {
            json!({
                "thread_id": row.thread_id, "owner_principal": row.owner_principal,
                "source_kind": row.source_kind, "source_id": row.source_id,
                "reference_ts": row.reference_ts, "stale": i64::from(row.stale),
                "sent_at": row.sent_at,
            })
        })
        .collect();
    told.sort_by_key(|row| {
        let of = |name: &str| row[name].as_str().unwrap().to_string();
        (of("thread_id"), of("source_kind"), of("source_id"))
    });
    BTreeMap::from([
        ("threads", json!(threads)),
        ("admissions", json!(admissions)),
        ("work", json!(work)),
        ("told", json!(told)),
    ])
}

/// An action that was begun, as the file writes its row.
fn begun(row: &threads::Begun) -> Value {
    json!({
        "thread_id": row.thread_id, "revision": row.revision, "action_index": row.action_index,
        "call_id": row.call_id, "tool": row.tool, "arguments_json": row.arguments_json,
        "result_json": row.result_json,
        "status": if row.completed { "completed" } else { "planned" },
        "created_at": row.created_at, "updated_at": row.updated_at,
    })
}

#[test]
fn the_memory_of_a_thread_is_the_python_hosts_step_by_step() {
    let vectors = vectors();
    let zone = TimeZone::get(vectors["zone"].as_str().unwrap()).unwrap();
    let begins = vectors["now"].as_i64().unwrap();
    let (mut steps, mut refused, mut passed_over, mut excerpts) = (0, 0, 0, 0);
    let mut rows_written = 0;
    // How often a connection was asked whether it has its thread, and how often it had.
    let mut held = (0, 0);
    // How many things the voices of the threads were told of, over all the steps.
    let mut told_of = 0;
    for story in vectors["stories"].as_array().unwrap() {
        let name = story["name"].as_str().unwrap();
        let mut memory = Memory::default();
        let mut book: Vec<Delegation> = Vec::new();
        // The thread each connection opened, as whom, and under which connection's name it
        // asks (its own, but for one that a story made by hand).
        let mut opened: BTreeMap<String, (String, String, String)> = BTreeMap::new();
        let mut contexts: Vec<Value> = Vec::new();
        let (mut now, mut made) = (begins, 0_u128);
        let mut before = kept(&memory);
        // The rows of the memory as a Host keeps them: what the memory said to write.
        let mut store: BTreeMap<(&str, String), Value> = BTreeMap::new();
        for (number, step) in story["steps"].as_array().unwrap().iter().enumerate() {
            let step = &whole(step);
            let at = format!("{name}, step {number} ({})", step["do"].as_str().unwrap());
            let connection = step["connection"].as_str().unwrap_or("");
            let its = opened.get(connection).cloned().unwrap_or_default();
            let asks = Opened {
                thread_id: &its.0,
                owner: &its.1,
                connection: &its.2,
            };
            let owner_of = |operation_id: &str| {
                let found = book.iter().find(|row| row.delegation_id == operation_id);
                found.map(|row| row.owner_principal.clone())
            };
            // What the memory answers: what it says back, or the words it refuses in.
            let mut answered: Result<Value, &'static str> = Ok(Value::Null);
            let mut ported = true;
            match step["do"].as_str().unwrap() {
                "passes" => now += step["ms"].as_i64().unwrap(),
                "open" => {
                    let new_id = format!("oracle-{:032x}", made + 1);
                    let opening = Opening {
                        owner: step["owner"].as_str().unwrap(),
                        contact: step["contact"].as_str().unwrap(),
                        thread_id: step["thread_id"].as_str(),
                        connection,
                        fresh: step["fresh"] == true,
                        new_id: &new_id,
                    };
                    answered = memory.open(&opening, now).map(|thread_id| {
                        made += u128::from(thread_id == new_id);
                        opened.insert(
                            connection.to_string(),
                            (
                                thread_id.clone(),
                                opening.owner.to_string(),
                                connection.to_string(),
                            ),
                        );
                        json!(thread_id)
                    });
                }
                "close" => {
                    // No connection has the thread any more: as a story of the file makes it
                    // so. The memory is made again of its rows, with that one changed.
                    let mut rows: Vec<(&str, Value)> = Vec::new();
                    for thread in memory.threads() {
                        let mut thread = thread.clone();
                        if thread.thread_id == its.0 {
                            thread.active_connection.clear();
                        }
                        rows.push((threads::THREADS, json!(thread)));
                    }
                    rows.extend(
                        memory
                            .begun()
                            .into_iter()
                            .map(|row| (threads::BEGUN, json!(row))),
                    );
                    rows.extend(
                        memory
                            .linked()
                            .iter()
                            .map(|row| (threads::LINKED, json!(row))),
                    );
                    rows.extend(memory.told().iter().map(|row| (threads::TOLD, json!(row))));
                    memory = Memory::kept(rows.iter().map(|(kind, row)| (*kind, row)));
                    store = rows
                        .into_iter()
                        .map(|(kind, row)| ((kind, name_of(kind, &row)), row))
                        .collect();
                }
                // One who did not open a thread asks of it: as an owner, under a connection's
                // name.
                "forge" => {
                    let of = |name: &str| step[name].as_str().unwrap().to_string();
                    opened.insert(
                        connection.to_string(),
                        (of("thread"), of("owner"), of("as")),
                    );
                }
                "delegation" => book.push(delegation(&step["row"], now)),
                "load" => answered = memory.load(asks),
                "save" => answered = memory.save(asks, &step["state"], now).map(|()| Value::Null),
                "admission" => {
                    let place = (step["revision"].as_i64().unwrap(), index(&step["index"]));
                    let action = (step["tool"].as_str().unwrap(), &step["arguments"]);
                    answered = memory.admission(asks, place, action, now).map(begun);
                }
                "finish" => {
                    let place = (step["revision"].as_i64().unwrap(), index(&step["index"]));
                    let finished = memory.finish(asks, place, &step["result"], &owner_of, now);
                    answered = finished.map(|()| Value::Null);
                }
                "admissions" => {
                    let listed = memory.admissions(asks, step["revision"].as_i64());
                    answered =
                        listed.map(|rows| json!(rows.into_iter().map(begun).collect::<Vec<_>>()));
                }
                "link" => {
                    let operation = step["operation_id"].as_str().unwrap();
                    answered = memory
                        .link(asks, operation, &owner_of, now)
                        .map(|()| Value::Null);
                }
                "work" => {
                    answered = memory.work(asks, &book).map(|rows| {
                        json!(
                            rows.iter()
                                .map(|row| row.delegation_id.clone())
                                .collect::<Vec<_>>()
                        )
                    });
                }
                "reconcile" => answered = memory.reconcile(asks, &book, now).map(|()| Value::Null),
                "claim" if step["reference_ts"].is_i64() => {
                    let what = (step["kind"].as_str().unwrap(), step["id"].as_str().unwrap());
                    let when = (
                        step["reference_ts"].as_i64().unwrap(),
                        step["stale"] == true,
                    );
                    answered = memory.claim(asks, what, when, now).map(|new| json!(new));
                }
                "release" => {
                    let what = (step["kind"].as_str().unwrap(), step["id"].as_str().unwrap());
                    answered = memory.release(asks, what).map(|()| Value::Null);
                }
                "history" => {
                    let state = memory.load(asks).unwrap();
                    let work = memory.work(asks, &book).unwrap();
                    let excerpt = Excerpt {
                        thread_id: &its.0,
                        state: &state,
                        work: &work,
                        contexts: &contexts,
                        roster: &step["roster"],
                    };
                    let later = step["later"].as_i64().unwrap_or(0);
                    answered = Ok(threads::history(&excerpt, now + later, &zone));
                    excerpts += 1;
                }
                // A time that is no whole number is none a Host of this kind can be handed;
                // the log of what a voice said, and what a user adds, are not this Host's.
                "claim" | "observe" | "context" | "uncontext" | "contexts" => ported = false,
                other => panic!("no such step: {other}"),
            }
            assert_eq!(json!(now), step["at"], "{at}");
            steps += 1;
            if !ported {
                passed_over += 1;
            } else {
                match (&answered, step["raises"].as_str()) {
                    (Err(why), Some(raises)) => {
                        assert_eq!(*why, raises, "{at}");
                        refused += 1;
                    }
                    (Ok(said), None) => {
                        let returned = step.get("returned").cloned().unwrap_or(Value::Null);
                        // The work of a thread is told by its ids here, in the same order.
                        let returned = match step["do"].as_str().unwrap() {
                            "work" => json!(
                                returned
                                    .as_array()
                                    .unwrap()
                                    .iter()
                                    .map(|row| row["delegation_id"].clone())
                                    .collect::<Vec<_>>()
                            ),
                            _ => returned,
                        };
                        assert_eq!(*said, returned, "{at}");
                    }
                    (ours, theirs) => panic!("{at}: {ours:?} here, {theirs:?} there"),
                }
            }
            // What a user added to the thread, as the file's tables have it now.
            if let Some(rows) = step["kept"]["contexts"].as_array() {
                let mut rows: Vec<Value> = rows
                    .iter()
                    .filter(|row| row["removed_at"].is_null())
                    .cloned()
                    .collect();
                rows.sort_by_key(|row| (row["added_at"].as_i64(), row["context_id"].to_string()));
                contexts = rows;
            }
            // The tables: those the step changed there are as they are here, and no other
            // changed here.
            let after = kept(&memory);
            for (table, rows) in &after {
                match step["kept"].get(*table) {
                    Some(theirs) => assert_eq!(rows, theirs, "{at}: {table}"),
                    None => assert_eq!(rows, &before[table], "{at}: {table} changed here alone"),
                }
            }
            // Which connection has the thread it opened, now.
            assert_eq!(step["has"].as_object().unwrap().len(), opened.len(), "{at}");
            for (connection, (thread_id, owner, asks_as)) in &opened {
                let asks = Opened {
                    thread_id,
                    owner,
                    connection: asks_as,
                };
                let has = memory.has(asks);
                assert_eq!(json!(has), step["has"][connection], "{at}: {connection}");
                held.0 += 1;
                held.1 += usize::from(has);
            }
            // What the voice of each thread was told of: the rows of that thread, and no
            // other's.
            for thread in memory.threads() {
                let mut told = memory.told_of(&thread.thread_id);
                told.sort();
                let rows = memory.told().iter();
                let mut of_thread: Vec<String> = rows
                    .filter(|row| row.thread_id == thread.thread_id)
                    .map(|row| format!("{}:{}", row.source_kind, row.source_id))
                    .collect();
                of_thread.sort();
                assert_eq!(told, of_thread, "{at}: {}", thread.thread_id);
                told_of += told.len();
            }
            // What the memory said to write is all of it: made again of the rows a Host
            // keeps, it is the same memory.
            for written in memory.written() {
                assert_eq!(
                    written.row.as_ref().map(|row| name_of(written.kind, row)),
                    written.row.as_ref().map(|_| written.id.clone()),
                    "{at}"
                );
                match written.row {
                    Some(row) => store.insert((written.kind, written.id), row),
                    None => store.remove(&(written.kind, written.id)),
                };
                rows_written += 1;
            }
            let again = Memory::kept(store.iter().map(|((kind, _), row)| (*kind, row)));
            assert_eq!(
                kept(&again),
                after,
                "{at}: what was written is not the whole memory"
            );
            before = after;
        }
    }
    assert_eq!(
        (
            (steps, refused, passed_over, excerpts),
            rows_written,
            held,
            told_of
        ),
        ((262, 38, 22, 11), 124, (518, 314), 84)
    );
}

fn index(value: &Value) -> usize {
    usize::try_from(value.as_u64().unwrap()).unwrap()
}

/// The name a row of the memory is kept under, by its kind.
fn name_of(kind: &str, row: &Value) -> String {
    let of = |name: &str| match &row[name] {
        Value::String(text) => text.clone(),
        other => other.to_string(),
    };
    match kind {
        threads::THREADS => of("thread_id"),
        threads::BEGUN => format!(
            "{}:{}:{}",
            of("thread_id"),
            of("revision"),
            of("action_index")
        ),
        threads::LINKED => format!("{}:{}", of("thread_id"), of("operation_id")),
        _ => format!(
            "{}:{}:{}",
            of("thread_id"),
            of("source_kind"),
            of("source_id")
        ),
    }
}

#[test]
fn a_result_an_age_and_a_time_are_written_as_the_python_host_writes_them() {
    let vectors = vectors();
    let zone = TimeZone::get(vectors["zone"].as_str().unwrap()).unwrap();
    let now = vectors["now"].as_i64().unwrap();
    let mut cut = 0;
    for case in vectors["results"].as_array().unwrap() {
        let case = &whole(case);
        let room = usize::try_from(case["budget"].as_u64().unwrap()).unwrap();
        let payload = threads::result_payload(&delegation(&case["row"], now), room);
        assert_eq!(
            Value::Object(payload.clone()),
            case["payload"],
            "{}",
            case["row"]
        );
        cut += usize::from(payload["finding_excerpt"] == true);
    }
    assert_eq!(
        (vectors["results"].as_array().unwrap().len(), cut),
        (24, 12)
    );
    for case in vectors["ages"].as_array().unwrap() {
        let before = case["ms_before"].as_i64().unwrap();
        assert_eq!(
            json!(threads::age_phrase(now - before, now)),
            case["age"],
            "{before}"
        );
    }
    let mut shown = 0;
    for case in vectors["times"].as_array().unwrap() {
        let time = threads::iso_time(case["ms"].as_i64().unwrap(), &zone);
        assert_eq!(
            json!(time),
            case.get("time").cloned().unwrap_or(Value::Null),
            "{case}"
        );
        shown += usize::from(time.is_some());
    }
    assert_eq!(
        (vectors["times"].as_array().unwrap().len(), shown),
        (12, 11)
    );
    // The names an action and its work are known by.
    assert_eq!(
        threads::call_id("a-thread", 3, 1).len(),
        "oracle-".len() + 40
    );
    assert_ne!(
        threads::operation_id("a-phone", "a-call", false),
        threads::operation_id("a-phone", "a-call", true)
    );
}

#[test]
fn a_talk_too_long_to_keep_is_not_kept_and_ends_no_call() {
    let mut memory = Memory::default();
    let opening = Opening {
        owner: "a-phone",
        contact: "theo",
        thread_id: None,
        connection: "conn-1",
        fresh: false,
        new_id: "thread-1",
    };
    let thread_id = memory.open(&opening, 1_000).unwrap();
    let opened = Opened {
        thread_id: &thread_id,
        owner: "a-phone",
        connection: "conn-1",
    };
    // Kept within its bound, and up to it exactly: measured in bytes, as it is written.
    let first = json!({ "revision": 1, "fragments": [] });
    let bytes = python::canonical(&first).len();
    assert_eq!(memory.save_within(opened, &first, 2_000, bytes), Ok(()));
    // A byte over the bound is not kept: what was kept stays as it was.
    let longer = json!({ "revision": 2, "fragments": ["\u{e9}"] });
    let over = python::canonical(&longer).len() - 1;
    assert_eq!(
        memory.save_within(opened, &longer, 3_000, over),
        Err(threads::TOO_MUCH)
    );
    assert_eq!(memory.load(opened).unwrap(), first);
    // By another connection than the one that has the thread, nothing is kept either.
    let other = Opened {
        connection: "conn-2",
        ..opened
    };
    assert_eq!(
        memory.save_within(other, &first, 4_000, usize::MAX),
        Err(threads::STALE)
    );
    // The call goes on where its talk was too much or the store failed, and that is said
    // once a call; it ends where its thread is another call's now.
    let mut said = false;
    let told = |why: &str| format!("a call of the Oracle could not keep its talk: {why}");
    assert_eq!(
        threads::not_kept(threads::TOO_MUCH, &mut said),
        threads::NotKept::GoesOn {
            say: Some(told(threads::TOO_MUCH))
        }
    );
    assert_eq!(
        threads::not_kept(threads::TOO_MUCH, &mut said),
        threads::NotKept::GoesOn { say: None }
    );
    assert_eq!(
        threads::not_kept(threads::STORE_FAILED, &mut said),
        threads::NotKept::GoesOn { say: None }
    );
    let mut fresh = false;
    assert_eq!(
        threads::not_kept(threads::STORE_FAILED, &mut fresh),
        threads::NotKept::GoesOn {
            say: Some(told(threads::STORE_FAILED))
        }
    );
    assert_eq!(
        threads::not_kept(threads::STALE, &mut said),
        threads::NotKept::Ends
    );
    assert_eq!(threads::STATE_BYTES, 2 * 1024 * 1024);
}
