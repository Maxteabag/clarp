//! Compare native transcript evidence and registry changes with the recorded Python Host.
#![allow(clippy::unwrap_used)]
use clarp_kernel::provider_tasks::{self as native, Task};
use serde_json::{Value, json};

const NOW: i64 = 1_800_000_000_000;
const SESSION: &str = "fixture-session";
const TURN: &str = "aaaaaaaaaaaaaaaaaaaaaaaa";
fn fixture() -> Value {
    serde_json::from_str(include_str!("fixtures/provider-background-tasks.json")).unwrap()
}
#[test]
fn the_parent_transcript_reveals_only_matching_background_bash_evidence() {
    let f = fixture();
    let mut counts = [0; 3];
    for row in f["parser"].as_array().unwrap() {
        assert_eq!(
            json!(native::parse_actions(SESSION, "project", &row[0], NOW)),
            row[1],
            "{}",
            row[0]
        );
        counts[0] += 1;
    }
    for row in f["ids"].as_array().unwrap() {
        let id = row[0].as_str().unwrap();
        assert_eq!(native::valid_id(id), row[1]);
        assert_eq!(native::job_id("agent", SESSION, id), row[2]);
        counts[1] += 1;
    }
    for row in f["redaction"].as_array().unwrap() {
        assert_eq!(native::safe_text(row[0].as_str().unwrap()), row[1]);
        counts[2] += 1;
    }
    assert_eq!(counts, [121, 10, 41]);
}
#[test]
fn replay_and_late_evidence_match_the_state_and_event_of_each_write() {
    let f = fixture();
    let mut steps = 0;
    for story in f["stories"].as_array().unwrap() {
        let mut job: Option<Task> = None;
        for step in story.as_array().unwrap() {
            let request = &step[0];
            let now = request["now"].as_i64().unwrap();
            let changed = match request["op"].as_str().unwrap() {
                "action" => serde_json::from_value(request["action"].clone())
                    .ok()
                    .is_some_and(|evidence| {
                        native::observe(&mut job, SESSION, NOW - 1, &evidence, now)
                    }),
                "exit" => job.as_mut().is_some_and(|t| t.close_turn(TURN, now)),
                "expire" => job
                    .as_mut()
                    .is_some_and(|t| t.expire(request["protected"] == true, now)),
                "reconcile" => job
                    .as_mut()
                    .is_some_and(|t| t.reconcile(request["ended"] == true, now)),
                other => panic!("unrecognized operation {other}"),
            };
            let mut snapshot = json!(job);
            if let Some(task) = job.as_ref() {
                snapshot["display"] = task.display(now);
            }
            let expected = &f["snapshots"][step[1].as_u64().unwrap() as usize];
            assert_eq!(
                (&snapshot, json!(changed)),
                (expected, step[2].clone()),
                "{request}"
            );
            steps += 1;
        }
    }
    assert_eq!(steps, 1142);
}
#[test]
fn only_exact_registrations_supersede_display_and_provider_cancellation_is_refused() {
    let f = fixture();
    for row in f["mirrors"].as_array().unwrap() {
        assert_eq!(
            native::mirror(
                &row[0],
                row[1].as_array().unwrap(),
                row[2].as_i64().unwrap()
            ),
            row[3],
            "{row}"
        );
    }
    let cancel = &f["cancel"];
    let (status, receipt) = native::cancel_refusal(&cancel[1]["job"]);
    assert_eq!(json!([status, receipt]), *cancel);
    for row in f["retention"].as_array().unwrap() {
        assert_eq!(
            native::turn_link_expired(NOW, NOW + row[0].as_i64().unwrap()),
            row[1]
        );
    }
    assert_eq!(f["mirrors"].as_array().unwrap().len(), 36);
    assert_eq!(f["retention"].as_array().unwrap().len(), 3);
}

#[test]
fn visits_rotate_across_eligible_runtime_bindings() {
    type Case = (
        Vec<(String, String, Option<i64>, bool)>,
        (String, String),
        Vec<(String, String)>,
    );
    let cases: Vec<Case> =
        serde_json::from_str(include_str!("fixtures/provider-observer.json")).unwrap();
    let mut count = 0;
    for (bindings, after, selected) in cases {
        let eligible = bindings
            .into_iter()
            .filter_map(|(agent, native, ended, unfinished)| {
                native::visit_binding(ended, unfinished, NOW).then_some((agent, native))
            })
            .collect();
        assert_eq!(native::rotate_idle(eligible, &after), selected);
        count += 1;
    }
    assert_eq!(count, 320);
}

#[test]
fn the_hook_returns_only_the_scoped_origin_and_background_turn_advice(){
    let data:Value=serde_json::from_str(include_str!("fixtures/provider-hook.json")).unwrap();
    let reference=data["contexts"][0].as_str().unwrap();
    let rust=reference.replace("A process you detach with setsid or nohup outlives the turn but still belongs to the Clarp runtime service, so a runtime restart can kill it, and nothing reports its result to you.","A process you detach with setsid can outlive the turn and a Host restart; nothing reports its result to you.");
    let mut count=0;
    for row in data["cases"].as_array().unwrap(){
        let answer=native::background_hook(row[1].as_str().unwrap(),row[2].as_bool().unwrap(),&row[0]);
        let mut expected=json!({"hookEventName":"PreToolUse"});
        if !row[3].is_null(){expected["updatedInput"]=row[3].clone();}
        if !row[4].is_null(){expected["additionalContext"]=json!(rust);}
        let expected=(!row[3].is_null()||!row[4].is_null()).then(||json!({"hookSpecificOutput":expected}));
        assert_eq!(answer,expected,"{}",row[0]);
        count+=1;
    }
    assert_eq!(count,117);
}
