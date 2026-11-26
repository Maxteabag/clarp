//! The actor's view of the extra user inputs of one running turn: those written to its tool's
//! input (Claude Code, and the agy-acp driver, which say alike when they read a message and
//! which result answers it), or the steered words of a lane's turn (Codex, Muse, OpenCode).

use std::collections::BTreeSet;
use std::path::Path;

use clarp_backend::Steers;
use clarp_kernel::agent::{Agent, Command};
use clarp_kernel::conversation::{Entry, Message, TurnEnd};
use clarp_kernel::journal::{Batch, kind};
use clarp_kernel::steering::{self, InputOwner, Join, Observation, Recovery, Stage};
use clarp_kernel::takeover;
use clarp_kernel::told;
use clarp_kernel::turn::{COMPACTION, Origin, RunOutcome, TurnId};
use clarp_spool::{Entry as Line, ShimEvent};
use serde_json::Value;

use super::{
    Action, Actor, ActorError, Facts, JoinedRead, Outputs, QueueState, Steering, conversation,
    message_at, written,
};

#[derive(Default)]
pub(super) struct State {
    owner: Option<InputOwner>,
    stream: steering::Stream,
    primary_uuid: Option<String>,
    primary_started: bool,
    /// A result of the process named the turn's own message as one it answers.
    primary_answered: bool,
    pub(super) input_skipped: bool,
    parent: Option<(RunOutcome, Facts, String)>,
    /// The process is answering another user message past its first result.
    pub(super) separate: Option<Separate>,
    /// Once a separate turn was read, the process's exit is no longer the parent's verdict.
    crossed: bool,
    /// Whether the newest of the user's messages read into the turn asked for speech, where
    /// one was: each send carries the app's audio setting when it was sent.
    pub(super) joined_speech: Option<bool>,
    /// Another sender's message read into the turn asked for speech.
    pub(super) joined_asked: bool,
    /// A message read into the turn asked for speech: its tool was told how to speak then.
    pub(super) joined_told: bool,
    pub(super) mixed: bool,
    /// The tool takes steered words on a lane: what it can take is told by its lane's lines,
    /// not a process's.
    lane: bool,
    /// A lane runs the turn and takes steered words until the turn ends.
    lane_open: bool,
    /// How the lane's turn ended, which is how the words read into it ended.
    lane_end: Option<&'static str>,
    /// The messages whose reading the replay of the spool has met: each was read once.
    replayed_reads: Vec<TurnId>,
}

pub(super) struct Sharing {
    pub(super) speak: bool,
    /// The message is the user's own: its flag is the app's audio setting now.
    pub(super) user: bool,
    pub(super) revealed: bool,
}

pub(super) struct Separate {
    pub(super) turn: TurnId,
    pub(super) message: Message,
    pub(super) closes: String,
}

impl Separate {
    pub(super) fn reply_origin(&self) -> Origin {
        if self.message.origin.is_user_send() {
            self.message.origin.clone()
        } else {
            // A trusted front-desk relay is shown as an answer to the user.
            Origin::User
        }
    }
}

impl State {
    pub(super) fn for_prompt(steers: Steers, primary_uuid: Option<String>) -> Self {
        Self {
            primary_uuid,
            lane: steers == Steers::Words,
            ..Self::default()
        }
    }

    pub(super) fn prompt_uuid(&self) -> Option<&str> {
        self.primary_uuid.as_deref()
    }

    /// Whether the process has answered the turn's own message. A resumed Claude session can
    /// first answer what its last process left behind (a `<task-notification>`), with a
    /// result of its own that names no message or another one; only a result that names the
    /// turn's message answers it. A tool whose input is not named this way has answered when
    /// it says so.
    pub(super) fn answered_its_prompt(&self) -> bool {
        self.primary_uuid.is_none() || self.primary_answered
    }

