//! The personas a Host has built in, against the Python Host's.
//!
//! `fixtures/persona-vectors.json` is written by `server-rs/tools/persona-vectors.py`: what the
//! Python Host lists as its personas under several made-up owner's files, and which kind of
//! tool and which way of speaking it gives a name. The same must be listed here; only the
//! order is this Host's own (the roster's), and is checked as that.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_kernel::personas::{Owner, archetype, builtins, personality, tier};
use serde_json::Value;

fn vectors() -> Value {
    serde_json::from_str(include_str!("fixtures/persona-vectors.json")).unwrap()
}

fn table(value: &Value) -> Vec<(String, String)> {
    let pairs = value.as_array().unwrap().iter();
    pairs
        .map(|pair| {
            (
                pair[0].as_str().unwrap().to_string(),
                pair[1].as_str().unwrap().to_string(),
            )
        })
        .collect()
}

#[test]
fn a_name_has_the_kind_of_tool_and_the_way_of_speaking_the_python_host_gives_it() {
    let vectors = vectors();
    let asked = vectors["asked"].as_array().unwrap();
    assert!(asked.len() >= 20);
    for case in asked {
        let name = case["name"].as_str().unwrap();
        assert_eq!(
            tier(name),
            case["tier"].as_str().unwrap_or(""),
            "the kind of {name:?}"
        );
        assert_eq!(
            personality(name),
            case["personality"].as_str().unwrap(),
            "how {name:?} speaks"
        );
    }
    assert_eq!(archetype("Claude-5628"), "Claude");
    assert_eq!(archetype("mike-zzzz"), "mike-zzzz");
    assert_eq!(archetype("Mike"), "Mike");
    assert_eq!(archetype("a-b-00ff"), "a-b");
}

#[test]
fn the_personas_built_in_are_the_python_hosts_under_every_owners_file() {
    let vectors = vectors();
    let cases = vectors["cases"].as_array().unwrap();
    assert!(cases.len() >= 3);
    for case in cases {
        let said = case["case"]["name"].as_str().unwrap();
        let owner = &case["case"]["owner"];
        let (roster, cartesia, gemini) = (
            table(&owner["roster"]),
            table(&owner["cartesia"]),
            table(&owner["gemini"]),
        );
        let listed = builtins(&Owner {
            roster: &roster,
            cartesia: &cartesia,
            gemini: &gemini,
        });
        let expected = case["personas"].as_array().unwrap();
        assert!(expected.len() >= 83, "{said}");
        assert_eq!(listed.len(), expected.len(), "{said}: how many");
        // Each persona, by its id: every field as the Python Host says it.
        for persona in expected {
            let id = persona["id"].as_str().unwrap();
            let here = listed.iter().find(|here| here["id"] == id);
            assert_eq!(here, Some(persona), "{said}: {id}");
        }
        // In the roster's order, the owner's own names after it.
        let names: Vec<&str> = listed
            .iter()
            .map(|persona| persona["name"].as_str().unwrap())
            .collect();
        assert_eq!(&names[..3], ["Mike", "Rachel", "Domi"], "{said}");
        let own: Vec<&str> = roster
            .iter()
            .map(|(name, _)| name.as_str())
            .filter(|name| *name != "Mike")
            .collect();
        assert_eq!(
            &names[names.len() - own.len()..],
            own.as_slice(),
            "{said}: the owner's own names last"
        );
    }
}
