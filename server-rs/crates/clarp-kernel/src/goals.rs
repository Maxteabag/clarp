//! Goals: an agent's durable commitments to an outcome, and the plans it works them by.
//!
//! A goal says what is to be achieved, what counts as achieved (its criteria, each with the
//! evidence for it), and within which limits. Its plan is the agent's current way there:
//! steps that are taken up, finished, set aside, replaced. A checkpoint records where the
//! work stands and what is next, and names how the work goes on: at a time, when something
//! awaited ends, or not until someone unblocks it. Notes that belong to the goal are kept
//! with it as versioned documents.
//!
//! These are the rules of the Python Host's `server/lib/task_plans.py`, rule for rule and
//! word for word in what they refuse: agents and their skills read those words. The two are
//! held together by `clarp-host/tests/fixtures/goal-vectors.json`, which
//! `server-rs/tools/goal-vectors.py` writes from the Python tool. What a goal is given and
//! what its continuation holds come and go with what happened to it, which is why those are
//! kept as JSON here and not as fields.
//!
//! Nothing here stores anything. A method of [`GoalBook`] that changes something answers
//! with the [`Change`]; [`GoalBook::record`] says what the log keeps of it, and
//! [`GoalBook::apply`] takes what the log has.

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};

use crate::jobs::parse_handle;

pub mod bookkeeping;
pub mod delegation;
pub mod keeper;
pub mod ledger;
pub mod listener;
pub mod subgoals;
mod wake;

pub use wake::{Looked, Seen, TurnSeen, WAKE_PREFIX};

pub const MAX_PLAN_ITEMS: usize = 500;
/// How much of its history a goal carries along. Older entries are counted, not carried:
/// the log has them all.
pub const GOAL_HISTORY_LIMIT: usize = 200;
const ITEM_STATUSES: [&str; 7] = [
    "pending",
    "in_progress",
    "completed",
    "blocked",
    "skipped",
    "deferred",
    "removed",
];
const TERMINAL_ITEM_STATUSES: [&str; 4] = ["completed", "skipped", "deferred", "removed"];
const GOAL_ACTIONS: [&str; 14] = [
    "add_step",
    "block",
    "cancel",
    "checkpoint",
    "dependency",
    "document",
    "enroll",
    "pause",
    "rebind",
    "replan",
    "resume",
    "supersede",
    "unwaive",
    "waive",
];

/// A command that is refused, in the words the tool prints.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Refused(pub String);

impl std::fmt::Display for Refused {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for Refused {}

fn refuse<T>(why: &str) -> Result<T, Refused> {
    Err(Refused(why.to_string()))
}

/// The statuses the Goal keeper may give a step, and the ones it leaves alone: a step its
/// owner closed stays closed.
const KEEPER_STEP_STATUSES: [&str; 4] = ["pending", "in_progress", "completed", "blocked"];
const KEPT_CLOSED: [&str; 4] = ["completed", "skipped", "deferred", "removed"];

/// What the Goal keeper writes of a goal ([`GoalBook::updater`]).
#[derive(Debug, Clone)]
pub struct Update<'a> {
    /// The plan's revision as the keeper read it.
    pub seen_revision: i64,
    /// The steps to move, each by its whole id: its status, and a note.
    pub steps: &'a [(String, String, String)],
    pub progress: &'a str,
    pub next_work: &'a str,
    pub model: &'a str,
    /// What the update was made from: rows, jobs, an event.
    pub source_refs: &'a [String],
    /// Where the next update reads from.
    pub through_ms: i64,
    pub through_event: i64,
    pub janitor_agent_id: &'a str,
}

/// Why the keeper's write was not made: by its kind, never by its words.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum NotUpdated {
    /// The goal is neither active nor paused any more.
    Closed,
    /// The owner changed the plan since the keeper read it.
    OwnerAhead,
    /// What was to be written is no update.
    Invalid(&'static str),
}

impl NotUpdated {
    /// The words the Python Host refuses with.
    pub fn said(self) -> &'static str {
        match self {
            Self::Closed => "goal is closed",
            Self::OwnerAhead => "plan changed; reload before editing",
            Self::Invalid(why) => why,
        }
    }
}

/// A step of a plan.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Item {
    pub required: i64,
    pub item_id: String,
    pub plan_id: String,
    pub parent_id: Option<String>,
    pub position: i64,
    pub title: String,
    pub detail: String,
    pub status: String,
    pub created_at: i64,
    pub started_at: Option<i64>,
    pub completed_at: Option<i64>,
    pub active_ms: i64,
}

impl Item {
    fn row(&self) -> Value {
        serde_json::to_value(self).unwrap_or(Value::Null)
    }
}

/// One version of a document that belongs to a goal.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Document {
    pub plan_id: String,
    pub name: String,
    pub revision: i64,
    pub format: String,
    pub content: Value,
    pub reason: String,
    pub updated_at: i64,
}

/// A goal: what it was given, how its work goes on, and the newest of what happened to it.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct Goal {
    /// The outcome, the limits, the criteria with their evidence, the last checkpoint, and
    /// whatever else the goal carries.
    pub fields: Map<String, Value>,
    /// How the work goes on: the state, the reason, when it is due, what it waits for.
    pub continuation: Map<String, Value>,
    /// The newest entries of its history, oldest first.
    pub history: Vec<Value>,
    /// How many older entries there were.
    pub history_offset: u64,
}

impl Goal {
    fn bound(&mut self) {
        if self.history.len() > GOAL_HISTORY_LIMIT {
            let excess = self.history.len() - GOAL_HISTORY_LIMIT;
            self.history.drain(..excess);
            self.history_offset += excess as u64;
        }
    }

    fn state(&self) -> &str {
        self.continuation
            .get("state")
            .and_then(Value::as_str)
            .unwrap_or("")
    }

    fn text(&self, field: &str) -> String {
        self.fields.get(field).map(text).unwrap_or_default()
    }

    /// The goal as it is read: one document.
    pub fn document(&self) -> Value {
        let mut whole = self.fields.clone();
        whole.insert("history".to_string(), json!(self.history));
        whole.insert("continuation".to_string(), json!(self.continuation));
        if self.history_offset > 0 {
            whole.insert("history_offset".to_string(), json!(self.history_offset));
        }
        Value::Object(whole)
    }
}

/// A plan as it stands: its own fields, its goal if it has one, and its steps.
#[derive(Debug, Clone, PartialEq)]
pub struct Plan {
    pub plan_id: String,
    pub agent_id: String,
    pub session: String,
    pub title: String,
    pub status: String,
    pub created_at: i64,
    pub updated_at: i64,
    pub completed_at: Option<i64>,
    pub revision: i64,
    pub recovery_enabled: i64,
    /// `None` for a plan that is only a list of steps.
    pub goal: Option<Goal>,
    pub subgoals: std::sync::Arc<Vec<Value>>,
    /// What happened to a plan without a goal.
    pub history: Vec<Value>,
    /// The steps, in the order they were made.
    pub items: Vec<Item>,
    /// How many entries the goal's ledger has, and when the last was made.
    pub event_count: i64,
    pub last_event_at: Option<i64>,
    /// The order plans were made in: it tells apart two that changed in the same moment.
    pub order: u64,
}

/// What a command changed: the plan as it now stands, and the document versions it added.
#[derive(Debug, Clone, PartialEq)]
pub struct Change {
    pub plan: Plan,
    pub documents: Vec<Document>,
}

/// How a history went on from what it was.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Grown {
    /// These entries were added at its end.
    By(Vec<Value>),
    /// It is this now, with so many older entries before it: it did not only grow.
    To(Vec<Value>, u64),
}

impl Default for Grown {
    fn default() -> Self {
        Self::By(Vec::new())
    }
}

impl Grown {
    fn is_nothing(&self) -> bool {
        matches!(self, Self::By(added) if added.is_empty())
    }

    /// How `new` came from `old`, when neither carries more than `limit` entries.
    fn between(old: (&[Value], u64), new: (&[Value], u64), limit: usize) -> Self {
        let before = old.1 + old.0.len() as u64;
        let after = new.1 + new.0.len() as u64;
        if let Some(added) = after.checked_sub(before)
            && let Ok(added) = usize::try_from(added)
            && added <= new.0.len()
        {
            let grown = Self::By(new.0[new.0.len() - added..].to_vec());
            let mut entries = old.0.to_vec();
            let mut offset = old.1;
            grown.onto(&mut entries, &mut offset, limit);
            if entries == new.0 && offset == new.1 {
                return grown;
            }
        }
        Self::To(new.0.to_vec(), new.1)
    }

    fn onto(&self, entries: &mut Vec<Value>, offset: &mut u64, limit: usize) {
        match self {
            Self::By(added) => {
                entries.extend(added.iter().cloned());
                if entries.len() > limit {
                    let excess = entries.len() - limit;
                    entries.drain(..excess);
                    *offset += excess as u64;
                }
            }
            Self::To(whole, before) => {
                entries.clone_from(whole);
                *offset = *before;
            }
        }
    }
}

/// What the log keeps of one change to a plan: the plan's own fields as the change left
/// them, and of everything that grows only what the change set or added. A goal with a long
/// history is not written again for every step.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct GoalRecord {
    /// New accounting summaries in event order. An initial empty list also
    /// distinguishes a known-empty plan from an import made before this field.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub accounting: Option<Vec<Value>>,
    pub at_ms: i64,
    pub plan_id: String,
    pub agent_id: String,
    pub session: String,
    pub title: String,
    pub status: String,
    pub created_at: i64,
    pub updated_at: i64,
    pub completed_at: Option<i64>,
    pub revision: i64,
    pub recovery_enabled: i64,
    pub event_count: i64,
    pub last_event_at: Option<i64>,
    /// Subgoal rows this write added or replaced.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub subgoals: Vec<Value>,
    /// Whether the plan has a goal.
    pub goal: bool,
    /// The goal's fields the change set.
    #[serde(default, skip_serializing_if = "Map::is_empty")]
    pub set: Map<String, Value>,
    /// The goal's fields the change took away.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub unset: Vec<String>,
    /// The goal's continuation, when the change touched it.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub continuation: Option<Map<String, Value>>,
    /// How the goal's history went on.
    #[serde(default, skip_serializing_if = "Grown::is_nothing")]
    pub history: Grown,
    /// How the history of a plan without a goal went on.
    #[serde(default, skip_serializing_if = "Grown::is_nothing")]
    pub plan_history: Grown,
    /// The steps the change made or changed, each whole.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub items: Vec<Item>,
    /// The document versions the change added.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub documents: Vec<Document>,
}

impl GoalRecord {
    /// A legacy keeper had counted history entries in this goal. Its surviving
    /// history is the baseline in the new ledger. An incoming ledger already
    /// uses event IDs, so preserve outstanding events and cap at its own tail.
    pub fn import_keeper_cursor(
        &mut self,
        plan: &Plan,
        source: Option<&[ledger::Event]>,
        baseline: i64,
    ) {
        let Some(updater) = plan
            .goal
            .as_ref()
            .and_then(|goal| goal.fields.get("updater"))
            .and_then(Value::as_object)
        else {
            return;
        };
        if !updater.get("through_ms").is_some_and(crate::python::truthy) {
            return;
        }
        let mut updater = updater.clone();
        let through = match source {
            None => baseline,
            Some(events) => updater
                .get("through_event")
                .and_then(Value::as_i64)
                .unwrap_or(0)
                .clamp(0, events.last().map_or(0, |event| event.event_id)),
        };
        updater.insert("through_event".into(), json!(through));
        self.set.insert("updater".into(), Value::Object(updater));
    }
}

