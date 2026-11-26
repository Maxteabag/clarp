//! Helpers: agents that another agent made to do one piece of work.
//!
//! A helper has a parent, the agent that made it, and its work has a state:
//!
//! - `running`: the helper is working;
//! - `reported`: its report reached the parent, or it sat idle so long that it waits for
//!   the parent;
//! - `ended-unreported`: its turn ended without a report, and the parent can review it;
//! - `done`: the parent or the user accepted the result;
//! - `failed`: the work ended in a failure nothing follows;
//! - `abandoned`: the parent was deleted or put away before the work was done.
//!
//! [`next_state`] is the whole table of what moves a helper where; an event the table does
//! not have for a state changes nothing. Who may be whose parent, and which finished helpers
//! are put away, is decided here too. These are the Python Host's rules
//! (`server/lib/policies/helper_state.py`).

/// The roles an agent can be made with. A janitor is made another way.
pub const CREATABLE_ROLES: [&str; 2] = ["agent", "helper"];
/// The states nothing follows by itself.
pub const FINISHED: [&str; 3] = ["done", "failed", "abandoned"];
/// How long a helper that is done stays in the list before it is put away.
pub const DEFAULT_ARCHIVE_GRACE_MS: i64 = 24 * 60 * 60 * 1_000;

/// Where an event moves a helper.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Moved {
    pub state: &'static str,
    /// The work is over: the moment is kept. Otherwise it is forgotten.
    pub completed: bool,
}

/// The state `event` moves a helper in `current` to, or `None` when it changes nothing: an
/// agent that is no helper, a second report, a helper that is done losing its parent.
pub fn next_state(current: &str, event: &str) -> Option<Moved> {
    let state = match (current, event) {
        // The helper wrote to its parent.
        ("running" | "ended-unreported", "reported") => "reported",
        // The parent sent it more work.
        ("reported" | "ended-unreported", "parent_prompted") => "running",
        // The parent or the user accepted it.
        ("running" | "reported" | "ended-unreported" | "failed" | "abandoned", "marked_done") => {
            "done"
        }
        // The parent or the user opened it again.
        ("reported" | "ended-unreported" | "done" | "failed" | "abandoned", "marked_running") => {
            "running"
        }
        ("running" | "reported" | "ended-unreported", "failed") => "failed",
        // The parent was deleted or put away.
        ("running" | "reported" | "ended-unreported", "parent_gone") => "abandoned",
        ("running", "ended_unreported") => "ended-unreported",
        // It sat idle past the limit: reported, not done or failed. What came of it is
        // still the parent's to say.
        ("running", "went_idle") => "reported",
        _ => return None,
    };
    Some(Moved {
        state,
        completed: FINISHED.contains(&state),
    })
}

/// The state a helper is shown in, where the stored one is `stored` and `in_turn` says whether
/// a turn of its runs now. A helper that reported and is still in a turn reads `running`: a
/// progress message to its parent marks it reported while it goes on working, and a helper
/// shown as finished then drops out of view. Once that turn is over with none begun after it,
/// it reads `reported` again. Every other state is shown as stored: `done` is the parent's
/// acceptance and stays `done` while the helper works on. The stored state and its moves
/// ([`next_state`]) are not changed by this.
pub fn shown_state(stored: Option<&str>, in_turn: bool) -> Option<&str> {
    match stored {
        Some("reported") if in_turn => Some("running"),
        other => other,
    }
}

/// An agent as [`above_work`] reads the fleet: who it is, who made it, and whether it is a
/// helper at work that is not put away.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Line<'a> {
    pub agent_id: &'a str,
    pub parent: Option<&'a str>,
    pub works: bool,
}

/// The agents with a helper at work below them, at any depth: every maker up the line of each
/// agent in `lines` that works (issue 936). A maker is walked through once, so a walk ends at
/// one already counted, and a line that turns back on itself ends too.
pub fn above_work<'a>(lines: &[Line<'a>]) -> std::collections::HashSet<&'a str> {
    let parents: std::collections::HashMap<&str, &str> = lines
        .iter()
        .filter_map(|line| Some((line.agent_id, line.parent?)))
        .collect();
    let mut above = std::collections::HashSet::new();
    for line in lines.iter().filter(|line| line.works) {
        let mut maker = line.parent;
        while let Some(id) = maker
            && above.insert(id)
        {
            maker = parents.get(id).copied();
        }
    }
    above
}

