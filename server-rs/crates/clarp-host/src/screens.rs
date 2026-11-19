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
use std::sync::LazyLock;

use axum::extract::{Query, State};
use axum::http::{StatusCode, Uri};
use axum::routing::{get, post};
use axum::{Json, Router};
use serde_json::{Value, json};

use crate::edge::{ApiError, App};

fn kept(path: &str) -> Value {
    static KEPT: LazyLock<Value> =
        LazyLock::new(|| serde_json::from_str(include_str!("screens.json")).unwrap_or(Value::Null));
    KEPT[path].clone()
}

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

/// The session a request names, when this Host has an agent of that name or id.
fn agent_of(app: &App, asked: &HashMap<String, String>, missing: &str) -> Result<String, ApiError> {
    let session = asked.get("session").map_or("", |said| said.trim());
    match app.fleet.directory().named(session) {
        Some(_) => Ok(session.to_string()),
        None => Err(ApiError::new(StatusCode::NOT_FOUND, missing)),
    }
}

// ---- what an agent does by itself: dreaming, an old kind of goal ----------------------------

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
    let session = asked
        .get("session")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim();
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

/// `GET /agent-goal?session=`: the goal an agent works toward by itself in the old way.
/// This Host keeps an agent's goals as its plans (`GET /task-plans`), and none of that kind.
async fn agent_goal(State(app): State<App>, Query(asked): Asked) -> Answered {
    if asked
        .get("session")
        .is_none_or(|said| said.trim().is_empty())
    {
        return Err(bad("session required"));
    }
    let session = agent_of(&app, &asked, "no such agent")?;
    Ok(Json(json!({ "session": session, "goal": null })))
}

async fn no_agent_goal(uri: Uri) -> ApiError {
    not_built_at(&uri, "A goal of this kind is")
}

async fn portrait_generation() -> Json<Value> {
    Json(kept("/agent-portrait-generation"))
}

async fn avatar_settings() -> Json<Value> {
    Json(kept("/avatar-settings"))
}

async fn no_pictures() -> ApiError {
    not_built("Making and choosing pictures is")
}

// ---- how the Host speaks up and judges ---------------------------------------------------------

async fn diagnostics() -> Json<Value> {
    Json(kept("/diagnostics/settings"))
}

async fn no_diagnostics() -> ApiError {
    not_built("Diagnostics of this kind are")
}

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

// ---- the tools' logins and what they use ------------------------------------------------------

/// `GET /backend-auth` (`?harnesses=all` for an app that lists every harness): whether each
/// tool this Host runs is installed and has a login, read off the file the tool keeps it in.
async fn logins(State(app): State<App>, Query(asked): Asked) -> Answered {
    let every = asked.get("harnesses").is_some_and(|said| said == "all");
    let now = app.clock.now_ms();
    let read = move || {
        let home = crate::config::home();
        let mut told = crate::logins::told(&home, every, now);
        if app.config.ask_usage
            && let Some(rows) = told["backends"].as_array_mut()
        {
            for row in rows.iter_mut() {
                if row["state"] == "unverified"
                    && row["id"]
                        .as_str()
                        .is_some_and(|tool| app.limits.login_verified(tool, &home, now))
                {
                    row["state"] = json!("valid");
                    row["logged_in"] = json!(true);
                }
            }
        }
        told
    };
    let told = tokio::task::spawn_blocking(read).await;
    let mut told = told.map_err(|_| {
        ApiError::new(
            StatusCode::SERVICE_UNAVAILABLE,
            "the logins could not be read",
        )
    })?;
    if let Some(row) = told["backends"].as_array_mut().and_then(|rows| {
        rows.iter_mut()
            .find(|row| row["id"] == "grok" && row["installed"] == true)
    }) {
        crate::logins::probe_grok(row).await;
    }
    Ok(Json(told))
}

async fn no_logins(uri: Uri) -> ApiError {
    not_built_at(&uri, "Signing a tool in or out from an app is")
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

/// `GET /managed-skills`: the skills this Host keeps installed for its agents' tools. It
/// keeps none: the skills a tool finds on this machine are whoever's put them there.
async fn skills() -> Json<Value> {
    Json(json!({ "skills": [] }))
}

async fn no_skills() -> ApiError {
    not_built("Keeping skills installed is")
}

/// `GET /harness-providers?harness=`: the providers of models a harness can be signed in
/// to. This Host runs no harness that takes one: none is installed, none is listed.
async fn model_providers(Query(asked): Asked) -> Json<Value> {
    let harness = match asked.get("harness").map(|said| said.trim()) {
        Some(named) if !named.is_empty() => named,
        _ => "opencode",
    };
    Json(json!({
        "catalogue_available": false, "harness": harness, "installed": false,
        "providers": [], "store_harness": harness, "tasks": {},
    }))
}

/// `GET /harness-providers/models?provider=`: no provider is known here.
async fn models_of_provider(Query(asked): Asked) -> ApiError {
    let provider = asked.get("provider").map_or("", |said| said.trim());
    bad(&format!("unknown provider {provider}"))
}

async fn no_model_providers(uri: Uri) -> ApiError {
    not_built_at(&uri, "Signing a harness in to a provider of models is")
}

/// What an app can ask to have done to an agent that this Host cannot do yet: servers of
/// its own for its tool.
async fn no_servers() -> ApiError {
    not_built("Giving a tool servers of its own is")
}

async fn no_teams() -> ApiError {
    not_built("Teams are")
}

async fn no_model_install() -> ApiError {
    not_built("Installing a transcription model from an app is")
}

pub fn routes() -> Router<App> {
    Router::new()
        .route("/harness-providers", get(model_providers))
        .route("/harness-providers/models", get(models_of_provider))
        .route("/harness-providers/{action}", post(no_model_providers))
        .route("/agent-mcp", post(no_servers))
        .route("/team-nudging", post(no_teams))
        .route("/transcription-models/install", post(no_model_install))
        .route("/server-update", get(software).post(no_update))
        .route("/managed-skills", get(skills).post(no_skills))
        .route("/backend-auth", get(logins))
        .route("/backend-auth/{action}", post(no_logins))
        .route("/agent-dreaming", post(set_dreaming))
        .route("/agent-goal", get(agent_goal).post(no_agent_goal))
        .route("/agent-goal/{action}", post(no_agent_goal))
        .route(
            "/agent-portrait-generation",
            get(portrait_generation).post(no_pictures),
        )
        .route("/avatar-settings", get(avatar_settings).post(no_pictures))
        .route(
            "/diagnostics/settings",
            get(diagnostics).post(no_diagnostics),
        )
        .route("/hotseat/status", get(hotseat_status))
        .route("/hotseat/desk", get(hotseat_desk))
        .route("/hotseat/resume", post(hotseat_resume))
        .route("/hotseat/switch", post(hotseat_switch))
        .route("/podcast-history", get(podcasts))
        .route("/podcast-history/sources", get(podcast_sources))
        .route("/oracle/calls/status", get(oracle_calls))
        .route("/oracle/{*rest}", post(no_oracle))
}
