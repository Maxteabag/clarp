//! One agent's thread: the actor, a mailbox in front of it, and the doing of what it decides.
//!
//! The thread is the only thing that touches the actor, so the actor needs no lock, and
//! everything an agent does costs that agent's time and nobody else's. It blocks on its own log
//! and its own files, which is why it is a thread and not a task of the server's runtime.
//!
//! When the actor reports that its log or files failed, the thread drops it and opens it again
//! from the log: the same path as after a restart, and the only way back to a known state.

use std::sync::Arc;
use std::sync::mpsc::{self, RecvTimeoutError};
use std::time::{Duration, Instant};

use clarp_actor::conversation::{Page, Versioned};
use clarp_actor::{
    Action, Actor, ActorError, Changed, ImportedRow, Outputs, Profile, QueueState, Sent, Status,
    Waiting,
};
use clarp_backend::Backend;
use clarp_kernel::conversation::{Entry, Message, TurnEnd};
use clarp_kernel::turn::{Activity, Admission, Outcome, TurnId};
use clarp_ports::clock::Clock;
use clarp_ports::codec::CodecSet;
use clarp_ports::log::LogEngine;
use tokio::sync::{oneshot, watch};

use crate::fleet::AgentRecord;
use crate::hub::Hub;
use crate::ids;
use crate::launcher::{self, Launcher, Told};
use crate::wire;

/// The least time between two "the conversation changed" events for one agent. A reply that
/// grows is told a few times a second, and once more when it stops growing.
const TRANSCRIPT_EVERY: Duration = Duration::from_millis(250);

const REOPEN_FIRST: Duration = Duration::from_millis(200);
const REOPEN_MOST: Duration = Duration::from_secs(30);

/// The agent could not answer: its log or its files are failing, or it is closed.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Unavailable(pub String);

pub type Answer<T> = oneshot::Sender<Result<T, Unavailable>>;

/// What an agent's thread is asked.
pub enum Ask {
    Send {
        message: Message,
        admission: Admission,
        answer: Answer<Sent>,
    },
    /// Answers how many turns were told to end, and where the queue stands after.
    Stop {
        by: String,
        answer: Answer<(u32, QueueState)>,
    },
    /// Answers whether the mailbox was held, and where the queue stands after.
    Resume {
        by: String,
        answer: Answer<(bool, QueueState)>,
    },
    /// What waits, in the order it will run.
    Queue {
        answer: Answer<(Vec<Waiting>, QueueState)>,
    },
    /// Take over the conversation the agent had on another Host. Answers how many rows came,
    /// or why it cannot be done.
    Import {
        from: String,
        backend_session: Option<String>,
        rows: Vec<ImportedRow>,
        answer: Answer<Result<usize, String>>,
    },
    Withdraw {
        turn: TurnId,
        by: String,
        answer: Answer<bool>,
    },
    Tail {
        limit: usize,
        answer: Answer<Page>,
    },
    After {
        revision: u64,
        limit: usize,
        answer: Answer<Page>,
    },
    Before {
        id: String,
        limit: usize,
        answer: Answer<Option<Page>>,
    },
    /// From a turn's thread: the live turn's spool may have grown.
    SpoolChanged,
    /// From a turn's thread: nothing answers at the turn's shim any more.
    ShimGone {
        turn: TurnId,
    },
    /// From a turn's thread: no shim could be started.
    LaunchFailed {
        turn: TurnId,
        reason: String,
    },
    /// The agent was removed: the thread ends.
    Close,
}

/// What an agent is doing, as anyone may read it at any time without asking the agent.
#[derive(Debug, Clone, PartialEq)]
pub struct View {
    pub status: Status,
    /// The newest row of the conversation: what a list of chats shows of it.
    pub newest: Option<Versioned>,
    /// How the last turn ended, with what its tool said of itself.
    pub ended: Option<TurnEnd>,
    pub backend: &'static str,
    /// Why the agent cannot answer right now, when it cannot.
    pub broken: Option<String>,
}

