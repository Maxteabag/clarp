//! A turn: the work an agent does for one message, as a state machine.
//!
//! There are two ways to move a turn, and they cannot disagree because one is built on the other.
//!
//! - The actor *decides* with the typed transitions (`Queued::start`, `Running::complete`, ...).
//!   Each consumes the state it leaves, so a transition that makes no sense is not written wrongly,
//!   it does not compile: a settled turn has no `complete`, a queued turn has no `stop`.
//! - A restart *replays* events read from the log with [`Turn::apply`]. Events on disk are data,
//!   so there the check happens when it runs, and a sequence that makes no sense is a corrupt log.
//!
//! Every typed transition returns the event that records it; applying that event to the old state
//! gives the same new state. The tests hold the two to that.

use serde::{Deserialize, Serialize};

/// A turn's number within its agent. The first turn is 1.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
pub struct TurnId(pub u64);

/// Who asked for the turn. Admission decides differently for each.
#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(tag = "from", rename_all = "kebab-case")]
pub enum Origin {
    #[default]
    User,
    Agent {
        agent_id: String,
    },
    /// Something the Host does by itself, by the name the apps file it under: `automation`
    /// for a goal that is taken up again, a schedule, an answer that is delivered.
    Trigger {
        name: String,
    },
}

impl Origin {
    pub fn is_user(&self) -> bool {
        *self == Self::User
    }

    /// The word the contract has for it.
    pub fn name(&self) -> &str {
        match self {
            Self::User => "user",
            Self::Agent { .. } => "agent",
            Self::Trigger { name } => name,
        }
    }

    /// What kind of automated activity a turn is that began this way: an event of a watcher
    /// outside, or the Host's own routine. The apps fold such turns away. Empty: none.
    pub fn automation_kind(&self) -> &str {
        automation_kind(self.name())
    }
}

/// What a turn is filed under in which a tool makes its own conversation shorter, asked for
/// from an app: nobody's message, and nothing is said in it. The Python Host has no such
/// turn (it types into the tool's own screen), so the word is this Host's.
pub const COMPACTION: &str = "compaction";

/// [`Origin::automation_kind`] for an origin by its name.
pub fn automation_kind(origin: &str) -> &str {
    match origin {
        "watcher" | "heartbeat" | "leader_tick" | "dreaming" | "janitor" | COMPACTION => origin,
        _ => "",
    }
}

/// Whether the turns of an origin are left out of a chat that is read without automation.
/// A janitor's are automated and stay: it works in a chat of its own.
pub fn left_out_of_chats(origin: &str) -> bool {
    matches!(
        origin,
        "watcher" | "heartbeat" | "leader_tick" | "dreaming" | COMPACTION
    )
}

/// Whether a turn of this origin that fails is told in the agent's state alone, with no row in
/// the conversation: nobody was waiting for it (the Python Host's `SILENT_ORIGINS`).
pub fn silent_when_failed(origin: &str) -> bool {
    matches!(
        origin,
        "heartbeat" | "leader_tick" | "dreaming" | "janitor" | "watcher"
    )
}

/// How a message enters the mailbox.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum Admission {
    /// Said to the agent: the agent comes to it when it is free. A hold that was placed before
    /// it came does not hold it: someone said this after the stop, and means it. Like every
    /// message, it enters the conversation when its turn starts; while it waits it keeps its
    /// delivery receipt, and it can be reworded or taken back in the queue.
    #[default]
    Queue,
    /// The user wants it now: it ends the live turn, goes ahead of what waits its turn, and runs
    /// even while the agent is paused, without lifting the pause.
    Direct,
    /// Left for later by someone: it waits its turn like a message that was said, in the queue,
    /// where it can be taken back, and goes through the queue even when nothing is in its way.
    Later,
    /// Work nobody sits waiting for: it waits in the queue like `Later`, and a hold holds it
    /// like everything that waited when the hold was placed.
    Background,
}

impl Admission {
    fn is_queue(&self) -> bool {
        *self == Self::Queue
    }

    /// Whether a message admitted this way was left for later: it goes through the queue even
    /// when nothing is in its way.
    pub fn left_for_later(self) -> bool {
        matches!(self, Self::Later | Self::Background)
    }
}

/// What a running turn is doing right now.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "activity", rename_all = "kebab-case")]
pub enum Activity {
    Thinking,
    /// Writing its answer.
    Responding,
    Tool {
        name: String,
    },
    /// Waiting for something outside the turn: an approval, a background task.
    Waiting,
    Compacting,
}

