//! The actor's view of the extra user inputs of one Claude process.

use clarp_kernel::agent::{Agent, Command};
use clarp_kernel::conversation::{Entry, Message, TurnEnd};
use clarp_kernel::journal::Batch;
use clarp_kernel::steering::{self, Join, Observation, Stage};
use clarp_kernel::turn::{Origin, RunOutcome, TurnId};
use clarp_spool::{Entry as Line, ShimEvent};
use serde_json::Value;

use super::{
    Action, Actor, ActorError, Facts, JoinedRead, Outputs, QueueState, Waiting, message_at, written,
};

#[derive(Default)]
pub(super) struct State {
    enabled: bool,
    stream: steering::Stream,
    primary_uuid: Option<String>,
    parent: Option<(RunOutcome, Facts, String)>,
    /// The process is answering another user message past its first result.
    pub(super) separate: Option<Separate>,
    /// Once a separate turn was read, the process's exit is no longer the parent's verdict.
    crossed: bool,
    pub(super) joined_speech: bool,
}

pub(super) struct Separate {
    pub(super) turn: TurnId,
    pub(super) message: Message,
    pub(super) closes: String,
}

impl State {
    pub(super) fn record(&mut self, entry: &Line) -> Option<Value> {
        match entry {
            Line::Shim(ShimEvent::InputOpen) => self.enabled = true,
            Line::Out(line) if self.enabled => return serde_json::from_str(line).ok(),
            _ => {}
        }
        None
    }

    pub(super) fn replay(
        &mut self,
        event: &Value,
        agent: &Agent,
        log: &dyn clarp_ports::log::AgentLog,
        facts: Facts,
        closes: &str,
    ) -> Result<(), ActorError> {
        match self.stream.observe(event) {
            Observation::Echo(uuid) => {
                let follow = agent
                    .joins()
                    .iter()
                    .find(|(_, follow)| follow.join.uuid == uuid);
                match follow {
                    Some((_, follow)) if self.parent.is_none() => {
                        self.joined_speech |= message_at(log, follow.message_seq)?.speak;
                    }
                    Some((turn, follow)) if self.separate.is_none() => {
                        self.separate(*turn, message_at(log, follow.message_seq)?);
                    }
                    None if self.primary_uuid.is_none() => {
                        self.primary_uuid = Some(uuid.to_owned())
                    }
                    _ => {}
                }
            }
            Observation::Result { prompts, failed } if !prompts.is_empty() => {
                if self
                    .separate
                    .as_ref()
                    .and_then(|turn| agent.joins().get(&turn.turn))
                    .is_some_and(|follow| contains_uuid(prompts, &follow.join.uuid))
                {
                    self.separate = None;
                }
                self.remember(prompts, failed, event, facts, closes);
            }
            _ => {}
        }
        Ok(())
    }

    pub(super) fn observe(
        &mut self,
        event: &Value,
        batch: &mut Batch<'_>,
        facts: Facts,
        closes: &str,
        now_ms: i64,
        out: &mut Outputs,
    ) -> Result<Option<bool>, ActorError> {
        match self.stream.observe(event) {
            Observation::Echo(uuid) => {
                let Some((&turn, follow)) =
                    batch.agent().joins().iter().find(|(_, follow)| {
                        follow.join.uuid == uuid && follow.stage == Stage::Pending
                    })
                else {
                    if self.primary_uuid.is_none() {
                        self.primary_uuid = Some(uuid.to_owned());
                    }
                    return Ok(None);
                };
                let message = message_at(batch.log(), follow.message_seq)?;
                let Some(queued) = batch.agent().waiting().find(|queued| queued.id == turn) else {
                    return Ok(None);
                };
                let waiting =
                    Waiting::from_message(&message, turn, queued.accepted_at_ms, queued.admission);
                if batch.command(Command::JoinRead { turn }, now_ms).is_ok() {
                    out.changed.queue = true;
                    let agent = batch.agent();
                    let queue = QueueState {
                        waiting: agent.waiting().count(),
                        paused: agent.is_paused(),
                        revision: agent.queue_revision(),
                    };
                    out.joined_reads.push(JoinedRead {
                        message: waiting,
                        queue,
                    });
                    let speak = message.speak;
                    if self.parent.is_some() {
                        if self.separate.is_none() {
                            self.separate(turn, message);
                        }
                        return Ok(None);
                    }
                    return Ok(Some(speak));
                }
            }
            Observation::Result { prompts, failed } if !prompts.is_empty() => {
                let settled: Vec<_> = batch
                    .agent()
                    .joins()
                    .iter()
                    .filter_map(|(&turn, follow)| {
                        let read_at = follow.stage.read_at()?;
                        contains_uuid(prompts, &follow.join.uuid).then_some((turn, read_at))
                    })
                    .collect();
                for (turn, read_at) in settled {
                    self.finish(
                        batch,
                        turn,
                        read_at,
                        if failed { "failed" } else { "completed" },
                        now_ms,
                    )?;
                }
                self.remember(prompts, failed, event, facts, closes);
            }
            _ => {}
        }
        Ok(None)
    }

