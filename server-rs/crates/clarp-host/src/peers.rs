//! Other Hosts this one is paired with. A peer's token is for requests to its stored URL.

mod registry;
mod transport;

use std::collections::BTreeMap;
use std::path::PathBuf;
use std::time::{Duration, Instant};

use axum::extract::{Extension, Path, State};
use axum::http::StatusCode;
use axum::routing::{delete, get};
use axum::{Json, Router};
use serde_json::{Map, Value, json};

use crate::edge::{ApiError, App, Principal};
use crate::fleet::Peer;
use crate::pairing::Scope;

use transport::{contains_token, direct_agent, fetch};
pub(crate) use transport::{normalize_url, valid_token};

const READ_SECONDS: u64 = 15;
const READ_MAX_BYTES: usize = 16 * 1024 * 1024;
const PAIRING_MAX_BYTES: usize = 4 * 1024 * 1024;
const CACHE_SECONDS: u64 = 10;

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

/// The file of this Host's peers, as it was started (`--peers`). `None`: it has no peers, and
/// reads no file for them: a Host started by hand for a test with no file named never reaches
/// the installed Host's peers (issue 741).
fn registry_path(app: &App) -> Option<PathBuf> {
    app.config.peers.clone()
}

/// Why a peer cannot be named or forgotten on a Host that keeps none.
fn none_kept() -> ApiError {
    ApiError::new(
        StatusCode::CONFLICT,
        "this Host was started without --peers: it keeps no peers",
    )
}

fn public(name: &str, peer: &Peer) -> Value {
    json!({
        "name": name, "url": peer.url, "enabled": peer.enabled, "server_id": peer.server_id,
    })
}

fn parse(value: Value) -> Option<Peer> {
    let mut peer = serde_json::from_value::<Peer>(value).ok()?;
    peer.url = normalize_url(&peer.url)?.to_owned();
    peer.token = peer.token.trim().to_owned();
    valid_token(&peer.token).then_some(peer)
}

fn registry_failed() -> ApiError {
    ApiError::new(
        StatusCode::INTERNAL_SERVER_ERROR,
        "the peer registry could not be read or written",
    )
}

