//! The goal tool against the Python Host's own.
//!
//! `fixtures/goal-vectors.json` is written by `server-rs/tools/goal-vectors.py`: sequences of
//! `clarp-goal` commands run against the Python Host's goal rules with a fixed clock and fixed
//! ids, each with what the tool printed and how it exited. Here the same commands are given
//! to this Host's tool with the same clock and ids, and must be answered the same.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_host::goaltool::{self, About};
use clarp_host::tools::NOT_HERE;
use clarp_kernel::goals::{GoalBook, Owner, World};
use serde_json::Value;

/// Where two values differ, as paths into them.
fn differences(path: &str, ours: &Value, theirs: &Value, found: &mut Vec<String>) {
    match (ours, theirs) {
        (Value::Object(a), Value::Object(b)) => {
            for (key, value) in a {
                match b.get(key) {
                    Some(other) => differences(&format!("{path}.{key}"), value, other, found),
                    None => found.push(format!("{path}.{key}: only here: {value}")),
                }
            }
            for (key, value) in b {
                if !a.contains_key(key) {
                    found.push(format!("{path}.{key}: only in the Python Host's: {value}"));
                }
            }
        }
        (Value::Array(a), Value::Array(b)) if a.len() == b.len() => {
            for (index, (value, other)) in a.iter().zip(b).enumerate() {
                differences(&format!("{path}[{index}]"), value, other, found);
            }
        }
        _ if ours == theirs => {}
        _ => found.push(format!("{path}: here {ours}, the Python Host's {theirs}")),
    }
}

/// Whether two wake messages say the same. What a goal waited for is shown in them as JSON,
/// whose fields this Host writes in the order of the alphabet and the Python Host in the
/// order they were given: that line is compared as what it says.
fn same_message(ours: &str, theirs: &str) -> bool {
    let (ours, theirs): (Vec<&str>, Vec<&str>) = (ours.lines().collect(), theirs.lines().collect());
    ours.len() == theirs.len()
        && ours.iter().zip(&theirs).all(|(a, b)| {
            let result = |line: &str| {
                let said = line.strip_prefix("Dependency result: ")?;
                serde_json::from_str::<Value>(said).ok()
            };
            match (result(a), result(b)) {
                (Some(a), Some(b)) => a == b,
                _ => a == b,
            }
        })
}

/// What the tool printed, as the vectors keep it: the JSON it is, or nothing.
fn printed(stdout: &str) -> Value {
    match stdout.trim() {
        "" => Value::Null,
        text => serde_json::from_str(text).unwrap_or_else(|_| Value::String(text.to_string())),
    }
}

#[test]
fn the_goal_tool_answers_as_the_python_hosts_does() {
    let vectors: Value = serde_json::from_str(include_str!("fixtures/goal-vectors.json")).unwrap();
    let mut compared = 0;
    let mut prompts = 0;
    let mut wrong: Vec<String> = Vec::new();
    for scenario in vectors["scenarios"].as_array().unwrap() {
        let name = scenario["name"].as_str().unwrap();
        let owner = Owner {
            agent_id: scenario["agent_id"].as_str().unwrap(),
            session: scenario["session"].as_str().unwrap(),
        };
        let mut book = GoalBook::new();
        // The ids are counted as the generator counts them; making the agent took the first.
        let mut made = 1u64;
        let mut ids = move |bytes: usize| {
            made += 1;
            format!("{made:0width$x}", width = bytes * 2)
        };
        for (number, step) in scenario["steps"].as_array().unwrap().iter().enumerate() {
            let argv: Vec<String> = step["argv"]
                .as_array()
                .unwrap()
                .iter()
                .map(|arg| arg.as_str().unwrap().to_string())
                .collect();
            let at = step["at"].as_i64().unwrap();
            let verb = argv.first().map_or("", String::as_str);
            let context = format!("{name}, step {number} ({verb})");
            let said = match goaltool::parse(&argv) {
                Err(said) => said,
                Ok(ask) => {
                    let ours = match ask.about() {
                        About::Session(session) => session == owner.session,
                        About::Plan(plan_id) => goaltool::owner_of(plan_id) == owner.agent_id,
                        About::Nobody => false,
                    };
                    if ours {
                        let mut world = World {
                            now_ms: at,
                            native_session: "",
                            queue_revision: 0,
                            token_hex: &mut ids,
                            has_job: &|_, _| false,
                        };
                        let (said, change) = goaltool::answer(&book, owner, &ask, &mut world);
                        if let Some(change) = change {
                            // As the Host does it: what the log keeps is what the book takes.
                            let record = book.record(&change, at);
                            let kept: clarp_kernel::goals::GoalRecord =
                                serde_json::from_slice(&serde_json::to_vec(&record).unwrap())
                                    .unwrap();
                            book.apply(&kept);
                            let mut plan = book.plan(&change.plan.plan_id).cloned().unwrap();
                            plan.order = change.plan.order;
                            assert_eq!(plan, change.plan, "{context}: the record keeps the change");
                        }
                        said
                    } else {
                        goaltool::nobody(&ask, at)
                    }
                }
            };
            // The ledger, and what is kept in it, this Host does not have yet. A scenario is
            // followed until it changes something there.
            if matches!(verb, "ledger" | "subgoal" | "receipts" | "bookkeeping") {
                assert_eq!(said.exit, NOT_HERE, "{context}");
                if verb == "subgoal" {
                    break;
                }
                continue;
            }
            compared += 1;
            let mut found = Vec::new();
            let exit = step["exit"].as_i64().unwrap();
            if i64::from(said.exit) != exit {
                found.push(format!(
                    "exit: here {}, the Python Host's {exit}",
                    said.exit
                ));
            }
            differences(
                "stdout",
                &printed(&said.stdout),
                &step["stdout"],
                &mut found,
            );
            let stderr = step["stderr"].as_str().unwrap();
            // What Python itself says of a text that is no JSON is Python's to word.
            let pythons_own = stderr.starts_with("agent_tasks: Expecting ");
            if !pythons_own && said.stderr.trim() != stderr {
                found.push(format!(
                    "stderr: here {:?}, the Python Host's {stderr:?}",
                    said.stderr.trim()
                ));
            }
            // With every goal it printed, the Python Host says what it would wake the goal's
            // owner with at that moment.
            if let Some(theirs) = step["wake_prompt"].as_str() {
                let plan_id = step["stdout"]["plan_id"].as_str().unwrap();
                let ours = book.wake_prompt(plan_id).unwrap_or_default();
                prompts += 1;
                if !same_message(&ours, theirs) {
                    found.push(format!(
                        "the wake message: here\n{ours}\nthe Python Host's\n{theirs}"
                    ));
                }
            }
            if !found.is_empty() {
                found.truncate(12);
                wrong.push(format!("{context}:\n    {}", found.join("\n    ")));
            }
        }
    }
    assert!(
        wrong.is_empty(),
        "{} steps differ:\n{}",
        wrong.len(),
        wrong.join("\n")
    );
    assert!(compared >= 70, "only {compared} steps were compared");
    assert!(prompts >= 35, "only {prompts} wake messages were compared");
}
