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
