//! When the owner of a goal is woken for it.
//!
//! A goal whose owner asked to be woken for it (it is "enrolled") says in its continuation
//! how the work goes on: at a time, when a background job ends, or not until someone
//! unblocks it. Whoever holds the agent looks at its goals whenever something changed and
//! when the next one is due, and wakes the owner with a message when a goal is due and the
//! owner rests. A wake that is not followed by a checkpoint is tried again, later each time,
//! and after the goal's limit of tries a person has to look.
//!
//! The rules are those of the Python Host's `server/lib/task_goal_recovery.py`, and the
//! message is word for word the one it sends. What that file needs leases and receipts for
//! is not here: on this Host a goal and its owner's mailbox belong to one agent and are
//! written by one thread in one append, so a wake is sent and recorded, or neither.

use serde_json::{Map, Value, json};

use super::{Change, Goal, GoalBook, Plan, World, generation, plain, remember, set, truthy};
use crate::jobs::{Job, parse_handle};
use crate::python::dumps as python_json;

/// What the id of a wake begins with: of the message it is sent as, and of its trace.
pub const WAKE_PREFIX: &str = "task-goal-";
/// Failures that say nothing about the goal: no account had usage left. They do not use up
/// the goal's tries.
const CAPACITY_CODES: [&str; 1] = ["usage_limit"];
/// A goal none of whose wakes has completed for this long goes to attention whatever the
/// reason. Eight days, because a usage window lasts up to a week.
const WAKE_COMPLETION_CEILING_MS: i64 = 8 * 24 * 3_600_000;
/// The states in which a goal is left alone.
const AT_REST: [&str; 3] = ["blocked", "attention", "not_enrolled"];
const TRUNCATED: &str = " [preview truncated; read the current goal]";

/// Failures only a person can clear, and what that person has to do.
fn person_has_to(code: &str) -> Option<&'static str> {
    match code {
        "auth" => Some("a person has to sign in to the account again"),
        "account_plan" => Some(
            "a person has to switch to an account whose plan includes the model, \
             or choose another model",
        ),
        "billing" => {
            Some("a person has to pay or raise the limit, or pick another model or account")
        }
        _ => None,
    }
}

/// How a turn of the owner ended.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct TurnSeen {
    /// `completed`, `failed` or `interrupted`.
    pub outcome: String,
    /// Its tool was started.
    pub started: bool,
    pub ended_at_ms: i64,
    /// The category of a failure, when it has one: `usage_limit`, `auth` and the like.
    pub code: String,
    /// The failure in words.
    pub message: String,
}

/// What is seen around a goal when it is looked at.
pub struct Seen<'a> {
    pub now_ms: i64,
    /// The name the owner's session has now.
    pub session: &'a str,
    /// The owner's conversation with its tool as it is bound now, or empty.
    pub native_session: &'a str,
    /// Why the owner is not to be woken now, as a code and a sentence. `None`: it rests.
    pub busy: Option<(&'a str, &'a str)>,
    /// The owner's last turn that ended, when none runs.
    pub rested: Option<&'a TurnSeen>,
    /// The owner's background job with this id.
    pub job: &'a dyn Fn(&str) -> Option<&'a Job>,
    /// How the turn of the wake with this id ended, once it has.
    pub wake: &'a dyn Fn(&str) -> Option<TurnSeen>,
    pub token_hex: &'a mut dyn FnMut(usize) -> String,
}

/// What looking at a goal comes to.
#[derive(Debug, Clone, PartialEq)]
pub enum Looked {
    Nothing,
    /// Something about the goal is to be written. Nobody is woken.
    Noted(Change),
    /// The owner is to be woken with this message: it and the change are written together.
    Wake {
        change: Change,
        request_id: String,
        prompt: String,
    },
}

/// A wake that was sent and did not run to its end.
struct Undelivered {
    request_id: String,
    outcome: String,
    started: bool,
    code: String,
    reason: String,
}

fn number(state: &Map<String, Value>, field: &str) -> i64 {
    state.get(field).and_then(Value::as_i64).unwrap_or(0)
}

fn state_of(state: &Map<String, Value>) -> &str {
    state.get("state").and_then(Value::as_str).unwrap_or("")
}

/// A value for a prompt: all of it when it is short, its beginning when it is not.
fn preview(text: &str, limit: usize) -> String {
    if text.chars().count() <= limit {
        text.to_string()
    } else {
        format!("{}{TRUNCATED}", super::clip(text, limit))
    }
}

/// A word as a shell takes it for one word: as it is when nothing in it means anything to
/// a shell, in single quotes otherwise.
fn shell_quote(word: &str) -> String {
    let safe = |c: char| c.is_ascii_alphanumeric() || "_@%+=:,./-".contains(c);
    if !word.is_empty() && word.chars().all(safe) {
        word.to_string()
    } else {
        format!("'{}'", word.replace('\'', "'\"'\"'"))
    }
}

