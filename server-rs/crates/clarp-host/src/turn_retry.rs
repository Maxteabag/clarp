//! `POST /turn-retry`: the user's message whose turn failed, sent again at the user's word
//! (feature `turn_retry`, `docs/turn-errors.md`). It is what the buttons of a turn error do:
//! try again now, at the time the account has usage again, or after the conversation was
//! made shorter.
//!
//! The message goes again as a new message of the user's, with the same words (an attachment
//! is a link in them): the chat shows that it was sent again. Its id is made from the failed
//! message's, so a second tap is the same message, not a second one. A message whose turn
//! still runs, or that has an answer, is not sent again (409). A turn after one whose history
//! the provider rejected begins a fresh session by itself (`clarp_actor`), so `fresh` asks
//! nothing more than a try again.
//!
//! An agent's tools may ask it too, as they may send; the Host's log says which turn did.
//!
//! A try at a time is kept with the fleet until it is sent (`KEPT`), so a Host started again
//! in between still sends it. The schedules' runner looks for what is due.

use axum::Router;
use axum::body::Bytes;
use axum::extract::State;
use axum::http::HeaderMap;
use axum::Extension;
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::post;
use axum::Json;
use clarp_kernel::conversation::Entry;
use clarp_kernel::turn::Origin;
use clarp_protocol::requests::SendRequest;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

use crate::edge::{ApiError, App, Principal};

/// The kind the tries at a time are kept as, one record each.
const KIND: &str = "turn-retry";

/// What this desk keeps, declared once (`kept.rs`).
pub(crate) const KEPT: &[crate::kept::Kind] = &[crate::kept::Kind::book(
    KIND,
    crate::kept::Key::Plain,
    crate::kept::fits::<Planned>,
)];

/// A message to be sent again at a time.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
struct Planned {
    agent_id: String,
    message_id: String,
    text: String,
    at_ms: i64,
}

/// How far ahead a try may be planned: a usage window resets within a week.
const FURTHEST_MS: i64 = 8 * 24 * 60 * 60 * 1000;

/// The id a message sent again is filed under: one per failed message.
fn again_id(message_id: &str) -> String {
    format!("retry-{message_id}")
}

/// The key a planned try is kept under.
fn planned_key(agent_id: &str, message_id: &str) -> String {
    format!("{agent_id}:{message_id}")
}

fn bad(why: &str) -> Response {
    ApiError::new(StatusCode::BAD_REQUEST, why).into_response()
}

/// `POST /turn-retry {"session", "message_id", "at_ms"?, "compact"?, "fresh"?}`.
async fn retry(
    State(app): State<App>,
    Extension(principal): Extension<Principal>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    let Ok(Value::Object(asked)) = serde_json::from_slice::<Value>(&body) else {
        return bad("bad json");
    };
    let text_of = |key: &str| asked.get(key).and_then(Value::as_str).map_or("", str::trim);
    let (session, message_id) = (text_of("session"), text_of("message_id"));
    if session.is_empty() || message_id.is_empty() {
        return bad("session and message_id required");
    }
    let at_ms = asked.get("at_ms").and_then(Value::as_i64);
    let compact = asked.get("compact").and_then(Value::as_bool) == Some(true);
    // A device paired with limited access may send, and so try again; making the conversation
    // shorter is a full device's.
    if compact
        && matches!(
            principal,
            Principal::Device {
                scope: crate::pairing::Scope::Limited,
                ..
            }
        )
    {
        return ApiError::new(StatusCode::FORBIDDEN, clarp_kernel::devices::LIMITED_REFUSAL)
            .into_response();
    }
    if principal == Principal::Tools {
        let turn = headers
            .get("x-clarp-turn")
            .and_then(|value| value.to_str().ok())
            .unwrap_or("none named");
        eprintln!("clarp-host: /turn-retry for {session} {message_id} asked by an agent's tools (turn {turn})");
    }
    let Some(member) = app.fleet.directory().named(session).cloned() else {
        return ApiError::new(StatusCode::NOT_FOUND, "no such agent").into_response();
    };
    let message = match member.agent.row(message_id.to_string()).await {
        Ok(Some(version)) => match version.entry {
            Entry::Message(message) => message,
            _ => return ApiError::new(StatusCode::NOT_FOUND, "message not found").into_response(),
        },
        Ok(None) => return ApiError::new(StatusCode::NOT_FOUND, "message not found").into_response(),
        Err(why) => return ApiError::from(why).into_response(),
    };
    // Only the user's own words go again as the user's.
    if message.origin != Origin::User || message.sender.is_some() || message.text.trim().is_empty()
    {
        return ApiError::new(StatusCode::CONFLICT, "not a message of the user's").into_response();
    }
    if let Some(why) = settled_otherwise(&member, &message.id).await {
        return ApiError::new(StatusCode::CONFLICT, why).into_response();
    }
    let now = app.clock.now_ms();
    if at_ms.is_some_and(|at| at > now + FURTHEST_MS) {
        return bad("at_ms is more than a week ahead");
    }
    if let Some(at_ms) = at_ms.filter(|at| *at > now + 1_000) {
        let planned = Planned {
            agent_id: member.record.agent_id.clone(),
            message_id: message.id.clone(),
            text: message.text.clone(),
            at_ms,
        };
        let key = planned_key(&planned.agent_id, &planned.message_id);
        let Ok(stored) = serde_json::to_vec(&planned) else {
            return ApiError::new(StatusCode::INTERNAL_SERVER_ERROR, "could not keep it")
                .into_response();
        };
        if let Err(why) = app.fleet.keep(KIND, key, Some(stored)).await {
            return ApiError::new(StatusCode::SERVICE_UNAVAILABLE, why.0).into_response();
        }
        return Json(json!({"ok": true, "status": "scheduled", "at_ms": at_ms})).into_response();
    }
    // Sent now: a try planned for later is no longer wanted.
    let _ = app
        .fleet
        .keep(KIND, planned_key(&member.record.agent_id, &message.id), None)
        .await;
    if compact {
        // The conversation is made shorter first; the message waits behind it.
        let asked = json!({"session": member.record.session}).to_string();
        let answer = crate::compact::compact(State(app.clone()), Bytes::from(asked)).await;
        if !answer.status().is_success() {
            return answer;
        }
    }
    match send_again(&app, &member.record.session, &message.id, &message.text).await {
        Ok(()) => Json(json!({
            "ok": true,
            "status": if compact { "compacting" } else { "queued" },
        }))
        .into_response(),
        Err(why) => why.into_response(),
    }
}

