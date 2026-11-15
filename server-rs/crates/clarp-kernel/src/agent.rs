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

use crate::turn::{
    Activity, Admission, IllegalTransition, Origin, Outcome, Queued, RunOutcome, Settled,
    StartOutcome, Turn, TurnEvent, TurnId,
};

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
    /// End the live turn and hold the mailbox.
    Stop { by: String },
    /// Let the mailbox run again.
    Resume { by: String },
    /// The agent says, in a few words, what it is busy with beyond its turns: work it left
    /// running. An empty label takes that back.
    Label { text: String },
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
    /// The agent comes from another Host, with the tool's session it had there.
    Adopt {
        from: String,
        backend_session: Option<String>,
    },
}

/// What happened to an agent. These, in order, are its history.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "agent", rename_all = "kebab-case")]
pub enum AgentEvent {
    Turn {
        turn: TurnId,
        change: TurnEvent,
    },
    /// Someone asked the live turn to stop. It settles when the tool process is gone.
    StopRequested {
        turn: TurnId,
        by: String,
        at_ms: i64,
    },
    Paused {
        by: String,
        at_ms: i64,
    },
    Resumed {
        by: String,
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
}

/// What the actor must do after the events are in the log.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Effect {
    StartTurn { turn: TurnId, message_seq: u64 },
    StopTurn { turn: TurnId },
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
    /// A resume for an agent that is not held.
    NotPaused,
    /// An agent that has run a turn here is not one that just arrived.
    AlreadyAtWork,
}

impl std::fmt::Display for Refusal {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::NotWaiting(turn) => write!(f, "turn {} is not waiting", turn.0),
            Self::NotLive(turn) => write!(f, "turn {} is not the live turn", turn.0),
            Self::NotPaused => write!(f, "the agent is not paused"),
            Self::AlreadyAtWork => write!(f, "the agent has already worked on this Host"),
        }
    }
}

impl std::error::Error for Refusal {}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Agent {
    next_turn: u64,
    mailbox: VecDeque<Queued>,
    /// The turn that holds the agent: starting or running, never anything else.
    live: Option<Turn>,
    /// Who asked the live turn to stop, while it has not gone yet.
    stopping: Option<String>,
    paused: bool,
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
    /// What the agent says it is busy with beyond its turns. Empty: nothing.
    label: String,
    /// What the agent rests in since it last said so, until its next turn starts.
    resting: Option<Rest>,
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
            next_turn: 1,
            mailbox: VecDeque::new(),
            live: None,
            stopping: None,
            paused: false,
            settled: 0,
            queue_revision: 0,
            waited: BTreeSet::new(),
            live_from_queue: false,
            unheld: BTreeSet::new(),
            last: None,
            backend_session: None,
            label: String::new(),
            resting: None,
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

    pub fn waiting(&self) -> impl Iterator<Item = &Queued> {
        self.mailbox.iter()
    }

    pub fn is_paused(&self) -> bool {
        self.paused
    }

    /// How many turns have ended, however they ended.
    pub fn settled(&self) -> u64 {
        self.settled
    }

    /// How often the queue has changed: a message had to wait, a waiting one started or was
    /// taken back, the mailbox was held or let go.
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

