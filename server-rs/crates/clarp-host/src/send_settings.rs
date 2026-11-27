//! The owner's setting of what Send does while the agent works (feature `send_while_busy`,
//! issue 920): `GET /send-settings` says it, `POST /send-settings` changes it and tells every
//! app (`send-settings-updated`). It is kept among the Host's settings, so it is the same in
//! every app; each app sends it as `while_busy` with the owner's sends.

use axum::body::Bytes;
use axum::extract::State;
use axum::http::{HeaderMap, StatusCode};
use axum::{Extension, Json};
use clarp_kernel::while_busy::{KEY, NOT_A_VALUE, WhileBusy};
use serde_json::{Value, json};

use crate::edge::{ApiError, App, Principal};

/// The kind of the event every app is told a change by.
pub(crate) const CHANGED: &str = "send-settings-updated";

fn kept(app: &App) -> WhileBusy {
    WhileBusy::stored(app.fleet.directory().setting(KEY))
}

fn told(while_busy: WhileBusy) -> Value {
    json!({ "while_busy": while_busy.name() })
}

/// `GET /send-settings`: `{"while_busy": "steer" | "after_turn" | "take_over"}`, `steer` where
/// the owner chose nothing.
pub(crate) async fn get(State(app): State<App>) -> Json<Value> {
    Json(told(kept(&app)))
}

/// `POST /send-settings` with `{"while_busy": ...}`: only the owner changes it, with his token or
/// a full device. Every app is told of a change.
pub(crate) async fn set(
    State(app): State<App>,
    Extension(principal): Extension<Principal>,
    headers: HeaderMap,
    body: Bytes,
) -> Result<Json<Value>, ApiError> {
    if principal.is_agent_request(&headers) {
        return Err(ApiError::new(StatusCode::FORBIDDEN, "agents_may_not"));
    }
    let asked = serde_json::from_slice::<Value>(&body).unwrap_or(Value::Null);
    if !asked.is_object() {
        return Err(ApiError::new(StatusCode::BAD_REQUEST, "bad json"));
    }
    let wanted = asked["while_busy"]
        .as_str()
        .and_then(WhileBusy::named)
        .ok_or_else(|| ApiError::new(StatusCode::BAD_REQUEST, NOT_A_VALUE))?;
    app.fleet
        .set(vec![(KEY.to_owned(), wanted.name().to_owned())])
        .await?;
    Ok(Json(told(wanted)))
}
