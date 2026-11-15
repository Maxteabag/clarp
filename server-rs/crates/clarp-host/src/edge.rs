//! The edge: what the apps say to the Host over HTTP, in the contract's words.
//!
//! A handler here does three things: reads the request, asks the fleet or an agent, and writes
//! the answer in the types of `clarp-protocol`. It holds no state of its own and decides
//! nothing about an agent.

use std::collections::VecDeque;
use std::convert::Infallible;
use std::net::SocketAddr;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use axum::extract::{ConnectInfo, DefaultBodyLimit, Extension, Path, Query, Request, State};
use axum::http::{HeaderMap, HeaderValue, Method, StatusCode, header};
use axum::middleware::{self, Next};
use axum::response::sse::{Event as SseEvent, Sse};
use axum::response::{IntoResponse, Response};
use axum::routing::{delete, get, post};
use axum::{Json, Router};
use clarp_actor::ImportedRow;
use clarp_actor::conversation::Which;
use clarp_kernel::conversation::{Message, Sender};
use clarp_kernel::jobs::{Job, JobCommand};
use clarp_kernel::presence::Presence;
use clarp_kernel::turn::{Admission, Origin};
use clarp_ports::clock::Clock;
use clarp_protocol::requests::{CreateAgentRequest, SendRequest, SessionRequest};
use clarp_protocol::responses::{SelectResponse, SendResponse, StopResponse};
use clarp_protocol::server_info::{
    Capabilities, ClientCompatibility, Compatibility, Contract, LocalConnection, ServerInfo,
};
use futures_util::stream::{self, Stream};
use serde::Deserialize;
use serde_json::{Value, json};
use tokio::sync::broadcast::error::RecvError;

use crate::VERSION;
use crate::agent::Unavailable;
use crate::config::Config;
use crate::fleet::{AgentRecord, FleetHandle, Member};
use crate::hub::{Hub, Published};
use crate::pairing::{self, Device, Scope};
use crate::pusher::Pusher;
use crate::revisions::Revisions;
use crate::tools::{self, ToolAnswer, ToolCall};
use crate::{ids, joblog, library, plans, push, wire};

/// The contract this Host speaks: the Python Host's number on the day this was written. What
/// this Host does not have yet is absent from `FEATURES`, which is how an app learns it.
pub const HOST_CONTRACT: u64 = 99;
pub const MIN_IOS_CONTRACT: u64 = 1;

/// The features of the contract this Host has, each with the contract number that
/// introduced it (the Python Host's `FEATURE_CONTRACTS`): the apps gate on a feature being
/// listed, some on it being listed with its number. A feature is listed here when everything
/// it names is built and held to the Python Host; what is half there is not listed.
pub const FEATURES: &[(&str, u64)] = &[
    // What agents make and ask: the library, the inbox, requests and every way they end.
    ("artifact_summary_v1", 73),
    ("flat_artifact_responses", 1),
    ("artifacts", 1),
    // Background jobs, and a cancel that names the run it means.
    ("background_jobs", 1),
    // Pairing codes and devices with tokens of their own.
    ("pairing", 1),
    // `POST /turn-queue/resume`.
    ("turn_queue_resume", 7),
    // Plans with goals: `/task-plan`, `/task-plans`, `/task-plan/action`.
    ("durable_task_goals", 21),
    ("background_job_cancel_generation", 23),
    // A send that says it came from the desk is not pushed to the phone.
    ("desktop_send_quiet_push", 27),
    ("decision_lifecycle", 28),
    ("text_input_requests", 29),
    ("artifact_pins", 38),
    // `POST /devices` takes a phone's own grant, and the Host pushes by it.
    ("push_grants", 39),
    // `DELETE /devices` and `GET /devices/push-tokens`.
    ("push_unregister", 93),
];

