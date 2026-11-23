//! Changing an agent's backend (#720) against the fake Host: the feature
//! gate, the offer, the start, the phases to done, a refusal in words, and a
//! move this app did not start shown from the roster row.

mod common;

use clarp_core::transit::{Phase, Step};
use common::{Driver, Host};
use serde_json::json;

fn offer() -> serde_json::Value {
    json!({"providers": {
        "claude": {"label": "Claude", "sort_index": 1, "available": true, "models": [{"id": "opus", "label": "Opus", "default": true}]},
        "codex": {"label": "Codex", "sort_index": 2, "available": true, "models": [{"id": "gpt-6", "label": "GPT-6", "default": true}]},
        "grok": {"label": "Grok", "sort_index": 3, "available": false, "unavailable_reason": "signed_out", "models": []},
    }})
}

#[test]
fn an_older_host_offers_no_backend_change() {
    let host = Host::start("transit-off");
    let mut d = Driver::new(&host.base);
    d.connect();
    d.until("rachel", |e| e.roster().find("rachel").is_some());
    assert!(!d.engine.backend_transit_offered("rachel"));
    assert!(!d.engine.transit_open("rachel"));
    assert!(host.requests("GET", "/agent-model-options").iter().all(|r| r["query"]["session"].is_null()));
}

#[test]
fn a_move_runs_from_the_offer_through_its_phases_to_done() {
    let host = Host::start("transit-move");
    host.control("/__control/transit", json!({"on": true, "options": offer()}));
    let mut d = Driver::new(&host.base);
    d.connect();
    d.until("the feature", |e| e.backend_transit_offered("rachel") && e.roster().find("rachel").is_some());
    assert!(d.engine.transit_open("rachel"));
    d.until("the offer", |e| !e.transit_options("rachel").is_empty());
    let options = d.engine.transit_options("rachel");
    assert_eq!(options.iter().map(|b| b.id.as_str()).collect::<Vec<_>>(), ["claude", "codex", "grok"]);
    assert_eq!(options[2].reason.as_deref(), Some("Not signed in"));
    assert_eq!(host.requests("GET", "/agent-model-options").last().unwrap()["query"]["session"], "rachel");

    d.engine.transit_pick_backend("rachel", "grok");
    assert_eq!(d.engine.transit_flow("rachel").step, Step::Picking, "an unavailable backend is not picked");
    d.engine.transit_pick_backend("rachel", "codex");
    d.engine.transit_pick_model("rachel", "gpt-6");
    d.engine.transit_confirm("rachel");
    let mut phases = Vec::new();
    d.until_within("done", std::time::Duration::from_secs(10), |e, _| {
        if let Step::Moving { phase, .. } = e.transit_flow("rachel").step
            && phases.last() != Some(&phase)
        {
            phases.push(phase);
        }
        matches!(e.transit_flow("rachel").step, Step::Done { .. })
    });
    assert!(phases.contains(&Phase::Translating), "the phases were shown: {phases:?}");
    let start = host.requests("POST", "/agent-transit");
    assert_eq!(start.len(), 1);
    assert_eq!(start[0]["body"]["backend"], "codex");
    assert_eq!(start[0]["body"]["model"], "gpt-6");
    assert!(!start[0]["body"]["request_id"].as_str().unwrap_or_default().is_empty());
    d.until("the roster names the new backend", |e| e.roster().find("rachel").is_some_and(|a| a.backend == "codex" && a.transit.is_empty()));
}

#[test]
fn a_refused_start_says_why_and_leaves_the_agent() {
    let host = Host::start("transit-refused");
    host.control("/__control/transit", json!({"on": true, "options": offer(), "refuse": [409, {"error": "messages_waiting"}]}));
    let mut d = Driver::new(&host.base);
    d.connect();
    d.until("the feature", |e| e.backend_transit_offered("rachel") && e.roster().find("rachel").is_some());
    assert!(d.engine.transit_open("rachel"));
    d.until("the offer", |e| !e.transit_options("rachel").is_empty());
    d.engine.transit_pick_backend("rachel", "codex");
    d.engine.transit_pick_model("rachel", "gpt-6");
    d.engine.transit_confirm("rachel");
    d.until("refused", |e| matches!(e.transit_flow("rachel").step, Step::Failed { .. }));
    assert_eq!(
        d.engine.transit_flow("rachel").step,
        Step::Failed { backend: "codex".into(), reason: clarp_core::transit::MESSAGES_WAITING.into() }
    );
    assert_eq!(d.engine.roster().find("rachel").unwrap().backend, "claude");
}

#[test]
fn a_move_started_elsewhere_shows_from_the_roster_row() {
    let host = Host::start("transit-adopt");
    host.control("/__control/transit", json!({"on": true, "options": offer()}));
    host.control("/__control/agent", json!({"session": "rachel", "set": {"transit": {"transit_id": "t9", "phase": "registering", "backend": "codex", "model": "gpt-6"}}}));
    let mut d = Driver::new(&host.base);
    d.connect();
    d.until("adopted", |e| e.transit_flow("rachel").transit_id() == Some("t9"));
    assert!(!d.engine.transit_open("rachel"), "no picker while it moves");
}