/// The state a helper is shown in where `below_works` says whether a helper below it, at any
/// depth, works ([`above_work`]): `running` while one does, whatever it says itself, and what
/// it says itself ([`shown_state`]) once the last of them stops. Its own state is not changed
/// by this; an agent that is no helper has none to show.
pub fn shown_above(own: Option<&str>, below_works: bool) -> Option<&str> {
    match own {
        Some(_) if below_works => Some("running"),
        other => other,
    }
}

/// The event that setting a state by hand means, for the states that may be set by hand.
pub fn marked(state: &str) -> Option<&'static str> {
    match state {
        "done" => Some("marked_done"),
        "running" => Some("marked_running"),
        "failed" => Some("failed"),
        _ => None,
    }
}

/// The state an agent of this role starts in: a helper is working, anything else has none.
pub fn initial_state(role: &str) -> Option<&'static str> {
    (role == "helper").then_some("running")
}

/// A role an agent may be made with, from what a request said. Nothing said is `agent`.
pub fn parse_role(said: &str) -> Option<&'static str> {
    match said.trim().to_lowercase().as_str() {
        "" | "agent" => Some("agent"),
        "helper" => Some("helper"),
        _ => None,
    }
}

/// Why `parent_id` cannot be the parent of `child_id`, or nothing when it can.
/// `parent_ancestors` is the parent's own line upward. An agent that is being made has no
/// id yet: `child_id` is empty.
pub fn parent_refusal(
    child_id: &str,
    parent_id: &str,
    parent_ancestors: &[&str],
) -> Option<&'static str> {
    if parent_id.is_empty() || child_id.is_empty() {
        return None;
    }
    if parent_id == child_id {
        return Some("self_parent");
    }
    parent_ancestors
        .contains(&child_id)
        .then_some("parent_cycle")
}

/// A helper as the rule for putting helpers away looks at it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Kept<'a> {
    pub agent_id: &'a str,
    pub role: &'a str,
    pub helper_state: &'a str,
    pub archived: bool,
    pub helper_completed_at: Option<i64>,
}

/// The helpers that are done for longer than `grace_ms` and not put away yet.
pub fn archive_due<'a>(rows: &[Kept<'a>], now_ms: i64, grace_ms: i64) -> Vec<&'a str> {
    let cutoff = now_ms - grace_ms.max(0);
    rows.iter()
        .filter(|row| {
            row.role == "helper"
                && row.helper_state == "done"
                && !row.archived
                && row.helper_completed_at.unwrap_or(0) <= cutoff
        })
        .map(|row| row.agent_id)
        .collect()
}

// ---- a helper that went quiet ----------------------------------------------------------------

/// How long a helper that is working may sit idle before it counts as having reported.
pub const IDLE_AFTER_MS: i64 = 30 * 60 * 1_000;

/// What an agent is doing right now, as the list of agents counts it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Doing {
    /// A turn runs or waits.
    pub busy: bool,
    /// Background processes it left running, not the jobs that only mirror a helper.
    pub active_jobs: usize,
    /// Helpers of its own that are working.
    pub running_children: usize,
    /// The last time anything of it changed: a state, the end of a turn, a job.
    pub idle_since_ms: i64,
}

impl Doing {
    pub fn idle(&self) -> bool {
        !self.busy && self.active_jobs == 0 && self.running_children == 0
    }
}

/// Whether a helper that is working has sat idle past `limit_ms`. It then counts as having
/// reported, not as done or failed: what came of it is still its parent's to say. A limit
/// of nothing turns the rule off.
pub fn idle_due(helper_state: Option<&str>, doing: &Doing, now_ms: i64, limit_ms: i64) -> bool {
    limit_ms > 0
        && helper_state == Some("running")
        && doing.idle()
        && now_ms - doing.idle_since_ms > limit_ms
}

// ---- a label that outlived the work behind it ---------------------------------------------

