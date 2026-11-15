//! The goal tool's words: what `clarp-goal` is asked on its command line, and what it prints.
//!
//! The rules are in [`clarp_kernel::goals`]. Here the arguments are read as the Python Host's
//! `scripts/agent_tasks.py` reads them, and the answers are printed as it prints them: skills
//! call the tool by name and read what it says. Nothing here knows of a Host: an [`Ask`] is
//! answered from an agent's book, and the answer says what is to be written.

use std::fmt::Display;

use clarp_kernel::goals::{Change, GoalBook, Owner, World, item_key};
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
    /// A document of a goal.
    Read {
        plan_id: String,
        name: String,
        revision: Option<i64>,
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
            Self::Get { plan_id }
            | Self::Step { plan_id, .. }
            | Self::Finish { plan_id, .. }
            | Self::Act { plan_id, .. }
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
        // The ledger and what is kept in it.
        ("bookkeeping", _) | ("receipts" | "ledger", [_] | [_, _]) | ("subgoal", [_, _, _]) => {
            Ask::NotHere {
                verb: verb.to_string(),
            }
        }
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
    };
    match changed {
        Ok(change) => (printed(&book.shown(&change, now)), Some(change)),
        Err(why) => (refused(why), None),
    }
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
        // The ledger is not here yet, and says so instead of doing something else.
        let ask = parse(&args(&["ledger", "p"])).unwrap();
        assert_eq!(nobody(&ask, 0).exit, NOT_HERE);
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
