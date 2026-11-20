#![allow(clippy::unwrap_used)]
use clarp_kernel::controller_actions::{self as rules, Registry};
use clarp_kernel::destination::{self, Destination};
use serde_json::{Value, json};
fn answer(result: Result<Value, rules::Error>) -> Value {
    match result {
        Ok(v) => json!([200, v]),
        Err((s, t)) => json!([s, t]),
    }
}
#[test]
fn controller_actions_follow_the_reference_and_the_two_rulings() {
    let cases: Vec<Value> =
        serde_json::from_str(include_str!("fixtures/controller-actions-vectors.json")).unwrap();
    let mut state = Registry::default();
    let mut counts = [0; 8];
    let mut awaiting = 0;
    for row in cases {
        match row["kind"].as_str().unwrap() {
            // Where an action may send, and what is sent (R133): the URL rebuilt from its parsed
            // parts, or nothing.
            "address" => {
                let entries: Vec<Destination> = row["entries"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .filter_map(|v| destination::prefix(v.as_str().unwrap()))
                    .collect();
                let sent = destination::allowed(row["url"].as_str().unwrap(), &entries);
                if row["awaiting"] == "R139" {
                    // Refused here before the reference does: the rows say it admits them still,
                    // and once its PR is merged and they are written again, this fails and the
                    // mark goes.
                    assert_eq!(sent, None, "{row}");
                    assert!(
                        row["reference"].is_string(),
                        "R139 is in the reference: {row}"
                    );
                    awaiting += 1;
                    continue;
                }
                assert_eq!(json!(sent), row["reference"], "{row}");
                counts[0] += 1;
            }
            // How a text is parsed as a destination and as an allowed prefix.
            "parse" => {
                let text = row["text"].as_str().unwrap();
                let shown = |found: Option<Destination>| {
                    found.map_or(Value::Null, |d| {
                        json!({"scheme": d.scheme, "host": d.host, "port": d.port, "path": d.path,
                               "query": d.query, "url": d.url()})
                    })
                };
                if row["awaiting"] == "R139" {
                    assert_eq!(destination::destination(text), None, "{text:?}");
                    assert_eq!(destination::prefix(text), None, "{text:?}");
                    assert!(
                        row["destination"].is_object(),
                        "R139 is in the reference: {row}"
                    );
                    awaiting += 1;
                    continue;
                }
                assert_eq!(
                    shown(destination::destination(text)),
                    row["destination"],
                    "{text:?}"
                );
                assert_eq!(shown(destination::prefix(text)), row["prefix"], "{text:?}");
                counts[5] += 1;
            }
            // The owner's allowed prefixes: those that do not parse are refused, and said in one line.
            "config" => {
                let given = row["given"].as_array().unwrap();
                let texts: Vec<&str> = given
                    .iter()
                    .map(|v| v.as_str().unwrap().trim())
                    .filter(|text| !text.is_empty())
                    .collect();
                let kept: Vec<&str> = texts
                    .iter()
                    .copied()
                    .filter(|text| destination::prefix(text).is_some())
                    .collect();
                assert_eq!(json!(kept), row["kept"], "{row}");
                let told = row["told"].as_array().unwrap();
                assert_eq!(told.len(), usize::from(kept.len() != texts.len()), "{row}");
                counts[6] += 1;
            }
            // R132: a debounced request repeated after the window is its answer again, with
            // `duplicate`, and nothing runs a second time (the Host keeps it: its test).
            "repeat" => {
                let mut again = row["debounced"].clone();
                again["duplicate"] = true.into();
                assert_eq!(row["again"], again);
                assert_eq!(row["runs"], 1);
                counts[7] += 1;
            }
            "definition" => {
                assert_eq!(answer(rules::validate(&row["data"], &[])), row["expected"]);
                counts[1] += 1;
            }
            "change" => {
                let mut audit = Value::Null;
                let result = state
                    .change(
                        row["op"].as_str().unwrap(),
                        &row["data"],
                        &[],
                        "administrator",
                        row["at"].as_i64().unwrap(),
                    )
                    .map(|(v, a)| {
                        audit = a;
                        v
                    });
                assert_eq!(answer(result), row["expected"], "{}", row["data"]);
                assert_eq!(audit, row["audit"]);
                assert_eq!(state.screen(), row["state"]);
                counts[2] += 1;
                if row["data"]["device"] == "one" && row["data"]["action_id"] == "other" {
                    assert_ne!(state.mapped("one", "primary", "swipe-up"), Some("other"));
                    assert_ne!(state.mapped("one", "secondary", "hold"), Some("other"));
                    assert_eq!(state.mapped("one", "secondary", "swipe-up"), Some("other"));
                    assert_eq!(
                        state.mapped("unknown", "secondary", "swipe-up"),
                        Some("note")
                    );
                }
            }
            "http_result" => {
                assert_eq!(
                    rules::http_result(
                        row["status"].as_u64().unwrap() as u16,
                        row["body"].as_str().unwrap()
                    ),
                    row["expected"]
                );
                counts[3] += 1;
            }
            "debounce" => {
                assert_eq!(
                    json!(rules::debounced(
                        &row["action"],
                        row["elapsed"].as_u64().unwrap(),
                        Some(&row["first"])
                    )),
                    row["expected"]
                );
                counts[4] += 1;
            }
            _ => panic!("unrecognized vector"),
        }
    }
    assert_eq!((counts, awaiting), ([72, 62, 40, 5, 4, 84, 4, 1], 18));
    assert!(rules::receipt_current(0, 599_999));
    assert!(rules::receipt_current(0, 600_000));
    assert!(!rules::receipt_current(0, 600_001));
    // Another spelling of this machine's address is a name, not this machine (R133).
    assert_eq!(destination::allowed("http://127.1:8123/x", &[]), None);
    let port = destination::prefix("https://fixture.invalid:443/hook").unwrap();
    assert_eq!(
        destination::allowed("https://fixture.invalid/hook", std::slice::from_ref(&port))
            .as_deref(),
        Some("https://fixture.invalid/hook")
    );
    assert!(destination::allowed("https://fixture.invalid:444/hook", &[port]).is_none());
    assert_eq!(
        rules::request_id(&json!({"request_id":"X".repeat(140)})).unwrap(),
        "X".repeat(128)
    );
    assert_eq!(
        rules::source(
            &json!({"device":"x".repeat(129),"button":false,"source_kind":17,"url":"unused"})
        ),
        json!({"device":"x".repeat(128),"source_kind":"17"})
    );
}
