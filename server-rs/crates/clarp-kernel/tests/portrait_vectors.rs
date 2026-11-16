//! The face an agent is shown with, against the Python Host's.
//!
//! `fixtures/portrait-vectors.json` is written by `server-rs/tools/portrait-vectors.py`: for a
//! made-up directory of pictures, which picture the Python Host shows an agent with by its
//! name, its tool and its model, and the address it gives the picture. The same must be
//! chosen here.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_kernel::portraits::{
    AVATARS, MODEL_AVATARS, address, families, family_of, family_of_tool, files, model_files, slug,
};
use serde_json::Value;

fn vectors() -> Value {
    serde_json::from_str(include_str!("fixtures/portrait-vectors.json")).unwrap()
}

fn text<'a>(case: &'a Value, name: &str) -> &'a str {
    case[name]
        .as_str()
        .unwrap_or_else(|| panic!("no {name} in {case}"))
}

#[test]
fn a_name_a_model_and_a_tool_are_read_as_the_python_host_reads_them() {
    let vectors = vectors();
    for case in vectors["slugs"].as_array().unwrap() {
        let name = text(case, "name");
        assert_eq!(slug(name), text(case, "slug"), "{name:?}");
        assert_eq!(
            slug(name),
            text(case, "model_slug"),
            "{name:?} for a model's picture"
        );
    }
    for case in vectors["families"].as_array().unwrap() {
        assert_eq!(
            family_of(text(case, "model")),
            text(case, "family"),
            "{case}"
        );
    }
    for case in vectors["tools"].as_array().unwrap() {
        assert_eq!(
            family_of_tool(text(case, "backend")),
            text(case, "family"),
            "{case}"
        );
    }
    assert_eq!(
        families("agy", "claude-opus-4-6", "gemini-3-pro"),
        ["opus", "gemini"]
    );
}

#[test]
fn an_agent_is_shown_with_the_picture_the_python_host_shows_it_with() {
    let vectors = vectors();
    let shipped = vectors["shipped"].as_object().unwrap();
    let faces = vectors["faces"].as_array().unwrap();
    assert!(faces.len() >= 300);
    let mut shown = 0;
    for case in faces {
        let (persona, backend) = (text(case, "persona"), text(case, "backend"));
        let found = files(persona, backend)
            .into_iter()
            .find_map(|file| shipped.get(&file).map(|version| (file, version)));
        let url = match found {
            Some((file, version)) => address(AVATARS, &file, version.as_str().unwrap()),
            None => String::new(),
        };
        assert_eq!(url, text(case, "url"), "{persona:?} on {backend:?}");
        shown += usize::from(!url.is_empty());
    }
    assert!(shown >= 200, "{shown} have a picture");

    let on_models = vectors["on_models"].as_array().unwrap();
    assert!(on_models.len() >= 1_000);
    let mut drawn = 0;
    for case in on_models {
        let (persona, backend) = (text(case, "persona"), text(case, "backend"));
        let (model, default) = (text(case, "model"), text(case, "default_model"));
        let found = model_files(persona, backend, model, default)
            .into_iter()
            .find_map(|file| {
                shipped
                    .get(&format!("models/{file}"))
                    .map(|version| (file, version))
            });
        // The name of such a file is made of a slug and a family: nothing in it needs quoting.
        let url = match found {
            Some((file, version)) => {
                format!("{MODEL_AVATARS}/{file}?v={}", version.as_str().unwrap())
            }
            None => String::new(),
        };
        assert_eq!(url, text(case, "url"), "{case}");
        drawn += usize::from(!url.is_empty());
    }
    assert!(drawn >= 200, "{drawn} have a picture for their model");
}

#[test]
fn a_pictures_address_is_written_as_the_python_host_writes_it() {
    let vectors = vectors();
    let version = vectors["shipped"]["claire.png"].as_str().unwrap();
    let addresses = vectors["addresses"].as_array().unwrap();
    assert!(addresses.len() >= 30);
    for case in addresses {
        let url = address(text(case, "prefix"), text(case, "identity"), version);
        assert_eq!(url, text(case, "url"), "{case}");
    }
}
