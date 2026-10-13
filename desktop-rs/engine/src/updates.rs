//! The Updates surface (the Qt controller's attention items, background
//! jobs and update artifacts): what needs the user, what runs in the
//! background and what agents produced. Loaded on demand, a moment after the
//! event stream connects, and again whenever the Host says one changed.

use std::collections::HashSet;
use std::time::{Duration, Instant};

use clarp_core::jobs::JobTracker;
use clarp_core::json::{self, Object};
use clarp_core::update_feeds::{self, Gate, Intervals, Patch, Throttle};
use clarp_net::SseSignal;
use serde_json::{Value, json};

use crate::{Change, Engine, Message};

/// Attention, jobs and artifacts follow the sidebar instead of competing
/// with it for the Host on a cold start.
pub const FIRST_UPDATES_DELAY: Duration = Duration::from_millis(1500);

/// One reload is in flight at a time, so a reply that never comes would hold
/// every later one back: each request gives up after this.
pub const UPDATES_TIMEOUT: Duration = Duration::from_secs(60);

/// One of the three lists the surface shows.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum Feed {
    Attention,
    Jobs,
    Artifacts,
}

impl Feed {
    const ALL: [Feed; 3] = [Feed::Attention, Feed::Jobs, Feed::Artifacts];

    fn name(self) -> &'static str {
        match self {
            Feed::Attention => "attention",
            Feed::Jobs => "jobs",
            Feed::Artifacts => "artifacts",
        }
    }

    fn named(name: &str) -> Option<Feed> {
        Feed::ALL.into_iter().find(|feed| feed.name() == name)
    }
}

/// A reload asked for while one was in flight, run when it ends.
#[derive(Debug, Default, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
enum Owed {
    #[default]
    Nothing,
    /// By an event: it waits out the list's interval.
    Event,
    /// By the user: at once.
    User,
}

/// One list's reloads: one in flight at a time, event-driven ones at most
/// once per interval with a trailing one so the list ends up current.
#[derive(Default)]
struct FeedState {
    pending: bool,
    /// Replies tagged with an older one are dropped.
    generation: u64,
    owed: Owed,
    throttle: Option<Throttle>,
}

#[derive(Default)]
pub(crate) struct Updates {
    attention_items: Vec<Value>,
    background_jobs: Vec<Value>,
    artifacts: Vec<Value>,
    /// Attention, jobs, artifacts (`Feed` order). A busy fleet's events
    /// used to reload all three each (2026-10-09: 36 a minute, a 9.8 MB
    /// artifact list apiece; 2026-10-13: 492 lists of 1-8 MB in two days).
    feeds: [FeedState; 3],
    intervals: Intervals,
    /// The Host refused `representation=flat-v1` (400): ask in full.
    plain_artifacts: bool,
    /// Artifact ids being read one by one, and those that changed again
    /// meanwhile (read once more).
    items_pending: HashSet<String>,
    items_owed: HashSet<String>,
    /// Items read while the whole list was in flight: the list may predate
    /// them, so they are applied again on top of it.
    item_overrides: std::collections::HashMap<String, Option<Value>>,
    /// How many items the last whole list held (at least the limit asked
    /// for): a patched list never grows past it.
    artifact_cap: usize,
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

impl Updates {
    pub(crate) fn artifacts(&self) -> &[Value] {
        &self.artifacts
    }

    pub(crate) fn artifacts_mut(&mut self) -> &mut Vec<Value> {
        &mut self.artifacts
    }

    fn feed(&mut self, feed: Feed) -> &mut FeedState {
        &mut self.feeds[feed as usize]
    }

    fn interval(&self, feed: Feed) -> Duration {
        match feed {
            Feed::Attention => self.intervals.attention,
            Feed::Jobs => self.intervals.jobs,
            Feed::Artifacts => self.intervals.artifacts,
        }
    }
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
        self.updates.feeds.iter().any(|feed| feed.pending)
    }
    pub fn updates_error(&self) -> &str {
        &self.updates.error
    }
    pub fn process_revision(&self) -> u64 {
        self.updates.process_revision
    }

    /// Requests and bytes read per Updates route (`clarp_net::TRAFFIC_ROUTES`)
    /// since the last call, for the minute's memory line.
    pub fn take_list_traffic(&self) -> std::collections::BTreeMap<&'static str, clarp_net::Traffic> {
        self.api.take_traffic()
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

    /// The user asked (opened Updates, Refresh, an action's reply): all
    /// three lists reload now, past any interval.
    pub fn load_updates(&mut self) {
        self.updates.error.clear();
        for feed in Feed::ALL {
            self.request_list(feed, true);
        }
        self.changes.push(Change::Updates);
    }

    /// The open surface's timer: each list reloads when its interval allows.
    pub fn poll_updates(&mut self) {
        for feed in Feed::ALL {
            self.request_list(feed, false);
        }
    }

    /// The user opened the processes: the job list now, not the others.
    pub fn load_background_jobs(&mut self) {
        self.request_list(Feed::Jobs, true);
    }

