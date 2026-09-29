//! Active background jobs per agent, fed by `GET /background-jobs` and the
//! `background-job-updated` SSE event. Port of
//! `desktop/src/models/BackgroundJobTracker`: only queued and running jobs are
//! kept, because the roster shows what is still running, not history.

use std::collections::HashMap;

use serde_json::Value;

use crate::json::{self, Object};

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct JobCounts {
    pub total: i32,
    pub sub_agents: i32,
}

#[derive(Debug, Default, Clone)]
pub struct JobTracker {
    active: HashMap<String, Object>,
    loaded: bool,
}

pub fn is_active_status(status: &str) -> bool {
    matches!(status, "queued" | "running")
}

pub fn is_sub_agent(job: &Object) -> bool {
    json::string(job, "kind") == "sub-agent"
}

impl JobTracker {
    /// False until the first list arrives; callers then fall back to the
    /// snapshot's `background_jobs` counts.
    pub fn loaded(&self) -> bool {
        self.loaded
    }

    /// A full `/background-jobs` response replaces everything known so far.
    /// Returns true when the tracker changed (always on the first list).
    pub fn apply_list(&mut self, response: &Object) -> bool {
        let next: HashMap<String, Object> = json::array(response, "jobs")
            .into_iter()
            .filter_map(|value| value.as_object().cloned())
            .filter(|job| is_active_status(&json::string(job, "status")))
            .filter_map(|job| {
                let id = json::string(&job, "job_id");
                (!id.is_empty()).then_some((id, job))
            })
            .collect();
        let changed = !self.loaded || next != self.active;
        self.active = next;
        self.loaded = true;
        changed
    }

    /// Returns true when the event changed the active set.
    pub fn apply_event(&mut self, event: &Object) -> bool {
        let mut job = json::object(event, "job");
        if job.is_empty() {
            job = event.clone();
        }
        let job_id = job
            .get("job_id")
            .and_then(Value::as_str)
            .map_or_else(|| json::string(event, "job_id"), str::to_owned);
        if job_id.is_empty() {
            return false;
        }
        if !job.contains_key("status")
            && let Some(status) = event.get("status") {
                job.insert("status".into(), status.clone());
            }
        let existing = self.active.get(&job_id);
        // Events can be replayed after a reconnect; never let an older update
        // resurrect or rewind a job the tracker already knows more recently.
        if existing.is_some_and(|e| json::integer(&job, "updated_at") < json::integer(e, "updated_at")) {
            return false;
        }
        if is_active_status(&json::string(&job, "status")) {
            if existing == Some(&job) {
                return false;
            }
            self.active.insert(job_id, job);
            true
        } else {
            self.active.remove(&job_id).is_some()
        }
    }

    /// Returns true when something was known before.
    pub fn clear(&mut self) -> bool {
        let had = self.loaded || !self.active.is_empty();
        self.active.clear();
        self.loaded = false;
        had
    }

    /// Jobs owned by `agent_id`, or ownerless jobs for `session`; oldest first.
    pub fn active_jobs(&self, agent_id: &str, session: &str) -> Vec<&Object> {
        let mut jobs: Vec<&Object> = self
            .active
            .values()
            .filter(|job| {
                let owner = json::string(job, "agent_id");
                (!agent_id.is_empty() && owner == agent_id)
                    || (owner.is_empty() && !session.is_empty() && json::string(job, "session") == session)
            })
            .collect();
        jobs.sort_by(|a, b| {
            json::integer(a, "started_at")
                .cmp(&json::integer(b, "started_at"))
                .then_with(|| json::string(a, "job_id").cmp(&json::string(b, "job_id")))
        });
        jobs
    }

    pub fn counts_by_agent(&self) -> HashMap<String, JobCounts> {
        let mut counts: HashMap<String, JobCounts> = HashMap::new();
        for job in self.active.values() {
            let owner = json::string(job, "agent_id");
            if owner.is_empty() {
                continue;
            }
            let entry = counts.entry(owner).or_default();
            entry.total += 1;
            if is_sub_agent(job) {
                entry.sub_agents += 1;
            }
        }
        counts
    }
}
