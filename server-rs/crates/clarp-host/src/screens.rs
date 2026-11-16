//! What an app's screens ask of a Host that has nothing of the kind yet.
//!
//! The phone app asks a Host about fourteen features and calls everything else when its
//! user opens the screen: an agent's heartbeat, its schedules and portraits, the herald, the
//! vocabulary, calls, the Oracle. Where this Host has none of that, a missing route is an
//! error on a screen. So each is answered: a reading with what the Python Host says on its
//! first day, corrected where that would claim something this Host does not do (the herald
//! is off here, nobody rings, no picture is made), and a change with a refusal in words.
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

// ---- what an agent does by itself: heartbeat, dreaming, schedules, an old kind of goal --------

/// `GET /agent-heartbeat/status?session=`: no agent of this Host is woken by a heartbeat.
async fn heartbeat_status(State(app): State<App>, Query(asked): Asked) -> Answered {
    let session = agent_of(&app, &asked, "no such agent")?;
    let mut body = kept("/agent-heartbeat/status");
    body["session"] = json!(session);
    Ok(Json(body))
}

async fn heartbeat_settings() -> Json<Value> {
    Json(kept("/heartbeat/settings"))
}

/// Turning something off that is off is done; turning it on is refused in words. `strict`:
/// what is asked has to be plainly yes or no, as the Python Host asks of a heartbeat.
async fn set_of_agent(
    app: &App,
    body: &[u8],
    (field, strict): (&str, bool),
    what: &str,
) -> Answered {
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
    if strict && !enabled.is_boolean() {
        return Err(bad(&format!("{field} must be boolean")));
    }
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

async fn set_heartbeat(State(app): State<App>, body: axum::body::Bytes) -> Answered {
    let field = ("heartbeat_enabled", true);
    set_of_agent(&app, &body, field, "An agent's heartbeat is").await
}

async fn set_dreaming(State(app): State<App>, body: axum::body::Bytes) -> Answered {
    set_of_agent(&app, &body, ("dreaming_enabled", false), "Dreaming is").await
}

async fn no_heartbeat() -> ApiError {
    not_built("The heartbeat is")
}

/// `GET /agent-schedules`: nothing wakes an agent of this Host at a time.
async fn schedules() -> Json<Value> {
    Json(json!({ "schedules": [] }))
}

async fn no_schedules() -> ApiError {
    not_built("Schedules are")
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

async fn herald() -> Json<Value> {
    Json(kept("/herald/settings"))
}

async fn no_herald() -> ApiError {
    not_built("The herald is")
}

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

/// `GET /location`: where the owner's phone last said it is. Nothing asks for it here.
async fn location() -> Json<Value> {
    Json(json!({}))
}

async fn no_location() -> ApiError {
    not_built("Asking a phone where it is is")
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

// ---- calls and the Oracle ----------------------------------------------------------------------

/// `GET /calls`: the call the owner is in with his agents. There is none here, ever.
async fn calls(State(app): State<App>) -> Json<Value> {
    Json(json!({ "call": null, "server_now": app.clock.now_ms() }))
}

/// `GET /calls/ring`: whether an agent is ringing the owner. Nobody rings from here.
async fn ring() -> Json<Value> {
    Json(kept("/calls/ring"))
}

async fn no_calls() -> ApiError {
    not_built("Calls are")
}

async fn oracle_status() -> Json<Value> {
    Json(kept("/oracle/status"))
}

async fn oracle_contact() -> Json<Value> {
    Json(kept("/oracle/contact"))
}

async fn oracle_delegations() -> Json<Value> {
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
    let read = move || crate::logins::told(&crate::config::home(), every, now);
    let told = tokio::task::spawn_blocking(read).await;
    told.map(Json).map_err(|_| {
        ApiError::new(
            StatusCode::SERVICE_UNAVAILABLE,
            "the logins could not be read",
        )
    })
}

async fn no_logins() -> ApiError {
    not_built("Signing a tool in or out from an app is")
}

/// What is said where an app looks for how much of an account is used.
const USAGE_UNREAD: &str = "Usage is not read on this Host yet";

/// `GET /backend-usage`: how much of each tool's account is used. This Host asks no
/// provider and keeps no account of its turns' cost, so each tool is told as unknown, in
/// the Python Host's fields: an app shows that, and no gauge.
async fn usage(State(app): State<App>) -> Json<Value> {
    let tools = ["claude", "codex"];
    let backends: Vec<Value> = tools
        .iter()
        .map(|tool| {
            json!({
                "backend": tool, "error": USAGE_UNREAD, "fetched_at": null,
                "freshness": "unknown", "resets_at": null, "source": "none",
                "used_percentage": null, "windows": {},
            })
        })
        .collect();
    let providers: serde_json::Map<String, Value> = tools
        .iter()
        .map(|tool| {
            let provider = json!({
                "freshness": "unknown", "observed_at": null, "provider_id": tool,
                "provider_instance_id": format!("{}:{tool}", app.server_id),
                "source": { "detail": USAGE_UNREAD, "kind": "unavailable" },
                "windows": [],
            });
            ((*tool).to_string(), provider)
        })
        .collect();
    Json(json!({
        "backends": backends,
        "capability_catalog_schema_version": 2,
        "computer_id": app.server_id,
        "limit_events": [],
        "observed_at": crate::wire::iso(app.clock.now_ms()),
        "providers": providers,
        "schema_version": 1,
    }))
}

pub fn routes() -> Router<App> {
    Router::new()
        .route("/backend-auth", get(logins))
        .route("/backend-usage", get(usage))
        .route("/backend-auth/{action}", post(no_logins))
        .route("/agent-heartbeat/status", get(heartbeat_status))
        .route("/agent-heartbeat", post(set_heartbeat))
        .route("/agent-dreaming", post(set_dreaming))
        .route(
            "/heartbeat/settings",
            get(heartbeat_settings).post(no_heartbeat),
        )
        .route(
            "/agent-schedules",
            get(schedules).post(no_schedules).delete(no_schedules),
        )
        .route("/agent-schedules/toggle", post(no_schedules))
        .route("/agent-goal", get(agent_goal).post(no_agent_goal))
        .route("/agent-goal/{action}", post(no_agent_goal))
        .route("/goal-ledger", get(goal_ledger))
        .route(
            "/agent-portrait-generation",
            get(portrait_generation).post(no_pictures),
        )
        .route("/avatar-settings", get(avatar_settings).post(no_pictures))
        .route("/herald/settings", get(herald).post(no_herald))
        .route("/judgments/settings", get(judgments).post(no_judgments))
        .route(
            "/diagnostics/settings",
            get(diagnostics).post(no_diagnostics),
        )
        .route("/janitor-policy", get(janitor_policy).post(no_janitors))
        .route("/location", get(location).post(no_location))
        .route("/location/request", post(no_location))
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
        .route("/calls", get(calls).post(no_calls))
        .route("/calls/ring", get(ring).post(no_calls))
        .route("/calls/{*rest}", post(no_calls))
        .route("/oracle/status", get(oracle_status))
        .route("/oracle/contact", get(oracle_contact).post(no_oracle))
        .route(
            "/oracle/delegations",
            get(oracle_delegations).post(no_oracle),
        )
        .route("/oracle/calls/status", get(oracle_delegations))
        .route("/oracle/{*rest}", post(no_oracle))
}
