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

#[test]
fn portrait_start_matches_the_reference_admission_after_the_http_guards() {
    use clarp_kernel::portrait_generation::{Start, start};
    use serde_json::json;
    let v: Value = serde_json::from_str(include_str!("fixtures/portrait-generation.json")).unwrap();
    let mut counts = [0; 4];
    for row in v["starts"].as_array().unwrap() {
        let body = &row[0];
        if !body.is_object() || body["session"] == "janitor" {
            // These checks are exercised at the Host's HTTP boundary.
            assert!(matches!(row[3].as_u64(), Some(400 | 403)));
            counts[0] += 1;
            continue;
        }
        let cap = json!({"available":row[2],"reason":"Add or select a primary portrait first","job":{"status":row[1]}});
        match start(body["session"] != "missing", &cap) {
            Start::Begin => {
                assert_eq!(row[3], 202);
                assert_eq!(row[5], true);
                assert_eq!(row[6], true);
                counts[1] += 1;
            }
            Start::Reuse => {
                assert_eq!(row[3], 202);
                assert_eq!(row[5], false);
                assert_eq!(row[6], false);
                counts[2] += 1;
            }
            Start::Refused(why) => {
                assert_eq!(row[3], 409);
                assert_eq!(row[4], why);
                assert_eq!(row[5], false);
                assert_eq!(row[6], false);
                counts[3] += 1;
            }
        }
    }
    assert_eq!(counts, [36, 8, 4, 24]);
}

#[test]
fn generated_portrait_bytes_include_the_last_allowed_byte_and_require_a_supported_signature() {
    use clarp_kernel::portrait_generation::{MAX_IMAGE, generated_kind};
    for (header, expected) in [
        (b"GIF89a".as_slice(), Some("image/gif")),
        (b"RIFFbad!", None),
        (b"RIFF1234WEBP", Some("image/webp")),
        (b"\xff\xd8\xff", Some("image/jpeg")),
    ] {
        assert_eq!(generated_kind(header).ok(), expected);
    }
    let mut image = b"\x89PNG\r\n\x1a\n".to_vec();
    for (size, valid) in [
        (MAX_IMAGE - 1, true),
        (MAX_IMAGE, true),
        (MAX_IMAGE + 1, false),
    ] {
        image.resize(size, 0);
        assert_eq!(generated_kind(&image).is_ok(), valid);
    }
}

#[test]
fn portrait_edit_answers_are_bounded_and_require_a_decodable_image() {
    use clarp_kernel::portrait_generation::{MAX_RESPONSE, edit_answer};
    let image = b"\xff\xd8\xffedited";
    let valid = serde_json::json!({"data":[{"b64_json":clarp_kernel::python::b64encode(image)}]});
    let mut raw = serde_json::to_vec(&valid).unwrap();
    for size in [MAX_RESPONSE - 1, MAX_RESPONSE, MAX_RESPONSE + 1] {
        raw.resize(size, b' ');
        if size <= MAX_RESPONSE {
            assert_eq!(edit_answer(&raw).unwrap(), image);
        } else {
            assert_eq!(
                edit_answer(&raw).unwrap_err(),
                "OpenAI image response exceeded the safety limit"
            );
        }
    }
    for bad in [
        b"not json".as_slice(),
        b"{}",
        br#"{"data":[{"b64_json":55}]}"#,
        br#"{"data":[{"b64_json":"%%%"}]}"#,
    ] {
        assert_eq!(
            edit_answer(bad).unwrap_err(),
            "OpenAI image response was invalid"
        );
    }
}
