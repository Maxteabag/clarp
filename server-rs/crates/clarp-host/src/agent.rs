//! One agent's thread: the actor, a mailbox in front of it, and the doing of what it decides.
//!
//! The thread is the only thing that touches the actor, so the actor needs no lock, and
//! everything an agent does costs that agent's time and nobody else's. It blocks on its own log
//! and its own files, which is why it is a thread and not a task of the server's runtime.
//!
//! When the actor reports that its log or files failed, the thread drops it and opens it again
//! from the log: the same path as after a restart, and the only way back to a known state.

use std::collections::BTreeMap;
use std::sync::Arc;
use std::sync::mpsc::{self, RecvTimeoutError};
use std::time::{Duration, Instant};

use clarp_actor::conversation::{Page, Versioned, Which};
use clarp_actor::{
    Action, Actor, ActorError, Changed, ImportedRow, Kept, Outputs, Profile, QueueState, Sent,
    Status, Waiting,
};
use clarp_backend::Backend;
use clarp_kernel::artifacts::decisions as requests;
use clarp_kernel::artifacts::{Artifact, ArtifactBook, Ctx, Maker, Refused, Shelf};
use clarp_kernel::conversation::{Entry, Message, TurnEnd};
use clarp_kernel::goals::{Change as GoalChange, GoalBook, Looked, Owner, Seen, TurnSeen, World};
use clarp_kernel::jobs::{Job, JobChange, JobCommand, JobRecord, JobRefusal, Worker as JobWorker};
use clarp_kernel::turn::{Activity, Admission, Origin, Outcome, Settled, TurnId};
use clarp_ports::clock::Clock;
use clarp_ports::codec::CodecSet;
use clarp_ports::log::LogEngine;
use tokio::sync::{oneshot, watch};

use crate::fleet::AgentRecord;
use crate::goaltool;
use crate::hub::Hub;
use crate::ids;
use crate::launcher::{self, Launcher, Told};
use crate::library::{self, Books, Done, Want};
use crate::pusher::{Pusher, TurnDone};
use crate::revisions::Revisions;
use crate::tools::ToolAnswer;
use crate::{process, wire};

/// The least time between two "the conversation changed" events for one agent. A reply that
/// grows is told a few times a second, and once more when it stops growing.
const TRANSCRIPT_EVERY: Duration = Duration::from_millis(250);

/// A job nobody is named for is remarked on after this long without a sign of life, and
/// given up after twice as long: the Python Host's `job_stale_after_minutes` and its grace.
const JOB_STALE_AFTER_MS: i64 = 15 * 60_000;
const JOB_LOST_AFTER_MS: i64 = 30 * 60_000;
/// How long a job that has ended is still shown.
pub const JOB_SHOWN_FOR_MS: i64 = 24 * 60 * 60_000;
/// The wall clock went this much further than the time that passed here: the machine slept.
const CLOCK_JUMP_MS: i64 = 5_000;

/// What an agent's view says before its thread has opened it for the first time.
pub const OPENING: &str = "the agent is being opened";

const REOPEN_FIRST: Duration = Duration::from_millis(200);
const REOPEN_MOST: Duration = Duration::from_secs(30);

/// The agent could not answer: its log or its files are failing, or it is closed.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Unavailable(pub String);

pub type Answer<T> = oneshot::Sender<Result<T, Unavailable>>;

/// What a Stop did.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Stopped {
    /// How many turns were told to end.
    pub told: u32,
    /// Where the queue stands after.
    pub queue: QueueState,
    /// How many goals that were being worked are paused now.
    pub goals_paused: usize,
}

/// What an agent's thread is asked.
pub enum Ask {
    Send {
        message: Message,
        admission: Admission,
        answer: Answer<Sent>,
    },
    Stop {
        by: String,
        answer: Answer<Stopped>,
    },
    /// Answers whether the mailbox was held, and where the queue stands after.
    Resume {
        by: String,
        answer: Answer<(bool, QueueState)>,
    },
    /// What waits, in the order it will run.
    Queue {
        answer: Answer<(Vec<Waiting>, QueueState)>,
    },
    /// Take over the conversation the agent had on another Host. Answers how many rows came,
    /// or why it cannot be done.
    Import {
        from: String,
        backend_session: Option<String>,
        rows: Vec<ImportedRow>,
        answer: Answer<Result<usize, String>>,
    },
    Withdraw {
        turn: TurnId,
        by: String,
        answer: Answer<bool>,
    },
    Tail {
        limit: usize,
        which: Which,
        answer: Answer<Page>,
    },
    After {
        revision: u64,
        limit: usize,
        which: Which,
        answer: Answer<Page>,
    },
    Before {
        id: String,
        limit: usize,
        which: Which,
        answer: Answer<Option<Page>>,
    },
    /// The agent's own label for what it is busy with beyond its turns. Empty takes it back.
    Label {
        text: String,
        answer: Answer<()>,
    },
    /// Something asked of one of the agent's background jobs. The inner error is a command
    /// that makes no sense.
    Job {
        command: JobCommand,
        answer: Answer<Result<JobChange, JobRefusal>>,
    },
    /// The agent's goal tool was called about this agent's goals. Answers what it prints.
    Goal {
        ask: goaltool::Ask,
        answer: Answer<ToolAnswer>,
    },
    /// Something is asked of the agent's artifacts: one is made or changed, a request to
    /// the user is asked, answered, discarded or taken back.
    Artifact {
        want: Box<Want>,
        answer: Answer<Result<Done, Refused>>,
    },
    /// The newest changes of a job, oldest first.
    JobTimeline {
        id: String,
        limit: usize,
        answer: Answer<Vec<JobRecord>>,
    },
    /// One thing of the conversation, by its id, as it is now.
    Row {
        id: String,
        answer: Answer<Option<Versioned>>,
    },
    /// From a turn's thread: the live turn's spool may have grown.
    SpoolChanged,
    /// From a turn's thread: nothing answers at the turn's shim any more.
    ShimGone {
        turn: TurnId,
    },
    /// From a turn's thread: no shim could be started.
    LaunchFailed {
        turn: TurnId,
        reason: String,
    },
    /// The agent was removed: the thread ends.
    Close,
}

