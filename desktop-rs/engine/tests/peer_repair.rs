//! Repair and switch actions from a peer panel keep the primary connection intact.
mod common;

use common::{Driver, Host};

#[test]
fn a_peer_repair_is_committed_only_after_the_expected_host_answers() {
    // SAFETY: the only test in this process, before the runtime starts.
    unsafe { std::env::set_var("CLARP_PEER_TOKEN", "probe-token") };
    let primary = Host::start_with("repair-primary", &[]);
    let peer = Host::start_with("repair-peer", &["--peer"]);
    let moved = Host::start_with("repair-peer-new", &["--peer"]);
    let mut settings = clarp_core::settings::Settings::in_memory();
    clarp_core::hosts::remember(&mut settings, &peer.base, "Peer Host");
    settings.set(clarp_core::all_hosts::SETTING, true);
    let mut d = Driver::with_settings(&primary.base, settings);
    d.engine.start();
    d.until("both Hosts", |e| e.connection_state() == "live" && e.roster().find("@fake-host-2/nova").is_some());
    d.engine.select("@fake-host-2/nova");
    d.engine.switch_to_host_content(&primary.base);
    let selected = d.engine.selected_session().to_owned();
    assert!(!selected.is_empty() && !selected.starts_with('@'));
    let epoch = d.engine.live_epoch(&primary.base);
    d.engine.repair_peer(&peer.base, &moved.base, "expired", true);
    d.until("rejected pairing code in peer dialog", |e| e.repair_state(&peer.base).is_some_and(|(_, error)| error.contains("expired")));
    assert!(d.engine.saved_hosts().iter().any(|h| h.url == peer.base));
    d.engine.repair_peer(&peer.base, &moved.base, "123456", true);
    d.until("saved peer address replaced", |e| e.saved_hosts().iter().any(|h| h.url == moved.base));
    assert!(!d.engine.saved_hosts().iter().any(|h| h.url == peer.base));
    assert_eq!(d.engine.base_url(), primary.base);
    assert_eq!(d.engine.selected_session(), selected);
    assert_eq!(d.engine.live_epoch(&primary.base), epoch);
    assert!(primary.requests("POST", "/pairing/exchange").is_empty());
    assert_eq!(moved.requests("POST", "/pairing/exchange").len(), 2);
}