/// How a turn ended.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "outcome", rename_all = "kebab-case")]
pub enum Outcome {
    Completed,
    Failed {
        reason: String,
        /// What kind of failure it was (`failures`), as it was read when it happened: what
        /// the user is told follows from it, and the wordings it is read from change. Empty
        /// in a log that was written before the kind was kept.
        #[serde(default, skip_serializing_if = "String::is_empty")]
        kind: String,
    },
    /// Someone stopped it: who, whether the Host knew it was they, and why. The last two are
    /// absent from a log written before they were kept, and from a Stop that is the user's own
    /// claim and gave no reason: such a line is written as it was before.
    Stopped {
        by: String,
        #[serde(default, skip_serializing_if = "is_false")]
        verified: bool,
        #[serde(default, skip_serializing_if = "String::is_empty")]
        reason: String,
    },
    /// The tool process went away under the turn: a crash, a killed shim, a lost machine.
    Interrupted {
        reason: String,
    },
    /// A quiet turn superseded by the next user's message. Its receipt is still handed over.
    Replaced {
        by: TurnId,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        trace: Option<String>,
    },
    /// Taken back before it started.
    Withdrawn {
        by: String,
    },
}

#[allow(clippy::trivially_copy_pass_by_ref)]
fn is_false(value: &bool) -> bool {
    !*value
}

impl Outcome {
    /// A turn someone stopped: who, and why.
    pub fn stopped(by: crate::control::Control) -> Self {
        Self::Stopped {
            by: by.actor,
            verified: by.verified,
            reason: by.reason,
        }
    }

    /// Who stopped the turn and why, where someone did.
    pub fn stopped_by(&self) -> Option<crate::control::Control> {
        match self {
            Self::Stopped {
                by,
                verified,
                reason,
            } => Some(crate::control::Control {
                actor: by.clone(),
                verified: *verified,
                reason: reason.clone(),
            }),
            _ => None,
        }
    }

    /// A turn whose tool failed, with the tool's words for it.
    pub fn failed(reason: String) -> Self {
        Self::Failed {
            kind: crate::failures::classify_error(&reason).to_string(),
            reason,
        }
    }

    /// A turn whose tool could not be started.
    pub fn not_started(reason: String) -> Self {
        Self::Failed {
            kind: crate::failures::NOT_STARTED.to_string(),
            reason,
        }
    }
}

/// Everything that can happen to a turn. These are what the agent's log holds.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "event", rename_all = "kebab-case")]
pub enum TurnEvent {
    /// The message was accepted. From here on it is never lost and never runs twice.
    Accepted {
        turn: TurnId,
        message_seq: u64,
        origin: Origin,
        #[serde(default, skip_serializing_if = "Admission::is_queue")]
        admission: Admission,
        at_ms: i64,
        /// The message is another agent addressing this one (coordination between agents): the
        /// sender's agent id, empty for an agent of a peer Host. Absent: the user's, as every
        /// acceptance written before the mark was kept reads. Decided once, at the send, and
        /// written in the same item as the acceptance, which is then found by its key
        /// (`c-<turn>`): no restart falls between the two, and nothing loosens it.
        #[serde(default, skip_serializing_if = "Option::is_none")]
        coordination: Option<String>,
    },
    Started {
        at_ms: i64,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        takeover: Option<crate::takeover::Start>,
    },
    /// The tool told us which of its own sessions carries this turn.
    Bound {
        backend_session: String,
    },
    Moved {
        activity: Activity,
        at_ms: i64,
    },
    /// An attempt ended on a failure that does not end the turn: the turn waits for the next
    /// attempt. `kind` and `reason` are that failure, as it was read then and in the tool's words.
    Parked {
        wait: Wait,
        kind: String,
        reason: String,
        at_ms: i64,
    },
    /// The wait is over: the tool process of the next attempt is being started.
    Retried {
        at_ms: i64,
    },
    Settled {
        outcome: Outcome,
        at_ms: i64,
    },
}

/// What a turn waits for between two attempts.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "for", rename_all = "kebab-case")]
pub enum Wait {
    /// A moment, and then the same again: the attempt failed in a way that passes (a
    /// connection that dropped, a tool that stalled). The next begins at `until_ms`.
    Retry { until_ms: i64 },
    /// An account that serves the turn: the attempt failed on its account (a limit that was
    /// reached, a sign-in that ran out). The turn is tried when the tool's login has changed,
    /// and at `until_ms` at the latest, on whatever account is there then.
    Account { until_ms: i64 },
}

impl Wait {
    /// When the wait is over at the latest.
    pub fn until_ms(&self) -> i64 {
        match self {
            Self::Retry { until_ms } | Self::Account { until_ms } => *until_ms,
        }
    }

    pub fn is_for_an_account(&self) -> bool {
        matches!(self, Self::Account { .. })
    }
}

/// What a turn's waits for an account have been so far.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct AccountWaits {
    /// How many of the turn's waits were for an account.
    pub count: u32,
    /// Since when the turn has waited for one, when it has: the time that bounds them all.
    pub since_ms: Option<i64>,
}

/// A turn that was accepted and waits in the agent's mailbox.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Queued {
    pub id: TurnId,
    /// The position in the agent's log of the message this turn answers.
    pub message_seq: u64,
    pub origin: Origin,
    pub admission: Admission,
    pub accepted_at_ms: i64,
}

