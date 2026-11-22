use super::*;

#[test]
fn imports_only_owner_settings_and_dry_run_changes_nothing() {
    use clarp_ports::kv::KeyValueEngine as _;

    let place = Place::new("import-settings");
    let source = place.root.join("python state?.sqlite");
    let fixture = rusqlite::Connection::open(&source).unwrap();
    fixture.execute_batch(
        "CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at INTEGER NOT NULL);",
    ).unwrap();
    let rows = [
        ("transcription.engine", "cartesia"),
        ("transcription.retain_audio", "true"),
        ("transcription.agent_languages", r#"{"fixture":"nb"}"#),
        (
            "transcription.guidance.glossary",
            "fixture words to keep private",
        ),
        ("herald.disabled", "false"),
        ("oracle.contact_session", "fixture-session"),
        ("heartbeat.interval_sec", "600"),
        ("heartbeat.backoff_strategy", "exponential"),
        ("heartbeat.backoff_cap_sec", "5100"),
        ("heartbeat.dormant_after_noops", "13"),
        ("heartbeat.janitor_adopted", "runtime-only"),
        ("heartbeat.accounting_activation_ms", "runtime-only"),
        ("orchestrator.addressing_mode", "first_name"),
        ("orchestrator.enabled", "true"),
        ("orchestrator.model", "discard-old-model"),
        ("transcription.unknown_future_key", "leave out"),
        ("tool_explanations.identity_key", "leave out"),
        ("server_instance_id", "leave out"),
        ("quota-keeper.window.claude", "leave out"),
        ("dreaming.enabled", "leave out"),
    ];
    for (key, value) in rows {
        fixture
            .execute("INSERT INTO settings VALUES (?1, ?2, 0)", [key, value])
            .unwrap();
    }
    drop(fixture);
    let before = std::fs::read(&source).unwrap();

    let data = place.root.join("data");
    let fleet = data.join("fleet.db");
    let store = clarp_store_sqlite::SqliteKeyValue::new();
    let mut kept = store.open(&fleet).unwrap();
    kept.put("setting", "transcription.engine", b"old-rust-engine")
        .unwrap();
    kept.put("setting", "herald.disabled", b"false").unwrap();
    kept.put("setting", "server_id", b"rust-identity").unwrap();
    let heartbeat = json!({
        "settings": {"interval_sec":900,"backoff_strategy":"exponential","backoff_cap_sec":6000,"dormant_after_noops":3,"extension":true},
        "counted":{"fixture":"receipt-already-in-rust"},
        "agents":{},
    });
    let heartbeat = serde_json::to_vec(&heartbeat).unwrap();
    kept.put("kept.heartbeat", "all", &heartbeat).unwrap();
    let before_rust = kept.list("setting").unwrap();
    drop(kept);

    let run = |dry: bool| {
        let mut command = place.host_command();
        command
            .args(["import", "--settings", "--from-db"])
            .arg(&source)
            .arg("--data")
            .arg(&data);
        if dry {
            command.arg("--dry-run");
        }
        let output = command.ended();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        assert!(output.stderr.is_empty());
        let receipt = String::from_utf8(output.stdout).unwrap();
        assert!(!receipt.contains("fixture words to keep private"));
        receipt
    };
    let preview = run(true);
    assert!(preview.contains("transcription.engine\tcopied\n"));
    assert!(preview.contains("herald.disabled\tequal\n"));
    assert!(preview.contains("heartbeat.interval_sec\tcopied\n"));
    assert!(preview.contains("heartbeat.backoff_strategy\tequal\n"));
    assert!(preview.contains("orchestrator.addressing_mode\tcopied\n"));
    assert!(preview.contains("orchestrator.enabled\tcarried by janitor configs\n"));
    assert!(preview.contains("orchestrator.model\tcarried by janitor configs\n"));
    assert!(!preview.contains("discard-old-model"));
    for key in [
        "transcription.unknown_future_key",
        "tool_explanations.identity_key",
        "server_instance_id",
        "quota-keeper.window.claude",
        "heartbeat.janitor_adopted",
        "heartbeat.accounting_activation_ms",
        "dreaming.enabled",
    ] {
        assert!(preview.contains(&format!("{key}\tno reader\n")), "{key}");
    }
    assert_eq!(preview.lines().count(), rows.len());
    assert_eq!(
        store.open(&fleet).unwrap().list("setting").unwrap(),
        before_rust
    );

    assert_eq!(
        store
            .open(&fleet)
            .unwrap()
            .get("kept.heartbeat", "all")
            .unwrap(),
        Some(heartbeat)
    );
    assert_eq!(run(false), preview);
    let kept = store.open(&fleet).unwrap();
    assert_eq!(
        kept.get("setting", "server_id").unwrap().as_deref(),
        Some(b"rust-identity".as_slice())
    );
    assert_eq!(
        kept.get("setting", "transcription.engine")
            .unwrap()
            .as_deref(),
        Some(b"cartesia".as_slice())
    );
    assert_eq!(
        kept.get("setting", "transcription.retain_audio")
            .unwrap()
            .as_deref(),
        Some(b"true".as_slice())
    );
    assert_eq!(
        kept.get("setting", "transcription.agent_languages")
            .unwrap()
            .as_deref(),
        Some(br#"{"fixture":"nb"}"#.as_slice())
    );
    assert_eq!(
        kept.get("setting", "transcription.guidance.glossary")
            .unwrap()
            .as_deref(),
        Some(b"fixture words to keep private".as_slice())
    );
    assert_eq!(kept.list("setting").unwrap().len(), 8);
    assert_eq!(
        kept.get("setting", "orchestrator.addressing_mode")
            .unwrap()
            .as_deref(),
        Some(b"first_name".as_slice())
    );
    let policy: Value =
        serde_json::from_slice(&kept.get("kept.heartbeat", "all").unwrap().unwrap()).unwrap();
    assert_eq!(
        policy,
        json!({
            "settings": {"interval_sec":600,"backoff_strategy":"exponential","backoff_cap_sec":5100,"dormant_after_noops":13,"extension":true},
            "counted":{"fixture":"receipt-already-in-rust"},
            "agents":{},
        })
    );
    drop(kept);

    let again = run(false);
    assert_eq!(
        again
            .lines()
            .filter(|line| line.ends_with("\tequal"))
            .count(),
        11
    );
    assert!(!again.lines().any(|line| line.ends_with("\tcopied")));
    assert_eq!(std::fs::read(&source).unwrap(), before);
    assert!(!place.root.join("python state?.sqlite-shm").exists());
}
