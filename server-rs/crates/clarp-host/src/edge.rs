//! The edge: what the apps say to the Host over HTTP, in the contract's words.
//!
//! A handler here does three things: reads the request, asks the fleet or an agent, and writes
//! the answer in the types of `clarp-protocol`. It holds no state of its own and decides
//! nothing about an agent.

use std::collections::{BTreeMap, VecDeque};
use std::convert::Infallible;
use std::net::SocketAddr;
use std::sync::Arc;
use std::time::Duration;

use axum::extract::{ConnectInfo, Path, Query, Request, State};
use axum::http::{HeaderMap, HeaderValue, Method, StatusCode, header};
use axum::middleware::{self, Next};
use axum::response::sse::{Event as SseEvent, Sse};
use axum::response::{IntoResponse, Response};
use axum::routing::{delete, get, post};
use axum::{Json, Router};
use clarp_kernel::conversation::{Message, Sender};
use clarp_kernel::turn::{Admission, Origin};
use clarp_ports::clock::Clock;
use clarp_protocol::requests::{CreateAgentRequest, SendRequest, SessionRequest};
use clarp_protocol::responses::{SelectResponse, SendResponse, StopResponse};
use clarp_protocol::server_info::{Capabilities, Contract, ServerInfo};
use futures_util::stream::{self, Stream};
use serde::Deserialize;
use serde_json::{Value, json};
use tokio::sync::broadcast::error::RecvError;

use crate::VERSION;
use crate::agent::Unavailable;
use crate::config::Config;
use crate::fleet::{FleetHandle, Member};
use crate::hub::{Hub, Published};
use crate::pairing::{self, Device, Scope};
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

/// A request that cannot be met, as the Python Host says it: a status with `{"error": ...}`,
/// or, on the one route where the Python Host does so, with plain text.
#[derive(Debug)]
pub struct ApiError {
    status: StatusCode,
    error: String,
    plain: bool,
}

impl ApiError {
    fn new(status: StatusCode, error: impl Into<String>) -> Self {
        Self {
            status,
            error: error.into(),
            plain: false,
        }
    }

