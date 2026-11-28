//! The roster's edits, as the apps ask for them.
//!
//! What the user changes about an agent after it is made: its name (`POST /agent-rename`),
//! whether what it says pages the user (`/agent-mute`), whether it is put away
//! (`/agent-archive`), which model it runs with how much effort (`/agent-llm`), and how
//! much it says while it works on an answer that is read aloud (`/agent-voice-verbosity`). And the
//! agents that are put away, a page at a time (`GET /agents/archived`), for the apps that
//! read the snapshot without them.
//!
//! The rules are in [`clarp_kernel::roster`]. The change itself is the fleet's
//! ([`crate::fleet::Amend`]): one writer for who an agent is. The routes answer as the
//! Python Host's do (`server/lib/routes/agents.py`, `routes/voice.py`), in its words where
//! they refuse.

use std::collections::HashMap;

use axum::body::Bytes;
use axum::extract::{Extension, Query, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use clarp_kernel::python;
use clarp_kernel::roster as rules;
use clarp_kernel::told;
use serde_json::{Map, Value, json};

use crate::edge::{self, ApiError, App, Presented, Principal};
use crate::fleet::{AgentRecord, Amend, ArchivedBy, NotAmended};

pub(crate) type Fields = Map<String, Value>;

/// Why a request was not carried out: in a word, or with what the app needs to say more.
pub(crate) enum Refusal {
    Plain(ApiError),
    Told(StatusCode, Value),
}

impl Refusal {
    /// The status and the body this is answered with, the body as the Python Host writes it.
    pub(crate) fn answered(&self) -> (u16, String) {
        match self {
            Self::Plain(error) => (
                error.status().as_u16(),
                python::dumps(&json!({ "error": error.message() })),
            ),
            Self::Told(status, body) => (status.as_u16(), python::dumps(body)),
        }
    }
}

impl From<ApiError> for Refusal {
    fn from(error: ApiError) -> Self {
        Self::Plain(error)
    }
}

impl IntoResponse for Refusal {
    fn into_response(self) -> Response {
        match self {
            Self::Plain(error) => error.into_response(),
            Self::Told(status, body) => (status, Json(body)).into_response(),
        }
    }
}

type Answered = Result<Json<Value>, Refusal>;

fn bad(why: &str) -> Refusal {
    Refusal::Plain(ApiError::new(StatusCode::BAD_REQUEST, why))
}

fn no_such_agent() -> Refusal {
    Refusal::Plain(ApiError::new(StatusCode::NOT_FOUND, "no such agent"))
}

/// What is said when another agent has the name. `advice` is what follows naming it.
fn occupied_body(owner: &str, session: &str, advice: &str) -> Value {
    json!({
        "error": "contact_occupied",
        "message": format!("{owner} already has an active session.{advice}"),
        "owner": owner,
        "session": session,
    })
}

/// What is said to making an agent under a name another agent has.
pub(crate) fn occupied(owner: &str, session: &str) -> Value {
    let advice = " Open or release that chat before starting another.";
    occupied_body(owner, session, advice)
}

/// A request's body as the object it has to be. No body is an empty object.
pub(crate) fn object(body: &Bytes) -> Result<Fields, Refusal> {
    let given = if body.is_empty() {
        json!({})
    } else {
        serde_json::from_slice(body).unwrap_or(Value::Null)
    };
    match given {
        Value::Object(fields) => Ok(fields),
        _ => Err(bad("bad json")),
    }
}

/// A field that is a text, without what is around it. Anything else is no text.
fn text(fields: &Fields, key: &str) -> String {
    fields
        .get(key)
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim()
        .to_string()
}

/// Changes the agent a request names, by its session name or its id.
async fn amend(app: &App, key: &str, change: Amend) -> Result<AgentRecord, Refusal> {
    match app.fleet.amend(key.to_string(), change).await {
        Ok(Ok(record)) => Ok(record),
        Ok(Err(NotAmended::Unknown)) => Err(no_such_agent()),
        Ok(Err(NotAmended::NoContact)) => Err(Refusal::Told(
            StatusCode::CONFLICT,
            json!({ "error": "contact_pool_empty", "message": "contact_pool_empty" }),
        )),
        Ok(Err(NotAmended::Occupied { owner, session })) => Err(Refusal::Told(
            StatusCode::CONFLICT,
            occupied_body(&owner, &session, ""),
        )),
        Err(unavailable) => Err(Refusal::Plain(unavailable.into())),
    }
}

/// `POST /agent-mute` with `{session, muted}`: whether what the agent says pages the user.
/// What it says is marked unread either way.
async fn mute(State(app): State<App>, body: Bytes) -> Answered {
    let asked = object(&body)?;
    let session = edge::body_text(&asked, "session")?;
    if session.is_empty() {
        return Err(bad("session required"));
    }
    let Some(muted) = asked.get("muted") else {
        return Err(bad("muted required"));
    };
    // Muted is what is plainly true. Anything else is not.
    let record = amend(&app, session, Amend::Mute(*muted == Value::Bool(true))).await?;
    Ok(Json(
        json!({ "ok": true, "session": session, "muted": record.muted }),
    ))
}

/// `POST /agent-rename` with `{session, name}`: the name a person reads. The session name
/// and the id stay, so everything that names the agent goes on naming it.
async fn rename(State(app): State<App>, body: Bytes) -> Answered {
    let asked = object(&body)?;
    let session = edge::body_text(&asked, "session")?;
    if session.is_empty() {
        return Err(bad("session required"));
    }
    let said = match asked.get("name") {
        Some(Value::String(name)) => name.clone(),
        Some(Value::Number(name)) => name.to_string(),
        _ => String::new(),
    };
    let Some(name) = rules::display_name(&said) else {
        return Err(bad(rules::NAME_REFUSED));
    };
    let record = amend(&app, session, Amend::Rename(name)).await?;
    Ok(Json(
        json!({ "ok": true, "session": session, "name": record.persona }),
    ))
}

/// `POST /agent-archive` with `{session, archived}`: puts an agent away, or takes it out
/// again. An agent that is put away keeps everything and can still be written to. Who asked
/// is kept as [`asked_by`] says.
async fn archive(
    State(app): State<App>,
    principal: Option<Extension<Principal>>,
    presented: Option<Extension<Presented>>,
    headers: HeaderMap,
    body: Bytes,
) -> Answered {
    let asked = object(&body)?;
    let session = text(&asked, "session");
    let (false, Some(archived)) = (
        session.is_empty(),
        asked.get("archived").and_then(Value::as_bool),
    ) else {
        return Err(bad("session and archived required"));
    };
    let by = asked_by(
        principal.map(|Extension(principal)| principal),
        presented.map(|Extension(presented)| presented),
        &headers,
    );
    Ok(Json(put_away(&app, &session, archived, by).await?))
}

/// Who put an agent away through `POST /agent-archive`: the owner where a paired device asked,
/// or an app of his on this machine (the Host's token with `X-Clarp-Client`, or in the web
/// app's cookie); the Host's token where anything else presented it (a script, a `clarp-admin`
/// with the owner's token, a call with no token on a Host that has none).
fn asked_by(
    principal: Option<Principal>,
    presented: Option<Presented>,
    headers: &HeaderMap,
) -> ArchivedBy {
    let from_an_app = headers
        .get("x-clarp-client")
        .is_some_and(|value| !value.as_bytes().iter().all(u8::is_ascii_whitespace));
    match principal {
        Some(Principal::Device { .. }) => ArchivedBy::owner(),
        Some(Principal::Administrator) if from_an_app || presented == Some(Presented::Cookie) => {
            ArchivedBy::owner()
        }
        _ => ArchivedBy::host_token(),
    }
}

/// An agent is put away (`archived`) or taken back, as `POST /agent-archive` does it: answered
/// `{ok, session, archived, archived_by}`. Who may ask is the caller's to have checked; `by`
/// is who asked, kept on the agent while it is away and named in the Host's log line of it.
pub(crate) async fn put_away(
    app: &App,
    session: &str,
    archived: bool,
    by: ArchivedBy,
) -> Result<Value, Refusal> {
    let record = amend(app, session, Amend::ArchiveBy(archived, by)).await?;
    Ok(json!({
        "ok": true,
        "session": session,
        "archived": archived,
        "archived_by": record.archived_by,
    }))
}

/// `POST /agent-voice-verbosity` with `{session, voice_verbosity}`: how much the agent says
/// while it works, when its answer is read aloud. Its next turn is told the new level; no
/// turn has to be started again for it.
async fn narration(State(app): State<App>, body: Bytes) -> Answered {
    let asked = object(&body)?;
    let session = edge::body_text(&asked, "session")?;
    if session.is_empty() {
        return Err(bad("session required"));
    }
    let Some(level) = asked.get("voice_verbosity") else {
        return Err(bad("voice_verbosity required"));
    };
    // A whole number, and nothing that only reads like one.
    let whole = match level {
        Value::Number(number) if !number.is_f64() => Some(number.as_i64()),
        _ => None,
    };
    let Some(level) = whole else {
        return Err(bad("voice_verbosity must be an integer"));
    };
    let Some(level) = level.filter(|level| (0..=told::NARRATION_MOST).contains(level)) else {
        return Err(Refusal::Told(
            StatusCode::BAD_REQUEST,
            json!({
                "error": "voice_verbosity out of range",
                "min": 0,
                "max": told::NARRATION_MOST,
            }),
        ));
    };
    let record = amend(&app, session, Amend::Narration(level)).await?;
    Ok(Json(json!({
        "ok": true,
        "session": session,
        "voice_verbosity": record.narration,
        "options": told::narration_options(),
    })))
}

/// A field that is a text or says nothing: `Ok(None)` when the request does not have it.
fn optional(asked: &Fields, key: &str, refused: &str) -> Result<Option<String>, Refusal> {
    match asked.get(key) {
        None => Ok(None),
        Some(Value::Null) => Ok(Some(String::new())),
        Some(Value::String(said)) => Ok(Some(said.trim().to_string())),
        Some(_) => Err(bad(refused)),
    }
}

/// `POST /agent-llm` with `{session, model?, effort?}`: which model the agent's next turn
/// runs with, and how much effort. Only what the request has is changed; an empty text is
/// the tool's own choice. A turn that runs keeps what it was started with.
async fn llm(State(app): State<App>, body: Bytes) -> Answered {
    Ok(Json(tune(&app, &object(&body)?).await?))
}

/// `POST /agent-mcp` selects the catalog servers for the agent's next turn.
async fn mcp(State(app): State<App>, body: Bytes) -> Answered {
    let asked = object(&body)?;
    let session = edge::body_text(&asked, "session")?;
    if session.is_empty() {
        return Err(bad("session required"));
    }
    let target = app
        .fleet
        .directory()
        .named(session)
        .map(|member| member.record.session.clone())
        .ok_or_else(no_such_agent)?;
    let Some(Value::Array(requested)) = asked.get("mcp_servers") else {
        return Err(bad("mcp_servers must be a list"));
    };
    let requested = requested.clone();
    let (chosen, available) = tokio::task::spawn_blocking(move || crate::mcp::edit(&requested))
        .await
        .map_err(|why| ApiError::new(StatusCode::SERVICE_UNAVAILABLE, why.to_string()))?;
    let record = amend(&app, &target, Amend::McpServers(chosen)).await?;
    Ok(Json(json!({
        "ok": true,
        "session": session,
        "mcp_servers": record.mcp_servers.unwrap_or_default(),
        "available_mcp_servers": available,
    })))
}

/// Sets an agent's model and effort from what a request said, and answers what the route
/// answers. The agent's own tool for it (`clarp-admin model`) asks the same thing.
pub(crate) async fn tune(app: &App, asked: &Fields) -> Result<Value, Refusal> {
    let session = edge::body_text(asked, "session")?;
    if session.is_empty() {
        return Err(bad("session required"));
    }
    let Some(member) = app.fleet.directory().named(session).cloned() else {
        return Err(no_such_agent());
    };
    let backend = member.record.backend.clone();
    let valid = clarp_backend::efforts(&backend);
    // Which models a tool has, the tool says when a turn runs: any name is taken here.
    let model = optional(asked, "model", "model must be string or null")?;
    let effort = match optional(asked, "effort", "effort must be string or null")? {
        None => None,
        Some(said) => match rules::effort(&said, valid) {
            Some(effort) => Some(effort),
            None => {
                return Err(Refusal::Told(
                    StatusCode::BAD_REQUEST,
                    json!({
                        "error": "invalid effort for backend",
                        "backend": backend,
                        "valid_efforts": valid,
                    }),
                ));
            }
        },
    };
    // By its id: the agent that was looked at is the one that is changed.
    let record = amend(app, &member.record.agent_id, Amend::Tune { model, effort }).await?;
    Ok(json!({
        "ok": true,
        "backend": backend,
        "model": record.model,
        "effort": record.effort,
        "valid_efforts": valid,
    }))
}

/// `POST /agent-provider` with `{session, codex_home?, codex_provider?, cloud_speech?}`: binds
/// a Codex agent to a model provider of its own, or lets it go (both empty or null), and says
/// whether what it says may be spoken by a speech provider in the cloud (`null`: as its kind
/// has it). What the request leaves out stays. Its next turn runs with what it now has; one
/// that runs keeps what it was started with.
async fn provider(
    State(app): State<App>,
    axum::Extension(principal): axum::Extension<edge::Principal>,
    headers: axum::http::HeaderMap,
    body: Bytes,
) -> Response {
    let reader = crate::bound_privacy::reader_of(&app, Some(&principal), &headers).await;
    let reader = match reader {
        Ok(reader) => reader,
        Err(refused) => return refused.into_response(),
    };
    let answer = match object(&body) {
        Ok(asked) => bind(&app, &asked, &reader).await,
        Err(refused) => Err(refused),
    };
    match answer {
        Ok(answer) => Json(answer).into_response(),
        Err(refused) => refused.into_response(),
    }
}

/// Binds an agent to a provider of its own, or lets it go, from what a request said, and
/// answers what the route answers. The agents' own tool for it (`clarp-admin agent provider`)
/// asks the same thing.
///
/// `add_readers` and `remove_readers` (sessions or ids) change which agents may read it while
/// it is bound ([`crate::bound_privacy`]): the owner's word is done at once; an agent's adding
/// asks the owner on a card (`asked`), and an agent cannot remove one (403 `owner_only`).
pub(crate) async fn bind(
    app: &App,
    asked: &Fields,
    reader: &crate::bound_privacy::Reader,
) -> Result<Value, Refusal> {
    let session = edge::body_text(asked, "session")?;
    if session.is_empty() {
        return Err(bad("session required"));
    }
    let Some(member) = app.fleet.directory().named(session).cloned() else {
        return Err(no_such_agent());
    };
    let before = &member.record;
    let home = optional(asked, "codex_home", "codex_home must be string or null")?
        .unwrap_or_else(|| before.codex_home.clone());
    let provider = optional(
        asked,
        "codex_provider",
        "codex_provider must be string or null",
    )?
    .unwrap_or_else(|| before.codex_provider.clone());
    let cloud_speech = match asked.get("cloud_speech") {
        None => before.cloud_speech,
        Some(Value::Null) => None,
        Some(Value::Bool(on)) => Some(*on),
        Some(_) => return Err(bad("cloud_speech must be a boolean or null")),
    };
    if !home.is_empty() || !provider.is_empty() {
        let backend = before.backend.clone();
        let (said_home, said_provider) = (home.clone(), provider.clone());
        let checked = tokio::task::spawn_blocking(move || {
            crate::fleet::check_binding(&backend, &said_home, &said_provider)
        })
        .await
        .map_err(|why| ApiError::new(StatusCode::SERVICE_UNAVAILABLE, why.to_string()))?;
        if let Err(why) = checked {
            return Err(Refusal::Told(
                StatusCode::BAD_REQUEST,
                json!({"error": "invalid_codex_provider", "message": why}),
            ));
        }
    }
    let adding = agents_named(app, asked, "add_readers")?;
    let removing = agents_named(app, asked, "remove_readers")?;
    let rebinds = ["codex_home", "codex_provider", "cloud_speech"]
        .iter()
        .any(|key| asked.contains_key(*key));
    // A bound agent's binding is the owner's: anyone else asks him on a card (`bound_privacy`).
    let to = crate::bound_privacy::Binding {
        home,
        provider,
        cloud_speech,
    };
    let mut cards = Vec::new();
    let mut record = if rebinds && crate::bound_privacy::must_ask(reader, before, &to) {
        let asked = crate::bound_privacy::ask_binding(app, reader, before, &to).await;
        cards.push(asked.map_err(|(status, body)| Refusal::Told(status, body))?);
        before.clone()
    } else if rebinds || (adding.is_empty() && removing.is_empty()) {
        let change = Amend::Provider {
            home: to.home,
            provider: to.provider,
            cloud_speech: to.cloud_speech,
        };
        amend(app, &before.agent_id, change).await?
    } else {
        before.clone()
    };
    for (agents, add) in [(&removing, false), (&adding, true)] {
        if agents.is_empty() {
            continue;
        }
        if add && !record.bound() && *reader != crate::bound_privacy::Reader::Owner {
            let why = format!(
                "{} is not bound to a provider of its own: every agent reads it already.",
                record.persona
            );
            return Err(Refusal::Told(
                StatusCode::CONFLICT,
                json!({"error": "not_bound", "message": why}),
            ));
        }
        let changed = crate::bound_privacy::change_readers(app, reader, &record, agents, add).await;
        match changed.map_err(|(status, body)| Refusal::Told(status, body))? {
            crate::bound_privacy::ReadersChange::Done(readers) => record.readers = readers,
            crate::bound_privacy::ReadersChange::Asked(asked) => cards.extend(asked),
        }
    }
    let directory = app.fleet.directory();
    let readers: Vec<&str> = record
        .readers
        .iter()
        .filter_map(|id| {
            let mut members = directory.members.iter();
            let member = members.find(|member| member.record.agent_id == *id)?;
            Some(member.record.session.as_str())
        })
        .collect();
    let mut answer = json!({
        "ok": true,
        "session": record.session,
        "backend": record.backend,
        "codex_home": record.codex_home,
        "codex_provider": record.codex_provider,
        "cloud_speech": record.speaks_to_cloud(),
        "readers": readers,
    });
    if !cards.is_empty() {
        answer["asked"] = json!(cards);
    }
    Ok(answer)
}

/// The agents a request names under `key` (a list of sessions or ids), each once.
fn agents_named(app: &App, asked: &Fields, key: &str) -> Result<Vec<AgentRecord>, Refusal> {
    let named = match asked.get(key) {
        None | Some(Value::Null) => return Ok(Vec::new()),
        Some(Value::Array(named)) => named,
        Some(_) => return Err(bad(&format!("{key} must be a list of sessions"))),
    };
    let directory = app.fleet.directory();
    let mut agents: Vec<AgentRecord> = Vec::new();
    for said in named {
        let Some(said) = said.as_str().map(str::trim).filter(|said| !said.is_empty()) else {
            return Err(bad(&format!("{key} must be a list of sessions")));
        };
        let mut members = directory.members.iter();
        let Some(member) =
            members.find(|member| member.record.session == said || member.record.agent_id == said)
        else {
            return Err(no_such_agent());
        };
        if !agents
            .iter()
            .any(|agent| agent.agent_id == member.record.agent_id)
        {
            agents.push(member.record.clone());
        }
    }
    Ok(agents)
}

/// `GET /agents/archived?limit=&cursor=`: one page of the agents that are put away, the
/// last put away first, each as its row in the snapshot.
async fn archived(
    State(app): State<App>,
    Query(query): Query<HashMap<String, String>>,
) -> Answered {
    let given = |key: &str| query.get(key).map_or("", String::as_str);
    let refused = || bad("invalid archived agents query");
    let limit = rules::page_limit(given("limit")).ok_or_else(refused)?;
    let position = match given("cursor") {
        "" => None,
        cursor => Some(rules::place_of(cursor).ok_or_else(refused)?),
    };
    let directory = app.fleet.directory();
    let order = rules::listed(
        directory
            .members
            .iter()
            .filter_map(|member| {
                let at = member.record.archived_at_ms?;
                Some((at, member.record.agent_id.clone()))
            })
            .collect(),
    );
    let page = rules::page(&order, limit, position.as_ref());
    let families = edge::Families::of(&directory);
    let quota = crate::quota::rows(&app).await;
    let at = crate::availability::At::of(&app);
    let owners = app.mouth.owners();
    let forks = crate::conversation_forks::Named::read();
    // What the snapshot adds to a row of its own, so that the row here is the same one.
    let (live_defaults, live_available) = app.calls.taking(&app).await;
    let teams = app
        .teams
        .read(|teams, _| clarp_kernel::teams::memberships(teams));
    let context = edge::AgentRowContext {
        directory: &directory,
        families: &families,
        faces: &app.faces,
        owners: &owners,
        quota: &quota,
        forks: &forks,
        at: &at,
    };
    let agents: Vec<Value> = order[page.start..page.end]
        .iter()
        .filter_map(|(_, agent_id)| {
            let member = directory
                .members
                .iter()
                .find(|member| member.record.agent_id == *agent_id)?;
            let mut row = edge::agent_row(member, &context);
            let session = &member.record.session;
            row["team_ids"] = json!(teams.get(agent_id).map_or(&[][..], Vec::as_slice));
            row["live_call_available"] = json!(live_available.contains(session));
            if live_defaults.contains(session) {
                row["call_mode"] = json!(clarp_kernel::live_call::LIVE);
            }
            Some(row)
        })
        .collect();
    Ok(Json(json!({
        "agents": agents,
        "next_cursor": page.next_cursor,
        "total": page.total,
    })))
}

/// How a helper's work stands: `{session, state, by?}` marks it done, failed or working
/// again. `by` names the agent that marks it, which has to be the one that made it;
/// without it the mark is the user's. Answers the state the helper then has.
pub(crate) async fn mark_helper(app: &App, asked: &Fields) -> Result<Value, Refusal> {
    let (session, state) = (text(asked, "session"), text(asked, "state"));
    if session.is_empty() || state.is_empty() {
        return Err(bad("session and state required"));
    }
    let marked = app
        .fleet
        .mark(session.clone(), state, text(asked, "by"))
        .await
        .map_err(|unavailable| Refusal::Plain(unavailable.into()))?;
    match marked {
        Ok(state) => Ok(json!({ "ok": true, "session": session, "helper_state": state })),
        Err(told) => Err(Refusal::Told(
            StatusCode::from_u16(told.status).unwrap_or(StatusCode::CONFLICT),
            json!({ "error": told.code, "message": told.message }),
        )),
    }
}

async fn helper_state_post(
    State(app): State<App>,
    Extension(principal): Extension<Principal>,
    headers: axum::http::HeaderMap,
    body: Bytes,
) -> Answered {
    let mut asked = object(&body)?;
    if principal == Principal::Tools {
        let turn = headers
            .get(crate::turn_identity::HEADER)
            .and_then(|value| value.to_str().ok());
        let caller = crate::turn_identity::presented(&app, turn)
            .await
            .map_err(Refusal::Plain)?
            .ok_or_else(|| {
                Refusal::Plain(ApiError::new(
                    StatusCode::FORBIDDEN,
                    "verified turn identity required",
                ))
            })?;
        asked.insert("by".to_string(), json!(caller.record.session));
    }
    Ok(Json(mark_helper(&app, &asked).await?))
}

/// Who made an agent and how its work stands, without the whole list.
pub(crate) fn lineage(app: &App, session: &str) -> Result<Value, Refusal> {
    let session = session.trim();
    if session.is_empty() {
        return Err(bad("session required"));
    }
    let directory = app.fleet.directory();
    let Some(member) = directory.named(session) else {
        return Err(Refusal::Plain(ApiError::new(
            StatusCode::NOT_FOUND,
            "agent_not_found",
        )));
    };
    let record = &member.record;
    Ok(json!({
        "agent_id": record.agent_id,
        "session": record.session,
        "backend": record.backend,
        "role": record.role(),
        "parent_agent_id": record.parent_agent_id,
        "helper_state": crate::fleet::AboveWork::of(&directory).helper_state(member),
        "helper_completed_at": record.helper_completed_at_ms,
        "archived_at": record.archived_at_ms,
        "archived_by": record.archived_by,
    }))
}

async fn helper_state_get(
    State(app): State<App>,
    Query(query): Query<HashMap<String, String>>,
) -> Answered {
    let session = query.get("session").map_or("", String::as_str);
    Ok(Json(lineage(&app, session)?))
}

pub fn routes() -> Router<App> {
    Router::new()
        .route(
            "/agent-helper-state",
            get(helper_state_get).post(helper_state_post),
        )
        .route("/agent-mute", post(mute))
        .route("/agent-rename", post(rename))
        .route("/agent-archive", post(archive))
        .route("/agent-llm", post(llm))
        .route("/agent-mcp", post(mcp))
        .route("/agent-provider", post(provider))
        .route("/agent-voice-verbosity", post(narration))
        .route("/agents/archived", get(archived))
}
