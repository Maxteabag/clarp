use std::time::Duration;

use clarp_core::endpoint::*;
use clarp_core::sse::SseMessage;
use serde_json::json;
use url::Url;

fn url(text: &str) -> Url {
    Url::parse(text).unwrap()
}

#[test]
fn base_urls_are_normalised_and_paths_stay_beneath_them() {
    let base = normalize_base(url("https://host.example/clarp?x=1#frag"));
    assert_eq!(base.as_str(), "https://host.example/clarp/");
    assert_eq!(resolve(&base, "/agents/snapshot").unwrap().as_str(), "https://host.example/clarp/agents/snapshot");
    assert_eq!(resolve(&base, "//log").unwrap().as_str(), "https://host.example/clarp/log");
    assert_eq!(resolve(&base, "https://evil.example/p.png").unwrap().host_str(), Some("evil.example"));
}

#[test]
fn only_the_same_origin_may_receive_the_bearer_token() {
    let base = url("https://clarp.example.test/");
    assert!(same_origin(&url("https://clarp.example.test:443/a.png"), &base));
    assert!(same_origin(&url("HTTPS://CLARP.example.test/a.png"), &base));
    assert!(!same_origin(&url("https://evil.example.test/portrait.png"), &base));
    assert!(!same_origin(&url("http://clarp.example.test/a.png"), &base));
    assert!(!same_origin(&url("https://clarp.example.test:8443/a.png"), &base));
    assert!(!same_origin(&url("https://user:pw@clarp.example.test/a.png"), &base));
    assert!(redirect_is_no_less_safe(&url("http://a/"), &url("https://b/")));
    assert!(!redirect_is_no_less_safe(&url("https://a/"), &url("http://a/")));
}

#[test]
fn reconnect_backoff_doubles_to_the_cap() {
    let timing = SseTiming::default();
    let delays: Vec<u128> = (0..8).map(|a| timing.reconnect_delay(a).as_millis()).collect();
    assert_eq!(delays, [250, 500, 1000, 2000, 4000, 5000, 5000, 5000]);
    assert_eq!(timing.watchdog, Duration::from_secs(25));
}

#[test]
fn reconnect_jitter_adds_at_most_a_quarter() {
    use clarp_core::endpoint::jitter;
    let five = Duration::from_secs(5);
    assert_eq!(jitter(five, 0.0), five);
    assert_eq!(jitter(five, 1.0), Duration::from_millis(6_250));
    assert_eq!(jitter(five, 7.0), Duration::from_millis(6_250), "clamped");
    let timing = SseTiming::default();
    for attempt in 0..10 {
        let (base, waited) = (timing.reconnect_delay(attempt), timing.jittered_delay(attempt));
        assert!(waited >= base && waited <= base + base / 4, "{attempt}: {waited:?}");
    }
}

// Port of tst_native_core::sseCursorIsScopedToOneHost.
#[test]
fn sse_cursor_is_scoped_to_one_host() {
    let mut cursor = SseCursor::default();
    cursor.set_endpoint(&normalize_base(url("https://one.example")));
    cursor.set_last_event_id("42");
    cursor.set_endpoint(&normalize_base(url("https://one.example")));
    assert_eq!(cursor.last_event_id(), "42");
    cursor.set_endpoint(&normalize_base(url("https://two.example")));
    assert!(cursor.last_event_id().is_empty());
}

#[test]
fn accepted_events_carry_their_id() {
    let mut cursor = SseCursor::default();
    let data = json!({"type": "agent-state"}).as_object().cloned().unwrap();
    let event = cursor.accept(SseMessage { id: "41".into(), event: String::new(), data: data.clone() });
    assert_eq!(event["event_id"], 41);
    assert_eq!(cursor.last_event_id(), "41");
    let event = cursor.accept(SseMessage { id: "a-7".into(), event: String::new(), data: data.clone() });
    assert_eq!(event["event_id"], "a-7");
    let event = cursor.accept(SseMessage { id: String::new(), event: String::new(), data });
    assert!(!event.contains_key("event_id"));
    assert_eq!(cursor.last_event_id(), "a-7");
}

#[test]
fn error_bodies_name_the_host_message() {
    assert_eq!(error_message(br#"{"message":"busy","detail":"d"}"#, "HTTP 409"), "busy");
    assert_eq!(error_message(br#"{"message":"","detail":"no such agent"}"#, "x"), "no such agent");
    assert_eq!(error_message(br#"{"error":"unauthorized"}"#, "x"), "unauthorized");
    assert_eq!(error_message(b"<html>", "HTTP 502"), "HTTP 502");
    assert_eq!(error_message(br#"["list"]"#, "HTTP 500"), "HTTP 500");
}

#[test]
fn path_segments_are_percent_encoded_like_qt() {
    assert_eq!(percent_encode_segment("job 1/x"), "job%201%2Fx");
    assert_eq!(percent_encode_segment("a-b_c.d~e"), "a-b_c.d~e");
}
