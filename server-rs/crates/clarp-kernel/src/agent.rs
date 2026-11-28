//! An agent as the kernel sees it: a mailbox, at most one live turn, and the rules between them.
//!
//! This is the pure core of the actor. [`Agent::decide`] takes a command and says what happens:
//! the events to append to the agent's log and the effects to carry out once they are durable.
//! It changes nothing. [`Agent::apply`] is the only way the state changes, and it is driven by
//! events alone, so the state after a restart (a replay of the log) is the state before it.
//!
//! The rules it holds:
//!
//! 1. An accepted message becomes exactly one turn, and that turn starts at most once.
//! 2. An agent runs one turn at a time. The others wait in the mailbox, oldest first.
//! 3. A Stop ends the live turn and holds the mailbox. Nothing in the mailbox is forgotten: a
//!    resume lets it run again in order. (On 15 Nov 2026 the Python Host kept two of the owner's
//!    messages held after a Stop and a resume, and counted them as work in progress, which
//!    suppressed the agent's own wakes for three hours.)
//! 4. A direct message is the user saying "this, now". It ends the live turn, goes ahead of what
//!    waits its turn, and runs even while the mailbox is held, without lifting the hold. It never
//!    ends a turn that has not started: two direct messages in a row both run, in order.

use std::collections::{BTreeMap, BTreeSet, VecDeque};

use serde::{Deserialize, Serialize};

use crate::compaction::LastCompaction;
use crate::control::Control;
use crate::steering::{FollowUp, Gathered, Join, Stage};

use crate::turn::{
    Activity, Admission, COMPACTION, IllegalTransition, Origin, Outcome, Parked, Queued,
    RunOutcome, Settled, StartOutcome, Turn, TurnEvent, TurnId, Wait,
};

/// Who let a mailbox go that held nothing any more: the last thing that waited left to run.
pub const DRAINED: &str = "nothing waits";
/// The actor recorded on the mailbox hold in an agent import.
pub const IMPORT: &str = "move";

/// How many turns in a row may take a message of the user's into their prompt and end without
/// their tool having done anything for it ([`gives_back`]): the last of them ends it rather than
/// give it back once more.
pub const TAKEN_FOR_NOTHING: u32 = 3;

/// Who stopped a turn that a direct message replaced.
pub fn preempted_by(turn: TurnId) -> String {
    format!("message of turn {}", turn.0)
}

/// Whether a turn's Stop came from a newer message that took its place ([`preempted_by`]).
fn preempted(by: &str) -> bool {
    by.starts_with("message of turn ")
}

/// Whether a turn that ended so gives back the user's messages it took into its prompt, when
/// its tool had done nothing for them yet (no words of an answer, no tool: what a retry asks
/// of a turn, `nothing_done`): the user did not give up on them. A turn that had begun its work
/// ends them with it, whoever stopped it, so that nothing it did (a send, a commit) is done twice. Anyone's Stop but the
/// user's own, a newer message that took the turn's place, and a process lost under it
/// (Maximus's ruling of 23 Nov 2026: nothing the user wrote ends unanswered because someone
/// else stopped the turn). The user's own Stop ends them with the turn, as it ends a message
/// steered into it.
fn gives_back(outcome: &Outcome) -> bool {
    match outcome {
        Outcome::Interrupted { .. } => true,
        Outcome::Stopped { by, .. } => {
            preempted(by) || outcome.stopped_by().is_some_and(|by| !by.is_the_users())
        }
        _ => false,
    }
}

/// Whether a waiting message may be read into the live turn: the user's own words, said to
/// the agent, or a front desk's relay of them.
fn joins_live(waiting: &Queued) -> bool {
    match waiting.admission {
        Admission::Queue => waiting.origin.is_user_send(),
        Admission::Later => waiting.user_word && matches!(waiting.origin, Origin::Agent { .. }),
        _ => false,
    }
}

/// What can be asked of an agent, by a client or by the shim that runs its turn.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Command {
    /// Set or clear the durable fence on new work.
    SetMoved {
        to: Option<crate::agent_moves::Target>,
    },
    /// The actor knows this live slot never reached the tool.
    HoldUnsentForMove {
        turn: TurnId,
    },
    /// A continuation that may have reached the tool cannot be replayed here.
    InterruptMovedRetry {
        turn: TurnId,
    },
    /// A message was written to the log at `message_seq` and wants a turn.
    Accept {
        message_seq: u64,
        origin: Origin,
        admission: Admission,
        /// Another agent addresses this one: its id ("" for a peer Host's agent). `None`: the
        /// user's ([`TurnEvent::Accepted`]).
        coordination: Option<String>,
        /// The Host verified a designated front desk or a card answered by its user.
        user_word: bool,
    },
    /// Reserve a queued message for input to the live turn.
    Join {
        turn: TurnId,
        join: Join,
    },
    /// The live process echoed this follow-up.
    JoinRead {
        turn: TurnId,
    },
    /// An answer settled this follow-up, or the process ended after reading it.
    JoinSettled {
        turn: TurnId,
    },
    /// Take a waiting message back.
    Withdraw {
        turn: TurnId,
        by: String,
    },
    /// Let one waiting message run now, whatever holds the mailbox: its user sends it by
    /// hand. Only when no turn runs.
    SendNow {
        turn: TurnId,
        by: String,
    },
    /// The owner's waiting message takes over (issue 867): the live turn ends as stopped for
    /// it, as for a newer message of the owner's said direct (`owner_preempts`), and it runs
    /// the moment that turn is over, ahead of everything, held by nothing. Nothing is paused.
    /// Only while a turn runs: with none, the message is sent as any other.
    TakeOver {
        turn: TurnId,
        by: String,
    },
    /// The words of a waiting message were written anew, at `message_seq`: its turn answers
    /// those. It keeps its place among what waits.
    Reword {
        turn: TurnId,
        message_seq: u64,
    },
    /// End the live turn and hold the mailbox: who asks it, and why.
    Stop {
        by: Control,
    },
    /// A non-deferred Stop that lets the user's already waiting messages run.
    StopAndStartWaiting {
        by: Control,
    },
    /// Someone looked for a turn of the agent that nothing runs, and gives it back: nothing is
    /// ended and nothing is held or let go. What is kept is a note of who and why, on the state.
    Repair {
        by: Control,
    },
    /// Let the mailbox run again.
    Resume {
        by: String,
    },
    /// Run the first eligible message after releasing a transient launch fence.
    RunWaiting,
    ExpectTransitCheckpoint {
        checkpoint: TransitCheckpoint,
        recorded_work: bool,
    },
    ParkTransitCheckpoint {
        checkpoint: TransitCheckpoint,
    },
    HoldUnsentForTransit {
        turn: TurnId,
    },
    /// The agent says, in a few words, what it is busy with beyond its turns: work it left
    /// running. An empty label takes that back.
    Label {
        text: String,
    },
    /// A label that outlived its time is taken back, if it is still the one that was set at
    /// `set_at_ms`: one set again since stands.
    RetireLabel {
        set_at_ms: i64,
    },
    /// The shim: the tool process is up.
    TurnStarted {
        turn: TurnId,
    },
    /// The shim: the tool process could not be started.
    StartFailed {
        turn: TurnId,
        reason: String,
    },
    /// The shim: the tool named its own session.
    TurnBound {
        turn: TurnId,
        backend_session: String,
    },
    /// The shim: the tool is doing something else now.
    TurnMoved {
        turn: TurnId,
        activity: Activity,
    },
    /// The shim: the tool process is gone, and this is how it went.
    TurnEnded {
        turn: TurnId,
        outcome: RunOutcome,
    },
    /// Record who is about to ask a shim to hold, before it can send SIGSTOP.
    OfferTakeover {
        from: TurnId,
        into: TurnId,
        attempt: u32,
    },
    /// Reserve the next user's turn before asking the shim whether its hold can be consumed.
    SelectTakeover {
        selected: crate::takeover::Selection,
    },
    /// The held tool was not killed for takeover. Leave the next message in the queue.
    ClearTakeover {
        from: TurnId,
    },
    /// The shim durably marked the selected attempt and ended its tool.
    FinishTakeover {
        from: TurnId,
    },
    /// The tool process is gone on a failure that does not end the turn: it waits for its
    /// next attempt. `kind` and `reason` are the failure, read and in the tool's words.
    TurnParked {
        turn: TurnId,
        wait: Wait,
        kind: String,
        reason: String,
    },
    /// The wait is over: start the turn's next attempt.
    TurnRetried {
        turn: TurnId,
    },
    /// The turn waited for the network, which was gone, and it is back: start its next
    /// attempt. That attempt is no try against a bad line.
    NetworkBack {
        turn: TurnId,
    },
    /// The wait did not end well: the turn ends on the failure it waited on.
    WaitOver {
        turn: TurnId,
    },
    /// The turn waited for the network, and it did not come back in time: the turn ends on
    /// the failure it waited on, and says that the network was gone.
    NetworkGone {
        turn: TurnId,
    },
    /// The agent comes from another Host, with the tool's session it had there.
    Adopt {
        from: String,
        backend_session: Option<String>,
    },
    /// The agent begins with a session its tool kept on this machine: its first turn
    /// continues it. Only before its first turn.
    TakeUp {
        backend_session: String,
    },
    /// The agent goes on with another tool, `backend`, after `from`. Its conversation was
    /// written as a session of that tool, which its next turn continues; or, where none was
    /// (`None`), its next turn begins one that opens with the Host's record of it. Only
    /// while no turn runs and any waiting messages are fenced by Transit.
    ChangeTool {
        from: String,
        backend: String,
        selection: Option<ToolSelection>,
        backend_session: Option<String>,
        /// Where there is no session: what its next turn opens with, the record of the
        /// conversation as it was carried. `None`: the record is read when the turn begins.
        opening: Option<String>,
    },
    /// The agent is started again: its next turn begins a conversation with its tool
    /// instead of continuing the one it has. Only when no turn runs: a turn that runs is
    /// stopped first.
    StartAgain,
    /// What the user's compaction did, said once its turn has ended (`compaction`): only for
    /// that turn, and only once.
    RecordCompaction {
        turn: TurnId,
        said: LastCompaction,
    },
}

/// Launch settings recorded with the backend switch for restart recovery.
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct ToolSelection {
    pub model: String,
    pub effort: String,
}

/// Associates a shim checkpoint with the exact source attempt and its held spool offset.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct TransitCheckpoint {
    pub turn: TurnId,
    pub attempt: u32,
    pub fence: u64,
}

pub const TRANSIT_CHECKPOINT: &str = "transit_checkpoint";

/// The first attempt to run on the target backend after a committed handoff.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TransitAttempt {
    turn: TurnId,
    attempt: u32,
}

/// What happened to an agent. These, in order, are its history.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "agent", rename_all = "kebab-case")]
pub enum AgentEvent {
    MoveMarked {
        mark: Option<crate::agent_moves::Mark>,
    },
    TransitCheckpointExpected {
        checkpoint: TransitCheckpoint,
    },
    HeldUnsentForMove {
        turn: TurnId,
    },
    Turn {
        turn: TurnId,
        change: TurnEvent,
    },
    TakeoverOffered {
        from: TurnId,
        into: TurnId,
        attempt: u32,
    },
    TakeoverSelected {
        selected: crate::takeover::Selection,
    },
    TakeoverCleared {
        from: TurnId,
    },
    Joined {
        turn: TurnId,
        join: Join,
    },
    JoinRead {
        turn: TurnId,
        at_ms: i64,
    },
    JoinSettled {
        turn: TurnId,
    },
    /// Someone asked the live turn to stop. It settles when the tool process is gone. Whether
    /// the Host knew who asked, and why, are absent from a log written before they were kept.
    StopRequested {
        turn: TurnId,
        by: String,
        #[serde(default, skip_serializing_if = "is_false")]
        verified: bool,
        #[serde(default, skip_serializing_if = "String::is_empty")]
        reason: String,
        at_ms: i64,
    },
    /// Someone repaired the agent: a note of who and why, kept on its state until the state
    /// changes. `released`: the turn that was given back, none on this Host.
    Repaired {
        by: String,
        verified: bool,
        reason: String,
        released: String,
        at_ms: i64,
    },
    /// The agent was stopped, or its mailbox held: who did it, whether the Host knew it was
    /// they, and why (`Control`), written only where they say something, as `StopRequested`.
    /// The Stop of an idle agent leaves no other line, so this is where a reader asks whose
    /// Stop it was (`Control::is_the_users`).
    Paused {
        by: String,
        #[serde(default, skip_serializing_if = "is_false")]
        verified: bool,
        #[serde(default, skip_serializing_if = "String::is_empty")]
        reason: String,
        at_ms: i64,
    },
    /// A Stop lets these waiting turns run while the rest of the mailbox stays paused.
    StopReleasedWaiting {
        turns: Vec<TurnId>,
    },
    Resumed {
        by: String,
        at_ms: i64,
    },
    /// A waiting turn was selected to run next, by hand or by the Host's owner priority rule.
    SentNow {
        turn: TurnId,
        by: String,
        at_ms: i64,
    },
    /// The words of a waiting turn's message were written anew, at `message_seq`.
    Reworded {
        turn: TurnId,
        message_seq: u64,
        at_ms: i64,
    },
    /// The agent's own label for what it is busy with beyond its turns. Empty: none any more.
    Labelled {
        text: String,
        at_ms: i64,
    },
    /// The agent was taken over from another Host. Its next turn continues the tool's session
    /// it had there.
    Adopted {
        from: String,
        backend_session: Option<String>,
        at_ms: i64,
    },
    /// The agent began with a session its tool kept on this machine. Its first turn
    /// continues that session. It has worked nowhere yet: it is as new as an agent that
    /// begins with nothing.
    TookUp {
        backend_session: String,
        at_ms: i64,
    },
    /// The agent was started again. The tool's session it had is no longer continued, and
    /// its conversation, as it is read, begins anew after this.
    StartedAgain {
        at_ms: i64,
    },
    /// The agent goes on with another tool ([`Command::ChangeTool`]). Its conversation, as
    /// it is read, goes on: only the tool's session is another.
    ChangedTool {
        from: String,
        backend: String,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        selection: Option<ToolSelection>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        resume: Option<TransitAttempt>,
        backend_session: Option<String>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        opening: Option<String>,
        at_ms: i64,
    },
    /// What the user's compaction did, the turn it ran as having ended: what the agent's row
    /// says of its last compaction (`last_compaction`) until the next one.
    Compacted {
        turn: TurnId,
        said: LastCompaction,
    },
}

/// What the actor must do after the events are in the log.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Effect {
    StartTurn {
        turn: TurnId,
        message_seq: u64,
    },
    /// Start the tool again for a turn that has run before: its attempt number `attempt`.
    RetryTurn {
        turn: TurnId,
        message_seq: u64,
        attempt: u32,
    },
    StopTurn {
        turn: TurnId,
    },
}

/// The answer to a command: what to record, then what to do.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Decision {
    pub events: Vec<AgentEvent>,
    pub effects: Vec<Effect>,
}

/// Why a command changes nothing.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Refusal {
    Moved(crate::agent_moves::Mark),
    AlreadyMoved(crate::agent_moves::Mark),
    UnsentNotProven(TurnId),
    /// The turn is not waiting in the mailbox, so it cannot be withdrawn.
    NotWaiting(TurnId),
    /// A signal about a turn that is not the live one: late, or from a shim we no longer follow.
    NotLive(TurnId),
    /// The turn does not wait between two attempts.
    NotParked(TurnId),
    /// The turn waits between two attempts, but not for the network.
    NotOffline(TurnId),
    /// A resume for an agent that is not held.
    NotPaused,
    /// A turn of the agent runs: nothing that waits is sent by hand past it.
    AtWork,
    /// An agent that has run a turn here is not one that just arrived.
    AlreadyAtWork,
    /// The turn is not a compaction that ended and whose outcome is still owed.
    NoCompactionOwed(TurnId),
    /// The live turn or its next waiting user message changed during takeover.
    TakeoverStale,
    /// Messages wait for the agent: what changes under them would answer them otherwise.
    Waiting,
    /// Only a message of the owner's own (the user's, or the Oracle's for the user) takes over
    /// the live turn.
    NotTheOwners(TurnId),
    /// The live turn is not one a waiting message may cut: a native goal's own turn (a Stop
    /// pauses its goal first), or the tool's compaction or its summary for a move.
    Uncuttable,
    /// No turn runs: there is nothing to take over.
    NoTurn,
}

impl std::fmt::Display for Refusal {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Moved(_) => f.write_str(crate::agent_moves::MOVED),
            Self::AlreadyMoved(_) => f.write_str("already_moved"),
            Self::UnsentNotProven(turn) => {
                write!(f, "turn {} may already have reached the tool", turn.0)
            }
            Self::NotWaiting(turn) => write!(f, "turn {} is not waiting", turn.0),
            Self::NotLive(turn) => write!(f, "turn {} is not the live turn", turn.0),
            Self::NotParked(turn) => write!(f, "turn {} does not wait for an attempt", turn.0),
            Self::NotOffline(turn) => write!(f, "turn {} does not wait for the network", turn.0),
            Self::NotPaused => write!(f, "the agent is not paused"),
            Self::AtWork => write!(f, "a turn of the agent runs"),
            Self::AlreadyAtWork => write!(f, "the agent has already worked on this Host"),
            Self::NoCompactionOwed(turn) => {
                write!(f, "turn {} is no compaction whose outcome is owed", turn.0)
            }
            Self::TakeoverStale => write!(f, "the selected turn can no longer be taken over"),
            Self::Waiting => write!(f, "messages wait for the agent"),
            Self::NotTheOwners(turn) => write!(f, "turn {} is not the owner's message", turn.0),
            Self::Uncuttable => write!(f, "the live turn cannot be cut by a waiting message"),
            Self::NoTurn => write!(f, "no turn runs"),
        }
    }
}

impl std::error::Error for Refusal {}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Agent {
    latest_turn_start: Option<i64>,
    latest_turn_activity: Option<i64>,
    next_turn: u64,
    mailbox: VecDeque<Queued>,
    joins: BTreeMap<TurnId, FollowUp>,
    /// The user's messages each turn took into its own prompt with the message it began for,
    /// in the order they were read, until that turn ends. Unlike the joins, they outlast an
    /// attempt: every attempt of the turn is given all of them again.
    gathered: BTreeMap<TurnId, Vec<Gathered>>,
    /// The turn that holds the agent: starting or running, never anything else.
    live: Option<Turn>,
    /// Who asked the live turn to stop and why, while it has not gone yet.
    stopping: Option<Control>,
    stopping_since_ms: Option<i64>,
    /// Who last repaired the agent, why, and when: while nothing about its state changed since.
    note: Option<Note>,
    paused: bool,
    /// The destination of a move starts no turn until the handoff resumes it.
    import_held: bool,
    /// First turn number after the most recent explicit user Stop.
    user_stop_floor: Option<u64>,
    moved: Option<crate::agent_moves::Mark>,
    stop_generation: u64,
    settled: u64,
    /// How often what waits, or whether it is held, has changed. A message that was said and
    /// starts at once never waited and does not count: this is the number a client orders
    /// queue news by.
    queue_revision: u64,
    /// The turns that went through the queue: each counts once when it joins and once when it
    /// leaves. A turn that had to wait is one, and so is every message that was left for
    /// later, even when nothing is in its way and it leaves in the same breath.
    waited: BTreeSet<TurnId>,
    /// The live turn came through the queue.
    live_from_queue: bool,
    /// Confirmed pre-spawn failures of a queued handoff, rebuilt from its parked events.
    handoff_launch_failures: u32,
    live_takeover: Option<crate::takeover::Start>,
    /// Whether the live turn started with an existing provider session to continue.
    live_resumed_session: bool,
    /// The live turn's tool has begun its work, in any attempt of it: it wrote words of its
    /// answer or used a tool. Such a turn is not given again from nothing.
    live_worked: bool,
    /// The live turn carries the user's word: a front desk's relay of it, or the user's own
    /// message given back. Such a turn is the user's, whoever sent it.
    live_user_word: bool,
    selected_takeover: Option<crate::takeover::Selection>,
    /// The owner's waiting message that took over the live turn (issue 867), and who asked:
    /// sent by hand while that turn ran. When the turn is over it is sent by hand once more,
    /// so that it runs next, ahead of what the cut turn gives back. A Stop after it holds it
    /// with the rest, and ends this.
    taking_over: Option<(TurnId, String)>,
    /// The waiting turns a hold does not hold: they came after it was placed. The next stop
    /// holds them with the rest.
    unheld: BTreeSet<TurnId>,
    /// The user's waiting sends released by Stop, until they start or are read.
    stop_released: BTreeSet<TurnId>,
    /// The last turn that held the agent and ended.
    last: Option<Settled>,
    /// The last turn carried the user's word (`live_user_word` while it ran).
    last_user_word: bool,
    /// The turns put back in the queue unsent (`HeldUnsentForMove`) that carried the user's
    /// word when they were live: they still carry it when they end. The queued flag that
    /// orders them is not raised again, so where they wait and what they may cut is as before.
    /// A turn leaves this set when it ends or is read into another turn.
    held_user_word: BTreeSet<TurnId>,
    interrupted_since_ms: Option<i64>,
    /// The user's compaction whose turn ended and whose outcome is not said yet: how it ended.
    compaction_owed: Option<Settled>,
    /// What the user's last compaction did.
    last_compaction: Option<LastCompaction>,
    /// The tool's own session, as the tool last named it: where the next turn continues.
    backend_session: Option<String>,
    refused_live_binding: bool,
    /// What the agent says it is busy with beyond its turns. Empty: nothing.
    label: String,
    /// When it said so, while it says so.
    label_at: Option<i64>,
    /// What the agent rests in since it last said so, until its next turn starts.
    resting: Option<Rest>,
    /// The agent came from another Host, where it had worked.
    arrived: bool,
    /// The tool the agent had before it changed to the one it has, while it has no session
    /// of the new one: its next turn begins one with the Host's record of the conversation.
    carried_from: Option<String>,
    /// What that turn opens with, where the change said ([`Command::ChangeTool`]).
    carried_opening: Option<String>,
    /// The tool the agent's log last changed it from and to, since it was last started
    /// again: a record elsewhere that still says the first lost the change.
    changed: Option<(String, String, Option<ToolSelection>)>,
    transit_attempt: Option<TransitAttempt>,
    transit_checkpoint: Option<TransitCheckpoint>,
    /// When that change was made: what turns ended before it said was said of the tool it
    /// left (which model answered, how full its conversation was).
    changed_at_ms: Option<i64>,
    /// Where in the agent's own log the event being applied is: a send's revisions are taken
    /// from it. Set by whoever applies events that are in the log (the journal).
    at: u64,
    /// How many times the user stopped the agent: a send admitted before the last of them and
    /// not handed over since is shown stopped.
    user_stop_generation: u64,
    /// The user sends whose delivery can still change.
    deliveries: crate::delivery::Ledger,
}

/// What a repair left on the agent's state: who repaired it and why, the turn it gave back (none
/// on this Host), and when.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Note {
    pub by: Control,
    pub released: String,
    pub at_ms: i64,
}

#[allow(clippy::trivially_copy_pass_by_ref)]
fn is_false(value: &bool) -> bool {
    !*value
}

/// What an agent that runs no turn rests in, when it has said so itself.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Rest {
    /// Work it left running goes on in the background.
    Background,
    /// It took its label back: nothing goes on.
    Idle,
}

