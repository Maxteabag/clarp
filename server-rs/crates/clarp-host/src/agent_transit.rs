//! An agent moves to another backend and keeps its conversation: feature
//! `agent_backend_transit`, contract 158 (issue 720).
//!
//! `POST /agent-transit` starts a change and answers `202 {transit_id}`; the change goes on in
//! the Host, telling how it goes with the event `agent-transit` (`reading`, `translating`,
//! `registering`, `switching`, then `done` or `failed`), and `GET /agent-transit` answers its
//! latest state to an app that reconnects. While it runs, the agent's roster row carries it
//! (`transit`).
//!
//! The agent's conversation is the Host's own record of it: what the user said and what the
//! agent answered, turn by turn. That record is written as a session of the new tool, in the
//! tool's own format, where the Host's turns find it ([`clarp_backend::transit`]); the agent's
//! log says, in one write, that the agent goes on with that tool and that session, with the
//! one row every app shows of it; and its record in the fleet takes the new tool, model and
//! effort. The conversation's name (`conversation_id`), its persona, voice, goals, jobs and
//! history stay as they are: an app keeps what it cached.
//!
//! Antigravity gets no session (the Host never opens its database): its next turn begins one
//! that opens with the Host's record of the conversation ([`clarp_kernel::told::carried`]).
//!
//! Nothing changes while a turn runs or messages wait. A session that was written and then
//! not used is taken back. The log's write is the change: a Host that stops before its fleet
//! record follows still runs the agent on the new tool when it opens it again
//! (`agent::Worker::open`). What the Host knows of changes is kept in memory: a Host started
//! again has forgotten the finished ones.

use std::collections::{HashMap, VecDeque};
use std::sync::{Arc, LazyLock, Mutex, PoisonError};
use std::time::Duration;

use axum::Json;
use axum::Router;
use axum::body::Bytes;
use axum::extract::{Query, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::get;
use clarp_actor::{Actor, ActorError, Outputs, TRANSIT_ROW};
use clarp_backend::transit::{self, CARRIED_MOST, Place};
use clarp_kernel::roster as rules;
use serde_json::{Value, json};

use crate::edge::{self, ApiError, App};
use crate::fleet::{AgentRecord, Amend};
use crate::hub::Hub;
use crate::roster::{Fields, Refusal};

/// The tools an agent can be moved to: those that keep a conversation of their own.
pub const TARGETS: [&str; 5] = ["claude", "codex", "agy-acp", "grok", "opencode"];

/// How many changes the Host remembers, the finished ones forgotten first.
const KEPT: usize = 256;

/// Tells how a change goes: a phase, as it is reached.
#[derive(Clone)]
pub struct Progress(Arc<dyn Fn(&'static str) + Send + Sync>);

impl Progress {
    pub fn tell(&self, phase: &'static str) {
        (self.0)(phase);
    }
}

impl std::fmt::Debug for Progress {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("Progress")
    }
}

/// What the agent's own thread is asked to do: go on with this tool.
#[derive(Debug, Clone)]
pub struct Change {
    pub from: String,
    pub backend: String,
    pub model: String,
    pub effort: String,
    pub preserve_selection: bool,
    /// The version of the new tool, as it says it, when the Host knows it.
    pub tool_version: Option<String>,
    /// The tool and the model as a person reads them, for the row the change leaves.
    pub shown_as: (String, String),
    /// The tool the agent moves from, as a person reads it.
    pub shown_from: String,
    /// How many signs of the conversation the new tool is given at most ([`budget_of`]).
    pub budget: usize,
    /// What stands for the older part of the conversation where it does not all fit.
    pub summary: Option<transit::Summary>,
    /// The new tool keeps its conversations where the old one did ([`clarp_backend::same_store`]):
    /// the agent goes on in the same conversation, and nothing is carried.
    pub same_store: bool,
    pub progress: Progress,
}

/// What came of it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Changed {
    /// The new tool's own session, when one was written.
    pub backend_session: Option<String>,
    pub carried: usize,
    pub user: usize,
    pub assistant: usize,
    /// What was said that was read and not carried as it was, the oldest, beyond the budget.
    pub omitted: usize,
    /// Who summarized what was not carried as it was (`agent` or `model`), where it was.
    pub summary: Option<&'static str>,
}

/// Why it did not happen. Nothing changed.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Refused {
    /// A turn runs, or messages wait.
    Busy {
        live: bool,
        waiting: usize,
    },
    Failed {
        stage: &'static str,
        why: String,
    },
}

/// The row every app shows when the change is done.
fn done_row(backend: &str, model: &str, omitted: usize, summarized: bool) -> String {
    let turns = match omitted {
        1 => "its oldest turn".to_string(),
        _ => format!("its oldest {omitted} turns"),
    };
    match (omitted, summarized) {
        (0, _) => format!("Now on {backend} · {model}. The conversation came along."),
        (_, true) => format!(
            "Now on {backend} · {model}. The conversation came along, {turns} as a summary."
        ),
        (_, false) => {
            format!("Now on {backend} · {model}. The conversation came along, except {turns}.")
        }
    }
}

/// How many signs of the conversation `backend` is given at most on `model`: about 60% of
/// what the model takes in ([`transit::budget`]), or of what Codex's own configuration says
/// its model takes in ([`codex_window`]); for a tool whose first turn opens with the record
/// on its command line, as much as fits there ([`clarp_kernel::told::CARRIED_MOST`]).
pub fn budget_of(backend: &str, model: &str) -> usize {
    let configured = (backend == "codex")
        .then(|| {
            let file = crate::past::codex_home(&crate::config::home()).join("config.toml");
            let config: toml::Table = std::fs::read_to_string(file).ok()?.parse().ok()?;
            codex_window(&config, model)
        })
        .flatten();
    let budget = configured.map_or_else(
        || transit::budget(backend, model),
        transit::budget_of_tokens,
    );
    let on_its_command_line = transit::writer(backend).is_none()
        && clarp_backend::by_id(backend).is_some_and(|tool| tool.message_most().is_some());
    if on_its_command_line {
        budget.min(clarp_kernel::told::CARRIED_MOST)
    } else {
        budget
    }
}

