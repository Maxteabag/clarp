//! What an HTML report is read as, against the Python Host's reading.
//!
//! `fixtures/report-vectors.json` is written by `server-rs/tools/report-vectors.py` from
//! `server/lib/html_forms.py`: lists of origins a page asks to reach, and payloads that are
//! reports, forms or neither, each with what the Python functions said. The same must be
//! said here.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_kernel::artifacts::Refused;
use clarp_kernel::artifacts::reports::{
    connect_origins, content_security_policy, contract, normalized, read_only_schema, rendered,
};
use serde_json::{Value, json};

fn vectors() -> Value {
    serde_json::from_str(include_str!("fixtures/report-vectors.json")).unwrap()
}

/// What the Python Host said: its answer, or the words it refused with.
fn theirs(said: &Value) -> Result<Value, String> {
    match said.get("result") {
        Some(result) => Ok(result.clone()),
        None => Err(said["error"]["message"].as_str().unwrap().to_string()),
    }
}

fn ours<T: Into<Value>>(said: Result<T, Refused>) -> Result<Value, String> {
    said.map(Into::into).map_err(|refused| refused.message)
}

fn shown(value: &Value) -> String {
    value.to_string().chars().take(120).collect()
}

#[test]
fn the_origins_a_page_may_reach_are_read_as_the_python_host_reads_them() {
    let vectors = vectors();
    let origins = vectors["origins"].as_array().unwrap();
    assert!(origins.len() >= 60);
    let mut wrong = Vec::new();
    for vector in origins {
        let here = ours(connect_origins(&vector["given"]).map(|kept| json!(kept)));
        let there = theirs(vector);
        if here != there {
            wrong.push(format!(
                "{}: {here:?}, the Python Host: {there:?}",
                shown(&vector["given"])
            ));
        }
    }
    assert!(wrong.is_empty(), "{}", wrong.join("\n"));
}

#[test]
fn a_page_is_served_as_the_python_host_serves_it() {
    let vectors = vectors();
    assert_eq!(vectors["read_only_schema"], read_only_schema());
    let mut pages = 0;
    for vector in vectors["payloads"].as_array().unwrap() {
        let payload = vector["payload"].as_object().unwrap();
        assert_eq!(
            Ok(json!(content_security_policy(payload))),
            theirs(&vector["policy"]),
            "{}",
            shown(&vector["payload"])
        );
        let Some(page) = vector.get("rendered") else {
            continue;
        };
        let (page_here, policy_here) = rendered(payload, payload["content"].as_str().unwrap());
        assert_eq!(
            Ok(json!({ "page": page_here, "policy": policy_here })),
            theirs(page),
            "{}",
            shown(&vector["payload"])
        );
        pages += 1;
    }
    assert!(pages >= 35, "only {pages} pages were compared");
}

#[test]
fn a_payload_is_a_report_or_a_form_or_is_refused_in_the_python_hosts_words() {
    let vectors = vectors();
    let payloads = vectors["payloads"].as_array().unwrap();
    assert!(payloads.len() >= 45);
    let (mut reports, mut forms, mut wrong) = (0, 0, Vec::new());
    for vector in payloads {
        let what = shown(&vector["payload"]);
        let filled = ours(normalized(&vector["payload"]));
        if filled != theirs(&vector["normalize"]) {
            wrong.push(format!(
                "{what}: filled in as {filled:?}, the Python Host: {:?}",
                theirs(&vector["normalize"])
            ));
            continue;
        }
        let Ok(filled) = filled else {
            continue;
        };
        let here = contract(filled.as_object().unwrap()).map_err(|refused| refused.message);
        let takes_answers = filled.get("read_only") != Some(&json!(true));
        // A report and a form alike: what the Python Host says of each is said here. (The
        // schemas of these forms are all of words this Host reads; `form_vectors.rs` has
        // the ones that are not.)
        let there = theirs(&vector["contract"]).map(|_| ());
        match &here {
            Ok(()) if takes_answers => forms += 1,
            Ok(()) => reports += 1,
            Err(_) => {}
        }
        if here != there {
            wrong.push(format!("{what}: {here:?}, the Python Host: {there:?}"));
        }
    }
    assert!(wrong.is_empty(), "{}", wrong.join("\n"));
    assert!(
        reports >= 10 && forms >= 3,
        "{reports} reports, {forms} forms"
    );
    // What is no payload is left as it is.
    for vector in vectors["others"].as_array().unwrap() {
        assert_eq!(
            ours(normalized(&vector["payload"])),
            theirs(&vector["normalize"])
        );
    }
}
