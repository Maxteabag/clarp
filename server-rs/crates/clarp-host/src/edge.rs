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
use clarp_kernel::contacts::{self, Launch};
use clarp_kernel::conversation::{Message, Sender};
use clarp_kernel::coordination;
use clarp_kernel::helpers;
use clarp_kernel::jobs::{Job, JobCommand};
use clarp_kernel::journal::Reworded;
use clarp_kernel::kept_portraits::{self, Portrait};
use clarp_kernel::presence::Presence;
use clarp_kernel::python;
use clarp_kernel::request_body;
use clarp_kernel::trust::{self, FailureLimiter};
use clarp_kernel::turn::{Admission, Origin, TurnId};
use clarp_ports::blobs::Blobs;
use clarp_ports::clock::Clock;
use clarp_protocol::requests::{CreateAgentRequest, SendRequest, SessionRequest, StopRequest};
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

/// What this desk keeps, declared once (`kept.rs`): the chat each client last opened, read by key alone (one
/// for every client that ever opened one, and none is listed), judged as the object it reads.
pub(crate) const KEPT: &[crate::kept::Kind] = &[crate::kept::Kind::book(
    OPENED,
    crate::kept::Key::Plain,
    crate::kept::fits::<serde_json::Map<String, Value>>,
)
.when_read()];

/// The contract this Host speaks: the Python Host's number on the day this was written. What
/// this Host does not have yet is absent from `FEATURES`, which is how an app learns it.
pub const HOST_CONTRACT: u64 = 126;
pub const MIN_IOS_CONTRACT: u64 = 1;

/// The features of the contract this Host has, each with the contract number that
/// introduced it (the Python Host's `FEATURE_CONTRACTS`): the apps gate on a feature being
/// listed, some on it being listed with its number. A feature is listed here when everything
/// it names is built and held to the Python Host; what is half there is not listed.
const CHIP: &str = "live_activity_push";
const SEATS: &str = "hotseat_desk";

/// The features this Host has now: all of [`FEATURES`], without what it cannot do as it is
/// set up.
fn features(app: &App) -> impl Iterator<Item = &'static (&'static str, u64)> {
    let seats = app.seats.told() && !crate::seats::turned_off(app);
    let actions = app.config.accounts == clarp_kernel::recovery::Accounts::Select && seats;
    let chip = app.chip.pushes();
    let fcm = app.android.configured();
    let skills = crate::managed_skills::keeps(&app.config);
    FEATURES.iter().filter(move |(feature, _)| {
        (*feature != CHIP || chip)
            && (*feature != SEATS || seats)
            && (*feature != "hotseat_desk_actions" || actions)
            && (*feature != "backend_auth" || crate::logins::sign_in::keeps(app))
            && (!matches!(*feature, "push_fcm" | "live_activity_fcm") || fcm)
            && (*feature != "managed_skills" || skills)
    })
}

pub const FEATURES: &[(&str, u64)] = &[
    // What agents make and ask: the library, the inbox, requests and every way they end.
    // `GET /attention/inbox`: what agents made, a page at a time.
    ("attention_index", 2),
    ("artifact_summary_v1", 73),
    // Render specs: a small form drawn by the app from a closed catalog, answered once; a
    // spec only for an exact client name at its minimum. Contract 111 adds per-client
    // negotiation and administrator export while retaining the original feature number.
    ("render_spec", 102),
    ("flat_artifact_responses", 1),
    ("artifacts", 1),
    // What an agent publishes: `POST /media`, an agent's gallery, a file fetched whole and
    // in parts.
    ("media", 1),
    ("media_manage", 113),
    // A report: a page of HTML an agent hands its user, served behind its policy; what it
    // was, version by version; and the origins its page may reach.
    ("html_reports", 19),
    ("html_report_revisions", 49),
    ("html_form_network", 59),
    ("html_form_events", 51),
    ("html_form_reply_audio", 50),
    // Background jobs, and a cancel that names the run it means.
    ("background_jobs", 1),
    ("provider_background_tasks", 24),
    ("diagnostics", 1),
    // Pairing codes and devices with tokens of their own.
    ("pairing", 1),
    // Calls: the Host's record of who is in a phone's call, on hold and on the floor
    // (`/calls`, the `group-call` event, `call_id` of a send; `docs/group-calls.md`).
    ("group_calls", 60),
    // Rings: an agent calls its owner, and answering starts a call in which the agent's
    // opening line is heard first (`/calls/ring`, the `agent-ring` event and push;
    // `docs/agent-rings.md`). The ring whose line is said, and not the one that listens
    // first.
    ("agent_rings", 64),
    // A ring held as a live voice call: placed with what its producer says of an event,
    // answered with a grant, and held on a socket of its own between the phone and a voice
    // (`conversation_mode: agent_live_v1`, `GET /calls/ring/live`, its close and its switch).
    ("agent_ring_live", 71),
    // A phone switches rings off for itself (`/calls/ring/device`).
    ("agent_ring_device_optout", 72),
    ("agent_ring_listen_first", 74),
    ("call_transcription_policy", 74),
    // The user calls an agent that takes calls live (`POST /calls/live`; `call_mode` of an
    // agent in the list).
    ("agent_call_live", 76),
    // The Oracle: the voice a user speaks with hands-free. Its calls of the second kind
    // (`GET /oracle/v2`, with what `GET /oracle/status` says of them) and the work it hands
    // to agents (`/oracle/delegations`); who its contact is (`/oracle/contact`); a call in
    // which everything goes to the contact; a call that goes on in the thread of its owner
    // and contact, or begins anew; and the cues of sound a call plays. Its calls of the
    // first kind are not built, and the status says so. A call is put through to an agent
    // and back (`oracle_handoff`; `docs/oracle-handoff.md`). Its second voice is Gemini Live.
    ("oracle", 1),
    ("oracle_contact", 7),
    ("oracle_direct_contact", 8),
    ("oracle_context_memory", 9),
    ("oracle_earcons", 15),
    ("oracle_handoff", 22),
    ("oracle_gemini_voice", 36),
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
    // Goals kept current by the Goal keeper: `plan.goal.updater` beside the owner's own
    // checkpoint, `plan.updater`, a criterion set aside, a paused goal's step that keeps
    // its place.
    ("goal_ledger", 52),
    ("goal_updater", 96),
    // A receipt for everything the keeper did with a goal: `plan.updater.last_receipt`,
    // `receipts` on the keeper's runs, `clarp-goal receipts`.
    ("goal_keeper_receipts", 97),
    // A goal older than the keeper's first look back is read from its start once, and a
    // goal that has stalled says so, by a rule.
    ("goal_keeper_stalled", 98),
    ("background_job_cancel_generation", 23),
    // A send that says it came from the desk is not pushed to the phone.
    ("desktop_send_quiet_push", 27),
    ("push_fcm", 88),
    ("agent_live_fcm", 94),
    ("live_activity_fcm", 95),
    ("decision_lifecycle", 28),
    // A session the owner names (`[agents] decision_on_behalf_sessions`) answers a request in his
    // place, in its own turn: the Host knows the turn by the token it gave it (`turn_identity.rs`).
    ("decision_on_behalf", 79),
    // A quiet agent (a janitor) never reaches its owner by itself: its row says so, its turns tell
    // nobody, and it is in none of the lists the roster's members make.
    ("quiet_agents", 56),
    // An agent nobody named takes a free contact of its tool's pool or one minted for it
    // (`POST /agents` with `auto_contact` or `mint_contact`), and a running agent is given one
    // (`POST /agent-assign`). A minted contact has no picture here yet: a Python Host with
    // pictures off or no key mints the same contact without one and lists this all the same.
    ("auto_contact_minting", 85),
    // A Stop says who stopped the agent and why, an agent's own turn known by its token; and a
    // turn nothing runs is repaired, which here gives nothing back: nothing can be busy with
    // no process behind it (`controls.rs`).
    ("turn_slot_repair", 61),
    ("text_input_requests", 29),
    ("artifact_pins", 38),
    // `POST /devices` takes a phone's own grant, and the Host pushes by it.
    ("push_grants", 39),
    // Agents that make agents: who made an agent, how a helper's work stands
    // (`/agent-helper-state`), and what an agent's row says of the ones it made.
    ("helper_agents", 13),
    ("claude_subagent_cells", 14),
    // An agent forks itself into helpers that start from its conversation and each get a
    // task (`POST /agents/<session>/fork`). Each child starts from the conversation as
    // words (`fork: seed`): a tool's own copy of a conversation is not made here yet.
    ("agent_fork", 41),
    // `GET /agents/snapshot?include_archived=0` and `GET /agents/archived`.
    ("snapshot_without_archived", 82),
    // `DELETE /devices` and `GET /devices/push-tokens`.
    ("push_unregister", 93),
    ("launch_directories", 1),
    // An agent's row says that its tool's account is used up (`backend_quota`), and the apps
    // are told to read the rows again when that changes (`agent-roster`, `backend-quota`):
    // for Codex by what Codex writes of its account and by a turn that failed for a usage
    // limit, for Claude by what Claude Code says as it runs. No provider is asked.
    ("backend_quota", 3),
    // A tool signed in and out from an app (`/backend-auth/login`, `/login-code`, `/logout`).
    // Listed only by the Host that keeps the machine's tools (`--accounts select`): every
    // other refuses the change.
    ("backend_auth", 1),
    // The desk of the machine's account seats (`GET /hotseat/desk`): every saved account of
    // the agents' tools with its windows, its state and its clocks, read with the owner's
    // own tool when an app asks for a reading. Listed only by a Host that was told the tool.
    (SEATS, 84),
    ("hotseat_desk_actions", 86),
    // How the account seats stand as they were last read (`GET /hotseat/status`,
    // `clarp.hotseat-status/1`): served from what was kept, nothing read for it. Listed by
    // every Host, as there: the strict answer of a Host without the owner's tool says so.
    ("accounts_hotseat", 78),
    // Signing OpenCode and DeepSeek in to their providers of models from an app
    // (`/harness-providers`); with it an app lists every harness's login
    // (`/backend-auth?harnesses=all`). A change is made only by a Host that keeps this
    // machine's tools (`--accounts select`); any other refuses it in words.
    ("harness_model_providers", 75),
    ("transcription", 1),
    // What an agent says aloud (`/tts/providers`, `/voices`, `/voice-catalog`,
    // `/voice-preview`, the clips): five built-in engines, among them Google's Chirp 3 HD
    // voices and its refusal of an old adapter that took its id. Custom speech adapters are
    // not run here (docs/differences.md).
    ("tts", 1),
    ("controller_narration", 8),
    ("controller_actions", 63),
    ("remote_action", 1),
    ("tts_provider_google", 69),
    ("tts_google_id_guard", 70),
    // The clips of a reply, to play it again (`GET /clips/message`).
    ("message_audio_replay", 1),
    // The use of a tool explained in plain words (`POST /tool-explanations`): by a template,
    // by what was learned (`GET /tool-explanations/stats`), or by the model of the janitor
    // that explains, each with its source and, asked for, its provenance; the owner's
    // setting (`/tool-explanations/settings`), off here until the owner turns it on.
    ("tool_explanations", 1),
    ("tool_explanation_sources", 10),
    ("tool_explanation_stats", 20),
    ("tool_explanation_setting", 43),
    // How speech in is set from an app (`/transcription-providers`): the Host's language,
    // an agent's own language or set of up to three, an agent's own engines in the order
    // they are tried, Google's Chirp 3 among them; and the chat a recording is for, the one
    // its client names (`X-Transcription-Session`), else the one it last opened. Another
    // engine for long recordings is not built here (docs/differences.md).
    ("transcription_language", 31),
    ("transcription_agent_language", 32),
    ("transcription_agent_language_set", 53),
    ("transcription_session_header", 33),
    ("transcription_agent_engine", 34),
    ("transcription_agent_engine_fallback", 37),
    ("transcription_google_chirp", 35),
    // What a Host's janitors run: a scheduled task (`scheduled-prompt`), and the adaptive
    // interval every run of an interval ends with (worked, idle, failed; `cadence`).
    ("janitor_scheduled_tasks", 58),
    ("janitor_adaptive_intervals", 57),
    // A turn that fails is told: `interrupted` with its reason, its message and the
    // provider's own words, a row under its message, and `account_plan`.
    ("turn_failure_reason", 54),
    // Where the owner is (`/location`, an agent asks for it) and an event an agent asks to
    // be put in the owner's calendar (`/calendar/request`).
    ("location", 1),
    ("calendar", 1),
    // An agent whose user is with another raises its hand (`/herald/settings`).
    ("herald", 1),
    // How the agents speak: the owner's personalities and the humanness of a spoken reply.
    ("personalities", 1),
    // The skills kept installed for the tools (`/managed-skills`): listed only by a Host that
    // keeps them, one started with `--accounts select` and a tree of skills.
    ("managed_skills", 1),
    ("agent_portraits", 1),
    ("voice_humanness", 46),
    // The router of spoken messages (`/orchestrator/*`).
    ("orchestrator", 1),
    // Helpers and background processes counted apart, and one job's detail
    // (`GET /background-jobs/<id>`).
    ("background_process_detail", 16),
    // OpenCode's and DeepSeek's efforts, model by model.
    ("opencode_model_efforts", 25),
    // `GET /status` `runtime`: true of this Host, where nothing is drained: a turn lives in
    // its shim across a restart of the Host (docs/differences.md).
    ("runtime_drain_status", 62),
    // A quiet stream of events is sent a heartbeat as an event of its own, which EventSource
    // shows (`event: heartbeat`).
    ("sse_heartbeat", 44),
    // A work label that outlived the work behind it is retired: a job nobody can be asked
    // after goes stale and then is lost, a helper that sits idle has reported, and an idle
    // agent's own label older than two hours is taken back. The label checker among the
    // janitors is made here and does not run (docs/differences.md).
    ("stale_work_labels", 17),
    // The Host's own terminal (`GET /server-terminal`, a socket): a login shell in the
    // home of whoever the Host runs as, for the administrator and a device of full scope.
    ("server_home_terminal", 48),
    // What a user send's delivery is, on `GET /log` and its feed (`delivery_*`), and the
    // `delivery-updated` hint: in the in-process mode, the Host owning its turns itself.
    ("send_delivery_state", 81),
    ("waited_send_order", 115),
    // GET /turn-queue reads the same priority order the mailbox dispatches.
    ("queue_user_first", 118),
    // What the user's last compaction of an agent did, on its row (`last_compaction`).
    ("compaction_outcome", 107),
    ("voice_profile", 109),
    ("voice_pool", 123),
    ("voice_provider_maps", 122),
    ("speech_outcome", 109),
    ("pairing_identity", 114),
    ("hidden_coordination", 120),
    ("coordination_working_state", 124),
    ("agent_moved", 125),
    ("teams", 1),
];

