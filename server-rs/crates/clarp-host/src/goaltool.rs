//! The goal tool's words: what `clarp-goal` is asked on its command line, and what it prints.
//!
//! The rules are in [`clarp_kernel::goals`]. Here the arguments are read as the Python Host's
//! `scripts/agent_tasks.py` reads them, and the answers are printed as it prints them: skills
//! call the tool by name and read what it says. Nothing here knows of a Host: an [`Ask`] is
//! answered from an agent's book, and the answer says what is to be written.

use std::fmt::Display;

use clarp_kernel::goals::{Change, GoalBook, NotUpdated, Owner, Update, World, item_key};
use serde_json::Value;

use crate::tools::{NOT_HERE, ToolAnswer};

/// What the tool prints when it cannot read what it was called with.
pub const USAGE: &str = "usage: agent_tasks.py create SESSION PLAN_ID TITLE JSON_ITEMS | \
update RETURNED_PLAN_ID ITEM_ID STATUS [DETAIL] | \
finish RETURNED_PLAN_ID [STATUS] | show SESSION | list SESSION | \
goal SESSION ALIAS TITLE JSON_ITEMS JSON_GOAL | \
act PLAN_ID REVISION ACTION JSON_DATA | step PLAN_ID REVISION ITEM STATUS [DETAIL] | \
subgoal PLAN_ID add|update JSON | ledger PLAN_ID [AFTER_EVENT] | \
receipts PLAN_ID [LIMIT] | bookkeeping observe DELEGATION [JSON_OPTIONS] | \
bookkeeping record DELEGATION PLAN_ID WAKE_ID JSON_BATCH";

/// What a goal's owner does to it with a verb of its own: `checkpoint PLAN REVISION JSON`.
const ACTIONS: [&str; 12] = [
    "checkpoint",
    "replan",
    "pause",
    "resume",
    "cancel",
    "block",
    "supersede",
    "dependency",
    "enroll",
    "add_step",
    "document",
    "rebind",
];

/// What the Goal keeper's write is refused with, by its kind and never by its words: the
/// owner changed the plan since the keeper read it, or the goal is closed.
pub const OWNER_AHEAD: i32 = 75;
pub const CLOSED: i32 = 76;

/// What the Goal keeper writes of a goal ([`GoalBook::updater`]), as it is handed to the
/// goal's owner.
#[derive(Debug, Clone, PartialEq)]
pub struct Written {
    /// The plan's revision as the keeper read it.
    pub seen_revision: i64,
    /// The steps to move, each by its whole id: its status, and a note.
    pub steps: Vec<(String, String, String)>,
    pub progress: String,
    pub next_work: String,
    pub model: String,
    pub source_refs: Vec<String>,
    /// Where the next update reads from.
    pub through_ms: i64,
    pub through_event: i64,
    pub janitor_agent_id: String,
}

/// What the tool is asked.
#[derive(Debug, Clone, PartialEq)]
pub enum Ask {
    /// A new plan for a session, with a goal when one is given.
    Create {
        session: String,
        alias: String,
        title: String,
        items: Value,
        goal: Option<Value>,
    },
    Get {
        plan_id: String,
    },
    List {
        session: String,
    },
    /// The plan a session is working.
    Show {
        session: String,
    },
    /// A step moves. Without a revision it is the old way of asking, which a goal refuses.
    Step {
        plan_id: String,
        item: String,
        revision: Option<i64>,
        status: String,
        detail: Option<String>,
    },
    /// The plan ends.
    Finish {
        plan_id: String,
        status: String,
        revision: Option<i64>,
    },
    /// Everything else a goal's owner does to it.
    Act {
        plan_id: String,
        revision: i64,
        action: String,
        data: Value,
    },
    /// What the user does to a goal, through an app: everything its owner may do, and
    /// setting a criterion aside. No command line reads as this.
    UserAct {
        plan_id: String,
        revision: i64,
        action: String,
        data: Value,
    },
    Ledger {
        plan_id: String,
        after: i64,
    },
    /// A document of a goal.
    Read {
        plan_id: String,
        name: String,
        revision: Option<i64>,
    },
    /// What the Goal keeper writes beside the owner's own record: the statuses of steps and
    /// its own checkpoint. No command line reads as this. Printed: the steps that changed.
    Keeper {
        plan_id: String,
        written: Box<Written>,
    },
    /// The receipts the Goal keeper left of a goal. They are the Host's, not the owner's:
    /// who answers from a book has none.
    Receipts {
        plan_id: String,
        limit: Option<i64>,
    },
    /// A verb this Host does not have yet.
    NotHere {
        verb: String,
    },
}

