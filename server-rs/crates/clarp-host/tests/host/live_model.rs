//! The Host's tests of the model of GPT-Live it runs: one file a piece, declared in `host.rs`.

use super::*;

/// The model the owner names in his file (`[openai] live_model`, read once), one of the two
/// there are, or the default: what the Host advertises of the Oracle's calls and what a call
/// opens its voice with are one value from one place, whatever the file says. Another name is
/// refused when the file is read, in one line, and the default runs.
#[test]
fn the_oracles_model_is_the_owners_and_what_is_advertised_is_what_a_call_opens_with() {
    const ADMIN: &str = "the-administrators-token";
    for (named, runs, refused) in [
        (Some("\"gpt-live-1\""), "gpt-live-1", false),
        (Some("\"gpt-live-1.1\""), "gpt-live-1.1", false),
        (None, "gpt-live-1.1", false),
        (Some("\"gpt-live-2\""), "gpt-live-1.1", true),
        (Some("11"), "gpt-live-1.1", true),
    ] {
        let place = Place::new("live-model");
        let owners = place.root.join("owners.toml");
        let file = named.map_or(String::new(), |named| {
            format!("[openai]\nlive_model = {named}\n")
        });
        std::fs::write(&owners, file).unwrap();
        let voice = stand_in_oracle_voice();
        let router = stand_in_oracle_router();
        let world = [
            ("CLARP_LIVE_ENDPOINT", voice.address.as_str()),
            ("CLARP_ROUTER_ENDPOINT", router.address.as_str()),
            ("OPENAI_API_KEY", "a-key-of-the-test"),
        ];
        let host = place.start_in(ADMIN, &["--speech-keys", owners.to_str().unwrap()], &world);
        let can = host.get("/oracle/status");
        assert_eq!(
            (&can["v2"]["model"], &can["v2_tinkered"]["model"]),
            (&json!(runs), &json!(runs)),
            "{named:?}"
        );
        let (phone, _) = pair(&host, "Phone", "full");
        let door = socket_at(&host, &phone, "/oracle/v2?new_conversation=1");
        let _call = End::of_a_phone(door.expect("a call is opened"));
        let begun = voice.call(0).told_of("session.start", 1)[0]["session"].clone();
        assert_eq!(
            begun["model"], can["v2"]["model"],
            "{named:?}: one value, one place"
        );
        let said = host.heard.lock().unwrap().clone();
        let lines = said.matches("config.openai.live_model refused").count();
        assert_eq!(lines, usize::from(refused), "{named:?}: {said}");
    }
}

/// An agent's live call runs on the owner's model only for a client that says it accepts it:
/// exactly `ios` from contract 75, exactly `android` from 29, or what the owner's file says of a
/// name; every other client (below its number, another name, a name in other letters, no
/// header, a malformed one) is given `gpt-live-1`, as every app always was. The model is chosen
/// when the grant is made, for the client that asks, and kept with the call: the grant, a repeat
/// of it, the session's frame on the socket and the voice's own session name the same model, and
/// the grant differs in that alone.
#[test]
fn an_agents_live_call_runs_the_owners_model_only_for_a_client_that_accepts_it() {
    const ADMIN: &str = "the-administrators-token";
    const LIVE: &str = "agent_live_v1";
    let owned = "[openai]\nlive_model_min_client = { ios = 80 }\n";
    let older = "[openai]\nlive_model = \"gpt-live-1\"\n";
    // The table may name any client, by the header's name exactly as written (the release owner's
    // ruling): `Desktop` admits `Desktop/...`, never `desktop/...`.
    let named = "[openai]\nlive_model_min_client = { Desktop = 3 }\n";
    for (file, client, runs) in [
        ("", Some("ios/2985 contract=75"), "gpt-live-1.1"),
        ("", Some("ios/2984 contract=74"), "gpt-live-1"),
        ("", Some("android/120 contract=29"), "gpt-live-1.1"),
        ("", Some("android/119 contract=28"), "gpt-live-1"),
        ("", Some("IOS/2985 contract=99"), "gpt-live-1"),
        ("", Some("desktop/1 contract=200"), "gpt-live-1"),
        ("", None, "gpt-live-1"),
        ("", Some("ios/2985 contract="), "gpt-live-1"),
        (owned, Some("ios/2990 contract=79"), "gpt-live-1"),
        (owned, Some("ios/2991 contract=80"), "gpt-live-1.1"),
        (owned, Some("android/120 contract=29"), "gpt-live-1.1"),
        (older, Some("ios/2985 contract=75"), "gpt-live-1"),
        (named, Some("Desktop/1 contract=3"), "gpt-live-1.1"),
        (named, Some("Desktop/1 contract=2"), "gpt-live-1"),
        (named, Some("desktop/1 contract=3"), "gpt-live-1"),
    ] {
        let place = Place::new("live-model-ring");
        let owners = place.root.join("owners.toml");
        std::fs::write(&owners, file).unwrap();
        let voice = stand_in_live_voice();
        let world = [
            ("CLARP_LIVE_ENDPOINT", voice.address.as_str()),
            ("OPENAI_API_KEY", "a-key-of-the-test"),
        ];
        let host = place.start_in(ADMIN, &["--speech-keys", owners.to_str().unwrap()], &world);
        let theo = host.new_agent("Theo");
        let agents = host.get("/agents/snapshot")["agents"].clone();
        let row = agents
            .as_array()
            .unwrap()
            .iter()
            .find(|row| row["session"] == theo.as_str());
        let theo_id = row.unwrap()["agent_id"].as_str().unwrap().to_string();
        let calls =
            json!({ "user_call_default": "live", "user_call_opening": "Hei, det er Theo." });
        live_profile(&place, "theo", &theo, calls);
        let (phone, _) = pair(&host, "Phone", "full");
        let token =
            json!({ "token": "a1".repeat(32), "supported_conversation_modes": ["classic", LIVE] });
        assert_eq!(
            host.ask_as(&phone, "POST", "/calls/ring/voip-token", Some(&token))
                .0,
            200
        );
        let call = json!({ "agent": theo_id, "request_id": "call-1", "client_nonce": "nonce-1" });
        let headers: Vec<(&str, &str)> = client
            .map(|said| ("X-Clarp-Client", said))
            .into_iter()
            .collect();
        let ask = || {
            let got = fetch(
                &host,
                &phone,
                "POST",
                "/calls/live",
                &headers,
                call.to_string().as_bytes(),
            );
            assert_eq!(got.status, 200, "{client:?}");
            serde_json::from_slice::<Value>(&got.body).unwrap()["live"].clone()
        };
        let grant = ask();
        assert_eq!(grant["model"], runs, "{file:?} {client:?}");
        // The grant differs in its model alone: who gives it, its voice and who speaks first.
        assert_eq!(
            (&grant["provider"], &grant["voice"], &grant["opening_owner"]),
            (&json!("openai"), &json!("marin"), &json!("live"))
        );
        assert_eq!(ask(), grant, "a repeat of the answer keeps its model");
        let mut door = live_socket(&host, &phone);
        let heard = under_way(&mut door, &grant);
        let frame = heard
            .iter()
            .find(|said| said["type"] == "agent_live.session")
            .unwrap();
        assert_eq!(frame["model"], runs, "{client:?}: the session's frame");
        wait_for("the voice to be begun", || {
            !voice.told.lock().unwrap().is_empty()
        });
        let begun = voice.told.lock().unwrap()[0].clone();
        assert_eq!(
            begun["session"]["model"], runs,
            "{client:?}: the voice's session"
        );
    }
}

