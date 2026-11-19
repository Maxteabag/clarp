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

use std::collections::{BTreeSet, VecDeque};

use serde::{Deserialize, Serialize};

use crate::control::Control;

use crate::turn::{
    Activity, Admission, IllegalTransition, Origin, Outcome, Parked, Queued, RunOutcome, Settled,
    StartOutcome, Turn, TurnEvent, TurnId, Wait,
};

/// Who let a mailbox go that held nothing any more: the last thing that waited left to run.
pub const DRAINED: &str = "nothing waits";

/// Who stopped a turn that a direct message replaced.
pub fn preempted_by(turn: TurnId) -> String {
    format!("message of turn {}", turn.0)
}

/// What can be asked of an agent, by a client or by the shim that runs its turn.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Command {
    /// A message was written to the log at `message_seq` and wants a turn.
    Accept {
        message_seq: u64,
        origin: Origin,
        admission: Admission,
    },
    /// Take a waiting message back.
    Withdraw { turn: TurnId, by: String },
    /// Let one waiting message run now, whatever holds the mailbox: its user sends it by
    /// hand. Only when no turn runs.
    SendNow { turn: TurnId, by: String },
    /// The words of a waiting message were written anew, at `message_seq`: its turn answers
    /// those. It keeps its place among what waits.
    Reword { turn: TurnId, message_seq: u64 },
    /// End the live turn and hold the mailbox: who asks it, and why.
    Stop { by: Control },
    /// Someone looked for a turn of the agent that nothing runs, and gives it back: nothing is
    /// ended and nothing is held or let go. What is kept is a note of who and why, on the state.
    Repair { by: Control },
    /// Let the mailbox run again.
    Resume { by: String },
    /// The agent says, in a few words, what it is busy with beyond its turns: work it left
    /// running. An empty label takes that back.
    Label { text: String },
    /// A label that outlived its time is taken back, if it is still the one that was set at
    /// `set_at_ms`: one set again since stands.
    RetireLabel { set_at_ms: i64 },
    /// The shim: the tool process is up.
    TurnStarted { turn: TurnId },
    /// The shim: the tool process could not be started.
    StartFailed { turn: TurnId, reason: String },
    /// The shim: the tool named its own session.
    TurnBound {
        turn: TurnId,
        backend_session: String,
    },
    /// The shim: the tool is doing something else now.
    TurnMoved { turn: TurnId, activity: Activity },
    /// The shim: the tool process is gone, and this is how it went.
    TurnEnded { turn: TurnId, outcome: RunOutcome },
    /// The tool process is gone on a failure that does not end the turn: it waits for its
    /// next attempt. `kind` and `reason` are the failure, read and in the tool's words.
    TurnParked {
        turn: TurnId,
        wait: Wait,
        kind: String,
        reason: String,
    },
    /// The wait is over: start the turn's next attempt.
    TurnRetried { turn: TurnId },
    /// The wait did not end well: the turn ends on the failure it waited on.
    WaitOver { turn: TurnId },
    /// The agent comes from another Host, with the tool's session it had there.
    Adopt {
        from: String,
        backend_session: Option<String>,
    },
    /// The agent begins with a session its tool kept on this machine: its first turn
    /// continues it. Only before its first turn.
    TakeUp { backend_session: String },
    /// The agent is started again: its next turn begins a conversation with its tool
    /// instead of continuing the one it has. Only when no turn runs: a turn that runs is
    /// stopped first.
    StartAgain,
}