    /// The least time between event-driven reloads (tests shorten it).
    pub fn set_update_intervals(&mut self, intervals: Intervals) {
        self.updates.intervals = intervals;
    }

    /// Reads one artifact (`GET /artifacts/<id>`) into the list: an event
    /// named it, or (with a summary list) its content is needed to open it.
    pub fn fetch_artifact(&mut self, artifact_id: &str) {
        if artifact_id.is_empty() {
            return;
        }
        // A summary list's big form: its next list says what changed.
        if self.artifact_summary_defers(artifact_id) {
            self.request_list(Feed::Artifacts, false);
            return;
        }
        if self.updates.items_pending.contains(artifact_id) {
            self.updates.items_owed.insert(artifact_id.to_owned());
            return;
        }
        self.updates.items_pending.insert(artifact_id.to_owned());
        let path = update_feeds::artifact_item_path(artifact_id, !self.updates.plain_artifacts);
        self.api.get_with_timeout(&format!("artifact-item:{artifact_id}"), &path, UPDATES_TIMEOUT);
    }

    /// `user`: now, past the interval; otherwise an event's, at most once
    /// per interval with one trailing reload.
    fn request_list(&mut self, feed: Feed, user: bool) {
        let interval = self.updates.interval(feed);
        let state = self.updates.feed(feed);
        if state.pending {
            state.owed = state.owed.max(if user { Owed::User } else { Owed::Event });
            return;
        }
        let now = Instant::now();
        if !user {
            let throttle = state.throttle.get_or_insert_with(|| Throttle::new(interval));
            throttle.set_interval(interval);
            match throttle.request(now) {
                Gate::Now => {}
                Gate::After(delay) => return self.after(delay, Message::UpdatesFeedDue(feed)),
                Gate::Armed => return,
            }
        }
        self.start_list(feed, now);
    }

    fn start_list(&mut self, feed: Feed, now: Instant) {
        let interval = self.updates.interval(feed);
        let state = self.updates.feed(feed);
        state.pending = true;
        state.owed = Owed::Nothing;
        state.generation += 1;
        state.throttle.get_or_insert_with(|| Throttle::new(interval)).started(now);
        let tag = format!("updates:{}:{}", feed.name(), state.generation);
        let path = match feed {
            Feed::Attention => "/attention".to_owned(),
            Feed::Jobs => "/background-jobs".to_owned(),
            Feed::Artifacts => {
                self.updates.item_overrides.clear();
                let plain = update_feeds::artifact_list_path(!self.updates.plain_artifacts);
                self.artifact_list_path(&tag, &plain)
            }
        };
        self.changes.push(Change::Updates);
        self.api.get_with_timeout(&tag, &path, UPDATES_TIMEOUT);
    }

