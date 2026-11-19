//! Background jobs: the work of an agent that outlives a turn.
//!
//! A job is registered by a worker, a process the agent started and left running: a build, a
//! watcher, a helper. The worker says it is alive, says how far it is, and says how it ended.
//! Whoever looks at the agent sees what runs for it; the agent's goal can wait for a job's end.
//!
//! One job id can have several runs, one after the other. Each run is a **generation**, and
//! what a worker reports is fenced to its own run: the generation it was given, and the process
//! that registered it (its pid and start token). A report from an earlier run, or from another
//! process, changes nothing. That is what makes a job id safe to reuse.
//!
//! This module is the rules and nothing else: [`JobBook::decide`] says what a command does,
//! as a [`JobRecord`] to write, and [`JobBook::apply`] takes a record that was written. The
//! rules are the Python Host's (`server/lib/background_jobs.py`), which the agents' tools and
//! the apps were built against.

use std::collections::{BTreeMap, BTreeSet};

use serde::{Deserialize, Serialize};
use serde_json::Value;

/// How long a job may be silent before it counts as lost, unless it says otherwise.
pub const DEFAULT_HEARTBEAT_TIMEOUT_MS: i64 = 600_000;
const MIN_HEARTBEAT_TIMEOUT_MS: i64 = 30_000;
const MAX_HEARTBEAT_TIMEOUT_MS: i64 = 24 * 60 * 60 * 1_000;
/// The longest progress line that is kept.
pub const PROGRESS_MAX: usize = 500;
const KIND_MAX: usize = 40;
const TITLE_MAX: usize = 120;
const TEXT_MAX: usize = 1_000;
const PATH_MAX: usize = 4_096;
/// An id of this form belongs to what a tool reports of its own tasks, never to a worker.
const RESERVED_PREFIX: &str = "claude-task:";

/// Reasons a job ended that say nobody heard from it any more, not that its work failed.
pub const LOST_REASONS: [&str; 3] = ["heartbeat_expired", "worker_vanished", "heartbeat_lost"];

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum JobStatus {
    Queued,
    Running,
    Succeeded,
    Failed,
    Cancelled,
}

impl JobStatus {
    pub fn is_active(self) -> bool {
        matches!(self, Self::Queued | Self::Running)
    }

    pub fn is_terminal(self) -> bool {
        !self.is_active()
    }

    pub fn as_str(self) -> &'static str {
        match self {
            Self::Queued => "queued",
            Self::Running => "running",
            Self::Succeeded => "succeeded",
            Self::Failed => "failed",
            Self::Cancelled => "cancelled",
        }
    }
}

/// The process that speaks for a run. A pid of 0 and an empty token mean nobody is named:
/// then anyone who knows the generation may report.
#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize, Deserialize)]
pub struct Worker {
    #[serde(default)]
    pub pid: u32,
    /// What tells this process from a later one with the same pid: when it started.
    #[serde(default)]
    pub start_token: String,
}

impl Worker {
    pub fn none() -> Self {
        Self::default()
    }
}

/// A job as it stands.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Job {
    pub id: String,
    pub kind: String,
    pub title: String,
    #[serde(default)]
    pub detail: String,
    pub status: JobStatus,
    /// Which run of this id this is, counted from 1.
    pub generation: u64,
    /// The number of its newest change. Numbers grow across the whole Host.
    pub revision: u64,
    pub started_at_ms: i64,
    pub updated_at_ms: i64,
    /// When it was last heard from. Kept in memory between changes: see [`JobBook::beat`].
    #[serde(default)]
    pub heartbeat_at_ms: Option<i64>,
    pub heartbeat_timeout_ms: i64,
    /// What the last sign of life was: a registration, a start, a heartbeat.
    #[serde(default)]
    pub heartbeat_source: String,
    #[serde(default)]
    pub worker: Worker,
    #[serde(default)]
    pub terminal_at_ms: Option<i64>,
    #[serde(default)]
    pub terminal_reason: String,
    #[serde(default)]
    pub cancelled_at_ms: Option<i64>,
    #[serde(default)]
    pub progress_text: String,
    #[serde(default)]
    pub progress_at_ms: Option<i64>,
    #[serde(default)]
    pub log_path: String,
    #[serde(default)]
    pub worker_cwd: String,
    /// The trace of the owner's turn that registered this run, when it was registered in one.
    #[serde(default)]
    pub started_trace_id: String,
    #[serde(default)]
    pub metadata: Value,
}

impl Job {
    pub fn provider_owned(&self) -> bool {
        self.heartbeat_source == "provider_event"
    }
    /// The name a worker is given for its run: what it reports under.
    pub fn handle(&self) -> String {
        format!("bg1:{}:{}", self.generation, self.id)
    }

    /// Nobody heard from it any more: it was ended for that, which says nothing of its work.
    pub fn lost(&self) -> bool {
        LOST_REASONS.contains(&self.terminal_reason.as_str())
    }