/// Carries the agent's conversation into `change.backend` and records, in the agent's log,
/// that it goes on with it, with the row every app shows of it. Run on the agent's own
/// thread, so nothing can start a turn in between. `record` is the agent as the fleet has it.
pub(crate) fn carry(
    actor: &mut Actor,
    record: &AgentRecord,
    change: &Change,
    now_ms: i64,
) -> Result<(Result<Changed, Refused>, Outputs), ActorError> {
    if let Some((live, waiting)) = actor.busy_for_change() {
        return Ok((Err(Refused::Busy { live, waiting }), Outputs::default()));
    }
    if change.same_store {
        return in_the_same_store(actor, change, now_ms);
    }
    let mut said = actor.said_so_far(CARRIED_MOST.min(change.budget))?;
    // Claude keeps whole the results the Host kept only the start of.
    if record.backend == "claude" {
        let projects = transit::tool_home("claude", &crate::config::home()).join("projects");
        transit::whole_results(&mut said, &projects, &record.cwd);
    }
    let fitted = transit::fitted(
        &said,
        change.budget,
        change.summary.as_ref(),
        &change.shown_from,
        &change.shown_as.0,
    );
    let from = fitted.from;
    let kept = &said[from..];
    let carried = transit::with_head(&said, &fitted);
    change.progress.tell("translating");
    let written = match transit::writer(&change.backend) {
        Some(writer) if !kept.is_empty() => {
            let place = Place {
                tool_home: transit::tool_home(&change.backend, &crate::config::home()),
                cwd: record.cwd.clone(),
                now_ms,
                tool_version: change.tool_version.clone(),
                model: Some(change.model.clone()).filter(|model| !model.is_empty()),
                program: clarp_backend::program(&change.backend).map(std::path::PathBuf::from),
            };
            match writer.write(&carried, &place) {
                Ok(written) => Some(written),
                Err(failed) => {
                    let refused = Refused::Failed {
                        stage: failed.stage(),
                        why: failed.why().to_string(),
                    };
                    return Ok((Err(refused), Outputs::default()));
                }
            }
        }
        _ => None,
    };
    change.progress.tell("registering");
    let session = written.as_ref().map(|written| written.session.clone());
    // A tool given no session opens its first turn with the record as it is carried now,
    // with the tools each answer used as quoted blocks.
    let opening = if written.is_none() {
        let quoted: Vec<(bool, String)> = kept
            .iter()
            .map(|one| (one.role == transit::Role::User, transit::with_quoted(one)))
            .collect();
        let quoted: Vec<(bool, &str)> = quoted
            .iter()
            .map(|(by_user, text)| (*by_user, text.as_str()))
            .collect();
        clarp_kernel::told::carried_opening(
            &change.shown_as.0,
            &change.shown_from,
            fitted.head.as_deref(),
            &quoted,
            change.budget,
        )
    } else {
        None
    };
    let user = kept
        .iter()
        .filter(|one| one.role == transit::Role::User)
        .count();
    let changed = Changed {
        backend_session: session.clone(),
        carried: kept.len(),
        user,
        assistant: kept.len() - user,
        omitted: from,
        summary: change
            .summary
            .as_ref()
            .filter(|_| fitted.summarized)
            .map(|summary| summary.by.word()),
    };
    change.progress.tell("switching");
    let (backend, model) = &change.shown_as;
    let row = done_row(backend, model, from, fitted.summarized);
    match actor.change_tool(
        &change.backend,
        clarp_kernel::agent::ToolSelection {
            model: change.model.clone(),
            effort: change.effort.clone(),
        },
        session,
        opening,
        &row,
        now_ms,
    ) {
        Ok((Ok(()), out)) => Ok((Ok(changed), out)),
        Ok((Err(why), _)) => {
            if let Some(written) = written {
                written.undo();
            }
            let refused = Refused::Failed {
                stage: "switching",
                why,
            };
            Ok((Err(refused), Outputs::default()))
        }
        Err(error) => {
            if let Some(written) = written {
                written.undo();
            }
            Err(error)
        }
    }
}

/// A move between two tools of one store (`agy` and `agy-acp`): the agent keeps the tool's
/// own conversation, and its next turn continues it natively. Nothing is read or written. Should
/// the tool no longer have it, the next turn finds so (`keeps`, or the tool's own answer that it
/// does not know it, which marks it), and the turn after begins anew with the Host's record.
fn in_the_same_store(
    actor: &mut Actor,
    change: &Change,
    now_ms: i64,
) -> Result<(Result<Changed, Refused>, Outputs), ActorError> {
    change.progress.tell("translating");
    let session = actor.status().backend_session;
    change.progress.tell("registering");
    change.progress.tell("switching");
    let (backend, model) = &change.shown_as;
    let row = format!("Now on {backend} · {model}. The same conversation goes on.");
    match actor.change_tool(
        &change.backend,
        clarp_kernel::agent::ToolSelection {
            model: change.model.clone(),
            effort: change.effort.clone(),
        },
        session.clone(),
        None,
        &row,
        now_ms,
    )? {
        (Ok(()), out) => {
            let changed = Changed {
                backend_session: session,
                carried: 0,
                user: 0,
                assistant: 0,
                omitted: 0,
                summary: None,
            };
            Ok((Ok(changed), out))
        }
        (Err(why), _) => Ok((
            Err(Refused::Failed {
                stage: "switching",
                why,
            }),
            Outputs::default(),
        )),
    }
}

