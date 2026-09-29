use clarp_core::audio::{Effect, Output, PcmFormat, Playback, Player, RecordingLease, Transcriptions, encode_wav, hls_artifacts};
use clarp_core::json::Object;
use clarp_core::protocol::voice_delivery_session;
use serde_json::{Value, json};
use url::Url;

fn event(value: Value) -> Object {
    value.as_object().cloned().unwrap()
}

fn player() -> Player {
    let mut player = Player::default();
    player.set_base(Some(Url::parse("http://host.test:7682").unwrap()));
    player
}

fn acks(effects: &[Effect]) -> Vec<String> {
    effects
        .iter()
        .filter_map(|e| match e {
            Effect::Ack(body) => Some(body["status"].as_str().unwrap().to_owned()),
            _ => None,
        })
        .collect()
}

fn downloads(effects: &[Effect]) -> Vec<String> {
    effects.iter().filter_map(|e| if let Effect::Download(url) = e { Some(url.to_string()) } else { None }).collect()
}

#[test]
fn wav_encoding_produces_a_valid_pcm_header() {
    assert_eq!(voice_delivery_session("rachel", "bella"), "rachel");
    assert_eq!(voice_delivery_session("", "bella"), "bella");
    let pcm = vec![0u8; 3_200];
    let wav = encode_wav(&pcm, PcmFormat::DICTATION);
    assert_eq!(wav.len(), pcm.len() + 44);
    assert_eq!(&wav[0..4], b"RIFF");
    assert_eq!(&wav[8..12], b"WAVE");
    assert_eq!(&wav[12..16], b"fmt ");
    assert_eq!(&wav[36..40], b"data");
    assert_eq!(u32::from_le_bytes(wav[24..28].try_into().unwrap()), 16_000);
    assert!(encode_wav(&[], PcmFormat::DICTATION).is_empty());
}

#[test]
fn a_clip_is_queued_downloaded_played_and_acknowledged() {
    let mut player = player();
    let clip = event(json!({"clip_id": 1, "url": "/clips/1.mp3", "complete_url": "/clips/1/complete.mp3", "trace_id": "t1"}));
    let effects = player.enqueue(clip, false);
    assert_eq!(acks(&effects), ["queued"]);
    assert_eq!(downloads(&effects), ["http://host.test:7682/clips/1/complete.mp3"], "complete beats url for containers");
    let effects = player.downloaded(Ok(b"ID3audio".to_vec()));
    assert_eq!(effects, [Effect::Play(Playback::Media { bytes: b"ID3audio".to_vec() })]);
    assert_eq!(acks(&player.started()), ["play-start"]);
    assert!(acks(&player.started()).is_empty(), "play-start once");
    assert_eq!(player.output(), Output::Playing);
    let effects = player.ended(Ok(()), false, false);
    assert_eq!(acks(&effects), ["play-ok"]);
    assert!(matches!(effects[0], Effect::Finished(_)));
    let Effect::Ack(body) = &effects[2] else { panic!("ack") };
    assert_eq!(body["trace_id"], "t1");
    assert!(!player.has_current());
}

#[test]
fn clips_fail_fast_without_a_media_backend_and_report_it_once() {
    let mut player = player();
    let clip = json!({"clip_id": 1, "url": "/clips/1/complete.mp3", "complete_url": "/clips/1/complete.mp3"});
    let mut all = player.enqueue(event(clip.clone()), false);
    let mut second = event(clip);
    second.insert("clip_id".into(), json!(2));
    all.extend(player.enqueue(second, false));
    assert_eq!(player.queued(), 1, "the second waits behind the first");
    let missing = Err("No audio backend is available".to_owned());
    all.extend(player.downloaded(Ok(b"ID3".to_vec())));
    all.extend(player.ended(missing.clone(), true, false));
    all.extend(player.downloaded(Ok(b"ID3".to_vec())));
    all.extend(player.ended(missing, true, false));
    assert_eq!(acks(&all), ["queued", "queued", "play-fail", "play-fail"]);
    assert_eq!(all.iter().filter(|e| matches!(e, Effect::Error(_))).count(), 1, "reported once, not per clip");
    assert_eq!(player.output(), Output::Stopped);
}

#[test]
fn raw_pcm_uses_the_stream_and_its_format() {
    let mut player = player();
    let clip = json!({"clip_id": 3, "url": "/c.pcm", "stream_url": "/c/stream", "complete_url": "/c/complete",
                      "audio_format": {"container": "raw", "encoding": "pcm_s16le", "sample_rate": 24000, "channels": 1}});
    let effects = player.enqueue(event(clip), false);
    assert_eq!(downloads(&effects), ["http://host.test:7682/c/stream"], "raw audio never waits for the complete file");
    assert_eq!(
        player.downloaded(Ok(vec![0, 0])),
        [Effect::Play(Playback::Pcm { bytes: vec![0, 0], sample_rate: 24000, channels: 1, encoding: "pcm_s16le".into() })]
    );
    let mut bad = Player::default();
    bad.set_base(Some(Url::parse("http://host.test:7682").unwrap()));
    bad.enqueue(event(json!({"clip_id": 4, "url": "/x", "audio_format": {"container": "raw", "encoding": "opus"}})), false);
    assert_eq!(acks(&bad.downloaded(Ok(vec![1]))), ["play-fail"]);
}

