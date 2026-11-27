//! The owner's sends a Host takes while their agent is still being opened (issue 935).
//!
//! An agent's log has one writer, its own thread, and that thread opens the agent before it
//! writes anything: at a start whose disk is slow, minutes. A plain message of the owner's that
//! comes meanwhile is checked against what the agent's log committed (its glance: a message it
//! has already, a trace another message owns) and then kept here, on disk, before it is
//! answered: in a store of its own (`accepted.db`, one writer thread), never in the agent's log.
//! The agent's thread takes every message kept for it, in the order they were kept, through its
//! ordinary send, before it reads anything else of its mailbox, and lets each go here only once
//! its own log has it (taken, or had already). Whatever else its send answers leaves it kept, and
//! the agent is not opened: nothing that was answered is lost. A message it already has (kept
//! again after a crash between the two writes, or sent again by its app) is one message: the
//! agent's send says it was had and does nothing. Until it is taken, the agent's `/log` and its
//! queue show it as its send will put it (`edge::with_kept`, `edge::queue`).
//!
//! What is kept is also held in memory, so that reading it (an early `/log`, an agent's opening)
//! never waits for the writer: a keep that waits for the disk holds only the sends behind it.
//! The store is there only while something is kept in it: made by the first keep, and removed
//! once the last kept send is let go. A start that finds no store has nothing kept, and no
//! agent's opening waits for it; one that finds a store (a crash left sends kept) reads it on
//! the writer's own thread, beside the start, and an agent's opening takes what was kept for it
//! once it is read. Only what the owner says in
//! a chat comes here: a call's words, an agent's message, the Host's own and a send whose agent
//! is open go the way they always went.

use std::collections::BTreeMap;
use std::sync::mpsc;
use std::sync::{Arc, Condvar, Mutex, OnceLock, PoisonError};

use clarp_kernel::conversation::Message;
use clarp_kernel::turn::Admission;
use clarp_ports::kv::KeyValue;
use serde::{Deserialize, Serialize};

/// The table the kept messages are in.
const TABLE: &str = "accepted";

/// A message kept for an agent that is still being opened.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Accepted {
    pub agent_id: String,
    pub message: Message,
    pub admission: Admission,
    /// What its owner asked it to do while the agent works (`while_busy`), as it was said.
    pub while_busy: Option<String>,
    /// Its caller named its trace: a trace another message of the agent has is refused.
    #[serde(default)]
    pub trace_named: bool,
    /// When it was kept, on the Host's clock.
    pub at_ms: i64,
}

impl Accepted {
    /// It as its agent's queue lists it while it waits there.
    pub fn waiting(&self) -> clarp_actor::Waiting {
        let message = &self.message;
        clarp_actor::Waiting {
            // Its turn is numbered when the agent takes it.
            turn: clarp_kernel::turn::TurnId(0),
            message_id: message.id.clone(),
            client_id: message.client_id().to_string(),
            told_id: message.told_id().to_string(),
            text: message.text.clone(),
            speak: message.speak,
            accepted_at_ms: self.at_ms,
            admission: self.admission,
            status: clarp_actor::QueueItemStatus::Queued,
            origin: message.origin.clone(),
            sender_agent_id: None,
            read_together: false,
        }
    }
}

/// Whether an agent as it stands takes a send at once (its turn starts with it): no turn runs,
/// nothing waits and its mailbox is not held. Else a send waits in its queue.
pub fn takes_at_once(status: &clarp_actor::Status) -> bool {
    status.live.is_none() && status.listed_queue_depth == 0 && !status.paused
}

/// What was kept.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Kept {
    /// Where it is kept.
    pub key: String,
    /// It was kept before (the same id for the same agent): nothing was written now.
    pub again: bool,
    /// The messages kept for the agent before it, still waiting.
    pub ahead: usize,
}

/// Why a message was not kept.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Refused {
    /// Its caller named a trace another message kept for the agent has.
    TraceTaken,
    /// The store did not keep it: nothing was answered as kept.
    Store(String),
}

