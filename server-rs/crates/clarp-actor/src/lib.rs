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
use clarp_kernel::agent::{Command, Effect};
use clarp_kernel::conversation::{Entry, Message, Reply};
use clarp_kernel::journal::{Incoming, JournalError, Recorder, kind};
use clarp_kernel::turn::{Activity, Admission, RunOutcome, Settled, Turn, TurnId};
use clarp_ports::log::{AgentLog, Kinds, LogError, Seq};
use clarp_spool::{Entry as Line, ShimEvent, SpoolReader};

pub mod conversation;

use conversation::{Draft, Page, REVISION_STEP};

/// How many lines of a spool are read, understood and written to the log at a time.
const WINDOW: usize = 2_000;

/// What an actor is told about its agent. It does not change while the actor lives.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Profile {
    pub agent_id: String,
    /// The agent's session name: what its tools know it by.
    pub session: String,
    /// The agent's own directory. Its turns' spools are in `spool/` under it.
    pub dir: PathBuf,
    /// Where the shims' sockets are. A socket's path has to be short, so not under `dir`.
    pub run_dir: PathBuf,
    /// Where the agent's tool works.
    pub cwd: PathBuf,
    pub model: Option<String>,
    pub effort: Option<String>,
    pub identity: Option<String>,
    /// How long a tool gets to end by itself when it is told to stop.
    pub stop_grace_ms: u64,
    /// Keep a turn's spool when the turn is over and in the log.
    pub keep_spools: bool,
}

impl Profile {
    pub fn spool_path(&self, turn: TurnId) -> PathBuf {
        self.dir.join("spool").join(format!("{}.jsonl", turn.0))
    }

    /// The message of a turn, where its shim reads it.
    pub fn stdin_path(&self, turn: TurnId) -> PathBuf {
        self.dir.join("spool").join(format!("{}.stdin", turn.0))
    }

    pub fn socket_path(&self, turn: TurnId) -> PathBuf {
        self.run_dir
            .join(format!("{}-{}.sock", self.agent_id, turn.0))
    }
}

/// Everything a shim needs to take a turn.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ShimOrder {
    pub turn: TurnId,
    pub spool: PathBuf,
    pub socket: PathBuf,
    pub stdin_file: PathBuf,
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
    Follow { turn: TurnId, socket: PathBuf },
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
}

#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Outputs {
    pub actions: Vec<Action>,
    pub changed: Changed,
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
}

/// The turn that holds the agent.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LiveStatus {
    pub turn: TurnId,
    /// Its tool has not said anything yet.
    pub starting: bool,
    pub activity: Activity,
    pub started_at_ms: i64,
    pub tool_count: u32,
    /// It was told to stop and has not gone yet.
    pub stopping: bool,
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
    pub head_revision: u64,
    /// The tool's own session, where the next turn continues.
    pub backend_session: Option<String>,
}

/// The turn that holds the agent, as the actor follows it.
struct LiveTurn {
    turn: TurnId,
    interpreter: Box<dyn Interpreter>,
    /// How far the turn's spool has been read. Never behind the position in the log.
    offset: u64,
    /// The replies being written, oldest first.
    drafts: Vec<Draft>,
    /// The spool has said how the tool went. Nothing more will come.
    over: bool,
}