/// After this long without an event, a stream is sent a heartbeat.
const HEARTBEAT: Duration = Duration::from_secs(10);

/// Everything a handler can reach.
#[derive(Clone)]
pub struct App {
    pub capture: Arc<crate::diagnostics::Capture>,
    pub controller_actions: Arc<crate::controller_actions::Desk>,
    pub form_event_writer: Arc<tokio::sync::Mutex<()>>,
    /// Held by a change to the skills this Host keeps installed, from its reading of the
    /// tools' folders to its keeping of the chosen list.
    pub skill_links: Arc<tokio::sync::Mutex<()>>,
    pub config: Arc<Config>,
    /// What makes and reads the token each turn is given, which tells whose turn calls.
    pub turns: Arc<crate::turn_identity::Signer>,
    pub hub: Arc<Hub>,
    pub fleet: FleetHandle,
    pub clock: Arc<dyn Clock + Send + Sync>,
    /// Which Host this is, for an app that knows several.
    pub server_id: String,
    /// The numbers for changes the whole Host counts in one row.
    pub revisions: Arc<Revisions>,
    /// The way to the phones that are not listening.
    pub pusher: Pusher,
    pub android: Arc<crate::fcm::Sender>,
    pub android_chip: Arc<crate::android_chip::Desk>,
    /// The desks that have said their user sits at them: no phone is paged meanwhile.
    pub desks: Arc<Mutex<Presence>>,
    /// The bytes of the files agents publish.
    pub blobs: Arc<dyn Blobs>,
    pub gallery: crate::gallery::Writer,
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
    pub teams: Arc<crate::teams::Desk>,
    /// The heartbeat: its settings, and what is remembered of each agent it wakes.
    pub heartbeat: Arc<crate::heartbeat::Desk>,
    /// The janitors: what is kept of each agent that has a job of maintenance.
    pub fallbacks: Arc<crate::model_fallbacks::Desk>,
    pub janitors: Arc<crate::janitors::Desk>,
    /// The router of spoken messages: what it decided, and what it holds.
    pub router: Arc<crate::router::Desk>,
    /// The explainer of the use of tools: what it learned, and what waits for its model.
    pub explain: Arc<crate::explain::Desk>,
    /// The Oracle's desk: its contact and the voice it speaks with are read and written in turn.
    pub oracle: Arc<crate::oracle::Desk>,
    /// The Goal keeper: its own state of the goals it keeps, and its receipts.
    pub keeper: Arc<crate::keeper::Desk>,
    /// The Quota keeper: when it looks next, and its receipts.
    pub label_checker: Arc<crate::label_checker::Desk>,
    pub quota_keeper: Arc<crate::quota_keeper::Desk>,
    /// The audio bookkeeper: what was seen of clips, written down or not yet.
    pub audio: Arc<crate::audio::Desk>,
    /// What the tools said is left of their accounts.
    pub limits: Arc<crate::quota::Desk>,
    /// The desk of the machine's account seats, as the owner's own tool says them.
    pub seats: Arc<crate::seats::Desk>,
    pub pools: Vec<Arc<crate::pool::Service>>,
    /// What the tools were last found to have of models, for an app's picker.
    pub models: Arc<crate::models::Shelf>,
    /// The sign-ins of the tools started from an app, where this Host keeps the tools.
    pub sign_ins: Arc<crate::logins::sign_in::Desk>,
    /// What the door keeps count of: who failed to say who they are, and whose right to
    /// speak was taken away.
    pub door: Arc<Door>,
    /// The relay a phone reaches this Host through, when it has one.
    pub connector: Arc<crate::connector::Connector>,
}

/// What the door keeps count of.
pub struct Door {
    failures: Mutex<FailureLimiter>,
    identity_reads: Mutex<FailureLimiter>,
    since: std::time::Instant,
    /// The devices whose right to speak was taken away, told to the streams they have open.
    revoked: tokio::sync::broadcast::Sender<String>,
}

impl Default for Door {
    fn default() -> Self {
        Self {
            failures: Mutex::default(),
            identity_reads: Mutex::new(FailureLimiter::new(
                trust::IDENTITY_READS_MOST,
                trust::FAILURES_WINDOW_S,
                trust::FAILURE_SOURCES_MOST,
            )),
            since: std::time::Instant::now(),
            revoked: tokio::sync::broadcast::channel(64).0,
        }
    }
}

impl Door {
    /// Invalid authentication and pairing attempts spend the same failure budget.
    fn refused(&self, source: &str, too_many: &str) -> Option<Response> {
        self.spend(&self.failures, source, too_many)
    }

    /// An identity read spends only its own budget.
    fn identity_refused(&self, source: &str) -> Option<Response> {
        self.spend(&self.identity_reads, source, "too many identity reads")
    }

    fn spend(
        &self,
        budget: &Mutex<FailureLimiter>,
        source: &str,
        too_many: &str,
    ) -> Option<Response> {
        let now = self.since.elapsed().as_secs_f64();
        let wait = budget
            .lock()
            .unwrap_or_else(|held| held.into_inner())
            .failure(source, now);
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
    /// What is answered under `error` where it is more than words: a render spec's refusal.
    detail: Option<Value>,
    /// Says to the caller that the same request may well be met when it is asked again.
    retryable: bool,
    body: Option<Value>,
}

impl ApiError {
    pub(crate) fn new(status: StatusCode, error: impl Into<String>) -> Self {
        Self {
            status,
            error: error.into(),
            plain: false,
            detail: None,
            retryable: false,
            body: None,
        }
    }

    /// An error the caller is told it may ask again for (`"retryable": true` beside `error`), as
    /// the Python Host answers a turn's identity it could not look up.
    pub(crate) fn retryable(status: StatusCode, error: impl Into<String>) -> Self {
        Self {
            retryable: true,
            ..Self::new(status, error)
        }
    }

    /// An error whose `error` is this value; `error` says it in words, for a tool.
    pub(crate) fn detailed(status: StatusCode, error: impl Into<String>, detail: Value) -> Self {
        Self {
            detail: Some(detail),
            ..Self::new(status, error)
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
            detail: None,
            retryable: false,
            body: None,
        }
    }

    /// What is answered under `error`, where it is more than words.
    pub(crate) fn detail(&self) -> Option<&Value> {
        self.detail.as_ref()
    }
}

/// A JSON body field read as text, as in the Python Host's R146 reader.
pub(crate) fn body_text<'a>(
    body: &'a serde_json::Map<String, Value>,
    field: &str,
) -> Result<&'a str, ApiError> {
    match body.get(field).filter(|value| python::truthy(value)) {
        None => Ok(""),
        Some(Value::String(text)) => Ok(python::strip(text)),
        Some(_) => Err(ApiError::new(
            StatusCode::BAD_REQUEST,
            format!("{field} must be text"),
        )),
    }
}

