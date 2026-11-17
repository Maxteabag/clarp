//! The edge: what the apps say to the Host over HTTP, in the contract's words.
//!
//! A handler here does three things: reads the request, asks the fleet or an agent, and writes
//! the answer in the types of `clarp-protocol`. It holds no state of its own and decides
//! nothing about an agent.

use std::collections::{BTreeSet, HashMap, HashSet, VecDeque};
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
use clarp_actor::conversation::Which;
use clarp_actor::{ByHand, ImportedRow};
use clarp_kernel::activity::Leases;
use clarp_kernel::addressed;
use clarp_kernel::conversation::{Message, Sender};
use clarp_kernel::helpers;
use clarp_kernel::jobs::{Job, JobCommand};
use clarp_kernel::journal::Reworded;
use clarp_kernel::kept_portraits::{self, Portrait};
use clarp_kernel::presence::Presence;
use clarp_kernel::python;
use clarp_kernel::trust::{self, FailureLimiter};
use clarp_kernel::turn::{Admission, Origin, TurnId};
use clarp_ports::blobs::Blobs;
use clarp_ports::clock::Clock;
use clarp_protocol::requests::{CreateAgentRequest, SendRequest, SessionRequest};
use clarp_protocol::responses::{SelectResponse, SendResponse, StopResponse};
use clarp_protocol::server_info::{
    Capabilities, ClientCompatibility, Compatibility, Contract, LocalConnection, ServerInfo,
};
use futures_util::StreamExt as _;
use futures_util::stream::{self, Stream};
use serde::Deserialize;
use serde_json::{Value, json};
use tokio::sync::broadcast::error::RecvError;

use crate::VERSION;
use crate::agent::Unavailable;
use crate::config::Config;
use crate::fleet::{AgentRecord, Directory, FleetHandle, Member, NotMade};
use crate::hub::{Hub, Published};
use crate::pairing::{self, Device, Scope};
use crate::pusher::Pusher;
use crate::revisions::Revisions;
use crate::tools::{self, ToolAnswer, ToolCall};
use crate::{apps, ids, joblog, library, media, peers, plans, push, roster, wire};

/// The contract this Host speaks: the Python Host's number on the day this was written. What
/// this Host does not have yet is absent from `FEATURES`, which is how an app learns it.
pub const HOST_CONTRACT: u64 = 99;
pub const MIN_IOS_CONTRACT: u64 = 1;

/// The features of the contract this Host has, each with the contract number that
/// introduced it (the Python Host's `FEATURE_CONTRACTS`): the apps gate on a feature being
/// listed, some on it being listed with its number. A feature is listed here when everything
/// it names is built and held to the Python Host; what is half there is not listed.
const CHIP: &str = "live_activity_push";

/// The features this Host has now: all of [`FEATURES`], without what it cannot do as it is
/// set up.
fn features(app: &App) -> impl Iterator<Item = &'static (&'static str, u64)> {
    let chip = app.chip.pushes();
    FEATURES
        .iter()
        .filter(move |(feature, _)| *feature != CHIP || chip)
}

pub const FEATURES: &[(&str, u64)] = &[
    // What agents make and ask: the library, the inbox, requests and every way they end.
    // `GET /attention/inbox`: what agents made, a page at a time.
    ("attention_index", 2),
    ("artifact_summary_v1", 73),
    ("flat_artifact_responses", 1),
    ("artifacts", 1),
    // What an agent publishes: `POST /media`, an agent's gallery, a file fetched whole and
    // in parts.
    ("media", 1),
    // A report: a page of HTML an agent hands its user, served behind its policy; what it
    // was, version by version; and the origins its page may reach.
    ("html_reports", 19),
    ("html_report_revisions", 49),
    ("html_form_network", 59),
    // Background jobs, and a cancel that names the run it means.
    ("background_jobs", 1),
    // Pairing codes and devices with tokens of their own.
    ("pairing", 1),
    // Calls: the Host's record of who is in a phone's call, on hold and on the floor
    // (`/calls`, the `group-call` event, `call_id` of a send; `docs/group-calls.md`).
    ("group_calls", 60),
    // Rings: an agent calls its owner, and answering starts a call in which the agent's
    // opening line is heard first (`/calls/ring`, the `agent-ring` event and push;
    // `docs/agent-rings.md`). The ring whose line is said: not the live voice call
    // (`agent_ring_live`), not the one that listens first, not a phone's own switch.
    ("agent_rings", 64),
    // The chip a phone shows of the fleet on its lock screen is kept current by pushes
    // (`POST /devices/live-activity`; `docs/live-items.md` §8). Listed only by a Host that
    // can reach an activity: one with a relay and an account of its own there.
    (CHIP, 45),
    // `POST /turn-queue/resume`.
    ("turn_queue_resume", 7),
    // The heartbeat: the owner's settings, an agent's switch and cadence, and the turn an
    // idle agent that asked for it is given (`/heartbeat/settings`, `/agent-heartbeat`).
    ("heartbeat", 1),
    // A running turn as items that appear, grow and settle: `live` events on `/events?live=`
    // and `GET /live` (`docs/live-items.md`).
    ("live_items", 42),
    // A settled turn can be folded from the conversation alone: every row a turn writes
    // carries the turn's trace, what the agent said its phase, the turn's last row how the
    // turn ended, and a tool an interrupted turn cut off reads `interrupted`.
    ("log_turn_summary", 47),
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
    // Agents that make agents: who made an agent, how a helper's work stands
    // (`/agent-helper-state`), and what an agent's row says of the ones it made.
    ("helper_agents", 13),
    // An agent forks itself into helpers that start from its conversation and each get a
    // task (`POST /agents/<session>/fork`). Each child starts from the conversation as
    // words (`fork: seed`): a tool's own copy of a conversation is not made here yet.
    ("agent_fork", 41),
    // `GET /agents/snapshot?include_archived=0` and `GET /agents/archived`.
    ("snapshot_without_archived", 82),
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
    /// The bytes of the files agents publish.
    pub blobs: Arc<dyn Blobs>,
    /// What the apps have reported of themselves: whether one is in front, and touched.
    pub activity: Arc<Mutex<Leases>>,
    /// The engines that listen: what turns a clip of speech into text.
    pub ears: Arc<crate::heard::Ears>,
    /// The one that speaks, and where the clips of what agents say aloud are.
    pub mouth: Arc<crate::voice::Mouth>,
    /// The pictures agents are shown with.
    pub faces: Arc<crate::faces::Faces>,
    /// The calls: who is in each phone's call, on hold, and has the floor.
    pub calls: Arc<crate::calls::Desk>,
    /// The chip a phone shows of the fleet on its lock screen, and what keeps it current.
    pub chip: Arc<crate::chip::Chip>,
    /// The prompts agents are sent at set times.
    pub schedules: Arc<crate::schedules::Desk>,
    /// The heartbeat: its settings, and what is remembered of each agent it wakes.
    pub heartbeat: Arc<crate::heartbeat::Desk>,
    /// What the door keeps count of: who failed to say who they are, and whose right to
    /// speak was taken away.
    pub door: Arc<Door>,
    /// The relay a phone reaches this Host through, when it has one.
    pub connector: Arc<crate::connector::Connector>,
}

/// What the door keeps count of.
pub struct Door {
    failures: Mutex<FailureLimiter>,
    since: std::time::Instant,
    /// The devices whose right to speak was taken away, told to the streams they have open.
    revoked: tokio::sync::broadcast::Sender<String>,
}

impl Default for Door {
    fn default() -> Self {
        Self {
            failures: Mutex::default(),
            since: std::time::Instant::now(),
            revoked: tokio::sync::broadcast::channel(64).0,
        }
    }
}

impl Door {
    /// One more failure of an asker. Answers how many seconds it is told to wait: nothing
    /// while it is within what anybody may fail.
    fn failed(&self, source: &str) -> u64 {
        let now = self.since.elapsed().as_secs_f64();
        let mut failures = self
            .failures
            .lock()
            .unwrap_or_else(|held| held.into_inner());
        failures.failure(source, now)
    }

