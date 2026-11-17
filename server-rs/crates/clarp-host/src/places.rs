//! Where the owner's phone is.
//!
//! The app says where its phone is when its user shares that, by itself or because an agent
//! asked (`POST /location`); an agent's tools read the last place said for its session
//! (`GET /location`), and ask the app for a new one (`POST /location/request`), which every
//! listening app is told and the one in front shows as a question. One place for each
//! session, the newest, kept across restarts. The rules are in [`clarp_kernel::located`];
//! the routes answer as the Python Host's (`server/lib/routes/ops.py`).

use std::collections::HashMap;

use axum::body::Bytes;
use axum::extract::{Query, State};
use axum::http::StatusCode;
use axum::routing::{get, post};
use axum::{Json, Router};
use clarp_kernel::located::{Fix, WHOSE};
use clarp_kernel::python;
use serde_json::{Map, Value, json};

use crate::agent::Unavailable;
use crate::edge::{ApiError, App};
use crate::personas::object;

/// The kind of thing a place is, among what is left with the fleet.
const PLACES: &str = "place";

type Answered = Result<Json<Value>, ApiError>;

/// The session a request names in its body. None is refused.
fn session_of(said: &Map<String, Value>) -> Result<String, ApiError> {
    let named = said.get("session").and_then(Value::as_str).unwrap_or("");
    match python::strip(named) {
        "" => Err(ApiError::new(StatusCode::BAD_REQUEST, WHOSE)),
        session => Ok(session.to_string()),
    }
}

/// The last place said for a session, as it is told: an empty object when none was, or
/// when no session is named.
pub(crate) async fn last(app: &App, session: &str) -> Result<Value, Unavailable> {
    let session = python::strip(session);
    if session.is_empty() {
        return Ok(json!({}));
    }
    let kept = app.fleet.kept(PLACES, session.to_string()).await?;
    let fix = kept.as_deref().and_then(Fix::read);
    Ok(fix.map_or_else(|| json!({}), |fix| fix.told()))
}

/// An agent asks where its user is: the apps that listen are told, and the one in front
/// asks its user to share it.
pub(crate) fn ask(app: &App, session: &str) {
    let event = json!({ "type": "location-request", "session": session });
    app.hub.publish(event, app.clock.now_ms());
}

/// `GET /location?session=`: the last place said for a session; an empty object when none
/// was.
async fn told(State(app): State<App>, Query(asked): Query<HashMap<String, String>>) -> Answered {
    let session = asked.get("session").map_or("", String::as_str);
    Ok(Json(last(&app, session).await?))
}

/// `POST /location` with `{session, lat, lng, accuracy?}`: the app says where its phone is.
async fn said(State(app): State<App>, body: Bytes) -> Answered {
    let said = object(&body)?;
    let session = session_of(&said)?;
    let fix = Fix::said(&Value::Object(said), app.clock.now_ms())
        .map_err(|why| ApiError::new(StatusCode::BAD_REQUEST, why))?;
    app.fleet
        .keep(PLACES, session.clone(), Some(fix.kept()))
        .await?;
    Ok(Json(json!({
        "ok": true, "session": session, "lat": fix.lat, "lng": fix.lng,
        "accuracy": fix.accuracy, "ts": fix.ts,
    })))
}

/// `POST /location/request` with `{session}`: an agent asks where its user is. The apps
/// that listen are told; the one in front asks its user to share it.
async fn asked(State(app): State<App>, body: Bytes) -> Answered {
    let session = session_of(&object(&body)?)?;
    ask(&app, &session);
    Ok(Json(json!({ "ok": true })))
}

pub fn routes() -> Router<App> {
    Router::new()
        .route("/location", get(told).post(said))
        .route("/location/request", post(asked))
}
