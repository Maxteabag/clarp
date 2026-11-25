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

fn limits_of(e: &clarp_engine::Engine) -> Option<String> {
    e.lane_settings().and_then(clarp_engine::lanes::limits).map(str::to_owned)
}

#[test]
fn auto_limits_are_read_switched_and_a_fixed_limit_set_switches_to_fixed() {
    let host = Host::start("lanes-auto");
    host.control("/__control/lane-settings", json!({"on": true, "auto": true}));
    let mut d = Driver::new(&host.base);
    d.connect();
    d.until("the auto settings", |e| e.lane_auto_limits_offered() && limits_of(e).is_some());
    assert_eq!(limits_of(&d.engine).as_deref(), Some("auto"));
    let answer = d.engine.lane_settings().unwrap().clone();
    assert_eq!(clarp_engine::lanes::number(&answer, "reserve_percent"), Some(20.0));
    assert_eq!(clarp_engine::lanes::range(&answer, "reserve_percent"), Some((5.0, 50.0)));
    assert_eq!(clarp_engine::lanes::range(&answer, "ceiling"), Some((1.0, 64.0)));
    assert_eq!(clarp_engine::lanes::free_line(&answer), "15800 MB free · 6200 MB kept free · 9600 MB room for kept processes");

    // The owner switches to fixed, and back.
    d.engine.set_lane_setting("limits", json!("fixed"));
    d.until("the switch posted", |_| !host.requests("POST", "/lane-settings").is_empty());
    assert_eq!(host.requests("POST", "/lane-settings")[0]["body"], json!({"limits": "fixed"}));
    d.until("fixed", |e| limits_of(e).as_deref() == Some("fixed"));
    d.engine.set_lane_setting("limits", json!("auto"));
    d.until("auto again", |e| limits_of(e).as_deref() == Some("auto") && host.requests("POST", "/lane-settings").len() == 2);

    // An auto setting: posted alone, kept auto.
    d.engine.set_lane_setting("ceiling", json!(30));
    d.until("the ceiling", |e| e.lane_settings().and_then(|a| clarp_engine::lanes::number(a, "ceiling")) == Some(30.0));
    assert_eq!(limits_of(&d.engine).as_deref(), Some("auto"));

    // A refusal keeps the Host's sentence.
    d.engine.set_lane_setting("reserve_percent", json!(60));
    d.until("the refusal", |e| !e.lane_settings_error().is_empty());
    assert_eq!(d.engine.lane_settings_error(), "reserve_percent must be between 5 and 50");
    d.until("the Host's value back", |e| e.lane_settings().and_then(|a| clarp_engine::lanes::number(a, "reserve_percent")) == Some(20.0));

    // A fixed limit set in auto: the request names only it, the Host switches to fixed.
    d.engine.set_lane_setting("most", json!(10));
    assert_eq!(limits_of(&d.engine).as_deref(), Some("fixed"), "shown fixed at once");
    d.until("the most posted", |_| host.requests("POST", "/lane-settings").len() == 5);
    assert_eq!(host.requests("POST", "/lane-settings")[4]["body"], json!({"most": 10}));
    d.until("the Host's answer", |e| most_of(e) == Some(10.0) && e.lane_settings_error().is_empty());
    assert_eq!(limits_of(&d.engine).as_deref(), Some("fixed"));

    // The Host cannot read its memory: said so, never null.
    host.control("/__control/lane-settings", json!({"free": {"available_mb": null, "reserve_mb": null, "room_mb": null}}));
    host.control("/__control/event", json!({"type": "lane-settings-updated"}));
    d.until("nulls read", |e| e.lane_settings().is_some_and(|a| a["held"]["available_mb"].is_null()));
    let line = clarp_engine::lanes::free_line(d.engine.lane_settings().unwrap());
    assert_eq!(line, "The Host cannot read its machine's memory, so it keeps the fixed limits");
}

#[test]
fn without_auto_limits_no_limits_are_read() {
    let host = Host::start("lanes-no-auto");
    host.control("/__control/lane-settings", json!({"on": true}));
    let mut d = Driver::new(&host.base);
    d.connect();
    d.until("the settings", |e| e.lane_settings().is_some());
    assert!(!d.engine.lane_auto_limits_offered());
    assert_eq!(limits_of(&d.engine), None);
    assert_eq!(clarp_engine::lanes::free_line(d.engine.lane_settings().unwrap()), "");
    // A fixed limit set: no switch to fixed is posted or shown.
    d.engine.set_lane_setting("most", json!(9));
    d.until("the most posted", |_| !host.requests("POST", "/lane-settings").is_empty());
    assert_eq!(host.requests("POST", "/lane-settings")[0]["body"], json!({"most": 9}));
    d.until("the Host's answer", |e| most_of(e) == Some(9.0));
    assert_eq!(limits_of(&d.engine), None);
}

#[test]
fn pressure_is_read_where_the_host_lists_it_and_a_threshold_keeps_auto() {
    let host = Host::start("lanes-pressure");
    host.control("/__control/lane-settings", json!({"on": true, "auto": true, "pressure": true}));
    let mut d = Driver::new(&host.base);
    d.connect();
    d.until("the pressure", |e| e.lane_pressure_offered() && e.lane_settings().is_some_and(|a| a.contains_key("pressure")));
    let answer = d.engine.lane_settings().unwrap().clone();
    assert_eq!(clarp_engine::lanes::range(&answer, "pressure_io_percent"), Some((5.0, 95.0)));
    assert_eq!(clarp_engine::lanes::live_group(&answer).unwrap().summary, "8 · 2 busy · 6 idle · 1945 MB");
    assert_eq!(clarp_engine::lanes::cold_starts_group(&answer).unwrap().summary, "5 · or since the Host started, if sooner");
    assert_eq!(clarp_engine::lanes::pressure_group(&answer).unwrap().summary, "High · 2 turns held");

    // A threshold is posted alone and does not switch to fixed.
    d.engine.set_lane_setting("pressure_io_percent", json!(40));
    d.until("the threshold posted", |_| !host.requests("POST", "/lane-settings").is_empty());
    assert_eq!(host.requests("POST", "/lane-settings")[0]["body"], json!({"pressure_io_percent": 40}));
    d.until("the answer", |e| e.lane_settings().and_then(|a| clarp_engine::lanes::number(a, "pressure_io_percent")) == Some(40.0));
    assert_eq!(limits_of(&d.engine).as_deref(), Some("auto"));

    // The event reads what loads the machine again.
    host.control("/__control/lane-settings", json!({"pressure_status": {"held_for_pressure": 0}}));
    host.control("/__control/event", json!({"type": "lane-settings-updated", "pressure_io_percent": 40}));
    d.until("held again", |e| e.lane_settings().is_some_and(|a| a["held_for_pressure"] == 0));
}

#[test]
fn without_the_pressure_feature_nothing_of_it_is_shown() {
    let host = Host::start("lanes-no-pressure");
    host.control("/__control/lane-settings", json!({"on": true}));
    let mut d = Driver::new(&host.base);
    d.connect();
    d.until("the settings", |e| e.lane_settings().is_some());
    assert!(!d.engine.lane_pressure_offered());
    let answer = d.engine.lane_settings().unwrap();
    assert!(clarp_engine::lanes::live_group(answer).is_none() && clarp_engine::lanes::pressure_group(answer).is_none());
}
