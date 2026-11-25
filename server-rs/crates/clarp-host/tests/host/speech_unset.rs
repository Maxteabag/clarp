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
    host.send_aloud(
        &session,
        "Looking. <speak>The build is green.</speak>",
        "m1",
    );
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

    // Nor are a form's answers asked to be answered aloud.
    let form = json!({"session":session,"type":"html_form","artifact_id":"quiet-form",
        "title":"Plan","payload":{"content":"<p>Plan</p>","version":"v1",
        "answer_schema":{"type":"object","properties":{"note":{"type":"string"}}}}});
    assert_eq!(host.ask("POST", "/artifacts", Some(&form)).0, 201);
    let answers = json!({"version":"v1","submission_id":"submission-aloud-01",
        "answers":{"note":"<speak>Read it.</speak>"},"synthesize_audio":true});
    let sent = host.ask("POST", "/artifacts/quiet-form/submit", Some(&answers));
    assert_eq!(sent.0, 200, "{}", sent.1);
    wait_for("the answer to the form", || {
        host.turns(&session)
            .iter()
            .filter(|row| row["role"] == "assistant")
            .count()
            == 2
            && host.state(&session) == "done"
    });
    // The same answers sent again are the same submission, not another wish.
    let again = host.ask("POST", "/artifacts/quiet-form/submit", Some(&answers));
    assert_eq!(again.0, 200, "{}", again.1);
    std::thread::sleep(Duration::from_millis(500));
    let events = heard.lock().unwrap().clone();
    let ours = |kind: &str| {
        events
            .iter()
            .filter(|event| event["type"] == kind && event["session"] == session.as_str())
            .count()
    };
    assert_eq!(
        ours("tts-error"),
        0,
        "nor is the form's answer reported unspoken"
    );
    assert_eq!(ours("audio"), 0);

    // Nor is a decision's answer, asked aloud.
    let (status, card) = host.ask(
        "POST",
        "/decisions",
        Some(&json!({ "session": session, "title": "Ship it", "question": "Ship today?" })),
    );
    assert_eq!(status, 201, "{card}");
    let id = card["artifact"]["decision"]["decision_id"]
        .as_str()
        .unwrap()
        .to_string();
    let resolved = host.ask(
        "POST",
        &format!("/decisions/{id}/resolve"),
        Some(&json!({ "choice": "accepted", "expected_revision": 1, "synthesize_audio": true })),
    );
    assert_eq!(resolved.0, 200, "{}", resolved.1);
    wait_for("the answer to the decision", || {
        host.turns(&session)
            .iter()
            .filter(|row| row["role"] == "assistant")
            .count()
            == 3
            && host.state(&session) == "done"
    });
    std::thread::sleep(Duration::from_millis(500));
    let events = heard.lock().unwrap().clone();
    let told = |kind: &str| {
        events
            .iter()
            .filter(|event| event["type"] == kind && event["session"] == session.as_str())
            .count()
    };
    assert_eq!(told("tts-error"), 0, "nor is the decision's answer");
    assert_eq!(told("audio"), 0);
}