/// A turn whose tool process is being started: for the first time, or again.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Starting {
    pub id: TurnId,
    pub message_seq: u64,
    pub origin: Origin,
    pub accepted_at_ms: i64,
    /// When the turn began: its first attempt. A turn is counted from there however often
    /// it is tried.
    pub started_at_ms: i64,
    /// Which attempt this is, from 1.
    pub attempt: u32,
    /// What the attempts before left: the tool's session, and how many tools ran.
    pub backend_session: Option<String>,
    pub tool_count: u32,
    pub account: AccountWaits,
    /// The turn waited for an account before this attempt: the tool is told that it is
    /// taken up again, not given the message as if for the first time.
    pub recovering: bool,
}

/// A turn whose tool process is working.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Running {
    pub id: TurnId,
    pub message_seq: u64,
    pub origin: Origin,
    pub accepted_at_ms: i64,
    pub started_at_ms: i64,
    pub attempt: u32,
    pub backend_session: Option<String>,
    pub activity: Activity,
    pub tool_count: u32,
    pub account: AccountWaits,
}

/// A turn between two attempts. It holds the agent like a turn that runs, and has no tool
/// process: the last attempt's is gone, the next one's is not started.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Parked {
    pub id: TurnId,
    pub message_seq: u64,
    pub origin: Origin,
    pub accepted_at_ms: i64,
    pub started_at_ms: i64,
    /// How many attempts the turn has had.
    pub attempts: u32,
    pub backend_session: Option<String>,
    pub tool_count: u32,
    pub wait: Wait,
    /// The failure the last attempt ended on: what the turn ends on too, when the wait
    /// does not end well.
    pub kind: String,
    pub reason: String,
    pub since_ms: i64,
    /// The waits for an account, this one included when it is one.
    pub account: AccountWaits,
}

/// A turn that is over. Nothing moves it again.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Settled {
    pub id: TurnId,
    pub message_seq: u64,
    pub origin: Origin,
    pub outcome: Outcome,
    pub accepted_at_ms: i64,
    pub started_at_ms: Option<i64>,
    pub ended_at_ms: i64,
    pub tool_count: u32,
    /// How many attempts the turn had. None for a turn that was taken back before it started.
    pub attempts: u32,
    /// How many of them followed a wait for an account.
    pub account_waits: u32,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Turn {
    Queued(Queued),
    Starting(Starting),
    Running(Running),
    Parked(Parked),
    Settled(Settled),
}

/// An event that cannot follow the state it was applied to: the log is corrupt, or the code that
/// wrote it was wrong.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct IllegalTransition {
    pub turn: TurnId,
    pub state: &'static str,
    pub event: &'static str,
}

impl std::fmt::Display for IllegalTransition {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "turn {}: `{}` cannot follow the state `{}`",
            self.turn.0, self.event, self.state
        )
    }
}

impl std::error::Error for IllegalTransition {}

impl Queued {
    /// Accepts a message as a turn. This is where a turn begins.
    pub fn accept(
        turn: TurnId,
        message_seq: u64,
        origin: Origin,
        admission: Admission,
        at_ms: i64,
    ) -> (Self, TurnEvent) {
        let event = TurnEvent::Accepted {
            turn,
            message_seq,
            origin: origin.clone(),
            admission,
            at_ms,
            coordination: None,
        };
        (
            Self {
                id: turn,
                message_seq,
                origin,
                admission,
                accepted_at_ms: at_ms,
            },
            event,
        )
    }

    pub fn start(self, at_ms: i64) -> (Starting, TurnEvent) {
        let next = Starting {
            id: self.id,
            message_seq: self.message_seq,
            origin: self.origin,
            accepted_at_ms: self.accepted_at_ms,
            started_at_ms: at_ms,
            attempt: 1,
            backend_session: None,
            tool_count: 0,
            account: AccountWaits::default(),
            recovering: false,
        };
        (
            next,
            TurnEvent::Started {
                at_ms,
                takeover: None,
            },
        )
    }

    pub fn withdraw(self, by: String, at_ms: i64) -> (Settled, TurnEvent) {
        let outcome = Outcome::Withdrawn { by };
        let next = Settled {
            id: self.id,
            message_seq: self.message_seq,
            origin: self.origin,
            outcome: outcome.clone(),
            accepted_at_ms: self.accepted_at_ms,
            started_at_ms: None,
            ended_at_ms: at_ms,
            tool_count: 0,
            attempts: 0,
            account_waits: 0,
        };
        (next, TurnEvent::Settled { outcome, at_ms })
    }
}

