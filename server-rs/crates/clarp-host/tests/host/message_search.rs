//! Message search (feature `message_search`, issue 803): `GET /search` finds the rows of the
//! conversations its caller may read by their words, newest first and a page at a time, from an
//! index the Host keeps as rows change and fills with older history in the background. A
//! removed agent is never found again; a bound agent's words are never found by a reader who may
//! not read them; a search while history is still being read says `partial`.

use super::bound_privacy::{CODEX, agent_id, bound_agent, first_turn_token, own_home};
use super::*;

fn search(host: &Host, query: &str) -> Value {
    host.get(&format!("/search?{query}"))
}

/// The hits of a search, once the index holds everything and has found what is awaited.
fn found(host: &Host, query: &str, awaited: usize) -> Vec<Value> {
    let mut hits = Vec::new();
    wait_for(&format!("{awaited} hits for {query}"), || {
        let answer = search(host, query);
        hits = answer["hits"].as_array().cloned().unwrap_or_default();
        answer["partial"] == false && hits.len() >= awaited
    });
    hits
}

fn ids(hits: &[Value]) -> Vec<String> {
    hits.iter()
        .map(|hit| hit["message_id"].as_str().unwrap().to_string())
        .collect()
}

#[test]
fn an_old_message_is_found_by_its_words_and_a_removed_chat_never_again() {
    let place = Place::new("message-search");
    let host = place.start();
    let info = host.get("/server-info");
    assert!(
        info["features"].to_string().contains("message_search"),
        "{info}"
    );
    let ada = host.new_agent("Ada");
    let ben = host.new_agent("Ben");
    host.send(&ada, "Shall we meet in Oslo on Friday?", "s-1");
    wait_for("Ada's answer", || host.state(&ada) == "done");
    host.send(&ben, "The train to Bergen leaves at nine", "s-2");
    wait_for("Ben's answer", || host.state(&ben) == "done");
    host.send(&ada, "Møtet på kontoret er flyttet", "s-3");
    wait_for("Ada's second answer", || host.conversation(&ada).len() >= 4);

    // The message and the answer that repeats it are both found, newest first, each with the
    // id its row has in /log and the words that matched marked.
    let hits = found(&host, "q=oslo", 2);
    assert_eq!(hits.len(), 2, "{hits:?}");
    let rows: Vec<Value> = host.turns(&ada);
    let row_ids: Vec<&str> = rows.iter().map(|row| row["id"].as_str().unwrap()).collect();
    for hit in &hits {
        assert_eq!(hit["session"], ada.as_str(), "{hit}");
        assert_eq!(hit["name"], "Ada", "{hit}");
        assert!(
            row_ids.contains(&hit["message_id"].as_str().unwrap()),
            "{hit} is a row of {row_ids:?}"
        );
        let parts = hit["parts"].as_array().unwrap();
        assert!(
            parts
                .iter()
                .any(|part| part["match"] == true && part["text"] == "Oslo"),
            "{hit}"
        );
        assert!(hit["snippet"].as_str().unwrap().contains("Oslo"), "{hit}");
    }
    assert_eq!(hits[0]["role"], "assistant", "{hits:?}");
    assert_eq!(hits[1]["role"], "user", "{hits:?}");
    assert!(hits[0]["timestamp"].as_str().unwrap() >= hits[1]["timestamp"].as_str().unwrap());

    // Norwegian words, without their accents too, and every word asked must be there.
    assert_eq!(found(&host, "q=m%C3%B8tet%20pa", 2).len(), 2);
    assert!(
        search(&host, "q=oslo%20bergen")["hits"]
            .as_array()
            .unwrap()
            .is_empty()
    );

    // One agent's conversation alone.
    assert!(
        search(&host, &format!("q=bergen&session={ada}"))["hits"]
            .as_array()
            .unwrap()
            .is_empty()
    );
    assert_eq!(found(&host, &format!("q=bergen&session={ben}"), 2).len(), 2);

    // A page at a time, without a gap or a repeat.
    let first = search(&host, "q=oslo&limit=1");
    assert_eq!(first["hits"].as_array().unwrap().len(), 1, "{first}");
    let next = first["next_before"].as_str().unwrap();
    let second = search(&host, &format!("q=oslo&limit=1&before={next}"));
    assert_eq!(second["hits"].as_array().unwrap().len(), 1, "{second}");
    assert!(second["next_before"].is_null(), "{second}");
    let paged = [
        ids(first["hits"].as_array().unwrap()),
        ids(second["hits"].as_array().unwrap()),
    ]
    .concat();
    assert_eq!(paged, ids(&hits));

    // Nothing to search for, and a cursor the Host never gave, are refused in words.
    for query in ["q=", "q=%20%22", "q=oslo&before=yesterday"] {
        let (status, said) = host.ask("GET", &format!("/search?{query}"), None);
        assert_eq!(status, 400, "{query}: {said}");
    }

    // A removed agent's conversation is never found again, from the moment it is removed.
    assert_eq!(host.ask("DELETE", &format!("/agents/{ben}"), None).0, 200);
    let after = search(&host, "q=bergen");
    assert!(after["hits"].as_array().unwrap().is_empty(), "{after}");
    // Nor once the index has been looked over, nor by its id.
    std::thread::sleep(Duration::from_secs(3));
    assert!(
        search(&host, "q=bergen")["hits"]
            .as_array()
            .unwrap()
            .is_empty()
    );
    assert!(
        search(&host, &format!("q=bergen&session={ben}"))["hits"]
            .as_array()
            .unwrap()
            .is_empty()
    );
}

