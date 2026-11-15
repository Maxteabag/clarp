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
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "from", rename_all = "kebab-case")]
pub enum Origin {
    User,
    Agent { agent_id: String },
    Trigger { name: String },
}

/// How a message enters the mailbox.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum Admission {
    /// Said to the agent: it is in the conversation from now on, and the agent comes to it when
    /// it is free. A hold that was placed before it came does not hold it: someone said this
    /// after the stop, and means it.
    #[default]
    Queue,
    /// The user wants it now: it ends the live turn, goes ahead of what waits its turn, and runs
    /// even while the agent is paused, without lifting the pause.
    Direct,
    /// Left for later by someone: it waits its turn like a message that was said, but stays
    /// out of the conversation until its turn starts. Until then it is in the queue, where it
    /// can be taken back.
    Later,
    /// Work nobody sits waiting for: it waits in the queue like `Later`, and a hold holds it
    /// like everything that waited when the hold was placed.
    Background,
}

impl Admission {
    fn is_queue(&self) -> bool {
        *self == Self::Queue
    }

    /// Whether a message admitted this way stays out of the conversation while it waits.
    pub fn waits_outside(self) -> bool {
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
    },
    Stopped {
        by: String,
    },
    /// The tool process went away under the turn: a crash, a killed shim, a lost machine.
    Interrupted {
        reason: String,
    },
    /// Taken back before it started.
    Withdrawn {
        by: String,
    },
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
    },
    Started {
        at_ms: i64,
    },
    /// The tool told us which of its own sessions carries this turn.
    Bound {
        backend_session: String,
    },
    Moved {
        activity: Activity,
        at_ms: i64,
    },
    Settled {
        outcome: Outcome,
        at_ms: i64,
    },
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

/// A turn whose tool process is being started.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Starting {
    pub id: TurnId,
    pub message_seq: u64,
    pub origin: Origin,
    pub accepted_at_ms: i64,
    pub started_at_ms: i64,
}

/// A turn whose tool process is working.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Running {
    pub id: TurnId,
    pub message_seq: u64,
    pub origin: Origin,
    pub accepted_at_ms: i64,
    pub started_at_ms: i64,
    pub backend_session: Option<String>,
    pub activity: Activity,
    pub tool_count: u32,
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
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Turn {
    Queued(Queued),
    Starting(Starting),
    Running(Running),
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
        };
        (next, TurnEvent::Started { at_ms })
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
            backend_session: None,
            activity: Activity::Thinking,
            tool_count: 0,
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
        let next = Settled {
            id: self.id,
            message_seq: self.message_seq,
            origin: self.origin,
            outcome: outcome.clone(),
            accepted_at_ms: self.accepted_at_ms,
            started_at_ms: Some(self.started_at_ms),
            ended_at_ms: at_ms,
            tool_count: 0,
        };
        (next, TurnEvent::Settled { outcome, at_ms })
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
            Self::Failed { reason } => Outcome::Failed { reason },
            Self::Stopped { by } => Outcome::Stopped { by },
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
            Self::Failed { reason } => Outcome::Failed { reason },
            Self::Stopped { by } => Outcome::Stopped { by },
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
        let next = Settled {
            id: self.id,
            message_seq: self.message_seq,
            origin: self.origin,
            outcome: outcome.clone(),
            accepted_at_ms: self.accepted_at_ms,
            started_at_ms: Some(self.started_at_ms),
            ended_at_ms: at_ms,
            tool_count: self.tool_count,
        };
        (next, TurnEvent::Settled { outcome, at_ms })
    }
}

impl TurnEvent {
    fn name(&self) -> &'static str {
        match self {
            Self::Accepted { .. } => "accepted",
            Self::Started { .. } => "started",
            Self::Bound { .. } => "bound",
            Self::Moved { .. } => "moved",
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
            Self::Settled(t) => t.id,
        }
    }

    pub fn state_name(&self) -> &'static str {
        match self {
            Self::Queued(_) => "queued",
            Self::Starting(_) => "starting",
            Self::Running(_) => "running",
            Self::Settled(_) => "settled",
        }
    }

    /// Whether the turn holds the agent: an agent runs one turn at a time.
    pub fn is_live(&self) -> bool {
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
            (Self::Queued(t), TurnEvent::Started { at_ms }) => {
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
            (Self::Starting(t), TurnEvent::Settled { outcome, at_ms }) => match outcome {
                Outcome::Failed { reason } => Ok(Self::Settled(
                    t.settle(
                        StartOutcome::Failed {
                            reason: reason.clone(),
                        },
                        *at_ms,
                    )
                    .0,
                )),
                Outcome::Stopped { by } => Ok(Self::Settled(
                    t.settle(StartOutcome::Stopped { by: by.clone() }, *at_ms).0,
                )),
                Outcome::Interrupted { reason } => Ok(Self::Settled(
                    t.settle(
                        StartOutcome::Interrupted {
                            reason: reason.clone(),
                        },
                        *at_ms,
                    )
                    .0,
                )),
                Outcome::Completed | Outcome::Withdrawn { .. } => Err(illegal(&Self::Starting(t))),
            },
            (Self::Running(t), TurnEvent::Bound { backend_session }) => {
                Ok(Self::Running(t.bind(backend_session.clone()).0))
            }
            (Self::Running(t), TurnEvent::Moved { activity, at_ms }) => {
                Ok(Self::Running(t.moved(activity.clone(), *at_ms).0))
            }
            (Self::Running(t), TurnEvent::Settled { outcome, at_ms }) => match outcome {
                Outcome::Completed => Ok(Self::Settled(t.settle(RunOutcome::Completed, *at_ms).0)),
                Outcome::Failed { reason } => Ok(Self::Settled(
                    t.settle(
                        RunOutcome::Failed {
                            reason: reason.clone(),
                        },
                        *at_ms,
                    )
                    .0,
                )),
                Outcome::Stopped { by } => Ok(Self::Settled(
                    t.settle(RunOutcome::Stopped { by: by.clone() }, *at_ms).0,
                )),
                Outcome::Interrupted { reason } => Ok(Self::Settled(
                    t.settle(
                        RunOutcome::Interrupted {
                            reason: reason.clone(),
                        },
                        *at_ms,
                    )
                    .0,
                )),
                Outcome::Withdrawn { .. } => Err(illegal(&Self::Running(t))),
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
            Outcome::Failed {
                reason: "the tool is not installed".to_string()
            }
        );
        assert_eq!(
            Turn::replay(&[accepted, started, failed]).unwrap(),
            Some(Turn::Settled(settled))
        );
    }

    #[test]
    fn a_log_that_makes_no_sense_is_refused() {
        let (_, accepted) = accepted();
        let started = TurnEvent::Started { at_ms: 2 };
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
