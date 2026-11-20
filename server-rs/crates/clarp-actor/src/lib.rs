//! One agent's actor, without the thread it runs on.
//!
//! An [`Actor`] is everything one agent does. It owns the agent's log and is the only thing that
//! writes it. It is asked things (a message, a stop, "your turn's spool has grown"), writes what
//! happened to the log, and answers with what the world must now do: [`Action`]s. It starts no
//! process, opens no socket and waits for nothing, so the same actor runs under a thread in the
//! server and under a simulator that crashes it between any two steps.
//!
//! How a turn runs:
//!
//! 1. A message is accepted: the message and its turn are one write to the log.
//! 2. When the turn starts, the actor writes the message where a shim will read it and answers
//!    with [`Action::Launch`]. The shim makes the turn's spool, starts the tool and records what
//!    it prints.
//! 3. Whenever the spool has grown, the actor reads on from where it was, lets the backend say
//!    what the new lines mean, and writes that to the log together with the position it has
//!    reached. Meaning and position are one write, so nothing in a spool is taken twice and
//!    nothing is skipped.
//! 4. The spool's last record says how the tool went. That ends the turn, and the next waiting
//!    message starts.
//!
//! A server that restarts opens the actor again. A turn that was live is rejoined: the spool is
//! read from its beginning up to the position in the log, only to be where the old server was,
//! and from there on as usual. The tool never noticed.
//!
//! An error from an actor means its log or its files could not be read or written. The actor is
//! then in no known state: drop it and open it again. That is the same path as a restart.

use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use clarp_backend::{Backend, Interpreter, LiveReply, Signal, TurnSpec};
use clarp_kernel::agent::{Command, Effect, Note, Refusal, Rest};
use clarp_kernel::artifacts::{Artifact, ArtifactBook, ArtifactRecord};
use clarp_kernel::control::Control;
use clarp_kernel::conversation::{
    Commentary, CostSoFar, Entry, Imported, LimitsSaid, Message, Notice, Reply, ToolCall,
    ToolStatus, Total, TurnEnd,
};
use clarp_kernel::failures;
pub mod delegation;
mod goal_ledger;
use clarp_kernel::goals::ledger::{self, Author, Event as LedgerEvent};
use clarp_kernel::goals::{Change as GoalChange, GoalBook, GoalRecord};
use clarp_kernel::jobs::{JobBook, JobChange, JobCommand, JobRecord, JobRefusal};
use clarp_kernel::journal::{Batch, Incoming, JournalError, Recorder, Reworded, kind};
use clarp_kernel::live::{self, Hub};
use clarp_kernel::media::{Asset, MediaBook};
use clarp_kernel::recovery;
use clarp_kernel::spoken::{KEPT_FOR_MS, Line as Spoken, Said, SpeechBook, lines_of};
use clarp_kernel::told::{self, Told};
use clarp_kernel::turn::{
    Activity, Admission, COMPACTION, Origin, Outcome, Parked, RunOutcome, Settled, Turn, TurnId,
    Wait, silent_when_failed,
};
use clarp_kernel::turn_identity::Standing;
use clarp_ports::log::{AgentLog, Item, Kinds, LogError, NewItem, Seq};
use clarp_spool::{Entry as Line, ShimEvent, SpoolReader};
pub use goal_ledger::{page as read_goal_events, retire as retire_goal_ledger};

pub mod conversation;
pub mod helper_cells;
pub mod provider_jobs;

use conversation::{Draft, Page, REVISION_STEP, Versioned, Which};

/// How many lines of a spool are read, understood and written to the log at a time.
const WINDOW: usize = 2_000;
/// How many messages of a conversation are read at a time by what looks back through them.
const MESSAGES_AT_A_TIME: usize = 32;

/// What an actor is told about its agent. It does not change while the actor lives.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Profile {
    pub agent_id: String,
    /// The agent's session name: what its tools know it by.
    pub session: String,
    /// The name of the agent's conversation. A tool that can be told starts its own session
    /// under this name, so the conversation is called the same from its first message on.
    pub conversation_id: String,
    /// The agent's own directory. Its turns' spools are in `spool/` under it.
    pub dir: PathBuf,
    /// Where the shims' sockets are. A socket's path has to be short, so not under `dir`.
    pub run_dir: PathBuf,
    /// Where the agent's tool works.
    pub cwd: PathBuf,
    pub model: Option<String>,
    pub effort: Option<String>,
    /// What the agent's tool is told beside the user's words: who the agent is, and how
    /// its spoken answers sound. `None`: nothing. This is the one part of a profile that
    /// follows the agent as it is changed ([`Actor::retell`]).
    pub told: Option<Told>,
    /// How long a tool gets to end by itself when it is told to stop.
    /// What every turn's tool is told on top of what its backend sets: where the agent's own
    /// tools find the Host.
    pub tool_env: Vec<(String, String)>,
    pub stop_grace_ms: u64,
    /// Keep a turn's spool when the turn is over and in the log.
    pub keep_spools: bool,
    /// Whether a turn whose account is out waits for one (`recovery::Accounts`).
    pub accounts: recovery::Accounts,
    /// The name of this run of the Host, for those who watch a turn as it runs: a client that
    /// is told another name knows the Host started again and asks for everything anew.
    pub epoch: String,
}

impl Profile {
    pub fn spool_path(&self, turn: TurnId) -> PathBuf {
        self.spool_path_of(turn, 1)
    }

    /// The message of a turn, where its shim reads it.
    pub fn stdin_path(&self, turn: TurnId) -> PathBuf {
        self.stdin_path_of(turn, 1)
    }

    pub fn socket_path(&self, turn: TurnId) -> PathBuf {
        self.socket_path_of(turn, 1)
    }

    /// What one attempt of a turn writes: every attempt is a tool process of its own, with a
    /// shim, a spool and a socket of its own.
    pub fn spool_path_of(&self, turn: TurnId, attempt: u32) -> PathBuf {
        self.dir
            .join("spool")
            .join(format!("{}.jsonl", attempt_name(turn, attempt)))
    }

    pub fn stdin_path_of(&self, turn: TurnId, attempt: u32) -> PathBuf {
        self.dir
            .join("spool")
            .join(format!("{}.stdin", attempt_name(turn, attempt)))
    }

    pub fn socket_path_of(&self, turn: TurnId, attempt: u32) -> PathBuf {
        self.run_dir.join(format!(
            "{}-{}.sock",
            self.agent_id,
            attempt_name(turn, attempt)
        ))
    }
}

/// The name an attempt's files go by: the turn's number, and from the second attempt on the
/// attempt's too. A turn's first attempt keeps the name a turn always had.
fn attempt_name(turn: TurnId, attempt: u32) -> String {
    if attempt <= 1 {
        turn.0.to_string()
    } else {
        format!("{}.{attempt}", turn.0)
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OpeningOrder {
    pub id: String,
    pub trace: String,
    pub previous_unsent: bool,
}

fn opening_message(message: &Message) -> bool {
    message.unminted
        && message.client_id().starts_with("call-open:")
        && matches!(&message.origin, Origin::Trigger { name } if name == "automation")
        && message
            .trace_id
            .as_deref()
            .is_some_and(|t| t.starts_with("callopen-"))
}

/// Everything a shim needs to take a turn.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ShimOrder {
    pub before_stop: Option<(&'static str, u64)>,
    /// An initiative admitted by the Host. A user can reuse a name but cannot
    /// manufacture this provenance; the gate never intercepts a foreign turn.
    pub opening: Option<Box<OpeningOrder>>,
    pub turn: TurnId,
    /// Which attempt of the turn the shim runs, from 1.
    pub attempt: u32,
    pub spool: PathBuf,
    pub socket: PathBuf,
    pub stdin_file: PathBuf,
    pub prompt_as_file: bool,
    pub named_session: Option<String>,
    pub cwd: PathBuf,
    pub program: String,
    pub args: Vec<String>,
    /// Set on top of the environment the tool inherits.
    pub env: Vec<(String, String)>,
}

/// What the world must do for an actor.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Action {
    /// Start a shim for this turn, and then follow it. If the shim cannot be started, say
    /// [`Actor::launch_failed`]. Starting a shim for a turn that already has one is harmless:
    /// the second finds the spool there and leaves.
    Launch(ShimOrder),
    /// Follow the shim that has this turn: say [`Actor::spool_changed`] whenever it hints that
    /// its spool has grown, and [`Actor::shim_gone`] when nothing answers at its socket any more.
    Follow {
        turn: TurnId,
        attempt: u32,
        socket: PathBuf,
    },
    /// Ask the shim that has this turn to end its tool.
    Stop {
        turn: TurnId,
        socket: PathBuf,
        grace_ms: u64,
    },
}

/// What is different after a call, for whoever tells the clients.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct Changed {
    /// There is something newer in the conversation.
    pub conversation: bool,
    /// What the agent is doing changed.
    pub status: bool,
    /// What waits in the mailbox changed, or whether it is held.
    pub queue: bool,
    /// One of the agent's background jobs changed.
    pub jobs: bool,
    /// One of the agent's goals or plans changed.
    pub goals: bool,
    /// Something the agent made for its user, or asks its user, changed.
    pub artifacts: bool,
    /// The agent published a file.
    pub media: bool,
    /// A line the agent says aloud was written, or what became of one.
    pub speech: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Outputs {
    pub actions: Vec<Action>,
    pub changed: Changed,
    /// The agent's tool named a new conversation in place of one it had let go of, and the
    /// Host carried the conversation over: from which name to which. What is bound to the
    /// agent's conversation with its tool (its goals) follows.
    pub carried: Vec<(String, String)>,
    /// What the agent was doing at each point where the turn that holds it moved on to
    /// something else while its tool's output was read (it began to use a tool, to answer), in
    /// order, as its status stood then. Whoever tells clients of the agent's state tells each
    /// of these before the state at the end: what an app is told of an agent's states then
    /// follows from what its tool printed, and not from how much of it one read happened to
    /// hold.
    pub passed: Vec<Status>,
    /// Lines of a spool that were not records. They were passed over; the operator should know.
    pub damaged_lines: usize,
}

/// The actor's log or files failed. Drop the actor and open it again.
#[derive(Debug)]
pub enum ActorError {
    Journal(JournalError),
    Io(String),
    /// Something in the log is not what the log says it is.
    Corrupt(String),
}

impl std::fmt::Display for ActorError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Journal(error) => write!(f, "{error}"),
            Self::Io(why) => write!(f, "{why}"),
            Self::Corrupt(why) => write!(f, "{why}"),
        }
    }
}

impl std::error::Error for ActorError {}

impl From<JournalError> for ActorError {
    fn from(error: JournalError) -> Self {
        Self::Journal(error)
    }
}

impl From<LogError> for ActorError {
    fn from(error: LogError) -> Self {
        Self::Journal(JournalError::Log(error))
    }
}

/// What became of a message.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Sent {
    pub message_seq: Seq,
    /// The turn it became. `None` for a message that had been sent before.
    pub turn: Option<TurnId>,
    /// It waits: behind a turn that is running, or because the mailbox is held.
    pub queued: bool,
    /// The agent already had this message. Nothing was done.
    pub duplicate: bool,
    /// Where the queue stands with this message taken.
    pub queue: QueueState,
}

/// What became of the turn a message was sent for.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TurnFate {
    /// The agent has no message of the id.
    Unknown,
    /// The message waits in the queue: its turn has not begun.
    Waits,
    /// Its turn runs.
    Runs,
    /// Its turn is over: how it ended (`completed`, `failed` or `interrupted`), the row that
    /// closed it, and what it left: its answer, or the words it was told to have failed in
    /// (none for a turn that was stopped).
    Ended {
        status: String,
        closes: String,
        text: String,
        ended_at_ms: i64,
    },
    /// Its turn neither waits nor runs nor ended: the message was taken back before its
    /// turn began.
    Gone,
}

/// Where an agent's queue stands.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct QueueState {
    /// How many messages wait.
    pub waiting: usize,
    /// The mailbox is held: what waits runs only after a resume.
    pub paused: bool,
    /// Grows with every change of the two above.
    pub revision: u64,
}

/// What became of a waiting message its user sent by hand.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ByHand {
    /// Its turn has begun.
    Sent,
    /// It does not wait: it ran, was taken back, or never was.
    NotWaiting,
    /// A turn of the agent runs: nothing is sent past it.
    AtWork,
}

/// A message that waits its turn.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Waiting {
    pub turn: TurnId,
    /// The message's id in the conversation.
    pub message_id: String,
    /// The id it was sent under: what the queue names it by.
    pub client_id: String,
    /// The id an app is told it by: none for what the Host sent by itself.
    pub told_id: String,
    pub text: String,
    pub speak: bool,
    pub accepted_at_ms: i64,
}

/// A row of the conversation an agent had on another Host, on its way in.
#[derive(Debug, Clone, PartialEq)]
pub struct ImportedRow {
    pub id: String,
    pub ts_ms: i64,
    /// The row as the other Host served it.
    pub row: serde_json::Value,
}

/// A tool call that runs, as much of it as is shown while it does.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LiveTool {
    /// The id of its row in the conversation.
    pub id: String,
    pub name: String,
    /// What kind of thing it does, and what it does it to: see [`clarp_kernel::tools`].
    pub category: &'static str,
    pub label: String,
    /// The same call as the event of a tool state sums it up.
    pub action: String,
    pub summary: String,
    pub file_path: String,
    pub started_at_ms: i64,
}

/// Follows the tool calls that run: one that starts joins them, one that ends leaves.
/// `calls` keeps each as it was told, for the turn that is cut off under it.
fn follow_tool(running: &mut Vec<LiveTool>, calls: &mut Vec<ToolCall>, call: &ToolCall) {
    running.retain(|tool| tool.id != call.id);
    calls.retain(|known| known.id != call.id);
    if call.status == ToolStatus::Running {
        calls.push(call.clone());
        let described = clarp_kernel::tools::describe(&call.name, &call.input);
        let summed = clarp_kernel::tools::summarize(&call.name, &call.input);
        running.push(LiveTool {
            id: call.id.clone(),
            name: call.name.clone(),
            category: described.category,
            label: described.label,
            action: summed.action,
            summary: summed.summary,
            file_path: summed.file_path,
            started_at_ms: call.started_at_ms,
        });
    }
}

/// The turn that holds the agent.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LiveStatus {
    pub turn: TurnId,
    /// The id of the message this turn answers, the id an app is told it by (none for what
    /// the Host sent by itself), and the trace it travels under.
    pub message_id: String,
    pub client_id: String,
    pub trace_id: Option<String>,
    /// Who asked for it.
    pub origin: Origin,
    /// It waited in the queue before it started, or was left for later and went through it.
    pub from_queue: bool,
    /// Its tool has not said anything yet.
    pub starting: bool,
    pub activity: Activity,
    pub started_at_ms: i64,
    pub tool_count: u32,
    /// The tool calls that run now, the one that started last at the end.
    pub tools: Vec<LiveTool>,
    /// It was told to stop and has not gone yet.
    pub stopping: bool,
    /// It waits between two attempts: for what, since when, and on which failure.
    pub parked: Option<Parked>,
    /// The launch of its attempt is held until an account serves it.
    pub held_for_an_account: bool,
    /// It waits for an account that serves its model, while the account now active serves
    /// other turns: the model it runs on (empty: the tool's own choice).
    pub waits_for_its_model: Option<String>,
    /// How many attempts of the turn have failed so far.
    pub failed_attempts: u32,
}

/// What an agent is doing, for a snapshot.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Status {
    pub live: Option<LiveStatus>,
    /// The last turn that held the agent and ended.
    pub last: Option<Settled>,
    /// The turns that wait, in the order they will run.
    pub waiting: Vec<TurnId>,
    pub paused: bool,
    pub queue_revision: u64,
    pub stop_generation: u64,
    pub head_revision: u64,
    /// The tool's own session, where the next turn continues.
    pub backend_session: Option<String>,
    /// What the agent says it is busy with beyond its turns. Empty: nothing.
    pub label: String,
    /// When it said so, while it says so.
    pub label_at_ms: Option<i64>,
    /// What the agent rests in since it last said so, until its next turn starts.
    pub resting: Option<Rest>,
    /// How many tokens the tool's conversation held when a turn last said so: the gauge an
    /// app shows. A turn that says nothing of it (a tool that died) leaves it as it was.
    pub context_tokens: Option<u64>,
    /// When a turn last ran to its end, and the model that answered last. A turn that
    /// failed or was stopped leaves both as they were.
    pub completed_at_ms: Option<i64>,
    pub answered_by: Option<String>,
    /// The agent came from another Host, where it had worked.
    pub arrived: bool,
    /// The note a repair left on the agent's state, while nothing about the state changed since.
    pub note: Option<Note>,
}

/// What changed in the agent's books together, to be written together: a goal,
/// artifacts as changes left them, and files the agent published.
#[derive(Debug, Clone, Copy, Default)]
pub struct Kept<'a> {
    pub author: Author<'a>,
    pub subgoal_event: Option<&'a LedgerEvent>,
    pub imported_events: Option<&'a [LedgerEvent]>,
    pub entry_source: Option<&'a str>,
    pub import_high: i64,
    pub applied_cursor: Option<(&'a str, i64)>,
    pub goal: Option<&'a GoalChange>,
    pub artifacts: &'a [Artifact],
    pub media: &'a [Asset],
}

