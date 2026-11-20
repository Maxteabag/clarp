//! Signing a harness in to a provider of models from an app (`harness_model_providers`).
//!
//! OpenCode, and DeepSeek which runs through it, serve models of many providers, each with
//! a sign-in of its own. An app lists them (`GET /harness-providers`), lists a provider's
//! models, stores an API key, signs in through the provider's own device or code flow, and
//! signs out. The credentials are OpenCode's, in its own store (`auth.json`): a key is
//! written there and nowhere else, never logged, never answered and kept no longer than the
//! write. A sign-in through the provider runs in a private `opencode serve`, which writes
//! the store itself. The rules are the Python Host's (`clarp_kernel::harness_providers`).
//!
//! The tools' logins, settings and stores are one per machine, and one Host keeps a
//! machine's tools (the lead's ruling of 20 Nov 2026): a Host started with `--accounts
//! select` changes them, any other refuses every change in words and writes nothing. The
//! readings answer in every mode, but only a Host that keeps the tools ever starts OpenCode:
//! anywhere else the listing is made from OpenCode's files alone, with the sign-in methods
//! that were remembered, or the API key alone when none were (the lead's review of 20 Nov
//! 2026: a Host beside the Python Host must not run the owner's tool in his home, and the
//! methods serve only a sign-in, which such a Host refuses).
//!
//! One desk owns the sign-ins that run: a thread that is sent what happens and answers in
//! turn. What waits on OpenCode (starting its server, its answers, ending it) runs on
//! threads of its own, which tell the desk when they are done.

mod serve;

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::{Arc, mpsc};
use std::time::{Duration, Instant};

use axum::extract::{Extension, Path as Asked, Query, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use bytes::Bytes;
use clarp_kernel::harness_providers::{self as rules, Refused, Start};
use clarp_kernel::python;
use clarp_kernel::recovery::Accounts;
use serde_json::{Map, Value, json};
use tokio::sync::oneshot;

use crate::edge::{ApiError, App};
use crate::models::Shelf;
use serve::{Client, Serve};

/// A status and a body, as the Python Host answers them.
type Answer = (u16, Value);

fn answer((status, body): Answer) -> Response {
    let status = StatusCode::from_u16(status).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR);
    (status, Json(body)).into_response()
}

fn refusal(why: &Refused) -> Answer {
    (why.status(), json!({ "error": why.words() }))
}

fn unavailable() -> Response {
    ApiError::new(
        StatusCode::SERVICE_UNAVAILABLE,
        "the sign-in desk is not running",
    )
    .into_response()
}

/// A directory of the XDG spec as OpenCode resolves it: the variable when it says something,
/// else its place in the home.
fn xdg(variable: &str, default: &str, home: &Path) -> PathBuf {
    std::env::var(variable)
        .ok()
        .map(|said| said.trim().to_owned())
        .filter(|said| !said.is_empty())
        .map_or_else(|| home.join(default), PathBuf::from)
}

/// OpenCode's credential store.
fn auth_path(home: &Path) -> PathBuf {
    xdg("XDG_DATA_HOME", ".local/share", home)
        .join("opencode")
        .join("auth.json")
}

/// The store's bytes; `None` when there is none, `Err` when it could not be read.
fn read_store(path: &Path) -> Result<Option<Vec<u8>>, ()> {
    match std::fs::read(path) {
        Ok(bytes) => Ok(Some(bytes)),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(_) => Err(()),
    }
}

fn as_read(read: &Result<Option<Vec<u8>>, ()>) -> Result<Option<&[u8]>, ()> {
    read.as_ref().map(|bytes| bytes.as_deref()).map_err(|_| ())
}

/// OpenCode's catalogue of providers (its cache of models.dev): the providers it describes.
fn catalogue(home: &Path) -> Map<String, Value> {
    crate::logins::opencode::store(home, "XDG_CACHE_HOME", ".cache", "models.json")
        .into_iter()
        .filter(|(_, entry)| entry.is_object())
        .collect()
}

/// Whether a variable of this Host's environment says something: a provider's key may be
/// there, which is the Host's and not Clarp's to remove.
fn env_set(name: &str) -> bool {
    !name.is_empty()
        && !name.contains(['\0', '='])
        && std::env::var_os(name).is_some_and(|value| !value.is_empty())
}

