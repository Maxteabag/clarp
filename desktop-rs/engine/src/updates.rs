//! The Updates surface (the Qt controller's attention items, background
//! jobs and update artifacts): what needs the user, what runs in the
//! background and what agents produced. Loaded on demand, a moment after the
//! event stream connects, and again whenever the Host says one changed.

use std::collections::HashSet;
use std::time::Duration;

use clarp_core::jobs::JobTracker;
use clarp_core::json::{self, Object};
use clarp_net::SseSignal;
use serde_json::{Value, json};

use crate::{Change, Engine, Message};

/// Attention, jobs and artifacts follow the sidebar instead of competing
/// with it for the Host on a cold start.
pub const FIRST_UPDATES_DELAY: Duration = Duration::from_millis(1500);

/// One reload is in flight at a time, so a reply that never comes would hold
/// every later one back: each request gives up after this.
pub const UPDATES_TIMEOUT: Duration = Duration::from_secs(60);

#[derive(Default)]
pub(crate) struct Updates {
    attention_items: Vec<Value>,
    background_jobs: Vec<Value>,
    artifacts: Vec<Value>,
    generation: u64,
    pending: i32,
    /// Something changed while a reload was in flight: reload once more
    /// when it finishes. A busy fleet's events used to start one reload
    /// each (36 a minute, 37 in flight, a 9.8 MB artifact list apiece).
    dirty: bool,
    /// A job-list-only refetch (a job event) is in flight; replies tagged
    /// with an older `jobs_generation` are dropped.
    jobs_pending: bool,
    jobs_dirty: bool,
    jobs_generation: u64,
    error: String,
    /// `decision:<id>` / `job:<id>` while its request is in flight.
    pending_actions: HashSet<String>,
    jobs: JobTracker,
    /// Bumped whenever the job tracker changes (Qt `processRevision`).
    process_revision: u64,
    /// artifact id -> how its latest answer (a form's, a decision's) went.
    artifact_status: std::collections::HashMap<String, String>,
    /// "purpose:artifact" -> the bytes fetched for it, or why not.
    artifact_bytes: std::collections::HashMap<String, Result<Vec<u8>, String>>,
    /// job id -> its latest `GET /background-jobs/<id>` (timeline, log
    /// tail), or why it could not be read.
    job_details: std::collections::HashMap<String, Result<Object, String>>,
    /// job id -> how its latest stop went ("Stopping…", "Stopped", "Not
    /// stopped: …").
    job_outcomes: std::collections::HashMap<String, String>,
}

impl Engine {
    // ---- queries ---------------------------------------------------------

    pub fn attention_items(&self) -> &[Value] {
        &self.updates.attention_items
    }
    /// The nav rail's Updates badge.
    pub fn attention_count(&self) -> usize {
        self.updates.attention_items.len()
    }
    pub fn background_jobs(&self) -> &[Value] {
        &self.updates.background_jobs
    }
    pub fn update_artifacts(&self) -> &[Value] {
        &self.updates.artifacts
    }
    /// One of the three lists is still loading.
    pub fn updates_loading(&self) -> bool {
        self.updates.pending > 0
    }
    pub fn updates_error(&self) -> &str {
        &self.updates.error
    }
    pub fn process_revision(&self) -> u64 {
        self.updates.process_revision
    }

    /// The next chat that wants the user after the selected one: unread,
    /// waiting, or holding a pending attention item. Empty when none.
    pub fn next_attention_session(&self) -> String {
        let pending: Vec<String> = self
            .updates
            .attention_items
            .iter()
            .filter_map(|item| item.get("session").and_then(Value::as_str))
            .filter(|s| !s.is_empty())
            .map(str::to_owned)
            .collect();
        self.roster.next_attention_session(&self.selected, &pending).unwrap_or_default()
    }

    /// The agents Ctrl+J will visit, in order (the explorer numbers them).
    pub fn attention_queue(&self) -> Vec<String> {
        let pending: Vec<String> = self
            .updates
            .attention_items
            .iter()
            .filter_map(|item| item.get("session").and_then(Value::as_str))
            .filter(|s| !s.is_empty())
            .map(str::to_owned)
            .collect();
        self.roster.attention_queue(&self.selected, &pending)
    }

