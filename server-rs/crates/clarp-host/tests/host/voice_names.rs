//! PR 498 voice labels and hearing settings at the profile's public boundaries.
use super::*;

#[test]
fn voice_names_and_configured_languages_are_projected_from_private_state() {
    let place = Place::new("voice-names");
    let names = place.root.join("data/voice-names.json");
    std::fs::create_dir_all(names.parent().unwrap()).unwrap();
    let first_names =
        br#"{"cartesia":{"c-fixture":"First"},"elevenlabs":{"e-fixture":"From the file"}}"#;
    std::fs::write(&names, first_names).unwrap();
    let keys = place.root.join("synthetic-voice.toml");
    std::fs::write(
        &keys,
        r#"
[tts]
provider = "cartesia"
[tts.agents.Scriber]
provider = "elevenlabs"
[tts.agents.Design]
provider = "gemini"
[cartesia]
api_key = "fixture"
[elevenlabs]
api_key = "fixture"
[gemini_tts]
api_key = "fixture"
[gemini_tts.voices]
Fixture = "voice_fixture_design"
[gemini_tts.vertex_voices]
Fixture = "voice_fixture_vertex"
"#,
    )
    .unwrap();
    let (base, asked) = answers(vec![("/", "503 No Network", String::new())]);
    let cartesia = format!("{base}/tts/bytes");
    let home = place.root.to_str().unwrap();
    let start = || {
        place.start_in(
            "",
            &[
                "--accounts",
                "none",
                "--speech-keys",
                keys.to_str().unwrap(),
            ],
            &[
                ("HOME", home),
                ("XDG_CONFIG_HOME", home),
                ("XDG_DATA_HOME", home),
                ("XDG_CACHE_HOME", home),
                ("XDG_STATE_HOME", home),
                ("CLARP_CARTESIA_ENDPOINT", &cartesia),
                ("CLARP_ELEVEN_VOICE_ENDPOINT", &base),
                ("CLARP_GEMINI_ENDPOINT", &base),
                ("DEEPGRAM_API_KEY", ""),
                ("GOOGLE_APPLICATION_CREDENTIALS", ""),
            ],
        )
    };
    let host = start();
    let events = host.listen();
    let make = |name, provider, voice| {
        let stored = json!({(provider):voice}).to_string();
        host.post(
            "/agents",
            json!({"name":name,"backend":"scripted","voice_id":stored}),
        )
    };
    let cart = make("Namer", "cartesia", "c-fixture");
    let eleven = make("Scriber", "elevenlabs", "e-fixture");
    let gemini = make("Design", "gemini", "voice_fixture_vertex");
    for (created, label) in [
        (&cart, "First"),
        (&eleven, "From the file"),
        (&gemini, "Fixture (designed)"),
    ] {
        assert_eq!(
            created["agent"]["voice_profile"]["speech"]["voice_label"],
            label
        );
        let session = created["session"].as_str().unwrap();
        let roster = || {
            events
                .lock()
                .unwrap()
                .iter()
                .find(|event| {
                    event["type"] == "agent-roster"
                        && event["kind"] == "created"
                        && event["session"] == session
                })
                .cloned()
        };
        wait_for("voice profile creation event", || roster().is_some());
        assert_eq!(
            roster().unwrap()["voice_profile"]["speech"]["voice_label"],
            label
        );
    }
    let named = cart["session"].as_str().unwrap().to_string();
    let silent = host.new_agent("Unvoiced");
    host.post(
        "/transcription-providers",
        json!({
            "agent_engines":{"namer":"elevenlabs:scribe_v2"},
            "agent_languages":{"namer":"no,en"},
            "language":"pt",
        }),
    );
    let row = |shot: &Value, session: &str| {
        shot["agents"]
            .as_array()
            .unwrap()
            .iter()
            .find(|item| item["session"] == session)
            .unwrap()
            .clone()
    };
    let initial = host.get("/agents/snapshot");
    let shown = row(&initial, &named);
    let hearing = &shown["voice_profile"]["hearing"];
    assert_eq!(hearing["configured_languages"], json!(["no", "en"]));
    assert_eq!(hearing["languages"], json!([]));
    assert_eq!(hearing["language_origin"], "own");
    assert_eq!(
        initial["voice_defaults"]["hearing"]["configured_languages"],
        json!(["pt"])
    );
    let quiet = row(&initial, &silent);
    assert!(quiet["voice_profile"]["speech"]["voice_id"].is_null());
    assert!(quiet["voice_profile"]["speech"]["origin"].is_null());
    assert!(quiet["voice_profile"]["speech"]["silent_reason"].is_string());
    assert!(
        asked.lock().unwrap().is_empty(),
        "profile reads stay off the network"
    );

    let current_version = initial["voice_profile_version"].clone();
    host.kill();
    std::fs::write(&names, first_names).unwrap();
    let host = start();
    assert_eq!(
        host.get("/agents/snapshot")["voice_profile_version"],
        current_version
    );
    host.kill();
    std::fs::write(
        &names,
        br#"{"cartesia":{"c-fixture":"Later"},"elevenlabs":{"e-fixture":"From the file"}}"#,
    )
    .unwrap();
    let host = start();
    let later = host.get("/agents/snapshot");
    assert_ne!(later["voice_profile_version"], current_version);
    assert_eq!(
        row(&later, &named)["voice_profile"]["speech"]["voice_label"],
        "Later"
    );

    let events = host.listen();
    host.post("/agent-archive", json!({"session":named,"archived":true}));
    host.post(
        "/agent-rename",
        json!({"session":named,"name":"Archived Name"}),
    );
    let renamed = || {
        events
            .lock()
            .unwrap()
            .iter()
            .find(|event| {
                event["type"] == "agent-roster"
                    && event["kind"] == "agent-renamed"
                    && event["session"] == named
            })
            .cloned()
    };
    wait_for("archived rename event", || renamed().is_some());
    assert!(renamed().unwrap().get("voice_profile").is_none());
    assert!(
        row(&host.get("/agents/snapshot"), &named)
            .get("voice_profile")
            .is_none()
    );
    assert!(asked.lock().unwrap().is_empty());
}