/// OpenCode's program, where it is on this machine.
fn program() -> Option<String> {
    clarp_backend::program(rules::STORE_HARNESS).filter(|program| crate::logins::installed(program))
}

/// Replaces the store in one move: written whole beside it, 0600, in a folder that is
/// made 0700, then put in its place.
fn write_store(path: &Path, text: &str) -> std::io::Result<()> {
    use std::io::Write as _;
    use std::os::unix::fs::{DirBuilderExt as _, OpenOptionsExt as _, PermissionsExt as _};
    let folder = path.parent().ok_or(std::io::ErrorKind::NotFound)?;
    if let Some(above) = folder.parent() {
        std::fs::create_dir_all(above)?;
    }
    match std::fs::DirBuilder::new().mode(0o700).create(folder) {
        Err(e) if e.kind() != std::io::ErrorKind::AlreadyExists => return Err(e),
        _ => {}
    }
    let aside = folder.join(format!(".auth.{}.tmp", crate::ids::hex(6)));
    let written = (|| {
        let mut file = std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&aside)?;
        file.set_permissions(std::fs::Permissions::from_mode(0o600))?;
        file.write_all(text.as_bytes())?;
        file.sync_all()?;
        std::fs::rename(&aside, path)
    })();
    if written.is_err() {
        let _ = std::fs::remove_file(&aside);
    }
    written
}

/// What the desk is asked, and what its threads tell it.
enum Ask {
    List {
        harness: &'static str,
        remembered: Option<String>,
        reply: oneshot::Sender<Value>,
    },
    Save {
        asked: Map<String, Value>,
        remembered: Option<String>,
        shelf: Arc<Shelf>,
        reply: oneshot::Sender<Answer>,
    },
    Logout {
        asked: Map<String, Value>,
        remembered: Option<String>,
        shelf: Arc<Shelf>,
        reply: oneshot::Sender<Answer>,
    },
    Start {
        asked: Map<String, Value>,
        remembered: Option<String>,
        now_ms: i64,
        shelf: Arc<Shelf>,
        reply: oneshot::Sender<Answer>,
    },
    Code {
        provider: String,
        code: String,
        reply: oneshot::Sender<Answer>,
    },
    /// OpenCode's server answered the start of a sign-in, or did not start.
    Authorized {
        begun: Begun,
        outcome: Result<(Serve, Value), String>,
        reply: oneshot::Sender<Answer>,
    },
    /// The provider's page was finished, or the code was given: OpenCode said how it went.
    Called {
        provider: String,
        attempt: u64,
        outcome: Result<Value, String>,
        code: bool,
        reply: Option<oneshot::Sender<Answer>>,
    },
    /// A sign-in's server has ended.
    Closed {
        provider: String,
        attempt: u64,
        state: &'static str,
        error: String,
        shelf: Arc<Shelf>,
        reply: Option<oneshot::Sender<Answer>>,
    },
    /// Whether this asker may ask OpenCode for its sign-in methods: one at a time, and only
    /// while nothing fresh is remembered. `fresh` reads what is remembered when the desk
    /// decides, not when the asker asked: a prober keeps what it found before it says
    /// `Probed`, so an asker decided after that sees it.
    Probe {
        fresh: Box<dyn FnOnce() -> bool + Send>,
        reply: oneshot::Sender<bool>,
    },
    Probed,
}

/// A sign-in that was begun and waits for OpenCode's server.
struct Begun {
    provider: String,
    attempt: u64,
    started_at: i64,
    label: String,
    method: u64,
    shelf: Arc<Shelf>,
}

/// A sign-in whose server runs.
struct Session {
    attempt: u64,
    method: u64,
    mode: String,
    started_at: i64,
    label: String,
    authorization: Value,
    client: Client,
    serve: Option<Serve>,
    finishing: bool,
    deadline: Option<Instant>,
    shelf: Arc<Shelf>,
}

/// The way to the desk.
pub struct Desk {
    asks: mpsc::Sender<Ask>,
    home: PathBuf,
}

