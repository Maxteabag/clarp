#![allow(clippy::unwrap_used)]
use clarp_kernel::{fcm, python};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

#[test]
fn reference_android_data_and_activity_messages() {
    let fixture: Value = serde_json::from_str(include_str!("fixtures/fcm-vectors.json")).unwrap();
    let mut cases = 0;
    for row in fixture["cases"].as_array().unwrap() {
        let mut payload = row["payload"].clone();
        let repeats = row["repeats"].as_object().unwrap();
        for (key, repeated) in repeats {
            payload[key] = json!(repeated[0].as_str().unwrap().repeat(repeated[1].as_u64().unwrap() as usize));
        }
        let collapse = row["collapse"].as_str().unwrap();
        let data = fcm::data(&payload, &row["context"], collapse, 1795010000000);
        if repeats.is_empty() {
            assert_eq!(json!(data), row["data"], "case {cases}");
            let body = data.map(|data| fcm::message("android-device-of-test", data, row["priority"].as_str().unwrap(), collapse));
            assert_eq!(json!(body), row["message"], "case {cases}");
        } else {
            let hash = data.map(|data| format!("{:x}", Sha256::digest(python::canonical(&data).as_bytes())));
            assert_eq!(json!(hash), row["data"], "case {cases}");
        }
        cases += 1;
    }
    let mut chips = 0;
    for row in fixture["chips"].as_array().unwrap() {
        assert_eq!(fcm::activity(&row["state"], row["server"].as_str().unwrap()), row["payload"]);
        chips += 1;
    }
    assert_eq!((cases, chips), (176, 18));
}
