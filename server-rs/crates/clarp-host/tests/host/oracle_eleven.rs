use super::*;

const MINI: &str = "eleven_realtime_v1_mini";
const MAX: &str = "eleven_realtime_v1_max";
const ROUTE: &str = "/v1/convai/realtime";

fn elevenlabs_stand_in() -> (String, std::sync::mpsc::Receiver<(Value, End)>) {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let address = format!("http://{}", listener.local_addr().unwrap());
    let (ready, calls) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        for model in [MINI, MAX] {
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

#[test]
fn eleven_realtime_preserves_oracle_audio_routing_and_interruptions() {
    let place = Place::new("oracle-eleven");
    let (address, calls) = elevenlabs_stand_in();
    let router = stand_in_oracle_router();
    let host = place.start_in(
        "admin",
        &[],
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
            "/oracle/voice-provider",
            Some(&json!({
                "provider": "elevenlabs", "model": model,
            })),
        )
    };
    assert_eq!(
        host.get("/oracle/voice-provider")["available"],
        json!(["openai", "gemini", "elevenlabs"])
    );
    let call = |model: &str| {
        let (status, saved) = select(model);
        assert_eq!(status, 200, "{saved}");
        assert_eq!(saved["elevenlabs"]["models"], json!([MINI, MAX]));
        assert_eq!(host.get("/oracle/status")["v2"]["model"], model);
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
    assert_eq!(
        host.get("/oracle/voice-provider")["elevenlabs"]["model"],
        MINI
    );

    let pcm = clarp_kernel::python::b64encode(&[0xE8, 0x03].repeat(960));
    device.says(&json!({"type": "session.input_audio.append", "audio": pcm}));
    assert_eq!(
        provider.told_of("input_audio_buffer.append", 1)[0]["audio"],
        pcm
    );
    router.wants(json!([{"type": "message", "content": [
        {"type": "output_text", "text": "Den fiktive koden er FURU."},
    ]}]));
    provider.says(&json!({"type": "input_audio_buffer.speech_started"}));
    provider.says(&json!({"type": "input_audio_buffer.committed", "item_id": "user-audio"}));
    provider.says(&json!({"type": "response.created", "response": {"id": "automatic"}}));
    // Finish the provider tool before ASR. The Host must wait for the user's actual
    // words and forward the request only after those words enter the Oracle transcript.
    provider.says(&json!({"type": "response.done", "response": {
        "id": "automatic", "status": "completed", "output": [{
            "type": "function_call", "name": "delegate_to_agent", "call_id": "route-call",
            "arguments": "{\"request\":\"Finn den fiktive koden\"}",
        }],
    }}));
    provider.says(
        &json!({"type": "conversation.item.input_audio_transcription.completed",
        "item_id": "user-audio", "transcript": "Kan du finne den fiktive koden?"}),
    );
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
    device.says(&json!({"type": "session.close"}));
    device.told_of("session.closed", 1);
    device.ends();
    assert_eq!(device.of_kind("session.output_audio.delta").len(), 1);
    assert_eq!(provider.of_kind("response.create").len(), 1);

    let (second, _) = call(MAX);
    second.says(&json!({"type": "session.close"}));
    second.told_of("session.closed", 1);
    second.ends();
}
