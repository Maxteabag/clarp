//! The edge: what the apps say to the Host over HTTP, in the contract's words.
//!
//! A handler here does three things: reads the request, asks the fleet or an agent, and writes
//! the answer in the types of `clarp-protocol`. It holds no state of its own and decides
//! nothing about an agent.

use std::collections::{BTreeMap, VecDeque};
use std::convert::Infallible;
use std::sync::Arc;
use std::time::Duration;

use axum::extract::{Path, Query, Request, State};
use axum::http::{HeaderMap, StatusCode, header};
use axum::middleware::{self, Next};
use axum::response::sse::{Event as SseEvent, Sse};
use axum::response::{IntoResponse, Response};
use axum::routing::{delete, get, post};
use axum::{Json, Router};
use clarp_kernel::conversation::{Message, Sender};
use clarp_kernel::turn::{Admission, Origin};
use clarp_ports::clock::Clock;
use clarp_protocol::log::LogResponse;
use clarp_protocol::requests::{CreateAgentRequest, SendRequest, SessionRequest};
use clarp_protocol::responses::{SelectResponse, SendResponse, StopResponse};
use clarp_protocol::server_info::{Capabilities, Contract, ServerInfo};
use clarp_protocol::snapshot::Snapshot;
use futures_util::stream::{self, Stream};
use serde::Deserialize;
use serde_json::{Value, json};
use tokio::sync::broadcast::error::RecvError;

use crate::VERSION;
use crate::agent::Unavailable;
use crate::config::Config;
use crate::fleet::{FleetHandle, Member};
use crate::hub::{Hub, Published};
use crate::{ids, wire};

/// The contract this Host speaks: the Python Host's number on the day this was written. What
/// this Host does not have yet is absent from `FEATURES`, which is how an app learns it.
pub const HOST_CONTRACT: u64 = 99;
pub const MIN_IOS_CONTRACT: u64 = 1;

/// The features of the contract this Host has.
pub const FEATURES: &[&str] = &[];

/// After this long without an event, a stream is sent a heartbeat.
const HEARTBEAT: Duration = Duration::from_secs(10);

/// Everything a handler can reach.
#[derive(Clone)]
pub struct App {
    pub config: Arc<Config>,
    pub hub: Arc<Hub>,
    pub fleet: FleetHandle,
    pub clock: Arc<dyn Clock + Send + Sync>,
    /// Which Host this is, for an app that knows several.
    pub server_id: String,
}

/// A request that cannot be met, as the contract says it: a status and `{"ok": false, "error"}`.
#[derive(Debug)]
pub struct ApiError {
    status: StatusCode,
    error: String,
}

impl ApiError {
    fn new(status: StatusCode, error: impl Into<String>) -> Self {
        Self {
            status,
            error: error.into(),
        }
    }
}

impl From<Unavailable> for ApiError {
    fn from(unavailable: Unavailable) -> Self {
        Self::new(StatusCode::SERVICE_UNAVAILABLE, unavailable.0)
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        let body = json!({ "ok": false, "error": self.error });
        (self.status, Json(body)).into_response()
    }
}

impl App {
    fn member(&self, session: &str) -> Result<Arc<Member>, ApiError> {
        self.fleet
            .directory()
            .by_session(session)
            .cloned()
            .ok_or_else(|| {
                ApiError::new(StatusCode::NOT_FOUND, format!("unknown session {session}"))
            })
    }
}

pub fn router(app: App) -> Router {
    Router::new()
        .route("/server-info", get(server_info))
        .route("/agents/snapshot", get(snapshot))
        .route("/agents", post(create_agent))
        .route("/agents/{session}", delete(remove_agent))
        .route("/log", get(log))
        .route("/events", get(events))
        .route("/send", post(send))
        .route("/stop", post(stop))
        .route("/select", post(select))
        .route("/turn-queue", get(queue))
        .route("/turn-queue/resume", post(resume))
        .layer(middleware::from_fn_with_state(app.clone(), authorize))
        .with_state(app)
}

// ---- who may ask ----------------------------------------------------------------------------