    /// The artifacts one agent produced (profile panel).
    pub fn artifacts_for_session(&self, session: &str) -> Vec<Value> {
        self.updates.artifacts.iter().filter(|a| a.get("session").and_then(Value::as_str) == Some(session)).cloned().collect()
    }

    pub fn artifact_is_viewable_report(&self, artifact: &Object) -> bool {
        clarp_core::text::artifact_is_viewable_report(artifact)
    }

    /// The report view's model for an artifact; None when it has none.
    pub fn report_for_artifact(&self, artifact_id: &str) -> Option<Object> {
        clarp_core::text::report_for_artifact(&self.updates.artifacts, artifact_id, &self.base_url)
    }

    /// 0..1, or negative when the job reports no progress.
    pub fn background_job_progress(&self, job: &Object) -> f64 {
        clarp_core::jobs::job_progress(job)
    }

    /// A resolve or cancel is in flight: `kind` is `decision` or `job`.
    pub fn update_action_pending(&self, kind: &str, id: &str) -> bool {
        self.updates.pending_actions.contains(&format!("{kind}:{id}"))
    }

    /// One job's detail (`job`, `timeline`, `progress`, `log`) once read,
    /// or why it could not be.
    pub fn job_detail(&self, job_id: &str) -> Option<&Result<Object, String>> {
        self.updates.job_details.get(job_id)
    }

    /// How the job's latest stop went ("" before any).
    pub fn job_outcome(&self, job_id: &str) -> &str {
        self.updates.job_outcomes.get(job_id).map_or("", String::as_str)
    }

    /// An agent's processes (jobs, sub-agents, helpers) for its popover.
    pub fn agent_processes(&self, session: &str) -> Option<Object> {
        let now = chrono::Utc::now().timestamp_millis();
        clarp_core::roster::describe_agent_processes(&self.roster, &self.updates.jobs, session, now)
    }

    /// How the card's latest answer went: "Sending…", "Answers accepted"
    /// or "Not sent: …" ("" before any, and once a decision's is in).
    pub fn artifact_status(&self, artifact_id: &str) -> &str {
        self.updates.artifact_status.get(artifact_id).map_or("", String::as_str)
    }

    /// A card the window could not open or send says why.
    pub fn set_artifact_status(&mut self, artifact_id: &str, status: &str) {
        self.updates.artifact_status.insert(artifact_id.to_owned(), status.to_owned());
        self.changes.push(Change::Updates);
    }

    /// Fetches an artifact's bytes from the Host (a poster, a file to
    /// open); `take_artifact_bytes` hands them over once they arrive.
    pub fn fetch_artifact_bytes(&mut self, artifact_id: &str, purpose: &str, path: &str) {
        let tag = format!("artifact-bytes:{purpose}:{artifact_id}:{}", uuid::Uuid::new_v4().simple());
        self.api.get_bytes(&tag, path);
    }

    pub fn take_artifact_bytes(&mut self, artifact_id: &str, purpose: &str) -> Option<Result<Vec<u8>, String>> {
        self.updates.artifact_bytes.remove(&format!("{purpose}:{artifact_id}"))
    }

    // ---- commands --------------------------------------------------------

    /// Answers an artifact's decision (iOS `submitDecision`): `action` is
    /// `resolve` with `{"choice"}` or `{"answer"}`, or `dismiss`; always
    /// against the revision the user saw. The artifacts reload after.
    pub fn send_decision(&mut self, artifact_id: &str, decision_id: &str, action: &str, mut body: Value, revision: i64) {
        let key = format!("decision:{decision_id}");
        if decision_id.is_empty() || !matches!(action, "resolve" | "dismiss") || self.updates.pending_actions.contains(&key) {
            return;
        }
        if let Some(object) = body.as_object_mut() {
            object.insert("expected_revision".into(), json!(revision));
        }
        self.updates.pending_actions.insert(key);
        self.updates.artifact_status.insert(artifact_id.to_owned(), "Sending…".into());
        self.changes.push(Change::Updates);
        let path = format!("/decisions/{}/{action}", clarp_core::endpoint::percent_encode_segment(decision_id));
        self.api.post_json(&format!("decision-answer:{artifact_id}|{decision_id}"), &path, body, None);
    }