/// The hour and minute of a moment. In UTC, and said so: the kernel knows no time zone.
fn hour_and_minute(at_ms: i64) -> String {
    let minutes = at_ms.div_euclid(60_000).rem_euclid(24 * 60);
    format!("{:02}:{:02} UTC", minutes / 60, minutes % 60)
}

/// When the goal is woken next, in words.
fn next_try(state: &Map<String, Value>, now: i64) -> String {
    match number(state, "due_at") {
        due if due <= now => "now".to_string(),
        due => hour_and_minute(due),
    }
}

/// The last wake that did not complete, in words.
fn last_wake(state: &Map<String, Value>) -> String {
    let code = plain(state.get("undelivered_code"));
    let how = if CAPACITY_CODES.contains(&code.as_str()) {
        "found no account with usage"
    } else if truthy(state.get("undelivered_started")) {
        "started and failed"
    } else {
        "did not start"
    };
    format!(
        "the last wake {how} ({})",
        plain(state.get("undelivered_reason"))
    )
}

/// How the wake that was last sent ended without completing, or nothing while it waits,
/// runs, completed, or was already noted.
fn undelivered(state: &Map<String, Value>, seen: &Seen<'_>) -> Option<Undelivered> {
    let request = plain(state.get("request_id"));
    if !matches!(state_of(state), "admitted" | "queued" | "retry")
        || request.is_empty()
        || plain(state.get("undelivered_request_id")) == request
    {
        return None;
    }
    let turn = (seen.wake)(&request).filter(|turn| turn.outcome != "completed")?;
    let code = match turn.code.as_str() {
        "" if turn.started => "failed".to_string(),
        "" => "not_started".to_string(),
        code => code.to_string(),
    };
    let reason = match turn.message.as_str() {
        "" => format!("Its turn {}", turn.outcome),
        message => message.to_string(),
    };
    Some(Undelivered {
        request_id: request,
        outcome: turn.outcome,
        started: turn.started,
        code,
        reason,
    })
}

/// Why the goal's owner may not be woken for it now, or nothing.
fn boundary<'a>(plan: &Plan, goal: &Goal, seen: &Seen<'a>) -> Option<(&'a str, &'a str)> {
    if seen.session != plan.session || goal.text("owner_agent_id") != plan.agent_id {
        return Some((
            "owner_changed",
            "Owner binding changed; explicit re-enrollment required",
        ));
    }
    if seen.native_session.is_empty() || seen.native_session != goal.text("native_session_id") {
        return Some((
            "owner_changed",
            "Native conversation changed; explicit re-enrollment required",
        ));
    }
    seen.busy
}

/// Whether a goal differs from what is stored in more than the stamp of the latest look.
fn changed_beyond_observation(stored: &Goal, goal: &Goal) -> bool {
    let without_stamp = |state: &Map<String, Value>| {
        let mut state = state.clone();
        state.remove("observed_at");
        state.remove("observed_reason");
        state
    };
    stored.fields != goal.fields
        || stored.history != goal.history
        || stored.history_offset != goal.history_offset
        || without_stamp(&stored.continuation) != without_stamp(&goal.continuation)
}

fn noted(mut plan: Plan, goal: Goal, write: bool) -> Looked {
    if !write {
        return Looked::Nothing;
    }
    plan.goal = Some(goal);
    Looked::Noted(Change {
        plan,
        documents: Vec::new(),
    })
}

impl GoalBook {
    /// The goals whose owner is woken for them: enrolled, and being worked.
    pub fn watched(&self) -> impl Iterator<Item = &Plan> {
        self.plans.values().filter(|plan| {
            plan.status == "active" && plan.recovery_enabled != 0 && plan.goal.is_some()
        })
    }

    /// The earliest moment a watched goal is due, for whoever sets the alarm.
    pub fn next_due(&self) -> Option<i64> {
        self.watched()
            .filter_map(|plan| plan.goal.as_ref())
            .filter(|goal| !AT_REST.contains(&goal.state()))
            .filter_map(|goal| goal.continuation.get("due_at").and_then(Value::as_i64))
            .min()
    }

    /// The message a goal's owner is woken with.
    pub fn wake_prompt(&self, plan_id: &str) -> Option<String> {
        let plan = self.plans.get(plan_id)?;
        Some(self.prompt(plan, plan.goal.as_ref()?))
    }