/// The token a request carries: in its header, in its cookie, or in its address.
fn presented(headers: &HeaderMap, query: Option<&str>) -> Option<String> {
    let header_text = |name| headers.get(name).and_then(|value| value.to_str().ok());
    if let Some(bearer) = header_text(header::AUTHORIZATION).and_then(|v| v.strip_prefix("Bearer "))
    {
        return Some(bearer.trim().to_string());
    }
    let cookie = header_text(header::COOKIE).and_then(|cookies| {
        cookies
            .split(';')
            .find_map(|cookie| cookie.trim().strip_prefix("claude_pwa_token="))
    });
    if let Some(cookie) = cookie {
        return Some(cookie.to_string());
    }
    query?
        .split('&')
        .find_map(|pair| pair.strip_prefix("token="))
        .map(str::to_string)
}

/// Whether two tokens are the same, in a time that does not depend on where they differ.
fn same_token(presented: &str, expected: &str) -> bool {
    let (a, b) = (presented.as_bytes(), expected.as_bytes());
    let mut different = a.len() ^ b.len();
    for (index, byte) in b.iter().enumerate() {
        different |= usize::from(byte ^ a.get(index).copied().unwrap_or(0));
    }
    different == 0
}

async fn authorize(State(app): State<App>, request: Request, next: Next) -> Response {
    let expected = &app.config.auth_token;
    let allowed = expected.is_empty()
        || presented(request.headers(), request.uri().query())
            .is_some_and(|token| same_token(&token, expected));
    if allowed {
        next.run(request).await
    } else {
        ApiError::new(StatusCode::UNAUTHORIZED, "unauthorized").into_response()
    }
}

// ---- who the Host is, and who its agents are ------------------------------------------------

async fn server_info(State(app): State<App>) -> Json<ServerInfo> {
    Json(ServerInfo {
        server_id: app.server_id.clone(),
        name: app.config.name.clone(),
        deployment_mode: "native".to_string(),
        version: VERSION.to_string(),
        image_version: None,
        default_cwd: app.config.default_cwd.display().to_string(),
        clarp_version: VERSION.to_string(),
        min_app_version: "0".to_string(),
        capabilities: Capabilities {
            version: 1,
            features: FEATURES
                .iter()
                .map(|feature| (*feature).to_string())
                .collect(),
        },
        contract: Contract {
            host: HOST_CONTRACT,
            min_ios: MIN_IOS_CONTRACT,
            features: BTreeMap::new(),
        },
        client: None,
        local_connection: None,
    })
}

async fn snapshot(State(app): State<App>) -> Json<Snapshot> {
    let directory = app.fleet.directory();
    let agents = directory
        .members
        .iter()
        .map(|member| {
            let view = member.agent.view();
            let focused = directory.focus.as_deref() == Some(member.record.agent_id.as_str());
            wire::summary(&member.record, &view.status, view.backend, focused)
        })
        .collect();
    let roster = directory
        .members
        .iter()
        .map(|member| member.record.persona.clone())
        .collect();
    Json(Snapshot {
        agents,
        focus: directory.focus.clone(),
        roster: Some(roster),
        personas: Some(Vec::new()),
        available_mcp_servers: Some(Vec::new()),
    })
}

async fn create_agent(
    State(app): State<App>,
    Json(request): Json<CreateAgentRequest>,
) -> Result<Json<Value>, ApiError> {
    match app.fleet.create(request).await? {
        Ok(record) => Ok(Json(json!({
            "ok": true,
            "session": record.session,
            "persona": record.persona,
            "agent_id": record.agent_id,
            "backend": record.backend,
            "cwd": record.cwd,
        }))),
        Err(why) => Err(ApiError::new(StatusCode::BAD_REQUEST, why)),
    }
}

async fn remove_agent(
    State(app): State<App>,
    Path(session): Path<String>,
) -> Result<Json<Value>, ApiError> {
    if app.fleet.remove(session.clone()).await? {
        Ok(Json(json!({ "ok": true, "session": session })))
    } else {
        Err(ApiError::new(
            StatusCode::NOT_FOUND,
            format!("unknown session {session}"),
        ))
    }
}

// ---- a conversation --------------------------------------------------------------------------

#[derive(Debug, Deserialize)]
struct LogQuery {
    session: String,
    limit: Option<usize>,
    after_revision: Option<u64>,
    before: Option<String>,
}

