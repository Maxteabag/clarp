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
use axum::http::StatusCode;
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

async fn no_agent_goal() -> ApiError {
    not_built("A goal of this kind is")
}

/// `GET /goal-ledger?plan_id=&after=&limit=`: what was written into a goal's ledger. This
/// Host keeps no ledger: a goal's history is in its plan.
async fn goal_ledger(Query(asked): Asked) -> Answered {
    let said = |key: &str| asked.get(key).map_or("", |said| said.trim());
    if said("plan_id").is_empty() {
        return Err(bad("plan_id required"));
    }
    let whole = |key: &str| said(key).is_empty() || said(key).parse::<i64>().is_ok();
    if !(whole("after") && whole("limit")) {
        return Err(bad("after and limit are integers"));
    }
    Ok(Json(json!({
        "events": [], "has_more": false, "next_after": 0, "plan_id": said("plan_id"),
    })))
}

// ---- pictures ------------------------------------------------------------------------------

/// `GET /agent-portrait-generation?session=`: no picture is made here.
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

async fn judgments() -> Json<Value> {
    Json(kept("/judgments/settings"))
}

async fn no_judgments() -> ApiError {
    not_built("Judgments are")
}

async fn diagnostics() -> Json<Value> {
    Json(kept("/diagnostics/settings"))
}

async fn no_diagnostics() -> ApiError {
    not_built("Diagnostics of this kind are")
}

async fn janitor_policy() -> Json<Value> {
    Json(kept("/janitor-policy"))
}

async fn no_janitors() -> ApiError {
    not_built("Janitors are")
}

/// `GET /hotseat/status`: how the account seats stand, as they were last read. Nothing is
/// read for it (`crate::seats`).
async fn hotseat_status(State(app): State<App>) -> Json<Value> {
    Json(crate::seats::status(&app).await)
}

/// `GET /hotseat/desk`: the desk of the account seats as it is kept; with `refresh=1` (or
/// `true`, `yes`), after a new reading of the owner's tool where one is allowed.
async fn hotseat_desk(State(app): State<App>, Query(asked): Asked) -> Json<Value> {
    let refresh = asked.get("refresh").map(|said| said.to_lowercase());
    let refresh = matches!(refresh.as_deref(), Some("1" | "true" | "yes"));
    Json(crate::seats::asked(&app, refresh).await)
}

/// `POST /hotseat/resume`: work that stopped goes on, or a held queue is let go, or the
/// command of a session that is no Host's is given (`crate::seats::resume`).
async fn hotseat_resume(
    State(app): State<App>,
    body: axum::body::Bytes,
) -> axum::response::Response {
    use axum::response::IntoResponse as _;
    let asked: Value = serde_json::from_slice(&body).unwrap_or(Value::Null);
    if !asked.is_object() {
        return bad("bad json").into_response();
    }
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
    let asked: Value = serde_json::from_slice(&body).unwrap_or(Value::Null);
    if !asked.is_object() {
        return bad("bad json").into_response();
    }
    match crate::seats::switch(&app, &asked) {
        Some((status, said)) => {
            let status = StatusCode::from_u16(status).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR);
            (status, Json(said)).into_response()
        }
        None => no_hotseat().await.into_response(),
    }
}

async fn no_hotseat() -> ApiError {
    not_built("Changing the account seats is")
}

/// `GET /cartesia-voices`: the old picker of Cartesia's voices. The catalogue of voices
/// (`GET /voice-catalog`) has them; this list is not kept.
async fn cartesia_voices() -> Json<Value> {
    Json(json!({ "available": false, "voices": [] }))
}

async fn podcasts() -> Json<Value> {
    Json(json!({ "conversations": [], "next_cursor": null }))
}

async fn podcast_sources() -> Json<Value> {
    Json(json!({ "next_offset": null, "sources": [] }))
}

// ---- the vocabulary an engine is asked to listen for ---------------------------------------------

async fn vocab_packs() -> Json<Value> {
    Json(json!({ "packs": [] }))
}