    fn prompt(&self, plan: &Plan, goal: &Goal) -> String {
        let field = |from: Option<&Value>, name: &str| plain(from.and_then(|v| v.get(name)));
        let checkpoint = goal.fields.get("checkpoint").filter(|c| c.is_object());
        let at = match checkpoint.and_then(|checkpoint| checkpoint.get("at")) {
            Some(at) => plain(Some(at)),
            None => "none".to_string(),
        };
        let wake = &goal.continuation;
        let result = wake.get("dependency_result").unwrap_or(&Value::Null);
        let documents = self.document_index(&plan.plan_id, &[]);
        let index: Vec<Value> = documents
            .iter()
            .take(8)
            .map(|d| json!([preview(&plain(d.get("name")), 128), d["revision"]]))
            .collect();
        let more = if documents.len() > 8 {
            " [index truncated; read the current goal]"
        } else {
            ""
        };
        format!(
            "Continue your durable outcome commitment. Reassess current conditions and fresh \
             results; choose your own next actions, collaborators and wake timing within the \
             original authority. Earlier next-work text is provisional context, not a fixed \
             script. Answering a side question or ending a turn does not complete this goal. \
             Record a checkpoint with evidence and a continuation or explicit blocker before \
             yielding. Never self-approve pending permissions.\n\n\
             Goal: {plan_id} (plan revision {revision}).\n\
             Before acting, read the authoritative outcome, complete limits, criteria and \
             checkpoint: clarp-goal get {quoted}\n\
             The following previews are provisional context; they do not replace the full \
             goal.\n\
             Outcome: {outcome}\n\
             Limits: {limits}\n\
             Checkpoint at: {at}\n\
             Progress: {progress}\n\
             Provisional next work: {next_work}\n\
             Continuation: {reason}\n\
             Dependency result: {result}\n\
             Working documents (name, revision; retrieve with clarp-goal read): {index}{more}",
            plan_id = plan.plan_id,
            revision = plan.revision,
            quoted = shell_quote(&plan.plan_id),
            outcome = preview(&plain(goal.fields.get("outcome")), 1024),
            limits = preview(&plain(goal.fields.get("limits")), 1024),
            progress = preview(&field(checkpoint, "progress"), 1536),
            next_work = preview(&field(checkpoint, "next_work"), 1536),
            reason = preview(&plain(wake.get("reason")), 512),
            result = preview(&python_json(result), 512),
            index = python_json(&Value::Array(index)),
        )
    }

    /// A Stop holds the agent's goals too: every one that is being worked is paused, for
    /// this reason, until someone resumes it.
    pub fn stopped(&self, reason: &str, world: &mut World<'_>) -> Vec<Change> {
        let active: Vec<(&str, i64)> = self
            .plans
            .values()
            .filter(|plan| plan.status == "active" && plan.goal.is_some())
            .map(|plan| (plan.plan_id.as_str(), plan.revision))
            .collect();
        let data = json!({ "reason": reason });
        active
            .into_iter()
            .filter_map(|(plan_id, revision)| {
                self.act(plan_id, revision, "pause", &data, world).ok()
            })
            .collect()
    }