    /// What became of the work, as far as anyone knows: `pending` while it runs, `unknown`
    /// when its worker was lost, else the status it ended with.
    pub fn outcome_state(&self) -> &'static str {
        if self.lost()
            || self.provider_owned()
                && matches!(
                    self.metadata["provider_state"].as_str(),
                    Some("stopped" | "unknown")
                )
        {
            "unknown"
        } else if self.status.is_active() {
            "pending"
        } else {
            self.status.as_str()
        }
    }

    /// The newer of its heartbeat and its progress line.
    pub fn last_sign_ms(&self) -> i64 {
        self.heartbeat_at_ms
            .unwrap_or(self.updated_at_ms)
            .max(self.progress_at_ms.unwrap_or(0))
    }
}

/// Reads a handle: the job's id and the run it names. A bare id names the first run.
pub fn parse_handle(raw: &str) -> (String, u64) {
    if let Some(rest) = raw.strip_prefix("bg1:")
        && let Some((generation, id)) = rest.split_once(':')
        && !id.is_empty()
        && !generation.is_empty()
        && generation.bytes().all(|b| b.is_ascii_digit())
        && let Ok(generation) = generation.parse()
    {
        return (id.to_string(), generation);
    }
    (raw.to_string(), 1)
}

/// What is written to the log for a change of a job: the job as it is after the change.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct JobRecord {
    pub job: Job,
    pub at_ms: i64,
    /// What the job's timeline says of this change. Empty for a plain change of state.
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub note: String,
    /// Whether the change is an entry of the timeline at all. Naming the log file is not.
    #[serde(default = "yes", skip_serializing_if = "is_yes")]
    pub timeline: bool,
}

fn yes() -> bool {
    true
}

fn is_yes(value: &bool) -> bool {
    *value
}

/// A sign of life that is not written down: see [`JobBook::beat`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Beat {
    pub id: String,
    pub at_ms: i64,
    pub source: String,
    pub worker: Worker,
}

/// What a worker or a person asks of a job.
#[derive(Debug, Clone, PartialEq)]
pub enum JobCommand {
    /// A worker registers a run, or says again that it has it.
    Register {
        /// Register before a worker has adopted this generation.
        queued: bool,
        id: String,
        kind: String,
        title: String,
        detail: String,
        worker: Worker,
        /// A job that was cancelled starts again only when this is said.
        restart_cancelled: bool,
        started_trace_id: String,
        heartbeat_timeout_ms: Option<i64>,
        metadata: Value,
    },
    /// The worker has started the work.
    Start {
        id: String,
        generation: Option<u64>,
        worker: Worker,
    },
    /// The worker is alive.
    Heartbeat {
        id: String,
        generation: Option<u64>,
        worker: Worker,
        source: String,
    },
    /// The worker is done. `failed` is why, when the work did not succeed.
    Finish {
        id: String,
        generation: u64,
        worker: Worker,
        failed: Option<String>,
    },
    /// Someone ends the job. With a generation, only that run.
    Cancel {
        id: String,
        expected_generation: Option<u64>,
        reason: String,
    },
    /// The worker says how far it is.
    Progress {
        id: String,
        generation: u64,
        text: String,
    },
    /// The worker names the file it writes its log to.
    SetLog {
        id: String,
        generation: u64,
        path: String,
        worker_cwd: String,
    },
    /// The Host writes a remark into the timeline of a job that runs.
    Note { id: String, note: String },
    /// The Host ends a job nobody is running any more.
    Lose {
        id: String,
        reason: String,
        note: String,
    },
}

impl JobCommand {
    pub fn id(&self) -> &str {
        match self {
            JobCommand::Register { id, .. }
            | JobCommand::Start { id, .. }
            | JobCommand::Heartbeat { id, .. }
            | JobCommand::Finish { id, .. }
            | JobCommand::Cancel { id, .. }
            | JobCommand::Progress { id, .. }
            | JobCommand::SetLog { id, .. }
            | JobCommand::Note { id, .. }
            | JobCommand::Lose { id, .. } => id.as_str(),
        }
    }
}

/// Why a command was not carried out at all.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum JobRefusal {
    /// The id is of a form kept for something else.
    ReservedId,
    /// A log file is named by its whole path.
    LogPathNotAbsolute(String),
}

impl std::fmt::Display for JobRefusal {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::ReservedId => write!(
                f,
                "provider task identity is reserved for native observation"
            ),
            Self::LogPathNotAbsolute(path) => write!(f, "log path must be absolute: {path:?}"),
        }
    }
}

impl std::error::Error for JobRefusal {}

/// What a command did.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct JobChange {
    /// What to write. `None`: nothing about the job changed that is kept.
    pub record: Option<JobRecord>,
    /// A sign of life to note in memory.
    pub beat: Option<Beat>,
    /// The job as the command leaves it. `None`: there is no such job, or the command came
    /// from a run or a process that does not speak for it.
    pub job: Option<Job>,
    /// A cancel that named another run than the one there is.
    pub generation_mismatch: bool,
}

/// The first `max` characters of a text.
fn clip(text: &str, max: usize) -> String {
    text.chars().take(max).collect()
}

