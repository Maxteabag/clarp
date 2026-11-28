use super::*;

const MINI: &str = "eleven_realtime_v1_mini";
const MAX: &str = "eleven_realtime_v1_max";
const ROUTE: &str = "/v1/convai/realtime";
const PICKER: &str = "/oracle/voice-provider?provider_models=1";
const LEGACY_PICKER: &str = "/oracle/voice-provider";

fn elevenlabs_stand_in() -> (String, std::sync::mpsc::Receiver<(Value, End)>) {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let address = format!("http://{}", listener.local_addr().unwrap());
    let (ready, calls) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        for model in [MINI, MAX, MAX] {
            let (mut mint, _) = listener.accept().unwrap();
            mint.set_read_timeout(Some(Duration::from_secs(10)))
                .unwrap();
            let request = whole_request(&mut mint);
            let (head, body) = request.split_once("\r\n\r\n").unwrap();
            assert!(
                head.starts_with(&format!("POST {ROUTE}/client_secrets HTTP/1.1")),
                "{head}"
            );
            assert!(
                head.lines()
                    .any(|line| line.eq_ignore_ascii_case("xi-api-key: fixture-vault-eleven"))
            );
            let body: Value = serde_json::from_str(body).unwrap();
            let response =
                json!({"value": "eleven-ephemeral", "session": {"model": model}}).to_string();
            write!(mint, "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{response}", response.len()).unwrap();
            drop(mint);

            let (stream, _) = listener.accept().unwrap();
            stream
                .set_read_timeout(Some(Duration::from_secs(10)))
                .unwrap();
            #[allow(clippy::result_large_err)]
            let auth = |request: &tungstenite::handshake::server::Request, response| {
                assert_eq!(request.uri().path(), ROUTE);
                assert_eq!(
                    request.headers()["authorization"],
                    "Bearer eleven-ephemeral"
                );
                assert!(request.headers().get("xi-api-key").is_none());
                Ok(response)
            };
            let answers: Answers = Arc::new(move |event| {
                (event["type"] == "session.update")
                    .then(|| json!({"type": "session.updated", "session": {"model": model}}))
            });
            let provider = End::of(tungstenite::accept_hdr(stream, auth).unwrap(), answers);
            ready.send((body, provider)).unwrap();
        }
    });
    (address, calls)
}

fn opted_in(place: &Place) -> PathBuf {
    let path = place.root.join("oracle.toml");
    std::fs::write(&path, "[oracle]\neleven_realtime_enabled = true\n").unwrap();
    path
}

fn committed(provider: &End, item: &str) {
    provider.says(&json!({"type":"input_audio_buffer.speech_started"}));
    provider.says(&json!({"type":"input_audio_buffer.committed","item_id":item}));
}

fn transcribed(provider: &End, item: &str, transcript: &str) {
    provider.says(
        &json!({"type":"conversation.item.input_audio_transcription.completed",
        "item_id":item,"transcript":transcript}),
    );
}

fn says_tool(provider: &End, response: &str, call: &str) {
    provider.says(&json!({"type":"response.created","response":{"id":response}}));
    provider.says(&json!({"type":"response.done","response":{
        "id":response,"status":"completed","output":[{
            "type":"function_call","name":"delegate_to_agent","call_id":call,
            "arguments":"{\"request\":\"Finn den fiktive koden\"}",
        }],
    }}));
}

fn refuses_response(provider: &End, device: &End) {
    provider.says(
        &json!({"type":"error","error":{"code":"invalid_request_error",
        "message":"private upstream diagnostics"}}),
    );
    assert_eq!(
        device.told_of("error", 1)[0],
        json!({"type":"error","error":{
            "message":"Eleven Realtime refused the voice request",
        }})
    );
    assert_eq!(
        device.told_of("session.closed", 1)[0]["reason"],
        "provider_error"
    );
    device.ends();
}

