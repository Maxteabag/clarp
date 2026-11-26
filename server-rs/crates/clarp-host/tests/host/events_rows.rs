use super::*;

use std::collections::BTreeMap;

/// The `transcript-updated` events of one chat a connection was sent, in order.
fn updates_of(heard: &Arc<Mutex<Vec<Value>>>, from: usize, session: &str) -> Vec<Value> {
    heard.lock().unwrap()[from..]
        .iter()
        .filter(|event| event["type"] == "transcript-updated" && event["session"] == session)
        .cloned()
        .collect()
}

/// Issue 879: a connection that asks with `rows=1` follows a chat from its events alone; each
/// `transcript-updated` carries the rows above the head the one before it told; a connection
/// that did not ask is sent no rows.
#[test]
fn a_client_that_asked_for_rows_follows_a_chat_without_reading_the_log() {
    let place = Place::new("events-rows");
    let claude = place.root.join("claude");
    program(&claude, STAND_IN_STREAMS);
    let world = [("CLARP_CLAUDE_PROGRAM", claude.to_str().unwrap())];
    let host = place.start_in("", &[], &world);
    let features = host.get("/server-info")["capabilities"]["features"].clone();
    assert!(
        features.as_array().unwrap().contains(&json!("events_rows")),
        "{features}"
    );
    let made = host.post("/agents", json!({ "name": "Rows", "backend": "claude" }));
    let agent = made["session"].as_str().unwrap().to_string();
    let idle = |host: &Host| matches!(host.state(&agent).as_str(), "idle" | "done");
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

    let carrying = host.listen_at(&format!("/events?live={agent}&rows=1"));
    let plain = host.listen();
    let open = |heard: &Arc<Mutex<Vec<Value>>>| !heard.lock().unwrap().is_empty();
    wait_for("the connections to be open", || {
        open(&carrying) && open(&plain)
    });
    let (carrying_from, plain_from) = (carrying.lock().unwrap().len(), plain.lock().unwrap().len());
    host.send(&agent, "second two", "s2");
    wait_for("the second turn to end", || {
        host.turns(&agent).iter().any(|row| row["id"] == "r-Btwo") && idle(&host)
    });
    std::thread::sleep(Duration::from_millis(1_500));

    let carried = updates_of(&carrying, carrying_from, &agent);
    assert!(
        carried.iter().any(|event| event["rows"].is_array()),
        "{carried:#?}"
    );
    // The events of a conversation follow one another: each stands for the rows above the head
    // the one before it told, and carries none below.
    for pair in carried.windows(2) {
        assert_eq!(pair[1]["rows_from"], pair[0]["head_revision"], "{pair:#?}");
    }
    for event in &carried {
        let from = event["rows_from"].as_u64().unwrap();
        for row in event["rows"].as_array().into_iter().flatten() {
            assert!(row["revision"].as_u64().unwrap() > from, "{event:#}");
        }
    }

    // A client that holds the conversation follows it from the rows the events carry; one
    // that holds less than an event's rows_from (a gap) reads /log, as one that was given none.
    let mut held = held_at_start;
    let mut rows: BTreeMap<String, Value> = BTreeMap::new();
    let mut reads = 0;
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
    for event in &carried {
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

    // Who did not ask for rows is sent none, only the number they begin after.
    let plainly = updates_of(&plain, plain_from, &agent);
    assert!(!plainly.is_empty());
    for event in &plainly {
        assert!(
            event.get("rows").is_none() && event.get("rows_latest").is_none(),
            "{event}"
        );
    }
    assert!(plainly.iter().any(|event| event["rows_from"].is_u64()));

    // Rows larger than an event may carry are left to /log: the event says where they begin.
    let echo = host.new_agent("Large");
    host.send(&echo, "small first", "l1");
    wait_for("the echo", || {
        host.turns(&echo)
            .iter()
            .any(|row| row["role"] == "assistant")
    });
    std::thread::sleep(Duration::from_millis(400));
    let before = carrying.lock().unwrap().len();
    host.send(&echo, &format!("large {}", "word ".repeat(4_000)), "l2");
    wait_for("the large echo", || {
        host.turns(&echo)
            .iter()
            .filter(|row| row["role"] == "assistant")
            .count()
            > 1
    });
    std::thread::sleep(Duration::from_millis(800));
    let large = updates_of(&carrying, before, &echo);
    assert!(!large.is_empty());
    for event in &large {
        assert!(event["rows_from"].is_u64(), "{event}");
        let size = event["rows"].to_string().len();
        assert!(
            event.get("rows").is_none() || size <= 16 * 1024,
            "{size} bytes of rows carried"
        );
    }
    assert!(
        large.iter().any(|event| event.get("rows").is_none()),
        "the large rows were left to /log: {large:#?}"
    );
}
