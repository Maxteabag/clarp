use super::*;

#[test]
fn the_router_reuses_only_its_own_words_in_the_voice_that_made_them() {
    let place = Place::new("router-phrases");
    let words = "I do not have an agent by that name.";
    let rules = json!([
        ["free", {"kind":"clarify","confidence":0.95,"spoken_text":"What about Tuesday?","phrase_key":"no_such_agent"}],
        ["status", {"kind":"status_query","confidence":0.95,"target_session":"missing"}],
        ["fixed", {"kind":"clarify","confidence":0.95,"spoken_text":words,"phrase_key":"repeat_missing"}]
    ]);
    let (_, claude, asked) = routing_model(&place, &rules);
    let bytes = vec![7; 2000];
    let primary = cartesia_stand_in(bytes.clone());
    let (fallback, fallback_calls) =
        answers(vec![
            ("/v2/speak?model=flux-mike", "200 OK", "Mike speaks".into()),
            ("/v2/speak?model=flux-anna", "200 OK", "Anna speaks".into()),
        ]);
    let owners = place.root.join("synthetic-speech.toml");
    let configure = |voice: &str, model: &str| {
        std::fs::write(&owners,format!("[tts]\nprovider='cartesia'\nfallback='deepgram'\n[cartesia]\napi_key='fixture'\nmodel='{model}'\n[deepgram]\napi_key='fixture'\nmodel='flux-haley-en'\n[tts.agents.mike]\ncartesia_voice='{voice}'\n[tts.agents.anna]\ncartesia_voice='{voice}'\n[tts.agents.clarp]\ncartesia_voice='{voice}'\n")).unwrap();
    };
    configure("voice-a", "model-a");
    let start = || {
        place.start_in(
            "",
            &["--speech-keys", owners.to_str().unwrap()],
            &[
                ("CLARP_CLAUDE_PROGRAM", claude.to_str().unwrap()),
                ("CLARP_CARTESIA_ENDPOINT", &primary.address),
                ("CLARP_DEEPGRAM_VOICE_ENDPOINT", &fallback),
                ("CARTESIA_API_KEY", ""),
                ("ELEVEN_API_KEY", ""),
                ("DEEPGRAM_API_KEY", ""),
            ],
        )
    };
    let host = start();
    let mike = host.new_agent("Mike");
    let anna = host.new_agent("Anna");
    host.post(
        "/orchestrator/settings",
        json!({"enabled":true,"fallback_only":false,"provider":"claude"}),
    );
    let press = |host: &Host, session: &str, text: &str, id: &str| {
        let (status, body) = host.ask(
            "POST",
            "/send",
            Some(&json!({"session":session,"text":text,"hands_free":true,"client_msg_id":id})),
        );
        assert_eq!(status, 200, "{body}");
    };
    let heard = host.listen();
    // Hold the first speech request while the second routing decision is made.
    primary.held.store(true, Ordering::SeqCst);
    std::thread::scope(|scope| {
        let first = scope.spawn(|| press(&host, &mike, "fixed first", "p1"));
        wait_for("one speech request", || !primary.asked().is_empty());
        let second = scope.spawn(|| press(&host, &anna, "fixed second", "p2"));
        wait_for("the second routing request", || {
            std::fs::read_to_string(&asked).is_ok_and(|s| s.contains("fixed second"))
        });
        primary.held.store(false, Ordering::SeqCst);
        first.join().unwrap();
        second.join().unwrap();
    });
    assert_eq!(primary.asked().len(), 1);
    press(&host, &anna, "fixed another", "p3");
    wait_for("a clip served to Anna", || {
        heard
            .lock()
            .unwrap()
            .iter()
            .any(|e| e["cached_phrase"] == true && e["session"] == anna)
    });
    let event = heard
        .lock()
        .unwrap()
        .iter()
        .find(|e| e["cached_phrase"] == true && e["session"] == anna)
        .unwrap()
        .clone();
    assert_eq!(event["phrase_key"], "no_such_agent");
    assert!(event["agent_id"].as_str().is_some_and(|s| !s.is_empty()));
    let file = event["url"].as_str().unwrap();
    let response = fetch(&host, "", "GET", file, &[("Range", "bytes=12-21")], &[]);
    assert_eq!((response.status, response.body), (206, vec![7; 10]));
    press(&host, &mike, "status someone missing", "p4");
    wait_for("the router voice's cached event", || {
        heard
            .lock()
            .unwrap()
            .iter()
            .any(|e| e["cached_phrase"] == true && e["session"] == "" && e["agent_id"].is_null())
    });
    assert_eq!(primary.asked().len(), 1);
    for id in ["free1", "free2"] {
        press(&host, &mike, "free prose", id);
    }
    assert_eq!(primary.asked().len(), 3);
    wait_for("both free questions announced", || {
        heard
            .lock()
            .unwrap()
            .iter()
            .filter(|e| e["type"] == "audio")
            .count()
            >= 6
    });
    let before = heard
        .lock()
        .unwrap()
        .iter()
        .filter(|e| e["type"] == "audio")
        .count();
    let (_, response) = host.ask("POST","/send",Some(&json!({"session":mike,"text":"fixed recipient","orchestrator_fallback":true,"hands_free":true,"client_msg_id":"silent"})));
    assert!(response.get("orchestrator").is_some(), "{response}");
    assert_eq!(
        heard
            .lock()
            .unwrap()
            .iter()
            .filter(|e| e["type"] == "audio")
            .count(),
        before
    );
    host.kill();
    let stored = kept_records(&place, "router-phrase");
    assert_eq!(stored["clips"].as_array().unwrap().len(), 1);
    let host = start();
    press(&host, &mike, "fixed restored", "p5");
    assert_eq!(primary.asked().len(), 3);
    host.kill();
    // An absent or modified blob is never sent under its old address.
    use clarp_ports::blobs::BlobEngine as _;
    let blobs = clarp_store_files::FileBlobs::new()
        .open(&place.root.join("data/media"))
        .unwrap();
    let blob_name = file.strip_prefix("/audio/").unwrap();
    for damage in ["absent", "modified"] {
        blobs.remove(blob_name).unwrap();
        if damage == "modified" {
            blobs.put(blob_name, &vec![9; bytes.len()]).unwrap();
        }
        let host = start();
        assert_eq!(fetch(&host, "", "GET", file, &[], &[]).status, 404);
        assert!(
            host.heard
                .lock()
                .unwrap()
                .contains("unreadable phrase clip")
        );
        press(&host, &mike, "fixed repair", damage);
        wait_for("the repaired blob", || {
            fetch(&host, "", "GET", file, &[], &[]).body == bytes
        });
        host.kill();
    }
    let mut stale = stored["clips"].clone();
    stale[0]["text"] = json!("An obsolete phrase.");
    keep_record(&place, "router-phrase", "clips", &stale);
    let host = start();
    assert_eq!(fetch(&host, "", "GET", file, &[], &[]).status, 404);
    press(&host, &mike, "fixed stale", "p6");
    assert_eq!(primary.asked().len(), 6);
    assert!(!host.heard.lock().unwrap().contains("obsolete phrase"));
    configure("voice-b", "model-a");
    press(&host, &mike, "fixed different voice", "p7");
    assert_eq!(primary.asked().last().unwrap()["voice"]["id"], "voice-b");
    configure("voice-b", "model-b");
    primary.down.store(true, Ordering::SeqCst);
    let heard = host.listen();
    for (session, voice) in [(&mike, "flux-mike"), (&anna, "flux-anna")] {
        host.post(
            "/agent-voice",
            json!({"session":session, "provider":"deepgram", "voice_id":voice}),
        );
    }
    std::thread::scope(|scope| {
        scope.spawn(|| press(&host, &mike, "fixed fallback", "fallback1"));
        scope.spawn(|| press(&host, &anna, "fixed fallback", "fallback2"));
    });
    for (session, expected) in [(&mike, "Mike speaks"), (&anna, "Anna speaks")] {
        wait_for("the requester's own fallback voice", || {
            let events = heard.lock().unwrap().clone();
            events.iter().any(|e| {
                e["type"] == "audio"
                    && e["session"].as_str() == Some(session.as_str())
                    && e["herald"] == true
                    && fetch(&host, "", "GET", e["url"].as_str().unwrap(), &[], &[]).body
                        == expected.as_bytes()
            })
        });
    }
    press(&host, &mike, "fixed fallback again", "fallback3");
    assert_eq!(fallback_calls.lock().unwrap().len(), 3);
    let before_recovery = primary.asked().len();
    primary.down.store(false, Ordering::SeqCst);
    press(&host, &mike, "fixed recovered", "p8");
    let requests = primary.asked();
    assert_eq!(requests.len(), before_recovery + 1);
    assert_eq!(requests.last().unwrap()["model_id"], "model-b");
    press(&host, &mike, "fixed recovered hit", "p9");
    assert_eq!(primary.asked().len(), before_recovery + 1);
}