/// Changes of the books on their way into the log: the items to append, and the records
/// they are, for the books to take once the items are written.
struct Written {
    items: Vec<NewItem>,
    goal: Option<GoalRecord>,
    artifacts: Vec<ArtifactRecord>,
    media: Vec<Asset>,
}

/// The turn that holds the agent, as the actor follows it.
struct LiveTurn {
    opening: bool,
    /// Managed primary pinned to this admission, including after recovery.
    managed_model: Option<String>,
    turn: TurnId,
    /// The message this turn answers, and the id its app gave it.
    message_id: String,
    client_id: String,
    trace_id: Option<String>,
    /// Who asked for it: what every row of the turn is filed under.
    origin: Origin,
    /// The last row this turn put into the conversation: the message itself until the turn
    /// says something. It is the row that is told how the turn ended.
    last_row: String,
    /// The replies of this turn that said something and that no tool work has followed yet,
    /// oldest first. Tool work that comes makes them commentary.
    said: Vec<String>,
    /// What the turn's replies mark for the ear is said aloud.
    speak: bool,
    /// What the tool has said of itself so far: its model, what the turn cost, and how
    /// full its conversation was at its newest answer.
    model: Option<String>,
    tokens: Option<(u64, u64)>,
    /// What the tool said its whole conversation has taken so far, where it says that and not
    /// the turn's share; and what the conversation had taken before the turn, where that is
    /// known: nothing for a conversation that begins with the turn.
    total: Option<(u64, u64)>,
    base: Option<(u64, u64)>,
    context: Option<u64>,
    /// What the tool last said of its account's windows, and when.
    limits: Option<(i64, serde_json::Value)>,
    /// What the tool said its conversation has cost so far, and what it had cost before the
    /// turn, where that is known: nothing for a conversation that begins with the turn.
    per_turn_cost: Option<u64>,
    cost: Option<u64>,
    cost_before: Option<u64>,
    interpreter: Box<dyn Interpreter>,
    /// How far the turn's spool has been read. Never behind the position in the log.
    offset: u64,
    /// The replies being written, oldest first.
    drafts: Vec<Draft>,
    /// The tool calls that run, oldest first.
    tools: Vec<LiveTool>,
    /// The same calls as they were told: what is written once more when the turn is
    /// interrupted and they never return.
    calls: Vec<ToolCall>,
    /// The spool has said how the tool went. Nothing more will come.
    over: bool,
    /// Which attempt of the turn this is, from 1: whose spool is read.
    attempt: u32,
    /// The attempt was stopped because the account its tool runs on was out
    /// ([`Actor::stop_for_an_account`]): however it ends short of completing, the turn
    /// waits for an account.
    for_account: bool,
}

impl LiveTurn {
    fn facts(&self) -> Facts {
        Facts {
            trace_id: self.trace_id.clone(),
            model: self.model.clone(),
            tokens: self.tokens,
            total: self.total,
            base: self.base,
            context: self.context,
            limits: self.limits.clone(),
            per_turn_cost: self.per_turn_cost,
            cost: self.cost,
            cost_before: self.cost_before,
        }
    }
}

/// The accounts of the agent's tool, on a Host that runs the owner's selector itself
/// (`recovery::Accounts::Select`). Every agent of the tool shares them: what one turn's
/// failure means for the others is the pool's to say (`clarp_kernel::accounts`), and what
/// the pool wants done with this agent's turn comes back to the agent from outside
/// ([`Actor::stop_for_an_account`], [`Actor::login_changed`]).
pub trait Pooled: Send + Sync {
    /// An attempt of the agent's turn failed on its account. Whether the turn waits for
    /// another account: where it does not, it ends on that failure. `plan`: the provider
    /// said that the account does not offer the turn's model.
    fn failed(&self, turn: TurnId, plan: bool, now_ms: i64) -> bool;
    /// Nothing of the agent's turn runs any more, as the pool had asked.
    fn stopped(&self, now_ms: i64);
}

/// How much longer than the pool lets a turn wait the turn's own wait lasts: the pool takes
/// a turn up or lets it go, and the wait's own end is only what is left when it does not.
const POOL_MARGIN_MS: i64 = 300_000;

/// What is written down as the failure of an attempt that was stopped because the account
/// its tool runs on was out for another turn.
const STOPPED_FOR_AN_ACCOUNT: &str =
    "usage limit reached: the account was out, and the turn was stopped to wait for another";

pub struct Actor {
    /// Recovery can settle a turn while the worker still has opening reads to do.
    opening: bool,
    profile: Profile,
    backend: Arc<dyn Backend>,
    recorder: Recorder,
    live: Option<LiveTurn>,
    /// The pool of the tool's accounts, where this Host has one.
    pool: Option<Arc<dyn Pooled>>,
    /// The pool is to be told that nothing of the agent's turn runs any more, once what
    /// says so is in the log.
    stopped_for_pool: bool,
    /// The launch of the turn's attempt is held until an account serves it: those who watch
    /// are shown that the turn waits for one.
    held_for_an_account: bool,
    /// The turn waits for an account that serves its model, while the rest of the pool works.
    for_its_model: bool,
    /// The agent's background jobs, as the log has them and as they were last heard from.
    jobs: JobBook,
    provider_sessions: std::collections::BTreeMap<String, clarp_kernel::provider_tasks::Session>,
    /// The agent's goals and plans, as the log has them.
    goals: GoalBook,
    delegation: Option<delegation::Binding>,
    /// What the agent made for its user and what it asks its user, as the log has it.
    artifacts: ArtifactBook,
    /// The files the agent published, as the log has them.
    media: MediaBook,
    /// The lines the agent says aloud, as the log has them.
    speech: SpeechBook,
    /// How often something that is shown but not stored has changed since the last write to
    /// the log. It makes the revisions between two log items.
    unstored_changes: u64,
    /// The newest revision given to something that is shown and not stored.
    shown_revision: u64,
    /// What the turns before left for the agent's row: how full the tool's conversation was
    /// when a turn last said so, when a turn last ran to its end, and which model answered
    /// last ([`Status`]).
    remembered: Remembered,
    /// What those who watch the agent are shown of its turn while it runs: items that
    /// appear, grow and settle (`docs/live-items.md`). Nothing of it is kept; after a restart
    /// it is built again from the turn's own records.
    shown: Hub,
    /// The turn that is shown as running, and the name it is shown under: its trace.
    shown_turn: Option<(TurnId, String)>,
    /// The events of it that nobody has taken yet.
    shown_events: Vec<serde_json::Value>,
}

/// What a turn that waits for an account says of itself to those who watch.
const ACCOUNT_WAIT: &str = "Waiting for an account with available usage";

/// The name a turn is shown under: the trace its message was sent under, or one made of the
/// message's own name where it has none.
fn shown_name(trace_id: Option<&str>, message_id: &str) -> String {
    match trace_id {
        Some(trace) if !trace.is_empty() => trace.to_string(),
        _ => format!("turn-{}", turn_name(message_id)),
    }
}

/// What an agent's row goes on showing whatever became of the turns after.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
struct Remembered {
    context_tokens: Option<u64>,
    completed_at_ms: Option<i64>,
    answered_by: Option<String>,
}

impl Remembered {
    /// Takes what a turn's end says, where it says something. How full the conversation
    /// is: what the tool said of that at its last answer. A tool that says no such thing
    /// has no gauge, as on the Python Host: Codex keeps its conversation within bounds by
    /// itself, and what its turn took in says nothing of how full it is.
    fn take(&mut self, end: &TurnEnd) {
        if let Some(tokens) = end.context_tokens {
            self.context_tokens = Some(tokens);
        }
        if end.status == "completed" {
            self.completed_at_ms = Some(end.ended_at_ms);
            if let Some(model) = &end.model {
                self.answered_by = Some(model.clone());
            }
        }
    }

    /// Whether an older turn's end could still add something.
    fn is_whole(&self) -> bool {
        self.context_tokens.is_some()
            && self.completed_at_ms.is_some()
            && self.answered_by.is_some()
    }
}

/// How many turns back the log is asked what the agent's row keeps, when the agent is
/// opened. What no turn has said for so many turns in a row, the next turn says.
const CONTEXT_LOOKBACK: usize = 20;

/// Hands over every record of one kind that the log has, oldest first. Items of the same
/// range that a newer build wrote in a kind of its own are passed over.
fn records<R: serde::de::DeserializeOwned>(
    log: &dyn AgentLog,
    kinds: Kinds,
    kind: u16,
    mut take: impl FnMut(R),
) -> Result<(), ActorError> {
    let mut after = 0;
    loop {
        let page = log.read_after(after, kinds, WINDOW)?;
        let Some(last) = page.last() else {
            return Ok(());
        };
        after = last.seq;
        for item in page.iter().filter(|item| item.kind == kind) {
            let record = serde_json::from_slice(&item.body).map_err(|error| {
                ActorError::Corrupt(format!(
                    "log item {} is not the record its kind {kind} says: {error}",
                    item.seq
                ))
            })?;
            take(record);
        }
    }
}

/// A record of one of the agent's books, as an item of its log.
fn record_item<R: serde::Serialize>(
    kind: u16,
    key: String,
    record: &R,
    now_ms: i64,
) -> Result<NewItem, ActorError> {
    let body = serde_json::to_vec(record).map_err(|error| {
        ActorError::Corrupt(format!(
            "a record of kind {kind} could not be written: {error}"
        ))
    })?;
    Ok(NewItem::new(kind, now_ms, body).keyed(key))
}

fn io_error(what: &Path, error: std::io::Error) -> ActorError {
    ActorError::Io(format!("{}: {error}", what.display()))
}

/// Writes a file so that it is there whole or not at all.
fn write_whole(path: &Path, bytes: &[u8]) -> Result<(), ActorError> {
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir).map_err(|error| io_error(dir, error))?;
    }
    let partial = path.with_extension("partial");
    let mut file = std::fs::File::create(&partial).map_err(|error| io_error(&partial, error))?;
    file.write_all(bytes)
        .and_then(|()| file.sync_all())
        .map_err(|error| io_error(&partial, error))?;
    std::fs::rename(&partial, path).map_err(|error| io_error(path, error))
}

/// What the Host says in a conversation when a turn did not finish, so that whoever reads it
/// knows why there is no answer. A turn that was stopped on purpose needs no telling.
fn ending_notice(
    turn: TurnId,
    name: &str,
    code: &str,
    reason: &str,
    trace_id: Option<String>,
) -> Entry {
    Entry::Notice(Notice {
        id: format!("n-{name}-{code}"),
        turn: Some(turn.0),
        code: code.to_string(),
        text: reason.to_string(),
        trace_id,
    })
}

/// What a turn that failed leaves in the conversation.
struct Failure<'a> {
    turn: TurnId,
    /// The message the turn answered: the row goes under it, and is named after it.
    message_id: &'a str,
    origin: &'a Origin,
    /// What kind of failure it was (`failures`), and the tool's words for it.
    kind: &'a str,
    reason: &'a str,
    model: &'a str,
    trace_id: Option<String>,
}

/// The row that says, under the message that caused it, why its turn failed: the sentence for
/// that kind of failure, and below it the provider's own words (`failures::told`). `None` for a
/// turn nobody was waiting for, and for one that was ended on purpose: the agent's state says
/// it, the conversation does not.
///
/// Whether the row closes the turn is the Python Host's rule, as both recordings of run
/// 43741530173 show it: a turn that put something into the conversation is closed by this
/// row, below what it said; a turn that said nothing is closed by its message, and this row
/// stands below the turn's end ([`closed_by_marker`]).
fn failure_marker(failed: Failure<'_>) -> Option<Entry> {
    if failed.kind == failures::INTERRUPTED || silent_when_failed(failed.origin.name()) {
        return None;
    }
    let told = failures::told(
        failed.kind,
        failed.reason,
        failed.model,
        failures::Known::default(),
    );
    Some(Entry::Notice(Notice {
        id: format!("marker-{}", failed.message_id),
        turn: Some(failed.turn.0),
        code: Notice::TURN_FAILED.to_string(),
        text: told.row,
        trace_id: failed.trace_id,
    }))
}

/// Whether the row that says why a turn failed closes the turn: it does when the turn put
/// something into the conversation before it failed, and `last_row` is no longer its message.
fn closed_by_marker(last_row: &str, message_id: &str) -> bool {
    last_row != message_id
}

/// The name of one attempt of a turn: the turn's own for its first, and from the second on
/// the attempt's number with it, so that what two attempts write is never under one name.
fn attempt_of(name: &str, attempt: u32) -> String {
    if attempt <= 1 {
        name.to_string()
    } else {
        format!("{name}.{attempt}")
    }
}

/// A name for a turn that no other turn has, on this Host or on one the agent may move to: the
/// id of the message it answers, without the mark that says it is a message. A turn's number
/// is not such a name, because an agent that moves starts counting again.
fn turn_name(message_id: &str) -> &str {
    message_id.strip_prefix("u-").unwrap_or(message_id)
}

/// The log item for a piece of a conversation.
fn written(piece: &Entry, ts_ms: i64) -> Result<NewItem, ActorError> {
    piece.to_item(ts_ms).map_err(|error| {
        ActorError::Corrupt(format!("a piece of a turn could not be written: {error}"))
    })
}

/// What is known of a turn besides how it went: told with its end.
#[derive(Debug, Clone, Default)]
struct Facts {
    trace_id: Option<String>,
    model: Option<String>,
    tokens: Option<(u64, u64)>,
    total: Option<(u64, u64)>,
    base: Option<(u64, u64)>,
    context: Option<u64>,
    limits: Option<(i64, serde_json::Value)>,
    per_turn_cost: Option<u64>,
    cost: Option<u64>,
    cost_before: Option<u64>,
}

/// How the turn that has just ended in this batch ended, for the row that closed it.
fn closing(batch: &Batch<'_>, turn: TurnId, closes: &str, facts: Facts) -> Option<Entry> {
    let ended = batch.agent().last().filter(|last| last.id == turn)?;
    let mut end = TurnEnd::of(ended, closes)?;
    end.trace_id = facts.trace_id;
    end.model = facts.model;
    end.tokens_in = facts.tokens.map(|(tokens_in, _)| tokens_in);
    end.tokens_out = facts.tokens.map(|(_, tokens_out)| tokens_out);
    // A tool that told its conversation's running total: the turn's share is what the total
    // grew by. Where what it stood at before is not known, the share is not either, and the
    // total is kept for the turn after.
    if let Some((tokens_in, tokens_out)) = facts.total {
        let share = clarp_kernel::usage::share(facts.base, (tokens_in, tokens_out));
        end.tokens_in = share.map(|(took_in, _)| took_in);
        end.tokens_out = share.map(|(_, took_out)| took_out);
        end.total = batch.agent().backend_session().map(|session| Total {
            session: session.to_string(),
            tokens_in,
            tokens_out,
        });
    }
    end.context_tokens = facts.context;
    end.limits = facts.limits.map(|(at_ms, said)| LimitsSaid { at_ms, said });
    // What the tool tells of cost is what its whole conversation has cost so far: the turn's
    // share is what that grew by. Where what it stood at before is not known (a session that
    // was taken up, or one from before totals were kept), or the total fell, the share is not
    // known either, and the total is kept for the turn after.
    end.cost_micros = facts.per_turn_cost;
    if let Some(so_far) = facts.cost {
        end.cost_micros = clarp_kernel::usage::rise(facts.cost_before, so_far);
        end.cost_so_far = batch.agent().backend_session().map(|session| CostSoFar {
            session: session.to_string(),
            micros: so_far,
        });
    }
    Some(Entry::TurnEnd(end))
}

/// The replies at the end of what a turn has put into the conversation that said something
/// and that no tool work has followed, oldest first, as the log has them. An attempt before
/// the one that runs may have said them, and an actor before this one.
fn unfollowed(log: &dyn AgentLog, turn: TurnId) -> Result<Vec<String>, ActorError> {
    const AT_A_TIME: usize = 32;
    let corrupt = |error: serde_json::Error| {
        ActorError::Corrupt(format!("a row of turn {} cannot be read: {error}", turn.0))
    };
    let mut said = Vec::new();
    let mut before = Seq::MAX;
    'reading: loop {
        let chunk = log.read_before(before, kind::ROWS, AT_A_TIME)?;
        let Some(oldest) = chunk.first() else {
            break;
        };
        before = oldest.seq;
        for item in chunk.iter().rev() {
            match Entry::from_item(item).map_err(corrupt)? {
                Some(Entry::Reply(reply)) if reply.turn == turn.0 => {
                    let worked = !reply.tools.is_empty();
                    if !reply.text.trim().is_empty() {
                        said.push(reply.id);
                    }
                    if worked {
                        // What came before this was told when this was written.
                        break 'reading;
                    }
                }
                // A word of the Host about the turn is neither said by the agent nor work.
                Some(Entry::Notice(notice)) if notice.turn == Some(turn.0) => {}
                // A call that is a row, the turn's own message, or what was there before it.
                _ => break 'reading,
            }
        }
        if chunk.len() < AT_A_TIME {
            break;
        }
    }
    said.reverse();
    Ok(said)
}

/// Writes that tool work followed these replies: what they said was said along the way.
fn follow(
    batch: &mut Batch<'_>,
    said: &mut Vec<String>,
    turn: TurnId,
    ts_ms: i64,
) -> Result<(), ActorError> {
    for reply in said.drain(..) {
        let part = Entry::Commentary(Commentary::of(&reply, turn.0));
        batch.item(written(&part, ts_ms)?);
    }
    Ok(())
}

