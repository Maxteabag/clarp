//! What the Goal keeper makes of a goal's world, held to what the Python Host's own keeper
//! makes of the same (`tests/fixtures/goal-keeper-vectors.json`, written by
//! `server-rs/tools/goal-keeper-vectors.py` from `server/lib/goal_keeper.py`).

#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_kernel::goals::keeper::{self, Activity, Ended, Event, Helper, Job, Open, Row, World};
use serde_json::{Value, json};

/// The shape a model is held to names every step the prompt shows, by the id the prompt
/// shows it under, and an answer of that shape is read whole.
#[test]
fn the_shape_of_an_answer_names_the_steps_the_prompt_shows() {
    let plan = json!({
        "plan_id": "a:ship:01", "title": "Ship", "status": "active",
        "goal": { "outcome": "Shipped", "criteria": [] },
        "items": [
            { "item_id": "a:ship:01:probe", "title": "Probe", "status": "completed", "subtasks": [
                { "item_id": "a:ship:01:probe-2", "title": "Again", "status": "pending" },
            ] },
            { "item_id": "a:ship:01:build", "title": "Build", "status": "in_progress" },
        ],
    });
    let shape = keeper::schema(&plan);
    assert_eq!(
        shape["properties"]["steps"]["required"],
        json!(["probe", "probe-2", "build"])
    );
    assert_eq!(shape["required"], json!(["steps", "checkpoint", "changed"]));
    let step = &shape["properties"]["steps"]["properties"]["build"];
    assert_eq!(step["required"], json!(["status", "evidence", "note"]));
    assert_eq!(step["additionalProperties"], json!(false));
    // Every step of the shape is one the prompt shows.
    let asked = keeper::prompt(&plan, &json!({}), "");
    for name in ["\"probe\"", "\"probe-2\"", "\"build\""] {
        assert!(asked.contains(name), "{name}");
    }
    // An answer of that shape, as a model gives it: a step shown done is done, one called
    // done with nothing shown stays in progress.
    let answer = json!({
        "steps": {
            "probe": { "status": "completed", "evidence": "", "note": "" },
            "probe-2": { "status": "completed", "evidence": "PR 12 merged", "note": "" },
            "build": { "status": "blocked", "evidence": "", "note": "Waits for a review" },
        },
        "checkpoint": { "progress": "Probed twice.", "next_work": "The review." },
        "changed": true,
    });
    let (steps, progress, next_work) = keeper::decide(&plan, &answer);
    let of = |id: &str| {
        let found = steps.iter().find(|(item_id, _, _)| item_id == id).unwrap();
        (found.1.as_str(), found.2.as_str())
    };
    assert_eq!(of("a:ship:01:probe").0, "in_progress");
    assert!(of("a:ship:01:probe").1.starts_with(keeper::HELD_NOTE));
    assert_eq!(
        of("a:ship:01:probe-2"),
        ("completed", "Evidence: PR 12 merged")
    );
    assert_eq!(of("a:ship:01:build"), ("blocked", "Waits for a review"));
    assert_eq!(
        (progress.as_str(), next_work.as_str()),
        ("Probed twice.", "The review.")
    );
}

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