/// Whose plan it is: the agent, and the name its session has.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Owner<'a> {
    pub agent_id: &'a str,
    pub session: &'a str,
}

/// What a command needs to know of the world around the goals.
pub struct World<'a> {
    pub now_ms: i64,
    /// The owner's conversation with its tool as it is bound right now, or empty.
    pub native_session: &'a str,
    /// The revision of the owner's queue.
    pub queue_revision: u64,
    /// A new piece of an id: this many bytes, in hex.
    pub token_hex: &'a mut dyn FnMut(usize) -> String,
    /// Whether the owner has a background job with this id in this generation.
    pub has_job: &'a dyn Fn(&str, u64) -> bool,
}

/// The goals and plans of one agent.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct GoalBook {
    // Bounded projections, shared by readers of the immutable agent view.
    accounting: BTreeMap<String, std::sync::Arc<std::collections::VecDeque<Value>>>,
    plans: BTreeMap<String, Plan>,
    /// Every version of every document, oldest first.
    documents: Vec<Document>,
    made: u64,
}

fn text(value: &Value) -> String {
    match value {
        Value::String(text) => text.clone(),
        Value::Null => String::new(),
        other => other.to_string(),
    }
}

/// What Python takes for true: nothing empty, no zero, no null.
fn is_true(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(flag) => *flag,
        Value::String(text) => !text.is_empty(),
        Value::Number(number) => number.as_f64() != Some(0.0),
        Value::Array(items) => !items.is_empty(),
        Value::Object(fields) => !fields.is_empty(),
    }
}

fn truthy(value: Option<&Value>) -> bool {
    value.is_some_and(is_true)
}

/// A value as the text it is, where what is not true is no text.
fn plain(value: Option<&Value>) -> String {
    match value {
        Some(value) if is_true(value) => text(value),
        _ => String::new(),
    }
}

fn stripped(value: Option<&Value>) -> String {
    plain(value).trim().to_string()
}

fn clip(value: &str, max: usize) -> String {
    value.chars().take(max).collect()
}

fn sha256_hex(text: &str) -> String {
    Sha256::digest(text.as_bytes())
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

/// A name of at most 60 characters: a longer one keeps its beginning and gets a mark of the
/// whole.
fn bounded_key(key: &str) -> String {
    if key.chars().count() > 60 {
        format!("{}-{}", clip(key, 48), &sha256_hex(key)[..10])
    } else {
        key.to_string()
    }
}

/// The id of a step: the plan's, and the name the step was given.
pub fn item_key(
    plan_id: &str,
    stable_id: &str,
    token_hex: &mut dyn FnMut(usize) -> String,
) -> String {
    let clean = match stable_id.trim() {
        "" => format!("item-{}", token_hex(6)),
        clean => clean.to_string(),
    };
    format!("{plan_id}:{}", bounded_key(&clean))
}

fn new_item(
    plan_id: &str,
    item_id: String,
    parent_id: Option<String>,
    position: usize,
    raw: &Map<String, Value>,
    now: i64,
) -> Result<Item, Refused> {
    let title = stripped(raw.get("title"));
    if title.is_empty() {
        return refuse("task title required");
    }
    let required = match raw.get("required") {
        None => 1,
        Some(Value::Bool(flag)) => i64::from(*flag),
        Some(Value::Number(number)) => number.as_i64().unwrap_or(1),
        Some(Value::String(given)) => given
            .trim()
            .parse()
            .map_err(|_| Refused(format!("invalid literal for int() with base 10: '{given}'")))?,
        Some(_) => return refuse("required must be a number or a boolean"),
    };
    Ok(Item {
        required,
        item_id,
        plan_id: plan_id.to_string(),
        parent_id,
        position: position as i64,
        title: clip(&title, 500),
        detail: clip(&plain(raw.get("detail")), 2_000),
        status: "pending".to_string(),
        created_at: now,
        started_at: None,
        completed_at: None,
        active_ms: 0,
    })
}

fn set(state: &mut Map<String, Value>, fields: Value) {
    if let Value::Object(fields) = fields {
        for (key, value) in fields {
            state.insert(key, value);
        }
    }
}

fn generation(state: &Map<String, Value>) -> i64 {
    state.get("generation").and_then(Value::as_i64).unwrap_or(0)
}

fn check_revision(
    plan: &Plan,
    revision: Option<i64>,
    next_command: impl FnOnce() -> String,
) -> Result<(), Refused> {
    match revision {
        None if plan.goal.is_some() => Err(Refused(format!(
            "revision required for durable goal {} (now revision {}): run `clarp-goal {}`",
            plan.plan_id,
            plan.revision,
            next_command()
        ))),
        Some(revision) if revision != plan.revision => {
            refuse("plan changed; reload before editing")
        }
        _ => Ok(()),
    }
}

/// Adds an entry to the goal's history, and counts it in the plan's ledger.
fn remember(plan: &mut Plan, goal: &mut Goal, kind: &str, revision: i64, detail: Value, now: i64) {
    plan.event_count += 1;
    plan.last_event_at = Some(now);
    goal.history
        .push(json!({ "at": now, "kind": kind, "revision": revision, "detail": detail }));
    goal.bound();
}

/// A plan's own record of something that happened to it: a step, an end.
fn note(plan: &mut Plan, kind: &str, detail: Value, now: i64) {
    let revision = plan.revision + 1;
    let Some(mut goal) = plan.goal.take() else {
        plan.history
            .push(json!({ "at": now, "kind": kind, "revision": revision, "detail": detail }));
        return;
    };
    if matches!(kind, "completed" | "cancelled" | "blocked") {
        let reason = match plain(detail.get("reason")) {
            given if !given.is_empty() => given,
            _ if kind == "completed" => "Completion criteria have recorded evidence".to_string(),
            _ => format!("Goal {kind}"),
        };
        let next = generation(&goal.continuation) + 1;
        set(
            &mut goal.continuation,
            json!({
                "state": kind, "reason": reason, "due_at": null, "request_id": "",
                "lease_until": null, "generation": next, "observed_state": kind,
                "observed_reason": reason, "observed_at": now,
            }),
        );
    }
    remember(plan, &mut goal, kind, revision, detail, now);
    plan.goal = Some(goal);
}

/// Stops the clock of the steps that run: a paused goal's work is not running.
fn freeze_running(plan: &mut Plan, now: i64) {
    for item in &mut plan.items {
        if item.status == "in_progress"
            && let Some(started) = item.started_at.take()
        {
            item.active_ms += (now - started).max(0);
        }
    }
}

fn thaw_running(plan: &mut Plan, now: i64) {
    for item in &mut plan.items {
        if item.status == "in_progress" && item.started_at.is_none() {
            item.started_at = Some(now);
        }
    }
}

/// Ends the steps that run, as a plan that ends this way ends them.
fn close_running(plan: &mut Plan, plan_status: &str, now: i64) {
    let terminal = match plan_status {
        "cancelled" => "pending",
        "blocked" => "blocked",
        _ => "completed",
    };
    for item in &mut plan.items {
        if item.status != "in_progress" {
            continue;
        }
        if let Some(started) = item.started_at.take() {
            item.active_ms += (now - started).max(0);
        }
        item.status = terminal.to_string();
        item.completed_at = TERMINAL_ITEM_STATUSES.contains(&terminal).then_some(now);
    }
}

fn step_name<'a>(plan_id: &str, item_id: &'a str) -> &'a str {
    item_id
        .strip_prefix(plan_id)
        .and_then(|suffix| suffix.strip_prefix(':'))
        .unwrap_or(item_id)
}

fn step_names(plan_id: &str, mut items: Vec<&Item>) -> String {
    items.sort_by_key(|item| item.position);
    items
        .iter()
        .map(|item| step_name(plan_id, &item.item_id))
        .collect::<Vec<_>>()
        .join(", ")
}

fn require_completion(plan: &Plan) -> Result<(), Refused> {
    let Plan {
        plan_id, revision, ..
    } = plan;
    let unresolved = |item: &Item| {
        item.required != 0 && !matches!(item.status.as_str(), "completed" | "removed")
    };
    let required: Vec<_> = plan.items.iter().filter(|item| unresolved(item)).collect();
    if !required.is_empty() {
        let items = step_names(plan_id, required);
        return Err(Refused(format!(
            "required work remains unresolved: {items}; settle each with `clarp-goal step \
             {plan_id} {revision} ITEM completed` (or removed, with a reason)"
        )));
    }
    let running: Vec<_> = plan
        .items
        .iter()
        .filter(|item| item.status == "in_progress")
        .collect();
    if !running.is_empty() {
        let items = step_names(plan_id, running);
        return Err(Refused(format!(
            "running work must be resolved or deferred before completion: {items}; \
             `clarp-goal step {plan_id} {revision} ITEM completed` (or deferred, with a reason)"
        )));
    }
    match &plan.goal {
        Some(goal) => {
            let criteria = goal.fields.get("criteria").and_then(Value::as_array);
            let missing: Vec<_> = criteria
                .into_iter()
                .flatten()
                .filter(|criterion| {
                    stripped(criterion.get("evidence")).is_empty()
                        && !truthy(criterion.get("waived"))
                })
                .map(|criterion| plain(criterion.get("id")))
                .collect();
            if !missing.is_empty() {
                let names = missing.join(", ");
                let example = missing
                    .iter()
                    .map(|id| format!("\"{id}\":\"...\""))
                    .collect::<Vec<_>>()
                    .join(", ");
                return Err(Refused(format!(
                    "completion requires evidence for every acceptance criterion \
                     (or the owner's waiver of it, with a reason); missing: {names}. Record it with \
                     `clarp-goal checkpoint {plan_id} {revision} \
                     '{{\"progress\":\"...\",\"next_work\":\"...\",\"evidence\":{{{example}}}}}'`, \
                     then `clarp-goal complete {plan_id} REVISION` with the revision it answers"
                )));
            }
        }
        None => {
            if plan.items.iter().any(|item| item.status != "completed") {
                return refuse("unfinished legacy work cannot be completed");
            }
        }
    }
    Ok(())
}