impl Starting {
    /// The tool process is up and working.
    pub fn running(self, at_ms: i64) -> (Running, TurnEvent) {
        let next = Running {
            id: self.id,
            message_seq: self.message_seq,
            origin: self.origin,
            accepted_at_ms: self.accepted_at_ms,
            started_at_ms: self.started_at_ms,
            attempt: self.attempt,
            backend_session: self.backend_session,
            activity: Activity::Thinking,
            tool_count: self.tool_count,
            account: self.account,
        };
        (
            next,
            TurnEvent::Moved {
                activity: Activity::Thinking,
                at_ms,
            },
        )
    }

    /// The tool process could not be started, or the start was stopped.
    pub fn settle(self, outcome: StartOutcome, at_ms: i64) -> (Settled, TurnEvent) {
        let outcome = outcome.into_outcome();
        (
            self.ended(outcome.clone(), at_ms),
            TurnEvent::Settled { outcome, at_ms },
        )
    }

    /// The turn as it is once it has ended this way.
    fn ended(self, outcome: Outcome, at_ms: i64) -> Settled {
        Settled {
            id: self.id,
            message_seq: self.message_seq,
            origin: self.origin,
            outcome,
            accepted_at_ms: self.accepted_at_ms,
            started_at_ms: Some(self.started_at_ms),
            ended_at_ms: at_ms,
            tool_count: self.tool_count,
            attempts: self.attempt,
            account_waits: self.account.count,
        }
    }
}

/// The ways a turn can end before its tool process was working. It cannot complete from there.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum StartOutcome {
    Failed { reason: String },
    Stopped { by: String },
    Interrupted { reason: String },
}

impl StartOutcome {
    fn into_outcome(self) -> Outcome {
        match self {
            Self::Failed { reason } => Outcome::not_started(reason),
            Self::Stopped { by } => Outcome::stopped(crate::control::Control::by(by)),
            Self::Interrupted { reason } => Outcome::Interrupted { reason },
        }
    }
}

/// The ways a running turn can end. It cannot be withdrawn any more.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RunOutcome {
    Completed,
    Failed { reason: String },
    Stopped { by: String },
    Interrupted { reason: String },
}

impl RunOutcome {
    fn into_outcome(self) -> Outcome {
        match self {
            Self::Completed => Outcome::Completed,
            Self::Failed { reason } => Outcome::failed(reason),
            Self::Stopped { by } => Outcome::stopped(crate::control::Control::by(by)),
            Self::Interrupted { reason } => Outcome::Interrupted { reason },
        }
    }
}

impl Running {
    pub fn bind(mut self, backend_session: String) -> (Self, TurnEvent) {
        self.backend_session = Some(backend_session.clone());
        (self, TurnEvent::Bound { backend_session })
    }

    pub fn moved(mut self, activity: Activity, at_ms: i64) -> (Self, TurnEvent) {
        if matches!(activity, Activity::Tool { .. }) {
            self.tool_count = self.tool_count.saturating_add(1);
        }
        self.activity = activity.clone();
        (self, TurnEvent::Moved { activity, at_ms })
    }

    pub fn settle(self, outcome: RunOutcome, at_ms: i64) -> (Settled, TurnEvent) {
        let outcome = outcome.into_outcome();
        (
            self.ended(outcome.clone(), at_ms),
            TurnEvent::Settled { outcome, at_ms },
        )
    }

    /// The turn as it is once it has ended this way.
    fn ended(self, outcome: Outcome, at_ms: i64) -> Settled {
        Settled {
            id: self.id,
            message_seq: self.message_seq,
            origin: self.origin,
            outcome,
            accepted_at_ms: self.accepted_at_ms,
            started_at_ms: Some(self.started_at_ms),
            ended_at_ms: at_ms,
            tool_count: self.tool_count,
            attempts: self.attempt,
            account_waits: self.account.count,
        }
    }

    /// The attempt's tool process is gone on a failure that does not end the turn.
    pub fn park(self, wait: Wait, kind: String, reason: String, at_ms: i64) -> (Parked, TurnEvent) {
        let event = TurnEvent::Parked {
            wait: wait.clone(),
            kind: kind.clone(),
            reason: reason.clone(),
            at_ms,
        };
        // A wait for an account is counted, and all of them are bounded from the first.
        let account = if wait.is_for_an_account() {
            AccountWaits {
                count: self.account.count + 1,
                since_ms: self.account.since_ms.or(Some(at_ms)),
            }
        } else {
            self.account
        };
        let next = Parked {
            id: self.id,
            message_seq: self.message_seq,
            origin: self.origin,
            accepted_at_ms: self.accepted_at_ms,
            started_at_ms: self.started_at_ms,
            attempts: self.attempt,
            backend_session: self.backend_session,
            tool_count: self.tool_count,
            wait,
            kind,
            reason,
            since_ms: at_ms,
            account,
        };
        (next, event)
    }
}

impl Parked {
    /// The wait is over: the next attempt starts.
    pub fn retry(self, at_ms: i64) -> (Starting, TurnEvent) {
        let next = Starting {
            id: self.id,
            message_seq: self.message_seq,
            origin: self.origin,
            accepted_at_ms: self.accepted_at_ms,
            started_at_ms: self.started_at_ms,
            attempt: self.attempts + 1,
            backend_session: self.backend_session,
            tool_count: self.tool_count,
            account: self.account,
            recovering: self.wait.is_for_an_account(),
        };
        (next, TurnEvent::Retried { at_ms })
    }

