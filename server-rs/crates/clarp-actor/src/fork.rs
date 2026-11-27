//! The agent's conversation fork (issue 718).
//!
//! In Conversation view, while the agent's own session is working, a message of the user's
//! need not wait for it. The Host copies the tool's session (Claude
//! Code's `--fork-session`, which keeps the context the provider has cached) and that copy,
//! the fork, answers the user at once in the same chat, as the agent. Further messages of the
//! user's go to the same fork. The agent's own session's next turn opens with a short note of
//! what the user said and what the fork answered and did; the fork's session is then closed
//! and never used again.
//!
//! The kernel knows a message the fork takes as a follow-up reserved for another process of
//! the agent (`Command::Join`, as a steered message is): it waits in the queue, no turn of the
//! agent's own takes it, and the agent's own turn gives it back when it ends. The fork reads
//! it (`JoinRead`, which writes it into the conversation) only once its tool has begun to
//! answer. A fork that cannot start, or fails before that, leaves the message where it was:
//! it runs as it would have without a fork. Nothing is lost and nothing runs twice.
//!
//! What the fork is, is one record of the log (`kind::FORK`, keyed [`kind::FORK_LANE`]),
//! written in the same append as what the fork said and how far its spool was read, so a
//! Host that starts again follows a fork that ran on.

use std::collections::HashMap;
use std::path::PathBuf;

use clarp_backend::{Interpreter, Signal, TurnSpec};
use clarp_kernel::agent::Command;
use clarp_kernel::conversation::{Entry, Message, ToolCall, ToolStatus, TurnEnd};
use clarp_kernel::failures;
use clarp_kernel::journal::{Batch, kind};
use clarp_kernel::spoken::lines_of;
use clarp_kernel::steering::{Join, Stage as JoinStage};
use clarp_kernel::tools::summarize;
use clarp_kernel::turn::{Activity, RunOutcome, Turn, TurnId};
use clarp_spool::{Entry as Line, ShimEvent, SpoolReader};
use serde::{Deserialize, Serialize};
use serde_json::json;

use super::{
    Action, Actor, ActorError, Outputs, ShimOrder, WINDOW, attempt_of, conversation, ending_notice,
    follow, io_error, kept, record_item, said, steering, turn_name, write_whole, written,
};

/// What a fork's tool is started with in its environment. The Host's own tools refuse, when
/// they see it, what a fork must not start: helpers, background jobs, new agents.
pub const FORK_ENV: &str = "CLARP_FORK";

/// The attempt a fork's turn runs as. Its spool, input and socket are never those of the
/// turn's own attempts, which its message runs as when the fork gives it back.
pub const FORK_ATTEMPT: u32 = 1_001;

/// How much of what was said a note keeps of each thing.
const SHORT: usize = 600;

/// What a fork's turn is told after the user's words. After them, so that everything before
/// is what the agent's own session has cached.
pub const BRIEF: &str = "<system-reminder>\nYou are a short-lived fork of your own session. \
Your main session is busy with a turn; the user wrote to you meanwhile, and \
you answer them now, here, as yourself. Keep it short. You may read files and state, tell the \
user with clarp-admin tell-user, and relay work to other agents with clarp-admin prompt. The \
Host refuses you file edits, helpers, background jobs and new agents. Do not start builds, \
installs or other long work either. If something needs them, say so: your main session is told \
what was said and done here before its next turn, and takes it up then.\n</system-reminder>";

/// The tools of Claude Code that change files. A fork's are refused by a hook of its own: two
/// sessions editing one working tree is what a fork must never do. A hook, and not a list of
/// tools the tool is not given (`--disallowedTools`): that list changes the request, and the
/// fork would read its whole conversation again without the provider's cache, the opposite of
/// answering at once (`skills/clarp-status-fork`).
pub const EDIT_TOOLS: &str = "Edit|Write|MultiEdit|NotebookEdit";

