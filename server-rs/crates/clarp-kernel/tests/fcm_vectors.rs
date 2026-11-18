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
    let mut deliveries = 0;
    for row in fixture["deliveries"].as_array().unwrap() {
        let mut sending = fcm::Sending::default();
        let mut sends = 0;
        let mut refreshed = 0;
        let mut waits = Vec::new();
        let fields: Vec<Value> = row["fields"].as_array().unwrap().iter().map(|f| json!({"field":f})).collect();
        let body = json!({"error":{"status":row["rpc"], "details":[{"errorCode":row["code"]},{"@type":"google.rpc.BadRequest","fieldViolations":fields}]}});
        loop {
            sends += 1;
            assert!(sends < 5);
            let next = if sends > 1 && row["ends_well"] == true {
                sending.answered(200, &json!({"name":"taken"}), "")
            } else {
                sending.answered(row["status"].as_u64().unwrap() as u16, &body, row["retry"].as_str().unwrap())
            };
            match next {
                fcm::Next::Retry(wait) => waits.push(wait as f64),
                fcm::Next::Refresh => refreshed += 1,
                fcm::Next::Finished(status, reason, name) => {
                    assert_eq!(row["result"], json!([status,reason,name]), "{row}");
                    break;
                }
            }
        }
        assert_eq!((row["sends"].clone(),row["refreshed"].clone(),row["sleeps"].clone()), (json!(sends),json!(refreshed),json!(waits)), "{row}");
        deliveries += 1;
    }
    assert_eq!(deliveries, 56);
}