/// A turn that was interrupted cut its tools off: a use that had not returned never will.
/// Each is written once more, as interrupted, so that the row it is shown on says so and
/// whoever follows the conversation is given that row again. A turn that ended any other
/// way leaves its tools' uses as they are.
fn cut_off(
    batch: &mut Batch<'_>,
    calls: &mut Vec<ToolCall>,
    end: &Entry,
    ts_ms: i64,
) -> Result<(), ActorError> {
    if !matches!(end, Entry::TurnEnd(end) if end.status == "interrupted") {
        return Ok(());
    }
    for mut call in calls.drain(..) {
        call.status = ToolStatus::Interrupted;
        call.ended_at_ms = Some(ts_ms);
        batch.item(written(&Entry::Tool(call), ts_ms)?);
    }
    Ok(())
}

/// What the conversation keeps of what an agent said in a turn that began this way. An
/// answer to a heartbeat that says no more than that nothing is to be done is kept as one
/// line that says so, and so is that word alone in any other turn
/// ([`clarp_kernel::heartbeat::kept`]).
fn kept(text: String, origin: &Origin) -> String {
    if !text.contains(clarp_kernel::heartbeat::OK) {
        return text;
    }
    clarp_kernel::heartbeat::kept(&text, origin.name() == "heartbeat").0
}

fn show(drafts: &mut Vec<Draft>, turn: TurnId, origin: &Origin, live: LiveReply, ts_ms: i64) {
    let text = kept(live.text, origin);
    // An answer that is no answer is not shown while it is written.
    if text == clarp_kernel::heartbeat::NOTHING_TO_DO {
        drafts.retain(|draft| draft.id != live.id);
        return;
    }
    match drafts.iter_mut().find(|draft| draft.id == live.id) {
        Some(draft) => draft.text = text,
        None => drafts.push(Draft {
            id: live.id,
            turn: turn.0,
            text,
            revision: 0,
            ts_ms,
            origin: origin.clone(),
        }),
    }
}

impl Actor {
    /// Opens an agent from its log. A turn that was live is rejoined, or started if no shim ever
    /// took it; the actions say which.
    pub fn open(
        profile: Profile,
        backend: Arc<dyn Backend>,
        log: Box<dyn AgentLog>,
        now_ms: i64,
    ) -> Result<(Self, Outputs), ActorError> {
        let (mut actor, out) = Self::open_for_recovery(profile, backend, log, now_ms)?;
        actor.finish_opening();
        Ok((actor, out))
    }

    /// Opens the actor for a Host that also folds projections from its log. Call
    /// `finish_opening` after those reads so they do not immediately refill the cache.
    pub fn open_for_recovery(
        profile: Profile,
        backend: Arc<dyn Backend>,
        log: Box<dyn AgentLog>,
        now_ms: i64,
    ) -> Result<(Self, Outputs), ActorError> {
        let recorder = Recorder::open(log)?;
        let mut jobs = JobBook::new();
        records(
            recorder.log(),
            kind::JOBS,
            kind::JOB,
            |record: JobRecord| {
                jobs.apply(&record);
            },
        )?;
        let mut provider_sessions = std::collections::BTreeMap::new();
        records(
            recorder.log(),
            kind::JOBS,
            kind::PROVIDER_JOBS,
            |entry: clarp_kernel::provider_tasks::JournalEntry| {
                for record in entry.records {
                    jobs.apply(&record);
                }
                provider_sessions.insert(entry.native, entry.session);
            },
        )?;
        // What the Host did not hear while it was away says nothing about the workers.
        jobs.heard_at(now_ms);
        let mut goals = GoalBook::new();
        records(
            recorder.log(),
            kind::GOALS,
            kind::GOAL,
            |record: GoalRecord| {
                goals.apply(&record);
            },
        )?;
        let mut artifacts = ArtifactBook::new();
        records(
            recorder.log(),
            kind::ARTIFACTS,
            kind::ARTIFACT,
            |record: ArtifactRecord| {
                artifacts.apply(&record);
            },
        )?;
        let mut media = MediaBook::new();
        records(
            recorder.log(),
            kind::PUBLISHED,
            kind::MEDIA,
            |asset: Asset| {
                media.apply(&asset);
            },
        )?;
        let mut speech = SpeechBook::new();
        records(recorder.log(), kind::SPEECH, kind::LINE, |line: Spoken| {
            speech.apply(&line);
        })?;
        // What was over long ago is in the log and nowhere else.
        speech.forget_before(now_ms - KEPT_FOR_MS);
        let shown = Hub::new(&profile.epoch, &profile.agent_id, &profile.session);
        let delegation = delegation::active(recorder.log())?;
        let mut actor = Self {
            opening: true,
            delegation,
            pool: None,
            stopped_for_pool: false,
            held_for_an_account: false,
            for_its_model: false,
            shown,
            shown_turn: None,
            shown_events: Vec::new(),
            profile,
            backend,
            recorder,
            jobs,
            provider_sessions,
            goals,
            artifacts,
            media,
            speech,
            live: None,
            unstored_changes: 0,
            shown_revision: 0,
            remembered: Remembered::default(),
        };
        actor.open_goal_ledger(now_ms)?;
        actor.remembered = actor.remembered_in_log()?;
        // A line an engine was speaking when the Host went: begun a moment ago, it waits
        // again; begun long ago, it is over. And one that waited for a Host that was away
        // too long is not spoken any more.
        let cut_off = actor.speech.after_a_restart(now_ms);
        actor.write_lines(&cut_off, now_ms)?;
        let mut out = Outputs::default();
        out.changed.speech = actor.speech.next().is_some();
        let live = match actor.recorder.agent().live() {
            Some(Turn::Starting(turn)) => Some((turn.id, turn.message_seq, turn.attempt, true)),
            Some(Turn::Running(turn)) => Some((turn.id, turn.message_seq, turn.attempt, false)),
            _ => None,
        };
        if let Some((turn, message_seq, attempt, starting)) = live {
            actor.rejoin(turn, message_seq, attempt, starting, &mut out, now_ms)?;
        }
        // A turn that waited for its next attempt still waits: no tool ran for it meanwhile.
        if let Some(Turn::Parked(parked)) = actor.recorder.agent().live() {
            let (turn, message_seq, attempt) = (parked.id, parked.message_seq, parked.attempts);
            let message = actor.message_at(message_seq)?;
            actor.live = Some(LiveTurn {
                opening: opening_message(&message),
                managed_model: message.managed_model.clone(),
                turn,
                last_row: message.id.clone(),
                // The attempt that comes reads from the log what the turn has said.
                said: Vec::new(),
                speak: message.speak,
                interpreter: actor
                    .backend
                    .interpreter(turn.0, &attempt_of(turn_name(&message.id), attempt)),
                client_id: message.told_id().to_string(),
                message_id: message.id,
                trace_id: message.trace_id,
                origin: message.origin,
                model: None,
                tokens: None,
                total: None,
                base: actor.total_before(turn)?,
                context: None,
                limits: None,
                per_turn_cost: None,
                cost: None,
                cost_before: actor.cost_before(turn)?,
                offset: 0,
                drafts: Vec::new(),
                tools: Vec::new(),
                calls: Vec::new(),
                over: true,
                attempt,
                for_account: false,
            });
        }
        Ok((actor, out))
    }

    /// Ends recovery, including the Host's initial reads, exactly once.
    pub fn finish_opening(&mut self) {
        if std::mem::take(&mut self.opening) {
            self.rest_if_idle();
        }
    }

    fn rest_if_idle(&self) {
        if !self.opening && self.recorder.agent().live().is_none() {
            self.recorder.log().at_rest();
        }
    }

    /// What those who watch the agent are to be sent now: the events of its turn that nobody
    /// has taken yet, each a `live` event as `docs/live-items.md` has it.
    pub fn take_shown(&mut self, now_ms: i64) -> Vec<serde_json::Value> {
        self.show_turn(now_ms);
        std::mem::take(&mut self.shown_events)
    }

    /// When growth that is held back for a moment is to be sent at the latest: say
    /// [`Actor::shown_due`] then.
    pub fn shown_due_ms(&self) -> Option<i64> {
        self.shown.due_ms()
    }

    /// The moment [`Actor::shown_due_ms`] named has come.
    pub fn shown_due(&mut self, now_ms: i64) {
        let events = self.shown.fire(now_ms);
        self.shown_events.extend(events);
    }

    /// Puts fields on an item of what is shown: the explanation of a tool's use on its row.
    /// An item that is shown no longer is left alone.
    pub fn shown_patch(&mut self, item: &str, fields: &serde_json::Value, now_ms: i64) {
        let events = self.shown.patch(item, fields, None, now_ms);
        self.shown_events.extend(events);
    }

    /// Everything that is shown of the agent's turn, as `GET /live` answers it. An agent
    /// that has run no turn since the Host started shows an idle, empty view.
    pub fn shown_whole(&mut self, now_ms: i64) -> serde_json::Value {
        self.show_turn(now_ms);
        match self.shown.snapshot(now_ms) {
            Some((snapshot, events)) => {
                self.shown_events.extend(events);
                snapshot
            }
            None => {
                // An agent that was never written to has no conversation yet, here as in
                // its conversation's own answer.
                let written =
                    self.head_revision() > 0 || self.recorder.agent().backend_session().is_some();
                let conversation = match written {
                    true => self.profile.conversation_id.as_str(),
                    false => "",
                };
                live::empty_snapshot(
                    (conversation, &self.profile.session),
                    (&self.profile.agent_id, &self.profile.epoch),
                    now_ms,
                )
            }
        }
    }

    /// The status line of the agent's turn. `None` for an agent that has run no turn since
    /// the Host started.
    pub fn shown_activity(&self) -> Option<&serde_json::Value> {
        self.shown.activity()
    }

    /// The conversation a turn is shown as part of.
    fn shown_conversation(&self) -> String {
        if self.profile.conversation_id.is_empty() {
            format!("pending:{}", self.profile.agent_id)
        } else {
            self.profile.conversation_id.clone()
        }
    }

    /// How a turn that no longer holds the agent ended, in the words of those who watch:
    /// `completed`, `failed` with what went wrong, or `interrupted`.
    fn ended_as(&self, turn: TurnId) -> (&'static str, Option<serde_json::Value>) {
        let last = self.recorder.agent().last();
        let Some(settled) = last.filter(|settled| settled.id == turn) else {
            return ("interrupted", None);
        };
        match &settled.outcome {
            Outcome::Completed => ("completed", None),
            Outcome::Failed { reason, kind } => {
                let kind = match kind.as_str() {
                    "" => failures::classify_error(reason),
                    kind => kind,
                };
                let model = self.turn_model().unwrap_or_default();
                let told = failures::told(kind, reason, &model, failures::Known::default());
                let error = serde_json::json!({
                    "reason": kind,
                    "message": clarp_kernel::python::first(&told.message, 300),
                    "detail": clarp_kernel::python::first(&told.provider_message, 500),
                });
                ("failed", Some(error))
            }
            Outcome::Stopped { .. } | Outcome::Interrupted { .. } | Outcome::Withdrawn { .. } => {
                ("interrupted", None)
            }
        }
    }

    /// Brings what is shown of the turn in step with what the log says of it: a turn that
    /// holds the agent is begun for those who watch, one that holds it no longer is ended as
    /// it ended, and a turn that waits for an account says so.
    fn show_turn(&mut self, now_ms: i64) {
        let held = match self.recorder.agent().live() {
            Some(Turn::Starting(turn)) => Some((turn.id, turn.started_at_ms)),
            Some(Turn::Running(turn)) => Some((turn.id, turn.started_at_ms)),
            Some(Turn::Parked(turn)) => Some((turn.id, turn.started_at_ms)),
            _ => None,
        };
        let shown = self.shown_turn.as_ref().map(|(turn, _)| *turn);
        if shown != held.map(|(turn, _)| turn) {
            if let Some(turn) = shown {
                let (status, error) = self.ended_as(turn);
                let events = self.shown.end_turn(status, error.as_ref(), now_ms);
                self.shown_events.extend(events);
                self.shown_turn = None;
            }
            let followed = self.live.as_ref();
            if let Some((turn, started_at_ms)) = held
                && let Some(live) = followed.filter(|live| live.turn == turn)
            {
                let name = shown_name(live.trace_id.as_deref(), &live.message_id);
                let conversation = self.shown_conversation();
                let events = self.shown.begin_turn(
                    &self.profile.session,
                    &conversation,
                    &name,
                    Some(started_at_ms),
                    now_ms,
                );
                self.shown_events.extend(events);
                self.shown_turn = Some((turn, name));
            }
        }
        if self.shown_turn.is_some() {
            let waits = self.held_for_an_account
                || matches!(
                    self.recorder.agent().live(),
                    Some(Turn::Parked(parked)) if matches!(parked.wait, Wait::Account { .. })
                );
            let events = self
                .shown
                .account_recovery(waits.then_some(ACCOUNT_WAIT), now_ms);
            self.shown_events.extend(events);
        }
    }

    pub fn profile(&self) -> &Profile {
        &self.profile
    }

    pub fn turn_model(&self) -> Option<String> {
        self.live
            .as_ref()
            .and_then(|live| live.managed_model.clone())
            .or_else(|| self.profile.model.clone())
    }

    /// Sets which model the agent's next turn runs with, and how much effort. A turn that
    /// runs keeps what it was started with. `None` is the tool's own choice.
    pub fn retune(&mut self, model: Option<String>, effort: Option<String>) {
        self.profile.model = model;
        self.profile.effort = effort;
    }

    /// Sets what the agent's tool is told from its next turn on: who the agent is now, and
    /// how its spoken answers sound. A turn that runs was told when it started.
    pub fn retell(&mut self, told: Option<Told>) {
        self.profile.told = told;
    }

