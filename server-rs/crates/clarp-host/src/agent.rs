//! One agent's thread: the actor, a mailbox in front of it, and the doing of what it decides.
//!
//! The thread is the only thing that touches the actor, so the actor needs no lock, and
//! everything an agent does costs that agent's time and nobody else's. It blocks on its own log
//! and its own files, which is why it is a thread and not a task of the server's runtime.
//!
//! When the actor reports that its log or files failed, the thread drops it and opens it again
//! from the log: the same path as after a restart, and the only way back to a known state.

use std::collections::{BTreeMap, VecDeque};
use std::path::PathBuf;
use std::sync::mpsc::{self, RecvTimeoutError};
use std::sync::{Arc, Condvar, Mutex, PoisonError};
use std::time::{Duration, Instant};

use clarp_actor::conversation::{Page, Versioned, Which};
use clarp_actor::{
    Action, Actor, ActorError, ByHand, Changed, ImportedRow, JoinedRead, Kept, Outputs, Profile,
    QueueState, Sent, ShimOrder, Status, TurnFate, Waiting,
};
use clarp_backend::Backend;
use clarp_kernel::artifacts::decisions as requests;
use clarp_kernel::artifacts::reports;
use clarp_kernel::artifacts::{Artifact, Ctx, Maker, Refused, Shelf};
use clarp_kernel::control::Control;
use clarp_kernel::conversation::{Entry, Message, TurnEnd};
use clarp_kernel::goals::{Change as GoalChange, GoalBook, Looked, Owner, Seen, TurnSeen, World};
use clarp_kernel::jobs::{Job, JobChange, JobCommand, JobRecord, JobRefusal, Worker as JobWorker};
use clarp_kernel::journal::Reworded;
use clarp_kernel::media::Asset;
use clarp_kernel::oracle::attention;
use clarp_kernel::spoken::{Line as SpokenLine, Said, SpeechBook};
use clarp_kernel::turn::{Activity, Admission, Origin, Outcome, Settled, TurnId};
use clarp_kernel::turn_identity::Standing;
use clarp_ports::blobs::Blobs;
use clarp_ports::clock::Clock;
use clarp_ports::codec::CodecSet;
use clarp_ports::log::LogEngine;
use serde_json::{Value, json};
use tokio::sync::{oneshot, watch};

use crate::fleet::{AgentRecord, non_empty};
use crate::goaltool;
use crate::hub::Hub;
use crate::ids;
use crate::launcher::{self, Launcher, Told};
use crate::library::{self, Books, Done, Want};
use crate::pusher::{Pusher, TurnDone};
use crate::revisions::Revisions;
use crate::tools::ToolAnswer;
use crate::voice::Speaks;
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

/// Caller identity established by the Host before the goal command reaches its owner.
#[derive(Clone, Debug, Default)]
pub enum GoalCaller {
    #[default]
    Default,
    Verified(String),
    Peer(String, bool),
    System,
}

/// A line that was said to its end, together with its engine and voice.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Spoke {
    pub bytes: u64,
    pub provider: String,
    pub voice: String,
}

/// What an agent's thread is asked.
pub enum DelegationCommand {
    Inspect(Option<String>),
    Baseline(usize),
    Applied(String, i64),
    ListenerRead(
        String,
        String,
        i64,
        Option<clarp_actor::bookkeeping::ActivityVersion>,
    ),
    ListenerWrite(
        String,
        String,
        i64,
        clarp_actor::bookkeeping::ListenerUpdate,
    ),
    #[cfg(test)]
    Receipt {
        id: String,
        caller: String,
    },
    Change(clarp_actor::delegation::Transition),
}

#[derive(Debug)]
pub struct DelegationState {
    pub activity: Option<clarp_kernel::goals::listener::Activity>,
    pub version: Option<clarp_actor::bookkeeping::ActivityVersion>,
    pub active: Option<clarp_actor::delegation::Binding>,
    pub prior: Option<(clarp_actor::delegation::Transition, i64)>,
    pub accepted: bool,
    pub baseline: i64,
    pub applied: Option<clarp_actor::bookkeeping::Applied>,
}

enum Ask {
    RefreshTelling,
    Bookkeeping {
        cmd: crate::bookkeeping::Command,
        answer: Answer<ToolAnswer>,
    },
    Delegation {
        command: DelegationCommand,
        answer: mpsc::Sender<Result<DelegationState, Unavailable>>,
    },
    VisitProvider(String),
    ExpireProviderLinks(String),
    HistoricalRead {
        source: Box<crate::helper_source::HelperSource>,
        visit: Option<clarp_kernel::provider_tasks::Visit>,
        generation: u64,
    },
    HelperRead {
        source: Box<crate::helper_source::HelperSource>,
        session: String,
        cwd: String,
        generation: u64,
        provider: Option<clarp_kernel::provider_tasks::Visit>,
        more_helpers: bool,
        changed: bool,
    },
    Send {
        reservation: Option<Value>,
        message: Message,
        admission: Admission,
        /// Whether it is another agent's, and whether its caller named its trace.
        marks: clarp_actor::Marks,
        answer: Answer<Result<Sent, clarp_actor::TraceTaken>>,
    },
    /// A note the agent leaves for its user from its running turn: what became of it, and the
    /// user's notification of it when one was made (`Err`: why nobody was told).
    LeaveNote {
        turn: u64,
        id: String,
        text: String,
        answer: Answer<(clarp_actor::Noted, Option<Result<Value, String>>)>,
    },
    Stop {
        by: Control,
        answer: Answer<Stopped>,
    },
    /// A turn nothing runs is looked for and given back, and a note of who and why is kept.
    Repair {
        by: Control,
        answer: Answer<()>,
    },
    /// Answers whether the mailbox was held, and where the queue stands after.
    Resume {
        by: String,
        answer: Answer<(bool, QueueState)>,
    },
    /// Starts the agent again: its next turn begins a conversation with its tool, and its
    /// conversation begins anew. Answers whether it was: not while a turn of it runs.
    StartAgain {
        answer: Answer<bool>,
    },
    /// What waits, in the order it will run.
    Queue {
        answer: Answer<(Vec<Waiting>, QueueState)>,
    },
    /// What became of the turn that answers a message, by the message's id.
    OpeningRow {
        id: String,
        answer: Answer<Value>,
    },
    Fate {
        message_id: String,
        answer: Answer<TurnFate>,
    },
    UnrunRequest {
        message_id: String,
        requester: String,
        answer: Answer<Option<String>>,
    },
    /// Where one of the agent's turns stands, by its number.
    Standing {
        turn: u64,
        answer: Answer<Standing>,
    },
    /// The trace of one of the agent's turns, when it is the running one now.
    RunningTrace {
        turn: u64,
        answer: Answer<Option<String>>,
    },
    /// Everything that is shown of the agent's turn as it runs: what `GET /live` answers.
    Shown {
        answer: Answer<Value>,
    },
    /// Take over the conversation the agent had on another Host. Answers how many rows came,
    /// or why it cannot be done.
    Import {
        from: String,
        backend_session: Option<String>,
        rows: Vec<ImportedRow>,
        answer: Answer<Result<usize, String>>,
    },
    /// Begin with a session the agent's tool kept on this machine. Answers how many rows
    /// came, or why it cannot be done.
    TakeUp {
        helpers: Option<Box<crate::helper_source::HelperSource>>,
        session: String,
        rows: Vec<ImportedRow>,
        answer: Answer<Result<usize, String>>,
    },
    Withdraw {
        turn: TurnId,
        by: String,
        answer: Answer<(bool, QueueState)>,
    },
    /// Send one waiting message now, past whatever holds the mailbox.
    SendNow {
        turn: TurnId,
        by: String,
        answer: Answer<ByHand>,
    },
    /// Change the words of a message that waits outside the conversation.
    Reword {
        turn: TurnId,
        text: String,
        answer: Answer<Reworded>,
    },
    /// The Host says something quiet in the conversation, once under its id.
    Note {
        id: String,
        text: String,
        answer: Answer<bool>,
    },
    KeeperEvents {
        plan: String,
        after: i64,
        answer: Answer<(i64, Vec<clarp_kernel::goals::ledger::Event>, bool)>,
    },
    GoalEvents {
        plan: String,
        after: i64,
        limit: usize,
        answer: Answer<(Vec<clarp_kernel::goals::ledger::Event>, bool)>,
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
    /// A label that outlived its time is taken back, if it is still the one set then.
    RetireLabel {
        set_at_ms: i64,
        answer: Answer<bool>,
    },
    /// Something asked of one of the agent's background jobs. The inner error is a command
    /// that makes no sense.
    Job {
        command: JobCommand,
        origin: Option<clarp_kernel::provider_tasks::RegistrationOrigin>,
        answer: Answer<Result<JobChange, JobRefusal>>,
    },
    /// The agent's goal tool was called about this agent's goals. Answers what it prints.
    Goal {
        ask: goaltool::Ask,
        caller: GoalCaller,
        answer: Answer<ToolAnswer>,
    },
    /// Something is asked of the agent's artifacts: one is made or changed, a request to
    /// the user is asked, answered, discarded or taken back.
    Artifact {
        want: Box<Want>,
        answer: Answer<Result<Done, Refused>>,
    },
    /// What the agent made and asked on the Host it comes from: each artifact as that Host
    /// showed it, a report with the versions it had. Answers how many there are now, or
    /// why one cannot be taken.
    TakeLibrary {
        artifacts: Vec<(Value, Vec<Value>)>,
        answer: Answer<Result<usize, String>>,
    },
    /// Holds the agent's mailbox without ending or pausing anything: what would start by
    /// itself waits for a resume. For an agent that has just arrived from another Host.
    Hold {
        answer: Answer<()>,
    },
    /// The plans the agent had on the Host it comes from, each as that Host showed it, with
    /// the documents of its goal. Answers how many there are now, or why one cannot be taken.
    TakePlans {
        source: String,
        plans: Vec<crate::edge::MovedPlan>,
        answer: Answer<Result<usize, String>>,
    },
    /// The agent published a file: its bytes are kept, and this is what is to be written
    /// of it.
    Publish {
        assets: Vec<Asset>,
        answer: Answer<()>,
    },
    /// The newest changes of a job, oldest first.
    JobRecord {
        id: String,
        answer: Answer<Option<Job>>,
    },
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
    /// The delivery fields of a `GET /log` answer, for the rows of its page.
    Delivery {
        turns: Vec<Value>,
        query: clarp_kernel::delivery::Query,
        scope: clarp_kernel::delivery::Scope,
        answer: Answer<(Vec<Value>, serde_json::Map<String, Value>)>,
    },
    /// The revision the conversation had at a time.
    RevisionAt {
        ts_ms: i64,
        answer: Answer<u64>,
    },
    /// The one that speaks takes the agent's next line. `None`: nothing of this agent's is
    /// to be said now.
    ClaimLine {
        answer: Answer<Option<SpokenLine>>,
    },
    /// The line that was taken has a clip: its number and its file, the engine and the
    /// voice that are asked first, and the event the clip was announced with. Answers
    /// whether it was still that one's line.
    LineBegun {
        id: String,
        clip: (u64, String),
        asked: (String, String),
        event_id: Option<u64>,
        answer: Answer<bool>,
    },
    /// The one that took a line is done with it: the clip it made of it, when it made
    /// one, and how it ended. Answers whether it was still that one's line.
    LineEnded {
        id: String,
        clip: Option<u64>,
        outcome: Result<Spoke, String>,
        answer: Answer<bool>,
    },
    /// A clip that was held back is announced with this event. Answers whether the agent
    /// has that clip.
    LineAnnounced {
        clip: u64,
        event_id: u64,
        /// To be played whichever agent is open.
        across: bool,
        answer: Answer<bool>,
    },
    /// An app says something of playing a clip. Answers whether the agent has that clip.
    LinePlayed {
        clip: u64,
        status: String,
        error: Option<String>,
        answer: Answer<bool>,
    },
    /// From a turn's thread: the live turn's spool may have grown.
    TakeoverHeld {
        offer: clarp_actor::takeover::Offer,
        held: clarp_shim::Held,
    },
    SpoolChanged,
    /// From a turn's thread: nothing answers at the shim of this attempt of the turn any more.
    ShimGone {
        turn: TurnId,
        attempt: u32,
    },
    /// From a turn's thread: no shim could be started for this attempt of the turn.
    LaunchFailed {
        turn: TurnId,
        attempt: u32,
        reason: String,
    },
    /// From the fleet: the user changed something about the agent. This is the agent as it
    /// now is. Nobody is refused: `taken` is done when the thread has it.
    Amended {
        record: Box<AgentRecord>,
        taken: oneshot::Sender<()>,
    },
    /// The login of the agent's tool has changed: a turn that waits for an account is tried
    /// on the one that is there now.
    LoginChanged,
    /// A terminal on the agent's own tool is to be opened: what the tool is started with
    /// there, of what the agent's turns are started with.
    TerminalFacts {
        answer: Answer<TerminalFacts>,
    },
    /// A terminal on the agent's own tool was opened, or closed. While one is open no
    /// turn's tool is started: two processes on one conversation would spoil it.
    Terminal {
        open: bool,
    },
    /// From the pool of the tool's accounts: nothing of the agent's turn may run while
    /// another login is put in place.
    AccountStop,
    /// From the pool: the agent's turn, which waited for an account, is taken up.
    AccountBack,
    /// From the pool: the agent's turn, which waited for an account whose plan offers its
    /// model, ends with what the provider said.
    AccountEnd,
    /// From the pool: the agent's turn waits for an account that serves its model, while
    /// the rest of the pool works.
    AccountHeld,
    /// From the explainer of the use of tools: fields to put on an item of what is shown.
    Explained {
        item: String,
        fields: Value,
    },
    /// The agent was removed: the thread ends.
    Close(CloseReply),
}

/// What an agent is doing, as anyone may read it at any time without asking the agent.
#[derive(Debug, Clone, PartialEq)]
pub struct View {
    pub latest_turn_start: i64,
    pub status: Status,
    /// The newest row of the conversation: what a list of chats shows of it.
    pub newest: Option<Versioned>,
    pub(crate) visible_previews: (Option<Versioned>, Option<Versioned>),
    pub(crate) working: Option<wire::WorkingWith>,
    pub(crate) can_leave_note: bool,
    /// How the last turn ended, with what its tool said of itself.
    pub ended: Option<TurnEnd>,
    /// What the agent last finished for its user, of which the user was told and to which
    /// neither he nor the Oracle wrote since: as the rule that shows it to the Oracle's
    /// voice takes it ([`attention::finished`]).
    pub finished: Option<Arc<Value>>,
    pub backend: &'static str,
    /// The agent's background jobs: the ones that run, and the ones that ended within a day.
    pub jobs: Vec<Job>,
    pub provider_mirrors: BTreeMap<String, Option<String>>,
    /// Idle native bindings and the last known runtime end, for the shared runner.
    pub provider_bindings: Vec<(String, Option<i64>, Option<i64>)>,
    /// What the agent made and asks, and its goals: what the apps read across agents.
    pub books: Arc<Books>,
    /// What the agent says aloud, and the clips it became: where a clip is looked for.
    pub speech: Arc<SpeechBook>,
    /// Why the agent cannot answer right now, when it cannot.
    pub broken: Option<String>,
}

impl View {
    pub fn mirrored_provider(&self, id: &str) -> bool {
        self.provider_mirrors.get(id).is_some_and(Option::is_some)
    }
}

/// Everything an agent's thread is given.
pub struct Setup {
    pub(crate) delegation_rejoin: crate::fleet::delegations::Rejoin,
    pub runtime: tokio::runtime::Handle,
    pub opening_gate: crate::calls::opening_launch::Gate,
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
    /// Where the pages of the agent's reports are kept.
    pub blobs: Arc<dyn Blobs>,
    /// The way to the one that speaks: told when the agent has something to say.
    pub speaks: Speaks,
    /// What the agent's tool is told, by what holds when it is asked.
    pub telling: crate::telling::Telling,
    /// The way to the tally of what turns used.
    pub tally: crate::usage::Tally,
    /// What signs the token each turn is given, which tells whose turn calls.
    pub turns: Arc<crate::turn_identity::Signer>,
    /// The pool of the accounts of the agent's tool, on a Host that runs the owner's
    /// selector itself and has one for this tool.
    pub pools: Vec<Arc<crate::pool::Service>>,
}

impl Setup {
    fn pool(&self) -> Option<&Arc<crate::pool::Service>> {
        self.pools
            .iter()
            .find(|pool| pool.tool() == self.record.backend)
    }
}

/// What an agent's tool is started with in a terminal, of what its turns are started with.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TerminalFacts {
    /// The tool's own session the agent's conversation is in, when it has one.
    pub tool_session: Option<String>,
    /// Where the agent works.
    pub cwd: std::path::PathBuf,
    /// What is set on top of the Host's environment for a turn of the agent.
    pub env: Vec<(String, String)>,
}

