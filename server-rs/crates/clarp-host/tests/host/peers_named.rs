//! A Host's peers come only from the file it is named (issue 741): one file a piece, declared
//! in `host.rs`.

use super::*;

#[test]
fn a_host_started_without_a_peers_file_has_no_peers_where_the_installed_file_lies() {
    let place = Place::new("peers-named");
    // Where an installed Host keeps its peers, in the home this test's Hosts are given.
    let installed = sealed::home(&place.root).join(".config/clarp-host/peers.json");
    let named = place.start_with("", &["--peers", installed.to_str().unwrap()]);
    let peer = json!({
        "name": "work", "url": "https://work.invalid", "token": "fixture-peer-token",
        "server_id": "work-host",
    });
    named.post("/peers", peer);
    assert_eq!(named.get("/peers")[0]["name"], "work");
    named.kill();
    let kept = std::fs::read(&installed).unwrap();

    let unnamed = place.start();
    assert!(
        unnamed
            .heard
            .lock()
            .unwrap()
            .contains("started without --peers: this Host has no peers"),
        "it says so at its start"
    );
    assert_eq!(unnamed.get("/peers"), json!([]));
    assert_eq!(
        unnamed.get("/peers/snapshot")["peers"],
        json!([]),
        "and reads none of their agents"
    );
    let again = json!({"name": "home", "url": "https://home.invalid", "token": "fixture-peer-token"});
    assert_eq!(
        unnamed.ask("POST", "/peers", Some(&again)),
        (
            409,
            json!({"error": "this Host was started without --peers: it keeps no peers"})
        )
    );
    assert_eq!(unnamed.ask("DELETE", "/peers/work", None).0, 409);
    assert_eq!(std::fs::read(&installed).unwrap(), kept, "the file is left as it was");
}
