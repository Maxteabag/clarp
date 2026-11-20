use super::*;

#[test]
fn voice_timeline_rows_survive_restart_and_trace_only_playback_joins_the_utterance() {
    let place = Place::new("voice-timeline");
    let host = place.start_with_token("admin");
    let (phone, _) = pair(&host, "Voice fixture", "full");
    let (viewer, _) = pair(&host, "Viewer", "limited");
    let now = now_ms();
    let data = json!({"source":" IOS ","client_id":" fixture ","session":" studio ","sent_at":now,
        "events":[{"event":"speech_start","ts":now-120,"mono_ms":300,"utterance_id":"turn-fixture","detail":{"silence_ms":80}},
            {"event":"speech_end","ts":now-20,"utterance_id":"turn-fixture","level_db":-22.5,"peak_db":-4},
            {"event":"transcript","ts":now,"utterance_id":"turn-fixture","trace_id":"trace-fixture","duration_ms":15,"text":""}]});
    let (code, posted) = host.ask_as(&phone, "POST", "/voice-events", Some(&data));
    assert_eq!(code, 200);
    assert_eq!(posted["stored"], 3);
    let offset = posted["clock_offset_ms"].as_i64().unwrap();
    let query = "/voice-events?utterance_id=turn-fixture";
    let events = host.get(query);
    let rows = events["events"].as_array().unwrap();
    assert_eq!(rows.len(), 3);
    assert_eq!(rows[0]["ts"], now - 120 + offset);
    assert_eq!(rows[0]["client_ts"], now - 120);
    assert_eq!(rows[0]["received_at"], now + offset);
    assert_eq!(rows[0]["source"], "ios");
    assert_eq!(rows[0]["client_id"], "fixture");
    assert_eq!(rows[0]["session"], "studio");
    assert_eq!(rows[1]["level_db"], -22.5);
    assert_eq!(rows[2]["text"], "");
    assert_eq!(
        host.ask_as(&viewer, "GET", query, None),
        (200, events.clone())
    );
    let ack = |status| {
        host.ask_as(
            &phone,
            "POST",
            "/clips/ack",
            Some(&json!({"clip_id":900000,"trace_id":"trace-fixture","status":status})),
        )
    };
    assert_eq!(ack("play-start").0, 200);
    assert_eq!(ack("play-ok").0, 200);
    let answer = host.get("/voice-events/utterances?session=studio");
    let summary = &answer["utterances"][0];
    assert_eq!(summary["utterance_id"], "turn-fixture");
    assert_eq!(summary["events"], 5);
    assert_eq!(summary["speech_ms"], 100);
    assert_eq!(summary["speech_to_text_ms"], 20);
    assert_eq!(summary["silence_before_ms"], 80);
    assert_eq!(summary["trace_id"], "trace-fixture");
    assert!(summary["play_ms"].is_number());
    host.kill();
    let host = place.start_with_token("admin");
    assert_eq!(host.get(query), events);
    assert_eq!(host.get("/voice-events/utterances?session=studio"), answer);
    let one = host.get("/voice-events?trace_id=trace-fixture&event=play_end&limit=1");
    assert_eq!(one["events"][0]["source"], "server");
    assert_eq!(one["events"][0]["detail"]["clip_id"], 900000);
    assert_eq!(one["events"][0]["text"], Value::Null);
    let saved = one["events"][0]["event_id"].as_u64().unwrap();
    let more = host.post(
        "/voice-events",
        json!({"events":[{"event":"after-restart"}]}),
    );
    assert_eq!(more["stored"], 1);
    assert!(
        host.get("/voice-events?event=after-restart")["events"][0]["event_id"]
            .as_u64()
            .unwrap()
            > saved
    );
}