/// Whose goals an ask is about.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum About<'a> {
    /// The agent with this session name.
    Session(&'a str),
    /// The agent that owns this plan.
    Plan(&'a str),
    Nobody,
}

/// The agent a plan belongs to: its id is what the plan's id begins with.
pub fn owner_of(plan_id: &str) -> &str {
    plan_id.split(':').next().unwrap_or("")
}

impl Ask {
    pub fn about(&self) -> About<'_> {
        match self {
            Self::Create { session, .. } | Self::List { session } | Self::Show { session } => {
                About::Session(session)
            }
            Self::Ledger { plan_id, .. }
            | Self::Get { plan_id }
            | Self::Step { plan_id, .. }
            | Self::Finish { plan_id, .. }
            | Self::Act { plan_id, .. }
            | Self::UserAct { plan_id, .. }
            | Self::Keeper { plan_id, .. }
            | Self::Receipts { plan_id, .. }
            | Self::Read { plan_id, .. } => About::Plan(plan_id),
            Self::NotHere { .. } => About::Nobody,
        }
    }
}

fn refused(why: impl Display) -> ToolAnswer {
    ToolAnswer::said(1, format!("agent_tasks: {why}"))
}

fn printed(value: &Value) -> ToolAnswer {
    ToolAnswer::printed(0, value.to_string())
}

fn integer(given: &str) -> Result<i64, ToolAnswer> {
    given
        .trim()
        .parse()
        .map_err(|_| refused(format!("invalid literal for int() with base 10: '{given}'")))
}

fn json(given: &str) -> Result<Value, ToolAnswer> {
    serde_json::from_str(given).map_err(refused)
}

/// Reads the tool's arguments, without its own name. The error is what the tool says when
/// it cannot.
pub fn parse(argv: &[String]) -> Result<Ask, ToolAnswer> {
    let args: Vec<&str> = argv.iter().map(String::as_str).collect();
    let Some((&verb, rest)) = args.split_first() else {
        return Err(ToolAnswer::said(2, USAGE));
    };
    let text = |given: &&str| (*given).to_string();
    Ok(match (verb, rest) {
        ("receipts", [plan_id, limit @ ..]) if limit.len() <= 1 => Ask::Receipts {
            plan_id: text(plan_id),
            limit: limit.first().map(|given| integer(given)).transpose()?,
        },
        // The ledger and what is kept in it.
        ("ledger", [plan_id, rest @ ..]) if rest.len() <= 1 => Ask::Ledger {
            plan_id: text(plan_id),
            after: rest.first().map(|v| integer(v)).transpose()?.unwrap_or(0),
        },
        ("bookkeeping", _) | ("subgoal", [_, _, _]) => Ask::NotHere {
            verb: verb.to_string(),
        },
        ("create" | "goal", [session, alias, title, items, goal]) => Ask::Create {
            session: text(session),
            alias: text(alias),
            title: text(title),
            items: json(items)?,
            goal: Some(json(goal)?),
        },
        (action, [plan_id, revision, data]) if ACTIONS.contains(&action) => Ask::Act {
            plan_id: text(plan_id),
            revision: integer(revision)?,
            action: action.to_string(),
            data: json(data)?,
        },
        ("act", [plan_id, revision, action, data]) => Ask::Act {
            plan_id: text(plan_id),
            revision: integer(revision)?,
            action: text(action),
            data: json(data)?,
        },
        ("step", [plan_id, revision, item, status, detail @ ..]) if detail.len() <= 1 => {
            Ask::Step {
                plan_id: text(plan_id),
                item: text(item),
                revision: Some(integer(revision)?),
                status: text(status),
                detail: detail.first().map(text),
            }
        }
        ("complete", [plan_id, revision]) => Ask::Finish {
            plan_id: text(plan_id),
            status: "completed".to_string(),
            revision: Some(integer(revision)?),
        },
        ("list", [session]) => Ask::List {
            session: text(session),
        },
        ("read", [plan_id, name, revision @ ..]) if revision.len() <= 1 => Ask::Read {
            plan_id: text(plan_id),
            name: text(name),
            revision: revision.first().map(|given| integer(given)).transpose()?,
        },
        ("get", [plan_id]) => Ask::Get {
            plan_id: text(plan_id),
        },
        ("create", [session, alias, title, items]) => Ask::Create {
            session: text(session),
            alias: text(alias),
            title: text(title),
            items: json(items)?,
            goal: None,
        },
        ("update", [plan_id, item, status, detail @ ..]) if detail.len() <= 1 => Ask::Step {
            plan_id: text(plan_id),
            item: text(item),
            revision: None,
            status: text(status),
            detail: detail.first().map(text),
        },
        ("finish", [plan_id, status @ ..]) if status.len() <= 1 => Ask::Finish {
            plan_id: text(plan_id),
            status: status.first().map_or_else(|| "completed".to_string(), text),
            revision: None,
        },
        ("show", [session]) => Ask::Show {
            session: text(session),
        },
        _ => return Err(ToolAnswer::said(2, USAGE)),
    })
}

