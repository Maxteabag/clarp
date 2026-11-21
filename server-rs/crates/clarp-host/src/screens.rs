//! What an app's screens ask of a Host that has nothing of the kind yet.
//!
//! The phone app asks a Host about fourteen features and calls everything else when its
//! user opens the screen: whether an agent dreams, its portraits, the vocabulary, the
//! Oracle. Where this Host has none of that, a missing route is an
//! error on a screen. So each is answered: a reading with what the Python Host says on its
//! first day, corrected where that would claim something this Host does not do (nobody
//! rings, no picture is made), and a change with a refusal in words.
//! Nothing here is kept: when one of these is built it leaves this file.
//!
//! The answers are the Python Host's own, written down from a fresh one by the conformance
//! workflow on 16 Nov 2026 (`screens.json`).

use std::collections::HashMap;

use axum::extract::{Query, State};
use axum::http::{StatusCode, Uri};
use axum::routing::{get, post};
use axum::{Json, Router};
use serde_json::{Value, json};

use crate::edge::{ApiError, App};

type Asked = Query<HashMap<String, String>>;
type Answered = Result<Json<Value>, ApiError>;

/// What a change under a door of which this Host has built a part, or nothing, is answered
/// with: refused as not built where the Python Host has a route for it, else the plain 404 of
/// a route that is not there, as there (`clarp_kernel::doors`).
fn not_built_at(uri: &Uri, what: &str) -> ApiError {
    if clarp_kernel::doors::registered(uri.path()) {
        not_built(what)
    } else {
        ApiError::plain(StatusCode::NOT_FOUND, "not found")
    }
}

/// What a change to something this Host does not have is refused with.
fn not_built(what: &str) -> ApiError {
    let why = format!("{what} not built on this Host yet");
    ApiError::new(StatusCode::NOT_IMPLEMENTED, why)
}

fn bad(why: &str) -> ApiError {
    ApiError::new(StatusCode::BAD_REQUEST, why)
}

// ---- what an agent does by itself: dreaming ---------------------------------------------------

/// Turning something off that is off is done; turning it on is refused in words.
async fn set_of_agent(app: &App, body: &[u8], field: &str, what: &str) -> Answered {
    let asked: Value = if body.is_empty() {
        json!({})
    } else {
        serde_json::from_slice(body).unwrap_or(Value::Null)
    };
    let Some(asked) = asked.as_object() else {
        return Err(bad("bad json"));
    };
    let session = crate::edge::body_text(asked, "session")?;
    if session.is_empty() {
        return Err(bad("session required"));
    }
    let Some(enabled) = asked.get(field) else {
        return Err(bad(&format!("{field} required")));
    };
    if app.fleet.directory().named(session).is_none() {
        return Err(ApiError::new(StatusCode::NOT_FOUND, "no such agent"));
    }
    if *enabled == Value::Bool(true) {
        return Err(not_built(what));
    }
    Ok(Json(
        json!({ "ok": true, "session": session, field: false }),
    ))
}

async fn set_dreaming(State(app): State<App>, body: axum::body::Bytes) -> Answered {
    set_of_agent(&app, &body, "dreaming_enabled", "Dreaming is").await
}

// ---- how the Host speaks up and judges ---------------------------------------------------------

/// `GET /hotseat/status`: how the account seats stand, as they were last read. Nothing is
/// read for it (`crate::seats`).
async fn hotseat_status(State(app): State<App>) -> Json<Value> {
    Json(crate::seats::status(&app).await)
}

/// `GET /hotseat/desk`: the desk of the account seats as it is kept; with `refresh=1` (or
/// `true`, `yes`), after a new reading of the owner's tool where one is allowed.
async fn hotseat_desk(
    State(app): State<App>,
    Query(asked): Asked,
) -> Result<Json<Value>, ApiError> {
    if crate::seats::turned_off(&app) {
        return Err(ApiError::plain(StatusCode::NOT_FOUND, "not found"));
    }
    let refresh = asked.get("refresh").map(|said| said.to_lowercase());
    let refresh = matches!(refresh.as_deref(), Some("1" | "true" | "yes"));
    Ok(Json(crate::seats::asked(&app, refresh).await))
}

