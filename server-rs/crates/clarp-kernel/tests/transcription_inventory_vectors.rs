#![allow(clippy::unwrap_used)]
use clarp_kernel::heard::inventory;
use serde_json::{Value, json};

#[test]
fn the_linux_catalog_shows_the_models_in_the_reference_order() {
    let fixture: Value =
        serde_json::from_str(include_str!("fixtures/transcription-inventory.json")).unwrap();
    for case in fixture["catalog"].as_array().unwrap() {
        let record = &case["record"];
        let files = case["files"].as_array().unwrap();
        let mut models = Vec::new();
        if inventory::installed(record, |name| files.iter().any(|file| file == name)) {
            models.push(inventory::public_record(record).unwrap());
        }
        assert_eq!(json!(models), case["models"]);
        let installed = models
            .iter()
            .map(|record| record["id"].as_str().unwrap().to_owned())
            .collect::<Vec<_>>();
        assert_eq!(json!(inventory::catalog(&installed)), case["catalog"]);
    }
}
