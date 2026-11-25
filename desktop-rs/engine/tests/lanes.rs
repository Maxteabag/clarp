//! The owner's settings of lanes against the fake Host (feature
//! `lane_settings`, Rust Host contract 178): read when the Host lists the
//! feature, a change posted and its answer kept, a refusal's sentence kept
//! with the Host's value put back, and the event read again.

mod common;

use common::{Driver, Host};
use serde_json::json;

#[test]
fn lane_settings_are_read_changed_refused_and_read_again_on_the_event() {
    let host = Host::start("lanes");
    host.control("/__control/lane-settings", json!({"on": true}));
    let mut d = Driver::new(&host.base);
    d.connect();
    d.until("the settings", |e| e.lane_settings_offered() && e.lane_settings().is_some());
    assert_eq!(most_of(&d.engine), Some(8.0));
    let answer = d.engine.lane_settings().unwrap().clone();
    assert_eq!(clarp_engine::lanes::range(&answer, "most"), Some((1.0, 32.0)));
    assert_eq!(clarp_engine::lanes::switches(&answer).len(), 2);

    // A change: posted, and the Host's answer kept.
    d.engine.set_lane_setting("most", json!(12));
    d.until("the write", |_| !host.requests("POST", "/lane-settings").is_empty());
    assert_eq!(host.requests("POST", "/lane-settings")[0]["body"], json!({"most": 12}));
    d.until("the answer", |e| most_of(e) == Some(12.0));
    assert_eq!(d.engine.lane_settings_error(), "");

    // A late answer to an earlier change does not clear a later refusal.

    // Out of range: the Host's sentence, and its value back.
    d.engine.set_lane_setting("most", json!(40));
    d.until("the refusal", |e| !e.lane_settings_error().is_empty());
    assert_eq!(d.engine.lane_settings_error(), "most must be between 1 and 32");
    d.until("the Host's value back", |e| most_of(e) == Some(12.0));

    // Another device changes a switch: the event reads it all again.
    let reads = host.requests("GET", "/lane-settings").len();
    host.control("/__control/lane-settings", json!({"held": {"lanes": 3, "memory_mb": 500, "unanswered": 1, "tools": {}}}));
    host.control("/__control/event", json!({"type": "lane-settings-updated", "most": 12}));
    d.until("the read after the event", |_| host.requests("GET", "/lane-settings").len() > reads);
    d.until("what the lanes hold now", |e| e.lane_settings().is_some_and(|a| a["held"]["lanes"] == 3));
    assert!(clarp_engine::lanes::held_line(d.engine.lane_settings().unwrap()).contains("1 did not answer"));
}

fn most_of(e: &clarp_engine::Engine) -> Option<f64> {
    e.lane_settings().and_then(|a| clarp_engine::lanes::number(a, "most"))
}

#[test]
fn without_the_feature_nothing_is_read_or_written() {
    let host = Host::start("lanes-off");
    let mut d = Driver::new(&host.base);
    d.connect();
    d.settle(std::time::Duration::from_millis(300));
    assert!(!d.engine.lane_settings_offered());
    d.engine.set_lane_setting("most", json!(4));
    d.settle(std::time::Duration::from_millis(200));
    assert!(host.requests("GET", "/lane-settings").is_empty());
    assert!(host.requests("POST", "/lane-settings").is_empty());
}
