use super::*;

#[test]
fn opencode_local_usage_and_the_selected_keys_allowance_have_separate_provenance() {
    let place = Place::new("opencode-usage");
    let home = &place.root;
    let auth = home.join("opencode/auth.json");
    std::fs::create_dir_all(home.join("opencode")).unwrap();
    let save_key = |name| {
        std::fs::write(
            &auth,
            json!({
                "openrouter": {"type": "api", "key": name},
                "fireworks-ai": {"type": "api", "key": "fixture-fireworks"},
                "huggingface": {"type": "api", "key": "fixture-huggingface"}
            })
            .to_string(),
        )
        .unwrap();
    };
    save_key("fixture-alpha");

    let bin = home.join("opencode-usage-tool");
    program(
        &bin,
        r#"#!/usr/bin/env python3
import json, sys
if "--version" in sys.argv:
    print("1.18.33")
    sys.exit(0)
if sys.argv[1] != "run":
    sys.exit(0)
def report(kind, field, detail):
    detail["sessionID"] = "ses_usage"
    print(json.dumps({"type": kind, "properties": {field: detail}}))
for msg, provider, tokens, cost in (
    ("first", "openrouter", {"input": 8, "output": 3}, 0.125),
    ("second", "fireworks-ai", {"input": 0}, None),
    ("unknown", None, {"output": 2}, 0.0625),
):
    report("message.part.updated", "part", {"type": "step-start", "id": "started-"+msg, "messageID": msg})
    report("message.part.updated", "part", {"type": "text", "id": "text-"+msg, "messageID": msg, "text": msg})
    part = {"type": "step-finish", "id": "ended-"+msg, "messageID": msg, "tokens": tokens}
    if cost is not None:
        part["cost"] = cost
    report("message.part.updated", "part", part)
    if msg == "first":
        report("message.part.updated", "part", part)
    if provider is not None:
        report("message.updated", "info", {"role": "assistant", "id": msg, "providerID": provider, "modelID": "fixture"})
"#,
    );
    let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
    let address = listener.local_addr().unwrap().to_string();
    let seen = Arc::new(Mutex::new(Vec::new()));
    let requests = seen.clone();
    let collector = std::thread::spawn(move || {
        for (key, status, body) in [
            (
                "fixture-alpha",
                200,
                r#"{"data":{"usage_monthly":1.25,"limit":15,"limit_remaining":8.5,"limit_reset":"monthly","label":"fixture-alpha","wallet":999}}"#,
            ),
            (
                "fixture-beta",
                412,
                r#"{"error":"fixture-beta","data":{"usage_monthly":0}}"#,
            ),
        ] {
            let (mut stream, _) = listener.accept().unwrap();
            let request = whole_request(&mut stream);
            requests.lock().unwrap().push(request.clone());
            assert!(
                request.starts_with("GET /api/v1/key HTTP/1.1\r\n"),
                "{request}"
            );
            assert!(
                request
                    .to_lowercase()
                    .contains(&format!("authorization: bearer {key}\r\n"))
            );
            write!(stream, "HTTP/1.1 {status} Answer\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}", body.len()).unwrap();
        }
    });
    let root = home.to_str().unwrap();
    let unset = home.join("unconfigured");
    let world = [
        ("HOME", root),
        ("XDG_DATA_HOME", root),
        ("XDG_CACHE_HOME", root),
        ("CODEX_HOME", unset.to_str().unwrap()),
        ("CLAUDE_CONFIG_DIR", unset.to_str().unwrap()),
        ("CLARP_AGY_PROGRAM", unset.to_str().unwrap()),
        ("CLARP_OPENCODE_PROGRAM", bin.to_str().unwrap()),
        ("CLARP_OPENCODE_LIVE", "0"),
        ("CLARP_USAGE_LOOPBACK", address.as_str()),
    ];

    let token = "fixture-owner-token";
    let bare = place.start_in(token, &[], &world);
    let api = "/backend-usage?refresh=1";
    let sleeping = bare.get(api);
    assert_eq!(
        sleeping["opencode_provider_usage"]["providers"]["openrouter"]["remote"]["status"],
        "disabled"
    );
    assert!(seen.lock().unwrap().is_empty());
    drop(bare);

    let host = place.start_in(token, &["--ask-usage"], &world);
    let usage = |body: Value| body["opencode_provider_usage"].clone();
    let first = host.get(api);
    assert_eq!(first["schema_version"], 1);
    assert!(first["backends"].is_array() && first["providers"].is_object());
    assert!(!first.to_string().contains("fixture-alpha"));
    let book = usage(first);
    let remote = &book["providers"]["openrouter"]["remote"];
    assert_eq!(remote["status"], "available");
    assert_eq!(remote["source"], "openrouter_key");
    assert_eq!(remote["usage_monthly_usd"], 1.25);
    assert_eq!(remote["key_limit_usd"], 15.0);
    assert_eq!(remote["key_limit_remaining_usd"], 8.5);
    assert_eq!(remote["key_limit_reset"], "monthly");
    assert!(remote["observed_at"].is_string());
    assert!(remote.get("wallet").is_none());
    assert_eq!(
        book["providers"]["fireworks-ai"]["remote"]["status"],
        "unavailable"
    );
    assert_eq!(
        book["providers"]["huggingface"]["remote"]["status"],
        "unsupported"
    );
    assert!(book["providers"]["openrouter"]["local"].is_null());

    let limited = host.post(
        "/pairing/issue",
        json!({
            "device_name": "Fixture", "scope": "limited", "url": "https://host.example:7690"
        }),
    );
    let (status, paired) = host.ask_as(
        "",
        "POST",
        "/pairing/exchange",
        Some(&json!({
            "code": limited["code"], "device_name": "Fixture phone"
        })),
    );
    assert_eq!(status, 201);
    assert_eq!(
        host.ask_as(
            paired["device"]["token"].as_str().unwrap(),
            "GET",
            api,
            None
        )
        .0,
        403
    );
    assert_eq!(seen.lock().unwrap().len(), 1);

    let agent = host.post(
        "/agents",
        json!({
            "name": "Usage fixture", "backend": "opencode", "model": "openrouter/fixture"
        }),
    );
    let session = agent["session"].as_str().unwrap();
    assert_eq!(
        host.send(session, "report synthetic costs", "usage-message")["ok"],
        true
    );
    wait_for(
        "one durable turn tallied by the step's own provider",
        || {
            usage(host.get("/backend-usage"))["providers"]["openrouter"]["local"]["five_hour"]["turns"]
                == 1
        },
    );
    let book = usage(host.get(api));
    let window = &book["providers"]["openrouter"]["local"]["five_hour"];
    assert_eq!(window["tokens_in"], 8);
    assert_eq!(window["tokens_out"], 3);
    assert_eq!(window["cost_usd"], 0.125);
    let fireworks = &book["providers"]["fireworks-ai"]["local"]["five_hour"];
    assert_eq!(fireworks["turns"], 1);
    assert_eq!(fireworks["tokens_in"], 0);
    assert!(fireworks["tokens_out"].is_null() && fireworks["cost_usd"].is_null());
    assert_eq!(book["unattributed"]["five_hour"]["tokens_out"], 2);
    assert_eq!(book["unattributed"]["five_hour"]["cost_usd"], 0.0625);
    assert_eq!(seen.lock().unwrap().len(), 1);

    save_key("fixture-beta");
    let rotated = host.get(api);
    assert!(!rotated.to_string().contains("fixture-beta"));
    let book = usage(rotated);
    let remote = &book["providers"]["openrouter"]["remote"];
    assert_eq!(remote["status"], "unavailable");
    assert!(remote["usage_monthly_usd"].is_null() && remote["key_limit_remaining_usd"].is_null());
    assert_eq!(
        book["providers"]["openrouter"]["local"]["five_hour"]["cost_usd"],
        0.125
    );
    assert_eq!(
        usage(host.get(api))["providers"]["openrouter"]["remote"]["status"],
        "unavailable"
    );
    collector.join().unwrap();
    assert_eq!(seen.lock().unwrap().len(), 2);
}
