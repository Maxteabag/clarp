//! Jobs owned by the Host rather than by an agent. Fleet serializes their transitions
//! with agent job-id claims. Each write replaces the job and its visible timeline together.
use super::*;
use clarp_kernel::jobs::{Job, JobBook, JobChange, JobCommand, JobRecord};

/// What this desk keeps, declared once (`kept.rs`: the registry walks it at every start).
pub(crate) const KEPT: &[crate::kept::Kind] = &[crate::kept::Kind::book(
    "computer-jobs",
    crate::kept::Key::Plain,
    crate::kept::fits::<ComputerJob>,
)];

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
        left: &crate::kept::LeftOut,
    ) -> Result<Self, String> {
        let mut jobs = Self {
            computer_id,
            ..Self::default()
        };
        for (id, raw) in store.list(TABLE).map_err(|e| e.to_string())? {
            // A copy set aside, known by the count, is no job (`kept.rs`); the record it was moved
            // from keeps its id taken.
            if left.is_left_out(TABLE, &id) {
                if let Some(moved_from) = crate::kept::copy_of(&id) {
                    jobs.unreadable.insert(moved_from.to_string());
                }
                continue;
            }
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
        // What could not be read was set aside, counted and said by the start's walk; one whose id
        // is not its key is kept out too, and its id stays taken.
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
        writes: Vec<clarp_ports::kv::Write>,
    ) -> Result<Result<JobChange, ComputerJobRefusal>, Unavailable> {
        let now = self.now_ms();
        self.change_computer_job_at(command, writes, now)
    }

    pub(super) fn change_computer_job_at(
        &mut self,
        command: JobCommand,
        mut writes: Vec<clarp_ports::kv::Write>,
        now: i64,
    ) -> Result<Result<JobChange, ComputerJobRefusal>, Unavailable> {
        let id = command.id().trim();
        if let Some(agent_id) = self.jobs.get(id) {
            return Ok(Err(ComputerJobRefusal::AgentOwned {
                id: id.to_owned(),
                agent_id: agent_id.clone(),
            }));
        }
        let before = self.computer_jobs.book.get(id).map(|job| (job.generation, job.status));
        let deferred = writes.iter().any(|(table, _, _)| table == &shelf(super::portrait_jobs::PENDING));
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
            // A successful publication has a durable delivery that announces only after the
            // gallery is updated. Other terminal transitions can announce after this commit.
            if job.kind == clarp_kernel::portrait_generation::KIND
                && job.status.is_terminal()
                && before.is_none_or(|(generation, status)| {
                    generation != job.generation || !status.is_terminal()
                })
                && !deferred
                && let Some(announcer) = self.works.portrait_hints.clone()
            {
                let window = clarp_kernel::portrait_generation::MediaWindow::of(job);
                self.works.runtime.spawn(async move { announcer.tell(window).await });
            }
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

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod portrait_count_tests {
    use super::*;
    use clarp_kernel::jobs::Worker;
    #[test]
    fn portrait_attempts_count_their_own_kind_at_both_edges_of_the_day() {
        let mut jobs = ComputerJobs::default();
        let cutoff = 10_000;
        for (id, kind, now, queued) in [
            ("earlier", "portrait-generation", cutoff - 1, true),
            ("boundary", "portrait-generation", cutoff, true),
            ("later", "portrait-generation", cutoff + 1, true),
            ("contact", "persona-avatar", cutoff + 1, true),
            ("other-event", "portrait-generation", cutoff + 1, false),
        ] {
            let command = JobCommand::Register {
                queued,
                id: id.to_string(),
                kind: kind.to_string(),
                title: String::new(),
                detail: String::new(),
                worker: Worker::none(),
                restart_cancelled: true,
                started_trace_id: String::new(),
                heartbeat_timeout_ms: None,
                metadata: serde_json::Value::Null,
            };
            let change = jobs.book.decide(command, now, 1).unwrap();
            jobs.timelines
                .entry(id.to_string())
                .or_default()
                .push(change.record.unwrap());
        }
        for (since, count) in [
            (cutoff - 1, 3),
            (cutoff, 2),
            (cutoff + 1, 1),
            (cutoff + 2, 0),
        ] {
            assert_eq!(jobs.begun_since("portrait-generation", since), count);
        }
        assert_eq!(jobs.begun_since("persona-avatar", cutoff), 1);
    }
}