#[test]
fn server_voice_timeline_joins_transcription_replay_send_and_clip_ack() {
    let place = Place::new("voice-producers");
    let (endpoint, requests) = scribe_stand_in(r#"{"text":"synthetic fixture."}"#);
    let worker = place.root.join("speech.py");
    std::fs::write(place.root.join("voice-metrics.py"),r#"import json,sys
data=sys.stdin.buffer.read()
print(json.dumps({"metrics":{"bytes":len(data),"content_type":sys.argv[1],"duration_ms":90.0,"rms_db":-34.0,"peak_db":-25.0},"reasons":["too_short"]}))
"#).unwrap();
    let host = place.start_in(
        "admin",
        &[
            "--whisper-worker",
            worker.to_str().unwrap(),
            "--transcription-engine",
            "elevenlabs:scribe_v2",
        ],
        &[
            ("CLARP_SCRIBE_ENDPOINT", endpoint.as_str()),
            ("ELEVEN_API_KEY", "fixture"),
        ],
    );
    let session = host.new_agent("Fixture");
    let (phone, _) = pair(&host, "Fixture phone", "full");
    let hear = || {
        fetch(
            &host,
            &phone,
            "POST",
            "/transcribe",
            &[
                ("X-Transcription-ID", "recording-fixture"),
                ("X-Utterance-ID", "utterance-fixture"),
                ("X-Client-Ts", "123.5"),
                ("X-Transcription-Session", &session),
                ("Content-Type", "audio/webm"),
            ],
            b"synthetic audio",
        )
    };
    let got = hear();
    assert_eq!(got.status, 200, "{}", got.text());
    let reply: Value = serde_json::from_slice(&got.body).unwrap();
    let trace = reply["trace_id"].as_str().unwrap();
    let timeline = |event: &str| {
        host.get(&format!(
            "/voice-events?utterance_id=utterance-fixture&event={event}"
        ))["events"]
            .clone()
    };
    let transcript = timeline("transcript");
    assert_eq!(transcript.as_array().unwrap().len(), 1);
    assert_eq!(transcript[0]["text"], reply["text"]);
    assert_eq!(transcript[0]["session"], session);
    assert_eq!(transcript[0]["trace_id"], trace);
    assert_eq!(transcript[0]["client_ts"], 123);
    assert_eq!(transcript[0]["detail"]["bytes"], 15);
    assert_eq!(transcript[0]["detail"]["herald_consumed"], false);
    assert_eq!(transcript[0]["detail"]["hands_free"], false);
    assert_eq!(transcript[0]["detail"]["content_type"], "audio/webm");
    assert_eq!(transcript[0]["detail"]["model"], "server-default");
    assert_eq!(
        transcript[0]["detail"]["ends_terminal"],
        reply["ends_terminal"]
    );
    wait_for("synthetic metrics recorded", || {
        timeline("corrupt").as_array().unwrap().len() == 1
    });
    assert_eq!(timeline("level")[0]["level_db"], -34.0);
    assert_eq!(
        timeline("corrupt")[0]["detail"]["reasons"],
        json!(["too_short"])
    );
    let cached = hear();
    assert_eq!(cached.status, 200);
    assert_eq!(requests.lock().unwrap().len(), 1);
    let retry = timeline("retry");
    assert_eq!(retry[0]["text"], reply["text"]);
    assert_eq!(retry[0]["trace_id"], trace);
    let sent=host.post("/send",json!({"session":session,"text":"synthetic fixture.","trace_id":trace,
        "utterance_id":"utterance-fixture","client_msg_id":"voice-timeline-send","synthesize_audio":false}));
    let send = timeline("send");
    assert_eq!(send[0]["detail"]["dispatch"], sent["dispatch"]);
    assert_eq!(send[0]["detail"]["client_msg_id"], "voice-timeline-send");
    host.post(
        "/clips/ack",
        json!({"clip_id":776655,"trace_id":trace,"status":"play-fail","error":"fixture"}),
    );
    assert_eq!(
        host.get(&format!("/voice-events?trace_id={trace}&event=play_fail"))["events"][0]["detail"]
            ["error"],
        "fixture"
    );
    assert_eq!(
        host.get("/voice-events/utterances")["utterances"][0]["events"],
        6
    );
    let bad = fetch(
        &host,
        &phone,
        "POST",
        "/transcribe",
        &[("X-Utterance-ID", "bad-fixture")],
        b"",
    );
    assert_eq!(bad.status, 400);
    assert_eq!(
        host.get("/voice-events?utterance_id=bad-fixture")["events"][0]["detail"]["reasons"],
        json!(["bad_size"])
    );
}

#[test]
fn voice_timeline_analyser_matches_the_synthetic_pcm_reference() {
    let result = std::process::Command::new("python3")
        .args([
            "-IB",
            concat!(
                env!("CARGO_MANIFEST_DIR"),
                "/../../tools/test-voice-metrics.py"
            ),
        ])
        .output()
        .unwrap();
    assert!(
        result.status.success(),
        "{}",
        String::from_utf8_lossy(&result.stderr)
    );
}