/// What an agent is doing, as anyone may read it at any time without asking the agent.
#[derive(Debug, Clone, PartialEq)]
pub struct View {
    pub status: Status,
    /// The newest row of the conversation: what a list of chats shows of it.
    pub newest: Option<Versioned>,
    /// How the last turn ended, with what its tool said of itself.
    pub ended: Option<TurnEnd>,
    pub backend: &'static str,
    /// The agent's background jobs: the ones that run, and the ones that ended within a day.
    pub jobs: Vec<Job>,
    /// What the agent made and asks, and its goals: what the apps read across agents.
    pub books: Arc<Books>,
    /// Why the agent cannot answer right now, when it cannot.
    pub broken: Option<String>,
}

/// Everything an agent's thread is given.
pub struct Setup {
    pub record: AgentRecord,
    pub profile: Profile,
    pub backend: Arc<dyn Backend>,
    pub engine: Arc<dyn LogEngine>,
    pub codecs: CodecSet,
    pub launcher: Arc<Launcher>,
    pub clock: Arc<dyn Clock + Send + Sync>,
    pub hub: Arc<Hub>,
    pub revisions: Arc<Revisions>,
    /// How often an agent that has running jobs looks whether somebody still runs them.
    pub jobs_checked_every: Duration,
    /// The way to the phones that are not listening.
    pub pusher: Pusher,
}

/// The way to an agent's thread.
#[derive(Clone)]
pub struct AgentHandle {
    tx: mpsc::Sender<Ask>,
    view: watch::Receiver<Arc<View>>,
}

impl AgentHandle {
    async fn ask<T>(&self, make: impl FnOnce(Answer<T>) -> Ask) -> Result<T, Unavailable> {
        let (answer, reply) = oneshot::channel();
        self.tx
            .send(make(answer))
            .map_err(|_| Unavailable("the agent is closed".to_string()))?;
        reply
            .await
            .map_err(|_| Unavailable("the agent did not answer".to_string()))?
    }

    pub async fn send(&self, message: Message, admission: Admission) -> Result<Sent, Unavailable> {
        self.ask(|answer| Ask::Send {
            message,
            admission,
            answer,
        })
        .await
    }

    pub async fn stop(&self, by: String) -> Result<Stopped, Unavailable> {
        self.ask(|answer| Ask::Stop { by, answer }).await
    }

    pub async fn resume(&self, by: String) -> Result<(bool, QueueState), Unavailable> {
        self.ask(|answer| Ask::Resume { by, answer }).await
    }

    pub async fn queue(&self) -> Result<(Vec<Waiting>, QueueState), Unavailable> {
        self.ask(|answer| Ask::Queue { answer }).await
    }

    /// Takes over the conversation the agent had on another Host.
    pub async fn import(
        &self,
        from: String,
        backend_session: Option<String>,
        rows: Vec<ImportedRow>,
    ) -> Result<Result<usize, String>, Unavailable> {
        self.ask(|answer| Ask::Import {
            from,
            backend_session,
            rows,
            answer,
        })
        .await
    }

    pub async fn withdraw(&self, turn: TurnId, by: String) -> Result<bool, Unavailable> {
        self.ask(|answer| Ask::Withdraw { turn, by, answer }).await
    }

    pub async fn tail(&self, limit: usize, which: Which) -> Result<Page, Unavailable> {
        self.ask(|answer| Ask::Tail {
            limit,
            which,
            answer,
        })
        .await
    }

    pub async fn after(
        &self,
        revision: u64,
        limit: usize,
        which: Which,
    ) -> Result<Page, Unavailable> {
        self.ask(|answer| Ask::After {
            revision,
            limit,
            which,
            answer,
        })
        .await
    }

    pub async fn before(
        &self,
        id: String,
        limit: usize,
        which: Which,
    ) -> Result<Option<Page>, Unavailable> {
        self.ask(|answer| Ask::Before {
            id,
            limit,
            which,
            answer,
        })
        .await
    }

    /// Sets the agent's own label for what it is busy with beyond its turns.
    pub async fn label(&self, text: String) -> Result<(), Unavailable> {
        self.ask(|answer| Ask::Label { text, answer }).await
    }

    /// Asks something of one of the agent's background jobs.
    pub async fn job(
        &self,
        command: JobCommand,
    ) -> Result<Result<JobChange, JobRefusal>, Unavailable> {
        self.ask(|answer| Ask::Job { command, answer }).await
    }

    pub async fn job_timeline(
        &self,
        id: String,
        limit: usize,
    ) -> Result<Vec<JobRecord>, Unavailable> {
        self.ask(|answer| Ask::JobTimeline { id, limit, answer })
            .await
    }

    /// One thing of the agent's conversation, by its id. `None`: there is no such thing.
    pub async fn row(&self, id: String) -> Result<Option<Versioned>, Unavailable> {
        self.ask(|answer| Ask::Row { id, answer }).await
    }

    /// What the agent is doing, as it last said.
    /// Asks something of the agent's artifacts. The inner error is what the rules refuse.
    pub async fn artifact(&self, want: Want) -> Result<Result<Done, Refused>, Unavailable> {
        let want = Box::new(want);
        self.ask(|answer| Ask::Artifact { want, answer }).await
    }

    /// Answers the goal tool from this agent's goals.
    pub async fn goal(&self, ask: goaltool::Ask) -> Result<ToolAnswer, Unavailable> {
        self.ask(|answer| Ask::Goal { ask, answer }).await
    }

    pub fn view(&self) -> Arc<View> {
        self.view.borrow().clone()
    }

    /// Ends the agent's thread. Its turn, if one runs, is not touched.
    pub fn close(&self) {
        let _ = self.tx.send(Ask::Close);
    }
}

