//! The agent and its log together: write first, then change.
//!
//! [`Recorder`] is the agent's state with the log it is rebuilt from. A command is decided, its
//! events are appended to the log in one transaction, and only when that append has succeeded is
//! the state changed and the effects handed out. If the append fails, nothing happened: the state
//! is as it was and the caller is told. Opening a recorder replays the log, so a process that died
//! at any point comes back in the state of its last successful append.

use clarp_ports::log::{AgentLog, LogError, NewItem, Seq};

use crate::agent::{Agent, AgentEvent, Command, Effect, Refusal};
use crate::turn::{IllegalTransition, Origin};

/// What an item in an agent's log is. The numbers are permanent once a log holds them.
pub mod kind {
    /// An [`AgentEvent`](crate::agent::AgentEvent) as JSON.
    pub const AGENT_EVENT: u16 = 1;
    /// A message to the agent, as the bytes the sender gave.
    pub const MESSAGE: u16 = 10;
}

const PAGE: usize = 1_000;

#[derive(Debug)]
pub enum JournalError {
    Log(LogError),
    /// An item that should hold an event does not.
    Corrupt {
        seq: Seq,
        why: String,
    },
    /// The log holds a history that cannot have happened.
    Illegal {
        seq: Seq,
        transition: IllegalTransition,
    },
    Refused(Refusal),
}

impl std::fmt::Display for JournalError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Log(error) => write!(f, "{error}"),
            Self::Corrupt { seq, why } => write!(f, "log item {seq} is not an event: {why}"),
            Self::Illegal { seq, transition } => write!(f, "log item {seq}: {transition}"),
            Self::Refused(refusal) => write!(f, "{refusal}"),
        }
    }
}

impl std::error::Error for JournalError {}

impl From<LogError> for JournalError {
    fn from(error: LogError) -> Self {
        Self::Log(error)
    }
}

/// What a message did: where it is in the log, and what the actor must now do.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Accepted {
    pub message_seq: Seq,
    pub effects: Vec<Effect>,
}

pub struct Recorder {
    log: Box<dyn AgentLog>,
    agent: Agent,
}

fn event_item(event: &AgentEvent, turn: Option<u64>, ts_ms: i64) -> Result<NewItem, JournalError> {
    let body = serde_json::to_vec(event).map_err(|e| JournalError::Corrupt {
        seq: 0,
        why: e.to_string(),
    })?;
    Ok(NewItem {
        turn,
        kind: kind::AGENT_EVENT,
        ts_ms,
        body,
    })
}

fn turn_of(event: &AgentEvent) -> Option<u64> {
    match event {
        AgentEvent::Turn { turn, .. } | AgentEvent::StopRequested { turn, .. } => Some(turn.0),
        AgentEvent::Paused { .. } | AgentEvent::Resumed { .. } => None,
    }
}

impl Recorder {
    /// Opens an agent from its log: the state is whatever its history says.
    pub fn open(log: Box<dyn AgentLog>) -> Result<Self, JournalError> {
        let mut agent = Agent::new();
        let mut after = 0;
        loop {
            let page = log.read_after(after, PAGE)?;
            let Some(last) = page.last() else { break };
            after = last.seq;
            for item in &page {
                if item.kind != kind::AGENT_EVENT {
                    continue;
                }
                let event: AgentEvent =
                    serde_json::from_slice(&item.body).map_err(|e| JournalError::Corrupt {
                        seq: item.seq,
                        why: e.to_string(),
                    })?;
                agent
                    .apply(&event)
                    .map_err(|transition| JournalError::Illegal {
                        seq: item.seq,
                        transition,
                    })?;
            }
        }
        Ok(Self { log, agent })
    }

    pub fn agent(&self) -> &Agent {
        &self.agent
    }

    /// Records a command's events, then applies them. Returns the effects to carry out.
    pub fn handle(&mut self, command: Command, now_ms: i64) -> Result<Vec<Effect>, JournalError> {
        let decision = self
            .agent
            .decide(command, now_ms)
            .map_err(JournalError::Refused)?;
        let mut items = Vec::with_capacity(decision.events.len());
        for event in &decision.events {
            items.push(event_item(event, turn_of(event), now_ms)?);
        }
        self.commit(&items, &decision.events)?;
        Ok(decision.effects)
    }

    /// Accepts a message: the message and the turn it becomes are written in one transaction, so
    /// there is never a message without its turn or a turn without its message.
    pub fn accept(
        &mut self,
        body: Vec<u8>,
        origin: Origin,
        now_ms: i64,
    ) -> Result<Accepted, JournalError> {
        let message_seq = self.log.last_seq()? + 1;
        let decision = self
            .agent
            .decide(
                Command::Accept {
                    message_seq,
                    origin,
                },
                now_ms,
            )
            .map_err(JournalError::Refused)?;
        let mut items = vec![NewItem {
            turn: None,
            kind: kind::MESSAGE,
            ts_ms: now_ms,
            body,
        }];
        for event in &decision.events {
            items.push(event_item(event, turn_of(event), now_ms)?);
        }
        self.commit(&items, &decision.events)?;
        Ok(Accepted {
            message_seq,
            effects: decision.effects,
        })
    }

