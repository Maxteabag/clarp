//! Message search (issue 803): `GET /search` and `clarp-admin search` over the conversations
//! the caller may read, from an index kept as the agents' rows change.
//!
//! The index is `search.db` beside the fleet's file. One thread of its own writes it
//! ([`Desk`]); each search reads it on a read-only connection of its own. Nothing an agent
//! writes waits for it: no agent's append and no fsync of an agent's log touches it. What
//! changed is read off the hub, which says `transcript-updated` with the conversation's head;
//! the indexer then asks the agent for the rows that changed since the revision it holds, the
//! same page an app asks for on that event (`after`), and keeps them. The rows are read through
//! the actor, not from its file, so they are the rows `/log` serves: the same ids, the same
//! text, the same view, the same beginning.
//!
//! History from before is read in the background once the Host is up: one page at a time, at
//! most one every [`PACE`], and of an agent only while it has no turn running and nothing
//! waiting, so a working agent's thread is never kept from its work and the disk never sees a
//! burst (on 25 Nov 2026 a laptop's evening stalls were filesystem-wide fsyncs). A search
//! meanwhile answers what is indexed and says `partial`. Where each agent stands is kept with
//! its rows, so a restart resumes; an index found damaged is thrown away and read anew.
//!
//! Who may read what is decided when a search is made, not when a row is kept: only agents in
//! the fleet now, here and not moved away, and whose words the reader may read
//! (`bound_privacy`) are searched. So a removed agent, a moved one or a bound agent withheld
//! from the reader is never matched, counted or shown, also before its rows leave the index.

use std::collections::{BTreeMap, HashMap, HashSet};
use std::path::PathBuf;
use std::sync::{Arc, mpsc};
use std::time::Duration;

use axum::Json;
use axum::extract::{Extension, Query, State};
use axum::http::{HeaderMap, StatusCode};
use clarp_actor::conversation::{Page, Versioned, Which};
use clarp_store_sqlite::search::{Asked, Found, Mark, Row, SearchError, SearchIndex, SearchReader};
use serde::Deserialize;
use serde_json::{Value, json};
use tokio::sync::{Semaphore, broadcast, oneshot};
use tokio::time::Instant;

use crate::bound_privacy::{Reader, Withheld};
use crate::edge::{ApiError, App, Principal};
use crate::fleet::Member;
use crate::wire;

/// The file of the index, in the Host's data folder.
pub const FILE: &str = "search.db";
/// How many rows one read of an agent asks for.
const PAGE: usize = 200;
/// The least time between two reads of one agent's changes: a reply that grows is told a few
/// times a second, and is kept once it has settled.
const EVERY: Duration = Duration::from_secs(2);
/// The least time between two pages of history read in the background.
const PACE: Duration = Duration::from_millis(250);
/// The pause after a read of an agent's changes.
const BREATH: Duration = Duration::from_millis(50);
/// How long after the Host is up the index begins: what a start does first comes first.
const AFTER_START: Duration = Duration::from_secs(5);
/// How many hits a page has when the caller does not say.
const LIMIT: usize = 20;
/// How many searches read at once.
const READERS: usize = 4;

// ---- the index's own thread ------------------------------------------------------------------

type Answer<T> = oneshot::Sender<Result<T, SearchError>>;

enum Command {
    Marks(Answer<HashMap<String, Mark>>),
    Apply {
        agent_id: String,
        rows: Vec<Row>,
        mark: Mark,
        answer: Answer<()>,
    },
    Forget {
        agent_id: String,
        answer: Answer<()>,
    },
    /// A reader found the file damaged: it is thrown away and made anew.
    Discard,
}

/// The way to the index: its writer's thread, and the file searches read.
pub struct Desk {
    tx: mpsc::Sender<Command>,
    path: PathBuf,
    readers: Arc<Semaphore>,
}

