//! R146: fields read as text keep the reference's refusals and validation order.
use super::*;

#[test]
fn body_fields_read_as_text_reject_truthy_non_text() {
    let place = Place::new("body-text");
    let host = place.start();
    let check = |path: &str, body: Value, error: &str| {
        let (status, reply) = host.ask("POST", path, Some(&body));
        assert_eq!(
            (status, reply["error"].as_str()),
            (400, Some(error)),
            "{path}: {body}: {reply}"
        );
    };
    let reads = [
        ("/focus", "session"),
        ("/select", "session"),
        ("/compact", "session"),
        ("/agent-llm", "session"),
        ("/agent-rename", "session"),
        ("/agent-mute", "session"),
        ("/agent-voice-verbosity", "session"),
        ("/agent-heartbeat", "session"),
        ("/agent-dreaming", "session"),
        ("/location", "session"),
        ("/location/request", "session"),
        ("/agent-voice", "session"),
        ("/agent-voice", "voice_id"),
        ("/agent-voice", "provider"),
        ("/preview", "voice_id"),
        ("/preview", "text"),
        ("/preview", "session"),
        ("/devices", "token"),
        ("/devices", "platform"),
        ("/devices", "session"),
        ("/devices", "environment"),
        ("/devices", "base_url"),
        ("/clips/ack", "status"),
        ("/clips/ack", "url"),
        ("/clips/ack", "error"),
    ];
    for (path, field) in reads {
        for non_text in [
            json!(true),
            json!(1),
            json!(1.5),
            json!(["words"]),
            json!({"words": 1}),
        ] {
            let mut body = match path {
                "/devices" => json!({"token": "a".repeat(64)}),
                "/clips/ack" => json!({"status": "play-ok", "clip_id": 1}),
                _ => json!({}),
            };
            body[field] = non_text;
            check(path, body, &format!("{field} must be text"));
        }
    }
    for empty in [
        Value::Null,
        json!(false),
        json!(0),
        json!(0.0),
        json!([]),
        json!({}),
        json!(" \t "),
    ] {
        check("/select", json!({"session": empty}), "bad session name");
        check("/compact", json!({"session": empty}), "session required");
        check("/focus", json!({"session": empty}), "session required");
        check("/preview", json!({"voice_id": empty}), "voice_id required");
        check("/devices", json!({"token": empty}), "token required");
    }
    check("/remote-action", json!({"action": true}), "unknown action");
    check(
        "/clips/ack",
        json!({"clip_id": "bad", "status": [1]}),
        "status must be text",
    );
    check(
        "/clips/ack",
        json!({"clip_id": "bad", "url": true}),
        "bad clip_id",
    );
    check(
        "/clips/ack",
        json!({"status": "bad", "error": true}),
        "error must be text",
    );
    check(
        "/devices",
        json!({"token": "a".repeat(64), "platform": "android", "provider": "fcm",
       "supported_conversation_modes": ["not a mode"], "session": 1}),
        "live_mode_invalid",
    );
}
