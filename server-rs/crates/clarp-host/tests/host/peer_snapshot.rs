use super::*;
use std::os::unix::fs::PermissionsExt;

const ADMIN: &str = "fixture-peer-administrator";
const PEER_TOKEN: &str = "cld_fixture-peer-token-kept-on-the-host-only";
const CODE: &str = "clp_fixture-one-time-peer-code";

fn start(place: &Place, extras: &[(&str, &str)]) -> (Host, PathBuf) {
    let xdg = place.root.join("xdg");
    let mut env = vec![("XDG_CONFIG_HOME", xdg.to_str().unwrap())];
    env.extend_from_slice(extras);
    (
        place.start_in(ADMIN, &[], &env),
        xdg.join("clarp-host/peers.json"),
    )
}

fn add(host: &Host, name: &str, url: &str, id: &str) -> Value {
    host.post(
        "/peers",
        json!({ "name": name, "url": url, "token": PEER_TOKEN, "server_id": id }),
    )
}

fn request_head(request: &str) -> String {
    request
        .split("\r\n\r\n")
        .next()
        .unwrap()
        .to_ascii_lowercase()
}

#[test]
fn only_full_scope_reads_the_registered_peer_and_shares_the_cache() {
    let place = Place::new("peer-snapshot");
    let (proxy, proxy_requests) = answers_using(|_, _| ("502 Bad Gateway", "{}".into()));
    let (host, file) = start(
        &place,
        &[
            ("http_proxy", &proxy),
            ("HTTP_PROXY", &proxy),
            ("https_proxy", &proxy),
            ("HTTPS_PROXY", &proxy),
            ("no_proxy", ""),
            ("NO_PROXY", ""),
        ],
    );
    let (url, requests) = answers_using(|path, _| match path {
        "/clarp/server-info" => (
            "200 OK",
            json!({"server_id":"reported-id","name":"Host B"}).to_string(),
        ),
        "/clarp/agents/snapshot?include_archived=0" => (
            "200 OK",
            json!({"agents":[{"session":"ada","cwd":"/fixture/ada"}]}).to_string(),
        ),
        _ => ("404 Not Found", "{}".into()),
    });
    let url = format!("{url}/clarp");
    assert_eq!(
        add(&host, "work", &format!("{url}/"), ""),
        json!({"name":"work", "url":url, "enabled":true, "server_id":""}),
    );
    assert_eq!(
        std::fs::metadata(&file).unwrap().permissions().mode() & 0o777,
        0o600
    );
    let before = std::fs::read(&file).unwrap();
    let before_time = std::fs::metadata(&file).unwrap().modified().unwrap();
    let (limited, _) = pair(&host, "limited", "limited");
    let (full, _) = pair(&host, "full", "full");
    assert_eq!(host.ask_as(&limited, "GET", "/peers/snapshot", None).0, 403);
    assert_eq!(host.ask_as("", "GET", "/peers/snapshot", None).0, 401);
    assert!(requests.lock().unwrap().is_empty());

    let path = "/peers/snapshot?url=http://127.0.0.1:9&token=ignored";
    let (status, answer) = host.ask_as(&full, "GET", path, None);
    assert_eq!(status, 200);
    assert_eq!(
        answer,
        json!({
            "peers":{"reported-id":{
                "name":"Host B", "url":url, "server_id":"reported-id", "reachable":true,
                "error":null, "snapshot":{"agents":[{"session":"ada","cwd":"/fixture/ada"}]}
            }},
            "cache_seconds":10
        }),
    );
    assert_eq!(host.get("/peers/snapshot"), answer);
    assert_eq!(std::fs::read(&file).unwrap(), before);
    assert_eq!(
        std::fs::metadata(&file).unwrap().modified().unwrap(),
        before_time
    );
    assert_eq!(host.get("/peers")[0]["server_id"], "");
    assert!(!answer.to_string().contains(PEER_TOKEN));
    assert!(proxy_requests.lock().unwrap().is_empty());
    let requests = requests.lock().unwrap();
    assert_eq!(requests.len(), 2);
    for (request, path) in requests.iter().zip([
        "/clarp/server-info",
        "/clarp/agents/snapshot?include_archived=0",
    ]) {
        assert_eq!(
            request.lines().next(),
            Some(format!("GET {path} HTTP/1.1").as_str())
        );
        let head = request_head(request);
        assert!(head.contains(&format!("authorization: bearer {PEER_TOKEN}")));
        assert!(!head.contains("cookie:"));
        assert!(!head.contains("x-clarp-peer:"));
    }
}