impl Desk {
    pub fn new(path: PathBuf) -> Self {
        let (tx, rx) = mpsc::channel::<Command>();
        let at = path.clone();
        let spawned = std::thread::Builder::new()
            .name("search-index".to_string())
            .spawn(move || Writer::new(at).serve(&rx));
        if let Err(why) = spawned {
            eprintln!("clarp-host: the search index has no thread: {why}");
        }
        Self {
            tx,
            path,
            readers: Arc::new(Semaphore::new(READERS)),
        }
    }

    async fn ask<T>(&self, make: impl FnOnce(Answer<T>) -> Command) -> Result<T, SearchError> {
        let (answer, reply) = oneshot::channel();
        self.tx
            .send(make(answer))
            .map_err(|_| SearchError::Storage("the search index is closed".to_string()))?;
        reply
            .await
            .map_err(|_| SearchError::Storage("the search index did not answer".to_string()))?
    }

    async fn marks(&self) -> Result<HashMap<String, Mark>, SearchError> {
        self.ask(Command::Marks).await
    }

    async fn apply(&self, agent_id: &str, rows: Vec<Row>, mark: Mark) -> Result<(), SearchError> {
        let agent_id = agent_id.to_string();
        self.ask(|answer| Command::Apply {
            agent_id,
            rows,
            mark,
            answer,
        })
        .await
    }

    async fn forget(&self, agent_id: &str) -> Result<(), SearchError> {
        let agent_id = agent_id.to_string();
        self.ask(|answer| Command::Forget { agent_id, answer })
            .await
    }

    /// A search, on a read-only connection of its own, off the runtime.
    async fn search(
        &self,
        words: String,
        agents: Vec<String>,
        limit: usize,
        before: Option<String>,
    ) -> Result<Found, SearchError> {
        let _turn = self
            .readers
            .clone()
            .acquire_owned()
            .await
            .map_err(|_| SearchError::Storage("the search index is closed".to_string()))?;
        let path = self.path.clone();
        let found = tokio::task::spawn_blocking(move || {
            // Before the writer has made the file there is nothing yet: everything is to come.
            let Some(reader) = SearchReader::open(&path)? else {
                return Ok(Found {
                    partial: true,
                    ..Found::default()
                });
            };
            reader.search(Asked {
                words: &words,
                agents: &agents,
                limit,
                before: before.as_deref(),
            })
        })
        .await
        .map_err(|why| SearchError::Storage(why.to_string()))?;
        if matches!(found, Err(SearchError::Broken(_))) {
            let _ = self.tx.send(Command::Discard);
        }
        found
    }
}

/// The index's one writer. A file that is damaged is thrown away and made anew; the next
/// answer to the indexer says so once ([`SearchError::Broken`]), so it reads everything again.
struct Writer {
    path: PathBuf,
    index: Option<SearchIndex>,
    began_anew: bool,
}

impl Writer {
    fn new(path: PathBuf) -> Self {
        let mut writer = Self {
            path,
            index: None,
            began_anew: false,
        };
        // Made at once, so a search finds a file to read from the start.
        let _ = writer.index();
        writer
    }

    fn index(&mut self) -> Result<&mut SearchIndex, SearchError> {
        if self.index.is_none() {
            let (index, anew) = SearchIndex::open(&self.path)?;
            self.began_anew |= anew;
            self.index = Some(index);
        }
        self.index
            .as_mut()
            .ok_or_else(|| SearchError::Storage("the search index did not open".to_string()))
    }

    fn discard(&mut self, why: &str) {
        eprintln!("clarp-host: the search index is made anew: {why}");
        self.index = None;
        clarp_store_sqlite::search::discard(&self.path);
        self.began_anew = true;
    }

    /// What one command answers, with what a damaged file means for it.
    fn done<T>(&mut self, done: Result<T, SearchError>) -> Result<T, SearchError> {
        if let Err(SearchError::Broken(why)) = &done {
            let why = why.clone();
            self.discard(&why);
        }
        if std::mem::take(&mut self.began_anew) {
            let _ = self.index();
            return Err(SearchError::Broken("the index was made anew".to_string()));
        }
        done
    }