/// The user's words, sent to the agent again as a new message of theirs. It waits behind a
/// turn that runs (the compaction asked for just before, say).
async fn send_again(app: &App, session: &str, message_id: &str, text: &str) -> Result<(), ApiError> {
    let request = SendRequest {
        session: session.to_string(),
        text: text.to_string(),
        client_msg_id: Some(again_id(message_id)),
        queue_if_busy: Some(true),
        force_session: Some(true),
        synthesize_audio: Some(false),
        hands_free: Some(false),
        client: Some("turn-retry".to_string()),
        ..SendRequest::default()
    };
    crate::edge::send_again(app, request).await.map(|_| ())
}

/// Why a message is not to be sent again, if it is not: its turn still runs or waits, it has
/// an answer, or it went again already. `None`: its turn failed or was cut off.
async fn settled_otherwise(member: &crate::fleet::Member, message_id: &str) -> Option<&'static str> {
    match member.agent.fate(message_id.to_string()).await {
        Ok(clarp_actor::TurnFate::Runs | clarp_actor::TurnFate::Waits) => {
            return Some("its turn still runs");
        }
        Ok(clarp_actor::TurnFate::Ended { status, .. }) if status == "completed" => {
            return Some("it has an answer already");
        }
        _ => {}
    }
    let again = format!("u-{}", again_id(message_id));
    matches!(member.agent.row(again).await, Ok(Some(_))).then_some("it was sent again already")
}

/// The schedules' runner's turn: every try whose time has come is sent, and forgotten.
pub async fn run_due(app: &App) {
    let Ok(kept) = app.fleet.kept_all(KIND).await else {
        return;
    };
    let now = app.clock.now_ms();
    for (key, stored) in kept {
        let Ok(planned) = serde_json::from_slice::<Planned>(&stored) else {
            let _ = app.fleet.keep(KIND, key, None).await;
            continue;
        };
        if planned.at_ms > now {
            continue;
        }
        // Forgotten first: a try is sent at most once, even if the send then fails.
        if app.fleet.keep(KIND, key, None).await.is_err() {
            continue;
        }
        let directory = app.fleet.directory();
        let Some(member) = directory
            .members
            .iter()
            .find(|member| member.record.agent_id == planned.agent_id)
        else {
            continue;
        };
        // What came of it meanwhile decides: an answer, or a try sent with the button, and there
        // is nothing to send. The same words typed again by hand are not recognised.
        if let Some(why) = settled_otherwise(member, &planned.message_id).await {
            eprintln!(
                "clarp-host: the message {} of {} is not sent again at its time: {why}",
                planned.message_id, member.record.session
            );
            continue;
        }
        if let Err(why) = send_again(app, &member.record.session, &planned.message_id, &planned.text).await {
            eprintln!(
                "clarp-host: the message {} of {} was not sent again at its time: {}",
                planned.message_id,
                member.record.session,
                why.message()
            );
        }
    }
}

pub fn routes() -> Router<App> {
    Router::new().route("/turn-retry", post(retry))
}