/// A new goal from what it was given, and whether its owner is to be woken for it.
fn new_goal(
    agent_id: &str,
    title: &str,
    raw: &Value,
    native: &str,
    now: i64,
) -> Result<(Goal, bool), Refused> {
    let outcome = match plain(raw.get("outcome")) {
        given if !given.is_empty() => given.trim().to_string(),
        _ => title.trim().to_string(),
    };
    let criteria = raw.get("criteria").and_then(Value::as_array);
    let Some(criteria) = criteria.filter(|list| !list.is_empty() && list.len() <= 100) else {
        return refuse("goal requires 1–100 completion criteria");
    };
    let limits = raw.get("limits").and_then(Value::as_str);
    let Some(limits) = limits.filter(|limits| !limits.trim().is_empty()) else {
        return refuse(
            "explicit goal limits required (use \"No additional limits\" if applicable)",
        );
    };
    let texts: Vec<String> = criteria
        .iter()
        .map(|criterion| text(criterion).trim().to_string())
        .collect();
    if texts.iter().any(String::is_empty) {
        return refuse("completion criteria must not be empty");
    }
    let enroll = match raw.get("enroll") {
        None => false,
        Some(Value::Bool(flag)) => *flag,
        Some(_) => return refuse("enroll must be a boolean"),
    };
    if enroll && native.is_empty() {
        return refuse("recovery enrollment requires a bound native conversation");
    }
    let limit = match raw.get("recovery_limit") {
        None => 5,
        Some(given) => given.as_i64().unwrap_or(5),
    };
    let criteria: Vec<Value> = texts
        .iter()
        .enumerate()
        .map(|(n, text)| json!({ "id": format!("criterion-{}", n + 1), "text": text, "evidence": "" }))
        .collect();
    let mut goal = Goal::default();
    set(
        &mut goal.fields,
        json!({
            "outcome": outcome,
            "limits": limits,
            "owner_agent_id": agent_id,
            "native_session_id": native,
            "criteria": criteria,
            "checkpoint": null,
            "recovery_limit": limit.clamp(1, 20),
        }),
    );
    set(
        &mut goal.continuation,
        json!({
            "generation": 0,
            "state": if enroll { "ready" } else { "not_enrolled" },
            "reason": if enroll { "Awaiting owner checkpoint" } else { "Recovery not enrolled" },
            "due_at": if enroll { json!(now + 120_000) } else { Value::Null },
            "attempts": 0,
            "lease_until": null,
            "request_id": "",
        }),
    );
    goal.history.push(
        json!({ "at": now, "kind": "created", "revision": 0, "detail": { "outcome": outcome } }),
    );
    Ok((goal, enroll))
}

impl GoalBook {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn plan(&self, plan_id: &str) -> Option<&Plan> {
        self.plans.get(plan_id)
    }

    pub fn plans(&self) -> impl Iterator<Item = &Plan> {
        self.plans.values()
    }

    /// What the log keeps of a change.
    pub fn record(&self, change: &Change, at_ms: i64) -> GoalRecord {
        let new = &change.plan;
        let old = self.plans.get(&new.plan_id);
        let none = Goal::default();
        let was = old.and_then(|plan| plan.goal.as_ref()).unwrap_or(&none);
        let is = new.goal.as_ref().unwrap_or(&none);
        let set: Map<String, Value> = is
            .fields
            .iter()
            .filter(|(key, value)| was.fields.get(*key) != Some(value))
            .map(|(key, value)| (key.clone(), value.clone()))
            .collect();
        let unset: Vec<String> = was
            .fields
            .keys()
            .filter(|key| !is.fields.contains_key(*key))
            .cloned()
            .collect();
        let old_items = old.map_or(&[][..], |plan| plan.items.as_slice());
        let old_history = old.map_or(&[][..], |plan| plan.history.as_slice());
        GoalRecord {
            accounting: old.is_none().then(Vec::new),
            at_ms,
            plan_id: new.plan_id.clone(),
            agent_id: new.agent_id.clone(),
            session: new.session.clone(),
            title: new.title.clone(),
            status: new.status.clone(),
            created_at: new.created_at,
            updated_at: new.updated_at,
            completed_at: new.completed_at,
            revision: new.revision,
            recovery_enabled: new.recovery_enabled,
            event_count: new.event_count,
            last_event_at: new.last_event_at,
            subgoals: match old {
                Some(p) if std::sync::Arc::ptr_eq(&p.subgoals, &new.subgoals) => vec![],
                Some(p) => {
                    // Subgoal revisions advance on every edit. Do not compare evidence
                    // bodies against every other row in the plan to find the delta.
                    let revisions: BTreeMap<_, _> = p
                        .subgoals
                        .iter()
                        .map(|r| (r["subgoal_id"].as_str(), r["revision"].as_i64()))
                        .collect();
                    new.subgoals
                        .iter()
                        .filter(|r| {
                            revisions.get(&r["subgoal_id"].as_str())
                                != Some(&r["revision"].as_i64())
                        })
                        .cloned()
                        .collect()
                }
                None => (*new.subgoals).clone(),
            },
            goal: new.goal.is_some(),
            set,
            unset,
            continuation: (was.continuation != is.continuation).then(|| is.continuation.clone()),
            history: Grown::between(
                (was.history.as_slice(), was.history_offset),
                (is.history.as_slice(), is.history_offset),
                GOAL_HISTORY_LIMIT,
            ),
            plan_history: Grown::between((old_history, 0), (new.history.as_slice(), 0), usize::MAX),
            items: new
                .items
                .iter()
                .filter(|item| !old_items.contains(item))
                .cloned()
                .collect(),
            documents: change.documents.clone(),
        }
    }

    /// A plan that comes with an agent that moves here, as the Host it was on showed it
    /// (`GET /task-plans`), with the documents of its goal that came with it. Answers the
    /// change that puts it into the agent's books as it stood there: under its id, at its
    /// revision, with its steps, its history and how its work goes on. Nothing of it is
    /// judged anew: it was a plan there, and it is the same plan here.
    pub fn taken_over(
        &self,
        owner: Owner<'_>,
        shown: &Value,
        documents: &[Value],
    ) -> Result<Change, Refused> {
        let text = |name: &str| {
            let said = shown.get(name).and_then(Value::as_str);
            said.unwrap_or("").to_string()
        };
        let whole = |name: &str| shown.get(name).and_then(Value::as_i64);
        let plan_id = text("plan_id");
        if plan_id.is_empty() || text("agent_id") != owner.agent_id {
            return refuse("the plan is not this agent's");
        }
        if self.plans.contains_key(&plan_id) {
            return refuse("the agent has this plan already");
        }
        if text("status").is_empty() {
            return refuse("the plan does not say how it stands");
        }
        let unreadable = |what: &str, error: &serde_json::Error| {
            Refused(format!("{what} of the plan cannot be read: {error}"))
        };
        // The steps as they are kept: one after another, each naming the step it is under.
        let mut items: Vec<Item> = Vec::new();
        let roots = shown.get("items").and_then(Value::as_array);
        for root in roots.into_iter().flatten() {
            let under = root.get("subtasks").and_then(Value::as_array);
            for row in std::iter::once(root).chain(under.into_iter().flatten()) {
                let item = Item::deserialize(row).map_err(|error| unreadable("a step", &error))?;
                items.push(item);
            }
        }
        let goal = shown.get("goal").and_then(Value::as_object).map(|given| {
            let mut fields = given.clone();
            let listed = |value: Option<Value>| match value {
                Some(Value::Array(entries)) => entries,
                _ => Vec::new(),
            };
            let history = listed(fields.remove("history"));
            let continuation = match fields.remove("continuation") {
                Some(Value::Object(continuation)) => continuation,
                _ => Map::new(),
            };
            let history_offset = fields
                .remove("history_offset")
                .and_then(|offset| offset.as_u64());
            // The list of its documents is made from the documents themselves.
            fields.remove("documents");
            Goal {
                fields,
                continuation,
                history,
                history_offset: history_offset.unwrap_or(0),
            }
        });
        let mut came = Vec::new();
        for row in documents {
            let document =
                Document::deserialize(row).map_err(|error| unreadable("a document", &error))?;
            if document.plan_id != plan_id {
                return refuse("a document of another plan came with the plan");
            }
            came.push(document);
        }
        let ledger = |name: &str| shown.get("ledger").and_then(|ledger| ledger.get(name));
        let plan = Plan {
            agent_id: owner.agent_id.to_string(),
            session: owner.session.to_string(),
            title: text("title"),
            status: text("status"),
            created_at: whole("created_at").unwrap_or(0),
            updated_at: whole("updated_at").unwrap_or(0),
            completed_at: whole("completed_at"),
            revision: whole("revision").unwrap_or(1),
            recovery_enabled: whole("recovery_enabled").unwrap_or(0),
            goal,
            subgoals: subgoals::imported(&shown["subgoals"])?,
            history: shown
                .get("history")
                .and_then(Value::as_array)
                .cloned()
                .unwrap_or_default(),
            items,
            event_count: ledger("event_count").and_then(Value::as_i64).unwrap_or(0),
            last_event_at: ledger("last_event_at").and_then(Value::as_i64),
            order: 0,
            plan_id,
        };
        Ok(Change {
            plan,
            documents: came,
        })
    }

    /// Takes a change that is in the log.
    pub fn apply(&mut self, record: &GoalRecord) {
        if let Some(rows) = record.accounting.as_ref().filter(|rows| !rows.is_empty()) {
            let kept = std::sync::Arc::make_mut(
                self.accounting.entry(record.plan_id.clone()).or_default(),
            );
            kept.extend(rows.iter().cloned());
            kept.drain(..kept.len().saturating_sub(500));
        }
        let known = self.plans.remove(&record.plan_id);
        let order = known.as_ref().map_or(self.made + 1, |plan| plan.order);
        self.made = self.made.max(order);
        let (old_goal, mut history, mut items, mut subgoals) = match known {
            Some(plan) => (plan.goal, plan.history, plan.items, plan.subgoals),
            None => (None, Vec::new(), Vec::new(), Default::default()),
        };
        let goal = record.goal.then(|| {
            let mut goal = old_goal.unwrap_or_default();
            for key in &record.unset {
                goal.fields.remove(key);
            }
            for (key, value) in &record.set {
                goal.fields.insert(key.clone(), value.clone());
            }
            if let Some(continuation) = &record.continuation {
                goal.continuation.clone_from(continuation);
            }
            record.history.onto(
                &mut goal.history,
                &mut goal.history_offset,
                GOAL_HISTORY_LIMIT,
            );
            goal
        });
        record.plan_history.onto(&mut history, &mut 0, usize::MAX);
        for item in &record.items {
            match items.iter_mut().find(|known| known.item_id == item.item_id) {
                Some(known) => known.clone_from(item),
                None => items.push(item.clone()),
            }
        }
        if !record.subgoals.is_empty() {
            let rows = std::sync::Arc::make_mut(&mut subgoals);
            for row in &record.subgoals {
                if let Some(prior) = rows
                    .iter_mut()
                    .find(|r| r["subgoal_id"] == row["subgoal_id"])
                {
                    prior.clone_from(row);
                } else {
                    rows.push(row.clone());
                }
            }
            subgoals::sort_rows(rows);
        }
        let plan = Plan {
            subgoals,
            plan_id: record.plan_id.clone(),
            agent_id: record.agent_id.clone(),
            session: record.session.clone(),
            title: record.title.clone(),
            status: record.status.clone(),
            created_at: record.created_at,
            updated_at: record.updated_at,
            completed_at: record.completed_at,
            revision: record.revision,
            recovery_enabled: record.recovery_enabled,
            goal,
            history,
            items,
            event_count: record.event_count,
            last_event_at: record.last_event_at,
            order,
        };
        self.plans.insert(plan.plan_id.clone(), plan);
        self.documents.extend(record.documents.iter().cloned());
    }