/// The same for a ring the agent places: its model is chosen when the phone that answers it is
/// given its grant, for that phone's client, and the voice is begun with it.
#[test]
fn a_rings_model_is_chosen_for_the_client_that_answers_it() {
    const ADMIN: &str = "the-administrators-token";
    const LIVE: &str = "agent_live_v1";
    for (client, runs) in [
        (Some("ios/2985 contract=75"), "gpt-live-1.1"),
        (Some("android/119 contract=28"), "gpt-live-1"),
        (None, "gpt-live-1"),
    ] {
        let place = Place::new("live-model-answer");
        let voice = stand_in_live_voice();
        let world = [
            ("CLARP_LIVE_ENDPOINT", voice.address.as_str()),
            ("OPENAI_API_KEY", "a-key-of-the-test"),
        ];
        let host = place.start_in(ADMIN, &[], &world);
        let theo = host.new_agent("Theo");
        live_profile(&place, "theo", &theo, json!({}));
        let (phone, _) = pair(&host, "Phone", "full");
        let token =
            json!({ "token": "a1".repeat(32), "supported_conversation_modes": ["classic", LIVE] });
        assert_eq!(
            host.ask_as(&phone, "POST", "/calls/ring/voip-token", Some(&token))
                .0,
            200
        );
        let line = "Camera 2 shows a possible water leak at pump 01.";
        let ring = json!({
            "agent": "theo", "reason": "Possible water leak", "opening_line": line,
            "conversation_mode": LIVE, "event_context": live_event("theo", "evt-1", line),
        });
        let (status, rung) = host.ask("POST", "/calls/ring", Some(&ring));
        assert_eq!(status, 200, "{rung}");
        let answer = json!({
            "ring_id": rung["ring"]["ring_id"], "conversation_mode": LIVE,
            "request_id": "answer-1", "client_nonce": "nonce-1",
        });
        let headers: Vec<(&str, &str)> = client
            .map(|said| ("X-Clarp-Client", said))
            .into_iter()
            .collect();
        let got = fetch(
            &host,
            &phone,
            "POST",
            "/calls/ring/answer",
            &headers,
            answer.to_string().as_bytes(),
        );
        assert_eq!(got.status, 200, "{client:?}");
        let grant = serde_json::from_slice::<Value>(&got.body).unwrap()["live"].clone();
        assert_eq!(grant["model"], runs, "{client:?}");
        let mut door = live_socket(&host, &phone);
        let heard = under_way(&mut door, &grant);
        let frame = heard
            .iter()
            .find(|said| said["type"] == "agent_live.session")
            .unwrap();
        assert_eq!(frame["model"], runs, "{client:?}: the session's frame");
        wait_for("the voice to be begun", || {
            !voice.told.lock().unwrap().is_empty()
        });
        let begun = voice.told.lock().unwrap()[0].clone();
        assert_eq!(
            begun["session"]["model"], runs,
            "{client:?}: the voice's session"
        );
    }
}
