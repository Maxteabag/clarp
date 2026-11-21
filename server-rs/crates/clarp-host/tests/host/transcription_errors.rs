use super::*;

#[test]
fn a_loading_or_busy_transcription_has_fixed_client_and_call_words() {
    let place = Place::new("speech-loading-busy");
    let worker = place.root.join("worker.sh");
    let ready = place.root.join("allow-load");
    let began = place.root.join("clip-started");
    let release = place.root.join("allow-result");
    std::fs::write(
        &worker,
        r#"#!/bin/sh
await_file() {
  tries=0
  while [ ! -f "$1" ] && [ "$tries" -lt 350 ]; do
    sleep 0.05
    tries=$((tries+1))
  done
}
while IFS= read -r line; do
  case "$line" in
    *'"op":"load"'*) await_file "$1/allow-load"; echo '{"event":"ready"}' ;;
    *'"op":"transcribe"'*)
      id=${line#*\"id\":\"}; id=${id%%\"*}
      : > "$1/clip-started"
      await_file "$1/allow-result"
      echo "{\"id\":\"$id\",\"segments\":[{\"text\":\"Ready.\",\"no_speech_prob\":0.1}]}"
      ;;
    *'"op":"quit"'*) exit 0 ;;
  esac
done
"#,
    )
    .unwrap();
    // A wrapper gives the fake worker its gate directory. Each gate expires on its own.
    let wrapper = place.root.join("launch-worker.sh");
    std::fs::write(
        &wrapper,
        format!("exec sh '{}' '{}'", worker.display(), place.root.display()),
    )
    .unwrap();
    let (nova, _) = scribe_stand_in(
        r#"{"results":{"channels":[{"alternatives":[{"transcript":"Next engine."}]}]}}"#,
    );
    let host = place.start_in(
        "admin",
        &[
            "--whisper-python",
            "sh",
            "--whisper-worker",
            wrapper.to_str().unwrap(),
        ],
        &[
            ("HOME", place.root.to_str().unwrap()),
            ("CLARP_NOVA_ENDPOINT", nova.as_str()),
            ("DEEPGRAM_API_KEY", "test-deepgram"),
            ("ELEVEN_API_KEY", ""),
            ("CARTESIA_API_KEY", ""),
        ],
    );
    let (phone, _) = pair(&host, "Phone", "full");
    let (limited, _) = pair(&host, "Watch", "limited");
    let upload = |headers: &[(&str, &str)]| -> (u16, Value) {
        let response = fetch(
            &host,
            &phone,
            "POST",
            "/transcribe",
            headers,
            b"fixture clip",
        );
        (
            response.status,
            serde_json::from_slice(&response.body).unwrap(),
        )
    };
    let loading = upload(&[("X-Transcription-Model", "faster-whisper:small.en")]);
    assert_eq!(
        loading,
        (503, json!({"error":"transcription model loading"}))
    );
    std::fs::write(&ready, b"").unwrap();
    wait_for("Whisper to finish loading", || {
        host.get("/diagnostics/health")["checks"]["stt_ready"] == true
    });

    let agent = host.new_agent("Listener");
    let made = host.ask_as(
        &phone,
        "POST",
        "/transcription-providers",
        Some(&json!({
            "call_engines":"faster-whisper:small.en,deepgram:nova-3"
        })),
    );
    assert_eq!(made.0, 200, "{}", made.1);
    let call = host
        .ask_as(&phone, "POST", "/calls", Some(&json!({"agents":[agent]})))
        .1;
    let id = call["call"]["call_id"].as_str().unwrap();
    let (ordinary, busy, tried) = std::thread::scope(|scope| {
        let ordinary = scope.spawn(|| upload(&[]));
        wait_for(
            "the first upload holding the Whisper inference slot",
            || began.exists(),
        );
        let busy = scope.spawn(|| upload(&[("X-Utterance-Id", "busy-clip")]));
        let tried = upload(&[("X-Clarp-Call-Id", id), ("X-Transcription-Session", &agent)]);
        let busy = busy.join().unwrap();
        std::fs::write(&release, b"").unwrap();
        (ordinary.join().unwrap(), busy, tried)
    });
    assert_eq!(ordinary.0, 200, "{}", ordinary.1);
    assert_eq!(busy, (429, json!({"error":"whisper busy"})));
    assert_eq!(tried.0, 200, "{}", tried.1);
    assert_eq!(tried.1["stt"]["attempts"][0]["reason"], "whisper busy");
    assert_eq!(tried.1["stt"]["effective_engine"], "deepgram:nova-3");
    let events = host.get("/voice-events?utterance_id=busy-clip&event=error");
    assert_eq!(
        events["events"][0]["detail"],
        json!({"message":"whisper busy"})
    );
    let (status, health) = host.ask_as(&limited, "GET", "/diagnostics/health", None);
    assert_eq!(status, 200);
    assert_eq!(health["subsystems"]["stt"]["last_error"], "busy");
}
