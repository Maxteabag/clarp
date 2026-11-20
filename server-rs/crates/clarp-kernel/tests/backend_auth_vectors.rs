//! A tool's sign-in from an app, against the Python Host's own answers.
//!
//! `fixtures/backend-auth-vectors.json` is written by `server-rs/tools/backend-auth-vectors.py`:
//! what `server/lib/backend_auth.py` makes of texts made up for it (what a tool printed, what
//! an app is shown of it, why it failed), which tools it signs in and how, and what comes of
//! a code pasted back in every state a sign-in can be in. The same must be said here.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_kernel::sign_in::{
    self, CodeConflict, KEPT, LONGEST_CODE, SHOWN, State, TIMEOUT_MS, TOOLS,
};
use serde_json::{Value, json};

fn vectors() -> Value {
    serde_json::from_str(include_str!("fixtures/backend-auth-vectors.json")).unwrap()
}

fn text(value: &Value) -> &str {
    value.as_str().unwrap()
}

fn state(name: &str) -> State {
    match name {
        "idle" => State::Idle,
        "running" => State::Running,
        "complete" => State::Complete,
        "failed" => State::Failed,
        other => panic!("no state {other}"),
    }
}

#[test]
fn the_bounds_are_the_python_hosts() {
    let limits = &vectors()["limits"];
    assert_eq!(
        limits["timeout_seconds"].as_i64().unwrap() * 1000,
        TIMEOUT_MS
    );
    assert_eq!(limits["raw_output"].as_u64().unwrap() as usize, KEPT);
    assert_eq!(limits["code"].as_u64().unwrap() as usize, LONGEST_CODE);
    assert_eq!(SHOWN, 8000);
}

#[test]
fn what_a_tool_printed_is_shown_and_summed_up_as_there() {
    let vectors = vectors();
    let mut count = 0;
    for case in vectors["texts"].as_array().unwrap() {
        let raw = text(&case["raw"]);
        assert_eq!(sign_in::clean(raw), text(&case["clean"]), "clean {raw:?}");
        assert_eq!(
            sign_in::failure(raw),
            text(&case["summary"]),
            "summary {raw:?}"
        );
        count += 1;
    }
    assert_eq!(count, 108);
}

#[test]
fn the_newest_of_what_was_printed_is_kept() {
    let vectors = vectors();
    let mut count = 0;
    for case in vectors["appended"].as_array().unwrap() {
        let before = case["before"].as_u64().unwrap() as usize;
        let first = text(&case["first"]);
        let mut kept = first.repeat(before);
        for sign in text(&case["more"]).chars() {
            sign_in::append(&mut kept, &sign.to_string());
        }
        let signs: Vec<char> = kept.chars().collect();
        assert_eq!(signs.len() as u64, case["kept"].as_u64().unwrap(), "{case}");
        let head: String = signs[..3.min(signs.len())].iter().collect();
        let tail: String = signs[signs.len().saturating_sub(3)..].iter().collect();
        assert_eq!(
            (head.as_str(), tail.as_str()),
            (text(&case["head"]), text(&case["tail"]))
        );
        count += 1;
    }
    assert_eq!(count, 6);
}

#[test]
fn a_sign_in_is_told_as_there() {
    let vectors = vectors();
    let now = vectors["now"].as_i64().unwrap();
    let mut count = 0;
    for case in vectors["tasks"].as_array().unwrap() {
        let told = sign_in::task(
            text(&case["backend"]),
            state(text(&case["state"])),
            text(&case["raw"]),
            case["started_at"].as_i64().unwrap(),
            now,
            text(&case["error"]),
            text(&case["submitted"]),
        );
        assert_eq!(told, case["task"], "{}", case["raw"]);
        count += 1;
    }
    assert_eq!(count, 855);
}

