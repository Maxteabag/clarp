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
    let (fallback, fallback_calls) = answers(vec![("/v2/speak?", "200 OK", "fallback audio".into())]);
    let owners = place.root.join("synthetic-speech.toml");
    let configure = |voice: &str, model: &str| {
        std::fs::write(&owners,format!("[tts]\nprovider='cartesia'\nfallback='deepgram'\n[cartesia]\napi_key='fixture'\nmodel='{model}'\n[deepgram]\napi_key='fixture'\nmodel='flux-haley-en'\n[tts.agents.mike]\ncartesia_voice='{voice}'\n[tts.agents.anna]\ncartesia_voice='{voice}'\n[tts.agents.clarp]\ncartesia_voice='{voice}'\n")).unwrap();
    };
    configure("voice-a","model-a");
    let start = || place.start_in("", &["--speech-keys",owners.to_str().unwrap()], &[
        ("CLARP_CLAUDE_PROGRAM",claude.to_str().unwrap()),
        ("CLARP_CARTESIA_ENDPOINT",&primary.address), ("CLARP_DEEPGRAM_VOICE_ENDPOINT",&fallback),
        ("CARTESIA_API_KEY",""), ("ELEVEN_API_KEY",""), ("DEEPGRAM_API_KEY","")
    ]);
    let host = start();
    let mike = host.new_agent("Mike");
    let anna = host.new_agent("Anna");
    host.post("/orchestrator/settings",json!({"enabled":true,"fallback_only":false,"provider":"claude"}));
    let press = |host: &Host, session: &str, text: &str, id: &str| {
        let (status, body) = host.ask("POST","/send",Some(&json!({"session":session,"text":text,"hands_free":true,"client_msg_id":id})));
        assert_eq!(status,200,"{body}");
    };
    let heard = host.listen();
    // Hold the first speech request while the second routing decision is made.
    primary.held.store(true,Ordering::SeqCst);
    std::thread::scope(|scope| {
        let first = scope.spawn(|| press(&host,&mike,"fixed first","p1"));
        wait_for("one speech request", || !primary.asked().is_empty());
        let second = scope.spawn(|| press(&host,&anna,"fixed second","p2"));
        wait_for("the second routing request", || std::fs::read_to_string(&asked).is_ok_and(|s| s.contains("fixed second")));
        primary.held.store(false,Ordering::SeqCst);
        first.join().unwrap();
        second.join().unwrap();
    });
    assert_eq!(primary.asked().len(),1);
    press(&host,&anna,"fixed another","p3");
    wait_for("a clip served to Anna", || heard.lock().unwrap().iter().any(|e| e["cached_phrase"]==true && e["session"]==anna));
    let event = heard.lock().unwrap().iter().find(|e| e["cached_phrase"]==true && e["session"]==anna).unwrap().clone();
    assert_eq!(event["phrase_key"],"no_such_agent");
    assert!(event["agent_id"].as_str().is_some_and(|s| !s.is_empty()));
    let file = event["url"].as_str().unwrap();
    let response = fetch(&host,"","GET",file,&[("Range","bytes=12-21")],&[]);
    assert_eq!((response.status,response.body),(206,vec![7;10]));
    press(&host,&mike,"status someone missing","p4");
    wait_for("the router voice's cached event", || heard.lock().unwrap().iter().any(|e| e["cached_phrase"]==true && e["session"]=="" && e["agent_id"].is_null()));
    assert_eq!(primary.asked().len(),1);
    for id in ["free1","free2"] {press(&host,&mike,"free prose",id);}
    assert_eq!(primary.asked().len(),3);
    let before = heard.lock().unwrap().iter().filter(|e| e["type"]=="audio").count();
    let (_, response) = host.ask("POST","/send",Some(&json!({"session":mike,"text":"fixed recipient","orchestrator_fallback":true,"hands_free":true,"client_msg_id":"silent"})));
    assert!(response.get("orchestrator").is_some(),"{response}");
    assert_eq!(heard.lock().unwrap().iter().filter(|e| e["type"]=="audio").count(),before);
    host.kill();
    let stored = kept_records(&place,"router-phrase");
    assert_eq!(stored["clips"].as_array().unwrap().len(),1);
    let host = start();
    press(&host,&mike,"fixed restored","p5");
    assert_eq!(primary.asked().len(),3);
    host.kill();
    let mut stale = stored["clips"].clone();
    stale[0]["text"] = json!("An obsolete phrase.");
    keep_record(&place,"router-phrase","clips",&stale);
    let host = start();
    press(&host,&mike,"fixed stale","p6");
    assert_eq!(primary.asked().len(),4);
    assert!(!host.heard.lock().unwrap().contains("obsolete phrase"));
    configure("voice-b","model-a");
    press(&host,&mike,"fixed different voice","p7");
    assert_eq!(primary.asked().last().unwrap()["voice"]["id"],"voice-b");
    configure("voice-b","model-b");
    primary.down.store(true,Ordering::SeqCst);
    for id in ["fallback1","fallback2"] {press(&host,&mike,"fixed fallback",id);}
    assert_eq!(fallback_calls.lock().unwrap().len(),2);
    primary.down.store(false,Ordering::SeqCst);
    press(&host,&mike,"fixed recovered","p8");
    let requests = primary.asked();
    assert_eq!(requests.len(),8);
    assert_eq!(requests.last().unwrap()["model_id"],"model-b");
    press(&host,&mike,"fixed recovered hit","p9");
    assert_eq!(primary.asked().len(),8);
}