/// The most a conversation that arrives from another Host may weigh, as JSON.
const IMPORT_LIMIT: usize = 512 * 1024 * 1024;

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
    /// The numbers for changes the whole Host counts in one row.
    pub revisions: Arc<Revisions>,
    /// The way to the phones that are not listening.
    pub pusher: Pusher,
    /// The desks that have said their user sits at them: no phone is paged meanwhile.
    pub desks: Arc<Mutex<Presence>>,
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
    pub(crate) fn new(status: StatusCode, error: impl Into<String>) -> Self {
        Self {
            status,
            error: error.into(),
            plain: false,
        }
    }

    /// The status this error is answered with.
    pub(crate) fn status(&self) -> StatusCode {
        self.status
    }

    /// Why, in words.
    pub(crate) fn message(&self) -> &str {
        &self.error
    }

    pub(crate) fn plain(status: StatusCode, text: impl Into<String>) -> Self {
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
    pub(crate) fn member(&self, session: &str) -> Option<Arc<Member>> {
        self.fleet.directory().by_session(session).cloned()
    }
}

pub fn router(app: App) -> Router {
    Router::new()
        .route("/server-info", get(server_info))
        .route("/agents/snapshot", get(snapshot))
        .route("/agents", post(create_agent))
        .route("/agents/{session}", delete(remove_agent))
        // A whole conversation arrives in one request.
        .route(
            "/agents/import",
            post(import_agent).layer(DefaultBodyLimit::max(IMPORT_LIMIT)),
        )
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
        .route("/background-jobs", get(background_jobs))
        .route(
            "/background-jobs/{job_id}",
            get(background_job).delete(cancel_background_job),
        )
        .route("/tools/run", post(run_tool))
        .merge(library::routes())
        .merge(plans::routes())
        .merge(push::routes())
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
    // An agent's own tools speak as the Host's administrator, from this machine. No device does.
    if path.starts_with("/tools/") {
        return !matches!(principal, Principal::Administrator);
    }
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
        // What an installer stamped this installation with; a Host run from a build has none.
        version: app.config.deployed_version.clone(),
        image_version: Some(std::env::var("CLARP_IMAGE_VERSION").unwrap_or_default()),
        default_cwd: app.config.default_cwd.display().to_string(),
        clarp_version: VERSION.to_string(),
        min_app_version: "1.0".to_string(),
        capabilities: Capabilities {
            version: 1,
            features: FEATURES
                .iter()
                .map(|(feature, _)| (*feature).to_string())
                .collect(),
        },
        contract: Contract {
            host: HOST_CONTRACT,
            min_ios: MIN_IOS_CONTRACT,
            features: FEATURES
                .iter()
                .map(|(feature, introduced)| ((*feature).to_string(), *introduced))
                .collect(),
        },
        // Nobody said which app is asking, so nothing is known of whether it fits.
        client: Some(ClientCompatibility {
            status: Compatibility::Unknown,
            platform: Some(String::new()),
            build: Some(String::new()),
            contract: Some(0),
            reason: Some(String::new()),
        }),
        local_connection: Some(LocalConnection {
            enabled: false,
            port: None,
            certificate_sha256: None,
            urls: None,
            service_type: None,
        }),
    }
}