#[test]
fn read_or_send_never_follows_a_redirect_and_read_errors_are_codes() {
    const ABOVE_PREVIOUS_LIMIT: usize = 4 * 1024 * 1024 + 1;
    let place = Place::new("peer-refusals");
    let (proxy, proxy_requests) = answers_using(|_, _| ("502 Bad Gateway", "{}".into()));
    let (host, _) = start(&place, &[("HTTP_PROXY", &proxy), ("NO_PROXY", "")]);
    let (redirect, calls) = answers_using(|path, _| match path {
        "/server-info" | "/send" => (
            "307 Temporary Redirect\r\nLocation: /elsewhere",
            "{}".into(),
        ),
        _ => ("200 OK", "{}".into()),
    });
    add(&host, "redirect", &redirect, "known-id");
    let (refused, _) =
        answers_using(|_, _| ("401 Unauthorized", json!({"error":PEER_TOKEN}).to_string()));
    let (big, _) = answers_using(|_, _| {
        (
            "200 OK",
            format!("{{\"x\":\"{}\"}}", "x".repeat(16 * 1024 * 1024)),
        )
    });
    let (large_read, _) = answers_using(|path, _| match path {
        "/server-info" => ("200 OK", json!({"server_id":"large-id"}).to_string()),
        "/agents/snapshot?include_archived=0" => (
            "200 OK",
            format!("{{\"padding\":\"{}\"}}", "x".repeat(ABOVE_PREVIOUS_LIMIT)),
        ),
        _ => ("404 Not Found", "{}".into()),
    });
    let (array, _) = answers_using(|_, _| ("200 OK", "[]".into()));
    let (echo, _) = answers_using(|_, _| ("200 OK", json!({"secret":PEER_TOKEN}).to_string()));
    for (alias, url) in [
        ("refused", refused),
        ("big", big),
        ("large-read", large_read),
        ("array", array),
        ("echo", echo),
    ] {
        add(&host, alias, &url, "");
    }
    let answer = host.get("/peers/snapshot");
    let large = &answer["peers"]["large-id"];
    assert_eq!(large["reachable"], true);
    assert_eq!(
        large["snapshot"]["padding"].as_str().map(str::len),
        Some(ABOVE_PREVIOUS_LIMIT)
    );
    for (key, error) in [
        ("known-id", "redirect_refused"),
        ("peer:refused", "refused"),
        ("peer:big", "too_large"),
        ("peer:array", "not_json"),
        ("peer:echo", "refused"),
    ] {
        let entry = &answer["peers"][key];
        assert_eq!(entry["error"], error);
        assert_eq!(entry["reachable"], false);
        assert!(entry["snapshot"].is_null());
    }
    assert!(!answer.to_string().contains(PEER_TOKEN));

    let sent = place
        .host_command()
        .args([
            "tool",
            "clarp-admin",
            "prompt",
            "--server",
            "redirect",
            "--to",
            "ada",
            "--text",
            "hello",
        ])
        .env("CLARP_HOST_TOKEN", ADMIN)
        .env(clarp_host::TOOL_ADDR_ENV, &host.address)
        .ended();
    assert_eq!(sent.status.code(), Some(5));
    let error = String::from_utf8_lossy(&sent.stderr);
    assert!(error.contains("redirected /send (HTTP 307)"));
    assert!(!error.contains(PEER_TOKEN));
    let calls = calls.lock().unwrap();
    assert_eq!(calls.len(), 2);
    assert!(calls[0].starts_with("GET /server-info "));
    assert!(calls[1].starts_with("POST /send "));
    assert!(request_head(&calls[1]).contains(&format!("authorization: bearer {PEER_TOKEN}")));
    assert!(request_head(&calls[1]).contains("x-clarp-peer: redirect"));
    assert!(proxy_requests.lock().unwrap().is_empty());
}

#[test]
fn local_admin_pairs_by_code_and_stores_a_python_compatible_registry() {
    let place = Place::new("peer-code");
    let (host, file) = start(&place, &[]);
    let (url, calls) = answers_using(|path, _| match path {
        "/pairing/identity" => (
            "200 OK",
            json!({"name":"host-b","server_id":"peer-id"}).to_string(),
        ),
        "/pairing/exchange" => (
            "201 Created",
            json!({"device":{"token":PEER_TOKEN,"scope":"limited"}}).to_string(),
        ),
        _ => ("404 Not Found", "{}".into()),
    });
    let skipped = place
        .host_command()
        .args([
            "peers",
            "add",
            &url,
            "--code",
            CODE,
            "--host",
            &host.address,
        ])
        .env_remove("CLARP_HOST_TOKEN")
        .ended();
    assert_eq!(skipped.status.code(), Some(2));
    assert!(calls.lock().unwrap().is_empty());

    let added = place
        .host_command()
        .args(["tool", "clarp-admin", "peers", "add", &url, "--code", CODE])
        .env("CLARP_HOST_TOKEN", ADMIN)
        .env(clarp_host::TOOL_ADDR_ENV, &host.address)
        .ended();
    assert!(
        added.status.success(),
        "{}",
        String::from_utf8_lossy(&added.stderr)
    );
    let public: Value = serde_json::from_slice(&added.stdout).unwrap();
    assert_eq!(
        public,
        json!({"name":"host-b","url":url,"enabled":true,"server_id":"peer-id"})
    );
    assert!(!String::from_utf8_lossy(&added.stdout).contains(PEER_TOKEN));
    let kept: Value = serde_json::from_slice(&std::fs::read(file).unwrap()).unwrap();
    assert_eq!(
        kept,
        json!({"host-b":{"url":url,"enabled":true,"server_id":"peer-id","token":PEER_TOKEN}})
    );
    assert_eq!(host.get("/peers"), json!([public]));
    let calls = calls.lock().unwrap();
    assert_eq!(calls.len(), 2);
    assert!(calls[0].starts_with("GET /pairing/identity "));
    assert!(calls[1].starts_with("POST /pairing/exchange "));
    for request in calls.iter() {
        assert!(!request_head(request).contains("authorization:"));
    }
    let body: Value = serde_json::from_str(calls[1].split_once("\r\n\r\n").unwrap().1).unwrap();
    let our_name = host.get("/server-info")["name"]
        .as_str()
        .unwrap()
        .to_string();
    assert_eq!(
        body,
        json!({"code":CODE,"device_name":format!("{our_name} (peer Host)")})
    );
}