// ---- what the Host knows of changes ---------------------------------------------------------

#[derive(Default)]
struct Book {
    /// Each change's state as the apps are told it, by its id.
    states: HashMap<String, Value>,
    /// The ids in the order the changes began.
    order: VecDeque<String>,
    /// A change by the agent and the request that began it.
    asked: HashMap<(String, String), String>,
    /// The change that runs now, by the agent's id.
    running: HashMap<String, String>,
}

static BOOK: LazyLock<Mutex<Book>> = LazyLock::new(|| Mutex::new(Book::default()));

fn book() -> std::sync::MutexGuard<'static, Book> {
    BOOK.lock().unwrap_or_else(PoisonError::into_inner)
}

impl Book {
    fn begin(&mut self, request: Option<(String, String)>, state: Value) {
        let id = state["transit_id"].as_str().unwrap_or_default().to_string();
        let agent = state["agent_id"].as_str().unwrap_or_default().to_string();
        self.running.insert(agent, id.clone());
        if let Some(request) = request {
            self.asked.insert(request, id.clone());
        }
        self.states.insert(id.clone(), state);
        self.order.push_back(id);
        // The oldest finished are forgotten first; one that runs never is.
        let mut kept = self.order.len();
        let mut at = 0;
        while kept > KEPT && at < self.order.len() {
            let old = self.order[at].clone();
            let finished = !self.running.values().any(|running| *running == old);
            if finished {
                self.order.remove(at);
                self.states.remove(&old);
                self.asked.retain(|_, id| *id != old);
                kept -= 1;
            } else {
                at += 1;
            }
        }
    }
}

/// The change of this agent that runs now, as its roster row carries it, or null.
pub fn running_of(agent_id: &str) -> Value {
    let book = book();
    book.running
        .get(agent_id)
        .and_then(|id| book.states.get(id))
        .map_or(Value::Null, |state| {
            json!({
                "transit_id": state["transit_id"],
                "phase": state["phase"],
                "backend": state["backend"],
                "model": state["model"],
            })
        })
}

/// Only an actual backend switch fences the mailbox.
pub(crate) fn holds_mailbox(agent_id: &str) -> bool {
    let book = book();
    book.running
        .get(agent_id)
        .and_then(|id| book.states.get(id))
        .is_some_and(|state| state["backend"] != state["previous_backend"])
}

/// A change's state, as `GET /agent-transit` answers it.
fn state_of(id: &str) -> Option<Value> {
    book().states.get(id).cloned()
}

/// Moves a change to `phase` and tells the apps. A finished change no longer runs.
fn reach(hub: &Hub, now_ms: i64, id: &str, phase: &str, more: &[(&str, Value)]) {
    let told = {
        let mut book = book();
        let Some(state) = book.states.get_mut(id) else {
            return;
        };
        state["phase"] = json!(phase);
        for (key, value) in more {
            state[*key] = value.clone();
        }
        let mut told = state.clone();
        told["type"] = json!("agent-transit");
        let agent = state["agent_id"].as_str().unwrap_or_default().to_string();
        let session = state["session"].clone();
        if matches!(phase, "done" | "failed") {
            book.running.remove(&agent);
        }
        (told, session)
    };
    hub.publish(told.0, now_ms);
    if matches!(phase, "done" | "failed") {
        hub.publish(
            json!({"type": "agent-roster", "kind": "agent-transit", "session": told.1}),
            now_ms,
        );
    }
}

// ---- what can be chosen ---------------------------------------------------------------------

/// The version in what a tool says of itself: `2.1.321` of `2.1.321 (Claude Code)`, `0.167.1`
/// of `codex-cli 0.167.1`.
fn version_in(said: &str) -> Option<String> {
    said.split_whitespace()
        .find(|word| word.contains('.') && word.starts_with(|sign: char| sign.is_ascii_digit()))
        .map(str::to_string)
}

/// A moment as the tools write it, in milliseconds.
fn ms_of(said: &Value) -> Option<i64> {
    said.as_str()?
        .parse::<jiff::Timestamp>()
        .ok()
        .map(jiff::Timestamp::as_millisecond)
}

/// Whether an agent can be moved to a tool now, and why not: what `GET /agent-model-options`
/// adds to the tool's row (`available`, `unavailable_reason`, `until_ms`,
/// `unavailable_detail`), and the model the tool runs when none is named (`default` on its
/// model). `row` is the tool's row in that answer.
fn judge(backend: &str, row: &mut Value, used_up: &crate::quota::UsedUp, default: Option<&str>) {
    let home = crate::config::home();
    let (reason, until) = if !TARGETS.contains(&backend) {
        (Some("not_supported"), None)
    } else if row["installed"] != true || clarp_backend::by_id(backend).is_none() {
        (Some("not_installed"), None)
    } else if row["authenticated"] == false
        || clarp_backend::by_id(backend)
            .and_then(|tool| tool.login_file(&home))
            .is_some_and(|login| !login.is_file())
    {
        (Some("signed_out"), None)
    } else if let Some(used) = used_up.get(backend) {
        (Some("usage_limit"), ms_of(&used["resets_at"]))
    } else {
        (None, None)
    };
    row["available"] = json!(reason.is_none());
    row["unavailable_reason"] = json!(reason);
    row["until_ms"] = json!(until);
    // What the Host could not check would be said here (`error`); nothing it checks fails so.
    row["unavailable_detail"] = Value::Null;
    // The tool listed which models its login runs (`cli_probe`, as Codex does): what the Host
    // knows of it besides (a model of the bundled catalogue the login may not run) is not
    // offered. A sighting of the one model it last ran (`cli_cache`, Claude Code's) lists
    // nothing, and the catalogue stays.
    let asked_the_tool = row["source"]["kind"] == "cli_probe";
    let Some(models) = row["models"].as_array_mut() else {
        return;
    };
    if asked_the_tool {
        models.retain(|model| model["source"]["kind"] != "static_fallback");
    }
    let named = default.filter(|default| {
        models
            .iter()
            .any(|model| model["id"].as_str() == Some(*default))
    });
    for (at, model) in models.iter_mut().enumerate() {
        let is = match named {
            Some(default) => model["id"].as_str() == Some(default),
            None => at == 0,
        };
        model["default"] = json!(is);
    }
}

