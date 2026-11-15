//! What an app asks of any Host, whatever the Host says it has.
//!
//! An app gates few of its calls on what a Host lists (`capabilities.features`): the rest it
//! makes of every Host it is paired with. Counted on the Python Host's own log over three
//! days, three in ten of all requests were to routes this Host did not have, and most of
//! them were these: which agent the user is looking at, whether the app is in front, and
//! the lists an app's screens are made of. They are answered here: with what is so, where
//! this Host keeps it, and with a list that is empty where it keeps nothing of the kind.

use axum::extract::{Extension, State};
use axum::http::StatusCode;
use axum::routing::post;
use axum::{Json, Router};
use serde_json::{Value, json};

use crate::edge::{ApiError, App, Principal};
use crate::pairing::Scope;

fn bad(message: &str) -> ApiError {
    ApiError::new(StatusCode::BAD_REQUEST, message)
}

/// `POST /focus` with `{session}`: the app says which agent its user is looking at. An agent
/// nobody has is nothing to look at, and no error either.
async fn focus(State(app): State<App>, body: axum::body::Bytes) -> Result<Json<Value>, ApiError> {
    let Ok(Value::Object(said)) = serde_json::from_slice::<Value>(&body) else {
        return Err(bad("bad json"));
    };
    let session = said.get("session").and_then(Value::as_str).unwrap_or("");
    let session = session.trim();
    if session.is_empty() {
        return Err(bad("session required"));
    }
    app.fleet.select(session.to_string()).await?;
    Ok(Json(json!({ "ok": true })))
}

/// `POST /application-activity`: an app reports whether it is in front and when its user
/// last touched it. Only for whoever may speak for a person: the administrator, or a device
/// with full scope.
async fn activity(
    State(app): State<App>,
    Extension(principal): Extension<Principal>,
    body: axum::body::Bytes,
) -> Result<Json<Value>, ApiError> {
    if app.config.auth_token.is_empty() {
        return Err(ApiError::new(
            StatusCode::UNAUTHORIZED,
            "authentication required",
        ));
    }
    let owner = match &principal {
        Principal::Administrator => "administrator".to_string(),
        Principal::Device {
            device_id,
            scope: Scope::Full,
        } => device_id.clone(),
        _ => {
            return Err(ApiError::new(
                StatusCode::FORBIDDEN,
                "full device access required",
            ));
        }
    };
    let Ok(report @ Value::Object(_)) = serde_json::from_slice::<Value>(&body) else {
        return Err(bad("object required"));
    };
    let now = app.clock.now_ms();
    let taken = match app.activity.lock() {
        Ok(mut leases) => leases.report(&owner, &report, now),
        Err(_) => Err("activity is not kept right now".to_string()),
    };
    match taken {
        Ok(accepted) => Ok(Json(json!({ "accepted": accepted }))),
        Err(why) => Err(bad(&why)),
    }
}

pub fn routes() -> Router<App> {
    Router::new()
        .route("/focus", post(focus))
        .route("/application-activity", post(activity))
}
