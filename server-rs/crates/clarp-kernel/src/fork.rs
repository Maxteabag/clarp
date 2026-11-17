//! An agent forks itself into helpers that start from its conversation.
//!
//! One request makes a helper for each child it names, and sends each its own task from the
//! agent that forked: the child reports back like any helper. A child begins where its
//! parent is, not from a blank prompt: with the parent's conversation copied by the tool
//! itself where the tool can, and otherwise with the most recent part of that conversation
//! as words at the head of its first message.
//!
//! The rules are the Python Host's (`server/lib/agent_fork.py`), without a Host, a tool or a
//! disk: what a request asks for and why it is refused, the first message of a child, the
//! part of a conversation a child is given as words, and where a child's worktree is put
//! when the request does not say. Held to that module by `tests/fixtures/fork-vectors.json`,
//! which `server-rs/tools/fork-vectors.py` writes. A request that is malformed makes nothing.

use std::borrow::Cow;

use serde_json::{Map, Value};

use crate::python;

/// How many children one request may ask for.
pub const MOST: usize = 16;
/// How much of its parent's conversation a child is given as words, in signs.
pub const SEED_MOST: usize = 60_000;
/// How much of one thing that was said, in signs, before it is cut.
pub const SAID_MOST: usize = 4_000;
/// How much of what a tool was asked is shown beside its name.
const TOOL_MOST: usize = 160;
/// How long a child's name may be, in signs.
const NAME_MOST: usize = 64;

/// One child a request asks for.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Child {
    pub name: String,
    pub task: String,
    /// Which of the children of its request it is, counted from one, and how many there are.
    pub index: usize,
    pub total: usize,
    /// Where it works, when the request says. Empty: where its parent works.
    pub cwd: String,
    /// A git worktree of its own: the repository, the branch, what the branch starts from
    /// when it is new, and where the worktree is put. `repo` empty: no worktree.
    pub repo: String,
    pub branch: String,
    pub base: String,
    pub worktree_path: String,
    /// Its tool, model and effort, when the request names them. Empty: its parent's tool,
    /// and the tool's own choice.
    pub backend: String,
    pub model: String,
    pub effort: String,
}

/// Why a fork is not made, or one child of it: the status, a code, and words.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Refused {
    pub status: u16,
    /// One of this module's own words for the reason, or the word a Host has for a child
    /// it could not make.
    pub code: Cow<'static, str>,
    pub message: String,
}

impl Refused {
    pub fn new(
        status: u16,
        code: impl Into<Cow<'static, str>>,
        message: impl Into<String>,
    ) -> Self {
        Self {
            status,
            code: code.into(),
            message: message.into(),
        }
    }

    fn asked_wrongly(code: &'static str, message: impl Into<String>) -> Self {
        Self::new(400, code, message)
    }

    /// What an app or an agent's tool is answered.
    pub fn told(&self) -> Value {
        serde_json::json!({ "error": self.code, "message": self.message })
    }
}

/// A text a request gives under a key, without the blanks around it. Anything that is no
/// text is none.
fn text(given: Option<&Value>) -> String {
    given
        .and_then(Value::as_str)
        .map(python::strip)
        .unwrap_or_default()
        .to_string()
}

/// A number of children as Python's `int()` reads what a request gives: a whole number, a
/// number with a fraction cut toward nothing, yes as one and no as none, or a text that is
/// a whole number. `Err`: it is no number.
fn count_of(given: &Value) -> Result<i128, ()> {
    match given {
        Value::Bool(yes) => Ok(i128::from(*yes)),
        Value::Number(number) => match (number.as_i64(), number.as_u64(), number.as_f64()) {
            (Some(whole), _, _) => Ok(i128::from(whole)),
            (_, Some(whole), _) => Ok(i128::from(whole)),
            (_, _, Some(fraction)) if fraction.is_finite() => {
                // Larger than any count that is taken is all that has to be known of it.
                let cut = fraction.trunc().clamp(-1e30, 1e30);
                Ok(cut as i128)
            }
            _ => Err(()),
        },
        Value::String(written) => python::int(written).ok_or(()),
        _ => Err(()),
    }
}

