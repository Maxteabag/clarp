//! The shared voice-display fixture (`contract/fixtures/voice-display.json`):
//! the written text left once `<vox>` fillers are hidden. The Host, web and
//! iOS run the same cases.

use std::path::Path;

use clarp_core::text::cleaned_display_text;
use serde_json::Value;

#[test]
fn hidden_fillers_leave_the_punctuation_a_writer_would_use() {
    let path =
        Path::new(env!("CARGO_MANIFEST_DIR")).join("../../contract/fixtures/voice-display.json");
    let fixture: Value = serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
    let failures: Vec<String> = fixture["cases"]
        .as_array()
        .unwrap()
        .iter()
        .filter_map(|case| {
            let input = case["input"].as_str().unwrap();
            let streaming = case["streaming"].as_bool().unwrap_or(false);
            let got = cleaned_display_text(input, streaming);
            let expect = case["expect"].as_str().unwrap();
            (got != expect).then(|| format!("{}: got {got:?}, want {expect:?}", case["name"]))
        })
        .collect();
    assert!(failures.is_empty(), "{}", failures.join("\n"));
}
