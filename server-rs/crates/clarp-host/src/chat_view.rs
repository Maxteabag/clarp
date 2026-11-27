//! The user's view preference for one chat.

use axum::body::Bytes;
use axum::extract::State;
use axum::http::{HeaderMap, StatusCode};
use axum::{Extension, Json};
use clarp_kernel::chat_view::{View, setting_key};
use clarp_kernel::python;
use serde_json::{Value, json};

use crate::edge::{ApiError, App, Principal};
use crate::fleet::Directory;

pub(crate) fn preference(directory: &Directory, agent: &str) -> View {
    directory
        .setting(&setting_key(agent))
        .and_then(View::parse)
        .unwrap_or_default()
}

pub(crate) async fn set(
    State(app): State<App>,
    Extension(principal): Extension<Principal>,
    headers: HeaderMap,
    body: Bytes,
) -> Result<Json<Value>, ApiError> {
    if principal.is_agent_request(&headers) {
        return Err(ApiError::new(StatusCode::FORBIDDEN, "agents_may_not"));
    }
    let bad = |words: &str| ApiError::new(StatusCode::BAD_REQUEST, words);
    let asked = serde_json::from_slice::<Value>(&body)
        .ok()
        .filter(Value::is_object)
        .ok_or_else(|| bad("bad json"))?;
    let raw = &asked["session"];
    let session = raw.as_str().map(str::to_owned).unwrap_or_else(|| {
        if python::truthy(raw) {
            python::repr(raw)
        } else {
            String::new()
        }
    });
    let session = python::strip(&session);
    let raw = asked
        .get("chat_view")
        .filter(|_| !session.is_empty())
        .ok_or_else(|| bad("session and chat_view required"))?;
    let missing = || ApiError::new(StatusCode::NOT_FOUND, "no such agent");
    let member = app.anyone(session).ok_or_else(missing)?;
    let view = raw
        .as_str()
        .and_then(View::parse)
        .ok_or_else(|| bad("chat_view must be one of: conversation, session"))?;
    if !app
        .fleet
        .set_chat_view(member.record.agent_id.clone(), session.to_owned(), view)
        .await?
    {
        return Err(missing());
    }
    Ok(Json(
        json!({"ok": true, "session": session, "chat_view": view}),
    ))
}
