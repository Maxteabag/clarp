//! The janitors at the Host's edge: agents with a job of maintenance, as the apps read and
//! set them.
//!
//! The rules are the kernel's ([`clarp_kernel::janitors`]): what a Host offers, what a
//! janitor's job may be, and the book that is kept of them. Here is what is kept (the book,
//! whole, with the fleet), who a janitor is (a member of the fleet that is no agent of the
//! roster: [`crate::fleet::Directory::janitors`]), the seven janitors a Host installs for
//! itself, and the routes, which answer as the Python Host's do
//! (`server/lib/janitor_http.py`): the same statuses, bodies and words.
//!
//! **Nothing runs yet.** A janitor is made, set, paused and removed, and shown; no trigger
//! starts one, so none can be turned on: that is refused in words, with the Python Host's
//! word for a Host that cannot run janitors (`janitor_runtime_unavailable`), and
//! `GET /janitors` says `runtime_available: false`. The janitors of the Host's own are
//! installed paused for the same reason, where the Python Host starts three of them.

use std::sync::Arc;

use axum::body::Bytes;
use axum::extract::{Path, Query, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::get;
use axum::{Json, Router};
use clarp_kernel::janitors::book::{Agent, Asked, Book, Config, Effect, World};
use clarp_kernel::janitors::{self as rules, Refused, schedule};
use clarp_kernel::python;
use serde::Deserialize;
use serde_json::{Map, Value, json};

use crate::agent::Unavailable;
use crate::edge::App;
use crate::fleet::{Amend, Directory, JanitorSeed};
use crate::ids;

/// The kind of thing the janitors' book is, among what is left with the fleet.
const KIND: &str = "janitors";
const ALL: &str = "all";
/// What a schedule is taken to be apart when it cannot be read: a day.
const A_DAY_S: i64 = 86_400;
/// The tool a janitor of the Host's own runs on where its job recommends one this Host
/// has not.
const OWN_TOOL: &str = "codex";

/// What an app is told when it turns a janitor on.
const CANNOT_RUN: &str = "This Host cannot run janitors yet: a janitor is made, set and \
                          paused here, and nothing starts one. Nothing was turned on.";

/// The janitors of this Host. `None` until the book was read where it is kept.
#[derive(Default)]
pub struct Desk {
    book: tokio::sync::Mutex<Option<Book>>,
}

/// What the book asks of the Host around it: the fleet as it stands, the machine's zones,
/// the pictures the Host ships.
struct Around<'a> {
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

    fn efforts(&self, backend: &str) -> Vec<String> {
        let efforts = clarp_backend::efforts(backend).iter();
        efforts.map(|effort| (*effort).to_string()).collect()
    }

    fn base_interval(&self, expression: &str, zone: &str, now_ms: i64) -> i64 {
        schedule::base_interval(expression, zone, now_ms).unwrap_or(A_DAY_S)
    }

    fn face(&self, file: &str) -> String {
        self.app.faces.janitor(file)
    }
}

impl Desk {
    /// The book, read where it is kept the first time it is asked for.
    async fn held(
        &self,
        app: &App,
    ) -> Result<tokio::sync::MutexGuard<'_, Option<Book>>, Unavailable> {
        let mut held = self.book.lock().await;
        if held.is_none() {
            let kept = app.fleet.kept(KIND, ALL.to_string()).await?;
            // What cannot be read is a book with nothing in it.
            let book = kept.and_then(|kept| serde_json::from_slice(&kept).ok());
            *held = Some(book.unwrap_or_default());
        }
        Ok(held)
    }

    /// One reading of the book.
    async fn read<T>(
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
    async fn change(
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
            let kept = serde_json::to_vec(book).unwrap_or_default();
            app.fleet.keep(KIND, ALL.to_string(), Some(kept)).await?;
        }
        for effect in effects {
            let (agent_id, amend) = match effect {
                Effect::Convert { agent_id } => (agent_id, Amend::Janitor(true)),
                Effect::PutAway { agent_id } => (agent_id, Amend::Archive(true)),
                Effect::Release { agent_id } => (agent_id, Amend::Janitor(false)),
                Effect::Tune {
                    agent_id,
                    model,
                    effort,
                    ..
                } => (agent_id, Amend::Tune { model, effort }),
            };
            let _ = app.fleet.amend_any(agent_id, amend).await?;
        }
        Ok(book.shown(key, &Around::new(app), now_ms))
    }
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
fn changed(app: &App) {
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
        Ok(janitors) => Json(json!({
            "janitors": janitors,
            "templates": rules::templates(),
            "triggers": rules::triggers(),
            // No trigger starts a janitor on this Host yet.
            "runtime_available": false,
            "providers": [],
        }))
        .into_response(),
        Err(why) => unavailable(&why),
    }
}

