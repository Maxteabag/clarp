#![allow(clippy::unwrap_used)]
use serde_json::Value;

#[test]
fn the_shared_native_transcript_reads_as_the_python_host_reads_it() {
    let cases: Value = serde_json::from_str(include_str!(
        "../../clarp-kernel/tests/fixtures/backend-opencode-vectors.json"
    ))
    .unwrap();
    let mut count = 0;
    for row in cases["history"].as_array().unwrap() {
        let messages = serde_json::from_value(row[0].clone())
            .unwrap_or_else(|e| panic!("invalid history fixture: {e}"));
        let messages: Vec<(i64, Value, Vec<Value>)> = messages;
        assert_eq!(
            serde_json::json!(clarp_backend::history::opencode::turns(&messages)),
            row[1],
            "story {count}: {messages:?}"
        );
        count += 1;
    }
    assert_eq!(count, 81);
}