#[test]
fn playlists_download_their_segments_from_the_host_only() {
    let base = Url::parse("http://host.test:7682/").unwrap();
    let playlist_url = base.join("clips/9/index.m3u8").unwrap();
    let playlist = "#EXTM3U\n#EXT-X-MAP:URI=\"init.mp4\"\n#EXTINF:1,\nseg1.m4s\nhttp://evil.test/seg2.m4s\n#EXT-X-ENDLIST\n";
    let artifacts: Vec<String> = hls_artifacts(playlist, &playlist_url, &base).iter().map(Url::to_string).collect();
    assert_eq!(artifacts, ["http://host.test:7682/clips/9/init.mp4", "http://host.test:7682/clips/9/seg1.m4s"]);

    let mut player = player();
    let effects = player.enqueue(event(json!({"clip_id": 9, "url": "/clips/9.mp3", "playlist_url": "/clips/9/index.m3u8"})), false);
    assert_eq!(downloads(&effects), ["http://host.test:7682/clips/9/index.m3u8"]);
    assert_eq!(downloads(&player.downloaded(Ok(playlist.as_bytes().to_vec()))), ["http://host.test:7682/clips/9/init.mp4"]);
    assert_eq!(downloads(&player.downloaded(Ok(b"INIT".to_vec()))), ["http://host.test:7682/clips/9/seg1.m4s"]);
    assert_eq!(player.downloaded(Ok(b"SEG".to_vec())), [Effect::Play(Playback::Media { bytes: b"INITSEG".to_vec() })]);
}

#[test]
fn sources_outside_the_host_and_failed_downloads_fail_the_clip() {
    let mut player = player();
    let effects = player.enqueue(event(json!({"clip_id": 5, "url": "http://elsewhere.test/a.mp3"})), false);
    let Some(Effect::Ack(body)) = effects.iter().rev().find(|e| matches!(e, Effect::Ack(_))) else { panic!("ack") };
    assert_eq!(body["status"], "play-fail");
    assert_eq!(body["error"], "audio source is outside the configured server");
    player.enqueue(event(json!({"clip_id": 6, "url": "/a.mp3"})), false);
    let effects = player.downloaded(Err("HTTP 404".into()));
    assert_eq!(acks(&effects), ["play-fail"]);
    assert!(effects.contains(&Effect::Error("HTTP 404".into())));
}

#[test]
fn recording_holds_the_queue_and_mute_or_silence_drains_it() {
    let mut player = player();
    let effects = player.enqueue(event(json!({"clip_id": 1, "url": "/a.mp3"})), true);
    assert!(downloads(&effects).is_empty(), "the microphone is in use");
    assert_eq!(downloads(&player.start_next(false)), ["http://host.test:7682/a.mp3"]);
    player.enqueue(event(json!({"clip_id": 2, "url": "/b.mp3"})), false);
    let effects = player.silence();
    assert_eq!(effects.iter().filter(|e| matches!(e, Effect::Finished(_))).count(), 2);
    let Some(Effect::Ack(body)) = effects.iter().find(|e| matches!(e, Effect::Ack(_))) else { panic!("ack") };
    assert_eq!(body["error"], "interrupted by user");
    player.set_muted(true);
    assert!(matches!(player.enqueue(event(json!({"clip_id": 3, "url": "/c.mp3"})), false)[..], [Effect::Finished(_)]));
    assert!(player.set_base(Some(Url::parse("http://other.test").unwrap())).is_empty(), "nothing in hand to stop");
}

#[test]
fn pause_and_resume_follow_the_output() {
    let mut player = player();
    assert!(player.pause().is_empty(), "nothing playing");
    player.enqueue(event(json!({"clip_id": 1, "url": "/a.mp3"})), false);
    player.downloaded(Ok(b"x".to_vec()));
    player.started();
    assert_eq!(player.toggle()[0], Effect::PauseOutput);
    assert_eq!(player.output(), Output::Paused);
    assert_eq!(player.toggle()[0], Effect::ResumeOutput);
}

#[test]
fn background_transcriptions_keep_their_chat_ownership() {
    let mut dictations = Transcriptions::default();
    dictations.start("t1", "rachel");
    dictations.start("t2", "bella");
    assert_eq!((dictations.in_flight(), dictations.for_session("rachel"), dictations.for_session("bella")), (2, 1, 1));
    assert_eq!(dictations.finish("t2").as_deref(), Some("bella"));
    assert_eq!(dictations.finish("t1").as_deref(), Some("rachel"));
    assert_eq!(dictations.in_flight(), 0);
    dictations.start("t3", "rachel");
    assert!(dictations.cancel_session("rachel"));
    assert_eq!(dictations.finish("t3"), None, "a cancelled result is dropped");
    dictations.start("t4", "original-host-chat");
    assert!(dictations.cancel_all());
    assert_eq!(dictations.finish("t4"), None, "switching Host cancels pending delivery");
}

#[test]
fn the_microphone_is_exclusive_across_windows() {
    let path = std::env::temp_dir().join(format!("clarp-mic-test-{}.lock", std::process::id()));
    let mut first = RecordingLease::new(Some(path.clone()));
    let mut second = RecordingLease::new(Some(path.clone()));
    assert!(!first.busy());
    assert!(first.acquire("recording-window-chat"));
    assert!(!first.acquire("again"), "one recording at a time");
    assert!(second.busy() && !second.acquire("other"));
    assert_eq!(first.release(), "recording-window-chat");
    assert!(!second.busy() && second.acquire("other"));
    second.release();
    std::fs::remove_file(path).ok();
}
