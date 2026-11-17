//! An agent's plans and goals, as the apps read them and as the user acts on them.
//!
//! The rules are in [`clarp_kernel::goals`], and an agent keeps its plans with its own tool
//! (`clarp-goal`, [`crate::goaltool`]). Here are the routes the apps have beside it, answered
//! as the Python Host's are (`server/lib/routes/goals.py`): the plan a session works, its
//! plans, a document of a goal, and what the user does to a goal. The user may do what the
//! goal's owner may, and one thing more: set a criterion aside.
//!
//! What is read is read off the books the agents publish. What the user does is carried out
//! on the thread of the agent that owns the plan, the way the agent's own tool is: one
//! writer for a plan, whoever acts on it.

use std::collections::HashMap;
use std::sync::Arc;

use axum::extract::{Query, State};
use axum::http::{HeaderMap, StatusCode};
use axum::routing::{get, post};
use axum::{Json, Router};
use serde_json::{Value, json};

use crate::edge::{ApiError, App};
use crate::fleet::Member;
use crate::goaltool::{self, Ask};

type Params = Query<HashMap<String, String>>;

/// The header an agent's turn names itself with. A request that carries one is that agent,
/// not the user.
const TURN_HEADER: &str = "x-clarp-turn";

fn session_of(query: &HashMap<String, String>) -> Result<&str, ApiError> {
    match query.get("session").map(|given| given.trim()) {
        Some(session) if !session.is_empty() => Ok(session),
        _ => Err(ApiError::new(StatusCode::BAD_REQUEST, "session required")),
    }
}

/// The agent a plan belongs to, when the Host has it.
fn owner(app: &App, plan_id: &str) -> Option<Arc<Member>> {
    let agent_id = goaltool::owner_of(plan_id);
    let directory = app.fleet.directory();
    directory
        .members
        .iter()
        .find(|member| member.record.agent_id == agent_id)
        .cloned()
}

/// The plan a session is working, or nothing.
async fn active(State(app): State<App>, Query(query): Params) -> Result<Json<Value>, ApiError> {
    let session = session_of(&query)?;
    let mut plan = app
        .member(session)
        .map(|member| member.agent.view().books.goals.show(app.clock.now_ms()))
        .filter(|plan| plan.as_object().is_some_and(|fields| !fields.is_empty()));
    crate::keeper::tell(&app, plan.iter_mut()).await;
    Ok(Json(json!({ "plan": plan })))
}

/// A session's plans, the last changed first.
async fn plans(State(app): State<App>, Query(query): Params) -> Result<Json<Value>, ApiError> {
    let session = session_of(&query)?;
    let mut plans = app.member(session).map_or_else(
        || json!([]),
        |member| member.agent.view().books.goals.list(app.clock.now_ms()),
    );
    crate::keeper::tell(&app, plans.as_array_mut().into_iter().flatten()).await;
    Ok(Json(json!({ "plans": plans })))
}

/// One version of a goal's document: the newest, or the one asked for.
async fn document(State(app): State<App>, Query(query): Params) -> Result<Json<Value>, ApiError> {
    let not_found = |why: String| ApiError::new(StatusCode::NOT_FOUND, why);
    let given = |key: &str| query.get(key).map_or("", String::as_str);
    let revision =
        match query.get("revision") {
            None => None,
            Some(raw) => Some(raw.trim().parse::<i64>().map_err(|_| {
                not_found(format!("invalid literal for int() with base 10: '{raw}'"))
            })?),
        };
    let plan_id = given("plan_id");
    let Some(member) = owner(&app, plan_id) else {
        return Err(not_found("goal document not found".to_string()));
    };
    let books = member.agent.view().books.clone();
    match books.goals.read(plan_id, given("name"), revision) {
        Ok(document) => Ok(Json(json!({ "document": document }))),
        Err(why) => Err(not_found(why.to_string())),
    }
}

/// A field as the text it is, where what says nothing is no text.
fn said(data: &serde_json::Map<String, Value>, key: &str) -> String {
    match data.get(key) {
        Some(Value::String(text)) => text.clone(),
        Some(Value::Null | Value::Bool(false)) | None => String::new(),
        Some(other) => other.to_string(),
    }
}

/// What the user does to a goal: `{plan_id, revision, action, data}`. Answers the plan as it
/// then is, or 409 with the rule that refused.
async fn action(
    State(app): State<App>,
    headers: HeaderMap,
    body: axum::body::Bytes,
) -> Result<Json<Value>, ApiError> {
    let given: Value = if body.is_empty() {
        json!({})
    } else {
        serde_json::from_slice(&body).unwrap_or(Value::Null)
    };
    let Some(given) = given.as_object() else {
        return Err(ApiError::new(StatusCode::BAD_REQUEST, "bad json"));
    };
    let conflict = |why: &str| ApiError::new(StatusCode::CONFLICT, why);
    let plan_id = said(given, "plan_id");
    let Some(revision) = given.get("revision").and_then(Value::as_i64) else {
        return Err(conflict("revision required"));
    };
    // The apps call this for the user; an agent uses its own tool. A turn's token that is
    // there has to be one the Host gave out, and this Host gives none out yet: whoever
    // carries one is never the user.
    if headers.contains_key(TURN_HEADER) {
        return Err(ApiError::new(
            StatusCode::FORBIDDEN,
            "verified turn identity required",
        ));
    }
    let ask = Ask::UserAct {
        plan_id: plan_id.clone(),
        revision,
        action: said(given, "action"),
        data: given.get("data").cloned().unwrap_or(Value::Null),
    };
    let answered = match owner(&app, &plan_id) {
        Some(member) => member.agent.goal(ask).await?,
        None => goaltool::nobody(&ask, app.clock.now_ms()),
    };
    if answered.exit != 0 {
        return Err(conflict(goaltool::refusal(&answered)));
    }
    let mut plan: Value = serde_json::from_str(answered.stdout.trim()).unwrap_or(Value::Null);
    crate::keeper::tell(&app, std::iter::once(&mut plan)).await;
    Ok(Json(json!({ "plan": plan })))
}

pub fn routes() -> Router<App> {
    Router::new()
        .route("/task-plan", get(active))
        .route("/task-plans", get(plans))
        .route("/task-plan/document", get(document))
        .route("/task-plan/action", post(action))
}
