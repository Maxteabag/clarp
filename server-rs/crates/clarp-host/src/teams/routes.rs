use axum::body::Bytes;
use axum::extract::{Path, Query, State};
use axum::http::StatusCode;
use axum::routing::{delete, get, post};
use axum::{Json, Router};
use clarp_kernel::{python, teams};
use serde::Deserialize;
use serde_json::{Map, Value, json};

use super::Error;
use crate::edge::{ApiError, App};

pub(crate) fn routes() -> Router<App> {
    Router::new()
        .route("/teams", get(list).post(create))
        .route("/teams/{id}", post(update).delete(remove_team))
        .route("/teams/{id}/members", post(add_member))
        .route("/teams/{id}/members/{agent}", delete(remove_member))
        .route("/teams/{id}/messages", get(messages))
        .route("/team-nudging", post(nudge))
}

fn bad(message: &'static str) -> ApiError {
    ApiError::new(StatusCode::BAD_REQUEST, message)
}

fn failed(error: Error) -> ApiError {
    match error {
        Error::Invalid(reason) => bad(reason),
        Error::Missing(reason) => ApiError::new(StatusCode::NOT_FOUND, reason),
        Error::Storage(reason) => unavailable(reason),
    }
}

fn unavailable(reason: impl std::fmt::Display) -> ApiError {
    eprintln!("teamStoreFail {reason}");
    ApiError::new(StatusCode::SERVICE_UNAVAILABLE, "team store unavailable")
}

fn object(raw: &Bytes) -> Result<Map<String, Value>, ApiError> {
    serde_json::from_slice(raw).map_err(|_| bad("bad json"))
}

async fn ask(
    app: App,
    request: impl FnOnce(&App) -> Result<Value, Error> + Send + 'static,
) -> Result<Json<Value>, ApiError> {
    tokio::task::spawn_blocking(move || request(&app))
        .await
        .map_err(unavailable)?
        .map(Json)
        .map_err(failed)
}

async fn list(State(app): State<App>) -> Result<Json<Value>, ApiError> {
    ask(app, |app| Ok(json!({ "teams": app.teams.list() }))).await
}

async fn create(State(app): State<App>, raw: Bytes) -> Result<Json<Value>, ApiError> {
    let body = object(&raw)?;
    ask(app, move |app| {
        let team = app.teams.create(
            format!("team-{}", crate::ids::hex(6)),
            &body,
            app.clock.now_ms(),
        )?;
        Ok(json!({ "ok": true, "team": team }))
    })
    .await
}

async fn update(
    State(app): State<App>,
    Path(id): Path<String>,
    raw: Bytes,
) -> Result<Json<Value>, ApiError> {
    let body = object(&raw)?;
    ask(app, move |app| {
        let team = app.teams.change(&id, &body, app.clock.now_ms())?;
        Ok(json!({ "ok": true, "team": team }))
    })
    .await
}

async fn add_member(
    State(app): State<App>,
    Path(id): Path<String>,
    raw: Bytes,
) -> Result<Json<Value>, ApiError> {
    let body = object(&raw)?;
    let named = body.get("agent_id").map_or_else(String::new, teams::text);
    let agent = python::strip(&named).to_owned();
    if agent.is_empty() {
        return Err(bad("agent_id required"));
    }
    ask(app, move |app| {
        if app.fleet.directory().by_id(&agent).is_none() {
            return Err(Error::Missing("team or agent not found"));
        }
        let team = app.teams.add(&id, agent, app.clock.now_ms())?;
        Ok(json!({ "ok": true, "team": team }))
    })
    .await
}

async fn remove_member(
    State(app): State<App>,
    Path((id, agent)): Path<(String, String)>,
) -> Result<Json<Value>, ApiError> {
    ask(app, move |app| {
        let team = app.teams.remove(&id, &agent)?;
        Ok(json!({ "ok": true, "team": team }))
    })
    .await
}

async fn remove_team(
    State(app): State<App>,
    Path(id): Path<String>,
) -> Result<Json<Value>, ApiError> {
    ask(app, move |app| {
        app.teams.delete(&id)?;
        Ok(json!({ "ok": true }))
    })
    .await
}

async fn nudge(State(app): State<App>, raw: Bytes) -> Result<Json<Value>, ApiError> {
    let body = object(&raw)?;
    let named = body.get("team_id").map_or_else(String::new, teams::text);
    let id = python::strip(&named).to_owned();
    if id.is_empty() {
        return Err(bad("team_id required"));
    }
    let enabled = body
        .get("nudge_enabled")
        .ok_or_else(|| bad("nudge_enabled required"))?;
    let enabled = enabled.as_bool() == Some(true);
    ask(app, move |app| {
        let team = app.teams.nudge(&id, enabled, app.clock.now_ms())?;
        Ok(json!({ "ok": true, "team": team }))
    })
    .await
}

#[derive(Deserialize)]
struct MessageQuery {
    limit: Option<String>,
}

async fn messages(
    State(app): State<App>,
    Path(id): Path<String>,
    Query(query): Query<MessageQuery>,
) -> Result<Json<Value>, ApiError> {
    let limit = query
        .limit
        .as_deref()
        .and_then(python::int)
        .filter(|value| *value != 0)
        .unwrap_or(100)
        .clamp(1, 500);
    let limit = usize::try_from(limit).unwrap_or(100);
    ask(app, move |app| {
        let directory = app.fleet.directory();
        let messages = app.teams.read(|_, deliveries| {
            let mut rows: Vec<_> = deliveries
                .get(&id)
                .into_iter()
                .flat_map(|rows| rows.values())
                .collect();
            rows.sort_by_key(|row| (row.message.created_at, &row.message.team_message_id));
            rows[rows.len().saturating_sub(limit)..]
                .iter()
                .map(|row| {
                    let message = &row.message;
                    let source = directory
                        .shown_as(&message.source_agent_id)
                        .unwrap_or_default();
                    json!({
                        "team_message_id": message.team_message_id,
                        "team_id": message.team_id,
                        "source_agent_id": message.source_agent_id,
                        "source_message_id": message.source_message_id,
                        "trace_id": message.trace_id,
                        "text": message.text,
                        "created_at": message.created_at,
                        "source_name": source.0,
                        "source_session": source.1,
                    })
                })
                .collect::<Vec<_>>()
        });
        Ok(json!({ "team_id": id, "messages": messages }))
    })
    .await
}