async fn log(
    State(app): State<App>,
    Query(query): Query<LogQuery>,
) -> Result<Json<LogResponse>, ApiError> {
    let member = app.member(&query.session)?;
    let limit = query.limit.unwrap_or(100).clamp(1, 5_000);
    let (page, replace) = if let Some(revision) = query.after_revision {
        let page = member.agent.after(revision, limit).await?;
        // A client that holds a revision this conversation never reached holds another
        // conversation: it must drop what it has.
        let replace = revision > page.head_revision;
        (page, replace)
    } else if let Some(id) = query.before {
        let older = member.agent.before(id, limit).await?;
        (older.unwrap_or_default(), false)
    } else {
        (member.agent.tail(limit).await?, false)
    };
    Ok(Json(wire::log_response(&member.record, &page, replace)))
}

// ---- the commands ----------------------------------------------------------------------------

/// Who wrote a message that says it comes from another agent.
fn sender(app: &App, named: Option<&Value>) -> Option<Sender> {
    let directory = app.fleet.directory();
    let name = match named? {
        Value::String(name) => name.as_str(),
        Value::Object(fields) => fields
            .get("session")
            .or_else(|| fields.get("agent_id"))
            .and_then(Value::as_str)?,
        _ => return None,
    };
    let known = directory
        .members
        .iter()
        .find(|member| member.record.session == name || member.record.agent_id == name);
    Some(match known {
        Some(member) => Sender {
            agent_id: member.record.agent_id.clone(),
            name: member.record.persona.clone(),
            session: member.record.session.clone(),
        },
        // An agent of another Host: all that is known of it is the name it gave.
        None => Sender {
            agent_id: name.to_string(),
            name: String::new(),
            session: name.to_string(),
        },
    })
}

async fn send(
    State(app): State<App>,
    Json(request): Json<SendRequest>,
) -> Result<Json<SendResponse>, ApiError> {
    let member = app.member(&request.session)?;
    if request.text.trim().is_empty() {
        return Err(ApiError::new(
            StatusCode::BAD_REQUEST,
            "a message needs text",
        ));
    }
    let from_agent = request.origin.as_deref() == Some("agent");
    let sender = if from_agent {
        sender(&app, request.sender.as_ref())
    } else {
        None
    };
    let origin = match &sender {
        Some(sender) => Origin::Agent {
            agent_id: sender.agent_id.clone(),
        },
        None => Origin::User,
    };
    // The user's own message takes the place of a running turn unless it asks to wait. A
    // message from an agent never does.
    let admission = if sender.is_none() && request.queue_if_busy != Some(true) {
        Admission::Direct
    } else {
        Admission::Queue
    };
    let trace_id = request.trace_id.clone().unwrap_or_else(|| ids::hex(8));
    let client_msg_id = request.client_msg_id.clone().unwrap_or_else(ids::uuid);
    let message = Message {
        id: format!("u-{client_msg_id}"),
        text: request.text,
        origin,
        trace_id: Some(trace_id.clone()),
        sender,
        client: request.client,
    };
    let sent = member.agent.send(message, admission).await?;
    let waiting = member.agent.view().status.waiting.len() as u64;
    Ok(Json(SendResponse {
        ok: true,
        session: member.record.session.clone(),
        dispatch: member.record.backend.clone(),
        trace_id,
        queued: Some(sent.queued),
        queue_depth: Some(waiting),
        ..SendResponse::default()
    }))
}

async fn stop(
    State(app): State<App>,
    Json(request): Json<SessionRequest>,
) -> Result<Json<StopResponse>, ApiError> {
    let member = app.member(&request.session)?;
    let terminated = member.agent.stop("user".to_string()).await?;
    Ok(Json(StopResponse {
        ok: true,
        terminated: u64::from(terminated),
        stop_actor: Some("user".to_string()),
        queue_paused: Some(true),
        ..StopResponse::default()
    }))
}

async fn select(
    State(app): State<App>,
    Json(request): Json<SessionRequest>,
) -> Result<Json<SelectResponse>, ApiError> {
    match app.fleet.select(request.session.clone()).await? {
        Some(record) => Ok(Json(SelectResponse {
            ok: true,
            session: record.session,
        })),
        None => Err(ApiError::new(
            StatusCode::NOT_FOUND,
            format!("unknown session {}", request.session),
        )),
    }
}