    /// A list's trailing reload is due.
    pub(crate) fn updates_feed_due(&mut self, feed: Feed) {
        let owed = self.updates.feed(feed).throttle.as_mut().is_some_and(Throttle::fire);
        if owed && self.connected {
            self.request_list(feed, false);
        }
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

    /// The reply's list, when it is the one in flight.
    fn current_list(&mut self, tag: &str) -> Option<Option<Feed>> {
        let rest = tag.strip_prefix("updates:")?;
        let (name, generation) = rest.split_once(':').unwrap_or((rest, ""));
        let feed = Feed::named(name);
        let current = feed.zip(generation.parse::<u64>().ok()).filter(|(feed, generation)| {
            let state = &self.updates.feeds[*feed as usize];
            state.pending && state.generation == *generation
        });
        Some(current.map(|(feed, _)| feed))
    }

    /// A list's reload ended: what was asked for meanwhile follows.
    fn finish_list(&mut self, feed: Feed) {
        let state = self.updates.feed(feed);
        state.pending = false;
        let owed = std::mem::take(&mut state.owed);
        self.changes.push(Change::Updates);
        match owed {
            Owed::Nothing => {}
            Owed::Event => self.request_list(feed, false),
            Owed::User => self.request_list(feed, true),
        }
    }

    /// True when an item read meanwhile left it unlike the Host's.
    fn apply_artifact_list(&mut self, object: &Object) -> bool {
        let mut list = json::array(object, "artifacts");
        let cap = list.len().max(update_feeds::ARTIFACT_LIST_LIMIT);
        let mut stale = false;
        // Items read after this list was asked for are newer than it,
        // unless the list has a later version of them.
        for (id, item) in std::mem::take(&mut self.updates.item_overrides) {
            let listed = list.iter().find(|a| a.get("artifact_id").and_then(Value::as_str) == Some(id.as_str()));
            let stamp = |a: &Value| a.get("updated_at").and_then(Value::as_i64);
            if let (Some(listed), Some(item)) = (listed, item.as_ref())
                && stamp(listed) > stamp(item)
            {
                continue;
            }
            stale |= update_feeds::patch_artifact(&mut list, &id, item, cap).needs_reload();
        }
        self.updates.artifacts = list;
        self.updates.artifact_cap = cap;
        self.artifact_list_applied();
        stale
    }

    /// One artifact as the Host has it now (`None`: gone) into the list.
    fn patch_artifact(&mut self, artifact_id: &str, item: Option<Value>) {
        if self.updates.feeds[Feed::Artifacts as usize].pending {
            self.updates.item_overrides.insert(artifact_id.to_owned(), item.clone());
        }
        let cap = self.updates.artifact_cap.max(update_feeds::ARTIFACT_LIST_LIMIT);
        let patch = update_feeds::patch_artifact(&mut self.updates.artifacts, artifact_id, item, cap);
        if patch != Patch::Unchanged {
            self.changes.push(Change::Updates);
            self.artifact_list_applied();
        }
        // The Host's list dropped its oldest item or moved the next one
        // up; only a reload says which.
        if patch.needs_reload() {
            self.request_list(Feed::Artifacts, false);
        }
    }

    fn finish_item(&mut self, artifact_id: &str) {
        self.updates.items_pending.remove(artifact_id);
        if self.updates.items_owed.remove(artifact_id) {
            self.fetch_artifact(artifact_id);
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
        if let Some(current) = self.current_list(tag) {
            let Some(feed) = current else { return true };
            let mut stale = false;
            match feed {
                Feed::Attention => self.updates.attention_items = json::array(object, "items"),
                Feed::Jobs => self.apply_job_list(object),
                Feed::Artifacts => stale = self.apply_artifact_list(object),
            }
            self.finish_list(feed);
            if stale {
                self.request_list(feed, false);
            }
        } else if let Some(artifact_id) = tag.strip_prefix("artifact-item:") {
            let mut item = object.get("artifact").cloned().unwrap_or(Value::Null);
            if !self.updates.plain_artifacts {
                update_feeds::flatten(&mut item);
            }
            if item.is_object() {
                let item = self.summarize_item(item);
                self.patch_artifact(artifact_id, Some(item));
            } else {
                eprintln!("Updates: artifact {artifact_id} came without an artifact; reloading the list");
                self.request_list(Feed::Artifacts, false);
            }
            self.finish_item(artifact_id);
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

    pub(crate) fn updates_failure(&mut self, tag: &str, detail: &str, status: u16) -> bool {
        if let Some(key) = artifact_bytes_key(tag) {
            self.updates.artifact_bytes.insert(key, Err(detail.to_owned()));
            self.changes.push(Change::Updates);
            return true;
        }
        let unsupported = update_feeds::is_unsupported_representation(status, detail);
        if let Some(current) = self.current_list(tag) {
            let Some(feed) = current else { return true };
            if feed == Feed::Artifacts && unsupported && !self.updates.plain_artifacts {
                // An older Host: the full representation, at once.
                eprintln!("Updates: the Host refused representation={}; asking for full artifacts", update_feeds::FLAT_REPRESENTATION);
                self.updates.plain_artifacts = true;
                let owed = self.updates.feed(feed).owed;
                self.start_list(feed, Instant::now());
                self.updates.feed(feed).owed = owed;
                return true;
            }
            self.updates.error = detail.to_owned();
            self.finish_list(feed);
        } else if let Some(artifact_id) = tag.strip_prefix("artifact-item:") {
            let artifact_id = artifact_id.to_owned();
            if status == 404 {
                self.patch_artifact(&artifact_id, None);
            } else if unsupported && !self.updates.plain_artifacts {
                eprintln!("Updates: the Host refused representation={} for an artifact; asking in full", update_feeds::FLAT_REPRESENTATION);
                self.updates.plain_artifacts = true;
                self.updates.items_owed.insert(artifact_id.clone());
            } else {
                // The list catches up instead, within its interval.
                eprintln!("Updates: artifact {artifact_id} not read ({detail}); reloading the list");
                self.request_list(Feed::Artifacts, false);
            }
            self.finish_item(&artifact_id);
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
            SseSignal::Connected(true) => {
                // It may be a newer Host: ask for flat artifacts again.
                self.updates.plain_artifacts = false;
                self.after(FIRST_UPDATES_DELAY, Message::UpdatesDue);
            }
            SseSignal::Event(event) => match json::string(event, "type").as_str() {
                "background-job-updated" => {
                    // Apply the event's job at once so rows and the header
                    // change without waiting for the refetch; the list stays
                    // authoritative.
                    if self.updates.jobs.loaded() && self.updates.jobs.apply_event(event) {
                        self.jobs_changed();
                    }
                    // Attention and artifacts did not change.
                    self.request_list(Feed::Jobs, false);
                }
                "attention-updated" => self.request_list(Feed::Attention, false),
                // The Host names the artifact: read it alone. Before the
                // first list there is nothing to patch; that list has it.
                "artifact-updated" => match json::string(event, "artifact_id") {
                    id if id.is_empty() => self.request_list(Feed::Artifacts, false),
                    _ if self.updates.feeds[Feed::Artifacts as usize].generation == 0 => {}
                    id => self.fetch_artifact(&id),
                },
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