/// Starts an agent's thread. The agent is opened on that thread, not here.
pub fn spawn(setup: Setup) -> AgentHandle {
    let (tx, rx) = mpsc::channel();
    let idle = Status {
        live: None,
        last: None,
        waiting: Vec::new(),
        paused: false,
        queue_revision: 0,
        head_revision: 0,
        backend_session: None,
        label: String::new(),
        resting: None,
    };
    let (view_tx, view) = watch::channel(Arc::new(View {
        status: idle,
        newest: None,
        ended: None,
        backend: setup.backend.id(),
        jobs: Vec::new(),
        books: Arc::default(),
        broken: Some(OPENING.to_string()),
    }));
    let now_ms = setup.clock.now_ms();
    let worker = Worker {
        setup,
        actor: None,
        tx: tx.clone(),
        view: view_tx,
        told: None,
        newest: None,
        ended: None,
        transcript_sent: None,
        transcript_due: None,
        jobs_told: BTreeMap::new(),
        jobs_checked: Instant::now(),
        goals_looked_at: 0,
        books: Arc::default(),
        artifacts_told: BTreeMap::new(),
        clock_seen: (Instant::now(), now_ms),
    };
    let name = format!("agent-{}", worker.setup.record.session);
    let spawned = std::thread::Builder::new()
        .name(name)
        .spawn(move || worker.run(&rx));
    if let Err(error) = spawned {
        eprintln!("clarp-host: an agent's thread could not be started: {error}");
    }
    AgentHandle { tx, view }
}

struct Worker {
    setup: Setup,
    actor: Option<Actor>,
    /// The way to this thread, for the turns' threads.
    tx: mpsc::Sender<Ask>,
    view: watch::Sender<Arc<View>>,
    /// What the clients were last told, so that only a change is told.
    told: Option<Status>,
    /// The newest row of the conversation.
    newest: Option<Versioned>,
    /// How the last turn ended.
    ended: Option<TurnEnd>,
    transcript_sent: Option<Instant>,
    /// When a held-back "the conversation changed" is to be sent.
    transcript_due: Option<Instant>,
    /// The revision of each job the clients were last told.
    jobs_told: BTreeMap<String, u64>,
    /// When the running jobs were last looked at.
    jobs_checked: Instant,
    /// When the goals were last looked at, on the Host's clock. A goal that was due then and
    /// woke nobody is looked at again when something happens, not when the time comes.
    goals_looked_at: i64,
    /// The agent's books as they were last published: written anew when one of them changes.
    books: Arc<Books>,
    /// Each artifact as the clients were last told of it.
    artifacts_told: BTreeMap<String, ToldArtifact>,
    /// The two clocks as they were read together last: what tells a sleep of the machine.
    clock_seen: (Instant, i64),
}

/// The id of the message a goal's owner is woken with, from the wake's own id: it is a
/// message to the agent like any other.
fn wake_message_id(request_id: &str) -> String {
    format!("u-{request_id}")
}

/// A turn that ended, as the goals are told of it. A turn that was taken back before it
/// started is nothing the agent did.
fn seen_end(last: &Settled) -> Option<TurnSeen> {
    let (outcome, message) = match &last.outcome {
        Outcome::Completed => ("completed", String::new()),
        Outcome::Failed { reason } => ("failed", reason.clone()),
        Outcome::Interrupted { reason } => ("interrupted", reason.clone()),
        Outcome::Stopped { by } => ("interrupted", format!("Stopped by {by}")),
        Outcome::Withdrawn { .. } => return None,
    };
    Some(TurnSeen {
        outcome: outcome.to_string(),
        started: last.started_at_ms.is_some(),
        ended_at_ms: last.ended_at_ms,
        code: String::new(),
        message,
    })
}

/// What of an artifact a client is told when it changes: when it was last changed, what its
/// user did with it, and where its request stands.
type ToldArtifact = (i64, [Option<i64>; 3], Option<(i64, bool)>);

fn told_artifact(artifact: &Artifact) -> ToldArtifact {
    let request = artifact
        .decision
        .as_ref()
        .map(|decision| (decision.revision, decision.delivery_pending()));
    let by_user = [
        artifact.pinned_at,
        artifact.archived_at,
        artifact.deleted_at,
    ];
    (artifact.updated_at, by_user, request)
}

fn kept_goal(change: &GoalChange) -> Kept<'_> {
    Kept {
        goal: Some(change),
        artifacts: &[],
    }
}

/// The artifact a plan is shown as in the apps, as a change of the plan leaves it: made
/// with the plan, under the title the plan was given, and following its state from then on.
fn plan_shown(
    artifacts: &ArtifactBook,
    record: &AgentRecord,
    change: &GoalChange,
    title: Option<&str>,
    now_ms: i64,
) -> Option<Artifact> {
    let maker = Maker {
        agent_id: &record.agent_id,
        session: &record.session,
        persona: &record.persona,
    };
    let mut ctx = Ctx {
        now_ms,
        token_hex: &mut ids::hex,
        // An id of twelve random bytes is nobody else's.
        taken: &|_| false,
    };
    let given = title.unwrap_or(&change.plan.title);
    artifacts
        .plan_synced(maker, &change.plan, given, title.is_some(), &mut ctx)
        .ok()
}

/// Writes a change of a goal and, with it, the artifact its plan is shown as.
fn keep_goal(
    actor: &mut Actor,
    record: &AgentRecord,
    change: &GoalChange,
    title: Option<&str>,
    now_ms: i64,
) -> Result<Outputs, ActorError> {
    let shown = plan_shown(actor.artifacts(), record, change, title, now_ms);
    let kept = Kept {
        goal: Some(change),
        artifacts: shown.as_slice(),
    };
    actor.keep(&kept, now_ms)
}

