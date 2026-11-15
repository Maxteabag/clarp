//! The devices a Host tells of finished turns, as the Host serves them.
//!
//! The rules are in [`clarp_kernel::devices`], and the register is the fleet's to keep
//! ([`crate::fleet`]). Here are the routes, answered as the Python Host's are
//! (`server/lib/routes/ops.py`): a device registers the token its push service gave it,
//! takes it back, and reads what the Host has of it.
//!
//! A token is bound to the paired device that registers it, by the token the request was
//! made with: never by a field of the body.

use axum::extract::{Extension, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use clarp_kernel::devices::Refused;
use clarp_kernel::presence::LEASE_MS;
use serde_json::{Map, Value, json};

use crate::edge::{ApiError, App, Principal};
use crate::fleet::PushRegistration;
use crate::pairing::Scope;

/// A refusal the apps read by its code.
fn refused(status: StatusCode, code: &str) -> Response {
    (status, Json(json!({ "ok": false, "error": code }))).into_response()
}

fn forbidden() -> ApiError {
    ApiError::new(StatusCode::FORBIDDEN, "full device access required")
}

/// The body of a request as a JSON object. No body at all is an empty one.
fn object(body: &[u8]) -> Result<Map<String, Value>, ApiError> {
    if body.is_empty() {
        return Ok(Map::new());
    }
    match serde_json::from_slice(body) {
        Ok(Value::Object(fields)) => Ok(fields),
        _ => Err(ApiError::new(StatusCode::BAD_REQUEST, "bad json")),
    }
}

/// A field that is a text, trimmed. Anything else is no text.
fn text(data: &Map<String, Value>, key: &str) -> String {
    data.get(key)
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim()
        .to_string()
}

/// `POST /devices`: a device registers its push token, with the agent it has open, the side
/// of the push service the token belongs to, the address it reaches this Host at, and, when
/// it has one, the phone's own leave to push to it through the relay.
async fn register(
    State(app): State<App>,
    Extension(principal): Extension<Principal>,
    body: axum::body::Bytes,
) -> Result<Response, ApiError> {
    let data = object(&body)?;
    let token = text(&data, "token");
    if token.is_empty() {
        return Err(ApiError::new(StatusCode::BAD_REQUEST, "token required"));
    }
    // Only a paired device with full scope owns what it registers.
    let owner = match &principal {
        Principal::Device {
            device_id,
            scope: Scope::Full,
        } => device_id.clone(),
        _ => String::new(),
    };
    let registration = PushRegistration {
        token,
        session: text(&data, "session"),
        environment: text(&data, "environment"),
        platform: text(&data, "platform"),
        base_url: text(&data, "base_url"),
        push_grant: text(&data, "push_grant"),
        owner,
        provider: text(&data, "provider"),
    };
    Ok(match app.fleet.push_register(registration).await? {
        Ok(()) => Json(json!({ "ok": true })).into_response(),
        Err(Refused::OwnedElsewhere) => {
            refused(StatusCode::CONFLICT, "device_token_owned_elsewhere")
        }
        Err(Refused::Invalid(_)) => refused(StatusCode::BAD_REQUEST, "device_token_invalid"),
        Err(Refused::Value(_)) => {
            return Err(ApiError::new(
                StatusCode::INTERNAL_SERVER_ERROR,
                "register failed",
            ));
        }
    })
}

/// `DELETE /devices`: stops pushes to a token. A paired device names its own token and
/// stops only its own rows. The administrator names one token, the beginning of one, or a
/// device, whose every row then stops.
async fn unregister(
    State(app): State<App>,
    Extension(principal): Extension<Principal>,
    body: axum::body::Bytes,
) -> Result<Response, ApiError> {
    let data = object(&body)?;
    let (token, prefix, device) = (
        text(&data, "token"),
        text(&data, "token_prefix"),
        text(&data, "device_id"),
    );
    let stopped = match &principal {
        Principal::Device {
            scope: Scope::Limited,
            ..
        } => return Err(forbidden()),
        Principal::Device { device_id, .. } => {
            if token.is_empty() || !prefix.is_empty() || !device.is_empty() {
                return Ok(refused(StatusCode::BAD_REQUEST, "token_required"));
            }
            app.fleet
                .push_disable(token, String::new(), Some(device_id.clone()))
                .await?
        }
        Principal::Administrator => {
            let named = [&token, &prefix, &device]
                .iter()
                .filter(|given| !given.is_empty())
                .count();
            if named != 1 {
                return Ok(refused(
                    StatusCode::BAD_REQUEST,
                    "one_of_token_token_prefix_device_id",
                ));
            }
            let token = if prefix.is_empty() {
                token
            } else {
                if prefix.chars().count() < 12 {
                    return Ok(refused(StatusCode::BAD_REQUEST, "token_prefix_too_short"));
                }
                let directory = app.fleet.directory();
                let begun: Vec<&str> = directory
                    .push
                    .active()
                    .map(|row| row.token.as_str())
                    .filter(|token| token.starts_with(&prefix))
                    .collect();
                match begun.as_slice() {
                    [] => return Ok(refused(StatusCode::NOT_FOUND, "device_token_not_found")),
                    [one] => (*one).to_string(),
                    _ => return Ok(refused(StatusCode::CONFLICT, "token_prefix_ambiguous")),
                }
            };
            app.fleet.push_disable(token, device, None).await?
        }
    };
    Ok(match stopped {
        Ok(count) if count > 0 => Json(json!({ "ok": true, "disabled": count })).into_response(),
        _ => refused(StatusCode::NOT_FOUND, "device_token_not_found"),
    })
}

/// `GET /devices/push-tokens`: the rows this Host holds, without the whole token. The
/// administrator sees every row, a paired device with full scope its own.
async fn tokens(
    State(app): State<App>,
    Extension(principal): Extension<Principal>,
) -> Result<Json<Value>, ApiError> {
    let directory = app.fleet.directory();
    let can_receive = |owner: &str| directory.can_receive(owner);
    let rows = match &principal {
        Principal::Administrator => directory.push.shown(None, &can_receive),
        Principal::Device {
            device_id,
            scope: Scope::Full,
        } => directory.push.shown(Some(device_id.as_str()), &can_receive),
        Principal::Device { .. } => return Err(forbidden()),
    };
    Ok(Json(json!({ "ok": true, "tokens": rows })))
}

/// `POST /desktop-presence`: a desktop app says whether it is in front of its user, every
/// few seconds, with a lease that runs out by itself. While one runs, no phone is paged.
async fn desktop_presence(
    State(app): State<App>,
    Extension(principal): Extension<Principal>,
    body: axum::body::Bytes,
) -> Result<Json<Value>, ApiError> {
    // A Host that asks nobody for a token cannot know whose desk reports.
    if app.config.auth_token.is_empty() {
        return Err(ApiError::new(
            StatusCode::UNAUTHORIZED,
            "authenticated desktop required",
        ));
    }
    let reporter = match &principal {
        Principal::Administrator => "administrator",
        Principal::Device {
            device_id,
            scope: Scope::Full,
        } => device_id.as_str(),
        Principal::Device { .. } => return Err(forbidden()),
    };
    let data = if body.is_empty() {
        Map::new()
    } else {
        match serde_json::from_slice(&body) {
            Ok(Value::Object(fields)) => fields,
            _ => return Err(ApiError::new(StatusCode::BAD_REQUEST, "object required")),
        }
    };
    let field = |key: &str| data.get(key).cloned().unwrap_or(Value::Null);
    let accepted = app
        .desks
        .lock()
        .map_err(|_| ApiError::new(StatusCode::INTERNAL_SERVER_ERROR, "presence is not kept"))?
        .update(
            reporter,
            &field("instance_id"),
            &field("sequence"),
            &field("active"),
            &field("sent_at_ms"),
            app.clock.now_ms(),
        )
        .map_err(|why| ApiError::new(StatusCode::BAD_REQUEST, why))?;
    Ok(Json(json!({ "accepted": accepted, "lease_ms": LEASE_MS })))
}

pub fn routes() -> Router<App> {
    Router::new()
        .route("/devices", post(register).delete(unregister))
        .route("/devices/push-tokens", get(tokens))
        .route("/desktop-presence", post(desktop_presence))
}