impl Default for Agent {
    fn default() -> Self {
        Self {
            latest_turn_start: None,
            latest_turn_activity: None,
            next_turn: 1,
            mailbox: VecDeque::new(),
            joins: BTreeMap::new(),
            gathered: BTreeMap::new(),
            live: None,
            stopping: None,
            stopping_since_ms: None,
            note: None,
            paused: false,
            import_held: false,
            user_stop_floor: None,
            moved: None,
            settled: 0,
            queue_revision: 0,
            stop_generation: 0,
            waited: BTreeSet::new(),
            live_from_queue: false,
            handoff_launch_failures: 0,
            live_takeover: None,
            live_resumed_session: false,
            live_worked: false,
            live_user_word: false,
            selected_takeover: None,
            taking_over: None,
            unheld: BTreeSet::new(),
            stop_released: BTreeSet::new(),
            last: None,
            last_user_word: false,
            held_user_word: BTreeSet::new(),
            interrupted_since_ms: None,
            compaction_owed: None,
            last_compaction: None,
            backend_session: None,
            refused_live_binding: false,
            label: String::new(),
            label_at: None,
            resting: None,
            arrived: false,
            carried_from: None,
            carried_opening: None,
            changed: None,
            transit_attempt: None,
            transit_checkpoint: None,
            changed_at_ms: None,
            at: 0,
            user_stop_generation: 0,
            deliveries: crate::delivery::Ledger::default(),
        }
    }
}

impl Agent {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn live(&self) -> Option<&Turn> {
        self.live.as_ref()
    }

    pub fn joins(&self) -> &BTreeMap<TurnId, FollowUp> {
        &self.joins
    }

    /// The user's messages this turn took into its own prompt, in order: none for a turn that
    /// took none, or that has ended.
    pub fn gathered(&self, turn: TurnId) -> &[Gathered] {
        self.gathered.get(&turn).map_or(&[], Vec::as_slice)
    }

    /// Whether a waiting message would go to the tool together with the user's messages
    /// beside it, once its turn comes: the user's own words, said to the agent (not left for
    /// later, and not another agent's), given to no tool before, held by no Stop, and not
    /// reserved for a process that runs. Whether one is the words of another agent the
    /// kernel does not know; the actor asks the log.
    pub fn goes_together(&self, queued: &Queued) -> bool {
        queued.admission == Admission::Queue
            && queued.origin.is_user_send()
            && queued.attempt <= 1
            && !self.holds(queued.id)
            && !self.joins.contains_key(&queued.id)
    }

    /// The waiting messages that go to the tool with the message of the turn that is being
    /// started, as [`Command::Join`] with [`Join::together`] takes them: those directly
    /// behind it in the mailbox, in order, newer than it, while each [`goes_together`]. The
    /// first that does not ends the run, and it and all after it keep their own turns.
    ///
    /// [`goes_together`]: Self::goes_together
    pub fn waiting_together(&self) -> Vec<TurnId> {
        let Some(Turn::Starting(starting)) = &self.live else {
            return Vec::new();
        };
        if starting.attempt > 1 || starting.ran_before || !starting.origin.is_user_send() {
            return Vec::new();
        }
        let mut newest = self
            .gathered(starting.id)
            .last()
            .map_or(starting.id, |last| last.turn);
        self.mailbox
            .iter()
            .take_while(|queued| {
                let next = queued.id > newest && self.goes_together(queued);
                newest = queued.id;
                next
            })
            .map(|queued| queued.id)
            .collect()
    }

    /// The position in the log of the event applied next.
    pub fn at(&mut self, position: u64) {
        self.at = position;
    }

    /// How many times the user stopped the agent.
    pub fn user_stop_generation(&self) -> u64 {
        self.user_stop_generation
    }

    /// The user sends whose delivery can still change.
    pub fn deliveries(&self) -> &crate::delivery::Ledger {
        &self.deliveries
    }

    /// The same, to take what changed or to fold the log quietly.
    pub fn deliveries_mut(&mut self) -> &mut crate::delivery::Ledger {
        &mut self.deliveries
    }

    pub fn waiting(&self) -> impl Iterator<Item = &Queued> {
        self.mailbox.iter()
    }

    pub fn import_held(&self) -> bool {
        self.import_held
    }

    pub fn is_paused(&self) -> bool {
        self.paused || self.moved.is_some()
    }

    pub fn moved(&self) -> Option<&crate::agent_moves::Mark> {
        self.moved.as_ref()
    }

    pub fn require_here(&self) -> Result<(), Refusal> {
        match self.moved() {
            Some(mark) => Err(Refusal::Moved(mark.clone())),
            None => Ok(()),
        }
    }

    fn unsent(&self, turn: TurnId) -> Option<Queued> {
        match self.live.as_ref()? {
            Turn::Starting(starting) if starting.id == turn && !starting.ran_before => {
                Some(Queued {
                    id: turn,
                    message_seq: starting.message_seq,
                    origin: starting.origin.clone(),
                    accepted_at_ms: starting.accepted_at_ms,
                    admission: Admission::Queue,
                    user_word: false,
                    attempt: starting.attempt.saturating_add(1),
                    given_back: 0,
                })
            }
            Turn::Parked(parked) if parked.id == turn && !parked.ran => Some(Queued {
                id: turn,
                message_seq: parked.message_seq,
                origin: parked.origin.clone(),
                accepted_at_ms: parked.accepted_at_ms,
                admission: Admission::Queue,
                user_word: false,
                attempt: parked.attempts.saturating_add(1),
                given_back: 0,
            }),
            _ => None,
        }
    }

    /// Maximum committed turn start, rebuilt by the same event fold on open.
    pub fn latest_turn_start(&self) -> i64 {
        self.latest_turn_start.unwrap_or(0)
    }

    /// Newest committed start or state change of a turn, including after replay.
    pub fn latest_turn_activity(&self) -> Option<i64> {
        self.latest_turn_activity
    }

    fn note_turn_activity(&mut self, at_ms: i64) {
        self.latest_turn_activity = Some(
            self.latest_turn_activity
                .map_or(at_ms, |old| old.max(at_ms)),
        );
    }

    /// How many turns have ended, however they ended.
    pub fn settled(&self) -> u64 {
        self.settled
    }

    /// How often the queue has changed: a message had to wait, a waiting one started or was
    /// taken back, the mailbox was held or let go.
    pub fn stop_generation(&self) -> u64 {
        self.stop_generation
    }

    pub fn queue_revision(&self) -> u64 {
        self.queue_revision
    }

    pub(crate) fn queue_item_shown(&mut self) {
        self.queue_revision += 1;
    }

    /// Whether the live turn came through the queue: it had to wait, or it was left for later.
    pub fn live_from_queue(&self) -> bool {
        self.live.is_some() && self.live_from_queue
    }

    /// The failure count while a queued turn has yet to reach its tool (R193).
    pub fn handoff_launch_failures(&self) -> Option<u32> {
        let never_ran = match self.live.as_ref()? {
            Turn::Starting(turn) => !turn.ran_before,
            Turn::Parked(turn) => !turn.ran,
            _ => false,
        };
        (self.live_from_queue && never_ran).then_some(self.handoff_launch_failures)
    }

    pub fn live_takeover(&self) -> Option<&crate::takeover::Start> {
        self.live_takeover.as_ref()
    }

    /// A fresh first turn can be replaced without discarding any prior provider history.
    pub fn fresh_first_turn(&self) -> bool {
        self.live_id() == Some(TurnId(1))
            && self.settled == 0
            && !self.arrived
            && !self.live_resumed_session
    }

    pub fn selected_takeover(&self) -> Option<&crate::takeover::Selection> {
        self.selected_takeover.as_ref()
    }

    /// The next turn must be the new user message. Preparation and commitment both use this
    /// rule so a Stop or a different queue head does not silently lose accepted work.
    pub fn can_take_over(&self, from: TurnId, into: TurnId, attempt: u32) -> bool {
        let (live, origin, live_attempt) = match &self.live {
            Some(Turn::Starting(turn)) => (turn.id, &turn.origin, turn.attempt),
            Some(Turn::Running(turn)) => (turn.id, &turn.origin, turn.attempt),
            _ => return false,
        };
        live == from
            && live_attempt == attempt
            && origin.is_user()
            && self.stopping.is_none()
            && self.live_takeover.is_none()
            && self.joins.is_empty()
            // What a turn took into its prompt is in no takeover's input: a turn of a later
            // attempt, whose joins are gone, keeps it all the same.
            && self.gathered(live).is_empty()
            && !self.paused
            && self.next_to_run().is_some_and(|next| {
                next.id == into && next.origin.is_user() && next.admission == Admission::Queue
            })
    }

    /// The last turn that held the agent and ended. A message that was taken back never did.
    pub fn last(&self) -> Option<&Settled> {
        self.last.as_ref()
    }

    /// The last turn carried the user's word: a front desk's relay of it, or the user's own
    /// message given back. Such a turn is the user's, whoever sent it.
    pub fn last_carried_user_word(&self) -> bool {
        self.last.is_some() && self.last_user_word
    }

    /// The first interrupted state since a completed or waiting turn.
    pub fn interrupted_since_ms(&self) -> Option<i64> {
        self.interrupted_since_ms
    }

    /// The user's compaction whose turn ended, also one taken back before it began, while what
    /// it did is not said yet.
    pub fn compaction_owed(&self) -> Option<&Settled> {
        self.compaction_owed.as_ref()
    }

    /// What the user's last compaction did. None until the agent's first.
    pub fn last_compaction(&self) -> Option<&LastCompaction> {
        self.last_compaction.as_ref()
    }

    /// The tool's own session, where the agent's next turn continues.
    pub fn backend_session(&self) -> Option<&str> {
        self.backend_session.as_deref()
    }

    /// The tool the agent changed from, while its new tool has no session of its conversation
    /// yet: its next turn begins one that opens with the Host's record of it.
    pub fn carried_from(&self) -> Option<&str> {
        self.carried_from.as_deref()
    }

    /// What that turn opens with, where the change said: the record of the conversation as
    /// it was carried, made when the agent moved.
    pub fn carried_opening(&self) -> Option<&str> {
        self.carried_from
            .as_ref()
            .and(self.carried_opening.as_deref())
    }

    /// The tools the agent's log last changed it from and to, since it was last started
    /// again.
    pub fn changed(&self) -> Option<(&str, &str)> {
        self.changed
            .as_ref()
            .map(|(from, to, _)| (from.as_str(), to.as_str()))
    }

    /// A provider-capacity wait has no running process to interrupt.
    pub fn expected_transit_checkpoint(&self) -> Option<TransitCheckpoint> {
        self.transit_checkpoint
    }

    pub fn checkpointed_for_transit(&self) -> bool {
        matches!(self.live, Some(Turn::Parked(ref wait)) if wait.kind == TRANSIT_CHECKPOINT)
    }

    pub fn parked_for_transit(&self) -> Option<&Parked> {
        match self.live.as_ref() {
            Some(Turn::Parked(waiting))
                if (waiting.wait.is_for_an_account() || waiting.kind == TRANSIT_CHECKPOINT)
                    && waiting.origin != Origin::NativeGoal =>
            {
                Some(waiting)
            }
            _ => None,
        }
    }

    /// Pending resume after a committed switch. A later wait on the target is a new wait.
    pub fn transit_resume(&self) -> Option<TurnId> {
        let next = self.transit_attempt.as_ref()?;
        let waiting = self.parked_for_transit()?;
        (next.turn == waiting.id && next.attempt == waiting.attempts.saturating_add(1))
            .then_some(next.turn)
    }

    pub fn resumed_by_transit(&self, turn: TurnId, attempt: u32) -> bool {
        self.transit_attempt
            .as_ref()
            .is_some_and(|next| next.turn == turn && next.attempt == attempt)
    }

    pub fn changed_selection(&self) -> Option<&ToolSelection> {
        self.changed
            .as_ref()
            .and_then(|(_, _, selection)| selection.as_ref())
    }

    /// When the agent's log last changed its tool, since it was last started again.
    pub fn changed_at_ms(&self) -> Option<i64> {
        self.changed_at_ms
    }

    /// Whether a live tool ever named a session that this agent could not bind.
    pub fn refused_live_binding(&self) -> bool {
        self.refused_live_binding
    }

    // Old events remain readable. A refused binding leaves the next launch fresh.
    fn bind_session(&mut self, name: Option<&str>) {
        self.backend_session = name
            .filter(|id| crate::native_session_name(id))
            .map(str::to_owned);
    }

    /// Whether the agent came from another Host, where it had worked. One that has run no
    /// turn here is then at rest, and not new.
    pub fn arrived(&self) -> bool {
        self.arrived
    }

    /// What the agent says it is busy with beyond its turns. Empty: nothing.
    pub fn label(&self) -> &str {
        &self.label
    }

    /// When the agent set the label it has, while it has one.
    pub fn label_at(&self) -> Option<i64> {
        self.label_at
    }

    /// What the agent rests in since it last said so. `None` once a turn has started since.
    pub fn resting(&self) -> Option<Rest> {
        self.resting
    }

    /// The number the next accepted message's turn will have.
    pub fn next_turn(&self) -> TurnId {
        TurnId(self.next_turn)
    }

    /// Who asked the live turn to stop, while it has not gone yet.
    pub fn stopping(&self) -> Option<&Control> {
        self.stopping.as_ref()
    }

    /// Whether a message from `origin`, said to the agent now, takes the live turn's place at
    /// once rather than wait behind it (issue 839): a person's message (the user's, or the
    /// Oracle's on the user's behalf) never waits behind a turn that is not a person's:
    /// another agent's message, a trigger, a goal's wake, a janitor. The user's own turn, one
    /// that carries the user's word (a front desk's relay), a Codex native goal's turn, and the
    /// tool's compaction or summary for a move are not made to give way, and neither is a turn already on its way
    /// out: the message waits for those, or is read into them, as before.
    pub fn owner_goes_first(&self, origin: &Origin) -> bool {
        let Some(live) = &self.live else {
            return false;
        };
        let housekeeping = matches!(live.origin(), Origin::Trigger { name }
            if name == COMPACTION || name == crate::turn::TRANSIT_SUMMARY);
        // A Codex native goal's turn is the owner's own goal at work, and cutting it pauses
        // that goal: the owner's message waits for it, as for his own turn.
        let native_goal = matches!(live.origin(), Origin::NativeGoal);
        origin.is_user_send()
            && self.stopping.is_none()
            && self.moved.is_none()
            && !self.import_held
            && !self.live_user_word
            && !live.origin().is_user_send()
            && !housekeeping
            && !native_goal
    }

    /// Whether the live turn may be cut for a message of the owner's that takes it over: not a
    /// Codex native goal's own turn, nor the tool's compaction or summary for a move. No live
    /// turn is cuttable too: there is nothing to cut.
    pub fn live_cuttable(&self) -> bool {
        !self.live.as_ref().is_some_and(|live| {
            matches!(live.origin(), Origin::NativeGoal)
                || matches!(live.origin(), Origin::Trigger { name }
                    if name == COMPACTION || name == crate::turn::TRANSIT_SUMMARY)
        })
    }

    /// The Stop request's journaled time, while the turn's process is still being retired.
    pub fn stopping_since_ms(&self) -> Option<i64> {
        self.stopping_since_ms
    }

    /// The note a repair left, while nothing about the agent's state changed since.
    pub fn note(&self) -> Option<&Note> {
        self.note.as_ref()
    }

    fn live_id(&self) -> Option<TurnId> {
        self.live.as_ref().map(Turn::id)
    }

    /// The waiting turn that runs when the agent is free: the first one no hold holds.
    fn next_to_run(&self) -> Option<&Queued> {
        if self.moved.is_some() {
            return None;
        }
        self.mailbox.iter().find(|queued| self.runnable(queued))
    }

    fn has_owner_priority(&self, waiting: &Queued) -> bool {
        waiting.admission == Admission::Direct
            || waiting.user_word
            || waiting.origin.is_user_send()
            // An unsent relay returned by a move or Transit retains its verified word here.
            || self.held_user_word.contains(&waiting.id)
    }

    fn runnable(&self, queued: &Queued) -> bool {
        !self.joins.contains_key(&queued.id)
            && (!self.paused
                || queued.admission == Admission::Direct
                || self.unheld.contains(&queued.id))
    }

    /// A fresh user message reaching the agent is a continuation after a user Stop.
    /// An older message selected by Send Now and automatic work do not release the hold.
    fn continues_user_queue(&self, next: &Queued) -> bool {
        self.user_stop_floor.is_some_and(|floor| {
            next.id.0 >= floor
                && next.origin.is_user_send()
                && matches!(next.admission, Admission::Queue | Admission::Later)
        })
    }

    fn check_join(&self, turn: TurnId, join: &Join) -> Result<(), Refusal> {
        if !self.can_join(turn, join)
            || self.stop_released.range(..turn).next().is_some()
            || (!join.together && !self.older_words_waiting(turn).is_empty())
        {
            return Err(Refusal::NotWaiting(turn));
        }
        Ok(())
    }

    /// The fork lane owns this reservation until its first read. Project it into
    /// normal mailbox selection without recording an ordinary Joined event.
    pub(super) fn reserve_fork(
        &mut self,
        turn: TurnId,
        seq: u64,
        join: &Join,
    ) -> Result<(), Refusal> {
        self.check_join(turn, join)?;
        if !self.waiting().any(|q| q.id == turn && q.message_seq == seq) {
            return Err(Refusal::NotWaiting(turn));
        }
        self.joins.insert(
            turn,
            FollowUp {
                join: join.clone(),
                message_seq: seq,
                stage: Stage::Pending,
            },
        );
        Ok(())
    }

    pub(super) fn release_fork(&mut self, turn: TurnId, join: &Join) -> bool {
        if self
            .joins
            .get(&turn)
            .is_some_and(|held| held.stage == Stage::Pending && held.join == *join)
        {
            self.joins.remove(&turn);
            return true;
        }
        false
    }

    fn can_join(&self, turn: TurnId, join: &Join) -> bool {
        self.moved.is_none()
            && self
                .live
                .as_ref()
                .is_some_and(|live| live.id() == join.into && live.has_process())
            && self.stopping.is_none()
            && self.selected_takeover.is_none()
            && self
                .mailbox
                .iter()
                .any(|waiting| waiting.id == turn && joins_live(waiting))
            && !self.holds(turn)
            && (!join.together || self.can_gather(turn, join.into))
            && !join.uuid.is_empty()
            && !self.joins.contains_key(&turn)
            && !self.joins.values().any(|held| held.join.uuid == join.uuid)
    }

    /// How many waiting messages run before a message of the owner's said now (issue 918):
    /// the owner's earlier words, verified relays and take-overs that can run.
    pub fn ahead_of_owner(&self) -> usize {
        self.mailbox
            .iter()
            .filter(|waiting| {
                self.has_owner_priority(waiting)
                    && self.runnable(waiting)
                    && !(self.paused && self.import_held)
            })
            .count()
    }

    /// The user's words that wait and were said before `turn`, oldest first, and are not yet
    /// given to the live turn: a message of the user's steers into the live turn only once none
    /// of these waits (issue 839), so the tool reads the user's messages in the order they were
    /// said. They go in first, or the newer one waits with them.
    pub fn older_words_waiting(&self, turn: TurnId) -> Vec<TurnId> {
        let mut older: Vec<TurnId> = self
            .mailbox
            .iter()
            .filter(|waiting| {
                waiting.id < turn && joins_live(waiting) && !self.joins.contains_key(&waiting.id)
            })
            .map(|waiting| waiting.id)
            .collect();
        older.sort();
        older
    }

    /// Whether a message of the user's accepted now could be read into the live turn
    /// ([`Command::Join`]), as far as the agent's state says: a process runs it, nothing ends
    /// or moves it, no hold holds what waits, and none of the user's older words waits
    /// unread. The owner's message steers into a turn that is not his only then; else that
    /// turn makes way for it (issue 839).
    pub fn live_takes_words(&self) -> bool {
        self.live_takes_steers()
            && !self.paused
            && self.older_words_waiting(TurnId(self.next_turn)).is_empty()
    }

    /// Whether the live turn can be given a message of the user's on its input now, as far as
    /// the agent's state says: a process runs it, nothing ends or moves it, and no message a
    /// Stop let go waits to run before newer ones (issue 867, `steerable`). The user's older
    /// words that wait do not stop it: a steer reads them in first, oldest first.
    pub fn live_takes_steers(&self) -> bool {
        self.moved.is_none()
            && self.live.as_ref().is_some_and(Turn::has_process)
            && self.stopping.is_none()
            && self.selected_takeover.is_none()
            && !self.import_held
            && self.stop_released.is_empty()
    }

    /// A message goes into the prompt of the turn that is being started only as the next of
    /// [`Agent::waiting_together`], and only once the one before it was read: so the prompt
    /// holds the user's messages in the order they were said, and none twice.
    fn can_gather(&self, turn: TurnId, into: TurnId) -> bool {
        self.live_id() == Some(into)
            && !self
                .joins
                .values()
                .any(|held| held.join.together && held.stage == Stage::Pending)
            && self.waiting_together().first() == Some(&turn)
    }

    /// The user's messages a turn took into its prompt and never began to answer wait again,
    /// in the order they were said: behind a direct message, which runs before everything,
    /// and ahead of the rest. Their rows stay where they are, and a turn that takes one again
    /// writes no second row (the journal's `enter`). A Stop's hold holds them with the rest.
    fn give_back(&mut self, gathered: Vec<Gathered>) {
        if gathered.is_empty() {
            return;
        }
        let at = self
            .mailbox
            .iter()
            .position(|waiting| waiting.admission != Admission::Direct)
            .unwrap_or(self.mailbox.len());
        for (offset, one) in gathered.into_iter().enumerate() {
            self.waited.insert(one.turn);
            self.mailbox.insert(
                at + offset,
                Queued {
                    id: one.turn,
                    message_seq: one.message_seq,
                    origin: one.origin,
                    admission: Admission::Queue,
                    accepted_at_ms: one.accepted_at_ms,
                    user_word: true,
                    attempt: 1,
                    given_back: one.given_back.saturating_add(1),
                },
            );
        }
        self.queue_revision += 1;
    }

    /// Whether a message admitted this way now would start at once, without waiting: and so
    /// whether it is in the conversation at once. One that must wait enters it in the write
    /// that starts its turn (D14).
    pub fn starts_at_once(&self, admission: Admission) -> bool {
        self.moved.is_none()
            && !self.import_held
            && self.live.is_none()
            && !(self.paused && admission == Admission::Background)
    }

    /// Whether a hold holds this waiting turn.
    pub fn holds(&self, turn: TurnId) -> bool {
        self.paused
            && self.mailbox.iter().any(|queued| {
                queued.id == turn
                    && (self.import_held
                        || (queued.admission != Admission::Direct && !self.unheld.contains(&turn)))
            })
    }

    /// Says what a command does, without doing it.
    pub fn decide(&self, command: Command, now_ms: i64) -> Result<Decision, Refusal> {
        self.decide_with_transit_hold(command, now_ms, false)
    }