type Answer<T> = mpsc::Sender<Result<T, Refused>>;

enum Command {
    Keep(Box<Accepted>, Answer<Kept>),
    /// Nobody waits for a letting go of its agent (`Desk::let_go`).
    Taken(String, String, Option<Answer<()>>),
}

/// What is kept, by agent, each agent's in the order it takes them.
type Index = BTreeMap<String, Vec<(String, Accepted)>>;

/// How the desk opens its store, again whenever it was removed.
pub type Opener = Box<dyn Fn() -> Result<Box<dyn KeyValue>, String> + Send>;

/// The desk: one thread writes the store; what is kept is read from memory.
pub struct Desk {
    tx: mpsc::Sender<Command>,
    index: Mutex<Option<Index>>,
    read: Condvar,
}

impl Desk {
    /// Starts the desk's thread. Where `there` (a store was left), the thread opens it with
    /// `open` and reads what it keeps before it takes anything; else nothing is kept, at once.
    /// `remove` takes a store that keeps nothing away.
    pub fn start(
        there: bool,
        open: Opener,
        remove: Box<dyn Fn() + Send>,
    ) -> Result<Arc<Self>, String> {
        let (tx, rx) = mpsc::channel();
        let desk = Arc::new(Self {
            tx,
            index: Mutex::new((!there).then(Index::new)),
            read: Condvar::new(),
        });
        let own = desk.clone();
        std::thread::Builder::new()
            .name("accepted-sends".to_string())
            .spawn(move || {
                let mut writer = Writer {
                    store: None,
                    open,
                    remove,
                    next: 1,
                    broken: false,
                };
                if there {
                    match writer.read() {
                        Ok(index) => own.loaded(index),
                        Err(why) => {
                            // Never lost: the store stays where it is, and is read again at the
                            // next start. No agent's opening waits for it meanwhile.
                            eprintln!(
                                "clarp-host: the store of accepted sends cannot be read: {why}"
                            );
                            own.loaded(Index::new());
                            writer.broken = true;
                        }
                    }
                }
                while let Ok(command) = rx.recv() {
                    writer.serve(command, &own);
                }
            })
            .map_err(|error| {
                format!("the thread of the accepted sends could not start: {error}")
            })?;
        Ok(desk)
    }

    fn loaded(&self, index: Index) {
        *self.index.lock().unwrap_or_else(PoisonError::into_inner) = Some(index);
        self.read.notify_all();
    }

    fn ask<T>(&self, make: impl FnOnce(Answer<T>) -> Command) -> Result<T, Refused> {
        let (answer, reply) = mpsc::channel();
        let closed = || Refused::Store("the store of accepted sends is closed".to_string());
        self.tx.send(make(answer)).map_err(|_| closed())?;
        reply.recv().map_err(|_| closed())?
    }

    /// Keeps a message for its agent, on disk before it answers. A message the agent has kept
    /// already (by its id) is not kept twice; one whose named trace another kept message has is
    /// refused.
    pub fn keep(&self, accepted: Accepted) -> Result<Kept, Refused> {
        self.ask(|answer| Command::Keep(Box::new(accepted), answer))
    }

    /// The messages kept for an agent, in the order it takes them, once the store has been
    /// read: what an agent's opening takes. Waits for that first reading only.
    pub fn waiting(&self, agent_id: &str) -> Vec<(String, Accepted)> {
        let held = self.index.lock().unwrap_or_else(PoisonError::into_inner);
        let held = self
            .read
            .wait_while(held, |index| index.is_none())
            .unwrap_or_else(PoisonError::into_inner);
        held.as_ref()
            .and_then(|index| index.get(agent_id).cloned())
            .unwrap_or_default()
    }

    /// The messages kept for an agent as far as the store has been read now: what an early
    /// `/log` shows. Never waits.
    pub fn waiting_now(&self, agent_id: &str) -> Vec<Accepted> {
        let held = self.index.lock().unwrap_or_else(PoisonError::into_inner);
        held.as_ref()
            .and_then(|index| index.get(agent_id))
            .map(|kept| kept.iter().map(|(_, accepted)| accepted.clone()).collect())
            .unwrap_or_default()
    }