    /// Sends an HTML form's answers (iOS `HTMLFormView`): a fresh submission
    /// id with the form's version; the Host's receipt must echo both.
    pub fn submit_html_form(&mut self, artifact_id: &str, version: Value, answers: Value) {
        if artifact_id.is_empty() || !answers.is_object() {
            return;
        }
        let submission = uuid::Uuid::new_v4().to_string();
        self.updates.artifact_status.insert(artifact_id.to_owned(), "Sending…".into());
        self.changes.push(Change::Updates);
        let path = format!("/artifacts/{}/submit", clarp_core::endpoint::percent_encode_segment(artifact_id));
        let body = json!({"submission_id": submission, "version": version.clone(), "answers": answers});
        self.api.post_json(&format!("form-submit:{artifact_id}|{submission}|{version}"), &path, body, None);
    }

    pub fn load_updates(&mut self) {
        if self.updates.pending > 0 {
            self.updates.dirty = true;
            return;
        }
        self.updates.dirty = false;
        self.updates.jobs_dirty = false;
        // This reload's job list supersedes a job-only refetch in flight.
        self.updates.jobs_generation += 1;
        self.updates.generation += 1;
        self.updates.error.clear();
        self.updates.pending = 3;
        self.changes.push(Change::Updates);
        let generation = self.updates.generation;
        self.api.get_with_timeout(&format!("updates:{generation}:attention"), "/attention", UPDATES_TIMEOUT);
        self.api.get_with_timeout(&format!("updates:{generation}:jobs"), "/background-jobs", UPDATES_TIMEOUT);
        self.api.get_with_timeout(&format!("updates:{generation}:artifacts"), "/artifacts?limit=50&order=updated", UPDATES_TIMEOUT);
    }

    /// A job changed: only the job list is refetched, once at a time.
    fn load_background_jobs(&mut self) {
        if self.updates.pending > 0 || self.updates.jobs_pending {
            self.updates.jobs_dirty = true;
            return;
        }
        self.updates.jobs_dirty = false;
        self.updates.jobs_pending = true;
        self.updates.jobs_generation += 1;
        let tag = format!("updates-jobs:{}", self.updates.jobs_generation);
        self.api.get_with_timeout(&tag, "/background-jobs", UPDATES_TIMEOUT);
    }

    /// Answers a decision: `yes`/`accepted` or `no`/`rejected`, against the
    /// revision the user saw.
    pub fn resolve_decision(&mut self, decision_id: &str, choice: &str, revision: i64) {
        let choice = match choice {
            "yes" | "accepted" => "accepted",
            "no" | "rejected" => "rejected",
            _ => return,
        };
        let key = format!("decision:{decision_id}");
        if decision_id.is_empty() || self.updates.pending_actions.contains(&key) {
            return;
        }
        self.updates.pending_actions.insert(key);
        self.changes.push(Change::Updates);
        let path = format!("/decisions/{}/resolve", clarp_core::endpoint::percent_encode_segment(decision_id));
        self.api.post_json(
            &format!("update-action:decision:{decision_id}"),
            &path,
            json!({"choice": choice, "expected_revision": revision}),
            None,
        );
    }

    pub fn cancel_background_job(&mut self, job_id: &str) {
        self.cancel_background_job_run(job_id, None);
    }

    /// Stops one run of a job (iOS `cancelBackgroundJob`): with its
    /// generation the Host leaves a newer run alone (409).
    pub fn cancel_background_job_run(&mut self, job_id: &str, generation: Option<i64>) {
        let key = format!("job:{job_id}");
        if job_id.is_empty() || self.updates.pending_actions.contains(&key) {
            return;
        }
        self.updates.pending_actions.insert(key);
        self.updates.job_outcomes.insert(job_id.to_owned(), "Stopping…".into());
        self.changes.push(Change::Updates);
        self.changes.push(Change::Processes);
        let path = format!("/background-jobs/{}", clarp_core::endpoint::percent_encode_segment(job_id));
        let tag = format!("update-action:job:{job_id}");
        match generation.filter(|g| *g > 0) {
            Some(generation) => self.api.delete_json(&tag, &path, json!({"expected_generation": generation})),
            None => self.api.delete(&tag, &path),
        }
    }

