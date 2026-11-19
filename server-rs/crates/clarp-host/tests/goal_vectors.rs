//! The goal tool against the Python Host's own.
//!
//! `fixtures/goal-vectors.json` is written by `server-rs/tools/goal-vectors.py`: sequences of
//! `clarp-goal` commands run against the Python Host's goal rules with a fixed clock and fixed
//! ids, each with what the tool printed and how it exited. Here the same commands are given
//! to this Host's tool with the same clock and ids, and must be answered the same. Among them
//! is what an app does to a goal for its user (`POST /task-plan/action`), which goes by the
//! same rules with the user as the one who acts.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_host::goaltool::{self, About};
use clarp_host::tools::NOT_HERE;
use clarp_kernel::goals::{GoalBook, Owner, World};
use serde_json::{Value, json};

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

/// The carrying of goals to a new conversation in the vectors, counted by the script that
/// wrote them: how often the Host carried one over, how many plans followed, and how many
/// steps were taken under a bound conversation.
const CARRIED: (usize, usize, usize) = (4, 3, 17);

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
    let mut users = 0;
    let mut keeper = 0;
    let (mut carries, mut carried, mut bound) = (0, 0, 0);
    let mut wrong: Vec<String> = Vec::new();
    for scenario in vectors["scenarios"].as_array().unwrap() {
        let name = scenario["name"].as_str().unwrap();
        let owner = Owner {
            agent_id: scenario["agent_id"].as_str().unwrap(),
            session: scenario["session"].as_str().unwrap(),
        };
        let mut book = GoalBook::new();
        let mut ledger_rows: Vec<Value> = Vec::new();
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
            // The owner's conversation with its tool, as it was bound at this step.
            let native = step["native"].as_str().unwrap_or("");
            bound += usize::from(!native.is_empty());
            // The Host continued the owner's conversation in a new one: no command of the
            // tool either. Written down as [carry, from, to, reason], with the plans that
            // moved.
            if step["by"] == "host" {
                let moved = book.carried(&argv[1], &argv[2], &argv[3], at);
                let ids: Vec<&str> = moved
                    .iter()
                    .map(|change| change.plan.plan_id.as_str())
                    .collect();
                assert_eq!(
                    json!(ids),
                    step["stdout"],
                    "{context}: the plans that moved"
                );
                for change in &moved {
                    let record = book.record(change, at);
                    let kept: clarp_kernel::goals::GoalRecord =
                        serde_json::from_slice(&serde_json::to_vec(&record).unwrap()).unwrap();
                    book.apply(&kept);
                }
                carries += 1;
                carried += moved.len();
                continue;
            }
            // What the Goal keeper writes beside the owner's record is no command of the tool:
            // it is written down as [updater, plan, what was asked], with the plan as it is
            // read afterwards.
            if step["by"] == "keeper" {
                let asked: Value = serde_json::from_str(&argv[2]).unwrap();
                let wanted: Vec<(String, String, String)> = asked["steps"]
                    .as_object()
                    .unwrap()
                    .iter()
                    .map(|(item_id, wanted)| {
                        let of = |key: &str| wanted[key].as_str().unwrap_or_default().to_string();
                        (item_id.clone(), of("status"), of("note"))
                    })
                    .collect();
                let sources: Vec<String> =
                    serde_json::from_value(asked["source_refs"].clone()).unwrap();
                let update = clarp_kernel::goals::Update {
                    seen_revision: asked["seen_revision"].as_i64().unwrap(),
                    steps: &wanted,
                    progress: asked["progress"].as_str().unwrap(),
                    next_work: asked["next_work"].as_str().unwrap(),
                    model: asked["model"].as_str().unwrap(),
                    source_refs: &sources,
                    through_ms: asked["through_ms"].as_i64().unwrap(),
                    through_event: asked["through_event"].as_i64().unwrap(),
                    janitor_agent_id: asked["janitor_agent_id"].as_str().unwrap(),
                };
                match book.updater(&argv[1], &update, at) {
                    Ok((change, changed)) => {
                        let record = book.record(&change, at);
                        let kept: clarp_kernel::goals::GoalRecord =
                            serde_json::from_slice(&serde_json::to_vec(&record).unwrap()).unwrap();
                        book.apply(&kept);
                        let theirs = &step["stdout"];
                        assert_eq!(step["exit"], 0, "{context}: written here, refused there");
                        assert_eq!(
                            serde_json::json!({ "changed_steps": changed, "stored": true }),
                            *theirs,
                            "{context}"
                        );
                    }
                    Err(refused) => {
                        assert_eq!(
                            step["exit"], 1,
                            "{context}: refused here ({refused:?}), written there"
                        );
                        assert_eq!(
                            refused.said(),
                            step["stderr"].as_str().unwrap(),
                            "{context}"
                        );
                        let kind = match refused {
                            clarp_kernel::goals::NotUpdated::Closed => "GoalClosed",
                            clarp_kernel::goals::NotUpdated::OwnerAhead => "PlanChanged",
                            clarp_kernel::goals::NotUpdated::Invalid(_) => "ValueError",
                        };
                        assert_eq!(kind, step["refused"].as_str().unwrap(), "{context}");
                    }
                }
                let read = book.get(&argv[1], at).unwrap();
                if read != step["plan"] {
                    wrong.push(format!(
                        "{context}: the plan after the keeper wrote\n  theirs: {}\n  ours:   {}",
                        step["plan"], read
                    ));
                }
                keeper += 1;
                continue;
            }
            // What an app asks for its user is written down as [action, plan, revision, data].
            let by_user = step["by"] == "user";
            let asked = if by_user {
                users += 1;
                Ok(goaltool::Ask::UserAct {
                    plan_id: argv[1].clone(),
                    revision: argv[2].parse().unwrap(),
                    action: argv[0].clone(),
                    data: serde_json::from_str(&argv[3]).unwrap(),
                })
            } else {
                goaltool::parse(&argv)
            };
            let said = match asked {
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
                            native_session: native,
                            queue_revision: 0,
                            token_hex: &mut ids,
                            has_job: &|_, _| false,
                        };
                        let (said, change) = goaltool::answer(&book, owner, &ask, &mut world);
                        if let Some(change) = change {
                            // The Python Host makes the artifact a new plan is shown as, and
                            // names it: one id more.
                            if matches!(ask, goaltool::Ask::Create { .. }) {
                                ids(12);
                            }
                            // As the Host does it: what the log keeps is what the book takes.
                            let author = if by_user {
                                clarp_kernel::goals::ledger::Author::User
                            } else {
                                clarp_kernel::goals::ledger::Author::Owner(None)
                            };
                            for (_, mut event) in clarp_kernel::goals::ledger::changes(
                                book.plan(&change.plan.plan_id),
                                &change.plan,
                                author,
                            ) {
                                event.event_id = ledger_rows.len() as i64 + 1;
                                let mut row = json!(event);
                                row["actor_session"] =
                                    json!(if event.actor_agent_id == owner.agent_id {
                                        owner.session
                                    } else {
                                        ""
                                    });
                                ledger_rows.push(row);
                            }
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
            if verb == "ledger" {
                assert_eq!(said.exit, 0, "{context}");
                let expected = &step["stdout"];
                assert_eq!(
                    expected["events"],
                    json!(
                        ledger_rows
                            .iter()
                            .filter(|e| e["plan_id"] == argv[1])
                            .collect::<Vec<_>>()
                    ),
                    "{context}"
                );
                compared += 1;
                continue;
            }
            // The delegate's bookkeeping and owner subgoals are later pieces.
            if matches!(verb, "subgoal" | "receipts" | "bookkeeping") {
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
            // A route answers with the refusal alone, the tool with its name before it.
            let ours = if by_user {
                goaltool::refusal(&said)
            } else {
                said.stderr.trim()
            };
            if !pythons_own && ours != stderr {
                found.push(format!(
                    "stderr: here {ours:?}, the Python Host's {stderr:?}"
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
    assert!(compared >= 90, "only {compared} steps were compared");
    assert!(prompts >= 50, "only {prompts} wake messages were compared");
    assert!(
        users >= 12,
        "only {users} of the user's own steps were compared"
    );
    assert_eq!(
        keeper, 17,
        "what the Goal keeper wrote, and what it was refused"
    );
    // Counted by the script that wrote the vectors: the times the Host carried a
    // conversation over, the plans that followed, and the steps under a bound conversation.
    assert_eq!((carries, carried, bound), CARRIED);
}
