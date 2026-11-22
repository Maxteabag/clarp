use super::*;

#[test]
fn voice_edits_merge_and_migration_requires_the_current_review() {
    let place = Place::new("voice-maps");
    let owners = place.root.join("silent-voice.toml");
    std::fs::write(&owners, "[tts]\nprovider='none'\n").unwrap();
    let start = || place.start_with("", &["--speech-keys", owners.to_str().unwrap()]);
    let host = start();
    let uuid = "f0000000-0000-4000-8000-000000000000";
    let create = |name, voice| {
        host.post(
            "/agents",
            json!({
                "name":name, "backend":"scripted", "voice_id":voice, "avatar_symbol":"X",
            }),
        )
    };
    let first = create("First Voice", "old-voice");
    let risky = create("Risky Voice", uuid);
    let session = first["session"].as_str().unwrap();
    let risky_session = risky["session"].as_str().unwrap();
    assert_eq!(
        risky["agent"]["voice_id"],
        json!({"cartesia": uuid}).to_string()
    );
    drop(host);
    // Supply the legacy shapes a Host before 135 could store.
    {
        use clarp_ports::kv::KeyValueEngine as _;
        let mut store = clarp_store_sqlite::SqliteKeyValue
            .open(&place.root.join("data/fleet.db"))
            .unwrap();
        let records = store.list("agent").unwrap();
        for (session, legacy) in [(session, "old-voice"), (risky_session, uuid)] {
            let (id, mut record) = records
                .iter()
                .map(|(id, bytes)| (id, serde_json::from_slice::<Value>(bytes).unwrap()))
                .find(|(_, row)| row["session"] == session)
                .unwrap();
            record["voice_id"] = json!(legacy);
            store
                .put("agent", id, &serde_json::to_vec(&record).unwrap())
                .unwrap();
        }
    }
    let host = start();
    let run = |argv| host.post("/tools/run", json!({"tool":"clarp-admin","argv":argv}));
    let dry = run(json!(["voice", "migrate", "--json"]));
    assert_eq!(dry["exit"], 0, "{dry}");
    let report: Value = serde_json::from_str(dry["stdout"].as_str().unwrap()).unwrap();
    let voices = report["voices"].as_array().unwrap();
    assert!(!voices.iter().any(|row| row["session"] == session));
    assert!(
        voices
            .iter()
            .any(|row| row["session"] == risky_session && row["audible"] == true)
    );
    let digest = report["digest"].as_str().unwrap();
    assert_eq!(
        run(json!(["voice", "migrate", "--apply", "stale"]))["exit"],
        3
    );
    let applied = run(json!(["voice", "migrate", "--apply", digest, "--json"]));
    assert_eq!(applied["exit"], 0, "{applied}");
    let read = |session| {
        host.get("/agents/snapshot")["agents"]
            .as_array()
            .unwrap()
            .iter()
            .find(|row| row["session"] == session)
            .unwrap()["voice_id"]
            .clone()
    };
    assert_eq!(read(session), r#"{"elevenlabs":"old-voice"}"#);
    assert_eq!(read(risky_session), uuid);
    host.post("/agent-voice", json!({"session":session,"voice_id":uuid}));
    assert_eq!(
        read(session),
        json!({"cartesia":uuid,"elevenlabs":"old-voice"}).to_string()
    );

    let original = r#"{"cartesia":"old","elevenlabs":"original"}"#;
    assert_eq!(
        host.ask(
            "POST",
            "/personas",
            Some(&json!({
                "name":"Voice Contact", "voice_id":original, "generate_avatar":false,
            }))
        )
        .0,
        201
    );
    let edit = |voice, base| {
        host.post("/personas/update", json!({
        "original_name":"Voice Contact","name":"Voice Contact","voice_id":voice,"base_voice_id":base,
    }))
    };
    edit(r#"{"cartesia":"changed"}"#, Value::Null);
    let later = edit(
        r#"{"cartesia":"old","elevenlabs":"edited"}"#,
        json!(original),
    );
    assert_eq!(
        later["persona"]["voice_id"],
        r#"{"cartesia":"changed","elevenlabs":"edited"}"#
    );
}

#[test]
fn empty_voice_maps_inherit_at_creation_and_fill_when_a_contact_is_edited() {
    let place = Place::new("empty-voice-map");
    let owners = place.root.join("voice-pool.toml");
    std::fs::write(
        &owners,
        "[tts]\nprovider='cartesia'\nfallback='none'\n[cartesia]\npool=['p1','p2']\n",
    )
    .unwrap();
    let start = || place.start_with("", &["--speech-keys", owners.to_str().unwrap()]);
    let host = start();
    let voice = r#"{"cartesia":"contact-voice"}"#;
    let (status, _) = host.ask(
        "POST",
        "/personas",
        Some(&json!({"name":"Voice Contact","voice_id":voice,"generate_avatar":false})),
    );
    assert_eq!(status, 201);
    let made = host.post(
        "/agents",
        json!({"name":"Voice Contact","backend":"scripted","voice_id":"{}"}),
    );
    assert_eq!(made["agent"]["voice_id"], voice);
    let relaunched = host.post(
        "/agents",
        json!({"name":"Voice Contact","backend":"scripted","replace_sid":made["session"],"voice_id":"{}"}),
    );
    assert_eq!(relaunched["agent"]["voice_id"], voice);
    drop(host);

    {
        use clarp_ports::kv::KeyValueEngine as _;
        let mut store = clarp_store_sqlite::SqliteKeyValue
            .open(&place.root.join("data/fleet.db"))
            .unwrap();
        let (id, mut contact) = store
            .list("persona")
            .unwrap()
            .into_iter()
            .map(|(id, bytes)| (id, serde_json::from_slice::<Value>(&bytes).unwrap()))
            .find(|(_, row)| row["name"] == "Voice Contact")
            .unwrap();
        contact["voice_id"] = json!("{}");
        store
            .put("persona", &id, &serde_json::to_vec(&contact).unwrap())
            .unwrap();
    }
    let host = start();
    let edited = host.post(
        "/personas/update",
        json!({"original_name":"Voice Contact","name":"Voice Contact",
               "voice_id":"{\"gemini\":\"Kore\"}"}),
    );
    let voices: Value =
        serde_json::from_str(edited["persona"]["voice_id"].as_str().unwrap()).unwrap();
    assert_eq!(voices["gemini"], "Kore");
    assert!(matches!(voices["cartesia"].as_str(), Some("p1" | "p2")));
}
