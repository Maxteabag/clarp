//! What the Goal keeper makes of a goal's world, held to what the Python Host's own keeper
//! makes of the same (`tests/fixtures/goal-keeper-vectors.json`, written by
//! `server-rs/tools/goal-keeper-vectors.py` from `server/lib/goal_keeper.py`).

#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_kernel::goals::keeper::{self, Ended, Event, Helper, Job, Row, World};
use serde_json::{Value, json};

fn vectors() -> Value {
    serde_json::from_str(include_str!("fixtures/goal-keeper-vectors.json")).unwrap()
}

/// The world of a moment as the Host would give it: read from what the vectors hold of the
/// reference's database.
struct Given {
    status: String,
    created_at: i64,
    goal: Value,
    owner: String,
    owner_name: String,
    helpers: Vec<Helper>,
    rows: Vec<Row>,
    jobs: Vec<Job>,
    events: Vec<Event>,
    ended: Option<Ended>,
    last_written: Option<i64>,
}

fn given(of: &Value) -> Given {
    let rows = of["rows"].as_array().unwrap().iter().map(|row| {
        let mut row = row.clone();
        // The cards of a row's tools, where it has a list of them.
        let tools = row["tools_json"]
            .as_str()
            .and_then(|text| serde_json::from_str::<Value>(text).ok());
        row["tools"] = tools.filter(Value::is_array).unwrap_or_else(|| json!([]));
        serde_json::from_value::<Row>(row).unwrap()
    });
    let ended = of["last_state"].as_object().map(|state| {
        let detail = state["detail"]
            .as_str()
            .and_then(|text| serde_json::from_str::<Value>(text).ok());
        let reason = detail
            .as_ref()
            .and_then(|detail| detail["reason"].as_str())
            .unwrap_or_default();
        Ended {
            at: state["ts"].as_i64().unwrap(),
            kind: state["kind"].as_str().unwrap().to_string(),
            reason: reason.to_string(),
        }
    });
    let persona = of["owner"]["persona"]
        .as_str()
        .filter(|persona| !persona.is_empty());
    Given {
        status: of["goal"]["status"].as_str().unwrap().to_string(),
        created_at: of["goal"]["created_at"].as_i64().unwrap(),
        goal: of["goal"]["goal"].clone(),
        owner: of["owner"]["agent_id"].as_str().unwrap().to_string(),
        owner_name: persona
            .or(of["owner"]["session"].as_str())
            .unwrap()
            .to_string(),
        helpers: serde_json::from_value(of["helpers"].clone()).unwrap(),
        rows: rows.collect(),
        jobs: serde_json::from_value(of["jobs"].clone()).unwrap(),
        events: serde_json::from_value(of["events"].clone()).unwrap(),
        ended,
        last_written: of["last_written"].as_i64(),
    }
}

impl Given {
    fn world(&self) -> World<'_> {
        World {
            status: &self.status,
            created_at: self.created_at,
            goal: &self.goal,
            owner: &self.owner,
            owner_name: &self.owner_name,
            helpers: &self.helpers,
            rows: &self.rows,
            jobs: &self.jobs,
            events: &self.events,
            ended: self.ended.as_ref(),
            last_written: self.last_written,
        }
    }
}

