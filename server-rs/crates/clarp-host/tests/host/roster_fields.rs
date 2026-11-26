//! What an `agent-roster` event about one agent carries of its change (feature
//! `roster_fields`), and the read of one agent's row (`GET /agents/{session}`, feature
//! `agent_row`): both are the agent's row as `GET /agents/snapshot` has it (issue 809).

use super::*;

/// The agent's row in the snapshot as it is now.
fn snapshot_row(host: &Host, session: &str) -> Value {
    host.get("/agents/snapshot")["agents"]
        .as_array()
        .unwrap()
        .iter()
        .find(|row| row["session"] == session)
        .cloned()
        .unwrap_or_else(|| panic!("{session} is not in the snapshot"))
}

/// The newest roster event of this kind about this agent that carries `fields`.
fn carried(heard: &Arc<Mutex<Vec<Value>>>, kind: &str, session: &str) -> Option<Value> {
    heard
        .lock()
        .unwrap()
        .iter()
        .rev()
        .find(|event| {
            event["type"] == "agent-roster"
                && event["kind"] == kind
                && event["session"] == session
                && event["fields"].is_object()
        })
        .cloned()
}

/// Every field the event carries for the agent equals the same field of its row.
fn assert_fields_are_the_rows(fields: &Value, row: &Value) {
    let fields = fields.as_object().unwrap();
    assert!(!fields.is_empty());
    for (name, value) in fields {
        assert_eq!(
            value, &row[name],
            "{name}: the event's {fields:?}, the row {row}"
        );
    }
}

#[test]
fn a_roster_event_carries_what_it_changed_as_the_snapshot_row_has_it() {
    let place = Place::new("roster-fields");
    // The fleet's own look at its helpers is kept out of the way: every move here is the test's.
    let host = place.start_with("", &["--helpers-checked-every-ms", "3600000"]);
    let heard = host.listen();
    let maker = host.new_agent("Maker");
    let made = host.post(
        "/agents",
        json!({
            "name": "Slice", "backend": "scripted", "role": "helper", "parent": maker,
            "session": "slice",
        }),
    );
    assert_eq!(made["session"], "slice", "{made}");
    let mark = |state: &str| {
        let (status, said) = host.ask(
            "POST",
            "/agent-helper-state",
            Some(&json!({ "session": "slice", "state": state })),
        );
        assert_eq!(status, 200, "{said}");
    };

    // The helper is accepted, and opened again: each move is told with the helper's fields
    // and its maker's, which are what both rows in the snapshot say.
    for (state, running) in [("done", 0), ("running", 1)] {
        mark(state);
        wait_for("the helper's move with its fields", || {
            carried(&heard, "helper-state", "slice")
                .is_some_and(|event| event["fields"]["helper_state"] == state)
        });
        let event = carried(&heard, "helper-state", "slice").unwrap();
        let helper = snapshot_row(&host, "slice");
        assert_eq!(event["agent_id"], helper["agent_id"], "{event}");
        assert_fields_are_the_rows(&event["fields"], &helper);
        assert_eq!(
            event["fields"],
            json!({ "helper_state": state, "archived_at": null }),
            "{event}"
        );
        let parent = &event["parent"];
        let makers = snapshot_row(&host, &maker);
        assert_eq!(
            (&parent["session"], &parent["agent_id"]),
            (&makers["session"], &makers["agent_id"]),
            "{event}"
        );
        assert_fields_are_the_rows(&parent["fields"], &makers);
        let mut names: Vec<&String> = parent["fields"].as_object().unwrap().keys().collect();
        names.sort();
        assert_eq!(
            names,
            [
                "background_jobs",
                "child_count",
                "latest_state",
                "running_children",
                "status_text"
            ],
            "{event}"
        );
        assert_eq!(parent["fields"]["child_count"], 1, "{event}");
        assert_eq!(parent["fields"]["running_children"], running, "{event}");
    }
    let event = carried(&heard, "helper-state", "slice").unwrap();
    assert_eq!(
        event["parent"]["fields"]["latest_state"], "background",
        "{event}"
    );
    assert!(
        event["parent"]["fields"]["status_text"]
            .as_str()
            .is_some_and(|line| line.starts_with("slice")),
        "{event}"
    );

    // A mute and a chat's view carry theirs.
    host.post("/agent-mute", json!({ "session": maker, "muted": true }));
    wait_for("the mute with its field", || {
        carried(&heard, "mute-changed", &maker).is_some()
    });
    let event = carried(&heard, "mute-changed", &maker).unwrap();
    assert_eq!(event["fields"], json!({ "muted": true }), "{event}");
    assert_fields_are_the_rows(&event["fields"], &snapshot_row(&host, &maker));
    host.post(
        "/agent-chat-view",
        json!({ "session": maker, "chat_view": "conversation" }),
    );
    wait_for("the chat view with its field", || {
        carried(&heard, "chat_view", &maker).is_some()
    });
    let event = carried(&heard, "chat_view", &maker).unwrap();
    assert_eq!(
        event["fields"],
        json!({ "chat_view": "conversation" }),
        "{event}"
    );
    assert_fields_are_the_rows(&event["fields"], &snapshot_row(&host, &maker));

    // A kind whose change is more than a few fields carries none: the row is read.
    host.post(
        "/agent-rename",
        json!({ "session": maker, "name": "Builder" }),
    );
    wait_for("the rename", || {
        heard
            .lock()
            .unwrap()
            .iter()
            .any(|event| event["kind"] == "agent-renamed")
    });
    let told = heard.lock().unwrap().clone();
    let mut renames = told.iter().filter(|event| event["kind"] == "agent-renamed");
    assert!(renames.all(|event| event.get("fields").is_none()));

    // A conversation's change says its head revision and conversation as the row has them: an
    // app that holds that revision reads nothing.
    host.send(&maker, "hello", "u-1");
    // Its helper is at work, so its row reads `background`: the turn is over once it is not
    // busy and has ended one.
    wait_for("the turn to settle", || {
        let row = snapshot_row(&host, &maker);
        row["busy"] == false && row["last_turn_end"].as_i64().is_some_and(|at| at > 0)
    });
    wait_for("the newest transcript event to name the row's head", || {
        let row = snapshot_row(&host, &maker);
        heard
            .lock()
            .unwrap()
            .iter()
            .rev()
            .find(|event| event["type"] == "transcript-updated" && event["session"] == *maker)
            .is_some_and(|event| {
                event["head_revision"] == row["head_revision"]
                    && event["conversation_id"] == row["conversation_id"]
            })
    });
    assert!(
        snapshot_row(&host, &maker)["head_revision"]
            .as_u64()
            .unwrap()
            > 0
    );

    // One agent's row is its row in the snapshot, the maker's and the helper's alike; a session the Host does not have is not found.
    for session in [maker.as_str(), "slice"] {
        wait_for("the one row to be the snapshot's", || {
            host.get(&format!("/agents/{session}")) == snapshot_row(&host, session)
        });
    }
    let (status, said) = host.ask("GET", "/agents/nobody", None);
    assert_eq!(
        (status, &said["error"]),
        (404, &json!("agent_not_found")),
        "{said}"
    );
    // The other reads under `/agents/` are what they were.
    assert!(host.get("/agents/snapshot")["agents"].is_array());
    assert!(host.get("/agents/archived").is_object());
}

