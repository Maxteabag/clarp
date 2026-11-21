//! The administrator's move mark. The agent's own actor serializes it with admission.

use axum::{
    Extension, Json,
    body::Bytes,
    extract::State,
    http::{HeaderMap, StatusCode},
};
use clarp_kernel::{agent_moves::Target, python};
use serde_json::{Value, json};

use crate::edge::{ApiError, App, Principal};
use crate::pairing::Scope;

pub(crate) async fn set(
    State(app): State<App>,
    Extension(principal): Extension<Principal>,
    headers: HeaderMap,
    body: Bytes,
) -> Result<Json<Value>, ApiError> {
    if matches!(principal, Principal::Tools) || crate::coordination::from_an_agent(&headers) {
        return Err(ApiError::new(StatusCode::FORBIDDEN, "agents_may_not"));
    }
    if matches!(
        principal,
        Principal::Device {
            scope: Scope::Limited,
            ..
        }
    ) {
        return Err(ApiError::new(
            StatusCode::FORBIDDEN,
            "full device access required",
        ));
    }
    let bad = |why| ApiError::new(StatusCode::BAD_REQUEST, why);
    let Value::Object(fields) = serde_json::from_slice(&body).map_err(|_| bad("bad json"))? else {
        return Err(bad("bad json"));
    };
    let session = crate::edge::body_text(&fields, "session")?;
    let Some(target) = fields.get("moved_to").filter(|_| !session.is_empty()) else {
        return Err(bad("session and moved_to required"));
    };
    let member = app
        .anyone(session)
        .ok_or_else(|| ApiError::new(StatusCode::NOT_FOUND, "no such agent"))?;
    let to = match target {
        Value::Null => None,
        Value::Object(fields) => {
            let read = |key: &str| {
                let name = format!("moved_to.{key}");
                let text = fields
                    .get(key)
                    .filter(|value| python::truthy(value))
                    .map(|value| {
                        value
                            .as_str()
                            .map_or_else(|| python::repr(value), str::to_owned)
                    })
                    .unwrap_or_default();
                let text = python::strip(&text);
                if text.is_empty() {
                    Err(ApiError::new(
                        StatusCode::BAD_REQUEST,
                        format!("{name} required"),
                    ))
                } else if text.chars().count() > 200 {
                    Err(ApiError::new(
                        StatusCode::BAD_REQUEST,
                        format!("{name} too long"),
                    ))
                } else {
                    Ok(text.to_owned())
                }
            };
            Some(Target {
                host: read("host")?,
                id: read("id")?,
            })
        }
        _ => return Err(bad("moved_to must be {host, id} or null")),
    };
    let receipt = member.agent.set_move(to).await?;
    Ok(Json(json!({
        "ok": true, "session": member.record.session, "changed": receipt.changed, "moved": receipt.moved,
    })))
}
