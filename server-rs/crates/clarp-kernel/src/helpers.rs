//! Helpers: agents that another agent made to do one piece of work.
//!
//! A helper has a parent, the agent that made it, and its work has a state:
//!
//! - `running`: the helper is working;
//! - `reported`: its report reached the parent, or it sat idle so long that it waits for
//!   the parent;
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
        ("running", "reported") => "reported",
        // The parent sent it more work.
        ("reported", "parent_prompted") => "running",
        // The parent or the user accepted it.
        ("running" | "reported" | "failed" | "abandoned", "marked_done") => "done",
        // The parent or the user opened it again.
        ("reported" | "done" | "failed" | "abandoned", "marked_running") => "running",
        ("running" | "reported", "failed") => "failed",
        // The parent was deleted or put away.
        ("running" | "reported", "parent_gone") => "abandoned",
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