    /// The newest version of each of a plan's documents, without their content.
    fn document_index(&self, plan_id: &str, added: &[Document]) -> Vec<Value> {
        let mut newest: BTreeMap<&str, &Document> = BTreeMap::new();
        let known = self.documents.iter().filter(|d| d.plan_id == plan_id);
        for document in known.chain(added) {
            newest.insert(&document.name, document);
        }
        newest
            .values()
            .map(|d| {
                json!({ "name": d.name, "revision": d.revision, "format": d.format,
                        "reason": d.reason, "updated_at": d.updated_at })
            })
            .collect()
    }

    /// A plan as it is read: with its steps as a tree, what they add up to, and the moment.
    fn view(&self, plan: &Plan, added: &[Document], now: i64) -> Value {
        let paused = plan.status == "paused";
        let shown = |item: &Item| {
            let mut row = item.row();
            let running = item.status == "in_progress";
            let since = item.started_at.filter(|_| running);
            let elapsed = item.active_ms + since.map_or(0, |started| (now - started).max(0));
            row["subtasks"] = json!([]);
            row["elapsed_ms"] = json!(elapsed);
            row["paused"] = json!(paused && running);
            row
        };
        let mut roots: Vec<&Item> = plan
            .items
            .iter()
            .filter(|i| i.parent_id.is_none())
            .collect();
        roots.sort_by_key(|item| item.position);
        let items: Vec<Value> = roots
            .iter()
            .map(|root| {
                let mut children: Vec<&Item> = plan
                    .items
                    .iter()
                    .filter(|item| item.parent_id.as_deref() == Some(&root.item_id))
                    .collect();
                children.sort_by_key(|item| item.position);
                let mut row = shown(root);
                row["subtasks"] = children.into_iter().map(&shown).collect();
                row
            })
            .collect();
        let counts: Map<String, Value> = ITEM_STATUSES
            .iter()
            .map(|status| {
                let n = plan.items.iter().filter(|i| i.status == *status).count();
                ((*status).to_string(), json!(n))
            })
            .collect();
        let done = plan
            .items
            .iter()
            .filter(|i| i.status == "completed")
            .count();
        let mut data = json!({
            "revision": plan.revision,
            "recovery_enabled": plan.recovery_enabled,
            "plan_id": plan.plan_id,
            "agent_id": plan.agent_id,
            "session": plan.session,
            "title": plan.title,
            "status": plan.status,
            "created_at": plan.created_at,
            "updated_at": plan.updated_at,
            "completed_at": plan.completed_at,
            "goal": null,
            "history": plan.history,
            "legacy": plan.goal.is_none(),
            "completion_verified": plan.goal.is_some() && plan.status == "completed",
            "counts": counts,
            "items": items,
            "completed_count": done,
            "total_count": plan.items.len(),
            "server_now": now,
        });
        if let Some(goal) = &plan.goal {
            let mut whole = goal.document();
            whole["documents"] = json!(self.document_index(&plan.plan_id, added));
            data["goal"] = whole;
            data["subgoals"] = json!(*plan.subgoals);
            data["ledger"] = json!({
                "event_count": plan.event_count, "last_event_at": plan.last_event_at,
                "delegation": null, "accounting": self.accounting_for(&plan.plan_id),
            });
            data["updater"] = json!({
                "enabled": false, "janitor_agent_id": null, "last_run_at": null,
                "last_error": null, "goal_error": null, "retry_at": null, "last_receipt": null,
            });
        }
        data
    }

    /// A plan as it is read, or nothing when there is no such plan.
    pub fn get(&self, plan_id: &str, now: i64) -> Option<Value> {
        self.plans
            .get(plan_id)
            .map(|plan| self.view(plan, &[], now))
    }

    /// The plan a change leaves, as it is read.
    pub fn shown(&self, change: &Change, now: i64) -> Value {
        self.view(&change.plan, &change.documents, now)
    }

    /// The plans, the last changed first.
    fn newest_first(&self) -> Vec<&Plan> {
        let mut plans: Vec<&Plan> = self.plans.values().collect();
        plans.sort_by_key(|plan| std::cmp::Reverse((plan.updated_at, plan.order)));
        plans
    }

    /// The agent's plans as they are read, the last changed first: at most a hundred.
    pub fn list(&self, now: i64) -> Value {
        self.newest_first()
            .iter()
            .take(100)
            .map(|plan| self.view(plan, &[], now))
            .collect()
    }

    /// The plan that is being worked: the last changed active one, or nothing.
    pub fn show(&self, now: i64) -> Value {
        self.newest_first()
            .iter()
            .find(|plan| plan.status == "active")
            .map_or_else(|| json!({}), |plan| self.view(plan, &[], now))
    }

    /// One version of a document, with its content: the newest, or the one asked for.
    pub fn read(&self, plan_id: &str, name: &str, revision: Option<i64>) -> Result<Value, Refused> {
        let versions: Vec<&Document> = self
            .documents
            .iter()
            .filter(|d| d.plan_id == plan_id && d.name == name)
            .collect();
        let found = versions
            .iter()
            .rev()
            .find(|d| revision.is_none_or(|revision| d.revision == revision));
        let Some(document) = found else {
            return refuse("goal document not found");
        };
        let shown = if document.format == "markdown" {
            text(&document.content)
        } else {
            serde_json::to_string_pretty(&document.content).unwrap_or_default()
        };
        let history: Vec<Value> = versions
            .iter()
            .rev()
            .map(|d| json!({ "revision": d.revision, "reason": d.reason, "updated_at": d.updated_at }))
            .collect();
        Ok(json!({
            "plan_id": document.plan_id, "name": document.name, "revision": document.revision,
            "format": document.format, "reason": document.reason, "updated_at": document.updated_at,
            "content": document.content, "text": shown, "history": history,
        }))
    }

    /// Makes a plan, with a goal when one is given.
    pub fn create(
        &self,
        owner: Owner<'_>,
        alias: &str,
        title: &str,
        items: &Value,
        goal: Option<&Value>,
        world: &mut World<'_>,
    ) -> Result<Change, Refused> {
        let title = title.trim();
        if title.is_empty() {
            return refuse("plan title required");
        }
        let raw_items: Vec<&Map<String, Value>> = match items.as_array() {
            Some(list) if list.iter().all(Value::is_object) => {
                list.iter().filter_map(Value::as_object).collect()
            }
            _ => return refuse("plan items must be objects in a list"),
        };
        let now = world.now_ms;
        let stable = match alias.trim() {
            "" => "work".to_string(),
            alias => bounded_key(alias),
        };
        let agent_id = owner.agent_id;
        let plan_id = format!("{agent_id}:{stable}:{}", (world.token_hex)(4));
        let mut made: Vec<Item> = Vec::new();
        let too_many = || Refused(format!("task plan exceeds {MAX_PLAN_ITEMS} items"));
        for (position, raw) in raw_items.iter().enumerate() {
            if made.len() + 1 > MAX_PLAN_ITEMS {
                return Err(too_many());
            }
            let given = match plain(raw.get("id")) {
                given if !given.is_empty() => given,
                _ => format!("task-{}", (world.token_hex)(6)),
            };
            let item_id = item_key(&plan_id, &given, world.token_hex);
            made.push(new_item(
                &plan_id,
                item_id.clone(),
                None,
                position,
                raw,
                now,
            )?);
            let subtasks = raw.get("subtasks").filter(|given| is_true(given));
            let Some(subtasks) = subtasks else { continue };
            let Some(subtasks) = subtasks.as_array() else {
                return refuse("subtasks must be objects with at most two plan levels");
            };
            for (child_position, child) in subtasks.iter().enumerate() {
                let Some(child) = child.as_object().filter(|c| !truthy(c.get("subtasks"))) else {
                    return refuse("subtasks must be objects with at most two plan levels");
                };
                if made.len() + 1 > MAX_PLAN_ITEMS {
                    return Err(too_many());
                }
                let given = match plain(child.get("id")) {
                    given if !given.is_empty() => given,
                    _ => format!("subtask-{}", (world.token_hex)(6)),
                };
                let child_id = item_key(&plan_id, &given, world.token_hex);
                let parent = Some(item_id.clone());
                made.push(new_item(
                    &plan_id,
                    child_id,
                    parent,
                    child_position,
                    child,
                    now,
                )?);
            }
        }
        let (goal, enrolled) = match goal {
            Some(raw) => {
                let (goal, enrolled) = new_goal(agent_id, title, raw, world.native_session, now)?;
                (Some(goal), enrolled)
            }
            None => (None, false),
        };
        let has_goal = goal.is_some();
        Ok(Change {
            plan: Plan {
                plan_id,
                agent_id: agent_id.to_string(),
                session: owner.session.to_string(),
                title: clip(title, 240),
                status: "active".to_string(),
                created_at: now,
                updated_at: now,
                completed_at: None,
                revision: 0,
                recovery_enabled: i64::from(enrolled),
                goal,
                history: Vec::new(),
                subgoals: Default::default(),
                items: made,
                event_count: i64::from(has_goal),
                last_event_at: has_goal.then_some(now),
                order: 0,
            },
            documents: Vec::new(),
        })
    }

    /// Moves a step: taken up, done, set aside. `item_id` is the step's whole id.
    pub fn step(
        &self,
        item_id: &str,
        revision: Option<i64>,
        status: &str,
        detail: Option<&str>,
        now: i64,
    ) -> Result<Change, Refused> {
        if !ITEM_STATUSES.contains(&status) {
            return refuse("invalid task status");
        }
        if matches!(status, "skipped" | "deferred" | "removed")
            && detail.unwrap_or("").trim().is_empty()
        {
            return refuse("omitted work requires a reason");
        }
        let has = |plan: &&Plan| plan.items.iter().any(|item| item.item_id == item_id);
        let Some(plan) = self.plans.values().find(has) else {
            return refuse("task item not found");
        };
        let active = plan.status == "active";
        if !active && !(plan.status == "paused" && plan.goal.is_some()) {
            return refuse("task plan is no longer active");
        }
        check_revision(plan, revision, || {
            let step = step_name(&plan.plan_id, item_id);
            format!("step {} {} {step} {status}", plan.plan_id, plan.revision)
        })?;
        let mut plan = plan.clone();
        let before = plan
            .items
            .iter()
            .find(|item| item.item_id == item_id)
            .map(|item| item.status.clone())
            .unwrap_or_default();
        let reason = detail.unwrap_or("");
        note(
            &mut plan,
            "step",
            json!({ "item_id": item_id, "from": before, "status": status, "reason": reason }),
            now,
        );
        if let Some(item) = plan.items.iter_mut().find(|item| item.item_id == item_id) {
            if item.status == "in_progress"
                && let Some(started) = item.started_at
            {
                item.active_ms += (now - started).max(0);
            }
            // A paused goal's work is not running: its clock stays stopped.
            item.started_at = (status == "in_progress" && active).then_some(now);
            item.completed_at = TERMINAL_ITEM_STATUSES.contains(&status).then_some(now);
            item.status = status.to_string();
            if let Some(detail) = detail {
                item.detail = clip(detail, 2_000);
            }
        }
        plan.updated_at = now;
        plan.revision += 1;
        // A plan that is only a list of steps is done when its steps are.
        if plan.goal.is_none()
            && active
            && !plan.items.is_empty()
            && plan.items.iter().all(|item| item.status == "completed")
        {
            plan.status = "completed".to_string();
            plan.completed_at = Some(now);
        }
        Ok(Change {
            plan,
            documents: Vec::new(),
        })
    }