#[test]
fn eleven_realtime_preserves_oracle_audio_routing_and_interruptions() {
    let place = Place::new("oracle-eleven");
    let (address, calls) = elevenlabs_stand_in();
    let router = stand_in_oracle_router();
    let host = place.start_in(
        "admin",
        &["--speech-keys", opted_in(&place).to_str().unwrap()],
        &[
            ("CLARP_ELEVEN_REALTIME_ENDPOINT", &address),
            ("ELEVEN_API_KEY", "fixture-vault-eleven"),
            ("GEMINI_API_KEY", "fixture-gemini"),
            ("OPENAI_API_KEY", "a-key-of-the-test"),
            ("CLARP_ROUTER_ENDPOINT", &router.address),
        ],
    );
    let (phone, _) = pair(&host, "Full phone", "full");
    let select = |model: &str| {
        host.ask(
            "POST",
            PICKER,
            Some(&json!({
                "provider": "elevenlabs", "model": model,
            })),
        )
    };
    assert_eq!(
        host.get(PICKER)["available"],
        json!(["openai", "gemini", "elevenlabs"])
    );
    assert_eq!(
        host.get(LEGACY_PICKER)["available"],
        json!(["openai", "gemini"])
    );
    let call = |model: &str| {
        let (status, saved) = select(model);
        assert_eq!(status, 200, "{saved}");
        assert_eq!(saved["elevenlabs"]["models"], json!([MINI, MAX]));
        let status = host.get("/oracle/status")["v2"].clone();
        assert_eq!(status["model"], model);
        assert_eq!(status["voice_provider"], "elevenlabs");
        assert_eq!(status["available"], true);
        assert_eq!(status["direct_contact_available"], true);
        let legacy = host.get(LEGACY_PICKER);
        assert_eq!(legacy["provider"], "elevenlabs");
        assert_eq!(legacy["available"], json!(["elevenlabs"]));
        assert!(legacy.get("elevenlabs").is_none());
        let device = End::of_a_phone(socket_at(&host, &phone, "/oracle/v2").unwrap());
        let (mint, provider) = calls.recv_timeout(Duration::from_secs(10)).unwrap();
        assert_eq!(mint["session"]["model"], model);
        assert_eq!(mint["session"]["audio"]["input"]["format"]["rate"], 24000);
        assert_eq!(
            mint["session"]["audio"]["input"]["turn_detection"]["create_response"],
            true
        );
        assert_eq!(mint["session"]["tools"][0]["name"], "delegate_to_agent");
        let setup = &provider.told_of("session.update", 1)[0];
        assert!(
            setup["session"]["instructions"]
                .as_str()
                .unwrap()
                .contains("Clarp")
        );
        let began = device.told_of("session.started", 1);
        assert_eq!(began[0]["session"]["provider"], "elevenlabs");
        assert_eq!(began[0]["session"]["model"], model);
        (device, provider)
    };
    let (device, provider) = call(MINI);
    let (status, _) = select("a-foreign-model");
    assert_eq!(status, 400);
    assert_eq!(host.get(PICKER)["elevenlabs"]["model"], MINI);

    let pcm = clarp_kernel::python::b64encode(&[0xE8, 0x03].repeat(960));
    device.says(&json!({"type": "session.input_audio.append", "audio": pcm}));
    assert_eq!(
        provider.told_of("input_audio_buffer.append", 1)[0]["audio"],
        pcm
    );
    router.wants(json!([{"type": "message", "content": [
        {"type": "output_text", "text": "Den fiktive koden er FURU."},
    ]}]));
    committed(&provider, "user-audio");
    // Finish the provider tool before ASR. The Host must wait for the user's actual
    // words and forward the request only after those words enter the Oracle transcript.
    says_tool(&provider, "automatic", "route-call");
    transcribed(&provider, "user-audio", "Kan du finne den fiktive koden?");
    let heard = device.told_of("session.input_transcript.delta", 1);
    assert_eq!(heard[0]["delta"], "Kan du finne den fiktive koden?");
    let (key, question) = router.question(0);
    assert_eq!(key, "Bearer a-key-of-the-test");
    assert!(
        question
            .to_string()
            .contains("Kan du finne den fiktive koden?")
    );
    let tool_result = provider.told_of("conversation.item.create", 1);
    assert_eq!(tool_result[0]["item"]["call_id"], "route-call");
    assert!(
        tool_result[0]["item"]["output"]
            .as_str()
            .unwrap()
            .contains("FURU")
    );
    assert_eq!(provider.told_of("response.create", 1).len(), 1);
    provider.says(&json!({"type": "response.created", "response": {"id": "read-result"}}));
    provider.says(
        &json!({"type": "response.output_audio.delta", "response_id": "read-result", "delta": pcm}),
    );
    assert_eq!(
        device.told_of("session.output_audio.delta", 1)[0]["delta"],
        pcm
    );

    device.says(&json!({"type": "oracle_v2.interrupt"}));
    assert_eq!(
        provider.told_of("response.cancel", 1)[0]["response_id"],
        "read-result"
    );
    device.told_of("oracle_v2.playback_flush", 1);
    provider.says(
        &json!({"type": "response.output_audio.delta", "response_id": "read-result", "delta": pcm}),
    );
    provider.says(
        &json!({"type": "response.done", "response": {"id": "read-result", "status": "cancelled"}}),
    );

    committed(&provider, "pending-audio");
    transcribed(&provider, "pending-audio", "Stopp svaret før det begynner.");
    // Hearing the transcript proves that the commit preceded the local interrupt.
    device.told_of("session.input_transcript.delta", 2);
    device.says(&json!({"type":"oracle_v2.interrupt"}));
    device.told_of("oracle_v2.playback_flush", 2);
    provider.says(&json!({"type":"response.created","response":{"id":"pending-response"}}));
    assert_eq!(
        provider.told_of("response.cancel", 2)[1]["response_id"],
        "pending-response"
    );
    provider.says(
        &json!({"type":"response.output_audio.delta","response_id":"pending-response","delta":pcm}),
    );
    provider.says(
        &json!({"type":"response.done","response":{"id":"pending-response","status":"cancelled"}}),
    );

    committed(&provider, "auto-refused");
    transcribed(&provider, "auto-refused", "Neste automatiske svar.");
    device.told_of("session.input_transcript.delta", 3);
    refuses_response(&provider, &device);
    assert_eq!(device.of_kind("session.output_audio.delta").len(), 1);
    assert_eq!(provider.of_kind("response.create").len(), 1);

    let (second, max_provider) = call(MAX);
    router.wants(json!([{"type":"message","content":[{"type":"output_text","text":"Den andre fiktive koden er GRAN."}]}]));
    committed(&max_provider, "max-audio");
    transcribed(
        &max_provider,
        "max-audio",
        "Hva er den andre fiktive koden?",
    );
    says_tool(&max_provider, "max-automatic", "max-route-call");
    let tool = max_provider.told_of("conversation.item.create", 1);
    assert_eq!(tool[0]["item"]["call_id"], "max-route-call");
    assert!(tool[0]["item"]["output"].as_str().unwrap().contains("GRAN"));
    max_provider.told_of("response.create", 1);
    refuses_response(&max_provider, &second);
    assert_eq!(max_provider.of_kind("response.create").len(), 1);

    let (third, _) = call(MAX);
    third.says(&json!({"type":"session.close"}));
    assert_eq!(
        third.told_of("session.closed", 1)[0]["reason"],
        "client_closed"
    );
    third.ends();

    let old = host.post(LEGACY_PICKER, json!({"provider":"openai"}));
    assert_eq!(old["provider"], "openai");
    assert_eq!(old["available"], json!(["openai", "gemini"]));
    let saved = host.post(PICKER, json!({"provider":"elevenlabs"}));
    assert_eq!(saved["elevenlabs"]["model"], MAX);
}

