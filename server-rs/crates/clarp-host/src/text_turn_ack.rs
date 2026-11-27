//! Optional Host-managed instructions for new owner-authored text turns.

use axum::Json;
use axum::body::Bytes;
use axum::extract::State;
use axum::http::{HeaderMap, StatusCode};
use clarp_protocol::requests::SendRequest;
use serde_json::{Value, json};

use crate::edge::{ApiError, App};
use crate::fleet::Directory;

const KEY: &str = "text_turn_ack.enabled";

pub(crate) fn enabled(directory: &Directory) -> bool {
    directory.setting(KEY) == Some("true")
}

/// Recognize the chat send before voice routing, TTS availability or call admission can
/// turn a spoken request into one whose answer happens to be text.
pub(crate) fn is_text(request: &SendRequest) -> bool {
    let absent =
        |value: Option<&str>| value.is_none_or(|s| clarp_kernel::python::strip(s).is_empty());
    request.synthesize_audio != Some(true)
        && request.hands_free != Some(true)
        && request.orchestrator_fallback != Some(true)
        && absent(request.call_id.as_deref())
        && request.call_opening.is_none()
        && absent(request.utterance_id.as_deref())
        && absent(request.transcription_id.as_deref())
}

fn owner_request(headers: &HeaderMap) -> Result<(), ApiError> {
    // The edge's scope already keeps limited devices and agent tools out of both methods.
    if crate::coordination::from_an_agent(headers) {
        return Err(ApiError::new(StatusCode::FORBIDDEN, "agents_may_not"));
    }
    Ok(())
}

pub(crate) async fn get(
    State(app): State<App>,
    headers: HeaderMap,
) -> Result<Json<Value>, ApiError> {
    owner_request(&headers)?;
    Ok(Json(json!({"enabled": enabled(&app.fleet.directory())})))
}

pub(crate) async fn set(
    State(app): State<App>,
    headers: HeaderMap,
    body: Bytes,
) -> Result<Json<Value>, ApiError> {
    owner_request(&headers)?;
    let value = serde_json::from_slice::<Value>(&body).unwrap_or(Value::Null);
    let on = value["enabled"]
        .as_bool()
        .ok_or_else(|| ApiError::new(StatusCode::BAD_REQUEST, "enabled must be true or false"))?;
    app.fleet
        .set(vec![(KEY.to_owned(), on.to_string())])
        .await?;
    Ok(Json(json!({"enabled": on})))
}