/// Peers are the owner's to name, from this machine.
fn owner_only(principal: &Principal) -> Result<(), ApiError> {
    match principal {
        Principal::Administrator => Ok(()),
        Principal::Device { .. } | Principal::Tools => Err(ApiError::new(
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
    Ok(Json(json!(list_public(&app).await?)))
}

/// What an agent can read about its Host's peers, through the same projection as the owner.
pub(crate) async fn list_public(app: &App) -> Result<Vec<Value>, ApiError> {
    let Some(path) = registry_path(app) else {
        return Ok(Vec::new());
    };
    let legacy = app.fleet.directory().legacy_peers.clone();
    let peers = tokio::task::spawn_blocking(move || registry::read(&path, &legacy))
        .await
        .map_err(|_| registry_failed())?
        .map_err(|_| registry_failed())?;
    Ok(peers
        .into_iter()
        .filter_map(|(name, value)| parse(value).map(|peer| public(&name, &peer)))
        .collect())
}

/// `POST /peers`: names a Host, or says anew where one is.
async fn add(
    State(app): State<App>,
    Extension(principal): Extension<Principal>,
    Json(asked): Json<Map<String, Value>>,
) -> Result<Json<Value>, ApiError> {
    owner_only(&principal)?;
    let text = |key: &str| asked.get(key).and_then(Value::as_str).unwrap_or("").trim();
    let (name, token) = (text("name"), text("token"));
    let invalid = || {
        ApiError::new(
            StatusCode::BAD_REQUEST,
            "peer requires name, HTTP(S) URL, and token",
        )
    };
    let url = normalize_url(text("url")).ok_or_else(invalid)?;
    if name.is_empty() || !valid_token(token) {
        return Err(invalid());
    }
    let peer = Peer {
        url: url.to_string(),
        token: token.to_string(),
        enabled: true,
        server_id: text("server_id").to_string(),
    };
    let shown = public(name, &peer);
    let stored = serde_json::to_value(peer).map_err(|_| registry_failed())?;
    let name = name.to_string();
    let path = registry_path(&app).ok_or_else(none_kept)?;
    let legacy = app.fleet.directory().legacy_peers.clone();
    tokio::task::spawn_blocking(move || {
        registry::edit(&path, &legacy, |peers| peers.insert(name, stored))
    })
    .await
    .map_err(|_| registry_failed())?
    .map_err(|_| registry_failed())?;
    Ok(Json(shown))
}

/// `DELETE /peers/<name>`: forgets a Host.
async fn remove(
    State(app): State<App>,
    Extension(principal): Extension<Principal>,
    Path(name): Path<String>,
) -> Result<Json<Value>, ApiError> {
    owner_only(&principal)?;
    let path = registry_path(&app).ok_or_else(none_kept)?;
    let legacy = app.fleet.directory().legacy_peers.clone();
    let removed = name.clone();
    let known = tokio::task::spawn_blocking(move || {
        registry::edit(&path, &legacy, |peers| peers.remove(&removed).is_some())
    })
    .await
    .map_err(|_| registry_failed())?
    .map_err(|_| registry_failed())?;
    if known {
        Ok(Json(json!({ "ok": true, "removed": name })))
    } else {
        Err(ApiError::new(StatusCode::NOT_FOUND, "no such peer"))
    }
}

#[derive(Default)]
pub(crate) struct SnapshotCache {
    entry: Option<(Instant, Value)>,
    // Remember an ID only for the same registry entry, including the destination and token.
    learned: BTreeMap<String, (Peer, String)>,
}

fn read_peer(name: &str, peer: &Peer, known_id: &str) -> (String, Value) {
    let mut entry = json!({
        "name": name, "url": peer.url, "server_id": known_id, "reachable": false,
        "error": null, "snapshot": null,
    });
    let result = (|| {
        let info = fetch(
            &peer.url,
            "/server-info",
            Some(&peer.token),
            READ_SECONDS,
            READ_MAX_BYTES,
            None,
        )?;
        let snapshot = fetch(
            &peer.url,
            "/agents/snapshot?include_archived=0",
            Some(&peer.token),
            READ_SECONDS,
            READ_MAX_BYTES,
            None,
        )?;
        Ok::<_, String>((info, snapshot))
    })();
    let mut server_id = known_id.to_string();
    match result {
        Ok((info, snapshot)) => {
            server_id = reported(&info, "server_id").unwrap_or(server_id);
            entry["server_id"] = json!(server_id);
            entry["name"] = json!(reported(&info, "name").as_deref().unwrap_or(name));
            entry["reachable"] = json!(true);
            entry["snapshot"] = Value::Object(snapshot);
        }
        Err(code) => entry["error"] = json!(code),
    }
    let key = if server_id.is_empty() {
        format!("peer:{name}")
    } else {
        server_id
    };
    (key, entry)
}

/// The name and ID are strings on a Clarp Host. A missing or empty one uses the stored value.
pub(crate) fn reported(reply: &Map<String, Value>, key: &str) -> Option<String> {
    reply
        .get(key)
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
        .map(str::to_string)
}

impl SnapshotCache {
    fn refresh(
        &mut self,
        path: Option<&std::path::Path>,
        legacy: &BTreeMap<String, Peer>,
    ) -> Result<Value, ApiError> {
        let registry = match path {
            Some(path) => registry::read(path, legacy).map_err(|_| registry_failed())?,
            None => Map::new(),
        };
        let peers: BTreeMap<_, _> = registry
            .into_iter()
            .filter_map(|(name, value)| {
                parse(value)
                    .filter(|peer| peer.enabled)
                    .map(|peer| (name, peer))
            })
            .collect();
        self.learned
            .retain(|name, (prior, _)| peers.get(name) == Some(prior));
        let known: Vec<_> = peers
            .iter()
            .map(|(name, peer)| {
                let id = if peer.server_id.is_empty() {
                    self.learned.get(name).map_or("", |(_, id)| id.as_str())
                } else {
                    peer.server_id.as_str()
                };
                (name, peer, id)
            })
            .collect();
        // Bound simultaneous requests even when the owner has a long registry.
        let mut results = Vec::new();
        for group in known.chunks(16) {
            std::thread::scope(|scope| {
                let jobs: Vec<_> = group
                    .iter()
                    .map(|&(name, peer, id)| {
                        let job = std::thread::Builder::new()
                            .spawn_scoped(scope, move || read_peer(name, peer, id));
                        (name, peer, job)
                    })
                    .collect();
                for (name, peer, job) in jobs {
                    let (key, entry) = job
                        .ok()
                        .and_then(|job| job.join().ok())
                        .ok_or_else(registry_failed)?;
                    results.push((name, peer, key, entry));
                }
                Ok::<_, ApiError>(())
            })?;
        }
        let mut entries = Map::new();
        for (name, peer, key, entry) in results {
            if entry["reachable"] == true
                && let Some(id) = entry["server_id"].as_str().filter(|id| !id.is_empty())
            {
                self.learned
                    .insert(name.clone(), (peer.clone(), id.to_string()));
            }
            entries.insert(key, entry);
        }
        let result = json!({"peers": entries, "cache_seconds": CACHE_SECONDS});
        self.entry = Some((Instant::now(), result.clone()));
        Ok(result)
    }
}

async fn snapshot(
    State(app): State<App>,
    Extension(principal): Extension<Principal>,
) -> Result<Json<Value>, ApiError> {
    if !matches!(
        principal,
        Principal::Administrator
            | Principal::Device {
                scope: Scope::Full,
                ..
            }
    ) {
        return Err(ApiError::new(StatusCode::FORBIDDEN, "full scope required"));
    }
    let mut cache = app.fleet.peer_snapshot.clone().lock_owned().await;
    if let Some((at, answer)) = &cache.entry
        && at.elapsed() < Duration::from_secs(CACHE_SECONDS)
    {
        return Ok(Json(answer.clone()));
    }
    let path = registry_path(&app);
    let legacy = app.fleet.directory().legacy_peers.clone();
    // The refresh owns the lock even if the requesting browser disconnects.
    tokio::task::spawn_blocking(move || cache.refresh(path.as_deref(), &legacy).map(Json))
        .await
        .map_err(|_| registry_failed())?
}

/// Plain HTTP is expected on loopback and Tailscale. Elsewhere a peer token needs TLS.
pub(crate) fn warn_plain_http(base: &str) {
    let Ok(url) = url::Url::parse(base) else {
        return;
    };
    if url.scheme() != "http" {
        return;
    }
    let private = match url.host() {
        Some(url::Host::Domain(name)) => name == "localhost" || name.ends_with(".ts.net"),
        Some(url::Host::Ipv4(ip)) => {
            ip.is_loopback() || ip.octets()[0] == 100 && ip.octets()[1] & 0xc0 == 64
        }
        Some(url::Host::Ipv6(ip)) => {
            ip.is_loopback() || ip.segments()[..3] == [0xfd7a, 0x115c, 0xa1e0]
        }
        None => false,
    };
    if !private {
        eprintln!(
            "warning: {base} is plain http outside loopback and Tailscale; this peer's token would cross the network unencrypted. Prefer its https:// address."
        );
    }
}

/// Names and IDs come from the tokenless identity endpoint. Only the exchange receives the code.
pub(crate) fn identity(url: &str) -> Result<Map<String, Value>, String> {
    fetch(url, "/pairing/identity", None, 10, PAIRING_MAX_BYTES, None)
}

pub(crate) fn exchange_code(url: &str, code: &str, device_name: &str) -> Result<String, String> {
    let body = json!({"code": code.trim(), "device_name": device_name});
    let reply = fetch(
        url,
        "/pairing/exchange",
        None,
        15,
        PAIRING_MAX_BYTES,
        Some(&body),
    )?;
    reply
        .get("device")
        .and_then(|device| device.get("token"))
        .and_then(Value::as_str)
        .filter(|token| valid_token(token))
        .map(str::to_string)
        .ok_or_else(|| "the peer did not return a device token".to_string())
}

/// Hands a message to the Host the owner named: that Host's own `POST /send`.
pub(crate) async fn send(app: &App, name: &str, payload: Value) -> Result<Value, Failed> {
    let Some(path) = registry_path(app) else {
        return Err(Failed::Unknown(name.to_string()));
    };
    let legacy = app.fleet.directory().legacy_peers.clone();
    let name = name.to_string();
    tokio::task::spawn_blocking(move || {
        let mut peers = registry::read(&path, &legacy)
            .map_err(|_| Failed::Down("the peer registry could not be read".to_string()))?;
        let peer = peers
            .remove(&name)
            .and_then(parse)
            .filter(|peer| peer.enabled)
            .ok_or_else(|| Failed::Unknown(name.clone()))?;
        let mut response = direct_agent(30)
            .post(format!("{}/send", peer.url))
            .header("Authorization", format!("Bearer {}", peer.token))
            .header("X-Clarp-Peer", name.as_str())
            .send_json(&payload)
            // Only that it failed; the request held the other Host's token.
            .map_err(|_| Failed::Down(format!("the Host named {name} did not answer")))?;
        let status = response.status().as_u16();
        if (300..400).contains(&status) {
            return Err(Failed::Down(format!(
                "the Host named {name} redirected /send (HTTP {status}); redirect refused"
            )));
        }
        let said = response.body_mut().read_to_string().unwrap_or_default();
        let answer: Value = serde_json::from_str(&said).unwrap_or(Value::Null);
        if said.contains(&peer.token) || contains_token(&answer, &peer.token) {
            return Err(Failed::Down(format!(
                "the Host named {name} returned its credential"
            )));
        }
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
    .await
    .unwrap_or_else(|_| Err(Failed::Down("the message could not be sent".to_string())))
}

pub fn routes() -> Router<App> {
    Router::new()
        .route("/peers/snapshot", get(snapshot))
        .route("/peers", get(list).post(add))
        .route("/peers/{name}", delete(remove))
}