/// What happened to an agent. These, in order, are its history.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "agent", rename_all = "kebab-case")]
pub enum AgentEvent {
    Turn {
        turn: TurnId,
        change: TurnEvent,
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
    Resumed {
        by: String,
        at_ms: i64,
    },
    /// A waiting turn was sent by hand: it is the next to run, and no hold holds it.
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
    /// The turn is not waiting in the mailbox, so it cannot be withdrawn.
    NotWaiting(TurnId),
    /// A signal about a turn that is not the live one: late, or from a shim we no longer follow.
    NotLive(TurnId),
    /// The turn does not wait between two attempts.
    NotParked(TurnId),
    /// A resume for an agent that is not held.
    NotPaused,
    /// A turn of the agent runs: nothing that waits is sent by hand past it.
    AtWork,
    /// An agent that has run a turn here is not one that just arrived.
    AlreadyAtWork,
}

impl std::fmt::Display for Refusal {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::NotWaiting(turn) => write!(f, "turn {} is not waiting", turn.0),
            Self::NotLive(turn) => write!(f, "turn {} is not the live turn", turn.0),
            Self::NotParked(turn) => write!(f, "turn {} does not wait for an attempt", turn.0),
            Self::NotPaused => write!(f, "the agent is not paused"),
            Self::AtWork => write!(f, "a turn of the agent runs"),
            Self::AlreadyAtWork => write!(f, "the agent has already worked on this Host"),
        }
    }
}

impl std::error::Error for Refusal {}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Agent {
    latest_turn_start: Option<i64>,
    next_turn: u64,
    mailbox: VecDeque<Queued>,
    /// The turn that holds the agent: starting or running, never anything else.
    live: Option<Turn>,
    /// Who asked the live turn to stop and why, while it has not gone yet.
    stopping: Option<Control>,
    /// Who last repaired the agent, why, and when: while nothing about its state changed since.
    note: Option<Note>,
    paused: bool,
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
    /// The waiting turns a hold does not hold: they came after it was placed. The next stop
    /// holds them with the rest.
    unheld: BTreeSet<TurnId>,
    /// The last turn that held the agent and ended.
    last: Option<Settled>,
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
            next_turn: 1,
            mailbox: VecDeque::new(),
            live: None,
            stopping: None,
            note: None,
            paused: false,
            settled: 0,
            queue_revision: 0,
            stop_generation: 0,
            waited: BTreeSet::new(),
            live_from_queue: false,
            unheld: BTreeSet::new(),
            last: None,
            backend_session: None,
            refused_live_binding: false,
            label: String::new(),
            label_at: None,
            resting: None,
            arrived: false,
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

    pub fn is_paused(&self) -> bool {
        self.paused
    }