/// What the Host writes into a job's timeline when it has been silent for a while.
pub const STALE_NOTE: &str = "reconcile: heartbeat stale";
/// And when it gives the job up.
pub const LOST_NOTE: &str = "reconcile: heartbeat lost";

/// The jobs of one agent.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct JobBook {
    jobs: BTreeMap<String, Job>,
    /// The jobs whose silence has been remarked on, until they are heard from again.
    remarked: BTreeSet<String>,
}

impl JobBook {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn get(&self, id: &str) -> Option<&Job> {
        self.jobs.get(id)
    }

    pub fn jobs(&self) -> impl Iterator<Item = &Job> {
        self.jobs.values()
    }

    /// The jobs that run or wait to.
    pub fn active(&self) -> impl Iterator<Item = &Job> {
        self.jobs.values().filter(|job| job.status.is_active())
    }

    /// Takes a record that is in the log.
    pub fn apply(&mut self, record: &JobRecord) {
        let mut job = record.job.clone();
        // A heartbeat that was noted after the record was decided is not undone by it.
        if let Some(known) = self.jobs.get(&job.id)
            && known.generation == job.generation
        {
            job.heartbeat_at_ms = job.heartbeat_at_ms.max(known.heartbeat_at_ms);
        }
        if record.note == STALE_NOTE {
            self.remarked.insert(job.id.clone());
        } else if job.status.is_terminal() || !record.note.is_empty() {
            self.remarked.remove(&job.id);
        }
        self.jobs.insert(job.id.clone(), job);
    }

    /// Notes a sign of life. It is not written to the log: after a restart every running job
    /// counts as heard from at that moment, see [`JobBook::heard_at`].
    pub fn beat(&mut self, beat: &Beat) {
        if let Some(job) = self.jobs.get_mut(&beat.id)
            && job.status.is_active()
        {
            self.remarked.remove(&beat.id);
            job.heartbeat_at_ms = Some(beat.at_ms.max(job.heartbeat_at_ms.unwrap_or(0)));
            job.heartbeat_source.clone_from(&beat.source);
            if beat.worker.pid != 0 {
                job.worker.pid = beat.worker.pid;
            }
            if !beat.worker.start_token.is_empty() {
                job.worker.start_token.clone_from(&beat.worker.start_token);
            }
        }
    }

    /// Counts every running job as heard from now. For the moment a Host comes back: what it
    /// did not hear while it was away says nothing about the workers.
    pub fn heard_at(&mut self, now_ms: i64) {
        for job in self.jobs.values_mut() {
            if job.status.is_active() && !job.provider_owned() {
                job.heartbeat_at_ms = Some(now_ms.max(job.heartbeat_at_ms.unwrap_or(0)));
            }
        }
    }

    /// Whether this worker and this run speak for the job.
    fn speaks_for(job: &Job, generation: Option<u64>, worker: &Worker) -> bool {
        if generation.is_some_and(|generation| generation != job.generation) {
            return false;
        }
        if job.worker.pid != 0 && worker.pid != job.worker.pid {
            return false;
        }
        job.worker.start_token.is_empty() || worker.start_token == job.worker.start_token
    }

    fn written(job: Job, at_ms: i64, note: &str, timeline: bool) -> JobChange {
        JobChange {
            record: Some(JobRecord {
                job: job.clone(),
                at_ms,
                note: note.to_string(),
                timeline,
            }),
            job: Some(job),
            ..JobChange::default()
        }
    }

    fn unchanged(&self, id: &str) -> JobChange {
        JobChange {
            job: self.jobs.get(id).cloned(),
            ..JobChange::default()
        }
    }

