//! What is kept of a failed routing CLI's words: the Python Host's own answers since its PR
//! 445, R85 (`tools/routing-failure-vectors.py`).
#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_kernel::failures::{self, CliFailure};
use serde_json::Value;
use unicode_normalization::UnicodeNormalization;

fn vectors() -> Value {
    serde_json::from_str(include_str!("fixtures/routing-failure-vectors.json")).unwrap()
}

#[test]
fn a_failed_clis_words_are_kept_only_where_they_hold_none_of_the_request() {
    let said = vectors();
    assert_eq!(said["echo_run"], failures::ECHO_RUN);
    assert_eq!(said["kept_most"], failures::KEPT_MOST);
    let mut counted = [0usize; 4];
    for case in said["echoes"].as_array().unwrap() {
        let (text, prompt) = (
            case["text"].as_str().unwrap(),
            case["prompt"].as_str().unwrap(),
        );
        assert_eq!(
            failures::echoes(text, prompt),
            case["echoes"].as_bool().unwrap(),
            "{case}"
        );
        counted[0] += 1;
    }
    for case in said["kept"].as_array().unwrap() {
        let (words, prompt) = (
            case["words"].as_str().unwrap(),
            case["prompt"].as_str().unwrap(),
        );
        assert_eq!(
            failures::kept_line(words, prompt),
            case["line"].as_str().unwrap(),
            "{case}"
        );
        counted[1] += 1;
    }
    let prompt =
        "Route this: please book a table for four at the harbour restaurant tonight, thank you.";
    for case in said["failures"].as_array().unwrap() {
        let backend = case["backend"].as_str().unwrap();
        let ours = failures::cli_failure(
            backend,
            case["output"].as_str().unwrap(),
            prompt,
            case["returncode"].as_i64(),
        );
        assert_eq!(ours.line, case["line"].as_str().unwrap(), "{case}");
        assert_eq!(
            ours.moves_on,
            case["provider_failure"].as_bool().unwrap(),
            "{case}"
        );
        match case["kind"].as_str().unwrap() {
            "Cancelled" => assert_eq!(ours.category, failures::INTERRUPTED, "{case}"),
            _ => {
                assert_eq!(ours.category, case["category"].as_str().unwrap(), "{case}");
                assert_eq!(ours.denied, case["denied"].as_bool().unwrap(), "{case}");
                let theirs = case["error_code"].as_str().unwrap();
                let code = failures::keeper_code(&ours);
                assert_eq!(
                    code.as_deref().unwrap_or("ProviderFailure"),
                    theirs,
                    "{case}"
                );
            }
        }
        counted[2] += 1;
    }
    for case in said["codes"].as_array().unwrap() {
        let failure = CliFailure {
            line: case["line"].as_str().unwrap().to_string(),
            category: if case["category"] == "timeout" {
                failures::TIMEOUT
            } else {
                failures::RUNNER_EXIT
            },
            denied: false,
            backend: String::new(),
            status: None,
            moves_on: true,
        };
        let code = failures::keeper_code(&failure);
        assert_eq!(
            code.as_deref().unwrap_or("ProviderFailure"),
            case["error_code"].as_str().unwrap(),
            "{case}"
        );
        counted[3] += 1;
    }
    assert_eq!(counted, [124, 8, 180, 4]);
}

#[test]
fn a_sign_the_reference_has_not_assigned_is_left_as_it_is_written() {
    // The reference's Unicode is 15.1.0; this Host's tables are later. An outlined capital A of
    // Unicode 16 is folded to "A" by the later tables, and left as it is by the reference: here
    // too.
    assert_eq!(vectors()["unicode"], "15.1.0");
    let outlined = '\u{1CCD6}';
    assert!(failures::unassigned_in_reference(outlined));
    assert_eq!(
        outlined.to_string().nfkc().collect::<String>(),
        "A",
        "the later tables fold it"
    );
    assert_eq!(failures::nfkc(&outlined.to_string()), outlined.to_string());
    // What the reference has assigned is normalised as ever, also around such a sign.
    assert_eq!(
        failures::nfkc("\u{FB01}x\u{1CCD6}\u{FF21}"),
        "fix\u{1CCD6}A"
    );
    assert!(!failures::unassigned_in_reference('A'));
    assert!(failures::unassigned_in_reference('\u{FFFF}'));
}