/// The children a request asks for, or why it asks for none.
///
/// Either `children: [{name, task, cwd?, worktree?: {repo, branch, base?, path?}, backend?,
/// model?, effort?}]`, or `count` copies of one `task`, named `<name>-1` to `<name>-N`
/// (`name` is `fork` where none is given). `backend`, `model` and `effort` at the top are
/// for every child that names none of its own.
pub fn asked(request: &Value) -> Result<Vec<Child>, Refused> {
    let Some(request) = request.as_object() else {
        return Err(Refused::asked_wrongly("bad_json", "bad_json"));
    };
    let shared = |key: &str| text(request.get(key));
    let given = |key: &str| request.get(key).filter(|value| !value.is_null());
    let too_many =
        || Refused::asked_wrongly("too_many_children", format!("At most {MOST} children"));
    let mut named = given("children").cloned();
    if named.is_none()
        && let Some(count) = given("count")
    {
        let Ok(count) = count_of(count) else {
            return Err(Refused::asked_wrongly(
                "children_required",
                "count must be a number",
            ));
        };
        if count > MOST as i128 {
            return Err(too_many());
        }
        let base = match shared("name") {
            name if name.is_empty() => "fork".to_string(),
            name => name,
        };
        let tree = request.get("worktree");
        let copies = (1..=count.max(0)).map(|at| {
            let mut child = Map::new();
            child.insert("name".to_string(), Value::String(format!("{base}-{at}")));
            for key in ["task", "cwd"] {
                child.insert(
                    key.to_string(),
                    request.get(key).cloned().unwrap_or(Value::Null),
                );
            }
            // One branch for each: two worktrees cannot share one. And each is put where a
            // worktree of its branch goes, not where the request put the one.
            let own = match tree {
                Some(Value::Object(tree)) => {
                    let mut own = tree.clone();
                    let branch = text(own.get("branch"));
                    if !branch.is_empty() {
                        own.insert(
                            "branch".to_string(),
                            Value::String(format!("{branch}-{at}")),
                        );
                    }
                    own.remove("path");
                    Value::Object(own)
                }
                Some(other) => other.clone(),
                None => Value::Null,
            };
            child.insert("worktree".to_string(), own);
            Value::Object(child)
        });
        named = Some(Value::Array(copies.collect()));
    }
    let children = match &named {
        Some(Value::Array(children)) if !children.is_empty() => children,
        Some(other) if !other.is_array() => {
            return Err(Refused::asked_wrongly(
                "children_required",
                "children must be a list",
            ));
        }
        _ => {
            if given("count").is_some() && shared("task").is_empty() {
                return Err(Refused::asked_wrongly(
                    "task_required",
                    "Every child needs a task",
                ));
            }
            return Err(Refused::asked_wrongly(
                "children_required",
                "Give children or a count",
            ));
        }
    };
    if children.len() > MOST {
        return Err(too_many());
    }
    let mut taken: Vec<Child> = Vec::new();
    let mut seen: Vec<String> = Vec::new();
    for (at, child) in children.iter().enumerate() {
        let Some(child) = child.as_object() else {
            return Err(Refused::asked_wrongly(
                "children_required",
                "Each child must be an object",
            ));
        };
        let (name, task) = (text(child.get("name")), text(child.get("task")));
        if task.is_empty() {
            return Err(Refused::asked_wrongly(
                "task_required",
                "Every child needs a task",
            ));
        }
        if name.is_empty() {
            return Err(Refused::asked_wrongly(
                "name_required",
                "Every child needs a name",
            ));
        }
        if name.chars().count() > NAME_MOST {
            let begins = python::first(&name, NAME_MOST);
            return Err(Refused::asked_wrongly(
                "name_too_long",
                format!("Child name too long: {begins}…"),
            ));
        }
        // Two names that differ in their case alone are one name.
        let folded = name.to_lowercase();
        if seen.contains(&folded) {
            return Err(Refused::asked_wrongly(
                "duplicate_name",
                format!("Two children are named {name}"),
            ));
        }
        seen.push(folded);
        let mut of_its_own = Child {
            name: name.clone(),
            task,
            index: at + 1,
            total: children.len(),
            cwd: text(child.get("cwd")),
            ..Child::default()
        };
        match child.get("worktree") {
            None | Some(Value::Null) => {}
            Some(Value::Object(tree)) => {
                of_its_own.repo = text(tree.get("repo"));
                of_its_own.branch = text(tree.get("branch"));
                of_its_own.base = text(tree.get("base"));
                of_its_own.worktree_path = text(tree.get("path"));
                if of_its_own.repo.is_empty() {
                    return Err(Refused::asked_wrongly(
                        "worktree_repo_required",
                        format!("{name}: worktree needs a repo"),
                    ));
                }
                if of_its_own.branch.is_empty() {
                    return Err(Refused::asked_wrongly(
                        "worktree_branch_required",
                        format!("{name}: worktree needs a branch"),
                    ));
                }
            }
            Some(_) => {
                return Err(Refused::asked_wrongly(
                    "worktree_invalid",
                    "worktree must be {repo, branch}",
                ));
            }
        }
        let own_or_shared = |key: &str| match text(child.get(key)) {
            own if own.is_empty() => shared(key),
            own => own,
        };
        of_its_own.backend = own_or_shared("backend");
        of_its_own.model = own_or_shared("model");
        of_its_own.effort = own_or_shared("effort");
        taken.push(of_its_own);
    }
    Ok(taken)
}

