#![allow(clippy::unwrap_used)]
//! Values and refusal words recorded from the Python event store.
use clarp_kernel::artifacts::form_events;
use serde_json::Value;

#[test]
fn event_validation_and_receipt_digests_match_the_reference() {
    let cases: Value = serde_json::from_str(include_str!("fixtures/forms-events.json")).unwrap();
    let mut count = 0;
    for case in cases.as_array().unwrap() {
        let (data, result) = (&case["data"], &case["output"]);
        let error = result.get("error").and_then(Value::as_str);
        match case["kind"].as_str().unwrap() {
            "append" => match form_events::batch(data) {
                Ok(rows) => {
                    if let Some(error) = error {
                        // The entire batch passed validation; only a stored form/ID comparison failed.
                        assert!(matches!(
                            error,
                            "form version mismatch" | "event ID already used for different data"
                        ));
                    } else {
                        for e in rows {
                            assert_eq!(e.digest, case["digests"][e.id].as_str().unwrap());
                        }
                    }
                }
                Err(why) => assert_eq!(Some(why), error),
            },
            "configure" => match form_events::draft_key(data) {
                Ok(key) => assert_eq!(key, &result["draft_key"]),
                Err(why) => assert_eq!(Some(why), error),
            },
            "read" => {
                let number = |key: &str, otherwise: i128| {
                    data.get(key)
                        .map(|v| v.to_string().parse::<i128>().unwrap())
                        .unwrap_or(otherwise)
                };
                let cursor = form_events::cursors(
                    number("after", 0),
                    number("limit", 100),
                    number("through", 0),
                );
                assert_eq!(cursor.err(), error);
            }
            _ => panic!("unexpected oracle operation"),
        }
        count += 1;
    }
    assert_eq!(count, 59);
}
