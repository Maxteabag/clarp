use super::*;

#[test]
fn diagnostics_capture_is_opt_in_private_and_persists_across_restart() {
    let place = Place::new("diagnostics");
    let token = "fixture-admin";
    let h = place.start_with_token(token);
    let (limited, _) = pair(&h, "Fixture", "limited");
    let key = "/diagnostics/settings";
    let payload = json!({"event":"micFail","trace_id":"fixture-trace","clip_id":7,"duration_ms":1.5,"clip_url":"fixture-secret-url","persona":"fixture-private-persona","detail":{"prompt":"SYNTHETIC PRIVATE","msg":"getUserMedia rejected: NotAllowedError"}});
    let asked = json!({"enabled":true,"categories":[" CLIENT ","requests","agents","client"]});
    assert_eq!(h.ask_as(&limited, "GET", key, None).0, 403);
    assert_eq!(h.get(key)["settings"]["enabled"], false);
    let who = h.new_agent("Diagnostics");
    for n in 0..5 {
        h.send(&who, "synthetic", &format!("diagnostic-{n}"));
    }
    wait_for("the busy fixture agent settles", || h.state(&who) == "done");
    assert_eq!(
        h.post("/clog", payload.clone()),
        json!({"ok":true,"captured":true,"kept":1,"full_capture":false})
    );
    let dir = place.root.join("data/diagnostics");
    use clarp_ports::diagnostics::DiagnosticUsage;
    let store = clarp_store_files::diagnostics::HourFiles::new(dir, clarp_host::diagnostics::CAP);
    assert_eq!(store.usage().unwrap(), (0, 0));
    assert_eq!(
        h.ask_as(&limited, "GET", "/diagnostics/health", None).0,
        403
    );
    let health = h.get("/diagnostics/health");
    let observed = &health["host_health"];
    assert_eq!(observed["instance_id"], h.get("/host/start")["instance"]);
    assert!(observed["observed_at_ms"].as_i64().is_some_and(|ms| ms > 0));
    let fs = &observed["data_filesystem"];
    assert!(fs["total_bytes"].as_u64().is_some_and(|total| total > 0));
    assert!(fs["available_bytes"].as_u64() <= fs["total_bytes"].as_u64());
    let commits = &observed["slow_sqlite_commits"];
    assert_eq!(commits["threshold_ms"], 2000);
    assert_eq!(commits["window_ms"], 60000);
    assert!(commits["covered_ms"].as_u64().is_some_and(|ms| ms <= 60000));
    assert_eq!(health["ready"], health["checks"]["stt_ready"]);
    assert_eq!(health["checks"]["ffmpeg_ready"], true);
    assert!(health["tts_queue"]["in_flight"].is_number());
    assert_eq!(h.ask_as(&limited, "POST", key, Some(&asked)).0, 403);
    assert_eq!(
        h.ask("POST", key, Some(&json!({"categories":["INVALID"]})))
            .0,
        400
    );
    assert_eq!(
        h.post(key, asked)["settings"]["categories"],
        json!(["requests", "agents", "client"])
    );
    assert_eq!(
        h.ask_as(&limited, "POST", "/clog", Some(&payload)).1,
        json!({"ok":true,"captured":true,"kept":1,"full_capture":true})
    );
    let hour = now_ms().div_euclid(3_600_000);
    let name = "sseDup";
    let huge = json!({"msg":"x".repeat(32*1024)});
    let original = huge.to_string().len();
    h.post(
        "/clog",
        json!({"event":name,"agent_id":"keep-this-id","detail":huge}),
    );
    // Read the hourly store through its port and verify the actual bytes,
    // including what the writer kept when the entire row was too large.
    wait_for("both structured client rows to be written", || {
        let mut bytes = String::new();
        for h in hour - 1..=hour + 1 {
            if let Ok(mut file) = store.read_hour(h) {
                file.read_to_string(&mut bytes).unwrap();
            }
        }
        assert!(
            !bytes.contains("SYNTHETIC PRIVATE")
                && !bytes.contains("fixture-secret-url")
                && !bytes.contains("fixture-private-persona")
        );
        let events: Vec<_> = bytes
            .lines()
            .filter_map(|l| serde_json::from_str::<Value>(l).ok())
            .collect();
        let event = |name: &str| {
            events
                .iter()
                .map(|r| &r["event"])
                .find(|e| e["name"] == name)
        };
        let (Some(mic), Some(cut)) = (event("micFail"), event(name)) else {
            return false;
        };
        assert_eq!(
            mic["detail"]["msg"],
            "getUserMedia rejected: NotAllowedError"
        );
        assert_eq!(
            mic["detail"]["prompt"],
            clarp_kernel::diagnostics::masked(&json!("SYNTHETIC PRIVATE"))
        );
        assert_eq!(mic["trace_id"], "fixture-trace");
        assert_eq!(mic["clip_id"], 7);
        assert_eq!(mic["duration_ms"], 1.5);
        assert_eq!(cut["agent_id"], "keep-this-id");
        assert_eq!(cut["detail"]["_truncated"]["original_bytes"], original);
        assert_eq!(cut["detail"]["_truncated"]["limit_bytes"], 32768);
        true
    });
    h.kill();
    let h = place.start_with_token(token);
    assert_eq!(h.get(key)["settings"]["enabled"], true);
    h.post(key, json!({"enabled":false,"categories":["client"]}));
    assert_eq!(h.post("/clog", payload)["full_capture"], false);
}