/// Everything an agent's thread is given.
pub struct Setup {
    pub record: AgentRecord,
    pub profile: Profile,
    pub backend: Arc<dyn Backend>,
    pub engine: Arc<dyn LogEngine>,
    pub codecs: CodecSet,
    pub launcher: Arc<Launcher>,
    pub clock: Arc<dyn Clock + Send + Sync>,
    pub hub: Arc<Hub>,
}

/// The way to an agent's thread.
#[derive(Clone)]
pub struct AgentHandle {
    tx: mpsc::Sender<Ask>,
    view: watch::Receiver<Arc<View>>,
}

impl AgentHandle {
    async fn ask<T>(&self, make: impl FnOnce(Answer<T>) -> Ask) -> Result<T, Unavailable> {
        let (answer, reply) = oneshot::channel();
        self.tx
            .send(make(answer))
            .map_err(|_| Unavailable("the agent is closed".to_string()))?;
        reply
            .await
            .map_err(|_| Unavailable("the agent did not answer".to_string()))?
    }

    pub async fn send(&self, message: Message, admission: Admission) -> Result<Sent, Unavailable> {
        self.ask(|answer| Ask::Send {
            message,
            admission,
            answer,
        })
        .await
    }

    pub async fn stop(&self, by: String) -> Result<(u32, QueueState), Unavailable> {
        self.ask(|answer| Ask::Stop { by, answer }).await
    }

    pub async fn resume(&self, by: String) -> Result<(bool, QueueState), Unavailable> {
        self.ask(|answer| Ask::Resume { by, answer }).await
    }

    pub async fn queue(&self) -> Result<(Vec<Waiting>, QueueState), Unavailable> {
        self.ask(|answer| Ask::Queue { answer }).await
    }

    /// Takes over the conversation the agent had on another Host.
    pub async fn import(
        &self,
        from: String,
        backend_session: Option<String>,
        rows: Vec<ImportedRow>,
    ) -> Result<Result<usize, String>, Unavailable> {
        self.ask(|answer| Ask::Import {
            from,
            backend_session,
            rows,
            answer,
        })
        .await
    }

    pub async fn withdraw(&self, turn: TurnId, by: String) -> Result<bool, Unavailable> {
        self.ask(|answer| Ask::Withdraw { turn, by, answer }).await
    }

    pub async fn tail(&self, limit: usize) -> Result<Page, Unavailable> {
        self.ask(|answer| Ask::Tail { limit, answer }).await
    }

    pub async fn after(&self, revision: u64, limit: usize) -> Result<Page, Unavailable> {
        self.ask(|answer| Ask::After {
            revision,
            limit,
            answer,
        })
        .await
    }

    pub async fn before(&self, id: String, limit: usize) -> Result<Option<Page>, Unavailable> {
        self.ask(|answer| Ask::Before { id, limit, answer }).await
    }

    /// What the agent is doing, as it last said.
    pub fn view(&self) -> Arc<View> {
        self.view.borrow().clone()
    }

    /// Ends the agent's thread. Its turn, if one runs, is not touched.
    pub fn close(&self) {
        let _ = self.tx.send(Ask::Close);
    }
}

/// Starts an agent's thread. The agent is opened on that thread, not here.
pub fn spawn(setup: Setup) -> AgentHandle {
    let (tx, rx) = mpsc::channel();
    let idle = Status {
        live: None,
        last: None,
        waiting: Vec::new(),
        paused: false,
        queue_revision: 0,
        head_revision: 0,
        backend_session: None,
    };
    let (view_tx, view) = watch::channel(Arc::new(View {
        status: idle,
        newest: None,
        ended: None,
        backend: setup.backend.id(),
        broken: Some("the agent is being opened".to_string()),
    }));
    let worker = Worker {
        setup,
        actor: None,
        tx: tx.clone(),
        view: view_tx,
        told: None,
        newest: None,
        ended: None,
        transcript_sent: None,
        transcript_due: None,
    };
    let name = format!("agent-{}", worker.setup.record.session);
    let spawned = std::thread::Builder::new()
        .name(name)
        .spawn(move || worker.run(&rx));
    if let Err(error) = spawned {
        eprintln!("clarp-host: an agent's thread could not be started: {error}");
    }
    AgentHandle { tx, view }
}

