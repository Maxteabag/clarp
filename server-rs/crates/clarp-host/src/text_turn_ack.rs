//! Optional Host-managed instructions for new owner-authored text turns.

use axum::body::Bytes;
use axum::extract::State;
use axum::http::{HeaderMap, StatusCode};
use axum::{Extension, Json};
use clarp_protocol::requests::SendRequest;
use serde_json::{Value, json};

use crate::edge::{ApiError, App, Principal};
use crate::fleet::Directory;

const KEY: &str = "text_turn_ack.enabled";

pub(crate) fn enabled(directory: &Directory) -> bool {
    directory.setting(KEY) == Some("true")
}

/// The source of the user's words, before voice routing or reply playback is chosen.
/// Older clients can send dictated words without any marker.
pub(crate) fn is_text(request: &SendRequest) -> bool {
    let absent =
        |value: Option<&str>| value.is_none_or(|s| clarp_kernel::python::strip(s).is_empty());
    request.input_kind.as_deref() == Some("text")
        && request.hands_free != Some(true)
        && request.orchestrator_fallback != Some(true)
        && absent(request.call_id.as_deref())
        && request.call_opening.is_none()
        && absent(request.utterance_id.as_deref())
        && absent(request.transcription_id.as_deref())
}

fn owner_request(principal: &Principal, headers: &HeaderMap) -> Result<(), ApiError> {
    // The edge keeps limited devices out of both methods.
    if principal.is_agent_request(headers) {
        return Err(ApiError::new(StatusCode::FORBIDDEN, "agents_may_not"));
    }
    Ok(())
}

pub(crate) async fn get(
    State(app): State<App>,
    Extension(principal): Extension<Principal>,
    headers: HeaderMap,
) -> Result<Json<Value>, ApiError> {
    owner_request(&principal, &headers)?;
    Ok(Json(json!({"enabled": enabled(&app.fleet.directory())})))
}

pub(crate) async fn set(
    State(app): State<App>,
    Extension(principal): Extension<Principal>,
    headers: HeaderMap,
    body: Bytes,
) -> Result<Json<Value>, ApiError> {
    owner_request(&principal, &headers)?;
    let value = serde_json::from_slice::<Value>(&body).unwrap_or(Value::Null);
    let on = value["enabled"]
        .as_bool()
        .ok_or_else(|| ApiError::new(StatusCode::BAD_REQUEST, "enabled must be true or false"))?;
    app.fleet
        .set(vec![(KEY.to_owned(), on.to_string())])
        .await?;
    Ok(Json(json!({"enabled": on})))
}
