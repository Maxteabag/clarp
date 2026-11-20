//! Jobs owned by the Host rather than by an agent. Fleet serializes their transitions
//! with agent job-id claims. Each write replaces the job and its visible timeline together.
use super::*;
use clarp_kernel::jobs::{Job, JobBook, JobChange, JobCommand, JobRecord};

const TABLE: &str = "kept.computer-jobs";

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum JobOwner {
    Agent(String),
    Computer,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ComputerJobRefusal {
    Kernel(clarp_kernel::jobs::JobRefusal),
    AgentOwned { id: String, agent_id: String },
}

impl std::fmt::Display for ComputerJobRefusal {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Kernel(why) => std::fmt::Display::fmt(why, f),
            Self::AgentOwned { id, .. } => {
                write!(f, "job id already belongs to another owner: {id}")
            }
        }
    }
}

impl std::error::Error for ComputerJobRefusal {}

#[derive(Clone, serde::Serialize, serde::Deserialize)]
pub struct ComputerJob {
    pub job: Job,
    pub timeline: Vec<JobRecord>,
}

#[derive(Default)]
pub(super) struct ComputerJobs {
    computer_id: String,
    unreadable: std::collections::HashSet<String>,
    waiters: HashMap<String, (u64, watch::Sender<bool>)>,
    book: JobBook,
    timelines: HashMap<String, Vec<JobRecord>>,
}

impl ComputerJobs {
    pub(super) fn open(
        store: &dyn KeyValue,
        computer_id: String,
        revisions: &Revisions,
        log: &crate::unkept::Log,
    ) -> Result<Self, String> {
        let mut jobs = Self {
            computer_id,
            ..Self::default()
        };
        for (id, raw) in store.list(TABLE).map_err(|e| e.to_string())? {
            let Ok(row) = serde_json::from_slice::<ComputerJob>(&raw) else {
                jobs.unreadable.insert(id);
                continue;
            };
            if row.job.id != id {
                jobs.unreadable.insert(id);
                continue;
            }
            revisions.observe(row.job.revision);
            jobs.book.apply(&JobRecord {
                at_ms: row.job.updated_at_ms,
                job: row.job,
                note: String::new(),
                timeline: false,
            });
            jobs.timelines.insert(id, row.timeline);
        }
        if !jobs.unreadable.is_empty() {
            log.line(&format!(
                "clarp-host: skipped {} unreadable computer job records; original bytes retained",
                jobs.unreadable.len()
            ));
        }
        Ok(jobs)
    }
    pub(super) fn list(&self) -> Vec<Job> {
        self.book.jobs().cloned().collect()
    }
    pub(super) fn begun_since(&self, kind: &str, since: i64) -> u32 {
        let count = self
            .timelines
            .values()
            .flatten()
            .filter(|r| {
                r.job.kind == kind
                    && r.job.status == clarp_kernel::jobs::JobStatus::Queued
                    && r.at_ms >= since
            })
            .count();
        u32::try_from(count).unwrap_or(u32::MAX)
    }

    pub(super) fn contains(&self, id: &str) -> bool {
        self.book.get(id).is_some() || self.unreadable.contains(id)
    }
    pub(super) fn subscribe(
        &mut self,
        id: String,
        generation: u64,
    ) -> Option<watch::Receiver<bool>> {
        let job = self.book.get(&id)?;
        if job.generation != generation || !job.status.is_active() {
            return None;
        }
        let (_, sender) = self
            .waiters
            .entry(id)
            .or_insert_with(|| (generation, watch::channel(false).0));
        Some(sender.subscribe())
    }

    fn notify_producers(&mut self, job: &Job) {
        if let Some((generation, _)) = self.waiters.get(&job.id)
            && (*generation != job.generation || job.status.is_terminal())
            && let Some((_, sender)) = self.waiters.remove(&job.id)
        {
            sender.send_replace(true);
        }
    }

    pub(super) fn get(&self, id: &str) -> Option<ComputerJob> {
        Some(ComputerJob {
            job: self.book.get(id)?.clone(),
            timeline: self.timelines.get(id).cloned().unwrap_or_default(),
        })
    }
}

impl Fleet {
    pub(super) fn change_computer_job(
        &mut self,
        command: JobCommand,
        mut writes: Vec<clarp_ports::kv::Write>,
    ) -> Result<Result<JobChange, ComputerJobRefusal>, Unavailable> {
        let id = command.id().trim();
        if let Some(agent_id) = self.jobs.get(id) {
            return Ok(Err(ComputerJobRefusal::AgentOwned {
                id: id.to_owned(),
                agent_id: agent_id.clone(),
            }));
        }
        let now = self.now_ms();
        let mut change = match self.computer_jobs.book.decide(command, now, 0) {
            Ok(change) => change,
            Err(why) => return Ok(Err(ComputerJobRefusal::Kernel(why))),
        };
        if let (Some(record), Some(job)) = (&mut change.record, &mut change.job) {
            let revision = self.works.revisions.next(now);
            record.job.revision = revision;
            job.revision = revision;
        }
        let Some(job) = &change.job else {
            return Ok(Ok(change));
        };
        if change.record.is_none() && change.beat.is_none() {
            return Ok(Ok(change));
        }
        let mut timeline = self
            .computer_jobs
            .timelines
            .get(&job.id)
            .cloned()
            .unwrap_or_default();
        if let Some(entry) = &change.record
            && entry.timeline
        {
            if timeline.len() >= 200 {
                timeline.remove(0);
            }
            timeline.push(entry.clone());
        }
        let row = ComputerJob {
            job: job.clone(),
            timeline,
        };
        let bytes = serde_json::to_vec(&row).map_err(|e| Unavailable(e.to_string()))?;
        writes.push((TABLE.into(), job.id.clone(), Some(bytes)));
        self.store
            .put_together(&writes)
            .map_err(|e| Unavailable(e.to_string()))?;
        if self.computer_jobs.unreadable.remove(&job.id) {
            self.works
                .log
                .line("clarp-host: registration replaced an unreadable computer job record");
        }
        self.computer_jobs
            .timelines
            .insert(job.id.clone(), row.timeline);
        if let Some(record) = &change.record {
            self.computer_jobs.book.apply(record);
            self.computer_jobs.notify_producers(job);
            let computer_id = &self.computer_jobs.computer_id;
            self.works.hub.publish(serde_json::json!({
                "type":"background-job-updated", "change_revision":job.revision, "observed_at":now,
                "job_id":job.id,"session":"","agent_id":"","status":job.status.as_str(),
                "job":wire::computer_job(computer_id,job,now),
            }),now);
        }
        if let Some(beat) = &change.beat {
            self.computer_jobs.book.beat(beat);
        }
        Ok(Ok(change))
    }
}