/// How long an idle agent's own label stands: two hours.
pub const STATUS_TTL_MS: i64 = 2 * 3600 * 1_000;

/// Whether an idle agent's own label is older than `ttl_ms` and is taken back. A label a
/// janitor keeps has its own time, and one with no time is not guessed at. A limit of
/// nothing turns the rule off. The Python Host's `custom_status_due`.
pub fn status_due(
    status: &str,
    status_at: Option<i64>,
    janitor_owned: bool,
    doing: &Doing,
    now_ms: i64,
    ttl_ms: i64,
) -> bool {
    let Some(status_at) = status_at else {
        return false;
    };
    ttl_ms > 0
        && !crate::python::strip(status).is_empty()
        && !janitor_owned
        && doing.idle()
        && now_ms - status_at > ttl_ms
}

// ---- what an agent's row says of the work it handed out -----------------------------------

/// How the state of a helper that is working reads in its parent's line.
pub fn bucket(state: &str) -> &'static str {
    match state {
        "thinking" | "tool" | "compacting" | "background" => "working",
        "waiting" | "interrupted" => "waiting",
        _ => "idle",
    }
}

/// A helper that is working, as its parent's line describes it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Working {
    pub session: String,
    /// What it says it is doing, or what its state says.
    pub text: String,
    pub bucket: &'static str,
}

/// A background process an agent left running.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Process {
    pub title: String,
    /// The last line of progress it reported.
    pub progress: String,
    /// When it last reported progress, or when it started.
    pub active_at_ms: i64,
}

