//! Render specs, against the Python Host's own rules.
//!
//! `fixtures/render-spec-vectors.json` is written by `server-rs/tools/render-spec-vectors.py`
//! from `server/lib/render_spec.py` and `server_identity.render_spec_capable`, run as they
//! are: how a body is read and what its canonical bytes are, whether a payload is a spec,
//! whether answers are taken, what the agent is told, which client may see a spec, which
//! catalog files are loaded, and every case of the reference's own vectors
//! (`tests/fixtures/render_spec/scenarios.py`) played through the rules as its store plays
//! them. The same must come of the same here, byte for byte: a refusal is its code, its
//! pointer and its sentence.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::collections::BTreeMap;

use clarp_kernel::artifacts::render_spec::json::{self, J, Obj, Text};
use clarp_kernel::artifacts::render_spec::{
    self as spec, Admitted, Catalog, Previous, Refusal, Unavailable,
};
use clarp_kernel::python;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

fn vectors() -> Value {
    serde_json::from_str(include_str!("fixtures/render-spec-vectors.json")).unwrap()
}

fn catalog() -> Catalog {
    Catalog::embedded().unwrap()
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

/// A value as the generator describes what Python's reader made.
fn described(value: &J) -> Value {
    match value {
        J::Null => json!(["null"]),
        J::Bool(truth) => json!(["bool", truth]),
        J::Int(number) => json!(["int", number.to_string()]),
        J::Float(_) => json!(["float"]),
        J::Text(Text::Valid(text)) => json!(["text", text]),
        J::Text(Text::Lone(points)) => json!(["lone", points]),
        J::Array(items) => json!(["array", items.iter().map(described).collect::<Vec<_>>()]),
        J::Object(fields) => json!([
            "object",
            fields
                .iter()
                .map(|(key, value)| json!([described(&J::Text(key.clone())), described(value)]))
                .collect::<Vec<_>>(),
            fields.duplicate
        ]),
    }
}

/// A value from what the generator describes.
fn undescribed(value: &Value) -> J {
    let parts = value.as_array().unwrap();
    match parts[0].as_str().unwrap() {
        "null" => J::Null,
        "bool" => J::Bool(parts[1].as_bool().unwrap()),
        "int" => J::Int(parts[1].as_str().unwrap().parse().unwrap()),
        "float" => J::Float(0.5),
        "text" => J::Text(Text::Valid(parts[1].as_str().unwrap().to_string())),
        "array" => J::Array(
            parts[1]
                .as_array()
                .unwrap()
                .iter()
                .map(undescribed)
                .collect(),
        ),
        "object" => {
            let mut object = Obj::new();
            for pair in parts[1].as_array().unwrap() {
                let J::Text(key) = undescribed(&pair[0]) else {
                    panic!("a key that is no text")
                };
                object.insert(key, undescribed(&pair[1]));
            }
            J::Object(object)
        }
        other => panic!("no such description: {other}"),
    }
}

/// Whether a number as this reader holds it is the number Python read: beyond 128 bits it is
/// held at their end.
fn same_number(ours: &Value, theirs: &Value) -> bool {
    if ours == theirs {
        return true;
    }
    let (Some(["int", ours]), Some(["int", theirs])) = (pair(ours), pair(theirs)) else {
        return false;
    };
    theirs.parse::<i128>().is_err()
        && ((theirs.starts_with('-') && ours == i128::MIN.to_string())
            || (!theirs.starts_with('-') && ours == i128::MAX.to_string()))
}

fn pair(value: &Value) -> Option<[&str; 2]> {
    let parts = value.as_array()?;
    Some([parts.first()?.as_str()?, parts.get(1)?.as_str()?])
}

/// Two descriptions the same, where a number beyond 128 bits is held at their end here.
fn same(ours: &Value, theirs: &Value) -> bool {
    match (ours.as_array(), theirs.as_array()) {
        (Some(one), Some(two)) if one.len() == two.len() && one.first() == two.first() => {
            if one.first() == Some(&json!("int")) {
                return same_number(ours, theirs);
            }
            one.iter().zip(two).all(|(a, b)| same(a, b))
        }
        _ => ours == theirs,
    }
}

#[test]
fn a_body_is_read_and_written_canonically_as_the_python_host_does() {
    let (vectors, catalog) = (vectors(), catalog());
    let (mut read, mut not_json, mut written, mut refused) = (0, 0, 0, 0);
    for case in vectors["reads"].as_array().unwrap() {
        let name = case["name"].as_str().unwrap();
        let bytes = python::b64decode(case["bytes"].as_str().unwrap()).unwrap();
        let got = json::read(&bytes);
        let Some(value) = got else {
            assert!(case["read"].is_null(), "{name}: not read here, read there");
            not_json += 1;
            continue;
        };
        read += 1;
        assert!(
            same(&described(&value), &case["read"]),
            "{name}: read here as {}, there as {}",
            described(&value),
            case["read"]
        );
        match json::canonical(&value, "", Some(&catalog.ident)) {
            Ok(canonical) => {
                assert_eq!(hex(canonical.as_bytes()), case["canonical"], "{name}");
                written += 1;
            }
            Err(refusal) => {
                assert_eq!(refusal.triple(Ok(&catalog)), case["refused"], "{name}");
                refused += 1;
            }
        }
    }
    for example in vectors["canonical"].as_array().unwrap() {
        let value = undescribed(&example["value"]);
        let canonical = json::canonical(&value, "", None).unwrap();
        assert_eq!(hex(canonical.as_bytes()), example["utf8_hex"]);
        assert_eq!(
            hex(&Sha256::digest(canonical.as_bytes())),
            example["sha256"]
        );
    }
    assert_eq!((read, written, refused, not_json), (51, 16, 35, 35));
}

#[test]
fn a_payload_is_a_render_spec_or_is_refused_as_the_python_host_does() {
    let (vectors, catalog) = (vectors(), catalog());
    assert_eq!(catalog.sha256, vectors["catalog_sha256"]);
    let (mut valid, mut refused) = (0, 0);
    let mut codes: BTreeMap<String, usize> = BTreeMap::new();
    for case in vectors["payloads"].as_array().unwrap() {
        let name = case["name"].as_str().unwrap();
        let payload = json::read(case["text"].as_str().unwrap().as_bytes()).unwrap();
        match spec::validate(&catalog, &payload) {
            Ok(()) => {
                assert!(
                    case["refused"].is_null(),
                    "{name}: taken here, refused there"
                );
                let stored = spec::stored(&catalog, &payload).unwrap();
                assert_eq!(Value::Object(stored), case["stored"], "{name}");
                valid += 1;
            }
            Err(refusal) => {
                assert_eq!(refusal.triple(Ok(&catalog)), case["refused"], "{name}");
                *codes.entry(refusal.code.to_string()).or_default() += 1;
                refused += 1;
            }
        }
    }
    assert_eq!((valid, refused), (41, 219));
    let by_code: Vec<(&str, usize)> = codes.iter().map(|(code, n)| (code.as_str(), *n)).collect();
    assert_eq!(
        by_code,
        [
            ("catalog_version", 8),
            ("component", 2),
            ("depth", 2),
            ("element_count", 2),
            ("element_id", 12),
            ("element_tree", 9),
            ("expression", 24),
            ("invalid_json", 6),
            ("invalid_object", 16),
            ("invalid_version", 5),
            ("missing_key", 20),
            ("number_form", 14),
            ("prop", 22),
            ("schema_keyword", 3),
            ("schema_shape", 35),
            ("state_value", 8),
            ("submit_intent", 3),
            ("submit_label", 3),
            ("unknown_catalog", 3),
            ("unknown_key", 22),
        ]
    );
}

fn previous_of(case: &Value) -> Option<(String, bool)> {
    let before = case.get("previous").filter(|before| !before.is_null())?;
    Some((
        before["payload_hash"].as_str().unwrap().to_string(),
        before["synthesize_audio"].as_i64().unwrap() != 0,
    ))
}

fn admitted(result: &Admitted) -> Value {
    match result {
        Admitted::Repeat => json!({ "repeat": true }),
        Admitted::New {
            payload_hash,
            answers_json,
            ..
        } => json!({ "repeat": false, "payload_hash": payload_hash, "answers_json": answers_json }),
    }
}

#[test]
fn answers_are_taken_or_refused_as_the_python_host_does() {
    let (vectors, catalog) = (vectors(), catalog());
    let (mut taken, mut again, mut refused) = (0, 0, 0);
    for case in vectors["answers"].as_array().unwrap() {
        let name = case["name"].as_str().unwrap();
        let payload = J::of(&case["payload"]);
        let data = json::read(case["text"].as_str().unwrap().as_bytes()).unwrap();
        let before = previous_of(case);
        let previous = before.as_ref().map(|(hash, audio)| Previous {
            payload_hash: hash,
            synthesize_audio: *audio,
        });
        let artifact_id = case["artifact_id"].as_str().unwrap();
        let status = case["status"].as_str().unwrap();
        match spec::admit(&catalog, artifact_id, &payload, status, &data, previous) {
            Ok(result) => {
                assert_eq!(admitted(&result), case["admitted"], "{name}");
                if result == Admitted::Repeat {
                    again += 1;
                } else {
                    taken += 1;
                }
            }
            Err(refusal) => {
                assert_eq!(refusal.triple(Ok(&catalog)), case["refused"], "{name}");
                refused += 1;
            }
        }
    }
    assert_eq!((taken, again, refused), (9, 5, 57));
}

#[test]
fn the_reference_vectors_play_through_as_the_python_host_plays_them() {
    let (vectors, catalog) = (vectors(), catalog());
    let mut steps_played = 0;
    for flow in vectors["flows"].as_array().unwrap() {
        let name = flow["name"].as_str().unwrap();
        let (artifact_id, title) = (
            flow["artifact_id"].as_str().unwrap(),
            flow["title"].as_str().unwrap(),
        );
        let mut stored: Option<J> = None;
        let mut status = String::new();
        let mut kept: BTreeMap<String, (String, bool)> = BTreeMap::new();
        for step in flow["steps"].as_array().unwrap() {
            steps_played += 1;
            let data = json::read(step["text"].as_str().unwrap().as_bytes()).unwrap();
            if step["action"] == "submit" {
                let payload = stored.clone().unwrap_or(J::Null);
                let sid = data
                    .get("submission_id")
                    .and_then(J::str)
                    .unwrap_or("")
                    .to_string();
                let before = kept.get(&sid).cloned();
                let previous = before.as_ref().map(|(hash, audio)| Previous {
                    payload_hash: hash,
                    synthesize_audio: *audio,
                });
                match spec::admit(&catalog, artifact_id, &payload, &status, &data, previous) {
                    Ok(result) => {
                        assert_eq!(admitted(&result), step["admitted"], "{name}");
                        if let Admitted::New {
                            payload_hash,
                            answers_json,
                            synthesize_audio,
                        } = result
                        {
                            assert_eq!(step["expected_payload_hash"], payload_hash, "{name}");
                            let version = data.get("version").and_then(J::str).unwrap();
                            let told = spec::message_text(
                                title,
                                artifact_id,
                                version,
                                &sid,
                                &answers_json,
                            );
                            assert_eq!(step["message"], told, "{name}");
                            assert_eq!(step["expected_prompt"], told, "{name}");
                            kept.insert(sid, (payload_hash, synthesize_audio));
                            status = "completed".to_string();
                        }
                    }
                    Err(refusal) => {
                        let triple = refusal.triple(Ok(&catalog));
                        assert_eq!(triple, step["refused"], "{name}");
                        assert_eq!(triple, step["expected_error"], "{name}");
                    }
                }
            } else {
                match spec::validate(&catalog, &data) {
                    Ok(()) => {
                        let spec_of = data.get("spec").unwrap();
                        let version = data.get("version").and_then(J::str).unwrap();
                        let identity = spec::content_identity(&catalog, spec_of, version).unwrap();
                        assert_eq!(identity, step["identity"], "{name}");
                        if !step["expected_identity"].is_null() {
                            assert_eq!(identity, step["expected_identity"], "{name}");
                        }
                        if step["action"] == "publish" {
                            stored = Some(J::of(&Value::Object(
                                spec::stored(&catalog, &data).unwrap(),
                            )));
                            status = "ready".to_string();
                        }
                    }
                    Err(refusal) => {
                        let triple = refusal.triple(Ok(&catalog));
                        assert_eq!(triple, step["refused"], "{name}");
                        assert_eq!(triple, step["expected_error"], "{name}");
                    }
                }
            }
            let expected = step["stored_status"].as_str().unwrap_or("");
            assert_eq!(status, expected, "{name}");
        }
    }
    assert_eq!(
        (vectors["flows"].as_array().unwrap().len(), steps_played),
        (35, 56)
    );
}

#[test]
fn the_agent_is_told_the_answers_as_the_python_host_tells_them() {
    let vectors = vectors();
    let mut told = 0;
    for case in vectors["messages"].as_array().unwrap() {
        let (artifact, submission) = (&case["artifact"], &case["submission"]);
        let answers = json::canonical(&J::of(&submission["answers"]), "", None).unwrap();
        let text = spec::message_text(
            artifact["title"].as_str().unwrap(),
            artifact["artifact_id"].as_str().unwrap(),
            submission["version"].as_str().unwrap(),
            submission["submission_id"].as_str().unwrap(),
            &answers,
        );
        assert_eq!(text, case["text"]);
        told += 1;
    }
    assert_eq!(told, 10);
}

#[test]
fn a_client_may_see_a_spec_as_the_python_host_says() {
    let vectors = vectors();
    let (mut may, mut may_not) = (0, 0);
    for case in vectors["clients"].as_array().unwrap() {
        let header = case["header"].as_str();
        let minimum = case["minimum"].as_i64();
        let capable = spec::capable(header, minimum);
        assert_eq!(capable, case["capable"], "{header:?} at {minimum:?}");
        if let Some(wire) = case.get("wire") {
            assert_eq!(capable, *wire, "the wire vectors: {header:?}");
        }
        if capable {
            may += 1;
        } else {
            may_not += 1;
        }
    }
    assert_eq!((may, may_not), (22, 256));
}

#[test]
fn a_catalog_is_loaded_or_not_as_the_python_host_loads_it() {
    let vectors = vectors();
    let identifiers: Vec<&str> = vectors["identifiers"]
        .as_array()
        .unwrap()
        .iter()
        .map(|text| text.as_str().unwrap())
        .collect();
    let probes: Vec<J> = vectors["probes"]
        .as_array()
        .unwrap()
        .iter()
        .map(|text| json::read(text.as_str().unwrap().as_bytes()).unwrap())
        .collect();
    let (mut loaded, mut not) = (0, 0);
    for case in vectors["catalogs"].as_array().unwrap() {
        let name = case["name"].as_str().unwrap();
        let raw = python::b64decode(case["catalog"].as_str().unwrap()).unwrap();
        let refusals = python::b64decode(case["refusals"].as_str().unwrap()).unwrap();
        match Catalog::load(&raw, &refusals) {
            Err(Unavailable) => {
                assert!(
                    case["loaded"].is_null(),
                    "{name}: not loaded here, loaded there"
                );
                not += 1;
            }
            Ok(catalog) => {
                assert_eq!(catalog.sha256, case["loaded"]["sha256"], "{name}");
                let matched: Vec<bool> = identifiers
                    .iter()
                    .map(|text| catalog.ident.matches(text))
                    .collect();
                assert_eq!(json!(matched), case["loaded"]["identifiers"], "{name}");
                let verdicts: Vec<Value> = probes
                    .iter()
                    .map(|probe| match spec::validate(&catalog, probe) {
                        Ok(()) => json!("valid"),
                        Err(refusal) => refusal.triple(Ok(&catalog)),
                    })
                    .collect();
                assert_eq!(json!(verdicts), case["loaded"]["probes"], "{name}");
                loaded += 1;
            }
        }
    }
    // While the catalog cannot be loaded, every refusal is that one.
    let unavailable = Refusal::new("not_open", "").triple(Err(Unavailable));
    assert_eq!(unavailable, vectors["without_a_catalog"]);
    assert_eq!((loaded, not), (14, 27));
}
