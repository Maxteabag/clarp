use super::*;

#[test]
fn a_calendar_response_returns_to_the_agent_that_asked() {
    let place = Place::new("calendar-response");
    let host = place.start();
    let session = host.new_agent("Calendar");
    let requested = host.post(
        "/calendar/request",
        json!({"session": session, "title": "Meeting", "start": "9", "end": "10"}),
    );
    let request_id = requested["request_id"].as_str().unwrap();
    let mut receipt = json!({
        "request_id": request_id,
        "session": "someone-else",
        "status": "added",
        "platform": "android",
        "event_id": "123",
    });
    assert_eq!(host.ask("POST", "/calendar/response", Some(&receipt)).0, 404);
    receipt["session"] = json!(session);
    receipt["status"] = json!("unknown");
    assert_eq!(host.ask("POST", "/calendar/response", Some(&receipt)).0, 400);
    receipt["status"] = json!("added");
    assert_eq!(host.post("/calendar/response", receipt.clone())["ok"], true);
    receipt["status"] = json!("failed");
    assert_eq!(host.post("/calendar/response", receipt)["ok"], true);

    let text = format!("Calendar request {request_id} for \"Meeting\": the event was added.");
    wait_for("calendar result in the conversation", || {
        host.turns(&session).iter().any(|row| row["text"] == text)
    });
    let message_id = format!("u-calendar-response-{request_id}");
    let turns = host.turns(&session);
    assert_eq!(turns.iter().filter(|row| row["id"] == message_id).count(), 1);
}

#[test]
fn an_app_can_post_as_the_user_to_a_team_feed() {
    let place = Place::new("team-user-post");
    let host = place.start();
    let team = host.post("/teams", json!({"name": "Team"}));
    let id = team["team"]["team_id"].as_str().unwrap();
    let team_path = format!("/teams/{id}");
    let messages = format!("{team_path}/messages");
    let body = json!({"text": "  Prioritize the release.  ", "source_agent_id": "forged"});
    assert_eq!(host.ask("POST", &messages, Some(&body)).0, 400);
    host.post(&team_path, json!({"communication_enabled": true}));
    let session = host.new_agent("Member");
    let snapshot = host.get("/agents/snapshot");
    let member = snapshot["agents"].as_array().unwrap().iter()
        .find(|row| row["session"] == session).unwrap();
    host.post(&format!("{team_path}/members"), json!({"agent_id":member["agent_id"]}));
    assert_eq!(host.ask("POST", &messages, Some(&json!({"text": ""}))).0, 400);
    let posted = host.post(&messages, body);
    let feed = host.get(&messages);
    let row = &feed["messages"][0];
    assert_eq!(row["team_message_id"], posted["team_message_id"]);
    assert_eq!(row["text"], "Prioritize the release.");
    assert_eq!(row["source_name"], "User");
    assert_eq!(row["source_agent_id"], "user");
    assert_eq!(host.get("/teams")["teams"][0]["unread_count"], 1);
}

#[test]
fn a_paused_goal_stays_visible_and_its_change_is_announced() {
    let place = Place::new("task-plan-updated");
    let host = place.start();
    let session = host.new_agent("Planner");
    let heard = host.listen();
    let plan = goal(
        &host,
        &place.root,
        &[
            "create",
            &session,
            "fixture",
            "Fixture",
            "[]",
            r#"{"outcome":"Deliver","criteria":["Checked"],"limits":"Local"}"#,
        ],
    );
    let id = plan["plan_id"].as_str().unwrap();
    host.post(
        "/task-plan/action",
        json!({"plan_id": id, "revision": plan["revision"], "action": "pause", "data": {"reason": "fixture"}}),
    );
    let read = host.get(&format!("/task-plan?session={session}"));
    assert_eq!(read["plan"]["plan_id"], id);
    assert_eq!(read["plan"]["status"], "paused");
    assert_eq!(read["plan"]["revision"], 1);
    wait_for("task-plan update", || {
        heard.lock().unwrap().iter().any(|row| {
            row["type"] == "task-plan-updated" && row["session"] == session
                && row["plan_id"] == id && row["revision"] == 1
        })
    });
}