/// The way to an agent's thread.
#[derive(Clone)]
pub struct AgentHandle {
    tx: mpsc::Sender<Ask>,
    view: watch::Receiver<Arc<View>>,
    close: Arc<CloseState>,
}

#[derive(Default)]
struct CloseState {
    state: Mutex<Closing>,
    changed: Condvar,
}

impl CloseState {
    fn lock(&self) -> std::sync::MutexGuard<'_, Closing> {
        self.state.lock().unwrap_or_else(PoisonError::into_inner)
    }
}

#[derive(Clone, Copy, Default, PartialEq, Eq)]
enum Closing {
    #[default]
    Open,
    Preparing,
    Requested,
    Closed,
}

/// Dropping the reply also covers a thread that exits without receiving the request.
struct CloseReply(Arc<CloseState>);

impl Drop for CloseReply {
    fn drop(&mut self) {
        *self.0.lock() = Closing::Closed;
        self.0.changed.notify_all();
    }
}

/// How long a caller waits for an agent's thread to close. The fleet is one worker, and a close
/// is answered after the asks put before it: the same three seconds a delegation is given.
pub const CLOSE_WITHIN: Duration = Duration::from_secs(3);
pub(crate) const REMOVING: &str =
    "This agent is being removed. Ask to remove it again to finish removal.";
pub(crate) const REMOVING_CODE: &str = "agent_removing";

impl AgentHandle {
    pub(crate) fn refresh_telling(&self) {
        let _ = self.tx.send(Ask::RefreshTelling);
    }

    pub(crate) fn expire_provider_links(&self, native: &str) {
        let _ = self.tx.send(Ask::ExpireProviderLinks(native.to_owned()));
    }
    pub(crate) fn visit_provider(&self, native: &str) {
        let _ = self.tx.send(Ask::VisitProvider(native.to_owned()));
    }

    async fn ask<T>(&self, make: impl FnOnce(Answer<T>) -> Ask) -> Result<T, Unavailable> {
        let (answer, reply) = oneshot::channel();
        self.enqueue(make(answer))?;
        reply
            .await
            .map_err(|_| Unavailable("the agent did not answer".to_string()))?
    }

    pub(crate) fn accepting_work(&self) -> Result<(), Unavailable> {
        Self::check_open(*self.close.lock())
    }

    fn check_open(state: Closing) -> Result<(), Unavailable> {
        if state != Closing::Open {
            return Err(Unavailable(REMOVING.into()));
        }
        Ok(())
    }

    fn enqueue(&self, ask: Ask) -> Result<(), Unavailable> {
        // An ask either enters before Close or is refused, even through an old handle.
        let state = self.close.lock();
        Self::check_open(*state)?;
        self.tx
            .send(ask)
            .map_err(|_| Unavailable("the agent is closed".into()))
    }

    /// Fence admission before committing the tombstone. The fleet is the only caller.
    pub(crate) fn keep_removal(
        &self,
        keep: impl FnOnce() -> Result<(), Unavailable>,
    ) -> Result<(), Unavailable> {
        *self.close.lock() = Closing::Preparing;
        let kept = keep();
        if kept.is_err() {
            *self.close.lock() = Closing::Open;
        }
        kept
    }

    pub async fn send(&self, message: Message, admission: Admission) -> Result<Sent, Unavailable> {
        self.send_reserved(message, admission, None).await
    }

    pub async fn send_reserved(
        &self,
        message: Message,
        admission: Admission,
        reservation: Option<Value>,
    ) -> Result<Sent, Unavailable> {
        let marks = clarp_actor::Marks::default();
        match self
            .send_marked(message, admission, reservation, marks)
            .await?
        {
            Ok(sent) => Ok(sent),
            // A send whose caller named no trace is never refused for it.
            Err(clarp_actor::TraceTaken) => Err(Unavailable(clarp_actor::TRACE_TAKEN.to_string())),
        }
    }

    /// Sends as the Host decided it at the send ([`clarp_actor::Actor::send_marked`]): refused
    /// when the caller named a trace another message of the agent has.
    pub async fn send_marked(
        &self,
        message: Message,
        admission: Admission,
        reservation: Option<Value>,
        marks: clarp_actor::Marks,
    ) -> Result<Result<Sent, clarp_actor::TraceTaken>, Unavailable> {
        self.ask(|answer| Ask::Send {
            reservation,
            message,
            admission,
            marks,
            answer,
        })
        .await
    }

    /// Leaves a note for the user from the agent's turn `turn` under the row id `id`
    /// ([`clarp_actor::Actor::leave_note`]), and tells the user of it.
    pub async fn leave_note(
        &self,
        turn: u64,
        id: String,
        text: String,
    ) -> Result<(clarp_actor::Noted, Option<Result<Value, String>>), Unavailable> {
        self.ask(|answer| Ask::LeaveNote {
            turn,
            id,
            text,
            answer,
        })
        .await
    }

    pub async fn stop(&self, by: String) -> Result<Stopped, Unavailable> {
        self.stop_as(Control::by(by)).await
    }

    /// Stops the agent: who asks it, whether the Host knows it is they, and why.
    pub async fn stop_as(&self, by: Control) -> Result<Stopped, Unavailable> {
        self.ask(|answer| Ask::Stop { by, answer }).await
    }

    /// Gives back a turn of the agent that nothing runs, and keeps a note of who and why.
    pub async fn repair(&self, by: Control) -> Result<(), Unavailable> {
        self.ask(|answer| Ask::Repair { by, answer }).await
    }

    pub async fn resume(&self, by: String) -> Result<(bool, QueueState), Unavailable> {
        self.ask(|answer| Ask::Resume { by, answer }).await
    }

    /// Starts the agent again: a new conversation with its tool, and an empty one for those
    /// who read it. Answers whether it was: `false` while a turn of it runs, which whoever
    /// starts an agent again stops first.
    pub async fn start_again(&self) -> Result<bool, Unavailable> {
        self.ask(|answer| Ask::StartAgain { answer }).await
    }

    pub async fn queue(&self) -> Result<(Vec<Waiting>, QueueState), Unavailable> {
        self.ask(|answer| Ask::Queue { answer }).await
    }

    /// What became of the turn that answers the message with this id: asked by whoever
    /// handed the agent that message and follows what comes of it.
    pub async fn opening_row(&self, id: &str) -> Result<Value, Unavailable> {
        self.ask(|answer| Ask::OpeningRow {
            id: id.into(),
            answer,
        })
        .await
    }

    pub async fn fate(&self, message_id: String) -> Result<TurnFate, Unavailable> {
        self.ask(|answer| Ask::Fate { message_id, answer }).await
    }

    pub async fn unrun_request(
        &self,
        message_id: &str,
        requester: &str,
    ) -> Result<Option<String>, Unavailable> {
        self.ask(|answer| Ask::UnrunRequest {
            message_id: message_id.to_owned(),
            requester: requester.to_owned(),
            answer,
        })
        .await
    }

    /// Where one of the agent's turns stands, by its number: whether it runs, or when it ended,
    /// as its log says.
    pub async fn standing(&self, turn: u64) -> Result<Standing, Unavailable> {
        self.ask(|answer| Ask::Standing { turn, answer }).await
    }

    /// The trace of this turn when it is the agent's running turn now (`Actor::running_trace`).
    pub async fn running_trace(&self, turn: u64) -> Result<Option<String>, Unavailable> {
        self.ask(|answer| Ask::RunningTrace { turn, answer }).await
    }