    /// A transient Host cutover can fence dispatch without changing the replayable state.
    pub(crate) fn decide_with_transit_hold(
        &self,
        command: Command,
        now_ms: i64,
        transit_held: bool,
    ) -> Result<Decision, Refusal> {
        // Work on a copy and move it only through `apply`, so that what is decided here is, by
        // construction, what a replay of the events will rebuild.
        let mut work = Work {
            agent: self.clone(),
            decision: Decision::default(),
            transit_held,
        };
        let start_waiting = matches!(&command, Command::StopAndStartWaiting { .. });
        match command {
            Command::SetMoved { to } => {
                match (&work.agent.moved, &to) {
                    (Some(mark), Some(to)) if &mark.to != to => {
                        return Err(Refusal::AlreadyMoved(mark.clone()));
                    }
                    (Some(_), Some(_)) | (None, None) => return Ok(work.decision),
                    _ => {}
                }
                work.record(AgentEvent::MoveMarked {
                    mark: to.map(|to| crate::agent_moves::Mark { to, at: now_ms }),
                });
            }
            Command::HoldUnsentForMove { turn } => {
                if work.agent.moved.is_none() || work.agent.unsent(turn).is_none() {
                    return Err(Refusal::UnsentNotProven(turn));
                }
                work.record(AgentEvent::HeldUnsentForMove { turn });
            }
            Command::InterruptMovedRetry { turn } => {
                work.parked(turn)?;
                work.record(AgentEvent::Turn {
                    turn,
                    change: TurnEvent::Settled {
                        outcome: Outcome::Interrupted {
                            reason: crate::agent_moves::MOVED.into(),
                        },
                        at_ms: now_ms,
                    },
                });
            }
            Command::Accept {
                message_seq,
                origin,
                admission,
                coordination,
                user_word,
            } => {
                work.agent.require_here()?;
                // The import hold defers execution without changing mailbox priority.
                let admission = if work.agent.import_held {
                    Admission::Background
                } else {
                    admission
                };
                let turn = TurnId(work.agent.next_turn);
                let (queued, mut accepted) =
                    Queued::accept(turn, message_seq, origin, admission, now_ms);
                let resumes_while_busy =
                    work.agent.live.is_some() && work.agent.continues_user_queue(&queued);
                if let TurnEvent::Accepted {
                    coordination: mark,
                    user_word: priority,
                    ..
                } = &mut accepted
                {
                    *mark = coordination;
                    if user_word {
                        *priority = Some(true);
                    }
                }
                work.record(AgentEvent::Turn {
                    turn,
                    change: accepted,
                });
                // A busy agent takes the fresh message in normal queue order. An idle
                // agent starts it past the hold before releasing the older items.
                if resumes_while_busy {
                    work.resume_user_queue(now_ms);
                }
                if admission == Admission::Direct {
                    // The live turn makes way. One that is already on its way out is left to it.
                    work.stop_live(Control::by(preempted_by(turn)), now_ms);
                }
            }
            Command::Join { turn, join } => {
                work.agent.require_here()?;
                // No newer follow-up can jump a waiting send released by Stop, nor an older
                // message of the user's that waits (issue 839). This is an admission rule, so
                // a log that already recorded a join can still replay.
                work.agent.check_join(turn, &join)?;
                work.record(AgentEvent::Joined { turn, join });
            }
            Command::JoinRead { turn } => {
                if !work
                    .agent
                    .joins
                    .get(&turn)
                    .is_some_and(|join| join.stage == Stage::Pending)
                {
                    return Err(Refusal::NotWaiting(turn));
                }
                let continues = work
                    .agent
                    .mailbox
                    .iter()
                    .find(|queued| queued.id == turn)
                    .is_some_and(|queued| work.agent.continues_user_queue(queued));
                work.record(AgentEvent::JoinRead {
                    turn,
                    at_ms: now_ms,
                });
                // A recovered read of a pre-Stop message can empty the queue.
                // The recovery itself must leave the Stop's pause in force.
                if continues {
                    work.resume_user_queue(now_ms);
                }
            }
            Command::JoinSettled { turn } => {
                if !work
                    .agent
                    .joins
                    .get(&turn)
                    .is_some_and(|join| join.stage.read_at().is_some())
                {
                    return Err(Refusal::NotLive(turn));
                }
                work.record(AgentEvent::JoinSettled { turn });
            }
            Command::Withdraw { turn, by } => {
                if work.agent.joins.contains_key(&turn)
                    || !work.agent.mailbox.iter().any(|queued| queued.id == turn)
                {
                    return Err(Refusal::NotWaiting(turn));
                }
                work.record(AgentEvent::Turn {
                    turn,
                    change: TurnEvent::Settled {
                        outcome: Outcome::Withdrawn { by: by.clone() },
                        at_ms: now_ms,
                    },
                });
                work.let_go_of_nothing(&by, now_ms);
            }
            Command::Reword { turn, message_seq } => {
                if work.agent.joins.contains_key(&turn)
                    || !work.agent.mailbox.iter().any(|queued| queued.id == turn)
                {
                    return Err(Refusal::NotWaiting(turn));
                }
                work.record(AgentEvent::Reworded {
                    turn,
                    message_seq,
                    at_ms: now_ms,
                });
            }
            Command::SendNow { turn, by } => {
                work.agent.require_here()?;
                if work.agent.import_held
                    || !work.agent.mailbox.iter().any(|queued| queued.id == turn)
                {
                    return Err(Refusal::NotWaiting(turn));
                }
                if work.agent.live.is_some() {
                    return Err(Refusal::AtWork);
                }
                work.record(AgentEvent::SentNow {
                    turn,
                    by,
                    at_ms: now_ms,
                });
            }
            Command::TakeOver { turn, by } => {
                work.agent.require_here()?;
                let agent = &work.agent;
                let Some(queued) = agent.mailbox.iter().find(|queued| queued.id == turn) else {
                    return Err(Refusal::NotWaiting(turn));
                };
                // One already offered to the live turn's input, or chosen to take its place by
                // the tool's own takeover, is on its way into that turn: it cannot cut it.
                let offered = agent.joins.contains_key(&turn)
                    || agent
                        .selected_takeover
                        .as_ref()
                        .is_some_and(|selected| selected.into == turn);
                if agent.import_held || offered {
                    return Err(Refusal::NotWaiting(turn));
                }
                if !queued.origin.is_user_send() {
                    return Err(Refusal::NotTheOwners(turn));
                }
                if agent.live.is_some() && !agent.live_cuttable() {
                    return Err(Refusal::Uncuttable);
                }
                if agent.live.is_none() {
                    return Err(Refusal::NoTurn);
                }
                // Recorded as a send by hand, which an older build replays the same way.
                work.record(AgentEvent::SentNow {
                    turn,
                    by,
                    at_ms: now_ms,
                });
                // The live turn makes way, as for a direct message of the owner's: stopped by
                // a newer message, so the queue is not held. One already on its way out is
                // left to it.
                work.stop_live(Control::by(preempted_by(turn)), now_ms);
            }
            Command::Stop { by } | Command::StopAndStartWaiting { by } => {
                let release_waiting = start_waiting && by.actor == crate::control::USER;
                work.stop_live(by.clone(), now_ms);
                // A stop holds everything that waits, also what an earlier hold let through.
                // Even an already held mailbox observes a newer Stop. Listen-first
                // openings admitted under the previous Stop must lose authority.
                work.record(AgentEvent::Paused {
                    by: by.actor,
                    verified: by.verified,
                    reason: by.reason,
                    at_ms: now_ms,
                });
                if release_waiting && !work.agent.import_held {
                    let turns: Vec<_> = work
                        .agent
                        .waiting()
                        .filter(|queued| {
                            // An offered follow-up remains on the mailbox until its
                            // read is proved. If unread when the turn ends, let it run.
                            queued.origin.is_user_send()
                                && work
                                    .agent
                                    .selected_takeover
                                    .as_ref()
                                    .is_none_or(|selected| selected.into != queued.id)
                        })
                        .map(|queued| queued.id)
                        .collect();
                    if !turns.is_empty() {
                        work.record(AgentEvent::StopReleasedWaiting { turns });
                    }
                }
            }
            Command::Repair { by } => {
                // Nothing here holds a turn that nothing runs: there is nothing to give back.
                work.record(AgentEvent::Repaired {
                    by: by.actor,
                    verified: by.verified,
                    reason: by.reason,
                    released: String::new(),
                    at_ms: now_ms,
                });
            }
            Command::Resume { by } => {
                work.agent.require_here()?;
                if !work.agent.paused || (work.agent.import_held && by != crate::control::USER) {
                    return Err(Refusal::NotPaused);
                }
                work.record(AgentEvent::Resumed { by, at_ms: now_ms });
            }
            Command::Label { text } => {
                work.record(AgentEvent::Labelled {
                    text,
                    at_ms: now_ms,
                });
            }
            Command::RetireLabel { set_at_ms } => {
                if !work.agent.label.is_empty() && work.agent.label_at == Some(set_at_ms) {
                    work.record(AgentEvent::Labelled {
                        text: String::new(),
                        at_ms: now_ms,
                    });
                }
            }
            Command::Adopt {
                from,
                backend_session,
            } => {
                if work.agent.next_turn != 1 {
                    return Err(Refusal::AlreadyAtWork);
                }
                work.record(AgentEvent::Adopted {
                    from,
                    backend_session,
                    at_ms: now_ms,
                });
            }
            Command::TakeUp { backend_session } => {
                if work.agent.next_turn != 1 {
                    return Err(Refusal::AlreadyAtWork);
                }
                work.record(AgentEvent::TookUp {
                    backend_session,
                    at_ms: now_ms,
                });
            }
            Command::StartAgain => {
                work.agent.require_here()?;
                if work.agent.live.is_some() {
                    return Err(Refusal::AtWork);
                }
                work.record(AgentEvent::StartedAgain { at_ms: now_ms });
            }
            Command::HoldUnsentForTransit { turn } => {
                if !work.transit_held
                    || !matches!(&work.agent.live, Some(Turn::Parked(waiting))
                        if waiting.id == turn && !waiting.ran && waiting.wait.is_for_an_account())
                {
                    return Err(Refusal::UnsentNotProven(turn));
                }
                work.record(AgentEvent::HeldUnsentForMove { turn });
            }
            Command::ExpectTransitCheckpoint {
                checkpoint,
                recorded_work,
            } => {
                work.agent.require_here()?;
                let identity = match work.agent.live.as_ref() {
                    Some(Turn::Starting(source)) => (source.id, source.attempt),
                    Some(Turn::Running(source)) => (source.id, source.attempt),
                    _ => return Err(Refusal::TakeoverStale),
                };
                if !work.transit_held
                    || work.agent.stopping.is_some()
                    || (work.agent.live_worked && !recorded_work)
                    || identity != (checkpoint.turn, checkpoint.attempt)
                {
                    return Err(Refusal::TakeoverStale);
                }
                work.record(AgentEvent::TransitCheckpointExpected { checkpoint });
            }
            Command::ParkTransitCheckpoint { checkpoint } => {
                let turn = checkpoint.turn;
                if work.agent.transit_checkpoint != Some(checkpoint)
                    || work.agent.stopping.is_some()
                {
                    return Err(Refusal::TakeoverStale);
                }
                work.must_have_process(turn)?;
                work.running(turn, now_ms);
                work.record(AgentEvent::Turn {
                    turn,
                    change: TurnEvent::Parked {
                        wait: Wait::Retry {
                            until_ms: now_ms,
                            resume: true,
                        },
                        kind: TRANSIT_CHECKPOINT.into(),
                        reason: "provider checkpoint for backend Transit".into(),
                        at_ms: now_ms,
                    },
                });
            }
            Command::RunWaiting => {
                work.agent.require_here()?;
            }
            Command::ChangeTool {
                from,
                backend,
                selection,
                backend_session,
                opening,
            } => {
                work.agent.require_here()?;
                let resume = if work.transit_held {
                    work.agent
                        .parked_for_transit()
                        .map(|waiting| TransitAttempt {
                            turn: waiting.id,
                            attempt: waiting.attempts.saturating_add(1),
                        })
                } else {
                    None
                };
                if work.agent.live.is_some() && resume.is_none() {
                    return Err(Refusal::AtWork);
                }
                if !work.transit_held && !work.agent.mailbox.is_empty() {
                    return Err(Refusal::Waiting);
                }
                work.record(AgentEvent::ChangedTool {
                    from,
                    backend,
                    selection,
                    resume,
                    backend_session,
                    opening,
                    at_ms: now_ms,
                });
            }
            Command::RecordCompaction { turn, said } => {
                if work.agent.compaction_owed.as_ref().map(|owed| owed.id) != Some(turn) {
                    return Err(Refusal::NoCompactionOwed(turn));
                }
                work.record(AgentEvent::Compacted { turn, said });
            }
            Command::TurnStarted { turn } => {
                work.must_have_process(turn)?;
                if matches!(work.agent.live, Some(Turn::Starting(_))) {
                    work.record(AgentEvent::Turn {
                        turn,
                        change: TurnEvent::Moved {
                            activity: Activity::Thinking,
                            at_ms: now_ms,
                        },
                    });
                }
            }
            Command::StartFailed { turn, reason } => {
                work.must_have_process(turn)?;
                let outcome = match work.agent.stopping.clone() {
                    Some(by) => Outcome::stopped(by),
                    None => Outcome::not_started(reason),
                };
                work.record(AgentEvent::Turn {
                    turn,
                    change: TurnEvent::Settled {
                        outcome,
                        at_ms: now_ms,
                    },
                });
            }
            Command::TurnBound {
                turn,
                backend_session,
            } => {
                work.must_have_process(turn)?;
                work.running(turn, now_ms);
                work.record(AgentEvent::Turn {
                    turn,
                    change: TurnEvent::Bound { backend_session },
                });
            }
            Command::TurnMoved { turn, activity } => {
                work.must_have_process(turn)?;
                work.running(turn, now_ms);
                work.record(AgentEvent::Turn {
                    turn,
                    change: TurnEvent::Moved {
                        activity,
                        at_ms: now_ms,
                    },
                });
            }
            Command::TurnEnded { turn, outcome } => {
                work.must_have_process(turn)?;
                let outcome = match (work.agent.stopping.clone(), outcome) {
                    // Whatever the process said on its way out, it went because it was told to.
                    (Some(by), _) => Outcome::stopped(by),
                    (None, RunOutcome::Completed) => {
                        // A tool that finished before it said it had started did still run.
                        work.running(turn, now_ms);
                        Outcome::Completed
                    }
                    (None, RunOutcome::Failed { reason }) => Outcome::failed(reason),
                    (None, RunOutcome::Stopped { by }) => Outcome::stopped(Control::by(by)),
                    (None, RunOutcome::Interrupted { reason }) => Outcome::Interrupted { reason },
                };
                work.record(AgentEvent::Turn {
                    turn,
                    change: TurnEvent::Settled {
                        outcome,
                        at_ms: now_ms,
                    },
                });
            }
            Command::OfferTakeover {
                from,
                into,
                attempt,
            } => {
                if work.agent.selected_takeover.is_some()
                    || !work.agent.can_take_over(from, into, attempt)
                {
                    return Err(Refusal::TakeoverStale);
                }
                work.record(AgentEvent::TakeoverOffered {
                    from,
                    into,
                    attempt,
                });
            }
            Command::SelectTakeover { selected } => {
                if work.agent.selected_takeover.is_some()
                    || !work.agent.can_take_over(
                        selected.start.from,
                        selected.into,
                        selected.attempt,
                    )
                {
                    return Err(Refusal::TakeoverStale);
                }
                work.record(AgentEvent::TakeoverSelected { selected });
            }
            Command::ClearTakeover { from } => {
                if work
                    .agent
                    .selected_takeover
                    .as_ref()
                    .is_some_and(|selected| selected.start.from == from)
                {
                    work.record(AgentEvent::TakeoverCleared { from });
                }
            }
            Command::FinishTakeover { from } => {
                let Some(selected) = work.agent.selected_takeover.clone().filter(|selected| {
                    selected.start.from == from
                        && work
                            .agent
                            .can_take_over(from, selected.into, selected.attempt)
                }) else {
                    return Err(Refusal::TakeoverStale);
                };
                work.record(AgentEvent::Turn {
                    turn: from,
                    change: TurnEvent::Settled {
                        outcome: Outcome::Replaced {
                            by: selected.into,
                            trace: selected.trace,
                        },
                        at_ms: now_ms,
                    },
                });
                work.start_next_with(now_ms, Some(selected.start));
            }
            Command::TurnParked {
                turn,
                wait,
                kind,
                reason,
            } => {
                work.must_have_process(turn)?;
                let change = match work.agent.stopping.clone() {
                    // It went because it was told to: nothing is tried again.
                    Some(by) => TurnEvent::Settled {
                        outcome: Outcome::stopped(by),
                        at_ms: now_ms,
                    },
                    None => {
                        // A tool that failed before it said it had started did still run.
                        work.running(turn, now_ms);
                        TurnEvent::Parked {
                            wait,
                            kind,
                            reason,
                            at_ms: now_ms,
                        }
                    }
                };
                work.record(AgentEvent::Turn { turn, change });
            }
            // The next attempt's spool begins at 0: the writer of this command puts a cursor of
            // that spool at offset 0 in the same batch, which marks in the log where the attempt
            // began (the actor's replay after a restart seeds the attempt from it).
            Command::TurnRetried { turn } | Command::NetworkBack { turn } => {
                work.agent.require_here()?;
                let network_back = matches!(command, Command::NetworkBack { .. });
                let parked = work.parked(turn)?;
                // Only a turn that waited for the network has it come back.
                if network_back && !parked.wait.is_for_the_network() {
                    return Err(Refusal::NotOffline(turn));
                }
                let (message_seq, attempt) = (parked.message_seq, parked.attempts + 1);
                work.record(AgentEvent::Turn {
                    turn,
                    change: TurnEvent::Retried {
                        at_ms: now_ms,
                        network_back,
                    },
                });
                work.decision.effects.push(Effect::RetryTurn {
                    turn,
                    message_seq,
                    attempt,
                });
            }
            Command::WaitOver { turn } | Command::NetworkGone { turn } => {
                let parked = work.parked(turn)?;
                if matches!(command, Command::NetworkGone { .. })
                    && !parked.wait.is_for_the_network()
                {
                    return Err(Refusal::NotOffline(turn));
                }
                let reason = match command {
                    Command::NetworkGone { .. } => crate::failures::network_gone(&parked.reason),
                    _ => parked.reason.clone(),
                };
                let outcome = Outcome::Failed {
                    reason,
                    kind: parked.kind.clone(),
                };
                work.record(AgentEvent::Turn {
                    turn,
                    change: TurnEvent::Settled {
                        outcome,
                        at_ms: now_ms,
                    },
                });
            }
        }
        work.start_next(now_ms);
        Ok(work.decision)
    }

    /// Changes the state by one event. The only way the state changes.
    pub fn apply(&mut self, event: &AgentEvent) -> Result<(), IllegalTransition> {
        match event {
            AgentEvent::TransitCheckpointExpected { checkpoint } => {
                self.transit_checkpoint = Some(*checkpoint);
            }
            AgentEvent::MoveMarked { mark } => {
                self.moved.clone_from(mark);
                self.queue_revision += 1;
            }
            AgentEvent::HeldUnsentForMove { turn } => {
                let held = self.unsent(*turn).ok_or(IllegalTransition {
                    turn: *turn,
                    state: "may have reached the tool",
                    event: "held-unsent-for-move",
                })?;
                if self.live_user_word {
                    self.held_user_word.insert(*turn);
                }
                self.live = None;
                self.live_from_queue = false;
                self.live_takeover = None;
                self.live_resumed_session = false;
                self.stopping = None;
                self.stopping_since_ms = None;
                self.selected_takeover = None;
                self.joins.clear();
                self.mailbox.push_front(held);
                self.waited.insert(*turn);
                self.queue_revision += 1;
            }
            AgentEvent::Joined { turn, join } => {
                let invalid = IllegalTransition {
                    turn: *turn,
                    state: "unavailable",
                    event: "joined",
                };
                if !self.can_join(*turn, join) {
                    return Err(invalid);
                }
                let waiting = self
                    .mailbox
                    .iter()
                    .find(|waiting| waiting.id == *turn)
                    .ok_or(invalid)?;
                self.joins.insert(
                    *turn,
                    FollowUp {
                        join: join.clone(),
                        message_seq: waiting.message_seq,
                        stage: Stage::Pending,
                    },
                );
            }
            AgentEvent::JoinRead { turn, at_ms } => {
                let invalid = IllegalTransition {
                    turn: *turn,
                    state: "unavailable",
                    event: "join-read",
                };
                let position = self
                    .mailbox
                    .iter()
                    .position(|waiting| waiting.id == *turn)
                    .ok_or_else(|| invalid.clone())?;
                let join = self
                    .joins
                    .get_mut(turn)
                    .filter(|join| join.stage == Stage::Pending)
                    .ok_or(invalid)?;
                join.stage = Stage::Read(*at_ms);
                if join.join.together {
                    let waiting = &self.mailbox[position];
                    self.gathered
                        .entry(join.join.into)
                        .or_default()
                        .push(Gathered {
                            turn: *turn,
                            message_seq: join.message_seq,
                            origin: waiting.origin.clone(),
                            accepted_at_ms: waiting.accepted_at_ms,
                            given_back: waiting.given_back,
                        });
                }
                self.mailbox.remove(position);
                self.unheld.remove(turn);
                self.stop_released.remove(turn);
                self.held_user_word.remove(turn);
                if self.waited.remove(turn) {
                    self.queue_revision += 1;
                }
                self.deliveries
                    .hand(turn.0, crate::delivery::revision(self.at, 0), *at_ms);
                self.deliveries.launch(turn.0);
            }
            AgentEvent::JoinSettled { turn } => {
                let join = self
                    .joins
                    .get_mut(turn)
                    .filter(|join| join.stage.read_at().is_some())
                    .ok_or(IllegalTransition {
                        turn: *turn,
                        state: "unread",
                        event: "join-settled",
                    })?;
                join.stage = Stage::Settled;
                self.deliveries.over(turn.0);
                self.settled += 1;
            }
            AgentEvent::Paused {
                by,
                verified,
                reason,
                at_ms,
            } => {
                // A Stop is a state of its own: a note left before it is no longer the latest.
                self.note = None;
                // Only the user's Stop withdraws the user's sends not yet launched, and moves the
                // generation a send is shown stopped by; an agent's own verified Stop does not.
                let control = crate::control::Control {
                    actor: by.clone(),
                    verified: *verified,
                    reason: reason.clone(),
                };
                if control.is_the_users() {
                    self.user_stop_generation += 1;
                    self.deliveries.fence(self.at, *at_ms);
                }
                self.stop_generation += 1;
                let relaunch = control.is_relaunch();
                if !self.paused || (!relaunch && !self.unheld.is_empty()) {
                    self.queue_revision += 1;
                }
                self.paused = true;
                // The old importer recorded this same hold after writing the history.
                self.import_held |= self.arrived && by == IMPORT && !verified && reason.is_empty();
                if !relaunch {
                    self.user_stop_floor = (by == crate::control::USER).then_some(self.next_turn);
                }
                self.unheld.clear();
                self.stop_released.clear();
                // A Stop after a take-over holds the message that took over with the rest.
                self.taking_over = None;
            }
            AgentEvent::StopReleasedWaiting { turns } => {
                self.unheld.extend(turns);
                self.stop_released.extend(turns);
            }
            AgentEvent::Resumed { .. } => {
                self.paused = false;
                self.import_held = false;
                self.user_stop_floor = None;
                self.unheld.clear();
                self.queue_revision += 1;
            }
            AgentEvent::Reworded {
                turn, message_seq, ..
            } => {
                let waiting = self.mailbox.iter_mut().find(|queued| queued.id == *turn);
                let Some(queued) = waiting else {
                    return Err(IllegalTransition {
                        turn: *turn,
                        state: "not waiting",
                        event: "reworded",
                    });
                };
                queued.message_seq = *message_seq;
                // What waits changed: a client that orders queue news by the revision asks again.
                self.queue_revision += 1;
            }
            AgentEvent::SentNow { turn, by, .. } => {
                let Some(at) = self.mailbox.iter().position(|queued| queued.id == *turn) else {
                    return Err(IllegalTransition {
                        turn: *turn,
                        state: "not waiting",
                        event: "sent-now",
                    });
                };
                // Ahead of everything that waits, and held by nothing.
                if let Some(queued) = self.mailbox.remove(at) {
                    self.mailbox.push_front(queued);
                }
                self.unheld.insert(*turn);
                // Sent by hand while a turn runs: it took that turn over (issue 867).
                if self.live.is_some() {
                    self.taking_over = Some((*turn, by.clone()));
                }
                // Taken out of its place by hand, the queue changed; and it changes once
                // more when the turn starts. The Python Host counts a message sent by hand
                // so (it is claimed, then started), and a client orders queue news by this.
                self.queue_revision += 1;
            }
            AgentEvent::StopRequested {
                turn,
                by,
                verified,
                reason,
                at_ms,
            } => {
                if self.live_id() != Some(*turn) {
                    return Err(IllegalTransition {
                        turn: *turn,
                        state: "not live",
                        event: "stop-requested",
                    });
                }
                self.stopping = Some(Control {
                    actor: by.clone(),
                    verified: *verified,
                    reason: reason.clone(),
                });
                self.stopping_since_ms = Some(*at_ms);
                self.note_turn_activity(*at_ms);
                self.note = None;
            }
            AgentEvent::Repaired {
                by,
                verified,
                reason,
                released,
                at_ms,
            } => {
                let by = Control {
                    actor: by.clone(),
                    verified: *verified,
                    reason: reason.clone(),
                };
                self.note = Some(Note {
                    by,
                    released: released.clone(),
                    at_ms: *at_ms,
                });
            }
            AgentEvent::Adopted {
                backend_session, ..
            } => {
                self.bind_session(backend_session.as_deref());
                self.arrived = true;
            }
            AgentEvent::TookUp {
                backend_session, ..
            } => self.bind_session(Some(backend_session)),
            AgentEvent::ChangedTool {
                from,
                backend,
                selection,
                resume,
                backend_session,
                opening,
                at_ms,
            } => {
                self.changed_at_ms = Some(*at_ms);
                self.bind_session(backend_session.as_deref());
                self.carried_from = self.backend_session.is_none().then(|| from.clone());
                self.carried_opening = opening.clone().filter(|_| self.carried_from.is_some());
                self.changed = Some((from.clone(), backend.clone(), selection.clone()));
                self.transit_attempt.clone_from(resume);
                if let Some(next) = resume
                    && let Some(Turn::Parked(waiting)) = self.live.as_mut()
                    && waiting.id == next.turn
                {
                    waiting.backend_session.clone_from(backend_session);
                }
            }
            AgentEvent::StartedAgain { .. } => {
                self.backend_session = None;
                self.carried_from = None;
                self.changed = None;
                self.transit_attempt = None;
                self.changed_at_ms = None;
                self.note = None;
                // Python counts both the pause and its release even for an idle agent.
                // For a busy agent the Host records those transitions around the relaunch.
                if !self.paused {
                    self.queue_revision += 2;
                }
            }
            AgentEvent::Labelled { text, at_ms } => {
                self.resting = Some(if text.is_empty() {
                    Rest::Idle
                } else {
                    Rest::Background
                });
                self.label.clone_from(text);
                self.label_at = (!text.is_empty()).then_some(*at_ms);
            }
            AgentEvent::Compacted { turn, said } => {
                if self.compaction_owed.as_ref().map(|owed| owed.id) != Some(*turn) {
                    return Err(IllegalTransition {
                        turn: *turn,
                        state: "no compaction owed",
                        event: "compacted",
                    });
                }
                self.compaction_owed = None;
                self.last_compaction = Some(said.clone());
            }
            AgentEvent::TakeoverOffered { .. } => {
                // An offer never changes a turn's state. Its shim releases an unconsumed
                // hold on its own, including when the Host dies between these events.
            }
            AgentEvent::TakeoverSelected { selected } => {
                if self.selected_takeover.is_some()
                    || !self.can_take_over(selected.start.from, selected.into, selected.attempt)
                {
                    return Err(IllegalTransition {
                        turn: selected.start.from,
                        state: "takeover unavailable",
                        event: "takeover-selected",
                    });
                }
                self.selected_takeover = Some(selected.clone());
            }
            AgentEvent::TakeoverCleared { from } => {
                if self
                    .selected_takeover
                    .as_ref()
                    .is_some_and(|selected| selected.start.from == *from)
                {
                    self.selected_takeover = None;
                }
            }
            AgentEvent::Turn { turn, change } => self.apply_turn(*turn, change)?,
        }
        Ok(())
    }

