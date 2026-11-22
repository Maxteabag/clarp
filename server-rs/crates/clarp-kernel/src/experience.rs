//! A bounded view of the user's send history, folded from the actor journal.

use std::collections::{BTreeMap, VecDeque};

use crate::agent::AgentEvent;
use crate::failures::NOT_STARTED;
use crate::turn::{Activity, Origin, Outcome, TurnEvent, TurnId};

const MOST: usize = 16_384;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Read {
    pub waited_ms: i64,
    pub steered: bool,
}

#[derive(Debug, Default)]
pub struct Window {
    pub reads: Vec<Read>,
    pub failed: usize,
}

struct Send {
    accepted: i64,
    start: Option<i64>,
    read: Option<i64>,
}

#[derive(Default)]
pub(crate) struct History {
    pending: BTreeMap<TurnId, Send>,
    reads: VecDeque<(TurnId, i64, Read)>,
    failed: VecDeque<i64>,
}

fn bounded_push<T>(history: &mut VecDeque<T>, value: T) {
    if history.len() == MOST {
        history.pop_front();
    }
    history.push_back(value);
}

impl History {
    pub(crate) fn observe(&mut self, event: &AgentEvent) {
        match event {
            AgentEvent::Turn { turn, change } => match change {
                TurnEvent::Accepted {
                    origin: Origin::User,
                    at_ms,
                    ..
                } => {
                    self.pending.insert(
                        *turn,
                        Send {
                            accepted: *at_ms,
                            start: None,
                            read: None,
                        },
                    );
                }
                TurnEvent::Started { at_ms, .. } => {
                    if let Some(send) = self.pending.get_mut(turn) {
                        send.start.get_or_insert(*at_ms);
                    }
                }
                TurnEvent::Moved {
                    activity: Activity::Thinking,
                    at_ms,
                } => {
                    if let Some(send) = self.pending.get_mut(turn)
                        && send.read.is_none()
                    {
                        send.read = Some(*at_ms);
                        let duration = at_ms.saturating_sub(send.accepted);
                        if send.start.is_some_and(|start| start > send.accepted) && duration > 0 {
                            bounded_push(
                                &mut self.reads,
                                (
                                    *turn,
                                    *at_ms,
                                    Read {
                                        waited_ms: duration,
                                        steered: false,
                                    },
                                ),
                            );
                        }
                    }
                }
                TurnEvent::Parked { kind, at_ms, .. } if kind == NOT_STARTED => {
                    // A refusal passes through the running state to park, without a tool.
                    if let Some(send) = self.pending.get_mut(turn)
                        && send.read == Some(*at_ms)
                    {
                        send.read = None;
                        if let Some(index) = self
                            .reads
                            .iter()
                            .rposition(|(id, read, _)| id == turn && read == at_ms)
                        {
                            self.reads.remove(index);
                        }
                    }
                }
                TurnEvent::Settled { outcome, .. } => {
                    if let Some(send) = self.pending.remove(turn)
                        && !matches!(outcome, Outcome::Completed | Outcome::Replaced { .. })
                    {
                        bounded_push(&mut self.failed, send.start.unwrap_or(send.accepted));
                    }
                }
                _ => {}
            },
            AgentEvent::JoinRead { turn, at_ms } => {
                if let Some(send) = self.pending.remove(turn) {
                    let duration = at_ms.saturating_sub(send.accepted);
                    if duration > 0 {
                        bounded_push(
                            &mut self.reads,
                            (
                                *turn,
                                *at_ms,
                                Read {
                                    waited_ms: duration,
                                    steered: true,
                                },
                            ),
                        );
                    }
                }
            }
            AgentEvent::JoinSettled { turn } => {
                if let Some(send) = self.pending.remove(turn) {
                    bounded_push(&mut self.failed, send.start.unwrap_or(send.accepted));
                }
            }
            _ => {}
        }
    }

    pub(crate) fn window(&self, since: i64, now: i64) -> Window {
        Window {
            reads: self
                .reads
                .iter()
                .filter(|(_, at, _)| (since..=now).contains(at))
                .map(|(_, _, read)| *read)
                .collect(),
            failed: self
                .failed
                .iter()
                .filter(|at| (since..=now).contains(at))
                .count(),
        }
    }
}