    /// Everything that is shown of the agent's turn as it runs.
    pub async fn shown(&self) -> Result<Value, Unavailable> {
        self.ask(|answer| Ask::Shown { answer }).await
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

    /// Begins with a session the agent's tool kept on this machine: what was said in it,
    /// and the session to continue.
    pub async fn take_up(
        &self,
        session: String,
        rows: Vec<ImportedRow>,
        helpers: Option<Box<crate::helper_source::HelperSource>>,
    ) -> Result<Result<usize, String>, Unavailable> {
        self.ask(|answer| Ask::TakeUp {
            helpers,
            session,
            rows,
            answer,
        })
        .await
    }

    /// Takes a waiting message back. Answers whether it waited, and how the queue stands.
    pub async fn withdraw(
        &self,
        turn: TurnId,
        by: String,
    ) -> Result<(bool, QueueState), Unavailable> {
        self.ask(|answer| Ask::Withdraw { turn, by, answer }).await
    }

    /// Sends one waiting message now, past whatever holds the mailbox.
    pub async fn send_now(&self, turn: TurnId, by: String) -> Result<ByHand, Unavailable> {
        self.ask(|answer| Ask::SendNow { turn, by, answer }).await
    }

    /// Changes the words of a message that waits outside the conversation.
    pub async fn reword(&self, turn: TurnId, text: String) -> Result<Reworded, Unavailable> {
        self.ask(|answer| Ask::Reword { turn, text, answer }).await
    }

    /// Writes a quiet word of the Host's into the conversation, once under its id: that the
    /// agent was asked into a call, put on hold, that the call ended. Answers whether it was
    /// written now.
    pub async fn note(&self, id: String, text: String) -> Result<bool, Unavailable> {
        self.ask(|answer| Ask::Note { id, text, answer }).await
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

    /// Takes back the agent's label that outlived its time, if it is still the one set at
    /// `set_at_ms`. Answers whether it was taken back.
    pub async fn retire_label(&self, set_at_ms: i64) -> Result<bool, Unavailable> {
        self.ask(|answer| Ask::RetireLabel { set_at_ms, answer })
            .await
    }

    /// Asks something of one of the agent's background jobs.
    pub async fn job(
        &self,
        command: JobCommand,
    ) -> Result<Result<JobChange, JobRefusal>, Unavailable> {
        self.job_with_origin(command, None).await
    }

    pub async fn job_with_origin(
        &self,
        command: JobCommand,
        origin: Option<clarp_kernel::provider_tasks::RegistrationOrigin>,
    ) -> Result<Result<JobChange, JobRefusal>, Unavailable> {
        self.ask(|answer| Ask::Job {
            command,
            origin,
            answer,
        })
        .await
    }

    pub async fn job_record(&self, id: String) -> Result<Option<Job>, Unavailable> {
        self.ask(|answer| Ask::JobRecord { id, answer }).await
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
    pub async fn goal_events(
        &self,
        plan: String,
        after: i64,
        limit: usize,
    ) -> Result<(Vec<clarp_kernel::goals::ledger::Event>, bool), Unavailable> {
        self.ask(|answer| Ask::GoalEvents {
            plan,
            after,
            limit,
            answer,
        })
        .await
    }

    /// The keeper's first page and bounded starting cursor come from one actor ask.
    pub async fn keeper_events(
        &self,
        plan: String,
        after: i64,
    ) -> Result<(i64, Vec<clarp_kernel::goals::ledger::Event>, bool), Unavailable> {
        self.ask(|answer| Ask::KeeperEvents {
            plan,
            after,
            answer,
        })
        .await
    }

    pub async fn row(&self, id: String) -> Result<Option<Versioned>, Unavailable> {
        self.ask(|answer| Ask::Row { id, answer }).await
    }

    /// The delivery of the user sends of a page (send_delivery_state): the page's rows with
    /// theirs, and the fields of the answer, as the query asks.
    pub async fn delivery(
        &self,
        turns: Vec<Value>,
        query: clarp_kernel::delivery::Query,
        scope: clarp_kernel::delivery::Scope,
    ) -> Result<(Vec<Value>, serde_json::Map<String, Value>), Unavailable> {
        self.ask(|answer| Ask::Delivery {
            turns,
            query,
            scope,
            answer,
        })
        .await
    }

    /// The revision the agent's conversation had at a time: what is newer than it
    /// ([`Self::after`]) is what was written after that time.
    pub async fn revision_at(&self, ts_ms: i64) -> Result<u64, Unavailable> {
        self.ask(|answer| Ask::RevisionAt { ts_ms, answer }).await
    }

    /// Takes the agent's next line to be said. `None`: nothing of its is to be said now.
    pub async fn claim_line(&self) -> Result<Option<SpokenLine>, Unavailable> {
        self.ask(|answer| Ask::ClaimLine { answer }).await
    }

    /// Writes that a line that was taken has a clip. Answers whether it was still the
    /// taker's line.
    pub async fn line_begun(
        &self,
        id: String,
        clip: (u64, String),
        asked: (String, String),
        event_id: Option<u64>,
    ) -> Result<bool, Unavailable> {
        self.ask(|answer| Ask::LineBegun {
            id,
            clip,
            asked,
            event_id,
            answer,
        })
        .await
    }

    /// Writes how a line that was taken ended. Answers whether it was still the taker's.
    pub async fn line_ended(
        &self,
        id: String,
        clip: Option<u64>,
        outcome: Result<Spoke, String>,
    ) -> Result<bool, Unavailable> {
        self.ask(|answer| Ask::LineEnded {
            id,
            clip,
            outcome,
            answer,
        })
        .await
    }

    /// Writes that a clip that was held back is announced, and with which event. Answers
    /// whether the agent has that clip.
    pub async fn line_announced(
        &self,
        clip: u64,
        event_id: u64,
        across: bool,
    ) -> Result<bool, Unavailable> {
        self.ask(|answer| Ask::LineAnnounced {
            clip,
            event_id,
            across,
            answer,
        })
        .await
    }

    /// Writes what an app says of playing a clip. Answers whether the agent has that clip.
    pub async fn line_played(
        &self,
        clip: u64,
        status: String,
        error: Option<String>,
    ) -> Result<bool, Unavailable> {
        self.ask(|answer| Ask::LinePlayed {
            clip,
            status,
            error,
            answer,
        })
        .await
    }

    /// What the agent is doing, as it last said.
    /// Asks something of the agent's artifacts. The inner error is what the rules refuse.
    pub async fn artifact(&self, want: Want) -> Result<Result<Done, Refused>, Unavailable> {
        let want = Box::new(want);
        self.ask(|answer| Ask::Artifact { want, answer }).await
    }

    /// Gives the agent the library it had where it comes from. The inner error is an
    /// artifact that cannot be taken.
    pub async fn take_library(
        &self,
        artifacts: Vec<(Value, Vec<Value>)>,
    ) -> Result<Result<usize, String>, Unavailable> {
        self.ask(|answer| Ask::TakeLibrary { artifacts, answer })
            .await
    }

    /// Holds the agent's mailbox: nothing starts by itself until it is resumed.
    pub async fn hold(&self) -> Result<(), Unavailable> {
        self.ask(|answer| Ask::Hold { answer }).await
    }

    /// Gives the agent the plans it had where it comes from. The inner error is a plan
    /// that cannot be taken.
    pub(crate) async fn take_plans(
        &self,
        source: String,
        plans: Vec<crate::edge::MovedPlan>,
    ) -> Result<Result<usize, String>, Unavailable> {
        self.ask(|answer| Ask::TakePlans {
            source,
            plans,
            answer,
        })
        .await
    }

    /// Writes a file the agent published into its log. Its bytes are kept already.
    pub async fn publish(&self, asset: Asset) -> Result<(), Unavailable> {
        self.ask(|answer| Ask::Publish {
            assets: vec![asset],
            answer,
        })
        .await
    }

    pub(crate) async fn publish_portraits(&self, assets: [Asset; 2]) -> Result<(), Unavailable> {
        self.ask(|answer| Ask::Publish {
            assets: Vec::from(assets),
            answer,
        })
        .await
    }

    /// Answers the goal tool from this agent's goals.
    pub(crate) async fn bookkeeping(
        &self,
        cmd: crate::bookkeeping::Command,
    ) -> Result<ToolAnswer, Unavailable> {
        self.ask(|answer| Ask::Bookkeeping { cmd, answer }).await
    }

    pub async fn goal(&self, ask: goaltool::Ask) -> Result<ToolAnswer, Unavailable> {
        self.goal_as(ask, GoalCaller::Default).await
    }
    pub async fn goal_as(
        &self,
        ask: goaltool::Ask,
        caller: GoalCaller,
    ) -> Result<ToolAnswer, Unavailable> {
        self.ask(|answer| Ask::Goal {
            ask,
            caller,
            answer,
        })
        .await
    }

    pub fn view(&self) -> Arc<View> {
        self.view.borrow().clone()
    }

    /// Tells the agent's thread what the agent now is: its name, whether it is muted or put
    /// away, its model. Does not wait; `taken` is done when the thread has it.
    pub fn amended(&self, record: AgentRecord, taken: oneshot::Sender<()>) {
        let _ = self.tx.send(Ask::Amended {
            record: Box::new(record),
            taken,
        });
    }

    /// Tells the agent's thread that the login of its tool has changed. Does not wait.
    pub fn login_changed(&self) {
        let _ = self.tx.send(Ask::LoginChanged);
    }

    /// What a terminal on the agent's own tool is started with.
    pub async fn terminal_facts(&self) -> Result<TerminalFacts, Unavailable> {
        self.ask(|answer| Ask::TerminalFacts { answer }).await
    }

    /// Tells the agent's thread that a terminal on its tool was opened, or closed. Does
    /// not wait.
    pub fn terminal(&self, open: bool) {
        let _ = self.tx.send(Ask::Terminal { open });
    }

    /// Tells the agent's thread that nothing of its tool may run for a while: the account
    /// is out, and another is about to be put in place. Does not wait.
    pub fn stop_for_an_account(&self) {
        let _ = self.tx.send(Ask::AccountStop);
    }

    /// Tells the agent's thread that its turn, which waited for an account, is taken up.
    /// Does not wait.
    pub fn account_back(&self) {
        let _ = self.tx.send(Ask::AccountBack);
    }

    /// Tells the agent's thread that its turn waits for an account that serves its model,
    /// while the account now active serves the other turns of the tool. Does not wait.
    pub fn account_held(&self) {
        let _ = self.tx.send(Ask::AccountHeld);
    }

    /// Tells the agent's thread that its turn, which waited for an account whose plan
    /// offers its model, ends: there is none. Does not wait.
    pub fn account_end(&self) {
        let _ = self.tx.send(Ask::AccountEnd);
    }

    /// Has fields put on an item of what is shown of the agent's turn: the explanation of
    /// a tool's use on its row. Does not wait.
    pub fn explained(&self, item: String, fields: Value) {
        let _ = self.tx.send(Ask::Explained { item, fields });
    }

    /// The fleet is a serial worker. Wait for the principal's mailbox reply before
    /// releasing any reservation. The actor never asks the fleet to handle this command.
    pub(crate) fn delegation(
        &self,
        command: DelegationCommand,
    ) -> Result<DelegationState, Unavailable> {
        self.delegation_reply(command)?
    }
    /// A mailbox timeout is distinct from a refusal returned by the actor.
    /// Do not undo a START that might still be executing.
    pub(crate) fn delegation_reply(
        &self,
        command: DelegationCommand,
    ) -> Result<Result<DelegationState, Unavailable>, Unavailable> {
        // Three seconds allows a durable append while bounding how long this
        // single fleet worker can delay unrelated requests per mailbox ask.
        self.enqueue_delegation(command)?
            .recv_timeout(Duration::from_secs(3))
            .map_err(|e| Unavailable(format!("delegation mailbox unavailable: {e}")))
    }
    pub(crate) fn enqueue_delegation(
        &self,
        command: DelegationCommand,
    ) -> Result<mpsc::Receiver<Result<DelegationState, Unavailable>>, Unavailable> {
        let (answer, reply) = mpsc::channel();
        self.enqueue(Ask::Delegation { command, answer })?;
        Ok(reply)
    }

    /// Ends the agent's thread, waiting for it no longer than `within`. Its turn, if one runs,
    /// is not touched. Every attempt waits on the same reply, including after a timeout.
    pub fn close(&self, within: Duration) -> bool {
        let mut state = self.close.lock();
        if matches!(*state, Closing::Open | Closing::Preparing) {
            *state = Closing::Requested;
            // A failed send drops the reply and takes the lock to mark the thread closed.
            drop(state);
            let _ = self.tx.send(Ask::Close(CloseReply(self.close.clone())));
            state = self.close.lock();
        }
        let (state, _) = self
            .close
            .changed
            .wait_timeout_while(state, within, |state| *state == Closing::Requested)
            .unwrap_or_else(PoisonError::into_inner);
        *state == Closing::Closed
    }
}

/// What an agent that has never run a turn is doing: nothing, and nothing waits.
pub(crate) fn never_ran() -> Status {
    Status {
        live: None,
        last: None,
        waiting: Vec::new(),
        queue_depth: 0,
        paused: false,
        queue_revision: 0,
        stop_generation: 0,
        head_revision: 0,
        backend_session: None,
        label: String::new(),
        label_at_ms: None,
        resting: None,
        context_tokens: None,
        last_compaction: None,
        completed_at_ms: None,
        answered_by: None,
        arrived: false,
        note: None,
    }
}

/// Starts an agent's thread. The agent is opened on that thread, not here.
pub fn spawn(setup: Setup) -> AgentHandle {
    let (tx, rx) = mpsc::channel();
    let idle = never_ran();
    let (view_tx, view) = watch::channel(Arc::new(View {
        latest_turn_start: 0,
        status: idle,
        newest: None,
        visible_previews: Default::default(),
        working: None,
        can_leave_note: false,
        ended: None,
        finished: None,
        backend: setup.backend.id(),
        jobs: Vec::new(),
        provider_mirrors: BTreeMap::new(),
        provider_bindings: Vec::new(),
        books: Arc::default(),
        speech: Arc::default(),
        broken: Some(OPENING.to_string()),
    }));
    let now_ms = setup.clock.now_ms();
    let worker = Worker {
        setup,
        actor: None,
        tx: tx.clone(),
        view: view_tx,
        told: None,
        passed: Vec::new(),
        held: None,
        followups: None,
        pooled: None,
        terminals: 0,
        behind_terminal: None,
        let_go_by_hand: false,
        queue_edit: None,
        takeover_wait: VecDeque::new(),
        newest: None,
        visible_previews: Default::default(),
        ended: None,
        finished: None,
        transcript_sent: None,
        helper_change_session: None,
        transcript_due: None,
        jobs_told: BTreeMap::new(),
        helper_rows: Default::default(),
        helper_source: Some(Default::default()),
        helper_clock: Default::default(),
        provider_requested: false,
        provider_retry_at: None,
        historical_reading: false,
        historical_sources: BTreeMap::new(),
        helper_generation: 0,
        helper_owner: None,
        jobs_checked: Instant::now(),
        goals_looked_at: 0,
        books: Arc::default(),
        speech: Arc::default(),
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
    AgentHandle {
        tx,
        view,
        close: Arc::default(),
    }
}

enum QueueEdit {
    Edited { item: Waiting, text: String },
    Deleted(Waiting),
}

struct Worker {
    setup: Setup,
    actor: Option<Actor>,
    /// The way to this thread, for the turns' threads.
    tx: mpsc::Sender<Ask>,
    view: watch::Sender<Arc<View>>,
    /// What the clients were last told, so that only a change is told.
    told: Option<Status>,
    /// How the agent stood at each point where its live turn moved on to something else
    /// since the clients were last told, in order: told before the state it is in now.
    passed: Vec<Status>,
    /// The launch of an attempt that waits for an account: carried out when the pool of the
    /// tool's accounts takes the turn up.
    held: Option<ShimOrder>,
    /// Socket requests from this agent are written in the order it accepted them.
    followups: Option<mpsc::Sender<(PathBuf, String)>>,
    /// The turn the pool was last told of.
    pooled: Option<TurnId>,
    /// How many terminals are open on the agent's own tool.
    terminals: usize,
    /// The launch of an attempt that waits for those terminals to be closed.
    behind_terminal: Option<ShimOrder>,
    /// Its user let the mailbox go a moment ago: the clients are told so in a word of its
    /// own. A hold that ended because the last thing it held went to run is told with the
    /// news of that turn.
    let_go_by_hand: bool,
    /// The edit or withdrawal committed by the request this worker is handling.
    queue_edit: Option<QueueEdit>,
    /// Commands that could change the selected turns while a consume verdict is uncertain.
    takeover_wait: VecDeque<Ask>,
    /// The newest row of the conversation.
    newest: Option<Versioned>,
    visible_previews: (Option<Versioned>, Option<Versioned>),
    /// How the last turn ended.
    ended: Option<TurnEnd>,
    /// What the agent last finished for its user, as the view shows it.
    finished: Option<Arc<Value>>,
    transcript_sent: Option<Instant>,
    helper_change_session: Option<String>,
    /// When a held-back "the conversation changed" is to be sent.
    transcript_due: Option<Instant>,
    /// The revision of each job the clients were last told.
    jobs_told: BTreeMap<String, (u64, String)>,
    /// When the running jobs were last looked at.
    jobs_checked: Instant,
    helper_rows: clarp_actor::helper_cells::HelperRows,
    helper_source: Option<Box<crate::helper_source::HelperSource>>,
    helper_clock: crate::helper_source::RefreshClock,
    provider_requested: bool,
    provider_retry_at: Option<Instant>,
    historical_reading: bool,
    historical_sources: BTreeMap<String, Box<crate::helper_source::HelperSource>>,
    helper_generation: u64,
    helper_owner: Option<(String, String)>,
    /// When the goals were last looked at, on the Host's clock. A goal that was due then and
    /// woke nobody is looked at again when something happens, not when the time comes.
    goals_looked_at: i64,
    /// The agent's books as they were last published: written anew when one of them changes.
    books: Arc<Books>,
    /// What the agent says aloud, as it was last published. A week of lines at most: it is
    /// written anew whenever a line changes, which is a few times for every line.
    speech: Arc<SpeechBook>,
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
        Outcome::Failed { reason, .. } => ("failed", reason.clone()),
        Outcome::Interrupted { reason } => ("interrupted", reason.clone()),
        Outcome::Stopped { by, .. } => ("interrupted", format!("Stopped by {by}")),
        Outcome::Withdrawn { .. } | Outcome::Replaced { .. } => return None,
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
        ..Kept::default()
    }
}

/// The artifact a plan is shown as in the apps, as a change of the plan leaves it: made
/// with the plan, under the title the plan was given, and following its state from then on.
fn plan_shown(
    actor: &Actor,
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
        published: actor.media(),
        elsewhere: &|_| false,
        page: &|_| None,
    };
    let given = title.unwrap_or(&change.plan.title);
    actor
        .artifacts()
        .plan_synced(maker, &change.plan, given, title.is_some(), &mut ctx)
        .ok()
}

/// Writes a change of a goal and, with it, the artifact its plan is shown as.
fn keep_goal(
    actor: &mut Actor,
    record: &AgentRecord,
    change: &GoalChange,
    title: Option<&str>,
    author: clarp_kernel::goals::ledger::Author,
    now_ms: i64,
) -> Result<Outputs, ActorError> {
    let shown = plan_shown(actor, record, change, title, now_ms);
    let kept = Kept {
        goal: Some(change),
        author,
        artifacts: shown.as_slice(),
        ..Kept::default()
    };
    actor.keep(&kept, now_ms)
}

/// Why a goal is bound to another conversation after its tool let go of the one it was
/// bound to: the Python Host's words for it.
const CARRIED: &str = "Antigravity pruned the conversation; Clarp continued it in a new one that \
                       opens with its record of the old one";

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

impl Drop for Worker {
    fn drop(&mut self) {
        self.setup.speaks.counted(&self.setup.record.agent_id, &[]);
    }
}

// A burst of 32 messages amortizes a forced timer turn while bounding the
// number of mailbox handlers that can run ahead of an already-due deadline.
const MAILBOX_BURST: usize = 32;

#[derive(Default)]
struct Mailbox {
    handled: usize,
}

impl Mailbox {
    fn next<T>(
        &mut self,
        rx: &mpsc::Receiver<T>,
        due: Option<Instant>,
        now: Instant,
    ) -> Result<T, RecvTimeoutError> {
        if self.handled >= MAILBOX_BURST && due.is_some_and(|at| at <= now) {
            self.handled = 0;
            return Err(RecvTimeoutError::Timeout);
        }
        let result = match due {
            Some(at) => rx.recv_timeout(at.saturating_duration_since(now)),
            None => rx.recv().map_err(|_| RecvTimeoutError::Disconnected),
        };
        match result {
            Ok(_) => self.handled = (self.handled + 1).min(MAILBOX_BURST),
            Err(_) => self.handled = 0,
        }
        result
    }
}

impl Worker {
    fn run(mut self, rx: &mpsc::Receiver<Ask>) {
        let mut wait = REOPEN_FIRST;
        let mut mailbox = Mailbox::default();
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
                        if !refuse_for(rx, wait, &why, &mut |record| self.amended(record)) {
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
            // And an agent whose turn waits for its next attempt wakes to start it.
            let retry_due = self.actor.as_ref().and_then(Actor::retry_at).map(|due| {
                let wait = u64::try_from(due - self.now_ms()).unwrap_or(0);
                Instant::now() + Duration::from_millis(wait)
            });
            // And an agent whose turn holds something back from those who watch it (a text
            // that grows, lines a tool prints) wakes to send it.
            let shown_due = self
                .actor
                .as_ref()
                .and_then(Actor::shown_due_ms)
                .map(|due| {
                    let wait = u64::try_from(due - self.now_ms()).unwrap_or(0);
                    Instant::now() + Duration::from_millis(wait)
                });
            let helper_due = self.helper_due();
            let due = [
                helper_due,
                self.transcript_due,
                jobs_due,
                goals_due,
                requests_due,
                retry_due,
                shown_due,
            ]
            .into_iter()
            .flatten()
            .min();
            let resolved = self
                .actor
                .as_ref()
                .is_some_and(|actor| actor.selected_takeover().is_none());
            let queued = if resolved {
                self.takeover_wait.pop_front()
            } else {
                None
            };
            let request = queued.map_or_else(|| mailbox.next(rx, due, Instant::now()), Ok);
            let ask = match request {
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
                    if looked.is_ok() && retry_due.is_some_and(|due| due <= now) {
                        looked = self.retry_turn();
                    }
                    if let Err(error) = looked {
                        eprintln!(
                            "clarp-host: agent {} failed and is opened again: {error}",
                            self.setup.record.session
                        );
                        self.actor = None;
                    }
                    if shown_due.is_some_and(|due| due <= now) {
                        let now_ms = self.now_ms();
                        if let Some(actor) = self.actor.as_mut() {
                            actor.shown_due(now_ms);
                        }
                    }
                    self.tell_shown();
                    continue;
                }
                Err(RecvTimeoutError::Disconnected) => return,
            };
            if let Ask::Close(closed) = ask {
                // The agent is gone: the pool of its tool's accounts does not wait for its
                // turn, and does not take it up.
                if let (Some(pool), Some(_)) = (self.setup.pool(), self.pooled) {
                    pool.disowned(&self.setup.record.agent_id);
                }
                self.actor = None;
                drop(closed);
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
            self.tell_shown();
        }
    }

    /// Sends those who watch the agent what its turn has to show since they were last sent
    /// something: after everything that was asked of the agent, and after every time it
    /// woke on its own.
    fn tell_shown(&mut self) {
        let now = self.now_ms();
        let Some(actor) = self.actor.as_mut() else {
            return;
        };
        let view = self.view.borrow().clone();
        let trace = view
            .status
            .live
            .as_ref()
            .and_then(|live| live.trace_id.as_deref());
        for event in actor.take_shown(now) {
            self.setup
                .hub
                .show(event, now, trace.zip(view.working.as_ref()));
        }
    }

    fn helper_due(&mut self) -> Option<Instant> {
        if self.setup.record.backend != "claude" {
            return None;
        }
        let now_ms = self.now_ms();
        let actor = self.actor.as_mut()?;
        let (live, session) = actor.helper_context();
        let now = Instant::now();
        let helper_due = self.helper_clock.due(live, now);
        if let Some(retry) = self.provider_retry_at.take()
            && retry > now
        {
            self.provider_retry_at = Some(retry);
            return Some(retry);
        }
        self.helper_source.as_ref()?;
        let session = session
            .or_else(|| {
                actor
                    .provider_session(&self.setup.profile.conversation_id)
                    .map(|_| self.setup.profile.conversation_id.as_str())
            })?
            .to_owned();
        let attempt = actor.provider_attempt();
        let newest = actor
            .provider_session(&session)
            .and_then(|s| s.newest_owner());
        if newest.is_some() && newest != attempt {
            let root = crate::past::projects(&crate::config::home());
            if crate::helper_source::boundary(&root, &self.setup.record.cwd, &session)
                .ok()
                .is_none_or(|identity| {
                    actor
                        .provider_boundary(&session, None, identity, now_ms)
                        .is_err()
                })
            {
                let retry = now + Duration::from_secs(1);
                self.provider_retry_at = Some(retry);
                return Some(retry);
            }
        }
        let saved = actor.provider_session(&session);
        let due = if saved.is_some_and(|s| s.final_read.is_some()) || self.provider_requested {
            now
        } else {
            helper_due?
        };
        if due > now {
            return Some(due);
        }
        let mut visit = saved.map_or_else(
            || clarp_kernel::provider_tasks::Session::default().visit(session.clone()),
            |s| s.visit(session.clone()),
        );
        if visit.bound_at == 0 {
            visit.bound_at = match actor.first_provider_binding(&session) {
                Ok(Some(time)) => time,
                Ok(None) => now_ms,
                Err(_) => return Some(now + Duration::from_secs(1)),
            };
        }
        let helpers = helper_due.is_some_and(|t| t <= now)
            && actor.track_helpers(&mut self.helper_rows).unwrap_or(false);
        if helper_due.is_some_and(|t| t <= now) {
            self.helper_clock.started(now);
        }
        self.provider_requested = false;
        let session = session.to_owned();
        let cwd = self.setup.record.cwd.clone();
        let owner = (cwd.clone(), session.clone());
        if self.helper_owner.as_ref() != Some(&owner) {
            self.helper_source = Some(Default::default());
            self.helper_owner = Some(owner);
        }
        let mut source = self.helper_source.take()?;
        let missed = if !live && self.helper_rows.needs_receipt() {
            actor.status().last.as_ref().map(|last| last.id.0)
        } else {
            None
        };
        let at = now_ms;
        let mut request = crate::helper_source::ProviderRead {
            helpers,
            parent: helpers && (self.helper_rows.has_imported || missed.is_some()),
            visit,
        };
        let generation = self.helper_generation;
        let tx = self.tx.clone();
        self.setup.runtime.spawn_blocking(move || {
            let root = crate::past::projects(&crate::config::home());
            let result = source.read_visit(&root, &cwd, &mut request, at);
            let complete = result.is_ok() && request.visit.eof;
            let more_helpers = result.is_ok() && request.parent && !complete;
            let provider = result.is_ok().then_some(request.visit);
            let changed = result.unwrap_or(false);
            if missed.is_some() && complete {
                source.recovered_through = missed;
            }
            let _ = tx.send(Ask::HelperRead {
                source,
                provider,
                more_helpers,
                cwd,
                session,
                generation,
                changed: changed || missed.is_some(),
            });
        });
        None
    }

    /// A runner hint never interrupts an active read of the same transcript.
    fn visit_idle_provider(&mut self, native: String) {
        let Some(actor) = self.actor.as_ref() else {
            return;
        };
        let (live, current) = actor.helper_context();
        if self.setup.record.backend == "claude" && current == Some(&native) {
            if !live {
                self.provider_requested = true;
            }
            return;
        }
        let Some(binding) = actor.provider_session(&native) else {
            return;
        };
        if self.historical_reading {
            return;
        }
        self.historical_reading = true;
        let mut source = self.historical_sources.remove(&native).unwrap_or_default();
        let mut request = crate::helper_source::ProviderRead {
            helpers: false,
            parent: false,
            visit: binding.visit(native),
        };
        let cwd = if binding.cwd.is_empty() {
            self.setup.record.cwd.clone()
        } else {
            binding.cwd.clone()
        };
        let generation = self.helper_generation;
        let tx = self.tx.clone();
        let now = self.now_ms();
        self.setup.runtime.spawn_blocking(move || {
            let root = crate::past::projects(&crate::config::home());
            let visit = source
                .read_visit(&root, &cwd, &mut request, now)
                .ok()
                .map(|_| request.visit);
            let _ = tx.send(Ask::HistoricalRead {
                source,
                visit,
                generation,
            });
        });
    }

    fn now_ms(&self) -> i64 {
        self.setup.clock.now_ms()
    }

    /// Takes the agent as the fleet now has it. Its next turn runs with the model and the
    /// effort it now has, and its tool is told who the agent now is; a turn that runs keeps
    /// what it was started with.
    fn amended(&mut self, record: AgentRecord) {
        let (model, effort) = (non_empty(&record.model), non_empty(&record.effort));
        let told = Some(self.setup.telling.of(&record));
        self.setup.profile.model.clone_from(&model);
        self.setup.profile.effort.clone_from(&effort);
        self.setup.profile.told.clone_from(&told);
        if let Some(actor) = self.actor.as_mut() {
            actor.retune(model, effort);
            actor.retell(told);
        }
        // Another tool, another directory or a conversation of another name: the agent is
        // opened again as it now is. Only an agent that has no conversation with the tool it
        // had is given another, and only one that runs no turn is moved or started again
        // (who changes it sees to that).
        let was = &self.setup.record;
        let another = record.backend != was.backend;
        let moved = record.cwd != was.cwd || record.conversation_id != was.conversation_id;
        if let Some(backend) = clarp_backend::by_id(&record.backend).filter(|_| another) {
            self.setup.backend = backend;
            self.actor = None;
        }
        if moved {
            self.setup.profile.cwd = std::path::PathBuf::from(&record.cwd);
            self.setup
                .profile
                .conversation_id
                .clone_from(&record.conversation_id);
            self.actor = None;
        }
        self.setup.record = record;
    }

    fn open(&mut self) -> Result<(), String> {
        self.helper_rows = Default::default();
        self.helper_source = Some(Default::default());
        self.helper_clock = Default::default();
        self.provider_requested = false;
        self.historical_reading = false;
        self.historical_sources.clear();
        self.helper_owner = None;
        self.helper_generation += 1;
        self.helper_change_session = None;
        let log = self
            .setup
            .engine
            .open(&self.setup.profile.dir, self.setup.codecs.clone())
            .map_err(|error| error.to_string())?;
        let (mut actor, mut out) = Actor::open(
            self.setup.profile.clone(),
            self.setup.backend.clone(),
            log,
            self.now_ms(),
        )
        .map_err(|error| error.to_string())?;
        self.setup
            .delegation_rejoin
            .reconcile(&mut actor, self.now_ms());
        if self.setup.record.backend == "claude" {
            // No jobs view escapes this rejoin until every ended attempt's
            // final byte boundary has been consumed and its tasks reconciled.
            let current = actor.provider_attempt();
            let recovering: Vec<_> = actor
                .provider_sessions()
                .filter(|(_, s)| {
                    s.final_read.is_some()
                        || s.newest_owner().is_some() && s.newest_owner() != current
                })
                .map(|(id, _)| id.clone())
                .collect();
            let root = crate::past::projects(&crate::config::home());
            for session in recovering {
                let mut source = crate::helper_source::HelperSource::default();
                if actor
                    .provider_session(&session)
                    .and_then(|s| s.newest_owner())
                    .is_some_and(|t| Some(t) != current)
                {
                    let boundary =
                        crate::helper_source::boundary(&root, &self.setup.record.cwd, &session)
                            .map_err(|e| e.to_string())?;
                    actor
                        .provider_boundary(&session, None, boundary, self.now_ms())
                        .map_err(|e| e.to_string())?;
                }
                while let Some(saved) = actor
                    .provider_session(&session)
                    .filter(|s| s.final_read.is_some())
                {
                    let now = self.now_ms();
                    let mut read = crate::helper_source::ProviderRead {
                        helpers: false,
                        parent: false,
                        visit: clarp_kernel::provider_tasks::Visit {
                            native: session.clone(),
                            expected: saved.revision,
                            cursor: saved.cursor.clone(),
                            bound_at: saved.bound_at,
                            owner: saved.owner,
                            final_read: saved.final_read.clone(),
                            actions: Vec::new(),
                            eof: false,
                        },
                    };
                    source
                        .read_visit(&root, &self.setup.record.cwd, &mut read, now)
                        .map_err(|e| e.to_string())?;

                    let changes = actor
                        .apply_provider_visit(read.visit, now, || self.setup.revisions.next(now))
                        .map_err(|e| e.to_string())?;
                    out.changed.jobs |= changes.changed.jobs;
                }
            }
        }
        self.actor = Some(actor);
        if let (Some(pool), Some(actor)) = (self.setup.pool(), self.actor.as_mut()) {
            actor.pooled(pool.of(&self.setup.record.agent_id));
            // A turn that waited for an account when the Host last stopped waits on, in
            // the recovery it waited in: the pool asks the selector for it and takes it up.
            if let Some((turn, since)) = actor.waits_for_an_account() {
                let agent = self.setup.record.agent_id.clone();
                let model = actor.turn_model().unwrap_or_default();
                pool.carried(since, std::slice::from_ref(&agent));
                pool.starting(&agent, turn, &model);
                self.pooled = Some(turn);
            }
        }
        // What the agent is doing when it is opened is not news: a client that connects is
        // told to look at everything anyway. What follows from opening it is.
        self.told = self.actor.as_ref().map(Actor::status);

        // Nor are the jobs it has: only what changes from here on is told.
        self.jobs_told.clear();
        if let Some(actor) = self.actor.as_mut() {
            actor.take_job_events();
        }
        self.update_previews();
        self.ended = self.last_end();
        self.finished = self.last_finished();
        // Nor are its artifacts: only what changes from here on is told. How many of its
        // requests wait for the user is counted from the start.
        self.refresh_books(true);
        self.artifacts_told = self
            .books
            .artifacts
            .artifacts()
            .map(|artifact| (artifact.artifact_id.clone(), told_artifact(artifact)))
            .collect();
        self.count_requests(self.now_ms());
        self.read_speech();
        self.publish_view();
        self.after(out);
        // An answer that was given and not yet told when the Host went away is told now.
        self.settle_requests().map_err(|error| error.to_string())?;
        Ok(())
    }

    /// Reads the agent's books anew for those who read them without asking the agent. The
    /// files it published are many and are only ever added to: they are read anew when one
    /// was.
    fn refresh_books(&mut self, published: bool) {
        if let Some(actor) = &self.actor {
            let media = if published {
                Arc::new(actor.media().clone())
            } else {
                self.books.media.clone()
            };
            self.books = Arc::new(Books {
                artifacts: actor.artifacts().clone(),
                goals: actor.goals().clone(),
                media,
            });
        }
    }

    /// Reads what the agent says aloud anew, for those who read it without asking the
    /// agent. Answers whether it is not as it was last published.
    fn read_speech(&mut self) -> bool {
        match &self.actor {
            Some(actor) if *self.speech != *actor.speech() => {
                self.setup
                    .speaks
                    .counted(&self.setup.record.agent_id, actor.speech().lines());
                self.speech = Arc::new(actor.speech().clone());
                true
            }
            _ => false,
        }
    }

    /// Something is asked about what the agent says aloud. What changed is published
    /// before the one who asked is answered: where it looks next, it finds what it was
    /// told.
    fn handle_speech<T>(
        &mut self,
        ask: impl FnOnce(&mut Actor, i64) -> Result<T, ActorError>,
        answer: Answer<T>,
    ) -> Result<(), ActorError> {
        let now = self.now_ms();
        let Some(actor) = self.actor.as_mut() else {
            return Ok(());
        };
        match ask(actor, now) {
            Ok(done) => {
                if self.read_speech() {
                    self.publish_view();
                }
                let _ = answer.send(Ok(done));
                Ok(())
            }
            Err(error) => {
                let _ = answer.send(Err(unavailable(&error)));
                Err(error)
            }
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
        let pages = self.setup.blobs.as_ref();
        match library::carry_out(actor, &self.setup.record, want, now, pages) {
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
        caller: GoalCaller,
        answer: Answer<ToolAnswer>,
    ) -> Result<(), ActorError> {
        let now = self.now_ms();
        let Some(actor) = self.actor.as_mut() else {
            return Ok(());
        };
        if let Some(active) = actor.active_delegation() {
            use goaltool::Ask as Goal;
            let guarded = matches!(
                ask,
                Goal::Create { .. }
                    | Goal::Step { .. }
                    | Goal::Act { .. }
                    | Goal::Finish { .. }
                    | Goal::Subgoal { .. }
            );
            let identity = match &caller {
                GoalCaller::Verified(id) => Some((id.as_str(), "owner", true)),
                GoalCaller::Peer(id, verified) => Some((id.as_str(), "peer", *verified)),
                GoalCaller::Default => Some((self.setup.record.agent_id.as_str(), "owner", false)),
                GoalCaller::System => None,
            };
            if guarded && let Some((id, kind, verified)) = identity {
                let ancestry = &self.setup.delegation_rejoin;
                if let Err(why) = clarp_kernel::goals::delegation::guard(
                    kind,
                    verified,
                    ancestry.descends_from(id, &active.delegate_agent_id),
                    ancestry.backend(id).as_deref(),
                ) {
                    let _ = answer.send(Ok(ToolAnswer::said(4, format!("agent_tasks: {why}"))));
                    return Ok(());
                }
            }
        }
        let owner = Owner {
            agent_id: &self.setup.record.agent_id,
            session: &self.setup.record.session,
        };
        if let goaltool::Ask::Subgoal {
            plan_id,
            action,
            data,
        } = ask
        {
            let verified = match &caller {
                GoalCaller::Verified(id) => Some(id.as_str()),
                _ => None,
            };
            let (said, written) =
                goaltool::subgoal(actor.goals(), plan_id, action, data, verified, now);
            let Some((change, event)) = written else {
                let _ = answer.send(Ok(said));
                return Ok(());
            };
            let out = actor.keep(
                &Kept {
                    goal: Some(&change),
                    subgoal_event: Some(&event),
                    ..Kept::default()
                },
                now,
            );
            match out {
                Ok(out) => {
                    self.after(out);
                    let _ = answer.send(Ok(said));
                    return Ok(());
                }
                Err(error) => {
                    let _ = answer.send(Err(unavailable(&error)));
                    return Err(error);
                }
            }
        }
        let (said, change) = with_goals(actor, now, |book, world| {
            goaltool::answer(book, owner, ask, world)
        });
        // A plan that is made is shown under the title it was given.
        let title = match ask {
            goaltool::Ask::Create { title, .. } => Some(title.trim()),
            _ => None,
        };
        use clarp_kernel::goals::ledger::Author;
        let author = match (&caller, ask) {
            (GoalCaller::Verified(id), _) => Author::Owner(Some(id)),
            (GoalCaller::Peer(id, verified), _) => Author::Peer(id, *verified),
            (GoalCaller::System, _) => Author::System,
            (_, goaltool::Ask::UserAct { .. }) => Author::User,
            (_, goaltool::Ask::Keeper { written, .. }) => Author::Keeper(&written.janitor_agent_id),
            _ => Author::Owner(None),
        };
        let written = match &change {
            Some(change) => keep_goal(actor, &self.setup.record, change, title, author, now),
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

    /// Takes the library an agent brings from the Host it was on: every artifact as it
    /// was shown there, the pages of its reports kept first, all of it in one write.
    fn handle_library(
        &mut self,
        artifacts: &[(Value, Vec<Value>)],
        answer: Answer<Result<usize, String>>,
    ) -> Result<(), ActorError> {
        let now = self.now_ms();
        let Some(actor) = self.actor.as_mut() else {
            return Ok(());
        };
        let record = &self.setup.record;
        let maker = Maker {
            agent_id: &record.agent_id,
            session: &record.session,
            persona: &record.persona,
        };
        let mut arrived: Vec<Artifact> = Vec::new();
        for (shown, revisions) in artifacts {
            let here = |id: &str| {
                actor.artifacts().any(id).is_some()
                    || arrived.iter().any(|artifact| artifact.artifact_id == id)
            };
            let came = match actor.artifacts().taken_over(maker, shown, revisions, &here) {
                Ok(came) => came,
                Err(refused) => {
                    let _ = answer.send(Ok(Err(refused.message)));
                    return Ok(());
                }
            };
            for content in &came.pages {
                let name = reports::Page::of(content).blob();
                if let Err(error) = self.setup.blobs.put(&name, content.as_bytes()) {
                    let error = ActorError::Io(format!("a report's page was not kept: {error}"));
                    let _ = answer.send(Err(unavailable(&error)));
                    return Err(error);
                }
            }
            arrived.push(came.artifact);
        }
        let kept = Kept {
            artifacts: &arrived,
            ..Kept::default()
        };
        match actor.keep(&kept, now) {
            Ok(out) => {
                self.after(out);
                let _ = answer.send(Ok(Ok(arrived.len())));
                Ok(())
            }
            Err(error) => {
                let _ = answer.send(Err(unavailable(&error)));
                Err(error)
            }
        }
    }

    /// Takes the plans an agent brings from the Host it was on: each is written as it
    /// stood there, with the artifact it is shown as, and published before the answer.
    fn handle_plans(
        &mut self,
        source: &str,
        plans: &[crate::edge::MovedPlan],
        answer: Answer<Result<usize, String>>,
    ) -> Result<(), ActorError> {
        let now = self.now_ms();
        let Some(actor) = self.actor.as_mut() else {
            return Ok(());
        };
        let owner = Owner {
            agent_id: &self.setup.record.agent_id,
            session: &self.setup.record.session,
        };
        let mut outs = Vec::new();
        let mut taken = Ok(0);
        let imports: Vec<_> = plans
            .iter()
            .map(|p| {
                (
                    p.plan["plan_id"].as_str().unwrap_or("").to_owned(),
                    p.events.as_deref().unwrap_or_default(),
                )
            })
            .collect();
        if let Err(why) = actor.validate_goal_import(&imports) {
            let _ = answer.send(Ok(Err(why.to_string())));
            return Ok(());
        }
        let import_high = imports
            .iter()
            .filter_map(|(_, rows)| rows.last().map(|e| e.event_id))
            .max()
            .unwrap_or(0);
        let entering_existing = actor.next_goal_event_id()? > 1;
        for moved in plans {
            let change = match actor
                .goals()
                .taken_over(owner, &moved.plan, &moved.documents)
            {
                Ok(change) => change,
                Err(why) => {
                    taken = Err(why.0);
                    break;
                }
            };
            let shown = if actor.artifacts().of_plan(&change.plan.plan_id).is_none() {
                plan_shown(
                    actor,
                    &self.setup.record,
                    &change,
                    Some(&change.plan.title),
                    now,
                )
            } else {
                None
            };
            let kept = Kept {
                goal: Some(&change),
                imported_events: moved.events.as_deref(),
                author: clarp_kernel::goals::ledger::Author::Legacy,
                entry_source: entering_existing.then_some(source),
                import_high,
                artifacts: shown.as_slice(),
                ..Kept::default()
            };
            let written = actor.keep(&kept, now);
            match written {
                Ok(out) => outs.push(out),
                Err(error) => {
                    let _ = answer.send(Err(unavailable(&error)));
                    return Err(error);
                }
            }
            taken = taken.map(|count| count + 1);
        }
        for out in outs {
            self.after(out);
        }
        let _ = answer.send(Ok(taken));
        Ok(())
    }

    fn handle(&mut self, ask: Ask) -> Result<(), ActorError> {
        if matches!(
            &ask,
            Ask::Stop { .. }
                | Ask::Hold { .. }
                | Ask::Withdraw { .. }
                | Ask::Reword { .. }
                | Ask::SendNow { .. }
                | Ask::Send {
                    admission: Admission::Direct,
                    ..
                }
        ) && let Some((from, socket)) = self
            .actor
            .as_ref()
            .and_then(Actor::selected_takeover_socket)
        {
            // Release is ordered against the consume in the shim. Once it replies, a
            // missing marker means the old tool was never committed for takeover. If
            // the commit won, finish its journal batch before applying this command.
            let replied = crate::launcher::takeover_request(&socket, clarp_shim::Request::Release)
                .and_then(|reply| reply.let_go)
                .is_some();
            let now = self.now_ms();
            if let Some(actor) = self.actor.as_mut() {
                let out = actor.spool_changed(now)?;
                if replied && !actor.selected_takeover_has_marker()? {
                    actor.clear_takeover(from, now)?;
                }
                let defer = actor.selected_takeover().is_some();
                self.after(out);
                if defer {
                    self.takeover_wait.push_back(ask);
                    return Ok(());
                }
            }
        }
        let ask = match ask {
            Ask::Artifact { want, answer } => return self.handle_artifact(&want, answer),
            Ask::TakePlans {
                source,
                plans,
                answer,
            } => return self.handle_plans(&source, &plans, answer),
            Ask::TakeLibrary { artifacts, answer } => {
                return self.handle_library(&artifacts, answer);
            }
            Ask::Goal {
                ask,
                caller,
                answer,
            } => return self.handle_goal(&ask, caller, answer),
            Ask::Amended { record, taken } => {
                self.amended(*record);
                // What it last finished is told by the name it now has.
                if self.finished.is_some() && self.actor.is_some() {
                    self.finished = self.last_finished();
                    self.publish_view();
                }
                let _ = taken.send(());
                return Ok(());
            }
            Ask::VisitProvider(native) => {
                self.visit_idle_provider(native);
                return Ok(());
            }
            Ask::Terminal { open } => {
                self.terminal(open)?;
                return Ok(());
            }
            Ask::ClaimLine { answer } => {
                return self.handle_speech(|actor, now| actor.claim_line(now), answer);
            }
            Ask::LineBegun {
                id,
                clip,
                asked,
                event_id,
                answer,
            } => {
                let begun = |actor: &mut Actor, now| {
                    let asked = (asked.0.as_str(), asked.1.as_str());
                    let begun =
                        actor.line_begun(&id, (clip.0, clip.1.as_str()), asked, event_id, now);
                    begun.map(|line| line.is_some())
                };
                return self.handle_speech(begun, answer);
            }
            Ask::LineEnded {
                id,
                clip,
                outcome,
                answer,
            } => {
                let ended = |actor: &mut Actor, now| {
                    let outcome = match &outcome {
                        Ok(spoke) => Ok(Said {
                            bytes: spoke.bytes,
                            provider: &spoke.provider,
                            voice: &spoke.voice,
                        }),
                        Err(why) => Err(why.as_str()),
                    };
                    let ended = actor.line_ended(&id, clip, outcome, now);
                    ended.map(|line| line.is_some())
                };
                return self.handle_speech(ended, answer);
            }
            Ask::LineAnnounced {
                clip,
                event_id,
                across,
                answer,
            } => {
                let announced =
                    |actor: &mut Actor, now| actor.line_announced(clip, (event_id, across), now);
                return self.handle_speech(announced, answer);
            }
            Ask::LinePlayed {
                clip,
                status,
                error,
                answer,
            } => {
                let played = |actor: &mut Actor, now| {
                    actor.line_played(clip, &status, error.as_deref(), now)
                };
                return self.handle_speech(played, answer);
            }
            other => other,
        };
        let now = self.now_ms();
        if self.setup.record.backend == "claude"
            && let Ask::Send {
                message,
                admission: Admission::Queue,
                reservation: None,
                ..
            } = &ask
            && message.origin.is_user()
            && let Some(actor) = self.actor.as_mut()
        {
            // Read a tool call which reached the spool before this send, even if its wakeup
            // is still behind the send in the mailbox.
            let read = actor.spool_changed(now)?;
            self.after(read);
        }
        let Some(actor) = self.actor.as_mut() else {
            return Ok(());
        };
        // A message may start a turn, and a turn's tool is told what holds when it starts:
        // the owner's settings and file are read now.
        if matches!(ask, Ask::Send { .. }) {
            actor.retell(Some(self.setup.telling.of(&self.setup.record)));
            // Whether its turns that answer another agent are shown as such: the key may have
            // been read since the agent was opened.
            actor.hide_coordination(clarp_kernel::coordination::can_leave_note(
                &self.setup.record.agent_id,
                Some(&self.setup.record.backend),
                self.setup.turns.has_key(),
            ));
        }
        let (out, owed) = match ask {
            Ask::Send {
                message,
                admission,
                reservation,
                marks,
                answer,
            } => {
                let direct_user = admission == Admission::Queue
                    && message.origin.is_user()
                    && marks.coordination.is_none();
                let desk_relay = admission == Admission::Later
                    && marks.user_word
                    && marks.coordination.is_some();
                let can_offer_steering = reservation.is_none()
                    && (direct_user || desk_relay)
                    && self.setup.record.backend == "claude";
                let mut sent = actor
                    .send_marked(&message, admission, reservation, &marks, now)
                    .map(|sent| match sent {
                        Ok((sent, out)) => (Ok(sent), out),
                        Err(taken) => (Err(taken), Outputs::default()),
                    });
                if let Ok((Ok(sent), out)) = &mut sent
                    && !sent.duplicate
                    && let Some(turn) = sent.turn
                {
                    if let Some(c) = &self.setup.hub.capture {
                        c.turn(now, "accepted", &self.setup.record.agent_id, turn.0);
                    }
                    let steer = if sent.queued
                        && can_offer_steering
                        && let Some(uuid) = message
                            .trace_id
                            .as_deref()
                            .and_then(clarp_kernel::takeover::prompt_uuid)
                    {
                        actor.steer_active(turn, uuid, &message.text, now)?
                    } else {
                        None
                    };
                    if let Some(action) = steer {
                        out.actions.push(action);
                    } else if sent.queued
                        && matches!(self.setup.record.backend.as_str(), "claude" | "agy")
                        && let Ok(Some(offer)) = actor.request_takeover_hold(turn, now)
                        && let Some(program) = clarp_backend::program(&self.setup.record.backend)
                    {
                        let tx = self.tx.clone();
                        let backend = self.setup.record.backend.clone();
                        self.setup.runtime.spawn(async move {
                            if !crate::takeover::can_use(&backend, program.into()).await {
                                return;
                            }
                            let socket = offer.socket.clone();
                            let request = clarp_shim::Request::Hold {
                                ms: clarp_shim::HOLD_MOST_MS,
                            };
                            let held = tokio::task::spawn_blocking(move || {
                                crate::launcher::takeover_request(&socket, request)
                                    .and_then(|reply| reply.held)
                            })
                            .await;
                            if let Ok(Some(held)) = held {
                                let _ = tx.send(Ask::TakeoverHeld { offer, held });
                            }
                        });
                    }
                }
                answered(sent, answer)?
            }
            Ask::LeaveNote {
                turn,
                id,
                text,
                answer,
            } => {
                let left = actor.leave_note(turn, &id, &text, now);
                let left = left.map(|(noted, out)| {
                    // Only a note written now tells the user: one asked again was told.
                    let told = match &noted {
                        clarp_actor::Noted::Written { id, .. } => {
                            Some(wire::note_notification(&self.setup.record, id, &text, now))
                        }
                        _ => None,
                    };
                    ((noted, told), out)
                });
                if let Ok(((_, Some(Ok(told))), _)) = &left {
                    // A phone that is not listening is pushed to, when the rule says so.
                    if told["push"] == true {
                        self.setup.pusher.turn_done(TurnDone {
                            badge: true,
                            unread: true,
                            session: self.setup.record.session.clone(),
                            persona: self.setup.record.persona.clone(),
                            notification_id: told["notification_id"]
                                .as_str()
                                .unwrap_or("")
                                .to_string(),
                            preview: told["preview"].as_str().unwrap_or("").to_string(),
                            needs_response: actor.artifacts().has_pending_decision(now),
                        });
                    }
                    self.setup.hub.publish(told.clone(), now);
                }
                answered(left, answer)?
            }
            Ask::Stop { by, answer } => {
                // The turn is no longer the agent's to finish: the pool of its tool's
                // accounts does not wait for it, and does not take it up.
                if let (Some(pool), Some(_)) = (self.setup.pool(), self.pooled) {
                    pool.disowned(&self.setup.record.agent_id);
                }
                // A Stop holds the agent's goals too: each one that is being worked is paused
                // until someone resumes it, so that none wakes the agent the user just stopped.
                let reason = clarp_kernel::control::paused_by(&by);
                let stopped = actor.stop_as(by, now).and_then(|(told, mut out)| {
                    let paused = with_goals(actor, now, |book, world| book.stopped(&reason, world));
                    for change in &paused {
                        let kept = keep_goal(
                            actor,
                            &self.setup.record,
                            change,
                            None,
                            clarp_kernel::goals::ledger::Author::User,
                            now,
                        )?;
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
            Ask::Repair { by, answer } => {
                answered(actor.repair(by, now).map(|out| ((), out)), answer)?
            }
            // The mailbox alone: the agent's goals stay as they are, and wait with it.
            Ask::Hold { answer } => {
                answered(actor.stop("move", now).map(|(_, out)| ((), out)), answer)?
            }
            Ask::Resume { by, answer } => {
                let resumed = actor.resume(&by, now);
                self.let_go_by_hand = true;
                let queue = actor.queue_state();
                answered(resumed.map(|(held, out)| ((held, queue), out)), answer)?
            }
            Ask::StartAgain { answer } => answered(actor.start_again(now), answer)?,
            Ask::Queue { answer } => {
                let waiting = actor.queue().map(|waiting| (waiting, actor.queue_state()));
                return read(waiting, answer);
            }
            Ask::OpeningRow { id, answer } => return read(actor.opening_row(&id), answer),
            Ask::Fate { message_id, answer } => return read(actor.fate_of(&message_id), answer),
            Ask::UnrunRequest {
                message_id,
                requester,
                answer,
            } => {
                return read(actor.unrun_request(&message_id, &requester), answer);
            }
            Ask::Standing { turn, answer } => return read(actor.standing_of(turn), answer),
            Ask::RunningTrace { turn, answer } => return read(actor.running_trace(turn), answer),
            Ask::Shown { answer } => return read(Ok(actor.shown_whole(now)), answer),
            Ask::Import {
                from,
                backend_session,
                rows,
                answer,
            } => answered(actor.import(&from, backend_session, rows, now), answer)?,
            Ask::TakeUp {
                helpers,
                session,
                rows,
                answer,
            } => {
                let result = actor.take_up(&session, rows, now);
                if matches!(&result, Ok((Ok(_), _)))
                    && let Some(source) = helpers
                {
                    self.helper_source = Some(source);
                    self.helper_owner = Some((self.setup.record.cwd.clone(), session));
                    self.helper_generation += 1;
                }
                answered(result, answer)?
            }
            Ask::RefreshTelling => {
                actor.retell(Some(self.setup.telling.of(&self.setup.record)));
                return Ok(());
            }
            Ask::ExpireProviderLinks(native) => {
                if actor.expire_provider_links(&native, now)? {
                    self.publish_view();
                }
                return Ok(());
            }
            Ask::HistoricalRead {
                source,
                visit,
                generation,
            } => {
                if generation != self.helper_generation {
                    return Ok(());
                }
                self.historical_reading = false;
                if let Some(visit) = visit {
                    if source.has_uncommitted_bytes(&visit.cursor) {
                        self.historical_sources.insert(visit.native.clone(), source);
                    }
                    let revisions = &self.setup.revisions;
                    let out = actor.apply_provider_visit(visit, now, || revisions.next(now))?;
                    self.after(out);
                }
                return Ok(());
            }
            Ask::HelperRead {
                source,
                provider,
                more_helpers,
                cwd,
                session,
                generation,
                changed,
            } => {
                if self.helper_generation == generation {
                    if self.setup.record.cwd == cwd
                        && actor
                            .helper_context()
                            .1
                            .unwrap_or(&self.setup.profile.conversation_id)
                            == session.as_str()
                    {
                        {
                            if provider.is_none() {
                                self.provider_retry_at =
                                    Some(Instant::now() + Duration::from_secs(1));
                            }
                            if more_helpers {
                                self.helper_clock.finish_later();
                            }
                            let mut jobs_changed = false;
                            if let Some(visit) = provider {
                                let more = !visit.eof;
                                let revisions = &self.setup.revisions;
                                let applied = actor
                                    .apply_provider_visit(visit, now, || revisions.next(now))?;
                                jobs_changed = applied.changed.jobs;
                                if more && actor.helper_context().0 {
                                    self.provider_requested = true;
                                }
                            }
                            actor.track_helpers(&mut self.helper_rows)?;
                            let mut out = actor.refresh_helpers(
                                &mut self.helper_rows,
                                changed,
                                now,
                                |cell, imported, turn| source.apply(cell, imported, turn),
                            )?;
                            if out.changed.conversation {
                                self.helper_change_session = Some(session);
                            }
                            out.changed.jobs |= jobs_changed;
                            if out.changed.conversation || out.changed.jobs {
                                self.after(out);
                            }
                        }
                        self.helper_source = Some(source);
                    } else {
                        self.helper_source = Some(Default::default());
                        self.helper_owner = None;
                    }
                }
                return Ok(());
            }
            Ask::Withdraw { turn, by, answer } => {
                let item = actor.queue_item(turn)?;
                let withdrawn = actor.withdraw(turn, &by, now);
                if matches!(withdrawn, Ok((true, _))) {
                    self.queue_edit = item.map(QueueEdit::Deleted);
                }
                let queue = actor.queue_state();
                answered(withdrawn.map(|(was, out)| ((was, queue), out)), answer)?
            }
            Ask::SendNow { turn, by, answer } => answered(actor.send_now(turn, &by, now), answer)?,
            Ask::Reword { turn, text, answer } => {
                let item = actor.queue_item(turn)?;
                let reworded = actor.reword(turn, &text, now);
                if matches!(reworded, Ok((Reworded::Done, _))) {
                    self.queue_edit = item.map(|item| QueueEdit::Edited { item, text });
                }
                answered(reworded, answer)?
            }
            Ask::Note { id, text, answer } => answered(actor.note(&id, &text, now), answer)?,
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
            Ask::RetireLabel { set_at_ms, answer } => {
                answered(actor.retire_label(set_at_ms, now), answer)?
            }
            Ask::Job {
                mut command,
                origin,
                answer,
            } => {
                if let (JobCommand::Register { metadata, .. }, Some(origin)) =
                    (&mut command, origin)
                {
                    let bound = actor.helper_context().1.filter(|native| {
                        actor
                            .provider_session(native)
                            .is_some_and(|s| s.ended_at.is_none() || s.newest_owner().is_some())
                    });
                    *metadata = origin
                        .metadata(
                            &self.setup.record.agent_id,
                            &self.setup.record.backend,
                            bound,
                        )
                        .unwrap_or(Value::Null);
                }
                answered(
                    actor.job(command, now, || self.setup.revisions.next(now)),
                    answer,
                )?
            }
            Ask::Publish { assets, answer } => {
                let kept = Kept {
                    media: &assets,
                    ..Kept::default()
                };
                answered(actor.keep(&kept, now).map(|out| ((), out)), answer)?
            }
            Ask::JobRecord { id, answer } => {
                return read(Ok(actor.jobs().get(&id).cloned()), answer);
            }
            Ask::JobTimeline { id, limit, answer } => {
                return read(actor.job_timeline(&id, limit), answer);
            }
            Ask::Delegation { command, answer } => {
                let result = (|| -> Result<DelegationState, ActorError> {
                    let mut version = None;
                    let mut activity = None;
                    let mut baseline = 0;
                    let mut applied = None;
                    let (prior, accepted) = match command {
                        #[cfg(test)]
                        DelegationCommand::Receipt { id, caller } => {
                            let written = actor.as_delegate(&id, Some(&caller), |a| {
                                a.keep(
                                    &Kept {
                                        applied_cursor: Some((&id, 0)),
                                        ..Kept::default()
                                    },
                                    now,
                                )
                            })?;
                            (None, written.is_some())
                        }
                        DelegationCommand::ListenerRead(id, caller, initial, known) => {
                            let accepted = actor.admits_delegate(&id, Some(&caller));
                            if accepted {
                                let progress = actor.applied_bookkeeping(&id, initial)?;
                                version = Some(actor.activity_version(&progress)?);
                                if version != known {
                                    activity = Some(actor.listener_activity(
                                        &id,
                                        &caller,
                                        &progress,
                                        crate::wire::row,
                                    )?);
                                }
                                applied = Some(progress);
                            }
                            (None, accepted)
                        }
                        DelegationCommand::ListenerWrite(id, caller, initial, update) => {
                            let accepted = actor.admits_delegate(&id, Some(&caller));
                            if accepted {
                                actor.keep_listener(&id, initial, update, now)?;
                            }
                            (None, accepted)
                        }
                        DelegationCommand::Applied(id, initial) => {
                            applied = Some(actor.applied_bookkeeping(&id, initial)?);
                            (None, true)
                        }
                        DelegationCommand::Baseline(offset) => {
                            baseline = actor.delegation_baseline(offset)?;
                            (None, true)
                        }
                        DelegationCommand::Inspect(id) => (
                            match id {
                                Some(id) => actor.recorded_delegation(&id)?,
                                None => None,
                            },
                            true,
                        ),
                        DelegationCommand::Change(t) => {
                            let id = match &t {
                                clarp_actor::delegation::Transition::Start { binding }
                                | clarp_actor::delegation::Transition::Stop { binding, .. } => {
                                    binding.delegation_id.clone()
                                }
                            };
                            let accepted = actor.change_delegation(t, now)?;
                            (actor.recorded_delegation(&id)?, accepted)
                        }
                    };
                    Ok(DelegationState {
                        version,
                        activity,
                        applied,
                        baseline,
                        active: actor.active_delegation().cloned(),
                        prior,
                        accepted,
                    })
                })();
                match result {
                    Ok(state) => {
                        let _ = answer.send(Ok(state));
                        return Ok(());
                    }
                    Err(error) => {
                        let _ = answer.send(Err(unavailable(&error)));
                        return Err(error);
                    }
                }
            }
            Ask::Bookkeeping { cmd, answer } => {
                answered(crate::bookkeeping::execute(actor, cmd, now), answer)?
            }
            Ask::KeeperEvents {
                plan,
                after,
                answer,
            } => {
                let page = actor.next_goal_event_id().and_then(|next| {
                    let after = after.clamp(0, next - 1);
                    actor
                        .goal_events(&plan, after, 500)
                        .map(|(events, more)| (after, events, more))
                });
                return read(page, answer);
            }
            Ask::GoalEvents {
                plan,
                after,
                limit,
                answer,
            } => return read(actor.goal_events(&plan, after, limit), answer),
            Ask::Row { id, answer } => return read(actor.row(&id), answer),
            Ask::Delivery {
                mut turns,
                query,
                scope,
                answer,
            } => {
                let top = actor.delivery(&mut turns, &query, &scope);
                return read(Ok((turns, top)), answer);
            }
            Ask::RevisionAt { ts_ms, answer } => return read(actor.revision_at(ts_ms), answer),
            // Answered above, where what they change is published first.
            Ask::Artifact { .. }
            | Ask::Goal { .. }
            | Ask::Amended { .. }
            | Ask::Terminal { .. }
            | Ask::VisitProvider(_)
            | Ask::TakeLibrary { .. }
            | Ask::TakePlans { .. }
            | Ask::ClaimLine { .. }
            | Ask::LineBegun { .. }
            | Ask::LineEnded { .. }
            | Ask::LineAnnounced { .. }
            | Ask::LinePlayed { .. } => return Ok(()),
            Ask::TakeoverHeld { offer, held } => {
                let release = || {
                    crate::launcher::takeover_request(&offer.socket, clarp_shim::Request::Release);
                };
                let plan = if held.partial_out == 0
                    && held.partial_err == 0
                    && held.partial_reports == 0
                    && held.left_ms > 0
                    && actor.takeover_quiet(&offer, held.spool)?
                {
                    crate::takeover::plan(
                        &self.setup.record.backend,
                        &crate::config::home(),
                        &self.setup.record.cwd,
                        &self.setup.record.agent_id,
                        &offer,
                    )
                } else {
                    None
                };
                let Some((session, input)) = plan else {
                    release();
                    return Ok(());
                };
                if !actor.select_takeover(&offer, held.spool, session, input, now)? {
                    release();
                    return Ok(());
                }
                let reply = crate::launcher::takeover_request(
                    &offer.socket,
                    clarp_shim::Request::ConsumeHold {
                        fence: held.spool,
                        replaced_by: offer.into.0,
                    },
                );
                let now = self.setup.clock.now_ms();
                let out = match reply.and_then(|reply| reply.killed) {
                    Some(true) => actor.finish_takeover(offer.from, now)?,
                    Some(false) => {
                        actor.clear_takeover(offer.from, now)?;
                        release();
                        Outputs::default()
                    }
                    None => actor.spool_changed(now)?,
                };
                (out, nobody())
            }
            Ask::SpoolChanged => (actor.spool_changed(now)?, nobody()),
            Ask::LoginChanged => (actor.login_changed(now)?, nobody()),
            Ask::TerminalFacts { answer } => {
                let profile = actor.profile();
                // A turn of the agent is told the agent's name and where its tools find
                // their Host: so is its tool in a terminal.
                let named = (
                    clarp_backend::SESSION_ENV.to_string(),
                    profile.session.clone(),
                );
                let facts = TerminalFacts {
                    tool_session: actor.status().backend_session,
                    cwd: profile.cwd.clone(),
                    env: std::iter::once(named)
                        .chain(profile.tool_env.iter().cloned())
                        .collect(),
                };
                return read(Ok(facts), answer);
            }
            Ask::AccountStop => {
                if self.held.is_some() {
                    // Its launch is held: nothing of it runs.
                    if let Some(pool) = self.setup.pool() {
                        pool.stopped(&self.setup.record.agent_id);
                    }
                    return Ok(());
                }
                (actor.stop_for_an_account(now)?, nobody())
            }
            Ask::AccountBack => match self.held.take() {
                // The launch that was held is carried out now, if the pool lets it.
                Some(order) => {
                    actor.held_for_an_account(false, now);
                    let out = Outputs {
                        actions: vec![Action::Launch(order)],
                        changed: Changed {
                            status: true,
                            ..Changed::default()
                        },
                        ..Outputs::default()
                    };
                    (out, nobody())
                }
                None => (actor.login_changed(now)?, nobody()),
            },
            Ask::AccountEnd => (actor.end_waiting(now)?, nobody()),
            Ask::AccountHeld => {
                actor.held_for_its_model();
                let out = Outputs {
                    changed: Changed {
                        status: true,
                        ..Changed::default()
                    },
                    ..Outputs::default()
                };
                (out, nobody())
            }
            Ask::Explained { item, fields } => {
                // Whoever watches is told with what else is shown, after this.
                actor.shown_patch(&item, &fields, now);
                return Ok(());
            }
            Ask::ShimGone { turn, attempt } => (actor.shim_gone(turn, attempt, now)?, nobody()),
            Ask::LaunchFailed {
                turn,
                attempt,
                reason,
            } => (actor.launch_failed(turn, attempt, &reason, now)?, nobody()),
            Ask::Close(_) => return Ok(()),
        };
        // What follows from a command is done and published before its answer is given.
        self.after(out);
        owed();
        Ok(())
    }

    /// A terminal on the agent's own tool was opened, or closed. When one is closed, the
    /// launch that waited is carried out by the way of any launch: where another terminal
    /// is still open it waits again.
    fn terminal(&mut self, open: bool) -> Result<(), ActorError> {
        if self.setup.record.backend == "claude"
            && ((open && self.terminals == 0) || (!open && self.terminals == 1))
        {
            let now = self.now_ms();
            if let Some(actor) = self.actor.as_mut()
                && let Some(native) = actor.helper_context().1.map(str::to_owned)
            {
                actor.provider_terminal(&native, open, now)?;
                self.publish_view();
            }
        }
        if open {
            self.terminals += 1;
            return Ok(());
        }
        self.terminals = self.terminals.saturating_sub(1);
        if let Some(order) = self.behind_terminal.take() {
            self.carry_out(Action::Launch(order));
        }
        Ok(())
    }

    /// Starts the next attempt of a turn whose wait is over.
    fn retry_turn(&mut self) -> Result<(), ActorError> {
        let now = self.now_ms();
        let Some(actor) = self.actor.as_mut() else {
            return Ok(());
        };
        let out = actor.retry_due(now)?;
        self.after(out);
        Ok(())
    }

    /// Does what the actor decided, and tells the clients what changed.
    fn after(&mut self, mut out: Outputs) {
        // What the agent passed through while its tool's output was read is told before the
        // state at the end.
        self.passed = std::mem::take(&mut out.passed);
        // The agent's conversation with its tool was carried over into a new one: its goals
        // are bound to that conversation and follow it, each with the artifact it is shown as.
        for (from, to) in std::mem::take(&mut out.carried) {
            let now = self.now_ms();
            let Some(actor) = self.actor.as_mut() else {
                break;
            };
            let moved = with_goals(actor, now, |book, _| book.carried(&from, &to, CARRIED, now));
            for change in &moved {
                match keep_goal(
                    actor,
                    &self.setup.record,
                    change,
                    None,
                    clarp_kernel::goals::ledger::Author::System,
                    now,
                ) {
                    Ok(kept) => {
                        out.changed.goals |= kept.changed.goals;
                        out.changed.artifacts |= kept.changed.artifacts;
                    }
                    Err(error) => eprintln!(
                        "clarp-host: agent {}: a goal did not follow its conversation: {error}",
                        self.setup.record.session
                    ),
                }
            }
        }
        if out.damaged_lines > 0 {
            eprintln!(
                "clarp-host: agent {}: {} damaged line(s) in a spool were passed over",
                self.setup.record.session, out.damaged_lines
            );
        }
        for action in out.actions {
            self.carry_out(action);
        }
        // The pool of the tool's accounts is told when the turn it knows is over.
        if let (Some(pool), Some(turn)) = (self.setup.pool(), self.pooled) {
            let live = self.actor.as_ref().and_then(|actor| actor.status().live);
            if live.map(|live| live.turn) != Some(turn) {
                pool.over(&self.setup.record.agent_id, turn);
                self.pooled = None;
                self.held = None;
            }
        }
        // The books are published before anything is told of them, and before whoever
        // asked for the change is answered.
        if out.changed.goals || out.changed.artifacts || out.changed.media {
            self.refresh_books(out.changed.media);
            self.publish_view();
        }
        if out.changed.artifacts {
            self.tell_artifacts();
        }
        // What the agent says aloud is published before the one that speaks is told of it.
        if out.changed.speech {
            if self.read_speech() {
                self.publish_view();
            }
            self.setup.speaks.wake(&self.setup.record.agent_id);
        }
        self.tell_deliveries();
        self.show(out.changed, &out.joined_reads);
    }

    /// A user send's delivery moved (fenced by the user's Stop, or handed over): the apps are
    /// told which, by its id alone, and read what it is from the log.
    fn tell_deliveries(&mut self) {
        let now = self.now_ms();
        let Some(actor) = self.actor.as_mut() else {
            return;
        };
        match actor.take_deliveries(now) {
            Ok(told) => {
                let record = &self.setup.record;
                for message_id in told {
                    let hint = wire::delivery_updated(record, &message_id);
                    self.setup.hub.publish(hint, now);
                }
            }
            Err(error) => eprintln!(
                "clarp-host: agent {}: the delivery of its sends was not read: {error}",
                self.setup.record.session
            ),
        }
    }

    /// Each turn has a thread of its own for as long as its shim lives. It does the waiting, so
    /// the agent's thread never does.
    fn carry_out(&mut self, action: Action) {
        let action = match action {
            // No turn's tool is started while a terminal is open on the same tool: the
            // launch waits until the terminal is closed.
            Action::Launch(order) if self.terminals > 0 => {
                self.behind_terminal = Some(order);
                return;
            }
            // A turn whose launch waits for a terminal has no shim to stop: it is gone at
            // once.
            Action::Stop { turn, .. }
                if self
                    .behind_terminal
                    .as_ref()
                    .is_some_and(|order| order.turn == turn) =>
            {
                let attempt = self.behind_terminal.take().map_or(1, |order| order.attempt);
                tell(&self.tx, turn, attempt, Told::Gone);
                return;
            }
            other => other,
        };
        let action = match action {
            // An attempt of a turn is about to be launched: the pool of the tool's accounts
            // says whether it has to wait for an account instead. One that does is launched
            // when the pool takes the turn up.
            Action::Launch(order) if self.setup.pool().is_some() => {
                let now = self.now_ms();
                let model = self.actor.as_ref().and_then(|actor| actor.turn_model());
                let waits = self.setup.pool().is_some_and(|pool| {
                    let model = model.as_deref().unwrap_or("");
                    pool.starting(&self.setup.record.agent_id, order.turn, model)
                });
                self.pooled = Some(order.turn);
                if waits {
                    if let Some(actor) = self.actor.as_mut() {
                        actor.held_for_an_account(true, now);
                    }
                    self.held = Some(order);
                    return;
                }
                Action::Launch(order)
            }
            // A turn that ran on while the Host was down is the pool's as well. Where the
            // pool says it has to wait (a recovery was under way), it is stopped for its
            // account like any turn that runs when one begins.
            Action::Follow {
                turn,
                attempt,
                socket,
            } if self.setup.pool().is_some() => {
                let model = self.actor.as_ref().and_then(|actor| actor.turn_model());
                let waits = self.setup.pool().is_some_and(|pool| {
                    let model = model.as_deref().unwrap_or("");
                    pool.starting(&self.setup.record.agent_id, turn, model)
                });
                self.pooled = Some(turn);
                if waits {
                    let _ = self.tx.send(Ask::AccountStop);
                }
                Action::Follow {
                    turn,
                    attempt,
                    socket,
                }
            }
            // A turn whose launch is held has no shim to stop: it is gone at once.
            Action::Stop { turn, .. }
                if self.held.as_ref().is_some_and(|order| order.turn == turn) =>
            {
                let attempt = self.held.take().map_or(1, |order| order.attempt);
                tell(&self.tx, turn, attempt, Told::Gone);
                return;
            }
            other => other,
        };
        let tx = self.tx.clone();
        let spawned = match action {
            Action::Launch(mut order) => {
                if self.setup.record.backend == "claude" {
                    if let Some(settings) = self
                        .setup
                        .launcher
                        .claude_settings(&self.setup.record.agent_id)
                    {
                        order.args.extend(["--settings".into(), settings]);
                    }
                    let at = self.now_ms();
                    if let Some(actor) = self.actor.as_mut() {
                        let native = actor
                            .helper_context()
                            .1
                            .unwrap_or(&self.setup.profile.conversation_id)
                            .to_owned();
                        let root = crate::past::projects(&crate::config::home());
                        let mark =
                            crate::helper_source::boundary(&root, &self.setup.record.cwd, &native)
                                .map_err(|e| e.to_string())
                                .and_then(|identity| {
                                    actor
                                        .provider_boundary(
                                            &native,
                                            Some(clarp_kernel::provider_tasks::Attempt {
                                                turn: order.turn.0,
                                                attempt: order.attempt,
                                            }),
                                            identity,
                                            at,
                                        )
                                        .map_err(|e| e.to_string())
                                });
                        if let Err(why) = mark {
                            tell(
                                &tx,
                                order.turn,
                                order.attempt,
                                Told::Failed(format!(
                                    "could not record provider turn boundary: {why}"
                                )),
                            );
                            return;
                        }
                    }
                }
                let pool = self.setup.pool().cloned();
                // Whose turn this is: a Clarp tool of the turn that presents its token is known
                // to be this agent's. Nothing is written for it.
                let agent_id = &self.setup.record.agent_id;
                if let Some(token) = self.setup.turns.token(agent_id, order.turn.0) {
                    let named = crate::turn_identity::TURN_ENV.to_string();
                    order.env.push((named, token));
                }
                let launcher = self.setup.launcher.clone();
                let opening_gate = self.setup.opening_gate.clone();
                let (turn, attempt) = (order.turn, order.attempt);
                std::thread::Builder::new()
                    .name(format!("turn-{}", turn.0))
                    .spawn(move || {
                        launcher::gate_steering(&mut order);
                        if let Some(pool) = pool {
                            pool.guard_turn(&mut order);
                        }
                        if let Some(opening) = order.opening.as_deref() {
                            let (id, trace) = (opening.id.as_str(), opening.trace.as_str());
                            use crate::launcher::Launched;
                            use clarp_kernel::rings::opening::Gate as Phase;
                            loop {
                                if !opening_gate.ask(Phase::Claim, opening, attempt)
                                    || !opening_gate.ask(Phase::Parked, opening, attempt)
                                {
                                    tell(
                                        &tx,
                                        turn,
                                        attempt,
                                        Told::Failed("call opening is no longer authorized".into()),
                                    );
                                    return;
                                }
                                match launcher.launch(&order) {
                                    Launched::NoShim(reason) => {
                                        if !opening_gate.ask(Phase::ReleaseUnsent, opening, attempt)
                                        {
                                            tell(&tx, turn, attempt, Told::Failed(reason));
                                            return;
                                        }
                                        std::thread::sleep(Duration::from_secs(1));
                                    }
                                    Launched::Failed(_) => {
                                        opening_gate.failed(id, trace);
                                        tell(&tx, turn, attempt, Told::Gone);
                                        return;
                                    }
                                    Launched::SaidWhy => {
                                        // The actor parks from the synced SpawnFailed record.
                                        // Its next attempt presents that proof to the gate.
                                        tell(&tx, turn, attempt, Told::SpoolChanged);
                                        return;
                                    }
                                    Launched::TurnTaken => {
                                        crate::launcher::follow(&order.socket, &|told| {
                                            tell(&tx, turn, attempt, told)
                                        });
                                        return;
                                    }
                                    Launched::Ready(mut child) => {
                                        crate::launcher::follow(&order.socket, &|told| {
                                            tell(&tx, turn, attempt, told)
                                        });
                                        let _ = child.wait();
                                        return;
                                    }
                                }
                            }
                        }
                        launcher.launch_and_follow(&order, &|told| tell(&tx, turn, attempt, told))
                    })
            }
            Action::Follow {
                turn,
                attempt,
                socket,
            } => std::thread::Builder::new()
                .name(format!("turn-{}", turn.0))
                .spawn(move || launcher::follow(&socket, &|told| tell(&tx, turn, attempt, told))),
            Action::Stop {
                socket, grace_ms, ..
            } => std::thread::Builder::new()
                .name("stop".to_string())
                .spawn(move || launcher::stop(&socket, grace_ms)),
            Action::Steer { socket, input } => {
                self.write_followup(socket, input);
                return;
            }
        };
        if let Err(error) = spawned {
            eprintln!("clarp-host: a turn's thread could not be started: {error}");
        }
    }

    fn write_followup(&mut self, socket: PathBuf, input: String) {
        if self.followups.is_none() {
            let (send, receive) = mpsc::channel::<(PathBuf, String)>();
            if let Err(error) = std::thread::Builder::new()
                .name("follow-up-input".to_string())
                .spawn(move || {
                    let Ok(runtime) = tokio::runtime::Builder::new_current_thread()
                        .enable_all()
                        .build()
                    else {
                        return;
                    };
                    for (socket, input) in receive {
                        runtime.block_on(launcher::steer(&socket, input));
                    }
                })
            {
                eprintln!("clarp-host: follow-up input thread could not be started: {error}");
                return;
            }
            self.followups = Some(send);
        }
        if let Some(send) = &self.followups {
            let _ = send.send((socket, input));
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
                        ..Message::default()
                    };
                    // Left for the agent, as the Python Host leaves it: it goes through the
                    // queue, where an app shows it while it waits. A hold that was placed
                    // before does not hold it: a goal that is being worked was resumed or made
                    // after the last Stop.
                    let woken = actor.keep_with(
                        &kept_goal(&change),
                        &message,
                        Admission::Later,
                        &clarp_actor::Marks::default(),
                        now,
                    )?;
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
            let (_, out) = actor.job(command, now, || self.setup.revisions.next(now))?;
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
        // Python Host does. Also into a conversation that ended when the agent was started
        // again: what it last said is shown of it until it says something new.
        let newest = actor.tail_of_any(1).ok()?.entries.into_iter().last()?;
        if wire::previewed(&newest) {
            return Some(newest);
        }
        let back = actor.tail_of_any(50).ok()?.entries;
        back.into_iter().rev().find(wire::previewed)
    }

    fn update_previews(&mut self) {
        const HIDDEN_PREVIEW_ROWS: usize = 200;

        self.newest = self.newest_row();
        self.visible_previews = self
            .actor
            .as_ref()
            .filter(|actor| actor.profile().hides_coordination)
            .and_then(|actor| {
                actor
                    .visible_previews(wire::previewed_entry, HIDDEN_PREVIEW_ROWS)
                    .ok()
            })
            .unwrap_or_default();
    }

    /// How the last turn ended, as the log has it.
    fn last_end(&self) -> Option<TurnEnd> {
        self.actor.as_ref()?.last_end().ok()?
    }

    /// What the agent last finished for its user, as the log has it and as the agent is
    /// now named: nothing where the user or the Oracle wrote to it since, where the user
    /// was told of nothing, or where it is too long ago for anyone to be shown it.
    fn last_finished(&self) -> Option<Arc<Value>> {
        let actor = self.actor.as_ref()?;
        let since = self.now_ms() - attention::FINISHED_SHOWN_FOR_MS;
        let (asked, answer) = actor
            .last_finished_for(attention::for_the_user, since)
            .ok()??;
        let finished = wire::finished(&self.setup.record, &actor.status(), &asked, &answer);
        finished.map(Arc::new)
    }

    fn publish_view(&self) {
        let Some(actor) = &self.actor else {
            return;
        };
        let shown_since = self.now_ms() - JOB_SHOWN_FOR_MS;
        let status = actor.status();
        let working = status
            .live
            .as_ref()
            .map(|live| working_with(actor, live, &self.setup.hub));
        self.view.send_replace(Arc::new(View {
            latest_turn_start: actor.latest_turn_start(),
            status,
            newest: self.newest.clone(),
            visible_previews: self.visible_previews.clone(),
            working,
            can_leave_note: actor.profile().hides_coordination,
            ended: self.ended.clone(),
            finished: self.finished.clone(),
            backend: actor.backend_id(),
            provider_bindings: actor
                .provider_sessions()
                .map(|(id, s)| {
                    (
                        id.clone(),
                        s.ended_at,
                        s.links.values().filter_map(|link| link.exited_at).min(),
                    )
                })
                .collect(),
            provider_mirrors: actor
                .jobs()
                .jobs()
                .filter(|j| j.provider_owned())
                .map(|j| {
                    (
                        j.id.clone(),
                        actor.jobs().provider_replacement(j).map(str::to_owned),
                    )
                })
                .collect(),
            jobs: actor
                .jobs()
                .jobs()
                .filter(|job| job.status.is_active() || job.updated_at_ms >= shown_since)
                .cloned()
                .collect(),
            books: self.books.clone(),
            speech: self.speech.clone(),
            broken: None,
        }));
    }

    /// Publishes the agent's view and tells the clients what changed since they were last told.
    fn show(&mut self, changed: Changed, joined_reads: &[JoinedRead]) {
        let by_hand = std::mem::take(&mut self.let_go_by_hand);
        let queue_edit = self.queue_edit.take();
        let passed = std::mem::take(&mut self.passed);
        if !(changed.conversation || changed.status || changed.queue || changed.jobs) {
            return;
        }
        if changed.conversation {
            self.update_previews();
        }
        let job_events = self
            .actor
            .as_mut()
            .map(Actor::take_job_events)
            .unwrap_or_default();
        let Some(actor) = &self.actor else {
            return;
        };
        let status = actor.status();
        let backend = actor.backend_id();
        let before = self.told.take();
        let was_live = before.as_ref().and_then(|told| told.live.as_ref());
        let was_waiting: &[TurnId] = before.as_ref().map_or(&[], |told| told.waiting.as_slice());
        // The messages that have joined the queue since the clients were last told.
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
                .map(|waiting| waiting.told_id)
                .collect()
        } else {
            Vec::new()
        };
        let turn_ended = status.last.as_ref().map(|last| last.id)
            != before
                .as_ref()
                .and_then(|told| told.last.as_ref())
                .map(|was| was.id);
        let now = self.now_ms();
        // What the agent last finished for its user is another thing once a turn of the
        // user's or the Oracle's began or ended, and once something came to wait for the
        // agent or was taken back: whatever else it does between leaves that as it was.
        // (An agent that is started again is opened anew, and it is read then.)
        let began = status
            .live
            .as_ref()
            .filter(|live| Some(live.turn) != was_live.map(|was| was.turn));
        let ended = status.last.as_ref().filter(|_| turn_ended);
        if let Some(c) = &self.setup.hub.capture {
            if let Some(live) = began {
                c.turn(now, "started", &self.setup.record.agent_id, live.turn.0);
            }
            if let Some(last) = ended {
                c.turn(now, "settled", &self.setup.record.agent_id, last.id.0);
            }
        }
        if let Some(last) = ended
            && matches!(
                last.outcome,
                Outcome::Interrupted { .. } | Outcome::Failed { .. }
            )
            && let Ok(message) = actor.message_of(last)
            && message.unminted
            && message.client_id().starts_with("call-open:")
            && let Some(trace) = message
                .trace_id
                .as_deref()
                .filter(|s| s.starts_with("callopen-"))
        {
            self.setup.opening_gate.failed(message.client_id(), trace);
        }

        let theirs = |origin: &Origin| attention::for_the_user(origin.name());
        if began.is_some_and(|live| theirs(&live.origin))
            || ended.is_some_and(|last| theirs(&last.origin))
            || status.waiting != was_waiting
        {
            self.finished = self.last_finished();
        }
        if turn_ended {
            self.ended = self.last_end();
            // What the turn used is told to the Host's tally, under the turn's own name: a
            // Host that starts again tells an agent's last turn once more, and it is still
            // counted once. What is too old to be counted is not told again.
            let counted_since = now - clarp_kernel::usage::KEPT_FOR_MS;
            if let Some(end) = self
                .ended
                .as_ref()
                .filter(|end| end.ended_at_ms > counted_since)
                && let Some(used) = clarp_kernel::usage::Used::said(
                    backend,
                    end.ended_at_ms,
                    (end.tokens_in, end.tokens_out),
                    end.cost_micros.map(|micros| micros as f64 / 1_000_000.0),
                    end.cost_so_far.is_some(),
                )
            {
                let agent_id = &self.setup.record.agent_id;
                self.setup.tally.ended(agent_id, end.turn, used);
            }
            // And what the tool wrote of its account's limits meanwhile is read, whatever
            // became of the turn: Codex writes it into its conversation's own record.
            if backend == crate::quota::CODEX || backend == crate::quota::CLAUDE {
                let session = status.backend_session.as_deref().unwrap_or("");
                // How it ended: where it completed, by the turn's own end, which is the same
                // when a Host that starts again tells the turn once more.
                use crate::quota::Ending;
                let refused = clarp_kernel::failures::USAGE_LIMIT;
                let how = match status.last.as_ref().map(|last| (&last.outcome, last)) {
                    Some((Outcome::Completed, last)) => Ending::Completed(last.ended_at_ms),
                    Some((Outcome::Failed { kind, .. }, _)) if kind == refused => Ending::Refused,
                    _ => Ending::Otherwise,
                };
                // And what the tool itself said of its account in the turn, where it did.
                let said = self.ended.as_ref().and_then(|end| end.limits.clone());
                self.setup.tally.over(backend, session, how, said);
            }
        }
        let (hub, record) = (self.setup.hub.clone(), self.setup.record.clone());

        // The queue: a message joined it, one left it to run, it was held or let go.
        for client_id in &joined {
            hub.publish(
                wire::queue_updated(&record, &status, wire::QueueUpdate::Joined(client_id)),
                now,
            );
        }
        for read in joined_reads {
            let mut event = wire::queue_updated(
                &record,
                &status,
                wire::QueueUpdate::Started(&read.client_id),
            );
            event["queue_depth"] = json!(read.queue.waiting);
            event["queue_paused"] = json!(read.queue.paused);
            event["queue_revision"] = json!(read.queue.revision);
            hub.publish(event, now);
        }
        // A turn began: the word a send is answered with, for every client that did not send it.
        if let Some(live) = &status.live
            && was_live.map(|was| was.turn) != Some(live.turn)
        {
            // It left the queue to run: it had waited there, or it was left for later and
            // nothing was in its way.
            if live.from_queue {
                hub.publish(
                    wire::queue_updated(
                        &record,
                        &status,
                        wire::QueueUpdate::Started(&live.client_id),
                    ),
                    now,
                );
            }
            hub.publish(
                wire::dispatched(
                    &record,
                    live,
                    status.queue_depth,
                    &working_with(actor, live, &hub),
                ),
                now,
            );
        }
        // A turn that waited for an account is taken up: the word is said once more, as the
        // Python Host says it when it gives the turn to its tool anew.
        if let (Some(live), Some(was)) = (&status.live, was_live)
            && was.turn == live.turn
            && waits_for_an_account(was)
            && !waits_for_an_account(live)
        {
            hub.publish(
                wire::dispatched(
                    &record,
                    live,
                    status.queue_depth,
                    &working_with(actor, live, &hub),
                ),
                now,
            );
        }
        // The mailbox was held or let go, or a waiting message was taken back: it left the
        // queue and no turn began for it. A hold that ended by itself, because the last
        // thing it held went to run, was told with that turn a moment ago.
        let held_or_let_go = before
            .as_ref()
            .is_some_and(|told| told.paused != status.paused);
        let began_from_the_queue = status
            .live
            .as_ref()
            .is_some_and(|live| live.from_queue && was_live.map(|was| was.turn) != Some(live.turn));
        let told_with_its_turn = !status.paused && !by_hand && began_from_the_queue;
        let taken_back = was_waiting.iter().any(|turn| {
            !status.waiting.contains(turn)
                && !joined_reads.iter().any(|read| read.turn == *turn)
                && status.live.as_ref().map(|live| live.turn) != Some(*turn)
                && status.last.as_ref().map(|last| last.id) != Some(*turn)
        });
        let directory = queue_edit.as_ref().and_then(|_| hub.queue_directory());
        let name = |item: &Waiting| wire::queue_sender_name(item, directory.as_deref());
        let update = match &queue_edit {
            Some(QueueEdit::Edited { item, text }) => Some(wire::QueueUpdate::Edited {
                item,
                text,
                sender_name: name(item),
            }),
            Some(QueueEdit::Deleted(item)) => Some(wire::QueueUpdate::Deleted {
                item,
                sender_name: name(item),
            }),
            None if (held_or_let_go && !told_with_its_turn) || taken_back => {
                Some(wire::QueueUpdate::Mailbox)
            }
            None => None,
        };
        if let Some(update) = update {
            hub.publish(wire::queue_updated(&record, &status, update), now);
        }
        // What the agent passed through on the way: each state in its turn, as it stood. A
        // tool that prints a whole turn in one breath is told as one that took its time.
        let mut last_told = before.as_ref().map(told_state);
        let live_now = status.live.as_ref().map(|live| live.turn);
        for stood in &passed {
            // Of the turn that holds the agent, or of the one that ended with nothing begun
            // after it: what a turn passed through that another has followed in the same
            // breath is not told after that one began.
            let of = stood.live.as_ref().map(|live| live.turn);
            let in_order = of.is_some() && (live_now.is_none() || live_now == of);
            if in_order && last_told != Some(told_state(stood)) {
                tell_work(&hub, actor, &record, stood, backend, None, now);
                last_told = Some(told_state(stood));
            }
        }
        // What the agent is doing changed: its state, and the line that describes it.
        if last_told != Some(told_state(&status)) {
            if turn_ended {
                // The agent as it stood when the turn had ended and the next had not begun.
                let rested = Status {
                    live: None,
                    ..status.clone()
                };
                if let Some(at_once) = wire::stopped(&record, &rested, self.ended.as_ref()) {
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
            tell_work(
                &hub,
                actor,
                &record,
                &status,
                backend,
                self.ended.as_ref(),
                now,
            );
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
            let id = clarp_kernel::notify::id(&record.agent_id, ended.ended_at_ms);
            // A reply with nothing for the user in it is not something to be told of, and
            // neither is what agents say to each other.
            if let Some(told) =
                wire::user_notification(&record, &id, &asked, reply, ended.ended_at_ms)
            {
                // A phone that is not listening is pushed to, when the rule says so. With a
                // sound when the agent still waits for an answer from the user.
                if told["push"] == true {
                    self.setup.pusher.turn_done(TurnDone {
                        badge: told["badge"] == true,
                        unread: told["unread"] == true,
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
        for event in job_events {
            if let Some(job) = actor.jobs().get(&event.id) {
                let replacement = actor.jobs().provider_replacement(job);
                let mut told = wire::job_updated(&record, job, event.at);
                told["job"] = clarp_kernel::provider_tasks::projected_event(
                    &told["job"],
                    replacement,
                    event.at,
                );
                told["status"] = told["job"]["status"].clone();
                told["change_revision"] = json!(event.revision);
                let status = told["status"].as_str().unwrap_or_default().to_owned();
                hub.publish(told, now);
                if job.provider_owned() || job.metadata.get("tool_use_id").is_some() {
                    let key = (job.generation, status);
                    if self.jobs_told.get(&job.id) != Some(&key) {
                        self.jobs_told.insert(job.id.clone(), key);
                        hub.publish(json!({"type":"agent-roster","kind":"background-job"}), now);
                    }
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
        let mut event = wire::transcript_updated(&self.setup.record);
        if let Some(session) = self.helper_change_session.take() {
            event["backend_session_id"] = Value::String(session);
        }
        self.setup.hub.publish(event, now);
    }

    fn show_broken(&self, why: &str) {
        let mut view = (**self.view.borrow()).clone();
        view.broken = Some(why.to_string());
        self.view.send_replace(Arc::new(view));
    }
}

/// What a client is shown as an agent's state: which turn runs and what it does, or which
/// turn ended last, and a repair's note while it stands. A change in any of it is told.
fn told_state(status: &Status) -> ToldState {
    let live = status.live.as_ref();
    // What it does within its state: the tool it runs, or that it writes its answer now.
    // An agent that goes from thinking to answering is thinking still by its state, and
    // the apps are told all the same, as the Python Host tells them.
    let doing = match live.map(|live| &live.activity) {
        // It waits for an account: when it is taken up the apps are told that it works again.
        // And when what it waits for is said in other words: one that serves its model.
        _ if live.is_some_and(|live| live.waits_for_its_model.is_some()) => {
            "an account for its model".to_string()
        }
        _ if live.is_some_and(waits_for_an_account) => "an account".to_string(),
        Some(Activity::Tool { name }) => name.clone(),
        Some(Activity::Responding) => wire::RESPONDING.to_string(),
        _ => String::new(),
    };
    (
        live.map(|live| live.turn),
        wire::state_name(&wire::state_kind(status)),
        doing,
        status.last.as_ref().map(|last| last.id),
        status.label.clone(),
        // An attempt of the live turn failed and the turn is tried again: the apps are told
        // that it reconnects. They are not told when the next attempt begins.
        live.map_or(0, |live| live.failed_attempts),
        // A repair: the same state, told again with who repaired it and why.
        status.note.as_ref().map(|note| note.at_ms),
    )
}

/// What [`told_state`] compares: a change in any of it is told.
type ToldState = (
    Option<TurnId>,
    String,
    String,
    Option<TurnId>,
    String,
    u32,
    Option<i64>,
);

/// Whether a live turn waits for an account: between two attempts, or before its first.
fn waits_for_an_account(live: &clarp_actor::LiveStatus) -> bool {
    let parked = live.parked.as_ref();
    live.held_for_an_account || parked.is_some_and(|parked| parked.wait.is_for_an_account())
}

/// Passes what the thread of one attempt of a turn says on to the agent's thread.
fn tell(tx: &mpsc::Sender<Ask>, turn: TurnId, attempt: u32, told: Told) {
    let ask = match told {
        Told::SpoolChanged => Ask::SpoolChanged,
        Told::Gone => Ask::ShimGone { turn, attempt },
        Told::Failed(reason) => Ask::LaunchFailed {
            turn,
            attempt,
            reason,
        },
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
/// Whom a turn that begins works for, as its working state says it: the agents when another
/// agent's message began it and this agent can leave a note (its profile says so), with that
/// agent named when it is one of this Host; the user otherwise. A mark that cannot be read is
/// the user's: nothing is hidden by a fault.
fn working_with(actor: &Actor, live: &clarp_actor::LiveStatus, hub: &Hub) -> wire::WorkingWith {
    if !actor.profile().hides_coordination {
        return wire::WorkingWith::User;
    }
    let Ok(Some(sender)) = actor.mark(live.turn.0) else {
        return wire::WorkingWith::User;
    };
    if sender.is_empty() {
        return wire::WorkingWith::Agents { sender: None };
    }
    let known = hub.queue_directory().and_then(|directory| {
        directory
            .named(&sender)
            .map(|member| member.record.persona.clone())
    });
    wire::WorkingWith::Agents {
        sender: known.map(|name| (sender, name)),
    }
}

fn tell_work(
    hub: &Hub,
    actor: &Actor,
    record: &AgentRecord,
    status: &Status,
    backend: &str,
    ended: Option<&TurnEnd>,
    now: i64,
) {
    let mut state = wire::state(record, status, backend, ended);
    let mut activity = wire::activity(record, status);
    if let Some(live) = &status.live
        && matches!(
            state["kind"].as_str(),
            Some("thinking" | "tool" | "compacting")
        )
    {
        let who = working_with(actor, live, hub);
        who.tell(&mut state);
        if hub
            .queue_directory()
            .is_none_or(|directory| crate::hidden_coordination::hides(&directory, &record.agent_id))
            && let Some(headline) = who.headline()
        {
            clarp_kernel::hidden_coordination::veil_detail(&mut state["detail"], &headline);
            clarp_kernel::hidden_coordination::veil_activity(&mut activity, &headline);
        }
    }
    hub.publish(state, now);
    hub.publish(activity, now);
}

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
fn refuse_for(
    rx: &mpsc::Receiver<Ask>,
    wait: Duration,
    why: &str,
    amended: &mut dyn FnMut(AgentRecord),
) -> bool {
    let until = Instant::now() + wait;
    loop {
        let refusal = || Unavailable(format!("the agent cannot be opened: {why}"));
        match rx.recv_timeout(until.saturating_duration_since(Instant::now())) {
            Ok(Ask::Send { answer, .. }) => drop(answer.send(Err(refusal()))),
            Ok(Ask::LeaveNote { answer, .. }) => drop(answer.send(Err(refusal()))),
            Ok(Ask::Stop { answer, .. }) => drop(answer.send(Err(refusal()))),
            Ok(Ask::Repair { answer, .. }) => drop(answer.send(Err(refusal()))),
            Ok(Ask::Resume { answer, .. }) => drop(answer.send(Err(refusal()))),
            Ok(Ask::StartAgain { answer }) => drop(answer.send(Err(refusal()))),
            Ok(Ask::Queue { answer }) => drop(answer.send(Err(refusal()))),
            Ok(Ask::OpeningRow { answer, .. }) => drop(answer.send(Err(refusal()))),
            Ok(Ask::Fate { answer, .. }) => drop(answer.send(Err(refusal()))),
            Ok(Ask::UnrunRequest { answer, .. }) => drop(answer.send(Err(refusal()))),
            Ok(Ask::Standing { answer, .. }) => drop(answer.send(Err(refusal()))),
            Ok(Ask::RunningTrace { answer, .. }) => drop(answer.send(Err(refusal()))),
            Ok(Ask::Shown { answer }) => drop(answer.send(Err(refusal()))),
            Ok(Ask::Import { answer, .. } | Ask::TakeUp { answer, .. }) => {
                drop(answer.send(Err(refusal())));
            }
            Ok(Ask::Withdraw { answer, .. }) => drop(answer.send(Err(refusal()))),
            Ok(Ask::SendNow { answer, .. }) => drop(answer.send(Err(refusal()))),
            Ok(Ask::Reword { answer, .. }) => drop(answer.send(Err(refusal()))),
            Ok(Ask::Note { answer, .. }) => drop(answer.send(Err(refusal()))),
            Ok(Ask::Tail { answer, .. } | Ask::After { answer, .. }) => {
                drop(answer.send(Err(refusal())));
            }
            Ok(Ask::Before { answer, .. }) => drop(answer.send(Err(refusal()))),
            Ok(Ask::Label { answer, .. }) => drop(answer.send(Err(refusal()))),
            Ok(Ask::RetireLabel { answer, .. }) => drop(answer.send(Err(refusal()))),
            Ok(Ask::Job { answer, .. }) => drop(answer.send(Err(refusal()))),
            Ok(Ask::Publish { answer, .. }) => drop(answer.send(Err(refusal()))),
            Ok(Ask::TakePlans { answer, .. }) => drop(answer.send(Err(refusal()))),
            Ok(Ask::TakeLibrary { answer, .. }) => drop(answer.send(Err(refusal()))),
            Ok(Ask::Hold { answer }) => drop(answer.send(Err(refusal()))),
            Ok(Ask::TerminalFacts { answer }) => drop(answer.send(Err(refusal()))),
            Ok(Ask::JobRecord { answer, .. }) => drop(answer.send(Err(refusal()))),
            Ok(Ask::JobTimeline { answer, .. }) => drop(answer.send(Err(refusal()))),
            Ok(Ask::Bookkeeping { answer, .. }) => drop(answer.send(Err(refusal()))),
            Ok(Ask::Delegation { answer, .. }) => drop(answer.send(Err(refusal()))),
            Ok(Ask::KeeperEvents { answer, .. }) => drop(answer.send(Err(refusal()))),
            Ok(Ask::GoalEvents { answer, .. }) => drop(answer.send(Err(refusal()))),
            Ok(Ask::Row { answer, .. }) => drop(answer.send(Err(refusal()))),
            Ok(Ask::Delivery { answer, .. }) => drop(answer.send(Err(refusal()))),
            Ok(Ask::RevisionAt { answer, .. }) => drop(answer.send(Err(refusal()))),
            Ok(Ask::Goal { answer, .. }) => drop(answer.send(Err(refusal()))),
            Ok(Ask::Artifact { answer, .. }) => drop(answer.send(Err(refusal()))),
            Ok(Ask::ClaimLine { answer }) => drop(answer.send(Err(refusal()))),
            Ok(
                Ask::LineBegun { answer, .. }
                | Ask::LineEnded { answer, .. }
                | Ask::LineAnnounced { answer, .. }
                | Ask::LinePlayed { answer, .. },
            ) => drop(answer.send(Err(refusal()))),
            Ok(
                Ask::SpoolChanged
                | Ask::TakeoverHeld { .. }
                | Ask::ShimGone { .. }
                | Ask::LaunchFailed { .. }
                | Ask::LoginChanged
                | Ask::HelperRead { .. }
                | Ask::VisitProvider(_)
                | Ask::ExpireProviderLinks(_)
                | Ask::RefreshTelling
                | Ask::HistoricalRead { .. }
                | Ask::Terminal { .. }
                | Ask::AccountStop
                | Ask::AccountBack
                | Ask::AccountEnd
                | Ask::AccountHeld
                | Ask::Explained { .. },
            ) => {}
            // Who the agent is can be changed while it cannot be opened.
            Ok(Ask::Amended { record, taken }) => {
                amended(*record);
                let _ = taken.send(());
            }
            Ok(Ask::Close(closed)) => {
                drop(closed);
                return false;
            }
            Err(RecvTimeoutError::Disconnected) => return false,
            Err(RecvTimeoutError::Timeout) => return true,
        }
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod mailbox_tests {
    use super::*;

    fn agent_mailbox() -> (AgentHandle, mpsc::Receiver<Ask>) {
        let (tx, rx) = mpsc::channel();
        let (_, view) = watch::channel(Arc::new(View {
            latest_turn_start: 0,
            status: never_ran(),
            newest: None,
            visible_previews: Default::default(),
            working: None,
            can_leave_note: false,
            ended: None,
            finished: None,
            backend: "scripted",
            jobs: Vec::new(),
            provider_mirrors: BTreeMap::new(),
            provider_bindings: Vec::new(),
            books: Arc::default(),
            speech: Arc::default(),
            broken: None,
        }));
        (
            AgentHandle {
                tx,
                view,
                close: Arc::default(),
            },
            rx,
        )
    }

    #[test]
    fn a_later_close_waits_for_the_first_reply_without_sending_another_request() {
        let (agent, rx) = agent_mailbox();
        assert!(!agent.close(Duration::ZERO));
        let Ask::Close(reply) = rx.try_recv().unwrap() else {
            panic!("expected a close request");
        };
        let again = agent.clone();
        assert!(!again.close(Duration::ZERO));
        assert!(matches!(rx.try_recv(), Err(mpsc::TryRecvError::Empty)));
        drop(reply);
        assert!(again.close(Duration::ZERO));
        assert!(matches!(rx.try_recv(), Err(mpsc::TryRecvError::Empty)));
    }

    #[test]
    fn admission_is_fenced_during_a_tombstone_write_and_restored_if_it_fails() {
        let (agent, rx) = agent_mailbox();
        let original = agent.clone();
        let answer = agent.keep_removal(|| {
            assert!(matches!(original.enqueue(Ask::LoginChanged), Err(Unavailable(why)) if why == REMOVING));
            Err(Unavailable("storage failed".into()))
        });
        assert!(matches!(answer, Err(Unavailable(why)) if why == "storage failed"));
        original.enqueue(Ask::LoginChanged).unwrap();
        assert!(matches!(rx.try_recv(), Ok(Ask::LoginChanged)));
        assert!(matches!(rx.try_recv(), Err(mpsc::TryRecvError::Empty)));
    }

    #[test]
    fn a_ready_deadline_gets_a_turn_under_continuous_mail() {
        let (tx, rx) = mpsc::channel();
        let now = Instant::now();
        let mut mailbox = Mailbox::default();
        tx.send(0).unwrap();
        for batch in 0..2 {
            for n in 0..32 {
                let expected = batch * 32 + n;
                tx.send(expected + 1).unwrap();
                assert_eq!(mailbox.next(&rx, Some(now), now), Ok(expected));
            }
            // Mail is still waiting. The timeout branch runs due work before
            // the 33rd message, then gives the mailbox its turn again.
            assert_eq!(
                mailbox.next(&rx, Some(now), now),
                Err(RecvTimeoutError::Timeout)
            );
        }
        for _ in 0..33 {
            tx.send(64).unwrap();
            assert_eq!(mailbox.next(&rx, None, now), Ok(64));
        }
        assert_eq!(
            mailbox.next(&rx, Some(now + Duration::from_secs(1)), now),
            Ok(64)
        );
        drop(tx);
        assert_eq!(
            mailbox.next(&rx, None, now),
            Err(RecvTimeoutError::Disconnected)
        );
    }
}