struct Worker {
    setup: Setup,
    actor: Option<Actor>,
    /// The way to this thread, for the turns' threads.
    tx: mpsc::Sender<Ask>,
    view: watch::Sender<Arc<View>>,
    /// What the clients were last told, so that only a change is told.
    told: Option<Status>,
    /// The newest row of the conversation.
    newest: Option<Versioned>,
    /// How the last turn ended.
    ended: Option<TurnEnd>,
    transcript_sent: Option<Instant>,
    /// When a held-back "the conversation changed" is to be sent.
    transcript_due: Option<Instant>,
}

fn unavailable(error: &ActorError) -> Unavailable {
    Unavailable(error.to_string())
}

impl Worker {
    fn run(mut self, rx: &mpsc::Receiver<Ask>) {
        let mut wait = REOPEN_FIRST;
        loop {
            if self.actor.is_none() {
                match self.open() {
                    Ok(()) => wait = REOPEN_FIRST,
                    Err(why) => {
                        eprintln!(
                            "clarp-host: agent {} cannot be opened: {why}",
                            self.setup.record.session
                        );
                        self.show_broken(&why);
                        if !refuse_for(rx, wait, &why) {
                            return;
                        }
                        wait = (wait * 2).min(REOPEN_MOST);
                        continue;
                    }
                }
            }
            let ask = match self.transcript_due {
                None => match rx.recv() {
                    Ok(ask) => ask,
                    Err(_) => return,
                },
                Some(due) => match rx.recv_timeout(due.saturating_duration_since(Instant::now())) {
                    Ok(ask) => ask,
                    Err(RecvTimeoutError::Timeout) => {
                        self.tell_transcript();
                        continue;
                    }
                    Err(RecvTimeoutError::Disconnected) => return,
                },
            };
            if matches!(ask, Ask::Close) {
                return;
            }
            if let Err(error) = self.handle(ask) {
                eprintln!(
                    "clarp-host: agent {} failed and is opened again: {error}",
                    self.setup.record.session
                );
                self.actor = None;
            }
        }
    }

    fn now_ms(&self) -> i64 {
        self.setup.clock.now_ms()
    }

    fn open(&mut self) -> Result<(), String> {
        let log = self
            .setup
            .engine
            .open(&self.setup.profile.dir, self.setup.codecs.clone())
            .map_err(|error| error.to_string())?;
        let (actor, out) = Actor::open(
            self.setup.profile.clone(),
            self.setup.backend.clone(),
            log,
            self.now_ms(),
        )
        .map_err(|error| error.to_string())?;
        self.actor = Some(actor);
        // What the agent is doing when it is opened is not news: a client that connects is
        // told to look at everything anyway. What follows from opening it is.
        self.told = self.actor.as_ref().map(Actor::status);
        self.newest = self.newest_row();
        self.ended = self.last_end();
        self.publish_view();
        self.after(out);
        Ok(())
    }

