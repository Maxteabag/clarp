//! The stream following the panes with "Chats from all paired Hosts": a
//! peer's chat shown in a pane is told to that peer's Host on its own
//! stream, never to the primary (whose screen then shows none of its own).
//! One test in its own binary: it sets the peer token's variable.

mod common;

use common::{Driver, Host};
use serde_json::{Value, json};

fn viewport_host(name: &str, extra: &[&str]) -> Host {
    let host = Host::start_with(name, extra);
    host.control("/__control/live", json!({"on": true}));
    host.control("/__control/viewport", json!({"on": true}));
    host
}

/// Each `visible` the Host was opened with or asked for, in order.
fn visible_sets(host: &Host) -> Vec<Value> {
    let opened = host.requests("GET", "/events").into_iter().filter_map(|r| r["query"].get("visible").cloned());
    let posted = host.requests("POST", "/events/{id}/live").into_iter().map(|r| r["body"]["visible"].clone());
    opened.chain(posted).collect()
}

#[test]
fn a_peer_chat_is_shown_on_its_own_hosts_stream() {
    // SAFETY: the only test in this binary; set before any engine starts.
    unsafe { std::env::set_var("CLARP_PEER_TOKEN", "probe-token") };
    let primary = viewport_host("viewport-primary", &[]);
    let peer = viewport_host("viewport-peer", &["--peer"]);
    let mut settings = clarp_core::settings::Settings::in_memory();
    clarp_core::hosts::remember(&mut settings, &peer.base, "Peer Host");
    settings.set(clarp_core::all_hosts::SETTING, true);
    let mut d = Driver::with_settings(&primary.base, settings);
    d.connect();
    d.until("the peer's chats", |e| e.roster().find("@fake-host-2/nova").is_some());
    d.engine.select("rachel");
    d.until("rachel shown on the primary", |e| e.viewport_effective().is_some_and(|(_, v)| v == ["rachel"]));

    d.engine.select("@fake-host-2/nova");
    d.until("nova asked of the peer", |_| {
        visible_sets(&peer).iter().any(|v| v == &json!(["nova"]) || v == &json!("nova"))
    });
    d.until("the primary shows none of its own", |e| e.viewport_effective().is_some_and(|(_, v)| v.is_empty()));
    d.until("nova's log from the peer", |e| e.conversation("@fake-host-2/nova").is_some_and(|c| c.len() >= 1));
    for set in visible_sets(&primary) {
        let text = set.to_string();
        assert!(!text.contains("nova") && !text.contains('@'), "the primary is never told a peer's chat: {text}");
    }
    assert!(primary.requests("GET", "/log").iter().all(|r| r["query"]["session"] != "nova"));

    // Back to the primary's chat: the peer's screen holds none of its chats.
    d.engine.select("rachel");
    d.until("the peer shows nothing", |_| visible_sets(&peer).last() == Some(&json!([])));
}