impl Desk {
    /// A desk whose sign-ins in a browser are given up after `timeout`.
    pub fn new(home: PathBuf, timeout: Duration) -> Arc<Self> {
        let (asks, inbox) = mpsc::channel();
        let actor = Actor {
            asks: asks.clone(),
            tasks: Map::new(),
            sessions: HashMap::new(),
            attempts: 0,
            probing: false,
            home: home.clone(),
            timeout,
        };
        let _ = std::thread::Builder::new()
            .name("harness-providers".into())
            .spawn(move || actor.run(&inbox));
        Arc::new(Self { asks, home })
    }

    async fn ask<T>(&self, make: impl FnOnce(oneshot::Sender<T>) -> Ask) -> Option<T> {
        let (reply, answered) = oneshot::channel();
        self.asks.send(make(reply)).ok()?;
        answered.await.ok()
    }
}

struct Actor {
    asks: mpsc::Sender<Ask>,
    /// Each provider's sign-in as the apps poll it.
    tasks: Map<String, Value>,
    sessions: HashMap<String, Session>,
    attempts: u64,
    probing: bool,
    home: PathBuf,
    timeout: Duration,
}

impl Actor {
    fn run(mut self, inbox: &mpsc::Receiver<Ask>) {
        loop {
            let next = self
                .sessions
                .iter()
                .filter_map(|(p, s)| s.deadline.map(|at| (at, p.clone(), s.attempt)))
                .min_by_key(|(at, _, _)| *at);
            let ask = match next {
                Some((at, provider, attempt)) => {
                    match inbox.recv_timeout(at.saturating_duration_since(Instant::now())) {
                        Ok(ask) => ask,
                        Err(mpsc::RecvTimeoutError::Timeout) => {
                            let error = rules::TIMED_OUT.to_string();
                            self.finish(&provider, attempt, "failed", error, None);
                            continue;
                        }
                        Err(mpsc::RecvTimeoutError::Disconnected) => return,
                    }
                }
                None => match inbox.recv() {
                    Ok(ask) => ask,
                    Err(_) => return,
                },
            };
            self.handle(ask);
        }
    }

    fn task(&self, provider: &str) -> Value {
        self.tasks
            .get(provider)
            .cloned()
            .unwrap_or_else(|| rules::idle(provider))
    }

    fn handle(&mut self, ask: Ask) {
        match ask {
            Ask::List {
                harness,
                remembered,
                reply,
            } => {
                let _ = reply.send(self.listing(harness, remembered.as_deref()));
            }
            Ask::Save {
                asked,
                remembered,
                shelf,
                reply,
            } => {
                let _ = reply.send(self.save(&asked, remembered.as_deref(), &shelf));
            }
            Ask::Logout {
                asked,
                remembered,
                shelf,
                reply,
            } => {
                let _ = reply.send(self.logout(&asked, remembered.as_deref(), &shelf));
            }
            Ask::Start {
                asked,
                remembered,
                now_ms,
                shelf,
                reply,
            } => {
                self.start(&asked, remembered.as_deref(), now_ms, shelf, reply);
            }
            Ask::Code {
                provider,
                code,
                reply,
            } => self.code(&provider, code, reply),
            Ask::Authorized {
                begun,
                outcome,
                reply,
            } => self.authorized(begun, outcome, reply),
            Ask::Called {
                provider,
                attempt,
                outcome,
                code,
                reply,
            } => {
                let (state, error) = match outcome {
                    Ok(Value::Bool(false)) => {
                        let why = if code {
                            rules::CODE_REFUSED
                        } else {
                            rules::NOT_COMPLETED
                        };
                        ("failed", why.to_string())
                    }
                    Ok(_) => ("complete", String::new()),
                    Err(why) => ("failed", rules::bounded_error(&why)),
                };
                self.finish(&provider, attempt, state, error, reply);
            }
            Ask::Closed {
                provider,
                attempt,
                state,
                error,
                shelf,
                reply,
            } => {
                self.closed(&provider, attempt, state, &error, &shelf, reply);
            }
            Ask::Probe { fresh, reply } => {
                let granted = !self.probing && !fresh();
                self.probing |= granted;
                let _ = reply.send(granted);
            }
            Ask::Probed => self.probing = false,
        }
    }

