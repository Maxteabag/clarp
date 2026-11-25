//! `clarp-admin transcription agent` (issue 764): one file a piece, declared in `host.rs`.

use super::*;

#[test]
fn an_agent_shows_and_sets_how_an_agent_is_heard_and_default_takes_it_away() {
    let place = Place::new("transcription-agent");
    let host = place.start();
    let session = host.new_agent("Listener");
    let other = host.new_agent("Bystander");
    let id = row_id(&host, &session);
    let admin = |args: &[&str]| {
        let mut all = vec!["clarp-admin", "transcription", "agent"];
        all.extend_from_slice(args);
        tool(&host, &place.root, None, &all)
    };
    let shown = |args: &[&str]| -> Value {
        let (code, out, err) = admin(args);
        assert_eq!(code, 0, "{args:?}: {err}");
        serde_json::from_str(&out).unwrap()
    };
    let kept = |key: &str| host.get("/transcription-providers")[key].clone();

    let before = shown(&["--session", &session]);
    assert_eq!(before["session"], json!(session));
    assert_eq!(before["agent_id"], json!(id));
    assert_eq!(before["own_language"], false);
    assert_eq!(before["language"], before["host_language"]);
    assert_eq!(before["engines"], json!([]));
    assert_eq!(before["own_engines"], false);

    let set = shown(&[
        "--session",
        &session,
        "--language",
        "no,en",
        "--engines",
        "elevenlabs:scribe_v2,google:chirp_3",
    ]);
    assert_eq!(set["language"], "no,en");
    assert_eq!(set["own_language"], true);
    assert_eq!(
        set["engines"],
        json!(["elevenlabs:scribe_v2", "google:chirp_3"])
    );
    assert_eq!(set["applies"], "next clip");
    // Kept under the session, and under the agent's id beside it, as an app's save keeps it.
    let languages = kept("agent_languages");
    assert_eq!(languages[session.to_lowercase().as_str()], "no,en");
    assert_eq!(languages[id.to_lowercase().as_str()], "no,en");
    assert_eq!(
        kept("agent_engines")[session.to_lowercase().as_str()],
        "elevenlabs:scribe_v2,google:chirp_3"
    );
    // The other agent is heard as before.
    assert_eq!(shown(&["--session", &other])["own_language"], false);

    // What an app's save refuses, the command refuses in the same words, and keeps nothing.
    let (code, _, err) = admin(&["--session", &session, "--language", "norsk"]);
    assert_eq!(code, 1);
    assert!(err.contains("ISO 639-1"), "{err}");
    let (code, _, err) = admin(&["--session", &session, "--engines", "nobody:none"]);
    assert_eq!(code, 1);
    assert!(err.contains("unknown transcription engine"), "{err}");
    assert_eq!(kept("agent_languages"), languages);
    let (code, _, err) = admin(&["--session", "no-such-agent"]);
    assert_eq!(code, 1);
    assert!(err.contains("no such agent session"), "{err}");
    let (code, _, err) = admin(&["--session", &session, "--loud"]);
    assert_eq!(code, 2);
    assert!(
        err.contains("usage: clarp-admin transcription agent"),
        "{err}"
    );

    // An app may have kept it under the agent's persona: default takes that away too.
    host.post(
        "/transcription-providers",
        json!({"agent_languages": {"listener": "de"}}),
    );
    let cleared = shown(&[
        "--session",
        &session,
        "--language",
        "default",
        "--engines",
        "default",
    ]);
    assert_eq!(cleared["own_language"], false, "{cleared}");
    assert_eq!(cleared["language"], cleared["host_language"]);
    assert_eq!(cleared["own_engines"], false);
    assert_eq!(kept("agent_languages"), json!({}));
    assert_eq!(kept("agent_engines"), json!({}));

    // Help is read without running the command.
    let (code, out, _) = admin(&["--help"]);
    assert_eq!(code, 0);
    assert!(out.contains("[--language LANGUAGE]"), "{out}");
}
