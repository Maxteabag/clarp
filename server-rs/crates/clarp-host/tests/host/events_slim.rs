use super::*;

/// Issue 879: a client that streams no text asks for the turn without its words and for the
/// rows in `transcript-updated`, and follows a chat without reading `/log`; one that did not ask
/// is sent what it always was.
#[test]
fn a_client_that_streams_no_text_follows_a_chat_from_its_events_alone() {
    use std::collections::BTreeMap;

    let place = Place::new("slim-events");
    let claude = place.root.join("claude");
    program(&claude, STAND_IN_STREAMS);
    let world = [("CLARP_CLAUDE_PROGRAM", claude.to_str().unwrap())];
    let host = place.start_in("", &[], &world);
    let features = host.get("/server-info")["capabilities"]["features"].clone();
    assert!(
        features.as_array().unwrap().contains(&json!("events_slim")),
        "{features}"
    );
    let made = host.post("/agents", json!({ "name": "Slim", "backend": "claude" }));
    let agent = made["session"].as_str().unwrap().to_string();
    let idle = |host: &Host| host.state(&agent) == "idle" || host.state(&agent) == "done";
    // A first turn, so that the conversation has been told once.
    host.send(&agent, "first one", "s1");
    wait_for("the first turn to end", || {
        host.turns(&agent).iter().any(|row| row["id"] == "r-Bone") && idle(&host)
    });
    std::thread::sleep(Duration::from_millis(400));
    let log = |after: u64| {
        host.get(&format!(
            "/log?session={agent}&limit=100&include_automated=1&include_tool_details=0&after_revision={after}"
        ))
    };
    let held_at_start = log(0)["latest_revision"].as_u64().unwrap();

    let slim = host.listen_at(&format!("/events?live={agent}&text=0&rows=1"));
    let full = host.listen_at(&format!("/events?live={agent}"));
    let plain = host.listen();
    let open = |heard: &Arc<Mutex<Vec<Value>>>| !heard.lock().unwrap().is_empty();
    wait_for("the connections to be open", || {
        open(&slim) && open(&full) && open(&plain)
    });
    let heard_before = |heard: &Arc<Mutex<Vec<Value>>>| heard.lock().unwrap().len();
    let (slim_from, full_from, plain_from) = (
        heard_before(&slim),
        heard_before(&full),
        heard_before(&plain),
    );
    host.send(&agent, "second two", "s2");
    wait_for("the second turn to end", || {
        host.turns(&agent).iter().any(|row| row["id"] == "r-Btwo") && idle(&host)
    });
    std::thread::sleep(Duration::from_millis(1_500));
    let since =
        |heard: &Arc<Mutex<Vec<Value>>>, from: usize| heard.lock().unwrap()[from..].to_vec();
    let (slim, full, plain) = (
        since(&slim, slim_from),
        since(&full, full_from),
        since(&plain, plain_from),
    );
    let live_of = |events: &[Value]| -> Vec<Value> {
        events
            .iter()
            .filter(|event| event["type"] == "live")
            .cloned()
            .collect()
    };

    // The words come to who asked for them, and to no one else.
    let words = |events: &[Value]| {
        live_of(events)
            .iter()
            .flat_map(|event| event["ops"].as_array().unwrap().clone())
            .any(|op| {
                (op["op"] == "append" && op["field"] == "text")
                    || (op["kind"] == "message" || op["kind"] == "reasoning")
                        && op["item"].get("text").is_some()
            })
    };
    assert!(
        words(&full),
        "the full connection is sent the words: {full:#?}"
    );
    let slim_live = live_of(&slim);
    assert!(!slim_live.is_empty());
    assert!(
        !words(&slim),
        "the slim connection is sent none: {slim_live:#?}"
    );
    // Each event of it says which it follows, so that a missing one is still seen.
    for pair in slim_live.windows(2) {
        assert_eq!(pair[1]["lseq_prev"], pair[0]["lseq"], "{pair:#?}");
    }
    assert!(
        live_of(&full).len() > slim_live.len(),
        "events of the words alone are not sent to the slim connection"
    );
    assert!(
        live_of(&full)
            .iter()
            .all(|event| event.get("lseq_prev").is_none())
    );
    // The tool item and the turn's end come to both.
    let ended = |events: &[Value]| {
        live_of(events)
            .iter()
            .flat_map(|event| event["ops"].as_array().unwrap().clone())
            .any(|op| op["op"] == "done" && op["id"] == "cl:toolu_two")
    };
    assert!(ended(&slim) && ended(&full));
    let without = host.get(&format!("/live?session={agent}&text=0"));
    let whole = host.get(&format!("/live?session={agent}"));
    assert_eq!(
        without["items"].as_array().unwrap().len(),
        whole["items"].as_array().unwrap().len()
    );
    for item in without["items"].as_array().unwrap() {
        if item["kind"] == "message" || item["kind"] == "reasoning" {
            assert!(item.get("text").is_none(), "{item}");
        }
    }

    // A client that holds the conversation follows it from the rows the events carry.
    let updates = |events: &[Value]| -> Vec<Value> {
        events
            .iter()
            .filter(|event| event["type"] == "transcript-updated" && event["session"] == agent)
            .cloned()
            .collect()
    };
    let carried = updates(&slim);
    assert!(
        carried.iter().any(|event| event["rows"].is_array()),
        "{carried:#?}"
    );
    let mut held = held_at_start;
    let mut rows: BTreeMap<String, Value> = BTreeMap::new();
    let mut reads = 0;
    for event in &carried {
        let take = |page: &[Value], rows: &mut BTreeMap<String, Value>| {
            for row in page {
                let id = row["id"].as_str().unwrap().to_string();
                let newer = rows
                    .get(&id)
                    .is_none_or(|had| had["revision"].as_u64() <= row["revision"].as_u64());
                if newer {
                    rows.insert(id, row.clone());
                }
            }
        };
        match (event["rows"].as_array(), event["rows_from"].as_u64()) {
            (Some(page), Some(from)) if from <= held => {
                take(page, &mut rows);
                held = held.max(event["rows_latest"].as_u64().unwrap());
            }
            _ if event["head_revision"]
                .as_u64()
                .is_some_and(|head| head <= held) => {}
            _ => {
                reads += 1;
                let page = log(held);
                take(page["turns"].as_array().unwrap(), &mut rows);
                held = held.max(page["latest_revision"].as_u64().unwrap());
            }
        }
    }
    let truth = log(held_at_start);
    assert_eq!(held, truth["latest_revision"].as_u64().unwrap());
    let truth: BTreeMap<String, Value> = truth["turns"]
        .as_array()
        .unwrap()
        .iter()
        .map(|row| (row["id"].as_str().unwrap().to_string(), row.clone()))
        .collect();
    assert_eq!(rows, truth, "the events carried what /log answers");
    assert_eq!(reads, 0, "the client read nothing from /log");
    // Who did not ask for rows is sent none.
    for event in updates(&full).iter().chain(updates(&plain).iter()) {
        assert!(
            event.get("rows").is_none() && event.get("rows_latest").is_none(),
            "{event}"
        );
    }
}
