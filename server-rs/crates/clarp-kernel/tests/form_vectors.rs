//! What a form's answers may be, against the Python Host's reading.
//!
//! `fixtures/form-vectors.json` is written by `server-rs/tools/form-vectors.py` from
//! `server/lib/html_forms.py` and the library it asks (jsonschema, draft 2020-12): values as
//! Python shows and writes them, payloads of forms with what was said of each, and schemas
//! with answers and the first thing the library found wrong with them. The same must be
//! said here.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_kernel::artifacts::answers::{check, not_read, unfit};
use clarp_kernel::artifacts::reports::contract;
use clarp_kernel::python::{repr, sorted_ascii};
use serde_json::Value;

fn vectors() -> Value {
    serde_json::from_str(include_str!("fixtures/form-vectors.json")).unwrap()
}

fn shown(value: &Value) -> String {
    value.to_string().chars().take(160).collect()
}

#[test]
fn a_value_is_shown_and_written_as_python_shows_and_writes_it() {
    let vectors = vectors();
    let mut wrong = Vec::new();
    let reprs = vectors["reprs"].as_array().unwrap();
    for vector in reprs {
        let (here, there) = (repr(&vector["value"]), vector["repr"].as_str().unwrap());
        if here != there {
            wrong.push(format!("repr: {here}, Python: {there}"));
        }
    }
    let encoded = vectors["encoded"].as_array().unwrap();
    for vector in encoded {
        let here = sorted_ascii(&vector["value"]);
        let there = vector["encoded"].as_str().unwrap();
        if here != there {
            wrong.push(format!("written: {here}, Python: {there}"));
        }
    }
    assert!(wrong.is_empty(), "{}", wrong.join("\n"));
    assert!(reprs.len() >= 100 && encoded.len() >= 100);
}

#[test]
fn a_forms_contract_is_judged_as_the_python_host_judges_it() {
    let vectors = vectors();
    let mut wrong = Vec::new();
    let (mut taken, mut refused) = (0, 0);
    let contracts = vectors["contracts"].as_array().unwrap();
    for vector in contracts {
        let here = match vector["payload"].as_object() {
            Some(payload) => contract(payload).map_err(|refused| refused.message),
            None => continue,
        };
        let there = match vector["error"].as_str() {
            Some(why) => Err(why.to_string()),
            None => Ok(()),
        };
        match &here {
            Ok(()) => taken += 1,
            Err(_) => refused += 1,
        }
        if here != there {
            wrong.push(format!(
                "{}: {here:?}, the Python Host: {there:?}",
                shown(&vector["payload"])
            ));
        }
    }
    // A word the library reads and this Host does not: the Python Host takes the form, this
    // Host refuses it and names the word.
    let beyond = vectors["beyond"].as_array().unwrap();
    for vector in beyond {
        let word = vector["word"].as_str().unwrap();
        assert!(vector["error"].is_null(), "the Python Host takes {word}");
        let here = contract(vector["payload"].as_object().unwrap());
        let here = here.map_err(|refused| refused.message);
        if here != Err(not_read(word)) {
            wrong.push(format!("{word}: {here:?}"));
        }
    }
    assert!(wrong.is_empty(), "{}", wrong.join("\n"));
    assert!(
        taken >= 25 && refused >= 40 && beyond.len() >= 20,
        "{taken} taken, {refused} refused, {} beyond",
        beyond.len()
    );
}

#[test]
fn answers_that_do_not_fit_are_told_in_the_librarys_words() {
    let vectors = vectors();
    let mut wrong = Vec::new();
    let (mut fit, mut do_not) = (0, 0);
    let answers = vectors["answers"].as_array().unwrap();
    for vector in answers {
        let (schema, instance) = (&vector["schema"], &vector["instance"]);
        if let Err(refused) = check(schema) {
            wrong.push(format!("{}: not read: {}", shown(schema), refused.message));
            continue;
        }
        let here = unfit(schema, instance);
        let there = vector["error"].as_str().map(str::to_string);
        match &here {
            None => fit += 1,
            Some(_) => do_not += 1,
        }
        if here != there {
            wrong.push(format!(
                "{} of {}: {here:?}, the library: {there:?}",
                shown(schema),
                shown(instance)
            ));
        }
    }
    let first: Vec<&String> = wrong.iter().take(40).collect();
    assert!(wrong.is_empty(), "{} differ:\n{first:#?}", wrong.len());
    assert!(fit >= 250 && do_not >= 1_000, "{fit} fit, {do_not} do not");
}