    /// Says what a command does, without doing it. `revision` is the number the change gets
    /// if it is one.
    pub fn decide(
        &self,
        command: JobCommand,
        now_ms: i64,
        revision: u64,
    ) -> Result<JobChange, JobRefusal> {
        let target = command.id();
        if target.trim().starts_with(RESERVED_PREFIX) {
            return Err(JobRefusal::ReservedId);
        }
        match command {
            JobCommand::Register {
                queued,
                id,
                kind,
                title,
                detail,
                worker,
                restart_cancelled,
                started_trace_id,
                heartbeat_timeout_ms,
                metadata,
            } => {
                let id = id.trim().to_string();
                let existing = self.jobs.get(&id);
                if let Some(job) = existing
                    && job.status == JobStatus::Cancelled
                    && !restart_cancelled
                {
                    // Cancelled stays cancelled until someone says to run it again.
                    return Ok(self.unchanged(&id));
                }
                let worker_changed = existing.is_some_and(|job| {
                    !worker.start_token.is_empty()
                        && !job.worker.start_token.is_empty()
                        && worker.start_token != job.worker.start_token
                });
                // A new run: there was none, or the last one is over, or another process has it.
                let fresh = existing.is_none_or(|job| job.status.is_terminal() || worker_changed);
                let timeout = heartbeat_timeout_ms
                    .unwrap_or(DEFAULT_HEARTBEAT_TIMEOUT_MS)
                    .clamp(MIN_HEARTBEAT_TIMEOUT_MS, MAX_HEARTBEAT_TIMEOUT_MS);
                let mut job = Job {
                    id,
                    kind: clip(&kind, KIND_MAX),
                    title: clip(&title, TITLE_MAX),
                    detail: clip(&detail, TEXT_MAX),
                    status: if queued {
                        JobStatus::Queued
                    } else {
                        JobStatus::Running
                    },
                    generation: 1,
                    revision,
                    started_at_ms: now_ms,
                    updated_at_ms: now_ms,
                    heartbeat_at_ms: Some(now_ms),
                    heartbeat_timeout_ms: timeout,
                    heartbeat_source: "worker_registration".to_string(),
                    worker: worker.clone(),
                    terminal_at_ms: None,
                    terminal_reason: String::new(),
                    cancelled_at_ms: None,
                    progress_text: String::new(),
                    progress_at_ms: None,
                    log_path: String::new(),
                    worker_cwd: String::new(),
                    started_trace_id: clip(&started_trace_id, 200),
                    metadata,
                };
                if let Some(known) = existing {
                    if fresh {
                        job.generation = known.generation + 1;
                    } else {
                        // The same run says again that it is there: what it had, it keeps.
                        job.generation = known.generation;
                        job.status = known.status;
                        job.started_at_ms = known.started_at_ms;
                        job.started_trace_id.clone_from(&known.started_trace_id);
                        job.progress_text.clone_from(&known.progress_text);
                        job.progress_at_ms = known.progress_at_ms;
                        job.log_path.clone_from(&known.log_path);
                        job.worker_cwd.clone_from(&known.worker_cwd);
                        job.heartbeat_source.clone_from(&known.heartbeat_source);
                        if worker.start_token.is_empty() {
                            job.worker = known.worker.clone();
                        }
                    }
                }
                Ok(Self::written(job, now_ms, "", true))
            }
            JobCommand::Start {
                id,
                generation,
                worker,
            } => Ok(self.heard(
                &id,
                generation,
                &worker,
                "worker_start",
                true,
                now_ms,
                revision,
            )),
            JobCommand::Heartbeat {
                id,
                generation,
                worker,
                source,
            } => Ok(self.heard(&id, generation, &worker, &source, false, now_ms, revision)),
            JobCommand::Finish {
                id,
                generation,
                worker,
                failed,
            } => {
                let Some(job) = self.jobs.get(&id) else {
                    return Ok(JobChange::default());
                };
                if !Self::speaks_for(job, Some(generation), &worker) {
                    return Ok(JobChange::default());
                }
                if job.status.is_terminal() {
                    return Ok(self.unchanged(&id));
                }
                let mut job = job.clone();
                job.status = if failed.is_some() {
                    JobStatus::Failed
                } else {
                    JobStatus::Succeeded
                };
                job.terminal_at_ms = Some(now_ms);
                job.terminal_reason = clip(failed.as_deref().unwrap_or(""), TEXT_MAX);
                job.updated_at_ms = now_ms;
                job.revision = revision;
                Ok(Self::written(job, now_ms, "", true))
            }
            JobCommand::Cancel {
                id,
                expected_generation,
                reason,
            } => {
                let Some(job) = self.jobs.get(&id) else {
                    return Ok(JobChange::default());
                };
                if expected_generation.is_some_and(|expected| expected != job.generation) {
                    return Ok(JobChange {
                        job: Some(job.clone()),
                        generation_mismatch: true,
                        ..JobChange::default()
                    });
                }
                if job.status.is_terminal() {
                    return Ok(self.unchanged(&id));
                }
                let mut job = job.clone();
                job.status = JobStatus::Cancelled;
                job.cancelled_at_ms = Some(now_ms);
                job.terminal_at_ms = Some(now_ms);
                job.terminal_reason = clip(&reason, TEXT_MAX);
                job.updated_at_ms = now_ms;
                job.revision = revision;
                Ok(Self::written(job, now_ms, "", true))
            }
            JobCommand::Progress {
                id,
                generation,
                text,
            } => {
                let Some(job) = self.running(&id, generation) else {
                    return Ok(JobChange::default());
                };
                let text = clip(
                    &text.split_whitespace().collect::<Vec<_>>().join(" "),
                    PROGRESS_MAX,
                );
                let mut job = job.clone();
                job.progress_text.clone_from(&text);
                job.progress_at_ms = Some(now_ms);
                job.updated_at_ms = now_ms;
                job.revision = revision;
                Ok(Self::written(job, now_ms, &text, true))
            }
            JobCommand::SetLog {
                id,
                generation,
                path,
                worker_cwd,
            } => {
                let path = path.trim();
                if !path.starts_with('/') {
                    return Err(JobRefusal::LogPathNotAbsolute(path.to_string()));
                }
                let Some(job) = self.running(&id, generation) else {
                    return Ok(JobChange::default());
                };
                let worker_cwd = worker_cwd.trim();
                let mut job = job.clone();
                job.log_path = clip(path, PATH_MAX);
                job.worker_cwd = if worker_cwd.starts_with('/') {
                    clip(worker_cwd, PATH_MAX)
                } else {
                    String::new()
                };
                Ok(Self::written(job, now_ms, "", false))
            }
            JobCommand::Note { id, note } => {
                let Some(job) = self.jobs.get(&id).filter(|job| job.status.is_active()) else {
                    return Ok(JobChange::default());
                };
                let mut job = job.clone();
                job.revision = revision;
                Ok(Self::written(job, now_ms, &clip(&note, PROGRESS_MAX), true))
            }
            JobCommand::Lose { id, reason, note } => {
                let Some(job) = self.jobs.get(&id).filter(|job| job.status.is_active()) else {
                    return Ok(self.unchanged(&id));
                };
                let mut job = job.clone();
                job.status = JobStatus::Failed;
                job.terminal_at_ms = Some(now_ms);
                job.terminal_reason = clip(&reason, TEXT_MAX);
                job.updated_at_ms = now_ms;
                job.revision = revision;
                Ok(Self::written(job, now_ms, &clip(&note, PROGRESS_MAX), true))
            }
        }
    }