    fn separate(&mut self, turn: TurnId, message: Message) {
        self.crossed = true;
        self.separate = Some(Separate {
            turn,
            closes: message.id.clone(),
            message,
        });
    }

    fn remember(
        &mut self,
        prompts: &[Value],
        failed: bool,
        event: &Value,
        facts: Facts,
        closes: &str,
    ) {
        if self.parent.is_none()
            && self
                .primary_uuid
                .as_ref()
                .is_some_and(|uuid| contains_uuid(prompts, uuid))
        {
            let outcome = if failed {
                RunOutcome::Failed {
                    reason: event["result"]
                        .as_str()
                        .filter(|text| !text.trim().is_empty())
                        .unwrap_or("claude reported an error")
                        .to_owned(),
                }
            } else {
                RunOutcome::Completed
            };
            self.parent = Some((outcome, facts, closes.to_string()));
        }
    }

    pub(super) fn parent(&self) -> Option<&(RunOutcome, Facts, String)> {
        self.parent.as_ref().filter(|_| self.crossed)
    }

    /// An echoed follow-up cannot run again, even when the process was stopped after the echo.
    pub(super) fn finish_read(
        &mut self,
        batch: &mut Batch<'_>,
        now_ms: i64,
    ) -> Result<(), ActorError> {
        let read: Vec<_> = batch
            .agent()
            .joins()
            .iter()
            .filter_map(|(&turn, follow)| follow.stage.read_at().map(|at| (turn, at)))
            .collect();
        for (turn, read_at) in read {
            self.finish(batch, turn, read_at, "interrupted", now_ms)?;
        }
        Ok(())
    }

    fn finish(
        &mut self,
        batch: &mut Batch<'_>,
        turn: TurnId,
        read_at: i64,
        status: &str,
        now_ms: i64,
    ) -> Result<(), ActorError> {
        if batch.command(Command::JoinSettled { turn }, now_ms).is_ok()
            && self
                .separate
                .as_ref()
                .is_some_and(|separate| separate.turn == turn)
            && let Some(separate) = self.separate.take()
        {
            let end = TurnEnd {
                id: format!("t-{}", turn.0),
                turn: turn.0,
                closes: separate.closes,
                status: status.to_string(),
                started_at_ms: Some(read_at),
                ended_at_ms: now_ms,
                tool_count: 0,
                trace_id: separate.message.trace_id,
                model: None,
                tokens_in: None,
                tokens_out: None,
                context_tokens: None,
                cost_micros: None,
                cost_so_far: None,
                total: None,
                limits: None,
            };
            batch.item(written(&Entry::TurnEnd(end), now_ms)?);
        }
        Ok(())
    }
}

fn contains_uuid(prompts: &[Value], uuid: &str) -> bool {
    prompts.iter().any(|prompt| prompt.as_str() == Some(uuid))
}

impl Actor {
    /// Reserve the already accepted row before handing its bytes to Claude's stdin. A restart
    /// replays echoes but never attempts an uncertain write a second time.
    pub fn steer_active(
        &mut self,
        turn: TurnId,
        uuid: String,
        input: String,
        now_ms: i64,
    ) -> Result<Option<Outputs>, ActorError> {
        let Some(live) = self.live.as_ref() else {
            return Ok(None);
        };
        if !live.steering.enabled
            || !live.steering.stream.can_join()
            || live.opening
            || !matches!(live.origin, Origin::Agent { .. } | Origin::User)
        {
            return Ok(None);
        }
        let join = Join {
            into: live.turn,
            uuid,
        };
        let socket = self.profile.socket_path_of(live.turn, live.attempt);
        let mut batch = self.recorder.batch();
        if batch.command(Command::Join { turn, join }, now_ms).is_err() {
            return Ok(None);
        }
        batch.commit()?;
        let mut out = Outputs::default();
        out.actions.push(Action::Steer { socket, input });
        Ok(Some(out))
    }
}
