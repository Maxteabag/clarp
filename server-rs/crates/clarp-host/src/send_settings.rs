//! The owner's setting of what Send does while the agent works (feature `send_while_busy`,
//! issue 920): `GET /send-settings` says it, `POST /send-settings` changes it and tells every
//! app (`send-settings-updated`). It is kept among the Host's settings, so it is the same in
//! every app; each app sends it as `while_busy` with the owner's sends.

use axum::Json;
use axum::body::Bytes;
use axum::extract::State;
use axum::http::{HeaderMap, StatusCode};
use clarp_kernel::while_busy::{KEY, NOT_A_VALUE, WhileBusy};
use serde_json::{Value, json};

use crate::edge::{ApiError, App};

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
/// a full device; a limited device and an agent's tools are kept out by their scope, and a
/// request that names a turn or a peer Host is refused here, as the owner's other settings
/// refuse it. Every app is told of a change.
pub(crate) async fn set(
    State(app): State<App>,
    headers: HeaderMap,
    body: Bytes,
) -> Result<Json<Value>, ApiError> {
    if crate::coordination::from_an_agent(&headers) {
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
    let before = kept(&app);
    // Kept once it is chosen, also where it is the default, so the owner's choice holds.
    if app.fleet.directory().setting(KEY) != Some(wanted.name()) {
        app.fleet
            .set(vec![(KEY.to_owned(), wanted.name().to_owned())])
            .await?;
    }
    if wanted != before {
        app.hub.publish(
            json!({ "type": CHANGED, "while_busy": wanted.name() }),
            app.clock.now_ms(),
        );
    }
    Ok(Json(told(wanted)))
}