/// Where a child's worktree is put when the request does not say: beside the repository, in
/// `<repository>-worktrees`, under the branch's name with everything that is no plain sign
/// of a file's name as a hyphen.
pub fn worktree_place(repo: &str, branch: &str) -> String {
    let mut slug = String::new();
    let mut in_other = false;
    for sign in branch.chars() {
        if sign.is_ascii_alphanumeric() || matches!(sign, '.' | '_' | '-') {
            in_other = false;
            slug.push(sign);
        } else if !in_other {
            in_other = true;
            slug.push('-');
        }
    }
    let slug = match slug.trim_matches(|sign| sign == '-' || sign == '.') {
        "" => "fork",
        slug => slug,
    };
    // The repository's path as its parts: the last is its name, what is before it its parent.
    let parts: Vec<&str> = repo.split('/').filter(|part| !part.is_empty()).collect();
    let (name, parent) = match parts.split_last() {
        Some((name, parent)) => (*name, parent.join("/")),
        None => ("", String::new()),
    };
    let root = if repo.starts_with('/') { "/" } else { "" };
    let beside = match parent.as_str() {
        "" => format!("{root}{name}-worktrees"),
        parent => format!("{root}{parent}/{name}-worktrees"),
    };
    format!("{beside}/{slug}")
}

/// One thing that was said in a conversation, for the part of it a child is given as words:
/// who said it, what was said, and the tools that were called with it, each with its name
/// and a line of what it was asked.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct SaidThing {
    pub by_the_user: bool,
    pub text: String,
    pub tools: Vec<(String, String)>,
}

/// The most recent part of a conversation as plain words, the oldest of it first: as much
/// as fits in what a child is given, counted from the newest thing that was said.
pub fn seed(said: &[SaidThing]) -> String {
    let mut blocks: Vec<String> = Vec::new();
    for thing in said {
        let who = if thing.by_the_user {
            "User"
        } else {
            "Assistant"
        };
        let mut words = python::strip(&thing.text).to_string();
        if words.chars().count() > SAID_MOST {
            words = format!("{} […]", python::first(&words, SAID_MOST));
        }
        let tools: Vec<String> = thing
            .tools
            .iter()
            .map(|(name, asked)| {
                let name = if name.is_empty() { "tool" } else { name };
                if asked.is_empty() {
                    format!("  → {name}")
                } else {
                    format!("  → {name}: {}", python::first(asked, TOOL_MOST))
                }
            })
            .collect();
        if words.is_empty() && tools.is_empty() {
            continue;
        }
        let head = if words.is_empty() {
            format!("{who}:")
        } else {
            format!("{who}: {words}")
        };
        let mut block = vec![head];
        block.extend(tools);
        blocks.push(block.join("\n"));
    }
    let mut kept: Vec<&str> = Vec::new();
    let mut size = 0;
    for block in blocks.iter().rev() {
        let weighs = block.chars().count() + 2;
        if size + weighs > SEED_MOST {
            break;
        }
        kept.push(block);
        size += weighs;
    }
    kept.reverse();
    kept.join("\n\n")
}