#[test]
fn a_goal_is_due_and_an_update_is_settled_as_the_python_hosts_keeper_does_it() {
    let vectors = vectors();
    let (mut asked, mut ready, mut looked_at, mut settled, mut named) = (0, 0, 0, 0, 0);
    for scenario in vectors["scenarios"].as_array().unwrap() {
        for step in scenario["steps"].as_array().unwrap() {
            match step["what"].as_str().unwrap() {
                "due" => {
                    let what = step["note"].as_str().unwrap();
                    let watch = keeper::watch(&step["config"]);
                    let now = step["now"].as_i64().unwrap();
                    let open: Vec<Open<'_>> = step["open"]
                        .as_array()
                        .unwrap()
                        .iter()
                        .map(|goal| Open {
                            plan_id: goal["plan_id"].as_str().unwrap(),
                            needs_catch_up: goal["catch_up"].as_bool().unwrap(),
                        })
                        .collect();
                    let looked = &step["looked"];
                    let mut looks = Vec::new();
                    let mut look = |plan_id: &str, full: bool| -> Activity {
                        let found = &looked[plan_id];
                        assert_eq!(
                            json!(full),
                            found["full"],
                            "{what}: {plan_id} read whole or not"
                        );
                        looks.push(plan_id.to_string());
                        Activity {
                            triggers: serde_json::from_value(found["triggers"].clone()).unwrap(),
                            status_change: found["status_change"].as_bool().unwrap(),
                            any_messages: found["any_messages"].as_bool().unwrap(),
                            ..Activity::default()
                        }
                    };
                    let mut flags = Vec::new();
                    let mut stalled = |plan_id: &str| -> bool {
                        flags.push(plan_id.to_string());
                        looked[plan_id]["stall_flag"]
                            .as_bool()
                            .expect("the reference asked whether it stalled too")
                    };
                    let mut progress = step["before"].clone();
                    let ours =
                        keeper::due(&watch, &mut progress, now, &open, &mut look, &mut stalled);
                    assert_eq!(json!(ours), step["ready"], "{what}");
                    assert_eq!(progress, step["after"], "{what}");
                    assert_eq!(
                        progress.to_string(),
                        step["after"].to_string(),
                        "{what}, as text"
                    );
                    // Every goal the reference looked at was looked at, and no other; the same
                    // for the goals it asked of whether they had stalled.
                    let theirs = looked.as_object().unwrap();
                    assert_eq!(looks.len(), theirs.len(), "{what}: {looks:?}");
                    let asked_of = theirs
                        .values()
                        .filter(|found| found.get("stall_flag").is_some())
                        .count();
                    assert_eq!(flags.len(), asked_of, "{what}: {flags:?}");
                    asked += 1;
                    ready += ours.len();
                    looked_at += looks.len();
                }
                "settle" => {
                    let mut progress = step["before"].clone();
                    let (plan, outcome) = (
                        step["plan"].as_str().unwrap(),
                        step["outcome"].as_str().unwrap(),
                    );
                    keeper::settle(
                        &mut progress,
                        plan,
                        outcome,
                        step["now"].as_i64().unwrap(),
                        step["error"].as_str().unwrap(),
                    );
                    assert_eq!(progress, step["after"], "{outcome}");
                    assert_eq!(
                        progress.to_string(),
                        step["after"].to_string(),
                        "{outcome}, as text"
                    );
                    settled += 1;
                }
                "request_id" => {
                    let config = &step["config"];
                    let plans: Vec<String> = serde_json::from_value(step["plans"].clone()).unwrap();
                    let ours = keeper::request_id(
                        config["agent_id"].as_str().unwrap(),
                        config["generation"].as_i64().unwrap(),
                        &plans,
                        step["now"].as_i64().unwrap(),
                    );
                    assert_eq!(ours, step["id"].as_str().unwrap());
                    named += 1;
                }
                _ => {}
            }
        }
    }
    assert_eq!(
        (asked, ready, looked_at, settled, named),
        (27, 27, 44, 11, 3)
    );
}

#[test]
fn a_keeper_is_told_only_of_what_it_watches() {
    let seen = || Activity {
        triggers: vec![
            (10, "owner turn end".to_string()),
            (20, "helper message".to_string()),
            (30, "goal pause".to_string()),
        ],
        status_change: true,
        newest_ms: 30,
        newest_event: 4,
        any_messages: true,
    };
    let watch = |owner_turns, goal_activity| {
        keeper::watch(&json!({ "attachments": [
        { "attachment_id": "a", "enabled": owner_turns, "trigger_version": 1, "trigger_id": "agent-work-completed" },
        { "attachment_id": "b", "enabled": goal_activity, "trigger_version": 1, "trigger_id": "goal-activity-observed" },
    ] }))
    };
    let labels = |found: &Activity| -> Vec<String> {
        found
            .triggers
            .iter()
            .map(|(_, what)| what.clone())
            .collect()
    };
    let turns_only = keeper::watched(seen(), &watch(true, false));
    assert_eq!(labels(&turns_only), ["owner turn end"]);
    assert!(!turns_only.status_change && turns_only.any_messages);
    let around_only = keeper::watched(seen(), &watch(false, true));
    assert_eq!(labels(&around_only), ["helper message", "goal pause"]);
    assert!(around_only.status_change);
    assert_eq!(keeper::watched(seen(), &watch(true, true)), seen());
}