    /// Reads one job's timeline and log tail (`GET /background-jobs/<id>`).
    pub fn load_job_detail(&mut self, job_id: &str) {
        if job_id.is_empty() {
            return;
        }
        let path = format!("/background-jobs/{}", clarp_core::endpoint::percent_encode_segment(job_id));
        self.api.get(&format!("job-detail:{job_id}"), &path, &[]);
    }

    // ---- replies and events ----------------------------------------------

    fn finish_update_request(&mut self, generation: u64) {
        if generation != self.updates.generation {
            return;
        }
        self.updates.pending = (self.updates.pending - 1).max(0);
        self.changes.push(Change::Updates);
        if self.updates.pending == 0 {
            if self.updates.dirty {
                self.load_updates();
            } else if self.updates.jobs_dirty {
                self.load_background_jobs();
            }
        }
    }

    /// A job-only refetch ended; a job event during it asks for one more.
    fn finish_jobs_request(&mut self) {
        self.updates.jobs_pending = false;
        if self.updates.jobs_dirty {
            self.load_background_jobs();
        }
    }

    fn apply_job_list(&mut self, object: &Object) {
        self.updates.background_jobs = json::array(object, "jobs");
        if self.updates.jobs.apply_list(object) {
            self.jobs_changed();
        }
    }

    /// The tracker changed: roster counts follow, and views re-read processes.
    fn jobs_changed(&mut self) {
        let counts = self.updates.jobs.loaded().then(|| self.updates.jobs.counts_by_agent());
        match counts {
            Some(counts) => self.mutate_roster(|r| r.apply_live_job_counts(counts)),
            None => self.mutate_roster(|r| r.clear_live_job_counts()),
        }
        self.updates.process_revision += 1;
        self.changes.push(Change::Processes);
    }

    pub(crate) fn updates_json(&mut self, tag: &str, object: &Object) -> bool {
        if let Some(generation) = tag.strip_prefix("updates-jobs:") {
            if generation.parse::<u64>().ok() == Some(self.updates.jobs_generation) {
                self.apply_job_list(object);
                self.changes.push(Change::Updates);
            }
            self.finish_jobs_request();
        } else if let Some(rest) = tag.strip_prefix("updates:") {
            let (generation, kind) = rest.split_once(':').unwrap_or((rest, ""));
            let Ok(generation) = generation.parse::<u64>() else { return true };
            if generation != self.updates.generation {
                return true;
            }
            match kind {
                "attention" => self.updates.attention_items = json::array(object, "items"),
                "jobs" => self.apply_job_list(object),
                "artifacts" => self.updates.artifacts = json::array(object, "artifacts"),
                _ => {}
            }
            self.finish_update_request(generation);
        } else if let Some(job_id) = tag.strip_prefix("job-detail:") {
            self.updates.job_details.insert(job_id.to_owned(), Ok(object.clone()));
            self.changes.push(Change::Processes);
        } else if let Some(action) = tag.strip_prefix("update-action:") {
            self.updates.pending_actions.remove(action);
            if let Some(job_id) = action.strip_prefix("job:") {
                let outcome = if object.get("changed") == Some(&Value::Bool(false)) { "Already stopped" } else { "Stopped" };
                self.updates.job_outcomes.insert(job_id.to_owned(), outcome.into());
                self.changes.push(Change::Processes);
            }
            self.changes.push(Change::Updates);
            self.load_updates();
        } else if let Some(rest) = tag.strip_prefix("decision-answer:") {
            let (artifact, decision) = rest.split_once('|').unwrap_or((rest, ""));
            self.updates.pending_actions.remove(&format!("decision:{decision}"));
            self.updates.artifact_status.remove(artifact);
            self.changes.push(Change::Updates);
            self.load_updates();
        } else if let Some(rest) = tag.strip_prefix("form-submit:") {
            let mut parts = rest.splitn(3, '|');
            let (artifact, submission, version) = (parts.next().unwrap_or_default(), parts.next().unwrap_or_default(), parts.next().unwrap_or_default());
            let echoed = object.get("accepted").and_then(Value::as_bool) == Some(true)
                && json::string(object, "submission_id") == submission
                && json::string(object, "artifact_id") == artifact
                && object.get("version").map(Value::to_string).as_deref() == Some(version);
            let status = if echoed { "Answers accepted".to_owned() } else { "Not sent: the Host's receipt did not match these answers".to_owned() };
            self.updates.artifact_status.insert(artifact.to_owned(), status);
            self.changes.push(Change::Updates);
        } else {
            return false;
        }
        true
    }