    /// The wait did not end well: the turn ends on the failure it waited on.
    pub fn give_up(self, at_ms: i64) -> (Settled, TurnEvent) {
        let outcome = Outcome::Failed {
            reason: self.reason.clone(),
            kind: self.kind.clone(),
        };
        (
            self.ended(outcome.clone(), at_ms),
            TurnEvent::Settled { outcome, at_ms },
        )
    }

    /// Someone stopped the turn while it waited. There is no tool process to wait for.
    pub fn stop(self, by: String, at_ms: i64) -> (Settled, TurnEvent) {
        let outcome = Outcome::stopped(crate::control::Control::by(by));
        (
            self.ended(outcome.clone(), at_ms),
            TurnEvent::Settled { outcome, at_ms },
        )
    }

    fn ended(self, outcome: Outcome, at_ms: i64) -> Settled {
        Settled {
            id: self.id,
            message_seq: self.message_seq,
            origin: self.origin,
            outcome,
            accepted_at_ms: self.accepted_at_ms,
            started_at_ms: Some(self.started_at_ms),
            ended_at_ms: at_ms,
            tool_count: self.tool_count,
            attempts: self.attempts,
            account_waits: self.account.count,
        }
    }
}

impl TurnEvent {
    fn name(&self) -> &'static str {
        match self {
            Self::Accepted { .. } => "accepted",
            Self::Started { .. } => "started",
            Self::Bound { .. } => "bound",
            Self::Moved { .. } => "moved",
            Self::Parked { .. } => "parked",
            Self::Retried { .. } => "retried",
            Self::Settled { .. } => "settled",
        }
    }
}

impl Turn {
    pub fn id(&self) -> TurnId {
        match self {
            Self::Queued(t) => t.id,
            Self::Starting(t) => t.id,
            Self::Running(t) => t.id,
            Self::Parked(t) => t.id,
            Self::Settled(t) => t.id,
        }
    }