    /// Looks at a goal: notes what happened around it since it was last looked at, and says
    /// whether its owner is to be woken for it now.
    pub fn look(&self, plan_id: &str, seen: &mut Seen<'_>) -> Looked {
        let Some(known) = self.watched().find(|plan| plan.plan_id == plan_id) else {
            return Looked::Nothing;
        };
        let Some(stored) = &known.goal else {
            return Looked::Nothing;
        };
        if AT_REST.contains(&stored.state()) {
            return Looked::Nothing;
        }
        let now = seen.now_ms;
        let mut plan = known.clone();
        let mut goal = stored.clone();
        plan.goal = None;
        let revision = plan.revision;
        let limit = goal
            .fields
            .get("recovery_limit")
            .and_then(Value::as_i64)
            .unwrap_or(5);
        let mut changed = false;

        // What the owner's turn came to, once for each turn that ended after a wake.
        let dispatched = Some(number(&goal.continuation, "last_dispatch_at")).filter(|at| *at != 0);
        if let (Some(dispatched), Some(turn)) = (dispatched, seen.rested)
            && turn.ended_at_ms >= dispatched
            && goal
                .continuation
                .get("execution_observed_at")
                .and_then(Value::as_i64)
                != Some(turn.ended_at_ms)
        {
            let kind = if turn.outcome == "completed" {
                "done"
            } else {
                "interrupted"
            };
            let message = match turn.message.as_str() {
                "" => "Owner turn ended; outcome remains unfinished",
                message => message,
            };
            let execution = json!({ "kind": kind, "reason": turn.code, "message": message });
            set(
                &mut goal.continuation,
                json!({ "last_execution": execution, "execution_observed_at": turn.ended_at_ms }),
            );
            let at = turn.ended_at_ms;
            remember(
                &mut plan,
                &mut goal,
                "execution_observed",
                revision,
                execution,
                at,
            );
            changed = true;
        }

        // A wake that did not run to its end is said once: until then the goal reads as
        // if its owner had been woken.
        if let Some(failed) = undelivered(&goal.continuation, seen) {
            let state = &mut goal.continuation;
            let said = if CAPACITY_CODES.contains(&failed.code.as_str()) {
                // No account had usage: the goal is not at fault, so this try is not one of
                // its tries. It is still tried later each time.
                let failures = number(state, "capacity_failures") + 1;
                state.insert("capacity_failures".to_string(), json!(failures));
                if failed.started {
                    "Wake started and failed: no account with usage".to_string()
                } else {
                    "Wake not delivered: no account with usage".to_string()
                }
            } else {
                let counted = number(state, "attempts") - number(state, "capacity_failures");
                let how = if failed.started {
                    "Wake started and failed: "
                } else {
                    "Wake did not start: "
                };
                format!("{how}{}; attempt {counted} of {limit}", failed.reason)
            };
            // The first failure in a row starts the clock of the ceiling.
            if !state.contains_key("uncompleted_since") {
                state.insert(
                    "uncompleted_since".to_string(),
                    json!(dispatched.unwrap_or(now)),
                );
            }
            let reason = format!("{said}; next try {}", next_try(state, now));
            set(
                state,
                json!({
                    "reason": reason,
                    "undelivered_request_id": failed.request_id,
                    "undelivered_reason": failed.reason,
                    "undelivered_started": failed.started,
                    "undelivered_code": failed.code,
                }),
            );
            let kind = if failed.started {
                "wake_failed"
            } else {
                "wake_undelivered"
            };
            let detail = json!({
                "request_id": failed.request_id, "outcome": failed.outcome,
                "started": failed.started, "code": failed.code, "reason": failed.reason,
            });
            remember(&mut plan, &mut goal, kind, revision, detail, now);
            changed = true;
        }
        // Seen, not inferred: only a wake whose turn completed ends a row of failures.
        if goal.continuation.contains_key("uncompleted_since") {
            let request = plain(goal.continuation.get("request_id"));
            let completed = !request.is_empty()
                && (seen.wake)(&request).is_some_and(|turn| turn.outcome == "completed");
            if completed {
                goal.continuation.remove("uncompleted_since");
                changed = true;
            }
        }

        if let Some((code, reason)) = boundary(known, stored, seen) {
            let state = &mut goal.continuation;
            if state.get("observed_state").and_then(Value::as_str) != Some(code) {
                set(
                    state,
                    json!({ "observed_state": code, "observed_at": now, "observed_reason": reason }),
                );
                changed = true;
            }
            return noted(plan, goal, changed);
        }
        set(
            &mut goal.continuation,
            json!({
                "observed_state": "idle", "observed_at": now, "observed_reason": "Owner is idle",
            }),
        );

        // What the goal waits for may have ended.
        let handle = plain(goal.continuation.get("job_handle"));
        if goal.state() == "waiting" && !handle.is_empty() {
            let (job_id, job_generation) = parse_handle(&handle);
            match (seen.job)(&job_id).filter(|job| job.generation == job_generation) {
                None => set(
                    &mut goal.continuation,
                    json!({
                        "state": "ready", "due_at": now,
                        "reason": "Background dependency disappeared or was superseded; \
                                   inspect current work",
                    }),
                ),
                Some(job) if job.status.is_terminal() => {
                    // How the job ended is not what became of the work: a worker that
                    // vanished leaves that unknown.
                    let evidence = [&job.log_path, &job.terminal_reason, &job.detail]
                        .into_iter()
                        .find(|text| !text.is_empty())
                        .map_or("Recorded job terminal status", String::as_str);
                    let result = json!({
                        "key": goal.continuation.get("dependency_key"),
                        "outcome": job.status.as_str(),
                        "outcome_state": job.outcome_state(),
                        "terminal_reason": job.terminal_reason,
                        "evidence": evidence,
                        "job_handle": handle,
                    });
                    set(
                        &mut goal.continuation,
                        json!({
                            "state": "ready", "due_at": now,
                            "reason": format!("Background job {}", job.status.as_str()),
                            "dependency_result": result,
                        }),
                    );
                    remember(
                        &mut plan,
                        &mut goal,
                        "dependency_result",
                        revision,
                        result,
                        now,
                    );
                }
                Some(_) => {}
            }
        }

        if number(&goal.continuation, "due_at") > now {
            // An owner that rests is seen to rest at every look: writing the goal again only
            // to say when it was last seen would be writing for nothing.
            let write = changed_beyond_observation(stored, &goal);
            return noted(plan, goal, write);
        }
        if goal.state() == "waiting" {
            // A job that still runs is no longer awaited after this wake: that is said, or
            // its end is later expected to wake someone and does not.
            let reason = if handle.is_empty() {
                "External dependency deadline passed; inspect its actual outcome".to_string()
            } else {
                format!(
                    "Background job {} was still running when its deadline passed and is no \
                     longer awaited; to be woken when it ends, name it again as the \
                     dependency in your checkpoint",
                    plain(goal.continuation.get("dependency_key"))
                )
            };
            set(
                &mut goal.continuation,
                json!({ "state": "ready", "reason": reason }),
            );
        }
        let state = &mut goal.continuation;
        let request = plain(state.get("request_id"));
        let spent = !request.is_empty() && plain(state.get("undelivered_request_id")) == request;
        if number(state, "attempts") == 0 {
            // A checkpoint or a resume began anew.
            state.remove("capacity_failures");
            state.remove("uncompleted_since");
        }
        let stalled = state.get("uncompleted_since").and_then(Value::as_i64);
        if stalled.is_some_and(|since| now - since >= WAKE_COMPLETION_CEILING_MS) {
            let reason = format!(
                "No wake of this goal has completed in {} days; {}; a person has to find out \
                 why, then resume the goal",
                WAKE_COMPLETION_CEILING_MS / 86_400_000,
                last_wake(state)
            );
            set(state, json!({ "state": "attention", "reason": reason }));
            return noted(plan, goal, true);
        }
        // Tries that failed for lack of usage are not the goal's failures.
        if number(state, "attempts") - number(state, "capacity_failures") >= limit {
            let mut last = String::new();
            let mut then = "checkpoint or resume to continue".to_string();
            if spent {
                last = format!("; {}", last_wake(state));
                if let Some(what) = person_has_to(&plain(state.get("undelivered_code"))) {
                    then = format!("{what}, then resume the goal");
                }
            }
            let reason = format!("Recovery attempt limit reached{last}; {then}");
            set(state, json!({ "state": "attention", "reason": reason }));
            return noted(plan, goal, true);
        }
        // A wake that was sent and left the owner resting again is an unfinished turn, not a
        // finished goal: it gets a new wake under a new id. So does one whose turn failed,
        // and one that was claimed for an earlier revision of the plan.
        let claimed_for = state
            .get("plan_revision")
            .and_then(Value::as_i64)
            .unwrap_or(revision);
        let mut request_id = request;
        if matches!(state_of(state), "admitted" | "queued") || spent || claimed_for != revision {
            let next = generation(state) + 1;
            state.insert("generation".to_string(), json!(next));
            request_id.clear();
        }
        if request_id.is_empty() {
            request_id = format!("{WAKE_PREFIX}{}", (seen.token_hex)(16));
        }
        let tries = number(state, "attempts") + 1;
        set(
            state,
            json!({
                "request_id": request_id, "state": "dispatching", "plan_revision": revision,
                "attempts": tries,
            }),
        );
        let claim = json!({ "request_id": request_id, "generation": generation(state) });
        remember(&mut plan, &mut goal, "wake_claim", revision, claim, now);
        // The owner reads what the goal said before this wake was noted in it.
        let prompt = self.prompt(&plan, &goal);
        let backoff = (120_000_i64 << (tries - 1).clamp(0, 5)).min(3_600_000);
        let reason = "Canonical dispatch accepted the wake";
        set(
            &mut goal.continuation,
            json!({
                "state": "admitted", "reason": reason, "lease_until": null,
                "due_at": now + backoff, "last_dispatch_at": now,
            }),
        );
        let sent = json!({ "request_id": request_id, "reason": reason });
        remember(
            &mut plan,
            &mut goal,
            "dispatch_admitted",
            revision,
            sent,
            now,
        );
        plan.goal = Some(goal);
        Looked::Wake {
            change: Change {
                plan,
                documents: Vec::new(),
            },
            request_id,
            prompt,
        }
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::super::Owner;
    use super::*;
    use crate::jobs::{JobBook, JobCommand, Worker};

    const VERA: Owner<'static> = Owner {
        agent_id: "a1",
        session: "vera",
    };
    const T0: i64 = 1_000_000;