    /// An artifact's bytes arrived.
    pub(crate) fn updates_bytes(&mut self, tag: &str, bytes: &[u8]) -> bool {
        let Some(key) = artifact_bytes_key(tag) else { return false };
        self.updates.artifact_bytes.insert(key, Ok(bytes.to_vec()));
        self.changes.push(Change::Updates);
        true
    }

    pub(crate) fn updates_failure(&mut self, tag: &str, detail: &str) -> bool {
        if let Some(key) = artifact_bytes_key(tag) {
            self.updates.artifact_bytes.insert(key, Err(detail.to_owned()));
            self.changes.push(Change::Updates);
            return true;
        }
        if let Some(generation) = tag.strip_prefix("updates-jobs:") {
            if generation.parse::<u64>().ok() == Some(self.updates.jobs_generation) {
                self.updates.error = detail.to_owned();
                self.changes.push(Change::Updates);
            }
            self.finish_jobs_request();
        } else if let Some(rest) = tag.strip_prefix("updates:") {
            let generation = rest.split(':').next().and_then(|g| g.parse::<u64>().ok());
            if generation == Some(self.updates.generation) {
                self.updates.error = detail.to_owned();
                self.finish_update_request(self.updates.generation);
            }
        } else if let Some(job_id) = tag.strip_prefix("job-detail:") {
            self.updates.job_details.insert(job_id.to_owned(), Err(detail.to_owned()));
            self.changes.push(Change::Processes);
        } else if let Some(action) = tag.strip_prefix("update-action:") {
            self.updates.pending_actions.remove(action);
            self.updates.error = detail.to_owned();
            if let Some(job_id) = action.strip_prefix("job:") {
                self.updates.job_outcomes.insert(job_id.to_owned(), format!("Not stopped: {detail}"));
                self.changes.push(Change::Processes);
            }
            self.changes.push(Change::Updates);
        } else if let Some(rest) = tag.strip_prefix("decision-answer:") {
            let (artifact, decision) = rest.split_once('|').unwrap_or((rest, ""));
            self.updates.pending_actions.remove(&format!("decision:{decision}"));
            self.updates.artifact_status.insert(artifact.to_owned(), format!("Not sent: {detail}"));
            self.changes.push(Change::Updates);
        } else if let Some(rest) = tag.strip_prefix("form-submit:") {
            let artifact = rest.split('|').next().unwrap_or_default();
            self.updates.artifact_status.insert(artifact.to_owned(), format!("Not sent: {detail}"));
            self.changes.push(Change::Updates);
        } else {
            return false;
        }
        true
    }

    /// Watches the stream; the engine's own handling runs after.
    pub(crate) fn updates_sse(&mut self, signal: &SseSignal) {
        match signal {
            SseSignal::Connected(true) => self.after(FIRST_UPDATES_DELAY, Message::UpdatesDue),
            SseSignal::Event(event) => match json::string(event, "type").as_str() {
                "background-job-updated" => {
                    // Apply the event's job at once so rows and the header
                    // change without waiting for the refetch; the list stays
                    // authoritative.
                    if self.updates.jobs.loaded() && self.updates.jobs.apply_event(event) {
                        self.jobs_changed();
                    }
                    // Attention and artifacts did not change.
                    self.load_background_jobs();
                }
                "artifact-updated" | "attention-updated" => self.load_updates(),
                _ => {}
            },
            _ => {}
        }
    }

    pub(crate) fn updates_due(&mut self) {
        if self.connected {
            self.load_updates();
        }
    }
}

/// "purpose:artifact" from an `artifact-bytes:purpose:artifact:nonce` tag.
fn artifact_bytes_key(tag: &str) -> Option<String> {
    let rest = tag.strip_prefix("artifact-bytes:")?;
    let (key, _nonce) = rest.rsplit_once(':')?;
    Some(key.to_owned())
}
