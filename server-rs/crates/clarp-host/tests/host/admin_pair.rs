use super::*;

#[test]
fn the_pair_admin_tool_requires_owner_authority_and_lists_only_public_device_fields() {
    let place = Place::new("pair-admin-tool");
    let owner = "pairing-owner-fixture";
    let funnel = "https://computer.mesh.ts.net";
    let settings = place.root.join("owner.toml");
    std::fs::write(
        &settings,
        format!("[server]\npublic_base_url = \"{funnel}\"\n"),
    )
    .unwrap();
    let host = place.start_with(owner, &["--speech-keys", settings.to_str().unwrap()]);
    let run = |args: &[&str]| tool(&host, &place.root, None, args);
    let tools_token = clarp_host::tool_token(owner);
    let refused = place
        .host_command()
        .args([
            "tool",
            "clarp-admin",
            "pair",
            "create",
            "--json",
            "--url",
            "https://host.example",
        ])
        .current_dir(&place.root)
        .env("CLARP_HOST_ADDR", &host.address)
        .env("CLARP_TOOL_TOKEN", tools_token)
        .env_remove("CLARP_HOST_TOKEN")
        .ended();
    assert_eq!(refused.status.code(), Some(1));
    assert!(refused.stdout.is_empty());
    assert!(String::from_utf8_lossy(&refused.stderr).contains("owner token required"));

    let (exit, stdout, stderr) = run(&["clarp-admin", "pair", "create", "--json"]);
    assert_eq!(exit, 0, "{stderr}");
    let issued: Value = serde_json::from_str(&stdout).unwrap();
    assert_eq!(issued["url"], funnel);
    let link = url::Url::parse(issued["uri"].as_str().unwrap()).unwrap();
    let (_, linked_url) = link.query_pairs().find(|(key, _)| key == "url").unwrap();
    assert_eq!(linked_url, funnel);

    for private in [
        "http://127.0.0.1:7690",
        "http://127.0.0.42:7690",
        "http://[::ffff:127.0.0.1]:7690",
        "https://100.64.1.3",
    ] {
        let (exit, stdout, stderr) = run(&["clarp-admin", "pair", "create", "--url", private]);
        assert_eq!(exit, 1);
        assert!(stdout.is_empty());
        assert!(stderr.contains("local or tailnet-only"));
    }

    let (exit, stdout, stderr) = run(&[
        "clarp-admin",
        "pair",
        "create",
        "--json",
        "--url",
        "http://127.0.0.1:7690",
        "--allow-loopback",
        "--name",
        "Pair fixture",
        "--scope",
        "limited",
        "--ttl",
        "90",
    ]);
    assert_eq!(exit, 0, "{stderr}");
    let issued: Value = serde_json::from_str(&stdout).unwrap();
    let uri = url::Url::parse(issued["uri"].as_str().unwrap()).unwrap();
    assert_eq!(uri.scheme(), "clarp");
    assert_eq!(uri.host_str(), Some("pair"));
    assert_eq!(issued["url"], "http://127.0.0.1:7690");
    assert_eq!(issued["device_name"], "Pair fixture");
    assert_eq!(issued["scope"], "limited");
    assert_eq!(
        issued["expires_at"].as_i64().unwrap() - issued["created_at"].as_i64().unwrap(),
        90_000
    );
    assert!(issued.get("code").is_none());
    let (_, code) = uri.query_pairs().find(|(key, _)| key == "code").unwrap();
    let (status, paired) = host.ask_as(
        "",
        "POST",
        "/pairing/exchange",
        Some(&json!({"code": code})),
    );
    assert_eq!(status, 201);
    let device = &paired["device"];
    let id = device["device_id"].as_str().unwrap();
    let token = device["token"].as_str().unwrap();

    let listed = |args: &[&str]| {
        let (exit, stdout, stderr) = run(args);
        assert_eq!(exit, 0, "{stderr}");
        assert!(!stdout.contains(token));
        assert!(!stdout.contains(code.as_ref()));
        serde_json::from_str::<Value>(&stdout).unwrap()
    };
    let visible = listed(&["clarp-admin", "pair", "list"]);
    assert_eq!(
        visible,
        json!({"devices": [{
            "device_id": id, "name": "Pair fixture", "scope": "limited",
            "created_at": device["created_at"],
            "last_seen_at": visible["devices"][0]["last_seen_at"], "revoked_at": null,
        }]})
    );
    assert!(visible["devices"][0]["last_seen_at"].is_number());

    let (exit, stdout, stderr) = run(&["clarp-admin", "pair", "revoke", id]);
    assert_eq!(exit, 0, "{stderr}");
    assert_eq!(stdout, format!("revoked: {id}\n"));
    assert_eq!(
        listed(&["clarp-admin", "pair", "list"]),
        json!({"devices":[]})
    );
    let all = listed(&["clarp-admin", "pair", "list", "--all"]);
    let mut expected = visible;
    assert!(all["devices"][0]["revoked_at"].is_number());
    expected["devices"][0]["revoked_at"] = all["devices"][0]["revoked_at"].clone();
    assert_eq!(all, expected);
    assert_eq!(host.ask_as(token, "GET", "/agents/snapshot", None).0, 401);
}
