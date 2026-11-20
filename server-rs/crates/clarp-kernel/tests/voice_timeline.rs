#![allow(clippy::unwrap_used)]
use clarp_kernel::voice_timeline::{self, Query};
use serde_json::{Value, json};

#[test]
fn batches_and_grouped_latencies_follow_the_reference() {
    let v: Value = serde_json::from_str(include_str!("fixtures/voice-timeline.json")).unwrap();
    let now = v["now"].as_i64().unwrap();
    let mut rows = Vec::new();
    for (i, case) in v["batches"].as_array().unwrap().iter().enumerate() {
        match voice_timeline::batch(&case["body"], now) {
            Err((code, why)) => assert_eq!(json!([code,{"error":why}]), case["reply"], "batch {i}"),
            Ok(batch) => {
                assert_eq!(
                    json!([200,{"ok":true,"server_now":now+1,"stored":batch.rows.len(),"clock_offset_ms":batch.offset}]),
                    case["reply"],
                    "batch {i}"
                );
                rows = batch.rows;
                for (i, row) in rows.iter_mut().enumerate() {
                    row["event_id"] = json!(i + 1);
                }
                rows.sort_by_key(|r| (r["ts"].as_i64(), r["event_id"].as_u64()));
                assert_eq!(json!(rows), case["rows"], "batch {i}");
            }
        }
    }
    for case in v["reads"].as_array().unwrap() {
        let params =
            serde_json::from_value::<Vec<(String, String)>>(case["params"].clone()).unwrap();
        let q = Query::read(&params);
        assert_eq!(
            json!(q.rows(rows.iter().rev(), false)),
            case["events"]["events"]
        );
        let selected = q.rows(rows.iter().rev(), true);
        assert_eq!(
            json!(q.utterances(&selected)),
            case["utterances"]["utterances"]
        );
    }
}

#[test]
fn latest_windows_match_both_reference_limits() {
    let fixture: Value =
        serde_json::from_str(include_str!("fixtures/voice-timeline.json")).unwrap();
    let rows:Vec<_>=(1..=5003).map(|n| json!({"event_id":n,"ts":n,"utterance_id":n.to_string(),"event":"fixture","detail":{}})).collect();
    for window in fixture["windows"].as_array().unwrap() {
        let q = Query {
            limit: window["limit"].as_i64(),
            ..Query::default()
        };
        let selected = q.rows(rows.iter().rev(), false);
        assert_eq!(
            json!([
                selected.len(),
                selected.first().unwrap()["event_id"],
                selected.last().unwrap()["event_id"]
            ]),
            window["rows"]
        );
        let grouped = q.utterances(&q.rows(rows.iter().rev(), true));
        assert_eq!(
            json!([
                grouped.len(),
                grouped.first().unwrap()["utterance_id"],
                grouped.last().unwrap()["utterance_id"]
            ]),
            window["groups"]
        );
    }
}