    fn listing(&self, harness: &str, remembered: Option<&str>) -> Value {
        let catalogue = catalogue(&self.home);
        let types = rules::listed_types(as_read(&read_store(&auth_path(&self.home))));
        let methods = rules::remembered(remembered).methods;
        let read = rules::Read {
            catalogue: &catalogue,
            types: &types,
            methods: &methods,
            env: &env_set,
        };
        rules::listing(
            harness,
            &read,
            Value::Object(self.tasks.clone()),
            program().is_some(),
        )
    }

    /// Writes the store a change made, and answers with the provider's row.
    fn written(
        &mut self,
        provider: &str,
        store: &Map<String, Value>,
        remembered: Option<&str>,
        shelf: &Shelf,
    ) -> Answer {
        if write_store(&auth_path(&self.home), &rules::store_text(store)).is_err() {
            return (500, json!({ "error": rules::UNWRITABLE }));
        }
        self.tasks.remove(provider);
        // New credentials change which models OpenCode lists.
        shelf.forget();
        let methods = rules::remembered(remembered).methods;
        let types = rules::credential_types(store);
        let row = rules::provider_row(provider, &catalogue(&self.home), &types, &methods, &env_set);
        (200, json!({ "ok": true, "provider": row }))
    }

    fn save(
        &mut self,
        asked: &Map<String, Value>,
        remembered: Option<&str>,
        shelf: &Shelf,
    ) -> Answer {
        let field = |name: &str| asked.get(name).unwrap_or(&Value::Null);
        let read = read_store(&auth_path(&self.home));
        match rules::save_key(
            field("harness"),
            field("provider"),
            field("key"),
            as_read(&read),
        ) {
            Err(why) => refusal(&why),
            Ok((provider, store)) => self.written(&provider, &store, remembered, shelf),
        }
    }

    fn logout(
        &mut self,
        asked: &Map<String, Value>,
        remembered: Option<&str>,
        shelf: &Shelf,
    ) -> Answer {
        let field = |name: &str| asked.get(name).unwrap_or(&Value::Null);
        let read = read_store(&auth_path(&self.home));
        let methods = rules::remembered(remembered).methods;
        let out = rules::logout(
            field("harness"),
            field("provider"),
            as_read(&read),
            &catalogue(&self.home),
            &methods,
            &env_set,
        );
        match out {
            Err(why) => refusal(&why),
            Ok((provider, store)) => self.written(&provider, &store, remembered, shelf),
        }
    }

    fn start(
        &mut self,
        asked: &Map<String, Value>,
        remembered: Option<&str>,
        now_ms: i64,
        shelf: Arc<Shelf>,
        reply: oneshot::Sender<Answer>,
    ) {
        let field = |name: &str| asked.get(name).unwrap_or(&Value::Null);
        let methods = rules::remembered(remembered).methods;
        let found = program();
        let checked = rules::start(
            field("harness"),
            field("provider"),
            field("method"),
            field("inputs"),
            &methods,
            &catalogue(&self.home),
            found.is_some(),
        );
        let (start, program) = match (checked, found) {
            (Err(why), _) => {
                let _ = reply.send(refusal(&why));
                return;
            }
            (Ok(start), Some(program)) => (start, program),
            (Ok(_), None) => {
                let _ = reply.send(refusal(&Refused::Conflict(rules::NOT_INSTALLED.into())));
                return;
            }
        };
        let Start {
            provider,
            method,
            inputs,
            label,
        } = start;
        let current = self.task(&provider);
        if current["status"] == "running" {
            let _ = reply.send((202, current));
            return;
        }
        let running = rules::task(&rules::Task {
            provider: &provider,
            status: "running",
            started_at: now_ms,
            method_label: &label,
            authorization: &Value::Null,
            error: "",
            code_submitted: false,
        });
        self.tasks.insert(provider.clone(), running);
        self.attempts += 1;
        let begun = Begun {
            provider,
            attempt: self.attempts,
            started_at: now_ms,
            label,
            method,
            shelf,
        };
        let home = self.home.clone();
        let tell = self.asks.clone();
        std::thread::spawn(move || {
            let path = format!("/provider/{}/oauth/authorize", begun.provider);
            let body = json!({ "method": begun.method, "inputs": inputs });
            let outcome = Serve::start(&program, &home).and_then(|serve| {
                let authorization = serve.client.request("POST", &path, Some(&body), 30)?;
                Ok((serve, authorization))
            });
            let _ = tell.send(Ask::Authorized {
                begun,
                outcome,
                reply,
            });
        });
    }

