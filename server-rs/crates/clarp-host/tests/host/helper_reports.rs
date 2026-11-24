//! The Host's tests of a helper whose work settled without a report to its maker. One file
//! a piece, declared in `host.rs`.

use super::*;

#[test]
fn a_finished_helper_with_no_report_is_marked_for_its_maker() {
    let place = Place::new("unreported-helper");
    let host = place.start_with("", &["--helpers-checked-every-ms", "50"]);
    let maker = host.new_agent("Maker");
    let helper = host.post(
        "/agents",
        json!({
            "name": "Slice", "backend": "scripted", "role": "helper", "parent": maker,
        }),
    );
    let helper = helper["session"].as_str().unwrap();
    let state =
        || host.get(&format!("/agent-helper-state?session={helper}"))["helper_state"].clone();
    let events = host.listen();
    host.send(helper, "Do this work.", "first");
    wait_for("the settled helper with no report", || {
        state() == "ended-unreported"
    });
    wait_for("the helper state roster event", || {
        events.lock().unwrap().iter().any(|event| {
            event["type"] == "agent-roster"
                && event["kind"] == "helper-state"
                && event["session"] == helper
        })
    });
    assert_eq!(
        host.post(
            "/send",
            json!({
                "session": maker, "text": "Here is my report.", "origin": "agent",
                "sender": helper, "client_msg_id": "late-report"
            })
        )["ok"],
        true
    );
    assert_eq!(state(), "reported");
}

#[test]
fn a_helper_that_reports_mid_turn_reads_running_until_the_turn_is_over() {
    use super::coordination::{stand_in, start_host_as};
    let place = Place::new("helper-reports-mid-turn");
    let (tokens, release, holds) = stand_in(&place);
    let host = start_host_as(&place, &[], "fixture-admin");
    host.post(
        "/agents",
        json!({"name":"Boss","session":"boss","backend":"claude"}),
    );
    host.post(
        "/agents",
        json!({"name":"Hal","session":"hal","backend":"claude","role":"helper","parent":"boss"}),
    );
    let events = host.listen_at("/events?token=fixture-admin");
    // A change of the parent's mute is told to every app: once it is heard, so is the rest.
    wait_for("the event stream to subscribe", || {
        for muted in [true, false] {
            host.post("/agent-mute", json!({"session":"boss","muted":muted}));
        }
        events
            .lock()
            .unwrap()
            .iter()
            .any(|event| event["kind"] == "mute-changed")
    });
    let state = || host.get("/agent-helper-state?session=hal")["helper_state"].clone();
    let row = |session: &str| {
        host.get("/agents/snapshot")["agents"]
            .as_array()
            .unwrap()
            .iter()
            .find(|row| row["session"] == session)
            .cloned()
            .unwrap()
    };
    // How many times the apps were told to read the helper's state again.
    let told = || {
        events
            .lock()
            .unwrap()
            .iter()
            .filter(|event| {
                event["type"] == "agent-roster"
                    && event["kind"] == "helper-state"
                    && event["session"] == "hal"
            })
            .count()
    };
    let prompt = tokens.join("hal.prompt");
    // A turn of the helper that runs until the test lets it go.
    let held_turn = |text: &str, id: &str| {
        std::fs::write(holds.join("hal"), b"").unwrap();
        let _ = std::fs::remove_file(&prompt);
        host.send("hal", text, id);
        wait_for("the helper's turn to begin", || prompt.exists());
    };
    let let_go = || {
        std::fs::write(release.join("hal"), b"").unwrap();
        wait_for("the helper's turn to end", || host.state("hal") == "done");
    };

    held_turn("do the work", "work");
    // Its progress message to its parent marks it reported, and it is still in its turn.
    let sent = host.post(
        "/send",
        json!({"session":"boss","text":"halfway there","origin":"agent",
               "sender":"hal","client_msg_id":"progress"}),
    );
    assert_eq!(sent["ok"], true, "{sent}");
    wait_for("the report to tell the list", || told() == 1);
    assert_eq!(state(), "running");
    assert_eq!(row("hal")["helper_state"], "running");
    assert_eq!(row("boss")["running_children"], 1);
    let_go();
    wait_for("the turn's end to tell the list", || told() == 2);
    assert_eq!(state(), "reported");
    assert_eq!(row("hal")["helper_state"], "reported");
    assert_eq!(row("boss")["running_children"], 0);

    // A helper its parent accepted stays done while it works on.
    let done = host.post(
        "/agent-helper-state",
        json!({"session":"hal","state":"done"}),
    );
    assert_eq!(done["helper_state"], "done", "{done}");
    held_turn("one more thing", "more");
    assert_eq!(row("hal")["busy"], true);
    assert_eq!(state(), "done");
    assert_eq!(row("hal")["helper_state"], "done");
    let_go();
    assert_eq!(state(), "done");
}