    /// The job when this is its running or waiting run.
    fn running(&self, id: &str, generation: u64) -> Option<&Job> {
        self.jobs
            .get(id)
            .filter(|job| job.generation == generation && job.status.is_active())
    }

    /// A start or a heartbeat: a sign of life, and for a start that waited, a change.
    #[allow(clippy::too_many_arguments)]
    fn heard(
        &self,
        id: &str,
        generation: Option<u64>,
        worker: &Worker,
        source: &str,
        starts: bool,
        now_ms: i64,
        revision: u64,
    ) -> JobChange {
        let Some(job) = self.jobs.get(id) else {
            return JobChange::default();
        };
        if job.status.is_terminal() {
            return self.unchanged(id);
        }
        if !Self::speaks_for(job, generation, worker) {
            return JobChange::default();
        }
        // Never earlier than what is known: two signs in one millisecond are still two.
        let at_ms = now_ms.max(job.heartbeat_at_ms.unwrap_or(job.updated_at_ms) + 1);
        let beat = Beat {
            id: id.to_string(),
            at_ms,
            source: clip(source, 40),
            worker: worker.clone(),
        };
        let mut heard = job.clone();
        heard.heartbeat_at_ms = Some(at_ms);
        heard.heartbeat_source.clone_from(&beat.source);
        if worker.pid != 0 {
            heard.worker.pid = worker.pid;
        }
        if !worker.start_token.is_empty() {
            heard.worker.start_token.clone_from(&worker.start_token);
        }
        if starts && job.status == JobStatus::Queued {
            heard.status = JobStatus::Running;
            heard.updated_at_ms = at_ms;
            heard.revision = revision;
            return Self::written(heard, at_ms, "", true);
        }
        JobChange {
            beat: Some(beat),
            job: Some(heard),
            ..JobChange::default()
        }
    }

    /// What should happen to the jobs nobody is running any more.
    ///
    /// A running job ends as lost for one of three reasons: its last sign of life is older
    /// than its own timeout (`heartbeat_expired`); the process that registered it is gone
    /// (`worker_vanished`), which `alive` says; or nobody is named to ask, and it has been
    /// silent for `lost_after_ms` (`heartbeat_lost`), after a remark at `stale_after_ms`.
    pub fn overdue(
        &self,
        now_ms: i64,
        stale_after_ms: i64,
        lost_after_ms: i64,
        alive: &dyn Fn(&Worker) -> bool,
    ) -> Vec<JobCommand> {
        let mut todo = Vec::new();
        for job in self.jobs.values() {
            if job.status != JobStatus::Running || job.provider_owned() {
                continue; // one that waits to start is not expected to be heard from
            }
            let silent_ms = now_ms - job.last_sign_ms();
            let expired = silent_ms > job.heartbeat_timeout_ms;
            let named = job.worker.pid != 0 && !job.worker.start_token.is_empty();
            let lose = |reason: &str, note: &str| JobCommand::Lose {
                id: job.id.clone(),
                reason: reason.to_string(),
                note: note.to_string(),
            };
            if named {
                if !alive(&job.worker) {
                    todo.push(lose("worker_vanished", ""));
                } else if expired {
                    todo.push(lose("heartbeat_expired", ""));
                }
            } else if expired {
                // Half a name (a pid without its token, or the other way round) cannot be
                // asked after: the process counts as gone.
                let half_named = job.worker.pid != 0 || !job.worker.start_token.is_empty();
                todo.push(lose(
                    if half_named {
                        "worker_vanished"
                    } else {
                        "heartbeat_expired"
                    },
                    "",
                ));
            } else {
                let grace_ms = lost_after_ms - stale_after_ms;
                match heartbeat_verdict(silent_ms, false, stale_after_ms, grace_ms) {
                    Verdict::Lost => todo.push(lose("heartbeat_lost", LOST_NOTE)),
                    Verdict::Stale if !self.remarked.contains(&job.id) => {
                        todo.push(JobCommand::Note {
                            id: job.id.clone(),
                            note: STALE_NOTE.to_string(),
                        });
                    }
                    Verdict::Stale | Verdict::Keep => {}
                }
            }
        }
        todo
    }
}