    fn commit(&mut self, items: &[NewItem], events: &[AgentEvent]) -> Result<(), JournalError> {
        if items.is_empty() {
            return Ok(());
        }
        let first_seq = self.log.last_seq()? + 1;
        self.log.append(items)?;
        // The events are durable. From here the state must follow them; an event that was just
        // decided on this very state and does not apply means the state machine is wrong.
        for event in events {
            self.agent
                .apply(event)
                .map_err(|transition| JournalError::Illegal {
                    seq: first_seq,
                    transition,
                })?;
        }
        Ok(())
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use std::path::Path;
    use std::sync::Arc;
    use std::sync::atomic::{AtomicUsize, Ordering};

    use clarp_codec::standard_set;
    use clarp_ports::log::{Item, LogEngine};
    use clarp_store_mem::MemEngine;

    use super::*;
    use crate::turn::{Activity, RunOutcome, TurnId};

    fn open(engine: &MemEngine) -> Recorder {
        Recorder::open(
            engine
                .open(Path::new("/agents/a"), standard_set("zstd").unwrap())
                .unwrap(),
        )
        .unwrap()
    }

    /// The commands of a small working life, each with what a crash right after it must preserve.
    /// One step of a script: a command applied to the recorder at a time.
    type Step = Box<dyn Fn(&mut Recorder, i64) -> Result<Vec<Effect>, JournalError>>;

    fn script() -> Vec<Step> {
        vec![
            Box::new(|r, t| {
                r.accept(b"first".to_vec(), Origin::User, t)
                    .map(|a| a.effects)
            }),
            Box::new(|r, t| {
                r.accept(b"second".to_vec(), Origin::User, t)
                    .map(|a| a.effects)
            }),
            Box::new(|r, t| r.handle(Command::TurnStarted { turn: TurnId(1) }, t)),
            Box::new(|r, t| {
                r.handle(
                    Command::TurnMoved {
                        turn: TurnId(1),
                        activity: Activity::Tool {
                            name: "Bash".to_string(),
                        },
                    },
                    t,
                )
            }),
            Box::new(|r, t| {
                r.accept(
                    b"from another agent".to_vec(),
                    Origin::Agent {
                        agent_id: "arnold".to_string(),
                    },
                    t,
                )
                .map(|a| a.effects)
            }),
            Box::new(|r, t| {
                r.handle(
                    Command::Stop {
                        by: "peter".to_string(),
                    },
                    t,
                )
            }),
            Box::new(|r, t| {
                r.handle(
                    Command::TurnEnded {
                        turn: TurnId(1),
                        outcome: RunOutcome::Completed,
                    },
                    t,
                )
            }),
            Box::new(|r, t| {
                r.accept(b"carry on".to_vec(), Origin::User, t)
                    .map(|a| a.effects)
            }),
            Box::new(|r, t| {
                r.handle(
                    Command::TurnEnded {
                        turn: TurnId(2),
                        outcome: RunOutcome::Completed,
                    },
                    t,
                )
            }),
            Box::new(|r, t| {
                r.handle(
                    Command::TurnEnded {
                        turn: TurnId(3),
                        outcome: RunOutcome::Failed {
                            reason: "limit".to_string(),
                        },
                    },
                    t,
                )
            }),
            Box::new(|r, t| {
                r.handle(
                    Command::TurnEnded {
                        turn: TurnId(4),
                        outcome: RunOutcome::Completed,
                    },
                    t,
                )
            }),
        ]
    }

    #[test]
    fn a_crash_after_any_command_comes_back_in_the_same_state() {
        let engine = MemEngine::new();
        let mut recorder = open(&engine);
        for (step, command) in script().iter().enumerate() {
            command(&mut recorder, 1_000 + step as i64)
                .unwrap_or_else(|e| panic!("step {step}: {e}"));
            let before = recorder.agent().clone();
            drop(recorder); // the process dies here
            recorder = open(&engine);
            assert_eq!(
                recorder.agent(),
                &before,
                "after a crash following step {step}"
            );
        }
        assert_eq!(
            recorder.agent().settled(),
            4,
            "all four messages reached an end"
        );
        assert!(recorder.agent().live().is_none() && recorder.agent().waiting().next().is_none());
    }

    #[test]
    fn a_message_and_its_turn_are_one_write() {
        let engine = MemEngine::new();
        let mut recorder = open(&engine);
        let accepted = recorder.accept(b"hello".to_vec(), Origin::User, 5).unwrap();
        assert_eq!(accepted.message_seq, 1);
        assert_eq!(
            accepted.effects,
            vec![Effect::StartTurn {
                turn: TurnId(1),
                message_seq: 1
            }]
        );
        let log = engine
            .open(Path::new("/agents/a"), standard_set("zstd").unwrap())
            .unwrap();
        let items = log.read_after(0, 10).unwrap();
        let kinds: Vec<u16> = items.iter().map(|item| item.kind).collect();
        assert_eq!(
            kinds,
            vec![kind::MESSAGE, kind::AGENT_EVENT, kind::AGENT_EVENT],
            "the message, its acceptance, its start"
        );
        assert_eq!(items[0].body, b"hello");
        assert_eq!(items[1].turn, Some(1));
    }

    /// A log whose next append fails, as a full disk would make it.
    struct Failing {
        inner: Box<dyn AgentLog>,
        appends_left: Arc<AtomicUsize>,
    }

    impl AgentLog for Failing {
        fn append(&mut self, items: &[NewItem]) -> Result<Seq, LogError> {
            if self.appends_left.load(Ordering::SeqCst) == 0 {
                return Err(LogError::Storage("no space left on device".to_string()));
            }
            self.appends_left.fetch_sub(1, Ordering::SeqCst);
            self.inner.append(items)
        }
        fn last_seq(&self) -> Result<Seq, LogError> {
            self.inner.last_seq()
        }
        fn read_after(&self, after: Seq, limit: usize) -> Result<Vec<Item>, LogError> {
            self.inner.read_after(after, limit)
        }
        fn read_tail(&self, limit: usize) -> Result<Vec<Item>, LogError> {
            self.inner.read_tail(limit)
        }
    }

    #[test]
    fn when_the_log_cannot_be_written_nothing_happened() {
        let engine = MemEngine::new();
        let appends_left = Arc::new(AtomicUsize::new(1));
        let inner = engine
            .open(Path::new("/agents/a"), standard_set("zstd").unwrap())
            .unwrap();
        let mut recorder = Recorder::open(Box::new(Failing {
            inner,
            appends_left: appends_left.clone(),
        }))
        .unwrap();
        recorder.accept(b"first".to_vec(), Origin::User, 1).unwrap();
        let before = recorder.agent().clone();

        let refused = recorder.accept(b"second".to_vec(), Origin::User, 2);
        assert!(
            matches!(refused, Err(JournalError::Log(_))),
            "the caller is told the message was not accepted"
        );
        assert_eq!(recorder.agent(), &before, "and the agent is as it was");
        assert_eq!(
            open(&engine).agent(),
            &before,
            "on disk too: no half of the message was kept"
        );

        appends_left.store(10, Ordering::SeqCst);
        let accepted = recorder
            .accept(b"second, again".to_vec(), Origin::User, 3)
            .unwrap();
        assert_eq!(
            accepted.effects,
            vec![],
            "the first turn is still live; the retried message waits"
        );
        assert_eq!(
            recorder.agent().waiting().count(),
            1,
            "and it was accepted once, not twice"
        );
    }

    #[test]
    fn a_refused_command_writes_nothing() {
        let engine = MemEngine::new();
        let mut recorder = open(&engine);
        let refused = recorder.handle(
            Command::TurnEnded {
                turn: TurnId(9),
                outcome: RunOutcome::Completed,
            },
            1,
        );
        assert!(matches!(
            refused,
            Err(JournalError::Refused(Refusal::NotLive(TurnId(9))))
        ));
        let log = engine
            .open(Path::new("/agents/a"), standard_set("zstd").unwrap())
            .unwrap();
        assert_eq!(log.last_seq().unwrap(), 0);
    }

    #[test]
    fn a_log_that_cannot_have_happened_is_refused_at_open() {
        let engine = MemEngine::new();
        let mut log = engine
            .open(Path::new("/agents/a"), standard_set("zstd").unwrap())
            .unwrap();
        let impossible = AgentEvent::Turn {
            turn: TurnId(1),
            change: crate::turn::TurnEvent::Started { at_ms: 1 },
        };
        log.append(&[event_item(&impossible, Some(1), 1).unwrap()])
            .unwrap();
        log.append(&[NewItem {
            turn: None,
            kind: kind::AGENT_EVENT,
            ts_ms: 2,
            body: b"not json".to_vec(),
        }])
        .unwrap();
        drop(log);
        let opened = Recorder::open(
            engine
                .open(Path::new("/agents/a"), standard_set("zstd").unwrap())
                .unwrap(),
        );
        assert!(
            matches!(opened, Err(JournalError::Illegal { seq: 1, .. })),
            "a start with no acceptance cannot have happened"
        );
    }
}