    fn plain(status: StatusCode, text: impl Into<String>) -> Self {
        Self {
            status,
            error: text.into(),
            plain: true,
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
        if self.plain {
            let kind = [(header::CONTENT_TYPE, "text/plain")];
            (self.status, kind, self.error).into_response()
        } else {
            (self.status, Json(json!({ "error": self.error }))).into_response()
        }
    }
}

impl App {
    /// The agent with this session name, when the Host has one.
    fn member(&self, session: &str) -> Option<Arc<Member>> {
        self.fleet.directory().by_session(session).cloned()
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
        .route("/pairing/exchange", post(pairing_exchange))
        .route("/pairing/issue", post(pairing_issue))
        .route("/paired-devices", get(paired_devices))
        .route("/paired-devices/revoke", post(revoke_device))
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

/// Who is asking.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Principal {
    /// The Host's own token, presented from this machine; or anyone, on a Host that has none.
    Administrator,
    Device {
        device_id: String,
        scope: Scope,
    },
}

/// The only route a caller without a token may use: a device that has a code has no token yet.
const EXCHANGE: &str = "/pairing/exchange";

/// What a device with limited scope may send. Whatever it may read, it may read.
const LIMITED_POSTS: &[&str] = &["/send", "/select"];

/// Whether this caller is kept from this request by its scope.
fn out_of_scope(principal: &Principal, method: &Method, path: &str) -> bool {
    let Principal::Device {
        scope: Scope::Limited,
        ..
    } = principal
    else {
        return false;
    };
    if path.starts_with("/paired-devices") || path.starts_with("/pairing/issue") {
        return true;
    }
    if method == Method::GET || method == Method::HEAD {
        return false;
    }
    !(method == Method::POST && LIMITED_POSTS.contains(&path))
}

fn who(app: &App, request: &Request) -> Option<Principal> {
    let expected = &app.config.auth_token;
    if expected.is_empty() {
        return Some(Principal::Administrator);
    }
    let token = presented(request.headers(), request.uri().query())?;
    // The administrator's token opens the Host only from the machine it runs on. From
    // anywhere else a device presents a token of its own, which can be taken away again.
    let local = request
        .extensions()
        .get::<ConnectInfo<SocketAddr>>()
        .is_some_and(|peer| peer.0.ip().is_loopback());
    if local && same_token(&token, expected) {
        return Some(Principal::Administrator);
    }
    let directory = app.fleet.directory();
    let device = directory.device(&token)?;
    Some(Principal::Device {
        device_id: device.device_id.clone(),
        scope: device.scope,
    })
}

async fn authorize(State(app): State<App>, mut request: Request, next: Next) -> Response {
    let path = request.uri().path().to_string();
    let mut response = if path == EXCHANGE {
        next.run(request).await
    } else {
        match who(&app, &request) {
            None => ApiError::new(StatusCode::UNAUTHORIZED, "unauthorized").into_response(),
            Some(principal) if out_of_scope(&principal, request.method(), &path) => {
                ApiError::new(StatusCode::FORBIDDEN, "this device may not do that").into_response()
            }
            Some(principal) => {
                request.extensions_mut().insert(principal);
                next.run(request).await
            }
        }
    };
    // What the Host answers is true now and may not be a moment later: nothing keeps it.
    response
        .headers_mut()
        .entry(header::CACHE_CONTROL)
        .or_insert(HeaderValue::from_static("no-store"));
    response
}

// ---- who the Host is, and who its agents are ------------------------------------------------

async fn server_info(State(app): State<App>) -> Json<ServerInfo> {
    Json(identity(&app))
}

/// Who this Host is: what `GET /server-info` answers and what a pairing hands the new device.
fn identity(app: &App) -> ServerInfo {
    ServerInfo {
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
    }
}

async fn snapshot(State(app): State<App>) -> Json<Value> {
    let directory = app.fleet.directory();
    let now = app.clock.now_ms();
    let agents: Vec<Value> = directory
        .members
        .iter()
        .map(|member| {
            let view = member.agent.view();
            let focused = directory.focus.as_deref() == Some(member.record.agent_id.as_str());
            wire::summary(
                &member.record,
                &view.status,
                view.backend,
                view.newest.as_ref(),
                focused,
                now,
            )
        })
        .collect();
    let roster: Vec<&str> = directory
        .members
        .iter()
        .map(|member| member.record.persona.as_str())
        .collect();
    Json(json!({
        "agents": agents,
        "focus": directory.focus,
        "roster": roster,
        "personas": [],
        "available_mcp_servers": [],
        "model_avatars": false,
    }))
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
) -> Result<Json<Value>, ApiError> {
    let Some(member) = app.member(&query.session) else {
        // An agent the Host does not have has no conversation: the answer is the one for an
        // agent that has not said anything yet, and not an error.
        let home = std::env::var("HOME").unwrap_or_default();
        return Ok(Json(wire::no_conversation(&home)));
    };
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
    let status = &member.agent.view().status;
    Ok(Json(wire::log_response(
        &member.record,
        status,
        &page,
        replace,
    )))
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
    let Some(member) = app.member(&request.session) else {
        // The Python Host's words for a message that names an agent it does not have.
        return Err(ApiError::plain(
            StatusCode::NOT_FOUND,
            "unknown forced agent",
        ));
    };
    if request.text.trim().is_empty() {
        return Err(ApiError::new(StatusCode::BAD_REQUEST, "text required"));
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
    Ok(Json(SendResponse {
        ok: true,
        session: member.record.session.clone(),
        dispatch: member.record.backend.clone(),
        trace_id,
        queued: Some(sent.queued),
        queue_depth: Some(sent.queue.waiting as u64),
        queue_revision: Some(sent.queue.revision),
        ..SendResponse::default()
    }))
}

async fn stop(
    State(app): State<App>,
    Json(request): Json<SessionRequest>,
) -> Result<Json<StopResponse>, ApiError> {
    let answer = StopResponse {
        ok: true,
        stop_actor: Some("user".to_string()),
        stop_actor_verified: Some(false),
        stop_reason: Some(String::new()),
        ..StopResponse::default()
    };
    let Some(member) = app.member(&request.session) else {
        // Nothing runs under a name the Host does not have: nothing was ended, and that is all.
        return Ok(Json(answer));
    };
    let (terminated, queue) = member.agent.stop("user".to_string()).await?;
    Ok(Json(StopResponse {
        terminated: u64::from(terminated),
        queue_paused: Some(queue.paused),
        goals_paused: Some(0),
        ..answer
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
        None => Err(ApiError::new(StatusCode::NOT_FOUND, "no such session")),
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
    let member = app
        .member(&query.session)
        .ok_or_else(|| ApiError::new(StatusCode::NOT_FOUND, "agent not found"))?;
    let (waiting, state) = member.agent.queue().await?;
    let items: Vec<Value> = waiting
        .iter()
        .map(|message| {
            json!({
                // A waiting message is named as the client named it.
                "id": message.message_id.strip_prefix("u-").unwrap_or(&message.message_id),
                "text": message.text,
                "enqueued_at": message.accepted_at_ms,
                "synthesize_audio": false,
            })
        })
        .collect();
    Ok(Json(json!({
        "items": items,
        "paused": state.paused,
        "revision": state.revision,
    })))
}

/// Lets a held mailbox run again.
async fn resume(
    State(app): State<App>,
    Json(request): Json<SessionRequest>,
) -> Result<Json<Value>, ApiError> {
    let member = app
        .member(&request.session)
        .ok_or_else(|| ApiError::new(StatusCode::NOT_FOUND, "agent not found"))?;
    let (_, state) = member.agent.resume("user".to_string()).await?;
    Ok(Json(json!({
        "session": member.record.session,
        "count": state.waiting,
        "paused": state.paused,
        "revision": state.revision,
    })))
}

// ---- devices ---------------------------------------------------------------------------------

#[derive(Debug, Deserialize)]
struct ExchangeRequest {
    #[serde(default)]
    code: String,
    #[serde(default)]
    device_name: String,
}

/// A device hands in its code and is given a token of its own. Needs no token: the code is
/// the proof.
async fn pairing_exchange(
    State(app): State<App>,
    Json(request): Json<ExchangeRequest>,
) -> Result<Response, ApiError> {
    match app
        .fleet
        .exchange(request.code, request.device_name)
        .await?
    {
        Ok(device) => {
            let body = json!({ "device": device, "server": identity(&app) });
            Ok((StatusCode::CREATED, Json(body)).into_response())
        }
        Err(why) => Err(ApiError::new(StatusCode::CONFLICT, why)),
    }
}

#[derive(Debug, Deserialize)]
struct IssueRequest {
    #[serde(default)]
    device_name: String,
    #[serde(default)]
    scope: String,
    ttl_seconds: Option<i64>,
    /// The address the device will reach this Host at. With it the answer carries the link
    /// that pairs a device when it is opened there.
    url: Option<String>,
}

/// A value as it may stand in the query of a link.
fn in_a_link(value: &str) -> String {
    let mut out = String::with_capacity(value.len());
    for byte in value.bytes() {
        if byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.' | b'~') {
            out.push(char::from(byte));
        } else {
            out.push_str(&format!("%{byte:02X}"));
        }
    }
    out
}

/// The administrator has a code made for a device.
async fn pairing_issue(
    State(app): State<App>,
    Json(request): Json<IssueRequest>,
) -> Result<Json<Value>, ApiError> {
    let scope = Scope::parse(&request.scope)
        .ok_or_else(|| ApiError::new(StatusCode::BAD_REQUEST, "scope must be full or limited"))?;
    let ttl = request.ttl_seconds.unwrap_or(pairing::DEFAULT_TTL_SECONDS);
    let issued = app
        .fleet
        .issue(request.device_name, scope, ttl)
        .await?
        .map_err(|why| ApiError::new(StatusCode::BAD_REQUEST, why))?;
    let mut answer = json!({
        "code": issued.code,
        "device_name": issued.device_name,
        "scope": issued.scope,
        "created_at": issued.created_at,
        "expires_at": issued.expires_at,
    });
    if let Some(url) = request.url.filter(|url| !url.trim().is_empty()) {
        let uri = format!(
            "clarp://pair?name={}&url={}&code={}",
            in_a_link(&app.config.name),
            in_a_link(url.trim()),
            in_a_link(&issued.code)
        );
        answer["url"] = json!(url.trim());
        answer["uri"] = json!(uri);
    }
    Ok(Json(answer))
}

async fn paired_devices(State(app): State<App>) -> Json<Value> {
    let directory = app.fleet.directory();
    let mut devices: Vec<Device> = directory
        .devices
        .values()
        .map(|device| device.public())
        .collect();
    devices.sort_by_key(|device| std::cmp::Reverse(device.created_at));
    Json(json!({ "devices": devices }))
}

#[derive(Debug, Deserialize)]
struct RevokeRequest {
    #[serde(default)]
    device_id: String,
}

async fn revoke_device(
    State(app): State<App>,
    Json(request): Json<RevokeRequest>,
) -> Result<Json<Value>, ApiError> {
    let device_id = request.device_id.trim().to_string();
    if device_id.is_empty() {
        return Err(ApiError::new(StatusCode::BAD_REQUEST, "device_id required"));
    }
    if app.fleet.revoke(device_id).await? {
        Ok(Json(json!({ "ok": true })))
    } else {
        Err(ApiError::new(StatusCode::NOT_FOUND, "device not found"))
    }
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
    fn a_limited_device_may_read_and_send_and_nothing_else() {
        let limited = Principal::Device {
            device_id: "device_1".to_string(),
            scope: Scope::Limited,
        };
        let full = Principal::Device {
            device_id: "device_2".to_string(),
            scope: Scope::Full,
        };
        for (method, path, kept_out) in [
            (Method::GET, "/log", false),
            (Method::GET, "/agents/snapshot", false),
            (Method::POST, "/send", false),
            (Method::POST, "/select", false),
            (Method::POST, "/stop", true),
            (Method::POST, "/agents", true),
            (Method::DELETE, "/agents/echo", true),
            (Method::GET, "/paired-devices", true),
            (Method::POST, "/paired-devices/revoke", true),
            (Method::POST, "/pairing/issue", true),
        ] {
            assert_eq!(
                out_of_scope(&limited, &method, path),
                kept_out,
                "{method} {path}"
            );
            assert!(!out_of_scope(&full, &method, path), "{method} {path}");
            assert!(!out_of_scope(&Principal::Administrator, &method, path));
        }
    }

    #[test]
    fn a_pairing_link_carries_its_values_whole() {
        assert_eq!(in_a_link("Clarp Host (Rust)"), "Clarp%20Host%20%28Rust%29");
        assert_eq!(
            in_a_link("https://host.example:7690"),
            "https%3A%2F%2Fhost.example%3A7690"
        );
        assert_eq!(in_a_link("clp_Abc-123_x.y~z"), "clp_Abc-123_x.y~z");
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