#[test]
fn an_unconfigured_elevenlabs_key_reports_the_effective_oracle_fallback() {
    let place = Place::new("oracle-eleven-no-key");
    let host = place.start_without(
        "admin",
        &["--speech-keys", opted_in(&place).to_str().unwrap()],
        &[],
        &["ELEVEN_API_KEY"],
    );
    let (status, reply) = host.ask(
        "POST",
        PICKER,
        Some(&json!({"provider":"elevenlabs","model":MAX})),
    );
    assert_eq!(status, 200);
    assert_eq!(reply["selected"], "elevenlabs");
    assert_eq!(reply["provider"], "openai");
    assert!(reply.get("elevenlabs").is_none());
    assert_eq!(
        reply["fallback_reason"],
        "Eleven Realtime needs an ElevenLabs API key on this Host"
    );
    assert_eq!(host.get(PICKER), reply);
}

#[test]
fn a_shared_host_needs_explicit_opt_in_before_elevenlabs_can_be_selected() {
    let place = Place::new("oracle-eleven-disabled");
    let host = place.start_in("admin", &[], &[("ELEVEN_API_KEY", "fixture-vault-eleven")]);
    let before = host.get(PICKER);
    assert_eq!(before["provider"], "openai");
    assert!(before.get("elevenlabs").is_none());
    assert!(
        !before["available"]
            .as_array()
            .unwrap()
            .contains(&json!("elevenlabs"))
    );
    let (status, reply) = host.ask(
        "POST",
        PICKER,
        Some(&json!({"provider":"elevenlabs","model":MAX})),
    );
    assert_eq!(status, 409);
    assert_eq!(reply["error"], "Eleven Realtime is disabled on this Host");
    assert_eq!(host.get(PICKER), before);
    assert_eq!(host.get("/oracle/status")["v2"]["voice_provider"], "openai");
}
