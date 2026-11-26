use super::*;

use std::collections::BTreeMap;

/// The `transcript-updated` events of one chat a connection was sent, after `from`.
fn updates_of(heard: &Arc<Mutex<Vec<Value>>>, from: usize, session: &str) -> Vec<Value> {
    heard.lock().unwrap()[from..]
        .iter()
        .filter(|event| event["type"] == "transcript-updated" && event["session"] == session)
        .cloned()
        .collect()
}

/// Issue 928: the rows a `transcript-updated` carries are read with the reader's own `/log`
/// options, carry what `/log` says of coordination, and are not carried where the chat's
/// visibility changed since the connection's last rows of it.
#[test]
fn carried_rows_are_read_with_the_readers_own_log_options() {
    let place = Place::new("rows-options");
    let host = place.start();
    let features = host.get("/server-info")["capabilities"]["features"].clone();
    assert!(
        features
            .as_array()
            .unwrap()
            .contains(&json!("events_rows_options")),
        "{features}"
    );
    let agent = host.new_agent("Options");
    let answered = |host: &Host, n: usize| {
        host.turns(&agent)
            .iter()
            .filter(|row| row["role"] == "assistant")
            .count()
            >= n
    };
    // A first turn, so that the conversation has been told once.
    host.send(&agent, "first", "o1");
    wait_for("the first answer", || answered(&host, 1));
    std::thread::sleep(Duration::from_millis(400));
    let options = "include_automated=0&include_tool_details=0&coordination=auto";
    let log = |after: u64| {
        host.get(&format!(
            "/log?session={agent}&limit=100&{options}&after_revision={after}"
        ))
    };
    let held_at_start = log(0)["latest_revision"].as_u64().unwrap();
    let apps = host.listen_at(&format!(
        "/events?live={agent}&rows=1&rows_automated=0&rows_coordination=auto"
    ));
    let plain = host.listen_at(&format!("/events?live={agent}&rows=1"));
    let open = |heard: &Arc<Mutex<Vec<Value>>>| !heard.lock().unwrap().is_empty();
    wait_for("the connections to be open", || open(&apps) && open(&plain));
    let (apps_from, plain_from) = (apps.lock().unwrap().len(), plain.lock().unwrap().len());
    host.send(&agent, "second", "o2");
    wait_for("the second answer", || answered(&host, 2));
    std::thread::sleep(Duration::from_millis(1_000));

    // The rows match /log read the same way, and say what that read says of coordination.
    let carried = updates_of(&apps, apps_from, &agent);
    let with_rows: Vec<&Value> = carried.iter().filter(|e| e["rows"].is_array()).collect();
    assert!(!with_rows.is_empty(), "{carried:#?}");
    let policy = log(held_at_start)["coordination"].clone();
    assert!(policy.is_object(), "{policy}");
    for event in &with_rows {
        assert_eq!(event["rows_coordination"], policy, "{event:#}");
    }
    let mut held = held_at_start;
    let mut rows: BTreeMap<String, Value> = BTreeMap::new();
    let mut reads = 0;
    let take = |page: &[Value], rows: &mut BTreeMap<String, Value>| {
        for row in page {
            let id = row["id"].as_str().unwrap().to_string();
            if rows
                .get(&id)
                .is_none_or(|had| had["revision"].as_u64() <= row["revision"].as_u64())
            {
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
    assert_eq!(
        rows, truth,
        "the events carried what /log answers with the same options"
    );
    assert_eq!(reads, 0);
    // A connection without the options gets the rows as before, and nothing of coordination.
    let before = updates_of(&plain, plain_from, &agent);
    assert!(before.iter().any(|event| event["rows"].is_array()));
    assert!(
        before
            .iter()
            .all(|event| event.get("rows_coordination").is_none())
    );

    // The chat's visibility changes: the next rows are not carried, as /log would say the
    // client must read the chat anew; after that, rows come again under the new visibility.
    let now = policy["visibility"].as_str().unwrap().to_string();
    let other = if now == "show" { "hide" } else { "show" };
    host.post(
        "/agent-coordination",
        json!({ "session": agent, "visibility": other }),
    );
    let changed_from = apps.lock().unwrap().len();
    host.send(&agent, "third", "o3");
    wait_for("the third answer", || answered(&host, 3));
    host.send(&agent, "fourth", "o4");
    wait_for("the fourth answer", || answered(&host, 4));
    std::thread::sleep(Duration::from_millis(1_000));
    let after = updates_of(&apps, changed_from, &agent);
    let first_eligible = after
        .iter()
        .find(|event| event["rows_from"].is_u64())
        .expect("an event after the change");
    assert!(
        first_eligible.get("rows").is_none(),
        "no rows under a changed visibility: {first_eligible:#}"
    );
    let later = after.iter().filter(|event| event["rows"].is_array());
    for event in later {
        assert_eq!(event["rows_coordination"]["visibility"], other, "{event:#}");
    }
}