    fn observe_answer(&mut self, event: &Value) {
        let Some(uuid) = self.primary_uuid.as_deref() else {
            return;
        };
        let nested = event["parent_tool_use_id"]
            .as_str()
            .is_some_and(|id| !id.is_empty());
        if event["type"] == "result"
            && !nested
            && event["user_message_uuids"]
                .as_array()
                .is_some_and(|named| contains_uuid(named, uuid))
        {
            self.primary_answered = true;
        }
    }

    fn observe_start(&mut self, event: &Value) {
        let Some(uuid) = self.primary_uuid.as_deref() else {
            return;
        };
        if event["parent_tool_use_id"]
            .as_str()
            .is_some_and(|id| !id.is_empty())
        {
            return;
        }
        match event["type"].as_str() {
            Some("command_lifecycle") if event["command_uuid"] == uuid => {
                if event["state"] == "started" {
                    self.primary_started = true;
                } else if event["state"] == "completed" && !self.primary_started {
                    self.input_skipped = true;
                }
            }
            Some("system") if event["subtype"] == "init" => self.primary_started = true,
            Some("assistant" | "result") => self.primary_started = true,
            Some("stream_event") if event["event"]["type"] == "message_start" => {
                self.primary_started = true;
            }
            _ => {}
        }
    }

    pub(super) fn record(&mut self, entry: &Line) -> Option<Value> {
        match entry {
            Line::Shim(ShimEvent::InputOpen {
                shim_pid,
                shim_start,
                tool_pid,
                tool_start,
                group,
                projects,
            }) => {
                self.owner = Some(InputOwner {
                    shim_pid: *shim_pid,
                    shim_start: shim_start.clone(),
                    tool_pid: *tool_pid,
                    tool_start: tool_start.clone(),
                    group: *group,
                    projects: projects.clone(),
                });
            }
            // A lane says it takes steered words, which of them its tool read, and when the turn
            // ended. An exec turn, or an OpenCode turn on its own server, prints none of the
            // first two.
            Line::Out(line) if self.lane => {
                let event: Value = serde_json::from_str(line).ok()?;
                match event["type"].as_str() {
                    Some("steer.open") => self.lane_open = true,
                    Some("steer.read") => return Some(event),
                    Some(end @ ("turn.completed" | "turn.failed")) => {
                        self.lane_open = false;
                        self.lane_end = Some(if end == "turn.completed" {
                            "completed"
                        } else {
                            "failed"
                        });
                        return Some(event);
                    }
                    _ => {}
                }
            }
            Line::Out(line) if self.primary_uuid.is_some() || self.owner.is_some() => {
                let event = serde_json::from_str(line).ok()?;
                self.observe_start(&event);
                self.observe_answer(&event);
                if self.owner.is_some() {
                    return Some(event);
                }
            }
            _ => {}
        }
        None
    }

    /// The id a lane's line says its tool read, where the tool's own record confirms it
    /// (`"confirmed": true`). Such a reading is a fact about what the tool did before its turn
    /// ended, also when the lane finds it after a Stop (issue 933: Codex's own list of the turn,
    /// OpenCode's stored answer). A reading the lane only assumed, and every line of an older
    /// lane, say no such thing.
    pub(super) fn confirmed_read(&self, entry: &Line) -> Option<String> {
        let Line::Out(line) = entry else {
            return None;
        };
        if !self.lane {
            return None;
        }
        let event: Value = serde_json::from_str(line).ok()?;
        if event["type"] != "steer.read" || event["confirmed"] != true {
            return None;
        }
        event["id"]
            .as_str()
            .filter(|id| !id.is_empty())
            .map(str::to_owned)
    }

    /// Whether `entry` is a confirmed reading ([`Self::confirmed_read`]) of words that were
    /// steered into the live attempt of `into` and are still unread there. A reading of other
    /// words, or one already counted, changes nothing.
    pub(super) fn confirmed_read_into(&self, entry: &Line, agent: &Agent, into: TurnId) -> bool {
        self.confirmed_read(entry).is_some_and(|id| {
            agent.joins().values().any(|follow| {
                follow.join.into == into && follow.stage == Stage::Pending && follow.join.uuid == id
            })
        })
    }

