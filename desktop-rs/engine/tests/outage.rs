//! A Host out of reach for a moment (`clarp_core::outage`): a gateway's
//! 502 while it restarts, or a dropped stream. Quiet for the grace, then the
//! calm out-of-reach treatment, and on the first answer everything missed
//! shows once, in order. A refusal is said at once; a send is never resent.

mod common;

use std::time::{Duration, Instant};

use common::{Driver, Host};
use serde_json::json;

fn texts(d: &Driver, session: &str) -> Vec<String> {
    d.engine.conversation(session).map(|c| c.rows().iter().map(|r| r.text.clone()).collect()).unwrap_or_default()
}

#[test]
fn a_gateway_outage_is_quiet_then_calm_then_caught_up_once_in_order() {
    let host = Host::start("outage-gateway");
    let mut d = Driver::new(&host.base);
    d.engine.set_outage_grace(Duration::from_millis(1500));
    d.connect();
    d.engine.select("rachel");
    d.until("rachel's log", |e| e.conversation("rachel").is_some_and(|c| c.rows().len() == 2));
    let before = texts(&d, "rachel");
    // A numbered event, so the stream has a place to resume from.
    host.control("/__control/event", json!({"type": "agent-focus", "session": "mike"}));
    d.until("an event", |e| e.roster().find("mike").is_some_and(|a| a.focused));
    d.settle(Duration::from_millis(300));
    let streams_before = host.requests("GET", "/events").len();

    // The Host goes away behind its gateway: every read answers 502.
    for path in ["/events", "/agents/snapshot", "/log", "/server-info", "/live"] {
        host.control("/__control/fail", json!({"path": path, "status": 502, "count": 1000}));
    }
    host.control("/__control/outage", json!({"seconds": 0.2}));
    let dropped = Instant::now();
    d.until("the stream drops", |e| !e.connected());
    // Within the grace nothing is said and nothing shown changes.
    d.settle(Duration::from_millis(800));
    assert!(dropped.elapsed() < Duration::from_millis(1500));
    assert!(d.engine.error().is_empty(), "no raw error within the grace: {:?}", d.engine.error());
    assert_eq!(d.engine.connection_state(), "live");
    assert!(d.engine.roster().transport_available(), "rows as they were");
    assert!(d.engine.outage_active() && !d.engine.outage_shown());
    assert_eq!(texts(&d, "rachel"), before, "the open chat stays readable");
    assert!(d.engine.conversation("rachel").unwrap().error().is_empty());

    // Past the grace: the calm treatment, still no gateway words.
    d.until("out of reach", |e| e.connection_state() == "reconnecting");
    let shown_after = dropped.elapsed();
    assert!(shown_after >= Duration::from_millis(1400), "{shown_after:?}");
    assert!(d.engine.error().is_empty(), "{:?}", d.engine.error());
    assert_eq!(d.engine.roster().display_state("rachel").as_deref(), Some("offline"));
    assert_eq!(texts(&d, "rachel"), before, "cached rows stay");

    // The chat moves on meanwhile; the Host comes back.
    host.control("/__control/upsert", json!({"session": "rachel", "turns": [
        {"id": "gap-1", "role": "assistant", "text": "first while away"},
        {"id": "gap-2", "role": "assistant", "text": "second while away"}]}));
    for path in ["/events", "/agents/snapshot", "/log", "/server-info", "/live"] {
        host.control("/__control/fail", json!({"path": path, "status": 502, "count": 0}));
    }
    d.until("live again", |e| e.connected() && e.connection_state() == "live");
    assert!(!d.engine.outage_active(), "the outage ends with the stream");
    d.until("caught up", |e| e.conversation("rachel").is_some_and(|c| c.rows().len() == 4));
    d.settle(Duration::from_millis(500));
    let after = texts(&d, "rachel");
    assert_eq!(&after[..2], &before[..]);
    assert_eq!(&after[2..], ["first while away", "second while away"], "once, in order");
    assert!(d.engine.error().is_empty(), "{:?}", d.engine.error());
    assert_ne!(d.engine.roster().display_state("rachel").as_deref(), Some("offline"));
    let streams = host.requests("GET", "/events");
    assert!(streams.len() > streams_before + 1, "the stream kept trying: {}", streams.len());
    let resumed = streams.last().unwrap();
    assert!(resumed["last_event_id"].as_str().is_some_and(|id| !id.is_empty()), "resumed by event id: {resumed}");
}

#[test]
fn a_refused_stream_is_said_at_once() {
    let host = Host::start("outage-refused");
    let mut d = Driver::new(&host.base);
    d.engine.set_outage_grace(Duration::from_secs(60));
    d.connect();
    host.control("/__control/fail", json!({"path": "/events", "status": 401, "count": 1000}));
    host.control("/__control/outage", json!({"seconds": 0.2}));
    let started = Instant::now();
    d.until("the refusal", |e| e.error().contains("HTTP 401"));
    assert!(started.elapsed() < Duration::from_secs(10), "not held back by the grace");
}

#[test]
fn a_gateway_at_the_first_connection_is_tried_again_quietly() {
    let host = Host::start("outage-first");
    host.control("/__control/fail", json!({"path": "/server-info", "status": 502, "count": 3}));
    let mut d = Driver::new(&host.base);
    d.engine.set_outage_grace(Duration::from_secs(60));
    d.engine.start();
    d.until_within("live without help", Duration::from_secs(20), |e, changes| {
        assert!(e.error().is_empty(), "no raw error: {:?}", e.error());
        let _ = changes;
        e.connection_state() == "live" && e.roster().agents().len() >= 2
    });
    assert!(host.requests("GET", "/server-info").len() >= 4);
}

#[test]
fn a_first_connection_that_stays_out_of_reach_says_so_calmly_after_the_grace() {
    let host = Host::start("outage-first-long");
    host.control("/__control/fail", json!({"path": "/server-info", "status": 502, "count": 1000}));
    let mut d = Driver::new(&host.base);
    d.engine.set_outage_grace(Duration::from_millis(1200));
    let started = Instant::now();
    d.engine.start();
    d.until("offline", |e| e.connection_state() == "offline");
    assert!(started.elapsed() >= Duration::from_millis(1100));
    assert!(d.engine.error().contains("502"), "the connection page says why: {:?}", d.engine.error());
    host.control("/__control/fail", json!({"path": "/server-info", "status": 502, "count": 0}));
    d.until_within("back by itself", Duration::from_secs(15), |e, _| e.connection_state() == "live");
    assert!(d.engine.error().is_empty(), "{:?}", d.engine.error());
}
