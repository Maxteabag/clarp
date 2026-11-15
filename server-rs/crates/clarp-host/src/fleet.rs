//! The fleet: which agents this Host has.
//!
//! One thread owns the list. It is the only writer of the fleet's tables, and it publishes the
//! list as a whole whenever it changes, so that everyone else reads a list that is complete and
//! never waits for the fleet to find an agent.

use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::sync::mpsc;

use clarp_actor::Profile;
use clarp_ports::clock::Clock;
use clarp_ports::codec::CodecSet;
use clarp_ports::kv::KeyValue;
use clarp_ports::log::LogEngine;
use clarp_protocol::events::{AgentFocus, AgentRoster, Event};
use clarp_protocol::requests::CreateAgentRequest;
use serde::{Deserialize, Serialize};
use tokio::sync::{oneshot, watch};

use crate::agent::{self, AgentHandle, Answer, Setup, Unavailable};
use crate::config::Config;
use crate::hub::Hub;
use crate::ids;
use crate::launcher::Launcher;

const AGENTS: &str = "agent";
const SETTINGS: &str = "setting";
const FOCUS: &str = "focus";

/// How long a tool gets to end by itself when its turn is stopped.
const STOP_GRACE_MS: u64 = 5_000;

/// What the fleet knows about an agent: who it is, not what it is doing.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AgentRecord {
    pub agent_id: String,
    /// The agent's name in every request. Unique among the agents of a Host.
    pub session: String,
    /// The agent's name as a person reads it.
    pub persona: String,
    pub backend: String,
    pub cwd: String,
    #[serde(default)]
    pub model: String,
    #[serde(default)]
    pub effort: String,
    /// Changes when the agent starts a new conversation: clients then drop what they cached.
    pub conversation_id: String,
    pub created_at_ms: i64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub deleted_at_ms: Option<i64>,
}

/// An agent of the fleet: who it is and the way to its thread.
pub struct Member {
    pub record: AgentRecord,
    pub agent: AgentHandle,
}

/// The fleet as a whole, as it was when it last changed.
#[derive(Default)]
pub struct Directory {
    pub members: Vec<Arc<Member>>,
    /// The agent that is open, by id.
    pub focus: Option<String>,
}

impl Directory {
    pub fn by_session(&self, session: &str) -> Option<&Arc<Member>> {
        self.members
            .iter()
            .find(|member| member.record.session == session)
    }
}

/// What every agent of a Host is given.
pub struct Works {
    pub config: Arc<Config>,
    pub engine: Arc<dyn LogEngine>,
    pub codecs: CodecSet,
    pub launcher: Arc<Launcher>,
    pub clock: Arc<dyn Clock + Send + Sync>,
    pub hub: Arc<Hub>,
}

enum Ask {
    /// The inner error is a refusal: the request cannot be met, and says why.
    Create {
        request: CreateAgentRequest,
        answer: Answer<Result<AgentRecord, String>>,
    },
    Remove {
        session: String,
        answer: Answer<bool>,
    },
    Select {
        session: String,
        answer: Answer<Option<AgentRecord>>,
    },
}

/// The way to the fleet's thread, and the fleet as anyone may read it.
#[derive(Clone)]
pub struct FleetHandle {
    tx: mpsc::Sender<Ask>,
    directory: watch::Receiver<Arc<Directory>>,
}

impl FleetHandle {
    /// The fleet as it is now.
    pub fn directory(&self) -> Arc<Directory> {
        self.directory.borrow().clone()
    }

    async fn ask<T>(&self, make: impl FnOnce(Answer<T>) -> Ask) -> Result<T, Unavailable> {
        let (answer, reply) = oneshot::channel();
        self.tx
            .send(make(answer))
            .map_err(|_| Unavailable("the fleet is closed".to_string()))?;
        reply
            .await
            .map_err(|_| Unavailable("the fleet did not answer".to_string()))?
    }

