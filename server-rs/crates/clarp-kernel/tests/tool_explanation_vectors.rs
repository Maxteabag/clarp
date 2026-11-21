//! What is said of a tool's use without asking a model is what the Python Host says of it.
//!
//! `tests/fixtures/tool-explanation-vectors.json` (written by
//! `tools/tool-explanation-vectors.py` out of that Host's own functions) holds 795 uses of
//! tools. Of each: how it is read (the bounded fields), how it is told (the template that
//! fits with what the use names for it, or why none fits, for each of the seventeen reasons
//! there are), the sentence at each of the five levels of detail, and the parts the call
//! has, each with its shape. And, under `helpers`, what the smaller functions answer by
//! themselves.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_kernel::explain::{self, read, shapes};
use serde_json::{Value, json};

const VECTORS: &str = include_str!("fixtures/tool-explanation-vectors.json");

fn vectors() -> Value {
    serde_json::from_str(VECTORS).unwrap()
}

/// Two values are the same, and stand in the same order.
fn same(ours: &Value, theirs: &Value, of: &Value) {
    assert_eq!(ours, theirs, "{of}");
    assert_eq!(
        serde_json::to_string(ours).unwrap(),
        serde_json::to_string(theirs).unwrap(),
        "{of}"
    );
}

#[test]
fn a_use_is_read_as_the_python_host_reads_it() {
    let vectors = vectors();
    let (mut read_of, mut withheld) = (0, 0);
    for row in vectors["uses"].as_array().unwrap() {
        let ours = read::normalize(row["use"].as_object().unwrap());
        same(&Value::Object(ours), &row["read"], &row["use"]);
        read_of += 1;
        let hidden = |value: &Value| value.as_str().is_some_and(|said| said.contains('\u{2026}'));
        withheld += usize::from(hidden(&row["read"]["command"]) || hidden(&row["read"]["summary"]));
    }
    // Counted over the fixture by a script: 795 uses, in 36 of which a heredoc's body is withheld.
    assert_eq!((read_of, withheld), (795, 36));
}

#[test]
fn a_use_is_told_and_spoken_of_as_the_python_host_does() {
    let vectors = vectors();
    let key = vectors["identity_key"].as_str().unwrap();
    assert_eq!(vectors["version"], explain::version());
    assert_eq!(vectors["clip"], explain::LABEL_CLIP);
    let (mut fit, mut none, mut sentences) = (0, 0, 0);
    for row in vectors["uses"].as_array().unwrap() {
        let read = row["read"].as_object().unwrap();
        for level in 0..5u8 {
            let (said, route) = explain::lookup(read, level, key);
            same(&route, &row["route"], &row["use"]);
            let expected = row["said"][usize::from(level)].as_str();
            assert_eq!(said.as_deref(), expected, "{} at level {level}", row["use"]);
            sentences += usize::from(said.is_some());
        }
        if row["route"].get("template_id").is_some() {
            fit += 1;
        } else {
            none += 1;
        }
    }
    // Counted by the generator: a template fits 266 uses and none fits 529; 1062 sentences.
    assert_eq!((fit, none, sentences), (266, 529, 1062));
}