    fn serve(mut self, rx: &mpsc::Receiver<Command>) {
        while let Ok(command) = rx.recv() {
            match command {
                Command::Marks(answer) => {
                    let done = self.index().and_then(|index| index.marks());
                    let _ = answer.send(self.done(done));
                }
                Command::Apply {
                    agent_id,
                    rows,
                    mark,
                    answer,
                } => {
                    let done = self
                        .index()
                        .and_then(|index| index.apply(&agent_id, &rows, &mark));
                    let _ = answer.send(self.done(done));
                }
                Command::Forget { agent_id, answer } => {
                    let done = self.index().and_then(|index| index.forget(&agent_id));
                    let _ = answer.send(self.done(done));
                }
                Command::Discard => {
                    self.discard("a search found it damaged");
                    let _ = self.index();
                }
            }
        }
    }
}

// ---- keeping it ------------------------------------------------------------------------------

/// Starts keeping the index, once the Host is up. Never awaited by the start.
pub fn run(app: &App) {
    let app = app.clone();
    tokio::spawn(async move {
        let events = app.hub.subscribe(None).1;
        tokio::time::sleep(AFTER_START).await;
        Indexer::new(app).keep(events).await;
    });
}

struct Indexer {
    app: App,
    /// Where each agent the index holds stands, as the index has it.
    marks: HashMap<String, Mark>,
    /// The index's marks are to be read again (at the start, and after it began anew).
    reread: bool,
    /// The agents whose changes are to be read, and from when.
    due: BTreeMap<String, Instant>,
    /// When each agent's changes were last read.
    read_at: HashMap<String, Instant>,
    /// The agents the index holds nothing of yet: their newest page is read first, as history.
    unread: HashSet<String>,
    /// The agents whose history could not be read: tried again after their next change.
    stalled: HashSet<String>,
    /// The roster changed since the index was last looked over: many changes, one look.
    roster_changed: bool,
    /// When the last page of history was read.
    paced_from: Option<Instant>,
}

impl Indexer {
    fn new(app: App) -> Self {
        Self {
            app,
            marks: HashMap::new(),
            reread: true,
            due: BTreeMap::new(),
            read_at: HashMap::new(),
            unread: HashSet::new(),
            stalled: HashSet::new(),
            roster_changed: true,
            paced_from: None,
        }
    }

    async fn keep(mut self, mut events: broadcast::Receiver<Arc<crate::hub::Published>>) {
        loop {
            if self.reread {
                match self.app.search.marks().await {
                    Ok(marks) => {
                        self.marks = marks;
                        self.reread = false;
                        self.roster_changed = true;
                    }
                    Err(why) => self.failed(&why),
                }
            }
            // What the hub said meanwhile, without waiting.
            loop {
                match events.try_recv() {
                    Ok(event) => self.heard(&event.json),
                    Err(broadcast::error::TryRecvError::Empty) => break,
                    Err(broadcast::error::TryRecvError::Lagged(_)) => self.roster_changed = true,
                    Err(broadcast::error::TryRecvError::Closed) => return,
                }
            }
            if !self.reread && std::mem::take(&mut self.roster_changed) {
                self.sweep().await;
            }
            let now = Instant::now();
            let ready = self
                .due
                .iter()
                .find(|(_, at)| **at <= now)
                .map(|(agent, _)| agent.clone());
            if let (Some(agent_id), false) = (ready, self.reread) {
                self.due.remove(&agent_id);
                self.read_at.insert(agent_id.clone(), Instant::now());
                self.changes(&agent_id).await;
                tokio::time::sleep(BREATH).await;
                continue;
            }
            // History, one page, when the last is far enough behind.
            let paced = self.paced_from.map(|from| from + PACE);
            if !self.reread
                && paced.is_none_or(|at| at <= now)
                && let Some(agent_id) = self.next_history()
            {
                self.paced_from = Some(Instant::now());
                self.history(&agent_id).await;
                continue;
            }
            // Nothing to do now: wait for the hub, the next change that is due, or the pace.
            let wake = self
                .due
                .values()
                .copied()
                // The pace, where an agent that rests has history to read; one that works
                // is passed over until its turn ends, which the roster says.
                .chain(paced.filter(|_| self.next_history().is_some()))
                .chain(self.reread.then(|| now + PACE * 4))
                .min();
            let heard = match wake {
                Some(at) => tokio::select! {
                    heard = events.recv() => Some(heard),
                    () = tokio::time::sleep_until(at.max(now)) => None,
                },
                None => Some(events.recv().await),
            };
            match heard {
                Some(Ok(event)) => self.heard(&event.json),
                // What was missed is found by looking every agent over.
                Some(Err(broadcast::error::RecvError::Lagged(_))) => self.roster_changed = true,
                Some(Err(broadcast::error::RecvError::Closed)) => return,
                None => {}
            }
        }
    }

