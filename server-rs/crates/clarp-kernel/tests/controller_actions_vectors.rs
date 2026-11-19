#![allow(clippy::unwrap_used)]
use clarp_kernel::controller_actions::{self as rules, Address, Registry};
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
    let mut counts = [0; 5];
    let mut narrowed = 0;
    for row in cases {
        match row["kind"].as_str().unwrap() {
            "address" => {
                let entries: Vec<_> = row["entries"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .filter_map(|v| Address::parse(v.as_str().unwrap()))
                    .collect();
                let found = Address::allowed(row["url"].as_str().unwrap(), &entries).is_some();
                if row["narrowed"] != "" {
                    assert_eq!(row["reference"], true);
                    assert!(!found, "{row}");
                    narrowed += 1;
                } else {
                    assert_eq!(json!(found), row["reference"], "{row}");
                }
                counts[0] += 1;
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
    assert_eq!((counts, narrowed), ([24, 62, 40, 5, 4], 1));
    assert!(rules::receipt_current(0, 599_999));
    assert!(rules::receipt_current(0, 600_000));
    assert!(!rules::receipt_current(0, 600_001));
    let local = Address::allowed("http://127.1:8123/x", &[]).unwrap();
    assert_eq!(local.host(), Some("127.0.0.1"));
    let port = Address::parse("https://fixture.invalid:443/hook").unwrap();
    assert!(
        Address::allowed("https://fixture.invalid/hook", std::slice::from_ref(&port)).is_some()
    );
    assert!(Address::allowed("https://fixture.invalid:444/hook", &[port]).is_none());
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
