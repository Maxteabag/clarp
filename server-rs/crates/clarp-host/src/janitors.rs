//! The janitors at the Host's edge: agents with a job of maintenance, as the apps read and
//! set them.
//!
//! The rules are the kernel's ([`clarp_kernel::janitors`]): what a Host offers, what a
//! janitor's job may be, and the book that is kept of them. Here is what is kept (the book,
//! with the fleet: each row of its tables a record of its own, so that a change of one
//! janitor or one run writes that and nothing else), who a janitor is (a member of the fleet
//! that is no agent of the roster: [`crate::fleet::Directory::janitors`]), the seven janitors
//! a Host installs for itself, and the routes, which answer as the Python Host's do
//! (`server/lib/janitor_http.py`): the same statuses, bodies and words.
//!
//! **What runs: a scheduled task, and the router.** A janitor whose job is a scheduled task
//! is turned on, and then runs when its schedule says: the runner here looks every two
//! seconds at what the book says is due ([`clarp_kernel::janitors::runs`]), hands a run to
//! the janitor's own mailbox as a message the Host sends by itself, and tells the book how
//! the turn stands. The janitor that finds whom a spoken message is for runs too, once for
//! each message, when it is on ([`crate::router`]). The Quota keeper looks when its look is
//! due, in this runner too ([`crate::quota_keeper`]). Every job of
//! [`clarp_kernel::janitors::RUN_HERE`] runs; no other yet: turning such a
//! janitor on is refused in words, with the Python Host's word for a Host that cannot run
//! it (`janitor_runtime_unavailable`), and the janitors of the Host's own are installed
//! paused, where the Python Host starts three.

use std::sync::Arc;
use std::time::Duration;