/// Adds to `GET /agent-model-options` what tells which tool an agent can be moved to.
pub(crate) async fn annotate(app: &App, told: &mut Value) {
    // Used up only where no seat of the tool's pool would take the turn, as the agents' rows
    // say it, on the tool's default model.
    let used_up = crate::quota::rows(app).await.used_up();
    let tools: Vec<String> = told["providers"]
        .as_object()
        .map(|providers| providers.keys().cloned().collect())
        .unwrap_or_default();
    for tool in tools {
        let default = crate::models::default_for(app, &tool).await;
        if let Some(row) = told["providers"].get_mut(&tool) {
            judge(&tool, row, &used_up, default.as_deref());
        }
    }
}

/// A model as a person reads it, in a tool's row: its label, else its id, else the tool's
/// default model.
fn model_shown(row: &Value, model: &str) -> String {
    let models = row["models"].as_array().cloned().unwrap_or_default();
    let found = |wanted: &dyn Fn(&Value) -> bool| {
        models
            .iter()
            .find(|model| wanted(model))
            .and_then(|model| model["label"].as_str().or(model["id"].as_str()))
            .map(str::to_string)
    };
    if model.is_empty() {
        return found(&|model| model["default"] == true).unwrap_or_else(|| "default".into());
    }
    found(&|row| row["id"].as_str() == Some(model)).unwrap_or_else(|| model.to_string())
}

/// A tool as a person reads it.
fn backend_shown(row: &Value, backend: &str) -> String {
    row["label"].as_str().map_or_else(
        || {
            clarp_backend::catalogue::declared(backend)["shown"]["label"]
                .as_str()
                .unwrap_or(backend)
                .to_string()
        },
        str::to_string,
    )
}

// ---- the routes -----------------------------------------------------------------------------

fn told(status: StatusCode, body: Value) -> Refusal {
    Refusal::Told(status, body)
}

fn refused(status: StatusCode, error: &str) -> Refusal {
    told(status, json!({ "error": error }))
}

fn bad(why: &str) -> Refusal {
    Refusal::Plain(ApiError::new(StatusCode::BAD_REQUEST, why))
}

/// An optional text field: absent or null is `None`; anything but a text is refused.
fn optional(asked: &Fields, key: &str) -> Result<Option<String>, Refusal> {
    match asked.get(key) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(said)) => Ok(Some(said.trim().to_string())),
        Some(_) => Err(bad(&format!("{key} must be string or null"))),
    }
}

/// Begins a change of an agent's backend as a request asks, and answers its id: what `POST
/// /agent-transit` and `clarp-admin agent transit` do.
pub(crate) async fn start(app: &App, asked: &Fields) -> Result<String, Refusal> {
    let session = edge::body_text(asked, "session")?;
    if session.is_empty() {
        return Err(bad("session required"));
    }
    let member = app
        .fleet
        .directory()
        .named(session)
        .cloned()
        .filter(|member| !member.record.is_janitor)
        .ok_or_else(|| Refusal::Plain(ApiError::new(StatusCode::NOT_FOUND, "no such agent")))?;
    start_for(app, member, asked, false).await
}

