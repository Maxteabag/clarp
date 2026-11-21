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
    // A table with slips in it (the next test reads what the Host says of them): what is refused
    // leaves the name with what it had; a built-in name in other letters is another name.
    let slips = SLIPS;
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
        (slips, Some("Ios/1 contract=75"), "gpt-live-1.1"),
        (slips, Some("ios/2985 contract=75"), "gpt-live-1.1"),
        (slips, Some("android/120 contract=29"), "gpt-live-1.1"),
        (slips, Some("desktop/1 contract=99"), "gpt-live-1"),
        (NO_TABLE, Some("ios/2985 contract=75"), "gpt-live-1.1"),
        (NO_TABLE, Some("ios/2984 contract=74"), "gpt-live-1"),
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

/// An owner's table of minimums with a slip in each way there is: a key that is no client's name
/// (a blank, a digit first, nothing, a letter outside ASCII, 40 signs, 50 signs), a value that is
/// no whole number of at least 0 (a text, below 0, true, a fraction), and a built-in name in other
/// letters, twice.
const SLIPS: &str = "[openai]\nlive_model_min_client = { \"my client\" = 3, Ios = 75, desktop = \"3\", \
    Desktop = 4, IOS = 80, \"\" = 1, android = -1, \"1st\" = 5, \"caf\\u00e9\" = 6, \
    jjjjjjjjjjjjjjjjjjjjjjjjjjjjjjjjjjjjjjjj = 7, \
    kkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkk = 2, phone = true, watch = 1.5 }\n";

/// An owner's file whose table of minimums is no table: the built-in minimums stand.
const NO_TABLE: &str = "[openai]\nlive_model_min_client = 3\n";

/// The owner's table speaks when it is read, once, so that a slip in his file does not hide for
/// weeks, in the words of the one reader of such tables (a render spec's speaks alike): one line
/// names every entry refused, in the table's order of keys (each quoted, a long one cut short),
/// and one line names each key taken that differs from a built-in name only in its letters. A
/// value that is no table is ignored in one line. A table without a slip says nothing.
#[test]
fn the_owners_table_of_minimums_says_what_it_refused_and_what_is_a_name_in_other_letters() {
    const ADMIN: &str = "the-administrators-token";
    const TABLE: &str = "clarp-host: config.live_model_min_client: ";
    let refused = format!(
        "{TABLE}refused entries (a client's name as its header sends it, and a whole number of at \
         least 0): '', '1st', 'android', 'caf?', 'desktop', '{}', '{}...', 'my client', \
         'phone', 'watch'",
        "j".repeat(40),
        "k".repeat(40)
    );
    let cased = |key: &str| {
        format!(
            "{TABLE}'{key}' is taken as written: it counts for a client that sends '{key}', not \
             for 'ios'"
        )
    };
    let (upper, title) = (cased("IOS"), cased("Ios"));
    let no_table = format!("{TABLE}ignored: expected a table of client = number");
    for (file, lines) in [
        (
            SLIPS,
            vec![refused.as_str(), upper.as_str(), title.as_str()],
        ),
        (
            "[openai]\nlive_model_min_client = { ios = 80, desktop = 0 }\n",
            vec![],
        ),
        (NO_TABLE, vec![no_table.as_str()]),
        ("", vec![]),
    ] {
        let place = Place::new("live-model-table");
        let owners = place.root.join("owners.toml");
        std::fs::write(&owners, file).unwrap();
        let host = place.start_in(ADMIN, &["--speech-keys", owners.to_str().unwrap()], &[]);
        let _ = host.get("/oracle/status");
        let said = host.heard.lock().unwrap().clone();
        let told: Vec<&str> = said
            .lines()
            .filter(|line| line.contains("config.live_model_min_client"))
            .collect();
        assert_eq!(told, lines, "{file:?}: {said}");
    }
}
