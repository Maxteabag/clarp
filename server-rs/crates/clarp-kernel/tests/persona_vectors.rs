//! The personas a Host has built in, against the Python Host's.
//!
//! `fixtures/persona-vectors.json` is written by `server-rs/tools/persona-vectors.py`: what the
//! Python Host lists as its personas under several made-up owner's files, and which kind of
//! tool and which way of speaking it gives a name. The same must be listed here; only the
//! order is this Host's own (the roster's), and is checked as that.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::collections::HashSet;

use clarp_kernel::personas::{
    Book, Owner, Wanted, archetype, builtins, candidate_voices, personality, picked_voice, tier,
};
use serde_json::{Value, json};

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

#[test]
fn the_owner_makes_changes_and_removes_personas_as_on_the_python_host() {
    let vectors = vectors();
    let rooms = vectors["own"].as_array().unwrap();
    assert!(rooms.len() >= 2);
    let (mut made, mut refused_steps, mut pictures) = (0, 0, 0);
    for room in rooms {
        let room_name = room["room"]["name"].as_str().unwrap();
        let file = &room["room"]["owner"];
        let (roster, cartesia, gemini) = (
            table(&file["roster"]),
            table(&file["cartesia"]),
            table(&file["gemini"]),
        );
        let owner = Owner {
            roster: &roster,
            cartesia: &cartesia,
            gemini: &gemini,
        };
        // Nothing was fetched from Cartesia in these rooms, and no agent is there.
        let candidates = candidate_voices(&[], &owner);
        let nobody = HashSet::new();
        let mut book = Book::default();
        let mut voice = picked_voice(&candidates, &nobody, &book.voices(&owner));
        for (at, step) in room["steps"].as_array().unwrap().iter().enumerate() {
            let (what, given) = step["step"].as_object().unwrap().iter().next().unwrap();
            let said = |key: &str| given[key].as_str().unwrap_or("");
            let answered = &step["answered"];
            let refused = step["refused"].as_str();
            let wanted = Wanted {
                name: said("name"),
                voice_id: said("voice_id"),
                avatar_symbol: said("avatar_symbol"),
                personality: said("personality"),
                picture: said("avatar_base64"),
            };
            let about = format!("{room_name}, step {at}: {what} {given}");
            let changed = match what.as_str() {
                // The id is the Python Host's own draw: taken from what it answered.
                "create" => {
                    let id = answered["id"].as_str().unwrap_or("persona-never-made");
                    let now = i64::try_from(at).unwrap();
                    Some(book.create(&owner, &wanted, (id, &voice), now))
                }
                "update" => Some(book.update(&owner, said("original_name"), &wanted, &voice)),
                _ => None,
            };
            match changed {
                Some(Ok(change)) => {
                    assert_eq!(refused, None, "{about}");
                    assert_eq!(book.told(&owner, &change.kept), *answered, "{about}");
                    assert_eq!(
                        change.picture.is_some(),
                        !wanted.picture.is_empty(),
                        "{about}"
                    );
                    made += 1;
                    pictures += usize::from(change.picture.is_some());
                }
                Some(Err(why)) => {
                    assert_eq!(Some(why.as_str()), refused, "{about}");
                    refused_steps += 1;
                }
                None => {
                    let removed = book.delete(&owner, given.as_str().unwrap()).is_some();
                    assert_eq!(json!(removed), *answered, "{about}");
                }
            }
            // What the owner made or changed, as the Python Host then lists it. Each is
            // found by its id: the order of the list is each Host's own.
            let listed = book.listed(&owner);
            let touched = |persona: &&Value| {
                persona["builtin"] == false
                    || persona["avatar_symbol"] != ""
                    || persona["avatar_url"] != ""
            };
            let by_id = |persona: &&Value| persona["id"].as_str().unwrap().to_string();
            let mut here: Vec<&Value> = listed.iter().filter(touched).collect();
            let mut there: Vec<&Value> = step["changed"].as_array().unwrap().iter().collect();
            here.sort_by_key(by_id);
            there.sort_by_key(by_id);
            assert_eq!(here, there, "{about}: what is made or changed afterwards");
            // And every one of them is found by its name, whatever the case of its letters.
            for persona in &here {
                let name = persona["name"].as_str().unwrap();
                assert_eq!(
                    book.named(&owner, &name.to_ascii_uppercase()).as_ref(),
                    Some(*persona)
                );
            }
            // The voice the next persona would be given.
            voice = picked_voice(&candidates, &nobody, &book.voices(&owner));
            assert_eq!(json!(voice), step["next_voice"], "{about}: the next voice");
        }
    }
    assert!(
        made >= 12 && refused_steps >= 7 && pictures >= 3,
        "{made}, {refused_steps}, {pictures}"
    );
}