    /// The agent's log has the message kept under `key`: it is let go here.
    pub fn taken(&self, agent_id: &str, key: &str) -> Result<(), Refused> {
        self.ask(|answer| Command::Taken(agent_id.to_string(), key.to_string(), Some(answer)))
    }

    /// Lets what an agent's log has go, in the background: an agent that answers from its own
    /// view never waits for a writer a keep may hold. Each is had once at a next opening until it
    /// is gone; one that cannot be let go is said.
    pub fn let_go(&self, agent_id: &str, keys: Vec<String>) {
        for key in keys {
            let _ = self
                .tx
                .send(Command::Taken(agent_id.to_string(), key, None));
        }
    }

    fn key_of(&self, agent_id: &str, id: &str) -> Option<String> {
        let held = self.index.lock().unwrap_or_else(PoisonError::into_inner);
        let kept = held.as_ref()?.get(agent_id)?;
        kept.iter()
            .find(|(_, accepted)| accepted.message.id == id)
            .map(|(key, _)| key.clone())
    }

    fn with_index<T>(&self, change: impl FnOnce(&mut Index) -> T) -> T {
        let mut held = self.index.lock().unwrap_or_else(PoisonError::into_inner);
        change(held.get_or_insert_with(Index::new))
    }
}

struct Writer {
    store: Option<Box<dyn KeyValue>>,
    open: Opener,
    remove: Box<dyn Fn() + Send>,
    /// The next place: above every place kept, so that an agent takes its messages in order.
    next: u64,
    /// A store that was left and could not be read: never written over or removed.
    broken: bool,
}

impl Writer {
    /// Opens the store that was left, and reads what it keeps.
    fn read(&mut self) -> Result<Index, String> {
        let store = (self.open)()?;
        let mut index = Index::new();
        let mut highest = 0;
        for (key, value) in store.list(TABLE).map_err(|error| error.to_string())? {
            let Some((agent_id, place)) = key.rsplit_once('/') else {
                continue;
            };
            highest = highest.max(place.parse::<u64>().unwrap_or(0));
            match serde_json::from_slice::<Accepted>(&value) {
                Ok(accepted) => index
                    .entry(agent_id.to_string())
                    .or_default()
                    .push((key, accepted)),
                // Never dropped: it stays on disk, said, and its agent takes the others.
                Err(_) => {
                    eprintln!("clarp-host: a kept send cannot be read and is left: {key}");
                    self.broken = true;
                }
            }
        }
        for kept in index.values_mut() {
            kept.sort_by(|(a, _), (b, _)| a.cmp(b));
        }
        self.next = highest + 1;
        self.store = Some(store);
        Ok(index)
    }

    fn store(&mut self) -> Result<&mut Box<dyn KeyValue>, Refused> {
        if self.broken {
            return Err(Refused::Store(
                "the store of accepted sends was left unread".to_string(),
            ));
        }
        if self.store.is_none() {
            self.store = Some((self.open)().map_err(Refused::Store)?);
        }
        self.store
            .as_mut()
            .ok_or_else(|| Refused::Store("no store".to_string()))
    }

    fn serve(&mut self, command: Command, desk: &Desk) {
        match command {
            Command::Keep(accepted, answer) => {
                let _ = answer.send(self.keep(*accepted, desk));
            }
            Command::Taken(agent_id, key, answer) => {
                let taken = self.store().and_then(|store| {
                    store
                        .delete(TABLE, &key)
                        .map(|_| ())
                        .map_err(|error| Refused::Store(error.to_string()))
                });
                if taken.is_ok() {
                    let empty = desk.with_index(|index| {
                        if let Some(kept) = index.get_mut(&agent_id) {
                            kept.retain(|(at, _)| *at != key);
                            if kept.is_empty() {
                                index.remove(&agent_id);
                            }
                        }
                        index.is_empty()
                    });
                    // Nothing is kept: the store goes, and a start that finds none reads none.
                    if empty && !self.broken {
                        self.store = None;
                        (self.remove)();
                    }
                }
                match answer {
                    Some(answer) => drop(answer.send(taken)),
                    None => {
                        if let Err(why) = taken {
                            eprintln!(
                                "clarp-host: a send kept for agent {agent_id} stays kept, and is \
                                 had at its next opening: {why:?}"
                            );
                        }
                    }
                }
            }
        }
    }