impl From<Unavailable> for ApiError {
    fn from(unavailable: Unavailable) -> Self {
        use clarp_kernel::agent::Refusal;
        let body = match unavailable.1 {
            Some(Refusal::Moved(mark)) => json!({
                "ok": false, "error": unavailable.0, "moved_to": mark.to, "moved_at": mark.at,
            }),
            Some(Refusal::AlreadyMoved(mark)) => json!({
                "ok": false, "error": unavailable.0,
                "detail": format!("already moved to {} ({})", mark.to.host, mark.to.id), "moved": mark,
            }),
            _ => return Self::new(StatusCode::SERVICE_UNAVAILABLE, unavailable.0),
        };
        Self {
            body: Some(body),
            ..Self::new(StatusCode::CONFLICT, unavailable.0)
        }
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        if let Some(body) = self.body {
            (self.status, Json(body)).into_response()
        } else if self.plain {
            let kind = [(header::CONTENT_TYPE, "text/plain")];
            (self.status, kind, self.error).into_response()
        } else if let Some(detail) = self.detail {
            (self.status, Json(json!({ "error": detail }))).into_response()
        } else if self.retryable {
            let body = json!({ "error": self.error, "retryable": true });
            (self.status, Json(body)).into_response()
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

    /// The agent or the janitor with this session name: for what is read of either. A
    /// janitor is looked at and not spoken to.
    pub(crate) fn anyone(&self, session: &str) -> Option<Arc<Member>> {
        self.fleet.directory().anyone(session).cloned()
    }
}

pub fn router(app: App) -> Router {
    Router::new()
        .route("/server-info", get(server_info))
        .route("/host/start", get(host_start))
        .route("/message-tool-details", get(tool_details))
        .route("/agents/snapshot", get(snapshot))
        .route("/agents", post(create_agent))
        .route("/agents/{session}", delete(remove_agent))
        // A whole conversation arrives in one request.
        .route("/agents/import", post(import_agent))
        .route("/log", get(log))
        .route("/events", get(events))
        .route("/send", post(send))
        .route("/agent-moved", post(crate::agent_moved::set))
        .route(
            "/agent-coordination",
            post(crate::hidden_coordination::set_chat),
        )
        .route(
            "/coordination-settings",
            get(crate::hidden_coordination::get).post(crate::hidden_coordination::set),
        )
        .route("/agent-notes", post(crate::coordination::agent_note))
        .route("/orchestrator/route-delegation", post(route_delegation))
        .route("/stop", post(stop))
        .route("/repair-slot", post(repair_slot))
        .route("/select", post(select))
        .route("/turn-queue", get(queue))
        .route("/live", get(shown))
        .route("/turn-queue/resume", post(resume))
        .route("/turn-queue/{id}", delete(take_back).put(reword))
        .route("/turn-queue/{id}/send", post(send_by_hand))
        .route("/pairing/identity", get(pairing_identity))
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
        .merge(library::routes().layer(middleware::from_fn_with_state(app.clone(), library::texts)))
        .merge(media::routes())
        .merge(plans::routes())
        .merge(crate::native_goal_routes::routes())
        .merge(push::routes())
        .merge(peers::routes())
        .merge(roster::routes())
        .merge(crate::calls::routes())
        .merge(crate::chip::routes())
        .merge(crate::compact::routes())
        .merge(crate::schedules::routes())
        .merge(crate::heartbeat::routes())
        .merge(crate::forks::routes())
        .merge(crate::launch_directories::routes())
        .merge(crate::janitors::routes())
        .merge(crate::router::routes())
        .merge(crate::past::routes())
        .merge(crate::connector::routes())
        .merge(crate::personas::routes())
        .merge(crate::portrait_generation::routes())
        .merge(crate::managed_skills::routes())
        .merge(crate::contacts::routes())
        .merge(crate::places::routes())
        .merge(crate::oracle::routes())
        .merge(crate::calendar::routes())
        .merge(crate::models::routes())
        .merge(crate::usage::routes())
        .merge(crate::files::routes())
        .merge(crate::screens::routes())
        .merge(crate::vocabulary::routes())
        .merge(crate::terminal::routes())
        .merge(crate::listening::routes())
        .merge(crate::voice::routes())
        .merge(crate::voices::routes())
        .merge(crate::faces::routes())
        // What is no route of this Host, by its path or by its method, is answered as the
        // Python Host answers it: to whoever may ask at all.
        .fallback(no_route)
        .method_not_allowed_fallback(no_route)
        .layer(middleware::from_fn_with_state(app.clone(), looked_at_only))
        .layer(middleware::from_fn(json_body))
        .layer(middleware::from_fn_with_state(app.clone(), authorize))
        .layer(middleware::from_fn(declared_framing))
        .layer(middleware::from_fn_with_state(
            (app.capture.clone(), app.clock.clone()),
            crate::diagnostics::request,
        ))
        .with_state(app)
}

/// The controls of a chat, by their paths: what is asked of an agent a person speaks to.
const CHAT_CONTROLS: [&str; 22] = [
    "/send",
    "/stop",
    "/select",
    "/focus",
    "/compact",
    "/repair-slot",
    "/agent-voice",
    "/agent-llm",
    "/agent-mcp",
    "/agent-heartbeat",
    "/agent-archive",
    "/agent-dreaming",
    "/agent-mute",
    "/agent-rename",
    "/agent-assign",
    "/agent-portraits",
    "/agent-portrait-generation",
    "/agent-schedules",
    "/agent-goal",
    "/agent-goal/pause",
    "/agent-goal/resume",
    "/agent-goal/clear",
];

fn body_refusal(status: StatusCode, message: &str, close: bool) -> Response {
    let mut response = (status, Json(json!({"error":message}))).into_response();
    if close {
        response
            .headers_mut()
            .insert(header::CONNECTION, HeaderValue::from_static("close"));
    }
    response
}

pub(crate) fn too_large(close: bool) -> Response {
    body_refusal(
        StatusCode::PAYLOAD_TOO_LARGE,
        request_body::TOO_LARGE,
        close,
    )
}

/// This listener speaks HTTP/1. A transfer-coded request has no declared
/// size to admit before reading. Refuse it even when the route needs no body.
async fn declared_framing(request: Request, next: Next) -> Response {
    if request.headers().contains_key(header::TRANSFER_ENCODING) {
        return body_refusal(StatusCode::BAD_REQUEST, "bad json", true);
    }
    next.run(request).await
}

async fn json_body(request: Request, next: Next) -> Response {
    use tower::{Layer, ServiceExt};
    let path = request.uri().path();
    let Some(limit) = request_body::limit(request.method().as_str(), path) else {
        return next.run(request).await;
    };
    let Some(value) = request.headers().get(header::CONTENT_LENGTH) else {
        // Python's JSON reader treats a missing length as an empty object.
        // Never consume bytes that have no admitted length.
        let (parts, _) = request.into_parts();
        return next
            .run(Request::from_parts(parts, axum::body::Body::from("{}")))
            .await;
    };
    let Some(length) = value.to_str().ok().and_then(|s| s.parse::<u64>().ok()) else {
        return body_refusal(StatusCode::BAD_REQUEST, "bad json", true);
    };
    match request_body::admission(length, limit) {
        request_body::Admission::Refuse { close } => {
            eprintln!(
                "requestBodyTooLarge family={} bytes={length}",
                request_body::family(path)
            );
            if close {
                return too_large(true);
            }
            // Drain small refusals so an HTTP/1 client can receive the answer
            // and reuse the connection. Never attempt to parse discarded bytes.
            let complete = axum::body::to_bytes(request.into_body(), length as usize)
                .await
                .is_ok_and(|b| b.len() as u64 == length);
            return too_large(!complete);
        }
        request_body::Admission::LogLarge => eprintln!(
            "requestBodyLarge family={} bytes={length}",
            request_body::family(path)
        ),
        request_body::Admission::Accept => {}
    }
    // Downstream Bytes and Json extractors receive the admitted body under
    // the same cap, including a control inspected by the janitor middleware.
    DefaultBodyLimit::max(limit)
        .layer(next)
        .oneshot(request)
        .await
        .unwrap_or_else(|never| match never {})
}

/// A janitor is looked at and not spoken to: a control of a chat that names one, by its
/// session or its id, is refused in the Python Host's word and words, whatever it asks
/// (`server.py`, `_reject_janitor_control`). So is the removal of a janitor as an agent.
/// Anything else goes on as it came.
async fn looked_at_only(State(app): State<App>, request: Request, next: Next) -> Response {
    let refused = || {
        let told = json!({
            "error": "Janitors are inspection-only. Use their Pause or Configure controls.",
            "code": "janitor_inspection_only",
        });
        (StatusCode::FORBIDDEN, Json(told)).into_response()
    };
    let is_janitor = |named: &str| {
        let named = python::strip(named);
        !named.is_empty() && app.fleet.directory().janitor(named).is_some()
    };
    let path = request.uri().path().to_string();
    if request.method() == Method::DELETE
        && let Some(named) = path.strip_prefix("/agents/")
    {
        let named = percent_decoded(named);
        if is_janitor(named.trim_matches('/')) {
            return refused();
        }
        return next.run(request).await;
    }
    if request.method() != Method::POST || !CHAT_CONTROLS.contains(&path.as_str()) {
        return next.run(request).await;
    }
    let (parts, body) = request.into_parts();
    let Ok(bytes) = axum::body::to_bytes(body, request_body::JSON_LIMIT).await else {
        return too_large(true);
    };
    let said: Value = serde_json::from_slice(&bytes).unwrap_or(Value::Null);
    // The agent it names: its `session`, or where it names none, the one it replaces.
    let text = |key: &str| {
        said.get(key)
            .and_then(Value::as_str)
            .filter(|text| !text.is_empty())
    };
    if text("session")
        .or_else(|| text("replace_sid"))
        .is_some_and(is_janitor)
    {
        return refused();
    }
    let request = Request::from_parts(parts, axum::body::Body::from(bytes));
    next.run(request).await
}

/// A part of a path as it was meant: `%41` is `A`. What is no such sign stays as it is.
fn percent_decoded(part: &str) -> String {
    let bytes = part.as_bytes();
    let (mut out, mut at) = (Vec::with_capacity(bytes.len()), 0);
    while at < bytes.len() {
        let hex = bytes
            .get(at + 1..at + 3)
            .and_then(|hex| std::str::from_utf8(hex).ok());
        let sign = hex.and_then(|hex| u8::from_str_radix(hex, 16).ok());
        match (bytes[at], sign) {
            (b'%', Some(sign)) => {
                out.push(sign);
                at += 3;
            }
            (other, _) => {
                out.push(other);
                at += 1;
            }
        }
    }
    String::from_utf8_lossy(&out).into_owned()
}

pub(crate) async fn no_route() -> ApiError {
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

/// Where the token a request was let in by was found (the Python Host's
/// `_request_auth_source`): nowhere on a Host that has none.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Presented {
    Nowhere,
    Header,
    Cookie,
    Address,
}

/// Who a request comes from, as the chat a client last opened is kept under: a device by
/// its id, the administrator by that word on a Host that has a token; nobody else (the
/// Python Host's `_request_principal`).
pub(crate) fn principal_name(app: &App, principal: &Principal) -> String {
    match principal {
        Principal::Administrator if !app.config.auth_token.is_empty() => "administrator".into(),
        Principal::Device { device_id, .. } => device_id.clone(),
        _ => String::new(),
    }
}

/// The kind of record the chat each client last opened is kept as: the Host's own note
/// about one client, read by that client's next recording and by nothing else. No setting:
/// it is published to nobody.
pub(crate) const OPENED: &str = "client-chat";

/// Keeps the chat a client opened, for the recordings it makes (`clarp_kernel::heard`
/// `client_chat`), under the digest of who it is. What cannot be kept loses nothing but
/// this, and is said.
pub(crate) async fn opened_by(app: &App, principal: &Principal, session: &str) {
    let who = principal_name(app, principal);
    if who.is_empty() || session.is_empty() {
        return;
    }
    let kept = clarp_kernel::heard::client_chat_kept(session, app.clock.now_ms());
    let key = clarp_kernel::heard::client_chat_key(&who);
    if let Err(why) = app.fleet.keep(OPENED, key, Some(kept.into_bytes())).await {
        eprintln!(
            "clarp-host: the chat a client opened could not be kept: {}",
            why.0
        );
    }
}

/// The chat a client last opened, as it was kept: nothing where it opened none, or where
/// what was kept cannot be read (and that is said).
pub(crate) async fn last_opened(app: &App, who: &str) -> String {
    if who.is_empty() {
        return String::new();
    }
    let key = clarp_kernel::heard::client_chat_key(who);
    match app.fleet.kept_book(OPENED, key).await {
        Ok(kept) => {
            let text = kept.map(|bytes| String::from_utf8_lossy(&bytes).into_owned());
            clarp_kernel::heard::client_chat(text.as_deref(), app.clock.now_ms())
        }
        Err(why) => {
            eprintln!(
                "clarp-host: the chat a client opened could not be read: {}",
                why.0
            );
            String::new()
        }
    }
}

/// A device can check the Host's identity and exchange a code before it has a token.
const EXCHANGE: &str = "/pairing/exchange";
const PAIRING_IDENTITY: &str = "/pairing/identity";
/// And the pictures this Host ships, which anybody may fetch: an address of one is put
/// where no token goes (a notification, a page), as on the Python Host.
const PICTURES: &str = "/static/";
/// And the picture an agent has of its own, by an address this Host signed for a day: what
/// a push carries, for a phone that shows the notification without the app's token.
const SIGNED_PICTURES: &str = "/notification-avatars/";

/// Whether a request is one of the Oracle's: `/oracle` and what is under it, read or done.
fn of_the_oracle(request: &Request, path: &str) -> bool {
    let method = request.method();
    let read_or_done = method == Method::GET || method == Method::POST;
    read_or_done && (path == "/oracle" || path.starts_with("/oracle/"))
}

/// Whether this caller is kept from this request by its scope.
fn out_of_scope(principal: &Principal, method: &Method, path: &str) -> bool {
    // An agent's own tools are asked for from this machine, with the token a turn was given
    // or the administrator's. No device asks for them.
    if path.starts_with("/tools/") {
        return !matches!(principal, Principal::Administrator | Principal::Tools);
    }
    // And that token opens nothing else: what the apps read and do is not a tool's. But
    // for what an agent asks of its user's phone by a script that speaks to the Host
    // itself: an event for the calendar.
    if matches!(principal, Principal::Tools) {
        return !(method == Method::POST && path == "/calendar/request");
    }
    let Principal::Device {
        scope: Scope::Limited,
        ..
    } = principal
    else {
        return false;
    };
    // A request for a head is answered as a read.
    let method = if method == Method::HEAD {
        "GET"
    } else {
        method.as_str()
    };
    clarp_kernel::devices::limited_refused(method, path)
}

/// A request's headers as the rules of trust read them: each as it was sent, a value byte
/// for byte.
pub(crate) fn headers_of(headers: &HeaderMap) -> Vec<(String, String)> {
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
fn who(app: &App, request: &Request, local: bool) -> Option<(Principal, Presented)> {
    let expected = &app.config.auth_token;
    if expected.is_empty() {
        // A Host without a token is open to this machine, and to nobody else.
        return local.then_some((Principal::Administrator, Presented::Nowhere));
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
        return Some((principal, Presented::Header));
    }
    if let Some(principal) = text(header::COOKIE).and_then(cookie).and_then(known) {
        return Some((principal, Presented::Cookie));
    }
    // A token in an address is written into logs and histories on its way: it is taken
    // only where there is no way.
    if !local {
        return None;
    }
    let mut pairs = request.uri().query()?.split('&');
    let principal = pairs.find_map(|pair| known(pair.strip_prefix("token=")?.trim()))?;
    Some((principal, Presented::Address))
}

async fn authorize(State(app): State<App>, mut request: Request, next: Next) -> Response {
    // Scope is decided on the raw path before route parameters are decoded.
    let path = request.uri().path().to_string();
    let picture = path.starts_with(PICTURES) || path.starts_with(SIGNED_PICTURES);
    let read = request.method() == Method::GET;
    let open = path == EXCHANGE || (read && (path == PAIRING_IDENTITY || picture));
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
            // A Host without a token is open to whoever is on its machine, and nobody there
            // has said who they are: the Oracle, which speaks for its user, is not for that.
            Some(_) if app.config.auth_token.is_empty() && of_the_oracle(&request, &path) => {
                let why = "Oracle requires authenticated full-device access";
                ApiError::new(StatusCode::UNAUTHORIZED, why).into_response()
            }
            Some((principal, _)) if out_of_scope(&principal, request.method(), &path) => {
                let why = if path == "/agent-moved" && matches!(principal, Principal::Tools) {
                    "agents_may_not"
                } else if path == "/agent-moved" {
                    "full device access required"
                } else if matches!(principal, Principal::Tools) {
                    "an agent's tools may ask for tools only"
                } else {
                    clarp_kernel::devices::LIMITED_REFUSAL
                };
                ApiError::new(StatusCode::FORBIDDEN, why).into_response()
            }
            Some((principal, presented)) => {
                if let Principal::Device { device_id, .. } = &principal {
                    device = Some(device_id.clone());
                }
                request.extensions_mut().insert(Asker { source });
                request.extensions_mut().insert(principal);
                request.extensions_mut().insert(presented);
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

async fn server_info(
    State(app): State<App>,
    Extension(principal): Extension<Principal>,
) -> Json<Value> {
    let info = json!(identity(&app));
    let info = match principal {
        Principal::Device {
            scope: Scope::Limited,
            ..
        } => clarp_kernel::devices::limited_server_info(info),
        _ => info,
    };
    Json(info)
}

/// `GET /host/start`, this Host's own and the administrator's only: how its start went. How many
/// agents it has, how many are still being opened and how many could not be (their logs stay
/// as they were), and how many records of each kind it keeps and could not read
/// (`kept.rs`). Not the reference's `/server-info` or `/status`, which keep their shape in every
/// state: this is not zero for a moment at every start.
async fn host_start(
    State(app): State<App>,
    Extension(principal): Extension<Principal>,
) -> Result<Json<Value>, ApiError> {
    if !matches!(principal, Principal::Administrator) {
        return Err(ApiError::new(StatusCode::FORBIDDEN, "administrator only"));
    }
    let directory = app.fleet.directory();
    let all = directory.members.iter().chain(directory.janitors.iter());
    let (mut agents, mut opening, mut broken) = (0_u64, 0_u64, 0_u64);
    for member in all {
        agents += 1;
        match member.agent.view().broken.as_deref() {
            None => {}
            Some(crate::agent::OPENING) => opening += 1,
            Some(_) => broken += 1,
        }
    }
    Ok(Json(json!({
        "agents": agents, "opening": opening, "broken": broken,
        "left_out": app.fleet.left_out().counts(),
        "not_run": app.fleet.left_out().not_run_counts(),
        "judged_when_read": crate::kept::judged_when_read(),
    })))
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
                let mut doing = wire::activity(record, &view.status);
                let recovering = doing["phase"] == "account_recovery";
                let text = if said.is_empty() {
                    if view.can_leave_note
                        && crate::hidden_coordination::hides(directory, &record.agent_id)
                        && let Some(headline) =
                            view.working.as_ref().and_then(wire::WorkingWith::headline)
                    {
                        clarp_kernel::hidden_coordination::veil_activity(&mut doing, &headline);
                    }
                    doing["summary"].as_str().unwrap_or("").trim().to_string()
                } else {
                    said.to_string()
                };
                family.working.push(helpers::Working {
                    session: record.session.clone(),
                    text,
                    bucket: helpers::bucket(if recovering { "waiting" } else { &state }),
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
    owners: &toml::Table,
    used_up: &crate::quota::UsedUp,
) -> Value {
    let view = member.agent.view();
    let focused = directory.focus.as_deref() == Some(member.record.agent_id.as_str());
    let (global, choice) =
        crate::hidden_coordination::visibility(directory, &member.record.agent_id);
    let visibility = choice.unwrap_or(global);
    let hides =
        view.can_leave_note && visibility == clarp_kernel::hidden_coordination::Visibility::Hide;
    let newest = if hides {
        view.visible_previews.0.as_ref()
    } else {
        view.newest.as_ref()
    };
    let mut row = wire::summary(
        &member.record,
        &view.status,
        view.backend,
        newest,
        view.ended.as_ref(),
        focused,
    );
    row["moved"] = json!(view.status.moved);
    row["goal"] = json!(
        view.native_goal
            .as_ref()
            .map(clarp_kernel::agent_goal::Goal::public)
    );
    row["coordination_visibility"] = json!(visibility);
    row["coordination_override"] = json!(choice);
    let working = view.working.as_ref();
    let sender = working
        .and_then(wire::WorkingWith::sender)
        .and_then(|(id, _)| {
            directory
                .named(id)
                .map(|known| (id, known.record.persona.as_str()))
        });
    row["working_audience"] = json!(working.map(wire::WorkingWith::audience));
    row["working_with"] = json!(sender.map(|(id, name)| json!({"agent_id": id, "name": name})));
    if hides {
        row["last_completed_message"] = json!(
            view.visible_previews
                .1
                .as_ref()
                .map(wire::preview)
                .unwrap_or_default()
        );
        let at = newest.map_or(member.record.created_at_ms, |preview| {
            preview.ts_ms.max(member.record.created_at_ms)
        });
        row["last_activity"] = json!(at / 1_000);
        if working.is_some_and(|with| with.audience() == clarp_kernel::coordination::AGENTS) {
            let headline =
                clarp_kernel::hidden_coordination::working_headline(sender.map(|(_, name)| name));
            clarp_kernel::hidden_coordination::veil_activity(&mut row["activity"], &headline);
            clarp_kernel::hidden_coordination::veil_live(&mut row["live_activity"], &headline);
        }
    }
    // Whether its tool's account is used up: advice for whoever is about to write to it.
    row["backend_quota"] = used_up.get(view.backend).cloned().unwrap_or(Value::Null);
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
        None => {
            let field = format!("{}_model", view.backend);
            let default = crate::owner::text_at(owners, &["agents", &field]).unwrap_or_default();
            faces.on_model(&record.persona, view.backend, &record.model, &default)
        }
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
        .filter(|job| {
            !view.mirrored_provider(&job.id)
                && wire::mirrored_helper(job, &helper_sessions).is_none()
        })
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
async fn snapshot(
    State(app): State<App>,
    Extension(principal): Extension<Principal>,
    Query(query): Query<SnapshotQuery>,
) -> Json<Value> {
    let directory = app.fleet.directory();
    let without_archived = query.include_archived.as_deref() == Some("0");
    let put_away = |member: &Arc<Member>| member.record.archived_at_ms.is_some();
    let families = Families::of(&directory);
    let used_up = crate::quota::used_up(&app).await;
    // Which agents the user calls live: by their profiles, as they are now.
    let called_live = app.calls.taking(&app).await;
    let owners = app.mouth.owners_from(&directory);
    let teams = app
        .teams
        .read(|teams, _| clarp_kernel::teams::memberships(teams));
    let team_ids = |member: &Member| {
        teams
            .get(&member.record.agent_id)
            .map_or(&[][..], Vec::as_slice)
    };
    let mut agents: Vec<Value> = directory
        .members
        .iter()
        .filter(|member| !(without_archived && put_away(member)))
        .map(|member| {
            let mut row = agent_row(&directory, &families, member, &app.faces, &owners, &used_up);
            row["team_ids"] = json!(team_ids(member));
            if called_live.contains(&member.record.session) {
                row["call_mode"] = json!(clarp_kernel::live_call::LIVE);
            }
            row
        })
        .collect();
    // And the janitors, after them: members of the fleet that are no agents to chat with.
    // An app tells them by `is_janitor` and lists them apart; each is shown with the
    // picture of its job.
    let jobs = crate::janitors::jobs(&app).await;
    for member in &directory.janitors {
        if without_archived && put_away(member) {
            continue;
        }
        let mut row = agent_row(&directory, &families, member, &app.faces, &owners, &used_up);
        row["team_ids"] = json!(team_ids(member));
        let job = jobs.get(&member.record.agent_id).map_or("", String::as_str);
        row["avatar_url"] = json!(app.faces.janitor(clarp_kernel::janitors::portrait(job)));
        row["model_avatar_url"] = json!("");
        agents.push(row);
    }
    // Who an agent can be: the personas this Host has built in, with the voices the owner's
    // file gives them and the look the owner gave them, and the personas the owner made.
    let [named, cartesia, gemini] = crate::owner::personas(&owners);
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
    // A contact's picture under way, said on the contact and on its agents that have neither a
    // picture nor a symbol of their own (`avatar_generation_status_for_agent`).
    let under_way = crate::pictures::under_way(&app).await;
    let mut personas = personas.clone();
    if !under_way.is_empty() {
        let mut by_name = HashMap::new();
        for persona in &mut personas {
            let id = persona["id"].as_str().unwrap_or("").to_string();
            if let Some(status) = under_way.get(&id) {
                persona["avatar_generation_status"] = json!(status);
                let name = persona["name"].as_str().unwrap_or("").to_lowercase();
                by_name.insert(name, status.clone());
            }
        }
        for row in &mut agents {
            let bare = row["avatar_symbol"].as_str().unwrap_or("").is_empty()
                && row["is_janitor"] != true
                && directory.members.iter().any(|member| {
                    member.record.session == row["session"].as_str().unwrap_or("")
                        && member.record.avatar.is_none()
                        && member.record.avatar_symbol.is_empty()
                });
            let name = row["persona"].as_str().unwrap_or("").to_lowercase();
            if let Some(status) = by_name.get(&name).filter(|_| bare) {
                row["avatar_generation_status"] = json!(status);
            }
        }
    }
    // How each agent sounds and is heard, from one view of the Host and one pass over its
    // agents; an archived agent speaks to nobody and has none.
    let voices = crate::profile::view(&app.mouth, &app.ears, &directory, &owners);
    let mut profiles: HashMap<String, Value> = crate::profile::all(&voices, &directory)
        .into_iter()
        .collect();
    let archived: HashSet<&str> = directory
        .members
        .iter()
        .chain(&directory.janitors)
        .filter(|member| put_away(member))
        .map(|member| member.record.session.as_str())
        .collect();
    for row in &mut agents {
        let session = row["session"].as_str().unwrap_or("");
        if !archived.contains(session)
            && let Some(profile) = profiles.remove(session)
        {
            row["voice_profile"] = profile;
        }
    }
    if matches!(
        principal,
        Principal::Device {
            scope: Scope::Limited,
            ..
        }
    ) {
        clarp_kernel::devices::limit_snapshot_cwd(&mut agents);
    }
    let (voice_defaults, voice_profile_version) = crate::profile::head(&voices);
    let available_mcp_servers = tokio::task::spawn_blocking(crate::mcp::available)
        .await
        .unwrap_or_default();
    let mut body = json!({
        "agents": agents,
        "focus": directory.focus,
        "roster": roster,
        "voice_defaults": voice_defaults,
        "voice_profile_version": voice_profile_version,
        "personas": personas,
        "available_mcp_servers": available_mcp_servers,
        "model_avatars": clarp_kernel::portraits::model_avatars(directory.setting(clarp_kernel::portraits::MODEL_SETTING)),
        // Nothing explains a tool's use here, and an app is told so wherever it asks: it
        // then shows the tool's own command on the row.
        "tool_explanations": apps::explanations_told(&app),
    });
    if without_archived {
        let all = directory.members.iter().chain(&directory.janitors);
        let archived = all.filter(|member| put_away(member));
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

/// Why an agent was not made, or not started again, as an app is told.
fn not_made(why: NotMade) -> (StatusCode, Value) {
    match why {
        NotMade::Refused(why) => (StatusCode::BAD_REQUEST, json!({ "error": why })),
        NotMade::Occupied { owner, session } => {
            (StatusCode::CONFLICT, roster::occupied(&owner, &session))
        }
        NotMade::Told(told) => {
            let status = StatusCode::from_u16(told.status).unwrap_or(StatusCode::BAD_REQUEST);
            (
                status,
                json!({ "error": told.code, "message": told.message }),
            )
        }
    }
}

/// How long an agent's turn is given to end when its agent is started again, and how often
/// that is looked at.
const ENDS_WITHIN: Duration = Duration::from_secs(8);
const LOOKED_EVERY: Duration = Duration::from_millis(100);

/// Starts an agent again in place (`POST /agents` with `replace_sid`): the same agent under
/// the same session name, as the request says it is to be, with a new conversation with its
/// tool and an empty one for those who read it. What the request asks for is checked before
/// anything of the agent is touched. A turn that runs is stopped first: the Python Host
/// lets it run on, and its answer is then in no conversation anybody reads.
async fn started_again(
    app: &App,
    replaces: String,
    request: CreateAgentRequest,
) -> Result<Result<AgentRecord, NotMade>, (StatusCode, Value)> {
    let unavailable = |why: Unavailable| {
        let why = ApiError::from(why);
        (
            why.status,
            why.body.unwrap_or_else(|| json!({"error":why.error})),
        )
    };
    let checked = app
        .fleet
        .start_again(replaces.clone(), request.clone(), true);
    if let Err(refused) = checked.await.map_err(unavailable)? {
        return Ok(Err(refused));
    }
    let Some(member) = app.fleet.directory().by_session(&replaces).cloned() else {
        let why = "no such agent to replace";
        return Ok(Err(crate::fleet::Told::new(404, why, why).into()));
    };
    // Its log is told first: from there on its conversation is a new one. While a turn of
    // it runs it cannot be: the turn is stopped, its mailbox held meanwhile, and let go
    // again when the agent is started.
    let was_held = member.agent.view().status.paused;
    let mut held = false;
    let mut started = member.agent.start_again().await.map_err(unavailable)?;
    let give_up = tokio::time::Instant::now() + ENDS_WITHIN;
    while !started && tokio::time::Instant::now() < give_up {
        if !held {
            member.agent.hold().await.map_err(unavailable)?;
            held = true;
        }
        tokio::time::sleep(LOOKED_EVERY).await;
        started = member.agent.start_again().await.map_err(unavailable)?;
    }
    let let_go = || async {
        if held && !was_held {
            let _ = member.agent.resume("relaunch".to_string()).await;
        }
    };
    if !started {
        let_go().await;
        let why = "The agent's turn did not end: the agent was not started again.";
        return Err((
            StatusCode::CONFLICT,
            json!({ "error": "agent_busy", "message": why }),
        ));
    }
    let done = app.fleet.start_again(replaces, request, false);
    let done = done.await.map_err(unavailable);
    let_go().await;
    done
}

fn creation_backend(backend: &str) -> String {
    let backend = clarp_kernel::python::strip(backend).to_ascii_lowercase();
    match backend.as_str() {
        "antigravity" => "agy".to_string(),
        "open-code" | "opencode-ai" => "opencode".to_string(),
        "deep-seek" => "deepseek".to_string(),
        "muse-code" | "musecode" => "muse".to_string(),
        _ => backend,
    }
}

async fn creation_default_model(app: &App, backend: &str) -> Option<String> {
    let key = format!("{backend}_model");
    if let Some(model) = crate::owner::text_at(&app.mouth.owners(), &["agents", &key]) {
        return Some(model);
    }
    let backend = backend.to_owned();
    tokio::task::spawn_blocking(move || {
        let home = crate::config::home();
        match backend.as_str() {
            "claude" => {
                let config = std::env::var_os("CLAUDE_CONFIG_DIR")
                    .filter(|directory| !directory.is_empty())
                    .map_or_else(|| home.join(".claude"), std::path::PathBuf::from);
                let settings = std::fs::read_to_string(config.join("settings.json")).ok()?;
                let settings: Value = serde_json::from_str(&settings).ok()?;
                std::env::var("ANTHROPIC_MODEL")
                    .ok()
                    .filter(|model| !model.is_empty())
                    .or_else(|| settings["model"].as_str().map(str::to_owned))
            }
            "codex" => {
                let config = crate::past::codex_home(&home).join("config.toml");
                let config = std::fs::read_to_string(config).ok()?;
                let config: toml::Table = config.parse().ok()?;
                let profile = crate::owner::text_at(&config, &["profile"]);
                profile
                    .as_deref()
                    .and_then(|profile| {
                        crate::owner::text_at(&config, &["profiles", profile, "model"])
                    })
                    .or_else(|| crate::owner::text_at(&config, &["model"]))
            }
            _ => None,
        }
    })
    .await
    .ok()
    .flatten()
}

/// Makes an agent from what a request said: `POST /agents`, and `clarp-admin agent create`.
/// Answers what the Python Host answers: the new agent's session, its name and its row, or
/// why it was not made. With `replace_sid` the agent of that session is started again in
/// place.
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
        None => Ok(None),
        Some(Value::Null) => Ok(Some(String::new())),
        Some(Value::String(said)) => Ok(Some(said.clone())),
        Some(_) => Err(bad(&format!("{what} must be a string or null"))),
    };
    let plain = |key: &str| asked.get(key).and_then(Value::as_str).map(str::to_string);
    let presentation = |key: &str| {
        asked.get(key).map(|value| {
            if !clarp_kernel::python::truthy(value) {
                String::new()
            } else if let Some(value) = value.as_str() {
                value.to_owned()
            } else {
                clarp_kernel::python::repr(value)
            }
        })
    };
    for key in ["resume_session_id", "fork_session_id"] {
        match asked.get(key) {
            None | Some(Value::Null) => {}
            Some(Value::String(id))
                if id.is_empty()
                    || clarp_kernel::native_session_name(clarp_kernel::python::strip(id)) => {}
            _ => return Err(bad("invalid session id")),
        }
    }
    let mut request = CreateAgentRequest {
        name: plain("name").unwrap_or_default(),
        backend: plain("backend").map(|backend| creation_backend(&backend)),
        cwd: plain("cwd"),
        model: text("model", "model")?,
        effort: text("effort", "effort")?,
        voice_id: plain("voice_id"),
        parent: plain("parent"),
        role: plain("role"),
        session: plain("session"),
        personality: presentation("personality"),
        avatar_symbol: presentation("avatar_symbol"),
        avatar_base64: presentation("avatar_base64"),
        avatar_path: asked.get("avatar_path").cloned(),
        mcp_servers: None,
    };
    let mcp_selection = || clarp_kernel::mcp::selection(asked.get("mcp_servers")).map_err(bad);
    let unavailable =
        |why: Unavailable| (StatusCode::SERVICE_UNAVAILABLE, json!({ "error": why.0 }));
    // The agent it names is started again in place; with none named a new agent is made.
    let replaces = plain("replace_sid").map(|said| said.trim().to_string());
    let replaces = replaces.filter(|said| !said.is_empty());
    // A contact asked for (`mint_contact`, `auto_contact`) goes with no other way of naming
    // the agent: started again in place, it has its own.
    if replaces.is_some()
        && let Launch::Refused(status, word) = contacts::launch(asked, None, false)
    {
        let status = StatusCode::from_u16(status).unwrap_or(StatusCode::BAD_REQUEST);
        return Err(refused(status, word, word));
    }
    let anonymous = asked.get("anonymous") == Some(&Value::Bool(true));
    if anonymous && replaces.is_some() {
        return Err(bad("anonymous launch must be fresh"));
    }
    let replacing = replaces.as_deref().and_then(|session| {
        app.fleet
            .directory()
            .by_session(session)
            .map(|member| member.record.clone())
    });
    let backend = request
        .backend
        .as_deref()
        .filter(|backend| !backend.is_empty())
        .or_else(|| replacing.as_ref().map(|record| record.backend.as_str()))
        .unwrap_or("claude");
    let model = request
        .model
        .as_deref()
        .map(clarp_kernel::python::strip)
        .unwrap_or("");
    if !model.is_empty()
        && let Some(detail) = app.models.policy_model_error(backend, model)
    {
        let code = "invalid model for backend";
        let message = detail
            .strip_prefix("Invalid provider model identifier: ")
            .unwrap_or(code);
        return Err(refused(StatusCode::BAD_REQUEST, code, message));
    }
    let default_model = if model.is_empty() && (replaces.is_none() || request.model.is_some()) {
        creation_default_model(app, backend).await
    } else {
        None
    };
    if replaces.is_some() && request.model.is_some() && model.is_empty() {
        request.model = Some(default_model.clone().unwrap_or_default());
    }
    let record = match replaces {
        Some(replaces) => {
            request.mcp_servers = mcp_selection()?;
            started_again(app, replaces, request)
                .await?
                .map_err(not_made)?
        }
        None => {
            if let Some(parent) = request.parent.as_deref()
                && let Some(member) = app.fleet.directory().named(parent.trim())
            {
                member.agent.accepting_work().map_err(|why| {
                    refused(
                        StatusCode::SERVICE_UNAVAILABLE,
                        crate::agent::REMOVING_CODE,
                        &why.0,
                    )
                })?;
            }
            // A new agent may begin with a session its tool kept: taken up where it left
            // off, or from a copy of it.
            let said_cwd = request.cwd.as_deref().unwrap_or("");
            let home = crate::config::home();
            let cwd = crate::config::working_dir(said_cwd, &home, &app.config.default_cwd);
            let named_session =
                |key| plain(key).is_some_and(|id| !clarp_kernel::python::strip(&id).is_empty());
            let forks = named_session("fork_session_id");
            // A fork can write its transcript while resolving the native session. Refuse a
            // bad selection before it does. Reopening an existing agent leaves its row alone.
            if forks && !named_session("resume_session_id") && asked.contains_key("mcp_servers") {
                let selection = mcp_selection()?;
                let backend = request
                    .backend
                    .as_deref()
                    .and_then(crate::fleet::non_empty)
                    .unwrap_or_else(|| "claude".into());
                tokio::task::spawn_blocking(move || {
                    crate::mcp::check(&backend, selection.as_deref())
                })
                .await
                .map_err(|error| unavailable(Unavailable::new(error.to_string())))?
                .map_err(|why| not_made(why.into()))?;
            }
            let begins = crate::past::begins(app, asked, &cwd).await?;
            let taken = match begins {
                crate::past::Begins::Afresh => {
                    if request
                        .model
                        .as_deref()
                        .is_none_or(|model| model.trim().is_empty())
                    {
                        request.model = default_model;
                    }
                    None
                }
                // An agent has that session, and the request said to open what is there.
                crate::past::Begins::Open(owner) => {
                    let directory = app.fleet.directory();
                    let Some(member) = directory.by_session(&owner) else {
                        return Err(refused(
                            StatusCode::NOT_FOUND,
                            "agent_not_found",
                            "No such agent",
                        ));
                    };
                    return Ok(made_as(app, &member.record).await);
                }
                crate::past::Begins::With {
                    helpers,
                    session,
                    turns,
                    parent,
                    model,
                } => {
                    let named = |said: &Option<String>| {
                        let said = said.as_deref();
                        said.is_some_and(|said| !said.trim().is_empty())
                    };
                    // Forked from an agent's session, it is that agent's unless the request
                    // names another.
                    if !named(&request.parent) {
                        request.parent = parent;
                    }
                    // It runs on the model the session last ran on, unless the request
                    // names one.
                    if !named(&request.model) && !model.is_empty() {
                        request.model = Some(model);
                    }
                    Some((session, turns, helpers))
                }
            };
            request.mcp_servers = mcp_selection()?;
            // Who it is when the request names no contact of its own: the first free one of
            // its tool's pool, or one minted for it (`agent_lifecycle`, `NeedsContact`). The
            // fleet chooses it in the turn that registers the agent, so that launches at once
            // never take the same one; what is refused is refused here.
            let contact = match contacts::launch(asked, None, false) {
                Launch::AsNamed if anonymous => Some(crate::fleet::ContactAsked::Anonymous),
                Launch::AsNamed => None,
                Launch::Refused(status, word) => {
                    let status = StatusCode::from_u16(status).unwrap_or(StatusCode::BAD_REQUEST);
                    return Err(refused(status, word, word));
                }
                Launch::Pooled(_) | Launch::Minted => Some(crate::fleet::ContactAsked::Pool {
                    mint: asked.get("mint_contact") == Some(&Value::Bool(true)),
                }),
            };
            // An agent that takes a session up has a conversation of its own, under a name
            // of its own: the session may have been another agent's conversation before, and
            // whoever read that one counts from another place. A fork's conversation goes
            // by the name of its copy, which nobody had.
            let forked = taken
                .as_ref()
                .filter(|_| forks)
                .map(|(session, _, _)| session.clone());
            let made = app
                .fleet
                .create_contacted(request, forked, contact, app.mouth.cached_voice_names().0)
                .await
                .map_err(unavailable)?;
            let record = made.map_err(not_made)?;
            if let Some((session, turns, helpers)) = taken {
                let member = app.fleet.directory().named(&record.agent_id).cloned();
                let taken = match member {
                    Some(member) => {
                        let rows = crate::past::rows_of(&record.agent_id, &session, &turns);
                        member.agent.take_up(session, rows, helpers).await
                    }
                    None => Ok(Err("the agent is gone".to_string())),
                };
                if !matches!(taken, Ok(Ok(_))) {
                    // An agent without the session it was to begin with is not what was
                    // asked for: it does not stay.
                    let why = "The session could not be taken up: no agent was made.";
                    let failed = refused(StatusCode::CONFLICT, "resume_failed", why);
                    let session = record.session.clone();
                    return Err(not_to_stay(&app.fleet, session, failed, unavailable).await);
                }
            }
            record
        }
    };
    // A contact without a picture is asked one, where the request gave the agent neither a
    // picture nor a symbol of its own (`agent_lifecycle.create`); nothing waits for it.
    let given = |key: &str| plain(key).is_some_and(|said| !said.trim().is_empty());
    if !record.is_janitor && !given("avatar_base64") && !given("avatar_symbol") {
        crate::pictures::ask(app, &record.persona, "", &record.backend).await;
    }
    Ok(made_as(app, &record).await)
}

/// What whoever makes an agent is answered: its session, its name and its row, in the
/// fields the Python Host answers with.
async fn made_as(app: &App, record: &AgentRecord) -> Value {
    let directory = app.fleet.directory();
    let row = directory
        .named(&record.agent_id)
        .map(|member| {
            // Whether its tool's account is used up is none of the fields of this answer.
            let (families, unasked) = (Families::of(&directory), crate::quota::UsedUp::new());
            agent_row(
                &directory,
                &families,
                member,
                &app.faces,
                &app.mouth.owners(),
                &unasked,
            )
        })
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
    let mut agent = agent;
    agent.insert("alive".into(), json!(true));
    agent.insert("last_activity".into(), json!(record.created_at_ms));
    if record.avatar.is_none()
        && record.avatar_symbol.is_empty()
        && !record.is_janitor
        && app.config.contact_pictures
    {
        let [named, cartesia, gemini] = crate::owner::personas(&app.mouth.owners());
        let owner = clarp_kernel::personas::Owner {
            roster: &named,
            cartesia: &cartesia,
            gemini: &gemini,
        };
        if let Some(persona) = directory.personas.named(&owner, &record.persona)
            && let Some(id) = persona["id"].as_str()
            && let Ok(Some(kept)) = app.fleet.computer_job(crate::pictures::job_id(id)).await
            && kept.job.status.is_active()
        {
            agent.insert(
                "avatar_generation_status".into(),
                json!(kept.job.status.as_str()),
            );
        }
    }
    // How it sounds and is heard, as the snapshot will show it (feature voice_profile).
    if let Some(profile) =
        crate::profile::of_session(&app.mouth, &app.ears, &directory, &record.session)
    {
        agent.insert("voice_profile".into(), profile);
    }
    json!({
        "ok": true,
        "session": record.session,
        "name": record.persona,
        "agent": agent,
    })
}

async fn create_agent(
    State(app): State<App>,
    headers: HeaderMap,
    body: axum::body::Bytes,
) -> Response {
    let asked = if body.is_empty() {
        json!({})
    } else {
        serde_json::from_slice(&body).unwrap_or(Value::Null)
    };
    let Some(asked) = asked.as_object() else {
        return ApiError::new(StatusCode::BAD_REQUEST, "bad json").into_response();
    };
    if let Err(why) = crate::fleet::delegations::lineage(
        &app,
        asked.get("parent").and_then(Value::as_str).unwrap_or(""),
        turn_of(&headers),
    )
    .await
    {
        return why.into_response();
    }
    match make_agent(&app, asked).await {
        Ok(made) => Json(made).into_response(),
        Err((status, why)) => (status, Json(why)).into_response(),
    }
}

/// `DELETE /agents/<session>`: an agent is removed. Answered as the Python Host answers it:
/// that it is done, or that there is no such agent, in a word for the reason and in words.
async fn remove_agent(State(app): State<App>, Path(session): Path<String>) -> Response {
    match app.fleet.remove(session.clone()).await {
        Ok(true) => Json(json!({ "ok": true })).into_response(),
        Ok(false) => {
            let why = format!("No active agent session: {session}");
            let told = json!({ "error": "agent_not_found", "message": why });
            (StatusCode::NOT_FOUND, Json(told)).into_response()
        }
        Err(why) => ApiError::from(why).into_response(),
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
pub struct MovedPlan {
    pub(crate) plan: Value,
    #[serde(default)]
    pub(crate) events: Option<Vec<clarp_kernel::goals::ledger::Event>>,
    #[serde(default)]
    pub(crate) documents: Vec<Value>,
}

/// An agent made for what then failed does not stay. Where it cannot be let go, that is
/// what the caller is told, in the store's words: an agent is there that should not be, and
/// the failure that came first is no longer the whole of it.
pub(crate) async fn not_to_stay<E>(
    fleet: &FleetHandle,
    session: String,
    failed: E,
    told: impl FnOnce(Unavailable) -> E,
) -> E {
    match fleet.remove(session).await {
        Ok(_) => failed,
        Err(unavailable) => told(unavailable),
    }
}

/// Takes an agent over from another Host: who it is, the conversation it had, and the tool's
/// session, which its next turn here continues.
async fn import_agent(
    State(app): State<App>,
    Json(request): Json<ImportRequest>,
) -> Result<Json<Value>, ApiError> {
    crate::admin::movable(&request.agent)
        .map_err(|why| ApiError::new(StatusCode::CONFLICT, why))?;
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
        .import(request.from.clone(), backend_session, rows)
        .await;
    let imported = match outcome {
        Ok(Ok(imported)) => imported,
        failed => {
            // Nothing of the conversation was written. An agent without its past is not what
            // was asked for: it does not stay.
            let failed = match failed {
                Ok(Err(why)) => ApiError::new(StatusCode::CONFLICT, why),
                Err(unavailable) => unavailable.into(),
                Ok(Ok(_)) => ApiError::new(StatusCode::INTERNAL_SERVER_ERROR, "the move failed"),
            };
            let session = member.record.session.clone();
            return Err(not_to_stay(&app.fleet, session, failed, ApiError::from).await);
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
    let plans = request.plans;
    let taken = match elsewhere {
        Some(id) => Ok(Err(format!(
            "another agent on this Host has an artifact with the id {id}"
        ))),
        None => member.agent.take_library(library).await,
    };
    let taken = match taken {
        Ok(Ok(artifacts)) => match member.agent.take_plans(request.from, plans).await {
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
            let failed = match failed {
                Ok(Err(why)) => ApiError::new(StatusCode::CONFLICT, why),
                Err(unavailable) => unavailable.into(),
                Ok(Ok(_)) => ApiError::new(StatusCode::INTERNAL_SERVER_ERROR, "the move failed"),
            };
            let session = member.record.session.clone();
            return Err(not_to_stay(&app.fleet, session, failed, ApiError::from).await);
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
    coordination: Option<String>,
    coordination_read: Option<String>,
    /// What a client asks of its sends' delivery (send_delivery_state).
    delivery_since: Option<String>,
    delivery_until: Option<String>,
    delivery_stop_generation: Option<String>,
    delivery_bootstrap: Option<String>,
    delivery_runtime_release: Option<String>,
    delivery_runtime_instance: Option<String>,
}

async fn log(
    State(app): State<App>,
    Query(query): Query<LogQuery>,
) -> Result<Json<Value>, ApiError> {
    let mode =
        clarp_kernel::hidden_coordination::Mode::parse(query.coordination.as_deref().unwrap_or(""))
            .map_err(|why| ApiError::new(StatusCode::BAD_REQUEST, why))?;
    let Some(member) = app.anyone(&query.session) else {
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
    // Only an agent that can leave a note for its user has rows for the agents.
    let hides = coordination::can_leave_note(
        &member.record.agent_id,
        Some(&member.record.backend),
        app.turns.has_key(),
    );
    let directory = app.fleet.directory();
    let (global, chat) =
        crate::hidden_coordination::visibility(&directory, &member.record.agent_id);
    let policy = mode.map(|mode| mode.policy(global, chat));
    let which = if hides && policy.is_some_and(|policy| policy.hidden) {
        which.hide_coordination()
    } else {
        which
    };
    // "After revision 0" is how a client that has nothing yet asks: it gets the newest page.
    let after = query.after_revision.filter(|revision| *revision > 0);
    let (page, replace) = if let Some(revision) = after {
        let page = member.agent.after(revision, limit, which).await?;
        // A client that holds another conversation must drop what it has.
        let replace = page.ended_for(revision);
        (page, replace)
    } else if let Some(id) = query.before {
        let older = member.agent.before(id, limit, which).await?;
        if policy.is_some() && older.is_none() {
            return Err(ApiError::new(
                StatusCode::CONFLICT,
                "unknown history cursor",
            ));
        }
        (older.unwrap_or_default(), false)
    } else {
        (member.agent.tail(limit, which).await?, false)
    };
    // This view may have advanced since the actor returned the page. It only
    // supplies conversation identity. The page owns the cursor for its rows.
    let status = &member.agent.view().status;
    let replace = replace
        || (after.is_some()
            && policy.is_some_and(|policy| {
                policy.replaces(query.coordination_read.as_deref().unwrap_or(""))
            }));
    let mut answer = wire::log_response(&member.record, status, &page, replace, hides);
    if let Some(policy) = policy {
        answer["coordination"] = json!(policy);
    }
    crate::hidden_coordination::gaps(&mut answer, &page, &directory);
    answer["includes_automated"] = json!(include_automated);
    let no = |said: &Option<String>| {
        let said = said.as_deref().map(str::to_lowercase);
        matches!(said.as_deref(), Some("0" | "false" | "no"))
    };
    if no(&query.include_tool_details) {
        wire::without_tool_details(&mut answer);
    }
    // The delivery of the user sends: their rows, the scope and the generation, and a page of
    // the feed where the client asks for one. This Host owns its turns itself: it answers in
    // the in-process mode, under an instance of this start of the Host.
    let asked = |name: &str| match name {
        "delivery_since" => query.delivery_since.as_deref(),
        "delivery_until" => query.delivery_until.as_deref(),
        "delivery_stop_generation" => query.delivery_stop_generation.as_deref(),
        "delivery_bootstrap" => query.delivery_bootstrap.as_deref(),
        "delivery_runtime_release" => query.delivery_runtime_release.as_deref(),
        "delivery_runtime_instance" => query.delivery_runtime_instance.as_deref(),
        _ => None,
    };
    let delivery = clarp_kernel::delivery::Query::read(asked)
        .map_err(|why| ApiError::new(StatusCode::BAD_REQUEST, why))?;
    let scope = clarp_kernel::delivery::Scope {
        server_id: app.server_id.clone(),
        agent_id: member.record.agent_id.clone(),
        release: clarp_kernel::delivery::IN_PROCESS_RELEASE.to_string(),
        instance: app.hub.epoch().to_string(),
    };
    let turns = match answer["turns"].take() {
        Value::Array(turns) => turns,
        _ => Vec::new(),
    };
    let (turns, fields) = member.agent.delivery(turns, delivery, scope).await?;
    answer["turns"] = Value::Array(turns);
    if let Some(answer) = answer.as_object_mut() {
        answer.extend(fields);
    }
    // The other agents a row names are named as the roster has them now.
    let directory = app.fleet.directory();
    wire::roster_names(&mut answer, &|agent_id| directory.shown_as(agent_id));
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
    let member = app.anyone(session).ok_or_else(none)?;
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

/// The name of the sender a message names: its session or id.
fn sender_named(named: Option<&Value>) -> Option<&str> {
    match named? {
        Value::String(name) => Some(name.as_str()),
        Value::Object(fields) => fields
            .get("session")
            .or_else(|| fields.get("agent_id"))
            .and_then(Value::as_str),
        _ => None,
    }
}

/// The id of the sender a message names, when it is an agent of this Host (or a janitor);
/// empty otherwise: an agent of another Host, or none.
fn sender_known(app: &App, named: Option<&Value>) -> String {
    let Some(name) = sender_named(named).map(python::strip) else {
        return String::new();
    };
    let directory = app.fleet.directory();
    directory
        .anyone(name)
        .map(|member| member.record.agent_id.clone())
        .unwrap_or_default()
}

/// The agent of this Host with this id, as the sender of a message.
fn sender_by_id(app: &App, agent_id: &str) -> Option<Sender> {
    let directory = app.fleet.directory();
    let mut everyone = directory.members.iter().chain(directory.janitors.iter());
    let member = everyone.find(|member| member.record.agent_id == agent_id)?;
    Some(Sender {
        agent_id: member.record.agent_id.clone(),
        name: member.record.persona.clone(),
        session: member.record.session.clone(),
    })
}

/// Who wrote a message that says it comes from another agent.
pub(crate) fn sender(app: &App, named: Option<&Value>) -> Option<Sender> {
    let directory = app.fleet.directory();
    let name = sender_named(named)?;
    // An agent of the roster, or a janitor: a janitor's own turn may write to an agent.
    let known = directory.anyone(name);
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
    headers: HeaderMap,
    Json(request): Json<SendRequest>,
) -> Response {
    let made = match made_by(&app, &headers, &request).await {
        Ok(made) => made,
        Err(why) => return why.into_response(),
    };
    sent_from(&app, &principal, request, &made).await
}

/// Who made a send beyond what its body says: the turn whose token it presents, a peer Host
/// that names itself, and whether it named its trace (the Python Host's PR 482).
async fn made_by(
    app: &App,
    headers: &HeaderMap,
    request: &SendRequest,
) -> Result<crate::coordination::Made, ApiError> {
    let named = request.trace_id.as_deref().map(python::strip);
    let trace_named = named.is_some_and(|trace| !trace.is_empty());
    let peer = crate::coordination::from_a_peer(headers);
    crate::coordination::made(app, turn_of(headers), peer, trace_named).await
}

/// `POST /orchestrator/route-delegation`: a message the app could not tell the agent of. It
/// is a send that asks the router of spoken messages.
async fn route_delegation(
    State(app): State<App>,
    Extension(principal): Extension<Principal>,
    headers: HeaderMap,
    Json(mut request): Json<SendRequest>,
) -> Response {
    request.orchestrator_fallback = Some(true);
    let made = match made_by(&app, &headers, &request).await {
        Ok(made) => made,
        Err(why) => return why.into_response(),
    };
    sent_from(&app, &principal, request, &made).await
}

/// A message from an app: one that carries its call goes to the agent of the call that can
/// take speech, whoever it names; then it is a message like any other.
async fn sent_from(
    app: &App,
    principal: &Principal,
    mut request: SendRequest,
    made: &crate::coordination::Made,
) -> Response {
    let voice = crate::voice_timeline::Sent::read(&request);
    let call_id: String = python::strip(request.call_id.as_deref().unwrap_or(""))
        .chars()
        .take(64)
        .collect();
    if let Some(opening) = request.call_opening.take()
        && !python::strip(&request.text).is_empty()
    {
        if !opening["token"].is_string() || call_id.is_empty() {
            return ApiError::new(StatusCode::BAD_REQUEST, "bad call_opening").into_response();
        }
        let owner = match crate::calls::caller(principal) {
            Ok(owner) => owner,
            Err(why) => return why.into_response(),
        };
        let trace = request
            .trace_id
            .as_deref()
            .map(python::strip)
            .filter(|s| !s.is_empty())
            .map_or_else(|| ids::hex(8), str::to_owned);
        let message = request
            .client_msg_id
            .as_deref()
            .map(python::strip)
            .filter(|s| !s.is_empty())
            .unwrap_or(&trace)
            .to_owned();
        let change = crate::calls::opening::Change::Greeting {
            body: &opening,
            message: &message,
            trace: &trace,
            session: &request.session,
            call: &call_id,
        };
        let result = match app.calls.greeting(app, &owner, change).await {
            Ok(Ok(answer)) => answer,
            Ok(Err(refused)) => {
                return (
                    StatusCode::from_u16(refused.status).unwrap_or(StatusCode::CONFLICT),
                    Json(refused.body),
                )
                    .into_response();
            }
            Err(failed) => return ApiError::from(failed).into_response(),
        };
        request.force_session = Some(true);
        request.trace_id = Some(trace);
        request.client_msg_id = Some(message.clone());
        let binding = (result["route"] == "greeting").then(|| {
            (
                result["opening"]["ring_id"].as_str().unwrap_or(""),
                message.as_str(),
                result["opening"]["epoch"].as_u64().unwrap_or(1),
            )
        });
        let sent = send_as(app, request, false, binding, made).await;
        if result["route"] == "greeting"
            && let Some(id) = result["opening"]["ring_id"].as_str()
        {
            let admitted = app
                .calls
                .greeting_admitted(app, id, &message, sent.is_ok())
                .await;
            if let Err(why) = admitted {
                app.fleet.unkept("opening", &why.0);
            }
        }
        return match sent {
            Ok(answer) => {
                if let Some(voice) = &voice {
                    voice.posted(app, &answer);
                }
                let mut body = json!(answer);
                body["opening"] = result["opening"].clone();
                Json(body).into_response()
            }
            Err(why) => why.into_response(),
        };
    }
    // The caller, and those of the call who can take speech, for a message that carries one.
    let mut in_call: Option<(&str, Vec<String>)> = None;
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
            Ok(Ok((session, reachable))) => {
                request.session = session;
                in_call = Some((caller, reachable));
            }
            Ok(Err(refused)) => {
                let status = StatusCode::from_u16(refused.status).unwrap_or(StatusCode::CONFLICT);
                return (status, Json(refused.body)).into_response();
            }
            Err(unavailable) => return ApiError::from(unavailable).into_response(),
        }
    }
    // What was dictated hands-free and is not said to be for the agent it names is shown
    // to the router of spoken messages first, when there is one: it may give the message
    // to another agent, ask its user whom it was for, or do what was asked for in it.
    let dictated = request.hands_free == Some(true) && request.force_session != Some(true);
    if dictated && !python::strip(&request.text).is_empty() {
        let trace = match request.trace_id.as_deref().map(python::strip) {
            Some(named) if !named.is_empty() => named.to_string(),
            _ => ids::hex(8),
        };
        request.trace_id = Some(trace.clone());
        let asked = match python::strip(&request.session) {
            "" => addressed::DEFAULT_SESSION.to_string(),
            asked => asked.to_string(),
        };
        // In a call, nobody but those of the call who can take speech can be meant.
        let only = in_call.as_ref().map(|(_, reachable)| reachable.as_slice());
        match crate::router::route(app, &request, &trace, &asked, only).await {
            Ok(crate::router::Routed::NotMine) => {}
            Ok(crate::router::Routed::Answered(status, answer)) => {
                if let (Some((caller, _)), true) = (&in_call, answer.ok) {
                    // Another agent of the call got it: the floor is that agent's.
                    if !answer.session.is_empty() && answer.session != asked {
                        let desk = app.calls.clone();
                        let to = (*caller, call_id.as_str(), answer.session.as_str());
                        let _ = desk.routed(app, to).await;
                    }
                }
                if let Some(voice) = &voice {
                    voice.posted(app, &answer);
                }
                return (status, Json(answer)).into_response();
            }
            Err(refused) => return refused.into_response(),
        }
    }
    match send_as(app, request, false, None, made).await {
        Ok(sent) => {
            if let Some(voice) = &voice {
                voice.posted(app, &sent);
            }
            Json(sent).into_response()
        }
        Err(refused) => refused.into_response(),
    }
}

/// Takes a message for an agent: what `POST /send` does, and what an agent's own tool does
/// when it writes to another agent.
pub(crate) async fn send_message(
    app: &App,
    request: SendRequest,
) -> Result<SendResponse, ApiError> {
    send_as(
        app,
        request,
        false,
        None,
        &crate::coordination::Made::default(),
    )
    .await
}

/// Takes a message for an agent from whoever made it: the turn of an agent's own tool that
/// sends it, as `clarp-admin prompt` does.
pub(crate) async fn send_from(
    app: &App,
    request: SendRequest,
    made: &crate::coordination::Made,
) -> Result<SendResponse, ApiError> {
    send_as(app, request, false, None, made).await
}

/// Takes a message the Host sends by itself: a heartbeat, a prompt at a set time, the task
/// of a fork. It is a message like any other, but for this: no app gave it an id, and the
/// apps are told of it under none.
pub(crate) async fn send_own(app: &App, request: SendRequest) -> Result<SendResponse, ApiError> {
    send_as(
        app,
        request,
        true,
        None,
        &crate::coordination::Made::default(),
    )
    .await
}

async fn send_as(
    app: &App,
    request: SendRequest,
    own: bool,
    binding: Option<(&str, &str, u64)>,
    made: &crate::coordination::Made,
) -> Result<SendResponse, ApiError> {
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
    member.agent.accepting_work()?;
    member.agent.require_here()?;
    // A tool that takes its message on its command line takes no more than fits there:
    // refused before anything is kept, in the Python Host's words.
    let tool = clarp_backend::by_id(&member.record.backend);
    if let Some(most) = tool.and_then(|tool| tool.message_most())
        && text.len() > most
    {
        let why = format!(
            "This message is {} KiB, more than the {} KiB this agent can take in one \
             message. Shorten it or split it into several messages.",
            text.len() / 1024,
            most / 1024
        );
        return Err(ApiError::plain(StatusCode::PAYLOAD_TOO_LARGE, why));
    }
    // Who it comes from. A message that names its sender is that agent's, whatever else it
    // says of itself: on the Python Host a client that names a sender may still say the
    // message is the user's, and the focus and the queue then treat an agent as a person
    // (issue 360 of the repository). Without a sender: the origin it names, when that is one
    // a client may name; otherwise the user. What the Host stamps itself (a janitor, a
    // leader's tick) nobody may claim.
    //
    // Who made it decides too (the Python Host's `SendRequest.from_payload`, its PR 482): a
    // send made from another agent's own turn (the token it presents) is that agent's, its
    // sender where it names none, and never the user's word; a turn of one agent may not name
    // another as its sender; a peer Host forwards only its agents' messages; and an agent
    // waking itself from its own turn is automation.
    let (caller, caller_session) = made.turn.clone().unwrap_or_default();
    let principal = coordination::principal(
        &member.record.agent_id,
        &sender_known(app, request.sender.as_ref()),
        &caller,
        caller_session.as_deref(),
    )
    .map_err(crate::coordination::refused)?;
    let sender = sender(app, request.sender.as_ref()).or_else(|| {
        (!principal.sender_agent_id.is_empty())
            .then(|| sender_by_id(app, &principal.sender_agent_id))
            .flatten()
    });
    if let Some(sender) = &sender
        && app
            .fleet
            .delegation_view
            .has_principal(&member.record.agent_id)
    {
        let delegation = app
            .fleet
            .goal_delegation(crate::fleet::delegations::Request::Current(
                member.record.agent_id.clone(),
            ))
            .await?;
        if delegation["delegate_agent_id"].as_str() == Some(sender.agent_id.as_str()) {
            return Err(ApiError::plain(
                StatusCode::FORBIDDEN,
                "a bookkeeping delegate does not message its principal",
            ));
        }
    }
    let from_agent = || Origin::Agent {
        agent_id: sender
            .as_ref()
            .map(|sender| sender.agent_id.clone())
            .unwrap_or_default(),
    };
    use clarp_kernel::teams::runtime::{self as team, LEADER_TICK};
    let named = if own && request.origin.as_deref() == Some(LEADER_TICK) {
        if !app
            .teams
            .read(|teams, _| team::nudge_allowed(teams, &member.record.agent_id))
        {
            return Err(ApiError::plain(StatusCode::CONFLICT, team::NUDGE_DISABLED));
        }
        LEADER_TICK.to_owned()
    } else {
        coordination::origin(request.origin.as_deref(), &principal, made.peer)
    };
    // Another agent addressing this one: its turn is marked so, with that agent (none for an
    // agent of a peer Host). Decided from the origin as it was claimed and who made it, before
    // this Host maps the origin to its own kinds: the user's word never is, whatever sender it
    // names (`coordination::is_coordination`).
    let coordination = coordination::is_coordination(
        &member.record.agent_id,
        Some(&named),
        Some(&principal.sender_agent_id),
    )
    .then(|| principal.sender_agent_id.clone());
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
        (
            false,
            "oracle" | "schedule" | "automation" | "watcher" | "dreaming" | "heartbeat"
            | LEADER_TICK,
        ) => Origin::Trigger {
            name: named.clone(),
        },
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
    // A message to an agent that is busy waits its turn, as on the Python Host, in the queue,
    // and enters the conversation when its turn starts, whoever sent it (issue 491). What
    // differs is what is told of it. A plain message was said: it keeps its delivery receipt
    // while it waits. One sent with `queue_if_busy` was left for later: it goes through the
    // queue even when nothing is in its way, and when it does not come from a person, a Stop
    // holds it. (The kernel can also take a message that replaces the running turn; nothing
    // asks for it yet.)
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
        managed_model: None,
        sent_at_ms: None,
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
    let user_word = crate::coordination::front_desk_word(app, made).await;
    let guard = if binding.is_some() {
        Some(app.mouth.binding_admission.write().await)
    } else {
        None
    };
    let proof = binding
        .map(|(ring, _, epoch)| json!({"ring_id":ring,"kind":"greeting_reply","epoch":epoch}));
    let marks = clarp_actor::Marks {
        coordination,
        trace_named: made.trace_named,
        user_word,
    };
    let sent = member
        .agent
        .send_marked(message, admission, proof, marks)
        .await?;
    let sent = match sent {
        Ok(sent) => sent,
        // A trace another message of the agent has: no caller attaches its words, and the
        // mark with them, to that message's turn.
        Err(clarp_actor::TraceTaken) => {
            drop(guard);
            return Err(ApiError::new(
                StatusCode::CONFLICT,
                clarp_actor::TRACE_TAKEN,
            ));
        }
    };
    if let (Some((ring, id, epoch)), Some(turn)) = (binding, sent.turn) {
        app.calls
            .bind_clip(
                app,
                ring,
                &member.record.agent_id,
                clarp_kernel::rings::binding::Binding {
                    turn_id: turn.0,
                    kind: clarp_kernel::rings::binding::Kind::GreetingReply,
                    admission_id: id.into(),
                    epoch,
                },
            )
            .await?;
    }
    if sent.duplicate
        && let Some((ring, id, _)) = binding
    {
        app.calls
            .recover_clip(
                app,
                &member,
                ring,
                clarp_kernel::rings::binding::Kind::GreetingReply,
                id,
            )
            .await?;
    }
    drop(guard);
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
        if let Err(why) = app.fleet.open(member.record.session.clone()).await {
            app.fleet.unkept("focus", &why.0);
        }
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
    Extension(principal): Extension<Principal>,
    headers: HeaderMap,
    Json(request): Json<StopRequest>,
) -> Result<Json<StopResponse>, ApiError> {
    let by = crate::controls::control_of(
        &app,
        matches!(principal, Principal::Device { .. }),
        turn_of(&headers),
        request.actor.as_ref(),
        request.reason.as_ref(),
    )
    .await?;
    Ok(Json(
        crate::controls::stop(&app, &request.session, by).await?,
    ))
}

/// The token of the turn a request says it comes from (`X-Clarp-Turn`), where it says so. A
/// header that is no text is a token of nobody.
fn turn_of(headers: &HeaderMap) -> Option<&str> {
    let token = headers.get(crate::turn_identity::HEADER);
    token.map(|token| token.to_str().unwrap_or_default())
}

/// `POST /repair-slot {session, reason, actor?}` (`controls::repair`).
async fn repair_slot(
    State(app): State<App>,
    Extension(principal): Extension<Principal>,
    headers: HeaderMap,
    body: axum::body::Bytes,
) -> Result<Json<Value>, ApiError> {
    let Ok(Value::Object(data)) = serde_json::from_slice::<Value>(&body) else {
        return Err(ApiError::new(StatusCode::BAD_REQUEST, "bad json"));
    };
    let device = matches!(principal, Principal::Device { .. });
    let repaired = crate::controls::repair(&app, &data, device, turn_of(&headers)).await?;
    Ok(Json(repaired))
}

async fn select(
    State(app): State<App>,
    Extension(principal): Extension<Principal>,
    body: axum::body::Bytes,
) -> Result<Json<SelectResponse>, ApiError> {
    let data = crate::personas::object(&body)?;
    let session = body_text(&data, "session")?;
    if session.is_empty()
        || !session
            .chars()
            .all(|c| c.is_alphanumeric() || matches!(c, '.' | '_' | '-'))
    {
        return Err(ApiError::new(StatusCode::BAD_REQUEST, "bad session name"));
    }
    match app.fleet.select(session.to_owned()).await? {
        Some(record) => {
            // The chat this client opened, for what it records next.
            opened_by(&app, &principal, session).await;
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
async fn shown_jobs(
    app: &App,
    now: i64,
) -> Result<Vec<(Option<Arc<Member>>, Job, bool)>, ApiError> {
    let directory = app.fleet.directory();
    let mut jobs: Vec<(Option<Arc<Member>>, Job, bool)> = directory
        .members
        .iter()
        .flat_map(|member| {
            let view = member.agent.view();
            view.jobs
                .iter()
                .map(|job| {
                    (
                        Some(member.clone()),
                        job.clone(),
                        view.mirrored_provider(&job.id),
                    )
                })
                .collect::<Vec<_>>()
        })
        .collect();
    jobs.extend(
        app.fleet
            .computer_jobs()
            .await?
            .into_iter()
            .filter(|job| {
                job.status.is_active() || job.updated_at_ms >= now - crate::agent::JOB_SHOWN_FOR_MS
            })
            .map(|job| (None, job, false)),
    );
    // What runs first, then what ended; within each, the last changed first.
    jobs.sort_by_key(|(_, job, _)| {
        (
            job.status.is_terminal(),
            std::cmp::Reverse(job.updated_at_ms),
        )
    });
    Ok(jobs)
}

/// `GET /background-jobs`: what runs, and what ended within a day.
async fn background_jobs(State(app): State<App>) -> Result<Json<Value>, ApiError> {
    let now = app.clock.now_ms();
    let jobs = shown_jobs(&app, now).await?;
    let (active, ended): (Vec<_>, Vec<_>) =
        jobs.iter().partition(|(_, job, _)| job.status.is_active());
    let listed: Vec<Value> = active
        .iter()
        .chain(ended.iter().take(100))
        .filter(|(_, _, hidden)| !hidden)
        .map(|(member, job, _)| project_job(member.as_deref(), &app.server_id, job, now))
        .collect();
    Ok(Json(json!({
        "jobs": listed,
        "snapshot_revision": app.revisions.latest(),
        "observed_at": now,
    })))
}

fn project_job(member: Option<&Member>, computer_id: &str, job: &Job, now: i64) -> Value {
    match member {
        Some(member) => wire::job(&member.record, job, now),
        None => wire::computer_job(computer_id, job, now),
    }
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
    let job = member.agent.job_record(job_id.to_owned()).await?;
    Ok(job.map(|job| (member.clone(), job)))
}

/// `GET /background-jobs/<id>`: one job with its timeline, its progress and the end of its log.
async fn background_job(
    State(app): State<App>,
    Path(job_id): Path<String>,
    Extension(principal): Extension<Principal>,
) -> Result<Json<Value>, ApiError> {
    let (member, job, timeline) = if let Some((member, job)) = job_and_owner(&app, &job_id).await? {
        let timeline = member.agent.job_timeline(job.id.clone(), 200).await?;
        (Some(member), job, timeline)
    } else if let Some(row) = app.fleet.computer_job(job_id).await? {
        (None, row.job, row.timeline)
    } else {
        return Err(ApiError::new(StatusCode::NOT_FOUND, "job not found"));
    };
    let now = app.clock.now_ms();
    let owner = member.as_ref().map(|m| &m.record);
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
    } else if job.provider_owned() {
        joblog::provider_output(&job.metadata)
    } else {
        let roots = joblog::roots(&job.worker_cwd, owner.map_or("", |r| &r.cwd));
        joblog::tail(&job.log_path, &roots, joblog::TAIL_BYTES)
    };
    Ok(Json(json!({
        "job": project_job(member.as_deref(), &app.server_id, &job, now),
        "handle": job.handle(),
        "is_process": true,
        "owner_session": owner.map_or("", |r| r.session.as_str()),
        "owner_agent_id": owner.map_or("", |r| r.agent_id.as_str()),
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
    let owned = job_and_owner(&app, &job_id).await?;
    if let Some((member, current)) = &owned
        && current.provider_owned()
    {
        let job = wire::job(&member.record, current, app.clock.now_ms());
        let (status, mut receipt) = clarp_kernel::provider_tasks::cancel_refusal(&job);
        if let Some(wanted) = expected.filter(|generation| *generation != current.generation) {
            receipt["error"] = json!("generation_mismatch");
            receipt["expected_generation"] = json!(wanted);
            receipt["current_generation"] = json!(current.generation);
        }
        return Ok((
            StatusCode::from_u16(status).unwrap_or(StatusCode::CONFLICT),
            Json(receipt),
        )
            .into_response());
    }
    let member = owned.map(|(m, _)| m);
    let command = JobCommand::Cancel {
        id: job_id.clone(),
        expected_generation: expected,
        reason: "user_cancelled".to_string(),
    };
    let change = match &member {
        Some(member) => member
            .agent
            .job(command)
            .await?
            .map_err(|refusal| ApiError::new(StatusCode::BAD_REQUEST, refusal.to_string()))?,
        None => {
            if app.fleet.computer_job(job_id).await?.is_none() {
                return Err(ApiError::new(StatusCode::NOT_FOUND, "job not found"));
            }
            app.ears.installs.cancel(&app, command).await?
        }
    };
    let Some(job) = change.job else {
        return Err(ApiError::new(StatusCode::NOT_FOUND, "job not found"));
    };
    let changed = change.record.is_some();
    let now = app.clock.now_ms();
    let mut receipt = json!({
        "changed": changed,
        "job": project_job(member.as_deref(), &app.server_id, &job, now),
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
    if changed && let Some(member) = member {
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
        .anyone(&query.session)
        .ok_or_else(|| ApiError::new(StatusCode::NOT_FOUND, "agent not found"))?;
    let (waiting, state) = member.agent.queue().await?;
    let directory = app.fleet.directory();
    let items: Vec<Value> = waiting
        .iter()
        .map(|item| wire::queue_item(item, &directory))
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
        .anyone(session.trim())
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
    let mut named: Option<(Arc<Member>, TurnId)> = None;
    for member in &directory.members {
        if member.agent.view().status.waiting.is_empty() {
            continue;
        }
        let (waiting, _) = member.agent.queue().await?;
        if let Some(found) = waiting.iter().find(|message| message.client_id == id) {
            // An app's ids are each agent's own here: an id that waits at two agents names
            // neither, and nothing is done to either.
            if named.is_some() {
                return Err(ApiError::new(StatusCode::CONFLICT, AMBIGUOUS_WAITING));
            }
            named = Some((member.clone(), found.turn));
        }
    }
    Ok(named)
}

/// Why a waiting message is not acted on by an id that more than one agent holds waiting.
const AMBIGUOUS_WAITING: &str = "queued message id names more than one agent";

fn not_waiting() -> ApiError {
    ApiError::new(StatusCode::NOT_FOUND, "queued message not found")
}

/// `DELETE /turn-queue/<id>`: its user takes a waiting message back, whether it was said or
/// left for later. It never runs, and it was never in the conversation: no row of it is left.
/// A hold that held nothing else is over.
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
/// turn answers the new words, and its row, written when that turn starts, holds them: a
/// rewording either comes before the start or finds nothing waiting. Only a message that a
/// build before issue 491 put into the conversation while it waited is refused: what is in the
/// conversation was said, and stands.
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

/// The public bootstrap response holds only the information an app compares before pairing.
async fn pairing_identity(State(app): State<App>, Extension(asker): Extension<Asker>) -> Response {
    if let Some(wait) = app.door.identity_refused(&asker.source) {
        return wait;
    }
    Json(json!({
        "server_id": app.server_id,
        "name": app.config.name,
        "contract": { "host": HOST_CONTRACT },
    }))
    .into_response()
}

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
    Extension(principal): Extension<Principal>,
    Json(request): Json<RevokeRequest>,
) -> Result<Json<Value>, ApiError> {
    use clarp_kernel::devices::{self, RevokeRefusal};
    let limited_id = match &principal {
        Principal::Device {
            device_id,
            scope: Scope::Limited,
        } => Some(device_id.as_str()),
        _ => None,
    };
    let device_id = devices::revoke_target(&request.device_id, limited_id)
        .map_err(|why| match why {
            RevokeRefusal::Missing => ApiError::new(StatusCode::BAD_REQUEST, "device_id required"),
            RevokeRefusal::OtherDevice => {
                ApiError::new(StatusCode::FORBIDDEN, devices::LIMITED_REFUSAL)
            }
        })?
        .to_owned();
    if app.fleet.revoke(device_id.clone()).await? {
        // What the device still listens to ends now, not when it next asks.
        let _ = app.door.revoked.send(device_id.clone());
        app.android_chip.forget(&app, &device_id).await?;
        app.calls.retire_openings(&app, Some(&device_id)).await?;
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
    let event = SseEvent::default().data(published.json.clone());
    if published.id == 0 {
        event
    } else {
        event.id(published.id.to_string())
    }
}

fn unnumbered(event: &Value) -> SseEvent {
    SseEvent::default().data(event.to_string())
}

/// Whether an event is one this client is not to be told: a ring it may not be told of.
/// Read off the event as it is written, so that the many that are no ring cost nothing.
async fn not_for(app: &App, published: &Published, principal: &str) -> bool {
    if !published.json.contains("\"agent-ring\"") {
        return false;
    }
    let event: Value = serde_json::from_str(&published.json).unwrap_or(Value::Null);
    let ring_id = event["ring_id"].as_str().unwrap_or("");
    event["type"] == "agent-ring" && !app.calls.shows(app, ring_id, principal).await
}

async fn events(
    State(app): State<App>,
    Extension(principal): Extension<Principal>,
    headers: HeaderMap,
    Query(query): Query<EventsQuery>,
) -> Sse<impl Stream<Item = Result<SseEvent, Infallible>>> {
    // Who listens, as the rings know a client: a phone by its id, anybody else as the
    // administrator.
    let listener = match &principal {
        Principal::Device { device_id, .. } => device_id.clone(),
        _ => clarp_kernel::calls::ADMINISTRATOR.to_string(),
    };
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
    for published in &missed {
        if !not_for(&app, published, &listener).await {
            first.push_back(numbered(published));
        }
    }
    first.push_back(unnumbered(&json!({ "type": "agent-roster" })));
    let clock = app.clock.clone();
    let stream = stream::unfold(
        (first, live, watched),
        move |(mut first, mut live, mut watched)| {
            let clock = clock.clone();
            let (app, listener) = (app.clone(), listener.clone());
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
                        Ok(Heard::Event(Ok(published))) => {
                            // A ring this client may not be told of is passed over.
                            if not_for(&app, &published, &listener).await {
                                continue;
                            }
                            numbered(&published)
                        }
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
    #[tokio::test]
    async fn transfer_coding_is_refused_without_polling_even_one_body_frame() {
        use axum::body::{Body, Bytes};
        use tower::ServiceExt;
        let router = Router::new()
            .fallback(|| async { StatusCode::OK })
            .layer(middleware::from_fn(declared_framing));
        for method in [Method::GET, Method::POST, Method::DELETE] {
            for n in [2, clarp_kernel::request_body::JSON_LIMIT + 1] {
                let read = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
                let polled = read.clone();
                let body = Body::from_stream(futures_util::stream::once(async move {
                    polled.store(true, std::sync::atomic::Ordering::SeqCst);
                    let mut payload = vec![b' '; n];
                    payload[..2].copy_from_slice(b"{}");
                    Ok::<Bytes, std::convert::Infallible>(Bytes::from(payload))
                }));
                let req = Request::builder()
                    .method(method.clone())
                    .uri("/unregistered")
                    .header(header::TRANSFER_ENCODING, "chunked")
                    .body(body)
                    .unwrap();
                let reply = router.clone().oneshot(req).await.unwrap();
                assert_eq!(reply.status(), StatusCode::BAD_REQUEST);
                assert_eq!(reply.headers()[header::CONNECTION], "close");
                assert!(!read.load(std::sync::atomic::Ordering::SeqCst));
            }
        }
    }

    use axum::http::HeaderValue;

    use super::*;

    #[tokio::test]
    async fn a_row_committed_after_a_log_page_is_in_the_next_delta() {
        let place = crate::unkept::at_the_port::place("log-cursor");
        let app = place.app().await;
        let record = app
            .fleet
            .create(CreateAgentRequest {
                name: "Cursor".into(),
                backend: Some("scripted".into()),
                ..CreateAgentRequest::default()
            })
            .await
            .unwrap()
            .unwrap();
        let member = app.anyone(&record.session).unwrap();
        let agent = &member.agent;
        for id in ["first", "second", "third"] {
            assert!(agent.note(id.into(), id.into()).await.unwrap());
        }

        let tail = agent.tail(10, Which::All).await.unwrap();
        let first = tail.entries[0].revision;
        let second = tail.entries[1].revision;
        let third = tail.entries[2].revision;
        let older = agent
            .before("third".into(), 1, Which::All)
            .await
            .unwrap()
            .unwrap();
        let full = agent.after(first, 10, Which::All).await.unwrap();
        let capped = agent.after(first, 1, Which::All).await.unwrap();

        // The actor has supplied the rows, but the edge has not read its view
        // to render the response. Commit through the same mailbox in that gap.
        assert!(
            agent
                .note("later".into(), "arrived during the read".into())
                .await
                .unwrap()
        );
        let view = agent.view();
        assert!(view.status.head_revision > third);

        for (page, cursor, more, expected) in [
            (tail, third, false, &["later"][..]),
            (older, third, true, &["later"][..]),
            (full, third, false, &["later"][..]),
            (capped, second, true, &["third", "later"][..]),
        ] {
            let rendered = wire::log_response(&record, &view.status, &page, false, false);
            assert_eq!(rendered["latest_revision"], cursor);
            assert_eq!(rendered["has_more"], more);
            let query = serde_json::from_value(json!({
                "session": record.session,
                "after_revision": rendered["latest_revision"],
            }))
            .unwrap();
            let Json(delta) = log(State(app.clone()), Query(query)).await.unwrap();
            let rows = delta["turns"].as_array().unwrap();
            let ids: Vec<_> = rows.iter().map(|row| row["id"].as_str().unwrap()).collect();
            assert_eq!(ids.as_slice(), expected);
            assert_eq!(delta["latest_revision"], view.status.head_revision);
        }
    }

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
            (Method::POST, "/paired-devices/revoke", false),
            (Method::POST, "/pairing/issue", true),
            (Method::GET, "/dirs", true),
            (Method::GET, "/past-sessions", true),
            (Method::GET, "/terminal/echo", true),
            (Method::GET, "/terminal/", true),
            (Method::GET, "/terminal", true),
            (Method::GET, "/server-terminal", true),
            (Method::GET, "/personalities/settings", true),
            (Method::GET, "/automation-settings", true),
            (Method::GET, "/agent-files", true),
            (Method::GET, "/agent-file", true),
            (Method::GET, "/backend-auth", true),
            (Method::GET, "/orchestrator/settings", true),
            (Method::GET, "/orchestrator", true),
            (Method::GET, "/calls", true),
            (Method::GET, "/voice-humanness", false),
            (Method::GET, "/persona-avatars/persona-1", true),
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
