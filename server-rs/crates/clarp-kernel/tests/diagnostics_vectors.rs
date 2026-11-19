#![allow(clippy::unwrap_used)]
use clarp_kernel::diagnostics::{self as rules, Settings};
use serde_json::{Value, json};
#[test]
fn diagnostic_policy_matches_the_reference() {
    let f: Value = serde_json::from_str(include_str!("fixtures/diagnostics.json")).unwrap();
    let mut counts = [0; 5];
    for row in f["updates"].as_array().unwrap() {
        let result = Settings::update(&row[0])
            .map(Settings::public)
            .unwrap_or_else(Value::String);
        assert_eq!(result, row[1]);
        counts[0] += 1;
    }
    for row in f["decode"].as_array().unwrap() {
        assert_eq!(Settings::decode(row[0].as_str().unwrap()).public(), row[1]);
        counts[1] += 1;
    }
    for row in f["gates"].as_array().unwrap() {
        let s = Settings::update(&json!({"enabled":row[0],"categories":row[1]})).unwrap();
        assert_eq!(json!(s.uploads()), row[2]);
        for [source, name, _, category, allowed] in
            serde_json::from_value::<Vec<[Value; 5]>>(row[3].clone()).unwrap()
        {
            if source == "client" {
                assert_eq!(rules::client_category(name.as_str().unwrap()), category);
            }
            let permit = if name == "httpRequest" {
                s.requests()
            } else {
                s.allows(category.as_str().unwrap())
            };
            assert_eq!(json!(permit), allowed);
        }
        counts[2] += 1;
    }
    for row in f["private"].as_array().unwrap() {
        assert_eq!(rules::safe_detail(&row[0]), row[1]);
        counts[3] += 1;
    }
    for row in f["health"].as_array().unwrap() {
        assert_eq!(
            rules::health(
                row[0] == true,
                row[1] != "hls" || row[2] == true,
                row[3]["subsystems"].clone(),
                7,
                2,
                "test-version"
            ),
            row[3]
        );
        counts[4] += 1;
    }
    assert_eq!(counts, [40, 6, 24, 65, 8]);
}

#[test]
fn diagnostic_client_uploads_match_the_handler_and_only_keep_the_ruled_identifiers() {
    let f: Value = serde_json::from_str(include_str!("fixtures/diagnostics.json")).unwrap();
    let mut n = 0;
    for row in f["uploads"].as_array().unwrap() {
        let name = rules::client_name(&row[0]);
        assert_eq!(rules::client_event(&row[0], name), row[1]);
        n += 1;
    }
    assert_eq!(n, 48);
    let keys = [
        "session",
        "agent_id",
        "backend_session_id",
        "client_id",
        "trace_id",
    ];
    for size in [0, 1, 127, 128, 129] {
        let id = "é".repeat(size);
        let mut item = json!({"persona":"private","clip_url":"address","clip_id":7,"duration_ms":2.5,"extra":"excluded"});
        for k in keys {
            item[k] = json!(id);
        }
        let event = rules::client_event(&item, "fixture");
        for k in keys {
            assert_eq!(event[k], "é".repeat(size.min(128)));
        }
        assert_eq!(event["clip_id"], 7);
        assert_eq!(event["duration_ms"], 2.5);
        assert_eq!(event.as_object().unwrap().len(), 9);
    }
    for bad in [json!(null), json!(false), json!("42"), json!([]), json!({})] {
        let item = json!({"trace_id":3,"clip_id":bad,"duration_ms":bad});
        assert_eq!(
            rules::client_event(&item, "log"),
            json!({"kind":"client_upload","name":"log"})
        );
    }
    assert!(
        rules::client_event(&json!({"clip_id":1.5}), "log")
            .get("clip_id")
            .is_none()
    );
}