async fn snapshot(State(app): State<App>) -> Json<Value> {
    let directory = app.fleet.directory();
    let agents: Vec<Value> = directory
        .members
        .iter()
        .map(|member| {
            let view = member.agent.view();
            let focused = directory.focus.as_deref() == Some(member.record.agent_id.as_str());
            let mut row = wire::summary(
                &member.record,
                &view.status,
                view.backend,
                view.newest.as_ref(),
                view.ended.as_ref(),
                focused,
            );
            // What runs for the agent besides its turn.
            let running = view
                .jobs
                .iter()
                .filter(|job| job.status.is_active())
                .count();
            row["background_jobs"]["count"] = json!(running);
            row
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
        // What the Python Host says while nobody has chosen otherwise.
        "tool_explanations": { "detail_level": 2, "enabled": true },
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

/// An agent as another Host knew it, with the conversation it had there.
#[derive(Debug, Deserialize)]
struct ImportRequest {
    /// The agent's row in the other Host's snapshot.
    agent: Value,
    /// Where it comes from, for the record.
    from: String,
    /// The rows of its conversation, oldest first, as the other Host's `GET /log` gave them.
    turns: Vec<Value>,
}

/// Takes an agent over from another Host: who it is, the conversation it had, and the tool's
/// session, which its next turn here continues.
async fn import_agent(
    State(app): State<App>,
    Json(request): Json<ImportRequest>,
) -> Result<Json<Value>, ApiError> {
    let field = |name: &str| request.agent[name].as_str().unwrap_or("").to_string();
    let backend_session = Some(field("backend_session_id")).filter(|session| !session.is_empty());
    let record = AgentRecord {
        agent_id: field("agent_id"),
        session: field("session"),
        persona: field("persona"),
        backend: field("backend"),
        cwd: field("cwd"),
        model: field("model"),
        effort: field("effort"),
        // The conversation keeps its name: it is the tool's session, which goes on.
        conversation_id: backend_session.clone().unwrap_or_default(),
        created_at_ms: app.clock.now_ms(),
        deleted_at_ms: None,
    };
    let rows: Vec<ImportedRow> = request
        .turns
        .into_iter()
        .filter_map(|row| {
            let id = row["id"].as_str()?.to_string();
            let ts_ms = row["timestamp"]
                .as_str()
                .and_then(wire::from_iso)
                .unwrap_or(0);
            Some(ImportedRow { id, ts_ms, row })
        })
        .collect();
    let member = app
        .fleet
        .adopt(record)
        .await?
        .map_err(|why| ApiError::new(StatusCode::CONFLICT, why))?;
    let outcome = member
        .agent
        .import(request.from, backend_session, rows)
        .await;
    let imported = match outcome {
        Ok(Ok(imported)) => imported,
        failed => {
            // Nothing of the conversation was written. An agent without its past is not what
            // was asked for: it does not stay.
            let _ = app.fleet.remove(member.record.session.clone()).await;
            return Err(match failed {
                Ok(Err(why)) => ApiError::new(StatusCode::CONFLICT, why),
                Err(unavailable) => unavailable.into(),
                Ok(Ok(_)) => ApiError::new(StatusCode::INTERNAL_SERVER_ERROR, "the move failed"),
            });
        }
    };
    Ok(Json(json!({
        "ok": true,
        "session": member.record.session,
        "agent_id": member.record.agent_id,
        "imported": imported,
    })))
}

// ---- a conversation --------------------------------------------------------------------------

#[derive(Debug, Deserialize)]
struct LogQuery {
    session: String,
    limit: Option<usize>,
    after_revision: Option<u64>,
    before: Option<String>,
    /// `0`, `false` or `no`: without what a watcher or the Host's own routine began.
    include_automated: Option<String>,
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
    let include_automated = !query
        .include_automated
        .as_deref()
        .is_some_and(|said| matches!(said.to_lowercase().as_str(), "0" | "false" | "no"));
    let which = if include_automated {
        Which::All
    } else {
        Which::WithoutAutomated
    };
    // "After revision 0" is how a client that has nothing yet asks: it gets the newest page.
    let after = query.after_revision.filter(|revision| *revision > 0);
    let (page, replace) = if let Some(revision) = after {
        let page = member.agent.after(revision, limit, which).await?;
        // A client that holds a revision this conversation never reached holds another
        // conversation: it must drop what it has.
        let replace = revision > page.head_revision;
        (page, replace)
    } else if let Some(id) = query.before {
        let older = member.agent.before(id, limit, which).await?;
        (older.unwrap_or_default(), false)
    } else {
        (member.agent.tail(limit, which).await?, false)
    };
    let status = &member.agent.view().status;
    let mut answer = wire::log_response(&member.record, status, &page, replace);
    answer["includes_automated"] = json!(include_automated);
    Ok(Json(answer))
}

// ---- the commands ----------------------------------------------------------------------------

/// The short name an app gives itself on a send (`desktop`), when it is one: lower case
/// letters, digits and hyphens, at most 32.
fn client_name(given: &str) -> Option<String> {
    let name = given.trim().to_lowercase();
    let plain = |c: char| c.is_ascii_lowercase() || c.is_ascii_digit();
    let fits = name.chars().count() <= 32
        && name.chars().next().is_some_and(plain)
        && name.chars().all(|c| plain(c) || c == '-');
    fits.then_some(name)
}

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
    send_message(&app, request).await.map(Json)
}

/// Takes a message for an agent: what `POST /send` does, and what an agent's own tool does
/// when it writes to another agent.
pub(crate) async fn send_message(
    app: &App,
    request: SendRequest,
) -> Result<SendResponse, ApiError> {
    // What the Python Host refuses a send with, in its order and its words (plain text).
    if request.text.trim().is_empty() {
        return Err(ApiError::plain(StatusCode::BAD_REQUEST, "empty text"));
    }
    let Some(member) = app.member(&request.session) else {
        return Err(ApiError::plain(
            StatusCode::NOT_FOUND,
            "unknown forced agent",
        ));
    };
    // Who it comes from, as the Python Host reads a send: the origin it names, when that is
    // one a sender may name; otherwise another agent, when it names its sender; otherwise
    // the user. What the Host stamps itself (a janitor, a leader's tick) nobody may claim.
    let sender = sender(app, request.sender.as_ref());
    let from_agent = || Origin::Agent {
        agent_id: sender
            .as_ref()
            .map(|sender| sender.agent_id.clone())
            .unwrap_or_default(),
    };
    let named = request
        .origin
        .as_deref()
        .map(|origin| origin.trim().to_lowercase())
        .unwrap_or_default();
    if named == "heartbeat" {
        // This Host has no heartbeats. The Python Host's words for one where they are off.
        return Err(ApiError::plain(
            StatusCode::CONFLICT,
            "Heartbeats are disabled on this Host",
        ));
    }
    let origin = match named.as_str() {
        "user" => Origin::User,
        "agent" => from_agent(),
        "oracle" | "schedule" | "automation" | "watcher" | "dreaming" => Origin::Trigger {
            name: named.clone(),
        },
        _ if sender.is_some() => from_agent(),
        _ => Origin::User,
    };
    let origin_is_user = origin == Origin::User;
    // A message to an agent that is busy waits its turn, as on the Python Host. What differs
    // is where it waits. A plain message is said: it is in the conversation at once. One sent
    // with `queue_if_busy` was left for later: it is in the queue until its turn starts, and
    // when it does not come from a person, a Stop holds it. (The kernel can also take a message
    // that replaces the running turn; nothing asks for it yet.)
    let admission = match (request.queue_if_busy.unwrap_or(false), origin_is_user) {
        (false, _) => Admission::Queue,
        (true, true) => Admission::Later,
        (true, false) => Admission::Background,
    };
    let trace_id = request.trace_id.clone().unwrap_or_else(|| ids::hex(8));
    let client_msg_id = request.client_msg_id.clone().unwrap_or_else(ids::uuid);
    let message = Message {
        id: format!("u-{client_msg_id}"),
        text: request.text,
        origin,
        trace_id: Some(trace_id.clone()),
        sender,
        client: request.client.as_deref().and_then(client_name),
    };
    let sent = member.agent.send(message, admission).await?;
    if origin_is_user {
        // Writing to an agent makes it the open one, without a word about it.
        let _ = app.fleet.open(member.record.session.clone()).await;
    }
    Ok(SendResponse {
        ok: true,
        session: member.record.session.clone(),
        dispatch: member.record.backend.clone(),
        trace_id,
        queued: Some(sent.queued),
        // Where the queue stands with this message in it. A message that did not have to
        // wait says nothing about the queue: the Python Host answers zeros, and so does this.
        queue_depth: Some(if sent.queued {
            sent.queue.waiting as u64
        } else {
            0
        }),
        queue_revision: Some(if sent.queued { sent.queue.revision } else { 0 }),
        ..SendResponse::default()
    })
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
    let stopped = member.agent.stop("user".to_string()).await?;
    Ok(Json(StopResponse {
        terminated: u64::from(stopped.told),
        queue_paused: Some(stopped.queue.paused),
        goals_paused: Some(stopped.goals_paused as u64),
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

// ---- background jobs, and the agents' own tools ----------------------------------------------

/// An agent's own tool, calling its Host.
async fn run_tool(State(app): State<App>, Json(call): Json<ToolCall>) -> Json<ToolAnswer> {
    Json(tools::run(&app, &call).await)
}

/// Every job the Host knows that is worth showing, with the agent it belongs to.
fn shown_jobs(app: &App) -> Vec<(Arc<Member>, Job)> {
    let directory = app.fleet.directory();
    let mut jobs: Vec<(Arc<Member>, Job)> = directory
        .members
        .iter()
        .flat_map(|member| {
            let view = member.agent.view();
            view.jobs
                .iter()
                .map(|job| (member.clone(), job.clone()))
                .collect::<Vec<_>>()
        })
        .collect();
    // What runs first, then what ended; within each, the last changed first.
    jobs.sort_by_key(|(_, job)| {
        (
            job.status.is_terminal(),
            std::cmp::Reverse(job.updated_at_ms),
        )
    });
    jobs
}

/// `GET /background-jobs`: what runs, and what ended within a day.
async fn background_jobs(State(app): State<App>) -> Json<Value> {
    let now = app.clock.now_ms();
    let jobs = shown_jobs(&app);
    let (active, ended): (Vec<_>, Vec<_>) =
        jobs.iter().partition(|(_, job)| job.status.is_active());
    let listed: Vec<Value> = active
        .iter()
        .chain(ended.iter().take(100))
        .map(|(member, job)| wire::job(&member.record, job, now))
        .collect();
    Json(json!({
        "jobs": listed,
        "snapshot_revision": app.revisions.latest(),
        "observed_at": now,
    }))
}

/// The agent a job belongs to, with the job as it stands.
async fn job_and_owner(app: &App, job_id: &str) -> Result<Option<(Arc<Member>, Job)>, ApiError> {
    let Some(agent_id) = app.fleet.job_owner(job_id.to_string()).await? else {
        return Ok(None);
    };
    let directory = app.fleet.directory();
    let Some(member) = directory
        .members
        .iter()
        .find(|member| member.record.agent_id == agent_id)
    else {
        return Ok(None);
    };
    let view = member.agent.view();
    Ok(view
        .jobs
        .iter()
        .find(|job| job.id == job_id)
        .map(|job| (member.clone(), job.clone())))
}

/// `GET /background-jobs/<id>`: one job with its timeline, its progress and the end of its log.
async fn background_job(
    State(app): State<App>,
    Path(job_id): Path<String>,
    Extension(principal): Extension<Principal>,
) -> Result<Json<Value>, ApiError> {
    let Some((member, job)) = job_and_owner(&app, &job_id).await? else {
        return Err(ApiError::new(StatusCode::NOT_FOUND, "job not found"));
    };
    let now = app.clock.now_ms();
    let timeline = member.agent.job_timeline(job.id.clone(), 200).await?;
    // A log may hold anything its worker printed: a device with limited scope is shown the
    // timeline and the progress, not the file.
    let limited = matches!(
        principal,
        Principal::Device {
            scope: Scope::Limited,
            ..
        }
    );
    let log = if limited {
        joblog::forbidden()
    } else {
        let roots = joblog::roots(&job.worker_cwd, &member.record.cwd);
        joblog::tail(&job.log_path, &roots, joblog::TAIL_BYTES)
    };
    Ok(Json(json!({
        "job": wire::job(&member.record, &job, now),
        "handle": job.handle(),
        "is_process": true,
        "owner_session": member.record.session,
        "owner_agent_id": member.record.agent_id,
        "started_trace_id": job.started_trace_id,
        "helper_session": "",
        "progress": { "text": job.progress_text, "at": job.progress_at_ms },
        "timeline": wire::job_timeline(&timeline),
        "log": log,
    })))
}

#[derive(Debug, Deserialize)]
struct CancelQuery {
    expected_generation: Option<String>,
}

/// The largest generation a JSON client can hold exactly.
const MAX_GENERATION: u64 = (1 << 53) - 1;

/// The run a cancel names: in the address, or else in the body. `Err` for one that is no
/// run's number. Absent means "whatever run there is".
fn expected_generation(query: &CancelQuery, body: &[u8]) -> Result<Option<u64>, ()> {
    let in_range = |value: u64| (1..=MAX_GENERATION).contains(&value);
    if let Some(raw) = &query.expected_generation {
        let raw = raw.trim();
        if raw.is_empty() || !raw.bytes().all(|b| b.is_ascii_digit()) {
            return Err(());
        }
        return raw
            .parse()
            .ok()
            .filter(|v| in_range(*v))
            .map(Some)
            .ok_or(());
    }
    if body.is_empty() {
        return Ok(None);
    }
    let Ok(Value::Object(fields)) = serde_json::from_slice::<Value>(body) else {
        return Err(());
    };
    match fields.get("expected_generation") {
        None => Ok(None),
        Some(value) => value.as_u64().filter(|v| in_range(*v)).map(Some).ok_or(()),
    }
}

/// `DELETE /background-jobs/<id>`: the user ends a job. With `expected_generation`, only if it
/// is still that run.
async fn cancel_background_job(
    State(app): State<App>,
    Path(job_id): Path<String>,
    Query(query): Query<CancelQuery>,
    body: axum::body::Bytes,
) -> Result<Response, ApiError> {
    let Ok(expected) = expected_generation(&query, &body) else {
        let refused = json!({ "ok": false, "error": "invalid expected_generation" });
        return Ok((StatusCode::BAD_REQUEST, Json(refused)).into_response());
    };
    let Some((member, _)) = job_and_owner(&app, &job_id).await? else {
        return Err(ApiError::new(StatusCode::NOT_FOUND, "job not found"));
    };
    let command = JobCommand::Cancel {
        id: job_id.clone(),
        expected_generation: expected,
        reason: "user_cancelled".to_string(),
    };
    let change = member
        .agent
        .job(command)
        .await?
        .map_err(|refusal| ApiError::new(StatusCode::BAD_REQUEST, refusal.to_string()))?;
    let Some(job) = change.job else {
        return Err(ApiError::new(StatusCode::NOT_FOUND, "job not found"));
    };
    let changed = change.record.is_some();
    let now = app.clock.now_ms();
    let mut receipt = json!({
        "changed": changed,
        "job": wire::job(&member.record, &job, now),
        "job_handle": job.handle(),
    });
    let conflict = |receipt: &mut Value, error: String| {
        receipt["ok"] = json!(false);
        receipt["error"] = json!(error);
        (StatusCode::CONFLICT, Json(receipt.clone())).into_response()
    };
    if change.generation_mismatch {
        receipt["expected_generation"] = json!(expected);
        receipt["current_generation"] = json!(job.generation);
        return Ok(conflict(&mut receipt, "generation_mismatch".to_string()));
    }
    if job.status != clarp_kernel::jobs::JobStatus::Cancelled {
        let error = format!("job already {}", job.status.as_str());
        return Ok(conflict(&mut receipt, error));
    }
    if changed {
        // The job is closed for its worker from this moment. The owner is told, so that it
        // can clean up what the worker left; nothing depends on that message arriving.
        let worker_pid = match job.worker.pid {
            0 => "unknown".to_string(),
            pid => pid.to_string(),
        };
        let token = match job.worker.start_token.as_str() {
            "" => "unknown",
            token => token,
        };
        let handle = job.handle();
        let text = format!(
            "the user cancelled background job {}: {} (handle {handle}; pid={worker_pid} \
             start_token={token}). Before stopping anything, run `clarp-agent-bg {} job-cancelled \
             {handle}`. Proceed only if it returns 0 and the target process still matches that \
             exact worker identity; otherwise this cleanup was superseded by a newer generation.",
            job.id, job.title, member.record.session
        );
        let message = Message {
            id: format!("u-{}", ids::uuid()),
            text,
            origin: Origin::Trigger {
                name: "automation".to_string(),
            },
            trace_id: Some(ids::hex(8)),
            sender: None,
            client: None,
        };
        let _ = member.agent.send(message, Admission::Queue).await;
    }
    receipt["ok"] = json!(true);
    Ok(Json(receipt).into_response())
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
            ids::in_a_link(&app.config.name),
            ids::in_a_link(url.trim()),
            ids::in_a_link(&issued.code)
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
        assert_eq!(
            ids::in_a_link("Clarp Host (Rust)"),
            "Clarp%20Host%20%28Rust%29"
        );
        assert_eq!(
            ids::in_a_link("https://host.example:7690"),
            "https%3A%2F%2Fhost.example%3A7690"
        );
        assert_eq!(ids::in_a_link("clp_Abc-123_x.y~z"), "clp_Abc-123_x.y~z");
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
