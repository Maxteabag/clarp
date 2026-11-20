//! The rules exercised by the portrait routes in the isolated Python Host.
#![allow(clippy::unwrap_used)]
use clarp_kernel::portraits;
use serde_json::Value;

#[test]
fn the_model_display_switch_reads_the_values_the_reference_accepts() {
    let cases: Value = serde_json::from_str(include_str!("fixtures/portrait-generation.json")).unwrap();
    let values=cases["preferences"].as_array().unwrap();
    for row in values {
        assert_eq!(portraits::model_avatars(row[0].as_str()),row[1]["model_avatars"].as_bool().unwrap(),"{row}");
    }
    assert_eq!(values.len(),10);
}