/// Answers the tool from its owner's book: what it prints, and the change that is to be
/// written before it is printed.
pub fn answer(
    book: &GoalBook,
    owner: Owner<'_>,
    ask: &Ask,
    world: &mut World<'_>,
) -> (ToolAnswer, Option<Change>) {
    let now = world.now_ms;
    let changed = match ask {
        Ask::Ledger { plan_id, after } => {
            return (
                printed(
                    &serde_json::json!({"plan_id":plan_id,"events":[],"next_after":after,"has_more":false}),
                ),
                None,
            );
        }
        Ask::Get { plan_id } => {
            let plan = book.get(plan_id, now).unwrap_or(Value::Null);
            return (printed(&plan), None);
        }
        Ask::List { .. } => return (printed(&book.list(now)), None),
        Ask::Show { .. } => return (printed(&book.show(now)), None),
        Ask::Read {
            plan_id,
            name,
            revision,
        } => {
            let said = match book.read(plan_id, name, *revision) {
                Ok(document) => printed(&document),
                Err(why) => refused(why),
            };
            return (said, None);
        }
        Ask::Keeper { plan_id, written } => {
            let update = Update {
                seen_revision: written.seen_revision,
                steps: &written.steps,
                progress: &written.progress,
                next_work: &written.next_work,
                model: &written.model,
                source_refs: &written.source_refs,
                through_ms: written.through_ms,
                through_event: written.through_event,
                janitor_agent_id: &written.janitor_agent_id,
            };
            return match book.updater(plan_id, &update, now) {
                Ok((change, changed)) => {
                    let said = serde_json::json!({ "changed_steps": changed, "stored": true });
                    (printed(&said), Some(change))
                }
                Err(why) => {
                    let exit = match why {
                        NotUpdated::OwnerAhead => OWNER_AHEAD,
                        NotUpdated::Closed => CLOSED,
                        NotUpdated::Invalid(_) => 1,
                    };
                    let said = format!("agent_tasks: {}", why.said());
                    (ToolAnswer::said(exit, said), None)
                }
            };
        }
        Ask::Receipts { .. } => return (printed(&Value::Array(Vec::new())), None),
        Ask::NotHere { verb } => {
            let said = ToolAnswer::said(
                NOT_HERE,
                format!(
                    "clarp-goal {verb}: not available on this Host yet (the Rust Host). \
                     Nothing was done."
                ),
            );
            return (said, None);
        }
        Ask::Create {
            alias,
            title,
            items,
            goal,
            ..
        } => book.create(owner, alias, title, items, goal.as_ref(), world),
        Ask::Step {
            plan_id,
            item,
            revision,
            status,
            detail,
        } => {
            let item_id = item_key(plan_id, item, world.token_hex);
            book.step(&item_id, *revision, status, detail.as_deref(), now)
        }
        Ask::Finish {
            plan_id,
            status,
            revision,
        } => book.finish(plan_id, status, "", *revision, now),
        Ask::Act {
            plan_id,
            revision,
            action,
            data,
        } => book.act(plan_id, *revision, action, data, world),
        Ask::UserAct {
            plan_id,
            revision,
            action,
            data,
        } => book.act_for_user(plan_id, *revision, action, data, world),
    };
    match changed {
        Ok(change) => (printed(&book.shown(&change, now)), Some(change)),
        Err(why) => (refused(why), None),
    }
}

