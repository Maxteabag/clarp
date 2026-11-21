//! Native Codex goal controls, backed by the agent worker's lane.

use std::collections::HashMap;

use axum::body::Bytes;
use axum::extract::{Query, State};
use axum::http::StatusCode;
use axum::routing::{get, post};
use axum::{Json, Router};
use clarp_kernel::agent_goal::Goal;
use clarp_kernel::python;
use clarp_shim::codex_lane::GoalAction;
use serde_json::{Value, json};

use crate::edge::{ApiError, App, body_text};

pub(crate) fn routes() -> Router<App> {
    Router::new()
        .route("/agent-goal", get(read).post(start))
        .route("/agent-goal/pause", post(pause))
        .route("/agent-goal/resume", post(resume))
        .route("/agent-goal/clear", post(clear))
}

async fn read(
    State(app): State<App>,
    Query(params): Query<HashMap<String, String>>,
) -> Result<Json<Value>, ApiError> {
    let session = params.get("session").map_or("", |s| python::strip(s));
    let member = member(&app, session)?;
    let view = member.agent.view();
    Ok(Json(json!({
        "session": session,
        "goal": view.native_goal.as_ref().map(Goal::public),
    })))
}

async fn start(State(app): State<App>, body: Bytes) -> Result<Json<Value>, ApiError> {
    mutate(app, &body, GoalAction::Start).await
}

async fn pause(State(app): State<App>, body: Bytes) -> Result<Json<Value>, ApiError> {
    mutate(app, &body, GoalAction::Pause).await
}

async fn resume(State(app): State<App>, body: Bytes) -> Result<Json<Value>, ApiError> {
    mutate(app, &body, GoalAction::Resume).await
}

async fn clear(State(app): State<App>, body: Bytes) -> Result<Json<Value>, ApiError> {
    mutate(app, &body, GoalAction::Clear).await
}

async fn mutate(app: App, body: &[u8], action: GoalAction) -> Result<Json<Value>, ApiError> {
    let bad = |error| ApiError::new(StatusCode::BAD_REQUEST, error);
    let given: Value = serde_json::from_slice(body).map_err(|_| bad("bad json"))?;
    let given = given.as_object().ok_or_else(|| bad("bad json"))?;
    let session = body_text(given, "session")?;
    if session.is_empty() {
        return Err(bad("session required"));
    }
    let objective = given
        .get("objective")
        .filter(|value| python::truthy(value))
        .map(|value| match value {
            Value::String(text) => python::strip(text).to_owned(),
            other => python::repr(other),
        })
        .unwrap_or_default();
    if action == GoalAction::Start && objective.is_empty() {
        return Err(bad("objective required"));
    }
    let member = member(&app, session)?;
    let backend = &member.record.backend;
    if backend != "codex" {
        let declared = clarp_backend::catalogue::declared(backend);
        let label = declared["shown"]["label"].as_str().unwrap_or(backend);
        return Err(ApiError::new(
            StatusCode::NOT_IMPLEMENTED,
            format!("{label} has no goal control Clarp can drive yet."),
        ));
    }
    if member.agent.view().status.backend_session.is_none() {
        return Err(bad(
            "Send the agent a message first; a goal needs a conversation to run in.",
        ));
    }
    let goal = member
        .agent
        .native_goal(action, objective)
        .await?
        .map_err(|error| ApiError::new(StatusCode::BAD_GATEWAY, error))?;
    Ok(Json(json!({
        "ok": true,
        "session": session,
        "action": action,
        "goal": goal.as_ref().map(Goal::public),
    })))
}

fn member(app: &App, session: &str) -> Result<std::sync::Arc<crate::fleet::Member>, ApiError> {
    if session.is_empty() {
        return Err(ApiError::new(StatusCode::BAD_REQUEST, "session required"));
    }
    app.member(session)
        .ok_or_else(|| ApiError::new(StatusCode::NOT_FOUND, "no such agent"))
}