    /// Adds an agent. The inner error says why the request cannot be met.
    pub async fn create(
        &self,
        request: CreateAgentRequest,
    ) -> Result<Result<AgentRecord, String>, Unavailable> {
        self.ask(|answer| Ask::Create { request, answer }).await
    }

    /// Removes an agent from the fleet. Its history stays on disk. Returns whether it was there.
    pub async fn remove(&self, session: String) -> Result<bool, Unavailable> {
        self.ask(|answer| Ask::Remove { session, answer }).await
    }

    /// Makes an agent the one that is open. `None` when there is no such agent.
    pub async fn select(&self, session: String) -> Result<Option<AgentRecord>, Unavailable> {
        self.ask(|answer| Ask::Select { session, answer }).await
    }
}

struct Fleet {
    works: Works,
    store: Box<dyn KeyValue>,
    members: Vec<Arc<Member>>,
    focus: Option<String>,
    directory: watch::Sender<Arc<Directory>>,
}

fn non_empty(text: &str) -> Option<String> {
    Some(text.trim().to_string()).filter(|text| !text.is_empty())
}

fn setup(works: &Works, record: &AgentRecord) -> Result<Setup, String> {
    let backend = clarp_backend::by_id(&record.backend)
        .ok_or_else(|| format!("this Host has no backend named `{}`", record.backend))?;
    let profile = Profile {
        agent_id: record.agent_id.clone(),
        session: record.session.clone(),
        dir: works.config.data_dir.join("agents").join(&record.agent_id),
        run_dir: works.config.run_dir.clone(),
        cwd: PathBuf::from(&record.cwd),
        model: non_empty(&record.model),
        effort: non_empty(&record.effort),
        identity: None,
        stop_grace_ms: STOP_GRACE_MS,
        keep_spools: works.config.keep_spools,
    };
    Ok(Setup {
        record: record.clone(),
        profile,
        backend,
        engine: works.engine.clone(),
        codecs: works.codecs.clone(),
        launcher: works.launcher.clone(),
        clock: works.clock.clone(),
        hub: works.hub.clone(),
    })
}

/// Opens the fleet from its store, starts a thread for every agent in it, and returns the way
/// to the fleet's own thread.
pub fn start(works: Works, store: Box<dyn KeyValue>) -> Result<FleetHandle, String> {
    let mut members = Vec::new();
    for (agent_id, stored) in store.list(AGENTS).map_err(|error| error.to_string())? {
        let record: AgentRecord = serde_json::from_slice(&stored).map_err(|error| {
            format!("the fleet's entry for agent {agent_id} cannot be read: {error}")
        })?;
        if record.deleted_at_ms.is_some() {
            continue;
        }
        match setup(&works, &record) {
            Ok(setup) => members.push(Arc::new(Member {
                record,
                agent: agent::spawn(setup),
            })),
            // The agent stays in the store; a Host that has its backend will run it.
            Err(why) => eprintln!("clarp-host: agent {} is left out: {why}", record.session),
        }
    }
    let focus = store
        .get(SETTINGS, FOCUS)
        .map_err(|error| error.to_string())?
        .and_then(|stored| String::from_utf8(stored).ok())
        .filter(|agent_id| {
            members
                .iter()
                .any(|member| member.record.agent_id == *agent_id)
        });
    let (directory, read) = watch::channel(Arc::new(Directory {
        members: members.clone(),
        focus: focus.clone(),
    }));
    let (tx, rx) = mpsc::channel();
    let fleet = Fleet {
        works,
        store,
        members,
        focus,
        directory,
    };
    std::thread::Builder::new()
        .name("fleet".to_string())
        .spawn(move || fleet.run(&rx))
        .map_err(|error| format!("the fleet's thread could not be started: {error}"))?;
    Ok(FleetHandle {
        tx,
        directory: read,
    })
}

impl Fleet {
    fn run(mut self, rx: &mpsc::Receiver<Ask>) {
        for ask in rx {
            match ask {
                Ask::Create { request, answer } => {
                    let _ = answer.send(Ok(self.create(request)));
                }
                Ask::Remove { session, answer } => {
                    let _ = answer.send(self.remove(&session));
                }
                Ask::Select { session, answer } => {
                    let _ = answer.send(self.select(&session));
                }
            }
        }
    }

