//! The reading of a failed turn against the Python Host's.
//!
//! `fixtures/failure-vectors.json` is written by `server-rs/tools/failure-vectors.py` from
//! `server/lib/error_classify.py`: every text the Python Host's own tests of that module use,
//! the wordings its comments record from real failures, and variations of both. The same must
//! be read here.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_kernel::failures::{
    self, NOTIFY, QUOTA_UNCONFIRMED_MESSAGE, account_wait_message, classify_error, classify_result,
    explain, is_account_wait_message, provider_message, usage_limit_resets,
};
use serde_json::Value;

fn vectors() -> Value {
    serde_json::from_str(include_str!("fixtures/failure-vectors.json")).unwrap()
}

#[test]
fn a_failure_is_read_as_the_python_host_reads_it() {
    let vectors = vectors();
    let texts = vectors["texts"].as_array().unwrap();
    assert!(texts.len() > 600, "the corpus is the Python Host's own");
    let mut wrong = Vec::new();
    for vector in texts {
        let text = vector["text"].as_str().unwrap();
        let ours = (
            classify_error(text),
            usage_limit_resets(text),
            explain(text),
            provider_message(text),
            is_account_wait_message(text),
        );
        let theirs = (
            vector["kind"].as_str().unwrap(),
            vector["resets"].as_bool().unwrap(),
            vector["explained"].as_str().unwrap(),
            vector["provider_message"].as_str().unwrap().to_string(),
            vector["account_wait"].as_bool().unwrap(),
        );
        if ours != theirs {
            wrong.push(format!("{text:?}: {ours:?}, the Python Host: {theirs:?}"));
        }
    }
    assert!(
        wrong.is_empty(),
        "{} of {} read differently:\n{}",
        wrong.len(),
        texts.len(),
        wrong.join("\n")
    );
    // Nothing said is read as an empty text is.
    let absent = &vectors["absent"];
    assert_eq!(classify_error(""), absent["kind"].as_str().unwrap());
    assert_eq!(usage_limit_resets(""), absent["resets"].as_bool().unwrap());
    assert_eq!(explain(""), absent["explained"].as_str().unwrap());
    assert_eq!(
        provider_message(""),
        absent["provider_message"].as_str().unwrap()
    );
    assert_eq!(
        is_account_wait_message(""),
        absent["account_wait"].as_bool().unwrap()
    );
}

#[test]
fn a_tools_last_word_on_a_turn_is_read_as_the_python_host_reads_it() {
    let vectors = vectors();
    for vector in vectors["results"].as_array().unwrap() {
        assert_eq!(
            classify_result(&vector["event"]),
            vector["kind"].as_str().unwrap(),
            "{}",
            vector["event"]
        );
    }
}

#[test]
fn a_wait_for_an_account_is_worded_as_the_python_host_words_it() {
    let vectors = vectors();
    for vector in vectors["waits"].as_array().unwrap() {
        let said = account_wait_message(
            vector["backend"].as_str().unwrap(),
            vector["model"].as_str(),
        );
        assert_eq!(said, vector["message"].as_str().unwrap());
        assert!(
            is_account_wait_message(&said) || vector["backend"] == "",
            "{said}"
        );
    }
}

#[test]
fn the_kinds_and_who_is_told_are_the_python_hosts() {
    let vectors = vectors();
    let mut kinds = vec![
        failures::CLEAN,
        failures::CONNECTION,
        failures::TRANSIENT,
        failures::INTERRUPTED,
        failures::USAGE_LIMIT,
        failures::AUTH,
        failures::ACCOUNT_PLAN,
        failures::RUNNER_EXIT,
        failures::TIMEOUT,
        failures::NOT_STARTED,
        failures::UNKNOWN,
        failures::MUSE_SESSION_LOCKED,
    ];
    kinds.sort_unstable();
    let theirs: Vec<&str> = vectors["kinds"]
        .as_array()
        .unwrap()
        .iter()
        .map(|kind| kind.as_str().unwrap())
        .collect();
    assert_eq!(kinds, theirs);
    let mut told = NOTIFY.to_vec();
    told.sort_unstable();
    let theirs: Vec<&str> = vectors["notify"]
        .as_array()
        .unwrap()
        .iter()
        .map(|kind| kind.as_str().unwrap())
        .collect();
    assert_eq!(told, theirs);
    assert_eq!(
        QUOTA_UNCONFIRMED_MESSAGE,
        vectors["quota_unconfirmed"].as_str().unwrap()
    );
}
