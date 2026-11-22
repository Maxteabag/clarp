//! What an agent asks its user's phone to put into a calendar (`POST /calendar/request`).
//!
//! The request names the agent's session, a title and when the event begins and ends, and
//! may say more of it. The Host gives it an id and tells the apps that listen
//! (`calendar-request`); the one in front asks its user and writes the event into the
//! phone's own calendar. A bounded receipt window lets a device report the result. The rules are in
//! [`clarp_kernel::calendar`]; the route answers as the Python Host's
//! (`server/lib/routes/ops.py`).

use std::collections::VecDeque;
use std::sync::{Mutex, PoisonError};

use axum::body::Bytes;
use axum::extract::State;
use axum::http::StatusCode;
use axum::routing::post;
use axum::{Json, Router};
use clarp_kernel::calendar;
use clarp_kernel::conversation::Message;
use clarp_kernel::turn::{Admission, Origin};
use serde_json::{Map, Value, json};

use crate::edge::{ApiError, App};
use crate::ids;
use crate::personas::object;

const MAX_REQUESTS: usize = 4_096;
const RECEIPT_WINDOW_MS: i64 = 24 * 60 * 60 * 1_000;

#[derive(Clone)]
struct Pending {
    id: String,
    agent_id: String,
    session: String,
    title: String,
    at: i64,
}

/// A short-lived calendar request can only report back to the agent that asked for it.
#[derive(Default)]
pub struct Receipts(Mutex<VecDeque<Pending>>);

impl Receipts {
    fn remember(&self, request: Pending) {
        let mut pending = self.0.lock().unwrap_or_else(PoisonError::into_inner);
        if pending.len() >= MAX_REQUESTS {
            pending.pop_front();
        }
        pending.push_back(request);
    }

    fn find(&self, id: &str, session: &str, now: i64) -> Option<Pending> {
        self.0
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .iter()
            .find(|request| {
                request.id == id
                    && request.session == session
                    && now.saturating_sub(request.at) <= RECEIPT_WINDOW_MS
            })
            .cloned()
    }
}

/// Gives a request its id and tells the apps: what the asker is answered, or the words a
/// request is refused with that leaves out what it must say.
pub(crate) fn request(app: &App, said: &Map<String, Value>) -> Result<Value, String> {
    let request_id = format!("cal-{}", ids::hex(8));
    let event = calendar::asked(said, &request_id)?;
    let now = app.clock.now_ms();
    let session = event["session"].as_str().unwrap_or_default();
    if let Some(member) = app.member(session) {
        app.calendar_receipts.remember(Pending {
            id: request_id.clone(),
            agent_id: member.record.agent_id.clone(),
            session: session.to_string(),
            title: event["title"]
                .as_str()
                .unwrap_or_default()
                .chars()
                .take(200)
                .collect(),
            at: now,
        });
    }
    app.hub.publish(event, now);
    Ok(json!({ "ok": true, "request_id": request_id }))
}

/// `POST /calendar/request`: an agent asks for an event. Answered with the id the request
/// goes by; a request that leaves out what it must say is refused with what is missing.
async fn asked(State(app): State<App>, body: Bytes) -> Result<Json<Value>, ApiError> {
    let answered = request(&app, &object(&body)?);
    let answered = answered.map_err(|why| ApiError::new(StatusCode::BAD_REQUEST, why))?;
    Ok(Json(answered))
}

/// `POST /calendar/response`: a phone reports what happened. One receipt per request
/// enters the originating agent's mailbox, including when devices retry concurrently.
async fn responded(State(app): State<App>, body: Bytes) -> Result<Json<Value>, ApiError> {
    let data = object(&body)?;
    let request_id = crate::edge::body_text(&data, "request_id")?;
    let session = crate::edge::body_text(&data, "session")?;
    let status = crate::edge::body_text(&data, "status")?;
    if request_id.is_empty() || session.is_empty() {
        return Err(ApiError::new(
            StatusCode::BAD_REQUEST,
            "request_id and session required",
        ));
    }
    let outcome = match status {
        "added" => "was added",
        "declined" => "was declined",
        "failed" => "could not be added",
        _ => {
            return Err(ApiError::new(
                StatusCode::BAD_REQUEST,
                "invalid calendar status",
            ));
        }
    };
    let not_found = || ApiError::new(StatusCode::NOT_FOUND, "calendar request not found");
    let request = app
        .calendar_receipts
        .find(request_id, session, app.clock.now_ms())
        .ok_or_else(not_found)?;
    let directory = app.fleet.directory();
    let member = directory.by_id(&request.agent_id).ok_or_else(not_found)?;
    let sent = member
        .agent
        .send(
            Message {
                id: format!("u-calendar-response-{}", request.id),
                text: format!(
                    "Calendar request {} for {:?}: the event {outcome}.",
                    request.id, request.title
                ),
                origin: Origin::Trigger {
                    name: "calendar-response".into(),
                },
                unminted: true,
                ..Message::default()
            },
            Admission::Background,
        )
        .await?;
    if !sent.duplicate {
        app.hub.publish(
            json!({
                "type": "calendar-response",
                "request_id": request.id,
                "session": member.record.session,
                "agent_id": request.agent_id,
                "status": status,
            }),
            app.clock.now_ms(),
        );
    }
    Ok(Json(json!({ "ok": true })))
}

pub fn routes() -> Router<App> {
    Router::new()
        .route("/calendar/request", post(asked))
        .route("/calendar/response", post(responded))
}