async fn start_for(
    app: &App,
    member: Arc<crate::fleet::Member>,
    asked: &Fields,
    preserve_selection: bool,
) -> Result<String, Refusal> {
    let before = member.record.clone();
    let request = optional(asked, "request_id")?
        .filter(|id| !id.is_empty())
        .map(|id| (before.agent_id.clone(), id));
    if let Some(request) = &request
        && let Some(id) = book().asked.get(request)
    {
        return Ok(id.clone());
    }
    let backend = optional(asked, "backend")?
        .unwrap_or_default()
        .to_ascii_lowercase();
    let backend = clarp_backend::canonical_id(&backend).to_owned();
    // An agent bound to a provider of its own stays on it: moving it to another tool would
    // carry its conversation, and the summary of it, to other services. Its models are its
    // provider's deployments, which no catalogue lists.
    let bound = before.bound();
    if bound && backend != before.backend {
        return Err(told(
            StatusCode::CONFLICT,
            json!({
                "error": "own_provider",
                "message": "This agent is bound to a model provider of its own and stays on Codex. \
                    Clear its codex_home and codex_provider first.",
            }),
        ));
    }
    let mut catalogue = app.models.told(app.clock.now_ms()).await;
    annotate(app, &mut catalogue).await;
    let Some(row) = catalogue["providers"].get(&backend).cloned() else {
        return Err(refused(StatusCode::BAD_REQUEST, "unknown_backend"));
    };
    // The machine's own Codex account says nothing of whether a provider of its own answers.
    if !bound && row["available"] != true {
        let mut body = json!({"error": "backend_unavailable", "reason": row["unavailable_reason"]});
        for key in ["until_ms", "unavailable_detail"] {
            if !row[key].is_null() {
                body[key] = row[key].clone();
            }
        }
        return Err(told(StatusCode::BAD_REQUEST, body));
    }
    let model = optional(asked, "model")?.unwrap_or_default();
    let offered: Vec<&str> = row["models"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|model| model["id"].as_str())
        .collect();
    if !bound
        && !model.is_empty()
        && !offered.is_empty()
        && !crate::models::model_is_offered(&backend, &model, &offered)
    {
        return Err(refused(StatusCode::BAD_REQUEST, "unknown_model"));
    }
    let valid = clarp_backend::efforts(&backend);
    let effort = match optional(asked, "effort")? {
        Some(said) if said.is_empty() => String::new(),
        Some(said) => match rules::effort(&said, valid) {
            Some(effort) => effort,
            None => {
                return Err(told(
                    StatusCode::BAD_REQUEST,
                    json!({"error": "invalid effort for backend", "valid_efforts": valid}),
                ));
            }
        },
        None if valid.contains(&before.effort.as_str()) => before.effort.clone(),
        None => String::new(),
    };
    let same_backend = backend == before.backend;
    if same_backend && model == before.model && effort == before.effort {
        return Err(refused(StatusCode::BAD_REQUEST, "same_backend_and_model"));
    }
    let view = member.agent.view();
    let status = &view.status;
    if same_backend {
        if status.live.is_some() {
            return Err(refused(StatusCode::CONFLICT, "turn_running"));
        }
        if status.queue_depth > 0 || !status.waiting.is_empty() {
            return Err(refused(StatusCode::CONFLICT, "messages_waiting"));
        }
    }
    let id = crate::ids::uuid();
    let now = app.clock.now_ms();
    let state = json!({
        "agent_id": before.agent_id,
        "session": before.session,
        "transit_id": id,
        "backend": backend,
        "model": model,
        "phase": if same_backend { "switching" } else { "reading" },
        "error": null,
        "previous_backend": before.backend,
        "previous_model": before.model,
        "carried": null,
    });
    {
        let mut book = book();
        if book.running.contains_key(&before.agent_id) {
            return Err(refused(StatusCode::CONFLICT, "transit_running"));
        }
        book.begin(request, state.clone());
    }
    let mut first = state;
    first["type"] = json!("agent-transit");
    app.hub.publish(first, now);
    app.hub.publish(
        json!({"type": "agent-roster", "kind": "agent-transit", "session": before.session}),
        now,
    );
    let old_row = catalogue["providers"]
        .get(&before.backend)
        .cloned()
        .unwrap_or(Value::Null);
    let shown = Shown {
        backend: backend_shown(&row, &backend),
        model: model_shown(&row, &model),
        old_backend: backend_shown(&old_row, &before.backend),
        old_model: model_shown(&old_row, &before.model),
    };
    let goal = Goal {
        id: id.clone(),
        backend,
        model,
        effort,
        preserve_selection,
        tool_version: row["cli_version"].as_str().and_then(version_in),
        shown,
    };
    let app = app.clone();
    let (prepared, ready) = tokio::sync::oneshot::channel();
    tokio::spawn(async move {
        if !same_backend && let Err(why) = member.agent.hold_for_transit(true).await {
            fail(&app, &member, &goal, "reading", why.0).await;
            return;
        }
        let _ = prepared.send(());
        run(&app, &member, &before, goal).await;
    });
    let _ = ready.await;
    Ok(id)
}

/// Drain standalone Antigravity attempts on their original shim, then change the
/// transport through Transit. The opening actor fences fresh work before replay.
pub fn retire_legacy_agy(app: &App) {
    let directory = app.fleet.directory();
    for member in directory.members.iter().chain(&directory.janitors) {
        if member.record.backend != clarp_backend::LEGACY_AGY {
            continue;
        }
        if !clarp_backend::same_store(clarp_backend::LEGACY_AGY, clarp_backend::agy_acp::ID) {
            eprintln!(
                "clarp-host: {} needs a Transit across Antigravity profiles",
                member.record.session
            );
            continue;
        }
        let (app, member) = (app.clone(), member.clone());
        tokio::spawn(async move {
            let session = member.record.session.clone();
            if let Err(why) = retire_legacy_agent(&app, member).await {
                eprintln!("clarp-host: cannot move {session} to Antigravity ACP: {why}");
            }
        });
    }
}

async fn retire_legacy_agent(app: &App, member: Arc<crate::fleet::Member>) -> Result<(), String> {
    // The request waits for the actor to open. It can already have recovered
    // a journaled switch which the fleet never heard before a restart.
    member
        .agent
        .hold_for_transit(true)
        .await
        .map_err(|why| why.0)?;
    if member.agent.view().backend == clarp_backend::agy_acp::ID {
        let amended = app
            .fleet
            .amend_any(member.record.agent_id.clone(), Amend::RetiredAgy)
            .await;
        member
            .agent
            .hold_for_transit(false)
            .await
            .map_err(|why| why.0)?;
        amended
            .map_err(|why| why.0)?
            .map_err(|why| format!("{why:?}"))?;
        return Ok(());
    }
    let asked = Fields::from_iter([
        ("backend".to_string(), json!(clarp_backend::agy_acp::ID)),
        ("model".to_string(), json!(member.record.model)),
        ("effort".to_string(), json!(member.record.effort)),
    ]);
    start_for(app, member, &asked, true)
        .await
        .map_err(|why| why.answered().1.to_string())?;
    Ok(())
}

struct Shown {
    backend: String,
    model: String,
    old_backend: String,
    old_model: String,
}

struct Goal {
    id: String,
    backend: String,
    model: String,
    effort: String,
    preserve_selection: bool,
    tool_version: Option<String>,
    shown: Shown,
}