/// What becomes of a running job nobody can be asked after, by how long it has been silent.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Verdict {
    /// It goes on.
    Keep,
    /// It is remarked on its timeline once, and not ended yet.
    Stale,
    /// It ends as lost (`heartbeat_lost`).
    Lost,
}

/// The Python Host's `job_heartbeat` (`policies/stale_work.py`): a job whose worker is
/// verified alive is never touched here; one silent past `stale_ms` is stale, and past
/// `stale_ms` and `grace_ms` lost. A `stale_ms` of nothing turns the rule off.
pub fn heartbeat_verdict(age_ms: i64, verified: bool, stale_ms: i64, grace_ms: i64) -> Verdict {
    if verified || stale_ms <= 0 {
        return Verdict::Keep;
    }
    if age_ms > stale_ms + grace_ms.max(0) {
        return Verdict::Lost;
    }
    if age_ms > stale_ms {
        return Verdict::Stale;
    }
    Verdict::Keep
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    /// A book that is driven the way the actor drives it: decide, write, apply.
    struct Driven {
        book: JobBook,
        written: Vec<JobRecord>,
        now_ms: i64,
        revision: u64,
    }

    impl Driven {
        fn new() -> Self {
            Self {
                book: JobBook::new(),
                written: Vec::new(),
                now_ms: 1_000,
                revision: 0,
            }
        }

        fn run(&mut self, command: JobCommand) -> JobChange {
            self.now_ms += 10;
            self.revision += 1;
            let change = self
                .book
                .decide(command, self.now_ms, self.revision)
                .unwrap();
            if let Some(record) = &change.record {
                self.written.push(record.clone());
                self.book.apply(record);
            }
            if let Some(beat) = &change.beat {
                self.book.beat(beat);
            }
            // What is in the log rebuilds the book, but for the signs of life.
            let mut rebuilt = JobBook::new();
            for record in &self.written {
                rebuilt.apply(record);
            }
            let plain = |book: &JobBook| {
                book.jobs()
                    .map(|job| {
                        let mut job = job.clone();
                        job.heartbeat_at_ms = None;
                        job.heartbeat_source.clear();
                        job.worker = Worker::none();
                        job
                    })
                    .collect::<Vec<_>>()
            };
            assert_eq!(plain(&rebuilt), plain(&self.book));
            change
        }

        fn register(&mut self, id: &str, worker: &Worker) -> JobChange {
            self.run(JobCommand::Register {
                queued: false,
                id: id.to_string(),
                kind: "build".to_string(),
                title: "Build the thing".to_string(),
                detail: String::new(),
                worker: worker.clone(),
                restart_cancelled: false,
                started_trace_id: "trace-1".to_string(),
                heartbeat_timeout_ms: None,
                metadata: Value::Null,
            })
        }
    }

    fn worker(pid: u32, token: &str) -> Worker {
        Worker {
            pid,
            start_token: token.to_string(),
        }
    }

    #[test]
    fn a_handle_names_a_run_and_a_bare_id_names_the_first() {
        assert_eq!(parse_handle("bg1:3:build-a"), ("build-a".to_string(), 3));
        assert_eq!(
            parse_handle("bg1:1:with:colons"),
            ("with:colons".to_string(), 1)
        );
        assert_eq!(parse_handle("build-a"), ("build-a".to_string(), 1));
        assert_eq!(
            parse_handle("bg1:x:build-a"),
            ("bg1:x:build-a".to_string(), 1)
        );
        assert_eq!(parse_handle("bg1:2:"), ("bg1:2:".to_string(), 1));
    }

    #[test]
    fn a_worker_registers_reports_and_finishes_its_run() {
        let mut d = Driven::new();
        let w = worker(4_242, "t-1");
        let job = d.register("build-a", &w).job.unwrap();
        assert_eq!(
            (job.handle().as_str(), job.status, job.generation),
            ("bg1:1:build-a", JobStatus::Running, 1)
        );
        assert_eq!(job.heartbeat_source, "worker_registration");
        // It says how far it is: one line, kept short, and a line of the timeline.
        let progress = d.run(JobCommand::Progress {
            id: "build-a".to_string(),
            generation: 1,
            text: "  tests\n3/10 ".to_string(),
        });
        assert_eq!(progress.job.unwrap().progress_text, "tests 3/10");
        assert_eq!(d.written.last().unwrap().note, "tests 3/10");
        // It names its log: kept, and not a line of the timeline.
        let log = d.run(JobCommand::SetLog {
            id: "build-a".to_string(),
            generation: 1,
            path: "/var/tmp/build.log".to_string(),
            worker_cwd: "relative".to_string(),
        });
        let logged = log.job.unwrap();
        assert_eq!(
            (logged.log_path.as_str(), logged.worker_cwd.as_str()),
            ("/var/tmp/build.log", "")
        );
        assert!(!d.written.last().unwrap().timeline);
        assert_eq!(
            d.book.decide(
                JobCommand::SetLog {
                    id: "build-a".to_string(),
                    generation: 1,
                    path: "build.log".to_string(),
                    worker_cwd: String::new()
                },
                1,
                1
            ),
            Err(JobRefusal::LogPathNotAbsolute("build.log".to_string()))
        );
        // A heartbeat is noted and not written.
        let before = d.written.len();
        let beat = d.run(JobCommand::Heartbeat {
            id: "build-a".to_string(),
            generation: Some(1),
            worker: w.clone(),
            source: "worker_heartbeat".to_string(),
        });
        assert!(beat.record.is_none() && beat.beat.is_some());
        assert_eq!(d.written.len(), before);
        assert_eq!(
            d.book.get("build-a").unwrap().heartbeat_source,
            "worker_heartbeat"
        );
        // It finishes.
        let done = d.run(JobCommand::Finish {
            id: "build-a".to_string(),
            generation: 1,
            worker: w.clone(),
            failed: None,
        });
        let done = done.job.unwrap();
        assert_eq!(done.status, JobStatus::Succeeded);
        assert_eq!(done.terminal_at_ms, Some(d.now_ms));
        // What is over stays as it ended.
        let again = d.run(JobCommand::Finish {
            id: "build-a".to_string(),
            generation: 1,
            worker: w,
            failed: Some("too late".to_string()),
        });
        assert!(again.record.is_none());
        assert_eq!(again.job.unwrap().status, JobStatus::Succeeded);
    }

    #[test]
    fn what_another_run_or_another_process_reports_changes_nothing() {
        let mut d = Driven::new();
        let w = worker(100, "t-1");
        d.register("watch", &w);
        let stranger = worker(200, "t-9");
        for command in [
            JobCommand::Finish {
                id: "watch".to_string(),
                generation: 2,
                worker: w.clone(),
                failed: None,
            },
            JobCommand::Finish {
                id: "watch".to_string(),
                generation: 1,
                worker: stranger.clone(),
                failed: None,
            },
            JobCommand::Heartbeat {
                id: "watch".to_string(),
                generation: Some(1),
                worker: worker(100, "t-other"),
                source: "worker_heartbeat".to_string(),
            },
            JobCommand::Progress {
                id: "watch".to_string(),
                generation: 7,
                text: "x".to_string(),
            },
        ] {
            let change = d.run(command);
            assert!(change.record.is_none() && change.beat.is_none() && change.job.is_none());
        }
        assert_eq!(d.book.get("watch").unwrap().status, JobStatus::Running);
        // A job nobody is named for takes a report from whoever knows its run.
        d.register("anyone", &Worker::none());
        let done = d.run(JobCommand::Finish {
            id: "anyone".to_string(),
            generation: 1,
            worker: stranger,
            failed: Some("exit 3".to_string()),
        });
        let done = done.job.unwrap();
        assert_eq!(
            (done.status, done.terminal_reason.as_str()),
            (JobStatus::Failed, "exit 3")
        );
    }

    #[test]
    fn an_id_is_used_again_as_a_new_run_with_a_clean_record() {
        let mut d = Driven::new();
        let first = worker(100, "t-1");
        d.register("portrait", &first);
        d.run(JobCommand::Progress {
            id: "portrait".to_string(),
            generation: 1,
            text: "half".to_string(),
        });
        // The same run says again that it is there: nothing is reset.
        let same = d.register("portrait", &first).job.unwrap();
        assert_eq!((same.generation, same.progress_text.as_str()), (1, "half"));
        // Another process registers the id: a new run, and the old one is fenced out.
        let second = worker(300, "t-2");
        let next = d.register("portrait", &second).job.unwrap();
        assert_eq!(
            (next.generation, next.progress_text.as_str(), next.status),
            (2, "", JobStatus::Running)
        );
        let late = d.run(JobCommand::Finish {
            id: "portrait".to_string(),
            generation: 1,
            worker: first,
            failed: None,
        });
        assert!(
            late.job.is_none(),
            "the first run no longer speaks for the job"
        );
        // A run that ended starts the count again too.
        d.run(JobCommand::Finish {
            id: "portrait".to_string(),
            generation: 2,
            worker: second.clone(),
            failed: None,
        });
        assert_eq!(d.register("portrait", &second).job.unwrap().generation, 3);
        assert_eq!(
            d.book.decide(
                JobCommand::Register {
                    queued: false,
                    id: "claude-task:7".to_string(),
                    kind: String::new(),
                    title: String::new(),
                    detail: String::new(),
                    worker: Worker::none(),
                    restart_cancelled: false,
                    started_trace_id: String::new(),
                    heartbeat_timeout_ms: None,
                    metadata: Value::Null
                },
                1,
                1
            ),
            Err(JobRefusal::ReservedId)
        );
    }

    #[test]
    fn a_cancel_ends_the_run_it_names_and_cancelled_stays_cancelled() {
        let mut d = Driven::new();
        let w = worker(100, "t-1");
        d.register("job", &w);
        let wrong = d.run(JobCommand::Cancel {
            id: "job".to_string(),
            expected_generation: Some(4),
            reason: "user_cancelled".to_string(),
        });
        assert!(wrong.generation_mismatch && wrong.record.is_none());
        let cancelled = d.run(JobCommand::Cancel {
            id: "job".to_string(),
            expected_generation: Some(1),
            reason: "owner_cancelled".to_string(),
        });
        let job = cancelled.job.unwrap();
        assert_eq!(
            (
                job.status,
                job.terminal_reason.as_str(),
                job.cancelled_at_ms
            ),
            (JobStatus::Cancelled, "owner_cancelled", Some(d.now_ms))
        );
        // Saying it again changes nothing, and neither does the worker registering again.
        assert!(
            d.run(JobCommand::Cancel {
                id: "job".to_string(),
                expected_generation: Some(1),
                reason: "again".to_string()
            })
            .record
            .is_none()
        );
        let still = d.register("job", &w);
        assert!(still.record.is_none());
        assert_eq!(still.job.unwrap().status, JobStatus::Cancelled);
        // Until it is restarted on purpose: then it is a new run.
        let restarted = d.run(JobCommand::Register {
            queued: false,
            id: "job".to_string(),
            kind: "build".to_string(),
            title: "Again".to_string(),
            detail: String::new(),
            worker: w,
            restart_cancelled: true,
            started_trace_id: String::new(),
            heartbeat_timeout_ms: Some(5),
            metadata: Value::Null,
        });
        let job = restarted.job.unwrap();
        assert_eq!((job.status, job.generation), (JobStatus::Running, 2));
        assert_eq!(job.cancelled_at_ms, None);
        assert_eq!(
            job.heartbeat_timeout_ms, 30_000,
            "a timeout is at least half a minute"
        );
        assert!(
            d.run(JobCommand::Cancel {
                id: "nobody".to_string(),
                expected_generation: None,
                reason: String::new()
            })
            .job
            .is_none()
        );
    }

    #[test]
    fn a_job_nobody_runs_any_more_is_lost_and_says_which_way() {
        let mut d = Driven::new();
        d.register("named", &worker(100, "t-1"));
        d.register("unnamed", &Worker::none());
        let minute = 60_000;
        let (stale, lost) = (15 * minute, 30 * minute);
        let gone = |_: &Worker| false;
        let there = |_: &Worker| true;
        // Its process is gone: lost at once, whatever its heartbeat says.
        let todo = d.book.overdue(d.now_ms + 1, stale, lost, &gone);
        assert_eq!(
            todo,
            vec![JobCommand::Lose {
                id: "named".to_string(),
                reason: "worker_vanished".to_string(),
                note: String::new()
            }]
        );
        // Its process is there and silent past its timeout: expired.
        let late = d.now_ms + DEFAULT_HEARTBEAT_TIMEOUT_MS + 1_000;
        assert!(
            d.book
                .overdue(late, stale, lost, &there)
                .contains(&JobCommand::Lose {
                    id: "named".to_string(),
                    reason: "heartbeat_expired".to_string(),
                    note: String::new()
                })
        );
        // Nobody to ask: a remark first, then lost. (Its own timeout here is longer.)
        d.run(JobCommand::Register {
            queued: false,
            id: "slow".to_string(),
            kind: "watch".to_string(),
            title: "Slow".to_string(),
            detail: String::new(),
            worker: Worker::none(),
            restart_cancelled: false,
            started_trace_id: String::new(),
            heartbeat_timeout_ms: Some(2 * 60 * minute),
            metadata: Value::Null,
        });
        let at = d.now_ms + stale + 1_000;
        assert!(
            d.book
                .overdue(at, stale, lost, &there)
                .contains(&JobCommand::Note {
                    id: "slow".to_string(),
                    note: "reconcile: heartbeat stale".to_string()
                })
        );
        // The remark is made once.
        d.run(JobCommand::Note {
            id: "slow".to_string(),
            note: STALE_NOTE.to_string(),
        });
        assert!(
            !d.book
                .overdue(at, stale, lost, &there)
                .iter()
                .any(|command| matches!(command, JobCommand::Note { .. }))
        );
        let at = d.now_ms + lost + 1_000;
        assert!(
            d.book
                .overdue(at, stale, lost, &there)
                .contains(&JobCommand::Lose {
                    id: "slow".to_string(),
                    reason: "heartbeat_lost".to_string(),
                    note: "reconcile: heartbeat lost".to_string()
                })
        );
        // Losing it is a change like any other, and a lost job is not lost twice.
        let lost_job = d
            .run(JobCommand::Lose {
                id: "slow".to_string(),
                reason: "heartbeat_lost".to_string(),
                note: "reconcile: heartbeat lost".to_string(),
            })
            .job
            .unwrap();
        assert_eq!(
            (lost_job.status, lost_job.terminal_reason.as_str()),
            (JobStatus::Failed, "heartbeat_lost")
        );
        assert!(LOST_REASONS.contains(&lost_job.terminal_reason.as_str()));
        assert!(
            !d.book
                .overdue(at + lost, stale, lost, &there)
                .iter()
                .any(|command| matches!(command, JobCommand::Lose { id, .. } if id == "slow"))
        );
        // A Host that comes back has heard from every running job at that moment.
        d.book.heard_at(late);
        assert!(d.book.overdue(late + 1, stale, lost, &there).is_empty());
    }
}