#[test]
fn router_phrase_eviction_releases_only_unreferenced_audio() {
    use clarp_ports::blobs::BlobEngine as _;
    use sha2::{Digest, Sha256};
    let place = Place::new("phrase-eviction");
    let words = "I could not do that.";
    let (_, claude, _) = routing_model(
        &place,
        &json!([["fixed", {"kind":"clarify","confidence":0.95,"spoken_text":words}]]),
    );
    let owners = place.root.join("fixture.toml");
    std::fs::write(&owners,"[tts]\nprovider='cartesia'\nfallback='none'\n[cartesia]\napi_key='fixture'\n[tts.agents.mike]\ncartesia_voice='new'\n").unwrap();
    let sound = vec![4; 256 * 1024];
    let primary = cartesia_stand_in(sound.clone());
    let oversized = cartesia_stand_in(vec![5; 256 * 1024 + 1]);
    let start = |engine: &StandInVoice| {
        place.start_in(
            "",
            &["--speech-keys", owners.to_str().unwrap()],
            &[
                ("CLARP_CLAUDE_PROGRAM", claude.to_str().unwrap()),
                ("CLARP_CARTESIA_ENDPOINT", &engine.address),
                ("CARTESIA_API_KEY", ""),
                ("ELEVEN_API_KEY", ""),
            ],
        )
    };
    let host = start(&primary);
    let mike = host.new_agent("Mike");
    host.post(
        "/orchestrator/settings",
        json!({"enabled":true,"fallback_only":false,"provider":"claude"}),
    );
    let press = |host: &Host, id: &str| {
        let (status, body) = host.ask("POST","/send",Some(&json!({"session":mike,"text":format!("fixed {id}"),"client_msg_id":id,"hands_free":true})));
        assert_eq!(status, 200, "{body}");
    };
    host.kill();
    let blobs = clarp_store_files::FileBlobs::new()
        .open(&place.root.join("data/media"))
        .unwrap();
    let blob = |bytes: &[u8]| {
        let name = format!("phrase-{:x}.mp3", Sha256::digest(bytes));
        blobs.put(&name, bytes).unwrap();
        name
    };
    for (round, count, size) in [("byte", 256, 256 * 1024), ("count", 1024, 1)] {
        let oldest = blob(&vec![1; size]);
        let shared = blob(&vec![2; size]);
        let rows: Vec<_> = (0..count).map(|i| json!({"key":{"phrase":"command_refused","voice":{"provider":"cartesia","voice":format!("old-{i}"),"model":"synthetic"}},"name":if i==0 {&oldest} else {&shared}, "size":size,"text":words})).collect();
        keep_record(&place, "router-phrase", "clips", &json!(rows));
        let host = start(&primary);
        press(&host, round);
        let read = |name: &str| {
            fetch(
                &host,
                "",
                "GET",
                &format!("/audio/{name}"),
                &[("Range", "bytes=0-0")],
                &[],
            )
        };
        wait_for("the cold fill to evict the oldest clip", || {
            read(&oldest).status == 404
        });
        assert_eq!(read(&shared).body, vec![2]);
        assert_eq!(
            read(&format!("phrase-{:x}.mp3", Sha256::digest(&sound))).body,
            vec![4]
        );
        host.kill();
        let kept = kept_records(&place, "router-phrase");
        let rows = kept["clips"].as_array().unwrap();
        assert_eq!(rows.len(), count);
        assert_eq!(rows[0]["key"]["voice"]["voice"], "old-1");
        assert_eq!(rows.last().unwrap()["key"]["voice"]["voice"], "new");
        assert_eq!(blobs.len(&oldest).unwrap(), None);
        assert_eq!(blobs.len(&shared).unwrap(), Some(size as u64));
    }
    let mut kept = kept_records(&place, "router-phrase")["clips"]
        .as_array()
        .unwrap()
        .clone();
    kept.pop();
    kept.push(json!({"corrupt":true}));
    keep_record(&place, "router-phrase", "clips", &json!(kept));
    let host = start(&oversized);
    let heard = host.listen();
    press(&host, "large-one");
    press(&host, "large-two");
    assert_eq!(oversized.asked().len(), 2);
    wait_for("both oversized clips announced normally", || {
        heard
            .lock()
            .unwrap()
            .iter()
            .filter(|e| e["type"] == "audio" && e["herald"] == true)
            .count()
            >= 2
    });
    assert!(host.heard.lock().unwrap().contains("invalid phrase record"));
    host.kill();
    assert_eq!(
        kept_records(&place, "router-phrase")["clips"]
            .as_array()
            .unwrap()
            .len(),
        1023
    );
}
