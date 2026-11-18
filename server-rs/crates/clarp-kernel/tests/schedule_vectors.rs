//! Schedules, against the Python Host's own scheduler.
//!
//! `fixtures/schedule-vectors.json` is written by `server-rs/tools/schedule-vectors.py`:
//! which cron expressions that scheduler takes and why not, when each runs next from moments
//! made up there, and what it answers through scripts of making, changing, running and
//! removing. The same must come of the same here.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_kernel::schedules::{self, Change, Made, Schedules};
use serde_json::{Value, json};

fn vectors() -> Value {
    serde_json::from_str(include_str!("fixtures/schedule-vectors.json")).unwrap()
}

#[test]
fn an_expression_is_read_and_runs_next_when_the_python_scheduler_says() {
    let vectors = vectors();
    let moments: Vec<i64> = vectors["moments"]
        .as_array()
        .unwrap()
        .iter()
        .map(|moment| moment.as_i64().unwrap())
        .collect();
    let rows = vectors["expressions"].as_array().unwrap();
    let (mut taken, mut refused, mut never) = (0, 0, 0);
    for row in rows {
        let expression = row["expression"].as_str().unwrap();
        match schedules::parse(expression) {
            Ok(cron) => {
                taken += 1;
                let fields = [
                    &cron.minutes,
                    &cron.hours,
                    &cron.days,
                    &cron.months,
                    &cron.weekdays,
                ];
                assert_eq!(json!(fields), row["fields"], "{expression:?}");
                let next: Vec<Option<i64>> = moments
                    .iter()
                    .map(|moment| cron.next_run(*moment))
                    .collect();
                assert_eq!(json!(next), row["next"], "{expression:?}");
                never += next.iter().filter(|next| next.is_none()).count();
                let again = schedules::next_run(expression, moments[0]).unwrap();
                assert_eq!(again, next[0]);
            }
            Err(why) => {
                refused += 1;
                assert_eq!(json!(why), row["refused"], "{expression:?}");
            }
        }
    }
    assert!(
        rows.len() >= 87 && taken >= 46 && refused >= 41 && never >= 20,
        "{taken} taken, {refused} refused, {never} that never run"
    );
}

#[test]
fn schedules_are_made_changed_run_and_removed_as_the_python_scheduler_does() {
    let vectors = vectors();
    let agents = vectors["agents"].as_array().unwrap();
    // The agent a request names, by its session or else by its id.
    let found = |named: &str| -> Option<(&str, &str)> {
        let by = |key: &str| agents.iter().find(|agent| agent[key] == named);
        let agent = by("session").or_else(|| by("agent_id"))?;
        Some((
            agent["agent_id"].as_str().unwrap(),
            agent["session"].as_str().unwrap(),
        ))
    };
    let (mut steps, mut refusals, mut prompts) = (0, 0, 0);
    for script in vectors["scripts"].as_array().unwrap() {
        let name = script["name"].as_str().unwrap();
        let mut kept = Schedules::default();
        let mut made: u64 = 0;
        for (place, row) in script["steps"].as_array().unwrap().iter().enumerate() {
            let step = &row["step"];
            let now = row["now"].as_i64().unwrap();
            let said = |key: &str| step[key].as_str().unwrap_or("");
            let here = format!("{name}, step {place}: {step}");
            let before = made;
            let mut sent: Vec<Value> = Vec::new();
            let answered: Result<Value, String> = match said("do") {
                "tick" => Ok(Value::Null),
                "create" => {
                    let new_id = format!("{:012x}{}", made + 1, "0".repeat(20));
                    let asked = Made {
                        agent: found(said("session")),
                        named: said("session"),
                        name: said("name"),
                        cron: said("cron"),
                        prompt: said("prompt"),
                        enabled: step.get("enabled").is_none_or(|on| on == true),
                    };
                    let done = kept
                        .create(&asked, &new_id, now)
                        .map(|schedule| json!(schedule));
                    made += u64::from(done.is_ok());
                    done
                }
                "update" => {
                    let change = Change {
                        enabled: step["enabled"].as_bool(),
                        name: step["name"].as_str(),
                        cron: step["cron"].as_str(),
                        prompt: step["prompt"].as_str(),
                    };
                    let done = kept.update(said("schedule_id"), &change, now);
                    done.map(|schedule| json!(schedule))
                }
                "delete" => Ok(json!(kept.delete(said("schedule_id")))),
                "list" => Ok(json!(kept.list(said("session"), said("agent_id")))),
                "due" => Ok(json!(kept.due(now))),
                "run" => {
                    let ran = kept.run(now);
                    let told = ran
                        .iter()
                        .map(|(_, session, prompt)| json!([session, prompt]));
                    sent = told.collect();
                    Ok(json!(sent.len()))
                }
                other => panic!("{other}"),
            };
            steps += 1;
            match answered {
                Ok(answer) => {
                    assert_eq!(row["refused"], Value::Null, "{here}");
                    assert_eq!(answer, row["result"], "{here}");
                }
                Err(why) => {
                    refusals += 1;
                    assert_eq!(json!(why), row["refused"], "{here}");
                }
            }
            assert_eq!(json!(sent), row["sent"], "{here}");
            assert_eq!(json!(made - before), row["minted"], "{here}");
            prompts += sent.len();
            // What is kept is read back as it was.
            let written = serde_json::to_vec(kept.rows()).unwrap();
            let again = Schedules::kept(serde_json::from_slice(&written).unwrap());
            assert_eq!(again, kept, "{here}");
        }
    }
    assert!(
        steps >= 80 && refusals >= 9 && prompts >= 7,
        "{steps} steps, {refusals} refused, {prompts} prompts"
    );
}
