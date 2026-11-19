//! The chat a recording is for, and what is kept of the chat each client opened: the
//! Python Host's own answers (`tools/client-chat-vectors.py`).
#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_kernel::heard;
use serde_json::Value;

#[test]
fn a_recording_is_for_the_chat_the_python_host_takes_it_to_be_for() {
    let said: Value =
        serde_json::from_str(include_str!("fixtures/client-chat-vectors.json")).unwrap();
    let known: Vec<&str> = said["known"]
        .as_array()
        .unwrap()
        .iter()
        .map(|name| name.as_str().unwrap())
        .collect();
    let mut kept = 0;
    for case in said["recorded"].as_array().unwrap() {
        let principal = case["principal"].as_str().unwrap();
        let session = case["session"].as_str().unwrap();
        // Nothing is kept for nobody, nor of no chat.
        if principal.is_empty() || session.is_empty() {
            assert!(case["kept"].is_null(), "{case}");
            continue;
        }
        assert_eq!(
            heard::client_chat_key(principal),
            case["key"].as_str().unwrap(),
            "{case}"
        );
        let at = case["at"].as_i64().unwrap();
        assert_eq!(
            heard::client_chat_kept(session, at),
            case["kept"].as_str().unwrap(),
            "{case}"
        );
        kept += 1;
    }
    let mut read = 0;
    for case in said["read"].as_array().unwrap() {
        let now = case["now"].as_i64().unwrap();
        let ours = heard::client_chat(case["kept"].as_str(), now);
        match &case["current"] {
            // What the reference cannot read at all (a list where an object was written)
            // it never wrote; here it is no chat.
            Value::Object(raised) => {
                assert!(raised.contains_key("raised"), "{case}");
                assert_eq!(ours, "", "{case}");
            }
            current => assert_eq!(ours, current.as_str().unwrap(), "{case}"),
        }
        read += 1;
    }
    let mut chosen = 0;
    for case in said["chosen"].as_array().unwrap() {
        let header = case["header"].as_str().unwrap_or("");
        let last = case["last"].as_str().unwrap();
        let ours = heard::transcription_session(header, last, |name| known.contains(&name));
        assert_eq!(ours, case["session"].as_str().unwrap(), "{case}");
        chosen += 1;
    }
    assert_eq!((kept, read, chosen), (12, 70, 32));
}
