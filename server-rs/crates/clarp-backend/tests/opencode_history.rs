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

/// Issue 874. A past OpenCode session that compacted its context by itself: its own user message
/// of the compaction, its summary for itself, and its own message that tells it to go on are no
/// rows of the conversation; the user's message and the answer are.
#[test]
fn a_past_sessions_own_compaction_is_no_row() {
    use serde_json::json;
    let messages: Vec<(i64, Value, Vec<Value>)> = vec![
        (
            1_795_000_000_000,
            json!({"role": "user", "parts": [{"type": "text", "text": "Fix the build."}]}),
            vec![],
        ),
        (
            1_795_000_001_000,
            json!({"role": "user", "parts": [{"type": "compaction", "auto": true}]}),
            vec![],
        ),
        (
            1_795_000_002_000,
            json!({"role": "assistant", "mode": "compaction", "agent": "compaction",
                   "summary": true, "finish": "stop",
                   "parts": [{"type": "text", "text": "## Objective\nSummarize conversation history to enable a coding agent to continue"}]}),
            vec![],
        ),
        (
            1_795_000_003_000,
            json!({"role": "user", "parts": [{"type": "text", "synthetic": true,
                   "text": "Continue if you have next steps, or stop and ask for clarification."}]}),
            vec![],
        ),
        (
            1_795_000_004_000,
            json!({"role": "assistant", "mode": "build", "agent": "build", "finish": "stop",
                   "parts": [{"type": "text", "text": "The build is fixed."}]}),
            vec![],
        ),
    ];
    let rows = clarp_backend::history::opencode::turns(&messages);
    let said: Vec<(&str, &str)> = rows
        .iter()
        .map(|row| (row["role"].as_str().unwrap(), row["text"].as_str().unwrap()))
        .collect();
    assert_eq!(
        said,
        [
            ("user", "Fix the build."),
            ("assistant", "The build is fixed.")
        ]
    );
}