    /// What the Goal keeper writes beside the owner's own record: the statuses of steps, and
    /// its own checkpoint (`updater` in the goal). Never the goal's state, what it waits for,
    /// its criteria or their evidence, and never the owner's checkpoint. The plan's revision
    /// stays what it was, so that the owner's next command is not refused because of the
    /// keeper; the keeper's write is refused instead where the owner changed the plan since
    /// the keeper read it. A step the owner closed stays closed. Answers the plan as it now
    /// stands and the steps that changed, each with what it was. The rule is the Python
    /// Host's (`task_plans.updater_apply`), held by the goal tool's vectors.
    pub fn updater(
        &self,
        plan_id: &str,
        update: &Update<'_>,
        now: i64,
    ) -> Result<(Change, Vec<Value>), NotUpdated> {
        let progress = clip(crate::python::strip(update.progress), 2_000);
        let next_work = clip(crate::python::strip(update.next_work), 1_000);
        if progress.is_empty() {
            return Err(NotUpdated::Invalid("updater checkpoint requires progress"));
        }
        let open = |plan: &&Plan| matches!(plan.status.as_str(), "active" | "paused");
        let Some(plan) = self.plans.get(plan_id).filter(open) else {
            return Err(NotUpdated::Closed);
        };
        if plan.revision != update.seen_revision {
            return Err(NotUpdated::OwnerAhead);
        }
        let mut plan = plan.clone();
        let Some(mut goal) = plan.goal.take() else {
            return Err(NotUpdated::Invalid("not a durable goal"));
        };
        let running = plan.status == "active";
        let mut changed = Vec::new();
        for (item_id, status, said) in update.steps {
            let known = KEEPER_STEP_STATUSES.contains(&status.as_str());
            let Some(item) = plan.items.iter_mut().find(|item| item.item_id == *item_id) else {
                continue;
            };
            if !known || KEPT_CLOSED.contains(&item.status.as_str()) || item.status == *status {
                continue;
            }
            if item.status == "in_progress"
                && let Some(started) = item.started_at
            {
                item.active_ms += (now - started).max(0);
            }
            changed.push(json!({
                "item_id": item_id, "from": item.status, "status": status, "note": clip(said, 500),
            }));
            // A paused goal's work is not running: its clock stays stopped.
            item.started_at = (status == "in_progress" && running).then_some(now);
            item.completed_at = (status == "completed").then_some(now);
            item.status.clone_from(status);
        }
        let sources: Vec<&String> = update.source_refs.iter().take(40).collect();
        let ids: Vec<&Value> = changed.iter().map(|step| &step["item_id"]).collect();
        let through = if update.through_ms == 0 {
            now
        } else {
            update.through_ms
        };
        goal.fields.insert(
            "updater".to_string(),
            json!({
                "at": now, "progress": progress, "next_work": next_work, "model": update.model,
                "source_refs": sources, "changed_steps": ids, "through_ms": through,
                "through_event": update.through_event, "by": "goal-keeper",
                "janitor_agent_id": update.janitor_agent_id,
            }),
        );
        let detail = json!({
            "reason": format!("Goal keeper: {}", clip(&progress, 200)), "by": "goal-keeper",
            "model": update.model, "steps": changed, "source_refs": sources,
        });
        let revision = plan.revision;
        remember(&mut plan, &mut goal, "updater", revision, detail, now);
        plan.goal = Some(goal);
        plan.updated_at = now;
        Ok((
            Change {
                plan,
                documents: Vec::new(),
            },
            changed,
        ))
    }

    /// The Host continued the owner's conversation with its tool in a new one: the tool let
    /// go of the old, and the new one opens with the Host's own record of it. A goal is
    /// bound to its owner's conversation and stops when that changes, since a silent change
    /// means the agent lost what it knew; here the Host carried the conversation over, so
    /// every goal that is bound to the old one and is not over follows it, one revision on,
    /// and a wake that is out for the plan as it stood is the plan's still. Answers the
    /// plans that moved, each as it now stands, in the order they were made. The rule is
    /// the Python Host's (`task_plans.carry_native_session`), held by the goal tool's
    /// vectors.
    pub fn carried(&self, from: &str, to: &str, reason: &str, now: i64) -> Vec<Change> {
        if from.is_empty() || to.is_empty() {
            return Vec::new();
        }
        let mut open: Vec<&Plan> = self.plans.values().collect();
        open.sort_by_key(|plan| plan.order);
        let mut moved = Vec::new();
        for plan in open {
            let over = matches!(
                plan.status.as_str(),
                "completed" | "cancelled" | "superseded"
            );
            let bound = |goal: &&Goal| goal.fields.get("native_session_id") == Some(&json!(from));
            let Some(goal) = plan.goal.as_ref().filter(|_| !over).filter(bound) else {
                continue;
            };
            let (mut plan, mut goal) = (plan.clone(), goal.clone());
            goal.fields
                .insert("native_session_id".to_string(), json!(to));
            let next = plan.revision + 1;
            let waited_for = goal.continuation.get("plan_revision");
            if waited_for.and_then(Value::as_i64) == Some(plan.revision) {
                goal.continuation
                    .insert("plan_revision".to_string(), json!(next));
            }
            let detail = json!({
                "reason": reason, "from_native_session_id": from, "native_session_id": to,
            });
            remember(&mut plan, &mut goal, "rebind", next, detail, now);
            plan.revision = next;
            plan.updated_at = now;
            plan.goal = Some(goal);
            moved.push(Change {
                plan,
                documents: Vec::new(),
            });
        }
        moved
    }

    /// Ends a plan: completed, blocked or cancelled.
    pub fn finish(
        &self,
        plan_id: &str,
        status: &str,
        reason: &str,
        revision: Option<i64>,
        now: i64,
    ) -> Result<Change, Refused> {
        if !matches!(status, "completed" | "blocked" | "cancelled") {
            return refuse("invalid plan status");
        }
        let Some(plan) = self
            .plans
            .get(plan_id)
            .filter(|plan| plan.status == "active")
        else {
            return refuse("task plan is no longer active");
        };
        check_revision(plan, revision, || {
            let Plan {
                plan_id, revision, ..
            } = plan;
            match status {
                "blocked" => format!("block {plan_id} {revision} '{{\"reason\":\"...\"}}'"),
                "cancelled" => format!("cancel {plan_id} {revision} '{{\"reason\":\"...\"}}'"),
                _ => format!("complete {plan_id} {revision}"),
            }
        })?;
        if status == "completed" {
            require_completion(plan)?;
        }
        let mut plan = plan.clone();
        note(&mut plan, status, json!({ "reason": reason }), now);
        close_running(&mut plan, status, now);
        plan.status = status.to_string();
        plan.updated_at = now;
        plan.completed_at = Some(now);
        plan.revision += 1;
        Ok(Change {
            plan,
            documents: Vec::new(),
        })
    }

    /// The versions a list of document changes makes, each on top of the newest there is.
    fn written(
        &self,
        plan_id: &str,
        documents: &Value,
        now: i64,
    ) -> Result<Vec<Document>, Refused> {
        let Some(list) = documents.as_array().filter(|list| list.len() <= 32) else {
            return refuse("checkpoint supports at most 32 document changes");
        };
        let mut made: Vec<Document> = Vec::new();
        for document in list {
            let name = stripped(document.get("name"));
            if name.is_empty()
                || name.chars().count() > 160
                || made.iter().any(|known| known.name == name)
            {
                return refuse("unique document name required, at most 160 characters");
            }
            let format = document.get("format").and_then(Value::as_str).unwrap_or("");
            let content = document.get("content").cloned().unwrap_or(Value::Null);
            let reason = stripped(document.get("reason"));
            if !matches!(format, "markdown" | "json")
                || (format == "markdown" && !content.is_string())
                || reason.is_empty()
            {
                return refuse(
                    "document requires markdown/json format, valid content and change reason",
                );
            }
            if content.to_string().len() > 65_536 {
                return refuse(
                    "document exceeds 64 KiB; reference a managed evidence file instead",
                );
            }
            let current = self
                .documents
                .iter()
                .filter(|d| d.plan_id == plan_id && d.name == name)
                .map(|d| d.revision)
                .max()
                .unwrap_or(0);
            if document.get("document_revision").and_then(Value::as_i64) != Some(current) {
                return refuse("document changed; reload its current revision");
            }
            made.push(Document {
                plan_id: plan_id.to_string(),
                name,
                revision: current + 1,
                format: format.to_string(),
                content,
                reason,
                updated_at: now,
            });
        }
        Ok(made)
    }

    /// Everything else a goal's owner does to it: a checkpoint, a pause, a new plan, a note.
    pub fn act(
        &self,
        plan_id: &str,
        revision: i64,
        action: &str,
        data: &Value,
        world: &mut World<'_>,
    ) -> Result<Change, Refused> {
        self.act_by(false, plan_id, revision, action, data, world)
    }

    /// What the user does to a goal, through an app. The user may do what the owner may, and
    /// one thing the owner may not: set a criterion aside, or take that back.
    pub fn act_for_user(
        &self,
        plan_id: &str,
        revision: i64,
        action: &str,
        data: &Value,
        world: &mut World<'_>,
    ) -> Result<Change, Refused> {
        self.act_by(true, plan_id, revision, action, data, world)
    }

