#![allow(clippy::unwrap_used)]
//! What the user's compaction did, held to the Python Host's own functions
//! (`server-rs/tools/compaction-outcome-vectors.py`), and what a turn's end means for it here.

use clarp_kernel::compaction::{self, Boundary, Decided};
use clarp_kernel::turn::Outcome;
use serde_json::{Value, json};

fn vectors() -> Value {
    serde_json::from_str(include_str!("fixtures/compaction-outcome-vectors.json")).unwrap()
}

fn number(value: &Value) -> Option<u64> {
    value.as_u64()
}

/// A line of the conversation as Claude Code writes it, as its output says the same: the
/// record's names written as the output writes them.
fn as_said(text: &str) -> Option<Value> {
    let said = text
        .replace("compactMetadata", "compact_metadata")
        .replace("preTokens", "pre_tokens")
        .replace("postTokens", "post_tokens");
    serde_json::from_str(&said).ok()
}

#[test]
fn what_is_said_has_the_python_hosts_shape() {
    let vectors = vectors();
    let mut counted = 0;
    for case in vectors["shapes"].as_array().unwrap() {
        let said = compaction::outcome(
            case["kind"].as_str().unwrap(),
            number(&case["before"]),
            number(&case["after"]),
            case["after_is_estimate"].as_bool().unwrap(),
            case["detail"].as_str().unwrap(),
        );
        assert_eq!(serde_json::to_value(&said).unwrap(), case["said"], "{case}");
        counted += 1;
    }
    assert_eq!(
        json!([
            compaction::COMPACTED,
            compaction::NOT_COMPACTED,
            compaction::UNKNOWN
        ]),
        vectors["outcomes"]
    );
    assert_eq!(
        json!(compaction::HARD_CAP_SEC as f64),
        vectors["hard_cap_sec"]
    );
    assert_eq!(counted, 12);
}

#[test]
fn claude_codes_record_decides_as_on_the_python_host() {
    let mut counted = 0;
    for case in vectors()["decided"].as_array().unwrap() {
        let said = compaction::decide_claude(
            number(&case["before"]),
            number(&case["proof"]),
            case["transcript"].as_bool().unwrap(),
            case["ending"].as_str().unwrap(),
        );
        assert_eq!(serde_json::to_value(&said).unwrap(), case["said"], "{case}");
        counted += 1;
    }
    assert_eq!(counted, 128);
}

#[test]
fn claude_codes_record_is_read_as_on_the_python_host() {
    let vectors = vectors();
    let mut counted = 0;
    for case in vectors["read"].as_array().unwrap() {
        let text = case["text"].as_str().unwrap();
        let read = as_said(text).and_then(|line| compaction::boundary(&line));
        assert_eq!(
            read.map(|record| record.pre_tokens),
            number(&case["pre_tokens"]),
            "{}",
            case["line"]
        );
        // What the record says of the size after is its own number, or none.
        if let Some(record) = read {
            let after = as_said(text).unwrap()["compact_metadata"]["post_tokens"].as_u64();
            assert_eq!(record.post_tokens, after, "{}", case["line"]);
        }
        counted += 1;
    }
    let mut newest = 0;
    for case in vectors["newest"].as_array().unwrap() {
        let mut held = None;
        for line in case["lines"].as_array().unwrap() {
            let line = as_said(&line.to_string()).unwrap();
            held = compaction::newest(held, compaction::boundary(&line));
        }
        assert_eq!(
            held.map(|record| record.pre_tokens),
            number(&case["pre_tokens"]),
            "{}",
            case["written"]
        );
        newest += 1;
    }
    assert_eq!((counted, newest), (23, 6));
}

/// What a compaction turn's end means here, and the one thing this Host says that the Python
/// Host does not: the size after, where Claude Code's record says it.
#[test]
fn a_compaction_turns_end_says_what_it_did() {
    let record = |pre, post| Some(Boundary { pre_tokens: pre, post_tokens: post });
    let python = |before, proof, ending| compaction::decide_claude(before, proof, true, ending);
    let stopped = Outcome::Stopped {
        by: "user".to_string(),
        verified: true,
        reason: String::new(),
    };
    let failed = Outcome::failed("API Error: 529 overloaded".to_string());
    let interrupted = Outcome::Interrupted {
        reason: "the tool ended and nothing said how".to_string(),
    };
    let withdrawn = Outcome::Withdrawn {
        by: "user".to_string(),
    };
    let mut counted = 0;
    // A record with both sizes: compacted, the size after is the tool's own number, and the
    // words say nothing of a next call.
    for ended in [&Outcome::Completed, &stopped, &failed, &interrupted] {
        let said = compaction::decide(ended, record(30_729, Some(8_957)), Some(41_250), "");
        let mut python = python(Some(41_250), Some(30_729), "settled");
        assert_eq!(said.outcome, compaction::COMPACTED);
        assert_eq!(said.after_tokens, Some(8_957));
        assert!(!said.after_is_estimate);
        assert_eq!(said.detail, "Claude Code recorded the compaction");
        python.after_tokens = said.after_tokens;
        python.detail.clone_from(&said.detail);
        assert_eq!(said, python);
        counted += 1;
    }
    // A record without a size after: the Python Host's answer as it is.
    let said = compaction::decide(&Outcome::Completed, record(30_729, None), Some(41_250), "");
    assert_eq!(said, python(Some(41_250), Some(30_729), "settled"));
    // A record without a size before: the gauge before is the size before.
    let said = compaction::decide(&Outcome::Completed, record(0, Some(8_957)), Some(41_250), "");
    assert_eq!(said.before_tokens, Some(41_250));
    let said = compaction::decide(&Outcome::Completed, record(0, None), None, "");
    assert_eq!(said, python(None, Some(0), "settled"));
    counted += 3;
    // No record.
    let none = |ended: &Outcome| compaction::decide(ended, None, Some(41_250), "");
    assert_eq!(none(&Outcome::Completed), python(Some(41_250), None, "settled"));
    assert_eq!(none(&stopped), python(Some(41_250), None, "stopped"));
    let expect = |kind: &str, detail: &str| Decided {
        outcome: kind.to_string(),
        before_tokens: Some(41_250),
        after_tokens: None,
        after_is_estimate: false,
        detail: detail.to_string(),
    };
    assert_eq!(
        none(&withdrawn),
        expect(compaction::NOT_COMPACTED, "stopped before it began")
    );
    assert_eq!(
        none(&interrupted),
        expect(compaction::UNKNOWN, compaction::ENDED_UNSAID)
    );
    // A failure the tool said: the words the chat shows of it.
    let told = clarp_kernel::failures::told(
        clarp_kernel::failures::classify_error("API Error: 529 overloaded"),
        "API Error: 529 overloaded",
        "",
        clarp_kernel::failures::Known::default(),
    );
    assert_eq!(none(&failed), expect(compaction::NOT_COMPACTED, &told.row));
    let unkinded = Outcome::Failed {
        reason: "API Error: 529 overloaded".to_string(),
        kind: String::new(),
    };
    assert_eq!(none(&unkinded), none(&failed));
    counted += 6;
    assert_eq!(counted, 13);
}