use axum::body::{Body, Bytes};
use axum::extract::{Path, Query, State};
use axum::http::{Method, Request, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::get;
use axum::{Json, Router};
use clarp_kernel::conversation::Message;
use clarp_kernel::janitors::book::{Agent, Asked, Book, Config, Effect, World};
use clarp_kernel::janitors::creation::Begun;
use clarp_kernel::janitors::runs::{self, Delivery, Ended, Handed, Run};
use clarp_kernel::janitors::{self as rules, Refused, schedule};
use clarp_kernel::python;
use clarp_kernel::turn::{Admission, Origin, Outcome};
use serde::Deserialize;
use serde_json::{Map, Value, json};
use tower::ServiceExt as _;

use crate::agent::Unavailable;
use crate::edge::App;
use crate::fleet::{Amend, Directory, JanitorSeed, NotMade};
use crate::ids;

/// The kind of thing the janitors' book is, among what is left with the fleet: what is left
/// of it once its tables are kept apart.
const KIND: &str = "janitors";
const ALL: &str = "all";
/// The book's tables, each a kind of its own with one record a row: a run of a janitor, or
/// one change of one, writes what changed of it and nothing of the rest. A book kept whole
/// before is read, and kept apart at once.
const TABLES: [(&str, &str); 7] = [
    ("configs", "janitor-config"),
    ("triggers", "janitor-trigger"),
    ("builtins", "janitor-builtin"),
    ("runs", "janitor-run"),
    ("progress", "janitor-progress"),
    ("creations", "janitor-creation"),
    ("alerts", "janitor-alert"),
];

/// A record written with a change of the book, by a desk that keeps its own beside it.
pub(crate) type Row = (&'static str, String, Vec<u8>);
/// A record let go with a change of the book.
pub(crate) type Gone = (&'static str, String);

/// What one change of the book writes: each row of a table that is new or changed, each
/// that went, and what is left of the book where that changed. `before`: nothing, where all
/// of it is written.
fn written(before: Option<&Book>, after: &Book) -> (Vec<Row>, Vec<Gone>) {
    let as_value = |book: &Book| serde_json::to_value(book).unwrap_or(Value::Null);
    let mut after = as_value(after);
    let mut before = before.map(as_value).unwrap_or(Value::Null);
    let (mut rows, mut gone) = (Vec::new(), Vec::new());
    for (table, kind) in TABLES {
        let table_of = |book: &mut Value| {
            let fields = book.as_object_mut();
            fields
                .and_then(|fields| fields.remove(table))
                .unwrap_or(Value::Null)
        };
        let (now, was) = (table_of(&mut after), table_of(&mut before));
        let (now, was) = (
            now.as_object().cloned().unwrap_or_default(),
            was.as_object().cloned().unwrap_or_default(),
        );
        for (key, row) in &now {
            if was.get(key) != Some(row) {
                rows.push((
                    kind,
                    key.clone(),
                    serde_json::to_vec(row).unwrap_or_default(),
                ));
            }
        }
        let went = was.keys().filter(|key| !now.contains_key(*key));
        gone.extend(went.map(|key| (kind, key.clone())));
    }
    if after != before {
        rows.push((
            KIND,
            ALL.to_string(),
            serde_json::to_vec(&after).unwrap_or_default(),
        ));
    }
    (rows, gone)
}

/// Keeps a change of the book, and what a desk keeps beside it, in one write. What reached the
/// owner of a janitor that fails is brought in line with the change first, and kept with it.
/// Kept, then shown: only once the write answered well does the owner see the change (what
/// the counts and lists read, the apps told, the badge moved); a write that fails tells
/// nobody anything, and what is shown stays what was kept.
async fn keep(
    app: &App,
    before: Option<&Book>,
    after: &mut Book,
    (mut rows, mut gone): (Vec<Row>, Vec<Gone>),
) -> Result<(), Unavailable> {
    let raised = after.reconcile_attention(&Around::new(app), app.clock.now_ms());
    let moved = before.is_none_or(|before| before.alerts != after.alerts);
    let (book, went) = written(before, after);
    rows.extend(book);
    gone.extend(went);
    let after = &*after;
    kept_then_shown(app.fleet.change_together(rows, gone), || {
        app.janitors
            .changes
            .fetch_add(1, std::sync::atomic::Ordering::SeqCst);
        app.janitors.mirror(after);
        if raised > 0 {
            // The apps read the janitors again, as the Python Host tells them.
            let told = json!({ "type": "agent-roster", "kind": "janitor-attention" });
            app.hub.publish(told, app.clock.now_ms());
        }
        if moved {
            app.hub.badge_moved();
        }
    })
    .await
}

/// Waits for a write, and only once it answered well does what shows it.
async fn kept_then_shown<E>(
    written: impl std::future::Future<Output = Result<(), E>>,
    shown: impl FnOnce(),
) -> Result<(), E> {
    written.await?;
    shown();
    Ok(())
}
/// What a schedule is taken to be apart when it cannot be read: a day.
const A_DAY_S: i64 = 86_400;
/// The tool a janitor of the Host's own runs on where its job recommends one this Host
/// has not.
const OWN_TOOL: &str = "codex";

/// Who a run's turn is from, as the apps file it: the Host itself, for a janitor.
const FROM_A_JANITOR: &str = "janitor";

/// The janitors of this Host. `None` until the book was read where it is kept.
#[derive(Default)]
pub struct Desk {
    book: tokio::sync::Mutex<Option<Book>>,
    /// How often the book was kept since the Host started: whoever arms itself on the book
    /// (the Quota keeper) sees by it that the book changed since.
    changes: std::sync::atomic::AtomicU64,
    /// The janitors' jobs and what reached their owners, as the book last kept them: read by
    /// what counts and lists for the owner without waiting for the book.
    attention: std::sync::RwLock<Book>,
}

/// What the book asks of the Host around it: the fleet as it stands, the machine's zones,
/// the pictures the Host ships.
pub(crate) struct Around<'a> {
    app: &'a App,
    directory: Arc<Directory>,
}

impl<'a> Around<'a> {
    fn new(app: &'a App) -> Self {
        Self {
            app,
            directory: app.fleet.directory(),
        }
    }
}

impl World for Around<'_> {
    fn agent(&self, key: &str) -> Option<Agent> {
        let member = self.directory.anyone(key)?;
        let record = &member.record;
        let status = member.agent.view().status.clone();
        Some(Agent {
            agent_id: record.agent_id.clone(),
            session: record.session.clone(),
            name: record.persona.clone(),
            backend: record.backend.clone(),
            model: record.model.clone(),
            effort: record.effort.clone(),
            is_janitor: record.is_janitor,
            archived: record.archived_at_ms.is_some(),
            busy: status.live.is_some() || !status.waiting.is_empty(),
        })
    }

    fn zone(&self, name: &str) -> bool {
        schedule::zone(name).is_some()
    }

    fn draw(&mut self) -> String {
        format!("attachment-{}", ids::hex(16))
    }

    fn backend(&self, name: &str) -> bool {
        clarp_backend::by_id(name).is_some()
    }

    fn model(&self, _backend: &str, _model: &str) -> bool {
        // This Host keeps no catalogue of models: a model is set by its name, and the tool
        // says at the next turn when it has none of that name.
        true
    }

    fn policy_model_error(&self, backend: &str, model: &str) -> Option<String> {
        self.app.models.policy_model_error(backend, model)
    }

    fn efforts(&self, backend: &str) -> Vec<String> {
        let efforts = clarp_backend::efforts(backend).iter();
        efforts.map(|effort| (*effort).to_string()).collect()
    }

    fn base_interval(&self, expression: &str, zone: &str, now_ms: i64) -> i64 {
        schedule::base_interval(expression, zone, now_ms).unwrap_or(A_DAY_S)
    }

    fn next_run(&self, expression: &str, zone: &str, from_ms: i64) -> Option<i64> {
        schedule::next_run(expression, zone, from_ms).ok().flatten()
    }

    fn directory(&self, path: &str) -> String {
        let home = std::env::var("HOME").unwrap_or_else(|_| "/".to_string());
        let named = python::strip(path);
        let named = match named.strip_prefix("~/") {
            Some(rest) => format!("{home}/{rest}"),
            None if named == "~" => home.clone(),
            None => named.to_string(),
        };
        let there = !named.is_empty() && std::path::Path::new(&named).is_dir();
        if there { named } else { home }
    }

    fn drawn(&mut self) -> String {
        ids::hex(6)
    }

    fn face(&self, file: &str) -> String {
        self.app.faces.janitor(file)
    }
}

impl Desk {
    /// How often the book was kept since the Host started.
    pub(crate) fn changes(&self) -> u64 {
        self.changes.load(std::sync::atomic::Ordering::SeqCst)
    }

    /// The book, read where it is kept the first time it is asked for.
    async fn held(
        &self,
        app: &App,
    ) -> Result<tokio::sync::MutexGuard<'_, Option<Book>>, Unavailable> {
        let mut held = self.book.lock().await;
        if held.is_none() {
            let kept = app.fleet.kept(KIND, ALL.to_string()).await?;
            let mut whole: Value = kept
                .and_then(|kept| serde_json::from_slice(&kept).ok())
                .filter(Value::is_object)
                .unwrap_or_else(|| json!({}));
            let filled = |table: &str, whole: &Value| {
                whole[table]
                    .as_object()
                    .is_some_and(|rows| !rows.is_empty())
            };
            let kept_whole = TABLES.iter().any(|(table, _)| filled(table, &whole));
            for (table, kind) in TABLES {
                for (key, row) in app.fleet.kept_all(kind).await? {
                    if let Ok(row) = serde_json::from_slice::<Value>(&row) {
                        whole[table][key] = row;
                    }
                }
            }
            // What cannot be read is a book with nothing in it.
            let mut book: Book = serde_json::from_value(whole).unwrap_or_default();
            if kept_whole {
                keep(app, None, &mut book, Default::default()).await?;
            }
            self.mirror(&book);
            *held = Some(book);
        }
        Ok(held)
    }

    /// Hold the same ownership fence as a configure/pause while the checker publishes.
    pub(crate) async fn label_document(
        &self,
        app: &App,
        run: &str,
        member: &crate::fleet::Member,
        doc: &Value,
    ) -> Result<bool, Unavailable> {
        let held = self.held(app).await?;
        if !held
            .as_ref()
            .is_some_and(|b| b.is_current(run, &Around::new(app), app.clock.now_ms()))
        {
            return Ok(false);
        }
        crate::library::label_document(member, doc)
            .await
            .map_err(|_| Unavailable("label report could not be saved".into()))?;
        Ok(true)
    }

    /// Keeps what the owner is shown of the janitors as the book has it now.
    fn mirror(&self, book: &Book) {
        if let Ok(mut shown) = self.attention.write() {
            shown.configs.clone_from(&book.configs);
            shown.alerts.clone_from(&book.alerts);
        }
    }

    /// What reached the owner of janitors that failed again and again and waits for them, as
    /// `GET /attention` lists it among its `janitor_items` (`attention.rs`).
    pub(crate) fn pending(&self, app: &App, include_archived: bool) -> Vec<Value> {
        let shown = self.attention.read();
        let shown = shown.as_deref().ok();
        shown.map_or_else(Vec::new, |book| {
            book.pending_attention(&Around::new(app), include_archived)
        })
    }

    /// One such item, by its id, whatever became of it.
    pub(crate) fn alert(&self, artifact_id: &str) -> Option<clarp_kernel::artifacts::Artifact> {
        let shown = self.attention.read().ok()?;
        shown.alerts.get(artifact_id).cloned()
    }

    /// The owner archives such an item, or shows it again, or discards it: by the artifacts'
    /// own rules, in a write of the book. `None`: the book has no such item.
    pub(crate) async fn put_away(
        &self,
        app: &App,
        artifact_id: &str,
        archived: Option<bool>,
        expected_updated_at: i64,
    ) -> Result<
        Option<Result<(clarp_kernel::artifacts::Artifact, bool), clarp_kernel::artifacts::Refused>>,
        Unavailable,
    > {
        self.update(app, |book, _, now_ms| {
            let current = book.alerts.get(artifact_id)?;
            let done = match archived {
                Some(archived) => clarp_kernel::artifacts::archived(
                    current,
                    archived,
                    expected_updated_at,
                    now_ms,
                ),
                None => clarp_kernel::artifacts::discarded(current, expected_updated_at, now_ms),
            };
            if let Ok((changed, true)) = &done {
                book.alerts.insert(artifact_id.to_string(), changed.clone());
            }
            Some(done)
        })
        .await
        .map(|(done, _)| done)
    }

    /// One reading of the book. Nothing is kept, and nothing of it is copied.
    pub(crate) async fn read<T>(
        &self,
        app: &App,
        read: impl FnOnce(&Book, &Around<'_>, i64) -> T,
    ) -> Result<T, Unavailable> {
        let held = self.held(app).await?;
        let Some(book) = held.as_ref() else {
            return Err(Unavailable(
                "the janitors' book could not be read".to_string(),
            ));
        };
        Ok(read(book, &Around::new(app), app.clock.now_ms()))
    }

    /// One change of the book and of the agents it is about: the book is kept, then what
    /// has to become of an agent is done, and the janitor is read as it then is. Nobody
    /// else reads or changes the book meanwhile.
    pub(crate) async fn change(
        &self,
        app: &App,
        key: &str,
        change: impl FnOnce(&mut Book, &mut Around<'_>, i64) -> Result<Vec<Effect>, Refused>,
    ) -> Result<Result<Value, Refused>, Unavailable> {
        let mut held = self.held(app).await?;
        let Some(book) = held.as_mut() else {
            return Err(Unavailable(
                "the janitors' book could not be read".to_string(),
            ));
        };
        let now_ms = app.clock.now_ms();
        let before = book.clone();
        let effects = match change(book, &mut Around::new(app), now_ms) {
            Ok(effects) => effects,
            Err(refused) => {
                *book = before;
                return Ok(Err(refused));
            }
        };
        if *book != before {
            keep(app, Some(&before), book, Default::default()).await?;
        }
        for effect in effects {
            let (agent_id, amend) = match effect {
                Effect::Convert { agent_id } => (agent_id, Amend::Janitor(true)),
                Effect::PutAway { agent_id } => (agent_id, Amend::Archive(true)),
                Effect::Release { agent_id } => (agent_id, Amend::Janitor(false)),
                Effect::Tune {
                    agent_id,
                    backend,
                    model,
                    effort,
                } => {
                    // Its tool first, then the model and the effort that go with it.
                    if let Some(backend) = backend {
                        let tool = Amend::Tool(backend);
                        let amended = app.fleet.amend_any(agent_id.clone(), tool).await?;
                        not_amended(app, amended);
                    }
                    (agent_id, Amend::Tune { model, effort })
                }
            };
            let amended = app.fleet.amend_any(agent_id, amend).await?;
            not_amended(app, amended);
        }
        // A run the change cut off may not go on: its turn is taken back.
        let was_out = before.runs.values().filter(|run| run.out());
        let cut_off = was_out.filter(|run| book.runs.get(&run.run_id).is_none_or(|run| !run.out()));
        for run in cut_off {
            take_back(app, &run.agent_id, &run.run_id).await;
        }
        Ok(book.shown(key, &Around::new(app), now_ms))
    }

    /// A request for a janitor that is made as a new agent, at its first asking or any
    /// later one: where it stands, with a session kept for its agent.
    async fn begin(
        &self,
        app: &App,
        request_id: &Value,
        payload: &Value,
    ) -> Result<Result<Begun, Refused>, Unavailable> {
        let mut held = self.held(app).await?;
        let Some(book) = held.as_mut() else {
            return Err(Unavailable(
                "the janitors' book could not be read".to_string(),
            ));
        };
        let before = book.clone();
        let now_ms = app.clock.now_ms();
        let begun = book.begin_creation(request_id, payload, &mut Around::new(app), now_ms);
        if *book != before {
            keep(app, Some(&before), book, Default::default()).await?;
        }
        Ok(begun)
    }

    /// One look at the book, or one change of it, by another desk of the Host that keeps
    /// its runs here: the router of spoken messages, whose every message is routed in a
    /// run of its janitor. Kept when it changed anything.
    pub(crate) async fn with<T>(
        &self,
        app: &App,
        change: impl FnOnce(&mut Book, &dyn World, i64) -> T,
    ) -> Result<T, Unavailable> {
        let changed = self.update(app, |book, around, now_ms| change(book, around, now_ms));
        Ok(changed.await?.0)
    }

    /// One change of the book by a desk of the Host that keeps records of its own beside
    /// it: what the change answers, with the records to write and to let go, which are kept
    /// in the same write as the book. Nobody else reads or changes the book meanwhile.
    pub(crate) async fn with_kept<T>(
        &self,
        app: &App,
        change: impl FnOnce(&mut Book, &dyn World, i64) -> (T, Vec<Row>, Vec<Gone>),
    ) -> Result<T, Unavailable> {
        let mut held = self.held(app).await?;
        let Some(book) = held.as_mut() else {
            return Err(Unavailable(
                "the janitors' book could not be read".to_string(),
            ));
        };
        let before = book.clone();
        let (done, rows, gone) = change(book, &Around::new(app), app.clock.now_ms());
        if *book != before || !rows.is_empty() || !gone.is_empty() {
            keep(app, Some(&before), book, (rows, gone)).await?;
        }
        Ok(done)
    }

    /// One change of the book by the Host itself, kept when it changed anything. Answers
    /// what the change answered, and whether anything changed.
    async fn update<T>(
        &self,
        app: &App,
        change: impl FnOnce(&mut Book, &Around<'_>, i64) -> T,
    ) -> Result<(T, bool), Unavailable> {
        let mut held = self.held(app).await?;
        let Some(book) = held.as_mut() else {
            return Err(Unavailable(
                "the janitors' book could not be read".to_string(),
            ));
        };
        let before = book.clone();
        let done = change(book, &Around::new(app), app.clock.now_ms());
        let changed = *book != before;
        if changed {
            keep(app, Some(&before), book, Default::default()).await?;
        }
        Ok((done, changed))
    }
}

// ---- the runner ----------------------------------------------------------------------------

/// How the turn of a run stands in its janitor's conversation: when it started, if it has,
/// and how it ended, if it has.
fn turn_of(app: &App, run: &Run) -> (Option<i64>, Option<Ended>) {
    let directory = app.fleet.directory();
    let Some(member) = directory.janitor(&run.agent_id) else {
        return (None, None);
    };
    let view = member.agent.view();
    let trace = Some(run.trace_id.as_str());
    if let Some(live) = &view.status.live
        && live.trace_id.as_deref() == trace
    {
        return (Some(live.started_at_ms), None);
    }
    let end = view.ended.as_ref();
    let Some(end) = end.filter(|end| end.trace_id.as_deref() == trace) else {
        return (None, None);
    };
    let last = view.status.last.as_ref();
    let failed = |why: String| Ended { failed: true, why };
    let ended = match last
        .filter(|last| last.id.0 == end.turn)
        .map(|last| &last.outcome)
    {
        Some(Outcome::Completed) => Ended::default(),
        Some(Outcome::Failed { reason, .. } | Outcome::Interrupted { reason }) => {
            failed(reason.clone())
        }
        Some(Outcome::Stopped { by, .. }) => failed(format!("Stopped by {by}")),
        Some(Outcome::Withdrawn { .. }) => {
            failed("Its turn was taken back before it started".to_string())
        }
        // Its end is known and no more of it: what the end says of itself holds.
        None => Ended {
            failed: end.status != "completed",
            why: String::new(),
        },
    };
    (end.started_at_ms, Some(ended))
}

/// Hands the turn of a run to its janitor's own mailbox: a message the Host sends by
/// itself, under the run's id, that waits its turn behind a turn that runs. Nobody is
/// told of it and nothing is said aloud.
async fn deliver(app: &App, hand: &Handed) -> Delivery {
    let directory = app.fleet.directory();
    let Some(member) = directory.janitor(&hand.session) else {
        return Delivery::NotTaken;
    };
    let managed_model = app
        .janitors
        .read(app, |book, _, _| {
            let frozen = book
                .runs
                .get(&hand.run_id)?
                .configuration
                .get("effective_chain")?;
            let first = &frozen["chain"][0];
            (frozen["source"] == "global" && first["provider"] == member.record.backend)
                .then(|| first["model"].as_str().map(str::to_owned))
                .flatten()
        })
        .await
        .ok()
        .flatten();
    let (id, marked) = Message::filed(&hand.run_id);
    let message = Message {
        id,
        marked,
        unminted: true,
        text: hand.prompt.clone(),
        managed_model,
        origin: Origin::Trigger {
            name: FROM_A_JANITOR.to_string(),
        },
        trace_id: Some(hand.run_id.clone()),
        ..Message::default()
    };
    match member.agent.send(message, Admission::Background).await {
        Ok(_) => Delivery::Taken,
        Err(_) => Delivery::Broke("Unavailable".to_string()),
    }
}

/// Takes back the turn of a run that was cut off: a message that waits is withdrawn, a
/// turn that runs is stopped. The janitor's mailbox is not left held: its next run is not
/// this one's.
/// A change the janitors' book made that the fleet refused (the agent gone, its name
/// another's): the book has it and the agent has not. Nothing waits on it: it is said.
fn not_amended<T>(app: &App, amended: Result<T, crate::fleet::NotAmended>) {
    let why = match amended {
        Ok(_) => return,
        Err(crate::fleet::NotAmended::Unknown) => "no such agent",
        Err(crate::fleet::NotAmended::Occupied { .. }) => "its name is another agent's",
        Err(crate::fleet::NotAmended::NoContact) => "no contact could be minted for it",
    };
    app.fleet.unkept("agent", why);
}

async fn take_back(app: &App, agent: &str, run_id: &str) {
    let directory = app.fleet.directory();
    let Some(member) = directory.janitor(agent).or_else(|| directory.anyone(agent)) else {
        return;
    };
    if let Ok((waiting, _)) = member.agent.queue().await {
        for one in waiting.iter().filter(|one| one.client_id == run_id) {
            let _ = member
                .agent
                .withdraw(one.turn, FROM_A_JANITOR.to_string())
                .await;
        }
    }
    let view = member.agent.view();
    let live = view.status.live.as_ref();
    if live.is_some_and(|live| live.trace_id.as_deref() == Some(run_id)) {
        let _ = member.agent.stop(FROM_A_JANITOR.to_string()).await;
        let _ = member.agent.resume(FROM_A_JANITOR.to_string()).await;
    }
}

/// One look of the runner at every trigger of a scheduled task that may act. The apps are
/// told nothing by it, as on the Python Host: what a run's turn does is told like any turn.
async fn look(app: &App) -> Result<(), Unavailable> {
    let desk = &app.janitors;
    let ids = desk
        .read(app, |book, world, _| book.scheduled(world))
        .await?;
    for id in ids {
        let out = desk
            .read(app, |book, _, _| book.out_for(&id).cloned())
            .await?;
        let (started, ended) = out.as_ref().map_or((None, None), |run| turn_of(app, run));
        let (hand, _) = desk
            .update(app, |book, world, now_ms| {
                // Its turn has started: said once, with the time it did.
                let unstarted = out.as_ref().filter(|run| {
                    let kept = book.runs.get(&run.run_id);
                    kept.is_some_and(|kept| kept.started_at.is_none())
                });
                if let (Some(run), Some(started_at)) = (unstarted, started) {
                    let _ = book.started(&run.run_id, world, started_at);
                }
                book.look(&id, ended.as_ref(), world, now_ms)
            })
            .await?;
        let Some(hand) = hand else {
            continue;
        };
        let how = deliver(app, &hand).await;
        let handed = desk.update(app, |book, world, now_ms| {
            book.handed(&id, &how, world, now_ms)
        });
        let (taken, _) = handed.await?;
        if how == Delivery::Taken && !taken {
            // The run was cut off between the look and the hand-over.
            take_back(app, &hand.session, &hand.run_id).await;
        }
    }
    Ok(())
}

/// Each keeper gets its look even when another could not read its records.
async fn tick(app: &App) {
    if let Err(why) = look(app).await {
        app.fleet.unkept(KIND, &why.0);
    }
    // After every look, what was seen of clips is written down: the bookkeeper asks no
    // model and has no turn of its own.
    crate::audio::keep(app).await;
    // And the Quota keeper looks where its look is due, beside the runner.
    crate::quota_keeper::look(app);
    crate::label_checker::look(app);
}

/// Only bound runtimes enter the rotation. A live turn shares its own
/// incremental read with helper cells. Idle hints are delivered by mailbox.
fn visit_idle_bindings(app: &App, after: &mut (String, String)) {
    use clarp_kernel::provider_tasks::{rotate_idle, visit_binding};
    let directory = app.fleet.directory();
    let now = app.clock.now_ms();
    let mut bindings = Vec::new();
    let mut agents = std::collections::BTreeMap::new();
    for member in directory.members.iter().chain(&directory.janitors) {
        let view = member.agent.view();
        if view.broken.is_some() {
            continue;
        }
        let current = view.status.backend_session.as_deref();
        let unfinished = view
            .jobs
            .iter()
            .any(|job| job.provider_owned() && job.status.is_active());
        for (native, ended, first_exit) in &view.provider_bindings {
            if first_exit.is_some_and(|at| clarp_kernel::provider_tasks::turn_link_expired(at, now))
            {
                member.agent.expire_provider_links(native);
            }
            if (view.status.live.is_none() || current != Some(native.as_str()))
                && visit_binding(*ended, unfinished, now)
            {
                bindings.push((member.record.agent_id.clone(), native.clone()));
            }
        }
        // Existing bindings that predate the observer have no cursor yet.
        if member.record.backend == "claude"
            && view.status.live.is_none()
            && let Some(native) = current
            && !view.provider_bindings.iter().any(|(id, _, _)| id == native)
            && visit_binding(
                view.status.last.as_ref().map(|turn| turn.ended_at_ms),
                unfinished,
                now,
            )
        {
            bindings.push((member.record.agent_id.clone(), native.to_owned()));
        }
        agents.insert(member.record.agent_id.as_str(), &member.agent);
    }
    for binding in rotate_idle(bindings, after) {
        if let Some(agent) = agents.get(binding.0.as_str()) {
            agent.visit_provider(&binding.1);
        }
        *after = binding;
    }
}

/// Starts the runner: it looks for what is due every so often.
pub fn run(app: &App) {
    let app = app.clone();
    let every = Duration::from_millis(app.config.janitors_checked_every_ms.max(50));
    tokio::spawn(async move {
        let mut after = Default::default();
        loop {
            tokio::time::sleep(every).await;
            visit_idle_bindings(&app, &mut after);
            tick(&app).await;
        }
    });
}

/// One request of the janitors' own routes, asked inside the Host: for the agents' tool,
/// which does what the Python tool does by asking what it asks. Answers the status and the
/// body.
pub(crate) async fn asked(
    app: &App,
    method: Method,
    path: &str,
    body: Option<&Value>,
) -> (u16, Value) {
    let said = body.map_or_else(Body::empty, |body| Body::from(body.to_string()));
    let Ok(request) = Request::builder().method(method).uri(path).body(said) else {
        return (400, json!({ "error": "Janitor route not found" }));
    };
    let response = match routes().with_state(app.clone()).oneshot(request).await {
        Ok(response) => response,
        Err(never) => match never {},
    };
    let status = response.status().as_u16();
    let bytes = axum::body::to_bytes(response.into_body(), usize::MAX).await;
    let body = serde_json::from_slice(&bytes.unwrap_or_default()).unwrap_or(Value::Null);
    (status, body)
}

/// A janitor as it goes out: with both sides of its scope named, also where what is kept
/// names none (the janitors of the Host's own watch every agent).
fn as_sent(mut janitor: Value) -> Value {
    let mut scope = json!({ "agent_ids": [], "exclude_agent_ids": [] });
    if let (Some(whole), Some(kept)) = (scope.as_object_mut(), janitor["scope"].as_object()) {
        whole.extend(kept.clone());
        janitor["scope"] = scope;
    }
    janitor
}

fn refusal(refused: &Refused) -> Response {
    let status = StatusCode::from_u16(refused.status).unwrap_or(StatusCode::BAD_REQUEST);
    (status, Json(refused.told())).into_response()
}

/// What the Python Host answers where it was given something it cannot read.
fn bad(why: &str) -> Response {
    (StatusCode::BAD_REQUEST, Json(json!({ "error": why }))).into_response()
}

fn no_route(what: &str) -> Response {
    let why = json!({ "error": format!("Janitor {what} not found") });
    (StatusCode::NOT_FOUND, Json(why)).into_response()
}

fn unavailable(why: &Unavailable) -> Response {
    let why = json!({ "error": why.0 });
    (StatusCode::SERVICE_UNAVAILABLE, Json(why)).into_response()
}

/// A body as the Python Host reads one: nothing is an empty object, and what is no object
/// is nothing.
fn object(body: &Bytes) -> Option<Asked> {
    if body.is_empty() {
        return Some(Map::new());
    }
    match serde_json::from_slice(body) {
        Ok(Value::Object(fields)) => Some(fields),
        _ => None,
    }
}

/// The revision an app says it saw, as the Python Host reads it (`janitor_http._revision`):
/// a whole number above nothing, also written as a text.
fn revision(said: Option<&Value>) -> Result<Value, &'static str> {
    let number = match said {
        Some(Value::Bool(_)) => return Err("expected_revision must be an integer"),
        Some(Value::Number(number)) => match (number.as_i64(), number.as_f64()) {
            (Some(whole), _) => Some((i128::from(whole), false)),
            (None, Some(fraction)) if fraction.is_finite() => {
                Some((fraction.trunc() as i128, fraction.fract() != 0.0))
            }
            _ => None,
        },
        Some(Value::String(written)) => python::int(written).map(|whole| (whole, false)),
        _ => None,
    };
    match number {
        None => Err("expected_revision is required"),
        Some((whole, fraction)) if whole < 1 || fraction => {
            Err("expected_revision must be a positive integer")
        }
        Some((whole, _)) => Ok(json!(u64::try_from(whole).unwrap_or(u64::MAX))),
    }
}

/// Tells the apps to read the janitors again.
pub(crate) fn changed(app: &App) {
    let told = json!({ "type": "agent-roster", "kind": "janitor-changed" });
    app.hub.publish(told, app.clock.now_ms());
}

/// `GET /janitors`: the janitors of this Host, and what a Host offers.
async fn list(State(app): State<App>) -> Response {
    let listed = app.janitors.read(&app, |book, world, now_ms| {
        let shown = book.listed(world).into_iter();
        let shown = shown.filter_map(|id| book.shown(&id, world, now_ms).ok());
        shown.map(as_sent).collect::<Vec<Value>>()
    });
    match listed.await {
        Ok(mut janitors) => {
            for janitor in &mut janitors {
                crate::label_checker::show_cadence(&app, janitor).await;
            }
            Json(json!({
                "janitors": janitors,
                "templates": rules::templates(),
                "triggers": rules::triggers(),
                // A scheduled task runs here; turning any other job on is refused in words.
                "runtime_available": true,
                "providers": [],
            }))
            .into_response()
        }
        Err(why) => unavailable(&why),
    }
}

/// `GET /janitors/<session>`.
async fn one(State(app): State<App>, Path(key): Path<String>) -> Response {
    let shown = app
        .janitors
        .read(&app, |book, world, now_ms| book.shown(&key, world, now_ms));
    match shown.await {
        Ok(Ok(mut janitor)) => {
            crate::label_checker::show_cadence(&app, &mut janitor).await;
            Json(json!({ "janitor": as_sent(janitor) })).into_response()
        }
        Ok(Err(refused)) => refusal(&refused),
        Err(why) => unavailable(&why),
    }
}

#[derive(Debug, Deserialize)]
struct Runs {
    limit: Option<String>,
}

/// A count an address carries, as Python's `int()` reads it; `Err` in its words.
fn count(said: Option<&str>, otherwise: i128) -> Result<i128, String> {
    match said {
        None => Ok(otherwise),
        Some(written) => python::int(written).ok_or_else(|| {
            let written = python::text(written);
            format!("invalid literal for int() with base 10: {written}")
        }),
    }
}

/// `GET /janitors/<session>/runs?limit=N`: what a janitor did, the newest run first.
async fn more(
    State(app): State<App>,
    Path((key, what)): Path<(String, String)>,
    Query(query): Query<Runs>,
) -> Response {
    if what == "effective-model-chain" {
        return match app
            .janitors
            .read(&app, |book, world, _| {
                let agent = world.agent(&key)?;
                let config = book.configs.get(&agent.agent_id)?;
                Some(book.model_policy.effective(&key, &agent, config))
            })
            .await
        {
            Ok(Some(chain)) => Json(chain).into_response(),
            Ok(None) => refusal(&Refused::new(404, "not_found", "Janitor unavailable")),
            Err(why) => unavailable(&why),
        };
    }
    let shown = app
        .janitors
        .read(&app, |book, world, now_ms| book.shown(&key, world, now_ms));
    match shown.await {
        Ok(Ok(_)) => {}
        Ok(Err(refused)) => return refusal(&refused),
        Err(why) => return unavailable(&why),
    }
    if what != "runs" {
        return no_route("route");
    }
    let limit = match count(query.limit.as_deref(), i128::from(runs::RUNS_SHOWN)) {
        Ok(limit) => i64::try_from(limit.clamp(1, 100)).unwrap_or(runs::RUNS_SHOWN),
        Err(why) => return bad(&why),
    };
    let read = app
        .janitors
        .read(&app, |book, world, _| book.runs_of(&key, limit, world));
    match read.await {
        Ok(Ok(mut runs)) => {
            // A run of the Goal keeper says what it did with each goal.
            crate::keeper::with_receipts(&app, &mut runs).await;
            Json(json!({ "runs": runs })).into_response()
        }
        Ok(Err(refused)) => refusal(&refused),
        Err(why) => unavailable(&why),
    }
}

/// `GET /janitor-triggers`.
async fn triggers() -> Json<Value> {
    Json(json!({ "triggers": rules::triggers() }))
}

#[derive(Debug, Deserialize)]
struct Preview {
    cron: Option<String>,
    timezone: Option<String>,
    count: Option<String>,
}

/// `GET /janitor-triggers/preview?cron=&timezone=&count=`: when a schedule would run next,
/// without keeping it.
async fn preview(State(app): State<App>, Query(query): Query<Preview>) -> Response {
    let (expression, zone) = (
        query.cron.unwrap_or_default(),
        query.timezone.unwrap_or_default(),
    );
    let asked = match count(query.count.as_deref(), 3) {
        Ok(asked) => i64::try_from(asked.clamp(1, 10)).unwrap_or(3),
        Err(why) => return bad(&why),
    };
    match schedule::preview(&expression, &zone, app.clock.now_ms(), asked) {
        Ok(next_runs) => Json(json!({ "next_runs": next_runs, "timezone": zone })).into_response(),
        Err(why) => bad(&why),
    }
}

/// The fields of a request that are a janitor's own to say.
fn fields(asked: &Asked, keys: &[&str]) -> Asked {
    let named = keys.iter().filter_map(|key| {
        let value = asked.get(*key)?;
        Some(((*key).to_string(), value.clone()))
    });
    named.collect()
}

/// `POST /janitors`: an agent is made a janitor, paused, with the job a request names.
async fn make(State(app): State<App>, body: Bytes) -> Response {
    let Some(asked) = object(&body) else {
        return bad("Expected a JSON object");
    };
    let key = match asked.get("session") {
        Some(Value::String(session)) => python::strip(session).to_string(),
        Some(Value::Null | Value::Bool(false)) | None => String::new(),
        Some(other) => other.to_string(),
    };
    // A janitor that is made as a new agent: one request however often it is sent. A session
    // is kept for it, its agent is made as a janitor, it is given its job like any other, and
    // what is answered is kept as the answer to the request.
    let mut request = None;
    let key = if key.is_empty() {
        let request_id = asked.get("request_id").cloned().unwrap_or(Value::Null);
        let said = Value::Object(asked.clone());
        let begun = app.janitors.begin(&app, &request_id, &said);
        let begun = match begun.await {
            Ok(Ok(begun)) => begun,
            Ok(Err(refused)) => return refusal(&refused),
            Err(why) => return unavailable(&why),
        };
        if begun.completed {
            let answered = begun.response.unwrap_or(Value::Null);
            return Json(json!({ "janitor": as_sent(answered) })).into_response();
        }
        if begun.agent_id.is_none() {
            let who = &begun.identity;
            let seed = JanitorSeed {
                agent_id: None,
                session: begun.session.clone(),
                persona: who.persona.clone(),
                backend: who.backend.clone(),
                cwd: who.cwd.clone(),
                model: who.model.clone(),
                effort: who.effort.clone(),
            };
            let made = match app.fleet.make_janitor(seed).await {
                Ok(Ok(made)) => made,
                Ok(Err(not_made)) => {
                    let why = match not_made {
                        NotMade::Refused(why) => why,
                        NotMade::Occupied { session, .. } => {
                            format!("The session {session} belongs to another agent")
                        }
                        NotMade::Told(told) => told.message,
                    };
                    return refusal(&Refused::new(409, "creation_conflict", why));
                }
                Err(why) => return unavailable(&why),
            };
            let linked = app.janitors.update(&app, |book, _, _| {
                book.creation_made(&begun.request_id, &made.agent_id);
            });
            if let Err(why) = linked.await {
                return unavailable(&why);
            }
        }
        request = Some(begun.request_id.clone());
        begun.session
    } else {
        key
    };
    let job = fields(
        &asked,
        &[
            "template_id",
            "scope",
            "attachments",
            "options",
            "execution",
        ],
    );
    let made = app.janitors.change(&app, &key, |book, world, now_ms| {
        book.create(&key, &job, world, now_ms)
    });
    let janitor = match made.await {
        Ok(Ok(janitor)) => janitor,
        Ok(Err(refused)) => return refusal(&refused),
        Err(why) => return unavailable(&why),
    };
    let janitor = match request {
        None => janitor,
        Some(request_id) => {
            let finished = app.janitors.update(&app, |book, world, now_ms| {
                book.complete_creation(&request_id, &key, world, now_ms)
            });
            match finished.await {
                Ok((Ok(janitor), _)) => janitor,
                Ok((Err(refused), _)) => return refusal(&refused),
                Err(why) => return unavailable(&why),
            }
        }
    };
    changed(&app);
    Json(json!({ "janitor": as_sent(janitor) })).into_response()
}

/// Whether a janitor can be given another tool now: it has no conversation with the tool it
/// has, and no turn of it runs or waits. A janitor that answers one request at a time never
/// has either.
pub(crate) fn tool_can_change(app: &App, key: &str) -> bool {
    let directory = app.fleet.directory();
    let Some(member) = directory.janitor(key) else {
        return false;
    };
    let status = &member.agent.view().status;
    status.backend_session.is_none() && status.live.is_none() && status.waiting.is_empty()
}

/// `POST /janitors/<session>/configure`, `/enabled`, `/reset-defaults`, `/adopt-options`.
async fn change(
    State(app): State<App>,
    Path((key, action)): Path<(String, String)>,
    body: Bytes,
) -> Response {
    let Some(asked) = object(&body) else {
        return bad("Expected a JSON object");
    };
    // The janitor has to be there before anything it is asked is read.
    let there = app
        .janitors
        .read(&app, |book, world, now_ms| book.shown(&key, world, now_ms));
    let agent = match there.await {
        Ok(Ok(janitor)) => janitor,
        Ok(Err(refused)) => return refusal(&refused),
        Err(why) => return unavailable(&why),
    };
    let seen = match revision(asked.get("expected_revision")) {
        Ok(seen) => seen,
        Err(why) => return bad(why),
    };
    let done = match action.as_str() {
        "configure" => {
            let keys = [
                "template_id",
                "scope",
                "attachments",
                "model",
                "effort",
                "execution",
                "backend",
                "options",
            ];
            let job = fields(&asked, &keys);
            // A janitor is given another tool while it has no conversation with the one it
            // has: one that has is started again to change its tool, which this Host does
            // not do yet.
            let another = job.get("backend").filter(|named| !named.is_null());
            let another = another.is_some_and(|named| *named != agent["backend"]);
            if another && !tool_can_change(&app, &key) {
                return refusal(&Refused::new(
                    400,
                    "invalid_janitor",
                    "Unsupported Janitor backend",
                ));
            }
            let configured = app.janitors.change(&app, &key, |book, world, now_ms| {
                book.configure(&key, &seen, &job, world, now_ms)
            });
            configured.await
        }
        "reset-defaults" => {
            let reset = app.janitors.change(&app, &key, |book, world, now_ms| {
                book.reset_defaults(&key, &seen, world, now_ms)
            });
            reset.await
        }
        "enabled" => {
            let Some(Value::Bool(on)) = asked.get("enabled") else {
                return bad("enabled must be true or false");
            };
            let on = *on;
            let seats =
                app.config.accounts == clarp_kernel::recovery::Accounts::Select && app.seats.told();
            let job = agent["template_id"].as_str().unwrap_or_default();
            if on && !rules::runs_here(job, seats) {
                let why = rules::cannot_run(seats);
                let told = Refused::new(503, "janitor_runtime_unavailable", why);
                return refusal(&told);
            }
            let turned = app.janitors.change(&app, &key, |book, world, now_ms| {
                book.set_enabled(&key, &seen, on, world, now_ms)?;
                Ok(Vec::new())
            });
            turned.await
        }
        "release" => {
            let successor = (
                asked.get("successor_session"),
                asked.get("successor_revision"),
            );
            let let_go = app.janitors.change(&app, &key, |book, world, now_ms| {
                book.release(&key, &seen, successor, world, now_ms)
            });
            return match let_go.await {
                Ok(Ok(janitor)) => {
                    changed(&app);
                    let told =
                        json!({ "janitor": as_sent(janitor), "cancellation_pending": false });
                    Json(told).into_response()
                }
                Ok(Err(refused)) => refusal(&refused),
                Err(why) => unavailable(&why),
            };
        }
        "adopt-options" => {
            let options = asked.get("options").cloned().unwrap_or(Value::Null);
            let adopted = app.janitors.change(&app, &key, |book, world, now_ms| {
                book.adopt_options(&key, &seen, &options, world, now_ms)?;
                Ok(Vec::new())
            });
            return match adopted.await {
                Ok(Ok(janitor)) => {
                    changed(&app);
                    Json(json!({ "janitor": as_sent(janitor) })).into_response()
                }
                Ok(Err(refused)) => refusal(&refused),
                Err(why) => unavailable(&why),
            };
        }
        _ => return no_route("action"),
    };
    match done {
        Ok(Ok(janitor)) => {
            changed(&app);
            // A turn that was cut off may still be ending.
            let pending = janitor["cancellation_pending"] == true;
            let told = json!({ "janitor": as_sent(janitor), "cancellation_pending": pending });
            Json(told).into_response()
        }
        Ok(Err(refused)) => refusal(&refused),
        Err(why) => unavailable(&why),
    }
}

#[derive(Debug, Deserialize)]
struct Seen {
    expected_revision: Option<String>,
}

/// `DELETE /janitors/<session>?expected_revision=N`: the janitor is paused for good and its
/// agent put away. Who it was and what it did stay.
async fn remove(
    State(app): State<App>,
    Path(key): Path<String>,
    Query(query): Query<Seen>,
) -> Response {
    let said = query.expected_revision.map(Value::String);
    let seen = match revision(said.as_ref()) {
        Ok(seen) => seen,
        Err(why) => return bad(why),
    };
    let removed = app.janitors.change(&app, &key, |book, world, now_ms| {
        book.remove(&key, &seen, world, now_ms)
    });
    match removed.await {
        Ok(Ok(janitor)) => {
            changed(&app);
            let pending = janitor["cancellation_pending"] == true;
            Json(json!({ "ok": true, "cancellation_pending": pending })).into_response()
        }
        Ok(Err(refused)) => refusal(&refused),
        Err(why) => unavailable(&why),
    }
}

/// `GET /janitor-runs/<run>/context`: what a run is given to read when its turn begins.
async fn run_read(State(app): State<App>, Path(rest): Path<String>) -> Response {
    let Some((run_id, "context")) = rest.split_once('/') else {
        return no_route("route");
    };
    let read = app
        .janitors
        .read(&app, |book, world, _| book.context(run_id, world));
    match read.await {
        Ok(Ok(context)) => Json(context).into_response(),
        Ok(Err(refused)) => refusal(&refused),
        Err(why) => unavailable(&why),
    }
}

/// What a request says under a key, as the Python Host makes a text of it (`str(x or "")`):
/// nothing, no, zero and what is empty are no text.
fn text_of(asked: &Asked, key: &str) -> String {
    match asked.get(key) {
        Some(Value::String(text)) => text.clone(),
        None | Some(Value::Null | Value::Bool(false)) => String::new(),
        Some(Value::Number(number)) if number.as_f64() == Some(0.0) => String::new(),
        Some(Value::Array(items)) if items.is_empty() => String::new(),
        Some(Value::Object(fields)) if fields.is_empty() => String::new(),
        Some(other) => python::repr(other),
    }
}

/// `POST /janitor-runs/<run>/activity`: what a run says of itself before it ends:
/// `worked`, `idle` or `failed`, and one line.
async fn run_said(State(app): State<App>, Path(rest): Path<String>, body: Bytes) -> Response {
    let Some(asked) = object(&body) else {
        return bad("Expected a JSON object");
    };
    let Some((run_id, "activity")) = rest.split_once('/') else {
        return no_route("route");
    };
    let (activity, summary) = (text_of(&asked, "activity"), text_of(&asked, "summary"));
    let said = app.janitors.update(&app, |book, world, _| {
        book.report(run_id, &activity, &summary, world)
    });
    match said.await {
        Ok((Ok(activity), _)) => {
            changed(&app);
            Json(json!({ "activity": activity })).into_response()
        }
        Ok((Err(refused), _)) => refusal(&refused),
        Err(why) => unavailable(&why),
    }
}

/// The janitors a Host installs for itself, each once: an agent of its own for each job,
/// under the id and the session name it has on every Host, paused. Run when the Host
/// starts; what is installed already is left as its owner set it.
pub async fn install(app: &App) -> Result<(), Unavailable> {
    let mut held = app.janitors.held(app).await?;
    let Some(book) = held.as_mut() else {
        return Ok(());
    };
    let before = book.clone();
    let now_ms = app.clock.now_ms();
    for role in rules::roles() {
        if book.builtins.values().any(|installed| installed == role) {
            continue;
        }
        let Ok(job) = rules::template(role) else {
            continue;
        };
        let Some(agent_id) = rules::own_id(role) else {
            continue;
        };
        // The tool its job recommends, where this Host has it; the model and the effort
        // that go with that tool, and none with another.
        let recommended = job["recommended_backend"].as_str().unwrap_or(OWN_TOOL);
        let has = clarp_backend::by_id(recommended).is_some();
        let backend = if has { recommended } else { OWN_TOOL };
        let word = |key: &str| {
            job[key]
                .as_str()
                .filter(|_| has)
                .unwrap_or_default()
                .to_string()
        };
        let mut wanted = json!({ "executor": "ephemeral", "provider": backend });
        if let (Some(wanted), Some(seed)) =
            (wanted.as_object_mut(), job["seed_execution"].as_object())
        {
            wanted.extend(seed.clone());
        }
        // A way to run that this Host's tools have not (shut off from everything) is left out.
        let plain = json!({ "executor": "ephemeral", "provider": backend });
        let execution = rules::execution(role, Some(&wanted), backend)
            .or_else(|_| rules::execution(role, Some(&plain), backend));
        let Ok(execution) = execution else {
            continue;
        };
        let directory = app.fleet.directory();
        let session = match directory.anyone(&format!("clarp-{role}")) {
            None => format!("clarp-{role}"),
            Some(_) => format!("clarp-{role}-{}", &agent_id[..8]),
        };
        let seed = JanitorSeed {
            agent_id: Some(agent_id.to_string()),
            session,
            persona: job["name"].as_str().unwrap_or(role).to_string(),
            backend: backend.to_string(),
            cwd: String::new(),
            model: word("recommended_model"),
            effort: word("recommended_effort"),
        };
        // An id or a session another member has: this one is not installed, and the Host
        // starts without it.
        let Ok(made) = app.fleet.make_janitor(seed).await? else {
            continue;
        };
        let first = job["default_trigger_id"].as_str().unwrap_or_default();
        let seeds = match job["seed_attachments"].as_array() {
            Some(seeds) => seeds.clone(),
            None => vec![json!({ "trigger_id": first, "config": {} })],
        };
        let asked: Vec<Value> = seeds
            .iter()
            .map(|seed| {
                let trigger = seed["trigger_id"].as_str().unwrap_or_default();
                let id = if trigger == first {
                    format!("builtin-{role}-v1")
                } else {
                    format!("builtin-{role}-{trigger}-v1")
                };
                json!({ "attachment_id": id, "trigger_id": trigger, "config": seed["config"] })
            })
            .collect();
        let mut world = Around::new(app);
        let mut keeping = Installing { world: &mut world };
        let Ok(taken) = rules::attachment_values(&json!(asked), &made.agent_id, role, &mut keeping)
        else {
            continue;
        };
        // The one that finds whom a spoken message is for comes with its options chosen: the
        // Python Host installs it with what its owner had set for the router before there
        // were janitors, which on a Host where nothing was set is what the job has by default.
        let mut options = Map::new();
        if role == "message-delegator" {
            for option in job["options"].as_array().map_or(&[][..], Vec::as_slice) {
                if let Some(key) = option["key"].as_str() {
                    options.insert(key.to_string(), option["default"].clone());
                }
            }
        }
        let config = Config {
            template_id: role.clone(),
            revision: 1,
            generation: 1,
            execution,
            options,
            created_at: now_ms,
            updated_at: now_ms,
            ..Config::default()
        };
        book.install(role, &made.agent_id, config, taken);
    }
    if *book != before {
        keep(app, Some(&before), book, Default::default()).await?;
    }
    Ok(())
}

/// Whoever keeps attachments while the Host's own janitors are installed: no attachment is
/// anybody's yet.
struct Installing<'a, 'b> {
    world: &'a mut Around<'b>,
}

impl rules::Kept for Installing<'_, '_> {
    fn attachment_free(&self, _attachment_id: &str, _agent_id: &str) -> bool {
        true
    }

    fn zone(&self, name: &str) -> bool {
        self.world.zone(name)
    }

    fn draw(&mut self) -> String {
        self.world.draw()
    }
}

/// The job each janitor has, by the agent's id: for whoever shows a janitor's row.
pub async fn jobs(app: &App) -> std::collections::BTreeMap<String, String> {
    let jobs = app.janitors.read(app, |book, _, _| {
        let all = book.configs.iter();
        all.map(|(id, config)| (id.clone(), config.template_id.clone()))
            .collect()
    });
    jobs.await.unwrap_or_default()
}

async fn policy(State(app): State<App>) -> Response {
    match app
        .janitors
        .read(&app, |book, _, _| json!(book.model_policy))
        .await
    {
        Ok(value) => Json(value).into_response(),
        Err(why) => unavailable(&why),
    }
}

async fn set_policy(State(app): State<App>, body: Bytes) -> Response {
    let Some(body) = object(&body) else {
        return bad("Expected a JSON object");
    };
    let result = app
        .janitors
        .with(&app, |book, world, _| {
            book.model_policy.configure(
                body.get("configuration").unwrap_or(&Value::Null),
                body.get("expected_revision").unwrap_or(&Value::Null),
                world,
                &book.configs,
            )
        })
        .await;
    match result {
        Ok(Ok(value)) => Json(value).into_response(),
        Ok(Err(why)) if why.code.is_empty() => bad(&why.message),
        Ok(Err(why)) => refusal(&why),
        Err(why) => unavailable(&why),
    }
}

pub fn routes() -> Router<App> {
    Router::new()
        .route("/janitor-policy", get(policy).post(set_policy))
        .route("/janitors", get(list).post(make))
        .route("/janitors/{session}", get(one).delete(remove))
        .route("/janitors/{session}/{action}", get(more).post(change))
        .route("/janitor-triggers", get(triggers))
        .route("/janitor-triggers/preview", get(preview))
        .route("/janitor-runs/{*rest}", get(run_read).post(run_said))
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn a_refused_janitor_book_still_gives_each_keeper_its_pass() {
        let place = crate::unkept::at_the_port::place("janitor-passes");
        let app = place.app();
        place.refuse_reads("janitors");
        tick(&app).await;
        // Quota and labels do their work beside the tick. Wait for their actual reports.
        let lines = tokio::time::timeout(Duration::from_secs(3), async {
            loop {
                let lines = place.said.lock().unwrap().clone();
                if lines.len() >= 4 {
                    break lines;
                }
                place.notice.notified().await;
            }
        })
        .await
        .expect("each obligation reports the refusal");
        let mut kinds = Vec::new();
        for line in &lines {
            let (kind, why) = line
                .strip_prefix("clarp-host: a record of the kind ")
                .unwrap()
                .split_once(" was not kept: ")
                .unwrap();
            assert!(why.ends_with("storage failed: read refused here"));
            kinds.push(kind);
        }
        kinds.sort_unstable();
        assert_eq!(
            kinds,
            ["audio-facts", "janitors", "label-auditor", "quota-monitor"]
        );
    }

    #[test]
    fn a_revision_is_read_as_the_python_host_reads_it() {
        let read = |said: Value| revision(Some(&said));
        assert_eq!(read(json!(3)), Ok(json!(3)));
        assert_eq!(read(json!("3")), Ok(json!(3)));
        assert_eq!(read(json!(" 12 ")), Ok(json!(12)));
        assert_eq!(read(json!(3.0)), Ok(json!(3)));
        assert_eq!(
            read(json!(3.5)),
            Err("expected_revision must be a positive integer")
        );
        assert_eq!(
            read(json!(0)),
            Err("expected_revision must be a positive integer")
        );
        assert_eq!(
            read(json!(-2)),
            Err("expected_revision must be a positive integer")
        );
        assert_eq!(
            read(json!(true)),
            Err("expected_revision must be an integer")
        );
        assert_eq!(read(json!("three")), Err("expected_revision is required"));
        assert_eq!(read(json!("3.5")), Err("expected_revision is required"));
        assert_eq!(read(json!(null)), Err("expected_revision is required"));
        assert_eq!(read(json!([3])), Err("expected_revision is required"));
        assert_eq!(revision(None), Err("expected_revision is required"));
    }

    #[test]
    fn a_change_of_the_book_is_shown_only_once_it_is_kept() {
        let run = |write: Result<(), &'static str>| {
            let said = std::cell::RefCell::new(Vec::new());
            let written = async {
                said.borrow_mut().push("written");
                write
            };
            let runtime = tokio::runtime::Builder::new_current_thread()
                .build()
                .unwrap();
            let done =
                runtime.block_on(kept_then_shown(written, || said.borrow_mut().push("shown")));
            (done, said.into_inner())
        };
        // Kept, then shown.
        assert_eq!(run(Ok(())), (Ok(()), vec!["written", "shown"]));
        // A write that fails shows nothing, and says why.
        assert_eq!(run(Err("full")), (Err("full"), vec!["written"]));
    }

    #[test]
    fn a_janitor_goes_out_with_both_sides_of_its_scope() {
        let own = as_sent(json!({ "session": "clarp-goal-keeper", "scope": {} }));
        assert_eq!(
            own["scope"],
            json!({ "agent_ids": [], "exclude_agent_ids": [] })
        );
        let set = as_sent(json!({ "scope": { "agent_ids": ["a"], "exclude_agent_ids": ["b"] } }));
        assert_eq!(
            set["scope"],
            json!({ "agent_ids": ["a"], "exclude_agent_ids": ["b"] })
        );
    }
}