/// A helper is shown at work while a helper below it, at any depth, works, and its own state
/// again once the last of them stops (issue 936). Its row, the one row, its makers' counts,
/// `GET /agent-helper-state` and the events about each say the same, and the helper at work
/// stays in the list and opens by its session throughout.
#[test]
fn a_helper_reads_running_while_a_helper_below_it_works() {
    let place = Place::new("helper-below");
    let host = place.start_with("", &["--helpers-checked-every-ms", "3600000"]);
    let heard = host.listen();
    let root = host.new_agent("Root");
    let made = |session: &str, parent: &str| {
        let made = host.post(
            "/agents",
            json!({
                "name": session, "backend": "scripted", "role": "helper", "parent": parent,
                "session": session,
            }),
        );
        assert_eq!(made["session"], session, "{made}");
    };
    made("middle", &root);
    made("leaf", "middle");
    let middle_id = snapshot_row(&host, "middle")["agent_id"].clone();
    let mark = |session: &str, state: &str| {
        let (status, said) = host.ask(
            "POST",
            "/agent-helper-state",
            Some(&json!({ "session": session, "state": state })),
        );
        assert_eq!(status, 200, "{said}");
    };
    let state_of = |session: &str| {
        host.get(&format!("/agent-helper-state?session={session}"))["helper_state"].clone()
    };
    // What every read says of the three, and that the newest event about the middle helper
    // says what its row says, with its maker's counts.
    let check = |middle: &str, running_below_root: usize| {
        let (rows, root_row) = (snapshot_row(&host, "middle"), snapshot_row(&host, &root));
        assert_eq!(rows["helper_state"], middle, "{rows}");
        assert_eq!(state_of("middle"), middle);
        assert_eq!(
            root_row["running_children"], running_below_root,
            "{root_row}"
        );
        assert_eq!(root_row["child_count"], 1, "{root_row}");
        let leaf = snapshot_row(&host, "leaf");
        assert_eq!(leaf["parent_agent_id"], middle_id, "{leaf}");
        for session in [root.as_str(), "middle", "leaf"] {
            wait_for("the one row to be the snapshot's", || {
                host.get(&format!("/agents/{session}")) == snapshot_row(&host, session)
            });
        }
        wait_for("the middle helper's newest event to be its row", || {
            carried(&heard, "helper-state", "middle")
                .is_some_and(|event| event["fields"]["helper_state"] == middle)
        });
        let event = carried(&heard, "helper-state", "middle").unwrap();
        assert_fields_are_the_rows(&event["fields"], &snapshot_row(&host, "middle"));
        assert_fields_are_the_rows(&event["parent"]["fields"], &snapshot_row(&host, &root));
        assert_eq!(event["parent"]["session"], root, "{event}");
    };

    // The middle helper is accepted while the leaf below it works: it reads running.
    mark("middle", "done");
    check("running", 1);
    assert_eq!(snapshot_row(&host, "leaf")["helper_state"], "running");
    // The leaf is accepted: the middle helper reads its own result again.
    mark("leaf", "done");
    check("done", 0);
    // Opened again, and then put away: a helper put away counts for nobody above it, and opens
    // by its session all the same.
    mark("leaf", "running");
    check("running", 1);
    host.post(
        "/agent-archive",
        json!({ "session": "leaf", "archived": true }),
    );
    check("done", 0);
    let leaf = host.get("/agents/leaf");
    assert_eq!(
        (&leaf["helper_state"], leaf["archived_at"].is_i64()),
        (&json!("running"), true),
        "{leaf}"
    );
}
