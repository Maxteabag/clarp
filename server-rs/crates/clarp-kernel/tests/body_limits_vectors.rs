#![allow(clippy::unwrap_used)]
use clarp_kernel::request_body::{self as rule, Admission};
use serde_json::{Value, json};

#[test]
fn the_python_dispatcher_refuses_drains_and_logs_at_its_boundaries() {
    let cases: Vec<Value> =
        serde_json::from_str(include_str!("fixtures/body-limits-vectors.json")).unwrap();
    let mut counted = 0;
    for case in cases {
        let method = case[0].as_str().unwrap();
        let path = case[1].as_str().unwrap();
        let length = case[2].as_u64().unwrap();
        let limit = rule::limit(method, path);
        let family = rule::family(path);
        assert_eq!(json!(limit), case[3]);
        assert_eq!(family, case[4]);
        let mut logs = json!([]);
        let mut response = Value::Null;
        if let Some(limit) = limit {
            let kind = match rule::admission(length, limit) {
                Admission::Accept => None,
                Admission::LogLarge => Some("requestBodyLarge"),
                Admission::Refuse { close } => {
                    response = json!([413,{"error":rule::TOO_LARGE},"application/json",close.then(||json!({"Connection":"close"}))]);
                    Some("requestBodyTooLarge")
                }
            };
            if let Some(kind) = kind {
                logs = json!([[kind, format!("family={family} bytes={length}")]]);
            }
        }
        assert_eq!(!response.is_null(), case[5]);
        assert_eq!(logs, case[6]);
        assert_eq!(response, case[7]);
        counted += 1;
    }
    assert_eq!(counted, 637);
}