/// What an agent's helpers and processes are doing, in a line. The counts are on the row's
/// badge already, so the line says what goes on: one helper by its name and what it does,
/// several as "3 working, 1 waiting", a process by its newest line of progress or its title.
/// A bare count is the last resort.
pub fn work_summary(helpers: &[Working], processes: &[Process]) -> String {
    let mut parts: Vec<String> = Vec::new();
    match helpers {
        [] => {}
        [only] if only.text.is_empty() => parts.push(only.session.clone()),
        [only] => parts.push(format!("{}: {}", only.session, only.text)),
        several => {
            let counted: Vec<String> = ["working", "waiting", "idle"]
                .iter()
                .filter_map(|bucket| {
                    let count = several
                        .iter()
                        .filter(|helper| helper.bucket == *bucket)
                        .count();
                    (count > 0).then(|| format!("{count} {bucket}"))
                })
                .collect();
            parts.push(counted.join(", "));
        }
    }
    // The newest progress stands for all; of two as new, the one that started later.
    let newest = processes
        .iter()
        .enumerate()
        .max_by_key(|(place, process)| (process.active_at_ms, *place))
        .map(|(_, process)| process);
    if let Some(newest) = newest {
        let (title, text) = (newest.title.trim(), newest.progress.trim());
        let described = match (title.is_empty(), text.is_empty()) {
            (false, false) => format!("{title}: {text}"),
            (true, false) => text.to_string(),
            (false, true) => title.to_string(),
            (true, true) if processes.len() == 1 => "1 background process".to_string(),
            (true, true) => format!("{} background processes", processes.len()),
        };
        parts.push(described);
    }
    parts.join(" \u{b7} ")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn doing(busy: bool, active_jobs: usize, running_children: usize, idle_since_ms: i64) -> Doing {
        Doing {
            busy,
            active_jobs,
            running_children,
            idle_since_ms,
        }
    }

    #[test]
    fn a_helper_that_reported_reads_running_while_its_turn_runs() {
        assert_eq!(shown_state(Some("reported"), true), Some("running"));
        assert_eq!(shown_state(Some("reported"), false), Some("reported"));
        assert_eq!(shown_state(Some("done"), true), Some("done"));
        assert_eq!(
            shown_state(Some("ended-unreported"), true),
            Some("ended-unreported")
        );
        assert_eq!(shown_state(Some("running"), false), Some("running"));
        assert_eq!(shown_state(None, true), None);
    }

    #[test]
    fn a_helper_that_sits_idle_too_long_counts_as_reported() {
        let limit = IDLE_AFTER_MS;
        let quiet = doing(false, 0, 0, 1_000);
        assert!(!idle_due(Some("running"), &quiet, 1_000 + limit, limit));
        assert!(idle_due(Some("running"), &quiet, 1_001 + limit, limit));
        // Only a helper that is working, and only one with nothing going on.
        assert!(!idle_due(Some("reported"), &quiet, 1_001 + limit, limit));
        assert!(!idle_due(None, &quiet, 1_001 + limit, limit));
        for alive in [
            doing(true, 0, 0, 1_000),
            doing(false, 1, 0, 1_000),
            doing(false, 0, 1, 1_000),
        ] {
            assert!(!idle_due(Some("running"), &alive, 1_001 + limit, limit));
        }
        assert!(
            !idle_due(Some("running"), &quiet, i64::MAX / 2, 0),
            "the rule is off"
        );
    }

    fn working(session: &str, text: &str, state: &str) -> Working {
        Working {
            session: session.to_string(),
            text: text.to_string(),
            bucket: bucket(state),
        }
    }

    fn process(title: &str, progress: &str, active_at_ms: i64) -> Process {
        Process {
            title: title.to_string(),
            progress: progress.to_string(),
            active_at_ms,
        }
    }

    #[test]
    fn a_line_says_what_the_helpers_and_processes_are_doing() {
        assert_eq!(work_summary(&[], &[]), "");
        assert_eq!(
            work_summary(&[working("slice5-helper", "running tests", "tool")], &[]),
            "slice5-helper: running tests"
        );
        assert_eq!(
            work_summary(&[working("slice5-helper", "", "spawned")], &[]),
            "slice5-helper"
        );
        let several = [
            working("a", "x", "thinking"),
            working("b", "", "tool"),
            working("c", "", "waiting"),
            working("d", "", "done"),
            working("e", "", "background"),
        ];
        assert_eq!(work_summary(&several, &[]), "3 working, 1 waiting, 1 idle");
        assert_eq!(
            work_summary(&several[..2], &[]),
            "2 working",
            "what nobody is doing is not counted"
        );
        // A process by its newest progress; of two as new, the later one.
        let processes = [
            process("Build", "linking", 30),
            process("Tests", "412 passed", 40),
            process("Deploy", "", 40),
        ];
        assert_eq!(work_summary(&[], &processes[..2]), "Tests: 412 passed");
        assert_eq!(work_summary(&[], &processes), "Deploy");
        assert_eq!(work_summary(&[], &[process("", "half way", 1)]), "half way");
        assert_eq!(
            work_summary(&[], &[process("", "", 1)]),
            "1 background process"
        );
        assert_eq!(
            work_summary(&[], &[process("", "", 1), process(" ", " ", 1)]),
            "2 background processes"
        );
        assert_eq!(
            work_summary(&[working("h", "reading", "tool")], &processes[..1]),
            "h: reading \u{b7} Build: linking"
        );
    }

    #[test]
    fn every_maker_above_a_helper_at_work_is_counted_and_a_loop_ends() {
        let line = |agent_id, parent, works| Line {
            agent_id,
            parent,
            works,
        };
        // root <- middle <- leaf at work; a second helper of root that does not work.
        let fleet = [
            line("root", None, false),
            line("middle", Some("root"), false),
            line("leaf", Some("middle"), true),
            line("idle", Some("root"), false),
        ];
        let mut above: Vec<_> = above_work(&fleet).into_iter().collect();
        above.sort();
        assert_eq!(above, ["middle", "root"]);
        let resting = [line("leaf", Some("middle"), false), fleet[1], fleet[0]];
        assert!(above_work(&resting).is_empty());
        // Makers that name each other, and one that names itself, still end.
        let looped = [
            line("a", Some("b"), false),
            line("b", Some("a"), false),
            line("c", Some("a"), true),
            line("d", Some("d"), true),
        ];
        let mut above: Vec<_> = above_work(&looped).into_iter().collect();
        above.sort();
        assert_eq!(above, ["a", "b", "d"]);
        // Shown working while one below works, its own state again after.
        assert_eq!(shown_above(Some("done"), true), Some("running"));
        assert_eq!(shown_above(Some("done"), false), Some("done"));
        assert_eq!(
            shown_above(None, true),
            None,
            "no helper has no state to show"
        );
    }
}