/// The settings a fork's tool is started with on top of the agent's own: a hook that refuses
/// every tool of [`EDIT_TOOLS`] (exit 2 tells Claude Code why, and the tool does not run). The
/// Host merges its own hooks into the same settings.
pub fn edit_guard() -> serde_json::Value {
    json!({"hooks": {"PreToolUse": [{
        "matcher": EDIT_TOOLS,
        "hooks": [{
            "type": "command",
            "command": "echo 'A conversation fork may not edit files. Say what is to change: the main session is told.' >&2; exit 2",
            "timeout": 3,
        }],
    }]}})
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
enum Stage {
    /// Reserved for the fork; it waits for the fork's turn before it to end.
    Waiting,
    /// The fork's tool runs for it.
    Running,
    /// The fork answered it, or began to: it is in the conversation, and in the note.
    Answered,
    /// The fork gave it back unread: it is the agent's own again.
    GivenBack,
}

/// One message of the user's that the fork took.
#[derive(Debug, Clone, Serialize, Deserialize)]
struct Asked {
    turn: u64,
    message_seq: u64,
    stage: Stage,
    /// The fork's tool has begun to answer it: the message is in the conversation.
    #[serde(default)]
    read: bool,
    /// How far the spool of its turn is in the log.
    #[serde(default)]
    offset: u64,
    /// The row that closes its turn so far.
    #[serde(default)]
    last_row: String,
    /// What the fork's turn has said that the work after it decides on, by the rules of the
    /// agent's own turn. Its fields sit in this record as they always did.
    #[serde(flatten)]
    open: said::Unfollowed,
    /// For the note: what the user said, what the fork answered last, what it did.
    #[serde(default)]
    said: String,
    #[serde(default)]
    replied: String,
    #[serde(default)]
    did: Vec<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    failed: Option<String>,
    /// The user stopped the fork's turn for it.
    #[serde(default)]
    stopped: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
enum State {
    Open,
    /// Given to the agent's own session at the start of this turn of its, whose tool has not
    /// started yet: a turn that never runs passes the note on to the next.
    Offered {
        into: u64,
    },
    /// Told to the agent's own session: the tool of this turn of its started with it.
    Merged {
        into: u64,
    },
    /// Nothing came of it that the agent's own session needs to hear.
    Closed,
}

/// The fork as the log has it.
#[derive(Debug, Clone, Serialize, Deserialize)]
struct Record {
    /// The agent's own turn it was forked beside.
    from: u64,
    /// The fork's own session with its tool.
    session: String,
    /// The fork's session exists: its next turn takes it up instead of forking again.
    #[serde(default)]
    begun: bool,
    /// One of its turns failed before it read its message: it takes no more.
    #[serde(default)]
    broken: bool,
    asked: Vec<Asked>,
    state: State,
}

/// The turn of the fork that runs, as the actor reads it.
struct Running {
    turn: TurnId,
    interpreter: Box<dyn Interpreter>,
    message: Message,
    /// The calls of its tool that have not returned yet.
    calls: Vec<ToolCall>,
    first_written: HashMap<String, i64>,
    started_at_ms: i64,
    outcome: Option<RunOutcome>,
    over: bool,
    /// The user stopped it (`POST /stop`): however its tool ends, it was stopped.
    stopped: bool,
}

/// What the actor holds of its fork.
#[derive(Default)]
pub(super) struct Lane {
    record: Option<Record>,
    running: Option<Running>,
    /// How the fork's last turn ended, for what is shown of it: the status of its end row and
    /// the reason of a failure.
    ended: Option<(TurnId, &'static str, Option<String>)>,
    /// What is shown of the fork's turn while it runs ([`Shown`]).
    shown: Option<Shown>,
    /// Events that go out whatever is shown: those of the moment what is shown changed.
    ready: Vec<serde_json::Value>,
    /// The agent's own turn whose launch waits: a turn of the user's for the fork's turn to
    /// end, and any turn for the process of the turn before it to be gone
    /// ([`super::lingering`]). Its number, its message and its attempt. Its tool is started
    /// then, with the fork's note where there is one.
    deferred: Option<(TurnId, u64, u32)>,
}

/// The fork's turn as those who watch the agent see it (`GET /live` and its events), in a hub
/// of its own: the agent's own turn goes on in its hub meanwhile, and is told beside the
/// fork's as `main_turn`. Neither turn is ended for the other. When one hub takes over from
/// the other, its events are numbered past the other's, so that a client asks for the
/// snapshot instead of mixing the two.
struct Shown {
    turn: TurnId,
    name: String,
    hub: clarp_kernel::live::Hub,
    /// Its events that nobody has taken yet.
    events: Vec<serde_json::Value>,
    /// The `main_turn` last told, `Null` once told that none runs.
    told: Option<serde_json::Value>,
}

/// The operation that tells the agent's own turn beside the fork's (clarp-ios#139): a client
/// that knows it draws it beside the turn; the reducer leaves it alone.
pub const MAIN_TURN_OP: &str = "main_turn";

impl Lane {
    /// The tool of the agent's own turn `turn` started: a note offered to it is told now, and
    /// the fork is over. The item goes into the append that says the turn started.
    pub(super) fn ran(
        &mut self,
        turn: TurnId,
        t: i64,
    ) -> Result<Option<clarp_ports::log::NewItem>, ActorError> {
        let Some(record) = self
            .record
            .as_mut()
            .filter(|record| record.state == (State::Offered { into: turn.0 }))
        else {
            return Ok(None);
        };
        record.state = State::Merged { into: turn.0 };
        lane_item(record, t).map(Some)
    }
}

fn shortened(text: &str) -> String {
    let text = text.trim();
    if text.chars().count() <= SHORT {
        return text.to_owned();
    }
    let cut: String = text.chars().take(SHORT).collect();
    format!("{cut}…")
}

/// The note that opens the agent's own next turn: what the fork was told, said and did.
fn note(record: &Record) -> Option<String> {
    let told: Vec<String> = record
        .asked
        .iter()
        .filter(|asked| asked.stage == Stage::Answered)
        .map(|asked| {
            let mut line = format!("- The user said: \"{}\"", asked.said);
            if !asked.replied.is_empty() {
                line.push_str(&format!("\n  The fork replied: \"{}\"", asked.replied));
            }
            if !asked.did.is_empty() {
                line.push_str(&format!("\n  It did: {}", asked.did.join("; ")));
            }
            if asked.stopped {
                line.push_str(if asked.replied.is_empty() && asked.did.is_empty() {
                    "\n  The user stopped it before the fork answered."
                } else {
                    "\n  The user stopped the fork while it answered."
                });
            } else if let Some(why) = &asked.failed {
                line.push_str(&format!(
                    "\n  It could not finish ({why}): this message is yours to answer."
                ));
            }
            line
        })
        .collect();
    if told.is_empty() {
        return None;
    }
    Some(format!(
        "<system-reminder>\nWhile you were busy with a turn, a short-lived fork \
of this session answered the user in this chat, as you. What it said and did is yours: do not \
answer it again unless something is left to do.\n{}\n</system-reminder>",
        told.join("\n")
    ))
}

fn lane_item(record: &Record, now_ms: i64) -> Result<clarp_ports::log::NewItem, ActorError> {
    record_item(kind::FORK, kind::FORK_LANE.to_string(), record, now_ms)
}

/// The fork has begun to answer its message: the message enters the conversation now, in the
/// append that holds the first thing the fork said. `false`: the agent's own turn has taken
/// the message back, and nothing of the fork's is written.
fn read_now(
    batch: &mut Batch<'_>,
    asked: &mut Asked,
    (message, session): (&Message, &str),
    out: &mut Outputs,
    t: i64,
) -> Result<bool, ActorError> {
    if asked.read {
        return Ok(true);
    }
    let turn = TurnId(asked.turn);
    if batch.command(Command::JoinRead { turn }, t).is_err() {
        return Ok(false);
    }
    asked.read = true;
    asked.last_row = message.id.clone();
    // Which session the turn's rows come from, for whoever reads them, for good.
    batch.item(record_item(kind::FORK, turn_key(turn.0), &session, t)?);
    steering::queue_started(batch, out, turn, message);
    Ok(true)
}

/// The key under which the session of a turn the fork answered is kept.
pub(crate) fn turn_key(turn: u64) -> String {
    format!("fork-turn-{turn}")
}

impl Actor {
    /// The session each row of a page comes from: what a reply says of itself, the fork's for
    /// a turn the fork answered, and else the session the agent's turn ran in. One look by
    /// key for each turn of the page.
    pub(super) fn with_sessions(
        &self,
        mut page: conversation::Page,
    ) -> Result<conversation::Page, ActorError> {
        let mut forked: HashMap<u64, Option<String>> = HashMap::new();
        for row in &mut page.entries {
            if let Entry::Reply(reply) = &row.entry
                && let Some(session) = &reply.backend_session
            {
                row.session = Some(session.clone());
                continue;
            }
            let Some(turn) = row.turn_number else {
                continue;
            };
            // A turn the fork answered has its key only where the agent ever had a fork: its
            // record is written before any such key and never taken away. An agent without
            // one is asked nothing.
            if let std::collections::hash_map::Entry::Vacant(slot) = forked.entry(turn) {
                let session = match self.fork.record.as_ref() {
                    Some(_) => self.recorder.log().find(&turn_key(turn))?,
                    None => None,
                };
                let session =
                    session.and_then(|item| serde_json::from_slice::<String>(&item.body).ok());
                slot.insert(session);
            }
            row.session = match &forked[&turn] {
                Some(session) => Some(session.clone()),
                None => self.recorder.turn_session(turn).map(str::to_owned),
            };
        }
        Ok(page)
    }

    fn fork_socket(&self, turn: TurnId) -> PathBuf {
        self.profile.socket_path_of(turn, FORK_ATTEMPT)
    }

    /// Whether this attempt of this turn is the fork's that runs.
    pub(super) fn fork_follows(&self, turn: TurnId, attempt: u32) -> bool {
        attempt == FORK_ATTEMPT
            && self
                .fork
                .running
                .as_ref()
                .is_some_and(|running| running.turn == turn)
    }

    /// The trace of the fork's turn that runs, when it is `turn`: its tools may leave the
    /// user a note as any turn's may.
    pub(super) fn fork_trace(&self, turn: u64) -> Option<String> {
        let running = self.fork.running.as_ref()?;
        (running.turn.0 == turn).then(|| running.message.trace_id.clone().unwrap_or_default())
    }

    /// The agent's own turn beside which a fork could answer the user now: one whose
    /// Claude Code tool is running, has a session to copy, and has a usable fork.
    fn fork_beside(&self) -> Option<TurnId> {
        if self.backend.id() != "claude" {
            return None;
        }
        let agent = self.recorder.agent();
        let live = agent.live().filter(|live| live.has_process())?;
        if agent.stopping().is_some() || agent.backend_session().is_none() {
            return None;
        }
        let broken = self.fork.record.as_ref().is_some_and(|record| {
            record.broken && matches!(record.state, State::Open | State::Offered { .. })
        });
        (!broken).then_some(live.id())
    }

    /// Whether a fork can answer this message: words, and not said in a call.
    pub fn fork_answers(message: &Message) -> bool {
        !message.in_call() && !message.text.trim().is_empty()
    }

    /// Whether a message of the user's that waits is taken by the agent's fork, which answers
    /// beside the agent's running Claude Code turn. The Host decides which agents and
    /// chat views use this route. `false`: nothing changed, and the message is
    /// what it would have been without a fork.
    pub fn fork_take(&mut self, turn: TurnId, now_ms: i64) -> Result<(bool, Outputs), ActorError> {
        let no = || Ok((false, Outputs::default()));
        let Some(main) = self.fork_beside() else {
            return no();
        };
        let Some(message_seq) = self
            .recorder
            .agent()
            .waiting()
            .find(|waiting| waiting.id == turn)
            .map(|waiting| waiting.message_seq)
        else {
            return no();
        };
        let message = self.message_at(message_seq)?;
        if !Self::fork_answers(&message) {
            return no();
        }
        let mut record = match self.fork.record.clone() {
            // A note not yet told (offered to a turn whose tool has not started) stays with
            // the fork, which takes the user's words again; it is offered anew.
            Some(mut record) if matches!(record.state, State::Open | State::Offered { .. }) => {
                record.state = State::Open;
                record
            }
            _ => {
                let name = format!(
                    "fork:{}:{}:{}:{}",
                    self.profile.agent_id, self.profile.conversation_id, main.0, turn.0
                );
                let Some(session) = clarp_kernel::takeover::prompt_uuid(&name) else {
                    return no();
                };
                Record {
                    from: main.0,
                    session,
                    begun: false,
                    broken: false,
                    asked: Vec::new(),
                    state: State::Open,
                }
            }
        };
        if record.broken {
            return no();
        }
        let uuid = clarp_kernel::takeover::attempt_prompt_uuid(
            &self.profile.agent_id,
            turn.0,
            message.trace_id.as_deref(),
            FORK_ATTEMPT,
        );
        let join = Join {
            into: main,
            uuid,
            recovery: None,
            together: false,
        };
        record.asked.push(Asked {
            turn: turn.0,
            message_seq,
            stage: Stage::Waiting,
            read: false,
            offset: 0,
            last_row: String::new(),
            open: said::Unfollowed::default(),
            said: shortened(&message.text),
            replied: String::new(),
            did: Vec::new(),
            failed: None,
            stopped: false,
        });
        let mut batch = self.recorder.batch();
        if batch.command(Command::Join { turn, join }, now_ms).is_err() {
            return no();
        }
        batch.item(lane_item(&record, now_ms)?);
        batch.commit()?;
        self.fork.record = Some(record);
        let mut out = Outputs::default();
        self.fork_next(&mut out, now_ms)?;
        Ok((true, out))
    }

    /// Starts the fork's next turn, when it has a message waiting and nothing runs.
    fn fork_next(&mut self, out: &mut Outputs, now_ms: i64) -> Result<(), ActorError> {
        if self.fork.running.is_some() {
            return Ok(());
        }
        let Some(mut record) = self
            .fork
            .record
            .clone()
            .filter(|record| record.state == State::Open && !record.broken)
        else {
            return Ok(());
        };
        let joins = self.recorder.agent().joins();
        let Some(index) = record.asked.iter().position(|asked| {
            asked.stage == Stage::Waiting
                && joins
                    .get(&TurnId(asked.turn))
                    .is_some_and(|join| join.stage == JoinStage::Pending)
        }) else {
            return Ok(());
        };
        let turn = TurnId(record.asked[index].turn);
        let message = self.message_at(record.asked[index].message_seq)?;
        // The first turn copies the agent's own session; the next take up the copy.
        let resumed = if record.begun {
            Some(record.session.clone())
        } else {
            self.recorder.agent().backend_session().map(str::to_owned)
        };
        let told = self.profile.told.as_ref();
        // The agent's own model and effort: what its session's cache was made with.
        let preferences = self.launch_preferences(&message);
        let mut launch = self.backend.launch(&TurnSpec {
            agent_id: self.profile.agent_id.clone(),
            session: self.profile.session.clone(),
            turn: turn.0,
            attempt: FORK_ATTEMPT,
            trace_id: message.trace_id.clone(),
            takeover: None,
            cwd: self.profile.cwd.clone(),
            prompt: format!("{}\n\n{BRIEF}", message.text),
            backend_session: resumed,
            new_session: None,
            model: preferences.model.clone(),
            effort: preferences.effort.clone(),
            identity: told.and_then(|told| told.identity.clone()),
            told: told.map(|told| clarp_kernel::told::Turn {
                persona: told.persona.clone(),
                spoken: message.speak.then_some(told.sound),
                call: None,
                text_ack: self.text_turn_ack(&message),
            }),
        });
        if !record.begun {
            launch.args.extend([
                "--fork-session".to_owned(),
                "--session-id".to_owned(),
                record.session.clone(),
            ]);
        }
        launch
            .args
            .extend(["--settings".to_owned(), edit_guard().to_string()]);
        let stdin_file = self.profile.stdin_path_of(turn, FORK_ATTEMPT);
        write_whole(&stdin_file, &launch.stdin)?;
        record.asked[index].stage = Stage::Running;
        let mut batch = self.recorder.batch();
        batch.item(lane_item(&record, now_ms)?);
        batch.commit()?;
        self.fork.record = Some(record);
        let name = attempt_of(turn_name(&message.id), FORK_ATTEMPT);
        self.fork.running = Some(Running {
            turn,
            interpreter: self.backend.interpreter(turn.0, &name),
            message,
            calls: Vec::new(),
            first_written: HashMap::new(),
            started_at_ms: now_ms,
            outcome: None,
            over: false,
            stopped: false,
        });
        let env = launch
            .env
            .into_iter()
            .chain(self.profile.tool_env.iter().cloned())
            .chain([(FORK_ENV.to_owned(), "1".to_owned())])
            .collect();
        out.actions.push(Action::Fork(Box::new(ShimOrder {
            before_stop: launch.before_stop,
            opening: None,
            turn,
            attempt: FORK_ATTEMPT,
            spool: self.profile.spool_path_of(turn, FORK_ATTEMPT),
            socket: self.fork_socket(turn),
            stdin_file,
            prompt_as_file: launch.prompt_as_file,
            streams_input: false,
            named_session: None,
            model: preferences.model,
            effort: preferences.effort,
            cwd: self.profile.cwd.clone(),
            program: launch.program,
            args: launch.args,
            env,
        })));
        out.changed.status = true;
        Ok(())
    }

    /// Reads on in the spool of the fork's turn that runs.
    pub(super) fn pump_fork(&mut self, out: &mut Outputs, now_ms: i64) -> Result<(), ActorError> {
        loop {
            self.fork_show(now_ms);
            let Some(running) = self.fork.running.as_ref() else {
                return Ok(());
            };
            let turn = running.turn;
            let Some(mut record) = self.fork.record.clone() else {
                self.fork.running = None;
                return Ok(());
            };
            let Some(index) = record
                .asked
                .iter()
                .position(|asked| asked.turn == turn.0 && asked.stage == Stage::Running)
            else {
                self.fork.running = None;
                return Ok(());
            };
            let spool = self.profile.spool_path_of(turn, FORK_ATTEMPT);
            let read = SpoolReader::new(&spool)
                .read_window(record.asked[index].offset, u64::MAX, WINDOW)
                .map_err(|error| io_error(&spool, error))?;
            let empty = read.records.is_empty() && read.damaged.is_empty();
            if empty && !running.over {
                return Ok(());
            }
            out.damaged_lines += read.damaged.len();
            let session = record.session.clone();
            // Where the fork's tool is told to stop when its interpreter says it must not go on.
            let (halt_at, grace_ms) = (self.fork_socket(turn), self.profile.stop_grace_ms);
            let Some(running) = self.fork.running.as_mut() else {
                return Ok(());
            };
            let mut batch = self.recorder.batch();
            let mut signals = Vec::new();
            let mut aloud = Vec::new();
            let asked = &mut record.asked[index];
            for line in &read.records {
                let t = line.t;
                match &line.entry {
                    Line::Shim(ShimEvent::SpawnFailed { error }) => {
                        running.outcome.get_or_insert(RunOutcome::Failed {
                            reason: error.clone(),
                        });
                        running.over = true;
                    }
                    Line::Shim(ShimEvent::Exited { .. }) => running.over = true,
                    _ => {}
                }
                running.interpreter.on_record(line, &mut signals);
                for signal in signals.drain(..) {
                    let message = &running.message;
                    let piece = match signal {
                        Signal::Halt => {
                            out.actions.push(Action::Stop {
                                turn,
                                socket: halt_at.clone(),
                                grace_ms,
                            });
                            continue;
                        }
                        Signal::Ended(outcome) => {
                            running.outcome = Some(outcome);
                            continue;
                        }
                        Signal::Reply(mut reply) => {
                            if !read_now(&mut batch, asked, (message, &session), out, t)? {
                                continue;
                            }
                            reply.turn = turn.0;
                            reply.trace_id.clone_from(&message.trace_id);
                            reply.origin = message.origin.clone();
                            reply.backend_session = Some(session.clone());
                            reply.text = kept(std::mem::take(&mut reply.text), &reply.origin);
                            // Those who watch are shown the fork's turn while it is the one shown.
                            let shown = self
                                .fork
                                .shown
                                .as_mut()
                                .filter(|shown| shown.turn == turn)
                                .map(|shown| said::Shown {
                                    hub: &mut shown.hub,
                                    now: t,
                                    events: &mut shown.events,
                                });
                            let words = !reply.text.trim().is_empty();
                            let followed =
                                asked
                                    .open
                                    .reply(&reply.id, words, !reply.tools.is_empty(), shown);
                            follow(&mut batch, followed, turn, t)?;
                            if words {
                                asked.replied = shortened(&reply.text);
                            }
                            if message.speak {
                                let trace = reply.trace_id.as_deref();
                                for said in lines_of(&reply.id, reply.turn, &reply.text, trace, t) {
                                    batch.item(record_item(kind::LINE, said.id.clone(), &said, t)?);
                                    aloud.push(said);
                                }
                            }
                            Entry::Reply(reply)
                        }
                        Signal::Tool(mut call) => {
                            if !read_now(&mut batch, asked, (message, &session), out, t)? {
                                continue;
                            }
                            call.turn = turn.0;
                            call.trace_id.clone_from(&message.trace_id);
                            call.origin = message.origin.clone();
                            let known = running.first_written.contains_key(&call.id)
                                || running.calls.iter().any(|open| open.id == call.id);
                            if call.reply.is_none()
                                && (call.status == ToolStatus::Running || !known)
                            {
                                follow(&mut batch, asked.open.work(), turn, t)?;
                            }
                            if call.status == ToolStatus::Running && !known {
                                let summary = summarize(&call.name, &call.input).summary;
                                asked
                                    .did
                                    .push(shortened(&format!("{}: {summary}", call.name)));
                            }
                            running.calls.retain(|open| open.id != call.id);
                            if call.status == ToolStatus::Running {
                                running.calls.push(call.clone());
                            }
                            Entry::Tool(call)
                        }
                        Signal::Notice(notice) if asked.read => Entry::Notice(notice),
                        // What the fork does as it runs is shown as the turn the user talks to.
                        Signal::Glimpse(glimpse) => {
                            // A plan that changes is work, as in the agent's own turn.
                            if let clarp_kernel::live::Glimpse::Plan { steps, .. } = &glimpse
                                && let Some(followed) = asked.open.plan(steps)
                            {
                                follow(&mut batch, followed, turn, t)?;
                            }
                            if let Some(shown) =
                                self.fork.shown.as_mut().filter(|shown| shown.turn == turn)
                            {
                                let events = shown.hub.show(&glimpse, Some(&shown.name), t);
                                shown.events.extend(events);
                            }
                            continue;
                        }
                        _ => continue,
                    };
                    if piece.part_of().is_none() {
                        asked.last_row = piece.id().to_string();
                    }
                    let at = match &piece {
                        Entry::Tool(call) if call.reply.is_none() => {
                            *running.first_written.entry(call.id.clone()).or_insert(t)
                        }
                        _ => t,
                    };
                    batch.item(written(&piece, at)?);
                }
            }
            asked.offset = read.next_offset;
            let over = running.over;
            if over {
                let outcome = match running.outcome.take() {
                    _ if running.stopped => RunOutcome::Stopped {
                        by: clarp_kernel::control::USER.to_owned(),
                    },
                    Some(outcome) => outcome,
                    None => RunOutcome::Interrupted {
                        reason: "the fork's tool ended and nothing said how".to_owned(),
                    },
                };
                // A message the user stopped before the fork began to answer is settled as
                // stopped, as a stopped turn's message is: it enters the conversation now, with
                // its end, and is not given back to run later.
                let answered = asked.read;
                if !answered && running.stopped {
                    read_now(&mut batch, asked, (&running.message, &session), out, now_ms)?;
                }
                if asked.read {
                    if answered {
                        record.begun = true;
                    } else if !record.begun {
                        // Whether the copy was made is not known: the next turn makes a new one.
                        let fresh = format!("{}:{}", record.session, turn.0);
                        if let Some(session) = clarp_kernel::takeover::prompt_uuid(&fresh) {
                            record.session = session;
                        }
                    }
                    asked.stage = Stage::Answered;
                    asked.stopped = running.stopped;
                    let (status, failed) = match &outcome {
                        RunOutcome::Completed => ("completed", None),
                        RunOutcome::Stopped { .. } => ("stopped", None),
                        RunOutcome::Failed { reason } => ("failed", Some(reason.as_str())),
                        RunOutcome::Interrupted { reason } => {
                            ("interrupted", Some(reason.as_str()))
                        }
                    };
                    if let Some(reason) = failed {
                        asked.failed = Some(shortened(reason));
                        let name = attempt_of(turn_name(&running.message.id), FORK_ATTEMPT);
                        let notice = ending_notice(
                            turn,
                            &name,
                            "turn-interrupted",
                            reason,
                            running.message.trace_id.clone(),
                        );
                        asked.last_row = notice.id().to_string();
                        batch.item(written(&notice, now_ms)?);
                    }
                    if status != "completed" {
                        for mut call in running.calls.drain(..) {
                            call.status = ToolStatus::Interrupted;
                            call.ended_at_ms = Some(now_ms);
                            let at = match call.reply {
                                None => running
                                    .first_written
                                    .get(&call.id)
                                    .copied()
                                    .unwrap_or(now_ms),
                                Some(_) => now_ms,
                            };
                            batch.item(written(&Entry::Tool(call), at)?);
                        }
                    }
                    let end = TurnEnd {
                        id: format!("t-{}", turn.0),
                        turn: turn.0,
                        closes: asked.last_row.clone(),
                        status: status.to_owned(),
                        never_ran: None,
                        replaced_by: None,
                        started_at_ms: Some(running.started_at_ms),
                        ended_at_ms: now_ms,
                        tool_count: u32::try_from(asked.did.len()).unwrap_or(u32::MAX),
                        trace_id: running.message.trace_id.clone(),
                        model: None,
                        tokens_in: None,
                        tokens_out: None,
                        context_tokens: None,
                        cost_micros: None,
                        provider_usage: Vec::new(),
                        cost_so_far: None,
                        total: None,
                        limits: None,
                        note_count: 0,
                        reason: match &outcome {
                            RunOutcome::Completed => None,
                            RunOutcome::Stopped { by } => {
                                Some(clarp_kernel::conversation::stopped_sentence(by, ""))
                            }
                            RunOutcome::Failed { reason } | RunOutcome::Interrupted { reason } => {
                                Some(clarp_kernel::conversation::interrupted_sentence(reason))
                            }
                        },
                        kind: match &outcome {
                            RunOutcome::Failed { reason } => {
                                Some(failures::classify_error(reason).to_owned())
                            }
                            _ => None,
                        },
                    };
                    batch.item(written(&Entry::TurnEnd(end), now_ms)?);
                    self.fork.ended = Some((turn, status, failed.map(str::to_owned)));
                    // The agent's own turn may have settled it already, when it ended first.
                    let _ = batch.command(Command::JoinSettled { turn }, now_ms);
                } else if running.stopped {
                    // The agent's own turn took it back already: a Stop still means it does
                    // not run.
                    let by = clarp_kernel::control::USER.to_owned();
                    let _ = batch.command(Command::Withdraw { turn, by }, now_ms);
                    asked.stage = Stage::GivenBack;
                    self.fork.ended = Some((turn, "interrupted", None));
                } else {
                    self.fork.ended = Some((turn, "interrupted", None));
                    // Nothing of it reached the user: its message stays the agent's own, and
                    // runs when the agent's turn ends. The fork takes nothing more.
                    eprintln!(
                        "clarp-host: agent {}: its fork could not answer turn {}: {outcome:?}",
                        self.profile.session, turn.0
                    );
                    record.broken = true;
                    for asked in &mut record.asked {
                        if matches!(asked.stage, Stage::Waiting | Stage::Running) && !asked.read {
                            asked.stage = Stage::GivenBack;
                        }
                    }
                }
            }
            batch.item(lane_item(&record, now_ms)?);
            batch.commit()?;
            self.fork.record = Some(record);
            for said in &aloud {
                self.speech.apply(said);
                out.changed.speech = true;
            }
            self.unstored_changes = 0;
            out.changed.conversation = true;
            out.changed.queue = true;
            if over {
                self.fork.running = None;
                if !self.profile.keep_spools {
                    let _ = std::fs::remove_file(self.profile.spool_path_of(turn, FORK_ATTEMPT));
                    let _ = std::fs::remove_file(self.profile.stdin_path_of(turn, FORK_ATTEMPT));
                }
                self.fork_next(out, now_ms)?;
                return Ok(());
            }
        }
    }

    /// Nothing answers at the shim of the fork's turn any more, or it could not be started.
    pub(super) fn fork_gone(
        &mut self,
        turn: TurnId,
        attempt: u32,
        reason: &str,
        now_ms: i64,
    ) -> Result<Outputs, ActorError> {
        let mut out = Outputs::default();
        self.pump_fork(&mut out, now_ms)?;
        if let Some(running) = self.fork.running.as_mut()
            && running.turn == turn
            && attempt == FORK_ATTEMPT
        {
            running.over = true;
            running.outcome.get_or_insert(RunOutcome::Interrupted {
                reason: reason.to_owned(),
            });
            self.pump_fork(&mut out, now_ms)?;
        }
        self.fork_reconcile(&mut out, now_ms)?;
        self.resume_deferred(&mut out, now_ms)?;
        Ok(out)
    }

    /// What the agent's own turn gave back is the fork's no more: a message the fork had not
    /// read when that turn ended runs as the agent's own, and the fork's tool that still had
    /// it is stopped, whatever it would say.
    pub(super) fn fork_reconcile(
        &mut self,
        out: &mut Outputs,
        now_ms: i64,
    ) -> Result<(), ActorError> {
        let Some(mut record) = self
            .fork
            .record
            .clone()
            .filter(|record| record.state == State::Open)
        else {
            return Ok(());
        };
        let joins = self.recorder.agent().joins();
        let mut changed = false;
        for asked in &mut record.asked {
            if matches!(asked.stage, Stage::Waiting | Stage::Running)
                && !asked.read
                && !joins.contains_key(&TurnId(asked.turn))
            {
                asked.stage = Stage::GivenBack;
                changed = true;
                if self
                    .fork
                    .running
                    .as_ref()
                    .is_some_and(|running| running.turn.0 == asked.turn)
                {
                    self.fork.running = None;
                    out.actions.push(Action::Stop {
                        turn: TurnId(asked.turn),
                        socket: self
                            .profile
                            .socket_path_of(TurnId(asked.turn), FORK_ATTEMPT),
                        grace_ms: self.profile.stop_grace_ms,
                    });
                }
            }
        }
        if !changed {
            return Ok(());
        }
        let mut batch = self.recorder.batch();
        batch.item(lane_item(&record, now_ms)?);
        batch.commit()?;
        self.fork.record = Some(record);
        self.fork_next(out, now_ms)
    }

    /// The note that opens the agent's own turn `turn`, where its fork has done what it was
    /// asked: the fork is then over. The same note opens another attempt of that turn. A
    /// turn of the Host's own (`skip`: a compaction, an opening) is not given it.
    pub(super) fn fork_note(
        &mut self,
        turn: TurnId,
        skip: bool,
        out: &mut Outputs,
        now_ms: i64,
    ) -> Result<Option<String>, ActorError> {
        if skip {
            return Ok(None);
        }
        self.fork_reconcile(out, now_ms)?;
        let Some(record) = self.fork.record.as_ref() else {
            return Ok(None);
        };
        match record.state {
            // Another attempt of the turn that has it is told it again.
            State::Merged { into } | State::Offered { into } if into == turn.0 => {
                return Ok(note(record));
            }
            // The turn it was offered to never ran its tool: this one is given it.
            State::Offered { .. } | State::Open => {}
            State::Merged { .. } | State::Closed => return Ok(None),
        }
        let busy = self.fork.running.is_some()
            || record
                .asked
                .iter()
                .any(|asked| matches!(asked.stage, Stage::Waiting | Stage::Running));
        if busy {
            return Ok(None);
        }
        let told = note(record);
        let mut record = record.clone();
        record.state = match told {
            Some(_) => State::Offered { into: turn.0 },
            None => State::Closed,
        };
        let mut batch = self.recorder.batch();
        batch.item(lane_item(&record, now_ms)?);
        batch.commit()?;
        self.fork.record = Some(record);
        Ok(told)
    }

    pub(super) fn defer_launch(&mut self, turn: TurnId, message_seq: u64, attempt: u32) {
        self.fork.deferred = Some((turn, message_seq, attempt));
    }

    pub(super) fn launch_deferred(&self, turn: TurnId) -> bool {
        self.fork
            .deferred
            .is_some_and(|(deferred, _, _)| deferred == turn)
    }

    pub(super) fn undefer_launch(&mut self) {
        self.fork.deferred = None;
    }

    /// Nothing holds the launch any more (the fork's turn and the turn before's process are
    /// over): the agent's own turn that waited starts, opening with the fork's note where
    /// there is one, if it still holds the agent.
    pub(super) fn resume_deferred(
        &mut self,
        out: &mut Outputs,
        now_ms: i64,
    ) -> Result<(), ActorError> {
        if self.fork_runs() || self.lingers() {
            return Ok(());
        }
        let Some((turn, message_seq, attempt)) = self.fork.deferred.take() else {
            return Ok(());
        };
        let still = matches!(
            self.recorder.agent().live(),
            Some(Turn::Starting(starting)) if starting.id == turn && starting.attempt == attempt
        );
        if still {
            self.start(turn, message_seq, attempt, out, now_ms)?;
        }
        Ok(())
    }

    /// Whether a turn of the agent's conversation fork runs now.
    pub fn fork_runs(&self) -> bool {
        self.fork.running.is_some()
    }

    /// `POST /stop` while the fork answers stops the fork's turn, and only that: it is the
    /// turn the user talks to. Answers whether one ran.
    pub fn fork_halt(&mut self) -> (bool, Outputs) {
        let mut out = Outputs::default();
        let Some(running) = self.fork.running.as_mut() else {
            return (false, out);
        };
        running.stopped = true;
        let turn = running.turn;
        out.actions.push(Action::Stop {
            turn,
            socket: self.fork_socket(turn),
            grace_ms: self.profile.stop_grace_ms,
        });
        (true, out)
    }

    /// The fork's turn that runs, as it is shown: its number, when it began, and its name.
    pub(super) fn fork_held(&self) -> Option<(TurnId, i64, String)> {
        let running = self.fork.running.as_ref()?;
        let message = &running.message;
        let name = super::shown_name(message.trace_id.as_deref(), &message.id);
        Some((running.turn, running.started_at_ms, name))
    }

    /// How the fork's turn `turn` ended, in the words of those who watch (`completed`,
    /// `failed` with what went wrong, or `interrupted`; one the user stopped is interrupted,
    /// as a stopped turn of the agent's own is).
    fn fork_ended_as(&self, turn: TurnId) -> (&'static str, Option<serde_json::Value>) {
        match self
            .fork
            .ended
            .as_ref()
            .filter(|(ended, _, _)| *ended == turn)
        {
            Some((_, "completed", _)) => ("completed", None),
            Some((_, "failed", Some(reason))) => {
                let kind = failures::classify_error(reason);
                let model = self.turn_model().unwrap_or_default();
                let told = failures::told(kind, reason, &model, failures::Known::default());
                let error = json!({
                    "reason": kind,
                    "message": clarp_kernel::python::first(&told.message, 300),
                    "detail": clarp_kernel::python::first(&told.provider_message, 500),
                });
                ("failed", Some(error))
            }
            _ => ("interrupted", None),
        }
    }

    /// Brings what is shown in step with the fork: its turn is begun in a hub of its own when
    /// it starts to run, the agent's own turn is told beside it as it changes, and when the
    /// fork's turn is over it is ended there and the agent's own hub takes over again.
    pub(super) fn fork_show(&mut self, now_ms: i64) {
        let held = self.fork_held();
        let shown_turn = self.fork.shown.as_ref().map(|shown| shown.turn);
        if shown_turn.is_some() && shown_turn != held.as_ref().map(|(turn, _, _)| *turn) {
            self.fork_unshow(now_ms);
        }
        if self.fork.shown.is_none()
            && let Some((turn, started_at_ms, name)) = held
        {
            let conversation = self.shown_conversation();
            let mut hub = clarp_kernel::live::Hub::new(
                &self.profile.epoch,
                &self.profile.agent_id,
                &self.profile.session,
            )
            .growing();
            // Its first events are numbered from one: nobody is sent them. The first event a
            // client is sent is numbered past the agent's own hub, and sends it to the snapshot.
            let _ = hub.begin_turn(
                &self.profile.session,
                &conversation,
                &name,
                Some(started_at_ms),
                clarp_kernel::coordination::USER,
                now_ms,
            );
            hub.continue_after(self.shown.lseq());
            // What the agent's own hub said before goes out first.
            self.fork.ready.append(&mut self.shown_events);
            self.fork.shown = Some(Shown {
                turn,
                name,
                hub,
                events: Vec::new(),
                told: None,
            });
        }
        let Some(main) = self
            .fork
            .shown
            .as_ref()
            .map(|_| self.main_turn_beside_fork())
        else {
            return;
        };
        let conversation = self.shown_conversation();
        if let Some(shown) = self.fork.shown.as_mut() {
            let now = main.unwrap_or(serde_json::Value::Null);
            if shown.told.as_ref() != Some(&now) {
                let op = json!({"op": MAIN_TURN_OP, "conv": conversation, MAIN_TURN_OP: now});
                let events = shown.hub.tell(op, now_ms);
                shown.events.extend(events);
                shown.told = Some(now);
            }
        }
    }

    /// The fork's turn is over: it is ended in its hub, the agent's own turn is told to be no
    /// longer beside it, and the agent's own hub takes over, numbered past the fork's.
    fn fork_unshow(&mut self, now_ms: i64) {
        let Some(mut shown) = self.fork.shown.take() else {
            return;
        };
        let (status, error) = self.fork_ended_as(shown.turn);
        let ended = shown.hub.end_turn(status, error.as_ref(), now_ms);
        shown.events.extend(ended);
        if shown.told.as_ref() != Some(&serde_json::Value::Null) {
            let conversation = self.shown_conversation();
            let op = json!({"op": MAIN_TURN_OP, "conv": conversation, MAIN_TURN_OP: null});
            shown.events.extend(shown.hub.tell(op, now_ms));
        }
        self.fork.ready.append(&mut shown.events);
        // What the agent's own hub said meanwhile was not sent: its snapshot has it.
        self.shown_events.clear();
        self.shown.continue_after(shown.hub.lseq());
    }

    /// The events to send now, in order: those of a hand-over, and then the hub that is shown.
    pub(super) fn shown_taken(&mut self) -> Vec<serde_json::Value> {
        let mut out = std::mem::take(&mut self.fork.ready);
        match self.fork.shown.as_mut() {
            Some(shown) => {
                out.append(&mut shown.events);
                // The agent's own turn is told beside the fork's, as `main_turn`.
                self.shown_events.clear();
            }
            None => out.append(&mut self.shown_events),
        }
        out
    }

    /// `GET /live` while the fork's turn is shown: that turn, and the agent's own beside it.
    pub(super) fn fork_whole(&mut self, now_ms: i64) -> Option<serde_json::Value> {
        let main = self.main_turn_beside_fork();
        let shown = self.fork.shown.as_mut()?;
        let (mut snapshot, events) = shown.hub.snapshot(now_ms)?;
        shown.events.extend(events);
        snapshot[MAIN_TURN_OP] = main.unwrap_or(serde_json::Value::Null);
        Some(snapshot)
    }

    pub(super) fn fork_due_ms(&self) -> Option<i64> {
        self.fork
            .shown
            .as_ref()
            .and_then(|shown| shown.hub.due_ms())
    }

    pub(super) fn fork_due(&mut self, now_ms: i64) {
        if let Some(shown) = self.fork.shown.as_mut() {
            let events = shown.hub.fire(now_ms);
            shown.events.extend(events);
        }
    }

    /// Puts fields on an item of the fork's turn, when the item is the fork's.
    pub(super) fn fork_patch(
        &mut self,
        item: &str,
        fields: &serde_json::Value,
        now_ms: i64,
    ) -> bool {
        let Some(shown) = self
            .fork
            .shown
            .as_mut()
            .filter(|shown| shown.hub.has_item(item))
        else {
            return false;
        };
        let events = shown.hub.patch(item, fields, None, now_ms);
        shown.events.extend(events);
        true
    }

    /// The agent's own turn, while its fork's is the one shown (`main_turn` of `GET /live`):
    /// what it is, how far, and what it does, without a headline.
    pub(super) fn main_turn_beside_fork(&self) -> Option<serde_json::Value> {
        let (forked, _, _) = self.fork_held()?;
        let agent = self.recorder.agent();
        let (turn, started_at_ms) = match agent.live()? {
            Turn::Starting(turn) => (turn.id, turn.started_at_ms),
            Turn::Running(turn) => (turn.id, turn.started_at_ms),
            Turn::Parked(turn) => (turn.id, turn.started_at_ms),
            _ => return None,
        };
        let live = self
            .live
            .as_ref()
            .filter(|live| live.turn == turn && turn != forked && !self.launch_deferred(turn))?;
        let name = super::shown_name(live.trace_id.as_deref(), &live.message_id);
        let audience = self
            .audience_of(turn)
            .unwrap_or(clarp_kernel::coordination::USER);
        let activity = match super::told_activity(agent) {
            Some(Activity::Tool { name }) => json!({"state": "tool", "tool": {"name": name}}),
            Some(Activity::Responding) => json!({"state": "responding"}),
            Some(Activity::Waiting) => json!({"state": "waiting"}),
            Some(Activity::Compacting) => json!({"state": "compacting"}),
            Some(Activity::Thinking | Activity::Retrying(_)) | None => {
                json!({"state": "thinking"})
            }
        };
        Some(json!({
            "turn_id": name,
            "audience": audience,
            "status": "running",
            "started_at_ms": started_at_ms,
            "tool_count": live.tool_count,
            "activity": activity,
        }))
    }

    /// Takes the fork up as the log has it, when the actor is opened: a turn of it that ran
    /// on is read up to where the log is and followed again.
    pub(super) fn fork_take_up(
        &mut self,
        out: &mut Outputs,
        now_ms: i64,
    ) -> Result<(), ActorError> {
        let Some(item) = self.recorder.log().find(kind::FORK_LANE)? else {
            return Ok(());
        };
        let record: Record = serde_json::from_slice(&item.body).map_err(|error| {
            ActorError::Corrupt(format!("log item {} is not a fork: {error}", item.seq))
        })?;
        let running = record
            .asked
            .iter()
            .find(|asked| asked.stage == Stage::Running)
            .map(|asked| (TurnId(asked.turn), asked.message_seq, asked.offset));
        self.fork.record = Some(record);
        if let Some((turn, message_seq, offset)) = running {
            let message = self.message_at(message_seq)?;
            let name = attempt_of(turn_name(&message.id), FORK_ATTEMPT);
            let mut interpreter = self.backend.interpreter(turn.0, &name);
            // Where the old Host was: what it read is read again, and nothing of it written,
            // but what the turn holds as it runs: when it began, its calls that have not
            // returned, and when each call that is a row was first written. As `rejoin` does
            // for the agent's own turn.
            let spool = self.profile.spool_path_of(turn, FORK_ATTEMPT);
            let mut at = 0;
            let mut signals = Vec::new();
            let mut started_at_ms = None;
            let mut calls: Vec<ToolCall> = Vec::new();
            let mut first_written = HashMap::new();
            while at < offset {
                let read = SpoolReader::new(&spool)
                    .read_window(at, offset, WINDOW)
                    .map_err(|error| io_error(&spool, error))?;
                if read.records.is_empty() {
                    break;
                }
                for line in &read.records {
                    started_at_ms.get_or_insert(line.t);
                    interpreter.on_record(line, &mut signals);
                    for signal in signals.drain(..) {
                        let Signal::Tool(call) = signal else {
                            continue;
                        };
                        if call.reply.is_none() {
                            first_written.entry(call.id.clone()).or_insert(line.t);
                        }
                        calls.retain(|open| open.id != call.id);
                        if call.status == ToolStatus::Running {
                            let mut call = call;
                            call.turn = turn.0;
                            call.trace_id.clone_from(&message.trace_id);
                            call.origin = message.origin.clone();
                            calls.push(call);
                        }
                    }
                }
                at = read.next_offset;
            }
            self.fork.running = Some(Running {
                turn,
                interpreter,
                message,
                calls,
                first_written,
                started_at_ms: started_at_ms.unwrap_or(now_ms),
                outcome: None,
                over: false,
                stopped: false,
            });
            out.actions.push(Action::ForkFollow {
                turn,
                attempt: FORK_ATTEMPT,
                socket: self.fork_socket(turn),
            });
        }
        Ok(())
    }

    /// Once the agent's own turn is where it was: what the agent's turn gave back while the
    /// Host was away is the fork's no more, and the fork's next message, or the turn of the
    /// user's that waited for it, goes on.
    pub(super) fn fork_restore(
        &mut self,
        out: &mut Outputs,
        now_ms: i64,
    ) -> Result<(), ActorError> {
        self.fork_reconcile(out, now_ms)?;
        self.fork_next(out, now_ms)?;
        self.resume_deferred(out, now_ms)
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;

    /// A fork's record written before its replies were kept by the agent's own rules still
    /// loads with them, and is written again under the same names.
    #[test]
    fn a_question_written_before_keeps_its_open_replies() {
        let before = json!({
            "turn": 4, "message_seq": 9, "stage": "running", "read": true, "offset": 120,
            "last_row": "r-2", "open": ["r-1", "r-2"], "open_worked": true,
            "said": "what is it", "replied": "this", "did": [], "stopped": false,
        });
        let mut asked: Asked = serde_json::from_value(before).unwrap();
        let again = serde_json::to_value(&asked).unwrap();
        assert_eq!(again["open"], json!(["r-1", "r-2"]));
        assert_eq!(again["open_worked"], json!(true));
        assert!(again.get("plan").is_none());
        // Work that comes makes them said along the way.
        assert_eq!(asked.open.work(), ["r-1", "r-2"]);
        // A record with neither, as the first ones were written, loads with nothing open.
        let first = json!({"turn": 1, "message_seq": 2, "stage": "waiting"});
        let asked: Asked = serde_json::from_value(first).unwrap();
        assert!(asked.open.is_empty());
    }
}