/// A change failed, and nothing changed: the row every app shows, and the event.
/// What the row every app shows says of why a change failed: a person's words, not the
/// tool's. What the tool said stays in the event's `error` and the Host's log.
fn plain_reason(stage: &str, backend: &str, shown: &str) -> String {
    match stage {
        "translating" | "translate" | "registering" | "register" if backend == "opencode" => {
            "the conversation couldn't be imported into OpenCode".to_string()
        }
        "translating" | "translate" | "registering" | "register" => {
            format!("the conversation couldn't be written for {shown}")
        }
        "turn_running" => "a turn started".to_string(),
        "messages_waiting" => "messages are waiting".to_string(),
        "switching" => "the agent couldn't be switched".to_string(),
        _ => "something went wrong".to_string(),
    }
}

async fn fail(app: &App, member: &crate::fleet::Member, goal: &Goal, stage: &str, why: String) {
    eprintln!(
        "clarp-host: moving agent {} to {} failed at {stage}: {why}",
        member.record.session, goal.backend
    );
    let reason = plain_reason(stage, &goal.backend, &goal.shown.backend);
    let row = format!(
        "Couldn't move to {}: {reason}. Still on {} · {}.",
        goal.shown.backend, goal.shown.old_backend, goal.shown.old_model
    );
    let at = app.clock.now_ms();
    let _ = member.agent.note(format!("{TRANSIT_ROW}{at}"), row).await;
    reach(
        &app.hub,
        app.clock.now_ms(),
        &goal.id,
        "failed",
        &[("error", json!(why))],
    );
}

/// Carries out a change that began: to `done`, or to `failed` with nothing changed.
async fn run(app: &App, member: &crate::fleet::Member, before: &AgentRecord, goal: Goal) {
    // The same tool: only the model or the effort changes.
    if goal.backend == before.backend {
        let mut tune = Fields::new();
        tune.insert("session".into(), json!(before.agent_id));
        tune.insert("model".into(), json!(goal.model));
        tune.insert("effort".into(), json!(goal.effort));
        match crate::roster::tune(app, &tune).await {
            Ok(_) => {
                let at = app.clock.now_ms();
                let row = format!("Now on {}.", goal.shown.model);
                let _ = member.agent.note(format!("{TRANSIT_ROW}{at}"), row).await;
                reach(&app.hub, app.clock.now_ms(), &goal.id, "done", &[]);
            }
            Err(refusal) => fail(app, member, &goal, "switching", refusal.answered().1).await,
        }
        return;
    }
    let result = carry_at_boundary(app, member, before, &goal).await;
    // The successful change reopens the agent on the new backend before it takes this ask.
    // On failure it releases the same mailbox to the unchanged backend.
    let released = member.agent.hold_for_transit(false).await;
    let carried = match result {
        Ok(carried) => carried,
        Err((stage, why)) => return fail(app, member, &goal, stage, why).await,
    };
    let counts = json!({
        "turns": carried.carried,
        "user": carried.user,
        "assistant": carried.assistant,
        "omitted": carried.omitted,
        "summary": carried.summary,
    });
    if let Err(why) = released {
        eprintln!(
            "clarp-host: {} could not release the Transit hold: {}",
            before.session, why.0
        );
    }
    reach(
        &app.hub,
        app.clock.now_ms(),
        &goal.id,
        "done",
        &[("carried", counts)],
    );
}

async fn carry_at_boundary(
    app: &App,
    member: &crate::fleet::Member,
    before: &AgentRecord,
    goal: &Goal,
) -> Result<Changed, (&'static str, String)> {
    member
        .agent
        .await_transit_boundary()
        .await
        .map_err(|why| ("reading", why.0))?;
    let progress = {
        let (hub, clock, id) = (app.hub.clone(), app.clock.clone(), goal.id.clone());
        Progress(Arc::new(move |phase| {
            reach(&hub, clock.now_ms(), &id, phase, &[])
        }))
    };
    let budget = budget_of(&goal.backend, &goal.model);
    // A tool of the same store goes on in the same conversation: there is nothing to summarize.
    let same_store = clarp_backend::same_store(&before.backend, &goal.backend);
    let summary = if same_store {
        None
    } else {
        summarized(app, member, budget).await
    };
    let change = Change {
        from: before.backend.clone(),
        backend: goal.backend.clone(),
        model: goal.model.clone(),
        effort: goal.effort.clone(),
        preserve_selection: goal.preserve_selection,
        tool_version: goal.tool_version.clone(),
        shown_as: (goal.shown.backend.clone(), goal.shown.model.clone()),
        shown_from: goal.shown.old_backend.clone(),
        budget,
        summary,
        same_store,
        progress,
    };
    let carried = match member.agent.change_tool(change).await {
        Ok(Ok(changed)) => changed,
        Ok(Err(Refused::Busy { live, .. })) => {
            let why = if live {
                "a turn started"
            } else {
                "messages are waiting"
            };
            let stage = if live {
                "turn_running"
            } else {
                "messages_waiting"
            };
            return Err((stage, why.to_string()));
        }
        Ok(Err(Refused::Failed { stage, why })) => {
            return Err((stage, why));
        }
        Err(unavailable) => return Err(("reading", unavailable.0)),
    };
    // The log has the change: the fleet's record follows it. Should that fail, the agent runs
    // on the new tool all the same, and its record is put right when the Host next opens it.
    let amend = if goal.preserve_selection {
        Amend::RetiredAgy
    } else {
        Amend::ToolChanged {
            backend: goal.backend.clone(),
            model: goal.model.clone(),
            effort: goal.effort.clone(),
        }
    };
    let _ = app.fleet.amend_any(before.agent_id.clone(), amend).await;
    Ok(carried)
}