    /// The answer to a caller that did not say who it is, or said it wrongly: that, or, to
    /// one that fails too often, how long to wait.
    fn refused(&self, source: &str, too_many: &str) -> Option<Response> {
        let wait = self.failed(source);
        (wait > 0).then(|| {
            let mut refused =
                ApiError::new(StatusCode::TOO_MANY_REQUESTS, too_many).into_response();
            if let Ok(wait) = HeaderValue::from_str(&wait.to_string()) {
                refused.headers_mut().insert(header::RETRY_AFTER, wait);
            }
            refused
        })
    }

    /// An answer that goes on for as long as its asker listens, ended when the device that
    /// asked loses its right to speak.
    fn until_revoked(&self, device_id: String, answer: Response) -> Response {
        let mut revoked = self.revoked.subscribe();
        let gone = async move {
            loop {
                match revoked.recv().await {
                    Ok(revoked) if revoked == device_id => return,
                    Ok(_) => {}
                    // What was missed may have been this device: the stream ends, and a
                    // device that may still speak opens it again.
                    Err(RecvError::Lagged(_)) => return,
                    Err(RecvError::Closed) => std::future::pending::<()>().await,
                }
            }
        };
        let (head, body) = answer.into_parts();
        let body = body.into_data_stream().take_until(Box::pin(gone));
        Response::from_parts(head, axum::body::Body::from_stream(body))
    }
}

/// Whose failure a request's failure is: put on every request, for a handler that counts
/// failures of its own (a pairing code that is none).
#[derive(Debug, Clone)]
pub struct Asker {
    pub source: String,
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
        .route("/message-tool-details", get(tool_details))
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
        .route("/orchestrator/route-delegation", post(route_delegation))
        .route("/stop", post(stop))
        .route("/select", post(select))
        .route("/turn-queue", get(queue))
        .route("/live", get(shown))
        .route("/turn-queue/resume", post(resume))
        .route("/turn-queue/{id}", delete(take_back).put(reword))
        .route("/turn-queue/{id}/send", post(send_by_hand))
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
        .merge(apps::routes())
        .merge(crate::heard::routes())
        .merge(library::routes())
        .merge(media::routes())
        .merge(plans::routes())
        .merge(push::routes())
        .merge(peers::routes())
        .merge(roster::routes())
        .merge(crate::calls::routes())
        .merge(crate::chip::routes())
        .merge(crate::schedules::routes())
        .merge(crate::heartbeat::routes())
        .merge(crate::forks::routes())
        .merge(crate::connector::routes())
        .merge(crate::personas::routes())
        .merge(crate::places::routes())
        .merge(crate::files::routes())
        .merge(crate::screens::routes())
        .merge(crate::voice::routes())
        .merge(crate::voices::routes())
        .merge(crate::faces::routes())
        // What is no route of this Host, by its path or by its method, is answered as the
        // Python Host answers it: to whoever may ask at all.
        .fallback(no_route)
        .method_not_allowed_fallback(no_route)
        .layer(middleware::from_fn_with_state(app.clone(), authorize))
        .with_state(app)
}

async fn no_route() -> ApiError {
    ApiError::plain(StatusCode::NOT_FOUND, "not found")
}

// ---- who may ask ----------------------------------------------------------------------------

/// The token in an `Authorization` header: what follows the word `Bearer`, in any case.
fn bearer(given: &str) -> Option<&str> {
    let given = python::strip(given);
    let word = given.get(..7)?;
    word.eq_ignore_ascii_case("bearer ")
        .then(|| python::strip(&given[7..]))
}

/// The token in the cookie a page of this Host keeps it in.
fn cookie(cookies: &str) -> Option<&str> {
    let mut cookies = cookies.split(';');
    let token = cookies.find_map(|cookie| cookie.trim().strip_prefix("claude_pwa_token="));
    token.map(str::trim)
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
    /// One of an agent's own tools, with the token its turn was given, from this machine.
    Tools,
}

/// The only route a caller without a token may use: a device that has a code has no token yet.
const EXCHANGE: &str = "/pairing/exchange";
/// And the pictures this Host ships, which anybody may fetch: an address of one is put
/// where no token goes (a notification, a page), as on the Python Host.
const PICTURES: &str = "/static/";
/// And the picture an agent has of its own, by an address this Host signed for a day: what
/// a push carries, for a phone that shows the notification without the app's token.
const SIGNED_PICTURES: &str = "/notification-avatars/";

/// What a device with limited scope may send. Whatever it may read, it may read.
const LIMITED_POSTS: &[&str] = &[
    "/send",
    "/transcribe",
    "/select",
    "/focus",
    "/upload",
    "/clog",
    "/clips/ack",
    "/voice-events",
    "/location",
];

/// What a device with limited scope is kept from altogether, also from reading: the Host's
/// logins and software, its owner's settings, what is on its disk, calls. The Python Host's
/// list (`_DEVICE_FULL_ONLY_PREFIXES`), whether or not this Host has the route yet: a route
/// that is built later is closed from its first day.
const FULL_ONLY: &[&str] = &[
    "/backend-auth",
    "/harness-providers",
    "/server-update",
    "/managed-skills",
    "/hotseat/desk",
    "/hotseat/switch",
    "/hotseat/resume",
    "/orchestrator/",
    "/herald/",
    "/personalities/",
    "/automation-settings",
    "/avatar-settings",
    "/paired-devices",
    "/tts/providers",
    "/network/",
    "/server-terminal",
    "/oracle/",
    "/agent-file",
    "/janitor-runs/",
    "/calls",
];

/// Whether this caller is kept from this request by its scope.
fn out_of_scope(principal: &Principal, method: &Method, path: &str) -> bool {
    // An agent's own tools are asked for from this machine, with the token a turn was given
    // or the administrator's. No device asks for them.
    if path.starts_with("/tools/") {
        return !matches!(principal, Principal::Administrator | Principal::Tools);
    }
    // And that token opens nothing else: what the apps read and do is not a tool's.
    if matches!(principal, Principal::Tools) {
        return true;
    }
    let Principal::Device {
        scope: Scope::Limited,
        ..
    } = principal
    else {
        return false;
    };
    let closed = |prefix: &&str| path == prefix.trim_end_matches('/') || path.starts_with(*prefix);
    if FULL_ONLY.iter().any(closed) || path.starts_with("/pairing/issue") {
        return true;
    }
    // What is on this machine's disk, by a path the caller chooses.
    if matches!(path, "/dirs" | "/past-sessions") {
        return true;
    }
    if method == Method::GET || method == Method::HEAD {
        return false;
    }
    !(method == Method::POST && LIMITED_POSTS.contains(&path))
}

/// A request's headers as the rules of trust read them: each as it was sent, a value byte
/// for byte.
fn headers_of(headers: &HeaderMap) -> Vec<(String, String)> {
    let headers = headers.iter().map(|(name, value)| {
        let value = value.as_bytes().iter().map(|byte| char::from(*byte));
        (name.as_str().to_string(), value.collect())
    });
    headers.collect()
}

/// Whether a request is local, and whose failure its failure is.
///
/// A request the relay's connector hands in has no socket of its own: it is never local,
/// and the one it is counted for is the one the relay says it asks for.
fn asked(request: &Request) -> (bool, String) {
    let headers = headers_of(request.headers());
    let peer = request.extensions().get::<ConnectInfo<SocketAddr>>();
    let peer = peer.map(|peer| peer.0.ip().to_string());
    let local = peer
        .as_deref()
        .is_some_and(|peer| trust::local_request(peer, &headers));
    let source = trust::failure_source(peer.as_deref().unwrap_or("127.0.0.1"), &headers);
    (local, source)
}