/// The gate of a door of the desk that changes something: the desk turned off, then a body
/// that is no JSON object, as the Python Host asks them (`clarp_kernel::desk::gate`). This
/// Host's desk is always there.
fn desk_gate(app: &App, body: &[u8]) -> Result<Value, ApiError> {
    use clarp_kernel::desk::{self, Gate};
    match desk::gate(!crate::seats::turned_off(app), true, Some(body)) {
        Gate::Asked(asked) => Ok(asked),
        Gate::Off => Err(ApiError::plain(StatusCode::NOT_FOUND, "not found")),
        Gate::Unavailable => Err(ApiError::new(
            StatusCode::SERVICE_UNAVAILABLE,
            desk::UNAVAILABLE,
        )),
        Gate::NoObject => Err(bad(desk::NO_OBJECT)),
    }
}

/// `POST /hotseat/resume`: work that stopped goes on, or a held queue is let go, or the
/// command of a session that is no Host's is given (`crate::seats::resume`).
async fn hotseat_resume(
    State(app): State<App>,
    body: axum::body::Bytes,
) -> axum::response::Response {
    use axum::response::IntoResponse as _;
    let asked = match desk_gate(&app, &body) {
        Ok(asked) => asked,
        Err(refused) => return refused.into_response(),
    };
    let (status, said) = crate::seats::resume(&app, &asked).await;
    let status = StatusCode::from_u16(status).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR);
    (status, Json(said)).into_response()
}

/// `POST /hotseat/switch`: this Host reads the seats and moves none. Beside another Host
/// it says so in words; where it keeps the logins itself, switching from the desk is not
/// built (`crate::seats::switch`, `docs/differences.md`).
async fn hotseat_switch(
    State(app): State<App>,
    body: axum::body::Bytes,
) -> axum::response::Response {
    use axum::response::IntoResponse as _;
    let asked = match desk_gate(&app, &body) {
        Ok(asked) => asked,
        Err(refused) => return refused.into_response(),
    };
    let (status, said) = crate::seats::switch(&app, &asked).await;
    (
        StatusCode::from_u16(status).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR),
        Json(said),
    )
        .into_response()
}

async fn podcasts() -> Json<Value> {
    Json(json!({ "conversations": [], "next_cursor": null }))
}

async fn podcast_sources() -> Json<Value> {
    Json(json!({ "next_offset": null, "sources": [] }))
}

// ---- the Oracle --------------------------------------------------------------------------------

async fn oracle_calls() -> Json<Value> {
    Json(json!({ "delegations": [] }))
}

async fn no_oracle(uri: Uri) -> ApiError {
    not_built_at(&uri, "The Oracle is")
}

// ---- the Host's own software and the skills of its agents' tools -----------------------------

/// `GET /server-update`: who this Host is and whether a newer one is to be had. It is: who
/// it is, as `GET /server-info` says; that nobody looked for a newer one; and that it is
/// not updated from an app. Whoever installed it updates it, with the installer.
async fn software(State(app): State<App>) -> Json<Value> {
    let mut told = serde_json::to_value(crate::edge::identity(&app)).unwrap_or_else(|_| json!({}));
    for (key, value) in [
        ("checked", json!(false)),
        ("latest_version", json!("")),
        ("update_available", Value::Null),
        ("update_method", json!("installer")),
        ("can_update_in_app", json!(false)),
        (
            "update_command",
            json!("server-rs/tools/install-host --start"),
        ),
    ] {
        told[key] = value;
    }
    if told["image_version"].is_null() {
        told["image_version"] = json!("");
    }
    Json(told)
}

async fn no_update() -> ApiError {
    not_built("Updating this Host from an app is")
}

/// What an app can ask to have done to an agent that this Host cannot do yet: servers of
/// its own for its tool.
async fn no_servers() -> ApiError {
    not_built("Giving a tool servers of its own is")
}

pub fn routes() -> Router<App> {
    Router::new()
        .merge(crate::harness_providers::routes())
        .route("/agent-mcp", post(no_servers))
        .merge(crate::teams::routes())
        .route("/server-update", get(software).post(no_update))
        .route("/backend-auth", get(crate::logins::sign_in::read))
        .route(
            "/backend-auth/{action}",
            post(crate::logins::sign_in::change),
        )
        .route("/agent-dreaming", post(set_dreaming))
        .route(
            "/diagnostics/settings",
            get(crate::diagnostics::settings).post(crate::diagnostics::update),
        )
        .route("/diagnostics/health", get(crate::diagnostics::health))
        .route("/hotseat/status", get(hotseat_status))
        .route("/hotseat/desk", get(hotseat_desk))
        .route("/hotseat/resume", post(hotseat_resume))
        .route("/hotseat/switch", post(hotseat_switch))
        .route("/podcast-history", get(podcasts))
        .route("/podcast-history/sources", get(podcast_sources))
        .route("/oracle/calls/status", get(oracle_calls))
        .route("/oracle/{*rest}", post(no_oracle))
}
