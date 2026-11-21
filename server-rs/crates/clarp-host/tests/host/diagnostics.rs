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
        json!({"ok":true,"captured":false})
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
        json!({"ok":true})
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
    assert_eq!(h.post("/clog", payload)["captured"], false);
}
