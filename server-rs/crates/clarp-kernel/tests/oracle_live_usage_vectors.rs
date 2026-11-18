#![allow(clippy::unwrap_used)]
use clarp_kernel::oracle::usage::{Billing, LiveUsage};
use serde_json::Value;

#[test]
fn cumulative_voice_usage_matches_the_reference() {
    let stories: Vec<Value> =
        serde_json::from_str(include_str!("fixtures/oracle-live-usage-vectors.json")).unwrap();
    let mut count = 0;
    for story in &stories {
        let mode = match story["mode"].as_str().unwrap() {
            "api" => Billing::Api,
            "gemini" => Billing::Gemini,
            "subscription" => Billing::Subscription,
            other => panic!("unknown mode {other}"),
        };
        let mut usage = LiveUsage::new(mode);
        for step in story["steps"].as_array().unwrap() {
            count += 1;
            assert_eq!(usage.observe(&step["event"]), step["observed"]);
            assert_eq!(usage.snapshot(), step["snapshot"]);
        }
    }
    assert_eq!((stories.len(), count), (9, 126));
}