/// Why the tool refused, without the tool's name before it: what a route answers with.
pub fn refusal(said: &ToolAnswer) -> &str {
    let said = said.stderr.trim();
    said.strip_prefix("agent_tasks: ").unwrap_or(said)
}

/// What the tool says when there is nobody to ask: no agent has that session name, or no
/// agent owns that plan.
pub fn nobody(ask: &Ask, now_ms: i64) -> ToolAnswer {
    if matches!(ask, Ask::Create { .. }) {
        return refused("agent session not found");
    }
    let owner = Owner {
        agent_id: "",
        session: "",
    };
    let mut world = World {
        now_ms,
        native_session: "",
        queue_revision: 0,
        token_hex: &mut |bytes| "0".repeat(bytes * 2),
        has_job: &|_, _| false,
    };
    answer(&GoalBook::new(), owner, ask, &mut world).0
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    fn args(given: &[&str]) -> Vec<String> {
        given.iter().map(|arg| (*arg).to_string()).collect()
    }

    #[test]
    fn the_arguments_are_read_as_the_python_tool_reads_them() {
        let ask = parse(&args(&[
            "checkpoint",
            "a:ship:01",
            "7",
            "{\"progress\":\"x\"}",
        ]))
        .unwrap();
        assert!(matches!(&ask, Ask::Act { action, revision: 7, .. } if action == "checkpoint"));
        assert_eq!(ask.about(), About::Plan("a:ship:01"));
        assert_eq!(owner_of("a:ship:01"), "a");

        let ask = parse(&args(&["create", "vera", "ship", "Ship", "[]", "{}"])).unwrap();
        assert!(matches!(&ask, Ask::Create { goal: Some(_), .. }));
        assert_eq!(ask.about(), About::Session("vera"));
        let ask = parse(&args(&["create", "vera", "ship", "Ship", "[]"])).unwrap();
        assert!(matches!(&ask, Ask::Create { goal: None, .. }));

        let ask = parse(&args(&[
            "step",
            "p",
            "3",
            "probe",
            "completed",
            "It behaves",
        ]))
        .unwrap();
        assert!(matches!(
            &ask,
            Ask::Step {
                revision: Some(3),
                detail: Some(_),
                ..
            }
        ));
        let ask = parse(&args(&["update", "p", "probe", "completed"])).unwrap();
        assert!(matches!(
            &ask,
            Ask::Step {
                revision: None,
                detail: None,
                ..
            }
        ));
        let ask = parse(&args(&["finish", "p"])).unwrap();
        assert!(
            matches!(&ask, Ask::Finish { status, revision: None, .. } if status == "completed")
        );
        let ask = parse(&args(&["read", "p", "notes.md", "2"])).unwrap();
        assert!(matches!(
            &ask,
            Ask::Read {
                revision: Some(2),
                ..
            }
        ));
    }

    #[test]
    fn what_cannot_be_read_is_said_with_the_tools_exit_codes() {
        assert_eq!(parse(&[]).unwrap_err().exit, 2);
        assert_eq!(parse(&args(&["frobnicate", "p"])).unwrap_err().exit, 2);
        assert_eq!(parse(&args(&["checkpoint", "p", "1"])).unwrap_err().exit, 2);
        let said = parse(&args(&["checkpoint", "p", "one", "{}"])).unwrap_err();
        assert_eq!(said.exit, 1);
        assert_eq!(
            said.stderr.trim(),
            "agent_tasks: invalid literal for int() with base 10: 'one'"
        );
        assert_eq!(
            parse(&args(&["checkpoint", "p", "1", "not json"]))
                .unwrap_err()
                .exit,
            1
        );
        // A ledger read of an unknown plan returns an empty page.
        let ask = parse(&args(&["ledger", "p"])).unwrap();
        let page = nobody(&ask, 0);
        assert_eq!(page.exit, 0);
        assert_eq!(
            serde_json::from_str::<Value>(&page.stdout).unwrap(),
            serde_json::json!({"plan_id":"p","events":[],"has_more":false,"next_after":0})
        );
        // The keeper's receipts of a goal are asked for by the goal, with how many.
        let ask = parse(&args(&["receipts", "a:ship:01", "5"])).unwrap();
        assert!(matches!(&ask, Ask::Receipts { limit: Some(5), .. }));
        assert_eq!(ask.about(), About::Plan("a:ship:01"));
        assert_eq!(nobody(&ask, 0).stdout.trim(), "[]");
        assert_eq!(
            parse(&args(&["receipts", "p", "five"])).unwrap_err().exit,
            1
        );
    }

    #[test]
    fn the_keepers_write_is_refused_by_its_kind() {
        let owner = Owner {
            agent_id: "a",
            session: "vera",
        };
        let mut token = |bytes: usize| "0".repeat(bytes * 2);
        let mut world = World {
            now_ms: 1_000,
            native_session: "",
            queue_revision: 0,
            token_hex: &mut token,
            has_job: &|_, _| false,
        };
        let mut book = GoalBook::new();
        let done = |book: &mut GoalBook, ask: &Ask, world: &mut World<'_>| {
            let (said, change) = answer(book, owner, ask, world);
            if let Some(change) = &change {
                let record = book.record(change, world.now_ms);
                book.apply(&record);
            }
            said
        };
        let items = r#"[{"id": "probe", "title": "Probe"}]"#;
        let goal = r#"{"outcome": "Shipped", "criteria": ["It runs"], "limits": "None"}"#;
        let create = parse(&args(&["create", "vera", "ship", "Ship", items, goal])).unwrap();
        let made: Value =
            serde_json::from_str(&done(&mut book, &create, &mut world).stdout).unwrap();
        let plan_id = made["plan_id"].as_str().unwrap().to_string();
        let keeper = |seen_revision: i64| Ask::Keeper {
            plan_id: plan_id.clone(),
            written: Box::new(Written {
                seen_revision,
                steps: vec![(
                    format!("{plan_id}:probe"),
                    "in_progress".to_string(),
                    String::new(),
                )],
                progress: "Probing.".to_string(),
                next_work: String::new(),
                model: "m".to_string(),
                source_refs: Vec::new(),
                through_ms: 900,
                through_event: 1,
                janitor_agent_id: "k".to_string(),
            }),
        };
        // The owner changed the plan since: by a number of its own, and in the tool's words.
        let ahead = done(&mut book, &keeper(7), &mut world);
        assert_eq!(
            (ahead.exit, ahead.stderr.trim()),
            (
                OWNER_AHEAD,
                "agent_tasks: plan changed; reload before editing"
            )
        );
        // As it was read: written, and the steps that changed are printed.
        let written = done(&mut book, &keeper(0), &mut world);
        let printed: Value = serde_json::from_str(&written.stdout).unwrap();
        assert_eq!(written.exit, 0);
        assert_eq!(printed["stored"], true);
        assert_eq!(printed["changed_steps"][0]["from"], "pending");
        assert_eq!(keeper(0).about(), About::Plan(&plan_id));
        // A goal that is closed.
        let cancel = parse(&args(&[
            "cancel",
            &plan_id,
            "0",
            r#"{"reason": "Not needed"}"#,
        ]));
        assert_eq!(done(&mut book, &cancel.unwrap(), &mut world).exit, 0);
        let closed = done(&mut book, &keeper(1), &mut world);
        assert_eq!(
            (closed.exit, closed.stderr.trim()),
            (CLOSED, "agent_tasks: goal is closed")
        );
    }

    #[test]
    fn with_nobody_to_ask_the_tool_says_what_the_python_tool_says() {
        let get = parse(&args(&["get", "nobody:ship:01"])).unwrap();
        assert_eq!(nobody(&get, 0).stdout.trim(), "null");
        let list = parse(&args(&["list", "nobody"])).unwrap();
        assert_eq!(nobody(&list, 0).stdout.trim(), "[]");
        let show = parse(&args(&["show", "nobody"])).unwrap();
        assert_eq!(nobody(&show, 0).stdout.trim(), "{}");
        let create = parse(&args(&["create", "nobody", "ship", "Ship", "[]", "{}"])).unwrap();
        assert_eq!(
            nobody(&create, 0).stderr.trim(),
            "agent_tasks: agent session not found"
        );
        let act = parse(&args(&["pause", "nobody:ship:01", "0", "{}"])).unwrap();
        assert_eq!(
            nobody(&act, 0).stderr.trim(),
            "agent_tasks: task plan not found"
        );
        let step = parse(&args(&[
            "step",
            "nobody:ship:01",
            "0",
            "probe",
            "completed",
        ]))
        .unwrap();
        assert_eq!(
            nobody(&step, 0).stderr.trim(),
            "agent_tasks: task item not found"
        );
    }
}