    fn authorized(
        &mut self,
        begun: Begun,
        outcome: Result<(Serve, Value), String>,
        reply: oneshot::Sender<Answer>,
    ) {
        let Begun {
            provider,
            attempt,
            started_at,
            label,
            method,
            shelf,
        } = begun;
        let failed = |error: &str| {
            rules::task(&rules::Task {
                provider: &provider,
                status: "failed",
                started_at,
                method_label: &label,
                authorization: &Value::Null,
                error,
                code_submitted: false,
            })
        };
        let (serve, authorization) = match outcome {
            Err(why) => {
                let task = failed(&rules::bounded_error(&why));
                self.tasks.insert(provider.clone(), task.clone());
                let _ = reply.send((202, task));
                return;
            }
            Ok((serve, answer)) => match rules::authorization(&answer) {
                Some(authorization) => (serve, authorization),
                None => {
                    // An answer of another shape fails the sign-in and ends its server (R156).
                    drop(serve);
                    let task = failed(rules::NOT_ANSWERED);
                    self.tasks.insert(provider.clone(), task.clone());
                    let _ = reply.send((202, task));
                    return;
                }
            },
        };
        let mode = match &authorization["method"] {
            Value::String(mode) => mode.clone(),
            _ => String::new(),
        };
        if let Some(mut earlier) = self.sessions.remove(&provider) {
            let serve = earlier.serve.take();
            std::thread::spawn(move || drop(serve));
        }
        let task = rules::task(&rules::Task {
            provider: &provider,
            status: "running",
            started_at,
            method_label: &label,
            authorization: &authorization,
            error: "",
            code_submitted: false,
        });
        self.tasks.insert(provider.clone(), task.clone());
        let client = serve.client.clone();
        if mode == "auto" {
            let tell = self.asks.clone();
            let waiting = client.clone();
            let named = provider.clone();
            std::thread::spawn(move || {
                let path = format!("/provider/{named}/oauth/callback");
                let body = json!({ "method": method });
                let seconds = u64::try_from(rules::LOGIN_TIMEOUT_SECONDS).unwrap_or(600);
                let outcome = waiting.request("POST", &path, Some(&body), seconds);
                let _ = tell.send(Ask::Called {
                    provider: named,
                    attempt,
                    outcome,
                    code: false,
                    reply: None,
                });
            });
        }
        let session = Session {
            attempt,
            method,
            deadline: (mode != "auto").then(|| Instant::now() + self.timeout),
            mode,
            started_at,
            label,
            authorization,
            client,
            serve: Some(serve),
            finishing: false,
            shelf,
        };
        self.sessions.insert(provider, session);
        let _ = reply.send((202, task));
    }

    fn code(&mut self, provider: &str, code: String, reply: oneshot::Sender<Answer>) {
        let running = self
            .tasks
            .get(provider)
            .is_some_and(|t| t["status"] == "running");
        let Some(session) = self.sessions.get(provider).filter(|_| running) else {
            let _ = reply.send(refusal(&Refused::Conflict(rules::NO_CODE_WAITING.into())));
            return;
        };
        if session.mode != "code" {
            let _ = reply.send(refusal(&Refused::Conflict(rules::NOT_ASKING_CODE.into())));
            return;
        }
        if let Some(Value::Object(task)) = self.tasks.get_mut(provider) {
            task.insert("awaiting_code".into(), json!(false));
            task.insert("code_submitted".into(), json!(true));
        }
        let (client, attempt, method) = (session.client.clone(), session.attempt, session.method);
        let tell = self.asks.clone();
        let provider = provider.to_string();
        std::thread::spawn(move || {
            let path = format!("/provider/{provider}/oauth/callback");
            let body = json!({ "method": method, "code": code });
            let outcome = client.request("POST", &path, Some(&body), 60);
            let _ = tell.send(Ask::Called {
                provider,
                attempt,
                outcome,
                code: true,
                reply: Some(reply),
            });
        });
    }