    /// An agent with one enrolled goal, driven the way the Host drives it.
    struct Driven {
        book: GoalBook,
        jobs: JobBook,
        plan_id: String,
        ids: u64,
        /// How the turn of each wake ended.
        wakes: Vec<(String, TurnSeen)>,
        busy: Option<(&'static str, &'static str)>,
        rested: Option<TurnSeen>,
        native: &'static str,
    }

    impl Driven {
        fn new() -> Self {
            let mut driven = Self {
                book: GoalBook::new(),
                jobs: JobBook::new(),
                plan_id: String::new(),
                ids: 0,
                wakes: Vec::new(),
                busy: None,
                rested: None,
                native: "s1",
            };
            let goal = json!({
                "outcome": "It is done", "criteria": ["It runs"], "limits": "None",
                "enroll": true,
            });
            let items = json!([{ "id": "one", "title": "The first step" }]);
            let change = driven
                .with_world(T0, |book, world| {
                    book.create(VERA, "ship", "Ship it", &items, Some(&goal), world)
                })
                .unwrap();
            driven.plan_id = change.plan.plan_id.clone();
            driven.keep(&change, T0);
            driven
        }

        fn with_world<T>(
            &mut self,
            now: i64,
            ask: impl FnOnce(&GoalBook, &mut World<'_>) -> T,
        ) -> T {
            let ids = &mut self.ids;
            let mut token = |bytes: usize| {
                *ids += 1;
                format!("{:0width$x}", *ids, width = bytes * 2)
            };
            let jobs = &self.jobs;
            let mut world = World {
                now_ms: now,
                native_session: self.native,
                queue_revision: 0,
                token_hex: &mut token,
                has_job: &|id, generation| {
                    jobs.get(id).is_some_and(|job| job.generation == generation)
                },
            };
            ask(&self.book, &mut world)
        }

        fn keep(&mut self, change: &Change, now: i64) {
            let record = self.book.record(change, now);
            self.book.apply(&record);
        }

        fn act(&mut self, now: i64, action: &str, data: Value) {
            let plan_id = self.plan_id.clone();
            let revision = self.book.plan(&plan_id).unwrap().revision;
            let change = self
                .with_world(now, |book, world| {
                    book.act(&plan_id, revision, action, &data, world)
                })
                .unwrap();
            self.keep(&change, now);
        }

        /// Looks at the goal and writes what the look comes to. Answers the wake, if it is one.
        fn look(&mut self, now: i64) -> Option<(String, String)> {
            let ids = &mut self.ids;
            let mut token = |bytes: usize| {
                *ids += 1;
                format!("{:0width$x}", *ids, width = bytes * 2)
            };
            let jobs = &self.jobs;
            let wakes = &self.wakes;
            let mut seen = Seen {
                now_ms: now,
                session: "vera",
                native_session: self.native,
                busy: self.busy,
                rested: self.rested.as_ref(),
                job: &|id| jobs.get(id),
                wake: &|id| {
                    let found = wakes.iter().find(|(request, _)| request == id);
                    found.map(|(_, turn)| turn.clone())
                },
                token_hex: &mut token,
            };
            match self.book.look(&self.plan_id, &mut seen) {
                Looked::Nothing => None,
                Looked::Noted(change) => {
                    self.keep(&change, now);
                    None
                }
                Looked::Wake {
                    change,
                    request_id,
                    prompt,
                } => {
                    self.keep(&change, now);
                    Some((request_id, prompt))
                }
            }
        }

        /// The turn of a wake ends this way, and the owner rests again.
        fn wake_ends(&mut self, request_id: &str, outcome: &str, at: i64) {
            let turn = TurnSeen {
                outcome: outcome.to_string(),
                started: true,
                ended_at_ms: at,
                ..TurnSeen::default()
            };
            self.wakes.push((request_id.to_string(), turn.clone()));
            self.rested = Some(turn);
        }

        fn state(&self) -> &Map<String, Value> {
            let plan = self.book.plan(&self.plan_id).unwrap();
            &plan.goal.as_ref().unwrap().continuation
        }

        fn history_kinds(&self) -> Vec<String> {
            let plan = self.book.plan(&self.plan_id).unwrap();
            let history = &plan.goal.as_ref().unwrap().history;
            history
                .iter()
                .map(|entry| plain(entry.get("kind")))
                .collect()
        }
    }