/// Who is asking, by the token the request carries: in its header, in its cookie, or, from
/// this machine, in its address.
fn who(app: &App, request: &Request, local: bool) -> Option<Principal> {
    let expected = &app.config.auth_token;
    if expected.is_empty() {
        // A Host without a token is open to this machine, and to nobody else.
        return local.then_some(Principal::Administrator);
    }
    let known = |token: &str| -> Option<Principal> {
        // The administrator's token opens the Host only from the machine it runs on, and so
        // does what a turn was given for its agent's tools. From anywhere else a device
        // presents a token of its own, which can be taken away again.
        if local && same_token(token, expected) {
            return Some(Principal::Administrator);
        }
        if local && same_token(token, &crate::tool_token(expected)) {
            return Some(Principal::Tools);
        }
        let directory = app.fleet.directory();
        let device = directory.device(token)?;
        Some(Principal::Device {
            device_id: device.device_id.clone(),
            scope: device.scope,
        })
    };
    let headers = request.headers();
    let text = |name| headers.get(name).and_then(|value| value.to_str().ok());
    if let Some(principal) = text(header::AUTHORIZATION).and_then(bearer).and_then(known) {
        return Some(principal);
    }
    if let Some(principal) = text(header::COOKIE).and_then(cookie).and_then(known) {
        return Some(principal);
    }
    // A token in an address is written into logs and histories on its way: it is taken
    // only where there is no way.
    if !local {
        return None;
    }
    let mut pairs = request.uri().query()?.split('&');
    pairs.find_map(|pair| known(pair.strip_prefix("token=")?.trim()))
}

async fn authorize(State(app): State<App>, mut request: Request, next: Next) -> Response {
    let path = request.uri().path().to_string();
    let picture = path.starts_with(PICTURES) || path.starts_with(SIGNED_PICTURES);
    let open = path == EXCHANGE || (picture && request.method() == Method::GET);
    let (local, source) = asked(&request);
    let mut device = None;
    let mut response = if open {
        request.extensions_mut().insert(Asker { source });
        next.run(request).await
    } else {
        match who(&app, &request, local) {
            None => {
                let too_many = app
                    .door
                    .refused(&source, "too many authentication failures");
                too_many.unwrap_or_else(|| {
                    ApiError::new(StatusCode::UNAUTHORIZED, "unauthorized").into_response()
                })
            }
            Some(principal) if out_of_scope(&principal, request.method(), &path) => {
                let why = if matches!(principal, Principal::Tools) {
                    "an agent's tools may ask for tools only"
                } else {
                    "this device may not do that"
                };
                ApiError::new(StatusCode::FORBIDDEN, why).into_response()
            }
            Some(principal) => {
                if let Principal::Device { device_id, .. } = &principal {
                    device = Some(device_id.clone());
                }
                request.extensions_mut().insert(Asker { source });
                request.extensions_mut().insert(principal);
                next.run(request).await
            }
        }
    };
    // A stream of events a device listens to ends when the device may no longer listen.
    let streams = response.headers().get(header::CONTENT_TYPE);
    let streams = streams.is_some_and(|kind| kind.as_bytes().starts_with(b"text/event-stream"));
    if let Some(device_id) = device.filter(|_| streams) {
        response = app.door.until_revoked(device_id, response);
    }
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
pub(crate) fn identity(app: &App) -> ServerInfo {
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
            features: features(app)
                .map(|(feature, _)| (*feature).to_string())
                .collect(),
        },
        contract: Contract {
            host: HOST_CONTRACT,
            min_ios: MIN_IOS_CONTRACT,
            features: features(app)
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

/// The agents an agent made, as its row counts them.
#[derive(Default)]
struct Family {
    children: usize,
    /// The sessions of the helpers among them.
    helper_sessions: Vec<String>,
    /// The helpers that are working, each as its maker's line describes it.
    working: Vec<helpers::Working>,
}

/// What every agent's row says of the agents it made: counted once for a whole list, over
/// every agent of the fleet, whichever agents the list shows.
pub(crate) struct Families(HashMap<String, Family>);

impl Families {
    pub(crate) fn of(directory: &Directory) -> Self {
        let mut families: HashMap<String, Family> = HashMap::new();
        for member in &directory.members {
            let record = &member.record;
            let Some(parent) = &record.parent_agent_id else {
                continue;
            };
            let family = families.entry(parent.clone()).or_default();
            family.children += 1;
            if record.is_helper() {
                family.helper_sessions.push(record.session.clone());
            }
            if record.helper_state.as_deref() == Some("running") {
                let view = member.agent.view();
                let state = wire::state_name(&wire::state_kind(&view.status));
                // What it says it is doing, or what its state says.
                let said = view.status.label.trim();
                let text = if said.is_empty() {
                    let doing = wire::activity(record, &view.status);
                    doing["summary"].as_str().unwrap_or("").trim().to_string()
                } else {
                    said.to_string()
                };
                family.working.push(helpers::Working {
                    session: record.session.clone(),
                    text,
                    bucket: helpers::bucket(&state),
                });
            }
        }
        Self(families)
    }

    /// The agents put away that an agent in the list names as the one that made it. A
    /// client that reads the list without them cannot tell such a maker from one that was
    /// removed: this tells them apart.
    fn archived_parents(directory: &Directory) -> Vec<String> {
        let put_away: HashSet<&str> = directory
            .members
            .iter()
            .filter(|member| member.record.archived_at_ms.is_some())
            .map(|member| member.record.agent_id.as_str())
            .collect();
        let named: BTreeSet<String> = directory
            .members
            .iter()
            .filter(|member| member.record.archived_at_ms.is_none())
            .filter_map(|member| member.record.parent_agent_id.clone())
            .filter(|parent| put_away.contains(parent.as_str()))
            .collect();
        named.into_iter().collect()
    }
}

/// An agent's row in the snapshot: who it is and what it is doing.
pub(crate) fn agent_row(
    directory: &Directory,
    families: &Families,
    member: &Member,
    faces: &crate::faces::Faces,
) -> Value {
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
    // The face it is shown with: the picture it has of its own; else, unless it has a
    // symbol, the picture this Host ships for its name, or the one for its tool. And the
    // picture drawn for its name on its model, where there is one and the agent wears no
    // picture of its own: that one is the owner's choice and is never changed under him.
    let record = &member.record;
    let own = record.avatar.as_deref().map(|digest| {
        let version = clarp_kernel::personas::version(digest);
        clarp_kernel::portraits::address(crate::personas::OF_AGENTS, &record.agent_id, version)
    });
    row["avatar_symbol"] = json!(record.avatar_symbol);
    row["model_avatar_url"] = json!(match &own {
        Some(_) => String::new(),
        None => faces.on_model(&record.persona, view.backend, &record.model),
    });
    row["avatar_url"] = json!(match own {
        Some(own) => own,
        None if record.avatar_symbol.is_empty() => faces.of(&record.persona, view.backend),
        None => String::new(),
    });
    // Two things keep an agent busy after its turn: helpers it made that are working, and
    // background processes it left running. A job that only mirrors one of its helpers is
    // not a process of its own.
    let none = Family::default();
    let family = families.0.get(&member.record.agent_id).unwrap_or(&none);
    let helper_sessions: Vec<&str> = family.helper_sessions.iter().map(String::as_str).collect();
    let processes: Vec<helpers::Process> = view
        .jobs
        .iter()
        .filter(|job| job.status.is_active())
        .filter(|job| wire::mirrored_helper(job, &helper_sessions).is_none())
        .map(|job| helpers::Process {
            title: job.title.clone(),
            progress: job.progress_text.clone(),
            active_at_ms: job.progress_at_ms.unwrap_or(job.started_at_ms),
        })
        .collect();
    row["child_count"] = json!(family.children);
    row["running_children"] = json!(family.working.len());
    row["background_jobs"] =
        json!({ "count": processes.len(), "sub_agents": family.working.len() });
    if !processes.is_empty() || !family.working.is_empty() {
        // The apps draw it as working, not as idle; and its line says what goes on, when
        // the agent did not say so itself.
        let at_work = ["thinking", "tool", "compacting", "background"];
        if !at_work.contains(&row["latest_state"].as_str().unwrap_or("")) {
            row["latest_state"] = json!("background");
        }
        if row["status_text"].is_null() {
            row["status_text"] = json!(helpers::work_summary(&family.working, &processes));
        }
    }
    row
}

#[derive(Debug, Deserialize)]
struct SnapshotQuery {
    include_archived: Option<String>,
}

/// Every agent of the Host. With `include_archived=0` the ones that are put away are left
/// out and counted: `GET /agents/archived` pages them.
async fn snapshot(State(app): State<App>, Query(query): Query<SnapshotQuery>) -> Json<Value> {
    let directory = app.fleet.directory();
    let without_archived = query.include_archived.as_deref() == Some("0");
    let put_away = |member: &Arc<Member>| member.record.archived_at_ms.is_some();
    let families = Families::of(&directory);
    let agents: Vec<Value> = directory
        .members
        .iter()
        .filter(|member| !(without_archived && put_away(member)))
        .map(|member| agent_row(&directory, &families, member, &app.faces))
        .collect();
    // Who an agent can be: the personas this Host has built in, with the voices the owner's
    // file gives them and the look the owner gave them, and the personas the owner made.
    let [named, cartesia, gemini] = crate::owner::personas(&app.mouth.owners());
    let personas = directory.personas.listed(&clarp_kernel::personas::Owner {
        roster: &named,
        cartesia: &cartesia,
        gemini: &gemini,
    });
    // The names an app offers: every persona's, then every agent's that is none of them,
    // whichever agents were asked for. A name is one name whatever the case of its letters.
    let mut roster: Vec<&str> = Vec::new();
    let mut offered: HashSet<String> = HashSet::new();
    let of_personas = personas
        .iter()
        .filter_map(|persona| persona["name"].as_str());
    let of_agents = directory
        .members
        .iter()
        .map(|member| member.record.persona.as_str());
    for name in of_personas.chain(of_agents) {
        let name = name.trim();
        if !name.is_empty() && offered.insert(name.to_lowercase()) {
            roster.push(name);
        }
    }
    let mut body = json!({
        "agents": agents,
        "focus": directory.focus,
        "roster": roster,
        "personas": personas,
        "available_mcp_servers": [],
        "model_avatars": false,
        // Nothing explains a tool's use here, and an app is told so wherever it asks: it
        // then shows the tool's own command on the row.
        "tool_explanations": apps::explanations_told(&app),
    });
    if without_archived {
        let archived = directory.members.iter().filter(|member| put_away(member));
        body["archived_count"] = json!(archived.count());
        body["archived_parents"] = json!(Families::archived_parents(&directory));
    }
    Json(body)
}

/// What the answer to making an agent says of it: the fields of its row that the Python
/// Host answers with.
const MADE_FIELDS: [&str; 21] = [
    "agent_id",
    "session",
    "persona",
    "backend",
    "cwd",
    "model",
    "effort",
    "voice_id",
    "avatar_symbol",
    "muted",
    "heartbeat_enabled",
    "dreaming_enabled",
    "parent_agent_id",
    "role",
    "helper_state",
    "alive",
    "latest_state",
    "last_activity",
    "mcp_servers",
    "avatar_url",
    "avatar_generation_status",
];

/// Makes an agent from what a request said: `POST /agents`, and `clarp-admin agent create`.
/// Answers what the Python Host answers: the new agent's session, its name and its row, or
/// why it was not made.
pub(crate) async fn make_agent(
    app: &App,
    asked: &serde_json::Map<String, Value>,
) -> Result<Value, (StatusCode, Value)> {
    let refused = |status: StatusCode, code: &str, message: &str| {
        (status, json!({ "error": code, "message": message }))
    };
    let bad = |why: &str| refused(StatusCode::BAD_REQUEST, why, why);
    // A field that is a text, or says nothing.
    let text = |key: &str, what: &str| match asked.get(key) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(said)) => Ok(Some(said.clone())),
        Some(_) => Err(bad(&format!("{what} must be a string or null"))),
    };
    let plain = |key: &str| asked.get(key).and_then(Value::as_str).map(str::to_string);
    // What this Host cannot do with a new agent yet is refused in words. Passed over, a
    // request to start an agent again in place would be answered as a name that is taken,
    // and one to take up a past session would start an agent that knows nothing of it.
    let named = |key: &str| plain(key).is_some_and(|said| !said.trim().is_empty());
    if named("replace_sid") {
        let why = "Starting an agent again in place is not built on this Host yet. Its \
                   model and effort are changed in its settings; anything else takes a \
                   new agent.";
        return Err(refused(
            StatusCode::BAD_REQUEST,
            "relaunch_unsupported",
            why,
        ));
    }
    if named("resume_session_id") || named("fork_session_id") {
        let why = "Taking up or forking a past session of a tool is not built on this Host yet.";
        return Err(refused(StatusCode::BAD_REQUEST, "resume_unsupported", why));
    }
    // No tool is given servers of its own here: every one that is named is one this Host
    // does not know, and is refused in the Python Host's words for that.
    match asked.get("mcp_servers") {
        None => {}
        Some(Value::Array(names)) if names.iter().all(Value::is_string) => {
            let mut unknown: Vec<&str> = Vec::new();
            for name in names.iter().filter_map(Value::as_str).map(str::trim) {
                if !name.is_empty() && !unknown.contains(&name) {
                    unknown.push(name);
                }
            }
            if !unknown.is_empty() {
                let why = format!("Unknown MCP server: {}", unknown.join(", "));
                return Err(refused(StatusCode::BAD_REQUEST, "unknown mcp server", &why));
            }
        }
        Some(_) => return Err(bad("mcp_servers must be a list of names")),
    }
    let request = CreateAgentRequest {
        name: plain("name").unwrap_or_default(),
        backend: plain("backend"),
        cwd: plain("cwd"),
        model: text("model", "model")?,
        effort: text("effort", "effort")?,
        voice_id: plain("voice_id"),
        parent: plain("parent"),
        role: plain("role"),
        session: plain("session"),
        personality: plain("personality"),
        avatar_symbol: plain("avatar_symbol"),
        avatar_base64: plain("avatar_base64"),
    };
    let unavailable =
        |why: Unavailable| (StatusCode::SERVICE_UNAVAILABLE, json!({ "error": why.0 }));
    let record = match app.fleet.create(request).await.map_err(unavailable)? {
        Ok(record) => record,
        Err(NotMade::Refused(why)) => {
            return Err((StatusCode::BAD_REQUEST, json!({ "error": why })));
        }
        Err(NotMade::Occupied { owner, session }) => {
            return Err((StatusCode::CONFLICT, roster::occupied(&owner, &session)));
        }
        Err(NotMade::Told(told)) => {
            let status = StatusCode::from_u16(told.status).unwrap_or(StatusCode::BAD_REQUEST);
            return Err(refused(status, told.code, &told.message));
        }
    };
    let directory = app.fleet.directory();
    let row = directory
        .named(&record.agent_id)
        .map(|member| agent_row(&directory, &Families::of(&directory), member, &app.faces))
        .unwrap_or_default();
    // Three of them the Python Host answers as it stores them: a number, 0 or 1.
    let stored = ["muted", "heartbeat_enabled", "dreaming_enabled"];
    let agent: serde_json::Map<String, Value> = MADE_FIELDS
        .iter()
        .map(|field| {
            let value = match row[*field].as_bool() {
                Some(truth) if stored.contains(field) => json!(i32::from(truth)),
                _ => row[*field].clone(),
            };
            ((*field).to_string(), value)
        })
        .collect();
    Ok(json!({
        "ok": true,
        "session": record.session,
        "name": record.persona,
        "agent": agent,
    }))
}