/// Asks an agent's book of goals something, with the world around it as its actor has it.
fn with_goals<T>(
    actor: &Actor,
    now_ms: i64,
    ask: impl FnOnce(&GoalBook, &mut World<'_>) -> T,
) -> T {
    let status = actor.status();
    let jobs = actor.jobs();
    ask(
        actor.goals(),
        &mut World {
            now_ms,
            native_session: status.backend_session.as_deref().unwrap_or(""),
            queue_revision: status.queue_revision,
            token_hex: &mut ids::hex,
            has_job: &|id, generation| jobs.get(id).is_some_and(|job| job.generation == generation),
        },
    )
}

fn unavailable(error: &ActorError) -> Unavailable {
    Unavailable(error.to_string())
}

impl Worker {
    fn run(mut self, rx: &mpsc::Receiver<Ask>) {
        let mut wait = REOPEN_FIRST;
        loop {
            if self.actor.is_none() {
                match self.open() {
                    Ok(()) => wait = REOPEN_FIRST,
                    Err(why) => {
                        eprintln!(
                            "clarp-host: agent {} cannot be opened: {why}",
                            self.setup.record.session
                        );
                        self.show_broken(&why);
                        if !refuse_for(rx, wait, &why) {
                            return;
                        }
                        wait = (wait * 2).min(REOPEN_MOST);
                        continue;
                    }
                }
            }
            // An agent with running jobs wakes on its own now and then to look after them.
            let has_jobs = self
                .actor
                .as_ref()
                .is_some_and(|actor| actor.jobs().active().next().is_some());
            let jobs_due = has_jobs.then(|| self.jobs_checked + self.setup.jobs_checked_every);
            // And an agent with a goal that is due later wakes for that.
            let goals_due = self
                .actor
                .as_ref()
                .and_then(|actor| actor.goals().next_due())
                .filter(|due| *due > self.goals_looked_at)
                .map(|due| {
                    let wait = u64::try_from(due - self.now_ms()).unwrap_or(0);
                    Instant::now() + Duration::from_millis(wait)
                });
            // And an agent with a request that runs out wakes to close it.
            let requests_due = self
                .actor
                .as_ref()
                .and_then(|actor| actor.artifacts().next_expiry())
                .map(|due| {
                    let wait = u64::try_from(due - self.now_ms()).unwrap_or(0);
                    Instant::now() + Duration::from_millis(wait)
                });
            let due = [self.transcript_due, jobs_due, goals_due, requests_due]
                .into_iter()
                .flatten()
                .min();
            let ask = match due {
                None => match rx.recv() {
                    Ok(ask) => ask,
                    Err(_) => return,
                },
                Some(due) => match rx.recv_timeout(due.saturating_duration_since(Instant::now())) {
                    Ok(ask) => ask,
                    Err(RecvTimeoutError::Timeout) => {
                        let now = Instant::now();
                        if self.transcript_due.is_some_and(|due| due <= now) {
                            self.tell_transcript();
                        }
                        let mut looked = Ok(());
                        if jobs_due.is_some_and(|due| due <= now) {
                            looked = self.check_jobs();
                        }
                        if looked.is_ok() && goals_due.is_some_and(|due| due <= now) {
                            looked = self.check_goals();
                        }
                        if looked.is_ok() && requests_due.is_some_and(|due| due <= now) {
                            looked = self.settle_requests();
                        }
                        if let Err(error) = looked {
                            eprintln!(
                                "clarp-host: agent {} failed and is opened again: {error}",
                                self.setup.record.session
                            );
                            self.actor = None;
                        }
                        continue;
                    }
                    Err(RecvTimeoutError::Disconnected) => return,
                },
            };
            if matches!(ask, Ask::Close) {
                return;
            }
            // Whatever happened may have left a goal's owner resting, or ended what a goal
            // waits for.
            if let Err(error) = self.handle(ask).and_then(|()| self.check_goals()) {
                eprintln!(
                    "clarp-host: agent {} failed and is opened again: {error}",
                    self.setup.record.session
                );
                self.actor = None;
            }
        }
    }

    fn now_ms(&self) -> i64 {
        self.setup.clock.now_ms()
    }

    fn open(&mut self) -> Result<(), String> {
        let log = self
            .setup
            .engine
            .open(&self.setup.profile.dir, self.setup.codecs.clone())
            .map_err(|error| error.to_string())?;
        let (actor, out) = Actor::open(
            self.setup.profile.clone(),
            self.setup.backend.clone(),
            log,
            self.now_ms(),
        )
        .map_err(|error| error.to_string())?;
        self.actor = Some(actor);
        // What the agent is doing when it is opened is not news: a client that connects is
        // told to look at everything anyway. What follows from opening it is.
        self.told = self.actor.as_ref().map(Actor::status);
        // Nor are the jobs it has: only what changes from here on is told.
        self.jobs_told = self
            .actor
            .iter()
            .flat_map(|actor| actor.jobs().jobs())
            .map(|job| (job.id.clone(), job.revision))
            .collect();
        self.newest = self.newest_row();
        self.ended = self.last_end();
        // Nor are its artifacts: only what changes from here on is told. How many of its
        // requests wait for the user is counted from the start.
        self.refresh_books();
        self.artifacts_told = self
            .books
            .artifacts
            .artifacts()
            .map(|artifact| (artifact.artifact_id.clone(), told_artifact(artifact)))
            .collect();
        self.count_requests(self.now_ms());
        self.publish_view();
        self.after(out);
        // An answer that was given and not yet told when the Host went away is told now.
        self.settle_requests().map_err(|error| error.to_string())?;
        Ok(())
    }

    /// Reads the agent's books anew for those who read them without asking the agent.
    fn refresh_books(&mut self) {
        if let Some(actor) = &self.actor {
            self.books = Arc::new(Books {
                artifacts: actor.artifacts().clone(),
                goals: actor.goals().clone(),
            });
        }
    }

    /// Closes the requests whose time ran out and tells the agent every end that still
    /// waits to be told.
    fn settle_requests(&mut self) -> Result<(), ActorError> {
        let now = self.now_ms();
        let Some(actor) = self.actor.as_mut() else {
            return Ok(());
        };
        for out in library::settle(actor, now)? {
            self.after(out);
        }
        Ok(())
    }

    /// Tells the apps of every artifact that is not as they were last told, and how many
    /// requests wait for the user now.
    fn tell_artifacts(&mut self) {
        let now = self.now_ms();
        let record = &self.setup.record;
        let mut news = false;
        for artifact in self.books.artifacts.artifacts() {
            let state = told_artifact(artifact);
            if self.artifacts_told.get(&artifact.artifact_id) != Some(&state) {
                self.artifacts_told
                    .insert(artifact.artifact_id.clone(), state);
                let changed = wire::artifact_updated(record, &artifact.artifact_id);
                self.setup.hub.publish(changed, now);
                news = true;
            }
        }
        let in_all = self.count_requests(now);
        if news {
            self.setup.hub.publish(wire::attention_updated(in_all), now);
        }
    }

    /// Says how many of the agent's requests wait for the user, and answers how many wait
    /// in all.
    fn count_requests(&self, now: i64) -> usize {
        let record = &self.setup.record;
        let shelf = Shelf {
            maker: Maker {
                agent_id: &record.agent_id,
                session: &record.session,
                persona: &record.persona,
            },
            artifacts: &self.books.artifacts,
            goals: &self.books.goals,
        };
        let waiting = requests::attention(&[shelf], true, false, now).len();
        self.setup.hub.waiting_for_user(&record.agent_id, waiting)
    }

    /// Something is asked of the agent's artifacts. What changed is published before the
    /// one who asked is answered, so that what it reads next is what it was told.
    fn handle_artifact(
        &mut self,
        want: &Want,
        answer: Answer<Result<Done, Refused>>,
    ) -> Result<(), ActorError> {
        let now = self.now_ms();
        let Some(actor) = self.actor.as_mut() else {
            return Ok(());
        };
        match library::carry_out(actor, &self.setup.record, want, now) {
            Ok(carried) => {
                for out in carried.outs {
                    self.after(out);
                }
                let _ = answer.send(Ok(carried.said));
                Ok(())
            }
            Err(error) => {
                let _ = answer.send(Err(unavailable(&error)));
                Err(error)
            }
        }
    }

    /// The goal tool, or an app for its user, asks something of the agent's goals. What the
    /// tool prints of a change is true once the change is written, and it is published
    /// before the one who asked is answered: the plan it reads next, from the routes or in
    /// the library, is the plan it was told.
    fn handle_goal(
        &mut self,
        ask: &goaltool::Ask,
        answer: Answer<ToolAnswer>,
    ) -> Result<(), ActorError> {
        let now = self.now_ms();
        let Some(actor) = self.actor.as_mut() else {
            return Ok(());
        };
        let owner = Owner {
            agent_id: &self.setup.record.agent_id,
            session: &self.setup.record.session,
        };
        let (said, change) = with_goals(actor, now, |book, world| {
            goaltool::answer(book, owner, ask, world)
        });
        // A plan that is made is shown under the title it was given.
        let title = match ask {
            goaltool::Ask::Create { title, .. } => Some(title.trim()),
            _ => None,
        };
        let written = match &change {
            Some(change) => keep_goal(actor, &self.setup.record, change, title, now),
            None => Ok(Outputs::default()),
        };
        match written {
            Ok(out) => {
                self.after(out);
                let _ = answer.send(Ok(said));
                Ok(())
            }
            Err(error) => {
                let _ = answer.send(Err(unavailable(&error)));
                Err(error)
            }
        }
    }

    fn handle(&mut self, ask: Ask) -> Result<(), ActorError> {
        let ask = match ask {
            Ask::Artifact { want, answer } => return self.handle_artifact(&want, answer),
            Ask::Goal { ask, answer } => return self.handle_goal(&ask, answer),
            other => other,
        };
        let now = self.now_ms();
        let Some(actor) = self.actor.as_mut() else {
            return Ok(());
        };
        let (out, owed) = match ask {
            Ask::Send {
                message,
                admission,
                answer,
            } => answered(actor.send(&message, admission, now), answer)?,
            Ask::Stop { by, answer } => {
                // A Stop holds the agent's goals too: each one that is being worked is paused
                // until someone resumes it, so that none wakes the agent the user just stopped.
                let stopped = actor.stop(&by, now).and_then(|(told, mut out)| {
                    let reason = match by.as_str() {
                        "user" => "User stopped this agent".to_string(),
                        by => format!("Stopped by {by} (unverified)"),
                    };
                    let paused = with_goals(actor, now, |book, world| book.stopped(&reason, world));
                    for change in &paused {
                        let kept = keep_goal(actor, &self.setup.record, change, None, now)?;
                        out.changed.goals |= kept.changed.goals;
                        out.changed.artifacts |= kept.changed.artifacts;
                    }
                    Ok(((told, paused.len()), out))
                });
                let queue = actor.queue_state();
                let stopped = stopped.map(|((told, goals_paused), out)| {
                    let stopped = Stopped {
                        told,
                        queue,
                        goals_paused,
                    };
                    (stopped, out)
                });
                answered(stopped, answer)?
            }
            Ask::Resume { by, answer } => {
                let resumed = actor.resume(&by, now);
                let queue = actor.queue_state();
                answered(resumed.map(|(held, out)| ((held, queue), out)), answer)?
            }
            Ask::Queue { answer } => {
                let waiting = actor.queue().map(|waiting| (waiting, actor.queue_state()));
                return read(waiting, answer);
            }
            Ask::Import {
                from,
                backend_session,
                rows,
                answer,
            } => answered(actor.import(&from, backend_session, rows, now), answer)?,
            Ask::Withdraw { turn, by, answer } => answered(actor.withdraw(turn, &by, now), answer)?,
            Ask::Tail {
                limit,
                which,
                answer,
            } => return read(actor.tail_of(limit, which), answer),
            Ask::After {
                revision,
                limit,
                which,
                answer,
            } => return read(actor.after_of(revision, limit, which), answer),
            Ask::Before {
                id,
                limit,
                which,
                answer,
            } => return read(actor.before_of(&id, limit, which), answer),
            Ask::Label { text, answer } => {
                answered(actor.label(&text, now).map(|out| ((), out)), answer)?
            }
            Ask::Job { command, answer } => {
                let revision = self.setup.revisions.next(now);
                answered(actor.job(command, now, revision), answer)?
            }
            Ask::JobTimeline { id, limit, answer } => {
                return read(actor.job_timeline(&id, limit), answer);
            }
            Ask::Row { id, answer } => return read(actor.row(&id), answer),
            // Answered above, where what they change is published first.
            Ask::Artifact { .. } | Ask::Goal { .. } => return Ok(()),
            Ask::SpoolChanged => (actor.spool_changed(now)?, nobody()),
            Ask::ShimGone { turn } => (actor.shim_gone(turn, now)?, nobody()),
            Ask::LaunchFailed { turn, reason } => {
                (actor.launch_failed(turn, &reason, now)?, nobody())
            }
            Ask::Close => return Ok(()),
        };
        // What follows from a command is done and published before its answer is given.
        self.after(out);
        owed();
        Ok(())
    }

    /// Does what the actor decided, and tells the clients what changed.
    fn after(&mut self, out: Outputs) {
        if out.damaged_lines > 0 {
            eprintln!(
                "clarp-host: agent {}: {} damaged line(s) in a spool were passed over",
                self.setup.record.session, out.damaged_lines
            );
        }
        for action in out.actions {
            self.carry_out(action);
        }
        // The books are published before anything is told of them, and before whoever
        // asked for the change is answered.
        if out.changed.goals || out.changed.artifacts {
            self.refresh_books();
            self.publish_view();
        }
        if out.changed.artifacts {
            self.tell_artifacts();
        }
        self.show(out.changed);
    }

    /// Each turn has a thread of its own for as long as its shim lives. It does the waiting, so
    /// the agent's thread never does.
    fn carry_out(&self, action: Action) {
        let tx = self.tx.clone();
        let spawned = match action {
            Action::Launch(order) => {
                let launcher = self.setup.launcher.clone();
                let turn = order.turn;
                std::thread::Builder::new()
                    .name(format!("turn-{}", turn.0))
                    .spawn(move || {
                        launcher.launch_and_follow(&order, &|told| tell(&tx, turn, told))
                    })
            }
            Action::Follow { turn, socket } => std::thread::Builder::new()
                .name(format!("turn-{}", turn.0))
                .spawn(move || launcher::follow(&socket, &|told| tell(&tx, turn, told))),
            Action::Stop {
                socket, grace_ms, ..
            } => std::thread::Builder::new()
                .name("stop".to_string())
                .spawn(move || launcher::stop(&socket, grace_ms)),
        };
        if let Err(error) = spawned {
            eprintln!("clarp-host: a turn's thread could not be started: {error}");
        }
    }

    /// Looks whether somebody still runs the agent's running jobs, and ends the ones nobody
    /// does. A job whose process is gone drops out here at once, not a timeout later.
    /// Looks at the agent's goals: notes what happened around them, and wakes the agent for
    /// one that is due while it rests.
    fn check_goals(&mut self) -> Result<(), ActorError> {
        let now = self.now_ms();
        self.goals_looked_at = now;
        let Some(actor) = self.actor.as_mut() else {
            return Ok(());
        };
        let watched: Vec<String> = actor
            .goals()
            .watched()
            .map(|plan| plan.plan_id.clone())
            .collect();
        let mut outs = Vec::new();
        for plan_id in watched {
            // Read anew for every goal: a wake for one makes the agent busy for the next.
            let status = actor.status();
            // What a Stop left held is not work the agent still has: nothing runs it until
            // someone resumes it, and a goal that is being worked was resumed after that Stop.
            let resting = status.live.is_none() && (status.waiting.is_empty() || status.paused);
            // An agent that waits for an approval, or for an answer it said it cannot go on
            // without, is not woken to go on.
            let busy = if actor.artifacts().has_blocking_decision(now) {
                Some(("approval", "Waiting for an answer or approval"))
            } else if resting {
                None
            } else {
                Some(("working", "Owner has live or queued work"))
            };
            let last = status.last.as_ref().filter(|_| status.live.is_none());
            let rested = last.and_then(seen_end);
            let looked = {
                let actor = &*actor;
                let jobs = actor.jobs();
                let wake = |request: &str| {
                    let end = actor.end_of(&wake_message_id(request)).ok().flatten()?;
                    // Why a turn failed is known of the last one that ended.
                    let known = status.last.as_ref().filter(|last| last.id.0 == end.turn);
                    Some(known.and_then(seen_end).unwrap_or(TurnSeen {
                        outcome: end.status,
                        started: end.started_at_ms.is_some(),
                        ended_at_ms: end.ended_at_ms,
                        ..TurnSeen::default()
                    }))
                };
                actor.goals().look(
                    &plan_id,
                    &mut Seen {
                        now_ms: now,
                        session: &self.setup.record.session,
                        native_session: status.backend_session.as_deref().unwrap_or(""),
                        busy,
                        rested: rested.as_ref(),
                        job: &|id| jobs.get(id),
                        wake: &wake,
                        token_hex: &mut ids::hex,
                    },
                )
            };
            match looked {
                Looked::Nothing => {}
                Looked::Noted(change) => outs.push(actor.keep(&kept_goal(&change), now)?),
                Looked::Wake {
                    change,
                    request_id,
                    prompt,
                } => {
                    let message = Message {
                        id: wake_message_id(&request_id),
                        text: prompt,
                        origin: Origin::Trigger {
                            name: "automation".to_string(),
                        },
                        trace_id: Some(request_id),
                        sender: None,
                        client: None,
                    };
                    // Left for the agent, as the Python Host leaves it: it goes through the
                    // queue, where an app shows it while it waits. A hold that was placed
                    // before does not hold it: a goal that is being worked was resumed or made
                    // after the last Stop.
                    let woken =
                        actor.keep_with(&kept_goal(&change), &message, Admission::Later, now)?;
                    outs.push(woken.1);
                }
            }
        }
        for out in outs {
            self.after(out);
        }
        Ok(())
    }

    fn check_jobs(&mut self) -> Result<(), ActorError> {
        let checked = Instant::now();
        let now = self.now_ms();
        let (seen_at, seen_ms) = self.clock_seen;
        let passed_here = i64::try_from(checked.duration_since(seen_at).as_millis()).unwrap_or(0);
        let slept = (now - seen_ms) - passed_here > CLOCK_JUMP_MS;
        self.clock_seen = (checked, now);
        self.jobs_checked = checked;
        let Some(actor) = self.actor.as_mut() else {
            return Ok(());
        };
        if slept {
            // What was not heard while the machine slept says nothing about the workers.
            actor.jobs_heard_at(now);
            return Ok(());
        }
        let alive = |worker: &JobWorker| process::is_alive(worker.pid, &worker.start_token);
        let overdue = actor
            .jobs()
            .overdue(now, JOB_STALE_AFTER_MS, JOB_LOST_AFTER_MS, &alive);
        let mut all = Outputs::default();
        for command in overdue {
            let revision = self.setup.revisions.next(now);
            let (_, out) = actor.job(command, now, revision)?;
            all.changed.jobs |= out.changed.jobs;
        }
        self.after(all);
        Ok(())
    }

    /// The newest row of the conversation that a list of chats shows, as the actor has it
    /// now: the newest thing someone said, not a tool call and nothing automated.
    fn newest_row(&self) -> Option<Versioned> {
        let actor = self.actor.as_ref()?;
        // Nearly always it is the newest row itself. Otherwise look back, as far as the
        // Python Host does.
        let newest = actor.tail(1).ok()?.entries.into_iter().last()?;
        if wire::previewed(&newest) {
            return Some(newest);
        }
        let back = actor.tail(50).ok()?.entries;
        back.into_iter().rev().find(wire::previewed)
    }

    /// How the last turn ended, as the log has it.
    fn last_end(&self) -> Option<TurnEnd> {
        self.actor.as_ref()?.last_end().ok()?
    }

    fn publish_view(&self) {
        let Some(actor) = &self.actor else {
            return;
        };
        let shown_since = self.now_ms() - JOB_SHOWN_FOR_MS;
        self.view.send_replace(Arc::new(View {
            status: actor.status(),
            newest: self.newest.clone(),
            ended: self.ended.clone(),
            backend: actor.backend_id(),
            jobs: actor
                .jobs()
                .jobs()
                .filter(|job| job.status.is_active() || job.updated_at_ms >= shown_since)
                .cloned()
                .collect(),
            books: self.books.clone(),
            broken: None,
        }));
    }

    /// Publishes the agent's view and tells the clients what changed since they were last told.
    fn show(&mut self, changed: Changed) {
        if !(changed.conversation || changed.status || changed.queue || changed.jobs) {
            return;
        }
        let Some(actor) = &self.actor else {
            return;
        };
        let status = actor.status();
        let backend = actor.backend_id();
        let before = self.told.take();
        let was_live = before.as_ref().and_then(|told| told.live.as_ref());
        let was_waiting: &[TurnId] = before.as_ref().map_or(&[], |told| told.waiting.as_slice());
        // The ids of the messages that have joined the queue since the clients were last told.
        let joined: Vec<String> = if status
            .waiting
            .iter()
            .any(|turn| !was_waiting.contains(turn))
        {
            actor
                .queue()
                .unwrap_or_default()
                .into_iter()
                .filter(|waiting| !was_waiting.contains(&waiting.turn))
                .map(|waiting| waiting.message_id)
                .collect()
        } else {
            Vec::new()
        };
        if changed.conversation {
            self.newest = self.newest_row();
        }
        let turn_ended = status.last.as_ref().map(|last| last.id)
            != before
                .as_ref()
                .and_then(|told| told.last.as_ref())
                .map(|was| was.id);
        if turn_ended {
            self.ended = self.last_end();
        }
        let now = self.now_ms();
        let (hub, record) = (self.setup.hub.clone(), self.setup.record.clone());

        // The queue: a message joined it, one left it to run, it was held or let go.
        for message_id in &joined {
            hub.publish(
                wire::queue_updated(&record, &status, Some(message_id), false),
                now,
            );
        }
        // A turn began: the word a send is answered with, for every client that did not send it.
        if let Some(live) = &status.live
            && was_live.map(|was| was.turn) != Some(live.turn)
        {
            // It left the queue to run: it had waited there, or it was left for later and
            // nothing was in its way.
            if live.from_queue {
                hub.publish(
                    wire::queue_updated(&record, &status, Some(&live.message_id), true),
                    now,
                );
            }
            hub.publish(wire::dispatched(&record, live, status.waiting.len()), now);
        }
        if before
            .as_ref()
            .is_some_and(|told| told.paused != status.paused)
        {
            hub.publish(wire::queue_updated(&record, &status, None, false), now);
        }
        // What the agent is doing changed: its state, and the line that describes it.
        if before.as_ref().map(told_state) != Some(told_state(&status)) {
            if turn_ended {
                // The agent as it stood when the turn had ended and the next had not begun.
                let rested = Status {
                    live: None,
                    ..status.clone()
                };
                if let Some(at_once) = wire::stopped(&record, &rested) {
                    hub.publish(at_once, now);
                }
                if status.live.is_some() {
                    // The next turn began in the same breath. How the last one ended is told
                    // first: a client that waits for an agent to be done hears that it was.
                    hub.publish(
                        wire::state(&record, &rested, backend, self.ended.as_ref()),
                        now,
                    );
                    hub.publish(wire::activity(&record, &rested), now);
                }
            }
            hub.publish(
                wire::state(&record, &status, backend, self.ended.as_ref()),
                now,
            );
            hub.publish(wire::activity(&record, &status), now);
        }
        // A turn ended with an answer: the user is told, once.
        let answered = status
            .last
            .as_ref()
            .filter(|last| turn_ended && last.outcome == Outcome::Completed);
        // The answer is the row that closed the turn. It need not be the newest row: a message
        // that was left for later enters the conversation in the same breath.
        let answer = answered.and_then(|ended| {
            let end = self.ended.as_ref().filter(|end| end.turn == ended.id.0)?;
            let closed = actor.row(&end.closes).ok()??;
            matches!(closed.entry, Entry::Reply(_)).then_some((ended, closed))
        });
        if let Some((ended, reply)) = &answer {
            // The message the turn answered, and the app it came from: a user who asked at
            // the desk is not paged.
            let (message_id, client) = actor.message_of(ended).map_or_else(
                |_| (String::new(), String::new()),
                |message| (message.id, message.client.unwrap_or_default()),
            );
            let asked = wire::Asked {
                message_id: &message_id,
                client: &client,
            };
            let id = format!("pn-{}", ids::hex(6));
            // A reply with nothing for the user in it is not something to be told of, and
            // neither is what agents say to each other.
            if let Some(told) =
                wire::user_notification(&record, &id, &asked, reply, ended.ended_at_ms)
            {
                // A phone that is not listening is pushed to, when the rule says so. With a
                // sound when the agent still waits for an answer from the user.
                if told["push"] == true {
                    self.setup.pusher.turn_done(TurnDone {
                        session: record.session.clone(),
                        persona: record.persona.clone(),
                        notification_id: id.clone(),
                        preview: told["preview"].as_str().unwrap_or("").to_string(),
                        needs_response: actor.artifacts().has_pending_decision(now),
                    });
                }
                hub.publish(told, now);
            }
        }
        // A job changed: the clients are told the job as it now stands, once for each change.
        if changed.jobs {
            for job in actor.jobs().jobs() {
                if self.jobs_told.get(&job.id) != Some(&job.revision) {
                    self.jobs_told.insert(job.id.clone(), job.revision);
                    hub.publish(wire::job_updated(&record, job, now), now);
                }
            }
        }
        self.told = Some(status);
        self.publish_view();
        if changed.conversation {
            let now = Instant::now();
            match self.transcript_sent {
                Some(sent) if now.duration_since(sent) < TRANSCRIPT_EVERY => {
                    self.transcript_due = Some(sent + TRANSCRIPT_EVERY);
                }
                _ => self.tell_transcript(),
            }
        }
    }

    fn tell_transcript(&mut self) {
        self.transcript_due = None;
        self.transcript_sent = Some(Instant::now());
        let now = self.now_ms();
        self.setup
            .hub
            .publish(wire::transcript_updated(&self.setup.record), now);
    }

    fn show_broken(&self, why: &str) {
        let mut view = (**self.view.borrow()).clone();
        view.broken = Some(why.to_string());
        self.view.send_replace(Arc::new(view));
    }
}

/// What a client is shown as an agent's state: which turn runs and what it does, or which
/// turn ended last. A change in any of it is told.
fn told_state(status: &Status) -> (Option<TurnId>, String, String, Option<TurnId>, String) {
    let live = status.live.as_ref();
    let tool = match live.map(|live| &live.activity) {
        Some(Activity::Tool { name }) => name.clone(),
        _ => String::new(),
    };
    (
        live.map(|live| live.turn),
        wire::state_name(&wire::state_kind(status)),
        tool,
        status.last.as_ref().map(|last| last.id),
        status.label.clone(),
    )
}

/// Passes what a turn's thread says on to the agent's thread.
fn tell(tx: &mpsc::Sender<Ask>, turn: TurnId, told: Told) {
    let ask = match told {
        Told::SpoolChanged => Ask::SpoolChanged,
        Told::Gone => Ask::ShimGone { turn },
        Told::Failed(reason) => Ask::LaunchFailed { turn, reason },
    };
    let _ = tx.send(ask);
}

/// The answer that is owed to whoever asked for a command, to be given once what follows
/// from the command is done and published: what they read next is what they were told.
type Owed = Box<dyn FnOnce() + Send>;

/// Nobody asked: nothing is owed.
fn nobody() -> Owed {
    Box::new(|| {})
}

/// The result of a command, for the thread to go on with what follows from it, and the
/// answer that is owed for it. A command that failed is answered at once.
fn answered<T: Send + 'static>(
    result: Result<(T, Outputs), ActorError>,
    answer: Answer<T>,
) -> Result<(Outputs, Owed), ActorError> {
    match result {
        Ok((value, out)) => {
            let owed: Owed = Box::new(move || {
                let _ = answer.send(Ok(value));
            });
            Ok((out, owed))
        }
        Err(error) => {
            let _ = answer.send(Err(unavailable(&error)));
            Err(error)
        }
    }
}

