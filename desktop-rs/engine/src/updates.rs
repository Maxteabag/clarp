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

#[derive(Default)]
pub(crate) struct Updates {
    attention_items: Vec<Value>,
    background_jobs: Vec<Value>,
    artifacts: Vec<Value>,
    generation: u64,
    pending: i32,
    error: String,
    /// `decision:<id>` / `job:<id>` while its request is in flight.
    pending_actions: HashSet<String>,
    jobs: JobTracker,
    /// Bumped whenever the job tracker changes (Qt `processRevision`).
    process_revision: u64,
    /// artifact id -> how its form's latest answers went.
    form_status: std::collections::HashMap<String, String>,
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

    /// An agent's processes (jobs, sub-agents, helpers) for its popover.
    pub fn agent_processes(&self, session: &str) -> Option<Object> {
        let now = chrono::Utc::now().timestamp_millis();
        clarp_core::roster::describe_agent_processes(&self.roster, &self.updates.jobs, session, now)
    }

    /// How the form's latest answers went: "Sending…", "Answers accepted"
    /// or "Not sent: …" ("" before any).
    pub fn form_status(&self, artifact_id: &str) -> &str {
        self.updates.form_status.get(artifact_id).map_or("", String::as_str)
    }

    /// A form the window could not open or send says why on its card.
    pub fn set_form_status(&mut self, artifact_id: &str, status: &str) {
        self.updates.form_status.insert(artifact_id.to_owned(), status.to_owned());
        self.changes.push(Change::Updates);
    }

    // ---- commands --------------------------------------------------------

    /// Sends an HTML form's answers (iOS `HTMLFormView`): a fresh submission
    /// id with the form's version; the Host's receipt must echo both.
    pub fn submit_html_form(&mut self, artifact_id: &str, version: Value, answers: Value) {
        if artifact_id.is_empty() || !answers.is_object() {
            return;
        }
        let submission = uuid::Uuid::new_v4().to_string();
        self.updates.form_status.insert(artifact_id.to_owned(), "Sending…".into());
        self.changes.push(Change::Updates);
        let path = format!("/artifacts/{}/submit", clarp_core::endpoint::percent_encode_segment(artifact_id));
        let body = json!({"submission_id": submission, "version": version.clone(), "answers": answers});
        self.api.post_json(&format!("form-submit:{artifact_id}|{submission}|{version}"), &path, body, None);
    }

    pub fn load_updates(&mut self) {
        self.updates.generation += 1;
        self.updates.error.clear();
        self.updates.pending = 3;
        self.changes.push(Change::Updates);
        let generation = self.updates.generation;
        self.api.get(&format!("updates:{generation}:attention"), "/attention", &[]);
        self.api.get(&format!("updates:{generation}:jobs"), "/background-jobs", &[]);
        self.api.get(&format!("updates:{generation}:artifacts"), "/artifacts", &[("limit", "50"), ("order", "updated")]);
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
        let key = format!("job:{job_id}");
        if job_id.is_empty() || self.updates.pending_actions.contains(&key) {
            return;
        }
        self.updates.pending_actions.insert(key);
        self.changes.push(Change::Updates);
        let path = format!("/background-jobs/{}", clarp_core::endpoint::percent_encode_segment(job_id));
        self.api.delete(&format!("update-action:job:{job_id}"), &path);
    }

    // ---- replies and events ----------------------------------------------

    fn finish_update_request(&mut self, generation: u64) {
        if generation != self.updates.generation {
            return;
        }
        self.updates.pending = (self.updates.pending - 1).max(0);
        self.changes.push(Change::Updates);
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
        if let Some(rest) = tag.strip_prefix("updates:") {
            let (generation, kind) = rest.split_once(':').unwrap_or((rest, ""));
            let Ok(generation) = generation.parse::<u64>() else { return true };
            if generation != self.updates.generation {
                return true;
            }
            match kind {
                "attention" => self.updates.attention_items = json::array(object, "items"),
                "jobs" => {
                    self.updates.background_jobs = json::array(object, "jobs");
                    if self.updates.jobs.apply_list(object) {
                        self.jobs_changed();
                    }
                }
                "artifacts" => self.updates.artifacts = json::array(object, "artifacts"),
                _ => {}
            }
            self.finish_update_request(generation);
        } else if let Some(action) = tag.strip_prefix("update-action:") {
            self.updates.pending_actions.remove(action);
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
            self.updates.form_status.insert(artifact.to_owned(), status);
            self.changes.push(Change::Updates);
        } else {
            return false;
        }
        true
    }

    pub(crate) fn updates_failure(&mut self, tag: &str, detail: &str) -> bool {
        if let Some(rest) = tag.strip_prefix("updates:") {
            let generation = rest.split(':').next().and_then(|g| g.parse::<u64>().ok());
            if generation == Some(self.updates.generation) {
                self.updates.error = detail.to_owned();
                self.finish_update_request(self.updates.generation);
            }
        } else if let Some(action) = tag.strip_prefix("update-action:") {
            self.updates.pending_actions.remove(action);
            self.updates.error = detail.to_owned();
            self.changes.push(Change::Updates);
        } else if let Some(rest) = tag.strip_prefix("form-submit:") {
            let artifact = rest.split('|').next().unwrap_or_default();
            self.updates.form_status.insert(artifact.to_owned(), format!("Not sent: {detail}"));
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
                    self.load_updates();
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
