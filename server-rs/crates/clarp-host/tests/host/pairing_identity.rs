//! Pairing before a device spends its code and revoking the device's own pairing.
use super::*;

#[test]
fn pairing_identity_has_its_own_budget_and_limited_devices_may_revoke_only_themselves() {
    const ADMIN: &str = "the-administrators-token";
    let place = Place::new("pairing-identity");
    let home = place.root.join("home");
    let homes = [
        ("HOME", home.clone()),
        ("XDG_CONFIG_HOME", home.join(".config")),
        ("XDG_CACHE_HOME", home.join(".cache")),
        ("XDG_DATA_HOME", home.join(".local/share")),
        ("XDG_STATE_HOME", home.join(".local/state")),
    ];
    let world: Vec<_> = homes
        .iter()
        .map(|(name, path)| (*name, path.to_str().unwrap()))
        .collect();
    let host = place.start_in(ADMIN, &["--name", "Fixture Host"], &world);
    let info = host.get("/server-info");
    let vectors: Value = serde_json::from_str(include_str!(
        "../../../clarp-kernel/tests/fixtures/pairing-identity-vectors.json"
    ))
    .unwrap();
    let mut identity = vectors["identity"][0].clone();
    identity["server_id"] = info["server_id"].clone();
    identity["contract"]["host"] = info["contract"]["host"].clone();
    assert!(!identity["server_id"].as_str().unwrap().is_empty());

    let body = |answer: &Fetched| serde_json::from_slice::<Value>(&answer.body).unwrap();
    for (peer, after_pairing) in [("192.0.2.10", false), ("192.0.2.11", true)] {
        let source = [("x-forwarded-for", peer)];
        let exchange = || {
            fetch(
                &host,
                "",
                "POST",
                "/pairing/exchange",
                &[source[0], ("content-type", "application/json")],
                br#"{"code":"clp_nothing","device_name":""}"#,
            )
        };
        let failed_pairing = || {
            for requested in 1..=20 {
                assert_eq!(
                    exchange().status,
                    409,
                    "pairing failure {requested} at {peer}"
                );
            }
            let wait = exchange();
            assert_eq!(wait.status, 429);
            assert_eq!(body(&wait), json!({ "error": "too many pairing failures" }));
        };
        if after_pairing {
            failed_pairing();
        }
        let read = || fetch(&host, "", "GET", "/pairing/identity", &source, b"");
        for requested in 1..=30 {
            let answered = read();
            assert_eq!(answered.status, 200, "read {requested} at {peer}");
            assert_eq!(body(&answered), identity);
            assert_eq!(answered.header("retry-after"), "");
            assert_eq!(answered.header("cache-control"), "no-store");
        }
        let wait = read();
        assert_eq!(wait.status, 429, "{peer}");
        assert_eq!(body(&wait), json!({ "error": "too many identity reads" }));
        assert!((1..=60).contains(&wait.header("retry-after").parse::<u64>().unwrap()));
        if !after_pairing {
            failed_pairing();
        }
    }
    assert_eq!(
        host.ask_as("", "POST", "/pairing/identity", Some(&json!({})))
            .0,
        401,
    );

    let (limited, own_id) = pair(&host, "Limited", "limited");
    let (full, other_id) = pair(&host, "Full", "full");
    let revoke = |token: &str, id: &str| {
        host.ask_as(
            token,
            "POST",
            "/paired-devices/revoke",
            Some(&json!({ "device_id": id })),
        )
    };
    let refused = (403, json!({ "error": "full device access required" }));
    for other in [
        other_id.as_str(),
        "unknown-device",
        &format!("{own_id}x"),
        &own_id.to_uppercase(),
    ] {
        assert_eq!(revoke(&limited, other), refused, "{other}");
    }
    assert_eq!(
        revoke(&limited, " \u{1f} "),
        (400, json!({ "error": "device_id required" })),
    );
    assert_eq!(host.ask_as(&full, "GET", "/server-info", None).0, 200);
    assert_eq!(
        revoke(&limited, &format!("\u{1c} {own_id} \u{a0}")),
        (200, json!({ "ok": true })),
    );
    assert_eq!(host.ask_as(&limited, "GET", "/server-info", None).0, 401);
    assert_eq!(host.ask_as(&full, "GET", "/server-info", None).0, 200);
    let (other, other_id) = pair(&host, "Other", "limited");
    assert_eq!(revoke(&full, &other_id), (200, json!({ "ok": true })));
    assert_eq!(host.ask_as(&other, "GET", "/server-info", None).0, 401);
    assert_eq!(
        revoke(&full, &other_id),
        (404, json!({ "error": "device not found" })),
    );
}