/// A child's first message: what it is, the part of its parent's conversation it starts from
/// where that came as words (`seed`), its task, and how to report back. `seed` is `None`
/// when the tool itself copied the conversation: it is then above the message already.
pub fn message(parent: &str, helper: &str, child: &Child, cwd: &str, seed: Option<&str>) -> String {
    let place = if child.branch.is_empty() {
        cwd.to_string()
    } else {
        format!("your own git worktree {cwd} on branch {}", child.branch)
    };
    let siblings = if child.total > 1 {
        format!(
            " You are child {} of {} forked together; the others work on their own tasks.",
            child.index, child.total
        )
    } else {
        String::new()
    };
    let name = &child.name;
    let intro = match seed {
        None => format!(
            "You are the Clarp helper agent `{helper}` ({name}), a fork of `{parent}`. The \
             conversation above is {parent}'s, up to the moment it forked you; any tool call it \
             was making then was the fork itself."
        ),
        Some(_) => format!(
            "You are the Clarp helper agent `{helper}` ({name}), a fork of `{parent}`. Below is \
             the most recent part of {parent}'s conversation, up to the moment it forked you. \
             Treat it as your own memory."
        ),
    };
    let mut parts = vec![format!(
        "{intro}{siblings} {parent} carries on with its own work: do not continue it, only the \
         task below. Work in {place} and stay there."
    )];
    if let Some(seed) = seed {
        let seed = if seed.is_empty() {
            "(no transcript was readable)"
        } else {
            seed
        };
        parts.push(format!(
            "--- {parent}'s conversation so far ---\n{seed}\n--- end ---"
        ));
    }
    parts.push(format!("Your task:\n{}", child.task));
    parts.push(format!(
        "When you are finished (or blocked), send your final report to your parent in one \
         message:\n  clarp-admin prompt --to {parent} --from {helper} --text \"<report>\"\nThat \
         message marks you reported. Do not message your parent for routine progress."
    ));
    parts.join("\n\n")
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    fn said(by_the_user: bool, text: &str, tools: &[(&str, &str)]) -> SaidThing {
        SaidThing {
            by_the_user,
            text: text.to_string(),
            tools: tools
                .iter()
                .map(|(name, asked)| (name.to_string(), asked.to_string()))
                .collect(),
        }
    }

    #[test]
    fn a_child_is_given_the_newest_part_of_its_parents_conversation_as_words() {
        let conversation = [
            said(true, "  do the thing  ", &[]),
            said(
                false,
                "on it",
                &[("Bash", "ls"), ("", ""), ("Read", &"x".repeat(200))],
            ),
            said(false, "", &[]),
            said(false, "", &[("Edit", "a file")]),
        ];
        let words = seed(&conversation);
        let long = "x".repeat(TOOL_MOST);
        assert_eq!(
            words,
            format!(
                "User: do the thing\n\nAssistant: on it\n  → Bash: ls\n  → tool\n  → Read: {long}\n\n\
                 Assistant:\n  → Edit: a file"
            )
        );
        // What was said at length is cut, and says so.
        let lengthy = [said(true, &"y".repeat(SAID_MOST + 1), &[])];
        assert_eq!(
            seed(&lengthy),
            format!("User: {} […]", "y".repeat(SAID_MOST))
        );
        // Only as much as a child is given, counted from the newest; a thing that does not
        // fit whole is left out with everything before it.
        let many: Vec<SaidThing> = (0..40)
            .map(|at| said(at % 2 == 0, &format!("{at:02}{}", "z".repeat(3_000)), &[]))
            .collect();
        let words = seed(&many);
        assert!(words.chars().count() <= SEED_MOST);
        let kept: Vec<&str> = words.split("\n\n").collect();
        assert!(kept.last().unwrap().contains("39zzz"));
        // Each weighs its words, its speaker and the line between: nineteen fit, not twenty.
        assert_eq!(kept.len(), 19, "{}", kept.len());
        assert!(kept[0].contains(": 21zzz"));
        assert_eq!(seed(&[]), "");
    }
}