/// The context window Codex's configuration sets (`model_context_window`, in the active
/// profile or at the top), which Codex itself holds its model to: for `model` where that is
/// the model the configuration runs, or the tool's default (`""`). Another model is held to
/// what the Host knows of it.
fn codex_window(config: &toml::Table, model: &str) -> Option<usize> {
    let profile = crate::owner::text_at(config, &["profile"]);
    let in_profile = |key: &str| {
        let profile = profile.as_deref()?;
        config
            .get("profiles")?
            .as_table()?
            .get(profile)?
            .as_table()?
            .get(key)
            .cloned()
    };
    let runs = profile
        .as_deref()
        .and_then(|profile| crate::owner::text_at(config, &["profiles", profile, "model"]))
        .or_else(|| crate::owner::text_at(config, &["model"]));
    if !model.is_empty() && runs.as_deref() != Some(model) {
        return None;
    }
    let window = in_profile("model_context_window")
        .or_else(|| config.get("model_context_window").cloned())?
        .as_integer()?;
    usize::try_from(window).ok().filter(|window| *window > 0)
}

// ---- what stands for what does not fit ------------------------------------------------------

/// How long another model is given to summarize older conversation.
const MODEL_WITHIN: Duration = Duration::from_secs(180);
/// How much of the older part of the conversation another model is given to summarize.
const MODEL_READS: usize = 300_000;
/// Summary calls run outside the mailbox so they cannot delay its first turn by joining it.
const SUMMARIZERS: [(&str, &str); 2] = [("codex", ""), ("claude", "haiku")];

async fn summarized(
    app: &App,
    member: &crate::fleet::Member,
    budget: usize,
) -> Option<transit::Summary> {
    let said = member.agent.said_so_far(budget).await.ok()?;
    let fitted = transit::fitted(&said, budget, None, "", "");
    if fitted.from == 0 {
        return None;
    }
    by_a_model(app, &said[..fitted.from]).await
}

/// A summary by a cheap model, of the older part of the conversation as the Host recorded it.
async fn by_a_model(app: &App, older: &[transit::Said]) -> Option<transit::Summary> {
    let (instructions, asked, schema) = transit::summary_reading(older, MODEL_READS);
    for (backend, model) in SUMMARIZERS {
        let reading = clarp_backend::Reading {
            instructions: instructions.clone(),
            asked: asked.clone(),
            schema: schema.clone(),
            model: (!model.is_empty()).then(|| model.to_string()),
            effort: None,
        };
        match crate::once::read_on(app, backend, &reading, MODEL_WITHIN).await {
            Ok(answer) => {
                let text = clarp_kernel::router::extract_json(&answer)
                    .ok()
                    .and_then(|object| object.get("summary")?.as_str().map(str::to_string))
                    .filter(|text| !text.trim().is_empty());
                if let Some(text) = text {
                    let tool = clarp_backend::catalogue::declared(backend)["shown"]["label"]
                        .as_str()
                        .unwrap_or(backend)
                        .to_string();
                    let by = if model.is_empty() {
                        tool
                    } else {
                        format!("{tool} ({model})")
                    };
                    return Some(transit::Summary {
                        text,
                        by: transit::SummaryBy::Model(by),
                    });
                }
                eprintln!("clarp-host: {backend} gave no summary of a moving conversation");
            }
            Err(why) => eprintln!(
                "clarp-host: {backend} could not summarize a moving conversation: {}",
                why.words()
            ),
        }
    }
    None
}

async fn post(State(app): State<App>, body: Bytes) -> Response {
    let asked = match crate::roster::object(&body) {
        Ok(asked) => asked,
        Err(refusal) => return refusal.into_response(),
    };
    match start(&app, &asked).await {
        Ok(id) => (StatusCode::ACCEPTED, Json(json!({ "transit_id": id }))).into_response(),
        Err(refusal) => refusal.into_response(),
    }
}

async fn read(Query(query): Query<HashMap<String, String>>) -> Response {
    let asked = |key: &str| query.get(key).map_or("", |said| said.trim());
    let state = state_of(asked("transit_id")).filter(|state| {
        let session = asked("session");
        session.is_empty() || state["session"] == session || state["agent_id"] == session
    });
    match state {
        Some(state) => Json(state).into_response(),
        None => (
            StatusCode::NOT_FOUND,
            Json(json!({"error": "unknown transit"})),
        )
            .into_response(),
    }
}

/// Begins a change and waits until it is done or failed: what `clarp-admin agent transit`
/// prints. Answers the change's last state.
pub(crate) async fn start_and_wait(app: &App, asked: &Fields) -> Result<Value, Refusal> {
    let id = start(app, asked).await?;
    for _ in 0..(10 * 60 * 5) {
        if let Some(state) = state_of(&id)
            && matches!(state["phase"].as_str(), Some("done" | "failed"))
        {
            return Ok(state);
        }
        tokio::time::sleep(Duration::from_millis(200)).await;
    }
    Ok(state_of(&id).unwrap_or_else(|| json!({ "transit_id": id })))
}