    fn keep(&mut self, accepted: Accepted, desk: &Desk) -> Result<Kept, Refused> {
        let kept = desk.waiting_now(&accepted.agent_id);
        if let Some(ahead) = kept
            .iter()
            .position(|had| had.message.id == accepted.message.id)
        {
            let key = desk
                .key_of(&accepted.agent_id, &accepted.message.id)
                .unwrap_or_default();
            return Ok(Kept {
                key,
                again: true,
                ahead,
            });
        }
        let trace = accepted.message.trace_id.as_deref().unwrap_or("");
        if accepted.trace_named
            && !trace.is_empty()
            && kept
                .iter()
                .any(|had| had.message.trace_id.as_deref() == Some(trace))
        {
            return Err(Refused::TraceTaken);
        }
        // A place of twenty digits: the keys order as the numbers do.
        let key = format!("{}/{:020}", accepted.agent_id, self.next);
        let value = serde_json::to_vec(&accepted).map_err(|e| Refused::Store(e.to_string()))?;
        self.store()?
            .put(TABLE, &key, &value)
            .map_err(|error| Refused::Store(error.to_string()))?;
        self.next += 1;
        let ahead = kept.len();
        desk.with_index(|index| {
            index
                .entry(accepted.agent_id.clone())
                .or_default()
                .push((key.clone(), accepted));
        });
        Ok(Kept {
            key,
            again: false,
            ahead,
        })
    }
}

static DESK: OnceLock<Arc<Desk>> = OnceLock::new();

/// Makes the desk of this process, whose store is the file at `path`, opened with `open`. Once.
pub fn install(path: &std::path::Path, open: Opener) -> Result<Arc<Desk>, String> {
    let file = path.to_path_buf();
    let remove = Box::new(move || {
        // The store's own files, its log first: what a removal cut short is a store whose
        // table is empty, read as nothing kept.
        for suffix in ["-wal", "-shm", ""] {
            let mut name = file.clone().into_os_string();
            name.push(suffix);
            let _ = std::fs::remove_file(std::path::PathBuf::from(name));
        }
    });
    let desk = Desk::start(path.exists(), open, remove)?;
    DESK.set(desk.clone())
        .map_err(|_| "the desk of accepted sends was installed twice".to_string())?;
    Ok(desk)
}

/// This process's desk: none in a process that installed none (a test's), where every send
/// goes to its agent's thread as before.
pub fn shared() -> Option<Arc<Desk>> {
    DESK.get().cloned()
}

