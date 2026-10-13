use std::borrow::Cow;

use clarp_core::endpoint::error_message;
use clarp_core::host_json::{ContentIdentity, REPAIRED_FIELD, parse, repair_lone_surrogates, verify_content};
use clarp_core::sse::SseParser;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

/// JSON source with `~` standing for each backslash, so escapes read as the
/// Host writes them.
fn j(source: &str) -> Vec<u8> {
    source.replace('~', "\\").into_bytes()
}

fn c1(text: &str) -> String {
    format!("c1:{}", Sha256::digest(text.as_bytes()).iter().map(|b| format!("{b:02x}")).collect::<String>())
}

fn object(value: &Value) -> &serde_json::Map<String, Value> {
    value.as_object().expect("an object")
}

#[test]
fn serde_json_alone_rejects_the_whole_body_for_one_lone_surrogate() {
    for source in [r#"["~ud800"]"#, r#"["~udc00"]"#, r#"["~ud800x"]"#, r#"{"a":[1,"~ud83d~ude00"],"b":"~ud800"}"#] {
        let error = serde_json::from_slice::<Value>(&j(source)).expect_err(source);
        assert!(error.to_string().contains("escape"), "{source}: {error}");
    }
}

#[test]
fn only_unpaired_escapes_are_rewritten_byte_for_byte() {
    let cases = [
        // Two highs in a row: the first is lone, the second pairs.
        (r#""~ud800~ud83d~ude00""#, r#""~ufffd~ud83d~ude00""#, 1),
        // A low that follows a lone high is lone too.
        (r#""~ud800~u0041~udc00""#, r#""~ufffd~u0041~ufffd""#, 2),
        // Low then high is no pair.
        (r#""~udc00~ud800""#, r#""~ufffd~ufffd""#, 2),
        (r#""~ud83d~ude00~udc00""#, r#""~ud83d~ude00~ufffd""#, 1),
        // Upper-case hex is still an escape.
        (r#""~uD800~uDBFF~uDFFF""#, r#""~ufffd~uDBFF~uDFFF""#, 1),
        // At the very end of the body, and cut short.
        (r#""~ud800"#, r#""~ufffd"#, 1),
        (r#""~ud8"#, r#""~ud8"#, 0),
        // An escaped backslash then "ud800" is text, not an escape.
        (r#""~~ud800""#, r#""~~ud800""#, 0),
        (r#""~~~ud800""#, r#""~~~ufffd""#, 1),
        (r#""~"~ud800~n""#, r#""~"~ufffd~n""#, 1),
    ];
    for (input, output, count) in cases {
        let (input, output) = (j(input), j(output));
        let (repaired, lone) = repair_lone_surrogates(&input);
        assert_eq!((&repaired[..], lone.len()), (&output[..], count), "{}", String::from_utf8_lossy(&input));
    }
}

#[test]
fn a_repair_touches_only_its_own_escape_in_keys_values_and_raw_utf8() {
    let body = j(r#"{"~ud800":"a","k~ud800y":["~ud800","x","~~ud800","é ø 😀 ~"q~"",{"~~ud800":"~ud83d~ude00"}],"raw":"漢~ud800字","n":1.5}"#);
    let (repaired, lone) = repair_lone_surrogates(&body);
    assert_eq!(lone.len(), 4);
    let expected = j(r#"{"~ufffd":"a","k~ufffdy":["~ufffd","x","~~ud800","é ø 😀 ~"q~"",{"~~ud800":"~ud83d~ude00"}],"raw":"漢~ufffd字","n":1.5}"#);
    assert_eq!(&repaired[..], &expected[..]);
    let value = parse(&body, "/test").unwrap();
    assert_eq!(
        value,
        json!({
            "\u{FFFD}": "a",
            "k\u{FFFD}y": ["\u{FFFD}", "x", "\\ud800", "é ø 😀 \"q\"", {"\\ud800": "😀"}],
            "raw": "漢\u{FFFD}字",
            "n": 1.5,
            "_desktop_repaired": 4,
        })
    );
}

#[test]
fn valid_json_reaches_serde_as_the_same_bytes_and_is_never_marked() {
    let bodies = [
        j(r#"{"a":"~ud83d~ude00 ~u00e9 ~~ud800 plain ø 漢字","~u0041~~udc00":[1,"~"",{"b":null}]}"#),
        // U+FFFD sent by the Host itself is its own text, not a repair.
        j(r#"{"a":"~ufffd","b":"�"}"#),
        b"".to_vec(),
        b"\\".to_vec(),
    ];
    for body in &bodies {
        let (repaired, lone) = repair_lone_surrogates(body);
        assert!(lone.is_empty());
        assert!(matches!(repaired, Cow::Borrowed(bytes) if std::ptr::eq(bytes, &body[..])));
    }
    for body in &bodies[..2] {
        let ours = parse(body, "/test").unwrap();
        assert_eq!(ours, serde_json::from_slice::<Value>(body).unwrap());
        assert!(!ours.to_string().contains(REPAIRED_FIELD));
    }
    assert_eq!(REPAIRED_FIELD, "_desktop_repaired");
}

#[test]
fn every_object_holding_a_repaired_string_is_marked_and_no_other() {
    let body = j(r#"{"artifacts":[{"id":"bad","content":"x~ud800","meta":{"title":"~udc00","ok":"y"}},{"id":"good","content":"fine"}],"next":"c2"}"#);
    let value = parse(&body, "/artifacts").unwrap();
    assert_eq!(value[REPAIRED_FIELD], 2);
    assert_eq!(value["artifacts"][0][REPAIRED_FIELD], 2);
    assert_eq!(value["artifacts"][0]["meta"][REPAIRED_FIELD], 1);
    assert_eq!(value["artifacts"][1], json!({"id": "good", "content": "fine"}));
    assert_eq!(value["next"], "c2");
}

#[test]
fn a_repaired_string_is_never_claimed_verified() {
    let marker = |sha: Value| json!({"reason": "summary", "sha256": sha, "version": 3, "updated_at": 1700});

    let clean = parse(&j(r#"{"id":"a","content":"hello ~ud83d~ude00"}"#), "/artifacts/a").unwrap();
    let content = clean["content"].as_str().unwrap();
    assert_eq!(verify_content(object(&clean), content, object(&marker(c1(content).into()))), ContentIdentity::Verified(c1(content)));
    assert_eq!(verify_content(object(&clean), content, object(&marker(c1("other").into()))), ContentIdentity::Mismatch);

    // Even a digest that matches the repaired text does not verify it.
    let repaired = parse(&j(r#"{"id":"b","content":"tail ~udc00"}"#), "/artifacts/b").unwrap();
    let content = repaired["content"].as_str().unwrap();
    assert_eq!(content, "tail \u{FFFD}");
    let fallback = ContentIdentity::Unverified { id: "b".into(), updated_at: Some(1700), version: Some(3), repaired: true };
    assert_eq!(verify_content(object(&repaired), content, object(&marker(c1(content).into()))), fallback);

    // The Host's own hash_pending falls back the same way, without the repair.
    let mut pending = marker(Value::Null);
    pending["hash_pending"] = true.into();
    let fallback = ContentIdentity::Unverified { id: "a".into(), updated_at: Some(1700), version: Some(3), repaired: false };
    assert_eq!(verify_content(object(&clean), clean["content"].as_str().unwrap(), object(&pending)), fallback);
}

#[test]
fn an_sse_event_split_mid_escape_survives_a_lone_surrogate() {
    let mut parser = SseParser::default();
    assert!(parser.feed(&j(r#"id: 7
event: artifact
data: {"type":"artifact","text":"a~ud8"#)).is_empty());
    let messages = parser.feed(&j("00b\",\"pair\":\"~ud83d~ude00\"}\n\n"));
    assert_eq!(messages.len(), 1);
    assert_eq!(messages[0].id, "7");
    assert_eq!(messages[0].data["text"], "a\u{FFFD}b");
    assert_eq!(messages[0].data["pair"], "😀");
    assert_eq!(messages[0].data[REPAIRED_FIELD], 1);
}

#[test]
fn an_error_reply_with_a_lone_surrogate_still_yields_its_message() {
    assert_eq!(error_message(&j(r#"{"message":"bad ~ud800 name"}"#), "fallback"), "bad \u{FFFD} name");
}