    /// What the agent says it is busy with beyond its turns. Empty: nothing.
    pub fn label(&self) -> &str {
        &self.label
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
    pub fn stopping(&self) -> Option<&str> {
        self.stopping.as_deref()
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
                    if let (Some(live), None) = (work.agent.live_id(), &work.agent.stopping) {
                        work.record(AgentEvent::StopRequested {
                            turn: live,
                            by: preempted_by(turn),
                            at_ms: now_ms,
                        });
                        work.decision.effects.push(Effect::StopTurn { turn: live });
                    }
                }
            }
            Command::Withdraw { turn, by } => {
                if !work.agent.mailbox.iter().any(|queued| queued.id == turn) {
                    return Err(Refusal::NotWaiting(turn));
                }
                work.record(AgentEvent::Turn {
                    turn,
                    change: TurnEvent::Settled {
                        outcome: Outcome::Withdrawn { by },
                        at_ms: now_ms,
                    },
                });
            }
            Command::Stop { by } => {
                if let (Some(turn), None) = (work.agent.live_id(), &work.agent.stopping) {
                    work.record(AgentEvent::StopRequested {
                        turn,
                        by: by.clone(),
                        at_ms: now_ms,
                    });
                    work.decision.effects.push(Effect::StopTurn { turn });
                }
                // A stop holds everything that waits, also what an earlier hold let through.
                if !work.agent.paused || !work.agent.unheld.is_empty() {
                    work.record(AgentEvent::Paused { by, at_ms: now_ms });
                }
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
            Command::TurnStarted { turn } => {
                work.must_be_live(turn)?;
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
                work.must_be_live(turn)?;
                let outcome = match work.agent.stopping.clone() {
                    Some(by) => Outcome::Stopped { by },
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
                work.must_be_live(turn)?;
                work.running(turn, now_ms);
                work.record(AgentEvent::Turn {
                    turn,
                    change: TurnEvent::Bound { backend_session },
                });
            }
            Command::TurnMoved { turn, activity } => {
                work.must_be_live(turn)?;
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
                work.must_be_live(turn)?;
                let outcome = match (work.agent.stopping.clone(), outcome) {
                    // Whatever the process said on its way out, it went because it was told to.
                    (Some(by), _) => Outcome::Stopped { by },
                    (None, RunOutcome::Completed) => {
                        // A tool that finished before it said it had started did still run.
                        work.running(turn, now_ms);
                        Outcome::Completed
                    }
                    (None, RunOutcome::Failed { reason }) => Outcome::failed(reason),
                    (None, RunOutcome::Stopped { by }) => Outcome::Stopped { by },
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
        }
        work.start_next(now_ms);
        Ok(work.decision)
    }

    /// Changes the state by one event. The only way the state changes.
    pub fn apply(&mut self, event: &AgentEvent) -> Result<(), IllegalTransition> {
        match event {
            AgentEvent::Paused { .. } => {
                self.paused = true;
                self.unheld.clear();
                self.queue_revision += 1;
            }
            AgentEvent::Resumed { .. } => {
                self.paused = false;
                self.unheld.clear();
                self.queue_revision += 1;
            }
            AgentEvent::StopRequested { turn, by, .. } => {
                if self.live_id() != Some(*turn) {
                    return Err(IllegalTransition {
                        turn: *turn,
                        state: "not live",
                        event: "stop-requested",
                    });
                }
                self.stopping = Some(by.clone());
            }
            AgentEvent::Adopted {
                backend_session, ..
            } => self.backend_session.clone_from(backend_session),
            AgentEvent::Labelled { text, .. } => {
                self.resting = Some(if text.is_empty() {
                    Rest::Idle
                } else {
                    Rest::Background
                });
                self.label.clone_from(text);
            }
            AgentEvent::Turn { turn, change } => self.apply_turn(*turn, change)?,
        }
        Ok(())
    }

    fn apply_turn(&mut self, turn: TurnId, change: &TurnEvent) -> Result<(), IllegalTransition> {
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
                self.backend_session = Some(backend_session.clone());
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

    fn must_be_live(&self, turn: TurnId) -> Result<(), Refusal> {
        if self.agent.live_id() == Some(turn) {
            Ok(())
        } else {
            Err(Refusal::NotLive(turn))
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
                by: "peter".to_string()
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
            matches!(a.history.last(), Some(AgentEvent::Turn { change: TurnEvent::Settled { outcome: Outcome::Stopped { by }, .. }, .. }) if by == "peter")
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
            by: "peter".to_string(),
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
            by: "peter".to_string(),
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
            AgentEvent::Turn { turn: TurnId(1), change: TurnEvent::Settled { outcome: Outcome::Stopped { by }, .. } }
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
            by: "peter".to_string(),
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
            by: "peter".to_string(),
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
            by: "peter".to_string(),
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
                let command = match next(10) {
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
                        by: "peter".to_string(),
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
