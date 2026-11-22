//! V2b creates and assigns voices at the same serialized fleet write.
use super::*;

#[test]
fn the_voice_pool_fills_new_rows_and_keeps_old_roster_voices() {
    let place = Place::new("voice-pool");
    let owners = place.root.join("synthetic-voice.toml");
    let choose = |pool: &str, primary: &str, fallback: &str| {
        std::fs::write(&owners, format!(
            "[tts]\nprovider='{primary}'\nfallback='{fallback}'\n[cartesia]\npool={pool}\n[cartesia.voices]\nAda='c-ada'\n"
        )).unwrap();
    };
    choose("['c1','c2']", "cartesia", "elevenlabs");
    let home = place.root.to_str().unwrap();
    let host = place.start_in(
        "",
        &[
            "--accounts",
            "none",
            "--speech-keys",
            owners.to_str().unwrap(),
        ],
        &[
            ("HOME", home),
            ("XDG_CONFIG_HOME", home),
            ("XDG_DATA_HOME", home),
            ("XDG_CACHE_HOME", home),
            ("XDG_STATE_HOME", home),
            ("CARTESIA_API_KEY", ""),
            ("ELEVEN_API_KEY", ""),
            ("GEMINI_API_KEY", ""),
            ("GOOGLE_APPLICATION_CREDENTIALS", ""),
        ],
    );
    let create = |name: &str, voice: &str| {
        host.post(
            "/agents",
            json!({"name":name,"backend":"scripted","voice_id":voice}),
        )
    };
    let stored =
        |row: &Value| serde_json::from_str::<Value>(row["voice_id"].as_str().unwrap()).unwrap();
    let snapshot = || host.get("/agents/snapshot");
    let identify = |session: &str| {
        snapshot()["agents"]
            .as_array()
            .unwrap()
            .iter()
            .find(|row| row["session"] == session)
            .unwrap()
            .clone()
    };

    // A contact and an agent created at once share the same reservation/write owner.
    let (agent, contact) = std::thread::scope(|scope| {
        let a = scope.spawn(|| create("Pool Agent", r#"{"cartesia":"auto-123"}"#));
        let b = scope.spawn(|| {
            let (status, said) = host.ask(
                "POST",
                "/personas",
                Some(&json!({"name":"Pool Contact","generate_avatar":false})),
            );
            assert_eq!(status, 201, "{said}");
            said["persona"].clone()
        });
        (a.join().unwrap(), b.join().unwrap())
    });
    let agent_voice = stored(&agent["agent"]);
    let contact_voice = stored(&contact);
    let agent_cartesia = agent_voice["cartesia"].as_str().unwrap();
    let contact_cartesia = contact_voice["cartesia"].as_str().unwrap();
    assert_ne!(agent_cartesia, contact_cartesia);
    assert!(["c1", "c2"].contains(&agent_cartesia));
    assert!(["c1", "c2"].contains(&contact_cartesia));
    assert!(agent_voice["elevenlabs"].is_string() && contact_voice["elevenlabs"].is_string());
    assert!(agent["agent"]["voice_profile"]["speech"]["voice_id"].is_null());
    assert_eq!(
        agent["agent"]["voice_profile"]["speech"]["silent_reason"],
        "not_configured"
    );
    assert_eq!(snapshot()["voice_defaults"]["speech"]["assignment"], "pool");

    let forced = json!({"cartesia":agent_cartesia}).to_string();
    let (status, refusal) = host.ask(
        "POST",
        "/agents",
        Some(&json!({"name":"Pinned Duplicate","backend":"scripted","voice_id":forced})),
    );
    let vectors: Value = serde_json::from_str(include_str!(
        "../../../clarp-kernel/tests/fixtures/voice-pool-vectors.json"
    ))
    .unwrap();
    let collision = &vectors["creation_collisions"][agent_cartesia];
    assert_eq!(json!(status), collision["status"]);
    assert_eq!(refusal, collision["body"]);

    let shared = create("Auto Share", "{}");
    assert!(["c1", "c2"].contains(&stored(&shared["agent"])["cartesia"].as_str().unwrap()));

    let session = agent["session"].as_str().unwrap();
    let helper = host.post(
        "/agents",
        json!({"name":"Pool Helper","backend":"scripted",
        "role":"helper","parent":session,"voice_id":"{}"}),
    );
    assert_eq!(stored(&helper["agent"]), agent_voice);
    let uuid = "f0000000-0000-4000-8000-000000000000";
    let pinned = create("Canonical Voice", uuid);
    assert_eq!(stored(&pinned["agent"])["cartesia"], uuid);
    let janitors = snapshot()["agents"].as_array().unwrap().clone();
    assert!(janitors.iter().any(|row| row["is_janitor"] == true));
    for row in janitors.iter().filter(|row| row["is_janitor"] == true) {
        assert_eq!(
            row["voice_profile"]["speech"]["silent_reason"],
            "voiceless_role"
        );
        assert!(row["voice_profile"]["speech"]["voice_id"].is_null());
    }

    choose("[]", "cartesia", "none");
    let ordinary = create("Empty Configured Pool", "{}");
    let picked = stored(&ordinary["agent"]);
    assert!(
        clarp_kernel::voice_pool::curated_names()
            .contains_key(picked["cartesia"].as_str().unwrap())
    );

    let ada = create("Ada", "{}");
    let ada = ada["session"].as_str().unwrap();
    assert_eq!(identify(ada)["voice_id"], "{}");
    host.post(
        "/agent-rename",
        json!({"session":ada,"name":"Different Name"}),
    );
    assert_eq!(stored(&identify(ada))["cartesia"], "c-ada");
    assert_eq!(
        stored(&identify(session)),
        agent_voice,
        "new assignments leave existing agents alone"
    );

    host.post(
        "/agent-assign",
        json!({"session":session,"mode":"choose","name":"Pool Contact"}),
    );
    assert_eq!(stored(&identify(session)), contact_voice);
}