/// The journal's name for a message: a short digest of its id, never its words. An app finds
/// its own by the same digest of the `client_msg_id` it sent (`u-` before an id without it).
pub fn mark(id: &str) -> String {
    use sha2::Digest as _;
    let digest = sha2::Sha256::digest(id.as_bytes());
    digest.iter().take(6).map(|b| format!("{b:02x}")).collect()
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    fn accepted(agent: &str, id: &str, trace: &str) -> Accepted {
        Accepted {
            agent_id: agent.to_string(),
            message: Message {
                id: id.to_string(),
                text: "words".to_string(),
                trace_id: Some(trace.to_string()),
                ..Message::default()
            },
            admission: Admission::Queue,
            while_busy: None,
            trace_named: true,
            at_ms: 1,
        }
    }

    fn open(path: &std::path::Path) -> Arc<Desk> {
        let (at, gone) = (path.to_path_buf(), path.to_path_buf());
        Desk::start(
            path.exists(),
            Box::new(move || {
                let tables = clarp_store_sqlite::SqliteKeyValue::new();
                clarp_ports::kv::KeyValueEngine::open(&tables, &at)
                    .map_err(|error| error.to_string())
            }),
            Box::new(move || {
                let _ = std::fs::remove_file(&gone);
            }),
        )
        .unwrap()
    }

    /// Issue 935: a message is kept once by its id, an agent's in the order they came, apart
    /// from another agent's; a named trace another kept message has is refused; what was kept is
    /// there for a desk that opens the store again, as after a crash; what was taken is gone.
    #[test]
    fn kept_once_in_order_and_still_there_after_a_crash() {
        let path = std::env::temp_dir().join(format!("clarp-accepted-{}.db", std::process::id()));
        let _ = std::fs::remove_file(&path);
        let desk = open(&path);
        let first = desk.keep(accepted("a", "m-1", "t-1")).unwrap();
        let other = desk.keep(accepted("b", "m-1", "t-1")).unwrap();
        let second = desk.keep(accepted("a", "m-2", "t-2")).unwrap();
        assert!(!first.again && !other.again && !second.again);
        assert_eq!((first.ahead, second.ahead), (0, 1));
        let again = desk.keep(accepted("a", "m-1", "t-1")).unwrap();
        assert_eq!(
            (again.key.as_str(), again.again, again.ahead),
            (first.key.as_str(), true, 0)
        );
        assert_eq!(
            desk.keep(accepted("a", "m-9", "t-1")),
            Err(Refused::TraceTaken),
            "a named trace another kept message has"
        );
        drop(desk);

        let desk = open(&path);
        let waiting = desk.waiting("a");
        let ids: Vec<&str> = waiting.iter().map(|(_, m)| m.message.id.as_str()).collect();
        assert_eq!(ids, ["m-1", "m-2"]);
        desk.taken("a", &waiting[0].0).unwrap();
        let third = desk.keep(accepted("a", "m-3", "t-3")).unwrap();
        assert_eq!(third.ahead, 1);
        assert!(third.key > waiting[1].0, "a later message is taken later");
        let ids: Vec<String> = desk
            .waiting_now("a")
            .into_iter()
            .map(|m| m.message.id)
            .collect();
        assert_eq!(ids, ["m-2", "m-3"]);
        assert_eq!(desk.waiting("b").len(), 1);
        drop(desk);
        let desk = open(&path);
        let ids: Vec<String> = desk
            .waiting("a")
            .into_iter()
            .map(|(_, m)| m.message.id)
            .collect();
        assert_eq!(ids, ["m-2", "m-3"], "the order holds across a reopening");
        // Once nothing is kept the store goes, and a start finds nothing to read. Let go in the
        // background, as an agent that answers lets go: the writer takes it in its order.
        let keys = desk.waiting("a").into_iter().map(|(key, _)| key).collect();
        desk.let_go("a", keys);
        for (key, _) in desk.waiting("b") {
            desk.taken("b", &key).unwrap();
        }
        assert!(!path.exists(), "a store that keeps nothing is removed");
        drop(desk);
        let desk = open(&path);
        assert!(desk.waiting("a").is_empty());
        assert!(!desk.keep(accepted("a", "m-4", "t-4")).unwrap().again);
        assert!(path.exists(), "the next keep makes it again");
        let _ = std::fs::remove_file(&path);
    }

    /// A store that cannot be opened keeps nothing and answers so: nothing is answered as kept,
    /// and an agent's opening is not held by it.
    #[test]
    fn a_store_that_cannot_be_opened_keeps_nothing_and_holds_nobody() {
        let desk = Desk::start(
            true,
            Box::new(|| Err("no disk".to_string())),
            Box::new(|| {}),
        )
        .unwrap();
        assert!(desk.waiting("a").is_empty());
        assert!(matches!(
            desk.keep(accepted("a", "m-1", "t-1")),
            Err(Refused::Store(_))
        ));
    }
}