    fn handle(&mut self, ask: Ask) -> Result<(), ActorError> {
        let now = self.now_ms();
        let Some(actor) = self.actor.as_mut() else {
            return Ok(());
        };
        let out = match ask {
            Ask::Send {
                message,
                admission,
                answer,
            } => answered(actor.send(&message, admission, now), answer)?,
            Ask::Stop { by, answer } => {
                let stopped = actor.stop(&by, now);
                let queue = actor.queue_state();
                answered(stopped.map(|(told, out)| ((told, queue), out)), answer)?
            }
            Ask::Resume { by, answer } => {
                let resumed = actor.resume(&by, now);
                let queue = actor.queue_state();
                answered(resumed.map(|(held, out)| ((held, queue), out)), answer)?
            }
            Ask::Queue { answer } => {
                let waiting = actor.queue().map(|waiting| (waiting, actor.queue_state()));
                return read(waiting, answer);
            }
            Ask::Import {
                from,
                backend_session,
                rows,
                answer,
            } => answered(actor.import(&from, backend_session, rows, now), answer)?,
            Ask::Withdraw { turn, by, answer } => answered(actor.withdraw(turn, &by, now), answer)?,
            Ask::Tail { limit, answer } => return read(actor.tail(limit), answer),
            Ask::After {
                revision,
                limit,
                answer,
            } => return read(actor.after(revision, limit), answer),
            Ask::Before { id, limit, answer } => return read(actor.before(&id, limit), answer),
            Ask::SpoolChanged => actor.spool_changed(now)?,
            Ask::ShimGone { turn } => actor.shim_gone(turn, now)?,
            Ask::LaunchFailed { turn, reason } => actor.launch_failed(turn, &reason, now)?,
            Ask::Close => return Ok(()),
        };
        self.after(out);
        Ok(())
    }

    /// Does what the actor decided, and tells the clients what changed.
    fn after(&mut self, out: Outputs) {
        if out.damaged_lines > 0 {
            eprintln!(
                "clarp-host: agent {}: {} damaged line(s) in a spool were passed over",
                self.setup.record.session, out.damaged_lines
            );
        }
        for action in out.actions {
            self.carry_out(action);
        }
        self.show(out.changed);
    }

    /// Each turn has a thread of its own for as long as its shim lives. It does the waiting, so
    /// the agent's thread never does.
    fn carry_out(&self, action: Action) {
        let tx = self.tx.clone();
        let spawned = match action {
            Action::Launch(order) => {
                let launcher = self.setup.launcher.clone();
                let turn = order.turn;
                std::thread::Builder::new()
                    .name(format!("turn-{}", turn.0))
                    .spawn(move || {
                        launcher.launch_and_follow(&order, &|told| tell(&tx, turn, told))
                    })
            }
            Action::Follow { turn, socket } => std::thread::Builder::new()
                .name(format!("turn-{}", turn.0))
                .spawn(move || launcher::follow(&socket, &|told| tell(&tx, turn, told))),
            Action::Stop {
                socket, grace_ms, ..
            } => std::thread::Builder::new()
                .name("stop".to_string())
                .spawn(move || launcher::stop(&socket, grace_ms)),
        };
        if let Err(error) = spawned {
            eprintln!("clarp-host: a turn's thread could not be started: {error}");
        }
    }

    /// The newest row of the conversation, as the actor has it now.
    fn newest_row(&self) -> Option<Versioned> {
        let page = self.actor.as_ref()?.tail(1).ok()?;
        page.entries.into_iter().last()
    }

    /// How the last turn ended, as the log has it.
    fn last_end(&self) -> Option<TurnEnd> {
        self.actor.as_ref()?.last_end().ok()?
    }

    fn publish_view(&self) {
        let Some(actor) = &self.actor else {
            return;
        };
        self.view.send_replace(Arc::new(View {
            status: actor.status(),
            newest: self.newest.clone(),
            ended: self.ended.clone(),
            backend: actor.backend_id(),
            broken: None,
        }));
    }