    #[test]
    fn an_enrolled_goal_that_is_due_wakes_its_owner_when_it_rests() {
        let mut d = Driven::new();
        assert_eq!(d.book.next_due(), Some(T0 + 120_000));
        // Not due yet: the owner is seen to rest, which is written once and not again.
        assert_eq!(d.look(T0 + 1_000), None);
        assert_eq!(d.state()["observed_state"], "idle");
        let seen_at = d.state()["observed_at"].clone();
        assert_eq!(d.look(T0 + 2_000), None);
        assert_eq!(
            d.state()["observed_at"],
            seen_at,
            "a look that sees the same writes nothing"
        );

        let revision = d.book.plan(&d.plan_id).unwrap().revision;
        let (request_id, prompt) = d.look(T0 + 120_000).unwrap();
        assert!(request_id.starts_with(WAKE_PREFIX), "{request_id}");
        assert!(
            prompt.starts_with("Continue your durable outcome commitment."),
            "{prompt}"
        );
        assert!(
            prompt.contains(&format!("clarp-goal get {}\n", d.plan_id)),
            "{prompt}"
        );
        assert!(
            prompt.contains("Continuation: Awaiting owner checkpoint\n"),
            "{prompt}"
        );
        assert_eq!(d.state()["state"], "admitted");
        assert_eq!(d.state()["attempts"], 1);
        assert_eq!(d.state()["due_at"], json!(T0 + 240_000));
        assert_eq!(d.state()["request_id"], request_id.as_str());
        let kinds = d.history_kinds();
        assert_eq!(
            kinds[kinds.len() - 2..],
            ["wake_claim", "dispatch_admitted"]
        );
        let plan = d.book.plan(&d.plan_id).unwrap();
        assert_eq!(
            plan.revision, revision,
            "a wake does not make the owner's copy stale"
        );
        assert_eq!(d.book.next_due(), Some(T0 + 240_000));
    }