#[test]
fn client_events_are_kept_without_diagnostics_redacted_and_readable_by_the_admin() {
    let place = Place::new("client-events-kept");
    let h = place.start_with_token("fixture-admin");
    let (phone, device) = pair(&h, "Log Phone", "limited");
    let post = |items: Value| {
        let response = fetch(
            &h,
            &phone,
            "POST",
            "/clog",
            &[
                ("content-type", "application/json"),
                ("x-clarp-client", "iOS/2990 contract=80"),
            ],
            &serde_json::to_vec(&items).unwrap(),
        );
        assert_eq!(response.status, 200, "{}", response.text());
        serde_json::from_slice::<Value>(&response.body).unwrap()
    };
    let secret = "fixture-secret-that-must-not-be-stored";
    let payload = json!({"events":[
        {"event":"launch","trace_id":"t1","detail":{"nested":{"api key":secret},
            "url":format!("https://fixture.example/x?token={secret}&keep=1")}},
        {"event":"fatalError","level":"fault",
         "detail":"Bearer fixturesecretvalue cld_fixturetoken clp_fixturecode"},
        {"event":"large","detail":"é".repeat(4200)}
    ]});
    assert_eq!(
        post(payload),
        json!({"ok":true,"captured":true,"kept":3,"full_capture":false})
    );
    let rows = h.get(&format!("/client-log?device={device}&since=30m"));
    assert_eq!(rows["count"], 3);
    let events = rows["events"].as_array().unwrap();
    assert_eq!(events[0]["trace_id"], "t1");
    assert_eq!(events[0]["device"], device);
    assert_eq!(events[0]["client"], "ios");
    assert_eq!(events[0]["client_build"], "2990");
    assert_eq!(events[0]["client_contract"], 80);
    assert_eq!(events[0]["kept"], true);
    assert_eq!(events[1]["kept"], true);
    assert_eq!(events[0]["detail"]["nested"]["api key"], "[redacted]");
    assert_eq!(
        events[0]["detail"]["url"],
        "https://fixture.example/x?token=[redacted]&keep=1"
    );
    let error = events[1]["detail"].as_str().unwrap();
    for gone in ["fixturesecretvalue", "fixturetoken", "fixturecode"] {
        assert!(!error.contains(gone));
    }
    let large = events[2]["detail"].as_str().unwrap();
    assert!(large.len() <= 4096 && large.ends_with("…[truncated]"));
    let (status, _) = h.ask_as(&phone, "GET", "/client-log", None);
    assert_eq!(status, 403);
    assert_eq!(h.ask("GET", "/client-log?since=yesterday", None).0, 400);
    assert_eq!(h.get("/client-log?limit=1")["events"][0]["event"], "large");

    assert_eq!(
        h.post(
            "/crash",
            json!({
                "crashDiagnostics": [{"authorization":format!("Bearer {secret}")}]
            })
        ),
        json!({"ok":true})
    );
    let reports = h.get("/client-log?event=metrickit_report");
    let report = &reports["events"][0];
    assert_eq!(report["kept"], true);
    assert_eq!(report["detail"]["crashes"], 1);
    let path = place
        .root
        .join("data/crashes")
        .join(report["detail"]["file"].as_str().unwrap());
    let stored = std::fs::read_to_string(path).unwrap();
    assert!(!stored.contains(secret));

    let answered = h.post(
        "/tools/run",
        json!({
            "tool":"clarp-admin","argv":["client-log","--device",device,"--level","error"]
        }),
    );
    assert_eq!(answered["exit"], 0, "{answered}");
    let lines = answered["stdout"]
        .as_str()
        .unwrap()
        .lines()
        .collect::<Vec<_>>();
    assert_eq!(lines.len(), 1, "{answered}");
    assert_eq!(
        serde_json::from_str::<Value>(lines[0]).unwrap()["event"],
        "fatalError"
    );

    h.kill();
    let h = place.start_with_token("fixture-admin");
    assert_eq!(h.get("/client-log?event=fatalError")["count"], 1);
}