#[test]
fn a_call_has_the_parts_and_the_shapes_the_python_host_gives_it() {
    let vectors = vectors();
    let key = vectors["identity_key"].as_str().unwrap();
    let (mut parts, mut exact, mut slotted, mut placed, mut renamed, mut failed) =
        (0, 0, 0, 0, 0, 0);
    for row in vectors["uses"].as_array().unwrap() {
        let ours = shapes::split(row["read"].as_object().unwrap(), key);
        let Some(theirs) = row.get("parts").and_then(Value::as_array) else {
            // The Python Host's splitter fails on a command that has no word at all (defect
            // R39). This Host tells such a command as one piece.
            assert_eq!(row["parts_fail"], "IndexError", "{}", row["use"]);
            assert_eq!(ours.len(), 1, "{}", row["use"]);
            assert!(
                ours[0].exact && ours[0].reason == "opaque",
                "{}",
                row["use"]
            );
            failed += 1;
            continue;
        };
        assert_eq!(ours.len(), theirs.len(), "{}", row["use"]);
        for (part, theirs) in ours.iter().zip(theirs) {
            let mut written = theirs.as_object().unwrap().clone();
            let asked = written.shift_remove("template_activity").unwrap();
            same(&part.to_json(), &Value::Object(written), &row["use"]);
            same(
                &Value::Object(part.template_activity()),
                &asked,
                &row["use"],
            );
            assert_eq!(
                &shapes::Part::from_json(&part.to_json()),
                part,
                "{}",
                row["use"]
            );
            parts += 1;
            exact += usize::from(part.exact);
            slotted += usize::from(!part.slots.is_empty());
            placed += usize::from(!part.cwd.is_empty());
            renamed += usize::from(!part.legacy.is_empty());
        }
    }
    // Counted by the generator: 825 parts, 128 of them exact, 354 with slots, 34 with a
    // directory, 86 with an earlier name; its splitter fails on 2 uses.
    assert_eq!(
        (parts, exact, slotted, placed, renamed, failed),
        (825, 128, 354, 34, 86, 2)
    );
}

#[test]
fn the_smaller_answers_of_the_second_layer_are_the_python_hosts() {
    let vectors = vectors();
    let key = vectors["identity_key"].as_str().unwrap();
    let cases = |name: &str| vectors["helpers"][name].as_array().unwrap().clone();
    let mut counted = Vec::new();

    let kinds = cases("kinds");
    for case in &kinds {
        assert_eq!(
            shapes::kind(case[0].as_str().unwrap()),
            case[1].as_str(),
            "{case}"
        );
    }
    counted.push(kinds.len());

    let programs = cases("programs");
    for case in &programs {
        assert_eq!(
            shapes::first_program(case[0].as_str().unwrap()),
            case[1],
            "{case}"
        );
    }
    counted.push(programs.len());

    let slots = cases("slots");
    for case in &slots {
        let ours = shapes::slot_values(case[0].as_object().unwrap());
        same(&Value::Object(ours), &case[1], case);
    }
    counted.push(slots.len());

    let rendered = cases("rendered");
    for case in &rendered {
        let ours = shapes::render(case[0].as_str().unwrap(), case[1].as_object().unwrap());
        assert_eq!(ours.as_deref(), case[2].as_str(), "{case}");
    }
    counted.push(rendered.len());

    let parameterised = cases("parameterised");
    for case in &parameterised {
        let slots = case[2].as_object().unwrap();
        let ours = match shapes::parameterise(case[0].as_str().unwrap(), case[1].as_str(), slots) {
            Ok(template) => json!([template, ""]),
            Err(why) => json!([null, why]),
        };
        assert_eq!(ours, case[3], "{case}");
    }
    counted.push(parameterised.len());

    let joined = cases("joined");
    for case in &joined {
        let said: Vec<&str> = case[0]
            .as_array()
            .unwrap()
            .iter()
            .filter_map(Value::as_str)
            .collect();
        assert_eq!(shapes::join(&said), case[1], "{case}");
    }
    counted.push(joined.len());

    let snippets = cases("snippets");
    for case in &snippets {
        let limit = usize::try_from(case[1].as_u64().unwrap()).unwrap();
        assert_eq!(read::snippet(&case[0], limit), case[2], "{case}");
    }
    counted.push(snippets.len());

    let scripts = cases("scripts");
    for case in &scripts {
        let read = read::normalize(case[0].as_object().unwrap());
        let part = shapes::split(&read, key).remove(0);
        let bound = shapes::with_scripts(part, case[1].as_array().unwrap());
        same(&bound.to_json(), &case[2], case);
    }
    counted.push(scripts.len());

    // Counted by the generator, in its order.
    assert_eq!(counted, [71, 39, 15, 165, 27, 19, 25, 12]);
}