    fn apply_turn(&mut self, turn: TurnId, change: &TurnEvent) -> Result<(), IllegalTransition> {
        // What a turn does is a state of the agent: a note left before it is no longer the
        // latest. A message that only joins the queue changes no state.
        if !matches!(change, TurnEvent::Accepted { .. }) {
            self.note = None;
        }
        // A launch anew after a wait for an account is a launch the apps are told of.
        let for_an_account = matches!(
            &self.live,
            Some(Turn::Parked(parked)) if parked.id == turn && parked.wait.is_for_an_account()
        );
        let was_live = self.live_id() == Some(turn);
        self.move_turn(turn, change)?;
        match change {
            TurnEvent::Started { at_ms, .. }
            | TurnEvent::Moved { at_ms, .. }
            | TurnEvent::Parked { at_ms, .. }
            | TurnEvent::Retried { at_ms, .. } => self.note_turn_activity(*at_ms),
            TurnEvent::Settled { at_ms, .. } if was_live => self.note_turn_activity(*at_ms),
            _ => {}
        }
        // What a user's send is shown of its delivery (send_delivery_state): admitted with a
        // revision of this event, handed over once its tool process is being started, and over
        // when its turn ends without that.
        let revision = crate::delivery::revision(self.at, 0);
        match change {
            TurnEvent::Accepted {
                message_seq,
                origin,
                admission,
                at_ms,
                ..
            } => {
                if origin.is_user() && matches!(admission, Admission::Queue | Admission::Direct) {
                    let stops = self.user_stop_generation;
                    self.deliveries
                        .admit(turn.0, *message_seq, stops, revision, *at_ms);
                }
            }
            _ => {
                let handed = self
                    .live
                    .as_ref()
                    .is_some_and(|live| live.id() == turn && live.has_process());
                let waits = self.mailbox.iter().any(|queued| queued.id == turn);
                if handed {
                    let at_ms = change_at(change).unwrap_or_default();
                    self.deliveries.hand(turn.0, revision, at_ms);
                    let launch = matches!(change, TurnEvent::Started { .. })
                        || (matches!(change, TurnEvent::Retried { .. }) && for_an_account);
                    if launch {
                        self.deliveries.launch(turn.0);
                    }
                } else if !waits && self.live_id() != Some(turn) {
                    self.deliveries.over(turn.0);
                }
            }
        }
        Ok(())
    }

    fn move_turn(&mut self, turn: TurnId, change: &TurnEvent) -> Result<(), IllegalTransition> {
        if let TurnEvent::Accepted {
            turn: accepted,
            user_word,
            ..
        } = change
        {
            if *accepted != turn || turn.0 != self.next_turn {
                return Err(IllegalTransition {
                    turn,
                    state: "out of order",
                    event: "accepted",
                });
            }
            if let Turn::Queued(queued) = Turn::begin(change)? {
                // A hold holds what waited when it was placed, and background work that comes
                // later. What someone says after the stop is not held by it.
                let held = self.paused && queued.admission == Admission::Background;
                if self.paused && matches!(queued.admission, Admission::Queue | Admission::Later) {
                    self.unheld.insert(turn);
                }
                // Does it go through the queue? It does when it has to wait, behind a turn or
                // because of a hold: a free agent has nothing waiting that could run, so nothing
                // else can be in its way. And it does when it was left for later, whether or not
                // anything is in its way: whoever left it there is told that it joined and, in
                // the same breath, that it left to run. The Python Host counts it so, and an app
                // that orders queue news by the revision sees the same numbers from both.
                if self.live.is_some() || held || queued.admission.left_for_later() {
                    self.waited.insert(turn);
                    self.queue_revision += 1;
                }
                if queued.admission == Admission::Direct {
                    // Ahead of what waits its turn, behind the direct messages already waiting.
                    let at = self
                        .mailbox
                        .iter()
                        .position(|waiting| waiting.admission != Admission::Direct)
                        .unwrap_or(self.mailbox.len());
                    self.mailbox.insert(at, queued);
                } else if *user_word == Some(true) {
                    let at = crate::user_word::slot(self.mailbox.iter().map(|waiting| {
                        waiting.user_word || waiting.admission == Admission::Direct
                    }));
                    self.mailbox.insert(at, queued);
                } else {
                    self.mailbox.push_back(queued);
                }
            }
            self.next_turn += 1;
            return Ok(());
        }
        if self.live_id() == Some(turn) {
            if matches!(change, TurnEvent::Parked { .. } | TurnEvent::Settled { .. }) {
                self.transit_checkpoint = None;
            }
            let Some(live) = self.live.take() else {
                return Ok(());
            };
            if let TurnEvent::Bound { backend_session } = change {
                self.bind_session(Some(backend_session));
                self.refused_live_binding |= self.backend_session.is_none();
                if self.backend_session.is_some() {
                    self.carried_from = None;
                }
            }
            match live.apply(change)? {
                Turn::Settled(ended) => {
                    match &ended.outcome {
                        Outcome::Failed { .. }
                        | Outcome::Interrupted { .. }
                        | Outcome::Stopped { .. } => {
                            self.interrupted_since_ms.get_or_insert(ended.ended_at_ms);
                        }
                        Outcome::Completed => {
                            self.interrupted_since_ms = None;
                        }
                        _ => {}
                    }
                    self.joins.clear();
                    let gathered = self.gathered.remove(&turn).unwrap_or_default();
                    if !self.live_worked && gives_back(&ended.outcome) {
                        self.give_back(gathered);
                    } else {
                        // What the turn took into its prompt is over with it, also where no
                        // process settled it (a turn held back for a move ends without one).
                        for gathered in gathered {
                            self.deliveries.over(gathered.turn.0);
                        }
                    }
                    self.live_worked = false;
                    let held_user_word = self.held_user_word.remove(&turn);
                    self.last_user_word =
                        std::mem::take(&mut self.live_user_word) || held_user_word;
                    self.settled += 1;
                    self.stopping = None;
                    self.stopping_since_ms = None;
                    self.live_from_queue = false;
                    self.owe_compaction(&ended);
                    self.live_takeover = None;
                    self.live_resumed_session = false;
                    self.selected_takeover = None;
                    self.last = Some(ended);
                }
                next => {
                    if matches!(
                        change,
                        TurnEvent::Moved {
                            activity: Activity::Waiting,
                            ..
                        }
                    ) {
                        self.interrupted_since_ms = None;
                    }
                    if matches!(
                        change,
                        TurnEvent::Moved {
                            activity: Activity::Responding | Activity::Tool { .. },
                            ..
                        }
                    ) {
                        self.live_worked = true;
                    }
                    if let Turn::Parked(parked) = &next {
                        if self.live_from_queue
                            && !parked.ran
                            && matches!(change, TurnEvent::Parked { kind, .. } if kind == crate::failures::NOT_STARTED)
                        {
                            self.handoff_launch_failures =
                                self.handoff_launch_failures.saturating_add(1);
                        }
                        self.joins.clear();
                    }
                    self.live = Some(next);
                }
            }
            return Ok(());
        }
        let Some(index) = self.mailbox.iter().position(|queued| queued.id == turn) else {
            return Err(IllegalTransition {
                turn,
                state: "unknown",
                event: "turn",
            });
        };
        // A waiting turn starts only when nothing is live, and only when it is the next to run.
        let starting = matches!(change, TurnEvent::Started { .. });
        let next = self.next_to_run().map(|queued| queued.id);
        if starting && (self.live.is_some() || next != Some(turn)) {
            return Err(IllegalTransition {
                turn,
                state: "waiting behind another",
                event: "started",
            });
        }
        let Some(queued) = self.mailbox.remove(index) else {
            return Ok(());
        };
        let waited = self.waited.remove(&turn);
        if waited {
            self.queue_revision += 1;
        }
        self.unheld.remove(&turn);
        self.stop_released.remove(&turn);
        if self.taking_over.as_ref().is_some_and(|(id, _)| *id == turn) {
            self.taking_over = None;
        }
        let user_word = queued.user_word;
        match Turn::Queued(queued).apply(change)? {
            Turn::Settled(ended) => {
                self.held_user_word.remove(&turn);
                for gathered in self.gathered.remove(&turn).unwrap_or_default() {
                    self.deliveries.over(gathered.turn.0);
                }
                self.settled += 1;
                self.owe_compaction(&ended);
            }
            next => {
                if let TurnEvent::Started { at_ms, takeover } = change {
                    self.live_resumed_session = self.backend_session.is_some();
                    self.live_worked = false;
                    self.live_user_word = user_word;
                    self.live_takeover.clone_from(takeover);
                    if let Some(start) = takeover {
                        self.bind_session(Some(&start.session));
                    }
                    self.latest_turn_start =
                        Some(self.latest_turn_start.map_or(*at_ms, |old| old.max(*at_ms)));
                }
                self.live = Some(next);
                self.live_from_queue = waited;
                self.handoff_launch_failures = 0;
                // A turn runs: what the agent rested in before is over.
                self.resting = None;
            }
        }
        Ok(())
    }

    /// A turn ended: if it was the user's compaction, what it did is owed. Only that turn, and
    /// never a compaction the tool made by itself within another turn.
    fn owe_compaction(&mut self, ended: &Settled) {
        if matches!(&ended.origin, Origin::Trigger { name } if name == COMPACTION) {
            self.compaction_owed = Some(ended.clone());
        }
    }

    /// Rebuilds an agent from its history, oldest event first.
    pub fn replay<'a>(
        events: impl IntoIterator<Item = &'a AgentEvent>,
    ) -> Result<Self, IllegalTransition> {
        let mut agent = Self::new();
        for event in events {
            agent.apply(event)?;
        }
        Ok(agent)
    }
}

/// When a turn's change happened, where it says.
fn change_at(change: &TurnEvent) -> Option<i64> {
    match change {
        TurnEvent::Accepted { at_ms, .. }
        | TurnEvent::Started { at_ms, .. }
        | TurnEvent::Moved { at_ms, .. }
        | TurnEvent::Parked { at_ms, .. }
        | TurnEvent::Retried { at_ms, .. }
        | TurnEvent::Settled { at_ms, .. } => Some(*at_ms),
        TurnEvent::Bound { .. } => None,
    }
}

/// A copy of the agent being moved forward while a decision is made.
struct Work {
    agent: Agent,
    decision: Decision,
    transit_held: bool,
}

impl Work {
    fn record(&mut self, event: AgentEvent) {
        // Settle the carrier first, which returns its gathered messages on every reader.
        // Then remove exhausted messages with the existing queued settlement before deciding
        // what starts next. All these events are written in one journal append.
        let exhausted: Vec<_> = match &event {
            AgentEvent::Turn {
                turn,
                change: TurnEvent::Settled { outcome, at_ms },
            } if self.agent.live_id() == Some(*turn)
                && !self.agent.live_worked
                && gives_back(outcome) =>
            {
                self.agent
                    .gathered(*turn)
                    .iter()
                    .filter(|one| one.given_back >= TAKEN_FOR_NOTHING - 1)
                    .map(|one| AgentEvent::Turn {
                        turn: one.turn,
                        change: TurnEvent::Settled {
                            outcome: Outcome::Withdrawn { by: "host".into() },
                            at_ms: *at_ms,
                        },
                    })
                    .collect()
            }
            _ => Vec::new(),
        };
        // Decided events are legal in sequence. If one fails, do not apply its dependents.
        for event in std::iter::once(event).chain(exhausted) {
            if self.agent.apply(&event).is_err() {
                break;
            }
            self.decision.events.push(event);
        }
    }

    /// A signal of a tool process is about the live turn, and the live turn has one. News
    /// of an attempt that is over is late news.
    fn must_have_process(&self, turn: TurnId) -> Result<(), Refusal> {
        match &self.agent.live {
            Some(live) if live.id() == turn && live.has_process() => Ok(()),
            _ => Err(Refusal::NotLive(turn)),
        }
    }

    /// The live turn, when it is this one and waits between two attempts.
    fn parked(&self, turn: TurnId) -> Result<&Parked, Refusal> {
        match &self.agent.live {
            Some(Turn::Parked(parked)) if parked.id == turn => Ok(parked),
            _ => Err(Refusal::NotParked(turn)),
        }
    }

    /// Ends the live turn for someone: a turn with a tool process is asked to stop and ends
    /// when the process is gone; a turn that waits between two attempts has none, and ends
    /// here. One that is already on its way out is left to it.
    fn stop_live(&mut self, by: Control, now_ms: i64) {
        match (&self.agent.live, &self.agent.stopping) {
            (Some(Turn::Parked(parked)), _) => {
                let turn = parked.id;
                self.record(AgentEvent::Turn {
                    turn,
                    change: TurnEvent::Settled {
                        outcome: Outcome::stopped(by),
                        at_ms: now_ms,
                    },
                });
            }
            (Some(live), None) => {
                let turn = live.id();
                self.record(AgentEvent::StopRequested {
                    turn,
                    by: by.actor,
                    verified: by.verified,
                    reason: by.reason,
                    at_ms: now_ms,
                });
                self.decision.effects.push(Effect::StopTurn { turn });
            }
            _ => {}
        }
    }

    /// Moves a turn that is still starting to running: the shim's later signals imply it.
    fn running(&mut self, turn: TurnId, now_ms: i64) {
        if matches!(self.agent.live, Some(Turn::Starting(_))) {
            self.record(AgentEvent::Turn {
                turn,
                change: TurnEvent::Moved {
                    activity: Activity::Thinking,
                    at_ms: now_ms,
                },
            });
        }
    }

    /// Starts the next waiting turn when the agent is free: the first one no hold holds.
    fn start_next(&mut self, now_ms: i64) {
        self.start_next_with(now_ms, None);
    }

    fn start_next_with(&mut self, now_ms: i64, takeover: Option<crate::takeover::Start>) {
        // Keep the replay's ordering check compatible with historical turns that started under
        // an import pause. New decisions must never start one before the handoff.
        if self.agent.live.is_some() || self.agent.import_held || self.transit_held {
            return;
        }
        // The message that took over the turn that just ended runs next: sent by hand once
        // more, ahead of what that turn gave back, unless it was in this same step.
        if let Some((turn, by)) = self.agent.taking_over.clone()
            && self.agent.mailbox.iter().any(|queued| queued.id == turn)
            && !self.decision.events.iter().any(
                |event| matches!(event, AgentEvent::SentNow { turn: sent, .. } if *sent == turn),
            )
        {
            self.record(AgentEvent::SentNow {
                turn,
                by,
                at_ms: now_ms,
            });
        }
        let Some(head) = self.agent.next_to_run() else {
            return;
        };
        let sent_by_hand = self
            .decision
            .events
            .iter()
            .any(|event| matches!(event, AgentEvent::SentNow { turn, .. } if *turn == head.id));
        let next = if sent_by_hand || takeover.is_some() || self.agent.has_owner_priority(head) {
            head
        } else {
            // A replayed or restored queue can still contain an agent ahead of the owner.
            self.agent
                .mailbox
                .iter()
                .find(|waiting| {
                    self.agent.has_owner_priority(waiting) && self.agent.runnable(waiting)
                })
                .unwrap_or(head)
        };
        let continues = self.agent.continues_user_queue(next);
        // A message the user's Stop let through runs as part of that Stop, and one the user
        // sent by hand (Send Now) runs past the hold: neither ends it, the last one included.
        // The hold stays for what comes after them, until the user's next fresh message or a
        // resume. Were it to end when the last of the Stop's own starts, the queue a Stop
        // leaves would depend on how soon the stopped turn's process exits, and agree with what
        // the Stop answered only when it was read before that.
        let released = self.agent.stop_released.contains(&next.id) || sent_by_hand;
        let (turn, message_seq) = (next.id, next.message_seq);
        if turn != head.id {
            // Record the selection before Started, so historical and older readers replay the
            // same head. The selected message was already runnable under any existing Stop.
            self.record(AgentEvent::SentNow {
                turn,
                by: "host".to_string(),
                at_ms: now_ms,
            });
        }
        self.record(AgentEvent::Turn {
            turn,
            change: TurnEvent::Started {
                at_ms: now_ms,
                takeover,
            },
        });
        self.decision
            .effects
            .push(Effect::StartTurn { turn, message_seq });
        if continues {
            self.resume_user_queue(now_ms);
        } else if self.agent.live_from_queue() && !released {
            self.let_go_of_nothing(DRAINED, now_ms);
        }
    }

    fn resume_user_queue(&mut self, now_ms: i64) {
        self.record(AgentEvent::Resumed {
            by: crate::control::USER.to_string(),
            at_ms: now_ms,
        });
    }

    /// A hold holds what waits. When the last of it has left the queue, taken back or gone
    /// to run, the hold is over: the next thing that is left for the agent is not held by
    /// a stop that held something else. So the Python Host ends a pause (`turn_queue`).
    fn let_go_of_nothing(&mut self, by: &str, now_ms: i64) {
        if self.agent.paused && !self.agent.import_held && self.agent.mailbox.is_empty() {
            self.record(AgentEvent::Resumed {
                by: by.to_string(),
                at_ms: now_ms,
            });
        }
    }
}

