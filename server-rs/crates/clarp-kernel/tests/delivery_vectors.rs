//! A user send's delivery, held to the Python Host's own answers: the projection of a receipt,
//! the feed's pages and a page's rows, a `GET /log` query in the in-process mode, and the
//! replay of a `delivery-updated` hint (fixtures written by `tools/delivery-*-vectors.py`).
#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_kernel::delivery::{self, Feed, Query, Scope};
use serde_json::{Map, Value, json};

fn fixture(name: &str) -> Value {
    let path = format!("{}/tests/fixtures/{name}", env!("CARGO_MANIFEST_DIR"));
    serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap()
}

fn text(value: &Value) -> Option<&str> {
    value.as_str()
}

/// The store a vector's answer was made from.
struct Given {
    pointers: Vec<(u64, String)>,
    receipts: Map<String, Value>,
    current: u64,
    watermark: u64,
    generation: i64,
}

impl Given {
    fn of(given: &Value) -> Self {
        Self {
            pointers: given["pointers"]
                .as_array()
                .map(|all| {
                    all.iter()
                        .map(|p| (p[0].as_u64().unwrap(), p[1].as_str().unwrap().to_string()))
                        .collect()
                })
                .unwrap_or_default(),
            receipts: given["receipts"].as_object().cloned().unwrap_or_default(),
            current: given["current"].as_u64().unwrap_or(0),
            watermark: given["watermark"].as_u64().unwrap_or(0),
            generation: given["generation"].as_i64().unwrap(),
        }
    }

    fn receipt(&self, trace: &str) -> Option<Value> {
        delivery::parse(self.receipts.get(trace).and_then(text))
    }
}

impl Feed for Given {
    fn current(&self) -> u64 {
        self.current
    }
    fn watermark(&self) -> u64 {
        self.watermark
    }
    fn generation(&self) -> i64 {
        self.generation
    }
    fn pointers(&self, after: u64, through: u64, cap: usize) -> Vec<(u64, String, Option<Value>)> {
        self.pointers
            .iter()
            .filter(|(seq, _)| *seq > after && *seq <= through)
            .take(cap)
            .map(|(seq, trace)| (*seq, trace.clone(), self.receipt(trace)))
            .collect()
    }
}

#[test]
fn a_receipt_is_shown_as_the_python_host_shows_it() {
    let vectors = fixture("delivery-projection-vectors.json");
    assert_eq!(
        vectors["states"],
        json!([
            delivery::PENDING_LAUNCH,
            delivery::STOPPED,
            delivery::HANDED_OVER
        ])
    );
    let mut counted = [0usize; 2];
    for vector in vectors["vectors"].as_array().unwrap() {
        let shown = delivery::project(
            &vector["receipt"],
            vector["agent_id"].as_str().unwrap(),
            vector["generation"].as_i64().unwrap(),
            text(&vector["origin"]),
            text(&vector["sender_agent_id"]),
        );
        assert_eq!(shown.unwrap_or(Value::Null), vector["shown"], "{vector}");
        counted[usize::from(vector["shown"].is_null())] += 1;
    }
    assert_eq!(counted, [259, 877]);
}

#[test]
fn the_feed_and_a_pages_rows_are_read_as_the_python_host_reads_them() {
    let vectors = fixture("delivery-feed-vectors.json");
    assert_eq!(vectors["page_cap"], json!(delivery::PAGE_CAP));
    let mut pages = 0;
    for vector in vectors["pages"].as_array().unwrap() {
        let given = Given::of(&vector["given"]);
        let page = delivery::page(
            &given,
            vector["agent_id"].as_str().unwrap(),
            i128::from(vector["since"].as_i64().unwrap()),
            vector["until"].as_u64(),
            vector["stop_generation"].as_i64().map(i128::from),
            vector["bootstrap"].as_bool().unwrap(),
            usize::try_from(vector["cap"].as_u64().unwrap()).unwrap(),
        );
        let told = json!({
            "updates": page.updates, "cursor": page.cursor, "more": page.more,
            "expired": page.expired, "until": page.until,
            "stop_generation": page.stop_generation, "skipped": page.skipped,
        });
        assert_eq!(told, vector["page"], "{}", vector["what"]);
        pages += 1;
    }
    let mut rows = 0;
    for vector in vectors["rows"].as_array().unwrap() {
        let given = Given::of(&json!({"receipts": vector["given"]["receipts"],
                                      "generation": vector["given"]["generation"]}));
        let mut turns = vector["turns"].as_array().unwrap().clone();
        let epoch = delivery::annotate(
            vector["agent_id"].as_str().unwrap(),
            &mut turns,
            given.generation,
            |trace| given.receipt(trace),
        );
        assert_eq!(json!(turns), vector["rows"], "{}", vector["what"]);
        assert_eq!(json!(epoch), vector["epoch"]);
        rows += 1;
    }
    assert_eq!([pages, rows], [47, 7]);
}

#[test]
fn a_query_of_the_log_is_read_and_answered_as_the_python_host_answers_it() {
    let vectors = fixture("delivery-query-vectors.json");
    let given = Given::of(&vectors["given"]);
    let field = |key: &str| vectors["given"][key].as_str().unwrap().to_string();
    let scope = Scope {
        server_id: field("server_id"),
        agent_id: field("agent_id"),
        release: field("release"),
        instance: field("instance"),
    };
    assert_eq!(scope.release, delivery::IN_PROCESS_RELEASE);
    let mut counted = [0usize; 3];
    for vector in vectors["vectors"].as_array().unwrap() {
        if vector["session"] != "s" {
            // An agent the Host does not have: nothing is added, whatever is asked.
            assert_eq!(vector["answer"], vectors["body"]);
            continue;
        }
        let asked = vector["query"].as_object().unwrap();
        let read = Query::read(|name| asked.get(name).and_then(Value::as_str));
        let mut answer = vectors["body"].clone();
        match read {
            Err(words) => {
                assert_eq!(json!(words), vector["said"], "{vector}");
                counted[0] += 1;
            }
            Ok(query) => {
                assert_eq!(vector["said"], "", "{vector}");
                let mut turns = answer["turns"].as_array().unwrap().clone();
                let top = delivery::answer(&given, &scope, &mut turns, &query, |trace| {
                    given.receipt(trace)
                });
                answer["turns"] = json!(turns);
                answer.as_object_mut().unwrap().extend(top);
                counted[1 + usize::from(answer.get("delivery_updates").is_some())] += 1;
            }
        }
        assert_eq!(answer, vector["answer"], "{vector}");
    }
    assert_eq!(counted, [54, 29, 81]);
}

#[test]
fn a_hint_is_replayed_with_its_identity_alone() {
    let vectors = fixture("delivery-hint-vectors.json");
    let mut counted = [0usize; 2];
    for vector in vectors["vectors"].as_array().unwrap() {
        let out = delivery::egress(&vector["stored"], vector["internal"].as_bool().unwrap());
        assert_eq!(
            out.clone().unwrap_or(Value::Null),
            vector["out"],
            "{vector}"
        );
        counted[usize::from(out.is_none())] += 1;
    }
    assert_eq!(counted, [20, 30]);
}
