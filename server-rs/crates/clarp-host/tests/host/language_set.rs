//! R152 engine admission and fallback over the Host's HTTP and provider boundaries.

use serde_json::{Value, json};

use super::{Host, Place, WHISPER_STAND_IN, fetch, scribe_stand_in, wait_for};

#[test]
fn a_language_set_is_heard_only_by_engines_the_owner_chose() {
    const SCRIBE: &str = "elevenlabs:scribe_v2";
    const NOVA: &str = "deepgram:nova-3";
    let place = Place::new("language-set");
    let worker = place.root.join("whisper-stand-in.sh");
    std::fs::write(&worker, WHISPER_STAND_IN).unwrap();
    let (scribe, heard) = scribe_stand_in(r#"{"text":"Scribe heard it.","words":[]}"#);
    let (nova, nova_calls) = scribe_stand_in(
        r#"{"results":{"channels":[{"alternatives":[{"transcript":"Nova heard it."}]}]}}"#,
    );
    fn world<'a>(
        place: &'a Place,
        scribe: &'a str,
        nova: &'a str,
    ) -> [(&'static str, &'a str); 12] {
        let home = place.root.to_str().unwrap();
        [
            ("HOME", home),
            ("XDG_CONFIG_HOME", home),
            ("XDG_DATA_HOME", home),
            ("XDG_CACHE_HOME", home),
            ("XDG_STATE_HOME", home),
            ("TMPDIR", home),
            ("CLARP_SCRIBE_ENDPOINT", scribe),
            ("CLARP_NOVA_ENDPOINT", nova),
            ("ELEVEN_API_KEY", "stand-in-scribe"),
            ("DEEPGRAM_API_KEY", "stand-in-nova"),
            ("CARTESIA_API_KEY", ""),
            ("GOOGLE_APPLICATION_CREDENTIALS", ""),
        ]
    }
    let host = place.start_in(
        "",
        &[
            "--accounts",
            "none",
            "--whisper-python",
            "sh",
            "--whisper-worker",
            worker.to_str().unwrap(),
            "--whisper-model",
            "small",
        ],
        &world(&place, &scribe, &nova),
    );
    let session = host.new_agent("Language Tester");
    let configure = |host: &Host, agent: &str, chosen, language, chain: &str| {
        host.post(
            "/transcription-providers",
            json!({
                "engine": chosen,
                "agent_languages": { (agent): language },
                "agent_engines": { (agent): chain }
            }),
        );
    };
    let transcribe = |host: &Host, agent: &str, model: &str, id: &str, clip: &[u8]| {
        let reply = fetch(
            host,
            "",
            "POST",
            "/transcribe",
            &[
                ("X-Transcription-Session", agent),
                ("X-Transcription-Model", model),
                ("X-Transcription-ID", id),
            ],
            clip,
        );
        (
            reply.status,
            serde_json::from_slice::<Value>(&reply.body).unwrap(),
        )
    };
    let receipt =
        |to| json!({"from": NOVA, "to": to, "reason": "language_set", "languages": "detected"});

    configure(&host, &session, SCRIBE, "no,en", "");
    let (status, first) = transcribe(&host, &session, NOVA, "hosted-set", b"ordinary audio");
    assert_eq!(status, 200, "{first}");
    assert_eq!(first["text"], "Scribe heard it.");
    assert_eq!(first["engine_fallback"], receipt(SCRIBE));
    let request = heard.lock().unwrap()[0].clone();
    assert!(request.contains("ordinary audio"));
    assert!(!request.contains("name=\"language_code\""));
    let (status, replay) = transcribe(&host, &session, NOVA, "hosted-set", b"ordinary audio");
    assert_eq!(status, 200, "{replay}");
    assert_eq!(replay["cached"], true);
    assert_eq!(replay["trace_id"], first["trace_id"]);
    assert_eq!(replay["engine_fallback"], first["engine_fallback"]);
    assert_eq!(heard.lock().unwrap().len(), 1);
    assert!(nova_calls.lock().unwrap().is_empty());

    let chain = format!("{NOVA},{SCRIBE}");
    configure(&host, &session, NOVA, "no,en", &chain);
    let (status, continued) =
        transcribe(&host, &session, "local", "planned-set", b"ordinary audio");
    assert_eq!(status, 200, "{continued}");
    assert_eq!(continued["text"], "Scribe heard it.");
    assert!(continued.get("engine_fallback").is_none());
    assert_eq!(heard.lock().unwrap().len(), 2);

    configure(&host, &session, NOVA, "no,en", "");
    wait_for("the own engine to hear the language-set fallback", || {
        let (status, local) = transcribe(&host, &session, NOVA, "own-set", b"abc");
        if status == 503 {
            return false;
        }
        assert_eq!(status, 200, "{local}");
        assert_eq!(local["text"], "Open the log of 3 bytes.");
        assert_eq!(local["engine_fallback"], receipt("server-default"));
        true
    });
    let (status, failure) = transcribe(&host, &session, NOVA, "own-broke", b"ab");
    assert_eq!(status, 422, "{failure}");
    assert_eq!(
        failure["reasons"],
        json!(["the Host's own engine could not hear it"])
    );
    assert!(!failure.to_string().contains("synthetic failure"));
    assert!(nova_calls.lock().unwrap().is_empty());
    assert_eq!(
        heard.lock().unwrap().len(),
        2,
        "another provider's key is not permission"
    );

    configure(&host, &session, SCRIBE, "en", "");
    let (status, ordinary) = transcribe(&host, &session, NOVA, "one-language", b"ordinary audio");
    assert_eq!(status, 200, "{ordinary}");
    assert_eq!(ordinary["text"], "Nova heard it.");
    assert!(ordinary.get("engine_fallback").is_none());
    assert_eq!(nova_calls.lock().unwrap().len(), 1);

    let failed_place = Place::new("language-set-exhausted");
    let (unavailable, failures) = scribe_stand_in(r#"{"error":"no transcript"}"#);
    let host = failed_place.start_in(
        "",
        &["--accounts", "none"],
        &world(&failed_place, &unavailable, &nova),
    );
    let session = host.new_agent("Language Tester");
    let chain = format!("{SCRIBE},{SCRIBE},{NOVA}");
    configure(&host, &session, SCRIBE, "no,en", &chain);
    let snapshot = host.get("/agents/snapshot");
    let agent = snapshot["agents"]
        .as_array()
        .unwrap()
        .iter()
        .find(|row| row["session"] == session)
        .unwrap();
    let order: Vec<_> = agent["voice_profile"]["hearing"]["engines"]
        .as_array()
        .unwrap()
        .iter()
        .map(|row| row["id"].as_str().unwrap())
        .collect();
    assert_eq!(order, [SCRIBE, NOVA]);
    let (status, refusal) = transcribe(&host, &session, "local", "exhausted", b"ordinary audio");
    assert_eq!(status, 422, "{refusal}");
    assert_eq!(
        refusal,
        json!({
            "error": "language_set_unsupported",
            "message": "deepgram:nova-3 cannot hear this agent's languages (no, en); choose an engine that can: elevenlabs:scribe_v2, google:chirp_3 (elevenlabs:scribe_v2 already failed for this recording; the Host's own engine is off)",
            "engine": NOVA,
            "languages": ["no", "en"],
            "engines": [SCRIBE, "google:chirp_3"],
            "reasons": ["elevenlabs:scribe_v2 already failed for this recording", "the Host's own engine is off"],
        })
    );
    assert_eq!(
        failures.lock().unwrap().len(),
        1,
        "do not send one recording twice"
    );
    assert_eq!(
        nova_calls.lock().unwrap().len(),
        1,
        "Nova rejected the set before contact"
    );

    configure(&host, &session, SCRIBE, "no,en", "");
    let (status, error) = transcribe(&host, &session, NOVA, "hosted-down", b"ordinary audio");
    assert_eq!(status, 500, "{error}");
    assert!(error.get("engine_fallback").is_none());
    assert_eq!(failures.lock().unwrap().len(), 2);
}
