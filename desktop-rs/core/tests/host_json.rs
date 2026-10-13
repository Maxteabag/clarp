use std::borrow::Cow;

use clarp_core::endpoint::error_message;
use clarp_core::host_json::{parse, repair_lone_surrogates};
use clarp_core::sse::SseParser;
use serde_json::{Value, json};

// What Python's json.dumps writes for "\ud800", "\udc00", "\ud800x" and "😀".
const BODY: &[u8] = br#"{"items":[{"id":"a","text":"\ud800"},{"id":"b","text":"\udc00"},{"id":"c","text":"\ud800x"},{"id":"d","text":"😀"}],"total":4}"#;

#[test]
fn serde_json_alone_rejects_the_whole_body_for_one_lone_surrogate() {
    for body in [BODY, br#"["\ud800"]"#, br#"["\udc00"]"#, br#"["\ud800x"]"#] {
        let error = serde_json::from_slice::<Value>(body).expect_err("a lone surrogate fails the parse");
        assert!(error.to_string().contains("escape"), "{error}");
    }
}

#[test]
fn lone_surrogates_become_replacement_characters_and_the_rest_survives() {
    let value = parse(BODY, "/artifacts").expect("the body parses");
    let texts: Vec<&str> = value["items"].as_array().unwrap().iter().map(|i| i["text"].as_str().unwrap()).collect();
    assert_eq!(texts, ["\u{FFFD}", "\u{FFFD}", "\u{FFFD}x", "😀"]);
    assert_eq!(value["items"][3]["id"], "d");
    assert_eq!(value["total"], 4);
}

#[test]
fn only_unpaired_escapes_are_rewritten_byte_for_byte() {
    let (repaired, count) = repair_lone_surrogates(BODY);
    assert_eq!(count, 3);
    assert_eq!(repaired.len(), BODY.len());
    let expected = br#"{"items":[{"id":"a","text":"�"},{"id":"b","text":"�"},{"id":"c","text":"�x"},{"id":"d","text":"😀"}],"total":4}"#;
    assert_eq!(&repaired[..], &expected[..]);

    let cases: [(&[u8], &[u8], usize); 8] = [
        // Two highs in a row: the first is lone, the second pairs.
        (br#""\ud800😀""#, br#""�😀""#, 1),
        // A low that follows a lone high is lone too.
        (br#""\ud800A\udc00""#, br#""�A�""#, 2),
        // Upper-case hex is still an escape.
        (br#""\uD800􏿿""#, br#""�􏿿""#, 1),
        // At the very end of the body.
        (br#""\ud800"#, br#""�"#, 1),
        (br#""\ud8"#, br#""\ud8"#, 0),
        // A literal backslash then "ud800" is text, not an escape.
        (br#""\\ud800""#, br#""\\ud800""#, 0),
        (br#""\\\ud800""#, br#""\\�""#, 1),
        (br#""\"\ud800\n""#, br#""\"�\n""#, 1),
    ];
    for (input, output, replaced) in cases {
        let (repaired, count) = repair_lone_surrogates(input);
        assert_eq!((&repaired[..], count), (output, replaced), "{}", String::from_utf8_lossy(input));
    }
}

#[test]
fn a_body_without_lone_surrogates_is_not_copied() {
    for body in [&br#"{"a":"😀 é \\ud800 plain ø"}"#[..], b"", b"\\"] {
        assert!(matches!(repair_lone_surrogates(body), (Cow::Borrowed(_), 0)), "{}", String::from_utf8_lossy(body));
    }
    assert_eq!(parse(br#"{"a":"\\ud800"}"#, "/x").unwrap()["a"], "\\ud800");
}

#[test]
fn a_replaced_string_is_never_claimed_verified() {
    // The Host cannot hash a lone surrogate and says hash_pending; the repair
    // changes the text but leaves that marker alone and adds no c1: hash.
    let body = br#"{"summary":{"text":"tail \udc00","content_hash":"hash_pending"}}"#;
    let value = parse(body, "/artifacts/x").unwrap();
    assert_eq!(value["summary"], json!({"text": "tail \u{FFFD}", "content_hash": "hash_pending"}));
}

#[test]
fn an_sse_event_split_mid_escape_survives_a_lone_surrogate() {
    let mut parser = SseParser::default();
    assert!(parser.feed(b"id: 7\nevent: artifact\ndata: {\"type\":\"artifact\",\"text\":\"a\\ud8").is_empty());
    let messages = parser.feed(b"00b\",\"pair\":\"\\ud83d\\ude00\"}\n\n");
    assert_eq!(messages.len(), 1);
    assert_eq!(messages[0].id, "7");
    assert_eq!(messages[0].data["text"], "a\u{FFFD}b");
    assert_eq!(messages[0].data["pair"], "😀");
}

#[test]
fn an_error_reply_with_a_lone_surrogate_still_yields_its_message() {
    assert_eq!(error_message(br#"{"message":"bad \ud800 name"}"#, "fallback"), "bad \u{FFFD} name");
}
