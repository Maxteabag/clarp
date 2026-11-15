//! Other Hosts this one may write to.
//!
//! An agent hands work to another agent with `clarp-admin prompt`. The other agent may live
//! on another Host: the Python Host beside this one while agents move over one by one, or a
//! Host on another machine. The owner names such a Host a peer, with its address and a token
//! it takes; an agent then writes to `(peer, session)`. Hosts share nothing else: no files,
//! no database.
//!
//! This is the Python Host's scheme (`server/lib/server_peers.py`): the message is the other
//! Host's own `POST /send`, with the peer's token and the name it goes by here.

use std::time::Duration;

use axum::extract::{Extension, Path, State};
use axum::http::StatusCode;
use axum::routing::{delete, get};
use axum::{Json, Router};
use serde_json::{Map, Value, json};

use crate::edge::{ApiError, App, Principal};

/// Why a message did not reach a peer.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Failed {
    /// The owner named no such Host, or switched it off.
    Unknown(String),
    /// The other Host refused the message, and said why.
    Refused { status: u16, detail: String },
    /// The other Host did not answer, or not in a way to be read.
    Down(String),
}

/// A peer as it may be shown: never its token.
fn public(name: &str, url: &str, enabled: bool) -> Value {
    json!({ "name": name, "url": url, "enabled": enabled })
}

/// Peers are the owner's to name, from this machine.
fn owner_only(principal: &Principal) -> Result<(), ApiError> {
    match principal {
        Principal::Administrator => Ok(()),
        Principal::Device { .. } => Err(ApiError::new(
            StatusCode::FORBIDDEN,
            "peers are named from the Host's own machine",
        )),
    }
}

/// `GET /peers`: the Hosts this one may write to.
async fn list(
    State(app): State<App>,
    Extension(principal): Extension<Principal>,
) -> Result<Json<Value>, ApiError> {
    owner_only(&principal)?;
    let directory = app.fleet.directory();
    let peers: Vec<Value> = directory
        .peers
        .iter()
        .map(|(name, peer)| public(name, &peer.url, peer.enabled))
        .collect();
    Ok(Json(json!(peers)))
}

/// `POST /peers` with `{name, url, token}`: names a Host, or says anew where one is.
async fn add(
    State(app): State<App>,
    Extension(principal): Extension<Principal>,
    Json(asked): Json<Map<String, Value>>,
) -> Result<Json<Value>, ApiError> {
    owner_only(&principal)?;
    let text = |key: &str| {
        asked
            .get(key)
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim()
            .to_string()
    };
    let (name, url) = (text("name"), text("url"));
    let url = url.trim_end_matches('/').to_string();
    match app
        .fleet
        .peer_add(name.clone(), url.clone(), text("token"))
        .await?
    {
        Ok(()) => Ok(Json(public(&name, &url, true))),
        Err(why) => Err(ApiError::new(StatusCode::BAD_REQUEST, why)),
    }
}

/// `DELETE /peers/<name>`: forgets a Host.
async fn remove(
    State(app): State<App>,
    Extension(principal): Extension<Principal>,
    Path(name): Path<String>,
) -> Result<Json<Value>, ApiError> {
    owner_only(&principal)?;
    if app.fleet.peer_remove(name.clone()).await? {
        Ok(Json(json!({ "ok": true, "removed": name })))
    } else {
        Err(ApiError::new(StatusCode::NOT_FOUND, "no such peer"))
    }
}

/// Hands a message to the Host the owner named `name`: that Host's own `POST /send`.
/// Answers what the other Host answered.
pub(crate) async fn send(app: &App, name: &str, payload: Value) -> Result<Value, Failed> {
    let directory = app.fleet.directory();
    let Some(peer) = directory
        .peers
        .get(name)
        .filter(|peer| peer.enabled)
        .cloned()
    else {
        return Err(Failed::Unknown(name.to_string()));
    };
    let name = name.to_string();
    // The other Host is asked on a thread that may wait: nothing else waits with it.
    let asked = tokio::task::spawn_blocking(move || {
        let config = ureq::Agent::config_builder()
            .timeout_global(Some(Duration::from_secs(30)))
            .http_status_as_error(false)
            .build();
        let agent: ureq::Agent = config.into();
        let mut response = agent
            .post(format!("{}/send", peer.url))
            .header("Authorization", format!("Bearer {}", peer.token))
            .header("X-Clarp-Peer", name.as_str())
            .send_json(&payload)
            // Only that it failed: never what the request held.
            .map_err(|_| Failed::Down(format!("the Host named {name} did not answer")))?;
        let status = response.status().as_u16();
        let said = response.body_mut().read_to_string().unwrap_or_default();
        let answer: Value = serde_json::from_str(&said).unwrap_or(Value::Null);
        if (200..300).contains(&status) {
            return Ok(answer);
        }
        // A Host says why in JSON, or, for a send, in plain words.
        let detail = match answer["error"].as_str() {
            Some(why) => why.to_string(),
            None => said.trim().chars().take(300).collect(),
        };
        if (400..500).contains(&status) {
            Err(Failed::Refused { status, detail })
        } else {
            Err(Failed::Down(format!(
                "the Host named {name} answered {status}"
            )))
        }
    })
    .await;
    asked.unwrap_or_else(|_| Err(Failed::Down("the message could not be sent".to_string())))
}

pub fn routes() -> Router<App> {
    Router::new()
        .route("/peers", get(list).post(add))
        .route("/peers/{name}", delete(remove))
}