#[test]
fn a_goals_world_is_read_as_the_python_hosts_keeper_reads_it() {
    let vectors = vectors();
    assert_eq!(keeper::SYSTEM, vectors["system"].as_str().unwrap());
    let (mut looks, mut stalled, mut triggers, mut lines) = (0, 0, 0, 0);
    for scenario in vectors["scenarios"].as_array().unwrap() {
        for step in scenario["steps"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|step| step.get("given").is_some())
        {
            let what = format!("{}: {}", scenario["name"], step["what"]);
            let of = &step["given"];
            let held = given(of);
            let world = held.world();
            let now = of["now"].as_i64().unwrap();
            let full = step["full"].as_bool().unwrap();

            // Where the update reads from.
            let (since_ms, since_event) = keeper::cursor(&held.goal, held.created_at, now, full);
            assert_eq!(json!([since_ms, since_event]), step["cursor"], "{what}");
            let whole = keeper::cursor(&held.goal, held.created_at, now, true);
            assert_eq!(json!([whole.0, whole.1]), step["cursor_whole"], "{what}");
            let catch_up = keeper::needs_catch_up(&held.goal, &held.status, held.created_at, now);
            assert_eq!(json!(catch_up), step["needs_catch_up"], "{what}");

            // Which of the agents the owner made are the goal's helpers.
            let children = of["children"].as_array().unwrap().iter();
            let chosen: Vec<&str> = children
                .filter(|child| {
                    let deleted = child["deleted"].as_i64().unwrap() != 0;
                    keeper::is_helper(
                        child["created_at"].as_i64().unwrap(),
                        deleted,
                        held.created_at,
                    )
                })
                .take(keeper::HELPERS_MOST)
                .map(|child| child["agent_id"].as_str().unwrap())
                .collect();
            let theirs: Vec<&str> = held
                .helpers
                .iter()
                .map(|helper| helper.agent_id.as_str())
                .collect();
            assert_eq!(chosen, theirs, "{what}: the helpers");

            // What happened since.
            let seen = keeper::activity(&world, since_ms, since_event);
            let ours = json!({
                "triggers": seen.triggers, "status_change": seen.status_change, "newest_ms": seen.newest_ms,
                "newest_event": seen.newest_event, "any_messages": seen.any_messages,
            });
            assert_eq!(ours, step["activity"], "{what}");
            triggers += seen.triggers.len();

            // What was said, and the sheet of facts.
            let (said, refs) = keeper::delta(&world, since_ms, now.max(seen.newest_ms));
            assert_eq!(said, step["delta"]["text"].as_str().unwrap(), "{what}");
            assert_eq!(json!(refs), step["delta"]["refs"], "{what}");
            lines += refs.len();
            let (sheet, job_refs) = keeper::fact_sheet(&world, now);
            assert_eq!(sheet, step["sheet"]["sheet"], "{what}");
            assert_eq!(
                sheet.to_string(),
                step["sheet"]["sheet"].to_string(),
                "{what}, as text"
            );
            assert_eq!(json!(job_refs), step["sheet"]["refs"], "{what}");

            // Whether it has stalled.
            let found = keeper::stall(&world, now);
            let ours = found.as_ref().map_or(Value::Null, |stall| {
                json!([stall.since, stall.cause, stall.code])
            });
            assert_eq!(ours, step["stall"], "{what}");
            let line = found
                .as_ref()
                .map(|stall| keeper::stall_line(stall.since, stall.cause))
                .unwrap_or_default();
            assert_eq!(line, step["stall_line"].as_str().unwrap(), "{what}");
            assert_eq!(
                json!(keeper::needs_stall_flag(&world, now)),
                step["needs_stall_flag"],
                "{what}"
            );
            stalled += usize::from(found.is_some());

            // The whole prompt.
            let prompt = keeper::prompt(&of["plan"], &sheet, &said);
            assert_eq!(prompt, step["prompt"].as_str().unwrap(), "{what}");
            looks += 1;
        }
    }
    assert_eq!((looks, stalled), (41, 15));
    assert!(
        triggers > 40 && lines > 60,
        "{triggers} triggers, {lines} lines"
    );
}

#[test]
fn what_a_model_answered_is_taken_as_the_python_hosts_keeper_takes_it() {
    let vectors = vectors();
    let mut decided = 0;
    for scenario in vectors["scenarios"].as_array().unwrap() {
        for step in scenario["steps"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|step| step["what"] == "decide")
        {
            let (steps, progress, next_work) = keeper::decide(&step["plan"], &step["answer"]);
            let steps: serde_json::Map<String, Value> = steps
                .into_iter()
                .map(|(item_id, status, note)| (item_id, json!({ "status": status, "note": note })))
                .collect();
            let ours = json!({ "steps": steps, "progress": progress, "next_work": next_work });
            assert_eq!(ours, step["decided"], "{}", step["answer"]);
            assert_eq!(
                ours.to_string(),
                step["decided"].to_string(),
                "{}, as text",
                step["answer"]
            );
            decided += 1;
        }
    }
    assert_eq!(decided, 6);
}

#[test]
fn what_a_keeper_watches_is_read_off_its_attachments_as_the_python_host_reads_it() {
    let vectors = vectors();
    let watched = vectors["watched"].as_array().unwrap();
    for row in watched {
        let ours = keeper::watch(&row["config"]);
        let sweep = ours.sweep.as_ref().map_or(
            Value::Null,
            |(cron, zone)| json!({ "cron": cron, "timezone": zone }),
        );
        let ours = json!({
            "owner_turns": ours.owner_turns, "goal_activity": ours.goal_activity, "fold_ms": ours.fold_ms,
            "sweep": sweep, "progress_attachment": ours.progress_attachment,
        });
        assert_eq!(ours, row["watch"], "{}", row["config"]);
    }
    assert_eq!(watched.len(), 5);
}