#[test]
fn a_search_while_history_is_read_says_partial_and_then_finds_it() {
    let place = Place::new("message-search-partial");
    let host = place.start();
    let ada = host.new_agent("Ada");
    host.send(&ada, "Kites over Lista on Sunday", "p-1");
    wait_for("Ada's answer", || host.state(&ada) == "done");
    host.kill();
    // The index is thrown away: the next Host builds it again from the agents' logs.
    for name in ["search.db", "search.db-wal", "search.db-shm"] {
        let _ = std::fs::remove_file(place.root.join("data").join(name));
    }
    let host = place.start();
    let early = search(&host, "q=kites");
    assert_eq!(early["partial"], true, "{early}");
    let hits = found(&host, "q=kites", 2);
    assert_eq!(hits[1]["role"], "user", "{hits:?}");
    assert_eq!(hits[0]["session"], ada.as_str());
}

/// What an agent's tool finds: `clarp-admin search`, with its turn where it has one, and with
/// the agents' tools token alone (the owner's token where `owner` says so).
fn tool_search(host: &Host, place: &Place, turn: Option<&str>, owner: bool, words: &str) -> Value {
    let mut command = sealed_command(&place.root, env!("CARGO_BIN_EXE_clarp-host"));
    command
        .args(["tool", "clarp-admin", "search", words])
        .current_dir(&place.root)
        .env("CLARP_HOST_ADDR", &host.address)
        .env_remove("CLARP_SESSION")
        .env_remove("CLAUDE_PWA_SESSION");
    if owner {
        command.env("CLARP_HOST_TOKEN", &host.token);
    } else {
        command
            .env("CLARP_TOOL_TOKEN", clarp_host::tool_token(&host.token))
            .env_remove("CLARP_HOST_TOKEN");
    }
    match turn {
        Some(turn) => command.env("CLARP_PROVIDER_TURN", turn),
        None => command.env_remove("CLARP_PROVIDER_TURN"),
    };
    let out = command.ended();
    assert_eq!(
        out.status.code(),
        Some(0),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    serde_json::from_slice::<Value>(&out.stdout).unwrap()
}

fn sessions_of(found: &Value) -> Vec<String> {
    let mut sessions: Vec<String> = found["hits"]
        .as_array()
        .unwrap()
        .iter()
        .map(|hit| hit["session"].as_str().unwrap().to_string())
        .collect();
    sessions.sort();
    sessions.dedup();
    sessions
}

#[test]
fn an_agent_finds_what_its_turn_may_read_of_log_and_no_more() {
    let place = Place::new("message-search-bound");
    let codex = place.root.join("codex");
    program(&codex, CODEX);
    let host = place.start_in(
        "search-owner-token",
        &[],
        &[("CLARP_CODEX_PROGRAM", codex.to_str().unwrap())],
    );
    // Ada is bound to a provider of her own; Ben and Cid run on the machine's tools. Each of
    // them says "customer", Ada in words only she says.
    let home = own_home(&place, "home-a");
    let ada = bound_agent(&host, "Ada", &home);
    let ben = host.new_agent("Ben");
    let cid = host.new_agent("Cid");
    host.send(&ada, "hello", "b-1");
    host.send(&ben, "customer notes of Ben", "b-2");
    host.send(&cid, "customer notes of Cid", "b-3");
    for session in [&ada, &ben, &cid] {
        wait_for("each first turn", || host.state(session) == "done");
    }
    // The owner finds all three.
    let everyone = [ada.clone(), ben.clone(), cid.clone()];
    let mut sorted = everyone.to_vec();
    sorted.sort();
    wait_for("the owner finds every chat", || {
        let found = search(&host, "q=customer");
        found["partial"] == false && sessions_of(&found) == sorted
    });
    let ben_turn = first_turn_token(&place, &agent_id(&host, &ben));
    let ada_turn = first_turn_token(&place, &agent_id(&host, &ada));
    let as_ben = [("X-Clarp-Turn", ben_turn.as_str())];

    // What Ben's turn may read of /log: Cid's chat, not Ada's.
    let log_as_ben = |session: &str| {
        let path = format!("/log?session={session}&limit=100");
        fetch(&host, &host.token, "GET", &path, &as_ben, b"").status
    };
    assert_eq!(log_as_ben(&cid), 200);
    assert_eq!(log_as_ben(&ada), 403);

    // His search finds exactly that: his own chat and Cid's, nothing of Ada's, not even that
    // she has the word.
    let mut readable = vec![ben.clone(), cid.clone()];
    readable.sort();
    let answered = fetch(
        &host,
        &host.token,
        "GET",
        "/search?q=customer",
        &as_ben,
        b"",
    );
    assert_eq!(answered.status, 200);
    let said: Value = serde_json::from_slice(&answered.body).unwrap();
    assert_eq!(sessions_of(&said), readable, "{said}");
    assert!(!said.to_string().contains(&ada), "{said}");
    // Asked of her conversation alone, he is refused as her /log refuses him.
    let path = format!("/search?q=customer&session={ada}");
    let answered = fetch(&host, &host.token, "GET", &path, &as_ben, b"");
    assert_eq!(answered.status, 403);
    let said: Value = serde_json::from_slice(&answered.body).unwrap();
    assert_eq!(said["error"], "bound_conversation", "{said}");
    // Another Host finds nothing of hers either.
    let peer = [("X-Clarp-Peer", "elsewhere")];
    let answered = fetch(&host, &host.token, "GET", "/search?q=customer", &peer, b"");
    let said: Value = serde_json::from_slice(&answered.body).unwrap();
    assert!(!said.to_string().contains(&ada), "{said}");

    // The agents' tools token is refused on the route, as on /log.
    let tools = clarp_host::tool_token(&host.token);
    assert_eq!(
        host.ask_as(&tools, "GET", "/search?q=customer", None).0,
        403
    );
    assert_eq!(
        host.ask_as(&tools, "GET", &format!("/log?session={cid}"), None)
            .0,
        403
    );

    // In the agents' tool, Ben's turn finds what his turn reads, with either token.
    for owner in [true, false] {
        let bens = tool_search(&host, &place, Some(&ben_turn), owner, "customer");
        assert_eq!(sessions_of(&bens), readable, "{bens}");
    }
    // A tool that cannot say whose turn it is finds nothing of a bound agent.
    let nobodys = tool_search(&host, &place, None, false, "customer");
    assert_eq!(sessions_of(&nobodys), readable, "{nobodys}");
    // Ada's turn finds her own words, and the others'.
    let adas = tool_search(&host, &place, Some(&ada_turn), false, "customer");
    assert_eq!(sessions_of(&adas), sorted, "{adas}");
}
