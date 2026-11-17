//! What is said of a tool's use without asking a model is what the Python Host says of it.
//!
//! `tests/fixtures/tool-explanation-vectors.json` (written by
//! `tools/tool-explanation-vectors.py` out of that Host's own functions) holds 543 uses of
//! tools as they were read: how each is told (the template that fits with what the use names
//! for it, or why none fits, for each of the seventeen reasons there are) and the sentence at
//! each of the five levels of detail.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_kernel::explain;
use serde_json::Value;

const VECTORS: &str = include_str!("fixtures/tool-explanation-vectors.json");

#[test]
fn a_use_is_told_and_spoken_of_as_the_python_host_does() {
    let vectors: Value = serde_json::from_str(VECTORS).unwrap();
    let key = vectors["identity_key"].as_str().unwrap();
    assert_eq!(vectors["version"], explain::version());
    assert_eq!(vectors["clip"], explain::LABEL_CLIP);
    let (mut fit, mut none, mut sentences) = (0, 0, 0);
    for row in vectors["uses"].as_array().unwrap() {
        let read = row["read"].as_object().unwrap();
        let told = serde_json::to_string(&row["route"]).unwrap();
        for level in 0..5u8 {
            let (said, route) = explain::lookup(read, level, key);
            assert_eq!(route, row["route"], "how {} is told", row["use"]);
            // What a route says stands in the Python Host's order too.
            assert_eq!(
                serde_json::to_string(&route).unwrap(),
                told,
                "{}",
                row["use"]
            );
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
    // Counted by the generator: a template fits 253 uses and none fits 290; 1010 sentences.
    assert_eq!((fit, none, sentences), (253, 290, 1010));
}