    fn act_by(
        &self,
        by_user: bool,
        plan_id: &str,
        revision: i64,
        action: &str,
        data: &Value,
        world: &mut World<'_>,
    ) -> Result<Change, Refused> {
        let now = world.now_ms;
        let Some(known) = self.plans.get(plan_id) else {
            return refuse("task plan not found");
        };
        check_revision(known, Some(revision), || format!("get {plan_id}"))?;
        let mut plan = known.clone();
        let mut data = if is_true(data) && data.is_object() {
            data.clone()
        } else {
            json!({})
        };
        let mut documents: Vec<Document> = Vec::new();
        let was_legacy = plan.goal.is_none();
        if action == "enroll" && was_legacy {
            if plan.status != "active" {
                return refuse("only active legacy work can enroll");
            }
            let (goal, enrolled) = new_goal(
                &plan.agent_id,
                &plan.title,
                &data,
                world.native_session,
                now,
            )?;
            plan.goal = Some(goal);
            plan.recovery_enabled = i64::from(enrolled);
            plan.event_count += 1;
            plan.last_event_at = Some(now);
        }
        let Some(mut goal) = plan.goal.take() else {
            return refuse("legacy plan must be explicitly enrolled as a goal");
        };
        if matches!(
            plan.status.as_str(),
            "completed" | "cancelled" | "superseded"
        ) {
            return refuse("goal is closed");
        }
        let reason = stripped(data.get("reason"));
        match action {
            "enroll" if !was_legacy => {
                if data.get("enroll") != Some(&Value::Bool(true)) || reason.is_empty() {
                    return refuse("explicit enrollment requires enroll:true and a reason");
                }
                let native = world.native_session;
                let bound = goal.text("native_session_id");
                if native.is_empty() || !(bound.is_empty() || bound == native) {
                    return refuse("verify and rebind the owner conversation before enrollment");
                }
                goal.fields
                    .insert("native_session_id".to_string(), json!(native));
                plan.recovery_enabled = 1;
                let wake = &mut goal.continuation;
                let next = generation(wake) + 1;
                set(
                    wake,
                    json!({ "generation": next, "request_id": "", "lease_until": null }),
                );
                if plan.status == "active" {
                    set(
                        wake,
                        json!({
                            "state": "ready", "due_at": now + 15_000, "reason": reason,
                            "observed_state": "ready", "observed_reason": reason,
                            "observed_at": now,
                        }),
                    );
                }
            }
            "enroll" => {}
            "pause" | "cancel" | "supersede" | "block" | "resume" => {
                if reason.is_empty() {
                    return refuse("goal control requires a reason");
                }
                if action == "supersede" {
                    let other = plain(data.get("replacement_id"));
                    let same_owner = self
                        .plans
                        .get(&other)
                        .is_some_and(|replacement| replacement.agent_id == plan.agent_id);
                    if !same_owner || other == plan_id {
                        return refuse("replacement must be another plan owned by the same agent");
                    }
                    goal.fields
                        .insert("superseded_by".to_string(), json!(other));
                }
                let status = match action {
                    "pause" => "paused",
                    "cancel" => "cancelled",
                    "supersede" => "superseded",
                    "block" => "blocked",
                    _ => "active",
                };
                let waiting = goal.state() == "waiting";
                let wake = &mut goal.continuation;
                if action == "resume" {
                    wake.insert(
                        "resume_queue_revision".to_string(),
                        json!(world.queue_revision),
                    );
                }
                // What the goal waited for when it was paused or blocked, it waits for again
                // when it is resumed, with the deadline it had.
                if matches!(action, "pause" | "block") && waiting {
                    let due = wake.get("due_at").cloned().unwrap_or(Value::Null);
                    wake.insert("paused_dependency_due_at".to_string(), due);
                }
                let held_through = wake.contains_key("paused_dependency_due_at");
                let has_result = truthy(wake.get("dependency_result"));
                let awaited = action == "resume"
                    && truthy(wake.get("dependency_key"))
                    && held_through
                    && !has_result;
                let paused_due = if matches!(action, "resume" | "cancel" | "supersede") {
                    wake.remove("paused_dependency_due_at")
                } else {
                    None
                };
                // A result that was held through the pause is delivered now.
                let held_result = action == "resume"
                    && has_result
                    && paused_due.as_ref().is_some_and(|due| !due.is_null());
                let next_state = match action {
                    "resume" if awaited => "waiting",
                    "resume" => "ready",
                    _ => status,
                };
                let due_at = if action != "resume" {
                    Value::Null
                } else if awaited {
                    let due = paused_due.as_ref().and_then(Value::as_i64).unwrap_or(0);
                    json!(due.max(now + 15_000))
                } else if held_result {
                    json!(now)
                } else {
                    json!(now + 15_000)
                };
                let next = generation(wake) + 1;
                set(
                    wake,
                    json!({
                        "generation": next, "state": next_state, "reason": reason,
                        "observed_state": next_state, "observed_reason": reason,
                        "observed_at": now, "due_at": due_at, "lease_until": null,
                        "request_id": "", "attempts": 0,
                    }),
                );
                plan.status = status.to_string();
                plan.completed_at = matches!(action, "cancel" | "supersede").then_some(now);
                match action {
                    "pause" => freeze_running(&mut plan, now),
                    "resume" => thaw_running(&mut plan, now),
                    "block" => close_running(&mut plan, "blocked", now),
                    _ => close_running(&mut plan, "cancelled", now),
                }
            }
            "rebind" => {
                let native = world.native_session;
                if plan.status != "paused"
                    || reason.is_empty()
                    || native.is_empty()
                    || data.get("native_session_id").and_then(Value::as_str) != Some(native)
                {
                    return refuse(
                        "pause first, then explicitly name the verified current native \
                         conversation and reason",
                    );
                }
                goal.fields
                    .insert("native_session_id".to_string(), json!(native));
                let wake = &mut goal.continuation;
                let next = generation(wake) + 1;
                set(
                    wake,
                    json!({
                        "generation": next, "request_id": "", "lease_until": null,
                        "reason": "Owner conversation rebound; explicitly resume when authorized",
                    }),
                );
            }
            "document" => documents = self.written(plan_id, &json!([data]), now)?,
            "checkpoint" => {
                let paused = plan.status == "paused";
                if !matches!(plan.status.as_str(), "active" | "paused") {
                    return Err(Refused(if plan.status == "blocked" {
                        format!(
                            "resume goal before checkpointing: it is blocked; \
                             `clarp-goal resume {plan_id} {} '{{\"reason\":\"...\"}}'`",
                            plan.revision
                        )
                    } else {
                        format!("the goal is {} and takes no checkpoint", plan.status)
                    }));
                }
                if paused && truthy(data.get("continuation")) {
                    return refuse(
                        "Nothing was saved: the goal is paused, so a checkpoint records progress \
                         and evidence only. Send it again without continuation, or resume the \
                         goal first",
                    );
                }
                let progress = stripped(data.get("progress"));
                let next_work = stripped(data.get("next_work"));
                if progress.is_empty() || next_work.is_empty() {
                    return refuse("checkpoint requires progress and next work");
                }
                let evidence = data.get("evidence").filter(|e| is_true(e)).cloned();
                if let Some(Value::Object(evidence)) = &evidence {
                    let criteria = goal
                        .fields
                        .get_mut("criteria")
                        .and_then(Value::as_array_mut);
                    let mut none = Vec::new();
                    let criteria = criteria.unwrap_or(&mut none);
                    for (id, proof) in evidence {
                        let criterion = criteria
                            .iter_mut()
                            .find(|criterion| criterion["id"].as_str() == Some(id.as_str()));
                        match criterion.and_then(Value::as_object_mut) {
                            Some(criterion) if !text(proof).trim().is_empty() => {
                                criterion.insert("evidence".to_string(), json!(text(proof)));
                            }
                            _ => return refuse("unknown criterion or empty evidence"),
                        }
                    }
                }
                let changes = data.get("documents").filter(|d| is_true(d)).cloned();
                documents = self.written(plan_id, &changes.unwrap_or_else(|| json!([])), now)?;
                // While the goal is paused, how it goes on is the pause's to say.
                let given = data.get("continuation").filter(|c| is_true(c) && !paused);
                let wake_in = given.cloned().unwrap_or_else(|| json!({}));
                let kind = wake_in
                    .get("kind")
                    .map_or_else(|| "timer".to_string(), text);
                let kind = kind.as_str();
                if !matches!(kind, "timer" | "dependency" | "blocked") {
                    return refuse("invalid continuation kind");
                }
                let due = wake_in.get("due_at").and_then(Value::as_i64);
                let due = due.filter(|due| *due != 0).unwrap_or(now + 120_000);
                let wake_reason = stripped(wake_in.get("reason"));
                let wake_key = stripped(wake_in.get("key"));
                if kind == "dependency"
                    && (wake_key.is_empty() || wake_reason.is_empty() || due <= now)
                {
                    return refuse("dependency requires key, wait reason and future timeout");
                }
                if kind == "blocked" && wake_reason.is_empty() {
                    return refuse("blocker requires a reason");
                }
                let job_handle = plain(wake_in.get("job_handle"));
                if !job_handle.is_empty() {
                    let (job_id, job_generation) = parse_handle(&job_handle);
                    if kind != "dependency" || !(world.has_job)(&job_id, job_generation) {
                        return refuse(
                            "background dependency must match this owner and job generation",
                        );
                    }
                }
                let proof = evidence.unwrap_or_else(|| json!({}));
                goal.fields.insert(
                    "checkpoint".to_string(),
                    json!({ "at": now, "progress": progress, "next_work": next_work, "evidence": proof }),
                );
                let now_state = goal.state().to_string();
                let wake = &mut goal.continuation;
                // What the goal still waits for is never dropped as a side effect.
                let pending = truthy(wake.get("dependency_key"))
                    && (now_state == "waiting"
                        || (now_state == "blocked"
                            && wake.contains_key("paused_dependency_due_at")));
                let awaited_key = plain(wake.get("dependency_key"));
                let awaited_job = plain(wake.get("job_handle"));
                let awaited = json!({ "key": awaited_key, "job_handle": awaited_job });
                let replaces = kind == "dependency"
                    || wake_in.get("replace_dependency") == Some(&Value::Bool(true));
                let next = generation(wake) + 1;
                let mut replaced: Option<Value> = None;
                if paused {
                    // The pause has the continuation; the resume decides the next wake.
                } else if pending && given.is_none() {
                    set(
                        wake,
                        json!({ "generation": next, "request_id": "", "lease_until": null, "attempts": 0 }),
                    );
                    let mut kept = awaited;
                    kept["due_at"] = wake.get("due_at").cloned().unwrap_or(Value::Null);
                    data["dependency_kept"] = kept;
                } else if pending && kind == "timer" && !replaces {
                    let named = if awaited_job.is_empty() {
                        awaited_key
                    } else {
                        format!("{awaited_key} (job {awaited_job})")
                    };
                    return Err(Refused(format!(
                        "Nothing was saved: goal is waiting on dependency {named}, and a timer \
                         would replace that wake. Send the checkpoint again: omit continuation \
                         to keep waiting for it; to be woken earlier, send the dependency \
                         continuation again with an earlier due_at; to stop waiting for it \
                         (also when it has already ended), add \"replace_dependency\": true"
                    )));
                } else if pending && kind == "blocked" {
                    // Held as a pause holds it: the resume waits for it again.
                    if !wake.contains_key("paused_dependency_due_at") {
                        let due = wake.get("due_at").cloned().unwrap_or(Value::Null);
                        wake.insert("paused_dependency_due_at".to_string(), due);
                    }
                    set(
                        wake,
                        json!({
                            "generation": next, "state": "blocked", "reason": wake_reason,
                            "due_at": null, "request_id": "", "lease_until": null, "attempts": 0,
                        }),
                    );
                } else {
                    if pending && wake_key != awaited_key {
                        let by = if kind == "dependency" {
                            format!("dependency {wake_key}")
                        } else {
                            kind.to_string()
                        };
                        let mut detail = awaited;
                        detail["by"] = json!(by);
                        detail["reason"] = json!(format!(
                            "Checkpoint replaced the pending dependency on {awaited_key}"
                        ));
                        replaced = Some(detail);
                    }
                    wake.remove("paused_dependency_due_at");
                    let next_state = match kind {
                        "dependency" => "waiting",
                        "blocked" => "blocked",
                        _ => "ready",
                    };
                    let reason = if wake_reason.is_empty() {
                        "Scheduled continuation"
                    } else {
                        wake_reason.as_str()
                    };
                    let due_at = if kind == "blocked" {
                        Value::Null
                    } else {
                        json!(now.max(due))
                    };
                    set(
                        wake,
                        json!({
                            "generation": next, "state": next_state, "reason": reason,
                            "due_at": due_at, "job_handle": job_handle,
                            "dependency_key": wake_key, "dependency_result": null,
                            "request_id": "", "lease_until": null, "attempts": 0,
                        }),
                    );
                }
                if let Some(detail) = replaced {
                    remember(
                        &mut plan,
                        &mut goal,
                        "dependency_replaced",
                        revision + 1,
                        detail,
                        now,
                    );
                }
            }
            "dependency" => {
                let now_state = goal.state().to_string();
                let wake = &mut goal.continuation;
                // A result that comes while the goal is paused or blocked is kept for the
                // resume.
                let held = matches!(now_state.as_str(), "paused" | "blocked")
                    && wake.contains_key("paused_dependency_due_at")
                    && !truthy(wake.get("dependency_result"));
                let awaited = wake.get("dependency_key").unwrap_or(&Value::Null);
                if (now_state != "waiting" && !held)
                    || data.get("key").unwrap_or(&Value::Null) != awaited
                {
                    return refuse("dependency is stale or does not belong to this goal");
                }
                let outcome = data.get("outcome").and_then(Value::as_str).unwrap_or("");
                if !matches!(outcome, "succeeded" | "failed") || !truthy(data.get("evidence")) {
                    return refuse("external result requires outcome and evidence");
                }
                if held {
                    wake.insert("dependency_result".to_string(), data.clone());
                } else {
                    set(
                        wake,
                        json!({
                            "state": "ready", "due_at": now,
                            "reason": format!("External work {outcome}"),
                            "dependency_result": data.clone(), "request_id": "",
                        }),
                    );
                }
            }
            "replan" => {
                if reason.is_empty() {
                    return refuse("replanning requires a discovery or reason");
                }
                let proposed = data.get("steps").and_then(Value::as_array);
                let Some(proposed) = proposed.filter(|steps| steps.len() <= MAX_PLAN_ITEMS) else {
                    return refuse("replan requires a list of at most 500 steps");
                };
                let old: Vec<String> = plan.items.iter().map(|i| i.item_id.clone()).collect();
                let before: Vec<Value> = plan.items.iter().map(Item::row).collect();
                let detail = json!({ "items": before });
                remember(
                    &mut plan,
                    &mut goal,
                    "prior_steps",
                    revision + 1,
                    detail,
                    now,
                );
                let mut keys: Vec<String> = Vec::new();
                let empty = Map::new();
                for (position, raw) in proposed.iter().enumerate() {
                    let raw = raw.as_object().unwrap_or(&empty);
                    let key = item_key(plan_id, &plain(raw.get("id")), world.token_hex);
                    if keys.contains(&key) {
                        return refuse("duplicate step id");
                    }
                    keys.push(key.clone());
                    let Some(existing) = plan.items.iter_mut().find(|item| item.item_id == key)
                    else {
                        plan.items
                            .push(new_item(plan_id, key, None, position, raw, now)?);
                        continue;
                    };
                    let title = clip(&stripped(raw.get("title")), 500);
                    if existing.status == "completed" && title != existing.title {
                        return refuse(
                            "completed step meaning is immutable; use a new step id for \
                             different work",
                        );
                    }
                    if title.is_empty() {
                        return refuse("step title required");
                    }
                    existing.title = title;
                    existing.position = position as i64;
                    existing.parent_id = None;
                    if existing.status == "removed" {
                        // A retired step that is proposed again is back in the plan.
                        existing.status = "pending".to_string();
                        existing.detail = clip(&plain(raw.get("detail")), 2_000);
                        existing.started_at = None;
                        existing.completed_at = None;
                    }
                }
                for item in &mut plan.items {
                    if old.contains(&item.item_id)
                        && !keys.contains(&item.item_id)
                        && !matches!(item.status.as_str(), "completed" | "removed")
                    {
                        if let Some(started) = item.started_at.take() {
                            item.active_ms += (now - started).max(0);
                        }
                        item.status = "removed".to_string();
                        item.detail.clone_from(&reason);
                        item.completed_at = Some(now);
                    }
                }
                let now_state = goal.state().to_string();
                let wake = &mut goal.continuation;
                let next = generation(wake) + 1;
                set(
                    wake,
                    json!({ "generation": next, "request_id": "", "lease_until": null }),
                );
                if !matches!(now_state.as_str(), "blocked" | "waiting") {
                    set(
                        wake,
                        json!({
                            "state": "ready", "due_at": now + 120_000,
                            "reason": "Plan revised; reassess current results",
                        }),
                    );
                }
            }
            // A criterion that will not be met as it was written can be set aside, with a
            // reason, so that the goal can end honestly instead of being cancelled. The
            // waiver is shown, never hidden. Only the user sets one: an agent that could
            // waive its own criteria could complete any goal without evidence.
            "waive" | "unwaive" => {
                if !by_user {
                    return refuse("only the user can waive or restore a criterion");
                }
                let wanted = plain(data.get("criterion_id"));
                let criterion = goal
                    .fields
                    .get_mut("criteria")
                    .and_then(Value::as_array_mut)
                    .and_then(|criteria| {
                        criteria
                            .iter_mut()
                            .find(|criterion| criterion["id"].as_str() == Some(wanted.as_str()))
                    })
                    .and_then(Value::as_object_mut);
                let Some(criterion) = criterion else {
                    return refuse("unknown criterion");
                };
                if action == "waive" {
                    if reason.is_empty() {
                        return refuse("waiving a criterion requires a reason");
                    }
                    criterion.insert(
                        "waived".to_string(),
                        json!({ "at": now, "reason": clip(&reason, 2000), "by": "user" }),
                    );
                } else {
                    if !truthy(criterion.get("waived")) {
                        return refuse("criterion is not waived");
                    }
                    criterion.shift_remove("waived");
                }
            }
            "add_step" => {
                if reason.is_empty() {
                    return refuse("plan adaptation requires a reason");
                }
                let given = match plain(data.get("id")) {
                    given if !given.is_empty() => given,
                    _ => (world.token_hex)(4),
                };
                let key = item_key(plan_id, &given, world.token_hex);
                let position = plan.items.len();
                if position >= MAX_PLAN_ITEMS {
                    return refuse("too many steps");
                }
                let empty = Map::new();
                let raw = data.as_object().unwrap_or(&empty);
                plan.items
                    .push(new_item(plan_id, key, None, position, raw, now)?);
            }
            _ => {
                return Err(Refused(format!(
                    "unknown goal action {}: use one of {} \
                 (and `clarp-goal complete PLAN REVISION` to finish a goal)",
                    crate::python::text(action),
                    GOAL_ACTIONS.join(", ")
                )));
            }
        }
        if plan.recovery_enabled == 0 {
            let now_state = goal.state().to_string();
            let wake = &mut goal.continuation;
            wake.insert("due_at".to_string(), Value::Null);
            if matches!(
                now_state.as_str(),
                "ready" | "dispatching" | "admitted" | "queued" | "retry"
            ) {
                set(
                    wake,
                    json!({
                        "state": "not_enrolled",
                        "reason": "Automatic continuation is not enrolled",
                        "observed_state": "not_enrolled",
                        "observed_reason": "Automatic continuation is not enrolled",
                    }),
                );
            }
        }
        // What a document says is in the document, not in the history a second time.
        if let Some(fields) = data.as_object_mut() {
            fields.remove("content");
            let named = fields.remove("documents").filter(is_true);
            if let Some(Value::Array(changes)) = named {
                let names: Vec<Value> = changes.iter().map(|d| d["name"].clone()).collect();
                fields.insert("documents".to_string(), json!(names));
            }
        }
        remember(&mut plan, &mut goal, action, revision + 1, data, now);
        plan.goal = Some(goal);
        plan.revision += 1;
        plan.updated_at = now;
        Ok(Change { plan, documents })
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    const VERA: Owner<'static> = Owner {
        agent_id: "a1",
        session: "vera",
    };