    #[test]
    fn an_owner_that_works_is_not_woken_and_the_goal_says_why() {
        let mut d = Driven::new();
        d.busy = Some(("working", "Owner has live or queued work"));
        assert_eq!(d.look(T0 + 120_000), None);
        assert_eq!(d.state()["observed_state"], "working");
        assert_eq!(d.state()["state"], "ready");
        d.busy = None;
        assert!(d.look(T0 + 130_000).is_some());
    }

    #[test]
    fn a_goal_whose_owner_has_another_conversation_now_is_not_woken() {
        let mut d = Driven::new();
        d.native = "s2";
        assert_eq!(d.look(T0 + 120_000), None);
        assert_eq!(d.state()["observed_state"], "owner_changed");
        assert_eq!(
            d.state()["observed_reason"],
            "Native conversation changed; explicit re-enrollment required"
        );
    }

    #[test]
    fn a_wake_that_no_checkpoint_follows_is_tried_again_later_each_time_and_then_given_up() {
        let mut d = Driven::new();
        let mut now = T0 + 120_000;
        let mut waits = Vec::new();
        for attempt in 1..=5 {
            let (request_id, _) = d.look(now).unwrap();
            assert_eq!(d.state()["attempts"], attempt);
            let due = d.state()["due_at"].as_i64().unwrap();
            waits.push(due - now);
            // The owner answers and rests, without a word about the goal.
            d.wake_ends(&request_id, "completed", now + 5_000);
            assert_eq!(d.look(now + 6_000), None, "not before it is due again");
            now = due;
        }
        assert_eq!(waits, [120_000, 240_000, 480_000, 960_000, 1_920_000]);
        assert!(
            d.history_kinds()
                .contains(&"execution_observed".to_string())
        );
        // The sixth time nobody is woken: a person has to look.
        assert_eq!(d.look(now), None);
        assert_eq!(d.state()["state"], "attention");
        assert_eq!(
            d.state()["reason"],
            "Recovery attempt limit reached; checkpoint or resume to continue"
        );
        assert_eq!(d.look(now + 3_600_000), None);
        assert_eq!(
            d.book.next_due(),
            None,
            "nothing is due while it waits for a person"
        );

        // A checkpoint begins anew.
        let later = now + 7_200_000;
        d.act(
            later,
            "checkpoint",
            json!({ "progress": "On it again", "next_work": "The rest" }),
        );
        assert_eq!(d.state()["state"], "ready");
        assert_eq!(d.state()["attempts"], 0);
        assert!(d.look(later + 120_000).is_some());
    }

    #[test]
    fn a_wake_whose_turn_failed_is_said_and_counted() {
        let mut d = Driven::new();
        let now = T0 + 120_000;
        let (request_id, _) = d.look(now).unwrap();
        let mut turn = TurnSeen {
            outcome: "failed".to_string(),
            started: true,
            ended_at_ms: now + 3_000,
            ..TurnSeen::default()
        };
        d.wakes.push((request_id.clone(), turn.clone()));
        d.rested = Some(turn.clone());
        assert_eq!(d.look(now + 4_000), None);
        let reason = plain(d.state().get("reason"));
        assert!(
            reason
                .starts_with("Wake started and failed: Its turn failed; attempt 1 of 5; next try "),
            "{reason}"
        );
        assert!(reason.ends_with(" UTC"), "{reason}");
        assert_eq!(d.state()["undelivered_request_id"], request_id.as_str());
        assert!(d.history_kinds().contains(&"wake_failed".to_string()));
        assert_eq!(d.look(now + 5_000), None, "it is said once");
        let said = d
            .history_kinds()
            .iter()
            .filter(|kind| *kind == "wake_failed")
            .count();
        assert_eq!(said, 1);

        // The next wake has an id of its own. No account with usage is not the goal's failure.
        let (second, _) = d.look(now + 240_000).unwrap();
        assert_ne!(second, request_id);
        turn.code = "usage_limit".to_string();
        turn.ended_at_ms = now + 250_000;
        d.wakes.push((second, turn.clone()));
        d.rested = Some(turn);
        assert_eq!(d.look(now + 260_000), None);
        assert_eq!(d.state()["capacity_failures"], 1);
        let reason = plain(d.state().get("reason"));
        assert!(
            reason.starts_with("Wake started and failed: no account with usage; next try "),
            "{reason}"
        );
    }