// `StartOutcome` is part of the turn's typed API; the agent reaches the same states through events.
const _: fn(StartOutcome) = |_| {};

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    #[test]
    fn a_first_turn_only_counts_as_fresh_without_a_session_before_it() {
        let mut fresh = Driven::new();
        fresh.user(1);
        fresh
            .send(Command::TurnBound {
                turn: TurnId(1),
                backend_session: "the-new-session".to_string(),
            })
            .unwrap();
        assert!(fresh.agent.fresh_first_turn());

        let mut resumed = Driven::new();
        resumed
            .send(Command::TakeUp {
                backend_session: "a-kept-session".to_string(),
            })
            .unwrap();
        resumed.user(1);
        assert!(!resumed.agent.fresh_first_turn());
    }

    /// An agent with its whole history, driven the way the actor will drive it.
    struct Driven {
        agent: Agent,
        history: Vec<AgentEvent>,
        started: Vec<TurnId>,
        now_ms: i64,
    }

    impl Driven {
        fn new() -> Self {
            Self {
                agent: Agent::new(),
                history: Vec::new(),
                started: Vec::new(),
                now_ms: 1_000,
            }
        }

        fn send(&mut self, command: Command) -> Result<Vec<Effect>, Refusal> {
            self.send_with_transit_hold(command, false)
        }

        fn send_with_transit_hold(
            &mut self,
            command: Command,
            transit_held: bool,
        ) -> Result<Vec<Effect>, Refusal> {
            self.now_ms += 10;
            let decision =
                self.agent
                    .decide_with_transit_hold(command, self.now_ms, transit_held)?;
            for event in &decision.events {
                self.agent.apply(event).expect("a decided event applies");
            }
            self.history.extend(decision.events);
            for effect in &decision.effects {
                if let Effect::StartTurn { turn, .. } = effect {
                    self.started.push(*turn);
                }
            }
            // The state is always what a restart would rebuild.
            assert_eq!(
                Agent::replay(&self.history).expect("the history replays"),
                self.agent
            );
            // One turn at a time.
            assert!(self.agent.live().is_none_or(Turn::is_live));
            Ok(decision.effects)
        }

        /// A message that waits its turn.
        fn user(&mut self, message_seq: u64) -> Vec<Effect> {
            self.send(Command::Accept {
                message_seq,
                origin: Origin::User,
                admission: Admission::Queue,
                coordination: None,
                user_word: false,
            })
            .unwrap()
        }

        /// Work nobody sits waiting for.
        fn background(&mut self, message_seq: u64) -> Vec<Effect> {
            self.send(Command::Accept {
                message_seq,
                origin: Origin::Trigger {
                    name: "schedule".to_string(),
                },
                admission: Admission::Background,
                coordination: None,
                user_word: false,
            })
            .unwrap()
        }

        /// A message the user wants answered now.
        fn direct(&mut self, message_seq: u64) -> Vec<Effect> {
            self.send(Command::Accept {
                message_seq,
                origin: Origin::User,
                admission: Admission::Direct,
                coordination: None,
                user_word: false,
            })
            .unwrap()
        }

        fn finish(&mut self, turn: u64) -> Vec<Effect> {
            self.send(Command::TurnEnded {
                turn: TurnId(turn),
                outcome: RunOutcome::Completed,
            })
            .unwrap()
        }

        fn waiting(&self) -> Vec<u64> {
            self.agent.waiting().map(|queued| queued.id.0).collect()
        }

        fn live(&self) -> Option<u64> {
            self.agent.live().map(|turn| turn.id().0)
        }
    }

    #[test]
    fn guidance_recency_is_the_maximum_committed_start_even_after_replay() {
        let mut a = Driven::new();
        assert_eq!(a.agent.latest_turn_start(), 0);
        for (turn, now, expected) in [(1, 50, 60), (2, 10, 60), (3, 80, 90)] {
            a.now_ms = now;
            a.user(turn);
            assert_eq!(a.agent.latest_turn_start(), expected);
            a.send(Command::TurnStarted { turn: TurnId(turn) }).unwrap();
            a.finish(turn);
            assert_eq!(a.agent.latest_turn_start(), expected);
        }
        let mut replay = Agent::new();
        for event in &a.history {
            replay.apply(event).unwrap();
        }
        assert_eq!(replay.latest_turn_start(), 90);
    }

    #[test]
    fn turn_activity_survives_replay_without_counting_unstarted_work() {
        let mut a = Driven::new();
        a.user(1);
        a.send(Command::TurnStarted { turn: TurnId(1) }).unwrap();
        let began = a.agent.latest_turn_start();
        a.now_ms += 7 * 3_600_000;
        a.send(Command::TurnMoved {
            turn: TurnId(1),
            activity: Activity::Responding,
        })
        .unwrap();
        let worked = a.agent.latest_turn_activity().unwrap();
        assert!(worked > began + 6 * 3_600_000);
        a.user(2);
        a.send(Command::Withdraw {
            turn: TurnId(2),
            by: "user".into(),
        })
        .unwrap();
        assert_eq!(a.agent.latest_turn_activity(), Some(worked));
        let mut replay = Agent::new();
        for event in &a.history {
            replay.apply(event).unwrap();
        }
        assert_eq!(replay.latest_turn_activity(), Some(worked));
    }

    /// The connection of the live turn's tool drops: the turn waits a moment for its next attempt.
    /// The user's compaction owes one outcome once its turn has ended, also when it was taken
    /// back before it began, and the agent keeps the last one. No other turn owes one, and the
    /// outcome is written once, for that turn only.
    #[test]
    fn a_compaction_owes_one_outcome_and_the_agent_keeps_the_last() {
        use crate::compaction::{self, LastCompaction};
        let compaction = |message_seq| Command::Accept {
            message_seq,
            origin: Origin::Trigger {
                name: COMPACTION.to_string(),
            },
            admission: Admission::Queue,
            coordination: None,
            user_word: false,
        };
        let said = |turn: u64, before: u64| Command::RecordCompaction {
            turn: TurnId(turn),
            said: LastCompaction {
                decided: compaction::outcome(compaction::COMPACTED, Some(before), None, false, ""),
                backend: "claude".to_string(),
                at: 7,
            },
        };
        let mut a = Driven::new();
        assert_eq!(a.agent.last_compaction(), None);
        // A turn of the user's owes nothing, and nothing is written for it.
        a.user(1);
        a.send(Command::TurnStarted { turn: TurnId(1) }).unwrap();
        a.finish(1);
        assert_eq!(a.agent.compaction_owed(), None);
        assert_eq!(
            a.send(said(1, 5)),
            Err(Refusal::NoCompactionOwed(TurnId(1)))
        );
        // The compaction: nothing owed while it runs, one outcome once it ended.
        a.send(compaction(2)).unwrap();
        assert_eq!(
            a.send(said(2, 5)),
            Err(Refusal::NoCompactionOwed(TurnId(2)))
        );
        a.send(Command::TurnStarted { turn: TurnId(2) }).unwrap();
        a.finish(2);
        assert_eq!(
            a.agent.compaction_owed().map(|owed| owed.id),
            Some(TurnId(2))
        );
        a.send(said(2, 41_250)).unwrap();
        assert_eq!(a.agent.compaction_owed(), None);
        assert_eq!(
            a.send(said(2, 5)),
            Err(Refusal::NoCompactionOwed(TurnId(2)))
        );
        let first = a.agent.last_compaction().cloned();
        assert_eq!(
            first.as_ref().and_then(|last| last.decided.before_tokens),
            Some(41_250)
        );
        // Later turns keep it, and so does a restart (`send` replays the history every time).
        a.user(3);
        a.send(Command::TurnStarted { turn: TurnId(3) }).unwrap();
        a.finish(3);
        assert_eq!(a.agent.last_compaction().cloned(), first);
        // One taken back before it began owes one too; the newest is kept.
        a.user(4);
        a.send(compaction(5)).unwrap();
        a.send(Command::Withdraw {
            turn: TurnId(5),
            by: "user".to_string(),
        })
        .unwrap();
        let owed = a.agent.compaction_owed().cloned().unwrap();
        assert_eq!(
            (owed.id, &owed.outcome),
            (
                TurnId(5),
                &Outcome::Withdrawn {
                    by: "user".to_string()
                }
            )
        );
        a.send(said(5, 9)).unwrap();
        assert_eq!(
            a.agent
                .last_compaction()
                .and_then(|last| last.decided.before_tokens),
            Some(9)
        );
        // An outcome nothing owes cannot be in the log.
        let mut fresh = Agent::new();
        let stray = AgentEvent::Compacted {
            turn: TurnId(1),
            said: first.unwrap(),
        };
        assert!(fresh.apply(&stray).is_err());
    }

    fn dropped(turn: u64) -> Command {
        Command::TurnParked {
            turn: TurnId(turn),
            wait: Wait::Retry {
                until_ms: 5_000,
                resume: false,
            },
            kind: "connection".to_string(),
            reason: "read ECONNRESET".to_string(),
        }
    }

    #[test]
    fn a_turn_that_waits_for_its_next_attempt_holds_the_agent_and_is_tried_again() {
        let mut a = Driven::new();
        a.user(10);
        a.user(11);
        a.send(Command::TurnStarted { turn: TurnId(1) }).unwrap();
        // The tool is gone and the turn is not over: nothing else starts.
        assert_eq!(a.send(dropped(1)).unwrap(), vec![]);
        assert_eq!((a.live(), a.waiting()), (Some(1), vec![2]));
        assert!(matches!(a.agent.live(), Some(Turn::Parked(parked)) if parked.attempts == 1));
        // What the attempt that is over still says is late news.
        assert_eq!(
            a.send(Command::TurnEnded {
                turn: TurnId(1),
                outcome: RunOutcome::Completed
            }),
            Err(Refusal::NotLive(TurnId(1)))
        );
        assert_eq!(
            a.send(Command::TurnMoved {
                turn: TurnId(1),
                activity: Activity::Thinking
            }),
            Err(Refusal::NotLive(TurnId(1)))
        );
        // The wait is over: the same turn, its second attempt.
        assert_eq!(
            a.send(Command::TurnRetried { turn: TurnId(1) }).unwrap(),
            vec![Effect::RetryTurn {
                turn: TurnId(1),
                message_seq: 10,
                attempt: 2
            }]
        );
        assert!(matches!(a.agent.live(), Some(Turn::Starting(starting)) if starting.attempt == 2));
        // A turn is tried again only while it waits.
        assert_eq!(
            a.send(Command::TurnRetried { turn: TurnId(1) }),
            Err(Refusal::NotParked(TurnId(1)))
        );
        a.send(Command::TurnStarted { turn: TurnId(1) }).unwrap();
        // It works this time, and the message that waited runs.
        assert_eq!(
            a.finish(1),
            vec![Effect::StartTurn {
                turn: TurnId(2),
                message_seq: 11
            }]
        );
        assert_eq!(a.agent.last().unwrap().outcome, Outcome::Completed);
    }

    #[test]
    fn only_a_turn_that_waits_for_the_network_has_it_come_back_or_stay_gone() {
        let mut a = Driven::new();
        a.user(10);
        a.user(11);
        // A turn that waits a moment, not for the network: neither is taken.
        a.send(dropped(1)).unwrap();
        assert_eq!(
            a.send(Command::NetworkBack { turn: TurnId(1) }),
            Err(Refusal::NotOffline(TurnId(1)))
        );
        assert_eq!(
            a.send(Command::NetworkGone { turn: TurnId(1) }),
            Err(Refusal::NotOffline(TurnId(1)))
        );
        a.send(Command::TurnRetried { turn: TurnId(1) }).unwrap();
        a.send(Command::TurnStarted { turn: TurnId(1) }).unwrap();
        // One that waits for the network: the network back starts the next attempt, which
        // is counted as one that followed an outage.
        a.send(Command::TurnParked {
            turn: TurnId(1),
            wait: Wait::Network {
                until_ms: 905_000,
                resume: true,
            },
            kind: "connection".to_string(),
            reason: "EAI_AGAIN".to_string(),
        })
        .unwrap();
        assert_eq!(
            a.send(Command::NetworkBack { turn: TurnId(1) }).unwrap(),
            vec![Effect::RetryTurn {
                turn: TurnId(1),
                message_seq: 10,
                attempt: 3
            }]
        );
        assert!(matches!(a.agent.live(), Some(Turn::Starting(starting))
            if starting.offline == 1 && starting.resuming));
    }

    #[test]
    fn a_wait_that_does_not_end_well_ends_the_turn_on_the_failure_it_waited_on() {
        let mut a = Driven::new();
        a.user(10);
        a.user(11);
        a.send(dropped(1)).unwrap();
        assert_eq!(
            a.send(Command::WaitOver { turn: TurnId(1) }).unwrap(),
            vec![Effect::StartTurn {
                turn: TurnId(2),
                message_seq: 11
            }]
        );
        assert_eq!(
            a.agent.last().unwrap().outcome,
            Outcome::Failed {
                reason: "read ECONNRESET".to_string(),
                kind: "connection".to_string()
            }
        );
        // Only a turn that waits can give up.
        assert_eq!(
            a.send(Command::WaitOver { turn: TurnId(2) }),
            Err(Refusal::NotParked(TurnId(2)))
        );
    }

    #[test]
    fn a_stop_ends_a_turn_that_waits_at_once_and_a_direct_message_takes_its_place() {
        // A stop: there is no tool to wait for, and what waits is held.
        let mut a = Driven::new();
        a.user(10);
        a.user(11);
        a.send(dropped(1)).unwrap();
        assert_eq!(
            a.send(Command::Stop {
                by: Control::by("user"),
            })
            .unwrap(),
            vec![]
        );
        assert_eq!(
            a.agent.last().unwrap().outcome,
            Outcome::stopped(Control::by("user"))
        );
        assert_eq!((a.live(), a.waiting()), (None, vec![2]));
        assert!(a.agent.is_paused());
        assert_eq!(a.agent.stopping(), None);

        // A direct message: the turn that waits makes way without a word from a tool.
        let mut a = Driven::new();
        a.user(10);
        a.send(dropped(1)).unwrap();
        assert_eq!(
            a.direct(11),
            vec![Effect::StartTurn {
                turn: TurnId(2),
                message_seq: 11
            }]
        );
        assert_eq!(
            a.agent.last().unwrap().outcome,
            Outcome::stopped(Control::by(preempted_by(TurnId(2))))
        );

        // A tool that is told to stop and then drops its connection was stopped: it is not
        // tried again.
        let mut a = Driven::new();
        a.user(10);
        a.send(Command::Stop {
            by: Control::by("user"),
        })
        .unwrap();
        a.send(dropped(1)).unwrap();
        assert_eq!(
            a.agent.last().unwrap().outcome,
            Outcome::stopped(Control::by("user"))
        );
        assert_eq!(a.live(), None);
    }

    #[test]
    fn a_message_to_a_free_agent_starts_at_once_and_the_next_waits_its_turn() {
        let mut a = Driven::new();
        assert_eq!(
            a.user(10),
            vec![Effect::StartTurn {
                turn: TurnId(1),
                message_seq: 10
            }]
        );
        assert_eq!(
            a.user(11),
            vec![],
            "the agent is busy: the second message waits"
        );
        assert_eq!(a.user(12), vec![]);
        assert_eq!((a.live(), a.waiting()), (Some(1), vec![2, 3]));
        a.send(Command::TurnStarted { turn: TurnId(1) }).unwrap();
        assert_eq!(
            a.finish(1),
            vec![Effect::StartTurn {
                turn: TurnId(2),
                message_seq: 11
            }],
            "oldest first"
        );
        assert_eq!(
            a.finish(2),
            vec![Effect::StartTurn {
                turn: TurnId(3),
                message_seq: 12
            }]
        );
        assert_eq!(a.finish(3), vec![]);
        assert_eq!(
            (a.live(), a.waiting(), a.agent.settled()),
            (None, vec![], 3)
        );
        assert_eq!(
            a.started,
            vec![TurnId(1), TurnId(2), TurnId(3)],
            "each accepted message started exactly once"
        );
    }

    #[test]
    fn a_stop_ends_the_live_turn_holds_the_mailbox_and_forgets_nothing() {
        let mut a = Driven::new();
        a.user(10);
        a.send(Command::TurnStarted { turn: TurnId(1) }).unwrap();
        a.user(11);
        a.user(12);
        assert_eq!(
            a.send(Command::Stop {
                by: Control::by("peter"),
            })
            .unwrap(),
            vec![Effect::StopTurn { turn: TurnId(1) }]
        );
        assert!(a.agent.is_paused());
        assert_eq!(
            a.live(),
            Some(1),
            "the turn is live until its process is gone"
        );
        // The process goes; whatever it reports, it went because it was told to.
        assert_eq!(a.finish(1), vec![], "the mailbox is held: nothing starts");
        assert!(
            matches!(a.history.last(), Some(AgentEvent::Turn { change: TurnEvent::Settled { outcome: Outcome::Stopped { by, .. }, .. }, .. }) if by == "peter")
        );
        assert_eq!((a.live(), a.waiting()), (None, vec![2, 3]));
        // Background work that comes now is held with the rest.
        assert_eq!(a.background(13), vec![]);
        assert!(a.agent.is_paused());
        assert!(a.agent.holds(TurnId(2)) && a.agent.holds(TurnId(4)));
        // A resume lets them run, the oldest first.
        assert_eq!(
            a.send(Command::Resume {
                by: "peter".to_string()
            })
            .unwrap(),
            vec![Effect::StartTurn {
                turn: TurnId(2),
                message_seq: 11
            }]
        );
        assert!(!a.agent.is_paused());
        assert_eq!(
            a.finish(2),
            vec![Effect::StartTurn {
                turn: TurnId(3),
                message_seq: 12
            }]
        );
        assert_eq!(
            a.finish(3),
            vec![Effect::StartTurn {
                turn: TurnId(4),
                message_seq: 13
            }]
        );
        a.finish(4);
        assert_eq!(
            a.started,
            vec![TurnId(1), TurnId(2), TurnId(3), TurnId(4)],
            "nothing was left behind by the stop"
        );
    }

    #[test]
    fn the_interruption_clock_comes_from_the_first_failure_even_without_a_poll() {
        let mut a = Driven::new();
        let failure = |turn| Command::TurnEnded {
            turn: TurnId(turn),
            outcome: RunOutcome::Failed {
                reason: "fixture failure".into(),
            },
        };

        a.user(10);
        a.send(failure(1)).unwrap();
        let first = a.now_ms;
        a.user(11);
        a.send(failure(2)).unwrap();
        assert_eq!(a.agent.interrupted_since_ms(), Some(first));

        a.user(12);
        a.finish(3);
        assert_eq!(a.agent.interrupted_since_ms(), None);
        a.user(13);
        a.send(failure(4)).unwrap();
        let after_completion = a.now_ms;
        assert_eq!(a.agent.interrupted_since_ms(), Some(after_completion));

        a.user(14);
        a.send(Command::TurnMoved {
            turn: TurnId(5),
            activity: Activity::Waiting,
        })
        .unwrap();
        assert_eq!(a.agent.interrupted_since_ms(), None);
        a.send(failure(5)).unwrap();
        assert_eq!(a.agent.interrupted_since_ms(), Some(a.now_ms));
    }

    #[test]
    fn an_oracle_send_passes_the_stop_and_releases_users_first_in_queue_order() {
        let mut a = Driven::new();
        a.user(10);
        a.background(11);
        a.user(12);
        a.user(13);
        a.send(Command::Stop {
            by: Control::by("user"),
        })
        .unwrap();
        assert!(a.finish(1).is_empty());
        assert_eq!(a.waiting(), vec![3, 4, 2]);
        assert!(a.agent.is_paused());

        a.send(Command::Accept {
            message_seq: 14,
            origin: Origin::Trigger {
                name: "oracle".into(),
            },
            admission: Admission::Queue,
            coordination: None,
            user_word: false,
        })
        .unwrap();
        assert_eq!(a.live(), Some(5));
        assert!(!a.agent.is_paused());
        for turn in [5, 3, 4, 2] {
            a.finish(turn);
        }
        assert_eq!(a.started, [1, 5, 3, 4, 2].map(TurnId));
    }

    #[test]
    fn a_fresh_send_to_a_busy_stopped_agent_waits_behind_older_user_messages() {
        let oracle = Origin::Trigger {
            name: "oracle".into(),
        };
        for origin in [Origin::User, oracle] {
            let mut a = Driven::new();
            a.user(10);
            a.background(11);
            a.user(12);
            a.user(13);
            a.send(Command::Stop {
                by: Control::by("user"),
            })
            .unwrap();
            assert_eq!(a.live(), Some(1));
            assert!(a.agent.is_paused());
            assert_eq!(a.waiting(), vec![3, 4, 2]);

            let effects = a
                .send(Command::Accept {
                    message_seq: 14,
                    origin,
                    admission: Admission::Queue,
                    coordination: None,
                    user_word: false,
                })
                .unwrap();
            assert!(effects.is_empty(), "the stopped process has not exited");
            assert!(!a.agent.is_paused());
            assert_eq!(a.waiting(), vec![3, 4, 5, 2]);
            for turn in [1, 3, 4, 5, 2] {
                a.finish(turn);
            }
            assert_eq!(a.started, [1, 3, 4, 5, 2].map(TurnId));
        }
    }

    #[test]
    fn issue_719_stop_releases_unread_user_sends_ahead_of_a_new_send() {
        let mut a = Driven::new();
        a.user(10);
        for (turn, seq) in [(2, 11), (3, 12)] {
            a.user(seq);
            a.send(Command::Join {
                turn: TurnId(turn),
                join: Join {
                    into: TurnId(1),
                    uuid: format!("unread-{turn}"),
                    recovery: None,
                    together: false,
                },
            })
            .unwrap();
        }
        a.send(Command::StopAndStartWaiting {
            by: Control::by("user"),
        })
        .unwrap();
        a.finish(1);
        assert_eq!(
            a.live(),
            Some(2),
            "the oldest unread send starts after Stop"
        );
        a.user(13);
        assert_eq!(a.waiting(), vec![3, 4]);
        assert_eq!(
            a.send(Command::Join {
                turn: TurnId(4),
                join: Join {
                    into: TurnId(2),
                    uuid: "newer".into(),
                    recovery: None,
                    together: false,
                },
            }),
            Err(Refusal::NotWaiting(TurnId(4))),
            "the new send cannot steer past the second released send"
        );
        for turn in [2, 3, 4] {
            a.finish(turn);
        }
        assert_eq!(a.started, [1, 2, 3, 4].map(TurnId));
        assert!(a.waiting().is_empty());
    }

    /// Issue 839: a newer message of the user's never steers into the live turn ahead of their
    /// older ones that still wait. Those go in first, in the order they were said.
    #[test]
    fn a_newer_message_steers_in_only_after_the_older_ones_that_wait() {
        let mut a = Driven::new();
        a.user(10);
        a.send(Command::TurnStarted { turn: TurnId(1) }).unwrap();
        for seq in [11, 12, 13, 14, 15] {
            a.user(seq);
        }
        assert_eq!(
            a.agent.older_words_waiting(TurnId(6)),
            [2, 3, 4, 5].map(TurnId)
        );
        let join = |turn: u64| Command::Join {
            turn: TurnId(turn),
            join: Join {
                into: TurnId(1),
                uuid: format!("steer-{turn}"),
                recovery: None,
                together: false,
            },
        };
        assert_eq!(a.send(join(6)), Err(Refusal::NotWaiting(TurnId(6))));
        assert_eq!(a.send(join(3)), Err(Refusal::NotWaiting(TurnId(3))));
        for turn in [2, 3, 4, 5, 6] {
            a.send(join(turn)).unwrap();
        }
        for turn in [2, 3, 4, 5, 6] {
            a.send(Command::JoinRead { turn: TurnId(turn) }).unwrap();
        }
        assert!(a.waiting().is_empty());
        // Another agent's message that waits is not the user's: it does not hold the user back.
        let mut a = Driven::new();
        a.user(10);
        a.send(Command::TurnStarted { turn: TurnId(1) }).unwrap();
        a.send(Command::Accept {
            message_seq: 11,
            origin: Origin::Agent {
                agent_id: "a-peer".into(),
            },
            admission: Admission::Later,
            coordination: None,
            user_word: false,
        })
        .unwrap();
        a.user(12);
        a.send(join(3)).unwrap();
    }

    /// Issue 747: the user's Stop lets their waiting messages run and holds the rest. When the
    /// last of those messages starts, the hold is still there: it ends with the user's next
    /// fresh message, so the queue a Stop leaves is the same whenever it is read.
    #[test]
    fn issue_747_the_messages_a_stop_lets_through_do_not_end_its_hold() {
        let mut a = Driven::new();
        a.user(10);
        a.user(11);
        a.user(12);
        a.send(Command::StopAndStartWaiting {
            by: Control::by("user"),
        })
        .unwrap();
        assert!(a.agent.is_paused());
        a.finish(1);
        assert_eq!((a.live(), a.waiting()), (Some(2), vec![3]));
        assert!(a.agent.is_paused());
        a.finish(2);
        assert_eq!((a.live(), a.waiting()), (Some(3), vec![]));
        assert!(a.agent.is_paused(), "the last one let through started");
        a.finish(3);
        assert!(a.agent.is_paused() && a.live().is_none());
        assert!(
            !a.history
                .iter()
                .any(|event| matches!(event, AgentEvent::Resumed { .. }))
        );
        // What comes now that is not the user's word waits for them.
        assert!(a.background(13).is_empty());
        assert_eq!(a.waiting(), vec![4]);
        // The user's next message ends the hold, runs first, and the held work follows it.
        a.user(14);
        assert!(!a.agent.is_paused());
        assert_eq!(a.live(), Some(5));
        a.finish(5);
        assert_eq!(a.live(), Some(4));
        assert_eq!(a.started, [1, 2, 3, 5, 4].map(TurnId));
        assert_eq!(Agent::replay(&a.history).unwrap(), a.agent);
    }

    #[test]
    fn a_fresh_follow_up_is_admitted_unpaused_and_an_old_read_repair_does_not_resume() {
        let mut a = Driven::new();
        a.user(10);
        a.user(11);
        a.send(Command::Join {
            turn: TurnId(2),
            join: Join {
                into: TurnId(1),
                uuid: "pre-stop".into(),
                recovery: None,
                together: false,
            },
        })
        .unwrap();
        a.send(Command::Stop {
            by: Control::by("user"),
        })
        .unwrap();
        a.send(Command::JoinRead { turn: TurnId(2) }).unwrap();
        assert!(a.waiting().is_empty());
        assert!(
            a.agent.is_paused(),
            "recovering the old read did not resume the queue"
        );
        a.finish(1);
        a.agent = Agent::replay(&a.history).unwrap();
        assert!(a.agent.is_paused());
        a.user(12);
        assert!(
            !a.agent.is_paused(),
            "the fresh user message also ends an empty hold"
        );

        a.user(13);
        a.user(14);
        a.send(Command::Stop {
            by: Control::by("user"),
        })
        .unwrap();
        a.finish(3);
        a.send(Command::SendNow {
            turn: TurnId(4),
            by: "user".into(),
        })
        .unwrap();
        assert!(
            a.agent.is_paused(),
            "Send Now does not release the other held message"
        );
        a.send(Command::Accept {
            message_seq: 15,
            origin: Origin::Trigger {
                name: "oracle".into(),
            },
            admission: Admission::Queue,
            coordination: None,
            user_word: false,
        })
        .unwrap();
        assert!(
            !a.agent.is_paused(),
            "a fresh send ends the busy agent's pause on admission"
        );
        assert_eq!(a.waiting(), vec![5, 6]);
        let join = |turn: u64, uuid: &str| Command::Join {
            turn: TurnId(turn),
            join: Join {
                into: TurnId(4),
                uuid: uuid.into(),
                recovery: None,
                together: false,
            },
        };
        // The user's older message, no longer held, goes in first (issue 839).
        assert_eq!(
            a.send(join(6, "post-stop")),
            Err(Refusal::NotWaiting(TurnId(6)))
        );
        a.send(join(5, "released")).unwrap();
        a.send(Command::JoinRead { turn: TurnId(5) }).unwrap();
        a.send(join(6, "post-stop")).unwrap();
        a.send(Command::JoinRead { turn: TurnId(6) }).unwrap();
        assert!(!a.agent.is_paused());
        assert!(a.waiting().is_empty());
        assert_eq!(a.live(), Some(4));
    }

    #[test]
    fn what_is_said_after_a_stop_runs_and_what_was_held_stays_held() {
        let mut a = Driven::new();
        a.user(10);
        a.user(11);
        a.send(Command::Stop {
            by: Control::by("peter"),
        })
        .unwrap();
        assert_eq!(a.finish(1), vec![], "held");
        let revision = a.agent.queue_revision();
        // The agent is free and someone speaks: that is its next turn, hold or no hold.
        assert_eq!(
            a.user(12),
            vec![Effect::StartTurn {
                turn: TurnId(3),
                message_seq: 12
            }]
        );
        assert!(a.agent.is_paused(), "the hold stays as it was");
        assert_eq!(
            a.agent.queue_revision(),
            revision,
            "a message that starts at once never waited"
        );
        // While that turn runs, another message comes: it waits behind the turn, not the hold.
        assert_eq!(a.user(13), vec![]);
        assert!(a.agent.holds(TurnId(2)) && !a.agent.holds(TurnId(4)));
        assert_eq!(
            a.finish(3),
            vec![Effect::StartTurn {
                turn: TurnId(4),
                message_seq: 13
            }],
            "it runs past what the hold holds"
        );
        // A second stop holds everything that waits, also what the first one let through.
        a.user(14);
        assert!(!a.agent.holds(TurnId(5)));
        a.send(Command::Stop {
            by: Control::by("peter"),
        })
        .unwrap();
        assert!(a.agent.holds(TurnId(5)));
        assert_eq!(a.finish(4), vec![], "held with the rest");
        assert_eq!(a.waiting(), vec![2, 5]);
        assert_eq!(
            a.send(Command::Resume {
                by: "peter".to_string()
            })
            .unwrap(),
            vec![Effect::StartTurn {
                turn: TurnId(2),
                message_seq: 11
            }],
            "a resume runs them oldest first"
        );
    }

    #[test]
    fn the_words_of_a_waiting_message_are_written_anew_and_it_keeps_its_place() {
        let mut a = Driven::new();
        a.user(10);
        a.user(11);
        a.user(12);
        let revision = a.agent.queue_revision();
        let anew = |turn: u64, message_seq: u64| Command::Reword {
            turn: TurnId(turn),
            message_seq,
        };
        assert_eq!(a.send(anew(2, 20)).unwrap(), vec![]);
        assert_eq!(a.waiting(), vec![2, 3], "it keeps its place");
        assert_eq!(a.agent.queue_revision(), revision + 1);
        // What runs, what has run and what never was has no words to change.
        for turn in [1, 9] {
            assert_eq!(
                a.send(anew(turn, 21)),
                Err(Refusal::NotWaiting(TurnId(turn)))
            );
        }
        // Its turn answers the words as they are now.
        assert_eq!(
            a.finish(1),
            vec![Effect::StartTurn {
                turn: TurnId(2),
                message_seq: 20
            }]
        );
        assert_eq!(a.send(anew(2, 22)), Err(Refusal::NotWaiting(TurnId(2))));
        // Written anew twice, the last words stand; and so they do while a hold holds them.
        a.send(stop()).unwrap();
        a.finish(2);
        a.send(anew(3, 23)).unwrap();
        a.send(anew(3, 24)).unwrap();
        assert!(a.agent.is_paused() && a.agent.holds(TurnId(3)));
        assert_eq!(
            a.send(by_hand(3)).unwrap(),
            vec![Effect::StartTurn {
                turn: TurnId(3),
                message_seq: 24
            }]
        );
    }

    fn by_hand(turn: u64) -> Command {
        Command::SendNow {
            turn: TurnId(turn),
            by: "peter".to_string(),
        }
    }

    fn stop() -> Command {
        Command::Stop {
            by: Control::by("peter"),
        }
    }

    fn take_over(turn: u64) -> Command {
        Command::TakeOver {
            turn: TurnId(turn),
            by: "user".to_string(),
        }
    }

    #[test]
    fn the_owners_waiting_message_takes_over_the_live_turn_and_runs_next_once() {
        let mut a = Driven::new();
        a.user(10);
        a.send(Command::TurnStarted { turn: TurnId(1) }).unwrap();
        a.user(11);
        a.user(12);
        let revision = a.agent.queue_revision();
        assert_eq!(
            a.send(take_over(3)).unwrap(),
            vec![Effect::StopTurn { turn: TurnId(1) }],
            "the live turn is told to make way"
        );
        assert!(!a.agent.is_paused(), "a take-over holds nothing");
        assert_eq!(a.waiting(), vec![3, 2], "ahead of what waits");
        assert!(a.agent.queue_revision() > revision);
        // The old process goes: the turn ended as stopped for the newer message, and the
        // message that took it over runs at once.
        assert_eq!(
            a.finish(1),
            vec![Effect::StartTurn {
                turn: TurnId(3),
                message_seq: 12
            }]
        );
        assert!(a.history.iter().any(|event| matches!(
            event,
            AgentEvent::Turn { turn: TurnId(1), change: TurnEvent::Settled { outcome: Outcome::Stopped { by, .. }, .. } }
                if *by == preempted_by(TurnId(3))
        )));
        assert!(!a.agent.is_paused());
        assert_eq!(a.agent.taking_over, None);
        // What else waited goes on by itself, in its order.
        assert_eq!(
            a.finish(3),
            vec![Effect::StartTurn {
                turn: TurnId(2),
                message_seq: 11
            }]
        );
        a.finish(2);
        assert_eq!(
            a.started,
            vec![TurnId(1), TurnId(3), TurnId(2)],
            "every message ran exactly once"
        );
    }

    #[test]
    fn a_message_that_took_over_runs_ahead_of_what_the_cut_turn_gives_back() {
        let mut a = two_gathered();
        a.user(14);
        assert_eq!(
            a.send(take_over(5)).unwrap(),
            vec![Effect::StopTurn { turn: TurnId(2) }]
        );
        a.send(Command::TurnEnded {
            turn: TurnId(2),
            outcome: RunOutcome::Interrupted {
                reason: "stopped".into(),
            },
        })
        .unwrap();
        assert_eq!(a.live(), Some(5), "the message that took over runs first");
        assert_eq!(
            a.waiting(),
            vec![3, 4],
            "what the cut turn had not begun waits behind it"
        );
        assert!(!a.agent.is_paused());
        // A build that knows only a send by hand replays the same history the same way: the
        // message is sent by hand again in the step that starts it.
        let sent = a
            .history
            .iter()
            .filter(|event| {
                matches!(
                    event,
                    AgentEvent::SentNow {
                        turn: TurnId(5),
                        ..
                    }
                )
            })
            .count();
        assert_eq!(sent, 2);
    }

    #[test]
    fn only_the_owners_message_that_still_waits_takes_over() {
        let mut a = Driven::new();
        a.user(10);
        a.send(Command::TurnStarted { turn: TurnId(1) }).unwrap();
        a.background(11);
        a.user(12);
        assert_eq!(a.send(take_over(9)), Err(Refusal::NotWaiting(TurnId(9))));
        assert_eq!(
            a.send(take_over(1)),
            Err(Refusal::NotWaiting(TurnId(1))),
            "it runs"
        );
        assert_eq!(
            a.send(take_over(2)),
            Err(Refusal::NotTheOwners(TurnId(2))),
            "a schedule's message does not cut a turn"
        );
        // One offered to the live turn's input is on its way into it.
        a.send(Command::Join {
            turn: TurnId(3),
            join: Join {
                into: TurnId(1),
                uuid: "steered-3".into(),
                recovery: None,
                together: false,
            },
        })
        .unwrap();
        assert_eq!(a.send(take_over(3)), Err(Refusal::NotWaiting(TurnId(3))));
        assert!(a.agent.stopping.is_none(), "nothing was cut");
    }

    #[test]
    fn a_stop_after_a_take_over_holds_the_message_and_with_nothing_running_none_is_taken_over() {
        let mut a = Driven::new();
        a.user(10);
        a.send(Command::TurnStarted { turn: TurnId(1) }).unwrap();
        a.user(11);
        a.send(take_over(2)).unwrap();
        // The user's Stop comes after it: the message is held with the rest.
        a.send(stop()).unwrap();
        assert_eq!(a.finish(1), vec![], "held");
        assert!(a.agent.is_paused() && a.agent.holds(TurnId(2)));
        // Nothing runs now: there is nothing to take over, and nothing changes.
        assert_eq!(a.send(take_over(2)), Err(Refusal::NoTurn));
        assert!(a.agent.is_paused() && a.agent.holds(TurnId(2)));
    }

    /// A turn of the user's with three of the user's messages waiting behind it.
    fn three_behind() -> Driven {
        let mut a = Driven::new();
        a.user(10);
        a.send(Command::TurnStarted { turn: TurnId(1) }).unwrap();
        for seq in [11, 12, 13] {
            a.user(seq);
        }
        a
    }

    #[test]
    fn a_turn_sent_by_hand_past_a_hold_reads_in_only_its_own_message() {
        let mut a = three_behind();
        a.send(stop()).unwrap();
        assert_eq!(a.finish(1), vec![], "held");
        assert_eq!(
            a.send(by_hand(2)).unwrap(),
            vec![Effect::StartTurn {
                turn: TurnId(2),
                message_seq: 11
            }]
        );
        assert!(
            a.agent.waiting_together().is_empty(),
            "nothing the hold keeps"
        );
        assert_eq!(a.waiting(), vec![3, 4]);
        assert!(a.agent.holds(TurnId(3)) && a.agent.holds(TurnId(4)));
    }

    #[test]
    fn a_take_over_after_a_stop_reads_in_only_its_own_message() {
        let mut a = three_behind();
        a.send(stop()).unwrap();
        a.send(take_over(2)).unwrap();
        assert_eq!(
            a.finish(1),
            vec![Effect::StartTurn {
                turn: TurnId(2),
                message_seq: 11
            }]
        );
        assert!(
            a.agent.waiting_together().is_empty(),
            "nothing the hold keeps"
        );
        assert_eq!(a.waiting(), vec![3, 4]);
        assert!(a.agent.holds(TurnId(3)) && a.agent.holds(TurnId(4)));
    }

    /// The user's Stop lets the user's waiting messages run (issue 747): they are not held,
    /// so the first that starts reads the others in with it, as without a Stop, and the pause
    /// stays for what comes after them (issue 875: the run that showed three messages in one
    /// turn after a Stop).
    #[test]
    fn the_users_stop_lets_its_waiting_messages_run_read_together() {
        let mut a = three_behind();
        a.send(Command::StopAndStartWaiting {
            by: Control::by("user"),
        })
        .unwrap();
        assert_eq!(
            a.finish(1),
            vec![Effect::StartTurn {
                turn: TurnId(2),
                message_seq: 11
            }]
        );
        assert_eq!(a.agent.waiting_together(), vec![TurnId(3), TurnId(4)]);
        assert!(a.agent.is_paused());
    }

    #[test]
    fn without_a_hold_a_turn_reads_in_the_users_messages_behind_it_as_before() {
        let mut a = three_behind();
        a.finish(1);
        assert_eq!(a.live(), Some(2));
        assert_eq!(a.agent.waiting_together(), vec![TurnId(3), TurnId(4)]);
    }

    #[test]
    fn a_waiting_message_is_sent_by_hand_past_a_hold_and_the_hold_stays_after_the_last() {
        let mut a = Driven::new();
        a.user(10);
        a.user(11);
        a.user(12);
        // A turn runs: nothing that waits is sent past it.
        assert_eq!(a.send(by_hand(3)), Err(Refusal::AtWork));
        a.send(stop()).unwrap();
        assert_eq!(a.finish(1), vec![], "held");
        assert_eq!(a.waiting(), vec![2, 3]);
        let revision = a.agent.queue_revision();
        // The user sends the later of the two by hand: it runs now, and the other stays held.
        assert_eq!(
            a.send(by_hand(3)).unwrap(),
            vec![Effect::StartTurn {
                turn: TurnId(3),
                message_seq: 12
            }]
        );
        assert!(a.agent.is_paused() && a.agent.holds(TurnId(2)));
        assert!(a.agent.live_from_queue());
        assert_eq!(
            a.agent.queue_revision(),
            revision + 2,
            "it was taken out of its place, and it left the queue"
        );
        // What does not wait is not sent, and nothing is while a turn runs.
        assert_eq!(a.send(by_hand(3)), Err(Refusal::NotWaiting(TurnId(3))));
        assert_eq!(a.send(by_hand(9)), Err(Refusal::NotWaiting(TurnId(9))));
        assert_eq!(a.send(by_hand(2)), Err(Refusal::AtWork));
        assert_eq!(
            a.agent.queue_revision(),
            revision + 2,
            "what was refused changed nothing"
        );
        assert_eq!(a.finish(3), vec![], "the hold holds what is left");
        // The last one is sent by hand: nothing waits any more, and the hold stays all the
        // same. A Send Now runs one message past it and ends nothing.
        assert_eq!(
            a.send(by_hand(2)).unwrap(),
            vec![Effect::StartTurn {
                turn: TurnId(2),
                message_seq: 11
            }]
        );
        assert!(a.agent.is_paused() && a.waiting().is_empty());
        assert_eq!(
            a.agent.queue_revision(),
            revision + 4,
            "taken out of its place, and it left"
        );
        assert!(
            !a.history
                .iter()
                .any(|event| matches!(event, AgentEvent::Resumed { .. }))
        );
        a.finish(2);
        // Background work that comes now waits for the user: the hold still holds it.
        assert_eq!(a.background(13), vec![]);
        assert_eq!(a.waiting(), vec![4]);
        assert_eq!(a.started, vec![TurnId(1), TurnId(3), TurnId(2)]);
        assert_eq!(Agent::replay(&a.history).unwrap(), a.agent);
    }

    #[test]
    fn a_hold_ends_when_the_last_thing_it_held_is_taken_back() {
        let mut a = Driven::new();
        a.user(10);
        a.user(11);
        a.user(12);
        a.send(stop()).unwrap();
        a.finish(1);
        let take_back = |turn: u64| Command::Withdraw {
            turn: TurnId(turn),
            by: "peter".to_string(),
        };
        a.send(take_back(2)).unwrap();
        assert!(a.agent.is_paused(), "one still waits");
        let revision = a.agent.queue_revision();
        a.send(take_back(3)).unwrap();
        assert!(!a.agent.is_paused());
        assert_eq!(
            a.agent.queue_revision(),
            revision + 2,
            "taken back, and the hold ended"
        );
        assert!(matches!(a.history.last(), Some(AgentEvent::Resumed { by, .. }) if by == "peter"));
        assert_eq!((a.live(), a.waiting()), (None, vec![]));
        // Taken back where nothing is held, nothing else changes.
        a.user(13);
        a.user(14);
        let revision = a.agent.queue_revision();
        a.send(take_back(5)).unwrap();
        assert_eq!(a.agent.queue_revision(), revision + 1);
        assert!(!a.agent.is_paused());
    }

    #[test]
    fn a_stop_that_holds_nothing_ends_with_the_first_thing_that_goes_through_the_queue() {
        let mut a = Driven::new();
        a.user(10);
        a.send(stop()).unwrap();
        a.finish(1);
        assert!(a.agent.is_paused() && a.waiting().is_empty());
        // What a person says to a free agent never waited: the hold stays.
        a.user(11);
        assert!(a.agent.is_paused());
        a.finish(2);
        assert!(a.agent.is_paused());
        // What is left for later goes through the queue, and past a hold that was placed
        // before it came. Nothing waits behind it: the hold holds nothing, and is over.
        let later = Command::Accept {
            message_seq: 12,
            origin: Origin::User,
            admission: Admission::Later,
            coordination: None,
            user_word: false,
        };
        assert_eq!(
            a.send(later).unwrap(),
            vec![Effect::StartTurn {
                turn: TurnId(3),
                message_seq: 12
            }]
        );
        assert!(!a.agent.is_paused());
        assert!(matches!(a.history.last(), Some(AgentEvent::Resumed { by, .. }) if by == DRAINED));
    }

    /// Issue 839: the owner's message never waits behind a turn that is not a person's. The
    /// Host admits it direct where the kernel says it goes first: the other turn makes way at
    /// once and the owner's turn starts; behind the owner's own turn it waits as before.
    #[test]
    fn the_owners_message_goes_ahead_of_a_turn_that_is_not_a_persons() {
        let from = |origin: Origin, user_word: bool| {
            move |message_seq| Command::Accept {
                message_seq,
                origin: origin.clone(),
                admission: Admission::Later,
                coordination: None,
                user_word,
            }
        };
        let peer = from(
            Origin::Agent {
                agent_id: "a-peer".into(),
            },
            false,
        );
        let mut a = Driven::new();
        a.send(peer(10)).unwrap();
        a.send(Command::TurnStarted { turn: TurnId(1) }).unwrap();
        assert!(a.agent.owner_goes_first(&Origin::User));
        assert!(a.agent.owner_goes_first(&Origin::Trigger {
            name: "oracle".into()
        }));
        // Another agent's message, or the Host's own, still waits its turn.
        assert!(!a.agent.owner_goes_first(&Origin::Agent {
            agent_id: "a-other".into()
        }));
        assert!(!a.agent.owner_goes_first(&Origin::Trigger {
            name: "schedule".into()
        }));
        a.send(peer(11)).unwrap();
        assert_eq!(
            a.direct(12),
            vec![Effect::StopTurn { turn: TurnId(1) }],
            "the other agent's turn is told to make way"
        );
        // Once it is on its way out, a second message of the owner's makes nothing go again.
        assert!(!a.agent.owner_goes_first(&Origin::User));
        // The Host leaves the agent a note to take the cut work up again, behind the owner.
        a.background(13);
        assert_eq!(
            a.finish(1),
            vec![Effect::StartTurn {
                turn: TurnId(3),
                message_seq: 12
            }],
            "the owner's turn starts as soon as the other is gone"
        );
        assert!(a.history.iter().any(|event| matches!(
            event,
            AgentEvent::Turn { turn: TurnId(1), change: TurnEvent::Settled { outcome: Outcome::Stopped { by, .. }, .. } }
                if *by == preempted_by(TurnId(3))
        )));
        assert_eq!(
            a.waiting(),
            vec![2, 4],
            "the rest wait in order, the note last"
        );
        // Behind the owner's own turn the owner's next message waits as before.
        assert!(!a.agent.owner_goes_first(&Origin::User));
        a.finish(3);
        a.finish(2);
        assert_eq!(a.started, vec![TurnId(1), TurnId(3), TurnId(2), TurnId(4)]);

        // A turn that carries the owner's word (a front desk's relay) is the owner's.
        let mut a = Driven::new();
        a.send(from(
            Origin::Agent {
                agent_id: "a-desk".into(),
            },
            true,
        )(10))
            .unwrap();
        assert!(!a.agent.owner_goes_first(&Origin::User));
        // So is the owner's own turn, and a compaction is left to finish.
        let mut a = Driven::new();
        a.user(10);
        assert!(!a.agent.owner_goes_first(&Origin::User));
        let mut a = Driven::new();
        a.send(from(
            Origin::Trigger {
                name: COMPACTION.into(),
            },
            false,
        )(10))
            .unwrap();
        assert!(!a.agent.owner_goes_first(&Origin::User));
        // A trigger's turn (a schedule, a goal's wake, a janitor) makes way like a peer's.
        let mut a = Driven::new();
        a.background(10);
        assert!(a.agent.owner_goes_first(&Origin::User));
        // A Codex native goal's turn is the owner's goal at work: a cut would pause it.
        let mut a = Driven::new();
        a.send(from(Origin::NativeGoal, false)(10)).unwrap();
        assert!(!a.agent.owner_goes_first(&Origin::User));
        // A free agent has nothing to make way.
        assert!(!Driven::new().agent.owner_goes_first(&Origin::User));
    }

    /// Issue 918: what precedes a message of the owner's said now is what the mailbox puts in
    /// front of his word, not every waiting message: another agent's messages and automation
    /// wait behind it, his own earlier words and a front desk's relay of them go first, and what
    /// a Stop holds does not run before it.
    #[test]
    fn only_what_goes_before_the_owners_word_is_ahead_of_him() {
        let peer = |message_seq, user_word| Command::Accept {
            message_seq,
            origin: Origin::Agent {
                agent_id: "a-peer".into(),
            },
            admission: Admission::Later,
            coordination: Some("a-peer".into()),
            user_word,
        };
        let mut a = Driven::new();
        assert_eq!(a.agent.ahead_of_owner(), 0, "nothing waits");
        a.background(10);
        a.send(peer(11, false)).unwrap();
        a.send(peer(12, false)).unwrap();
        assert_eq!(a.waiting(), vec![2, 3]);
        assert_eq!(
            a.agent.ahead_of_owner(),
            0,
            "other agents' messages wait behind the owner's word"
        );
        a.user(13);
        assert_eq!(a.waiting(), vec![4, 2, 3]);
        assert_eq!(a.agent.ahead_of_owner(), 1, "his own earlier word");
        a.send(peer(14, true)).unwrap();
        assert_eq!(a.waiting(), vec![4, 5, 2, 3]);
        assert_eq!(
            a.agent.ahead_of_owner(),
            2,
            "and a front desk's relay of it"
        );
        // A Stop holds what waited; a relay of his word after it is not held, and runs first.
        a.send(stop()).unwrap();
        assert!(a.agent.is_paused());
        assert_eq!(a.agent.ahead_of_owner(), 0, "what a Stop holds");
        a.send(peer(15, true)).unwrap();
        assert_eq!(a.waiting(), vec![4, 5, 6, 2, 3]);
        assert_eq!(a.agent.ahead_of_owner(), 1);
    }

    /// Peter, 25 Nov 2026: the owner's message is steered into a running turn that is not his
    /// where that turn can read it now; only where it cannot does the turn make way.
    #[test]
    fn the_owners_words_steer_into_another_agents_turn_only_where_it_can_take_them() {
        let peer = |message_seq| Command::Accept {
            message_seq,
            origin: Origin::Agent {
                agent_id: "a-peer".into(),
            },
            admission: Admission::Later,
            coordination: None,
            user_word: false,
        };
        // Nothing runs: there is no turn to read them.
        assert!(!Driven::new().agent.live_takes_words());
        let mut a = Driven::new();
        a.send(peer(10)).unwrap();
        a.send(Command::TurnStarted { turn: TurnId(1) }).unwrap();
        assert!(a.agent.owner_goes_first(&Origin::User));
        assert!(a.agent.live_takes_words());
        // An older word of the owner's that waits unread goes in first, or the newer one waits:
        // the turn makes way instead.
        a.user(11);
        assert!(!a.agent.live_takes_words());
        // A turn on its way out reads nothing more.
        let mut a = Driven::new();
        a.send(peer(10)).unwrap();
        a.send(Command::TurnStarted { turn: TurnId(1) }).unwrap();
        a.send(stop()).unwrap();
        assert!(!a.agent.live_takes_words());
    }

    /// The owner's Stop after a message that went first ends that turn and holds the note
    /// the Host left behind it, as any Stop holds background work.
    #[test]
    fn the_owners_stop_after_going_first_holds_the_note() {
        let mut a = Driven::new();
        a.send(Command::Accept {
            message_seq: 10,
            origin: Origin::Agent {
                agent_id: "a-peer".into(),
            },
            admission: Admission::Later,
            coordination: None,
            user_word: false,
        })
        .unwrap();
        a.send(Command::TurnStarted { turn: TurnId(1) }).unwrap();
        a.direct(11);
        a.background(12);
        a.finish(1);
        assert_eq!(a.live(), Some(2));
        a.send(Command::Stop {
            by: Control::by("user"),
        })
        .unwrap();
        assert_eq!(a.finish(2), vec![], "nothing runs after the owner's Stop");
        assert!(a.agent.is_paused());
        assert!(a.agent.holds(TurnId(3)));
    }

    #[test]
    fn a_direct_message_ends_the_live_turn_and_runs_ahead_of_what_waits() {
        let mut a = Driven::new();
        a.user(10);
        a.send(Command::TurnStarted { turn: TurnId(1) }).unwrap();
        a.user(11);
        assert_eq!(
            a.direct(12),
            vec![Effect::StopTurn { turn: TurnId(1) }],
            "the live turn is told to make way"
        );
        assert!(
            !a.agent.is_paused(),
            "a direct message does not hold the mailbox"
        );
        assert_eq!(a.waiting(), vec![3, 2], "ahead of what waits its turn");
        // A second one before the first has started: behind it, and nothing more to stop.
        assert_eq!(a.direct(13), vec![]);
        assert_eq!(a.waiting(), vec![3, 4, 2]);
        // The old process goes. Whatever it says, it went because it was replaced.
        assert_eq!(
            a.finish(1),
            vec![Effect::StartTurn {
                turn: TurnId(3),
                message_seq: 12
            }]
        );
        assert!(a.history.iter().any(|event| matches!(
            event,
            AgentEvent::Turn { turn: TurnId(1), change: TurnEvent::Settled { outcome: Outcome::Stopped { by, .. }, .. } }
                if *by == preempted_by(TurnId(3))
        )));
        // The second direct message does not end the first: both run, in order, then the queue.
        assert_eq!(
            a.finish(3),
            vec![Effect::StartTurn {
                turn: TurnId(4),
                message_seq: 13
            }]
        );
        assert_eq!(
            a.finish(4),
            vec![Effect::StartTurn {
                turn: TurnId(2),
                message_seq: 11
            }]
        );
        a.finish(2);
        assert_eq!(
            a.started,
            vec![TurnId(1), TurnId(3), TurnId(4), TurnId(2)],
            "every message ran exactly once"
        );
    }

    #[test]
    fn replaying_a_refused_native_session_binding_clears_the_previous_one() {
        let mut a = Driven::new();
        for value in ["kept-id", "_obsolete"] {
            a.send(Command::Adopt {
                from: "elsewhere".to_owned(),
                backend_session: Some(value.to_owned()),
            })
            .unwrap();
        }
        assert_eq!(a.agent.backend_session(), None);
        a.user(10);
        a.send(Command::TurnStarted { turn: TurnId(1) }).unwrap();
        for value in ["kept-id", "_obsolete"] {
            a.send(Command::TurnBound {
                turn: TurnId(1),
                backend_session: value.to_owned(),
            })
            .unwrap();
        }
        a.finish(1);
        assert_eq!(a.agent.backend_session(), None);
    }

    #[test]
    fn an_agent_started_again_no_longer_continues_its_tools_session() {
        let bound = |turn: u64, session: &str| Command::TurnBound {
            turn: TurnId(turn),
            backend_session: session.to_string(),
        };
        let mut a = Driven::new();
        // Never at work, it is started again all the same: there is nothing to forget.
        assert_eq!(a.send(Command::StartAgain).unwrap(), vec![]);
        assert_eq!(a.agent.queue_revision(), 2);
        assert!(!a.agent.is_paused());
        a.user(10);
        a.send(Command::TurnStarted { turn: TurnId(1) }).unwrap();
        a.send(bound(1, "sess-1")).unwrap();
        assert_eq!(a.agent.backend_session(), Some("sess-1"));
        a.user(11);
        // While a turn of it runs it is not started again: that turn is stopped first.
        let revision = a.agent.queue_revision();
        assert_eq!(a.send(Command::StartAgain), Err(Refusal::AtWork));
        assert_eq!(a.agent.queue_revision(), revision);
        a.send(Command::Stop {
            by: Control::for_relaunch(),
        })
        .unwrap();
        assert_eq!(a.agent.queue_revision(), revision + 1);
        a.finish(1);
        assert_eq!((a.live(), a.waiting()), (None, vec![2]));
        assert_eq!(a.send(Command::StartAgain).unwrap(), vec![]);
        assert!(matches!(
            a.history.last(),
            Some(AgentEvent::StartedAgain { .. })
        ));
        assert_eq!(
            a.agent.backend_session(),
            None,
            "its next turn begins a conversation with its tool"
        );
        // What waits still waits, and how the last turn ended is still known.
        assert_eq!(a.waiting(), vec![2]);
        assert!(a.agent.last().is_some());
        assert_eq!(a.agent.queue_revision(), revision + 1);
        // Let go, the waiting message runs, and the session its tool names is the agent's.
        let resumed = a.send(Command::Resume {
            by: "relaunch".to_string(),
        });
        assert_eq!(
            resumed.unwrap(),
            vec![Effect::StartTurn {
                turn: TurnId(2),
                message_seq: 11
            }]
        );
        assert_eq!(a.agent.queue_revision(), revision + 3);
        a.send(Command::TurnStarted { turn: TurnId(2) }).unwrap();
        a.send(bound(2, "sess-2")).unwrap();
        assert_eq!(a.agent.backend_session(), Some("sess-2"));
    }

    #[test]
    fn relaunch_keeps_an_existing_pause_without_counting_another() {
        let mut a = Driven::new();
        a.user(10);
        a.send(Command::TurnStarted { turn: TurnId(1) }).unwrap();
        a.send(Command::Stop {
            by: Control::by("move"),
        })
        .unwrap();
        a.user(11);
        assert!(a.agent.unheld.contains(&TurnId(2)));
        let revision = a.agent.queue_revision();
        a.send(Command::Stop {
            by: Control::for_relaunch(),
        })
        .unwrap();
        a.finish(1);
        a.send(Command::StartAgain).unwrap();
        assert!(a.agent.is_paused());
        assert!(a.agent.unheld.is_empty());
        assert_eq!(a.waiting(), vec![2]);
        assert_eq!(a.agent.queue_revision(), revision);
    }

    #[test]
    fn an_agent_that_arrives_from_another_host_continues_its_tools_session() {
        let mut a = Driven::new();
        a.send(Command::Adopt {
            from: "http://elitebook:7682".to_string(),
            backend_session: Some("sess-9".to_string()),
        })
        .unwrap();
        assert_eq!(a.agent.backend_session(), Some("sess-9"));
        a.user(10);
        a.finish(1);
        assert_eq!(
            a.send(Command::Adopt {
                from: "elsewhere".to_string(),
                backend_session: None,
            }),
            Err(Refusal::AlreadyAtWork),
            "an agent arrives once, before it works"
        );
        assert_eq!(a.agent.backend_session(), Some("sess-9"));
        assert!(a.agent.arrived());
    }

    #[test]
    fn agent_transit_only_at_rest_keeps_what_the_agent_was_given() {
        let change = |session: Option<&str>| Command::ChangeTool {
            from: "claude".to_string(),
            backend: "codex".to_string(),
            selection: None,
            backend_session: session.map(str::to_string),
            opening: session.is_none().then(|| "the record".to_string()),
        };
        let bound = |turn: u64, session: &str| Command::TurnBound {
            turn: TurnId(turn),
            backend_session: session.to_string(),
        };
        let mut a = Driven::new();
        a.user(10);
        a.send(Command::TurnStarted { turn: TurnId(1) }).unwrap();
        a.send(bound(1, "claude-1")).unwrap();
        assert_eq!(a.send(change(Some("thread-1"))), Err(Refusal::AtWork));
        a.user(11);
        a.finish(1);
        // The message that waited runs now: still not at rest.
        assert!(a.send(change(Some("thread-1"))).is_err());
        a.send(Command::TurnStarted { turn: TurnId(2) }).unwrap();
        a.finish(2);
        assert_eq!(a.agent.backend_session(), Some("claude-1"));

        a.send(change(Some("thread-1"))).unwrap();
        assert_eq!(a.agent.backend_session(), Some("thread-1"));
        assert_eq!(a.agent.carried_from(), None);
        assert_eq!(a.agent.changed(), Some(("claude", "codex")));

        // Changed with no session of the new tool: its next turn begins one with the record,
        // until a turn names its session.
        a.send(change(None)).unwrap();
        assert_eq!(a.agent.backend_session(), None);
        assert_eq!(a.agent.carried_from(), Some("claude"));
        assert_eq!(a.agent.carried_opening(), Some("the record"));
        a.user(12);
        a.send(Command::TurnStarted { turn: TurnId(3) }).unwrap();
        a.send(bound(3, "fresh-3")).unwrap();
        assert_eq!(a.agent.carried_from(), None);
        assert_eq!(a.agent.carried_opening(), None);
        a.finish(3);

        // Started again, the change is no longer the agent's last word on its tool.
        a.send(Command::StartAgain).unwrap();
        assert_eq!(a.agent.changed(), None);
    }

    #[test]
    fn agent_transit_keeps_a_user_stop_pause_across_the_backend_change() {
        let mut a = Driven::new();
        a.user(10);
        a.send(Command::Stop {
            by: Control::by("user"),
        })
        .unwrap();
        a.send(Command::TurnStarted { turn: TurnId(1) }).unwrap();
        a.finish(1);
        a.background(11);
        assert_eq!(a.live(), None, "a held mailbox starts nothing");
        assert_eq!(a.waiting(), vec![2]);
        a.send_with_transit_hold(
            Command::ChangeTool {
                from: "claude".to_string(),
                backend: "codex".to_string(),
                selection: None,
                backend_session: Some("thread-1".to_string()),
                opening: None,
            },
            true,
        )
        .unwrap();
        a.send(Command::RunWaiting).unwrap();
        assert_eq!(a.agent.changed(), Some(("claude", "codex")));
        assert_eq!(a.live(), None);
        assert_eq!(a.waiting(), vec![2]);
        a.send(Command::Resume { by: "user".into() }).unwrap();
        assert_eq!(a.live(), Some(2));
    }

    #[test]
    fn agent_transit_returns_only_an_unstarted_account_wait_to_the_front() {
        for provider_started in [false, true] {
            let mut a = Driven::new();
            a.user(10);
            a.user(11);
            a.send(Command::TurnParked {
                turn: TurnId(1),
                wait: crate::turn::Wait::account(90_000),
                kind: if provider_started {
                    crate::failures::USAGE_LIMIT
                } else {
                    crate::failures::NOT_STARTED
                }
                .to_string(),
                reason: "waiting for an account".to_string(),
            })
            .unwrap();

            let hold = Command::HoldUnsentForTransit { turn: TurnId(1) };
            let held = a.send_with_transit_hold(hold, true);
            if provider_started {
                assert_eq!(held, Err(Refusal::UnsentNotProven(TurnId(1))));
                assert_eq!(a.live(), Some(1));
                assert_eq!(a.waiting(), vec![2]);
            } else {
                assert!(held.unwrap().is_empty());
                assert_eq!(a.live(), None);
                assert_eq!(a.waiting(), vec![1, 2]);
                assert_eq!(
                    a.send(Command::RunWaiting).unwrap(),
                    vec![Effect::StartTurn {
                        turn: TurnId(1),
                        message_seq: 10
                    }]
                );
            }
        }
    }

    #[test]
    fn transit_resumes_parked_work_on_the_target_before_draining_the_queue() {
        let mut a = Driven::new();
        a.user(10);
        a.user(11);
        a.send(Command::TurnStarted { turn: TurnId(1) }).unwrap();
        a.send(Command::TurnMoved {
            turn: TurnId(1),
            activity: Activity::Responding,
        })
        .unwrap();
        let park = |turn| Command::TurnParked {
            turn,
            wait: Wait::account(90_000),
            reason: "source usage exhausted".to_string(),
            kind: crate::failures::USAGE_LIMIT.to_string(),
        };
        a.send(park(TurnId(1))).unwrap();
        let change = Command::ChangeTool {
            from: "claude".into(),
            backend: "codex".into(),
            selection: Some(ToolSelection {
                model: "gpt-6.2-sol".into(),
                effort: "medium".into(),
            }),
            backend_session: Some("new-codex-thread".into()),
            opening: None,
        };
        assert_eq!(a.send(change.clone()), Err(Refusal::AtWork));
        assert!(a.send_with_transit_hold(change, true).unwrap().is_empty());
        assert_eq!(a.agent.transit_resume(), Some(TurnId(1)));
        assert_eq!(a.agent.backend_session(), Some("new-codex-thread"));
        assert_eq!(a.waiting(), vec![2]);

        assert_eq!(
            a.send(Command::TurnRetried { turn: TurnId(1) }).unwrap(),
            vec![Effect::RetryTurn {
                turn: TurnId(1),
                message_seq: 10,
                attempt: 2
            }]
        );
        assert!(a.agent.resumed_by_transit(TurnId(1), 2));
        assert_eq!(a.agent.transit_resume(), None);
        a.send(park(TurnId(1))).unwrap();
        assert_eq!(
            a.agent.transit_resume(),
            None,
            "a new target wait is a distinct wait"
        );
        a.send(Command::TurnRetried { turn: TurnId(1) }).unwrap();
        assert!(!a.agent.resumed_by_transit(TurnId(1), 3));
        a.send(Command::TurnStarted { turn: TurnId(1) }).unwrap();
        assert_eq!(
            a.finish(1),
            vec![Effect::StartTurn {
                turn: TurnId(2),
                message_seq: 11
            }]
        );
    }

    #[test]
    fn an_agent_that_begins_with_a_session_its_tool_kept_continues_it_and_is_new() {
        let mut a = Driven::new();
        a.send(Command::TakeUp {
            backend_session: "kept-1".to_string(),
        })
        .unwrap();
        assert_eq!(a.agent.backend_session(), Some("kept-1"));
        assert!(!a.agent.arrived(), "it has worked nowhere");
        a.user(10);
        a.finish(1);
        assert_eq!(
            a.send(Command::TakeUp {
                backend_session: "kept-2".to_string(),
            }),
            Err(Refusal::AlreadyAtWork),
            "a session is taken up once, before the agent works"
        );
        assert_eq!(a.agent.backend_session(), Some("kept-1"));
    }

    #[test]
    fn the_queue_has_a_revision_that_counts_only_what_waited() {
        let mut a = Driven::new();
        a.user(10);
        assert_eq!(
            a.agent.queue_revision(),
            0,
            "a message that starts at once never waited"
        );
        a.user(11);
        assert_eq!(a.agent.queue_revision(), 1, "a message joined the queue");
        a.send(Command::Stop {
            by: Control::by("peter"),
        })
        .unwrap();
        assert_eq!(a.agent.queue_revision(), 2, "the queue is held");
        a.finish(1);
        assert_eq!(
            a.agent.queue_revision(),
            2,
            "a turn that ends changes nothing about the queue"
        );
        a.send(Command::Resume {
            by: "peter".to_string(),
        })
        .unwrap();
        assert_eq!(
            a.agent.queue_revision(),
            4,
            "let go, and the waiting message left it to run"
        );
        a.user(12);
        a.send(Command::Withdraw {
            turn: TurnId(3),
            by: "peter".to_string(),
        })
        .unwrap();
        assert_eq!(a.agent.queue_revision(), 6, "joined, and was taken back");
    }

    #[test]
    fn what_is_left_for_later_goes_through_the_queue_even_when_nothing_is_in_its_way() {
        let mut a = Driven::new();
        assert_eq!(
            a.background(10),
            vec![Effect::StartTurn {
                turn: TurnId(1),
                message_seq: 10
            }]
        );
        assert_eq!(a.agent.queue_revision(), 2, "it joined, and left to run");
        assert!(a.agent.live_from_queue());
        a.finish(1);
        assert!(!a.agent.live_from_queue(), "no turn is live");
        // What is said to a free agent never waited.
        a.user(11);
        assert_eq!(a.agent.queue_revision(), 2);
        assert!(!a.agent.live_from_queue());
        // What is said to a busy one did.
        a.user(12);
        a.finish(2);
        assert_eq!(a.agent.queue_revision(), 4, "it joined, and left to run");
        assert!(a.agent.live_from_queue());
    }

    #[test]
    fn a_replayed_queue_runs_the_owners_words_before_waiting_agents() {
        let agent = |message_seq| Command::Accept {
            message_seq,
            origin: Origin::Agent {
                agent_id: "peer".to_string(),
            },
            admission: Admission::Later,
            coordination: None,
            user_word: false,
        };
        let mut a = Driven::new();
        a.user(1);
        a.send(agent(2)).unwrap();
        let legacy = AgentEvent::Turn {
            turn: TurnId(3),
            change: TurnEvent::Accepted {
                turn: TurnId(3),
                message_seq: 3,
                origin: Origin::User,
                admission: Admission::Queue,
                at_ms: 3,
                coordination: None,
                user_word: None,
            },
        };
        a.agent.apply(&legacy).unwrap();
        a.history.push(legacy);
        a.send(agent(4)).unwrap();
        a.user(5);
        let waiting: Vec<_> = a.agent.waiting().map(|queued| queued.id).collect();
        assert_eq!(waiting, [TurnId(2), TurnId(3), TurnId(5), TurnId(4)]);
        let ahead = a.agent.ahead_of_owner();
        for (finished, starts) in [(1, 3), (3, 5), (5, 2), (2, 4)] {
            assert_eq!(
                a.finish(finished),
                vec![Effect::StartTurn {
                    turn: TurnId(starts),
                    message_seq: starts,
                }],
            );
        }
        assert_eq!(ahead, 2);
    }

    #[test]
    fn a_direct_message_runs_past_a_stop_and_leaves_the_rest_held() {
        let mut a = Driven::new();
        a.user(10);
        a.user(11);
        a.send(Command::Stop {
            by: Control::by("peter"),
        })
        .unwrap();
        assert_eq!(a.finish(1), vec![], "held");
        assert_eq!(
            a.direct(12),
            vec![Effect::StartTurn {
                turn: TurnId(3),
                message_seq: 12
            }],
            "the user's next message runs although the mailbox is held"
        );
        assert!(a.agent.is_paused(), "and the hold stays as it was");
        assert_eq!(
            a.finish(3),
            vec![],
            "the held message still waits for a resume"
        );
        assert_eq!(a.waiting(), vec![2]);
        assert_eq!(
            a.send(Command::Resume {
                by: "peter".to_string()
            })
            .unwrap(),
            vec![Effect::StartTurn {
                turn: TurnId(2),
                message_seq: 11
            }]
        );
    }

    #[test]
    fn background_work_does_not_get_past_a_stop_but_a_resume_lets_it_run() {
        let mut a = Driven::new();
        a.send(Command::Stop {
            by: Control::by("peter"),
        })
        .unwrap();
        assert_eq!(a.background(20), vec![], "accepted, and held");
        assert_eq!(a.background(21), vec![]);
        assert_eq!(a.waiting(), vec![1, 2]);
        assert_eq!(a.agent.queue_revision(), 3, "held, and two joined");
        assert_eq!(
            a.send(Command::Resume {
                by: "peter".to_string()
            })
            .unwrap(),
            vec![Effect::StartTurn {
                turn: TurnId(1),
                message_seq: 20
            }]
        );
        assert_eq!(
            a.send(Command::Resume {
                by: "peter".to_string()
            }),
            Err(Refusal::NotPaused)
        );
    }

    #[test]
    fn an_agent_says_what_it_is_busy_with_beyond_its_turns() {
        let mut a = Driven::new();
        a.user(10);
        // In its turn it says that it left something running.
        a.send(Command::Label {
            text: "Building".to_string(),
        })
        .unwrap();
        assert_eq!(
            (a.agent.label(), a.agent.resting()),
            ("Building", Some(Rest::Background))
        );
        a.finish(1);
        assert_eq!(
            a.agent.resting(),
            Some(Rest::Background),
            "the turn is over and the work goes on"
        );
        // Its next turn is a turn like any other; the label stays until it is taken back.
        a.user(11);
        assert_eq!((a.agent.label(), a.agent.resting()), ("Building", None));
        a.send(Command::Label {
            text: String::new(),
        })
        .unwrap();
        a.finish(2);
        assert_eq!((a.agent.label(), a.agent.resting()), ("", Some(Rest::Idle)));
    }

    #[test]
    fn a_label_is_taken_back_only_if_it_is_the_one_set_then() {
        let mut a = Driven::new();
        a.send(Command::Label {
            text: "Building".to_string(),
        })
        .unwrap();
        let first = a.agent.label_at().unwrap();
        // It says something new before the old label is looked at: the new one stands.
        a.send(Command::Label {
            text: "Testing".to_string(),
        })
        .unwrap();
        let second = a.agent.label_at().unwrap();
        assert!(second > first);
        a.send(Command::RetireLabel { set_at_ms: first }).unwrap();
        assert_eq!(
            (a.agent.label(), a.agent.label_at()),
            ("Testing", Some(second))
        );
        // The one that is there, taken back by its own time: the agent rests idle, and a
        // label that is gone has no time.
        a.send(Command::RetireLabel { set_at_ms: second }).unwrap();
        assert_eq!(
            (a.agent.label(), a.agent.label_at(), a.agent.resting()),
            ("", None, Some(Rest::Idle))
        );
        // Nothing to take back: nothing happens.
        a.send(Command::RetireLabel { set_at_ms: second }).unwrap();
        assert_eq!((a.agent.label(), a.agent.resting()), ("", Some(Rest::Idle)));
    }

    #[test]
    fn a_waiting_message_can_be_taken_back_and_a_live_one_cannot() {
        let mut a = Driven::new();
        a.user(10);
        a.user(11);
        a.user(12);
        assert_eq!(
            a.send(Command::Withdraw {
                turn: TurnId(2),
                by: "peter".to_string()
            })
            .unwrap(),
            vec![]
        );
        assert_eq!(a.waiting(), vec![3]);
        assert_eq!(
            a.send(Command::Withdraw {
                turn: TurnId(1),
                by: "peter".to_string()
            }),
            Err(Refusal::NotWaiting(TurnId(1)))
        );
        assert_eq!(
            a.send(Command::Withdraw {
                turn: TurnId(9),
                by: "peter".to_string()
            }),
            Err(Refusal::NotWaiting(TurnId(9)))
        );
        assert_eq!(
            a.finish(1),
            vec![Effect::StartTurn {
                turn: TurnId(3),
                message_seq: 12
            }],
            "the withdrawn turn never starts"
        );
    }

    #[test]
    fn a_signal_about_a_turn_that_is_not_live_changes_nothing() {
        let mut a = Driven::new();
        a.user(10);
        a.finish(1);
        let before = a.agent.clone();
        assert_eq!(
            a.send(Command::TurnEnded {
                turn: TurnId(1),
                outcome: RunOutcome::Completed
            }),
            Err(Refusal::NotLive(TurnId(1)))
        );
        assert_eq!(
            a.send(Command::TurnMoved {
                turn: TurnId(5),
                activity: Activity::Waiting
            }),
            Err(Refusal::NotLive(TurnId(5)))
        );
        assert_eq!(a.agent, before);
    }

    #[test]
    fn a_start_that_fails_frees_the_agent_for_the_next_message() {
        let mut a = Driven::new();
        a.user(10);
        a.user(11);
        let effects = a
            .send(Command::StartFailed {
                turn: TurnId(1),
                reason: "the tool is not installed".to_string(),
            })
            .unwrap();
        assert_eq!(
            effects,
            vec![Effect::StartTurn {
                turn: TurnId(2),
                message_seq: 11
            }]
        );
        assert_eq!(a.agent.settled(), 1);
    }

    /// A small deterministic storm of commands. Whatever the order, no accepted message is lost
    /// and none starts twice, and the history always rebuilds the state (checked in `send`).
    #[test]
    fn under_any_order_of_commands_nothing_is_lost_and_nothing_runs_twice() {
        for seed in 1..=200u64 {
            let mut state = seed.wrapping_mul(0x9E37_79B9_7F4A_7C15) | 1;
            let mut next = move |modulo: u64| {
                state ^= state << 13;
                state ^= state >> 7;
                state ^= state << 17;
                state % modulo
            };
            let mut a = Driven::new();
            let mut accepted = 0u64;
            for step in 0..120u64 {
                let live = a.live().map(TurnId);
                let command = match next(12) {
                    0..=2 => {
                        accepted += 1;
                        let origin = if next(3) == 0 {
                            Origin::Agent {
                                agent_id: "x".to_string(),
                            }
                        } else {
                            Origin::User
                        };
                        let admission = match next(6) {
                            0 => Admission::Direct,
                            1 => Admission::Background,
                            2 => Admission::Later,
                            _ => Admission::Queue,
                        };
                        Command::Accept {
                            message_seq: 100 + step,
                            origin,
                            admission,
                            coordination: None,
                            user_word: false,
                        }
                    }
                    3 => Command::Stop {
                        by: Control::by("peter"),
                    },
                    4 => Command::Resume {
                        by: "peter".to_string(),
                    },
                    5 => Command::Withdraw {
                        turn: TurnId(next(accepted + 2)),
                        by: "peter".to_string(),
                    },
                    6 => Command::TurnStarted {
                        turn: live.unwrap_or(TurnId(999)),
                    },
                    7 => Command::TurnMoved {
                        turn: live.unwrap_or(TurnId(999)),
                        activity: Activity::Tool {
                            name: "Bash".to_string(),
                        },
                    },
                    8 => Command::StartFailed {
                        turn: TurnId(next(accepted + 2)),
                        reason: "no".to_string(),
                    },
                    9 => Command::SendNow {
                        turn: TurnId(next(accepted + 2)),
                        by: "peter".to_string(),
                    },
                    10 => Command::Reword {
                        turn: TurnId(next(accepted + 2)),
                        message_seq: 1_000 + step,
                    },
                    _ => Command::TurnEnded {
                        turn: live.unwrap_or(TurnId(999)),
                        outcome: RunOutcome::Completed,
                    },
                };
                let _ = a.send(command);
            }
            // Let everything that is still waiting run to its end.
            if a.agent.is_paused() {
                a.send(Command::Resume {
                    by: "the test".to_string(),
                })
                .unwrap();
            }
            while let Some(turn) = a.live() {
                a.finish(turn);
            }
            assert_eq!(
                a.waiting(),
                Vec::<u64>::new(),
                "seed {seed}: nothing is left waiting"
            );
            assert_eq!(
                a.agent.settled(),
                accepted,
                "seed {seed}: every accepted message reached an end"
            );
            let mut started = a.started.clone();
            started.sort();
            started.dedup();
            assert_eq!(
                started.len(),
                a.started.len(),
                "seed {seed}: no turn started twice"
            );
        }
    }

    /// The turn that is being started takes the user's messages that wait directly behind it,
    /// as the actor does: each reserved and read at once.
    fn take_together(a: &mut Driven, into: u64) -> Vec<u64> {
        let mut taken = Vec::new();
        for turn in a.agent.waiting_together() {
            a.send(Command::Join {
                turn,
                join: together_join(into, turn.0),
            })
            .unwrap();
            a.send(Command::JoinRead { turn }).unwrap();
            taken.push(turn.0);
        }
        taken
    }

    fn together_join(into: u64, turn: u64) -> Join {
        Join {
            into: TurnId(into),
            uuid: format!("together-{turn}"),
            recovery: None,
            together: true,
        }
    }

    fn gathered(a: &Driven, turn: u64) -> Vec<(u64, u64)> {
        a.agent
            .gathered(TurnId(turn))
            .iter()
            .map(|gathered| (gathered.turn.0, gathered.message_seq))
            .collect()
    }

    #[test]
    fn three_waiting_user_messages_are_one_turn_and_every_attempt_is_given_all_of_them() {
        let mut a = Driven::new();
        a.user(10);
        a.send(Command::TurnStarted { turn: TurnId(1) }).unwrap();
        for seq in [11, 12, 13] {
            a.user(seq);
        }
        assert_eq!(a.waiting(), vec![2, 3, 4]);
        // A turn that runs takes nothing into its prompt: that is steering's.
        assert!(a.agent.waiting_together().is_empty());
        assert_eq!(
            a.send(Command::Join {
                turn: TurnId(2),
                join: together_join(1, 2),
            }),
            Err(Refusal::NotWaiting(TurnId(2)))
        );
        a.finish(1);
        assert_eq!(a.live(), Some(2));
        assert_eq!(a.agent.waiting_together(), [3, 4].map(TurnId));
        // Only the next of them, and only once the one before it was read.
        assert_eq!(
            a.send(Command::Join {
                turn: TurnId(4),
                join: together_join(2, 4),
            }),
            Err(Refusal::NotWaiting(TurnId(4)))
        );
        a.send(Command::Join {
            turn: TurnId(3),
            join: together_join(2, 3),
        })
        .unwrap();
        assert_eq!(
            a.send(Command::Join {
                turn: TurnId(4),
                join: together_join(2, 4),
            }),
            Err(Refusal::NotWaiting(TurnId(4)))
        );
        a.send(Command::JoinRead { turn: TurnId(3) }).unwrap();
        assert_eq!(take_together(&mut a, 2), vec![4]);
        assert!(a.waiting().is_empty());
        assert_eq!(gathered(&a, 2), vec![(3, 12), (4, 13)]);

        // The attempt fails on something that passes: its process ended with them read, and
        // the next attempt is given them again, also after a restart.
        a.send(Command::TurnStarted { turn: TurnId(2) }).unwrap();
        for turn in [3, 4] {
            a.send(Command::JoinSettled { turn: TurnId(turn) }).unwrap();
        }
        a.send(dropped(2)).unwrap();
        assert!(a.agent.joins().is_empty());
        assert_eq!(gathered(&a, 2), vec![(3, 12), (4, 13)]);
        let effects = a.send(Command::TurnRetried { turn: TurnId(2) }).unwrap();
        assert!(matches!(
            effects.as_slice(),
            [Effect::RetryTurn {
                turn: TurnId(2),
                attempt: 2,
                ..
            }]
        ));
        let restarted = Agent::replay(&a.history).unwrap();
        assert_eq!(restarted.gathered(TurnId(2)), a.agent.gathered(TurnId(2)));
        // A later attempt takes nothing more: what it is given is what the first took.
        a.user(14);
        assert!(a.agent.waiting_together().is_empty());

        a.finish(2);
        assert!(gathered(&a, 2).is_empty());
        assert_eq!(
            a.live(),
            Some(5),
            "the message sent after it has a turn of its own"
        );
        assert_eq!(a.started, [1, 2, 5].map(TurnId));
        // Turns 1 and 2, and the two read into turn 2.
        assert_eq!(a.agent.settled(), 4);
    }

    #[test]
    fn the_last_turn_keeps_whether_it_carried_the_users_word() {
        let mut a = Driven::new();
        let relay = |a: &mut Driven, message_seq, user_word| {
            a.send(Command::Accept {
                message_seq,
                origin: Origin::Agent {
                    agent_id: "desk".into(),
                },
                admission: Admission::Later,
                coordination: Some("desk".into()),
                user_word,
            })
            .unwrap();
        };
        assert!(!a.agent.last_carried_user_word(), "nothing ended yet");
        relay(&mut a, 10, true);
        a.finish(1);
        assert_eq!(a.agent.last().unwrap().id, TurnId(1));
        assert!(
            a.agent.last_carried_user_word(),
            "a front desk's relay of the user's word is the user's"
        );
        relay(&mut a, 11, false);
        a.finish(2);
        assert_eq!(a.agent.last().unwrap().id, TurnId(2));
        assert!(!a.agent.last_carried_user_word(), "an agent's own message");
    }

    #[test]
    fn a_relay_put_back_unsent_still_carries_the_users_word_when_it_ends() {
        let desk = || Origin::Agent {
            agent_id: "desk".into(),
        };
        let from_desk = |a: &mut Driven, message_seq, user_word| {
            a.send(Command::Accept {
                message_seq,
                origin: desk(),
                admission: Admission::Later,
                coordination: Some("desk".into()),
                user_word,
            })
            .unwrap();
        };
        // The desk's relay of the user's word (turn 1, at once), the user (2), the desk's own (3).
        let accepted = || {
            let mut a = Driven::new();
            from_desk(&mut a, 10, true);
            a.user(11);
            from_desk(&mut a, 12, false);
            assert_eq!(a.live(), Some(1));
            a
        };
        // Turn 1, live and never sent to the tool, goes back to the queue: the backend switch
        // puts back an account wait, the Host move a launch it refused.
        let put_back = |a: &mut Driven, transit: bool| {
            if transit {
                a.send(Command::TurnParked {
                    turn: TurnId(1),
                    wait: Wait::account(90_000),
                    kind: crate::failures::NOT_STARTED.to_string(),
                    reason: "waiting for an account".to_string(),
                })
                .unwrap();
                let hold = Command::HoldUnsentForTransit { turn: TurnId(1) };
                assert!(a.send_with_transit_hold(hold, true).unwrap().is_empty());
            } else {
                let to = crate::agent_moves::Target {
                    host: "other".into(),
                    id: "agent".into(),
                };
                a.send(Command::SetMoved { to: Some(to) }).unwrap();
                a.send(Command::HoldUnsentForMove { turn: TurnId(1) })
                    .unwrap();
            }
            assert_eq!(a.live(), None);
            assert_eq!(a.waiting(), vec![1, 2, 3], "it goes back to the front");
            let back = a.agent.waiting().next().unwrap();
            assert!(
                !back.user_word,
                "the flag that orders the queue is as before"
            );
        };
        let run_again = |a: &mut Driven, transit: bool| {
            if transit {
                a.send(Command::RunWaiting).unwrap();
            } else {
                a.send(Command::SetMoved { to: None }).unwrap();
            }
            assert_eq!(a.live(), Some(1), "the relay runs again first");
        };
        for transit in [true, false] {
            let mut a = accepted();
            put_back(&mut a, transit);
            run_again(&mut a, transit);
            put_back(&mut a, transit);
            run_again(&mut a, transit);
            a.send(Command::TurnStarted { turn: TurnId(1) }).unwrap();
            assert!(
                a.agent.owner_goes_first(&Origin::User),
                "what may cut the resumed relay is as before"
            );
            assert_eq!(
                a.finish(1),
                vec![Effect::StartTurn {
                    turn: TurnId(2),
                    message_seq: 11
                }],
                "the queue runs in the same order"
            );
            assert_eq!(a.agent.last().unwrap().id, TurnId(1));
            assert!(
                a.agent.last_carried_user_word(),
                "the relay of the user's word ended without words is still the user's"
            );
            let replayed = Agent::replay(&a.history).unwrap();
            assert!(
                replayed.last_carried_user_word(),
                "and a restart says the same"
            );

            a.finish(2);
            a.send(Command::TurnStarted { turn: TurnId(3) }).unwrap();
            a.finish(3);
            assert_eq!(a.agent.last().unwrap().id, TurnId(3));
            assert!(!a.agent.last_carried_user_word(), "an agent's own message");
            assert!(a.agent.held_user_word.is_empty());

            // Put back and taken back, it can no longer end: nothing is kept for it.
            let mut a = accepted();
            put_back(&mut a, transit);
            run_again(&mut a, transit);
            put_back(&mut a, transit);
            assert!(a.agent.held_user_word.contains(&TurnId(1)));
            a.send(Command::Withdraw {
                turn: TurnId(1),
                by: "user".into(),
            })
            .unwrap();
            assert!(a.agent.held_user_word.is_empty());
        }
    }

    #[test]
    fn a_relay_of_the_users_word_between_two_user_messages_keeps_its_own_turn() {
        let mut a = Driven::new();
        a.user(10);
        a.user(11);
        // The front desk relays a word of the user's: it waits behind the user's own.
        a.send(Command::Accept {
            message_seq: 12,
            origin: Origin::Agent {
                agent_id: "desk".into(),
            },
            admission: Admission::Later,
            coordination: Some("desk".into()),
            user_word: true,
        })
        .unwrap();
        a.user(13);
        assert_eq!(a.waiting(), vec![2, 3, 4]);
        a.finish(1);
        assert_eq!(a.live(), Some(2));
        assert!(
            take_together(&mut a, 2).is_empty(),
            "the relay is not the user's own message, and nothing past it goes either"
        );
        a.finish(2);
        assert_eq!(a.live(), Some(3));
        assert!(take_together(&mut a, 3).is_empty(), "the relay's own turn");
        a.finish(3);
        assert_eq!(a.live(), Some(4));
        assert_eq!(a.started, [1, 2, 3, 4].map(TurnId));
    }

    #[test]
    fn a_queue_a_stop_holds_is_not_taken_together() {
        let mut a = Driven::new();
        a.user(10);
        a.user(11);
        a.user(12);
        a.send(Command::Stop {
            by: Control::by("user"),
        })
        .unwrap();
        a.finish(1);
        assert_eq!(a.live(), None);
        a.send(Command::SendNow {
            turn: TurnId(2),
            by: "user".into(),
        })
        .unwrap();
        assert_eq!(a.live(), Some(2));
        assert!(a.agent.is_paused());
        assert!(a.agent.waiting_together().is_empty());
        assert_eq!(
            a.send(Command::Join {
                turn: TurnId(3),
                join: together_join(2, 3),
            }),
            Err(Refusal::NotWaiting(TurnId(3)))
        );
        a.finish(2);
        assert_eq!(a.waiting(), vec![3], "the held message still waits");

        // A fresh message of the user's lets the hold go, but the older held message is not
        // put behind it in its prompt: the order would be wrong.
        a.user(13);
        assert_eq!(a.live(), Some(4));
        assert!(a.agent.waiting_together().is_empty());
    }

    /// Turn 1 runs; three of the user's messages wait behind it; turn 2 starts and takes the
    /// two behind its own.
    fn two_gathered() -> Driven {
        let mut a = Driven::new();
        a.user(10);
        a.send(Command::TurnStarted { turn: TurnId(1) }).unwrap();
        for seq in [11, 12, 13] {
            a.user(seq);
        }
        a.finish(1);
        assert_eq!(take_together(&mut a, 2), vec![3, 4]);
        a.send(Command::TurnStarted { turn: TurnId(2) }).unwrap();
        a
    }

    /// The process of turn 2 ends after a Stop, its joins settled as the actor settles them.
    fn ended_after_a_stop(a: &mut Driven, by: Option<Control>) {
        for turn in [3, 4] {
            a.send(Command::JoinSettled { turn: TurnId(turn) }).unwrap();
        }
        if let Some(by) = by {
            a.send(Command::Stop { by }).unwrap();
        }
        a.send(Command::TurnEnded {
            turn: TurnId(2),
            outcome: RunOutcome::Interrupted {
                reason: "the tool was ended by signal 15".into(),
            },
        })
        .unwrap();
    }

    fn an_agent() -> Control {
        Control {
            actor: "agent:hazel".into(),
            verified: true,
            reason: "wrong direction".into(),
        }
    }

    #[test]
    fn a_turn_that_took_the_users_messages_is_not_taken_over_on_a_later_attempt() {
        let mut a = two_gathered();
        a.send(dropped(2)).unwrap();
        a.send(Command::TurnRetried { turn: TurnId(2) }).unwrap();
        assert!(a.agent.joins().is_empty(), "the park let go of the joins");
        a.user(14);
        assert_eq!(a.waiting(), vec![5]);
        assert!(
            !a.agent.can_take_over(TurnId(2), TurnId(5), 2),
            "a takeover would give the tool turn 2's own words and the new ones, not 3 and 4"
        );
        assert_eq!(
            a.send(Command::SelectTakeover {
                selected: crate::takeover::Selection {
                    into: TurnId(5),
                    attempt: 2,
                    fence: 0,
                    trace: None,
                    start: crate::takeover::Start {
                        from: TurnId(2),
                        session: "s".into(),
                        input: crate::takeover::Input::Agy {
                            first: "words".into(),
                        },
                    },
                },
            })
            .map(|_| ()),
            Err(Refusal::TakeoverStale)
        );
        assert_eq!(
            gathered(&a, 2),
            vec![(3, 12), (4, 13)],
            "all kept for attempt 2"
        );
        a.finish(2);
        assert_eq!(a.live(), Some(5), "the new message waited for the turn");
    }

    #[test]
    fn an_agents_stop_gives_the_users_unanswered_messages_back_to_the_queue_in_order() {
        let mut a = two_gathered();
        ended_after_a_stop(&mut a, Some(an_agent()));
        assert_eq!(
            a.waiting(),
            vec![3, 4],
            "back in the queue, in order, held by the Stop as all that waits"
        );
        assert!(a.agent.is_paused());
        assert!(a.agent.waiting().all(|queued| queued.attempt == 1
            && queued.admission == Admission::Queue
            && queued.user_word));
        assert_eq!(Agent::replay(&a.history).unwrap(), a.agent);
        a.send(Command::Resume { by: "user".into() }).unwrap();
        assert_eq!(a.live(), Some(3));
        assert_eq!(
            take_together(&mut a, 3),
            vec![4],
            "they go to the tool together again"
        );
    }

    #[test]
    fn a_lost_process_or_a_newer_message_gives_them_back_and_the_users_stop_does_not() {
        // The process went away under the turn: they run again at once.
        let mut lost = two_gathered();
        ended_after_a_stop(&mut lost, None);
        assert_eq!(lost.live(), Some(3));
        assert_eq!(lost.waiting(), vec![4]);

        // A direct message took the turn's place: it runs first, they wait behind it.
        let mut newer = two_gathered();
        newer.user(14);
        newer.direct(15);
        newer
            .send(Command::TurnEnded {
                turn: TurnId(2),
                outcome: RunOutcome::Interrupted {
                    reason: "stopped".into(),
                },
            })
            .unwrap();
        assert_eq!(newer.live(), Some(6));
        assert_eq!(newer.waiting(), vec![3, 4, 5]);

        // The user's own Stop ends them with the turn.
        let mut theirs = two_gathered();
        ended_after_a_stop(&mut theirs, Some(Control::by("user")));
        assert!(theirs.waiting().is_empty());
        assert!(gathered(&theirs, 2).is_empty());

        // A turn that had begun to answer them ends them with it, whoever stopped it.
        let mut answered = two_gathered();
        answered
            .send(Command::TurnMoved {
                turn: TurnId(2),
                activity: Activity::Responding,
            })
            .unwrap();
        ended_after_a_stop(&mut answered, Some(an_agent()));
        assert!(answered.waiting().is_empty());

        // So does one that only used a tool for them, in an attempt before this one: what it
        // did (a send, a commit) is not done a second time from nothing.
        let mut worked = two_gathered();
        worked
            .send(Command::TurnMoved {
                turn: TurnId(2),
                activity: Activity::Tool {
                    name: "Bash".into(),
                },
            })
            .unwrap();
        worked.send(dropped(2)).unwrap();
        worked
            .send(Command::TurnRetried { turn: TurnId(2) })
            .unwrap();
        worked
            .send(Command::TurnStarted { turn: TurnId(2) })
            .unwrap();
        worked.send(Command::Stop { by: an_agent() }).unwrap();
        worked
            .send(Command::TurnEnded {
                turn: TurnId(2),
                outcome: RunOutcome::Interrupted {
                    reason: "the tool was ended by signal 15".into(),
                },
            })
            .unwrap();
        assert!(worked.waiting().is_empty(), "nothing given back");
        assert!(gathered(&worked, 2).is_empty());
    }

    /// How often each waiting message was given back, in queue order.
    fn given_back(a: &Driven) -> Vec<(u64, u32)> {
        a.agent
            .waiting()
            .map(|queued| (queued.id.0, queued.given_back))
            .collect()
    }

    /// The live turn takes what waits behind it, its process starts, and is gone before its
    /// tool did anything: its joins settled as the actor settles them.
    fn lost_with_nothing_done(a: &mut Driven, turn: u64) -> Vec<u64> {
        let taken = take_together(a, turn);
        a.send(Command::TurnStarted { turn: TurnId(turn) }).unwrap();
        for one in &taken {
            a.send(Command::JoinSettled { turn: TurnId(*one) }).unwrap();
        }
        a.send(Command::TurnEnded {
            turn: TurnId(turn),
            outcome: RunOutcome::Interrupted {
                reason: "the turn's process is gone and did not say how it went".into(),
            },
        })
        .unwrap();
        taken
    }

    /// Turn 1 runs and four of the user's messages (turns 2 to 5) wait behind it; it ends, and
    /// turn 2 is being started.
    fn four_waiting() -> Driven {
        let mut a = Driven::new();
        a.user(10);
        a.send(Command::TurnStarted { turn: TurnId(1) }).unwrap();
        for seq in [11, 12, 13, 14] {
            a.user(seq);
        }
        a.finish(1);
        assert_eq!(a.live(), Some(2));
        a
    }

    #[test]
    fn a_message_taken_into_three_turns_in_a_row_that_did_nothing_ends_with_the_third() {
        let mut a = four_waiting();
        assert_eq!(lost_with_nothing_done(&mut a, 2), vec![3, 4, 5]);
        // Given back fewer times than the limit: back in the queue, first, in order.
        assert_eq!(a.live(), Some(3));
        assert_eq!(given_back(&a), vec![(4, 1), (5, 1)]);
        assert_eq!(lost_with_nothing_done(&mut a, 3), vec![4, 5]);
        assert_eq!(a.live(), Some(4));
        assert_eq!(given_back(&a), vec![(5, 2)]);
        assert_eq!(take_together(&mut a, 4), vec![5]);
        assert_eq!(a.agent.gathered(TurnId(4))[0].given_back, 2);
        // The count is the log's: the state a restart rebuilds holds it.
        let restarted = Agent::replay(&a.history).unwrap();
        assert_eq!(restarted.gathered(TurnId(4)), a.agent.gathered(TurnId(4)));
        a.send(Command::TurnStarted { turn: TurnId(4) }).unwrap();
        a.send(Command::JoinSettled { turn: TurnId(5) }).unwrap();
        a.send(Command::TurnEnded {
            turn: TurnId(4),
            outcome: RunOutcome::Interrupted {
                reason: "the turn's process is gone and did not say how it went".into(),
            },
        })
        .unwrap();
        // The third turn that took it for nothing: it ends with that turn, not given back.
        assert!(a.waiting().is_empty(), "message 5 is over");
        assert_eq!(a.live(), None);
        assert!(gathered(&a, 4).is_empty());
        assert!(!a.agent.deliveries().holds(5));
        a.user(15);
        assert_eq!(a.live(), Some(6), "the agent takes new work as ever");
    }

    #[test]
    fn a_message_given_back_twice_ends_with_a_turn_that_worked_or_the_users_stop() {
        for how in ["worked", "stopped"] {
            let mut a = four_waiting();
            lost_with_nothing_done(&mut a, 2);
            lost_with_nothing_done(&mut a, 3);
            assert_eq!(given_back(&a), vec![(5, 2)]);
            assert_eq!(take_together(&mut a, 4), vec![5]);
            a.send(Command::TurnStarted { turn: TurnId(4) }).unwrap();
            if how == "worked" {
                a.send(Command::TurnMoved {
                    turn: TurnId(4),
                    activity: Activity::Responding,
                })
                .unwrap();
                a.send(Command::Stop { by: an_agent() }).unwrap();
            } else {
                a.send(Command::Stop {
                    by: Control::by("user"),
                })
                .unwrap();
            }
            a.send(Command::JoinSettled { turn: TurnId(5) }).unwrap();
            a.send(Command::TurnEnded {
                turn: TurnId(4),
                outcome: RunOutcome::Interrupted {
                    reason: "the tool was ended by signal 15".into(),
                },
            })
            .unwrap();
            assert!(a.waiting().is_empty(), "{how}: nothing given back");
        }
    }

    #[test]
    fn a_message_given_back_fewer_times_goes_back_again_ahead_of_newer_work() {
        let mut a = four_waiting();
        lost_with_nothing_done(&mut a, 2);
        a.user(15);
        assert_eq!(take_together(&mut a, 3), vec![4, 5, 6]);
        a.send(Command::TurnStarted { turn: TurnId(3) }).unwrap();
        a.user(16);
        for turn in [4, 5, 6] {
            a.send(Command::JoinSettled { turn: TurnId(turn) }).unwrap();
        }
        a.send(Command::TurnEnded {
            turn: TurnId(3),
            outcome: RunOutcome::Interrupted {
                reason: "the turn's process is gone and did not say how it went".into(),
            },
        })
        .unwrap();
        // Each counted on its own, and all of them ahead of the message sent after.
        assert_eq!(a.live(), Some(4));
        assert_eq!(given_back(&a), vec![(5, 2), (6, 1), (7, 0)]);
    }
}
