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
    assert_eq!(counts, [40, 6, 24, 60, 8]);
}

#[test]
fn diagnostic_client_detail_cannot_copy_free_text_or_untrusted_field_names() {
    let secret = "SYNTHETIC user sentence, tool output or machine path";
    let mut input = json!({"detail":[{"status":"SYNTHETIC secret-token","bytes":42}]});
    input[secret] = json!(secret);
    let kept = rules::safe_detail(&input);
    assert!(!kept.to_string().contains("SYNTHETIC"));
    assert!(
        kept["detail"][0]["status"]
            .as_str()
            .unwrap()
            .starts_with("<redacted length=")
    );
    assert_eq!(kept["detail"][0]["bytes"], 42);
    assert!(
        kept.as_object()
            .unwrap()
            .keys()
            .any(|key| key.starts_with("field_"))
    );
}
