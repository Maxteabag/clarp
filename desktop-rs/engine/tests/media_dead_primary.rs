//! "Chats from all paired Hosts" with the primary Host out of reach: a
//! peer's chat images come from the peer, with the peer's token, and arrive
//! while every request to the primary hangs. (28 Nov 2026:
//! the primary being off for a day broke images in the other Host's chats.)
//! One test in its own binary: it sets process environment variables.

mod common;

use std::time::Duration;

use clarp_engine::TranscriptImageReply;
use common::{Driver, Host};

#[test]
fn a_hung_primary_never_holds_up_a_peer_chats_media() {
    let cache = std::env::temp_dir().join(format!("clarp-engine-dead-primary-cache-{}", std::process::id()));
    // SAFETY: the only test in this binary; set before any engine starts.
    unsafe {
        std::env::set_var("CLARP_PEER_TOKEN", "probe-token");
        std::env::set_var("XDG_CACHE_HOME", &cache);
    }
    // The primary accepts connections and never answers: worse than a
    // closed port, every request to it waits.
    let hung = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let primary = format!("http://{}", hung.local_addr().unwrap());
    let peer = Host::start_with("dead-primary-peer", &["--peer"]);
    let mut settings = clarp_core::settings::Settings::in_memory();
    clarp_core::hosts::remember(&mut settings, &peer.base, "Peer Host");
    settings.set(clarp_core::all_hosts::SETTING, true);
    let mut d = Driver::with_settings(&primary, settings);
    d.engine.start();
    d.until("the peer's chats while the primary hangs", |e| e.roster().find("@fake-host-2/nova").is_some());
    assert_ne!(d.engine.connection_state(), "live", "the primary never answered");

    // The chat's own Host owns its images.
    let session = "@fake-host-2/nova";
    let host = d.engine.transcript_image_host(session).map(str::to_owned);
    assert_eq!(host.as_deref(), Some(peer.base.as_str()), "a peer chat's images come from the peer");
    let request = d.engine.fetch_transcript_image(session, &peer.base, "/media/img-a").expect("the peer is asked");
    let mut reply = None;
    for _ in 0..100 {
        d.settle(Duration::from_millis(50));
        match d.engine.take_transcript_image(&request) {
            TranscriptImageReply::Pending => continue,
            other => {
                reply = Some(other);
                break;
            }
        }
    }
    match reply {
        Some(TranscriptImageReply::Received(Ok(bytes))) => assert!(bytes.starts_with(b"\x89PNG"), "the peer's PNG"),
        Some(TranscriptImageReply::Received(Err(error))) => panic!("the peer's image failed: {error}"),
        Some(TranscriptImageReply::Abandoned) => panic!("the peer's image request was abandoned"),
        _ => panic!("the peer's image did not arrive within 5 s while the primary hung"),
    }
    let asked = peer.requests("GET", "/media/img-a");
    assert_eq!(asked.len(), 1, "the peer was asked once");

    assert!(d.engine.live_epoch(&peer.base).is_some_and(|epoch| epoch >= 1), "the peer turned live once");
    assert_eq!(d.engine.live_epoch(&primary), Some(0), "the hung primary never did");
    drop(hung);
    let _ = std::fs::remove_dir_all(&cache);
}