    /// Publishes the agent's view and tells the clients what changed since they were last told.
    fn show(&mut self, changed: Changed) {
        if !(changed.conversation || changed.status || changed.queue) {
            return;
        }
        let Some(actor) = &self.actor else {
            return;
        };
        let status = actor.status();
        let backend = actor.backend_id();
        let before = self.told.take();
        let was_live = before.as_ref().and_then(|told| told.live.as_ref());
        let was_waiting: &[TurnId] = before.as_ref().map_or(&[], |told| told.waiting.as_slice());
        // The ids of the messages that have joined the queue since the clients were last told.
        let joined: Vec<String> = if status
            .waiting
            .iter()
            .any(|turn| !was_waiting.contains(turn))
        {
            actor
                .queue()
                .unwrap_or_default()
                .into_iter()
                .filter(|waiting| !was_waiting.contains(&waiting.turn))
                .map(|waiting| waiting.message_id)
                .collect()
        } else {
            Vec::new()
        };
        if changed.conversation {
            self.newest = self.newest_row();
        }
        let turn_ended = status.last.as_ref().map(|last| last.id)
            != before
                .as_ref()
                .and_then(|told| told.last.as_ref())
                .map(|was| was.id);
        if turn_ended {
            self.ended = self.last_end();
        }
        let now = self.now_ms();
        let (hub, record) = (self.setup.hub.clone(), self.setup.record.clone());

        // The queue: a message joined it, one left it to run, it was held or let go.
        for message_id in &joined {
            hub.publish(
                wire::queue_updated(&record, &status, Some(message_id), false),
                now,
            );
        }
        // A turn began: the word a send is answered with, for every client that did not send it.
        if let Some(live) = &status.live
            && was_live.map(|was| was.turn) != Some(live.turn)
        {
            let waited = was_waiting.contains(&live.turn);
            if waited {
                hub.publish(
                    wire::queue_updated(&record, &status, Some(&live.message_id), true),
                    now,
                );
            }
            hub.publish(
                wire::dispatched(&record, live, waited, status.waiting.len()),
                now,
            );
        }
        if before
            .as_ref()
            .is_some_and(|told| told.paused != status.paused)
        {
            hub.publish(wire::queue_updated(&record, &status, None, false), now);
        }
        // What the agent is doing changed: its state, and the line that describes it.
        if before.as_ref().map(told_state) != Some(told_state(&status)) {
            if turn_ended {
                // The agent as it stood when the turn had ended and the next had not begun.
                let rested = Status {
                    live: None,
                    ..status.clone()
                };
                if let Some(at_once) = wire::stopped(&record, &rested) {
                    hub.publish(at_once, now);
                }
                if status.live.is_some() {
                    // The next turn began in the same breath. How the last one ended is told
                    // first: a client that waits for an agent to be done hears that it was.
                    hub.publish(
                        wire::state(&record, &rested, backend, self.ended.as_ref()),
                        now,
                    );
                    hub.publish(wire::activity(&record, &rested), now);
                }
            }
            hub.publish(
                wire::state(&record, &status, backend, self.ended.as_ref()),
                now,
            );
            hub.publish(wire::activity(&record, &status), now);
        }
        // A turn ended with an answer: the user is told, once.
        let answered = status
            .last
            .as_ref()
            .filter(|last| turn_ended && last.outcome == Outcome::Completed);
        // The answer is the row that closed the turn. It need not be the newest row: a message
        // that was left for later enters the conversation in the same breath.
        let answer = answered.and_then(|ended| {
            let end = self.ended.as_ref().filter(|end| end.turn == ended.id.0)?;
            let closed = actor.row(&end.closes).ok()??;
            matches!(closed.entry, Entry::Reply(_)).then_some((ended, closed))
        });
        if let Some((ended, reply)) = &answer {
            let asked = was_live
                .filter(|was| was.turn == ended.id)
                .map(|was| was.message_id.clone())
                .unwrap_or_default();
            let id = format!("pn-{}", ids::hex(6));
            hub.publish(
                wire::user_notification(&record, &id, &asked, reply, ended.ended_at_ms),
                now,
            );
        }
        self.told = Some(status);
        self.publish_view();
        if changed.conversation {
            let now = Instant::now();
            match self.transcript_sent {
                Some(sent) if now.duration_since(sent) < TRANSCRIPT_EVERY => {
                    self.transcript_due = Some(sent + TRANSCRIPT_EVERY);
                }
                _ => self.tell_transcript(),
            }
        }
    }