    /// Ends a sign-in once: its server is ended first, and only then is it told how it went,
    /// so whoever sees it complete reads a catalogue that has the provider's models.
    fn finish(
        &mut self,
        provider: &str,
        attempt: u64,
        state: &'static str,
        error: String,
        reply: Option<oneshot::Sender<Answer>>,
    ) {
        let session = self
            .sessions
            .get_mut(provider)
            .filter(|s| s.attempt == attempt && !s.finishing);
        let Some(session) = session else {
            if let Some(reply) = reply {
                let _ = reply.send((200, self.task(provider)));
            }
            return;
        };
        session.finishing = true;
        session.deadline = None;
        let serve = session.serve.take();
        let shelf = session.shelf.clone();
        let tell = self.asks.clone();
        let provider = provider.to_string();
        std::thread::spawn(move || {
            drop(serve);
            let _ = tell.send(Ask::Closed {
                provider,
                attempt,
                state,
                error,
                shelf,
                reply,
            });
        });
    }

    fn closed(
        &mut self,
        provider: &str,
        attempt: u64,
        state: &'static str,
        error: &str,
        shelf: &Shelf,
        reply: Option<oneshot::Sender<Answer>>,
    ) {
        if state == "complete" {
            shelf.forget();
        }
        let owned = self
            .sessions
            .get(provider)
            .is_some_and(|s| s.attempt == attempt);
        if owned && let Some(session) = self.sessions.remove(provider) {
            // Only this attempt's own task: a key saved or a sign-out meanwhile cleared it.
            let mine = self
                .tasks
                .get(provider)
                .is_some_and(|t| t["started_at"] == session.started_at);
            if mine {
                let task = rules::task(&rules::Task {
                    provider,
                    status: state,
                    started_at: session.started_at,
                    method_label: &session.label,
                    authorization: &session.authorization,
                    error,
                    code_submitted: false,
                });
                self.tasks.insert(provider.to_string(), task);
            }
        }
        if let Some(reply) = reply {
            let _ = reply.send((200, self.task(provider)));
        }
    }
}

/// What OpenCode says of the sign-in methods a phone can complete, with its version.
/// `None`: it did not start or did not answer as expected, and nothing is remembered.
fn probe(program: &str, home: &Path) -> Option<(String, Map<String, Value>)> {
    let version = serve::version(program);
    let serve = Serve::start(program, home).ok()?;
    let raw = serve
        .client
        .request("GET", "/provider/auth", None, 20)
        .ok()?;
    drop(serve);
    let raw = if python::truthy(&raw) { raw } else { json!({}) };
    if !raw.is_object() {
        return None;
    }
    rules::usable_methods(&raw).map(|methods| (version, methods))
}

/// Asks OpenCode again for its sign-in methods when what was remembered is old, on a task of
/// its own: the listing does not wait for it. Whether it is old is the desk's to decide, in
/// the same turn as whether a probe runs (see [`Ask::Probe`]); the question is at the desk
/// before the listing is answered.
fn refresh(app: &App, desk: &Arc<Desk>) {
    let Some(program) = program() else {
        return;
    };
    let reading = app.clone();
    let fresh = Box::new(move || {
        #[allow(clippy::cast_precision_loss)]
        let now = reading.clock.now_ms() as f64 / 1000.0;
        rules::fresh(rules::remembered(remembered(&reading).as_deref()).at, now)
    });
    let (reply, granted) = oneshot::channel();
    if desk.asks.send(Ask::Probe { fresh, reply }).is_err() {
        return;
    }
    let (app, desk) = (app.clone(), desk.clone());
    tokio::spawn(async move {
        if granted.await != Ok(true) {
            return;
        }
        let home = desk.home.clone();
        let found = tokio::task::spawn_blocking(move || probe(&program, &home))
            .await
            .ok()
            .flatten();
        if let Some((version, methods)) = found {
            #[allow(clippy::cast_precision_loss)]
            let at = app.clock.now_ms() as f64 / 1000.0;
            let kept = rules::remembered_text(&version, at, &methods);
            // Not kept means OpenCode is asked again at the next listing: said once, not fatal.
            if let Err(why) = app
                .fleet
                .set(vec![(rules::METHODS_KEY.to_string(), kept)])
                .await
            {
                app.fleet.unkept("harness provider methods", &why.0);
            }
        }
        let _ = desk.asks.send(Ask::Probed);
    });
}