    #[test]
    fn a_goal_that_waits_for_a_job_is_woken_when_the_job_ends() {
        let mut d = Driven::new();
        let register = JobCommand::Register {
            id: "build-1".to_string(),
            kind: "build".to_string(),
            title: "The build".to_string(),
            detail: String::new(),
            worker: Worker::none(),
            restart_cancelled: false,
            started_trace_id: String::new(),
            heartbeat_timeout_ms: None,
            metadata: Value::Null,
        };
        let change = d.jobs.decide(register, T0, 1).unwrap();
        d.jobs.apply(&change.record.unwrap());
        let due = T0 + 3_600_000;
        d.act(
            T0 + 1_000,
            "checkpoint",
            json!({
                "progress": "Build launched", "next_work": "Read its result",
                "continuation": {
                    "kind": "dependency", "key": "build-1", "reason": "Waiting for the build",
                    "due_at": due, "job_handle": "bg1:1:build-1",
                },
            }),
        );
        assert_eq!(d.state()["state"], "waiting");
        assert_eq!(d.look(T0 + 60_000), None, "the job still runs");
        assert_eq!(d.book.next_due(), Some(due));

        let finish = JobCommand::Finish {
            id: "build-1".to_string(),
            generation: 1,
            worker: Worker::none(),
            failed: None,
        };
        let change = d.jobs.decide(finish, T0 + 90_000, 2).unwrap();
        d.jobs.apply(&change.record.unwrap());
        let (_, prompt) = d.look(T0 + 91_000).unwrap();
        assert!(
            prompt.contains("Continuation: Background job succeeded\n"),
            "{prompt}"
        );
        assert!(prompt.contains("\"outcome\": \"succeeded\""), "{prompt}");
        assert_eq!(d.state()["dependency_result"]["key"], "build-1");
        assert!(d.history_kinds().contains(&"dependency_result".to_string()));
    }

    #[test]
    fn a_dependency_that_is_not_met_by_its_deadline_wakes_the_owner_to_look() {
        let mut d = Driven::new();
        let due = T0 + 600_000;
        d.act(
            T0 + 1_000,
            "checkpoint",
            json!({
                "progress": "Asked", "next_work": "Read the answer",
                "continuation": {
                    "kind": "dependency", "key": "answer", "reason": "Waiting for an answer",
                    "due_at": due,
                },
            }),
        );
        assert_eq!(d.look(due - 1), None);
        let (_, prompt) = d.look(due).unwrap();
        assert!(
            prompt.contains(
                "Continuation: External dependency deadline passed; inspect its actual outcome\n"
            ),
            "{prompt}"
        );
    }

    #[test]
    fn a_stop_pauses_every_goal_that_is_being_worked() {
        let mut d = Driven::new();
        let changes = d.with_world(T0 + 5_000, |book, world| {
            book.stopped("User stopped this agent", world)
        });
        assert_eq!(changes.len(), 1);
        d.keep(&changes[0], T0 + 5_000);
        assert_eq!(d.book.plan(&d.plan_id).unwrap().status, "paused");
        assert_eq!(d.state()["reason"], "User stopped this agent");
        assert_eq!(d.look(T0 + 120_000), None, "a paused goal wakes nobody");
        assert_eq!(d.book.next_due(), None);
        let again = d.with_world(T0 + 6_000, |book, world| book.stopped("again", world));
        assert!(again.is_empty(), "what is paused is not paused again");
    }

    #[test]
    fn the_words_of_a_prompt_are_written_as_python_writes_them() {
        assert_eq!(python_json(&json!(null)), "null");
        assert_eq!(
            python_json(&json!({ "key": "build-1", "n": [1, 2], "ok": true })),
            r#"{"key": "build-1", "n": [1, 2], "ok": true}"#
        );
        assert_eq!(
            python_json(&json!([["notes.md", 3]])),
            r#"[["notes.md", 3]]"#
        );
        assert_eq!(shell_quote("a1:ship:00000002"), "a1:ship:00000002");
        assert_eq!(shell_quote("a plan"), "'a plan'");
        assert_eq!(shell_quote("it's"), r#"'it'"'"'s'"#);
        assert_eq!(shell_quote(""), "''");
        assert_eq!(preview("short", 10), "short");
        assert_eq!(
            preview("a long text", 6),
            "a long [preview truncated; read the current goal]"
        );
        assert_eq!(hour_and_minute(0), "00:00 UTC");
        assert_eq!(hour_and_minute(1_794_740_016_000), "10:53 UTC");
    }
}
