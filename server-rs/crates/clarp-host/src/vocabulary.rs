//! Vocabulary administration publishes the complete kernel book through the fleet.
//! Compiled clip and stream runs are separate records in the kept store.
use crate::edge::{ApiError, App};
use axum::{
    Json, Router,
    body::Bytes,
    extract::{Query, State},
    http::{StatusCode, Uri},
    routing::{get, post},
};
use serde_json::{Value, json};
use std::collections::HashMap;
pub(crate) mod audio;
mod compile;
pub(crate) mod judge;
mod store;
pub(crate) mod workspace;
pub(crate) use compile::{compile, stream_run};
pub(crate) use store::Desk;

async fn perform(
    app: &App,
    action: String,
    data: Value,
    write: bool,
) -> Result<Json<Value>, ApiError> {
    let root = app.config.data_dir.clone();
    let now = app.clock.now_ms();
    let directory = app.fleet.directory();
    let agent = data["agent_id"].as_str().unwrap_or("").trim();
    let known = directory.members.iter().any(|m| m.record.agent_id == agent);
    let book = &app.ears.vocab;
    let mut value = if !write && matches!(action.as_str(), "runs" | "run") {
        book.report(&app.fleet, &action, &data).await
    } else {
        book.administration(&app.fleet, &action, &data, now, known, write)
            .await
    }
    .map_err(|(status, why)| ApiError::new(status, why))?;
    if action == "run" {
        let trace = clarp_kernel::vocabulary::book::text(&data["trace_id"]);
        let url = format!("/transcription-audio?trace_id={trace}");
        if tokio::task::spawn_blocking(move || audio::exists(&root, &trace))
            .await
            .unwrap_or(false)
        {
            value["audio_url"] = json!(url);
        }
    }
    Ok(Json(value))
}

async fn view(
    State(app): State<App>,
    uri: Uri,
    Query(query): Query<HashMap<String, String>>,
) -> Result<Json<Value>, ApiError> {
    if uri.path() == "/vocab/preview" {
        let word = |k: &str| query.get(k).map_or("", String::as_str);
        let delegated = clarp_kernel::heard::truthy(Some(word("delegated")));
        return compile(&app, word("session"), word("model"), delegated, None)
            .await
            .map(|(audit, _)| Json(audit))
            .map_err(|(status, why)| ApiError::new(status, why));
    }
    perform(
        &app,
        uri.path().trim_start_matches("/vocab/").into(),
        json!(query),
        false,
    )
    .await
}

async fn edit(State(app): State<App>, uri: Uri, body: Bytes) -> Result<Json<Value>, ApiError> {
    let data: Value = serde_json::from_slice(&body)
        .ok()
        .filter(Value::is_object)
        .ok_or_else(|| ApiError::new(StatusCode::BAD_REQUEST, "bad json"))?;
    perform(
        &app,
        uri.path().trim_start_matches("/vocab/").into(),
        data,
        true,
    )
    .await
}

pub fn routes() -> Router<App> {
    let mut routes = Router::new()
        .route("/judgments/settings", get(judge::get).post(judge::set))
        .route("/transcription-audio", get(audio::get));
    for path in ["/vocab/packs", "/vocab/terms", "/vocab/profiles"] {
        routes = routes.route(path, get(view).post(edit));
    }
    for path in [
        "/vocab/profile",
        "/vocab/assignments",
        "/vocab/runs",
        "/vocab/run",
        "/vocab/preview",
    ] {
        routes = routes.route(path, get(view));
    }
    for path in [
        "/vocab/packs/update",
        "/vocab/packs/delete",
        "/vocab/terms/update",
        "/vocab/terms/delete",
        "/vocab/profiles/delete",
        "/vocab/profiles/workspace",
        "/vocab/profiles/packs",
        "/vocab/profiles/packs/remove",
        "/vocab/assign",
        "/vocab/unassign",
    ] {
        routes = routes.route(path, post(edit));
    }
    routes
}