fn remembered(app: &App) -> Option<String> {
    app.fleet
        .directory()
        .setting(rules::METHODS_KEY)
        .map(str::to_owned)
}

/// `GET /harness-providers?harness=`: the providers, their credentials' types, the sign-ins
/// that run.
async fn listed(
    State(app): State<App>,
    Extension(desk): Extension<Arc<Desk>>,
    Query(asked): Query<HashMap<String, String>>,
) -> Response {
    let named = json!(asked.get("harness").cloned().unwrap_or_default());
    let harness = match rules::harness_id(&named) {
        Ok(harness) => harness,
        Err(why) => return answer(refusal(&why)),
    };
    let kept = remembered(&app);
    let Some(listing) = desk
        .ask(|reply| Ask::List {
            harness,
            remembered: kept,
            reply,
        })
        .await
    else {
        return unavailable();
    };
    // Only the Host that keeps the tools asks OpenCode anything.
    if app.config.accounts == Accounts::Select {
        refresh(&app, &desk);
    }
    answer((200, listing))
}

/// `GET /harness-providers/models?harness=&provider=`: a provider's models, from the
/// catalogue alone.
async fn models(Query(asked): Query<HashMap<String, String>>) -> Response {
    let field = |name: &str| json!(asked.get(name).cloned().unwrap_or_default());
    let (harness, provider) = (field("harness"), field("provider"));
    let read = tokio::task::spawn_blocking(move || {
        rules::models(&harness, &provider, &catalogue(&crate::config::home()))
    });
    match read.await {
        Ok(Ok(listed)) => answer((200, listed)),
        Ok(Err(why)) => answer(refusal(&why)),
        Err(_) => unavailable(),
    }
}

/// `POST /harness-providers/{login,login-code,logout}`: refused on a Host that does not keep
/// this machine's tools, before anything is read.
async fn change(
    State(app): State<App>,
    Extension(desk): Extension<Arc<Desk>>,
    Asked(action): Asked<String>,
    body: Bytes,
) -> Response {
    if !matches!(action.as_str(), "login" | "login-code" | "logout") {
        return ApiError::plain(StatusCode::NOT_FOUND, "not found").into_response();
    }
    if app.config.accounts != Accounts::Select {
        return answer((409, json!({ "error": rules::NOT_KEPT })));
    }
    let asked = if body.is_empty() {
        Some(Map::new())
    } else {
        match serde_json::from_slice::<Value>(&body) {
            Ok(Value::Object(asked)) => Some(asked),
            _ => None,
        }
    };
    let Some(asked) = asked else {
        return answer((400, json!({ "error": "bad json" })));
    };
    let (kept, shelf) = (remembered(&app), app.models.clone());
    let answered = match action.as_str() {
        "login" if asked.contains_key("key") => {
            desk.ask(|reply| Ask::Save {
                asked,
                remembered: kept,
                shelf,
                reply,
            })
            .await
        }
        "login" => {
            let now_ms = app.clock.now_ms();
            desk.ask(|reply| Ask::Start {
                asked,
                remembered: kept,
                now_ms,
                shelf,
                reply,
            })
            .await
        }
        "logout" => {
            desk.ask(|reply| Ask::Logout {
                asked,
                remembered: kept,
                shelf,
                reply,
            })
            .await
        }
        _ => {
            let field = |name: &str| asked.get(name).unwrap_or(&Value::Null);
            match rules::code(field("harness"), field("provider"), field("code")) {
                Err(why) => Some(refusal(&why)),
                Ok((provider, code)) => {
                    desk.ask(|reply| Ask::Code {
                        provider,
                        code,
                        reply,
                    })
                    .await
                }
            }
        }
    };
    answered.map_or_else(unavailable, answer)
}