    fn tell_transcript(&mut self) {
        self.transcript_due = None;
        self.transcript_sent = Some(Instant::now());
        let now = self.now_ms();
        self.setup
            .hub
            .publish(wire::transcript_updated(&self.setup.record), now);
    }

    fn show_broken(&self, why: &str) {
        let mut view = (**self.view.borrow()).clone();
        view.broken = Some(why.to_string());
        self.view.send_replace(Arc::new(view));
    }
}

/// What a client is shown as an agent's state: which turn runs and what it does, or which
/// turn ended last. A change in any of it is told.
fn told_state(status: &Status) -> (Option<TurnId>, String, String, Option<TurnId>) {
    let live = status.live.as_ref();
    let tool = match live.map(|live| &live.activity) {
        Some(Activity::Tool { name }) => name.clone(),
        _ => String::new(),
    };
    (
        live.map(|live| live.turn),
        wire::state_name(&wire::state_kind(status)),
        tool,
        status.last.as_ref().map(|last| last.id),
    )
}

/// Passes what a turn's thread says on to the agent's thread.
fn tell(tx: &mpsc::Sender<Ask>, turn: TurnId, told: Told) {
    let ask = match told {
        Told::SpoolChanged => Ask::SpoolChanged,
        Told::Gone => Ask::ShimGone { turn },
        Told::Failed(reason) => Ask::LaunchFailed { turn, reason },
    };
    let _ = tx.send(ask);
}

/// Answers a command: the asker gets the result, the thread goes on with what follows from it.
fn answered<T>(
    result: Result<(T, Outputs), ActorError>,
    answer: Answer<T>,
) -> Result<Outputs, ActorError> {
    match result {
        Ok((value, out)) => {
            let _ = answer.send(Ok(value));
            Ok(out)
        }
        Err(error) => {
            let _ = answer.send(Err(unavailable(&error)));
            Err(error)
        }
    }
}

/// Answers a question that changes nothing.
fn read<T>(result: Result<T, ActorError>, answer: Answer<T>) -> Result<(), ActorError> {
    match result {
        Ok(value) => {
            let _ = answer.send(Ok(value));
            Ok(())
        }
        Err(error) => {
            let _ = answer.send(Err(unavailable(&error)));
            Err(error)
        }
    }
}

/// While an agent cannot be opened, whoever asks is told why instead of being left waiting.
/// Returns false when the agent was closed meanwhile.
fn refuse_for(rx: &mpsc::Receiver<Ask>, wait: Duration, why: &str) -> bool {
    let until = Instant::now() + wait;
    loop {
        let refusal = || Unavailable(format!("the agent cannot be opened: {why}"));
        match rx.recv_timeout(until.saturating_duration_since(Instant::now())) {
            Ok(Ask::Send { answer, .. }) => drop(answer.send(Err(refusal()))),
            Ok(Ask::Stop { answer, .. }) => drop(answer.send(Err(refusal()))),
            Ok(Ask::Resume { answer, .. }) => drop(answer.send(Err(refusal()))),
            Ok(Ask::Queue { answer }) => drop(answer.send(Err(refusal()))),
            Ok(Ask::Import { answer, .. }) => drop(answer.send(Err(refusal()))),
            Ok(Ask::Withdraw { answer, .. }) => drop(answer.send(Err(refusal()))),
            Ok(Ask::Tail { answer, .. } | Ask::After { answer, .. }) => {
                drop(answer.send(Err(refusal())));
            }
            Ok(Ask::Before { answer, .. }) => drop(answer.send(Err(refusal()))),
            Ok(Ask::SpoolChanged | Ask::ShimGone { .. } | Ask::LaunchFailed { .. }) => {}
            Ok(Ask::Close) | Err(RecvTimeoutError::Disconnected) => return false,
            Err(RecvTimeoutError::Timeout) => return true,
        }
    }
}