pub fn routes() -> Router<App> {
    Router::new().route("/agent-transit", get(read).post(post))
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;

    #[test]
    fn a_tool_says_its_version_among_other_words() {
        assert_eq!(
            version_in("2.1.321 (Claude Code)").as_deref(),
            Some("2.1.321")
        );
        assert_eq!(version_in("codex-cli 0.167.1").as_deref(), Some("0.167.1"));
        assert_eq!(version_in("unknown"), None);
    }

    #[test]
    fn a_failure_is_told_in_a_persons_words() {
        assert_eq!(
            plain_reason("register", "opencode", "OpenCode"),
            "the conversation couldn't be imported into OpenCode"
        );
        assert_eq!(
            plain_reason("register", "codex", "Codex"),
            "the conversation couldn't be written for Codex"
        );
        assert_eq!(
            plain_reason("turn_running", "codex", "Codex"),
            "a turn started"
        );
    }

    #[test]
    fn codex_is_held_to_the_window_its_configuration_sets_for_the_model_it_runs() {
        let config: toml::Table = "model = \"gpt-6-luna\"\nmodel_context_window = 1000000\n"
            .parse()
            .unwrap();
        assert_eq!(codex_window(&config, "gpt-6-luna"), Some(1_000_000));
        assert_eq!(
            codex_window(&config, ""),
            Some(1_000_000),
            "the tool's default"
        );
        assert_eq!(codex_window(&config, "gpt-6.2-sol"), None, "another model");
        let profiled: toml::Table = "profile = \"big\"\nmodel = \"gpt-6-luna\"\n\
            model_context_window = 1000000\n[profiles.big]\nmodel = \"gpt-6.2-sol\"\n\
            model_context_window = 400000\n"
            .parse()
            .unwrap();
        assert_eq!(codex_window(&profiled, "gpt-6.2-sol"), Some(400_000));
        assert_eq!(codex_window(&profiled, "gpt-6-luna"), None);
        let none: toml::Table = "model = \"gpt-6-luna\"\n".parse().unwrap();
        assert_eq!(codex_window(&none, "gpt-6-luna"), None);
        assert_eq!(
            transit::budget_of_tokens(1_000_000),
            1_800_000,
            "60% of a million tokens"
        );
    }

    #[test]
    fn the_row_of_a_change_says_what_was_left_out() {
        assert_eq!(
            done_row("Codex", "GPT-5.5", 0, false),
            "Now on Codex · GPT-5.5. The conversation came along."
        );
        assert_eq!(
            done_row("Codex", "GPT-5.5", 3, false),
            "Now on Codex · GPT-5.5. The conversation came along, except its oldest 3 turns."
        );
        assert_eq!(
            done_row("Codex", "GPT-5.5", 1, false),
            "Now on Codex · GPT-5.5. The conversation came along, except its oldest turn."
        );
        assert_eq!(
            done_row("Codex", "GPT-5.5", 3, true),
            "Now on Codex · GPT-5.5. The conversation came along, its oldest 3 turns as a summary."
        );
    }

    #[test]
    fn a_tool_that_cannot_be_moved_to_says_why() {
        let used_up = crate::quota::UsedUp::new();
        let mut row = json!({"installed": true, "models": [{"id": "a"}, {"id": "b"}]});
        judge("muse", &mut row, &used_up, None);
        assert_eq!(row["available"], false);
        assert_eq!(row["unavailable_reason"], "not_supported");
        let mut row = json!({"installed": false, "models": []});
        judge("agy", &mut row, &used_up, None);
        assert_eq!(row["unavailable_reason"], "not_installed");
        let mut row = json!({"installed": true, "models": [{"id": "a"}, {"id": "b"}]});
        judge("muse", &mut row, &used_up, Some("b"));
        assert_eq!(row["models"][0]["default"], false);
        assert_eq!(row["models"][1]["default"], true);
        // The tool was asked: a model it did not name is not offered, even as the default.
        let probed = json!({"kind": "cli_probe"});
        let known = json!({"kind": "static_fallback"});
        let mut row = json!({"installed": true, "source": probed, "models": [
            {"id": "seen-1", "source": probed}, {"id": "seen-2", "source": probed},
            {"id": "known", "source": known}]});
        judge("codex", &mut row, &used_up, Some("known"));
        let ids: Vec<&str> = row["models"]
            .as_array()
            .unwrap()
            .iter()
            .map(|model| model["id"].as_str().unwrap())
            .collect();
        assert_eq!(ids, ["seen-1", "seen-2"]);
        assert_eq!(row["models"][0]["default"], true);
        // Not asked (no answer): the bundled catalogue is all there is, and it is offered.
        let mut row = json!({"installed": true, "source": known, "models": [
            {"id": "known", "source": known}]});
        judge("codex", &mut row, &used_up, None);
        assert_eq!(row["models"][0]["id"], "known");
        // Seen running one model (Claude Code's cache): that lists nothing, and the
        // catalogue's models stay beside it.
        let cached = json!({"kind": "cli_cache"});
        let mut row = json!({"installed": true, "source": cached, "models": [
            {"id": "seen", "source": cached}, {"id": "opus", "source": known},
            {"id": "sonnet", "source": known}]});
        judge("claude", &mut row, &used_up, None);
        assert_eq!(row["models"].as_array().unwrap().len(), 3);
        assert_eq!(row["models"][0]["default"], true);
    }

    #[test]
    fn a_change_is_found_by_the_request_that_began_it_and_runs_once() {
        let mut book = Book::default();
        let state = json!({"transit_id": "t-1", "agent_id": "a-1", "phase": "reading"});
        book.begin(Some(("a-1".into(), "r-1".into())), state);
        assert_eq!(book.asked[&("a-1".to_string(), "r-1".to_string())], "t-1");
        assert_eq!(book.running["a-1"], "t-1");
        // Far more finished ones than are kept: the one that runs stays.
        for at in 0..(KEPT + 10) {
            let id = format!("old-{at}");
            book.begin(
                None,
                json!({"transit_id": id, "agent_id": format!("b-{at}")}),
            );
            book.running.remove(&format!("b-{at}"));
        }
        assert!(book.states.contains_key("t-1"));
        assert!(book.states.len() <= KEPT + 1);
    }
}
