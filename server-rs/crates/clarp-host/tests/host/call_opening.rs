//! A call's opening whose agent cannot be started (issue 769): one file a piece, declared in
//! `host.rs`.

use super::*;

/// What the shim of each attempt of the agent's turns said of starting its tool.
fn shim_events(spool: &std::path::Path) -> Vec<String> {
    let mut events = Vec::new();
    for path in std::fs::read_dir(spool)
        .into_iter()
        .flatten()
        .flatten()
        .map(|file| file.path())
    {
        if path.extension().is_some_and(|ext| ext == "jsonl") {
            let text = std::fs::read_to_string(path).unwrap_or_default();
            for row in text
                .lines()
                .filter_map(|line| serde_json::from_str::<Value>(line).ok())
            {
                if row["s"] == "shim"
                    && let Some(event) = row["d"]["event"].as_str()
                {
                    events.push(event.to_owned());
                }
            }
        }
    }
    events
}

#[test]
fn an_opening_that_cannot_start_its_agent_tries_four_times_and_tells_the_caller_why() {
    let place = Place::new("opening-gives-up");
    let speaker = cartesia_stand_in(vec![7; 5000]);
    let settings = place.root.join("speech.toml");
    std::fs::write(
        &settings,
        "[tts]\nprovider='cartesia'\n[cartesia]\napi_key='fixture'\n[cartesia.voices]\nAlma='fixture'\n",
    )
    .unwrap();
    let args = ["--speech-keys", settings.to_str().unwrap(), "--keep-spools"];
    let env = [
        ("CLARP_CARTESIA_ENDPOINT", speaker.address.as_str()),
        ("OPENAI_API_KEY", ""),
        ("ELEVEN_API_KEY", ""),
        ("GEMINI_API_KEY", ""),
    ];
    let host = place.start_in("test-only", &args, &env);
    let (phone, _) = pair(&host, "Phone", "full");
    let folder = place.root.join("gone-work");
    std::fs::create_dir(&folder).unwrap();
    let made = host.post(
        "/agents",
        json!({"name":"Second","backend":"scripted","cwd":folder}),
    );
    let session = made["session"].as_str().unwrap().to_string();
    let spool = place
        .root
        .join("data/agents")
        .join(made["agent"]["agent_id"].as_str().unwrap())
        .join("spool");
    let rung = host.post(
        "/calls/ring",
        json!({"agent":session,"opening_mode":"listen_first","reason":"Water","opening_line":"Hello?"}),
    )["ring"]
        .clone();
    let answer = json!({"ring_id":rung["ring_id"],"opening_mode":"listen_first"});
    let op = host
        .ask_as(&phone, "POST", "/calls/ring/answer", Some(&answer))
        .1["opening"]
        .clone();
    // The agent's folder is gone before its first try, and stays gone.
    std::fs::remove_dir(&folder).unwrap();
    let silence = json!({"claim":"silence","request_id":"silence","epoch":op["epoch"],
        "token":op["token"],"at_ms":1250,"watermark_ms":1250});
    let claimed = host.ask_as(&phone, "POST", "/calls/ring/opening", Some(&silence));
    assert_eq!(claimed.0, 200, "{}", claimed.1);
    let began = Instant::now();

    let told = || {
        host.turns(&session).into_iter().find(|row| {
            row["text"]
                .as_str()
                .is_some_and(|text| text.starts_with("The agent could not be started to open"))
        })
    };
    wait_for("the opening to give up and tell its caller", || {
        told().is_some()
    });
    let took = began.elapsed();
    let failed = |events: Vec<String>| events.iter().filter(|e| *e == "spawn-failed").count();
    assert_eq!(failed(shim_events(&spool)), 4, "four tries, no more");
    // One second, then two, then four between the tries: not one each second for the call.
    assert!(took >= Duration::from_secs(6), "{took:?}");
    let text = told().unwrap()["text"].as_str().unwrap().to_owned();
    assert!(
        text.contains(&format!("{} no longer exists", folder.display())),
        "{text}"
    );
    assert!(text.ends_with("Speak to try again."), "{text}");

    // Nothing is tried again.
    std::thread::sleep(Duration::from_secs(3));
    assert_eq!(failed(shim_events(&spool)), 4);
    assert!(!shim_events(&spool).iter().any(|event| event == "spawned"));
}
