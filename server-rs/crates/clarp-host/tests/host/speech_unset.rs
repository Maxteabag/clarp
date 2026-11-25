//! A Host with no engine of speech set up (issue 802): one file a piece, declared in
//! `host.rs`.

use super::*;

#[test]
fn on_a_host_with_no_speech_set_up_a_reply_is_not_spoken_and_not_reported_unspoken() {
    let place = Place::new("speech-unset");
    // The owner chose an engine and gave the agent a voice in it, but set no key for it.
    let owners = place.root.join("owners.toml");
    std::fs::write(
        &owners,
        "[tts]\nprovider = \"cartesia\"\n\n[cartesia.voices]\nMaximus = \"voice-of-maximus\"\n",
    )
    .unwrap();
    let args = ["--speech-keys", owners.to_str().unwrap()];
    let world = [
        ("CARTESIA_API_KEY", ""),
        ("GEMINI_API_KEY", ""),
        ("GOOGLE_VERTEX_API_KEY", ""),
        ("ELEVEN_API_KEY", ""),
        ("DEEPGRAM_API_KEY", ""),
    ];
    let host = place.start_in("", &args, &world);
    let session = host.new_agent("Maximus");
    let heard = host.listen();

    // Said once, where the Host reports its speech: no voice is available here.
    assert_eq!(host.get("/voice-catalog")["voice_available"], false);

    // The app has its speaker on and asks for the answer aloud; the agent marks a part.
    host.send_aloud(&session, "Looking. <speak>The build is green.</speak>", "m1");
    wait_for("the answer", || {
        host.conversation(&session)
            .iter()
            .any(|(role, _)| role == "assistant")
            && host.state(&session) == "done"
    });
    std::thread::sleep(Duration::from_millis(500));
    let events = heard.lock().unwrap().clone();
    let ours = |kind: &str| {
        events
            .iter()
            .filter(|event| event["type"] == kind && event["session"] == session.as_str())
            .count()
    };
    assert_eq!(ours("tts-error"), 0, "no reply is reported as not spoken");
    assert_eq!(ours("audio"), 0, "and nothing was announced to be heard");
}
