#![allow(clippy::unwrap_used)]

use clarp_kernel::heard::Failure;
use serde::Deserialize;
use serde_json::Value;

#[derive(Deserialize)]
struct Case {
    kind: Failure,
    #[serde(rename = "type")]
    name: String,
    status: u16,
    word: String,
    voice: Vec<Value>,
}

#[test]
fn client_words_match_each_reference_failure() {
    let cases: Vec<Case> =
        serde_json::from_str(include_str!("fixtures/transcription-error-vectors.json")).unwrap();
    for case in cases {
        assert_eq!(
            case.kind.client_words(),
            (case.status, case.word.as_str()),
            "{}",
            case.name
        );
        assert_eq!(
            case.kind.voice_detail().into_iter().collect::<Vec<_>>(),
            case.voice,
            "{}",
            case.name
        );
    }
}
