use clarp_core::helpers_card::{State, duration, from_agents, from_cells, reporting_helper, title};
use clarp_core::protocol::Agent;
use serde_json::{Value, json};

fn agent(value: Value) -> Agent {
    Agent::from_json(value.as_object().expect("an object"))
}

#[test]
fn the_title_counts_the_helpers_and_the_time() {
    assert_eq!(title(2, 2, 0, 5), "2 helpers working · 5 s");
    assert_eq!(title(1, 3, 0, 65), "1 helper working · 1 min 5 s");
    assert_eq!(title(2, 2, 0, -1), "2 helpers working", "no start known: no time");
    assert_eq!(title(0, 2, 0, 90), "2 helpers finished");
    assert_eq!(title(0, 3, 1, 90), "3 helpers finished · 1 failed");
    assert_eq!(title(0, 1, 0, 0), "1 helper finished");
    assert_eq!(duration(120), "2 min");
    assert_eq!(duration(3720), "1 h 2 min");
}

fn cell(id: &str, status: &str, summary: &str, lines: Value) -> Value {
    json!({"id": id, "kind": "subagents", "title": "Running agent", "summary": summary, "status": status, "lines": lines})
}

#[test]
fn a_rows_subagent_cells_are_one_card() {
    let cells = vec![
        cell("t1", "running", "Map the auth flow", json!([{"label": "Task", "text": "Find where tokens are checked"}])),
        cell("t2", "ok", "Read the docs", json!([{"label": "Task", "text": "Summarise the docs"}, {"label": "Result", "text": "Three endpoints\nand more"}])),
        json!({"id": "c1", "kind": "command", "title": "ls", "status": "ok"}),
    ];
    let card = from_cells("row1", &cells, 1_000).expect("a card");
    assert_eq!(card.helpers.len(), 2, "the command cell is not a helper");
    assert_eq!(card.running(), 1);
    assert_eq!(card.title(5), "1 helper working · 5 s");
    assert_eq!(card.helpers[0].name, "Map the auth flow");
    assert_eq!(card.helpers[0].line, "Find where tokens are checked", "a running helper shows its task");
    assert_eq!(card.helpers[1].state, State::Done);
    assert_eq!(card.helpers[1].line, "Three endpoints", "a finished one its result's first line");
    assert!(card.helpers[1].chat.is_empty(), "an in-process helper has no chat");
    assert_eq!(card.helpers[1].lines[1].1, "Result  Three endpoints\nand more", "it opens to its lines");
    assert!(from_cells("row2", &[json!({"id": "c1", "kind": "command"})], 0).is_none(), "no helper, no card");
}

#[test]
fn a_failed_helper_counts_as_failed() {
    let card = from_cells("r", &[cell("t1", "error", "Probe", json!([{"label": "Status", "text": "killed"}]))], 0).unwrap();
    assert_eq!(card.failed(), 1);
    assert_eq!(card.title(0), "1 helper finished · 1 failed");
    assert_eq!(card.helpers[0].line, "killed");
}

#[test]
fn cells_about_one_helper_are_one_line_its_latest_state() {
    let cells = vec![
        json!({"id": "s", "kind": "subagents", "title": "Spawned agent", "summary": "Ada [worker]", "status": "running", "lines": [{"label": "Task", "text": "Fix it"}]}),
        json!({"id": "w", "kind": "subagents", "title": "Finished waiting", "summary": "Ada [worker]", "status": "ok", "lines": [{"label": "Status", "text": "completed"}]}),
    ];
    let card = from_cells("r", &cells, 0).unwrap();
    assert_eq!(card.helpers.len(), 1);
    assert_eq!(card.helpers[0].state, State::Done);
    assert_eq!(card.helpers[0].key, "r:s", "keyed by its first cell");
}

#[test]
fn a_parents_running_helper_agents_are_one_card_opening_their_chats() {
    let parent = agent(json!({"agent_id": "p", "session": "jasper", "role": "agent"}));
    let agents = vec![
        parent.clone(),
        agent(json!({"agent_id": "h1", "session": "slash", "persona": "", "role": "helper", "parent_agent_id": "p", "helper_state": "running",
            "busy": true, "turn_started_at": 5_000, "status_text": "Building the card"})),
        agent(json!({"agent_id": "h2", "session": "probe", "role": "helper", "parent_agent_id": "p", "helper_state": "running", "busy": true, "turn_started_at": 3_000})),
        agent(json!({"agent_id": "h3", "session": "old", "role": "helper", "parent_agent_id": "p", "helper_state": "done"})),
        agent(json!({"agent_id": "h4", "session": "theirs", "role": "helper", "parent_agent_id": "q", "helper_state": "running", "busy": true})),
    ];
    let card = from_agents("helpers:jasper", &parent, &agents).expect("a card");
    let chats: Vec<&str> = card.helpers.iter().map(|h| h.chat.as_str()).collect();
    assert_eq!(chats, ["slash", "probe"], "its own running helpers, not a done one or another agent's");
    assert_eq!(card.started_ms, 3_000, "timed from the first to start");
    assert_eq!(card.helpers[0].line, "Building the card");
    assert_eq!(card.title(5), "2 helpers working · 5 s");
    let report = reporting_helper("h3", &parent, &agents).map(|a| a.session.as_str());
    assert_eq!(report, Some("old"), "a done helper's report is still its helper's");
    assert!(reporting_helper("h4", &parent, &agents).is_none(), "another agent's helper is not");
    assert!(from_agents("x", &agents[3], &agents).is_none(), "no helpers, no card");
}
