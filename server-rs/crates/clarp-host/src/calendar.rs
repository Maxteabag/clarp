//! What an agent asks its user's phone to put into a calendar (`POST /calendar/request`).
//!
//! The request names the agent's session, a title and when the event begins and ends, and
//! may say more of it. The Host gives it an id and tells the apps that listen
//! (`calendar-request`); the one in front asks its user and writes the event into the
//! phone's own calendar. The Host keeps nothing of it. The rules are in
//! [`clarp_kernel::calendar`]; the route answers as the Python Host's
//! (`server/lib/routes/ops.py`).

use axum::body::Bytes;
use axum::extract::State;
use axum::http::StatusCode;
use axum::routing::post;
use axum::{Json, Router};
use clarp_kernel::calendar;
use serde_json::{Map, Value, json};

use crate::edge::{ApiError, App};
use crate::ids;
use crate::personas::object;

/// Gives a request its id and tells the apps: what the asker is answered, or the words a
/// request is refused with that leaves out what it must say.
pub(crate) fn request(app: &App, said: &Map<String, Value>) -> Result<Value, String> {
    let request_id = format!("cal-{}", ids::hex(8));
    let event = calendar::asked(said, &request_id)?;
    app.hub.publish(event, app.clock.now_ms());
    Ok(json!({ "ok": true, "request_id": request_id }))
}

/// `POST /calendar/request`: an agent asks for an event. Answered with the id the request
/// goes by; a request that leaves out what it must say is refused with what is missing.
async fn asked(State(app): State<App>, body: Bytes) -> Result<Json<Value>, ApiError> {
    let answered = request(&app, &object(&body)?);
    let answered = answered.map_err(|why| ApiError::new(StatusCode::BAD_REQUEST, why))?;
    Ok(Json(answered))
}

pub fn routes() -> Router<App> {
    Router::new().route("/calendar/request", post(asked))
}