    pub fn state_name(&self) -> &'static str {
        match self {
            Self::Queued(_) => "queued",
            Self::Starting(_) => "starting",
            Self::Running(_) => "running",
            Self::Parked(_) => "parked",
            Self::Settled(_) => "settled",
        }
    }

    /// Whether the turn holds the agent: an agent runs one turn at a time, and a turn that
    /// waits for its next attempt is still that turn.
    pub fn is_live(&self) -> bool {
        matches!(self, Self::Starting(_) | Self::Running(_) | Self::Parked(_))
    }

    /// What the turn's waits for an account have been so far.
    pub fn account_waits(&self) -> AccountWaits {
        match self {
            Self::Starting(t) => t.account,
            Self::Running(t) => t.account,
            Self::Parked(t) => t.account,
            Self::Queued(_) | Self::Settled(_) => AccountWaits::default(),
        }
    }

    /// Whether a tool process works for the turn, or is being started for it. A turn between
    /// two attempts has none.
    pub fn has_process(&self) -> bool {
        matches!(self, Self::Starting(_) | Self::Running(_))
    }

    /// The turn a log begins with. Any other first event is an illegal transition.
    pub fn begin(event: &TurnEvent) -> Result<Self, IllegalTransition> {
        match event {
            TurnEvent::Accepted {
                turn,
                message_seq,
                origin,
                admission,
                at_ms,
                ..
            } => Ok(Self::Queued(
                Queued::accept(*turn, *message_seq, origin.clone(), *admission, *at_ms).0,
            )),
            other => Err(IllegalTransition {
                turn: TurnId(0),
                state: "nothing",
                event: other.name(),
            }),
        }
    }

    /// Replays one event read from the log.
    pub fn apply(self, event: &TurnEvent) -> Result<Self, IllegalTransition> {
        let illegal = |turn: &Self| IllegalTransition {
            turn: turn.id(),
            state: turn.state_name(),
            event: event.name(),
        };
        match (self, event) {
            (Self::Queued(t), TurnEvent::Started { at_ms, .. }) => {
                Ok(Self::Starting(t.start(*at_ms).0))
            }
            (
                Self::Queued(t),
                TurnEvent::Settled {
                    outcome: Outcome::Withdrawn { by },
                    at_ms,
                },
            ) => Ok(Self::Settled(t.withdraw(by.clone(), *at_ms).0)),
            (
                Self::Starting(t),
                TurnEvent::Moved {
                    activity: Activity::Thinking,
                    at_ms,
                },
            ) => Ok(Self::Running(t.running(*at_ms).0)),
            // An ended turn is as its log says it ended: nothing is read a second time.
            (Self::Starting(t), TurnEvent::Settled { outcome, at_ms }) => match outcome {
                Outcome::Failed { .. }
                | Outcome::Stopped { .. }
                | Outcome::Interrupted { .. }
                | Outcome::Replaced { .. } => Ok(Self::Settled(t.ended(outcome.clone(), *at_ms))),
                Outcome::Completed | Outcome::Withdrawn { .. } => Err(illegal(&Self::Starting(t))),
            },
            (Self::Running(t), TurnEvent::Bound { backend_session }) => {
                Ok(Self::Running(t.bind(backend_session.clone()).0))
            }
            (Self::Running(t), TurnEvent::Moved { activity, at_ms }) => {
                Ok(Self::Running(t.moved(activity.clone(), *at_ms).0))
            }
            (Self::Running(t), TurnEvent::Settled { outcome, at_ms }) => match outcome {
                Outcome::Completed
                | Outcome::Failed { .. }
                | Outcome::Stopped { .. }
                | Outcome::Interrupted { .. }
                | Outcome::Replaced { .. } => Ok(Self::Settled(t.ended(outcome.clone(), *at_ms))),
                Outcome::Withdrawn { .. } => Err(illegal(&Self::Running(t))),
            },
            (
                Self::Running(t),
                TurnEvent::Parked {
                    wait,
                    kind,
                    reason,
                    at_ms,
                },
            ) => Ok(Self::Parked(
                t.park(wait.clone(), kind.clone(), reason.clone(), *at_ms).0,
            )),
            (Self::Parked(t), TurnEvent::Retried { at_ms }) => {
                Ok(Self::Starting(t.retry(*at_ms).0))
            }
            // A turn that waits has no tool that could complete it.
            (Self::Parked(t), TurnEvent::Settled { outcome, at_ms }) => match outcome {
                Outcome::Failed { .. } | Outcome::Stopped { .. } | Outcome::Interrupted { .. } => {
                    Ok(Self::Settled(t.ended(outcome.clone(), *at_ms)))
                }
                Outcome::Completed | Outcome::Withdrawn { .. } | Outcome::Replaced { .. } => {
                    Err(illegal(&Self::Parked(t)))
                }
            },
            (state, _) => Err(illegal(&state)),
        }
    }

    /// Rebuilds a turn from its events, oldest first.
    pub fn replay<'a>(
        events: impl IntoIterator<Item = &'a TurnEvent>,
    ) -> Result<Option<Self>, IllegalTransition> {
        let mut turn: Option<Self> = None;
        for event in events {
            turn = Some(match turn {
                None => Self::begin(event)?,
                Some(current) => current.apply(event)?,
            });
        }
        Ok(turn)
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    fn accepted() -> (Queued, TurnEvent) {
        Queued::accept(TurnId(7), 41, Origin::User, Admission::Queue, 1_000)
    }

    #[test]
    fn a_turn_waits_between_two_attempts_and_is_the_same_turn_after() {
        let (queued, accepted) = accepted();
        let (starting, started) = queued.start(1_100);
        assert_eq!(starting.attempt, 1);
        let (running, moved) = starting.running(1_200);
        let (running, bound) = running.bind("sess-1".to_string());
        let (running, tool) = running.moved(
            Activity::Tool {
                name: "Bash".to_string(),
            },
            1_250,
        );
        // The connection drops: the turn is not over.
        let (parked, dropped) = running.park(
            Wait::Retry { until_ms: 2_300 },
            "connection".to_string(),
            "read ECONNRESET".to_string(),
            1_300,
        );
        assert_eq!(
            (parked.attempts, parked.tool_count, parked.since_ms),
            (1, 1, 1_300)
        );
        let waiting = Turn::Parked(parked.clone());
        assert!(waiting.is_live(), "it holds the agent");
        assert!(!waiting.has_process(), "and no tool works for it");
        // The next attempt is the same turn: counted from its first start, on the tool's
        // session, with the tools that ran.
        let (again, retried) = parked.clone().retry(2_300);
        assert_eq!(
            (again.attempt, again.started_at_ms, again.tool_count),
            (2, 1_100, 1)
        );
        assert_eq!(again.backend_session.as_deref(), Some("sess-1"));
        let (running, moved_again) = again.running(2_400);
        assert_eq!(
            (
                running.attempt,
                running.tool_count,
                running.backend_session.as_deref()
            ),
            (2, 1, Some("sess-1"))
        );
        let (settled, done) = running.settle(RunOutcome::Completed, 2_900);
        assert_eq!(
            (
                settled.started_at_ms,
                settled.ended_at_ms,
                settled.tool_count
            ),
            (Some(1_100), 2_900, 1)
        );
        let history = [
            accepted.clone(),
            started.clone(),
            moved.clone(),
            bound.clone(),
            tool.clone(),
            dropped.clone(),
            retried,
            moved_again,
            done,
        ];
        assert_eq!(
            Turn::replay(&history).unwrap(),
            Some(Turn::Settled(settled))
        );

        // A wait that does not end well ends the turn on the failure it waited on.
        let (gave_up, failed) = parked.clone().give_up(9_000);
        assert_eq!(
            gave_up.outcome,
            Outcome::Failed {
                reason: "read ECONNRESET".to_string(),
                kind: "connection".to_string()
            }
        );
        let waited = [accepted, started, moved, bound, tool, dropped];
        let mut ended = waited.to_vec();
        ended.push(failed);
        assert_eq!(Turn::replay(&ended).unwrap(), Some(Turn::Settled(gave_up)));
        // And a stop ends it at once: there is no tool to wait for.
        let (stopped, by_hand) = parked.stop("user".to_string(), 1_500);
        assert_eq!(
            stopped.outcome,
            Outcome::stopped(crate::control::Control::by("user"))
        );
        let mut halted = waited.to_vec();
        halted.push(by_hand);
        assert_eq!(Turn::replay(&halted).unwrap(), Some(Turn::Settled(stopped)));
        // Nothing completes a turn that waits, and nothing moves it but its next attempt.
        for illegal in [
            TurnEvent::Settled {
                outcome: Outcome::Completed,
                at_ms: 1_400,
            },
            TurnEvent::Moved {
                activity: Activity::Thinking,
                at_ms: 1_400,
            },
            TurnEvent::Started {
                at_ms: 1_400,
                takeover: None,
            },
        ] {
            let mut wrong = waited.to_vec();
            wrong.push(illegal);
            assert!(Turn::replay(&wrong).is_err());
        }
    }

    #[test]
    fn a_failed_turn_keeps_the_kind_it_was_read_as() {
        let (queued, accepted) = accepted();
        let (starting, started) = queued.start(1_100);
        let (running, moved) = starting.running(1_200);
        let (settled, failed) = running.settle(
            RunOutcome::Failed {
                reason: "read ECONNRESET".to_string(),
            },
            1_300,
        );
        // Read when it happened.
        assert_eq!(
            settled.outcome,
            Outcome::Failed {
                reason: "read ECONNRESET".to_string(),
                kind: "connection".to_string()
            }
        );
        // A log says how a turn ended, and is not read again: wordings change, what
        // happened does not.
        let kept = TurnEvent::Settled {
            outcome: Outcome::Failed {
                reason: "read ECONNRESET".to_string(),
                kind: "as it was read then".to_string(),
            },
            at_ms: 1_300,
        };
        let Some(Turn::Settled(replayed)) =
            Turn::replay(&[accepted.clone(), started.clone(), moved.clone(), kept]).unwrap()
        else {
            panic!("the turn has ended");
        };
        assert_eq!(
            replayed.outcome,
            Outcome::Failed {
                reason: "read ECONNRESET".to_string(),
                kind: "as it was read then".to_string()
            }
        );
        // A log from before the kind was kept has none, and reads as it was written.
        let old: TurnEvent = serde_json::from_str(
            r#"{"event":"settled","outcome":{"outcome":"failed","reason":"boom"},"at_ms":1300}"#,
        )
        .unwrap();
        assert_eq!(
            old,
            TurnEvent::Settled {
                outcome: Outcome::Failed {
                    reason: "boom".to_string(),
                    kind: String::new()
                },
                at_ms: 1_300
            }
        );
        assert_eq!(
            serde_json::to_string(&old).unwrap(),
            r#"{"event":"settled","outcome":{"outcome":"failed","reason":"boom"},"at_ms":1300}"#
        );
        assert_eq!(
            Turn::replay(&[accepted, started, moved, failed]).unwrap(),
            Some(Turn::Settled(settled))
        );
    }

    /// Drives a whole turn with the typed transitions and returns the end state with its events.
    fn a_completed_turn() -> (Settled, Vec<TurnEvent>) {
        let mut events = Vec::new();
        let (queued, e) = accepted();
        events.push(e);
        let (starting, e) = queued.start(1_100);
        events.push(e);
        let (running, e) = starting.running(1_200);
        events.push(e);
        let (running, e) = running.bind("backend-session-1".to_string());
        events.push(e);
        let (running, e) = running.moved(
            Activity::Tool {
                name: "Bash".to_string(),
            },
            1_300,
        );
        events.push(e);
        let (running, e) = running.moved(Activity::Thinking, 1_400);
        events.push(e);
        let (running, e) = running.moved(
            Activity::Tool {
                name: "Read".to_string(),
            },
            1_500,
        );
        events.push(e);
        let (settled, e) = running.settle(RunOutcome::Completed, 1_600);
        events.push(e);
        (settled, events)
    }

    #[test]
    fn replaying_the_events_gives_the_state_the_typed_transitions_reached() {
        let (settled, events) = a_completed_turn();
        assert_eq!(settled.tool_count, 2);
        assert_eq!(settled.started_at_ms, Some(1_100));
        assert_eq!(settled.ended_at_ms, 1_600);
        assert_eq!(Turn::replay(&events).unwrap(), Some(Turn::Settled(settled)));
        // And at every prefix the replay is the state the turn was in then.
        let after_two = Turn::replay(&events[..2]).unwrap().unwrap();
        assert_eq!(after_two.state_name(), "starting");
        assert!(after_two.is_live());
        let after_four = Turn::replay(&events[..4]).unwrap().unwrap();
        assert!(
            matches!(after_four, Turn::Running(ref r) if r.backend_session.as_deref() == Some("backend-session-1"))
        );
        assert_eq!(Turn::replay(&[]).unwrap(), None);
    }

    #[test]
    fn events_survive_the_log() {
        let (_, events) = a_completed_turn();
        let text = serde_json::to_string(&events).unwrap();
        let back: Vec<TurnEvent> = serde_json::from_str(&text).unwrap();
        assert_eq!(back, events);
        assert!(
            text.contains(r#""event":"accepted""#)
                && text.contains(r#""from":"user""#)
                && text.contains(r#""outcome":"completed""#),
            "{text}"
        );
    }

    #[test]
    fn a_queued_turn_can_be_withdrawn_and_a_failed_start_is_a_failure() {
        let (queued, accepted) = accepted();
        let (settled, withdrawn) = queued.withdraw("peter".to_string(), 1_050);
        assert_eq!(
            settled.started_at_ms, None,
            "a withdrawn turn never started"
        );
        assert_eq!(
            Turn::replay(&[accepted.clone(), withdrawn]).unwrap(),
            Some(Turn::Settled(settled))
        );

        let (queued, _) =
            super::Queued::accept(TurnId(7), 41, Origin::User, Admission::Queue, 1_000);
        let (starting, started) = queued.start(1_100);
        let (settled, failed) = starting.settle(
            StartOutcome::Failed {
                reason: "the tool is not installed".to_string(),
            },
            1_150,
        );
        assert_eq!(
            settled.outcome,
            Outcome::not_started("the tool is not installed".to_string())
        );
        assert_eq!(
            Turn::replay(&[accepted, started, failed]).unwrap(),
            Some(Turn::Settled(settled))
        );
    }

    #[test]
    fn a_log_that_makes_no_sense_is_refused() {
        let (_, accepted) = accepted();
        let started = TurnEvent::Started {
            at_ms: 2,
            takeover: None,
        };
        let thinking = TurnEvent::Moved {
            activity: Activity::Thinking,
            at_ms: 3,
        };
        let completed = TurnEvent::Settled {
            outcome: Outcome::Completed,
            at_ms: 4,
        };
        let withdrawn = TurnEvent::Settled {
            outcome: Outcome::Withdrawn {
                by: "x".to_string(),
            },
            at_ms: 4,
        };
        let illegal: Vec<(&str, Vec<TurnEvent>)> = vec![
            ("a turn does not begin with a start", vec![started.clone()]),
            (
                "a queued turn cannot complete",
                vec![accepted.clone(), completed.clone()],
            ),
            (
                "a queued turn does not move",
                vec![accepted.clone(), thinking.clone()],
            ),
            (
                "a turn that is starting cannot complete",
                vec![accepted.clone(), started.clone(), completed.clone()],
            ),
            (
                "a turn that is starting cannot be withdrawn",
                vec![accepted.clone(), started.clone(), withdrawn.clone()],
            ),
            (
                "a running turn cannot be withdrawn",
                vec![
                    accepted.clone(),
                    started.clone(),
                    thinking.clone(),
                    withdrawn,
                ],
            ),
            (
                "a running turn does not start again",
                vec![
                    accepted.clone(),
                    started.clone(),
                    thinking.clone(),
                    started.clone(),
                ],
            ),
            (
                "a settled turn does not move",
                vec![
                    accepted.clone(),
                    started.clone(),
                    thinking.clone(),
                    completed.clone(),
                    thinking.clone(),
                ],
            ),
            (
                "a settled turn does not settle twice",
                vec![
                    accepted.clone(),
                    started.clone(),
                    thinking.clone(),
                    completed.clone(),
                    completed,
                ],
            ),
            ("a turn is accepted once", vec![accepted.clone(), accepted]),
        ];
        for (why, events) in illegal {
            let refused = Turn::replay(&events);
            assert!(refused.is_err(), "{why}");
        }
    }

    #[test]
    fn the_refusal_names_the_turn_the_state_and_the_event() {
        let (_, accepted) = accepted();
        let error = Turn::replay(&[
            accepted,
            TurnEvent::Settled {
                outcome: Outcome::Completed,
                at_ms: 9,
            },
        ])
        .unwrap_err();
        assert_eq!(
            error,
            IllegalTransition {
                turn: TurnId(7),
                state: "queued",
                event: "settled"
            }
        );
        assert_eq!(
            error.to_string(),
            "turn 7: `settled` cannot follow the state `queued`"
        );
    }
}