async fn create_agent(State(app): State<App>, body: axum::body::Bytes) -> Response {
    let asked = if body.is_empty() {
        json!({})
    } else {
        serde_json::from_slice(&body).unwrap_or(Value::Null)
    };
    let Some(asked) = asked.as_object() else {
        return ApiError::new(StatusCode::BAD_REQUEST, "bad json").into_response();
    };
    match make_agent(&app, asked).await {
        Ok(made) => Json(made).into_response(),
        Err((status, why)) => (status, Json(why)).into_response(),
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
    /// Its plans, each as the other Host's `GET /task-plans` gave it, with the documents of
    /// its goal as `GET /task-plan/document` gave them.
    #[serde(default)]
    plans: Vec<MovedPlan>,
    /// Its library, each artifact as the other Host's `GET /artifacts` gave it, a report
    /// with its versions as `GET /artifacts/<id>/revisions` gave them.
    #[serde(default)]
    library: Vec<MovedArtifact>,
    /// The picture it has of its own there, in base64, as `GET /avatars/<agent>` gave it.
    #[serde(default)]
    picture: Option<String>,
    /// The portraits it keeps there, each as the other Host's `GET /agent-portraits` listed
    /// it, with its picture in base64.
    #[serde(default)]
    portraits: Vec<Value>,
}

/// An artifact that comes with an agent.
#[derive(Debug, Deserialize)]
struct MovedArtifact {
    artifact: Value,
    #[serde(default)]
    revisions: Vec<Value>,
}

/// A plan that comes with an agent.
#[derive(Debug, Deserialize)]
struct MovedPlan {
    plan: Value,
    #[serde(default)]
    documents: Vec<Value>,
}

/// Takes an agent over from another Host: who it is, the conversation it had, and the tool's
/// session, which its next turn here continues.
async fn import_agent(
    State(app): State<App>,
    Json(request): Json<ImportRequest>,
) -> Result<Json<Value>, ApiError> {
    let field = |name: &str| request.agent[name].as_str().unwrap_or("").to_string();
    let backend_session = Some(field("backend_session_id")).filter(|session| !session.is_empty());
    // A picture that cannot be read is refused before anything is written: an agent that
    // arrives with another face than it was sent with is not what was asked for.
    let picture = clarp_kernel::personas::picture(request.picture.as_deref().unwrap_or(""))
        .map_err(|why| ApiError::new(StatusCode::BAD_REQUEST, why))?;
    // The portraits it keeps, the one it wears among them: that one is known by its
    // picture, which is the agent's. One whose picture cannot be read, or is no picture,
    // is left behind: the agent arrives, with the face it was sent with. A portrait whose
    // picture another has is that portrait, and no more are taken than an agent keeps.
    let mut portraits: Vec<Portrait> = Vec::new();
    let mut pictures: Vec<(String, Vec<u8>)> = Vec::new();
    for moved in &request.portraits {
        let said = |key: &str| moved[key].as_str().unwrap_or("").to_string();
        let Ok(Some((bytes, digest))) = clarp_kernel::personas::picture(&said("picture")) else {
            continue;
        };
        let Some(kind) = kept_portraits::picture_kind(&bytes) else {
            continue;
        };
        let twice = portraits.iter().any(|kept| kept.sha256 == digest);
        if said("portrait_id").is_empty() || twice || portraits.len() >= kept_portraits::MOST {
            continue;
        }
        portraits.push(Portrait {
            portrait_id: said("portrait_id"),
            media_asset_id: Some(said("media_asset_id")).filter(|asset| !asset.is_empty()),
            sha256: digest.clone(),
            mime_type: kind.to_string(),
            created_by: said("created_by"),
            created_at: moved["created_at"].as_i64().unwrap_or(0),
        });
        pictures.push((crate::fleet::picture_name(&digest), bytes));
    }
    let record = AgentRecord {
        portraits,
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
        // An agent that was muted or put away stays so where it goes, and keeps its voice
        // and its heartbeat (which the Python Host writes as a number).
        muted: request.agent["muted"] == true,
        heartbeat_enabled: matches!(request.agent["heartbeat_enabled"].as_i64(), Some(1))
            || request.agent["heartbeat_enabled"] == true,
        archived_at_ms: request.agent["archived_at"].as_i64(),
        voice_id: field("voice_id"),
        // A helper stays a helper, as its work stood. The agent that made it is named by
        // its id, whether or not it is on this Host.
        role: Some(field("role"))
            .filter(|role| role == "helper")
            .unwrap_or_default(),
        parent_agent_id: Some(field("parent_agent_id")).filter(|parent| !parent.is_empty()),
        helper_state: Some(field("helper_state")).filter(|state| !state.is_empty()),
        // Who it is there comes with it: its symbol, how it speaks (which the tool that
        // moves it reads off the persona of its name there), and its own picture.
        avatar_symbol: field("avatar_symbol"),
        personality: field("personality").trim().to_string(),
        avatar: picture.as_ref().map(|(_, digest)| digest.clone()),
        ..AgentRecord::default()
    };
    // The picture is kept before the agent that names it, and so are its portraits'.
    if let Some((bytes, digest)) = &picture {
        pictures.push((crate::fleet::picture_name(digest), bytes.clone()));
    }
    for (name, bytes) in &pictures {
        let kept = app.blobs.put(name, bytes);
        kept.map_err(|error| ApiError::new(StatusCode::SERVICE_UNAVAILABLE, error.to_string()))?;
    }
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
    // Then what it made and asked, and what it is committed to. An agent that arrives
    // without its library or its goals is not the agent that was asked for: it does not
    // stay either. The library first: a plan is shown as an artifact, and finds the one it
    // had.
    let library: Vec<(Value, Vec<Value>)> = request
        .library
        .into_iter()
        .map(|moved| (moved.artifact, moved.revisions))
        .collect();
    let elsewhere = library.iter().find_map(|(artifact, _)| {
        let id = artifact["artifact_id"].as_str()?;
        let directory = app.fleet.directory();
        let has = |other: &&Arc<Member>| {
            other.record.agent_id != member.record.agent_id
                && other.agent.view().books.artifacts.any(id).is_some()
        };
        directory.members.iter().find(has).map(|_| id.to_string())
    });
    let plans: Vec<(Value, Vec<Value>)> = request
        .plans
        .into_iter()
        .map(|moved| (moved.plan, moved.documents))
        .collect();
    let taken = match elsewhere {
        Some(id) => Ok(Err(format!(
            "another agent on this Host has an artifact with the id {id}"
        ))),
        None => member.agent.take_library(library).await,
    };
    let taken = match taken {
        Ok(Ok(artifacts)) => match member.agent.take_plans(plans).await {
            Ok(Ok(plans)) => Ok(Ok((artifacts, plans))),
            Ok(Err(why)) => Ok(Err(why)),
            Err(unavailable) => Err(unavailable),
        },
        Ok(Err(why)) => Ok(Err(why)),
        Err(unavailable) => Err(unavailable),
    };
    let (artifacts, plans) = match taken {
        Ok(Ok(taken)) => taken,
        failed => {
            let _ = app.fleet.remove(member.record.session.clone()).await;
            return Err(match failed {
                Ok(Err(why)) => ApiError::new(StatusCode::CONFLICT, why),
                Err(unavailable) => unavailable.into(),
                Ok(Ok(_)) => ApiError::new(StatusCode::INTERNAL_SERVER_ERROR, "the move failed"),
            });
        }
    };
    // It is here, and until its owner says it lives here it is on the other Host too. So it
    // takes no turn of its own yet: a goal that is due, or anything else that would start by
    // itself, waits for a resume. What its user says to it runs.
    let held = member.agent.hold().await.is_ok();
    Ok(Json(json!({
        "ok": true,
        "session": member.record.session,
        "agent_id": member.record.agent_id,
        "imported": imported,
        "artifacts": artifacts,
        "plans": plans,
        "held": held,
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
    /// `0`, `false` or `no`: with one header for a row's tools instead of every card whole.
    include_tool_details: Option<String>,
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
    let no = |said: &Option<String>| {
        let said = said.as_deref().map(str::to_lowercase);
        matches!(said.as_deref(), Some("0" | "false" | "no"))
    };
    if no(&query.include_tool_details) {
        wire::without_tool_details(&mut answer);
    }
    // An answer to another agent says whom it answers, by the name that agent has today.
    let directory = app.fleet.directory();
    wire::reply_to(&mut answer, &|agent_id| {
        let asked = directory.named(agent_id)?;
        Some((asked.record.persona.clone(), asked.record.session.clone()))
    });
    Ok(Json(answer))
}

/// `GET /message-tool-details?session=&message_id=`: the cards of one row whole, for an app
/// that was given their header and is asked to open them.
async fn tool_details(
    State(app): State<App>,
    Query(query): Query<HashMap<String, String>>,
) -> Result<Json<Value>, ApiError> {
    let said = |name: &str| query.get(name).map_or("", |given| given.trim());
    let (session, message_id) = (said("session"), said("message_id"));
    if session.is_empty() || message_id.is_empty() {
        return Err(ApiError::new(
            StatusCode::BAD_REQUEST,
            "session and message_id required",
        ));
    }
    let none = || ApiError::new(StatusCode::NOT_FOUND, "message not found");
    let member = app.member(session).ok_or_else(none)?;
    let version = member.agent.row(message_id.to_string()).await?;
    let row = wire::row(&version.ok_or_else(none)?);
    Ok(Json(json!({
        "message_id": message_id,
        "tools": row["tools"],
        "display_cells": row["display_cells"],
    })))
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
    Extension(principal): Extension<Principal>,
    Json(request): Json<SendRequest>,
) -> Response {
    sent_from(&app, &principal, request).await
}

/// `POST /orchestrator/route-delegation`: a message the app could not tell the agent of. It
/// is a send that asks the router of spoken messages.
async fn route_delegation(
    State(app): State<App>,
    Extension(principal): Extension<Principal>,
    Json(mut request): Json<SendRequest>,
) -> Response {
    request.orchestrator_fallback = Some(true);
    sent_from(&app, &principal, request).await
}

/// A message from an app: one that carries its call goes to the agent of the call that can
/// take speech, whoever it names; then it is a message like any other.
async fn sent_from(app: &App, principal: &Principal, mut request: SendRequest) -> Response {
    let call_id: String = python::strip(request.call_id.as_deref().unwrap_or(""))
        .chars()
        .take(64)
        .collect();
    // A message that says nothing is refused before its call is asked.
    if !call_id.is_empty() && !python::strip(&request.text).is_empty() {
        let caller = match principal {
            Principal::Device { device_id, .. } => device_id.as_str(),
            Principal::Administrator | Principal::Tools => clarp_kernel::calls::ADMINISTRATOR,
        };
        let named = match python::strip(&request.session) {
            "" => addressed::DEFAULT_SESSION,
            named => named,
        };
        let desk = app.calls.clone();
        match desk.route(app, (caller, &call_id, named)).await {
            Ok(Ok(session)) => request.session = session,
            Ok(Err(refused)) => {
                let status = StatusCode::from_u16(refused.status).unwrap_or(StatusCode::CONFLICT);
                return (status, Json(refused.body)).into_response();
            }
            Err(unavailable) => return ApiError::from(unavailable).into_response(),
        }
    }
    match send_message(app, request).await {
        Ok(sent) => Json(sent).into_response(),
        Err(refused) => refused.into_response(),
    }
}

/// Takes a message for an agent: what `POST /send` does, and what an agent's own tool does
/// when it writes to another agent.
pub(crate) async fn send_message(
    app: &App,
    request: SendRequest,
) -> Result<SendResponse, ApiError> {
    send_as(app, request, false).await
}

/// Takes a message the Host sends by itself: a heartbeat, a prompt at a set time, the task
/// of a fork. It is a message like any other, but for this: no app gave it an id, and the
/// apps are told of it under none.
pub(crate) async fn send_own(app: &App, request: SendRequest) -> Result<SendResponse, ApiError> {
    send_as(app, request, true).await
}

async fn send_as(app: &App, request: SendRequest, own: bool) -> Result<SendResponse, ApiError> {
    // What the Python Host refuses a send with, in its order and its words (plain text).
    let text = python::strip(&request.text).to_string();
    if text.is_empty() {
        return Err(ApiError::plain(StatusCode::BAD_REQUEST, "empty text"));
    }
    // The trace it travels under: the one it names, or one of the Host's.
    let trace_id = match request.trace_id.as_deref().map(python::strip) {
        Some(named) if !named.is_empty() => named.to_string(),
        _ => ids::hex(8),
    };
    // Whom it is for. What was typed, or dictated for the agent that is open, is for the
    // agent it names. What was dictated hands-free is for the agent whose name it begins
    // with, and without a name it stays with the agent that is open (`addressed`).
    let asked = match python::strip(&request.session) {
        "" => addressed::DEFAULT_SESSION,
        asked => asked,
    };
    let forced = request.force_session == Some(true) || request.hands_free != Some(true);
    let (member, text, chosen) = if forced {
        let Some(member) = app.member(asked) else {
            return Err(ApiError::plain(
                StatusCode::NOT_FOUND,
                "unknown forced agent",
            ));
        };
        (member, text, true)
    } else if request.orchestrator_fallback == Some(true) {
        // The app could not tell whom it is for and asks the router of spoken messages. This
        // Host has none: the answer of a Python Host whose router is off, which is how it
        // comes. Nothing is sent, and the app asks its user.
        return Ok(SendResponse {
            ok: true,
            trace_id,
            orchestrator: Some(
                json!({ "action": "disabled", "decision_id": null, "decision": null }),
            ),
            ..SendResponse::default()
        });
    } else {
        let directory = app.fleet.directory();
        let agents: Vec<(&str, &str)> = directory
            .members
            .iter()
            .map(|member| {
                (
                    member.record.session.as_str(),
                    member.record.persona.as_str(),
                )
            })
            .collect();
        let open = directory.focus.as_deref().and_then(|open| {
            let members = directory.members.iter();
            members
                .map(|member| &member.record)
                .find(|record| record.agent_id == open)
        });
        let open = open.map_or("", |record| record.session.as_str());
        let target = addressed::target(&text, (asked, open, addressed::DEFAULT_SESSION), &agents);
        let Some(member) = directory.by_session(&target.session).cloned() else {
            return Err(ApiError::plain(StatusCode::NOT_FOUND, "unknown agent"));
        };
        (member, target.text, target.by_name)
    };
    // Who it comes from. A message that names its sender is that agent's, whatever else it
    // says of itself: on the Python Host a client that names a sender may still say the
    // message is the user's, and the focus and the queue then treat an agent as a person
    // (issue 360 of the repository). Without a sender: the origin it names, when that is one
    // a client may name; otherwise the user. What the Host stamps itself (a janitor, a
    // leader's tick) nobody may claim.
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
    if named == "heartbeat" && app.config.heartbeats_disabled {
        // A Host whose heartbeats are off takes none from outside either: the Python Host's
        // words for it.
        return Err(ApiError::plain(
            StatusCode::CONFLICT,
            "Heartbeats are disabled on this Host",
        ));
    }
    let origin = match (sender.is_some(), named.as_str()) {
        (true, _) | (false, "agent") => from_agent(),
        (false, "oracle" | "schedule" | "automation" | "watcher" | "dreaming" | "heartbeat") => {
            Origin::Trigger {
                name: named.clone(),
            }
        }
        (false, _) => Origin::User,
    };
    let origin_is_user = origin == Origin::User;
    // What the Host sends by itself on its own schedule (the Python Host's
    // `ROUTINE_AUTOMATION_ORIGINS`).
    let routine = matches!(&origin, Origin::Trigger { name }
        if matches!(name.as_str(), "heartbeat" | "leader_tick" | "dreaming" | "janitor"));
    // Only a person moves the focus: the user, and the Oracle acting on what the user said
    // (the Python Host's `origins.FOCUS_ORIGINS`).
    let by_a_person =
        origin_is_user || matches!(&origin, Origin::Trigger { name } if name == "oracle");
    // A message to an agent that is busy waits its turn, as on the Python Host. What differs
    // is where it waits. A plain message is said: it is in the conversation at once. One sent
    // with `queue_if_busy` was left for later: it is in the queue until its turn starts, and
    // when it does not come from a person, a Stop holds it. (The kernel can also take a message
    // that replaces the running turn; nothing asks for it yet.)
    // A message from another agent of this Host never takes a running turn's place either:
    // it waits behind it like one left for later, and a Stop that came before does not hold
    // it (the Python Host's rule for a peer's message; one that asks to wait keeps its own).
    let from_peer = matches!(&origin, Origin::Agent { agent_id } if !agent_id.is_empty());
    let admission = match (request.queue_if_busy.unwrap_or(false), origin_is_user) {
        (false, _) if from_peer => Admission::Later,
        (false, _) => Admission::Queue,
        (true, true) => Admission::Later,
        (true, false) => Admission::Background,
    };
    // The id the app minted for it; where it minted none, its trace is its id, as on the
    // Python Host. It is filed under that id as it is where the id has the mark of a
    // message, as the phone's ids have, and with the mark before it otherwise.
    let minted = request.client_msg_id.as_deref().map(python::strip);
    let minted = minted.filter(|minted| !minted.is_empty());
    let (id, marked) = Message::filed(minted.unwrap_or(&trace_id));
    // The app names the sessions whose agents said something its user has not heard (the
    // first 64 names of a list; anything else names none).
    let unheard = match &request.unheard_audio_sessions {
        Some(Value::Array(sessions)) => {
            let named = sessions.iter().take(64).filter_map(Value::as_str);
            named
                .map(python::strip)
                .any(|session| session == member.record.session)
        }
        _ => false,
    };
    let message = Message {
        id,
        marked,
        unminted: own,
        text,
        origin,
        trace_id: Some(trace_id.clone()),
        sender,
        client: request.client.as_deref().and_then(client_name),
        // What a person sends is answered aloud unless the app says otherwise; anything
        // else only when it is asked for.
        speak: match request.synthesize_audio {
            Some(asked) => asked,
            None => origin_is_user,
        },
        unheard,
        // An agent the user spoke to in a call, not long ago, is told who else is in the
        // call and what was said since it last spoke.
        call: Some(app.calls.header(app, &member.record.session).await)
            .filter(|header| !header.is_empty()),
    };
    let writer = match &message.origin {
        Origin::Agent { agent_id } if !agent_id.is_empty() => Some(agent_id.clone()),
        _ => None,
    };
    if !routine {
        // For a minute this agent's answer is the one that is waited for: it is heard at
        // once, and the others raise their hands (`clarp_kernel::herald`). What the Host
        // sends by itself on its own schedule is waited for by nobody. Said before the
        // agent has the message, so that no answer is faster than this.
        app.mouth.sent(&member.record.session).await;
    }
    let sent = member.agent.send(message, admission).await?;
    if let Some(writer) = writer {
        // A helper that writes to the agent that made it has reported, and one that is
        // written to by it has more work.
        app.fleet
            .noted(writer, member.record.agent_id.clone())
            .await?;
    }
    if by_a_person && chosen {
        // Writing to an agent makes it the open one, without a word about it: an agent a
        // message names, by its session or at its beginning. A message that only stayed
        // with the agent that was open opens nothing.
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
        Some(record) => {
            // The user turned to this agent: what the herald held of it is played.
            app.mouth.opened(&record.session).await;
            Ok(Json(SelectResponse {
                ok: true,
                session: record.session,
            }))
        }
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
            ..Message::default()
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
                "id": message.client_id,
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

/// `GET /live?session=`: everything that is shown of an agent's turn as it runs, for a client
/// that has just opened the chat or missed an event. Nothing explains a tool's use on this
/// Host, and the answer says so.
async fn shown(
    State(app): State<App>,
    Query(query): Query<ShownQuery>,
) -> Result<Json<Value>, ApiError> {
    let session = query.session.unwrap_or_default();
    let member = app
        .member(session.trim())
        .ok_or_else(|| ApiError::new(StatusCode::NOT_FOUND, "unknown session"))?;
    let mut whole = member.agent.shown().await?;
    whole["tool_explanations"] = apps::explanations_told(&app);
    Ok(Json(whole))
}

#[derive(Debug, Deserialize)]
struct ShownQuery {
    session: Option<String>,
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

/// The agent a waiting message waits for, and the message's turn there: by the id its app
/// gave it, which is what the queue names it by.
async fn waiting_named(app: &App, id: &str) -> Result<Option<(Arc<Member>, TurnId)>, ApiError> {
    let directory = app.fleet.directory();
    for member in &directory.members {
        if member.agent.view().status.waiting.is_empty() {
            continue;
        }
        let (waiting, _) = member.agent.queue().await?;
        if let Some(found) = waiting.iter().find(|message| message.client_id == id) {
            return Ok(Some((member.clone(), found.turn)));
        }
    }
    Ok(None)
}

fn not_waiting() -> ApiError {
    ApiError::new(StatusCode::NOT_FOUND, "queued message not found")
}

/// `DELETE /turn-queue/<id>`: its user takes a waiting message back. It never runs, and it
/// was never in the conversation. A hold that held nothing else is over.
async fn take_back(
    State(app): State<App>,
    Path(id): Path<String>,
) -> Result<Json<Value>, ApiError> {
    let Some((member, turn)) = waiting_named(&app, &id).await? else {
        return Err(not_waiting());
    };
    // It may have gone to run since it was looked for.
    let (waited, _) = member.agent.withdraw(turn, "user".to_string()).await?;
    if !waited {
        return Err(not_waiting());
    }
    Ok(Json(json!({ "ok": true })))
}

/// `PUT /turn-queue/<id>` with `{text}`: its user changes the words of a waiting message. Its
/// turn answers the new words. A message that waits its turn in the conversation was said,
/// and what was said stands: on this Host that is every message that was not left for later.
async fn reword(
    State(app): State<App>,
    Path(id): Path<String>,
    body: axum::body::Bytes,
) -> Result<Json<Value>, ApiError> {
    // What is asked is read before what waits is looked for, as on the Python Host.
    let said: Value = serde_json::from_slice(&body).unwrap_or(Value::Null);
    let text = said["text"].as_str().map(python::strip).unwrap_or_default();
    if text.is_empty() {
        return Err(ApiError::new(StatusCode::BAD_REQUEST, "text required"));
    }
    let Some((member, turn)) = waiting_named(&app, &id).await? else {
        return Err(not_waiting());
    };
    match member.agent.reword(turn, text.to_string()).await? {
        Reworded::Done => Ok(Json(json!({ "ok": true }))),
        Reworded::NotWaiting => Err(not_waiting()),
        Reworded::InConversation => Err(ApiError::new(
            StatusCode::CONFLICT,
            "message is already in the conversation",
        )),
    }
}

/// `POST /turn-queue/<id>/send`: its user sends one waiting message now. It runs although
/// the queue is held, and what else waits stays held; while a turn runs nothing is sent
/// past it. Answered as a send that did not have to wait.
async fn send_by_hand(
    State(app): State<App>,
    Path(id): Path<String>,
) -> Result<Response, ApiError> {
    let Some((member, turn)) = waiting_named(&app, &id).await? else {
        return Err(not_waiting());
    };
    match member.agent.send_now(turn, "user".to_string()).await? {
        ByHand::Sent => {
            let answer = json!({
                "ok": true,
                "session": member.record.session,
                "queued": false,
                "queue_depth": 0,
                "queue_revision": 0,
            });
            Ok((StatusCode::ACCEPTED, Json(answer)).into_response())
        }
        ByHand::NotWaiting => Err(not_waiting()),
        ByHand::AtWork => Err(ApiError::new(
            StatusCode::CONFLICT,
            "agent is still working",
        )),
    }
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
    Extension(asker): Extension<Asker>,
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
        // A code that is none is a failure to say who one is, and counted as one: codes
        // are not to be guessed at.
        Err(why) => match app.door.refused(&asker.source, "too many pairing failures") {
            Some(wait) => Ok(wait),
            None => Err(ApiError::new(StatusCode::CONFLICT, why)),
        },
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
    /// The device will reach this Host through its relay: the address is the relay's for
    /// this Host.
    #[serde(default)]
    relay: bool,
}

/// The administrator has a code made for a device.
async fn pairing_issue(
    State(app): State<App>,
    Json(request): Json<IssueRequest>,
) -> Result<Json<Value>, ApiError> {
    let scope = Scope::parse(&request.scope)
        .ok_or_else(|| ApiError::new(StatusCode::BAD_REQUEST, "scope must be full or limited"))?;
    let through_the_relay = if request.relay {
        let none = "enable the managed relay before pairing through it";
        let relay = app.connector.pairing_url();
        Some(relay.ok_or_else(|| ApiError::new(StatusCode::BAD_REQUEST, none))?)
    } else {
        None
    };
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
    let url = through_the_relay.or(request.url);
    if let Some(url) = url.filter(|url| !url.trim().is_empty()) {
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
    if app.fleet.revoke(device_id.clone()).await? {
        // What the device still listens to ends now, not when it next asks.
        let _ = app.door.revoked.send(device_id);
        Ok(Json(json!({ "ok": true })))
    } else {
        Err(ApiError::new(StatusCode::NOT_FOUND, "device not found"))
    }
}

// ---- the stream of events --------------------------------------------------------------------

#[derive(Debug, Deserialize)]
struct EventsQuery {
    last_event_id: Option<u64>,
    /// The agents whose turns the connection wants to be shown as they run, by session or
    /// id, with commas between them: `*` for every one, nothing for the status lines alone.
    /// A connection that does not say `live` at all is sent no `live` events.
    live: Option<String>,
}

/// What a `live` event is sent as: no number, and no name, as every other event without one.
fn shown_event(json: &str) -> SseEvent {
    SseEvent::default().data(json)
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
    // The turns the connection asked to be shown as they run. What they show is never kept:
    // nothing of it is among what was missed.
    let wanted: Option<HashSet<String>> = query.live.map(|live| {
        live.split(',')
            .map(str::trim)
            .filter(|name| !name.is_empty())
            .map(str::to_string)
            .collect()
    });
    let watched = wanted.map(|wanted| (wanted, app.hub.watch()));
    // First what the client missed, then the word to look at everything: whatever happened
    // while it was away for longer than the hub remembers, the snapshot and the conversations
    // will tell.
    let mut first = VecDeque::with_capacity(missed.len() + 2);
    first.extend(missed.iter().map(|published| numbered(published)));
    first.push_back(unnumbered(&json!({ "type": "agent-roster" })));
    let clock = app.clock.clone();
    let stream = stream::unfold(
        (first, live, watched),
        move |(mut first, mut live, mut watched)| {
            let clock = clock.clone();
            async move {
                // A heartbeat is due this long after the last thing that was sent, whatever was
                // heard meanwhile and was not for this connection.
                let beat_at = tokio::time::Instant::now() + HEARTBEAT;
                loop {
                    if let Some(event) = first.pop_front() {
                        return Some((Ok(event), (first, live, watched)));
                    }
                    // What a turn shows, for a connection that asked for it; for one that did
                    // not, a wait that never ends.
                    let shown = async {
                        match watched.as_mut() {
                            Some((_, shown)) => shown.recv().await,
                            None => std::future::pending().await,
                        }
                    };
                    let heard = tokio::time::timeout_at(beat_at, async {
                        tokio::select! {
                            published = live.recv() => Heard::Event(published),
                            shown = shown => Heard::Shown(shown),
                        }
                    })
                    .await;
                    let event = match heard {
                        Ok(Heard::Event(Ok(published))) => numbered(&published),
                        Ok(Heard::Shown(Ok(shown))) => {
                            let wanted = watched.as_ref().map(|(wanted, _)| wanted);
                            match wanted.and_then(|wanted| shown.for_one(wanted)) {
                                Some(json) => shown_event(json),
                                // Not for this connection.
                                None => continue,
                            }
                        }
                        // A client that fell too far behind is cut off. It reconnects, and what
                        // it missed is given to it then; of what turns showed it asks anew.
                        Ok(
                            Heard::Event(Err(RecvError::Lagged(_) | RecvError::Closed))
                            | Heard::Shown(Err(RecvError::Lagged(_) | RecvError::Closed)),
                        ) => return None,
                        Err(_) => {
                            let beat = json!({ "type": "heartbeat", "ts": clock.now_ms() });
                            SseEvent::default()
                                .event("heartbeat")
                                .data(beat.to_string())
                        }
                    };
                    return Some((Ok(event), (first, live, watched)));
                }
            }
        },
    );
    Sse::new(stream)
}

/// What a connection that listens heard: an event every connection is sent, or something a
/// turn shows.
enum Heard {
    Event(Result<Arc<Published>, RecvError>),
    Shown(Result<Arc<crate::hub::Shown>, RecvError>),
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use axum::http::HeaderValue;

    use super::*;

    #[test]
    fn a_token_is_read_from_the_header_and_from_the_cookie() {
        assert_eq!(bearer("Bearer h-token"), Some("h-token"));
        assert_eq!(bearer("  bearer   h-token  "), Some("h-token"));
        assert_eq!(bearer("BEARER h-token"), Some("h-token"));
        assert_eq!(bearer("Bearer"), None);
        assert_eq!(bearer("Basic h-token"), None);
        assert_eq!(bearer("Bearerh-token"), None);
        assert_eq!(bearer(""), None);
        assert_eq!(cookie("x=1; claude_pwa_token=c-token"), Some("c-token"));
        assert_eq!(cookie("claude_pwa_token= c-token ; y=2"), Some("c-token"));
        assert_eq!(cookie("x=1; y=2"), None);
        assert_eq!(cookie(""), None);
    }

    #[test]
    fn a_request_is_counted_for_whom_a_proxy_on_this_machine_says_it_asks() {
        let from = |peer: Option<&str>, headers: &[(&str, &str)]| {
            let mut request = Request::new(axum::body::Body::empty());
            for (name, value) in headers {
                let name = axum::http::HeaderName::from_bytes(name.as_bytes()).unwrap();
                request
                    .headers_mut()
                    .insert(name, HeaderValue::from_str(value).unwrap());
            }
            if let Some(peer) = peer {
                let peer: SocketAddr = format!("{peer}:4711").parse().unwrap();
                request.extensions_mut().insert(ConnectInfo(peer));
            }
            asked(&request)
        };
        // A program on this machine, and one elsewhere.
        assert_eq!(
            from(Some("127.0.0.1"), &[]),
            (true, "127.0.0.1".to_string())
        );
        assert_eq!(from(Some("10.0.0.5"), &[]), (false, "10.0.0.5".to_string()));
        // A proxy on this machine hands on what somebody else asked.
        let handed_on = [("x-forwarded-for", "203.0.113.7")];
        assert_eq!(
            from(Some("127.0.0.1"), &handed_on),
            (false, "203.0.113.7".to_string())
        );
        // A caller from elsewhere is not believed about whom it asks for.
        assert_eq!(
            from(Some("10.0.0.5"), &handed_on),
            (false, "10.0.0.5".to_string())
        );
        // What the relay's connector hands in has no socket: it is never local, whatever
        // it carries, and it is counted for whom the relay says it asks.
        assert_eq!(from(None, &[]), (false, "127.0.0.1".to_string()));
        assert_eq!(from(None, &handed_on), (false, "203.0.113.7".to_string()));
        // A page loaded from elsewhere that asks this machine is not this machine.
        assert!(!from(Some("127.0.0.1"), &[("origin", "https://evil.example")]).0);
        assert!(from(Some("127.0.0.1"), &[("origin", "http://localhost:7690")]).0);
    }

    #[test]
    fn the_door_tells_an_asker_that_fails_too_often_to_wait() {
        let door = Door::default();
        for _ in 0..20 {
            assert!(door.refused("203.0.113.7", "too many").is_none());
        }
        let wait = door.refused("203.0.113.7", "too many").unwrap();
        assert_eq!(wait.status(), StatusCode::TOO_MANY_REQUESTS);
        let seconds = wait.headers()[header::RETRY_AFTER].to_str().unwrap();
        assert!(matches!(seconds, "60" | "59"), "{seconds}");
        // Another asker is not kept waiting for it.
        assert!(door.refused("198.51.100.1", "too many").is_none());
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
            (Method::GET, "/dirs", true),
            (Method::GET, "/past-sessions", true),
            (Method::GET, "/personalities/settings", true),
            (Method::GET, "/automation-settings", true),
            (Method::GET, "/agent-files", true),
            (Method::GET, "/agent-file", true),
            (Method::GET, "/backend-auth", true),
            (Method::GET, "/orchestrator/settings", true),
            (Method::GET, "/orchestrator", true),
            (Method::GET, "/calls", true),
            (Method::GET, "/voice-humanness", false),
            (Method::GET, "/persona-avatars/persona-1", false),
            (Method::POST, "/personas", true),
        ] {
            assert_eq!(
                out_of_scope(&limited, &method, path),
                kept_out,
                "{method} {path}"
            );
            assert!(!out_of_scope(&full, &method, path), "{method} {path}");
            assert!(!out_of_scope(&Principal::Administrator, &method, path));
            // The token of an agent's tools opens none of what the apps read and do.
            assert!(
                out_of_scope(&Principal::Tools, &method, path),
                "{method} {path}"
            );
        }
    }

    #[test]
    fn an_agents_tools_are_asked_for_from_this_machine_and_by_no_device() {
        let full = Principal::Device {
            device_id: "device_2".to_string(),
            scope: Scope::Full,
        };
        let path = "/tools/run";
        assert!(!out_of_scope(&Principal::Tools, &Method::POST, path));
        assert!(!out_of_scope(
            &Principal::Administrator,
            &Method::POST,
            path
        ));
        assert!(out_of_scope(&full, &Method::POST, path));
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