    /// Follows a line of the spool that the actor before this one read already, to be where it
    /// was. Returns whether the tool read a message into the turn there, as the live turn saw it.
    /// The live turn counts a read where the message still waited (`Stage::Pending`) and its
    /// read was recorded: so the replay counts the first echo of each message whose read the
    /// agent holds. A turn's joins are cleared when it waits for its next attempt, so a read the
    /// agent holds is this attempt's.
    pub(super) fn replay(
        &mut self,
        event: &Value,
        agent: &Agent,
        log: &dyn clarp_ports::log::AgentLog,
        facts: Facts,
        closes: &str,
    ) -> Result<bool, ActorError> {
        match self.stream.observe(event) {
            Observation::Echo(uuid) => {
                let Some((&turn, follow)) = agent
                    .joins()
                    .iter()
                    .find(|(_, follow)| follow.join.uuid == uuid)
                else {
                    return Ok(false);
                };
                // Where the live turn read nothing, the replay reads nothing either.
                if follow.stage == Stage::Pending || self.replayed_reads.contains(&turn) {
                    return Ok(false);
                }
                self.replayed_reads.push(turn);
                let message = message_at(log, follow.message_seq)?;
                if self.parent.is_none() {
                    self.joined_told |= message.speak;
                    if message.origin.is_user_send() {
                        self.joined_speech = Some(message.speak);
                    } else {
                        self.joined_asked |= message.speak;
                    }
                } else if self.separate.is_none() {
                    self.separate(turn, message);
                }
                return Ok(true);
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
        Ok(false)
    }

    pub(super) fn observe(
        &mut self,
        event: &Value,
        batch: &mut Batch<'_>,
        facts: Facts,
        (closes, open): (&str, conversation::OpenRows<'_>),
        now_ms: i64,
        out: &mut Outputs,
    ) -> Result<Option<Sharing>, ActorError> {
        match self.stream.observe(event) {
            Observation::Echo(uuid) => {
                let Some((&turn, follow)) =
                    batch.agent().joins().iter().find(|(_, follow)| {
                        follow.join.uuid == uuid && follow.stage == Stage::Pending
                    })
                else {
                    return Ok(None);
                };
                let message = message_at(batch.log(), follow.message_seq)?;
                let into = follow.join.into;
                if batch.command(Command::JoinRead { turn }, now_ms).is_ok() {
                    queue_started(batch, out, turn, &message);
                    let (speak, user) = (message.speak, message.origin.is_user_send());
                    if self.parent.is_some() {
                        if self.separate.is_none() {
                            self.separate(turn, message);
                        }
                        return Ok(None);
                    }
                    // A later message of the user's shows the turn to the user again, after
                    // a note gave it back to the agents (rule FU).
                    let revealed = self.mixed
                        || (conversation::mark_of(batch.log(), turn.0)?.is_none()
                            && conversation::split(batch, into, open, now_ms)?);
                    self.mixed |= revealed;
                    return Ok(Some(Sharing {
                        speak,
                        user,
                        revealed,
                    }));
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
                        .unwrap_or("the tool reported an error")
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
        // A lane's turn says how it ended; a process that ended without a result did not
        // answer what it read.
        let status = self.lane_end.unwrap_or("interrupted");
        for (turn, read_at) in read {
            self.finish(batch, turn, read_at, status, now_ms)?;
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
                never_ran: None,
                replaced_by: None,
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
                note_count: 0,
                // A follow-up read into another turn's process ends with that process.
                reason: match status {
                    "completed" => None,
                    "failed" => Some("The turn this message was read into failed.".to_string()),
                    _ => Some(
                        "The process this message was read into ended before it answered."
                            .to_string(),
                    ),
                },
                // The failure was the other turn's: its kind is on that turn's end.
                kind: None,
            };
            batch.item(written(&Entry::TurnEnd(end), now_ms)?);
        }
        Ok(())
    }
}

fn contains_uuid(prompts: &[Value], uuid: &str) -> bool {
    prompts.iter().any(|prompt| prompt.as_str() == Some(uuid))
}

/// A vanished socket is insufficient evidence that the tool is done.
pub(super) fn inactive_owner(agent: &Agent) -> Result<bool, ActorError> {
    let Some(owner) = agent
        .joins()
        .values()
        .find_map(|follow| follow.join.recovery.as_ref())
    else {
        return Ok(true);
    };
    super::orphan::ended(&owner.owner).map_err(|error| {
        ActorError::Io(format!("could not fence the follow-up's old tool: {error}"))
    })
}

/// Recover input that the tool logged without a corresponding durable stdout echo, where it
/// keeps such a log (`Backend::followups_read`). Only call after the shim recorded the tool's
/// exit, or after its exact group was fenced.
pub(super) fn recover_reads(
    batch: &mut Batch<'_>,
    backend: &dyn clarp_backend::Backend,
    cwd: &Path,
    open: conversation::OpenRows<'_>,
    now_ms: i64,
    out: &mut Outputs,
) -> Result<bool, ActorError> {
    let pending: Vec<_> = batch
        .agent()
        .joins()
        .iter()
        .filter(|(_, follow)| follow.stage == Stage::Pending)
        .filter_map(|(&turn, follow)| {
            Some((
                turn,
                follow.join.into,
                follow.join.uuid.clone(),
                follow.message_seq,
                follow.join.recovery.clone()?,
            ))
        })
        .collect();
    let Some((_, _, _, _, recovery)) = pending.first() else {
        return Ok(false);
    };
    if pending.iter().any(|(_, _, _, _, other)| other != recovery) {
        return Err(ActorError::Corrupt(
            "follow-ups of the same turn name different tools".into(),
        ));
    }
    let uuids: BTreeSet<_> = pending
        .iter()
        .map(|(_, _, uuid, _, _)| uuid.clone())
        .collect();
    let Some(read) = backend.followups_read(&recovery.owner, cwd, &recovery.session, &uuids) else {
        eprintln!(
            "clarp-host: could not read the follow-up receipts {} keeps",
            backend.id()
        );
        return Ok(false);
    };
    let mut revealed = false;
    for (turn, into, uuid, message_seq, _) in pending {
        if let Some(proof) = read.get(&uuid)
            && batch
                .command(Command::JoinRead { turn }, proof.at_ms.unwrap_or(now_ms))
                .is_ok()
        {
            let message = message_at(batch.log(), message_seq)?;
            queue_started(batch, out, turn, &message);
            if proof.joined && !revealed && conversation::mark_of(batch.log(), turn.0)?.is_none() {
                revealed = conversation::split(batch, into, open, now_ms)?;
            }
            let _ = batch.command(Command::JoinSettled { turn }, now_ms);
        }
    }
    Ok(revealed)
}

pub(super) fn queue_started(batch: &Batch<'_>, out: &mut Outputs, turn: TurnId, message: &Message) {
    out.changed.queue = true;
    let agent = batch.agent();
    let (waiting, listed) = batch.queue_depths();
    let queue = QueueState {
        waiting,
        listed,
        paused: agent.is_paused(),
        revision: agent.queue_revision(),
        ..QueueState::default()
    };
    out.joined_reads.push(JoinedRead {
        turn,
        row: message.id.clone(),
        client_id: message.told_id().to_owned(),
        origin: message.origin.clone(),
        sender_agent_id: message
            .sender
            .as_ref()
            .map(|sender| sender.agent_id.clone()),
        queue,
    });
}

impl Actor {
    /// Before a message of the user's steers into the live turn, the user's older messages
    /// that still wait go in first, oldest first (issue 839): what the tool reads is in the
    /// order it was said. Stops at the first that cannot go in; the newer one then waits
    /// with it (the kernel refuses to let it jump).
    pub fn steer_older(&mut self, turn: TurnId, now_ms: i64) -> Result<Vec<Action>, ActorError> {
        let mut actions = Vec::new();
        for older in self.recorder.agent().older_words_waiting(turn) {
            let Some(message_seq) = self
                .recorder
                .agent()
                .waiting()
                .find(|waiting| waiting.id == older)
                .map(|waiting| waiting.message_seq)
            else {
                break;
            };
            #[derive(serde::Deserialize)]
            struct WaitingWords {
                #[serde(flatten)]
                message: Message,
                #[serde(default, rename = "_wait_for_turn")]
                wait_for_turn: bool,
            }
            let waiting: WaitingWords = super::read_message_at(self.recorder.log(), message_seq)?;
            if waiting.wait_for_turn {
                break;
            }
            let message = waiting.message;
            let Some(uuid) = message.trace_id.as_deref().and_then(takeover::prompt_uuid) else {
                break;
            };
            match self.steer_active(older, uuid, &message.text, now_ms)? {
                Some(action) => actions.push(action),
                None => break,
            }
        }
        Ok(actions)
    }

    /// Whether the live turn's tool takes these words on its input now, as the turn's spool
    /// says: what [`Actor::steer_active`] asks of the turn before it writes anything.
    fn takes_now(&self, uuid: &str, text: &str) -> bool {
        self.backend.steers_message(text)
            && !text.trim().is_empty()
            && self
                .live
                .as_ref()
                .is_some_and(|live| live.steering.prompt_uuid() != Some(uuid))
            && self.turn_takes_words()
    }

    /// Whether the live turn's tool takes steered words on its input now, whatever they say:
    /// what [`Actor::takes_now`] asks of the turn beside the message itself.
    fn turn_takes_words(&self) -> bool {
        let Some(live) = self.live.as_ref() else {
            return false;
        };
        let can = self.backend.steers() != Steers::No
            && live.steering.stream.can_join()
            && !live.steering.input_skipped
            && !live.opening
            && live.origin.name() != COMPACTION;
        // A lane takes steered words until its turn ends; a tool that steers on its
        // input takes them once its process and session are known.
        can && if self.backend.steers() == Steers::Words {
            live.steering.lane_open
        } else {
            live.steering.owner.is_some() && self.recorder.agent().backend_session().is_some()
        }
    }

    /// Whether a message of the owner's sent now would be read into the running turn rather
    /// than wait until it is done (`steerable` on the live turn, issue 867): its tool takes
    /// steered words now and the agent lets them in. `offered`: whether the Host offers this
    /// tool's turns steered words at all (a lane's tool only while the owner's setting runs
    /// its chat turns there). The live turn is told when the answer changes.
    pub fn tell_steerable(&mut self, offered: bool, now_ms: i64) {
        // The turn that runs is the one shown first, so that it is the one told.
        self.show_turn(now_ms);
        let steerable =
            offered && self.turn_takes_words() && self.recorder.agent().live_takes_steers();
        self.steerable = steerable;
        let events = self.shown.set_turn_steerable(steerable, now_ms);
        self.shown_events.extend(events);
    }

    /// The owner's waiting message is read into the running turn now, as a send of it would
    /// be (issue 867): the user's older words that wait go in first, oldest first, and then
    /// it. `offered`: whether the Host offers this tool's turns steered words at all, as for
    /// [`Actor::tell_steerable`]. A message that cannot go in now waits on, untouched; older
    /// words that went in before it stay read.
    pub fn steer_waiting(
        &mut self,
        turn: TurnId,
        offered: bool,
        now_ms: i64,
    ) -> Result<(super::Steered, Outputs), ActorError> {
        use super::Steered;
        let agent = self.recorder.agent();
        let Some(waiting) = agent.waiting().find(|waiting| waiting.id == turn) else {
            return Ok((Steered::NotWaiting, Outputs::default()));
        };
        if agent.joins().contains_key(&turn) {
            return Ok((Steered::NotWaiting, Outputs::default()));
        }
        if !waiting.origin.is_user_send() {
            return Ok((Steered::NotTheOwners, Outputs::default()));
        }
        // Only what was said to the agent joins a running turn: a message left for later
        // waits for a turn of its own, as the kernel holds it (`Command::Join`).
        if waiting.admission != clarp_kernel::turn::Admission::Queue {
            return Ok((Steered::LeftForLater, Outputs::default()));
        }
        let message_seq = waiting.message_seq;
        let takes = offered && agent.live_takes_steers();
        let message = message_at(self.recorder.log(), message_seq)?;
        let uuid = message
            .trace_id
            .as_deref()
            .and_then(takeover::prompt_uuid)
            .filter(|uuid| takes && self.takes_now(uuid, &message.text));
        let Some(uuid) = uuid else {
            return Ok((Steered::NotSteerable, Outputs::default()));
        };
        let mut out = Outputs::default();
        out.actions.extend(self.steer_older(turn, now_ms)?);
        let steered = match self.steer_active(turn, uuid, &message.text, now_ms)? {
            Some(action) => {
                out.actions.push(action);
                Steered::Done
            }
            None => Steered::NotSteerable,
        };
        out.changed.queue = !out.actions.is_empty();
        out.changed.status = !out.actions.is_empty();
        Ok((steered, out))
    }

    /// The turn that runs, where one does.
    pub fn live_turn(&self) -> Option<TurnId> {
        self.recorder.agent().live().map(|live| live.id())
    }

    /// Whether the live turn may be cut for a message of the owner's that takes it over
    /// ([`clarp_kernel::agent::Agent::live_cuttable`]).
    pub fn live_cuttable(&self) -> bool {
        self.recorder.agent().live_cuttable()
    }

    /// Whether the live turn was last found to take a message of the owner's now
    /// ([`Actor::tell_steerable`]).
    pub fn steerable(&self) -> bool {
        self.steerable
    }

    /// Whether the live turn's tool takes no more steered words in this turn, whatever is
    /// asked (feature `send_while_busy`): its input ended (a Claude Code or agy-acp turn that
    /// gave its result), it skipped the turn's own message, or the lane's turn is over. A turn
    /// whose process is not known yet, or a lane that has not said it takes words yet, may
    /// still take them: that is "not yet", not this. The tool's compaction is neither: a send
    /// waits for it, as it cannot be cut.
    pub fn input_ended(&self) -> bool {
        let Some(live) = self.live.as_ref() else {
            return false;
        };
        if live.origin.name() == COMPACTION {
            return false;
        }
        match self.backend.steers() {
            Steers::No => true,
            Steers::Stdin => !live.steering.stream.can_join() || live.steering.input_skipped,
            Steers::Words => live.steering.lane_end.is_some() || !live.steering.stream.can_join(),
        }
    }

    /// Whether the Host must say if it offers this tool's turns steered words for
    /// [`Actor::tell_steerable`]: only a lane's tool depends on the owner's setting, and only
    /// while its turn could take them.
    pub fn steering_needs_the_lane_setting(&self) -> bool {
        self.backend.steers() == Steers::Words && self.turn_takes_words()
    }

    /// Whether the owner's message, sent now, would be steered into the live turn rather than
    /// wait: the turn's tool takes it on its input now and the agent lets it in (issue 839).
    /// A turn that is not the owner's makes way for his message only where this is false.
    pub fn can_steer(&self, message: &Message) -> bool {
        message
            .trace_id
            .as_deref()
            .and_then(takeover::prompt_uuid)
            .is_some_and(|uuid| self.takes_now(&uuid, &message.text))
            && self.recorder.agent().live_takes_words()
    }

    /// Reserve the already accepted row before handing its bytes to the tool's stdin. A restart
    /// replays echoes but never attempts an uncertain write a second time.
    pub fn steer_active(
        &mut self,
        turn: TurnId,
        uuid: String,
        text: &str,
        now_ms: i64,
    ) -> Result<Option<Action>, ActorError> {
        if !self.takes_now(&uuid, text) {
            return Ok(None);
        }
        let Some(live) = self.live.as_ref() else {
            return Ok(None);
        };
        // A lane takes steered words; a tool that steers on its input takes the message
        // there.
        let lane = self.backend.steers() == Steers::Words;
        // A tool that keeps its own record of what it read (Claude): the process that reads
        // it, and where it says it did, for a restart. Any other (agy-acp, a lane's turn
        // that said it takes steered words): its reading is in the spool alone.
        let recovery = match (
            &live.steering.owner,
            self.recorder.agent().backend_session(),
        ) {
            (Some(owner), Some(session)) if !lane && self.backend.records_reads() => {
                Some(Recovery {
                    owner: owner.clone(),
                    session: session.to_owned(),
                })
            }
            _ => None,
        };
        // The reference steers with team instructions and the inbox only. Its coordination
        // reminder belongs to a new attempt's protocol.
        let brief = self.teams.as_ref().map(|teams| {
            let mut origin = "user".to_owned();
            if let Some(waiting) = self
                .recorder
                .agent()
                .waiting()
                .find(|waiting| waiting.id == turn)
            {
                origin = waiting.origin.name().to_owned();
            }
            teams.brief(&self.profile.agent_id, &self.profile.session, &origin)
        });
        let prompt = brief
            .as_ref()
            .map_or_else(|| text.to_owned(), |brief| brief.prompt(text));
        // A message that asks for its answer to be spoken, read into a turn whose tool was not
        // told how to speak: it is told now, as it would have been at the turn's launch, so
        // that the answer is written for the ear.
        let seq = self
            .recorder
            .agent()
            .waiting()
            .find(|waiting| waiting.id == turn);
        let seq = seq.map(|waiting| waiting.message_seq);
        let asks = match seq {
            Some(seq) => message_at(self.recorder.log(), seq)?.speak,
            None => false,
        };
        let tell = (asks && !live.told_speech)
            .then_some(self.profile.told.as_ref())
            .flatten()
            .map(|told| told::Turn {
                persona: told.persona.clone(),
                spoken: Some(told.sound),
                call: None,
            });
        let blocks = match &tell {
            Some(told) => self
                .backend
                .steered_with_speech(told, &self.profile.session, &prompt),
            None => vec![prompt],
        };
        let input = if lane {
            Steering::Words {
                id: uuid.clone(),
                text: blocks.join("\n\n"),
            }
        } else {
            let texts: Vec<&str> = blocks.iter().map(String::as_str).collect();
            let mut line = takeover::claude_user_line(&uuid, &texts).to_string();
            line.push('\n');
            Steering::Stdin(line)
        };
        let join = Join {
            into: live.turn,
            uuid,
            recovery,
            together: false,
        };
        let socket = self.profile.socket_path_of(live.turn, live.attempt);
        let mut batch = self.recorder.batch();
        if batch.command(Command::Join { turn, join }, now_ms).is_err() {
            return Ok(None);
        }
        if let Some(brief) = brief.filter(|brief| !brief.ids.is_empty()) {
            let key = clarp_kernel::journal::team_transit::followup_key(turn);
            batch.item(crate::record_item(
                kind::TEAM_PROMPT,
                key,
                &brief.ids,
                now_ms,
            )?);
        }
        batch.commit()?;
        if tell.is_some()
            && let Some(live) = self.live.as_mut()
        {
            live.told_speech = true;
        }
        Ok(Some(Action::Steer { socket, input }))
    }
}