/// `GET /janitors/<session>`.
async fn one(State(app): State<App>, Path(key): Path<String>) -> Response {
    let shown = app
        .janitors
        .read(&app, |book, world, now_ms| book.shown(&key, world, now_ms));
    match shown.await {
        Ok(Ok(janitor)) => Json(json!({ "janitor": as_sent(janitor) })).into_response(),
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

/// `GET /janitors/<session>/runs`: what a janitor did. No janitor has run here.
async fn more(
    State(app): State<App>,
    Path((key, what)): Path<(String, String)>,
    Query(query): Query<Runs>,
) -> Response {
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
    match count(query.limit.as_deref(), 30) {
        Ok(_) => Json(json!({ "runs": [] })).into_response(),
        Err(why) => bad(&why),
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
    if key.is_empty() {
        // An agent that is made as a janitor, with a request that is one request however
        // often it is sent: with the runner.
        let why = "Making a new agent as a janitor is not built on this Host yet: make the \
                   agent, then give it the job. Nothing was made.";
        let told = json!({ "error": why, "code": "not_built" });
        return (StatusCode::NOT_IMPLEMENTED, Json(told)).into_response();
    }
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
    match made.await {
        Ok(Ok(janitor)) => {
            changed(&app);
            Json(json!({ "janitor": as_sent(janitor) })).into_response()
        }
        Ok(Err(refused)) => refusal(&refused),
        Err(why) => unavailable(&why),
    }
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
            // A janitor keeps the tool it has: an agent's tool is not changed on this Host.
            let another = job.get("backend").filter(|named| !named.is_null());
            if another.is_some_and(|named| *named != agent["backend"]) {
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
            if *on {
                let told = Refused::new(503, "janitor_runtime_unavailable", CANNOT_RUN);
                return refusal(&told);
            }
            let paused = app.janitors.change(&app, &key, |book, world, now_ms| {
                book.set_enabled(&key, &seen, false, world, now_ms)?;
                Ok(Vec::new())
            });
            paused.await
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
            // Nothing of a janitor runs here, so nothing is still being stopped.
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
    let mut held = match app.janitors.held(&app).await {
        Ok(held) => held,
        Err(why) => return unavailable(&why),
    };
    let Some(book) = held.as_mut() else {
        return unavailable(&Unavailable(
            "the janitors' book could not be read".to_string(),
        ));
    };
    let now_ms = app.clock.now_ms();
    let effects = match book.remove(&key, &seen, &Around::new(&app), now_ms) {
        Ok(effects) => effects,
        Err(refused) => return refusal(&refused),
    };
    let kept = serde_json::to_vec(book).unwrap_or_default();
    if let Err(why) = app.fleet.keep(KIND, ALL.to_string(), Some(kept)).await {
        return unavailable(&why);
    }
    for effect in effects {
        if let Effect::PutAway { agent_id } = effect
            && let Err(why) = app.fleet.amend_any(agent_id, Amend::Archive(true)).await
        {
            return unavailable(&why);
        }
    }
    drop(held);
    changed(&app);
    Json(json!({ "ok": true, "cancellation_pending": false })).into_response()
}

/// What is asked of a janitor's run: no janitor has run here.
async fn no_run() -> Response {
    no_route("route")
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
        let kept = serde_json::to_vec(book).unwrap_or_default();
        app.fleet.keep(KIND, ALL.to_string(), Some(kept)).await?;
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

pub fn routes() -> Router<App> {
    Router::new()
        .route("/janitors", get(list).post(make))
        .route("/janitors/{session}", get(one).delete(remove))
        .route("/janitors/{session}/{action}", get(more).post(change))
        .route("/janitor-triggers", get(triggers))
        .route("/janitor-triggers/preview", get(preview))
        .route("/janitor-runs/{*rest}", get(no_run).post(no_run))
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

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
