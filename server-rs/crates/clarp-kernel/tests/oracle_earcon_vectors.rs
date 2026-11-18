//! The sound cues of a call of the Oracle, against the Python Host's own module.
//!
//! `fixtures/oracle-earcon-vectors.json` is written by `server-rs/tools/oracle-earcon-vectors.py`
//! from `server/lib/oracle_earcons.py`: for every cue, and for the chime of many names, how
//! long the sound is, its first, middle and last samples and the sum of all of it, and which
//! pitch a name has. The same sound must be made here, sample for sample.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_kernel::oracle::earcons::{self, CUES, RATE};
use clarp_kernel::python;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

fn vectors() -> Value {
    serde_json::from_str(include_str!("fixtures/oracle-earcon-vectors.json")).unwrap()
}

/// A name as the Python Host takes whatever it is given for one (`str(persona or "")`).
fn name_of(value: &Value) -> String {
    match value {
        value if !python::truthy(value) => String::new(),
        Value::String(text) => text.clone(),
        other => python::repr(other),
    }
}

#[test]
fn every_cue_sounds_as_the_python_hosts_does_sample_for_sample() {
    let vectors = vectors();
    assert_eq!(
        json!([RATE, CUES]),
        json!([vectors["rate"], vectors["cues"]])
    );
    let mut sounds = 0;
    for sound in vectors["sounds"].as_array().unwrap() {
        let name = sound["name"].as_str().unwrap();
        let bytes = earcons::pcm(name, &name_of(&sound["persona"])).unwrap();
        let (pairs, _) = bytes.as_chunks::<2>();
        let samples: Vec<i16> = pairs.iter().map(|pair| i16::from_le_bytes(*pair)).collect();
        let middle = samples.len() / 2;
        let loudest = samples.iter().map(|sample| sample.unsigned_abs()).max();
        let digest: String = Sha256::digest(&bytes)
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect();
        assert_eq!(
            json!({
                "name": name, "persona": sound["persona"], "bytes": bytes.len(), "sha256": digest,
                "first": samples[..12], "middle": samples[middle..middle + 12],
                "last": samples[samples.len() - 12..], "loudest": loudest,
            }),
            *sound,
            "{name}"
        );
        sounds += 1;
    }
    assert_eq!(sounds, 33);
    let unknown = &vectors["unknown"];
    assert_eq!(earcons::pcm(unknown["name"].as_str().unwrap(), ""), None);
    assert_eq!(unknown["refused"], "unknown Oracle cue: no such cue");
}

#[test]
fn the_chime_of_an_agent_is_pitched_by_its_name() {
    let vectors = vectors();
    let mut pitches = std::collections::BTreeSet::new();
    for case in vectors["tints"].as_array().unwrap() {
        let tint = earcons::tint(&name_of(&case["persona"]));
        assert_eq!(json!(tint), case["tint"], "{case}");
        pitches.insert(tint.to_bits());
    }
    // Every one of the five notes is some name's.
    assert_eq!(pitches.len(), 5);
}
