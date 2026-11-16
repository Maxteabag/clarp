//! Who a spoken message is for, against the Python Host's own answers.
//!
//! `fixtures/addressed-vectors.json` is written by `server-rs/tools/addressed-vectors.py`:
//! what the Python Host's functions say of messages and agents made up for it. The same must
//! be said here, with one difference made on purpose: the word a name was found in is taken
//! off whole, and the test names the messages where that shows. (The Python Host's defect
//! with a name in letters English has not, R23, and what its first fix broke, R27, are
//! fixed there since 16 Nov 2026, and the vectors were written again from each fix.)

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
            // The messages this Host answers otherwise, on purpose: a word is taken off
            // whole or not at all.
            match WHOLE_WORDS.iter().find(|(said, _, _)| *said == text) {
                Some((_, here, there)) => {
                    whole_words += 1;
                    assert_eq!((rest.as_str(), theirs), (*here, *there), "{text:?}");
                }
                None => assert_eq!(rest, theirs, "{text:?}"),
            }
        }
    }
    assert!(
        messages >= 130 && for_an_agent >= 100,
        "{messages}, {for_an_agent}"
    );
    assert_eq!(whole_words, WHOLE_WORDS.len());
}

/// What is said, what this Host gives the agent of it, and what the Python Host gives it.
/// In each the word the name was found in does not end where the name does: it spells the
/// name another way than the agent has it, or goes on in a letter, a digit, a mark or a
/// sign that is no punctuation. The Python Host cuts after the letters of English it knows
/// and leaves the agent the end of the word; this Host takes the word off whole.
const WHOLE_WORDS: [(&str, &str, &str); 10] = [
    ("Rach\u{e9}l go", "go", "\u{e9}l go"),
    ("Zo\u{e9} go", "go", "\u{e9} go"),
    ("Zo\u{eb}y go", "go", "\u{eb}y go"),
    ("Rache\u{301}l go", "go", "\u{301}l go"),
    ("Zo\u{eb}1 go", "go", "\u{eb}1 go"),
    ("Zo\u{eb}\u{301} go", "go", "\u{eb}\u{301} go"),
    ("Zo\u{eb}\u{1f642}go", "\u{1f642}go", "\u{eb}\u{1f642}go"),
    ("Zo\u{eb}\u{200d}go", "\u{200d}go", "\u{eb}\u{200d}go"),
    ("Zo\u{eb}+go", "+go", "\u{eb}+go"),
    ("Zo\u{eb}$go", "$go", "\u{eb}$go"),
];

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