    /// The index answered with an error. One that says it began anew has everything read
    /// again; any other is told and the work it was for waits for the agent's next change.
    fn failed(&mut self, why: &SearchError) {
        if matches!(why, SearchError::Broken(_)) {
            self.marks.clear();
            self.unread.clear();
            self.stalled.clear();
            self.reread = true;
        } else {
            eprintln!("clarp-host: search index: {why}");
        }
    }

    /// An event of the hub: a conversation that changed is read again, no sooner than
    /// [`EVERY`] after its last read; a change of the roster has the index looked over. Every
    /// event wakes the indexer, so an agent that stood busy has its history read once a turn's
    /// end (which changes its conversation) has been told.
    fn heard(&mut self, event: &str) {
        if event.contains("\"type\":\"transcript-updated\"") {
            let agent_id = serde_json::from_str::<Value>(event)
                .ok()
                .and_then(|event| event["agent_id"].as_str().map(str::to_string));
            if let Some(agent_id) = agent_id {
                self.stalled.remove(&agent_id);
                self.mark_due(agent_id);
            }
        } else if event.contains("\"type\":\"agent-roster\"") {
            self.roster_changed = true;
        }
    }

    fn mark_due(&mut self, agent_id: String) {
        let at = self
            .read_at
            .get(&agent_id)
            .map_or_else(Instant::now, |read| *read + EVERY);
        self.due.entry(agent_id).or_insert(at);
    }

    /// The agent of this id, while it is in the fleet and here.
    fn here(&self, agent_id: &str) -> Option<Arc<Member>> {
        let directory = self.app.fleet.directory();
        directory
            .members
            .iter()
            .chain(&directory.janitors)
            .find(|member| member.record.agent_id == agent_id)
            .filter(|member| member.agent.view().status.moved.is_none())
            .cloned()
    }

    /// The next agent whose history is read: one the index holds nothing of first, and only
    /// one that rests now (no turn running, nothing waiting). A busy one is passed over.
    fn next_history(&self) -> Option<String> {
        let directory = self.app.fleet.directory();
        let rests = |agent_id: &str| {
            directory
                .members
                .iter()
                .chain(&directory.janitors)
                .find(|member| member.record.agent_id == agent_id)
                .is_none_or(|member| resting(member))
        };
        let unread = self
            .unread
            .iter()
            .find(|agent| !self.stalled.contains(*agent) && rests(agent));
        let older = || {
            self.marks
                .iter()
                .find(|(agent, mark)| {
                    !mark.complete && !self.stalled.contains(*agent) && rests(agent)
                })
                .map(|(agent, _)| agent)
        };
        unread.or_else(older).cloned()
    }