    /// The name of the backend that runs this agent's turns.
    pub fn backend_id(&self) -> &'static str {
        self.backend.id()
    }

    /// The newest revision of the conversation: that of its newest change, stored or shown.
    /// What the log holds besides the conversation does not move it.
    pub fn head_revision(&self) -> u64 {
        (self.recorder.conversation_seq() * REVISION_STEP).max(self.shown_revision)
    }

    /// Where the conversation begins, as it is read, and how far it has got.
    fn span(&self) -> conversation::Span {
        conversation::Span {
            floor: self.recorder.floor(),
            head_revision: self.head_revision(),
        }
    }

    /// The revision the conversation, as it is read, begins after: 0 for an agent that was
    /// never started again. A reader that holds an older revision holds a conversation
    /// that has ended.
    pub fn begins_after(&self) -> u64 {
        self.recorder.floor() * REVISION_STEP
    }

    pub fn latest_turn_start(&self) -> i64 {
        self.recorder.agent().latest_turn_start()
    }

    pub fn status(&self) -> Status {
        let agent = self.recorder.agent();
        let stopping = agent.stopping().is_some();
        let (message_id, client_id, trace_id, tools) = match &self.live {
            Some(live) => (
                live.message_id.clone(),
                live.client_id.clone(),
                live.trace_id.clone(),
                live.tools.clone(),
            ),
            None => (String::new(), String::new(), None, Vec::new()),
        };
        let from_queue = agent.live_from_queue();
        let live = match agent.live() {
            Some(Turn::Starting(turn)) => Some(LiveStatus {
                turn: turn.id,
                message_id,
                client_id,
                trace_id,
                origin: turn.origin.clone(),
                from_queue,
                starting: true,
                activity: Activity::Thinking,
                started_at_ms: turn.started_at_ms,
                tool_count: turn.tool_count,
                tools,
                stopping,
                parked: None,
                held_for_an_account: self.held_for_an_account,
                waits_for_its_model: self.model_waited_for(),
                failed_attempts: turn.attempt.saturating_sub(1),
            }),
            Some(Turn::Running(turn)) => Some(LiveStatus {
                turn: turn.id,
                message_id,
                client_id,
                trace_id,
                origin: turn.origin.clone(),
                from_queue,
                starting: false,
                activity: turn.activity.clone(),
                started_at_ms: turn.started_at_ms,
                tool_count: turn.tool_count,
                tools,
                stopping,
                parked: None,
                held_for_an_account: false,
                waits_for_its_model: None,
                failed_attempts: turn.attempt.saturating_sub(1),
            }),
            // Between two attempts no tool works: the turn is as busy as one that thinks.
            Some(Turn::Parked(turn)) => Some(LiveStatus {
                turn: turn.id,
                message_id,
                client_id,
                trace_id,
                origin: turn.origin.clone(),
                from_queue,
                starting: false,
                activity: Activity::Thinking,
                started_at_ms: turn.started_at_ms,
                tool_count: turn.tool_count,
                tools: Vec::new(),
                stopping,
                parked: Some(turn.clone()),
                held_for_an_account: false,
                waits_for_its_model: self.model_waited_for(),
                failed_attempts: turn.attempts,
            }),
            _ => None,
        };
        Status {
            live,
            last: agent.last().cloned(),
            waiting: agent.waiting().map(|queued| queued.id).collect(),
            paused: agent.is_paused(),
            queue_revision: agent.queue_revision(),
            stop_generation: agent.stop_generation(),
            head_revision: self.head_revision(),
            backend_session: agent.backend_session().map(str::to_string),
            label: agent.label().to_string(),
            label_at_ms: agent.label_at(),
            resting: agent.resting(),
            context_tokens: self.remembered.context_tokens,
            completed_at_ms: self.remembered.completed_at_ms,
            answered_by: self.remembered.answered_by.clone(),
            arrived: agent.arrived(),
            note: agent.note().cloned(),
        }
    }

    /// What the last turns' ends in the log say for the agent's row: of each thing, the
    /// newest word.
    fn remembered_in_log(&self) -> Result<Remembered, ActorError> {
        let mut kept = Remembered::default();
        let Some(last) = self.recorder.agent().last() else {
            return Ok(kept);
        };
        // From the newest end back: an older one only fills what the newer ones left open.
        let floor = self.recorder.floor();
        for turn in (1..=last.id.0).rev().take(CONTEXT_LOOKBACK) {
            let stored = self.recorder.log().find(&format!("t-{turn}"))?;
            // A turn that ended before the agent was started again says when a turn last
            // ran to its end, and nothing of the conversation there is now.
            let earlier = stored.is_some_and(|stored| stored.seq <= floor);
            if let Some(end) = self.turn_end(turn)? {
                let mut older = Remembered::default();
                older.take(&end);
                kept.completed_at_ms = kept.completed_at_ms.or(older.completed_at_ms);
                if !earlier {
                    kept.context_tokens = kept.context_tokens.or(older.context_tokens);
                    kept.answered_by = kept.answered_by.or(older.answered_by);
                }
            }
            if kept.is_whole() || (earlier && kept.completed_at_ms.is_some()) {
                break;
            }
        }
        Ok(kept)
    }

    /// What the tool's conversation had taken before a turn, where the tool tells running
    /// totals: nothing where the conversation begins with the turn (the agent has no session
    /// of its tool yet), what was told at the end of the newest turn before it that told a
    /// total, where that was the same conversation, and otherwise not known: a session that
    /// was taken up, or one from before totals were kept. No further back than twenty turns.
    fn total_before(&self, turn: TurnId) -> Result<Option<(u64, u64)>, ActorError> {
        let Some(session) = self.recorder.agent().backend_session() else {
            return Ok(Some((0, 0)));
        };
        for earlier in (turn.0.saturating_sub(20).max(1)..turn.0).rev() {
            if let Some(total) = self.turn_end(earlier)?.and_then(|end| end.total) {
                let same = total.session == session;
                return Ok(same.then_some((total.tokens_in, total.tokens_out)));
            }
        }
        Ok(None)
    }

    /// What the tool's conversation had cost before a turn, where the tool tells what it has
    /// cost so far: nothing where the conversation begins with the turn (the agent has no
    /// session of its tool yet), what was told at the end of the newest turn before it that
    /// told it, where that was the same conversation, and otherwise not known. No further
    /// back than twenty turns.
    fn cost_before(&self, turn: TurnId) -> Result<Option<u64>, ActorError> {
        let Some(session) = self.recorder.agent().backend_session() else {
            return Ok(Some(0));
        };
        for earlier in (turn.0.saturating_sub(20).max(1)..turn.0).rev() {
            if let Some(told) = self.turn_end(earlier)?.and_then(|end| end.cost_so_far) {
                return Ok((told.session == session).then_some(told.micros));
            }
        }
        Ok(None)
    }

    /// How the last turn that held the agent ended, with what its tool said of itself.
    pub fn last_end(&self) -> Result<Option<TurnEnd>, ActorError> {
        match self.recorder.agent().last() {
            Some(last) => self.turn_end(last.id.0),
            None => Ok(None),
        }
    }

    /// How the turn that answered the message with this id ended, once it has.
    pub fn end_of(&self, message_id: &str) -> Result<Option<TurnEnd>, ActorError> {
        let message = self.recorder.log().find(message_id)?;
        match message.and_then(|message| message.turn) {
            Some(turn) => self.turn_end(turn),
            None => Ok(None),
        }
    }

    pub fn opening_row(&self, id: &str) -> Result<serde_json::Value, ActorError> {
        use serde_json::{Value, json};
        let (filed, _) = Message::filed(id);
        let Some(item) = self.recorder.log().find(&filed)? else {
            return Ok(Value::Null);
        };
        let Some(turn) = item.turn else {
            return Ok(Value::Null);
        };
        let raw: Value =
            serde_json::from_slice(&item.body).map_err(|e| ActorError::Corrupt(e.to_string()))?;
        let message = self.message_at(item.seq)?;
        let foreign = !message.unminted
            || !matches!(&message.origin,Origin::Trigger{name} if name=="automation");
        let status = match self.fate_of(&filed)? {
            TurnFate::Unknown => return Ok(Value::Null),
            TurnFate::Waits => "queued",
            // The Host can have selected the turn while its launch gate is
            // still waiting. Only an opening marked started owns that launch.
            TurnFate::Runs => "claimed",
            TurnFate::Ended { .. } => "ended",
            TurnFate::Gone => "withdrawn",
        };
        Ok(
            json!({"reservation":raw["_call_opening"],"status":status,"trace_id":message.trace_id,"foreign":foreign,"turn":turn,"claimed_at":Value::Null}),
        )
    }

    /// What became of the turn that answers the message with this id: for whoever handed
    /// the agent that message and follows what comes of it.
    pub fn fate_of(&self, message_id: &str) -> Result<TurnFate, ActorError> {
        let message = self.recorder.log().find(message_id)?;
        let Some(turn) = message.and_then(|message| message.turn) else {
            return Ok(TurnFate::Unknown);
        };
        if let Some(end) = self.turn_end(turn)? {
            // What it left: the answer, in the row that closed it; of a turn that did not
            // finish, the row under its message that says why, where there is one.
            let left = if end.status == "completed" {
                end.closes.clone()
            } else {
                format!("marker-{message_id}")
            };
            let left = self.recorder.log().find(&left)?;
            let text = match left.as_ref().map(Entry::from_item) {
                Some(Ok(Some(Entry::Reply(reply)))) => reply.text,
                Some(Ok(Some(Entry::Notice(notice)))) => notice.text,
                _ => String::new(),
            };
            return Ok(TurnFate::Ended {
                status: end.status,
                closes: end.closes,
                text,
                ended_at_ms: end.ended_at_ms,
            });
        }
        let agent = self.recorder.agent();
        if agent.live().is_some_and(|live| live.id().0 == turn) {
            return Ok(TurnFate::Runs);
        }
        let waits = agent.waiting().any(|queued| queued.id.0 == turn);
        Ok(if waits {
            TurnFate::Waits
        } else {
            TurnFate::Gone
        })
    }

    /// Where one of the agent's turns stands, by its number: what tells whether a token given
    /// to that turn still names the agent. Two keyed reads, never a scan: the turn that runs,
    /// and the row that ended this one.
    pub fn standing_of(&self, turn: u64) -> Result<Standing, ActorError> {
        let live = self.recorder.agent().live().map(|live| live.id().0);
        if live == Some(turn) {
            return Ok(Standing::Live);
        }
        Ok(match self.turn_end(turn)? {
            Some(end) => Standing::Ended {
                at_ms: end.ended_at_ms,
            },
            None => Standing::Unknown,
        })
    }

    /// What the agent last finished for one of those `asked_by` names (by the contract's
    /// word for who began a turn), when none of them wrote to it since: the message that
    /// began that turn and the row that closed it, which says how and when it ended.
    ///
    /// It is the turn that answers the newest message of one of them in the conversation,
    /// and only where that turn ran to its end with something said, no earlier than
    /// `since_ms`. A newer message of theirs that waits its turn, is being answered or was
    /// not answered to the end is what they wrote since, and then there is nothing. Nor is
    /// there anything of a conversation that ended when the agent was started again.
    pub fn last_finished_for(
        &self,
        asked_by: impl Fn(&str) -> bool,
        since_ms: i64,
    ) -> Result<Option<(Versioned, Versioned)>, ActorError> {
        // What one of them wrote and the agent has not come to yet, they wrote since.
        let mut waiting = self.recorder.agent().waiting();
        if waiting.any(|queued| asked_by(queued.origin.name())) {
            return Ok(None);
        }
        let log = self.recorder.log();
        // How the turn that answers a message ended, once it has.
        let ended = |item: &Item| match item.turn {
            Some(turn) => self.turn_end(turn),
            None => Ok(None),
        };
        let floor = self.recorder.floor();
        let mut before = Seq::MAX;
        loop {
            let page = log.read_before(before, Kinds::only(kind::MESSAGE), MESSAGES_AT_A_TIME)?;
            let Some(oldest) = page.first() else {
                return Ok(None);
            };
            before = oldest.seq;
            for item in page.iter().rev() {
                if item.seq <= floor {
                    return Ok(None);
                }
                let message: Message = serde_json::from_slice(&item.body).map_err(|error| {
                    ActorError::Corrupt(format!(
                        "the message at {} cannot be read: {error}",
                        item.seq
                    ))
                })?;
                if !asked_by(message.origin.name()) {
                    // A message enters a conversation when its turn starts, after the
                    // turn before it has ended: what stands before one that entered so
                    // long ago ended longer ago still, and need not be read.
                    if item.ts_ms < since_ms {
                        return Ok(None);
                    }
                    continue;
                }
                let Some(end) = ended(item)? else {
                    return Ok(None);
                };
                if end.status != "completed" || end.ended_at_ms < since_ms {
                    return Ok(None);
                }
                let asked = conversation::one(log, &message.id)?;
                let answer = conversation::one(log, &end.closes)?;
                let said = |answer: &Versioned| matches!(answer.entry, Entry::Reply(_));
                return Ok(asked.zip(answer.filter(said)));
            }
        }
    }

    fn turn_end(&self, turn: u64) -> Result<Option<TurnEnd>, ActorError> {
        let Some(stored) = self.recorder.log().find(&format!("t-{turn}"))? else {
            return Ok(None);
        };
        match Entry::from_item(&stored) {
            Ok(Some(Entry::TurnEnd(end))) => Ok(Some(end)),
            Ok(_) => Ok(None),
            Err(error) => Err(ActorError::Corrupt(format!(
                "the end of turn {turn} cannot be read: {error}"
            ))),
        }
    }

    /// Where the queue stands right now.
    pub fn queue_state(&self) -> QueueState {
        let agent = self.recorder.agent();
        QueueState {
            waiting: agent.waiting().count(),
            paused: agent.is_paused(),
            revision: agent.queue_revision(),
        }
    }

    /// The messages that wait, in the order they will run.
    pub fn queue(&self) -> Result<Vec<Waiting>, ActorError> {
        let mut waiting = Vec::new();
        for queued in self.recorder.agent().waiting() {
            let message = self.message_at(queued.message_seq)?;
            waiting.push(Waiting {
                turn: queued.id,
                client_id: message.client_id().to_string(),
                told_id: message.told_id().to_string(),
                message_id: message.id,
                text: message.text,
                speak: message.speak,
                accepted_at_ms: queued.accepted_at_ms,
            });
        }
        Ok(waiting)
    }

    /// The message a turn that ended answered.
    pub fn message_of(&self, turn: &Settled) -> Result<Message, ActorError> {
        self.message_at(turn.message_seq)
    }

    /// The message the log holds at this position.
    fn message_at(&self, message_seq: Seq) -> Result<Message, ActorError> {
        let stored = self
            .recorder
            .log()
            .read_after(message_seq.saturating_sub(1), Kinds::ALL, 1)?
            .into_iter()
            .next()
            .filter(|item| {
                item.seq == message_seq && matches!(item.kind, kind::MESSAGE | kind::WAITING)
            })
            .ok_or_else(|| {
                ActorError::Corrupt(format!(
                    "a turn answers the message at {message_seq}, and the log has none there"
                ))
            })?;
        serde_json::from_slice(&stored.body).map_err(|error| {
            ActorError::Corrupt(format!(
                "the message at {message_seq} cannot be read: {error}"
            ))
        })
    }

    fn drafts(&self) -> &[Draft] {
        self.live
            .as_ref()
            .map_or(&[], |live| live.drafts.as_slice())
    }

    /// The newest `limit` things of the conversation, as a chat opens.
    pub fn tail(&self, limit: usize) -> Result<Page, ActorError> {
        self.tail_of(limit, Which::All)
    }

    /// The newest `limit` things a reader asks for.
    pub fn tail_of(&self, limit: usize, which: Which) -> Result<Page, ActorError> {
        conversation::tail(
            self.recorder.log(),
            self.drafts(),
            self.span(),
            limit,
            which,
        )
    }

    /// The newest `limit` things the agent's log has of any of its conversations, also of
    /// those that ended when it was started again. What a list of chats shows of an agent
    /// is the last thing that was said: an agent that was started again shows it until
    /// something is said in its new conversation.
    pub fn tail_of_any(&self, limit: usize) -> Result<Page, ActorError> {
        let whole = conversation::Span {
            floor: 0,
            head_revision: self.head_revision(),
        };
        conversation::tail(self.recorder.log(), self.drafts(), whole, limit, Which::All)
    }

    /// Up to `limit` things that are newer than `revision`, as a client catches up.
    pub fn after(&self, revision: u64, limit: usize) -> Result<Page, ActorError> {
        self.after_of(revision, limit, Which::All)
    }

    /// The revision the conversation had at a time: what is newer than it is what was
    /// written after that time ([`conversation::revision_at`]).
    pub fn revision_at(&self, ts_ms: i64) -> Result<u64, ActorError> {
        conversation::revision_at(self.recorder.log(), self.span(), ts_ms)
    }

    /// What was said in the conversation before the message of this id, the oldest first,
    /// each with whether the user said it: as much of its end as a record that is carried
    /// into a new conversation can hold ([`told::restored`]), and no more is read.
    fn said_before(&self, own: &str) -> Result<Vec<(bool, String)>, ActorError> {
        const AT_A_TIME: usize = 200;
        let mut said: Vec<(bool, String)> = Vec::new(); // the newest first
        let mut held = 0;
        let mut page = Some(self.tail_of(AT_A_TIME, Which::All)?);
        while let Some(rows) = page.take() {
            for row in rows.entries.iter().rev() {
                let (by_user, text) = match &row.entry {
                    Entry::Message(message) if message.id != own => (true, message.text.clone()),
                    Entry::Reply(reply) => (false, reply.text.clone()),
                    Entry::Imported(brought) => {
                        let text = brought.row["text"].as_str().unwrap_or_default().to_string();
                        match brought.row["role"].as_str() {
                            Some("user") => (true, text),
                            Some("assistant") => (false, text),
                            _ => continue,
                        }
                    }
                    _ => continue,
                };
                held += text.chars().count().min(told::RESTORED_OF_ONE);
                said.push((by_user, text));
            }
            let oldest = rows.entries.first().map(|row| row.entry.id().to_string());
            if held < told::RESTORED_MOST
                && rows.has_more
                && let Some(oldest) = oldest
            {
                page = self.before_of(&oldest, AT_A_TIME, Which::All)?;
            }
        }
        said.reverse();
        Ok(said)
    }

    /// Up to `limit` things a reader asks for that are newer than `revision`.
    pub fn after_of(&self, revision: u64, limit: usize, which: Which) -> Result<Page, ActorError> {
        conversation::after(
            self.recorder.log(),
            revision,
            self.drafts(),
            self.span(),
            limit,
            which,
        )
    }

    /// The agent's background jobs.
    pub fn jobs(&self) -> &JobBook {
        &self.jobs
    }

    /// Carries out what a worker, a person or the Host asks of a background job. `revision`
    /// is the number the change gets if it is one. The inner error is a command that makes
    /// no sense; a command that changes nothing is not an error.
    pub fn job(
        &mut self,
        command: JobCommand,
        now_ms: i64,
        revision: u64,
    ) -> Result<(Result<JobChange, JobRefusal>, Outputs), ActorError> {
        let change = match self.jobs.decide(command, now_ms, revision) {
            Ok(change) => change,
            Err(refusal) => return Ok((Err(refusal), Outputs::default())),
        };
        let mut out = Outputs::default();
        if let Some(record) = &change.record {
            let body = serde_json::to_vec(record).map_err(|error| {
                ActorError::Corrupt(format!("a job record could not be written: {error}"))
            })?;
            let item = NewItem::new(kind::JOB, now_ms, body).keyed(format!("j-{}", record.job.id));
            let mut batch = self.recorder.batch();
            batch.item(item);
            batch.commit()?;
            self.jobs.apply(record);
            out.changed.jobs = true;
        }
        if let Some(beat) = &change.beat {
            // Not written, but shown: how long ago a job was heard from is read off it.
            self.jobs.beat(beat);
            out.changed.jobs = true;
        }
        Ok((Ok(change), out))
    }

    /// The agent's goals and plans.
    pub fn goals(&self) -> &GoalBook {
        &self.goals
    }

    /// What the agent made for its user and what it asks its user.
    pub fn artifacts(&self) -> &ArtifactBook {
        &self.artifacts
    }

    /// The files the agent published.
    pub fn media(&self) -> &MediaBook {
        &self.media
    }

    /// The lines the agent says aloud.
    pub fn speech(&self) -> &SpeechBook {
        &self.speech
    }

    /// Writes lines as they stand now, and takes them into the book.
    fn write_lines(&mut self, lines: &[Spoken], now_ms: i64) -> Result<(), ActorError> {
        if lines.is_empty() {
            return Ok(());
        }
        let mut batch = self.recorder.batch();
        for line in lines {
            batch.item(record_item(kind::LINE, line.id.clone(), line, now_ms)?);
        }
        batch.commit()?;
        for line in lines {
            self.speech.apply(line);
        }
        Ok(())
    }

    /// An engine takes the line that is spoken next. `None`: nothing of this agent's is to
    /// be spoken now, because nothing waits or because a line of its is being spoken.
    pub fn claim_line(&mut self, now_ms: i64) -> Result<Option<Spoken>, ActorError> {
        self.speech.forget_before(now_ms - KEPT_FOR_MS);
        let taken = self
            .speech
            .next()
            .and_then(|line| line.claimed(now_ms).ok());
        if let Some(line) = &taken {
            self.write_lines(std::slice::from_ref(line), now_ms)?;
        }
        Ok(taken)
    }

    /// The line an engine took has a clip: its number on this Host and its file, the
    /// engine and the voice that are asked first, and the number of the event the clip was
    /// announced with. `None`: the agent has no such line being spoken without a clip.
    pub fn line_begun(
        &mut self,
        id: &str,
        clip: (u64, &str),
        asked: (&str, &str),
        event_id: Option<u64>,
        now_ms: i64,
    ) -> Result<Option<Spoken>, ActorError> {
        let line = self.speech.get(id);
        let begun = line.and_then(|line| line.begun(clip, asked, event_id).ok());
        if let Some(line) = &begun {
            self.write_lines(std::slice::from_ref(line), now_ms)?;
        }
        Ok(begun)
    }

    /// The one who took a line is done with it: the clip it made of it is whole, or the
    /// line was not spoken, or not all of it, and why. `clip` is the clip it made, or none.
    /// `None`: the agent has no such line being spoken with that clip.
    pub fn line_ended(
        &mut self,
        id: &str,
        clip: Option<u64>,
        outcome: Result<Said<'_>, &str>,
        now_ms: i64,
    ) -> Result<Option<Spoken>, ActorError> {
        let line = self.speech.get(id);
        let ended = line.and_then(|line| line.ended(clip, outcome, now_ms).ok());
        if let Some(line) = &ended {
            self.write_lines(std::slice::from_ref(line), now_ms)?;
        }
        Ok(ended)
    }

    /// A clip that was held back is announced to the apps with the event of this number.
    /// Answers whether the agent has that clip.
    pub fn line_announced(
        &mut self,
        clip_id: u64,
        (event_id, across): (u64, bool),
        now_ms: i64,
    ) -> Result<bool, ActorError> {
        let Some(line) = self.speech.by_clip(clip_id) else {
            return Ok(false);
        };
        let announced = line.announced(event_id, across);
        self.write_lines(std::slice::from_ref(&announced), now_ms)?;
        Ok(true)
    }

    /// What an app says of playing a clip. Answers whether the agent has that clip.
    pub fn line_played(
        &mut self,
        clip_id: u64,
        status: &str,
        error: Option<&str>,
        now_ms: i64,
    ) -> Result<bool, ActorError> {
        let Some(line) = self.speech.by_clip(clip_id) else {
            return Ok(false);
        };
        let played = line.played(status, error);
        self.write_lines(std::slice::from_ref(&played), now_ms)?;
        Ok(true)
    }

    /// The lines that still wait are taken back: nothing more of what was said is spoken.
    /// Answers how many there were.
    pub fn drop_lines(&mut self, now_ms: i64) -> Result<usize, ActorError> {
        let waiting = self.speech.waiting();
        let dropped: Vec<Spoken> = waiting
            .filter_map(|line| line.dropped(now_ms).ok())
            .collect();
        self.write_lines(&dropped, now_ms)?;
        Ok(dropped.len())
    }

    fn open_goal_ledger(&mut self, now: i64) -> Result<(), ActorError> {
        if self.recorder.log().find("ledger/installed")?.is_some() {
            return Ok(());
        }
        let plans: Vec<_> = self
            .goals
            .plans()
            .filter(|p| p.goal.is_some())
            .map(|p| p.plan_id.clone())
            .collect();
        for id in plans {
            // A tail and corrected summary are committed together. A crash
            // between plans resumes without relabelling any existing events.
            if goal_ledger::has_plan(self.recorder.log(), &id)? {
                continue;
            }
            let plan = self
                .goals
                .plan(&id)
                .ok_or_else(|| ActorError::Corrupt("missing legacy plan".into()))?;
            let events = ledger::legacy(plan);
            let mut change = GoalChange {
                plan: plan.clone(),
                documents: Vec::new(),
            };
            change.plan.event_count = events.len() as i64;
            change.plan.last_event_at = events.iter().map(|e| e.at).max();
            let mut items = Vec::new();
            let baseline =
                goal_ledger::stage(self.recorder.log(), &id, events, 0, 0, now, &mut items)?;
            let mut record = self.goals.record(&change, now);
            record.import_keeper_cursor(plan, None, baseline);
            items.push(record_item(kind::GOAL, format!("g-{id}"), &record, now)?);
            let mut batch = self.recorder.batch();
            for item in items {
                batch.item(item);
            }
            batch.commit()?;
            self.goals.apply(&record);
        }
        // Avoid adding any log item merely for opening a brand-new agent.
        if self.goals.plans().any(|p| p.goal.is_some()) {
            let mut batch = self.recorder.batch();
            batch.item(record_item(
                kind::GOAL_LEDGER,
                "ledger/installed".into(),
                &true,
                now,
            )?);
            batch.commit()?;
        }
        Ok(())
    }

    pub fn goal_events(
        &self,
        plan: &str,
        after: i64,
        limit: usize,
    ) -> Result<(Vec<LedgerEvent>, bool), ActorError> {
        goal_ledger::page(self.recorder.log(), plan, after, limit)
    }

    pub fn next_goal_event_id(&self) -> Result<i64, ActorError> {
        goal_ledger::next_id(self.recorder.log())
    }

    pub fn validate_goal_import(
        &self,
        plans: &[(String, &[LedgerEvent])],
    ) -> Result<(), ActorError> {
        goal_ledger::check_import(self.recorder.log(), plans)
    }

    /// Writes what was decided on the agent's books, all of it in one append, and takes it.
    pub fn keep(&mut self, kept: &Kept<'_>, now_ms: i64) -> Result<Outputs, ActorError> {
        let mut written = self.written(kept, now_ms)?;
        let mut out = Outputs::default();
        let mut batch = self.recorder.batch();
        for item in std::mem::take(&mut written.items) {
            batch.item(item);
        }
        batch.commit()?;
        self.take_written(&written, &mut out);
        Ok(out)
    }

    /// What the log is to keep of changes to the books.
    fn written(&self, kept: &Kept<'_>, now_ms: i64) -> Result<Written, ActorError> {
        let mut goal = kept.goal.map(|change| self.goals.record(change, now_ms));
        let artifacts: Vec<ArtifactRecord> = kept
            .artifacts
            .iter()
            .map(|artifact| ArtifactBook::record(artifact, now_ms))
            .collect();
        let mut items = Vec::new();
        if let Some(change) = kept.goal {
            let plan = &change.plan;
            let (mut events, imported) = match kept.imported_events {
                Some(imports) => (imports.to_vec(), imports.len()),
                None if kept.subgoal_event.is_some() => {
                    (kept.subgoal_event.into_iter().cloned().collect(), 0)
                }
                None if matches!(kept.author, Author::Legacy) => (ledger::legacy(plan), 0),
                None => (
                    ledger::changes(self.goals.plan(&plan.plan_id), plan, kept.author)
                        .into_iter()
                        .map(|(_, e)| e)
                        .collect(),
                    0,
                ),
            };
            if let Some(source) = kept.entry_source {
                events.push(ledger::entry_marker(plan, source, &events, now_ms));
            }
            if let Some(record) = goal
                .as_mut()
                .filter(|_| kept.imported_events.is_some() || matches!(kept.author, Author::Legacy))
            {
                record.event_count = i64::try_from(events.len())
                    .map_err(|_| ActorError::Corrupt("too many imported goal events".into()))?;
                record.last_event_at = events.iter().map(|e| e.at).max();
                if events.is_empty() && plan.goal.is_some() {
                    // An explicitly empty incoming ledger must stay empty after a restart.
                    goal_ledger::mark_empty(&plan.plan_id, now_ms, &mut items)?;
                }
            }
            let last = goal_ledger::stage(
                self.recorder.log(),
                &plan.plan_id,
                events,
                imported,
                kept.import_high,
                now_ms,
                &mut items,
            )?;
            if let Some(record) = goal
                .as_mut()
                .filter(|_| matches!(kept.author, Author::Legacy) || kept.imported_events.is_some())
            {
                // For an entry into an existing owner the entry marker is new, even
                // when all the source history has already been seen by its keeper.
                let baseline = last - i64::from(kept.entry_source.is_some());
                record.import_keeper_cursor(plan, kept.imported_events, baseline);
            }
        }
        if let Some(record) = &goal {
            let key = format!("g-{}", record.plan_id);
            items.push(record_item(kind::GOAL, key, record, now_ms)?);
        }
        if let Some((delegation, requested)) = kept.applied_cursor {
            items.push(goal_ledger::cursor(
                self.recorder.log(),
                delegation,
                requested,
                now_ms,
                &items,
            )?);
        }
        for record in &artifacts {
            let key = format!("a-{}", record.artifact.artifact_id);
            items.push(record_item(kind::ARTIFACT, key, record, now_ms)?);
        }
        for asset in kept.media {
            let key = format!("m-{}", asset.asset_id);
            items.push(record_item(kind::MEDIA, key, asset, now_ms)?);
        }
        Ok(Written {
            items,
            goal,
            artifacts,
            media: kept.media.to_vec(),
        })
    }

    /// Takes into the books what is in the log now.
    fn take_written(&mut self, written: &Written, out: &mut Outputs) {
        if let Some(record) = &written.goal {
            self.goals.apply(record);
            out.changed.goals = true;
        }
        for record in &written.artifacts {
            self.artifacts.apply(record);
            out.changed.artifacts = true;
        }
        for asset in &written.media {
            self.media.apply(asset);
            out.changed.media = true;
        }
    }

    /// Counts every running job as heard from now: after the machine slept, what was not
    /// heard meanwhile says nothing about the workers.
    pub fn jobs_heard_at(&mut self, now_ms: i64) {
        self.jobs.heard_at(now_ms);
    }

    /// A job's timeline: the newest `limit` of its changes, oldest first.
    pub fn job_timeline(&self, id: &str, limit: usize) -> Result<Vec<JobRecord>, ActorError> {
        let mut entries = Vec::new();
        let mut before = Seq::MAX;
        while entries.len() < limit {
            let page = self
                .recorder
                .log()
                .read_before(before, kind::JOBS, WINDOW)?;
            let Some(first) = page.first() else { break };
            before = first.seq;
            for item in page.iter().rev() {
                if item.kind == kind::PROVIDER_JOBS {
                    let entry: clarp_kernel::provider_tasks::JournalEntry =
                        serde_json::from_slice(&item.body)
                            .map_err(|e| ActorError::Corrupt(e.to_string()))?;
                    let room = limit - entries.len();
                    entries.extend(
                        entry
                            .records
                            .into_iter()
                            .rev()
                            .filter(|r| r.timeline && r.job.id == id)
                            .take(room),
                    );
                    continue;
                }
                if item.kind != kind::JOB || item.key.as_deref() != Some(&format!("j-{id}")) {
                    continue;
                }
                let record: JobRecord = serde_json::from_slice(&item.body).map_err(|error| {
                    ActorError::Corrupt(format!(
                        "log item {} is not the job record its kind says: {error}",
                        item.seq
                    ))
                })?;
                if record.timeline && entries.len() < limit {
                    entries.push(record);
                }
            }
        }
        entries.reverse();
        Ok(entries)
    }

    /// The stored row with this id, as it is now.
    pub fn row(&self, id: &str) -> Result<Option<Versioned>, ActorError> {
        conversation::one(self.recorder.log(), id)
    }

    /// The `limit` things before the one with this id, as a chat scrolls back. `None` when the
    /// conversation has no such thing.
    pub fn before(&self, id: &str, limit: usize) -> Result<Option<Page>, ActorError> {
        self.before_of(id, limit, Which::All)
    }

    /// The `limit` things a reader asks for before the one with this id.
    pub fn before_of(
        &self,
        id: &str,
        limit: usize,
        which: Which,
    ) -> Result<Option<Page>, ActorError> {
        let Some(item) = self.recorder.log().find(id)? else {
            return Ok(None);
        };
        conversation::older(
            self.recorder.log(),
            item.seq,
            &[],
            self.span(),
            limit,
            which,
        )
        .map(Some)
    }

    /// Takes a message for the agent. When this returns, the message is in the log.
    pub fn send(
        &mut self,
        message: &Message,
        admission: Admission,
        now_ms: i64,
    ) -> Result<(Sent, Outputs), ActorError> {
        self.take(message, admission, Vec::new(), None, now_ms)
    }

    /// File the Host's opening reservation in the same message as the
    /// assigned turn, so a crash cannot turn a matching client id into proof.
    pub fn send_for_opening(
        &mut self,
        message: &Message,
        admission: Admission,
        reservation: serde_json::Value,
        now_ms: i64,
    ) -> Result<(Sent, Outputs), ActorError> {
        self.take(message, admission, Vec::new(), Some(reservation), now_ms)
    }

    /// Tells the agent something of the Host's own, and writes what belongs with it in the
    /// same append: a goal's record of the wake it is, a request's mark that its answer was
    /// told. So there is never a message without its record, nor a record of a message that
    /// was not sent.
    pub fn keep_with(
        &mut self,
        kept: &Kept<'_>,
        message: &Message,
        admission: Admission,
        now_ms: i64,
    ) -> Result<(Sent, Outputs), ActorError> {
        let mut written = self.written(kept, now_ms)?;
        let items = std::mem::take(&mut written.items);
        let (sent, mut out) = self.take(message, admission, items, None, now_ms)?;
        if sent.duplicate {
            // The message is there already; what belongs with it has still to be written.
            return Ok((sent, self.keep(kept, now_ms)?));
        }
        self.take_written(&written, &mut out);
        Ok((sent, out))
    }

    /// Takes a message, and writes `with` with it.
    fn take(
        &mut self,
        message: &Message,
        admission: Admission,
        with: Vec<NewItem>,
        reservation: Option<serde_json::Value>,
        now_ms: i64,
    ) -> Result<(Sent, Outputs), ActorError> {
        let encoded = if let Some(reservation) = reservation {
            serde_json::to_value(message).and_then(|mut row| {
                row["_call_opening"] = reservation;
                serde_json::to_vec(&row)
            })
        } else {
            serde_json::to_vec(message)
        };
        let body = encoded.map_err(|error| {
            ActorError::Corrupt(format!("a message could not be written: {error}"))
        })?;
        let accepted = self.recorder.accept_message_with(
            Incoming {
                key: Some(message.id.clone()),
                body,
                origin: message.origin.clone(),
                admission,
            },
            with,
            now_ms,
        )?;
        let mut out = Outputs::default();
        if accepted.duplicate {
            let sent = Sent {
                message_seq: accepted.message_seq,
                turn: None,
                queued: false,
                duplicate: true,
                queue: self.queue_state(),
            };
            return Ok((sent, out));
        }
        let started = accepted.effects.iter().any(
            |effect| matches!(effect, Effect::StartTurn { turn, .. } if Some(*turn) == accepted.turn),
        );
        self.unstored_changes = 0;
        out.changed = Changed {
            conversation: accepted.entered,
            status: !accepted.effects.is_empty(),
            queue: !started,
            ..Changed::default()
        };
        self.carry_out(accepted.effects, &mut out)?;
        let sent = Sent {
            message_seq: accepted.message_seq,
            turn: accepted.turn,
            queued: !started,
            duplicate: false,
            queue: self.queue_state(),
        };
        Ok((sent, out))
    }

    /// Takes over the conversation an agent had on another Host: its rows as that Host served
    /// them, and the tool's session to continue. Everything arrives in one write or not at all.
    /// Only an agent that has no history here can be taken over; the inner error says so.
    pub fn import(
        &mut self,
        from: &str,
        backend_session: Option<String>,
        rows: Vec<ImportedRow>,
        now_ms: i64,
    ) -> Result<(Result<usize, String>, Outputs), ActorError> {
        let adopt = Command::Adopt {
            from: from.to_string(),
            backend_session,
        };
        self.begin_with(adopt, rows, now_ms)
    }

    /// Begins with a session the agent's tool kept on this machine: what was said in it, as
    /// rows read from the tool's own transcript, and the session to continue. In one write or
    /// not at all, and only for an agent that has no history here.
    pub fn take_up(
        &mut self,
        session: &str,
        rows: Vec<ImportedRow>,
        now_ms: i64,
    ) -> Result<(Result<usize, String>, Outputs), ActorError> {
        let take = Command::TakeUp {
            backend_session: session.to_string(),
        };
        self.begin_with(take, rows, now_ms)
    }

    /// Writes the rows an agent begins with and what says where they came from, together.
    fn begin_with(
        &mut self,
        how: Command,
        rows: Vec<ImportedRow>,
        now_ms: i64,
    ) -> Result<(Result<usize, String>, Outputs), ActorError> {
        if self.recorder.last_seq() != 0 {
            let refusal = "the agent already has a history on this Host".to_string();
            return Ok((Err(refusal), Outputs::default()));
        }
        let count = rows.len();
        // The adopted runtime starts here. Older launches in its existing
        // transcript cannot create new jobs for this agent.
        let binding = if self.backend_id() == "claude" {
            match &how {
                Command::TakeUp { backend_session }
                | Command::Adopt {
                    backend_session: Some(backend_session),
                    ..
                } => Some(clarp_kernel::provider_tasks::JournalEntry {
                    native: backend_session.clone(),
                    session: clarp_kernel::provider_tasks::Session {
                        revision: 1,
                        bound_at: now_ms,
                        cwd: self.profile.cwd.to_string_lossy().into_owned(),
                        ..Default::default()
                    },
                    records: Vec::new(),
                }),
                _ => None,
            }
        } else {
            None
        };
        let mut batch = self.recorder.batch();
        for row in rows {
            let ts_ms = row.ts_ms;
            let piece = Entry::Imported(Imported {
                id: row.id,
                row: row.row,
            });
            batch.item(written(&piece, ts_ms)?);
        }
        if let Err(refusal) = batch.command(how, now_ms) {
            return Ok((Err(refusal.to_string()), Outputs::default()));
        }
        if let Some(entry) = &binding {
            batch.item(NewItem::new(
                kind::PROVIDER_JOBS,
                now_ms,
                serde_json::to_vec(entry).map_err(|e| ActorError::Corrupt(e.to_string()))?,
            ));
        }
        batch.commit()?;
        if let Some(entry) = binding {
            self.provider_sessions.insert(entry.native, entry.session);
        }
        self.unstored_changes = 0;
        let out = Outputs {
            changed: Changed {
                conversation: true,
                status: true,
                queue: false,
                ..Changed::default()
            },
            ..Outputs::default()
        };
        Ok((Ok(count), out))
    }

    /// The Host says something in the conversation that belongs to no turn, quietly: that the
    /// agent was asked into a call, put on hold, that the call ended. A notice is written
    /// once, under its id. Answers whether it was written now.
    pub fn note(
        &mut self,
        id: &str,
        text: &str,
        now_ms: i64,
    ) -> Result<(bool, Outputs), ActorError> {
        if id.is_empty() || text.trim().is_empty() || self.row(id)?.is_some() {
            return Ok((false, Outputs::default()));
        }
        let notice = Entry::Notice(Notice {
            id: id.to_string(),
            turn: None,
            code: Notice::SYSTEM.to_string(),
            text: text.to_string(),
            trace_id: None,
        });
        let mut batch = self.recorder.batch();
        batch.item(written(&notice, now_ms)?);
        batch.commit()?;
        let out = Outputs {
            changed: Changed {
                conversation: true,
                ..Changed::default()
            },
            ..Outputs::default()
        };
        Ok((true, out))
    }

    /// Ends the live turn and holds the mailbox, as this actor's claim and for no reason given.
    /// Returns how many turns were told to end.
    pub fn stop(&mut self, by: &str, now_ms: i64) -> Result<(u32, Outputs), ActorError> {
        self.stop_as(Control::by(by), now_ms)
    }

    /// A turn that nothing runs is looked for and given back, and a note of who and why is kept
    /// on the agent's state. Nothing is ended, and nothing that waits is held or let go.
    pub fn repair(&mut self, by: Control, now_ms: i64) -> Result<Outputs, ActorError> {
        self.recorder.handle(Command::Repair { by }, now_ms)?;
        Ok(Outputs {
            changed: Changed {
                status: true,
                ..Changed::default()
            },
            ..Outputs::default()
        })
    }

    /// Ends the live turn and holds the mailbox: who asks it, and why. Returns how many turns
    /// were told to end.
    pub fn stop_as(&mut self, by: Control, now_ms: i64) -> Result<(u32, Outputs), ActorError> {
        let silenced = by.is_the_users();
        let stopped_between_attempts =
            matches!(self.recorder.agent().live(), Some(Turn::Parked(_)));
        let command = Command::Stop { by };
        let effects = self.recorder.handle(command, now_ms)?;
        let told = effects
            .iter()
            .filter(|effect| matches!(effect, Effect::StopTurn { .. }))
            .count();
        let mut out = Outputs {
            changed: Changed {
                conversation: false,
                status: true,
                queue: true,
                ..Changed::default()
            },
            ..Outputs::default()
        };
        self.carry_out(effects, &mut out)?;
        // Only the user's own Stop means "be quiet": the rest of what the agent had to say is
        // not spoken. An agent that stops another orchestrates; what was about to be said is
        // still the user's to hear.
        if silenced {
            out.changed.speech = self.drop_lines(now_ms)? > 0;
        }
        if stopped_between_attempts {
            self.rest_if_idle();
        }
        Ok((u32::try_from(told).unwrap_or(u32::MAX), out))
    }

    /// Starts the agent again: its next turn begins a conversation with its tool, and its
    /// conversation, as it is read, begins anew: empty, but for what waits its turn and was
    /// said already. Answers `false`, with nothing changed, while a turn of the agent runs:
    /// whoever starts an agent again stops that turn first.
    pub fn start_again(&mut self, now_ms: i64) -> Result<(bool, Outputs), ActorError> {
        let Some(moved) = self.recorder.start_again(now_ms)? else {
            return Ok((false, Outputs::default()));
        };
        // Nothing of the conversation that ended is shown any more. What the agent last
        // said stays what a list of chats shows of it, until it says something new. How
        // full its tool's conversation was and which model answered in it were said of the
        // conversation that ended: the new one has no such word yet.
        self.shown_revision = 0;
        self.remembered.context_tokens = None;
        self.remembered.answered_by = None;
        let out = Outputs {
            changed: Changed {
                status: true,
                queue: moved > 0,
                ..Changed::default()
            },
            ..Outputs::default()
        };
        Ok((true, out))
    }

    /// The agent says what it is busy with beyond its turns, or with an empty label, that it
    /// no longer is.
    pub fn label(&mut self, text: &str, now_ms: i64) -> Result<Outputs, ActorError> {
        let command = Command::Label {
            text: text.to_string(),
        };
        self.recorder.handle(command, now_ms)?;
        Ok(Outputs {
            changed: Changed {
                status: true,
                ..Changed::default()
            },
            ..Outputs::default()
        })
    }

    /// Takes back a label that outlived its time, if it is still the one set at `set_at_ms`.
    /// Returns whether it was taken back.
    pub fn retire_label(
        &mut self,
        set_at_ms: i64,
        now_ms: i64,
    ) -> Result<(bool, Outputs), ActorError> {
        let before = self.status().label_at_ms;
        self.recorder
            .handle(Command::RetireLabel { set_at_ms }, now_ms)?;
        let retired = before.is_some() && self.status().label_at_ms.is_none();
        let out = Outputs {
            changed: Changed {
                status: retired,
                ..Changed::default()
            },
            ..Outputs::default()
        };
        Ok((retired, out))
    }

    /// Lets a held mailbox run again. Returns whether it was held.
    pub fn resume(&mut self, by: &str, now_ms: i64) -> Result<(bool, Outputs), ActorError> {
        let command = Command::Resume { by: by.to_string() };
        self.asked(command, now_ms)
    }

    /// Takes a waiting message back. Returns whether it was waiting.
    pub fn withdraw(
        &mut self,
        turn: TurnId,
        by: &str,
        now_ms: i64,
    ) -> Result<(bool, Outputs), ActorError> {
        let command = Command::Withdraw {
            turn,
            by: by.to_string(),
        };
        self.asked(command, now_ms)
    }

    /// Changes the words of a message that waits outside the conversation. Its turn answers
    /// the new words; everything else about it stays: its id, its place, who sent it.
    pub fn reword(
        &mut self,
        turn: TurnId,
        text: &str,
        now_ms: i64,
    ) -> Result<(Reworded, Outputs), ActorError> {
        let unreadable = |error: serde_json::Error| JournalError::Encode(error.to_string());
        let anew = |kept: &[u8]| {
            let mut message: Message = serde_json::from_slice(kept).map_err(unreadable)?;
            message.text = text.to_string();
            serde_json::to_vec(&message).map_err(unreadable)
        };
        let reworded = self.recorder.reword(turn, anew, now_ms)?;
        let out = Outputs {
            changed: Changed {
                queue: reworded == Reworded::Done,
                ..Changed::default()
            },
            ..Outputs::default()
        };
        Ok((reworded, out))
    }

    /// Sends one waiting message now, whatever holds the mailbox: its user sends it by hand.
    /// The rest of what waits stays as it is, held where it was held.
    pub fn send_now(
        &mut self,
        turn: TurnId,
        by: &str,
        now_ms: i64,
    ) -> Result<(ByHand, Outputs), ActorError> {
        let command = Command::SendNow {
            turn,
            by: by.to_string(),
        };
        let effects = match self.recorder.handle(command, now_ms) {
            Ok(effects) => effects,
            Err(JournalError::Refused(Refusal::AtWork)) => {
                return Ok((ByHand::AtWork, Outputs::default()));
            }
            Err(JournalError::Refused(_)) => return Ok((ByHand::NotWaiting, Outputs::default())),
            Err(error) => return Err(error.into()),
        };
        let mut out = Outputs {
            changed: Changed {
                conversation: false,
                status: !effects.is_empty(),
                queue: true,
                ..Changed::default()
            },
            ..Outputs::default()
        };
        self.carry_out(effects, &mut out)?;
        Ok((ByHand::Sent, out))
    }

    /// A command a client asked for, which the agent may refuse. Returns whether it was done.
    fn asked(&mut self, command: Command, now_ms: i64) -> Result<(bool, Outputs), ActorError> {
        let effects = match self.recorder.handle(command, now_ms) {
            Ok(effects) => effects,
            Err(JournalError::Refused(_)) => return Ok((false, Outputs::default())),
            Err(error) => return Err(error.into()),
        };
        let mut out = Outputs {
            changed: Changed {
                conversation: false,
                status: !effects.is_empty(),
                queue: true,
                ..Changed::default()
            },
            ..Outputs::default()
        };
        self.carry_out(effects, &mut out)?;
        Ok((true, out))
    }

    /// Whether the actor follows this attempt of this turn. What is said of another attempt's
    /// shim is late news: that attempt is over, and its turn may be at its next one.
    fn follows(&self, turn: TurnId, attempt: u32) -> bool {
        self.live
            .as_ref()
            .is_some_and(|live| live.turn == turn && live.attempt == attempt)
    }

    /// The shim for this attempt of the turn could not be started.
    pub fn launch_failed(
        &mut self,
        turn: TurnId,
        attempt: u32,
        reason: &str,
        now_ms: i64,
    ) -> Result<Outputs, ActorError> {
        if !self.follows(turn, attempt) {
            return Ok(Outputs::default());
        }
        let command = Command::StartFailed {
            turn,
            reason: reason.to_string(),
        };
        self.ended_outside_the_spool(turn, command, now_ms)
    }

    /// Nothing answers at the shim of this attempt of the turn any more.
    pub fn shim_gone(
        &mut self,
        turn: TurnId,
        attempt: u32,
        now_ms: i64,
    ) -> Result<Outputs, ActorError> {
        if !self.follows(turn, attempt) {
            return Ok(Outputs::default());
        }
        let command = Command::TurnEnded {
            turn,
            outcome: RunOutcome::Interrupted {
                reason: "the turn's process is gone and did not say how it went".to_string(),
            },
        };
        self.ended_outside_the_spool(turn, command, now_ms)
    }

    /// Ends a turn on someone's word instead of the spool's. The spool is read first: if it does
    /// say how the turn went, that is what counts.
    fn ended_outside_the_spool(
        &mut self,
        turn: TurnId,
        command: Command,
        now_ms: i64,
    ) -> Result<Outputs, ActorError> {
        let mut out = Outputs::default();
        if self.live.as_ref().map(|live| live.turn) != Some(turn) {
            return Ok(out);
        }
        self.pump(&mut out, now_ms)?;
        if self.live.as_ref().map(|live| live.turn) != Some(turn) {
            return Ok(out);
        }
        // A turn between two attempts has no shim and no tool: what is said of the attempt
        // that is over is late news.
        if !self.recorder.agent().live().is_some_and(Turn::has_process) {
            return Ok(out);
        }
        // An attempt that was stopped for its account, and went without a word in its
        // spool: the turn waits for an account all the same, and the pool is told.
        if self.live.as_ref().is_some_and(|live| live.for_account)
            && self.recorder.agent().stopping().is_none()
        {
            let most = recovery::ACCOUNT_WAIT_MOST_MS + POOL_MARGIN_MS;
            let parked = Command::TurnParked {
                turn,
                wait: Wait::Account {
                    until_ms: now_ms + most,
                },
                kind: failures::USAGE_LIMIT.to_string(),
                reason: STOPPED_FOR_AN_ACCOUNT.to_string(),
            };
            let mut batch = self.recorder.batch();
            if batch.command(parked, now_ms).is_ok() {
                let effects = batch.commit()?.effects;
                if let Some(live) = self.live.as_mut() {
                    live.for_account = false;
                    live.drafts.clear();
                    live.tools.clear();
                    live.calls.clear();
                }
                let events = self.shown.settle("interrupted", now_ms);
                self.shown_events.extend(events);
                out.changed.status = true;
                out.changed.conversation = true;
                self.carry_out(effects, &mut out)?;
                if let Some(pool) = &self.pool {
                    pool.stopped(now_ms);
                }
                return Ok(out);
            }
        }
        // What the agent had written and nobody stored is kept: the user saw it.
        let unfinished = self.drafts().to_vec();
        let facts = self.live.as_ref().map(LiveTurn::facts).unwrap_or_default();
        let (message_id, origin) = self
            .live
            .as_ref()
            .map(|live| (live.message_id.clone(), live.origin.clone()))
            .unwrap_or_default();
        let name = turn_name(&message_id).to_string();
        let model = self.turn_model().unwrap_or_default();
        let mut closes = self
            .live
            .as_ref()
            .map(|live| live.last_row.clone())
            .unwrap_or_default();
        let mut calls = self
            .live
            .as_mut()
            .map(|live| std::mem::take(&mut live.calls))
            .unwrap_or_default();
        let mut batch = self.recorder.batch();
        for draft in unfinished {
            let piece = Entry::Reply(Reply {
                id: draft.id,
                turn: draft.turn,
                text: kept(draft.text, &draft.origin),
                trace_id: facts.trace_id.clone(),
                origin: draft.origin,
                ..Reply::default()
            });
            closes = piece.id().to_string();
            batch.item(written(&piece, now_ms)?);
        }
        // What is said of the end, and whether that row closes the turn.
        let told = match &command {
            _ if batch.agent().stopping().is_some() => None,
            Command::StartFailed { reason, .. } => failure_marker(Failure {
                turn,
                message_id: &message_id,
                origin: &origin,
                kind: failures::NOT_STARTED,
                reason,
                model: &model,
                trace_id: facts.trace_id.clone(),
            })
            .map(|marker| (marker, false)),
            Command::TurnEnded {
                outcome: RunOutcome::Interrupted { reason },
                ..
            } => {
                let trace = facts.trace_id.clone();
                let notice = ending_notice(turn, &name, "turn-interrupted", reason, trace);
                Some((notice, true))
            }
            _ => None,
        };
        if batch.command(command, now_ms).is_ok() {
            if let Some((notice, closing_row)) = &told {
                if *closing_row {
                    closes = notice.id().to_string();
                }
                batch.item(written(notice, now_ms)?);
            }
            if let Some(end) = closing(&batch, turn, &closes, facts) {
                cut_off(&mut batch, &mut calls, &end, now_ms)?;
                batch.item(written(&end, now_ms)?);
            }
        }
        let effects = batch.commit()?.effects;
        self.unstored_changes = 0;
        self.forget_live();
        out.changed.status = true;
        out.changed.conversation = true;
        self.carry_out(effects, &mut out)?;
        // The turn that started next may already have something in its spool.
        self.pump(&mut out, now_ms)?;
        Ok(out)
    }

    /// The live turn's spool may have grown.
    pub fn spool_changed(&mut self, now_ms: i64) -> Result<Outputs, ActorError> {
        let mut out = Outputs::default();
        self.pump(&mut out, now_ms)?;
        Ok(out)
    }

    fn carry_out(&mut self, effects: Vec<Effect>, out: &mut Outputs) -> Result<(), ActorError> {
        for effect in effects {
            match effect {
                Effect::StartTurn { turn, message_seq } => {
                    out.changed.status = true;
                    self.start(turn, message_seq, 1, out)?;
                }
                Effect::RetryTurn {
                    turn,
                    message_seq,
                    attempt,
                } => {
                    out.changed.status = true;
                    self.start(turn, message_seq, attempt, out)?;
                }
                Effect::StopTurn { turn } => out.actions.push(Action::Stop {
                    turn,
                    socket: self
                        .profile
                        .socket_path_of(turn, self.attempt_of_live(turn)),
                    grace_ms: self.profile.stop_grace_ms,
                }),
            }
        }
        Ok(())
    }

    /// Which attempt of this turn the actor follows: 1 when it follows none of it.
    fn attempt_of_live(&self, turn: TurnId) -> u32 {
        self.live
            .as_ref()
            .filter(|live| live.turn == turn)
            .map_or(1, |live| live.attempt)
    }

    /// Hands an attempt of a turn to a shim: the message goes to disk, and the world is asked
    /// to launch. A later attempt is given the same message; the tool is started on the
    /// session it named in an earlier one, when it named one.
    fn start(
        &mut self,
        turn: TurnId,
        message_seq: Seq,
        attempt: u32,
        out: &mut Outputs,
    ) -> Result<(), ActorError> {
        let message = self.message_at(message_seq)?;
        let opening = if opening_message(&message) {
            // A missing tool leaves only the shim's optional session record and
            // SpawnFailed. A lost, damaged, or already invoked attempt has no
            // evidence that can release its launch.
            let previous_unsent = attempt > 1 && {
                let path = self.profile.spool_path_of(turn, attempt - 1);
                SpoolReader::new(path)
                    .read_window(0, u64::MAX, 3)
                    .is_ok_and(|read| {
                        read.damaged.is_empty()
                            && matches!(
                                read.records.as_slice(),
                                [clarp_spool::Record {
                                    entry: Line::Shim(ShimEvent::SpawnFailed { .. }),
                                    ..
                                }] | [
                                    clarp_spool::Record {
                                        entry: Line::Shim(ShimEvent::Session { .. }),
                                        ..
                                    },
                                    clarp_spool::Record {
                                        entry: Line::Shim(ShimEvent::SpawnFailed { .. }),
                                        ..
                                    }
                                ]
                            )
                    })
            };
            Some(Box::new(OpeningOrder {
                id: message.client_id().to_string(),
                trace: message.trace_id.clone().unwrap_or_default(),
                previous_unsent,
            }))
        } else {
            None
        };
        let client_id = message.told_id().to_string();
        // Whatever an attempt before it waited for, this one begins.
        self.for_its_model = false;
        // What the attempts before put into the conversation is still the turn's.
        let before = self.live.take().filter(|live| live.turn == turn);
        let backend_session = self.recorder.agent().backend_session().map(str::to_string);
        // A tool that lets go of old conversations no longer has this one: named, it would
        // begin an empty one without a word. The turn begins a new one instead, which opens
        // with the Host's own record of the old.
        let lost = backend_session
            .as_deref()
            .is_some_and(|session| !self.backend.keeps(session));
        let backend_session = backend_session.filter(|_| !lost);
        // A turn that waited for an account is taken up again: the tool, which has the turn
        // in its own session, is told so. One whose tool never got as far as a session is
        // given the message, as if for the first time.
        let recovering = match self.recorder.agent().live() {
            Some(Turn::Starting(starting)) if starting.recovering && backend_session.is_some() => {
                Some(starting.account.count > 1)
            }
            _ => None,
        };
        // A turn in which the tool makes its conversation shorter is started with the tool's
        // own word for it and nothing else: nothing of the Host's before it or beside it.
        let bare = matches!(&message.origin, Origin::Trigger { name } if name == COMPACTION);
        let prompt = match recovering.filter(|_| !bare) {
            Some(relapsed) => recovery::resume_prompt(self.backend.id(), &message.text, relapsed),
            None => message.text,
        };
        let prompt = if lost && !bare {
            let said = self.said_before(&message.id)?;
            let said: Vec<(bool, &str)> = said
                .iter()
                .map(|(by_user, text)| (*by_user, text.as_str()))
                .collect();
            let declared = clarp_backend::catalogue::declared(self.backend.id());
            let tool = declared["shown"]["label"]
                .as_str()
                .unwrap_or(self.backend.id());
            told::restored(tool, &said, &prompt)
        } else {
            prompt
        };
        // A user who began to speak before hearing the last reply: the tool is told so before
        // the words. The conversation keeps the words as they were said.
        let prompt = if message.unheard && !bare {
            told::unheard(&prompt)
        } else {
            prompt
        };
        let told = self.profile.told.as_ref().filter(|_| !bare);
        let named_session = self
            .backend
            .binds_at_spawn()
            .then(|| {
                backend_session
                    .clone()
                    .unwrap_or_else(|| self.profile.conversation_id.clone())
            })
            .filter(|id| !id.is_empty());
        let launch = self.backend.launch(&TurnSpec {
            agent_id: self.profile.agent_id.clone(),
            session: self.profile.session.clone(),
            turn: turn.0,
            cwd: self.profile.cwd.clone(),
            prompt,
            // The first turn starts the tool's session under the conversation's own name.
            new_session: Some(self.profile.conversation_id.clone())
                .filter(|name| backend_session.is_none() && !name.is_empty()),
            backend_session,
            model: message
                .managed_model
                .clone()
                .or_else(|| self.profile.model.clone()),
            effort: self.profile.effort.clone(),
            identity: told.and_then(|told| told.identity.clone()),
            // How to speak is told to a turn whose answer is read aloud, and to no other.
            told: told.map(|told| told::Turn {
                persona: told.persona.clone(),
                spoken: message.speak.then_some(told.sound),
                call: message.call.clone(),
            }),
        });
        let stdin_file = self.profile.stdin_path_of(turn, attempt);
        write_whole(&stdin_file, &launch.stdin)?;
        let interpreter = self
            .backend
            .interpreter(turn.0, &attempt_of(turn_name(&message.id), attempt));
        // What the tool's conversation had taken before the turn is what it had taken before
        // the turn's first attempt.
        let base = match &before {
            Some(before) => before.base,
            None => self.total_before(turn)?,
        };
        let cost_before = match &before {
            Some(before) => before.cost_before,
            None => self.cost_before(turn)?,
        };
        // What the tool said of its account in an attempt before this one stands until it
        // says it anew.
        let limits = before.as_ref().and_then(|before| before.limits.clone());
        let (last_row, model, tokens, total, context) = match before {
            Some(before) => (
                before.last_row,
                before.model,
                before.tokens,
                before.total,
                before.context,
            ),
            None => (message.id.clone(), None, None, None, None),
        };
        self.live = Some(LiveTurn {
            opening: opening.is_some(),
            managed_model: message.managed_model.clone(),
            turn,
            last_row,
            said: unfollowed(self.recorder.log(), turn)?,
            speak: message.speak,
            client_id,
            message_id: message.id,
            trace_id: message.trace_id,
            origin: message.origin,
            model,
            tokens,
            total,
            base,
            context,
            limits,
            // What the conversation has cost so far is said again by the next attempt, with
            // what this one cost in it.
            per_turn_cost: None,
            cost: None,
            cost_before,
            interpreter,
            offset: 0,
            drafts: Vec::new(),
            tools: Vec::new(),
            calls: Vec::new(),
            over: false,
            attempt,
            for_account: false,
        });
        out.actions.push(Action::Launch(ShimOrder {
            opening,
            turn,
            attempt,
            spool: self.profile.spool_path_of(turn, attempt),
            socket: self.profile.socket_path_of(turn, attempt),
            stdin_file,
            prompt_as_file: launch.prompt_as_file,
            named_session,
            cwd: self.profile.cwd.clone(),
            before_stop: launch.before_stop,
            program: launch.program,
            args: launch.args,
            env: launch
                .env
                .into_iter()
                .chain(self.profile.tool_env.iter().cloned())
                .collect(),
        }));
        Ok(())
    }

    /// Takes up a turn that was live when the actor was last closed.
    fn rejoin(
        &mut self,
        turn: TurnId,
        message_seq: Seq,
        attempt: u32,
        starting: bool,
        out: &mut Outputs,
        now_ms: i64,
    ) -> Result<(), ActorError> {
        let spool = self.profile.spool_path_of(turn, attempt);
        let reader = SpoolReader::new(&spool);
        if !reader.exists() {
            if starting {
                // The attempt was decided and no shim ever took it. Nothing ran: start it now.
                return self.start(turn, message_seq, attempt, out);
            }
            let command = Command::TurnEnded {
                turn,
                outcome: RunOutcome::Interrupted {
                    reason: "the record of the turn's tool is gone".to_string(),
                },
            };
            let message = self.message_at(message_seq)?;
            let facts = Facts {
                trace_id: message.trace_id,
                ..Facts::default()
            };
            let mut batch = self.recorder.batch();
            if batch.command(command, now_ms).is_ok()
                && let Some(end) = closing(&batch, turn, &message.id, facts)
            {
                batch.item(written(&end, now_ms)?);
            }
            let effects = batch.commit()?.effects;
            out.changed.status = true;
            out.changed.conversation = true;
            return self.carry_out(effects, out);
        }
        // A shim took this turn. Read its spool up to the position in the log, only to be where
        // the actor before this one was: what those lines meant is in the log already.
        let until = self.recorder.spool_offset(turn);
        let message = self.message_at(message_seq)?;
        let mut last_row = message.id.clone();
        let mut per_turn_cost = None;
        let (mut model, mut tokens, mut context, mut cost) = (None, None, None, None);
        let (mut total, mut limits) = (None, None);
        let mut interpreter = self
            .backend
            .interpreter(turn.0, &attempt_of(turn_name(&message.id), attempt));
        let mut drafts = Vec::new();
        let mut tools = Vec::new();
        let mut calls = Vec::new();
        let mut signals = Vec::new();
        let mut offset = 0;
        // Those who watch are shown the turn as it stands: its items are made again from the
        // same records, with the times the records have. What that sends is for nobody: no
        // client of this run of the Host has seen any of it.
        let name = shown_name(message.trace_id.as_deref(), &message.id);
        let began = match self.recorder.agent().live() {
            Some(Turn::Starting(held)) => held.started_at_ms,
            Some(Turn::Running(held)) => held.started_at_ms,
            _ => now_ms,
        };
        let conversation = self.shown_conversation();
        let session = self.profile.session.clone();
        let _ = self
            .shown
            .begin_turn(&session, &conversation, &name, Some(began), began);
        self.shown_turn = Some((turn, name.clone()));
        while offset < until {
            let read = reader
                .read_window(offset, until, WINDOW)
                .map_err(|error| io_error(&spool, error))?;
            if read.next_offset == offset {
                break; // the spool is shorter than the log says: go on from what is there
            }
            for record in &read.records {
                interpreter.on_record(record, &mut signals);
                for signal in signals.drain(..) {
                    match signal {
                        Signal::Live(live) => {
                            show(&mut drafts, turn, &message.origin, live, record.t);
                        }
                        Signal::Reply(reply) => {
                            drafts.retain(|draft| draft.id != reply.id);
                            last_row = reply.id;
                        }
                        Signal::Tool(call) => {
                            follow_tool(&mut tools, &mut calls, &call);
                            // A use that is a part of a reply is no row: that reply is.
                            if call.reply.is_none() {
                                last_row = call.id;
                            }
                        }
                        Signal::Notice(notice) => last_row = notice.id,
                        Signal::Model(named) => model = Some(named),
                        Signal::Usage {
                            tokens_in,
                            tokens_out,
                        } => tokens = Some((tokens_in, tokens_out)),
                        Signal::Total {
                            tokens_in,
                            tokens_out,
                        } => total = Some((tokens_in, tokens_out)),
                        Signal::Context { tokens: held } => context = Some(held),
                        Signal::Limits(said) => limits = Some((record.t, said)),
                        Signal::TurnCost { micros } => per_turn_cost = Some(micros),
                        Signal::Cost { micros } => cost = Some(micros),
                        Signal::Glimpse(glimpse) => {
                            let _ = self.shown.show(&glimpse, Some(&name), record.t);
                        }
                        _ => {}
                    }
                }
            }
            offset = read.next_offset;
        }
        // One write to the log, so that every revision from here on is above every revision the
        // actor before this one gave to something it showed and did not store.
        let mut batch = self.recorder.batch();
        batch.cursor(turn, offset, now_ms);
        batch.commit()?;
        self.unstored_changes = 0;
        let base = self.recorder.last_seq() * REVISION_STEP;
        for draft in &mut drafts {
            self.unstored_changes += 1;
            draft.revision = base + self.unstored_changes.min(REVISION_STEP - 1);
            self.shown_revision = draft.revision;
        }
        self.live = Some(LiveTurn {
            opening: opening_message(&message),
            managed_model: message.managed_model.clone(),
            turn,
            client_id: message.told_id().to_string(),
            message_id: message.id,
            trace_id: message.trace_id,
            origin: message.origin,
            last_row,
            said: unfollowed(self.recorder.log(), turn)?,
            speak: message.speak,
            model,
            tokens,
            total,
            base: self.total_before(turn)?,
            context,
            limits,
            per_turn_cost,
            cost,
            cost_before: self.cost_before(turn)?,
            interpreter,
            offset,
            drafts,
            tools,
            calls,
            over: false,
            attempt,
            for_account: false,
        });
        out.changed.conversation = true;
        out.actions.push(Action::Follow {
            turn,
            attempt,
            socket: self.profile.socket_path_of(turn, attempt),
        });
        if self.recorder.agent().stopping().is_some() {
            // The stop was decided; whether the shim ever heard it is not known. Telling it
            // twice does no harm.
            out.actions.push(Action::Stop {
                turn,
                socket: self.profile.socket_path_of(turn, attempt),
                grace_ms: self.profile.stop_grace_ms,
            });
        }
        // What the tool wrote while nobody was reading.
        self.pump(out, now_ms)
    }

    /// The live turn is over, in the log: its files have done their work.
    fn forget_live(&mut self) {
        self.for_its_model = false;
        if let Some(live) = self.live.take() {
            // What the turn's end says is kept for the agent's row.
            if let Ok(Some(end)) = self.turn_end(live.turn.0) {
                self.remembered.take(&end);
            }
            self.rest_if_idle();
            for attempt in 1..=live.attempt {
                let _ = std::fs::remove_file(self.profile.stdin_path_of(live.turn, attempt));
                if !self.profile.keep_spools {
                    let _ = std::fs::remove_file(self.profile.spool_path_of(live.turn, attempt));
                }
            }
        }
    }

    /// When the turn that waits for its next attempt is to be tried again. `None`: no turn
    /// waits for that.
    pub fn retry_at(&self) -> Option<i64> {
        match self.recorder.agent().live() {
            Some(Turn::Parked(parked)) => Some(parked.wait.until_ms()),
            _ => None,
        }
    }

    /// Starts the next attempt of a turn whose wait is over. Nothing happens before then.
    pub fn retry_due(&mut self, now_ms: i64) -> Result<Outputs, ActorError> {
        if self.retry_at().is_none_or(|at| at > now_ms) {
            return Ok(Outputs::default());
        }
        self.retry(now_ms)
    }

    /// Gives the actor the pool of its tool's accounts.
    pub fn pooled(&mut self, pool: Arc<dyn Pooled>) {
        self.pool = Some(pool);
    }

    /// The turn waits for an account that serves its model, while the account now active
    /// serves the other turns of the tool: said so until its next attempt begins.
    pub fn held_for_its_model(&mut self) {
        self.for_its_model = true;
    }

    fn model_waited_for(&self) -> Option<String> {
        self.for_its_model
            .then(|| self.turn_model().unwrap_or_default())
    }

    /// The turn that waits for an account, and since when it has, where one does.
    pub fn waits_for_an_account(&self) -> Option<(TurnId, i64)> {
        let live = self.recorder.agent().live()?;
        match live {
            Turn::Parked(parked) if parked.wait.is_for_an_account() => {
                Some((parked.id, live.account_waits().since_ms?))
            }
            _ => None,
        }
    }

    /// The launch of the turn's attempt is held until an account serves it, or is no
    /// longer: those who watch the turn are shown that it waits for an account.
    pub fn held_for_an_account(&mut self, held: bool, now_ms: i64) {
        self.held_for_an_account = held;
        if !held {
            // Whatever it waited for, it begins now.
            self.for_its_model = false;
        }
        self.show_turn(now_ms);
    }

    /// The account the agent's tool runs on is out, and the owner's selector is about to
    /// put another in place: nothing of the tool may run while it does. A turn that runs is
    /// stopped, and waits for an account like a turn that failed on its own; the pool is
    /// told when nothing of it runs any more. A turn that waits already, or no turn at all,
    /// has nothing to stop: the pool is told so at once.
    pub fn stop_for_an_account(&mut self, now_ms: i64) -> Result<Outputs, ActorError> {
        let mut out = Outputs::default();
        let runs = match self.recorder.agent().live() {
            Some(Turn::Starting(turn)) => Some(turn.id),
            Some(Turn::Running(turn)) => Some(turn.id),
            _ => None,
        };
        let live = self.live.as_mut().filter(|live| !live.over);
        match (runs, live) {
            (Some(turn), Some(live)) if live.turn == turn => {
                live.for_account = true;
                out.actions.push(Action::Stop {
                    turn,
                    socket: self.profile.socket_path_of(turn, live.attempt),
                    grace_ms: self.profile.stop_grace_ms,
                });
            }
            _ => {
                if let Some(pool) = &self.pool {
                    pool.stopped(now_ms);
                }
            }
        }
        Ok(out)
    }

    /// The turn that waits for an account ends on the failure it waits on, with what the
    /// provider said: its plan does not offer the turn's model, and the owner's selector
    /// found no account whose plan does. A turn that waits for anything else waits on.
    pub fn end_waiting(&mut self, now_ms: i64) -> Result<Outputs, ActorError> {
        let mut out = Outputs::default();
        let waits = match self.recorder.agent().live() {
            Some(Turn::Parked(parked)) if parked.wait.is_for_an_account() => {
                Some((parked.id, parked.kind.clone(), parked.reason.clone()))
            }
            _ => None,
        };
        let Some((turn, kind, reason)) = waits else {
            return Ok(out);
        };
        let model = self.turn_model().unwrap_or_default();
        let Some(live) = self.live.as_mut().filter(|live| live.turn == turn) else {
            return Ok(out);
        };
        let mut batch = self.recorder.batch();
        // What is said of the end, as of an attempt that failed and is not tried again.
        let told = failure_marker(Failure {
            turn,
            message_id: &live.message_id,
            origin: &live.origin,
            kind: &kind,
            reason: &reason,
            model: &model,
            trace_id: live.trace_id.clone(),
        });
        if batch.command(Command::WaitOver { turn }, now_ms).is_err() {
            return Ok(out);
        }
        if let Some(notice) = told {
            if closed_by_marker(&live.last_row, &live.message_id) {
                live.last_row = notice.id().to_string();
            }
            batch.item(written(&notice, now_ms)?);
        }
        if let Some(end) = closing(&batch, turn, &live.last_row, live.facts()) {
            cut_off(&mut batch, &mut live.calls, &end, now_ms)?;
            batch.item(written(&end, now_ms)?);
        }
        let effects = batch.commit()?.effects;
        self.unstored_changes = 0;
        out.changed.conversation = true;
        out.changed.status = true;
        out.changed.queue = true;
        self.forget_live();
        self.carry_out(effects, &mut out)?;
        Ok(out)
    }

    /// The login of the agent's tool has changed: a turn that waits for an account is tried
    /// on the one that is there now. A turn that waits for anything else goes on waiting.
    pub fn login_changed(&mut self, now_ms: i64) -> Result<Outputs, ActorError> {
        match self.recorder.agent().live() {
            Some(Turn::Parked(parked)) if parked.wait.is_for_an_account() => self.retry(now_ms),
            _ => Ok(Outputs::default()),
        }
    }

    /// Starts the next attempt of the turn that waits for one.
    fn retry(&mut self, now_ms: i64) -> Result<Outputs, ActorError> {
        let mut out = Outputs::default();
        let Some(Turn::Parked(parked)) = self.recorder.agent().live() else {
            return Ok(out);
        };
        let turn = parked.id;
        let mut batch = self.recorder.batch();
        if batch
            .command(Command::TurnRetried { turn }, now_ms)
            .is_err()
        {
            return Ok(out);
        }
        // The next attempt has a spool of its own, read from its beginning.
        batch.cursor(turn, 0, now_ms);
        let effects = batch.commit()?.effects;
        out.changed.status = true;
        self.carry_out(effects, &mut out)?;
        Ok(out)
    }

    /// Reads what is new in the live turn's spool and writes what it means to the log.
    fn pump(&mut self, out: &mut Outputs, now_ms: i64) -> Result<(), ActorError> {
        loop {
            // The turn is shown from before its first record is read, and its end after its
            // last one was.
            self.show_turn(now_ms);
            let Some(live) = self.live.as_mut().filter(|live| !live.over) else {
                return Ok(());
            };
            let turn = live.turn;
            let spool = self.profile.spool_path_of(turn, live.attempt);
            let read = SpoolReader::new(&spool)
                .read_window(live.offset, u64::MAX, WINDOW)
                .map_err(|error| io_error(&spool, error))?;
            if read.records.is_empty() && read.damaged.is_empty() {
                return Ok(());
            }
            out.damaged_lines += read.damaged.len();

            let model = live
                .managed_model
                .clone()
                .or_else(|| self.profile.model.clone())
                .unwrap_or_default();
            let accounts = self.profile.accounts;
            let already_refused = self.recorder.agent().refused_live_binding();
            let mut batch = self.recorder.batch();
            let mut signals = Vec::new();
            let mut shown: Vec<String> = Vec::new();
            let mut aloud: Vec<Spoken> = Vec::new();
            // Where this reading stops before the end of what was read: after the record in
            // which the turn moved on to something else.
            let mut stopped_at: Option<u64> = None;
            for (place, record) in read.records.iter().enumerate() {
                let t = record.t;
                let doing = told_activity(batch.agent());
                // What the shim says about the process means the same for every backend. A
                // command the agent refuses is about a turn that is no longer live: late news.
                match &record.entry {
                    Line::Shim(ShimEvent::Spawned { .. }) => {
                        let _ = batch.command(Command::TurnStarted { turn }, t);
                    }
                    Line::Shim(ShimEvent::SpawnFailed { error }) => {
                        let reason = error.clone();
                        let stopping = batch.agent().stopping().is_some();
                        if live.opening && !stopping {
                            let park = Command::TurnParked {
                                turn,
                                wait: Wait::Retry {
                                    until_ms: t.saturating_add(1_000),
                                },
                                kind: failures::NOT_STARTED.into(),
                                reason: reason.clone(),
                            };
                            if batch.command(park, t).is_ok() {
                                live.over = true;
                                continue;
                            }
                        }
                        let failed = batch
                            .command(Command::StartFailed { turn, reason }, t)
                            .is_ok();
                        if failed
                            && !stopping
                            && let Some(marker) = failure_marker(Failure {
                                turn,
                                message_id: &live.message_id,
                                origin: &live.origin,
                                kind: failures::NOT_STARTED,
                                reason: error,
                                model: &model,
                                trace_id: live.trace_id.clone(),
                            })
                        {
                            batch.item(written(&marker, t)?);
                        }
                        if failed
                            && let Some(end) = closing(&batch, turn, &live.last_row, live.facts())
                        {
                            batch.item(written(&end, t)?);
                        }
                        live.over = true;
                    }
                    Line::Shim(ShimEvent::Exited { .. }) => live.over = true,
                    _ => {}
                }
                live.interpreter.on_record(record, &mut signals);
                for signal in signals.drain(..) {
                    let piece = match signal {
                        Signal::Bound { backend_session } => {
                            // Another conversation than the agent had, of a tool that has
                            // that one no more: the Host carried the conversation over.
                            let had = batch.agent().backend_session().map(str::to_string);
                            let command = Command::TurnBound {
                                turn,
                                backend_session,
                            };
                            if batch.command(command, t).is_ok()
                                && let Some(bound) = batch.agent().backend_session()
                                && let Some(had) =
                                    had.filter(|had| had != bound && !self.backend.keeps(had))
                            {
                                out.carried.push((had, bound.to_owned()));
                            }
                            continue;
                        }
                        Signal::Moved(activity) => {
                            let _ = batch.command(Command::TurnMoved { turn, activity }, t);
                            continue;
                        }
                        Signal::Ended(outcome) => {
                            // An attempt that was stopped for its account did not fail: the
                            // turn waits for an account, and the pool is told that nothing
                            // of it runs. One that completed all the same is the turn's end.
                            if live.for_account
                                && outcome != RunOutcome::Completed
                                && batch.agent().stopping().is_none()
                            {
                                let most = recovery::ACCOUNT_WAIT_MOST_MS + POOL_MARGIN_MS;
                                let parked = Command::TurnParked {
                                    turn,
                                    wait: Wait::Account { until_ms: t + most },
                                    kind: failures::USAGE_LIMIT.to_string(),
                                    reason: STOPPED_FOR_AN_ACCOUNT.to_string(),
                                };
                                if batch.command(parked, t).is_ok() {
                                    live.for_account = false;
                                    live.drafts.clear();
                                    live.tools.clear();
                                    live.calls.clear();
                                    let events = self.shown.settle("interrupted", t);
                                    self.shown_events.extend(events);
                                    self.stopped_for_pool = true;
                                    continue;
                                }
                            }
                            // An attempt that failed on something that passes is not the
                            // turn's end: the turn waits, and is tried again.
                            if let RunOutcome::Failed { reason } = &outcome
                                && batch.agent().stopping().is_none()
                            {
                                let kind = failures::classify_error(reason);
                                let waits = batch.agent().live().map(Turn::account_waits);
                                let waits = waits.unwrap_or_default();
                                let wait = match recovery::after_failed_attempt(
                                    recovery::Failed {
                                        kind,
                                        attempt: live.attempt,
                                        account_waits: waits.count,
                                        account_wait_since_ms: waits.since_ms,
                                        now_ms: t,
                                    },
                                    accounts,
                                ) {
                                    recovery::Next::Retry { after_ms } => Some(Wait::Retry {
                                        until_ms: t + after_ms,
                                    }),
                                    recovery::Next::Account { until_ms } => {
                                        Some(Wait::Account { until_ms })
                                    }
                                    recovery::Next::Pool { plan } => {
                                        let pool = self.pool.as_ref();
                                        let waits =
                                            pool.is_some_and(|pool| pool.failed(turn, plan, t));
                                        let most = recovery::ACCOUNT_WAIT_MOST_MS + POOL_MARGIN_MS;
                                        waits.then_some(Wait::Account { until_ms: t + most })
                                    }
                                    recovery::Next::Fail => None,
                                };
                                if let Some(wait) = wait {
                                    let parked = Command::TurnParked {
                                        turn,
                                        wait,
                                        kind: kind.to_string(),
                                        reason: reason.clone(),
                                    };
                                    if batch.command(parked, t).is_ok() {
                                        // What the attempt was writing when it failed is
                                        // not said: the next one says it.
                                        live.drafts.clear();
                                        live.tools.clear();
                                        live.calls.clear();
                                        // Nor does anything of it still run for those who
                                        // watch.
                                        let events = self.shown.settle("interrupted", t);
                                        self.shown_events.extend(events);
                                        continue;
                                    }
                                }
                            }
                            // What is said of the end, and whether that row closes the turn.
                            let told = match &outcome {
                                // It went because it was told to: nothing to explain.
                                _ if batch.agent().stopping().is_some() => None,
                                RunOutcome::Failed { reason } => failure_marker(Failure {
                                    turn,
                                    message_id: &live.message_id,
                                    origin: &live.origin,
                                    kind: failures::classify_error(reason),
                                    reason,
                                    model: &model,
                                    trace_id: live.trace_id.clone(),
                                })
                                .map(|marker| {
                                    let closes = closed_by_marker(&live.last_row, &live.message_id);
                                    (marker, closes)
                                }),
                                RunOutcome::Interrupted { reason } => {
                                    let name = turn_name(&live.message_id);
                                    let trace = live.trace_id.clone();
                                    let code = "turn-interrupted";
                                    Some((ending_notice(turn, name, code, reason, trace), true))
                                }
                                RunOutcome::Completed | RunOutcome::Stopped { .. } => None,
                            };
                            let ended = batch
                                .command(Command::TurnEnded { turn, outcome }, t)
                                .is_ok();
                            if let (true, Some((notice, closing_row))) = (ended, told) {
                                if closing_row {
                                    live.last_row = notice.id().to_string();
                                }
                                batch.item(written(&notice, t)?);
                            }
                            if ended
                                && let Some(end) =
                                    closing(&batch, turn, &live.last_row, live.facts())
                            {
                                cut_off(&mut batch, &mut live.calls, &end, t)?;
                                batch.item(written(&end, t)?);
                            }
                            continue;
                        }
                        Signal::Live(reply) => {
                            if !shown.contains(&reply.id) {
                                shown.push(reply.id.clone());
                            }
                            show(&mut live.drafts, turn, &live.origin, reply, t);
                            continue;
                        }
                        Signal::Glimpse(glimpse) => {
                            // Of the turn that is shown, and of no other: what a turn that
                            // is over still says is not given to the one after it.
                            if let Some((_, name)) =
                                self.shown_turn.as_ref().filter(|(held, _)| *held == turn)
                            {
                                let events = self.shown.show(&glimpse, Some(name), t);
                                self.shown_events.extend(events);
                            }
                            continue;
                        }
                        Signal::Model(named) => {
                            live.model = Some(named);
                            continue;
                        }
                        Signal::Usage {
                            tokens_in,
                            tokens_out,
                        } => {
                            live.tokens = Some((tokens_in, tokens_out));
                            continue;
                        }
                        Signal::Total {
                            tokens_in,
                            tokens_out,
                        } => {
                            live.total = Some((tokens_in, tokens_out));
                            continue;
                        }
                        Signal::Context { tokens } => {
                            live.context = Some(tokens);
                            continue;
                        }
                        Signal::Limits(said) => {
                            live.limits = Some((t, said));
                            continue;
                        }
                        Signal::TurnCost { micros } => {
                            live.per_turn_cost = Some(micros);
                            continue;
                        }
                        Signal::Cost { micros } => {
                            live.cost = Some(micros);
                            continue;
                        }
                        Signal::Reply(mut reply) => {
                            // The finished reply takes the place of the one being written.
                            live.drafts.retain(|draft| draft.id != reply.id);
                            reply.trace_id = live.trace_id.clone();
                            reply.origin = live.origin.clone();
                            reply.text = kept(std::mem::take(&mut reply.text), &reply.origin);
                            // The tools it calls are work after what the turn said before.
                            if !reply.tools.is_empty() {
                                follow(&mut batch, &mut live.said, turn, t)?;
                            }
                            if !reply.text.trim().is_empty() {
                                live.said.push(reply.id.clone());
                            }
                            // What the reply marks for the ear waits to be spoken from the
                            // moment the reply is in the conversation.
                            if live.speak {
                                let trace = live.trace_id.as_deref();
                                for line in lines_of(&reply.id, turn.0, &reply.text, trace, t) {
                                    batch.item(record_item(kind::LINE, line.id.clone(), &line, t)?);
                                    aloud.push(line);
                                }
                            }
                            Entry::Reply(reply)
                        }
                        Signal::Tool(mut call) => {
                            call.origin = live.origin.clone();
                            call.trace_id = live.trace_id.clone();
                            // A call no message carries is a row, and work, when it begins.
                            let known = live.tools.iter().any(|tool| tool.id == call.id);
                            if call.reply.is_none()
                                && (call.status == ToolStatus::Running || !known)
                            {
                                follow(&mut batch, &mut live.said, turn, t)?;
                            }
                            follow_tool(&mut live.tools, &mut live.calls, &call);
                            Entry::Tool(call)
                        }
                        Signal::Notice(notice) => Entry::Notice(notice),
                    };
                    // A part of a row is no row: the row it is a part of stays the last.
                    if piece.part_of().is_none() {
                        live.last_row = piece.id().to_string();
                    }
                    batch.item(written(&piece, t)?);
                }
                // The turn moved on to something else: what was read up to here is written
                // down by itself, and how the agent stood is noted, before what came after.
                if !live.over && told_activity(batch.agent()) != doing {
                    stopped_at = Some(read.ends.get(place).copied().unwrap_or(read.next_offset));
                    break;
                }
            }
            let read_until = stopped_at.unwrap_or(read.next_offset);
            if live.over
                && batch
                    .agent()
                    .live()
                    .is_some_and(|held| held.id() == turn && held.has_process())
            {
                // The process is gone and its backend did not say how the turn went.
                let reason = "the tool ended and nothing said how";
                let stopping = batch.agent().stopping().is_some();
                let outcome = RunOutcome::Interrupted {
                    reason: reason.to_string(),
                };
                let ended = batch
                    .command(Command::TurnEnded { turn, outcome }, now_ms)
                    .is_ok();
                if ended && !stopping {
                    let notice = ending_notice(
                        turn,
                        turn_name(&live.message_id),
                        "turn-interrupted",
                        reason,
                        live.trace_id.clone(),
                    );
                    live.last_row = notice.id().to_string();
                    batch.item(written(&notice, now_ms)?);
                }
                if ended && let Some(end) = closing(&batch, turn, &live.last_row, live.facts()) {
                    cut_off(&mut batch, &mut live.calls, &end, now_ms)?;
                    batch.item(written(&end, now_ms)?);
                }
            }
            let wrote = !batch.is_empty();
            if wrote {
                batch.cursor(turn, read_until, now_ms);
            }
            let committed = batch.commit()?;
            if !already_refused && self.recorder.agent().refused_live_binding() {
                clarp_backend::past::report_refused_session(self.backend.id());
            }
            live.offset = read_until;
            for line in &aloud {
                self.speech.apply(line);
                out.changed.speech = true;
            }
            if wrote {
                self.unstored_changes = 0;
                out.changed.conversation = true;
                out.changed.status |= !committed.events.is_empty();
            }
            let base = self.recorder.last_seq() * REVISION_STEP;
            for draft in &mut live.drafts {
                if shown.contains(&draft.id) {
                    self.unstored_changes += 1;
                    draft.revision = base + self.unstored_changes.min(REVISION_STEP - 1);
                    self.shown_revision = draft.revision;
                    out.changed.conversation = true;
                }
            }
            // The turn is over when its attempt is and it does not wait for another.
            if live.over && self.recorder.agent().live().map(Turn::id) != Some(turn) {
                self.forget_live();
            }
            self.carry_out(committed.effects, out)?;
            // That nothing of the turn runs any more is in the log: the pool is told.
            if std::mem::take(&mut self.stopped_for_pool)
                && let Some(pool) = &self.pool
            {
                pool.stopped(now_ms);
            }
            if let Some(until) = stopped_at {
                // What could not be read after that point is found again by the next reading.
                let again = read.damaged.iter().filter(|line| line.offset >= until);
                out.damaged_lines = out.damaged_lines.saturating_sub(again.count());
                out.passed.push(self.status());
            }
        }
    }
}

/// What an app is told the turn that holds the agent is doing: a turn that has not started
/// yet, or waits between two attempts, is told as thinking.
fn told_activity(agent: &clarp_kernel::agent::Agent) -> Option<Activity> {
    match agent.live() {
        Some(Turn::Running(turn)) => Some(turn.activity.clone()),
        Some(_) => Some(Activity::Thinking),
        None => None,
    }
}
