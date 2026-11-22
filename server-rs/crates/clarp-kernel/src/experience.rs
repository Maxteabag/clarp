//! A bounded view of the user's send history, folded from the actor journal.

use std::collections::{BTreeMap, VecDeque};

use crate::agent::AgentEvent;
use crate::artifacts::forms::is_answer_delivery;
use crate::failures::NOT_STARTED;
use crate::turn::{Activity, Origin, Outcome, TurnEvent, TurnId};

const MOST: usize = 16_384;

/// A wait for a fresh word from the user, including one the Oracle carried.
pub fn is_user_wait(origin: &Origin, client_id: &str, has_sender: bool) -> bool {
    origin.is_user_send() && !has_sender && !is_answer_delivery(client_id)
}

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
    wait_message: Option<u64>,
    count_failure: bool,
}

#[derive(Default)]
pub(crate) struct History {
    pending: BTreeMap<TurnId, Send>,
    reads: VecDeque<(TurnId, i64, u64, Read)>,
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
                    origin,
                    at_ms,
                    message_seq,
                    coordination,
                    ..
                } if origin.is_user_send() => {
                    self.pending.insert(
                        *turn,
                        Send {
                            accepted: *at_ms,
                            start: None,
                            read: None,
                            wait_message: coordination.is_none().then_some(*message_seq),
                            count_failure: origin.is_user(),
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
                        if let Some(message) = send.wait_message
                            && send.start.is_some_and(|start| start > send.accepted)
                            && duration > 0
                        {
                            bounded_push(
                                &mut self.reads,
                                (
                                    *turn,
                                    *at_ms,
                                    message,
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
                            .rposition(|(id, read, _, _)| id == turn && read == at_ms)
                        {
                            self.reads.remove(index);
                        }
                    }
                }
                TurnEvent::Settled { outcome, .. } => {
                    if let Some(send) = self.pending.remove(turn)
                        && send.count_failure
                        && !matches!(outcome, Outcome::Completed | Outcome::Replaced { .. })
                    {
                        bounded_push(&mut self.failed, send.start.unwrap_or(send.accepted));
                    }
                }
                _ => {}
            },
            AgentEvent::JoinRead { turn, at_ms } => {
                if let Some(send) = self.pending.remove(turn)
                    && let Some(message) = send.wait_message
                {
                    let duration = at_ms.saturating_sub(send.accepted);
                    if duration > 0 {
                        bounded_push(
                            &mut self.reads,
                            (
                                *turn,
                                *at_ms,
                                message,
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
                if let Some(send) = self.pending.remove(turn)
                    && send.count_failure
                {
                    bounded_push(&mut self.failed, send.start.unwrap_or(send.accepted));
                }
            }
            _ => {}
        }
    }

    pub(crate) fn window<E>(
        &self,
        since: i64,
        now: i64,
        mut from_user: impl FnMut(u64) -> Result<bool, E>,
    ) -> Result<Window, E> {
        let within = |at: i64| (since..=now).contains(&at);
        let mut reads = Vec::new();
        for &(_, at, message, read) in &self.reads {
            if within(at) && from_user(message)? {
                reads.push(read);
            }
        }
        Ok(Window {
            reads,
            failed: self.failed.iter().filter(|&&at| within(at)).count(),
        })
    }
}