    /// Maximum committed turn start, rebuilt by the same event fold on open.
    pub fn latest_turn_start(&self) -> i64 {
        self.latest_turn_start.unwrap_or(0)
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

    /// Whether the live turn came through the queue: it had to wait, or it was left for later.
    pub fn live_from_queue(&self) -> bool {
        self.live.is_some() && self.live_from_queue
    }

    /// The last turn that held the agent and ended. A message that was taken back never did.
    pub fn last(&self) -> Option<&Settled> {
        self.last.as_ref()
    }

    /// The tool's own session, where the agent's next turn continues.
    pub fn backend_session(&self) -> Option<&str> {
        self.backend_session.as_deref()
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

    /// The note a repair left, while nothing about the agent's state changed since.
    pub fn note(&self) -> Option<&Note> {
        self.note.as_ref()
    }

    fn live_id(&self) -> Option<TurnId> {
        self.live.as_ref().map(Turn::id)
    }

    /// The waiting turn that runs when the agent is free: the first one no hold holds.
    fn next_to_run(&self) -> Option<&Queued> {
        self.mailbox.iter().find(|queued| {
            !self.paused
                || queued.admission == Admission::Direct
                || self.unheld.contains(&queued.id)
        })
    }

    /// Whether a message admitted this way now would start at once, without waiting.
    pub fn starts_at_once(&self, admission: Admission) -> bool {
        self.live.is_none() && !(self.paused && admission == Admission::Background)
    }

    /// Whether a hold holds this waiting turn.
    pub fn holds(&self, turn: TurnId) -> bool {
        self.paused
            && self.mailbox.iter().any(|queued| {
                queued.id == turn
                    && queued.admission != Admission::Direct
                    && !self.unheld.contains(&turn)
            })
    }

    /// Says what a command does, without doing it.
    pub fn decide(&self, command: Command, now_ms: i64) -> Result<Decision, Refusal> {
        // Work on a copy and move it only through `apply`, so that what is decided here is, by
        // construction, what a replay of the events will rebuild.
        let mut work = Work {
            agent: self.clone(),
            decision: Decision::default(),
        };
        match command {
            Command::Accept {
                message_seq,
                origin,
                admission,
            } => {
                let turn = TurnId(work.agent.next_turn);
                let (_, accepted) = Queued::accept(turn, message_seq, origin, admission, now_ms);
                work.record(AgentEvent::Turn {
                    turn,
                    change: accepted,
                });
                if admission == Admission::Direct {
                    // The live turn makes way. One that is already on its way out is left to it.
                    work.stop_live(Control::by(preempted_by(turn)), now_ms);
                }
            }
            Command::Withdraw { turn, by } => {
                if !work.agent.mailbox.iter().any(|queued| queued.id == turn) {
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
                if !work.agent.mailbox.iter().any(|queued| queued.id == turn) {
                    return Err(Refusal::NotWaiting(turn));
                }
                work.record(AgentEvent::Reworded {
                    turn,
                    message_seq,
                    at_ms: now_ms,
                });
            }
            Command::SendNow { turn, by } => {
                if !work.agent.mailbox.iter().any(|queued| queued.id == turn) {
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
            Command::Stop { by } => {
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
                if !work.agent.paused {
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
                if work.agent.live.is_some() {
                    return Err(Refusal::AtWork);
                }
                work.record(AgentEvent::StartedAgain { at_ms: now_ms });
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
            Command::TurnRetried { turn } => {
                let parked = work.parked(turn)?;
                let (message_seq, attempt) = (parked.message_seq, parked.attempts + 1);
                work.record(AgentEvent::Turn {
                    turn,
                    change: TurnEvent::Retried { at_ms: now_ms },
                });
                work.decision.effects.push(Effect::RetryTurn {
                    turn,
                    message_seq,
                    attempt,
                });
            }
            Command::WaitOver { turn } => {
                let parked = work.parked(turn)?;
                let outcome = Outcome::Failed {
                    reason: parked.reason.clone(),
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
                if !self.paused || !self.unheld.is_empty() {
                    self.queue_revision += 1;
                }
                self.paused = true;
                self.unheld.clear();
            }
            AgentEvent::Resumed { .. } => {
                self.paused = false;
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
            AgentEvent::SentNow { turn, .. } => {
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
                ..
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
            AgentEvent::StartedAgain { .. } => {
                self.backend_session = None;
                self.note = None;
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
        self.move_turn(turn, change)?;
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
                } else if !waits && self.live_id() != Some(turn) {
                    self.deliveries.over(turn.0);
                }
            }
        }
        Ok(())
    }

    fn move_turn(&mut self, turn: TurnId, change: &TurnEvent) -> Result<(), IllegalTransition> {
        if let TurnEvent::Accepted { turn: accepted, .. } = change {
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
                if self.live.is_some() || held || queued.admission.waits_outside() {
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
                } else {
                    self.mailbox.push_back(queued);
                }
            }
            self.next_turn += 1;
            return Ok(());
        }
        if self.live_id() == Some(turn) {
            let Some(live) = self.live.take() else {
                return Ok(());
            };
            if let TurnEvent::Bound { backend_session } = change {
                self.bind_session(Some(backend_session));
                self.refused_live_binding |= self.backend_session.is_none();
            }
            match live.apply(change)? {
                Turn::Settled(ended) => {
                    self.settled += 1;
                    self.stopping = None;
                    self.live_from_queue = false;
                    self.last = Some(ended);
                }
                next => self.live = Some(next),
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
        match Turn::Queued(queued).apply(change)? {
            Turn::Settled(_) => self.settled += 1,
            next => {
                if let TurnEvent::Started { at_ms } = change {
                    self.latest_turn_start =
                        Some(self.latest_turn_start.map_or(*at_ms, |old| old.max(*at_ms)));
                }
                self.live = Some(next);
                self.live_from_queue = waited;
                // A turn runs: what the agent rested in before is over.
                self.resting = None;
            }
        }
        Ok(())
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
        | TurnEvent::Started { at_ms }
        | TurnEvent::Moved { at_ms, .. }
        | TurnEvent::Parked { at_ms, .. }
        | TurnEvent::Retried { at_ms }
        | TurnEvent::Settled { at_ms, .. } => Some(*at_ms),
        TurnEvent::Bound { .. } => None,
    }
}

/// A copy of the agent being moved forward while a decision is made.
struct Work {
    agent: Agent,
    decision: Decision,
}

impl Work {
    fn record(&mut self, event: AgentEvent) {
        // `decide` only records events it has just shown to be legal; if one is not, the state
        // machine itself is wrong, and dropping the event is the one way not to make it worse.
        if self.agent.apply(&event).is_ok() {
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
        if self.agent.live.is_some() {
            return;
        }
        let Some(next) = self.agent.next_to_run() else {
            return;
        };
        let (turn, message_seq) = (next.id, next.message_seq);
        self.record(AgentEvent::Turn {
            turn,
            change: TurnEvent::Started { at_ms: now_ms },
        });
        self.decision
            .effects
            .push(Effect::StartTurn { turn, message_seq });
        if self.agent.live_from_queue {
            self.let_go_of_nothing(DRAINED, now_ms);
        }
    }

    /// A hold holds what waits. When the last of it has left the queue, taken back or gone
    /// to run, the hold is over: the next thing that is left for the agent is not held by
    /// a stop that held something else. So the Python Host ends a pause (`turn_queue`).
    fn let_go_of_nothing(&mut self, by: &str, now_ms: i64) {
        if self.agent.paused && self.agent.mailbox.is_empty() {
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
            self.now_ms += 10;
            let decision = self.agent.decide(command, self.now_ms)?;
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
            })
            .unwrap()
        }

        /// A message the user wants answered now.
        fn direct(&mut self, message_seq: u64) -> Vec<Effect> {
            self.send(Command::Accept {
                message_seq,
                origin: Origin::User,
                admission: Admission::Direct,
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

    /// The connection of the live turn's tool drops: the turn waits a moment for its next attempt.
    fn dropped(turn: u64) -> Command {
        Command::TurnParked {
            turn: TurnId(turn),
            wait: Wait::Retry { until_ms: 5_000 },
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

    #[test]
    fn a_waiting_message_is_sent_by_hand_past_a_hold_and_the_hold_ends_with_the_last() {
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
        // The last one is sent by hand: nothing waits any more, and the hold is over.
        assert_eq!(
            a.send(by_hand(2)).unwrap(),
            vec![Effect::StartTurn {
                turn: TurnId(2),
                message_seq: 11
            }]
        );
        assert!(!a.agent.is_paused());
        assert_eq!(
            a.agent.queue_revision(),
            revision + 5,
            "taken out of its place, it left, and the hold ended"
        );
        assert!(matches!(a.history.last(), Some(AgentEvent::Resumed { by, .. }) if by == DRAINED));
        a.finish(2);
        // Background work that comes now runs: no hold is left to hold it.
        assert_eq!(
            a.background(13),
            vec![Effect::StartTurn {
                turn: TurnId(4),
                message_seq: 13
            }]
        );
        assert_eq!(a.started, vec![TurnId(1), TurnId(3), TurnId(2), TurnId(4)]);
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
        a.user(10);
        a.send(Command::TurnStarted { turn: TurnId(1) }).unwrap();
        a.send(bound(1, "sess-1")).unwrap();
        assert_eq!(a.agent.backend_session(), Some("sess-1"));
        a.user(11);
        // While a turn of it runs it is not started again: that turn is stopped first.
        assert_eq!(a.send(Command::StartAgain), Err(Refusal::AtWork));
        a.send(Command::Stop {
            by: Control::by("relaunch"),
        })
        .unwrap();
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
        a.send(Command::TurnStarted { turn: TurnId(2) }).unwrap();
        a.send(bound(2, "sess-2")).unwrap();
        assert_eq!(a.agent.backend_session(), Some("sess-2"));
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
}