    /// Looks the index over against the fleet: an agent that is gone or moved away leaves it,
    /// one it holds nothing of is to be read, and one whose conversation is newer than what
    /// the index holds of it is read.
    async fn sweep(&mut self) {
        let directory = self.app.fleet.directory();
        let present: Vec<(String, u64)> = directory
            .members
            .iter()
            .chain(&directory.janitors)
            .filter(|member| member.agent.view().status.moved.is_none())
            .map(|member| {
                let head = member.agent.view().status.head_revision;
                (member.record.agent_id.clone(), head)
            })
            .collect();
        let here: HashSet<&str> = present.iter().map(|(id, _)| id.as_str()).collect();
        let gone: Vec<String> = self
            .marks
            .keys()
            .filter(|agent_id| !here.contains(agent_id.as_str()))
            .cloned()
            .collect();
        self.unread
            .retain(|agent_id| here.contains(agent_id.as_str()));
        for agent_id in gone {
            let _ = self.forget(&agent_id).await;
        }
        for (agent_id, head) in present {
            match self.marks.get(&agent_id) {
                None => {
                    self.unread.insert(agent_id);
                }
                Some(mark) if mark.revision < head => self.mark_due(agent_id),
                Some(_) => {}
            }
        }
    }

    /// Answers whether the agent is out of the index now.
    async fn forget(&mut self, agent_id: &str) -> bool {
        match self.app.search.forget(agent_id).await {
            Ok(()) => {
                self.marks.remove(agent_id);
                self.due.remove(agent_id);
                self.read_at.remove(agent_id);
                self.stalled.remove(agent_id);
                true
            }
            Err(why) => {
                self.failed(&why);
                false
            }
        }
    }

    /// Reads what changed in an agent's conversation since the index last read it. An agent
    /// the index holds nothing of is left to the history, read while it rests; one whose
    /// conversation began again is forgotten, and read anew the same way.
    async fn changes(&mut self, agent_id: &str) {
        let Some(member) = self.here(agent_id) else {
            if self.marks.contains_key(agent_id) {
                let _ = self.forget(agent_id).await;
            }
            self.unread.remove(agent_id);
            return;
        };
        let which = which(&member);
        loop {
            let Some(mark) = self.marks.get(agent_id).cloned() else {
                self.unread.insert(agent_id.to_string());
                return;
            };
            let page = match member.agent.after(mark.revision, PAGE, which).await {
                Ok(page) => page,
                Err(why) => {
                    eprintln!("clarp-host: search index: {agent_id}: {}", why.0);
                    return;
                }
            };
            // The conversation began again: what the index held of it is gone.
            if page.begins_after != mark.begins_after || mark.revision > page.head_revision {
                if self.forget(agent_id).await {
                    self.unread.insert(agent_id.to_string());
                }
                return;
            }
            let more = page.has_more && page.latest_revision > mark.revision;
            let next = Mark {
                revision: page.latest_revision.max(mark.revision),
                ..mark
            };
            if !self.keep_page(agent_id, &page, next).await || !more {
                return;
            }
        }
    }

    /// One page of an agent's history: its newest page when the index holds nothing of it,
    /// else the page before the oldest row read.
    async fn history(&mut self, agent_id: &str) {
        let Some(member) = self.here(agent_id) else {
            self.unread.remove(agent_id);
            if self.marks.contains_key(agent_id) && !self.forget(agent_id).await {
                self.stalled.insert(agent_id.to_string());
            }
            return;
        };
        let which = which(&member);
        let mark = self.marks.get(agent_id).cloned();
        let (page, mark) = match mark {
            None => match member.agent.tail(PAGE, which).await {
                Ok(page) => {
                    let mark = Mark {
                        revision: page.latest_revision,
                        begins_after: page.begins_after,
                        ..Mark::default()
                    };
                    (page, mark)
                }
                Err(why) => return self.stall(agent_id, &why.0),
            },
            Some(mark) => {
                let Some(older_than) = mark.older_than.clone() else {
                    return self.stall(agent_id, "nothing read to go back from");
                };
                match member.agent.before(older_than, PAGE, which).await {
                    Ok(Some(page)) => (page, mark),
                    // A row the conversation no longer has: it began again, and is read anew.
                    Ok(None) => {
                        if self.forget(agent_id).await {
                            self.unread.insert(agent_id.to_string());
                        }
                        return;
                    }
                    Err(why) => return self.stall(agent_id, &why.0),
                }
            }
        };
        let older_than = page
            .entries
            .first()
            .map(|row| row.entry.id().to_string())
            .or(mark.older_than.clone());
        let next = Mark {
            complete: !page.has_more || page.entries.is_empty(),
            older_than,
            ..mark
        };
        if self.keep_page(agent_id, &page, next).await {
            self.unread.remove(agent_id);
        }
    }