pub struct Actor {
    profile: Profile,
    backend: Arc<dyn Backend>,
    recorder: Recorder,
    live: Option<LiveTurn>,
    /// How often something that is shown but not stored has changed since the last write to
    /// the log. It makes the revisions between two log items.
    unstored_changes: u64,
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

fn show(drafts: &mut Vec<Draft>, turn: TurnId, live: LiveReply, ts_ms: i64) {
    match drafts.iter_mut().find(|draft| draft.id == live.id) {
        Some(draft) => draft.text = live.text,
        None => drafts.push(Draft {
            id: live.id,
            turn: turn.0,
            text: live.text,
            revision: 0,
            ts_ms,
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
        let recorder = Recorder::open(log)?;
        let mut actor = Self {
            profile,
            backend,
            recorder,
            live: None,
            unstored_changes: 0,
        };
        let mut out = Outputs::default();
        let live = match actor.recorder.agent().live() {
            Some(Turn::Starting(turn)) => Some((turn.id, turn.message_seq, true)),
            Some(Turn::Running(turn)) => Some((turn.id, turn.message_seq, false)),
            _ => None,
        };
        if let Some((turn, message_seq, starting)) = live {
            actor.rejoin(turn, message_seq, starting, &mut out, now_ms)?;
        }
        Ok((actor, out))
    }

    pub fn profile(&self) -> &Profile {
        &self.profile
    }

    /// The name of the backend that runs this agent's turns.
    pub fn backend_id(&self) -> &'static str {
        self.backend.id()
    }

    /// The newest revision of the conversation.
    pub fn head_revision(&self) -> u64 {
        self.recorder.last_seq() * REVISION_STEP + self.unstored_changes.min(REVISION_STEP - 1)
    }

    pub fn status(&self) -> Status {
        let agent = self.recorder.agent();
        let stopping = agent.stopping().is_some();
        let live = match agent.live() {
            Some(Turn::Starting(turn)) => Some(LiveStatus {
                turn: turn.id,
                starting: true,
                activity: Activity::Thinking,
                started_at_ms: turn.started_at_ms,
                tool_count: 0,
                stopping,
            }),
            Some(Turn::Running(turn)) => Some(LiveStatus {
                turn: turn.id,
                starting: false,
                activity: turn.activity.clone(),
                started_at_ms: turn.started_at_ms,
                tool_count: turn.tool_count,
                stopping,
            }),
            _ => None,
        };
        Status {
            live,
            last: agent.last().cloned(),
            waiting: agent.waiting().map(|queued| queued.id).collect(),
            paused: agent.is_paused(),
            head_revision: self.head_revision(),
            backend_session: agent.backend_session().map(str::to_string),
        }
    }

    fn drafts(&self) -> &[Draft] {
        self.live
            .as_ref()
            .map_or(&[], |live| live.drafts.as_slice())
    }

    /// The newest `limit` things of the conversation, as a chat opens.
    pub fn tail(&self, limit: usize) -> Result<Page, ActorError> {
        conversation::tail(
            self.recorder.log(),
            self.drafts(),
            self.head_revision(),
            limit,
        )
    }

    /// Up to `limit` things that are newer than `revision`, as a client catches up.
    pub fn after(&self, revision: u64, limit: usize) -> Result<Page, ActorError> {
        conversation::after(
            self.recorder.log(),
            revision,
            self.drafts(),
            self.head_revision(),
            limit,
        )
    }

    /// The `limit` things before the one with this id, as a chat scrolls back. `None` when the
    /// conversation has no such thing.
    pub fn before(&self, id: &str, limit: usize) -> Result<Option<Page>, ActorError> {
        let Some(item) = self.recorder.log().find(id)? else {
            return Ok(None);
        };
        conversation::older(
            self.recorder.log(),
            item.seq,
            &[],
            self.head_revision(),
            limit,
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
        let body = serde_json::to_vec(message).map_err(|error| {
            ActorError::Corrupt(format!("a message could not be written: {error}"))
        })?;
        let accepted = self.recorder.accept_message(
            Incoming {
                key: Some(message.id.clone()),
                body,
                origin: message.origin.clone(),
                admission,
            },
            now_ms,
        )?;
        let mut out = Outputs::default();
        if accepted.duplicate {
            let sent = Sent {
                message_seq: accepted.message_seq,
                turn: None,
                queued: false,
                duplicate: true,
            };
            return Ok((sent, out));
        }
        let started = accepted.effects.iter().any(
            |effect| matches!(effect, Effect::StartTurn { turn, .. } if Some(*turn) == accepted.turn),
        );
        self.unstored_changes = 0;
        out.changed = Changed {
            conversation: true,
            status: !accepted.effects.is_empty(),
            queue: !started,
        };
        self.carry_out(accepted.effects, &mut out)?;
        let sent = Sent {
            message_seq: accepted.message_seq,
            turn: accepted.turn,
            queued: !started,
            duplicate: false,
        };
        Ok((sent, out))
    }

    /// Ends the live turn and holds the mailbox. Returns how many turns were told to end.
    pub fn stop(&mut self, by: &str, now_ms: i64) -> Result<(u32, Outputs), ActorError> {
        let command = Command::Stop { by: by.to_string() };
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
            },
            ..Outputs::default()
        };
        self.carry_out(effects, &mut out)?;
        Ok((u32::try_from(told).unwrap_or(u32::MAX), out))
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
            },
            ..Outputs::default()
        };
        self.carry_out(effects, &mut out)?;
        Ok((true, out))
    }

    /// The shim for this turn could not be started.
    pub fn launch_failed(
        &mut self,
        turn: TurnId,
        reason: &str,
        now_ms: i64,
    ) -> Result<Outputs, ActorError> {
        let command = Command::StartFailed {
            turn,
            reason: reason.to_string(),
        };
        self.ended_outside_the_spool(turn, command, now_ms)
    }

    /// Nothing answers at this turn's shim any more.
    pub fn shim_gone(&mut self, turn: TurnId, now_ms: i64) -> Result<Outputs, ActorError> {
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
        // What the agent had written and nobody stored is kept: the user saw it.
        let unfinished = self.drafts().to_vec();
        let mut batch = self.recorder.batch();
        for draft in unfinished {
            let piece = Entry::Reply(Reply {
                id: draft.id,
                turn: draft.turn,
                text: draft.text,
            });
            let item = piece.to_item(now_ms).map_err(|error| {
                ActorError::Corrupt(format!("a reply could not be written: {error}"))
            })?;
            batch.item(item);
        }
        let _ = batch.command(command, now_ms);
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
                    self.start(turn, message_seq, out)?;
                }
                Effect::StopTurn { turn } => out.actions.push(Action::Stop {
                    turn,
                    socket: self.profile.socket_path(turn),
                    grace_ms: self.profile.stop_grace_ms,
                }),
            }
        }
        Ok(())
    }

    /// Hands a turn to a shim: the message goes to disk, and the world is asked to launch.
    fn start(
        &mut self,
        turn: TurnId,
        message_seq: Seq,
        out: &mut Outputs,
    ) -> Result<(), ActorError> {
        let stored = self
            .recorder
            .log()
            .read_after(message_seq.saturating_sub(1), Kinds::only(kind::MESSAGE), 1)?
            .into_iter()
            .next()
            .filter(|item| item.seq == message_seq)
            .ok_or_else(|| {
                ActorError::Corrupt(format!(
                    "turn {} answers the message at {message_seq}, and the log has none there",
                    turn.0
                ))
            })?;
        let message: Message = serde_json::from_slice(&stored.body).map_err(|error| {
            ActorError::Corrupt(format!(
                "the message at {message_seq} cannot be read: {error}"
            ))
        })?;
        let launch = self.backend.launch(&TurnSpec {
            agent_id: self.profile.agent_id.clone(),
            session: self.profile.session.clone(),
            turn: turn.0,
            cwd: self.profile.cwd.clone(),
            prompt: message.text,
            backend_session: self.recorder.agent().backend_session().map(str::to_string),
            model: self.profile.model.clone(),
            effort: self.profile.effort.clone(),
            identity: self.profile.identity.clone(),
        });
        let stdin_file = self.profile.stdin_path(turn);
        write_whole(&stdin_file, &launch.stdin)?;
        self.live = Some(LiveTurn {
            turn,
            interpreter: self.backend.interpreter(turn.0),
            offset: 0,
            drafts: Vec::new(),
            over: false,
        });
        out.actions.push(Action::Launch(ShimOrder {
            turn,
            spool: self.profile.spool_path(turn),
            socket: self.profile.socket_path(turn),
            stdin_file,
            cwd: self.profile.cwd.clone(),
            program: launch.program,
            args: launch.args,
            env: launch.env,
        }));
        Ok(())
    }

    /// Takes up a turn that was live when the actor was last closed.
    fn rejoin(
        &mut self,
        turn: TurnId,
        message_seq: Seq,
        starting: bool,
        out: &mut Outputs,
        now_ms: i64,
    ) -> Result<(), ActorError> {
        let spool = self.profile.spool_path(turn);
        let reader = SpoolReader::new(&spool);
        if !reader.exists() {
            if starting {
                // The turn was decided and no shim ever took it. Nothing ran: start it now.
                return self.start(turn, message_seq, out);
            }
            let command = Command::TurnEnded {
                turn,
                outcome: RunOutcome::Interrupted {
                    reason: "the record of the turn's tool is gone".to_string(),
                },
            };
            let effects = self.recorder.handle(command, now_ms)?;
            out.changed.status = true;
            return self.carry_out(effects, out);
        }
        // A shim took this turn. Read its spool up to the position in the log, only to be where
        // the actor before this one was: what those lines meant is in the log already.
        let until = self.recorder.spool_offset(turn);
        let mut interpreter = self.backend.interpreter(turn.0);
        let mut drafts = Vec::new();
        let mut signals = Vec::new();
        let mut offset = 0;
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
                        Signal::Live(live) => show(&mut drafts, turn, live, record.t),
                        Signal::Reply(reply) => drafts.retain(|draft| draft.id != reply.id),
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
        }
        self.live = Some(LiveTurn {
            turn,
            interpreter,
            offset,
            drafts,
            over: false,
        });
        out.changed.conversation = true;
        out.actions.push(Action::Follow {
            turn,
            socket: self.profile.socket_path(turn),
        });
        if self.recorder.agent().stopping().is_some() {
            // The stop was decided; whether the shim ever heard it is not known. Telling it
            // twice does no harm.
            out.actions.push(Action::Stop {
                turn,
                socket: self.profile.socket_path(turn),
                grace_ms: self.profile.stop_grace_ms,
            });
        }
        // What the tool wrote while nobody was reading.
        self.pump(out, now_ms)
    }

    /// The live turn is over, in the log: its files have done their work.
    fn forget_live(&mut self) {
        if let Some(live) = self.live.take() {
            let _ = std::fs::remove_file(self.profile.stdin_path(live.turn));
            if !self.profile.keep_spools {
                let _ = std::fs::remove_file(self.profile.spool_path(live.turn));
            }
        }
    }

    /// Reads what is new in the live turn's spool and writes what it means to the log.
    fn pump(&mut self, out: &mut Outputs, now_ms: i64) -> Result<(), ActorError> {
        loop {
            let Some(live) = self.live.as_mut().filter(|live| !live.over) else {
                return Ok(());
            };
            let turn = live.turn;
            let spool = self.profile.spool_path(turn);
            let read = SpoolReader::new(&spool)
                .read_window(live.offset, u64::MAX, WINDOW)
                .map_err(|error| io_error(&spool, error))?;
            if read.records.is_empty() && read.damaged.is_empty() {
                return Ok(());
            }
            out.damaged_lines += read.damaged.len();

            let mut batch = self.recorder.batch();
            let mut signals = Vec::new();
            let mut shown: Vec<String> = Vec::new();
            for record in &read.records {
                let t = record.t;
                // What the shim says about the process means the same for every backend. A
                // command the agent refuses is about a turn that is no longer live: late news.
                match &record.entry {
                    Line::Shim(ShimEvent::Spawned { .. }) => {
                        let _ = batch.command(Command::TurnStarted { turn }, t);
                    }
                    Line::Shim(ShimEvent::SpawnFailed { error }) => {
                        let reason = error.clone();
                        let _ = batch.command(Command::StartFailed { turn, reason }, t);
                        live.over = true;
                    }
                    Line::Shim(ShimEvent::Exited { .. }) => live.over = true,
                    _ => {}
                }
                live.interpreter.on_record(record, &mut signals);
                for signal in signals.drain(..) {
                    let piece = match signal {
                        Signal::Bound { backend_session } => {
                            let command = Command::TurnBound {
                                turn,
                                backend_session,
                            };
                            let _ = batch.command(command, t);
                            continue;
                        }
                        Signal::Moved(activity) => {
                            let _ = batch.command(Command::TurnMoved { turn, activity }, t);
                            continue;
                        }
                        Signal::Ended(outcome) => {
                            let _ = batch.command(Command::TurnEnded { turn, outcome }, t);
                            continue;
                        }
                        Signal::Live(reply) => {
                            if !shown.contains(&reply.id) {
                                shown.push(reply.id.clone());
                            }
                            show(&mut live.drafts, turn, reply, t);
                            continue;
                        }
                        Signal::Reply(reply) => {
                            // The finished reply takes the place of the one being written.
                            live.drafts.retain(|draft| draft.id != reply.id);
                            Entry::Reply(reply)
                        }
                        Signal::Tool(call) => Entry::Tool(call),
                        Signal::Notice(notice) => Entry::Notice(notice),
                    };
                    let item = piece.to_item(t).map_err(|error| {
                        ActorError::Corrupt(format!(
                            "a piece of a turn could not be written: {error}"
                        ))
                    })?;
                    batch.item(item);
                }
            }
            if live.over && batch.agent().live().map(Turn::id) == Some(turn) {
                // The process is gone and its backend did not say how the turn went.
                let outcome = RunOutcome::Interrupted {
                    reason: "the tool ended and nothing said how".to_string(),
                };
                let _ = batch.command(Command::TurnEnded { turn, outcome }, now_ms);
            }
            let wrote = !batch.is_empty();
            if wrote {
                batch.cursor(turn, read.next_offset, now_ms);
            }
            let committed = batch.commit()?;
            live.offset = read.next_offset;
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
                    out.changed.conversation = true;
                }
            }
            if live.over {
                self.forget_live();
            }
            self.carry_out(committed.effects, out)?;
        }
    }
}
