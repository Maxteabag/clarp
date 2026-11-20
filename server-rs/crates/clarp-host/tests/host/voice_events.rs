//! The Host's tests of what a live voice says that this Host does not read: one file a piece,
//! declared in `host.rs`. (A ring's call holds the same rule; its test is the ring's own.)

use super::*;

/// A voice of GPT-Live that is in standby says so (`session.paused`, then `session.resumed`
/// when someone speaks), and a voice may say a kind of event no Host knows yet. The Oracle's
/// call passes them over: the call goes on, the phone is told nothing of them, and the journal
/// says each kind once, by its type alone, never what it carried.
#[test]
fn an_event_of_the_voice_the_oracle_does_not_read_is_passed_over_said_once_and_ends_no_call() {
    const ADMIN: &str = "the-administrators-token";
    let place = Place::new("voice-events");
    let voice = stand_in_oracle_voice();
    let router = stand_in_oracle_router();
    let world = [
        ("CLARP_LIVE_ENDPOINT", voice.address.as_str()),
        ("CLARP_ROUTER_ENDPOINT", router.address.as_str()),
        ("OPENAI_API_KEY", "a-key-of-the-test"),
    ];
    let host = place.start_in(ADMIN, &[], &world);
    let (phone, _) = pair(&host, "Phone", "full");
    let door = socket_at(&host, &phone, "/oracle/v2?new_conversation=1");
    let call = End::of_a_phone(door.expect("a call is opened"));
    let spoken = voice.call(0);
    spoken.told_of("session.start", 1);
    call.told_of("oracle_v2.context", 1);

    for kind in [
        "session.paused",
        "session.resumed",
        "session.paused",
        "session.of_a_kind_to_come",
        "session.of_a_kind_to_come",
    ] {
        spoken.says(&json!({ "type": kind, "said": "nothing a journal shows" }));
    }
    // The call goes on: what the voice says next reaches the phone, and nothing before it did.
    let aloud = json!({ "type": "session.output_audio.delta", "delta": loud() });
    spoken.says(&aloud);
    assert_eq!(call.told_of("session.output_audio.delta", 1), [aloud]);
    let told: Vec<Value> = call.told();
    assert!(
        told.iter().all(|said| {
            let kind = said["type"].as_str().unwrap_or("");
            !kind.starts_with("session.paused")
                && !kind.starts_with("session.resumed")
                && !kind.starts_with("session.of_a_kind")
        }),
        "{told:?}"
    );
    let unread = || -> Vec<String> {
        let heard = host.heard.lock().unwrap().clone();
        heard
            .lines()
            .filter(|line| line.starts_with("oracleVoiceEventUnread"))
            .map(str::to_string)
            .collect()
    };
    wait_for("the voice's unread kinds to be said", || {
        unread().len() == 3
    });
    assert_eq!(
        unread(),
        [
            "oracleVoiceEventUnread type=session.paused",
            "oracleVoiceEventUnread type=session.resumed",
            "oracleVoiceEventUnread type=session.of_a_kind_to_come",
        ]
    );
    assert!(
        !host
            .heard
            .lock()
            .unwrap()
            .contains("nothing a journal shows")
    );
}