#[derive(Debug, Deserialize)]
struct SessionQuery {
    session: String,
}

/// What waits in an agent's mailbox.
async fn queue(
    State(app): State<App>,
    Query(query): Query<SessionQuery>,
) -> Result<Json<Value>, ApiError> {
    let member = app.member(&query.session)?;
    let view = member.agent.view();
    let waiting: Vec<Value> = view
        .status
        .waiting
        .iter()
        .map(|turn| json!({ "id": turn.0 }))
        .collect();
    Ok(Json(json!({
        "ok": true,
        "session": member.record.session,
        "queued": waiting,
        "queue_paused": view.status.paused,
    })))
}

/// Lets a held mailbox run again.
async fn resume(
    State(app): State<App>,
    Json(request): Json<SessionRequest>,
) -> Result<Json<Value>, ApiError> {
    let member = app.member(&request.session)?;
    let resumed = member.agent.resume("user".to_string()).await?;
    Ok(Json(json!({ "ok": true, "resumed": resumed })))
}

// ---- the stream of events --------------------------------------------------------------------

#[derive(Debug, Deserialize)]
struct EventsQuery {
    last_event_id: Option<u64>,
}

fn numbered(published: &Published) -> SseEvent {
    SseEvent::default()
        .id(published.id.to_string())
        .data(published.json.clone())
}

fn unnumbered(event: &Value) -> SseEvent {
    SseEvent::default().data(event.to_string())
}

async fn events(
    State(app): State<App>,
    headers: HeaderMap,
    Query(query): Query<EventsQuery>,
) -> Sse<impl Stream<Item = Result<SseEvent, Infallible>>> {
    let after = headers
        .get("last-event-id")
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.trim().parse().ok())
        .or(query.last_event_id);
    let (missed, live) = app.hub.subscribe(after);
    // First what the client missed, then the word to look at everything: whatever happened
    // while it was away for longer than the hub remembers, the snapshot and the conversations
    // will tell.
    let mut first = VecDeque::with_capacity(missed.len() + 2);
    first.push_back(unnumbered(
        &json!({ "type": "server-version", "version": VERSION }),
    ));
    first.extend(missed.iter().map(|published| numbered(published)));
    first.push_back(unnumbered(&json!({ "type": "agent-roster" })));
    let clock = app.clock.clone();
    let stream = stream::unfold((first, live), move |(mut first, mut live)| {
        let clock = clock.clone();
        async move {
            if let Some(event) = first.pop_front() {
                return Some((Ok(event), (first, live)));
            }
            match tokio::time::timeout(HEARTBEAT, live.recv()).await {
                Ok(Ok(published)) => Some((Ok(numbered(&published)), (first, live))),
                // A client that fell too far behind is cut off. It reconnects, and what it
                // missed is given to it then.
                Ok(Err(RecvError::Lagged(_) | RecvError::Closed)) => None,
                Err(_) => {
                    let beat = json!({ "type": "heartbeat", "ts": clock.now_ms() });
                    let event = SseEvent::default()
                        .event("heartbeat")
                        .data(beat.to_string());
                    Some((Ok(event), (first, live)))
                }
            }
        }
    });
    Sse::new(stream)
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use axum::http::HeaderValue;

    use super::*;

    #[test]
    fn a_token_is_found_in_the_header_the_cookie_or_the_address() {
        let mut headers = HeaderMap::new();
        assert_eq!(presented(&headers, None), None);
        assert_eq!(
            presented(&headers, Some("a=1&token=q-token")).as_deref(),
            Some("q-token")
        );
        headers.insert(
            header::COOKIE,
            HeaderValue::from_static("x=1; claude_pwa_token=c-token"),
        );
        assert_eq!(
            presented(&headers, Some("token=q-token")).as_deref(),
            Some("c-token")
        );
        headers.insert(
            header::AUTHORIZATION,
            HeaderValue::from_static("Bearer h-token"),
        );
        assert_eq!(presented(&headers, None).as_deref(), Some("h-token"));
    }

    #[test]
    fn tokens_are_compared_whole() {
        assert!(same_token("secret", "secret"));
        assert!(!same_token("secret", "secre"));
        assert!(!same_token("secre", "secret"));
        assert!(!same_token("", "secret"));
        assert!(!same_token("Secret", "secret"));
    }
}
