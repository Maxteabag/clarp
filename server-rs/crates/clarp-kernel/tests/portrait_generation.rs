//! The rules exercised by the portrait routes in the isolated Python Host.
#![allow(clippy::unwrap_used)]
use clarp_kernel::portraits;
use serde_json::Value;

#[test]
fn the_model_display_switch_reads_the_values_the_reference_accepts() {
    let cases: Value =
        serde_json::from_str(include_str!("fixtures/portrait-generation.json")).unwrap();
    let values = cases["preferences"].as_array().unwrap();
    for row in values {
        assert_eq!(
            portraits::model_avatars(row[0].as_str()),
            row[1]["model_avatars"].as_bool().unwrap(),
            "{row}"
        );
    }
    assert_eq!(values.len(), 10);
}

#[test]
fn the_edits_and_capability_match_the_reference() {
    use clarp_kernel::portrait_generation as rules;
    use serde_json::json;
    let v: Value = serde_json::from_str(include_str!("fixtures/portrait-generation.json")).unwrap();
    for case in v["capabilities"].as_array().unwrap() {
        let primary = case[2]
            .as_bool()
            .unwrap()
            .then(|| (case[3].as_str().unwrap(), case[4].as_u64().unwrap()));
        assert_eq!(
            rules::capability(
                case[0].as_bool().unwrap(),
                case[1].as_bool().unwrap(),
                primary,
                Value::Null,
                0
            ),
            case[5]
        );
    }
    for case in v["ids"].as_array().unwrap() {
        assert_eq!(rules::job_id(case[0].as_str().unwrap()), case[1]);
    }
    assert_eq!(json!(rules::PROMPTS), v["prompts"]);
    let mut bodies = Vec::new();
    for prompt in rules::PROMPTS {
        for mime in ["image/png", "image/jpeg", "image/webp"] {
            let bytes = rules::multipart("fixture", prompt, b"fixture", mime);
            bodies.push(String::from_utf8(bytes).unwrap());
        }
    }
    assert_eq!(json!(bodies), v["multipart"]);
    let primary = Some(("image/png", 10));
    for n in [19, 20, 21] {
        let cap = rules::capability(true, true, primary, Value::Null, n);
        assert_eq!(cap["available"], n < 20);
        assert_eq!(cap["reason"], if n < 20 { "" } else { rules::DAILY_REASON });
        for active in ["queued", "running"] {
            let existing = json!({"status":active});
            let cap = rules::capability(true, true, primary, existing.clone(), n);
            assert_eq!(cap["job"], existing);
            assert_eq!(cap["available"], true);
        }
    }
}
