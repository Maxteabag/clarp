use super::*;
use std::os::unix::fs::PermissionsExt;

#[test]
#[allow(clippy::result_large_err)]
fn phones_can_use_both_relay_names_and_an_extra_refusal_leaves_the_primary_up() {
    let place = Place::new("relay-aliases");
    let primary_config = relay_settings(&place, 0o600);
    let extra_dir = place.root.join("old-host");
    std::fs::create_dir(&extra_dir).unwrap();
    let extra_config = extra_dir.join("config.toml");
    std::fs::write(&extra_config, "[network]\nrelay_enabled = false\n").unwrap();
    let extra_key = "cd".repeat(32);
    let extra_credential = extra_dir.join("relay.json");
    let contents = json!({
        "url": "https://old-relay.example",
        "host_id": "elitebook-old",
        "key": extra_key,
    });
    std::fs::write(&extra_credential, contents.to_string()).unwrap();
    std::fs::set_permissions(&extra_credential, std::fs::Permissions::from_mode(0o600)).unwrap();

    // The two relay sessions share one loopback listener, as names on a hosted relay do.
    let worker = StandInWorker::new();
    let host = place.start_in(
        "the-administrators-token",
        &[
            "--relay-config",
            primary_config.to_str().unwrap(),
            "--relay-also",
            extra_config.to_str().unwrap(),
        ],
        &[("CLARP_RELAY_DIAL", &worker.address)],
    );
    let mut primary = worker.session();
    let mut extra = worker.session();
    let extra_dial = format!("/connect?host=elitebook-old&key={extra_key}");
    if primary.dialled == extra_dial {
        std::mem::swap(&mut primary, &mut extra);
    }
    let primary_key = "ab".repeat(32);
    assert_eq!(
        primary.dialled,
        format!("/connect?host=elitebook-test&key={primary_key}")
    );
    assert_eq!(extra.dialled, extra_dial);

    let (token, _) = pair(&host, "Already paired", "full");
    let before = primary.get_as(&token, "/server-info");
    let through_extra = extra.get_as(&token, "/server-info");
    assert_eq!((before.status, through_extra.status), (200, 200));
    assert_eq!(
        before.json()["server_id"],
        through_extra.json()["server_id"]
    );
    assert!(before.json()["server_id"].is_string());

    let status = host.get("/network/relay");
    assert_eq!(status["host_id"], "elitebook-test");
    assert_eq!(status["connections"][0]["host_id"], "elitebook-test");
    assert_eq!(status["connections"][0]["state"], "connected");
    assert_eq!(status["connections"][1]["host_id"], "elitebook-old");
    assert_eq!(status["connections"][1]["state"], "connected");
    assert!(!status.to_string().contains(&primary_key));
    assert!(!status.to_string().contains(&extra_key));
    let issued = host.post(
        "/pairing/issue",
        json!({ "device_name": "New phone", "scope": "full", "relay": true }),
    );
    assert_eq!(issued["url"], "https://relay.example/h/elitebook-test");

    drop(extra);
    let stream = worker
        .connection_within(20)
        .expect("the extra relay retries");
    let refused = tungstenite::accept_hdr(
        stream,
        |request: &tungstenite::handshake::server::Request, _| {
            assert_eq!(request.uri().to_string(), extra_dial);
            Err(tungstenite::http::Response::builder()
                .status(401)
                .body(None)
                .unwrap())
        },
    );
    assert!(refused.is_err());
    wait_for("the extra relay's refusal to be visible", || {
        let status = host.get("/network/relay");
        status["state"] == "connected"
            && status["connections"][1]["last_loss"]["http_status"] == 401
    });
    let after = primary.get_as(&token, "/server-info");
    assert_eq!(after.status, 200);
    assert_eq!(after.json()["server_id"], before.json()["server_id"]);
}