    fn now_ms(&self) -> i64 {
        self.works.clock.now_ms()
    }

    fn publish(&self) {
        self.directory.send_replace(Arc::new(Directory {
            members: self.members.clone(),
            focus: self.focus.clone(),
        }));
    }

    fn tell_roster(&self, kind: &str, session: &str) {
        let event = Event::AgentRoster(AgentRoster {
            kind: Some(kind.to_string()),
            session: Some(session.to_string()),
        });
        self.works.hub.publish(event, self.now_ms());
    }

    fn taken(&self, session: &str) -> bool {
        self.members
            .iter()
            .any(|member| member.record.session == session)
    }

    fn create(&mut self, request: CreateAgentRequest) -> Result<AgentRecord, String> {
        let persona = request.name.trim().to_string();
        if persona.is_empty() {
            return Err("an agent needs a name".to_string());
        }
        let cwd = request
            .cwd
            .as_deref()
            .and_then(non_empty)
            .unwrap_or_else(|| self.works.config.default_cwd.display().to_string());
        if !Path::new(&cwd).is_dir() {
            return Err(format!("{cwd} is not a directory"));
        }
        let base = ids::slug(&persona);
        let mut session = base.clone();
        while self.taken(&session) {
            session = format!("{base}-{}", ids::hex(2));
        }
        let record = AgentRecord {
            agent_id: ids::hex(8),
            session,
            persona,
            backend: request
                .backend
                .as_deref()
                .and_then(non_empty)
                .unwrap_or_else(|| "claude".to_string()),
            cwd,
            model: request.model.unwrap_or_default(),
            effort: request.effort.unwrap_or_default(),
            conversation_id: ids::uuid(),
            created_at_ms: self.now_ms(),
            deleted_at_ms: None,
        };
        // Refused before anything is written: an agent whose backend this Host lacks.
        let setup = setup(&self.works, &record)?;
        self.put(&record)?;
        self.members.push(Arc::new(Member {
            record: record.clone(),
            agent: agent::spawn(setup),
        }));
        self.publish();
        self.tell_roster("created", &record.session);
        Ok(record)
    }

    fn put(&mut self, record: &AgentRecord) -> Result<(), String> {
        let stored = serde_json::to_vec(record).map_err(|error| error.to_string())?;
        self.store
            .put(AGENTS, &record.agent_id, &stored)
            .map_err(|error| error.to_string())
    }

    fn remove(&mut self, session: &str) -> Result<bool, Unavailable> {
        let Some(at) = self
            .members
            .iter()
            .position(|member| member.record.session == session)
        else {
            return Ok(false);
        };
        let mut record = self.members[at].record.clone();
        record.deleted_at_ms = Some(self.now_ms());
        self.put(&record).map_err(Unavailable)?;
        let member = self.members.remove(at);
        member.agent.close();
        if self.focus.as_deref() == Some(record.agent_id.as_str()) {
            self.focus = None;
            let _ = self.store.delete(SETTINGS, FOCUS);
        }
        self.publish();
        self.tell_roster("deleted", session);
        Ok(true)
    }

    fn select(&mut self, session: &str) -> Result<Option<AgentRecord>, Unavailable> {
        let Some(record) = self
            .members
            .iter()
            .find(|member| member.record.session == session)
            .map(|member| member.record.clone())
        else {
            return Ok(None);
        };
        self.store
            .put(SETTINGS, FOCUS, record.agent_id.as_bytes())
            .map_err(|error| Unavailable(error.to_string()))?;
        self.focus = Some(record.agent_id.clone());
        self.publish();
        let event = Event::AgentFocus(AgentFocus {
            session: Some(record.session.clone()),
            agent_id: Some(record.agent_id.clone()),
        });
        self.works.hub.publish(event, self.now_ms());
        Ok(Some(record))
    }
}