async fn vocab_terms(Query(asked): Asked) -> Answered {
    let pack = asked.get("pack_id").map_or("", |said| said.trim());
    if pack.is_empty() {
        return Err(bad("pack_id is required"));
    }
    Ok(Json(json!({ "pack_id": pack, "terms": [] })))
}

async fn vocab_profiles() -> Json<Value> {
    Json(json!({ "profiles": [] }))
}

async fn vocab_profile() -> ApiError {
    ApiError::new(StatusCode::NOT_FOUND, "no such profile")
}

async fn vocab_assignments() -> Json<Value> {
    Json(json!({ "assignments": [] }))
}

async fn vocab_runs() -> Json<Value> {
    Json(json!({ "runs": [] }))
}

async fn no_vocabulary() -> ApiError {
    not_built("The vocabulary is")
}

// ---- the Oracle --------------------------------------------------------------------------------

async fn oracle_calls() -> Json<Value> {
    Json(json!({ "delegations": [] }))
}

async fn no_oracle() -> ApiError {
    not_built("The Oracle is")
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

async fn no_logins() -> ApiError {
    not_built("Signing a tool in or out from an app is")
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

async fn no_model_providers() -> ApiError {
    not_built("Signing a harness in to a provider of models is")
}

/// What an app can ask to have done to an agent that this Host cannot do yet: servers of
/// its own for its tool.
async fn no_servers() -> ApiError {
    not_built("Giving a tool servers of its own is")
}

async fn no_teams() -> ApiError {
    not_built("Teams are")
}

pub fn routes() -> Router<App> {
    Router::new()
        .route("/harness-providers", get(model_providers))
        .route("/harness-providers/models", get(models_of_provider))
        .route("/harness-providers/{action}", post(no_model_providers))
        .route("/agent-mcp", post(no_servers))
        .route("/team-nudging", post(no_teams))
        .route("/server-update", get(software).post(no_update))
        .route("/managed-skills", get(skills).post(no_skills))
        .route("/backend-auth", get(logins))
        .route("/backend-auth/{action}", post(no_logins))
        .route("/agent-dreaming", post(set_dreaming))
        .route("/agent-goal", get(agent_goal).post(no_agent_goal))
        .route("/agent-goal/{action}", post(no_agent_goal))
        .route("/goal-ledger", get(goal_ledger))
        .route(
            "/agent-portrait-generation",
            get(portrait_generation).post(no_pictures),
        )
        .route("/avatar-settings", get(avatar_settings).post(no_pictures))
        .route("/judgments/settings", get(judgments).post(no_judgments))
        .route(
            "/diagnostics/settings",
            get(diagnostics).post(no_diagnostics),
        )
        .route("/janitor-policy", get(janitor_policy).post(no_janitors))
        .route("/hotseat/status", get(hotseat_status))
        .route("/hotseat/desk", get(hotseat_desk))
        .route("/hotseat/resume", post(hotseat_resume))
        .route("/hotseat/switch", post(hotseat_switch))
        .route("/hotseat/{action}", post(no_hotseat))
        .route("/cartesia-voices", get(cartesia_voices))
        .route("/podcast-history", get(podcasts))
        .route("/podcast-history/sources", get(podcast_sources))
        .route("/vocab/packs", get(vocab_packs).post(no_vocabulary))
        .route("/vocab/terms", get(vocab_terms).post(no_vocabulary))
        .route("/vocab/profiles", get(vocab_profiles).post(no_vocabulary))
        .route("/vocab/profile", get(vocab_profile).post(no_vocabulary))
        .route(
            "/vocab/assignments",
            get(vocab_assignments).post(no_vocabulary),
        )
        .route("/vocab/runs", get(vocab_runs))
        .route("/vocab/run", post(no_vocabulary))
        .route("/vocab/preview", post(no_vocabulary))
        .route("/vocab/profiles/workspace", post(no_vocabulary))
        .route("/oracle/calls/status", get(oracle_calls))
        .route("/oracle/{*rest}", post(no_oracle))
}