    fn world<'a>(now_ms: i64, ids: &'a mut dyn FnMut(usize) -> String) -> World<'a> {
        World {
            now_ms,
            native_session: "",
            queue_revision: 0,
            token_hex: ids,
            has_job: &|_, _| false,
        }
    }

    fn counter() -> impl FnMut(usize) -> String {
        let mut made = 0u64;
        move |bytes| {
            made += 1;
            format!("{made:0width$x}", width = bytes * 2)
        }
    }

    /// Does to the book what the Host does with a change: writes its record, takes it.
    fn keep(book: &mut GoalBook, change: &Change, now: i64) -> GoalRecord {
        let record = book.record(change, now);
        let read: GoalRecord =
            serde_json::from_slice(&serde_json::to_vec(&record).unwrap()).unwrap();
        assert_eq!(read, record, "a record is read as it was written");
        book.apply(&read);
        let mut kept = book.plan(&change.plan.plan_id).cloned().unwrap();
        kept.order = change.plan.order;
        assert_eq!(kept, change.plan, "the book has the plan the change left");
        record
    }

    fn a_goal(book: &mut GoalBook, ids: &mut dyn FnMut(usize) -> String) -> String {
        let goal = json!({ "outcome": "It is done", "criteria": ["It runs"], "limits": "None" });
        let items = json!([{ "id": "one", "title": "The first step" }]);
        let change = book
            .create(
                VERA,
                "ship",
                "Ship it",
                &items,
                Some(&goal),
                &mut world(1_000, ids),
            )
            .unwrap();
        keep(book, &change, 1_000);
        change.plan.plan_id
    }

    #[test]
    fn a_goal_refusal_names_the_next_command_at_its_current_revision() {
        let mut ids = counter();
        let mut book = GoalBook::new();
        let made = book
            .create(
                VERA,
                "ship",
                "Ship it",
                &json!([
                    { "id": "build", "title": "Build" },
                    { "id": "note", "title": "Write a note", "required": false },
                ]),
                Some(
                    &json!({ "outcome": "It is done", "criteria": ["It runs"], "limits": "None" }),
                ),
                &mut world(1_000, &mut ids),
            )
            .unwrap();
        keep(&mut book, &made, 1_000);
        let Plan {
            plan_id, revision, ..
        } = &made.plan;
        let build = format!("{plan_id}:build");
        assert_eq!(
            book.step(&build, None, "completed", None, 2_000)
                .unwrap_err()
                .0,
            format!(
                "revision required for durable goal {plan_id} (now revision {revision}): run `clarp-goal step {plan_id} {revision} build completed`"
            ),
        );
        assert_eq!(
            book.finish(plan_id, "completed", "", None, 2_000)
                .unwrap_err()
                .0,
            format!(
                "revision required for durable goal {plan_id} (now revision {revision}): run `clarp-goal complete {plan_id} {revision}`"
            ),
        );
        assert_eq!(
            book.finish(plan_id, "completed", "", Some(*revision), 2_000)
                .unwrap_err()
                .0,
            format!(
                "required work remains unresolved: build; settle each with `clarp-goal step {plan_id} {revision} ITEM completed` (or removed, with a reason)"
            ),
        );
        let built = book
            .step(&build, Some(*revision), "completed", None, 2_000)
            .unwrap();
        keep(&mut book, &built, 2_000);
        let running = book
            .step(
                &format!("{plan_id}:note"),
                Some(built.plan.revision),
                "in_progress",
                None,
                3_000,
            )
            .unwrap();
        keep(&mut book, &running, 3_000);
        let current = running.plan.revision;
        assert_eq!(
            book.finish(plan_id, "completed", "", Some(current), 4_000)
                .unwrap_err()
                .0,
            format!(
                "running work must be resolved or deferred before completion: note; `clarp-goal step {plan_id} {current} ITEM completed` (or deferred, with a reason)"
            ),
        );
        assert_eq!(book.plan(plan_id).unwrap().revision, current);
    }

    #[test]
    fn a_goal_follows_the_conversation_its_host_carried_over() {
        let mut ids = counter();
        let mut book = GoalBook::new();
        let goal = json!({ "outcome": "It is done", "criteria": ["It runs"], "limits": "None" });
        let items = json!([{ "id": "one", "title": "The first step" }]);
        // Two goals made in the agent's conversation "old", and one made in another.
        let mut made = |book: &mut GoalBook, name: &str, native: &str, at: i64| {
            let mut seen = World {
                native_session: native,
                ..world(at, &mut ids)
            };
            let change = book
                .create(VERA, name, "Ship it", &items, Some(&goal), &mut seen)
                .unwrap();
            keep(book, &change, at);
            change.plan.plan_id
        };
        let first = made(&mut book, "ship", "old", 1_000);
        let second = made(&mut book, "mend", "old", 1_001);
        let other = made(&mut book, "else", "another", 1_002);
        let bound = |book: &GoalBook, id: &str| {
            let goal = book.plan(id).unwrap().goal.clone().unwrap();
            goal.fields.get("native_session_id").cloned()
        };
        assert_eq!(bound(&book, &first), Some(json!("old")));
        // A wake is out for the first as it stands, and one for the second as it stood before.
        let revision = book.plan(&first).unwrap().revision;
        for (id, waits_for) in [(&first, revision), (&second, revision - 1)] {
            let plan = book.plans.get_mut(id.as_str()).unwrap();
            let goal = plan.goal.as_mut().unwrap();
            goal.continuation
                .insert("plan_revision".to_string(), json!(waits_for));
        }
        // Nothing to carry from, nothing to carry to, a conversation no goal is bound to:
        // nothing moves.
        assert!(book.carried("", "new", "why", 2_000).is_empty());
        assert!(book.carried("old", "", "why", 2_000).is_empty());
        assert!(book.carried("gone", "new", "why", 2_000).is_empty());
        let moved = book.carried("old", "new", "The tool let go of it", 2_000);
        let which: Vec<&str> = moved
            .iter()
            .map(|change| change.plan.plan_id.as_str())
            .collect();
        assert_eq!(which, [first.as_str(), second.as_str()]);
        // What is written down of it is read back as the same plans.
        for change in &moved {
            keep(&mut book, change, 2_000);
        }
        let plan = book.plan(&first).unwrap();
        let goal = plan.goal.as_ref().unwrap();
        assert_eq!((plan.revision, plan.updated_at), (revision + 1, 2_000));
        assert_eq!(goal.fields["native_session_id"], "new");
        // The wake that was out for the plan as it stood is the plan's still.
        assert_eq!(goal.continuation["plan_revision"], revision + 1);
        assert_eq!(
            goal.history.last().unwrap(),
            &json!({
                "at": 2_000, "kind": "rebind", "revision": revision + 1,
                "detail": {
                    "reason": "The tool let go of it",
                    "from_native_session_id": "old", "native_session_id": "new",
                },
            })
        );
        // One that was out for an earlier revision is as stale as it was.
        let goal = book.plan(&second).unwrap().goal.as_ref().unwrap();
        assert_eq!(goal.continuation["plan_revision"], revision - 1);
        assert_eq!(goal.fields["native_session_id"], "new");
        // A goal of another conversation stays where it is, and nothing follows twice.
        assert_eq!(bound(&book, &other), Some(json!("another")));
        assert!(book.carried("old", "new", "why", 3_000).is_empty());
    }

    #[test]
    fn a_plan_that_moves_with_its_agent_is_the_same_plan_where_it_arrives() {
        // A plan with a goal, steps under steps, a checkpoint and a document, on one Host.
        let mut ids = counter();
        let mut there = GoalBook::new();
        let goal = json!({ "outcome": "It is done", "criteria": ["It runs"], "limits": "None" });
        let items = json!([
            { "id": "one", "title": "The first step", "subtasks": [{ "id": "one-a", "title": "A part of it" }] },
            { "id": "two", "title": "The second step" },
        ]);
        let change = there
            .create(
                VERA,
                "ship",
                "Ship it",
                &items,
                Some(&goal),
                &mut world(1_000, &mut ids),
            )
            .unwrap();
        keep(&mut there, &change, 1_000);
        let plan_id = change.plan.plan_id.clone();
        let revision = there.plan(&plan_id).unwrap().revision;
        let data = json!({
            "progress": "Half of it", "next_work": "The other half",
            "continuation": { "kind": "timer", "due_at": 9_000 },
            "documents": [{ "name": "notes.md", "format": "markdown", "content": "# Notes",
                            "document_revision": 0, "reason": "the first" }],
        });
        let change = there
            .act(
                &plan_id,
                revision,
                "checkpoint",
                &data,
                &mut world(2_000, &mut ids),
            )
            .unwrap();
        keep(&mut there, &change, 2_000);
        let shown = there.get(&plan_id, 5_000).unwrap();
        let document = there.read(&plan_id, "notes.md", None).unwrap();

        // It arrives with its agent: as it was shown there, it is shown here.
        let mut here = GoalBook::new();
        let arrived = here
            .taken_over(VERA, &shown, std::slice::from_ref(&document))
            .unwrap();
        let record = here.record(&arrived, 6_000);
        let read: GoalRecord =
            serde_json::from_slice(&serde_json::to_vec(&record).unwrap()).unwrap();
        here.apply(&read);
        assert_eq!(here.get(&plan_id, 5_000).unwrap(), shown);
        assert_eq!(here.read(&plan_id, "notes.md", None).unwrap(), document);
        // And it goes on here from where it stood: the next change follows its revision.
        let revision = shown["revision"].as_i64().unwrap();
        let next = json!({ "progress": "All of it", "next_work": "Nothing",
                           "continuation": { "kind": "timer", "due_at": 20_000 } });
        let change = here
            .act(
                &plan_id,
                revision,
                "checkpoint",
                &next,
                &mut world(7_000, &mut ids),
            )
            .unwrap();
        assert_eq!(change.plan.revision, revision + 1);

        // What is not this agent's plan, or is here already, does not arrive.
        let other = Owner {
            agent_id: "b2",
            session: "omar",
        };
        let refused = GoalBook::new().taken_over(other, &shown, &[]).unwrap_err();
        assert_eq!(refused.0, "the plan is not this agent's");
        let again = here.taken_over(VERA, &shown, &[]).unwrap_err();
        assert_eq!(again.0, "the agent has this plan already");
    }

    #[test]
    fn the_log_keeps_what_a_change_set_and_not_the_whole_goal_again() {
        let mut ids = counter();
        let mut book = GoalBook::new();
        let plan_id = a_goal(&mut book, &mut ids);
        let checkpoint = json!({ "progress": "Half way", "next_work": "The other half" });
        let change = book
            .act(
                &plan_id,
                0,
                "checkpoint",
                &checkpoint,
                &mut world(2_000, &mut ids),
            )
            .unwrap();
        let record = keep(&mut book, &change, 2_000);
        assert!(record.set.contains_key("checkpoint"));
        assert!(
            !record.set.contains_key("criteria"),
            "the criteria did not change"
        );
        assert!(matches!(&record.history, Grown::By(added) if added.len() == 1));
        assert!(record.items.is_empty(), "no step changed");

        let item = format!("{plan_id}:one");
        let change = book
            .step(&item, Some(1), "in_progress", None, 3_000)
            .unwrap();
        let record = keep(&mut book, &change, 3_000);
        assert!(record.set.is_empty() && record.continuation.is_none());
        assert_eq!(record.items.len(), 1);
    }

    #[test]
    fn a_goal_carries_its_newest_history_and_counts_the_rest() {
        let mut ids = counter();
        let mut book = GoalBook::new();
        let plan_id = a_goal(&mut book, &mut ids);
        let checkpoint = json!({ "progress": "On", "next_work": "And on" });
        for revision in 0..(GOAL_HISTORY_LIMIT as i64 + 20) {
            let now = 2_000 + revision;
            let change = book
                .act(
                    &plan_id,
                    revision,
                    "checkpoint",
                    &checkpoint,
                    &mut world(now, &mut ids),
                )
                .unwrap();
            let record = keep(&mut book, &change, now);
            assert!(matches!(&record.history, Grown::By(added) if added.len() == 1));
        }
        let goal = book.plan(&plan_id).unwrap().goal.as_ref().unwrap();
        assert_eq!(goal.history.len(), GOAL_HISTORY_LIMIT);
        assert_eq!(goal.history_offset, 21);
        let read = book.get(&plan_id, 9_000).unwrap();
        assert_eq!(read["goal"]["history_offset"], json!(21));
        assert_eq!(
            read["ledger"]["event_count"],
            json!(GOAL_HISTORY_LIMIT + 21)
        );
    }

    #[test]
    fn a_history_that_did_not_only_grow_is_kept_whole() {
        let old = vec![json!(1), json!(2), json!(3)];
        let longer = [json!(1), json!(2), json!(3), json!(4)];
        let grown = Grown::between((&old[..], 0), (&longer[..], 0), 10);
        assert_eq!(grown, Grown::By(vec![json!(4)]));
        let rewritten = vec![json!(1), json!("two"), json!(3), json!(4)];
        let grown = Grown::between((&old[..], 0), (&rewritten[..], 0), 10);
        assert_eq!(grown, Grown::To(rewritten.clone(), 0));
        let shorter = vec![json!(3)];
        let grown = Grown::between((&old[..], 0), (&shorter[..], 0), 10);
        assert_eq!(grown, Grown::To(shorter.clone(), 0));
        let mut entries = old.clone();
        let mut offset = 0;
        Grown::By(vec![json!(4), json!(5)]).onto(&mut entries, &mut offset, 4);
        assert_eq!(entries, vec![json!(2), json!(3), json!(4), json!(5)]);
        assert_eq!(offset, 1);
    }

    #[test]
    fn a_plan_without_a_goal_is_done_when_its_steps_are() {
        let mut ids = counter();
        let mut book = GoalBook::new();
        let items = json!([{ "id": "one", "title": "The only step" }]);
        let change = book
            .create(
                VERA,
                "",
                "A list",
                &items,
                None,
                &mut world(1_000, &mut ids),
            )
            .unwrap();
        keep(&mut book, &change, 1_000);
        let plan_id = change.plan.plan_id.clone();
        assert_eq!(plan_id, "a1:work:00000001");
        let item = format!("{plan_id}:one");
        let change = book.step(&item, None, "completed", None, 2_000).unwrap();
        let record = keep(&mut book, &change, 2_000);
        assert_eq!(change.plan.status, "completed");
        assert!(matches!(&record.plan_history, Grown::By(added) if added.len() == 1));
        let read = book.get(&plan_id, 3_000).unwrap();
        assert_eq!(read["legacy"], json!(true));
        assert_eq!(read["goal"], Value::Null);
        assert_eq!(
            book.show(3_000),
            json!({}),
            "nothing is being worked any more"
        );
    }
}