#[test]
fn the_tools_signed_in_from_an_app_and_how_are_the_python_hosts() {
    let vectors = vectors();
    let mut count = 0;
    for case in vectors["names"].as_array().unwrap() {
        let name = text(&case["name"]);
        let driver = sign_in::driver(name);
        assert_eq!(driver.is_some(), case["driver"] == true, "{name}");
        if let Some(driver) = driver {
            assert_eq!(json!(driver.login), case["login"], "{name}");
            assert_eq!(json!(driver.logout), case["logout"], "{name}");
            assert_eq!(json!(driver.listed_by_default), case["listed"], "{name}");
        }
        count += 1;
    }
    assert_eq!(count, 15);
    let theirs: Vec<&str> = vectors["order"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|row| row["supports_auth"] == true)
        .map(|row| text(&row["id"]))
        .collect();
    assert_eq!(theirs, TOOLS);
}

/// `submit_login_code`, step by step: the tool, the code, then the sign-in it is for. What
/// the Host does with the tool's input (whether it is still open, and whether writing to it
/// worked) is said by the case and checked in the order the Python Host checks it.
#[test]
fn a_code_pasted_back_goes_or_is_refused_as_there() {
    let vectors = vectors();
    let now = vectors["now"].as_i64().unwrap();
    let (mut sent, mut refused, mut conflicts) = (0, 0, 0);
    for case in vectors["codes"].as_array().unwrap() {
        let tool = text(&case["backend"]);
        let outcome = (|| -> Result<(String, Value), (u16, String)> {
            if sign_in::driver(tool).is_none() {
                return Err((400, sign_in::not_installed(tool)));
            }
            let code = sign_in::code(text(&case["code"])).map_err(|why| (400, why.to_string()))?;
            let (status, output) = match text(&case["state"]) {
                "none" => ("", ""),
                "running-asking" => ("running", "Paste code here if prompted > "),
                "running-not-asking" => ("running", "Opening browser"),
                "complete-asking" => ("complete", "Paste code here if prompted > "),
                "failed" => ("failed", "Error"),
                other => panic!("no state {other}"),
            };
            let told = (!status.is_empty())
                .then(|| sign_in::task(tool, state(status), output, now - 1000, now, "", ""));
            let stdin = text(&case["stdin"]);
            sign_in::code_can_go(told.as_ref(), stdin != "no-process")
                .map_err(|conflict| (409, conflict.words()))?;
            let conflict = match stdin {
                "no-stdin" | "closed" => Some(CodeConflict::NotAccepting),
                "fails" => Some(CodeConflict::NotSent("the pipe is gone".into())),
                _ => None,
            };
            if let Some(conflict) = conflict {
                return Err((409, conflict.words()));
            }
            let told = told.unwrap();
            let shown = text(&told["output"]);
            let after = sign_in::task(tool, State::Running, shown, now - 1000, now, "", shown);
            Ok((format!("{code}\n"), after))
        })();
        match (outcome, text(&case["outcome"])) {
            (Ok((written, after)), "sent") => {
                assert_eq!(written, text(&case["written"]), "{case}");
                assert_eq!(after, case["task"], "{case}");
                assert_eq!(after["code_submitted"], true);
                sent += 1;
            }
            (Err((400, why)), "refused") => {
                assert_eq!(why, text(&case["error"]), "{case}");
                refused += 1;
            }
            (Err((409, why)), "conflict") => {
                assert_eq!(why, text(&case["error"]), "{case}");
                conflicts += 1;
            }
            (got, theirs) => panic!("{case}: {got:?}, the Python Host: {theirs}"),
        }
    }
    assert_eq!((sent, refused, conflicts), (10, 56, 72));
}

#[test]
fn a_field_and_a_tool_are_read_as_the_python_host_reads_them() {
    let body = |value: Value| value.as_object().unwrap().clone();
    for (said, read) in [
        (json!({ "backend": " Claude " }), "claude"),
        (json!({ "backend": "CODEX" }), "codex"),
        (json!({}), ""),
        (json!({ "backend": null }), ""),
        (json!({ "backend": false }), ""),
        (json!({ "backend": 0 }), ""),
        (json!({ "backend": [] }), ""),
        (json!({ "backend": 7 }), "7"),
        (json!({ "backend": true }), "true"),
        (json!({ "backend": ["Grok"] }), "['grok']"),
    ] {
        assert_eq!(sign_in::tool_named(&body(said.clone())), read, "{said}");
    }
    assert_eq!(sign_in::field(Some(&json!(1.5))), "1.5");
    assert_eq!(sign_in::field(None), "");
}
