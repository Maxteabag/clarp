//! R175 through the upload route, the call receipt, and the provider boundaries.

use clarp_kernel::call_transcription::ENGINES as CALL_ENGINES_KEY;
use clarp_kernel::languages::AGENT_ENGINES_KEY;
use clarp_ports::kv::KeyValueEngine as _;
use serde_json::{Value, json};

use super::{Host, Place, WHISPER_STAND_IN, fetch, pair, program, scribe_stand_in, wait_for};

#[test]
fn a_recording_reaches_a_provider_once_across_its_models() {
    const INK_WHISPER: &str = "cartesia:ink-whisper";
    const INK_TWO: &str = "cartesia:ink-2";
    const NOVA: &str = "deepgram:nova-3";
    let place = Place::new("engine-order");
    let worker = place.root.join("whisper.sh");
    let heard_own = place.root.join("heard-own");
    let script = WHISPER_STAND_IN.replace(
        "*'\"op\":\"transcribe\"'*)",
        &format!(
            "*'\"op\":\"transcribe\"'*)\n      echo heard >> '{}'",
            heard_own.display()
        ),
    );
    std::fs::write(&worker, script).unwrap();
    let own_count = || {
        std::fs::read_to_string(&heard_own)
            .unwrap_or_default()
            .lines()
            .count()
    };
    let (ink, ink_requests) = scribe_stand_in(r#"{"error":"stand-in outage"}"#);
    let (nova, nova_requests) = scribe_stand_in(
        r#"{"results":{"channels":[{"alternatives":[{"transcript":"Nova heard it."}]}]}}"#,
    );
    let home = place.root.to_str().unwrap();
    let world = [
        ("HOME", home),
        ("XDG_CONFIG_HOME", home),
        ("XDG_CACHE_HOME", home),
        ("XDG_DATA_HOME", home),
        ("XDG_STATE_HOME", home),
        ("TMPDIR", home),
        ("CLARP_INK_ENDPOINT", &ink),
        ("CLARP_NOVA_ENDPOINT", &nova),
        ("CARTESIA_API_KEY", "fixture-ink"),
        ("DEEPGRAM_API_KEY", "fixture-nova"),
        ("ELEVEN_API_KEY", ""),
        ("GOOGLE_APPLICATION_CREDENTIALS", ""),
    ];
    let start = |decoder: &str| {
        let mut world = world.to_vec();
        world.push(("CLARP_CHIRP_DECODER", decoder));
        place.start_in(
            "admin",
            &[
                "--whisper-python",
                "sh",
                "--whisper-worker",
                worker.to_str().unwrap(),
                "--whisper-model",
                "small",
            ],
            &world,
        )
    };
    let host = start("cat");
    let session = host.new_agent("Ada");
    let hear = |host: &Host, token: &str, call: Option<&str>, clip: &[u8]| {
        let mut headers = vec![("X-Transcription-Session", session.as_str())];
        if let Some(id) = call {
            headers.push(("X-Clarp-Call-Id", id));
        }
        let reply = fetch(host, token, "POST", "/transcribe", &headers, clip);
        (
            reply.status,
            serde_json::from_slice::<Value>(&reply.body).unwrap(),
        )
    };
    let hearing = |host: &Host| {
        let snapshot = host.get("/agents/snapshot");
        let profile = snapshot["agents"]
            .as_array()
            .unwrap()
            .iter()
            .find(|agent| agent["session"] == session)
            .unwrap();
        profile["voice_profile"]["hearing"]["engines"]
            .as_array()
            .unwrap()
            .iter()
            .map(|engine| engine["id"].as_str().unwrap().to_owned())
            .collect::<Vec<_>>()
    };

    // Saving and reading a list drop exact repeats, keeping another model of the same provider.
    let chain = format!("{INK_WHISPER},{INK_TWO},{NOVA}");
    let stored = host.post(
        "/transcription-providers",
        json!({
            "agent_engines": { (&session): format!("{INK_WHISPER}, {chain}") },
            "call_engines": format!("{NOVA},{NOVA}"),
            "call_languages": "en",
        }),
    );
    assert_eq!(stored["agent_engines"][&session], chain);
    assert_eq!(stored["call_engines"], NOVA);
    assert_eq!(hearing(&host), [INK_WHISPER, INK_TWO, NOVA]);
    let ordinary = hear(&host, "admin", None, b"ordinary fixture");
    assert_eq!(
        (ordinary.0, &ordinary.1["text"]),
        (200, &json!("Nova heard it."))
    );
    assert_eq!(ink_requests.lock().unwrap().len(), 1);
    assert_eq!(nova_requests.lock().unwrap().len(), 1);
    wait_for("the provider skip to be logged", || {
        host.heard.lock().unwrap().contains(&format!(
            "sttEngineRepeatSkipped session={session} {INK_TWO} (cartesia already had this recording)"
        ))
    });

    // The same provider exclusion is per clip. The call's receipt explains the skipped model.
    let (phone, _) = pair(&host, "Phone", "full");
    let (status, opened) =
        host.ask_as(&phone, "POST", "/calls", Some(&json!({"agents":[session]})));
    assert_eq!(status, 200, "{opened}");
    let call = opened["call"]["call_id"].as_str().unwrap();
    let (status, called) = hear(&host, &phone, Some(call), b"call fixture");
    assert_eq!(status, 200, "{called}");
    let report = &called["stt"];
    assert_eq!(report["effective_engine"], NOVA);
    let attempts = report["attempts"].as_array().unwrap();
    assert_eq!(attempts.len(), 3, "{report}");
    assert_eq!(attempts[0]["engine"], INK_WHISPER);
    assert_eq!(attempts[0]["outcome"], "error");
    assert_eq!(attempts[0]["language"], "en");
    assert_eq!(
        attempts[1],
        json!({"engine":INK_TWO,"outcome":"skipped","reason":"provider_already_sent"})
    );
    assert_eq!(attempts[2]["engine"], NOVA);
    assert_eq!(attempts[2]["outcome"], "ok");
    assert_eq!(ink_requests.lock().unwrap().len(), 2);
    assert_eq!(nova_requests.lock().unwrap().len(), 2);
    host.post(
        "/transcription-providers",
        json!({"agent_engines":{(&session):format!("{INK_WHISPER},{INK_TWO}")}}),
    );
    let (status, terminal) = hear(&host, &phone, Some(call), b"terminal failure");
    assert_eq!(status, 500, "{terminal}");
    assert_eq!(terminal["error"], "transcription failed");
    let attempts = terminal["stt"]["attempts"].as_array().unwrap();
    assert_eq!(attempts.len(), 2, "{terminal}");
    assert_eq!(attempts[0]["engine"], INK_WHISPER);
    assert_eq!(attempts[0]["reason"], "transcription failed");
    assert_eq!(
        attempts[1],
        json!({"engine":INK_TWO,"outcome":"skipped","reason":"provider_already_sent"})
    );
    assert_eq!(ink_requests.lock().unwrap().len(), 3);
    host.kill();

    // A saved older list can name a now unknown model. That refusal must leave the next
    // Cartesia model eligible. The store is opened through the same port as the Host.
    let seed = |picked: &str| {
        clarp_store_sqlite::SqliteKeyValue
            .open(&place.root.join("data/fleet.db"))
            .unwrap()
            .put(
                "setting",
                AGENT_ENGINES_KEY,
                json!({(&session):picked}).to_string().as_bytes(),
            )
            .unwrap();
    };
    seed(&format!("cartesia:retired,{INK_WHISPER},{NOVA}"));
    clarp_store_sqlite::SqliteKeyValue
        .open(&place.root.join("data/fleet.db"))
        .unwrap()
        .put(
            "setting",
            CALL_ENGINES_KEY,
            format!("{NOVA},{NOVA}").as_bytes(),
        )
        .unwrap();
    let host = start("cat");
    let policy = host.get("/transcription-providers");
    assert_eq!(policy["call_engines"], NOVA);
    assert_eq!(policy["call_policy_error"], "");
    let (status, said) = hear(&host, "admin", None, b"pre-contact fixture");
    assert_eq!((status, &said["text"]), (200, &json!("Nova heard it.")));
    let requests = ink_requests.lock().unwrap();
    assert_eq!(requests.len(), 4);
    assert!(requests[3].contains("pre-contact fixture"));
    assert!(
        requests
            .iter()
            .all(|request| !request.contains("/stt/websocket"))
    );
    drop(requests);
    host.kill();

    // The own engine also has one identity, but does not exhaust a cloud provider.
    seed(&format!("local,server-default,faster-whisper:small,{NOVA}"));
    let host = start("cat");
    assert_eq!(hearing(&host), ["local", NOVA]);
    wait_for("the own stand-in to hear the clip", || {
        let (status, said) = hear(&host, "admin", None, b"abc");
        assert_eq!(status, 200, "{said}");
        said["text"] == "Open the log of 3 bytes."
    });
    let before = own_count();
    let (status, said) = hear(&host, "admin", None, b"ab");
    assert_eq!((status, &said["text"]), (200, &json!("Nova heard it.")));
    assert_eq!(own_count(), before + 1);
    wait_for("each duplicate own alias to be logged", || {
        let heard = host.heard.lock().unwrap();
        ["server-default", "faster-whisper:small"]
            .iter()
            .all(|alias| {
                heard.contains(&format!(
                    "sttEngineRepeatSkipped session={session} {alias} (listed before)"
                ))
            })
    });
    host.kill();

    // Ink-2 receives the language, then its local decoder fails before any audio send.
    // Its batch sibling must still see this recording, before the final Nova fallback.
    let decode = place.root.join("reject-audio");
    program(&decode, "#!/bin/sh\nexit 1\n");
    seed(&format!("{INK_TWO},{INK_WHISPER},{NOVA}"));
    let host = start(decode.to_str().unwrap());
    let before = ink_requests.lock().unwrap().len();
    let (status, said) = hear(&host, "admin", None, b"local decoder refusal");
    assert_eq!((status, &said["text"]), (200, &json!("Nova heard it.")));
    let requests = ink_requests.lock().unwrap();
    assert_eq!(requests.len(), before + 1);
    assert!(requests[before].contains("local decoder refusal"));
    assert!(
        requests
            .iter()
            .all(|request| !request.contains("/stt/websocket"))
    );
}