pub fn routes() -> Router<App> {
    // How long a sign-in in a browser may take. CLARP_SIGN_IN_TIMEOUT_MS shortens it for a
    // test that waits for one to be given up; the reference's ten minutes otherwise.
    let timeout = std::env::var("CLARP_SIGN_IN_TIMEOUT_MS")
        .ok()
        .and_then(|ms| ms.parse().ok())
        .map_or_else(
            || Duration::from_secs(u64::try_from(rules::LOGIN_TIMEOUT_SECONDS).unwrap_or(600)),
            Duration::from_millis,
        );
    let desk = Desk::new(crate::config::home(), timeout);
    Router::new()
        .route("/harness-providers", get(listed))
        .route("/harness-providers/models", get(models))
        .route("/harness-providers/{action}", post(change))
        .layer(Extension(desk))
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;

    #[test]
    fn the_store_is_replaced_whole_0600_in_a_new_0700_folder() {
        use std::os::unix::fs::PermissionsExt as _;
        let root =
            std::env::temp_dir().join(format!("clarp-provider-store-{}", crate::ids::hex(6)));
        let path = root.join("data").join("opencode").join("auth.json");
        write_store(&path, "{}\n").unwrap();
        write_store(&path, "{\n  \"x\": 1\n}\n").unwrap();
        let mode = |p: &Path| std::fs::metadata(p).unwrap().permissions().mode() & 0o777;
        assert_eq!(
            std::fs::read_to_string(&path).unwrap(),
            "{\n  \"x\": 1\n}\n"
        );
        assert_eq!((mode(&path), mode(path.parent().unwrap())), (0o600, 0o700));
        let left: Vec<_> = std::fs::read_dir(path.parent().unwrap()).unwrap().collect();
        assert_eq!(left.len(), 1, "nothing is left beside the store");
        let _ = std::fs::remove_dir_all(&root);
    }

    /// The interleaving the train's gate once met by chance, written out: an asker whose
    /// listing read what was remembered before a probe's result was kept, and whose question
    /// reaches the desk after that probe was given back, is decided by what is kept by then.
    #[test]
    fn a_probe_runs_one_at_a_time_and_never_once_a_fresh_result_is_kept() {
        use std::sync::atomic::{AtomicBool, Ordering};
        let (asks, _inbox) = mpsc::channel();
        let mut actor = Actor {
            asks,
            tasks: Map::new(),
            sessions: HashMap::new(),
            attempts: 0,
            probing: false,
            home: PathBuf::new(),
            timeout: Duration::ZERO,
        };
        let kept = Arc::new(AtomicBool::new(false));
        let asker = || {
            let kept = kept.clone();
            Box::new(move || kept.load(Ordering::SeqCst))
        };
        let decide = |actor: &mut Actor, fresh: Box<dyn FnOnce() -> bool + Send>| {
            let (reply, mut answered) = oneshot::channel();
            actor.handle(Ask::Probe { fresh, reply });
            answered.try_recv().unwrap()
        };
        assert!(
            decide(&mut actor, asker()),
            "nothing is kept: the first asker probes"
        );
        assert!(!decide(&mut actor, asker()), "one probe at a time");
        // Asked while that probe ran, decided after its result was kept and it was given back.
        let late = asker();
        kept.store(true, Ordering::SeqCst);
        actor.handle(Ask::Probed);
        assert!(!decide(&mut actor, late), "what is kept by then decides");
        assert!(!decide(&mut actor, asker()), "and every asker after");
        assert!(!actor.probing, "a probe refused holds nothing");
        // Old again, or nothing kept by a probe that failed: the next asker probes.
        kept.store(false, Ordering::SeqCst);
        assert!(decide(&mut actor, asker()));
        actor.handle(Ask::Probed);
        assert!(decide(&mut actor, asker()));
    }

    #[test]
    fn a_store_that_is_not_there_is_empty_and_one_that_cannot_be_read_is_refused() {
        let root = std::env::temp_dir().join(format!("clarp-provider-read-{}", crate::ids::hex(6)));
        assert_eq!(read_store(&root.join("auth.json")), Ok(None));
        std::fs::create_dir_all(root.join("auth.json")).unwrap();
        assert_eq!(read_store(&root.join("auth.json")), Err(()));
        let _ = std::fs::remove_dir_all(&root);
    }
}
