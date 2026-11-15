//! The roster's edits, as the apps ask for them.
//!
//! What the user changes about an agent after it is made: its name (`POST /agent-rename`),
//! whether what it says pages the user (`/agent-mute`), whether it is put away
//! (`/agent-archive`), and which model it runs with how much effort (`/agent-llm`). And the
//! agents that are put away, a page at a time (`GET /agents/archived`), for the apps that
//! read the snapshot without them.
//!
//! The rules are in [`clarp_kernel::roster`]. The change itself is the fleet's
//! ([`crate::fleet::Amend`]): one writer for who an agent is. The routes answer as the
//! Python Host's do (`server/lib/routes/agents.py`, `routes/voice.py`), in its words where
//! they refuse.

use std::collections::HashMap;

use axum::body::Bytes;
use axum::extract::{Query, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use clarp_kernel::python;
use clarp_kernel::roster as rules;
use serde_json::{Map, Value, json};

use crate::edge::{self, ApiError, App};
use crate::fleet::{AgentRecord, Amend, NotAmended};

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
fn object(body: &Bytes) -> Result<Fields, Refusal> {
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
    let session = text(&asked, "session");
    if session.is_empty() {
        return Err(bad("session required"));
    }
    let Some(muted) = asked.get("muted") else {
        return Err(bad("muted required"));
    };
    // Muted is what is plainly true. Anything else is not.
    let record = amend(&app, &session, Amend::Mute(*muted == Value::Bool(true))).await?;
    Ok(Json(
        json!({ "ok": true, "session": session, "muted": record.muted }),
    ))
}

/// `POST /agent-rename` with `{session, name}`: the name a person reads. The session name
/// and the id stay, so everything that names the agent goes on naming it.
async fn rename(State(app): State<App>, body: Bytes) -> Answered {
    let asked = object(&body)?;
    let session = text(&asked, "session");
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
    let record = amend(&app, &session, Amend::Rename(name)).await?;
    Ok(Json(
        json!({ "ok": true, "session": session, "name": record.persona }),
    ))
}

/// `POST /agent-archive` with `{session, archived}`: puts an agent away, or takes it out
/// again. An agent that is put away keeps everything and can still be written to.
async fn archive(State(app): State<App>, body: Bytes) -> Answered {
    let asked = object(&body)?;
    let session = text(&asked, "session");
    let (false, Some(archived)) = (
        session.is_empty(),
        asked.get("archived").and_then(Value::as_bool),
    ) else {
        return Err(bad("session and archived required"));
    };
    amend(&app, &session, Amend::Archive(archived)).await?;
    Ok(Json(
        json!({ "ok": true, "session": session, "archived": archived }),
    ))
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

/// Sets an agent's model and effort from what a request said, and answers what the route
/// answers. The agent's own tool for it (`clarp-admin model`) asks the same thing.
pub(crate) async fn tune(app: &App, asked: &Fields) -> Result<Value, Refusal> {
    let session = text(asked, "session");
    if session.is_empty() {
        return Err(bad("session required"));
    }
    let Some(member) = app.fleet.directory().named(&session).cloned() else {
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
    let agents: Vec<Value> = order[page.start..page.end]
        .iter()
        .filter_map(|(_, agent_id)| {
            let member = directory
                .members
                .iter()
                .find(|member| member.record.agent_id == *agent_id)?;
            Some(edge::agent_row(&directory, &families, member))
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

async fn helper_state_post(State(app): State<App>, body: Bytes) -> Answered {
    Ok(Json(mark_helper(&app, &object(&body)?).await?))
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
        "role": record.role(),
        "parent_agent_id": record.parent_agent_id,
        "helper_state": record.helper_state,
        "helper_completed_at": record.helper_completed_at_ms,
        "archived_at": record.archived_at_ms,
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
        .route("/agents/archived", get(archived))
}