    /// History that could not be read is tried again after the agent's next change.
    fn stall(&mut self, agent_id: &str, why: &str) {
        eprintln!("clarp-host: search index: {agent_id}: {why}");
        self.stalled.insert(agent_id.to_string());
    }

    async fn keep_page(&mut self, agent_id: &str, page: &Page, mark: Mark) -> bool {
        let rows = page.entries.iter().filter_map(kept).collect();
        match self.app.search.apply(agent_id, rows, mark.clone()).await {
            Ok(()) => {
                self.marks.insert(agent_id.to_string(), mark);
                true
            }
            Err(why) => {
                if !matches!(why, SearchError::Broken(_)) {
                    self.stalled.insert(agent_id.to_string());
                }
                self.failed(&why);
                false
            }
        }
    }
}

/// Whether an agent rests: no turn of it runs and nothing of it waits. Only then is its
/// history read, so its thread serves its work first.
fn resting(member: &Member) -> bool {
    let view = member.agent.view();
    view.status.live.is_none() && view.status.waiting.is_empty()
}

/// The rows an agent's chat shows, as `/log` reads them for an app.
fn which(member: &Member) -> Which {
    Which::chat(true).of_helper(member.record.is_helper_chat())
}

/// A row as it is kept: what a person or an agent said (the user's messages, the agent's
/// replies and notes, and such rows brought from another Host), as `/log` shows it. Other rows
/// (tool cards, the Host's notices) are kept with no words, which takes an earlier version out.
/// A reply still being written is not kept: it is read again when it has settled.
fn kept(version: &Versioned) -> Option<Row> {
    if version.live {
        return None;
    }
    let row = wire::row(version);
    let role = row["role"].as_str().unwrap_or_default().to_string();
    let kind = row["kind"].as_str().unwrap_or_default();
    let said = matches!(role.as_str(), "user" | "assistant")
        && matches!(kind, "" | clarp_kernel::coordination::NOTE_KIND);
    let text = if said {
        row["text"].as_str().unwrap_or_default().to_string()
    } else {
        String::new()
    };
    let ts_ms = row["timestamp"]
        .as_str()
        .and_then(wire::from_iso)
        .unwrap_or(version.ts_ms);
    Some(Row {
        row_id: version.entry.id().to_string(),
        role,
        ts_ms,
        revision: version.revision,
        text,
    })
}

// ---- searching -------------------------------------------------------------------------------

#[derive(Debug, Default, Deserialize)]
pub(crate) struct Asking {
    q: Option<String>,
    session: Option<String>,
    limit: Option<usize>,
    before: Option<String>,
}

/// `GET /search`: the rows of the conversations this caller may read that have every word of
/// `q`, newest first, a page at a time.
pub(crate) async fn route(
    State(app): State<App>,
    Extension(principal): Extension<Principal>,
    headers: HeaderMap,
    Query(asking): Query<Asking>,
) -> Result<Json<Value>, ApiError> {
    let reader = crate::bound_privacy::reader_of(&app, Some(&principal), &headers).await?;
    find(&app, &reader, asking).await.map(Json)
}

