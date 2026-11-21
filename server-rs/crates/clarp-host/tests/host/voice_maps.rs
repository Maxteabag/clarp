use super::*;

#[test]
fn voice_edits_merge_and_migration_requires_the_current_review() {
    let place = Place::new("voice-maps");
    let owners = place.root.join("silent-voice.toml");
    std::fs::write(&owners, "[tts]\nprovider='none'\n").unwrap();
    let host = place.start_with("", &["--speech-keys", owners.to_str().unwrap()]);
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
    let run = |argv| host.post("/tools/run", json!({"tool":"clarp-admin","argv":argv}));
    let dry = run(json!(["voice", "migrate", "--json"]));
    assert_eq!(dry["exit"], 0, "{dry}");
    let report: Value = serde_json::from_str(dry["stdout"].as_str().unwrap()).unwrap();
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
    assert_eq!(read(risky["session"].as_str().unwrap()), uuid);
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