/// Answers a question that changes nothing.
fn read<T>(result: Result<T, ActorError>, answer: Answer<T>) -> Result<(), ActorError> {
    match result {
        Ok(value) => {
            let _ = answer.send(Ok(value));
            Ok(())
        }
        Err(error) => {
            let _ = answer.send(Err(unavailable(&error)));
            Err(error)
        }
    }
}

/// While an agent cannot be opened, whoever asks is told why instead of being left waiting.
/// Returns false when the agent was closed meanwhile.
fn refuse_for(rx: &mpsc::Receiver<Ask>, wait: Duration, why: &str) -> bool {
    let until = Instant::now() + wait;
    loop {
        let refusal = || Unavailable(format!("the agent cannot be opened: {why}"));
        match rx.recv_timeout(until.saturating_duration_since(Instant::now())) {
            Ok(Ask::Send { answer, .. }) => drop(answer.send(Err(refusal()))),
            Ok(Ask::Stop { answer, .. }) => drop(answer.send(Err(refusal()))),
            Ok(Ask::Resume { answer, .. }) => drop(answer.send(Err(refusal()))),
            Ok(Ask::Queue { answer }) => drop(answer.send(Err(refusal()))),
            Ok(Ask::Import { answer, .. }) => drop(answer.send(Err(refusal()))),
            Ok(Ask::Withdraw { answer, .. }) => drop(answer.send(Err(refusal()))),
            Ok(Ask::Tail { answer, .. } | Ask::After { answer, .. }) => {
                drop(answer.send(Err(refusal())));
            }
            Ok(Ask::Before { answer, .. }) => drop(answer.send(Err(refusal()))),
            Ok(Ask::Label { answer, .. }) => drop(answer.send(Err(refusal()))),
            Ok(Ask::Job { answer, .. }) => drop(answer.send(Err(refusal()))),
            Ok(Ask::JobTimeline { answer, .. }) => drop(answer.send(Err(refusal()))),
            Ok(Ask::Row { answer, .. }) => drop(answer.send(Err(refusal()))),
            Ok(Ask::Goal { answer, .. }) => drop(answer.send(Err(refusal()))),
            Ok(Ask::Artifact { answer, .. }) => drop(answer.send(Err(refusal()))),
            Ok(Ask::SpoolChanged | Ask::ShimGone { .. } | Ask::LaunchFailed { .. }) => {}
            Ok(Ask::Close) | Err(RecvTimeoutError::Disconnected) => return false,
            Err(RecvTimeoutError::Timeout) => return true,
        }
    }
}