/// A search as this reader may make it.
pub(crate) async fn find(app: &App, reader: &Reader, asking: Asking) -> Result<Value, ApiError> {
    let words = asking.q.unwrap_or_default();
    if words.trim().is_empty() {
        return Err(ApiError::new(StatusCode::BAD_REQUEST, "q is required"));
    }
    let limit = asking
        .limit
        .unwrap_or(LIMIT)
        .clamp(1, clarp_store_sqlite::search::MOST);
    let session = asking
        .session
        .map(|session| session.trim().to_string())
        .filter(|session| !session.is_empty());
    let withheld = Withheld::of(app, reader);
    let directory = app.fleet.directory();
    let searched: Vec<&std::sync::Arc<Member>> = directory
        .members
        .iter()
        .chain(&directory.janitors)
        .filter(|member| {
            session.as_ref().is_none_or(|session| {
                member.record.session == *session || member.record.agent_id == *session
            })
        })
        .filter(|member| member.agent.view().status.moved.is_none())
        .filter(|member| {
            withheld
                .as_ref()
                .is_none_or(|withheld| !withheld.has(&member.record.agent_id))
        })
        .collect();
    let agents = searched
        .iter()
        .map(|member| member.record.agent_id.clone())
        .collect();
    let found = app
        .search
        .search(words.clone(), agents, limit, asking.before)
        .await
        .map_err(|why| match why {
            SearchError::Asked(why) => ApiError::new(StatusCode::BAD_REQUEST, why),
            SearchError::Storage(why) | SearchError::Broken(why) => {
                ApiError::retryable(StatusCode::SERVICE_UNAVAILABLE, format!("search: {why}"))
            }
        })?;
    let hits: Vec<Value> = found
        .hits
        .iter()
        .filter_map(|hit| {
            let member = searched
                .iter()
                .find(|member| member.record.agent_id == hit.agent_id)?;
            let snippet: String = hit.parts.iter().map(|(text, _)| text.as_str()).collect();
            let parts: Vec<Value> = hit
                .parts
                .iter()
                .map(|(text, matched)| json!({ "text": text, "match": matched }))
                .collect();
            Some(json!({
                "session": member.record.session,
                "agent_id": member.record.agent_id,
                "name": member.record.persona,
                "message_id": hit.row_id,
                "role": hit.role,
                "timestamp": wire::iso(hit.ts_ms),
                "revision": hit.revision,
                "snippet": snippet,
                "parts": parts,
            }))
        })
        .collect();
    Ok(json!({
        "q": words,
        "hits": hits,
        "next_before": found.next,
        "partial": found.partial,
    }))
}

/// `clarp-admin search WORDS... [--session S] [--limit N] [--before CURSOR]`: the same search
/// for an agent, as the reader its turn is.
pub(crate) async fn tool(
    app: &App,
    call: &crate::tools::ToolCall,
    args: &[String],
    owner_token: bool,
) -> crate::tools::ToolAnswer {
    use crate::tools::ToolAnswer;
    // One word or more, as many as were typed.
    let names: Vec<&str> = std::iter::once("words")
        .chain(std::iter::repeat_n("[words]", args.len()))
        .collect();
    let parsed =
        match crate::artifact_tool::parse(args, &["--session", "--limit", "--before"], &[], &names)
        {
            Ok(parsed) => parsed,
            Err(why) => return ToolAnswer::said(2, format!("clarp-admin search: error: {why}")),
        };
    let limit = match parsed.whole("--limit") {
        Ok(limit) => limit.map(|limit| usize::try_from(limit.max(1)).unwrap_or(1)),
        Err(why) => return ToolAnswer::said(2, format!("clarp-admin search: error: {why}")),
    };
    let reader = crate::bound_privacy::reader_of_call(app, call, owner_token)
        .await
        .unwrap_or(Reader::Unknown);
    let asking = Asking {
        q: Some(parsed.positional.join(" ")),
        session: parsed.option("--session").map(str::to_string),
        limit,
        before: parsed.option("--before").map(str::to_string),
    };
    match find(app, &reader, asking).await {
        Ok(found) => {
            let mut answer = ToolAnswer::code(0);
            answer.stdout = serde_json::to_string_pretty(&found).unwrap_or_default();
            answer.stdout.push('\n');
            answer
        }
        Err(why) => ToolAnswer::said(1, format!("clarp-admin search: {}", why.message())),
    }
}
