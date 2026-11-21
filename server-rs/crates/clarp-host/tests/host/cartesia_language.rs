//! R168 at the speech, fallback and cache boundaries with loopback speakers.

use super::*;

#[test]
fn every_agent_cartesia_request_uses_its_first_supported_language() {
    let place = Place::new("cartesia-language");
    let cartesia = cartesia_stand_in(vec![19; 2000]);
    let (eleven, failures) = answers(vec![("/", "503 No Audio", String::new())]);
    let owners = place.root.join("synthetic-voice.toml");
    let configure = |model: &str, primary: &str, fallback: &str| {
        std::fs::write(&owners, format!(
            "[tts]\nprovider='{primary}'\nfallback='{fallback}'\n[cartesia]\napi_key='fixture'\nmodel='{model}'\n[elevenlabs]\napi_key='fixture'\n[cartesia.voices]\nRachel='fixture-narrator'\n"
        )).unwrap();
    };
    let home = place.root.to_str().unwrap();
    let start = || {
        place.start_in(
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
                ("CLARP_CARTESIA_ENDPOINT", &cartesia.address),
                ("CLARP_ELEVEN_VOICE_ENDPOINT", &eleven),
                ("GOOGLE_APPLICATION_CREDENTIALS", ""),
                ("GEMINI_API_KEY", ""),
                ("DEEPGRAM_API_KEY", ""),
                ("GOOGLE_VERTEX_API_KEY", ""),
            ],
        )
    };

    configure("sonic-3", "cartesia", "none");
    let host = start();
    let voice = json!({"cartesia":"fixture-voice", "elevenlabs":"fixture-eleven"}).to_string();
    let made = host.post(
        "/agents",
        json!({"name":"Language Tester","backend":"scripted","voice_id":voice}),
    );
    let session = made["session"].as_str().unwrap();
    let set = |host: &Host, default, own: Value| {
        host.post(
            "/transcription-providers",
            json!({"language": default, "agent_languages":{"Language Tester": own}}),
        );
    };
    let requested = |text: &str| {
        cartesia.asked().into_iter().find(|row| {
            row["transcript"]
                .as_str()
                .is_some_and(|spoken| spoken.contains(text))
        })
    };
    let speak = |host: &Host, text: &str, language: &str| {
        host.send(
            session,
            &format!("<speak>{text}</speak>"),
            &text.replace(' ', "-"),
        );
        wait_for(text, || requested(text).is_some());
        let sent = requested(text).unwrap();
        assert_eq!(sent["language"], language, "{sent}");
        assert_eq!(sent["voice"]["id"], "fixture-voice", "{sent}");
    };

    set(&host, "fr", json!("nb,de"));
    speak(&host, "first mapped reply", "no");
    set(&host, "fr", json!("nn,ja"));
    speak(&host, "second mapped reply", "no");
    set(&host, "fr", Value::Null);
    speak(&host, "host default reply", "fr");

    set(&host, "fr", json!("nb,de"));
    configure("sonic-2", "cartesia", "none");
    speak(&host, "narrow model reply", "en");
    let warning = "cartesiaLanguageUnsupported no on sonic-2 -> en";
    wait_for("one fallback log", || {
        host.heard.lock().unwrap().contains(warning)
    });
    assert_eq!(host.heard.lock().unwrap().matches(warning).count(), 1);

    configure("sonic-3", "elevenlabs", "cartesia");
    speak(&host, "eleven fallback reply", "no");
    assert_eq!(failures.lock().unwrap().len(), 1);
    assert!(
        failures.lock().unwrap()[0]
            .starts_with("GET /v1/text-to-speech/fixture-eleven/stream-input")
    );

    // Agent-scoped free-text preview is another direct caller of the Cartesia adapter.
    configure("sonic-latest", "cartesia", "none");
    set(&host, "fr", json!("or,en"));
    host.post("/preview", json!({"session":session,"voice_id":r#"{"cartesia":"fixture-voice"}"#,"text":"free preview language"}));
    assert_eq!(
        requested("free preview language").unwrap()["language"],
        "or"
    );

    // The narration bytes are a language-sensitive cache, including on a settings change.
    let narration = || {
        fetch(
            &host,
            "",
            "GET",
            "/controller-narration?text=Language%20cache",
            &[],
            &[],
        )
    };
    let initial = cartesia.asked().len();
    assert_eq!(narration().status, 200);
    assert_eq!(cartesia.asked().last().unwrap()["language"], "fr");
    assert_eq!(narration().status, 200);
    assert_eq!(cartesia.asked().len(), initial + 1);
    set(&host, "no", json!("or,en"));
    assert_eq!(narration().status, 200);
    assert_eq!(cartesia.asked().last().unwrap()["language"], "no");
    assert_eq!(cartesia.asked().len(), initial + 2);

    host.kill();
    {
        use clarp_ports::kv::KeyValueEngine as _;
        let mut store = clarp_store_sqlite::SqliteKeyValue
            .open(&place.root.join("data/fleet.db"))
            .unwrap();
        store
            .put("setting", "transcription.agent_languages", b"{")
            .unwrap();
        store
            .put("setting", "transcription.language", b"invalid")
            .unwrap();
    }
    let host = start();
    speak(&host, "unreadable setting reply", "en");
}

#[test]
fn cached_router_phrases_follow_the_effective_cartesia_language() {
    let place = Place::new("cartesia-phrase-language");
    let words = "I do not have an agent by that name.";
    let rules = json!([["fixed", {"kind":"clarify","confidence":0.95,"spoken_text":words,"phrase_key":"repeat_missing"}]]);
    let (_, script, _) = routing_model(&place, &rules);
    let cartesia = cartesia_stand_in(vec![19; 2000]);
    let owners = place.root.join("synthetic-voice.toml");
    std::fs::write(&owners, "[tts]\nprovider='cartesia'\n[cartesia]\napi_key='fixture'\nmodel='sonic-3'\n[tts.agents.mike]\ncartesia_voice='fixture'\n[tts.agents.clarp]\ncartesia_voice='fixture'\n").unwrap();
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
            ("CLARP_CLAUDE_PROGRAM", script.to_str().unwrap()),
            ("CLARP_CARTESIA_ENDPOINT", &cartesia.address),
            ("GOOGLE_APPLICATION_CREDENTIALS", ""),
            ("DEEPGRAM_API_KEY", ""),
            ("ELEVEN_API_KEY", ""),
        ],
    );
    let session = host.new_agent("Mike");
    host.post(
        "/orchestrator/settings",
        json!({"enabled":true,"fallback_only":false,"provider":"claude"}),
    );
    let press = |id: &str| {
        host.post(
            "/send",
            json!({"session":session,"text":"fixed please","hands_free":true,"client_msg_id":id}),
        );
    };
    for (language, fill, retry, count) in [
        ("en", "en-fill", "en-hit", 1),
        ("no", "no-fill", "no-hit", 2),
    ] {
        host.post("/transcription-providers", json!({"language":language}));
        press(fill);
        assert_eq!(cartesia.asked().len(), count);
        assert_eq!(cartesia.asked().last().unwrap()["language"], language);
        press(retry);
        assert_eq!(cartesia.asked().len(), count);
    }
}
