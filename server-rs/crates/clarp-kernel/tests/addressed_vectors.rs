//! Who a spoken message is for, against the Python Host's own answers.
//!
//! `fixtures/addressed-vectors.json` is written by `server-rs/tools/addressed-vectors.py`:
//! what the Python Host's functions say of messages and agents made up for it. The same must
//! be said here, with one difference made on purpose: a name's word is taken off whole
//! (reference defect R23), and the test says where that shows.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_kernel::addressed::{DEFAULT_SESSION, FILLERS, WORDS, alike, named, target};
use serde_json::Value;

fn vectors() -> Value {
    serde_json::from_str(include_str!("fixtures/addressed-vectors.json")).unwrap()
}

fn agents(room: &Value) -> Vec<(String, String)> {
    let said = |value: &Value| value.as_str().unwrap().to_string();
    let rows = room.as_array().unwrap().iter();
    rows.map(|row| (said(&row[0]), said(&row[1]))).collect()
}

#[test]
fn the_words_before_a_name_are_the_python_hosts() {
    let vectors = vectors();
    let theirs: Vec<&str> = vectors["fillers"]
        .as_array()
        .unwrap()
        .iter()
        .map(|word| word.as_str().unwrap())
        .collect();
    assert_eq!(FILLERS.to_vec(), theirs);
    assert_eq!(vectors["words"], WORDS);
    assert_eq!(DEFAULT_SESSION, "claude");
}

#[test]
fn two_words_are_as_alike_as_the_python_host_finds_them() {
    let vectors = vectors();
    let pairs = vectors["alike"].as_array().unwrap();
    assert!(pairs.len() >= 30);
    for pair in pairs {
        let (a, b) = (pair["a"].as_str().unwrap(), pair["b"].as_str().unwrap());
        assert_eq!(
            alike(a, b),
            pair["alike"].as_f64().unwrap(),
            "{a:?} and {b:?}"
        );
    }
}

#[test]
fn a_message_is_for_the_agent_the_python_host_finds_named_in_it() {
    let vectors = vectors();
    let (mut messages, mut for_an_agent, mut whole_words) = (0, 0, 0);
    for room in vectors["rooms"].as_array().unwrap() {
        let agents = agents(&room["agents"]);
        let agents: Vec<(&str, &str)> = agents
            .iter()
            .map(|(session, name)| (session.as_str(), name.as_str()))
            .collect();
        for row in room["said"].as_array().unwrap() {
            let text = row["text"].as_str().unwrap();
            let theirs = row["rest"].as_str().unwrap();
            messages += 1;
            let found = named(text, &agents);
            assert_eq!(
                found.as_ref().map(|(session, _)| *session),
                row["session"].as_str(),
                "{text:?}"
            );
            let Some((_, rest)) = found else {
                assert_eq!(
                    theirs, text,
                    "a message for nobody by name is left as it is"
                );
                continue;
            };
            for_an_agent += 1;
            if rest == theirs {
                continue;
            }
            // The one difference: the Python Host left the end of the name's word, in
            // letters it does not know, at the head of what the agent is given.
            whole_words += 1;
            let tail: String = theirs
                .chars()
                .take_while(|sign| !sign.is_ascii() && sign.is_alphanumeric())
                .collect();
            assert!(
                !tail.is_empty(),
                "{text:?}: {rest:?} here, {theirs:?} there"
            );
            let without =
                theirs[tail.len()..].trim_start_matches(|sign: char| sign.is_alphanumeric());
            assert_eq!(rest, without.trim(), "{text:?}");
        }
    }
    assert!(
        messages >= 90 && for_an_agent >= 60,
        "{messages}, {for_an_agent}"
    );
    assert_eq!(
        whole_words, 3,
        "the messages whose name ends in a letter English has not"
    );
}

#[test]
fn a_message_goes_where_the_python_host_sends_it() {
    let vectors = vectors();
    let agents = agents(&vectors["rooms"][0]["agents"]);
    let agents: Vec<(&str, &str)> = agents
        .iter()
        .map(|(session, name)| (session.as_str(), name.as_str()))
        .collect();
    let targets = vectors["targets"].as_array().unwrap();
    assert!(targets.len() >= 14);
    let (mut by_name, mut to_the_default) = (0, 0);
    for case in targets {
        let said = |key: &str| case[key].as_str().unwrap();
        let found = target(
            said("text"),
            (said("requested"), said("sticky"), said("default")),
            &agents,
        );
        assert_eq!(found.session, said("session"), "{case}");
        assert_eq!(found.text, said("given"), "{case}");
        assert_eq!(found.by_name, case["by_name"].as_bool().unwrap(), "{case}");
        by_name += usize::from(found.by_name);
        to_the_default += usize::from(found.session == said("default"));
    }
    assert!(
        by_name >= 3 && to_the_default >= 4,
        "{by_name}, {to_the_default}"
    );
}
