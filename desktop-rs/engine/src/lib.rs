//! The Clarp desktop's behaviour without a UI toolkit (ported from the Qt
//! bridge's `AppController`): one Host connection, the roster, the open
//! conversations, the selection and the commands a window issues.
//!
//! Threading: the engine lives on the UI thread. Network replies, event
//! stream signals, timers and keyring lookups arrive on other threads as
//! messages; each one calls the UI's `wake`, and the UI then calls
//! [`Engine::pump`] on its own thread, which applies them and returns what
//! changed. No toolkit type crosses into the engine.

/// A method about one chat: a chat on a peer Host (`@<server_id>/<session>`,
/// `clarp_core::all_hosts`) is that Host's engine's to answer, under its own
/// session; one whose Host is not read (any more) gets the default.
macro_rules! on_peer {
    ($self:ident . $route:ident ($($arg:expr),*), |$engine:ident, $local:ident| $body:expr) => {
        if let Some(peer) = $self.$route($($arg),*) {
            #[allow(clippy::unit_arg)]
            return peer.map(|($engine, $local)| $body).unwrap_or_default();
        }
    };
}

mod avatars;
mod peers;
pub mod blocks;
mod composer;
mod connection;
mod chat_view;
mod coordination;
pub mod lanes;
mod form_events;
pub use form_events::{FormEventStore, FormEvents};
mod voice;
mod narrator;
mod outage;
mod host_status;
pub mod lifecycle;
mod live;
mod queue;
mod send_while_working;
mod stop;
pub mod search;
mod panels;
pub mod profile;
pub mod publications;
pub mod roster_freshness;
mod roster_rows;
mod teams;
pub mod transit;
mod updates;
pub use updates::{TranscriptImageReply, TranscriptImageRequest};
mod viewport;
mod artifact_summary;
mod render_spec;
pub use render_spec::{FormState, FormView};
pub use artifact_summary::Hydration;
pub mod workspace;

use std::collections::{HashMap, HashSet};
use std::sync::Arc;
use std::sync::mpsc::{Receiver, Sender, channel};
use std::time::{Duration, Instant};

use clarp_core::conversation::{Conversation, LoadKind, Op as ConversationOp, Signal as ConversationSignal};
use clarp_core::json::{self, Object};
use clarp_core::protocol::{Agent, display_name};
use clarp_core::roster::Roster;
use clarp_core::settings::{Settings, default_token, normalized_base_url};
use clarp_net::{ApiClient, ApiReply, SseClient, SseSignal};
use serde_json::{Value, json};
use url::Url;

/// A send not confirmed by `/log` within this window is marked failed.
pub const DELIVERY_TIMEOUT: Duration = Duration::from_millis(20_000);
/// Set once this desktop has played an agent's clip: speech is set up here,
/// so a later "not spoken" failure is worth saying.
pub const SPEECH_HEARD: &str = "audio/speechHeard";
/// Snapshots are coalesced to at most one per interval.
pub const SNAPSHOT_MIN_INTERVAL: Duration = Duration::from_millis(700);

/// What a pump changed, for the UI to refresh.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Change {
    Connection,
    ServerInfo,
    /// The saved Hosts changed (`clarp_core::hosts`).
    Hosts,
    Roster,
    /// Archived agents changed.
    Archive,
    /// Agent-to-agent rooms changed (list or unread).
    Rooms,
    Selection,
    /// A conversation's rows or state changed.
    Conversation(String),
    /// A chat's live items, status line or turn changed.
    Live(String),
    Sending,
    Error,
    /// A preference (mute, theme, …) changed.
    Preferences,
    /// The pane layout, the active pane or the save warning changed.
    Panes,
    /// Tool explanations, the narrator's status, level or switch changed:
    /// transcripts present again.
    Narrator,
    /// Voice clips to play (announced, or recovered for the open chat).
    Clips(Vec<Object>),
    /// Speech should stop (a message was sent).
    Silence,
    /// The Host or token changed: `endpoint()` says where audio goes.
    Endpoint,
    /// Diagnostics, speech to text or the voice providers changed.
    HostStatus,
    /// A chat's composer attachments changed.
    Composer(String),
    /// A reply in a chat that is not open: the desktop may notify.
    Notification { title: String, body: String },
    /// Launch data changed: the model catalog, idle contacts, launch
    /// defaults, paths, past sessions, assignment contacts or the
    /// starting contact.
    Launch,
    /// The Host's contact pool had no one free to start.
    LaunchPoolEmpty,
    /// A create, rename, release or other agent setting succeeded (Qt
    /// `agentMutationSucceeded`), for this session.
    AgentMutated(String),
    /// This chat's turn queue changed (items, paused, loading or error).
    Queue(String),
    /// The UI should offer contact assignment for a chat.
    AssignmentRequested { session: String, automatic: bool },
    /// A contact was assigned to this chat.
    AssignmentSucceeded(String),
    /// Attention items, background jobs, artifacts, their loading, error or
    /// pending actions changed.
    Updates,
    /// The background job tracker changed (`agent_processes`).
    Processes,
    /// The team list, the selected team, its messages, loading or error.
    Teams,
    /// The profile's plan, heartbeat, prompts, loading or error.
    Profile,
    Voices,
    /// A chat's media list or a cached image changed.
    Media,
    /// An agent's publications (`publications`), their state or a cached
    /// image of theirs changed.
    Publications,
    Orchestrator,
    /// An agent portrait arrived (`avatar_source`).
    Avatars,
    /// Message search's cached chats were read (`search_messages`).
    Search,
    /// The roster became fresh, refreshing or stale (`roster_freshness`).
    RosterFreshness,
    /// A chat's Stop line changed (`stop_notice`).
    Stopped(String),
}

enum Message {
    Reply(ApiReply),
    Sse(SseSignal),
    Credential { base: String, token: String },
    SnapshotDue,
    /// The back-off after a failed snapshot is over (its token).
    SnapshotRetryDue(u64),
    RoomsDue(u64),
    DeliveryDue { client_id: String, token: u64 },
    DraftsDue(u64),
    PanesWritten(clarp_core::panes::WriteResult),
    Narrator(narrator::NarratorTimer),
    CredentialStored { base: String, result: Result<(), String> },
    CredentialRemoved { base: String, result: Result<(), String> },
    /// Look again for a created session the roster did not show yet.
    CreatedAgentDue(String),
    UpdatesDue,
    /// One Updates list's trailing reload is due.
    UpdatesFeedDue(updates::Feed),
    /// Portraits rounded and written (or not) off the UI thread.
    PortraitsCached(Vec<avatars::Cached>),
    /// A chat's transcript is due to be cached.
    CacheSaveDue(String),
    /// Every cached chat, read for message search off the UI thread.
    SearchCache(Result<Vec<(String, Vec<clarp_core::protocol::Message>)>, String>),
    /// A form's queued events are due to be sent.
    FormEventsDue { key: String, token: u64 },
    /// A saved Host's device token, looked up for reading it as a peer.
    PeerCredential { base: String, token: String, generation: u64 },
    /// Peers that could not be reached are tried again.
    PeersDue(u64),
    /// The quiet time after the Host stopped answering is over (its epoch).
    OutageGraceDue(u64),
    /// `/server-info` is asked again after a failure on the way (its try).
    ServerInfoRetryDue(u64),
    /// A chat's `/log` is read again after a failure on the way (its token).
    LogRetryDue { session: String, token: u64 },
    /// A catch-up round's `/live` is read again (the round's generation).
    LiveRetryDue { session: String, generation: u64 },
}

pub struct Config {
    pub base_url: String,
    /// Empty: look the device credential up in the keyring, then fall back
    /// to the local admin token for a loopback Host.
    pub token: String,
    pub settings: Settings,
    /// Where the pane layout is saved; None keeps it in memory.
    pub workspace_store: Option<std::path::PathBuf>,
    /// Device tokens may be read from and kept in the Secret Service.
    pub keyring: bool,
    /// Where chats are cached between runs; None keeps nothing.
    pub transcript_cache: Option<std::path::PathBuf>,
    /// Where HTML forms' events wait for the Host; None logs none.
    pub form_events: Option<std::path::PathBuf>,
}

impl Config {
    /// From the environment, like the desktop: `CLARP_BASE_URL`,
    /// `CLARP_TOKEN`, else the saved Host and a keyring lookup.
    pub fn from_env(settings: Settings) -> Self {
        let saved = settings.string("connection/baseUrl", "http://127.0.0.1:7682");
        let base_url = normalized_base_url(&std::env::var("CLARP_BASE_URL").unwrap_or(saved));
        let token = std::env::var("CLARP_TOKEN").unwrap_or_default();
        let keyring = std::env::var("CLARP_KEYRING").map_or(true, |v| v != "off");
        Self { base_url, token, settings, workspace_store: workspace::default_store_path(), keyring, transcript_cache: default_transcript_cache(), form_events: form_events::default_root() }
    }
}

/// `CLARP_TRANSCRIPT_CACHE` (`off` for none), else the user's cache dir.
fn default_transcript_cache() -> Option<std::path::PathBuf> {
    match std::env::var("CLARP_TRANSCRIPT_CACHE") {
        Ok(value) if value == "off" => None,
        Ok(value) if !value.is_empty() => Some(value.into()),
        _ => clarp_core::dirs::cache_home().map(|cache| cache.join("clarp").join("transcripts")),
    }
}

/// A chat's cache write waits this long after its last write.
pub const TRANSCRIPT_SAVE_COOLDOWN: Duration = Duration::from_secs(2);
/// While its agent is busy a chat's write is put off by this much.
const TRANSCRIPT_SAVE_BUSY_RETRY: Duration = Duration::from_secs(2);

pub struct Engine {
    base_url: String,
    token: String,
    settings: Settings,
    runtime: Arc<tokio::runtime::Runtime>,
    sender: Sender<Message>,
    receiver: Receiver<Message>,
    wake: Arc<dyn Fn() + Send + Sync>,
    api: ApiClient,
    sse: SseClient,
    changes: Vec<Change>,

    connected: bool,
    connecting: bool,
    connection_state: String,
    error: String,
    error_is_transport: bool,
    /// The Host out of reach for a moment (`outage.rs`).
    outage: outage::OutageState,
    server_name: String,
    server_version: String,

    roster: Roster,
    archived: Roster,
    rooms: Vec<Value>,
    rooms_in_flight: bool,
    rooms_dirty: bool,
    /// The Host answered 404 to `/agent-conversations`: it has no pair rooms,
    /// so it is not asked again until the desktop connects anew.
    rooms_unsupported: bool,
    rooms_refresh: u64,
    selected: String,
    waiting_for_session_choice: bool,
    conversations: HashMap<String, Conversation>,
    muted: bool,
    presentation: clarp_core::presentation::Settings,

    snapshot_generation: u64,
    snapshot_in_flight: bool,
    snapshot_dirty: bool,
    snapshot_scheduled: bool,
    snapshot_last: Option<Instant>,
    /// A retry after a failed snapshot is waiting: its token (0: none).
    /// While it waits, roster events only mark the roster dirty.
    snapshot_retry: u64,
    snapshot_retry_counter: u64,
    snapshot_timeout: Duration,
    freshness: roster_freshness::Freshness,
    log_in_flight: HashSet<String>,
    /// Chats shown from their cached copy: once caught up, the Host's tail
    /// checks their order (`Conversation::adopt_tail_order`).
    order_check: HashSet<String>,
    // Chats just opened from a cached copy, until their first delta: one
    // over a page behind is replaced by the latest page, not walked forward.
    catching_up: HashSet<String>,
    pending_log_mode: HashMap<String, String>,
    coordination: coordination::Coordination,
    lanes: lanes::Lanes,
    transits: transit::Transits,
    chat_view: chat_view::ChatView,
    /// One agent's change patched or read alone (issue 841).
    roster_rows: roster_rows::RosterRows,
    transcript_cache: Option<clarp_core::transcript_cache::TranscriptCache>,
    /// Chats with a cache write scheduled, and when each was last written.
    cache_save_due: HashSet<String>,
    cache_saved_at: HashMap<String, Instant>,
    deliveries: HashMap<String, (String, u64)>,
    /// The chat each /send in flight belongs to and how many POSTs for that
    /// client id are still unanswered. Outlives the delivery timer: a late
    /// answer still belongs to the chat that sent it, never the selection.
    send_owners: HashMap<String, (String, u32)>,
    delivery_counter: u64,
    sending: bool,
    /// In-flight `/message-tool-details` requests: tag → (session, message).
    tool_detail_requests: HashMap<String, (String, String)>,
    /// Drafts not yet written to settings (see `composer.rs`).
    pending_drafts: HashMap<String, String>,
    draft_flush_token: u64,
    /// In-flight `/upload`s: tag → the pending attachment.
    pending_uploads: HashMap<String, Object>,
    panes: workspace::Panes,
    /// The restored layout's active chat, opened once the roster has it.
    restored_session: String,
    restored_from_cache: bool,
    host_status: host_status::HostStatus,
    keyring: bool,
    has_stored_credential: bool,
    /// Optional requests whose failure was already logged once.
    quiet_failures: HashSet<String>,
    stops: stop::Stops,
    /// Chats whose "not spoken" banner showed since their voice last worked:
    /// it shows once per chat, not after every reply.
    voice_errors_shown: HashSet<String>,
    /// The request whose failure the error shows (empty for other errors).
    error_source: String,
    narrator: std::cell::RefCell<clarp_core::narrator::Narrator>,

    // lifecycle
    launch: lifecycle::Launch,
    queue: queue::TurnQueue,
    send_while_working: send_while_working::SendWhileWorking,

    // panels
    updates: updates::Updates,
    artifact_summary: artifact_summary::ArtifactSummary,
    render_specs: render_spec::RenderSpecs,
    teams: teams::Teams,
    profile: profile::Profile,
    publications: publications::Publications,
    avatars: avatars::Avatars,
    live: live::Live,
    form_events: FormEventStore,
    form_events_supported: bool,
    form_event_token: u64,
    /// The Host's `server_id` from `/server-info`.
    server_id: String,
    search: search::SearchState,
    /// The other paired Hosts read while "Chats from all paired Hosts" is
    /// on (`peers.rs`); never on a peer itself.
    peers: peers::Peers,
    /// This engine reads a peer Host for the primary one: it never picks a
    /// chat by itself and keeps no settings of its own.
    peer: bool,
    /// What the transcript cache files this Host's chats under: its URL,
    /// or for a peer whose server id is known, that id.
    cache_key: String,
}

impl Engine {
    /// `wake` is called from any thread whenever there is something to
    /// [`pump`](Self::pump); it must only schedule the pump on the UI thread.
    pub fn new(config: Config, wake: impl Fn() + Send + Sync + 'static) -> Result<Self, String> {
        let runtime = tokio::runtime::Builder::new_multi_thread()
            .worker_threads(2)
            .thread_name("clarp-engine")
            .enable_all()
            .build()
            .map_err(|e| format!("cannot start the network runtime: {e}"))?;
        let engine = Self::build(config, Arc::new(runtime), Arc::new(wake));
        Ok(engine)
    }

    /// An engine on `runtime`, waking the UI with `wake` (a peer shares the
    /// primary's).
    pub(crate) fn build(config: Config, runtime: Arc<tokio::runtime::Runtime>, wake: Arc<dyn Fn() + Send + Sync>) -> Self {
        let (sender, receiver) = channel();
        let post = |sender: Sender<Message>, wake: Arc<dyn Fn() + Send + Sync>| {
            move |message: Message| {
                if sender.send(message).is_ok() {
                    wake();
                }
            }
        };
        let reply_post = post(sender.clone(), wake.clone());
        let api = ApiClient::new(runtime.handle().clone(), move |reply| reply_post(Message::Reply(reply)));
        let sse_post = post(sender.clone(), wake.clone());
        let sse = SseClient::new(runtime.handle().clone(), clarp_core::endpoint::SseTiming::default(), move |signal| {
            sse_post(Message::Sse(signal))
        });
        let muted = config.settings.boolean("audio/muted", false);
        let workspace_store = config.workspace_store.clone();
        let keyring = config.keyring;
        let narrator = narrator::new(&config.settings);
        let tools_visible = config.settings.boolean("conversation/toolsVisible", false);
        let presentation = clarp_core::presentation::Settings {
            show_when_ready: config.settings.boolean("conversation/showWhenReady", false),
            activity_mode: config
                .settings
                .integer("conversation/activityDisplayMode", i64::from(tools_visible))
                .clamp(0, i64::from(clarp_core::presentation::HIDE_WHEN_DONE)) as i32,
            ..clarp_core::presentation::Settings::default()
        };
        // lifecycle
        let launch = lifecycle::Launch::new(&config.settings);
        Self {
            cache_key: normalized_base_url(&config.base_url),
            peers: Default::default(),
            peer: false,
            base_url: normalized_base_url(&config.base_url),
            token: config.token,
            settings: config.settings,
            runtime,
            sender,
            receiver,
            wake,
            api,
            sse,
            changes: Vec::new(),
            connected: false,
            connecting: false,
            connection_state: "offline".into(),
            error: String::new(),
            error_is_transport: false,
            outage: Default::default(),
            server_name: String::new(),
            server_version: String::new(),
            roster: Roster::default(),
            archived: Roster::archived(),
            rooms: Vec::new(),
            rooms_in_flight: false,
            rooms_dirty: false,
            rooms_unsupported: false,
            rooms_refresh: 0,
            selected: String::new(),
            waiting_for_session_choice: false,
            conversations: HashMap::new(),
            muted,
            presentation,
            snapshot_generation: 0,
            snapshot_in_flight: false,
            snapshot_dirty: false,
            snapshot_scheduled: false,
            snapshot_last: None,
            snapshot_retry: 0,
            snapshot_retry_counter: 0,
            snapshot_timeout: roster_freshness::snapshot_timeout(),
            freshness: Default::default(),
            log_in_flight: HashSet::new(),
            order_check: HashSet::new(),
            catching_up: HashSet::new(),
            pending_log_mode: HashMap::new(),
            coordination: coordination::Coordination::default(),
            lanes: lanes::Lanes::default(),
            transits: transit::Transits::default(),
            chat_view: chat_view::ChatView::default(),
            roster_rows: roster_rows::RosterRows::default(),
            transcript_cache: config.transcript_cache.clone().map(clarp_core::transcript_cache::TranscriptCache::new),
            cache_save_due: HashSet::new(),
            cache_saved_at: HashMap::new(),
            tool_detail_requests: HashMap::new(),
            pending_drafts: HashMap::new(),
            draft_flush_token: 0,
            pending_uploads: HashMap::new(),
            restored_session: String::new(),
            restored_from_cache: false,
            host_status: host_status::HostStatus::default(),
            keyring,
            has_stored_credential: false,
            quiet_failures: HashSet::new(),
            stops: stop::Stops::default(),
            voice_errors_shown: HashSet::new(),
            error_source: String::new(),
            narrator,
            panes: workspace::Panes::new(workspace_store),
            deliveries: HashMap::new(),
            send_owners: HashMap::new(),
            delivery_counter: 0,
            sending: false,

            // lifecycle
            launch,
            queue: queue::TurnQueue::default(),
            send_while_working: send_while_working::SendWhileWorking::default(),
            // panels
            updates: Default::default(),
            artifact_summary: Default::default(),
            render_specs: Default::default(),
            teams: Default::default(),
            profile: Default::default(),
            publications: Default::default(),
            avatars: Default::default(),
            live: Default::default(),
            form_events: form_events::store(config.form_events.clone()),
            form_events_supported: false,
            form_event_token: 0,
            server_id: String::new(),
            search: Default::default(),
        }
    }

    fn after(&self, delay: Duration, message: Message) {
        let (sender, wake) = (self.sender.clone(), self.wake.clone());
        self.runtime.spawn(async move {
            tokio::time::sleep(delay).await;
            if sender.send(message).is_ok() {
                wake();
            }
        });
    }

    /// Applies everything that arrived since the last pump.
    pub fn pump(&mut self) -> Vec<Change> {
        while let Ok(message) = self.receiver.try_recv() {
            match message {
                Message::Reply(reply) => self.handle_reply(reply),
                Message::Sse(signal) => self.handle_sse(signal),
                Message::Credential { base, token } => self.credential_looked_up(&base, token),
                Message::RoomsDue(generation) => {
                    if generation == self.rooms_refresh {
                        self.load_agent_conversations();
                    }
                }
                Message::SnapshotDue => {
                    self.snapshot_scheduled = false;
                    self.request_snapshot();
                }
                Message::SnapshotRetryDue(token) => self.snapshot_retry_due(token),
                Message::DeliveryDue { client_id, token } => self.delivery_timed_out(&client_id, token),
                Message::DraftsDue(token) => self.drafts_due(token),
                Message::PanesWritten(result) => self.panes_written(result),
                Message::Narrator(timer) => self.narrator_due(timer),
                Message::CredentialStored { base, result } => self.credential_stored(&base, result),
                Message::CredentialRemoved { base, result } => self.credential_removed(&base, result),
                Message::CreatedAgentDue(session) => self.created_agent_due(&session),
                Message::UpdatesDue => self.updates_due(),
                Message::UpdatesFeedDue(feed) => self.updates_feed_due(feed),
                Message::PortraitsCached(cached) => cached.into_iter().for_each(|c| self.portrait_cached(c)),
                Message::CacheSaveDue(session) => self.cache_save_due(&session),
                Message::SearchCache(chats) => self.search_cache_read(chats),
                Message::FormEventsDue { key, token } => self.form_events_due(&key, token),
                Message::PeerCredential { base, token, generation } => self.peer_credential(&base, token, generation),
                Message::PeersDue(generation) => self.peers_due(generation),
                Message::OutageGraceDue(epoch) => self.outage_grace_due(epoch),
                Message::ServerInfoRetryDue(attempt) => self.server_info_retry_due(attempt),
                Message::LogRetryDue { session, token } => self.log_retry_due(&session, token),
                Message::LiveRetryDue { session, generation } => self.live_retry_due(&session, generation),
            }
        }
        // What the Host said of a chat's view (#702): the primary keeps it.
        if !self.peer {
            let outcomes = self.take_chat_view_outcomes();
            self.settle_chat_views(outcomes);
        }
        // What the panes show, told to the Host once per pump (#905).
        self.sync_viewport();
        self.pump_peers();
        let mut changes = std::mem::take(&mut self.changes);
        let mut seen = HashSet::new();
        changes.retain(|change| matches!(change, Change::Notification { .. }) || seen.insert(format!("{change:?}")));
        changes
    }

    // ---- queries -------------------------------------------------------

    pub fn base_url(&self) -> &str {
        &self.base_url
    }
    pub fn connected(&self) -> bool {
        self.connected
    }
    pub fn connecting(&self) -> bool {
        self.connecting
    }
    pub fn connection_state(&self) -> &str {
        &self.connection_state
    }
    pub fn error(&self) -> &str {
        &self.error
    }
    pub fn server_version(&self) -> &str {
        &self.server_version
    }
    pub fn server_name(&self) -> &str {
        &self.server_name
    }
    /// The chat list: this Host's agents, or with "Chats from all paired
    /// Hosts" on and another Host read, every Host's (`peers.rs`).
    pub fn roster(&self) -> &Roster {
        self.peers.merged.as_ref().unwrap_or(&self.roster)
    }
    /// The Host's `server_id` from `/server-info` (empty before it answered).
    pub fn server_id(&self) -> &str {
        &self.server_id
    }
    /// Whether the roster is current, being fetched, or stale since a
    /// failed snapshot (kept while the engine retries).
    pub fn roster_freshness(&self) -> roster_freshness::State {
        self.freshness.state()
    }
    pub fn archived(&self) -> &Roster {
        &self.archived
    }
    /// Agent-to-agent rooms (`pair:` conversations), each with `unread`.
    pub fn rooms(&self) -> &[Value] {
        &self.rooms
    }
    pub fn unread_rooms(&self) -> usize {
        self.rooms.iter().filter(|room| room.get("unread").and_then(Value::as_bool) == Some(true)).count()
    }
    pub fn room(&self, conversation_id: &str) -> Option<&Object> {
        self.rooms
            .iter()
            .filter_map(Value::as_object)
            .find(|room| room.get("conversation_id").and_then(Value::as_str) == Some(conversation_id))
    }
    /// The name a chat shows: an agent's, or a room's title.
    pub fn chat_name(&self, session: &str) -> String {
        on_peer!(self.peer_ref(session), |engine, local| engine.chat_name(local));
        if session.starts_with("pair:") {
            let title = self.room(session).map(|r| json::string(r, "title")).unwrap_or_default();
            // i18n: chat title of a conversation between agents that has no title of its own
            return if title.is_empty() { clarp_core::tr!("Agent conversation") } else { title };
        }
        self.roster.find(session).map_or_else(|| session.to_owned(), |a| display_name(a).to_owned())
    }
    pub fn selected_session(&self) -> &str {
        &self.selected
    }
    pub fn selected_agent(&self) -> Option<&Agent> {
        self.roster().find(&self.selected)
    }
    pub fn conversation(&self, session: &str) -> Option<&Conversation> {
        on_peer!(self.peer_ref(session), |engine, local| engine.conversation(local));
        self.conversations.get(session)
    }
    pub fn sending(&self) -> bool {
        self.sending
    }
    /// Whether a `/log` request for this chat is still out (tests, benchmarks).
    pub fn log_pending(&self, session: &str) -> bool {
        on_peer!(self.peer_ref(session), |engine, local| engine.log_pending(local));
        self.log_in_flight.contains(session)
    }
    pub fn muted(&self) -> bool {
        self.muted
    }
    /// A conversation as the transcript shows it: tool activity grouped
    /// and collapsed per the activity mode (`clarp_core::presentation`).
    pub fn presented(&mut self, session: &str) -> Vec<clarp_core::presentation::PresentedRow> {
        on_peer!(self.peer_mut(session), |engine, local| engine.presented(local));
        self.present_with_explanations(session, &Default::default(), &Default::default())
    }
    /// The conversation without the rows in `hide` and without the tools
    /// and cells whose ids are in `strip`: what live item rows show instead.
    pub fn presented_except(
        &mut self,
        session: &str,
        hide: &std::collections::HashSet<String>,
        strip: &std::collections::HashSet<String>,
    ) -> Vec<clarp_core::presentation::PresentedRow> {
        // The chat's own Tool activity (this device's), else the global one.
        let mode = self.chat_activity_mode(session);
        self.presented_as(session, hide, strip, mode)
    }
    fn presented_as(
        &mut self,
        session: &str,
        hide: &std::collections::HashSet<String>,
        strip: &std::collections::HashSet<String>,
        mode: i32,
    ) -> Vec<clarp_core::presentation::PresentedRow> {
        on_peer!(self.peer_mut(session), |engine, local| engine.presented_as(local, hide, strip, mode));
        let global = self.presentation.activity_mode;
        self.presentation.activity_mode = mode;
        let rows = self.present_with_explanations(session, hide, strip);
        self.presentation.activity_mode = global;
        rows
    }
    /// The settings key of a chat's own choices on this device (#653):
    /// `@<server_id>/<session>`.
    fn chat_key(&self, session: &str) -> String {
        clarp_core::presentation::chat_key(&self.server_id, session)
    }
    /// A chat's own Tool activity on this device, if it has one (None:
    /// Follow Settings).
    pub fn chat_activity_choice(&self, session: &str) -> Option<i32> {
        clarp_core::presentation::chat_activity(&self.settings, &self.chat_key(session))
    }
    /// The Tool activity a chat shows: its own choice, else the global one.
    pub fn chat_activity_mode(&self, session: &str) -> i32 {
        self.chat_activity_choice(session).unwrap_or(self.presentation.activity_mode)
    }
    /// Sets a chat's own Tool activity (None: it follows the global one).
    pub fn set_chat_activity(&mut self, session: &str, mode: Option<i32>) {
        let key = self.chat_key(session);
        if key.is_empty() || self.chat_activity_choice(session) == mode {
            return;
        }
        clarp_core::presentation::set_chat_activity(&mut self.settings, &key, mode);
        self.changes.push(Change::Preferences);
        self.changes.push(Change::Conversation(session.to_owned()));
    }
    /// Fetches the tool calls of a message the Host sent without them
    /// (`tool_details_available`), once per message at a time.
    pub fn load_tool_details(&mut self, session: &str, message_id: &str) {
        on_peer!(self.peer_mut(session), |engine, local| engine.load_tool_details(local, message_id));
        if session.is_empty()
            || message_id.is_empty()
            || self.tool_detail_requests.values().any(|(s, m)| s == session && m == message_id)
        {
            return;
        }
        let tag = format!("tool-details:{}", uuid::Uuid::new_v4());
        self.tool_detail_requests.insert(tag.clone(), (session.to_owned(), message_id.to_owned()));
        self.api.get(&tag, "/message-tool-details", &[("session", session), ("message_id", message_id)]);
    }
    /// Opens or closes a group of old activity.
    pub fn toggle_group(&mut self, group_id: &str) {
        for peer in &mut self.peers.list {
            peer.engine.presentation.toggle_group(group_id);
        }
        self.presentation.toggle_group(group_id);
        let selected = self.selected.clone();
        self.changes.push(Change::Conversation(selected));
    }
    /// 0 grouped, 1 always visible, 2 group old, 3 hidden
    /// (C++ `activityDisplayMode`, extended with hidden).
    pub fn activity_mode(&self) -> i32 {
        self.presentation.activity_mode
    }
    pub fn set_activity_mode(&mut self, mode: i32) {
        let mode = mode.clamp(0, clarp_core::presentation::HIDE_WHEN_DONE);
        if self.presentation.activity_mode != mode {
            self.presentation.activity_mode = mode;
            self.settings.set("conversation/activityDisplayMode", i64::from(mode));
            self.settings.set("conversation/toolsVisible", mode == clarp_core::presentation::ALWAYS_VISIBLE);
            self.changes.push(Change::Preferences);
            let selected = self.selected.clone();
            self.changes.push(Change::Conversation(selected));
        }
    }
    pub fn show_when_ready(&self) -> bool {
        self.presentation.show_when_ready
    }
    pub fn set_show_when_ready(&mut self, value: bool) {
        if self.presentation.show_when_ready != value {
            self.presentation.show_when_ready = value;
            self.settings.set("conversation/showWhenReady", value);
            self.changes.push(Change::Preferences);
            let selected = self.selected.clone();
            self.changes.push(Change::Conversation(selected));
        }
    }

    /// The reading theme's id, remembered (C++ `setReadingTheme`).
    pub fn reading_theme(&self) -> String {
        clarp_core::reading_theme::normalized_theme_id(
            &self.settings.string("appearance/readingTheme", clarp_core::reading_theme::default_theme_id()),
        )
    }
    pub fn set_reading_theme(&mut self, id: &str) {
        let id = clarp_core::reading_theme::normalized_theme_id(id);
        if id != self.reading_theme() {
            self.settings.set("appearance/readingTheme", id);
            self.changes.push(Change::Preferences);
        }
    }

    /// The reader's own font for `theme` (Settings → Font), if any.
    pub fn font_override(&self, theme: &str) -> Option<clarp_core::reading_theme::FontOverride> {
        use clarp_core::reading_theme as reading;
        reading::font_override(self.settings.get(reading::FONT_OVERRIDES_KEY), &reading::normalized_theme_id(theme))
    }
    /// Sets `theme`'s own font, or removes it (None: the theme's default).
    pub fn set_font_override(&mut self, theme: &str, value: Option<clarp_core::reading_theme::FontOverride>) {
        use clarp_core::reading_theme as reading;
        let theme = reading::normalized_theme_id(theme);
        if self.font_override(&theme) == value {
            return;
        }
        let all = reading::with_font_override(self.settings.get(reading::FONT_OVERRIDES_KEY), &theme, value.as_ref());
        if all.as_object().is_some_and(serde_json::Map::is_empty) {
            self.settings.remove(reading::FONT_OVERRIDES_KEY);
        } else {
            self.settings.set(reading::FONT_OVERRIDES_KEY, all);
        }
        self.changes.push(Change::Preferences);
    }

    /// Voice replies on or off, remembered (C++ `setMuted`).
    pub fn set_muted(&mut self, muted: bool) {
        if self.muted != muted {
            self.muted = muted;
            self.settings.set("audio/muted", muted);
            self.changes.push(Change::Preferences);
        }
    }
    /// Reads the preferences the engine keeps a copy of (voice, streaming,
    /// tool activity and detail) from the settings again: they were changed
    /// through `clarp_core::prefs` or by editing the settings file.
    pub fn reload_preferences(&mut self) {
        let muted = self.settings.boolean("audio/muted", false);
        if self.muted != muted {
            self.muted = muted;
        }
        let show = self.settings.boolean("conversation/showWhenReady", false);
        if self.presentation.show_when_ready != show {
            self.presentation.show_when_ready = show;
            let selected = self.selected.clone();
            self.changes.push(Change::Conversation(selected));
        }
        let tools_visible = self.settings.boolean("conversation/toolsVisible", false);
        let mode = self
            .settings
            .integer("conversation/activityDisplayMode", i64::from(tools_visible))
            .clamp(0, i64::from(clarp_core::presentation::HIDE_WHEN_DONE)) as i32;
        if self.presentation.activity_mode != mode {
            self.set_activity_mode(mode);
        }
        let level = self.settings.integer("experiments/toolDetailLevel", 0).clamp(0, 4) as i32;
        if self.narrator_detail_level() != level {
            self.set_narrator_detail_level(level);
        }
        self.changes.push(Change::Preferences);
        self.share_preferences_with_peers();
        self.apply_all_hosts();
    }
    pub fn settings(&self) -> &Settings {
        &self.settings
    }
    pub fn settings_mut(&mut self) -> &mut Settings {
        &mut self.settings
    }
    pub fn runtime(&self) -> tokio::runtime::Handle {
        self.runtime.handle().clone()
    }

    // ---- connection ----------------------------------------------------

    /// Connects to the configured Host, looking the credential up first
    /// when there is no token.
    pub fn start(&mut self) {
        self.restored_session = self.panes.tree.active_session();
        self.restore_startup_cache();
        self.default_all_hosts_once();
        self.apply_all_hosts();
        if !self.token.is_empty() {
            self.reconnect();
            return;
        }
        self.look_up_credential();
    }

    /// Shows the last cached roster and open pane transcripts on Frame 1
    /// before the Host answers `/agents/snapshot`.
    fn restore_startup_cache(&mut self) {
        if std::env::var_os("CLARP_EMPTY_STARTUP").is_some() {
            return;
        }
        let Some(cache) = self.transcript_cache.clone() else { return };
        let snapshot = cache.load_roster(&self.cache_key);
        if !snapshot.is_empty() && self.roster.agents().is_empty() {
            self.mutate_roster(|r| r.apply_snapshot(&snapshot));
            self.changes.push(Change::Roster);
            self.archived.apply_snapshot(&snapshot);
            if !self.archived.take_ops().is_empty() {
                self.changes.push(Change::Archive);
            }
            self.restored_from_cache = true;
            let restored = self.restored_session.clone();
            let first = Some(restored)
                .filter(|s| !s.is_empty() && (self.roster.find(s).is_some() || s.starts_with("pair:") || self.reads_peer_session(s)))
                .or_else(|| self.roster.first_session().map(str::to_owned));
            if let Some(first) = first.filter(|_| !self.waiting_for_session_choice && !self.peer) {
                self.selected = first.clone();
                self.mutate_roster(|r| r.clear_unread(&first));
                self.ensure_conversation(&first);
                self.changes.push(Change::Selection);
                if self.panes.tree.active_session() != first {
                    self.panes.tree.set_active_session(&first);
                    let _ = self.panes.tree.take_signals();
                    self.changes.push(Change::Panes);
                }
            }
        } else if self.selected.is_empty() && !self.restored_session.is_empty() && !self.waiting_for_session_choice && !self.peer {
            let cached = cache.load(&self.cache_key, &self.restored_session);
            if !cached.is_empty() {
                let first = self.restored_session.clone();
                self.restored_from_cache = true;
                self.selected = first.clone();
                self.ensure_conversation(&first);
                self.changes.push(Change::Selection);
            }
        }
        let mut sessions: Vec<String> = self
            .panes
            .tree
            .view_layout()
            .iter()
            .filter(|pane| pane.get("shown").and_then(Value::as_bool) == Some(true))
            .filter_map(|pane| pane.get("session").and_then(Value::as_str).map(str::to_owned))
            .filter(|s| !s.is_empty())
            .collect();
        if !self.selected.is_empty() && !sessions.contains(&self.selected) {
            sessions.push(self.selected.clone());
        }
        for session in sessions.into_iter().filter(|s| !clarp_core::all_hosts::is_peer_session(s)) {
            self.ensure_conversation(&session);
            let cached = cache.load(&self.cache_key, &session);
            if !cached.is_empty() {
                self.with_conversation(&session, |c| c.restore_cache_snapshot(&cached));
            }
        }
    }

    fn credential_looked_up(&mut self, server: &str, token: String) {
        if normalized_base_url(server) != self.base_url {
            return;
        }
        let stored = !token.is_empty();
        if self.token.is_empty() {
            self.token = if token.is_empty() { default_token(&self.base_url) } else { token };
        }
        if stored != self.has_stored_credential {
            self.has_stored_credential = stored;
            self.changes.push(Change::Connection);
        }
        self.reconnect();
    }

    pub fn reconnect(&mut self) {
        self.rooms_unsupported = false;
        let endpoint = match Url::parse(&self.base_url) {
            Ok(url) if url.host_str().is_some_and(|h| !h.is_empty()) => url,
            _ => {
                // i18n: pairing dialog, error: the Host address is not a web address
                self.set_error(&clarp_core::tr!("Enter a valid Clarp server URL"));
                return;
            }
        };
        self.sse.stop();
        self.sse.set_endpoint(endpoint.clone(), &self.token);
        self.reset_transient_state();
        // The features are read again from /server-info.
        self.reset_launch_capabilities();
        self.reset_live();
        self.reset_lanes();
        self.reset_form_events();
        self.reset_artifact_summary();
        self.reset_render_specs();
        self.reset_roster_rows();
        self.api.set_endpoint(endpoint, &self.token);
        self.changes.push(Change::Endpoint);
        self.set_error("");
        self.set_connecting(true);
        // Still out of reach after the grace: say so again, not "connecting".
        if !self.outage.outage.shown() {
            self.set_connection_state("connecting");
        }
        self.outage_reconnecting();
        self.api.get("server-info", "/server-info", &[]);
        self.force_snapshot();
        if self.restored_from_cache && !self.selected.is_empty() && !clarp_core::all_hosts::is_peer_session(&self.selected) {
            let selected = self.selected.clone();
            if !selected.starts_with("pair:") && !self.roster.find(&selected).is_some_and(|a| a.janitor) {
                self.api.post_json(&format!("select:{selected}"), "/select", json!({"session": selected}), None);
            }
            self.open_log(&selected);
            self.request_recoverable_clips(&selected);
            let shown: Vec<String> = self
                .panes
                .tree
                .view_layout()
                .iter()
                .filter(|pane| pane.get("shown").and_then(Value::as_bool) == Some(true))
                .filter_map(|pane| pane.get("session").and_then(Value::as_str).map(str::to_owned))
                .filter(|s| !s.is_empty() && *s != selected)
                .collect();
            for session in shown {
                self.open_log(&session);
            }
        }
    }

    fn reset_transient_state(&mut self) {
        self.reset_transcript_images();
        self.snapshot_generation += 1;
        self.snapshot_in_flight = false;
        self.snapshot_dirty = false;
        self.snapshot_retry = 0;
        self.freshness.abandoned();
        self.changes.push(Change::RosterFreshness);
        self.pending_log_mode.clear();
        for session in std::mem::take(&mut self.log_in_flight) {
            self.with_conversation(&session, |c| c.set_loading(false));
        }
        for (client, (session, _)) in std::mem::take(&mut self.deliveries) {
            self.with_conversation(&session, |c| c.mark_delivery_failed(&client));
        }
        self.set_sending(false);
    }

    fn set_connecting(&mut self, value: bool) {
        if self.connecting != value {
            self.connecting = value;
            self.changes.push(Change::Connection);
        }
    }
    fn set_connection_state(&mut self, value: &str) {
        if self.connection_state != value {
            self.connection_state = value.into();
            self.changes.push(Change::Connection);
        }
    }
    fn set_error(&mut self, message: &str) {
        self.error_source.clear();
        if self.error != message {
            self.error = message.into();
            self.changes.push(Change::Error);
        }
    }
    pub fn clear_error(&mut self) {
        self.set_error("");
    }
    /// The banner's dismiss: the Host's error and the open conversation's,
    /// which a failed fetch sets too (clearing one leaves the other showing).
    pub fn dismiss_error(&mut self) {
        self.set_error("");
        let session = self.selected.clone();
        on_peer!(self.peer_mut(&session), |engine, _local| engine.dismiss_error());
        self.with_conversation(&session, |c| {
            c.set_error("");
            c.set_voice_error("");
        });
    }
    fn set_sending(&mut self, value: bool) {
        if self.sending != value {
            self.sending = value;
            self.changes.push(Change::Sending);
        }
    }

    // ---- roster --------------------------------------------------------

    /// Fetches the roster now (the Refresh agents command), even while a
    /// retry after a failure is waiting.
    pub fn refresh_agents(&mut self) {
        self.force_snapshot();
    }

    /// A snapshot now: a waiting retry is cancelled (this request is it).
    fn force_snapshot(&mut self) {
        self.snapshot_retry = 0;
        self.request_snapshot();
    }

    /// A snapshot soon. While one is on its way, or a retry after a failure
    /// waits, the roster is only marked dirty: that request (or the one
    /// after it) brings the change, and requests never stack.
    fn request_snapshot(&mut self) {
        if self.snapshot_in_flight || self.snapshot_retry != 0 {
            self.snapshot_dirty = true;
            return;
        }
        if let Some(elapsed) = self.snapshot_last.map(|t| t.elapsed()).filter(|e| *e < SNAPSHOT_MIN_INTERVAL) {
            if !self.snapshot_scheduled {
                self.snapshot_scheduled = true;
                self.after(SNAPSHOT_MIN_INTERVAL - elapsed, Message::SnapshotDue);
            }
            return;
        }
        self.snapshot_in_flight = true;
        // Sent now, it covers every change marked before it (one marked
        // while an earlier request failed in an outage included).
        self.snapshot_dirty = false;
        self.snapshot_last = Some(Instant::now());
        self.snapshot_generation += 1;
        self.freshness.started();
        self.changes.push(Change::RosterFreshness);
        self.api.get_with_timeout(&format!("snapshot:{}", self.snapshot_generation), "/agents/snapshot", self.snapshot_timeout);
    }

    fn complete_snapshot_request(&mut self) {
        self.snapshot_in_flight = false;
        self.snapshot_retry = 0;
        self.freshness.succeeded(std::time::SystemTime::now());
        self.changes.push(Change::RosterFreshness);
        if std::mem::take(&mut self.snapshot_dirty) {
            self.after(Duration::from_millis(100), Message::SnapshotDue);
        }
    }

    /// The roster shown is kept and marked stale; the next try waits out
    /// the back-off and also covers any roster change marked meanwhile.
    fn snapshot_failed(&mut self, detail: &str) {
        self.snapshot_in_flight = false;
        let failures = self.freshness.failed(detail);
        self.changes.push(Change::RosterFreshness);
        let delay = roster_freshness::retry_delay(failures);
        eprintln!("Engine: the agent list is stale ({failures} failed in a row: {detail}); retrying in {} s", delay.as_secs());
        self.snapshot_retry_counter += 1;
        self.snapshot_retry = self.snapshot_retry_counter;
        self.after(delay, Message::SnapshotRetryDue(self.snapshot_retry));
    }

    fn snapshot_retry_due(&mut self, token: u64) {
        if token != self.snapshot_retry {
            return;
        }
        self.snapshot_retry = 0;
        self.snapshot_dirty = false;
        self.request_snapshot();
    }

    fn mutate_roster<R>(&mut self, change: impl FnOnce(&mut Roster) -> R) -> R {
        let result = change(&mut self.roster);
        if !self.roster.take_ops().is_empty() {
            self.changes.push(Change::Roster);
        }
        result
    }

    fn apply_snapshot(&mut self, object: &Object) {
        self.restored_from_cache = false;
        if let Some(cache) = &self.transcript_cache {
            if let Err(error) = cache.save_roster(&self.cache_key, object) {
                eprintln!("clarp-engine: roster cache: {error}");
            }
        }
        if self.live.enabled {
            self.live_explanations(object.get("tool_explanations").and_then(Value::as_object));
        }
        self.mutate_roster(|r| r.apply_snapshot(object));
        self.changes.push(Change::Roster);
        self.archived.apply_snapshot(object);
        if !self.archived.take_ops().is_empty() {
            self.changes.push(Change::Archive);
        }
        self.schedule_agent_conversations();
        self.coordination_reloads();
        self.transit_snapshot();
        self.open_shown_panes();
        if self.lifecycle_snapshot(object) {
            return;
        }
        let selected_known =
            self.roster.find(&self.selected).is_some() || self.selected.starts_with("pair:") || self.reads_peer_session(&self.selected);
        if (self.selected.is_empty() || !selected_known) && !self.peer {
            // A restored layout reopens its active chat when it still exists.
            let restored = std::mem::take(&mut self.restored_session);
            let first = Some(restored)
                .filter(|s| self.roster.find(s).is_some() || s.starts_with("pair:") || self.reads_peer_session(s))
                .or_else(|| self.roster.first_session().map(str::to_owned));
            match first {
                Some(first) if !self.waiting_for_session_choice => self.select(&first),
                _ if !self.selected.is_empty() => {
                    self.selected.clear();
                    self.changes.push(Change::Selection);
                }
                _ => {}
            }
        }
        // Caches behind the Host's head, or on a dead conversation, catch up.
        // Following the panes, a new stream's `stream` event does this for
        // the chats it streams in full, and hidden ones wait for their pane.
        if self.viewport_streaming() {
            return;
        }
        let stale: Vec<(String, bool)> = self
            .conversations
            .iter()
            .filter_map(|(session, conversation)| {
                let agent = self.roster.find(session)?;
                if !conversation.conversation_id().is_empty()
                    && !agent.conversation_id.is_empty()
                    && conversation.conversation_id() != agent.conversation_id
                {
                    Some((session.clone(), true))
                } else if conversation.latest_revision() != agent.head_revision {
                    Some((session.clone(), false))
                } else {
                    None
                }
            })
            .collect();
        for (session, tail) in stale {
            if tail {
                self.request_tail(&session, false);
            } else {
                self.request_delta(&session);
            }
        }
    }

    // ---- agent-to-agent rooms --------------------------------------------

    fn load_agent_conversations(&mut self) {
        if self.rooms_in_flight {
            self.rooms_dirty = true;
            return;
        }
        if self.token.is_empty() || self.rooms_unsupported {
            return;
        }
        self.rooms_in_flight = true;
        self.api.get("agent-conversations", "/agent-conversations", &[]);
    }

    fn schedule_agent_conversations(&mut self) {
        self.rooms_refresh += 1;
        self.after(Duration::from_millis(400), Message::RoomsDue(self.rooms_refresh));
    }

    fn seen_key(&self, conversation_id: &str) -> String {
        clarp_core::settings::agent_conversation_seen_key(&self.base_url, conversation_id)
    }

    fn seen_revision(&self, conversation_id: &str) -> i64 {
        self.settings.integer(&self.seen_key(conversation_id), 0)
    }

    fn mark_agent_conversation_seen(&mut self, conversation_id: &str, revision: i64) {
        if conversation_id.is_empty() || revision <= 0 {
            return;
        }
        if revision > self.seen_revision(conversation_id) {
            let key = self.seen_key(conversation_id);
            self.settings.set(&key, revision);
        }
        let mut changed = false;
        for room in &mut self.rooms {
            if room.get("conversation_id").and_then(Value::as_str) == Some(conversation_id)
                && room.get("unread").and_then(Value::as_bool) == Some(true)
            {
                room["unread"] = json!(false);
                changed = true;
            }
        }
        if changed {
            self.changes.push(Change::Rooms);
        }
    }

    fn apply_agent_conversations(&mut self, conversations: &[Value]) {
        let mut rooms = Vec::new();
        let mut behind = Vec::new();
        for room in conversations.iter().filter_map(Value::as_object) {
            let id = json::string(room, "conversation_id");
            if !id.starts_with("pair:") {
                continue;
            }
            let latest = room.get("latest_revision").and_then(Value::as_i64).unwrap_or(0);
            if id == self.selected {
                self.mark_agent_conversation_seen(&id, latest);
            }
            let mut room = room.clone();
            room.insert("unread".into(), json!(latest > self.seen_revision(&id)));
            room.insert("session".into(), json!(id));
            rooms.push(Value::Object(room));
            if self.conversations.get(&id).is_some_and(|c| c.latest_revision() < latest) {
                behind.push(id);
            }
        }
        for id in behind {
            self.request_delta(&id);
        }
        if self.rooms != rooms {
            self.rooms = rooms;
            self.changes.push(Change::Rooms);
        }
    }

    fn refresh_pair_conversations_for(&mut self, session: &str) {
        let agent_id = self.roster.find(session).map(|a| a.agent_id.clone()).unwrap_or_default();
        if !agent_id.is_empty() {
            let related: Vec<String> =
                self.conversations.keys().filter(|id| id.starts_with("pair:") && id.contains(&agent_id)).cloned().collect();
            for id in related {
                self.request_delta(&id);
            }
        }
        self.schedule_agent_conversations();
    }

    // ---- selection and conversations ------------------------------------

    pub fn select(&mut self, session: &str) {
        if session.is_empty() {
            return;
        }
        if self.select_on_peer(session) {
            return;
        }
        self.waiting_for_session_choice = false;
        let changed = self.selected != session;
        let left = std::mem::replace(&mut self.selected, session.to_owned());
        if changed {
            self.end_coordination_visits(session);
            // The chat left takes its "not spoken" banner with it.
            self.with_conversation(&left, |c| c.set_voice_error(""));
        }
        self.mutate_roster(|r| r.clear_unread(session));
        if changed {
            self.ensure_conversation(session);
            self.changes.push(Change::Selection);
            if self.panes.tree.active_session() != session {
                self.with_panes(|panes| panes.set_active_session(session));
            }
        }
        self.live_chat_opened(session);
        if session.starts_with("pair:") {
            // A pair room is a projection, not an agent: no Host focus.
            let latest = self.room(session).and_then(|r| r.get("latest_revision")).and_then(Value::as_i64).unwrap_or(0);
            self.mark_agent_conversation_seen(session, latest);
            self.open_log(session);
            return;
        }
        let janitor = self.roster.find(session).is_some_and(|a| a.janitor);
        if !janitor {
            self.api.post_json(&format!("select:{session}"), "/select", json!({"session": session}), None);
        }
        self.open_log(session);
        self.request_recoverable_clips(session);
    }

    /// Opening a chat: what this client holds (in memory, else its cached
    /// copy, shown at once) and the delta after it; the tail when nothing.
    pub(crate) fn open_log(&mut self, session: &str) {
        on_peer!(self.peer_mut(session), |engine, local| engine.open_log(local));
        self.ensure_conversation(session);
        let empty = self.conversations.get(session).is_none_or(|c| c.is_empty() && c.latest_revision() == 0);
        if empty && let Some(cache) = &self.transcript_cache {
            let snapshot = cache.load(&self.cache_key, session);
            if !snapshot.is_empty() && self.with_conversation(session, |c| c.restore_cache_snapshot(&snapshot)).unwrap_or(false) {
                self.order_check.insert(session.to_owned());
            }
        }
        if self.conversations.get(session).is_some_and(|c| c.latest_revision() > 0) {
            self.catching_up.insert(session.to_owned());
            self.request_delta(session);
        } else {
            self.request_tail(session, false);
        }
    }

    /// A chat's rows changed from the Host: cache them a moment later, at
    /// most once per cooldown.
    fn schedule_cache_save(&mut self, session: &str) {
        if self.transcript_cache.is_none() || session.is_empty() || !self.coordination_cacheable(session) || !self.cache_save_due.insert(session.to_owned()) {
            return;
        }
        let delay = Duration::from_millis(clarp_core::transcript_cache::SAVE_DELAY_MS);
        let cooling = self.cache_saved_at.get(session).map_or(Duration::ZERO, |at| TRANSCRIPT_SAVE_COOLDOWN.saturating_sub(at.elapsed()));
        self.after(delay.max(cooling), Message::CacheSaveDue(session.to_owned()));
    }

    /// Writes a chat's durable rows off the UI thread; not while its agent
    /// is busy (the reply is still changing), then once it settles.
    fn cache_save_due(&mut self, session: &str) {
        if self.roster.find(session).is_some_and(|a| a.busy) || self.log_in_flight.contains(session) {
            self.after(TRANSCRIPT_SAVE_BUSY_RETRY, Message::CacheSaveDue(session.to_owned()));
            return;
        }
        self.cache_save_due.remove(session);
        if !self.coordination_cacheable(session) {
            return;
        }
        let (Some(cache), Some(conversation)) = (self.transcript_cache.clone(), self.conversations.get(session)) else { return };
        if conversation.latest_revision() <= 0 {
            return;
        }
        let snapshot = conversation.cache_snapshot();
        self.cache_saved_at.insert(session.to_owned(), Instant::now());
        let (base, session) = (self.cache_key.clone(), session.to_owned());
        self.runtime.spawn_blocking(move || {
            if let Err(error) = cache.save(&base, &session, &snapshot) {
                eprintln!("clarp-engine: transcript cache: {error}");
            }
        });
    }

    fn ensure_conversation(&mut self, session: &str) {
        if session.is_empty() || self.conversations.contains_key(session) || clarp_core::all_hosts::is_peer_session(session) {
            return;
        }
        let mut conversation = Conversation::new();
        conversation.open_session(session);
        conversation.take_ops();
        self.conversations.insert(session.to_owned(), conversation);
    }

    /// Runs `change` on a conversation and records what it changed; a
    /// confirmed delivery ends that send's wait.
    fn with_conversation<R>(&mut self, session: &str, change: impl FnOnce(&mut Conversation) -> R) -> Option<R> {
        let conversation = self.conversations.get_mut(session)?;
        let result = change(conversation);
        let ops = conversation.take_ops();
        if !ops.is_empty() {
            self.changes.push(Change::Conversation(session.to_owned()));
            self.search.changed(session);
        }
        for op in ops {
            match op {
                ConversationOp::Signal(ConversationSignal::DeliveryConfirmed(client_id)) => self.delivery_confirmed(&client_id),
                // The Host's conversation moved on: load it afresh.
                ConversationOp::Signal(ConversationSignal::ReplacementRequired) => self.request_tail(session, true),
                _ => {}
            }
        }
        Some(result)
    }

    pub fn refresh_session(&mut self, session: &str) {
        on_peer!(self.peer_mut(session), |engine, local| engine.refresh_session(local));
        self.request_tail(session, false);
    }

    pub fn load_older(&mut self, session: &str) {
        on_peer!(self.peer_mut(session), |engine, local| engine.load_older(local));
        if session.is_empty() || self.log_in_flight.contains(session) {
            return;
        }
        self.ensure_conversation(session);
        let before = match self.conversations.get(session) {
            Some(c) if c.has_more() && !c.is_empty() => c.rows()[0].id.clone(),
            _ => return,
        };
        self.log_in_flight.insert(session.to_owned());
        self.with_conversation(session, |c| c.set_loading(true));
        let mut query = vec![("session", session), ("limit", "100"), ("include_automated", "0"), ("before", before.as_str())];
        query.extend(self.coordination_query(session, false));
        self.log_request_sent(session, true);
        self.api.get(&format!("log-older:{session}"), "/log", &query);
    }

    fn begin_log_request(&mut self, session: &str, mode: &str) -> bool {
        if !self.log_in_flight.contains(session) {
            self.log_in_flight.insert(session.to_owned());
            return true;
        }
        let queued = self.pending_log_mode.get(session).cloned().unwrap_or_default();
        if queued.is_empty() || mode == "replace" || (mode == "tail" && queued != "replace") {
            self.pending_log_mode.insert(session.to_owned(), mode.to_owned());
        }
        false
    }

    fn continue_pending_log_request(&mut self, session: &str) {
        if self.log_in_flight.contains(session) {
            return;
        }
        match self.pending_log_mode.remove(session).as_deref() {
            Some("tail") => self.request_tail(session, false),
            Some("replace") => self.request_tail(session, true),
            Some("delta") => self.request_delta(session),
            _ => {}
        }
    }

    fn request_tail(&mut self, session: &str, replace: bool) {
        let session = if session.is_empty() { self.selected.clone() } else { session.to_owned() };
        on_peer!(self.peer_mut(&session), |engine, local| engine.request_tail(local, replace));
        // A chat no pane shows reads its history once it is shown (#905).
        let read = if replace { viewport::Read::Replace } else { viewport::Read::Tail };
        if session.is_empty() || !self.history_now(&session, read) {
            return;
        }
        let replace = replace || self.take_deferred(&session) == Some(viewport::Read::Replace);
        if !self.begin_log_request(&session, if replace { "replace" } else { "tail" }) {
            return;
        }
        self.ensure_conversation(&session);
        self.with_conversation(&session, |c| c.set_loading(true));
        let tag = format!("{}{session}", if replace { "log-replace:" } else { "log-tail:" });
        let mut query = vec![("session", session.as_str()), ("limit", "100"), ("include_automated", "0")];
        query.extend(self.coordination_query(&session, false));
        self.log_request_sent(&session, false);
        self.api.get(&tag, "/log", &query);
    }

    fn request_delta(&mut self, session: &str) {
        let session = if session.is_empty() { self.selected.clone() } else { session.to_owned() };
        on_peer!(self.peer_mut(&session), |engine, local| engine.request_delta(local));
        if session.is_empty() || !self.history_now(&session, viewport::Read::Delta) {
            return;
        }
        // A tail asked while the chat was hidden goes ahead instead.
        match self.take_deferred(&session) {
            Some(viewport::Read::Replace) => return self.request_tail(&session, true),
            Some(viewport::Read::Tail) => return self.request_tail(&session, false),
            _ => {}
        }
        if !self.begin_log_request(&session, "delta") {
            return;
        }
        self.ensure_conversation(&session);
        let after = self.conversations.get(&session).map_or(0, |c| c.latest_revision()).to_string();
        let mut query = vec![("session", session.as_str()), ("limit", "100"), ("include_automated", "0"), ("after_revision", after.as_str())];
        query.extend(self.coordination_query(&session, true));
        self.log_request_sent(&session, false);
        self.api.get(&format!("log-delta:{session}"), "/log", &query);
    }

    fn apply_log(&mut self, session: &str, object: &Object, kind: LoadKind) {
        self.log_in_flight.remove(session);
        let answered = self.outage.log_sent.remove(session);
        self.ensure_conversation(session);
        if kind == LoadKind::Delta && json::boolean(object, "has_more") && self.catching_up.remove(session) {
            // A chat opened from a copy over a page behind: its latest page
            // replaces it, so it opens at the end. Walking the backlog forward
            // a page at a time showed the chat far in the past while it
            // loaded (and every page moved the view); older rows come on
            // scrolling up, as for a chat opened fresh.
            self.order_check.remove(session);
            self.request_tail(session, true);
            self.continue_pending_log_request(session);
            self.catch_up_log(session, answered);
            return;
        }
        self.with_conversation(session, |c| c.apply_log(object, kind));
        if kind == LoadKind::Delta {
            self.mutate_roster(|r| r.apply_log_delta(session, object));
        }
        self.schedule_cache_save(session);
        if kind == LoadKind::Delta && json::boolean(object, "has_more") {
            self.request_delta(session);
        } else if kind == LoadKind::Delta && self.order_check.remove(session) {
            // A cached copy kept the order it was saved in: once caught up,
            // the Host's tail puts drifted rows back.
            self.request_tail(session, false);
        } else if kind != LoadKind::Delta {
            self.order_check.remove(session);
        }
        if kind != LoadKind::Older {
            self.catching_up.remove(session);
        }
        self.continue_pending_log_request(session);
        self.viewport_log_settled(session);
        self.catch_up_log(session, answered);
    }

    // ---- sending ---------------------------------------------------------

    pub fn send(&mut self, text: &str) {
        let session = self.selected.clone();
        self.send_to(&session, text, false);
    }

    pub fn send_to(&mut self, session: &str, text: &str, queue_if_busy: bool) {
        on_peer!(self.peer_mut(session), |engine, local| engine.send_to(local, text, queue_if_busy));
        self.send_with_voice(session, text, queue_if_busy, "", "", false);
    }

    /// A send, carrying the dictation it came from (trace and
    /// transcription ids, hands-free) when there was one.
    pub(crate) fn send_with_voice(&mut self, session: &str, text: &str, queue_if_busy: bool, trace: &str, transcription: &str, hands_free: bool) {
        on_peer!(self.peer_mut(session), |engine, local| engine.send_with_voice(local, text, queue_if_busy, trace, transcription, hands_free));
        let trimmed = text.trim();
        if trimmed.is_empty() || session.is_empty() {
            return;
        }
        self.ensure_conversation(session);
        self.mutate_roster(|r| r.record_outgoing_activity(session));
        let client_id = uuid::Uuid::new_v4().to_string();
        self.with_conversation(session, |c| c.add_optimistic(&client_id, trimmed));
        self.post_send(session, &client_id, trimmed, queue_if_busy, trace, transcription, hands_free);
    }

    /// Sends a failed message again under its own id (CLIENT-SEND-500).
    pub(crate) fn resend_failed(&mut self, session: &str, message_id: &str) {
        on_peer!(self.peer_mut(session), |engine, local| engine.resend_failed(local, message_id));
        let Some((client_id, text)) = self.with_conversation(session, |c| c.resend_failed(message_id)).flatten() else { return };
        self.post_send(session, &client_id, &text, false, "", "", false);
    }

    /// POST /send for a row already shown, and wait for its answer.
    fn post_send(&mut self, session: &str, client_id: &str, text: &str, queue_if_busy: bool, trace: &str, transcription: &str, hands_free: bool) {
        let trimmed = text;
        let client_id = client_id.to_owned();
        self.set_sending(true);
        let mut body = json!({
            // "client" tells the Host the desktop sent it, so the reply is
            // not pushed to the phone.
            "session": session, "text": trimmed, "client_msg_id": client_id, "client": "desktop",
            "synthesize_audio": !self.muted, "hands_free": hands_free, "queue_if_busy": queue_if_busy,
        });
        if !trace.is_empty() {
            body["trace_id"] = json!(trace);
        }
        self.send_while_working_field(&mut body);
        if !transcription.is_empty() {
            body["transcription_id"] = json!(transcription);
        }
        // Your own words stop the voice that was speaking.
        self.changes.push(Change::Silence);
        self.api.post_json(&format!("send:{client_id}"), "/send", body, None);
        self.send_owners.entry(client_id.clone()).or_insert_with(|| (session.to_owned(), 0)).1 += 1;
        self.delivery_counter += 1;
        let token = self.delivery_counter;
        self.deliveries.insert(client_id.clone(), (session.to_owned(), token));
        self.after(DELIVERY_TIMEOUT, Message::DeliveryDue { client_id, token });
    }

    fn delivery_timed_out(&mut self, client_id: &str, token: u64) {
        let Some((session, _)) = self.deliveries.get(client_id).filter(|(_, t)| *t == token).cloned() else { return };
        self.deliveries.remove(client_id);
        self.with_conversation(&session, |c| c.mark_delivery_failed(client_id));
        if self.deliveries.is_empty() {
            self.set_sending(false);
        }
    }

    /// One /send for `client_id` was answered: the chat it belongs to.
    fn send_answered(&mut self, client_id: &str) -> Option<String> {
        let (session, unanswered) = self.send_owners.get_mut(client_id)?;
        let session = session.clone();
        *unanswered -= 1;
        if *unanswered == 0 {
            self.send_owners.remove(client_id);
        }
        Some(session)
    }

    fn delivery_confirmed(&mut self, client_id: &str) {
        self.deliveries.remove(client_id);
        if self.deliveries.is_empty() {
            self.set_sending(false);
        }
    }

    pub fn stop(&mut self) {
        let session = self.selected.clone();
        self.stop_session(&session);
    }

    pub fn stop_session(&mut self, session: &str) {
        on_peer!(self.peer_mut(session), |engine, local| engine.stop_session(local));
        if !session.is_empty() {
            self.stop_requested(session);
            self.api.post_json(&format!("stop:{session}"), "/stop", json!({"session": session}), None);
        }
    }

    // ---- replies -----------------------------------------------------------

    fn handle_reply(&mut self, reply: ApiReply) {
        match reply {
            ApiReply::Json { tag, object } => self.handle_json(&tag, &object),
            ApiReply::Failed { tag, message, status } => self.handle_failure(&tag, &message, status),
            ApiReply::Bytes { tag, bytes, content_type } => self.panels_bytes(&tag, &bytes, &content_type),
        }
    }

    fn handle_json(&mut self, tag: &str, object: &Object) {
        if self.form_events_json(tag, object) {
            return;
        }
        if self.lifecycle_json(tag, object) {
            return;
        }
        if self.render_spec_json(tag, object) || self.panels_json(tag, object) {
            return;
        }
        if tag == "agent-conversations" {
            self.rooms_in_flight = false;
            if std::mem::take(&mut self.rooms_dirty) {
                self.schedule_agent_conversations();
            }
            self.apply_agent_conversations(&json::array(object, "conversations"));
            return;
        }
        if tag.starts_with("composer-upload:") {
            self.finish_upload(tag, Some(object));
            return;
        }
        if self.voice_json(tag, object) || self.narrator_json(tag, object) {
            return;
        }
        if self.host_status_json(tag, object) || self.stop_json(tag, object) || self.host_search_json(tag, object) {
            return;
        }
        if tag.starts_with("tool-details:") {
            if let Some((session, message_id)) = self.tool_detail_requests.remove(tag) {
                self.ensure_conversation(&session);
                self.with_conversation(&session, |c| c.apply_tool_details(&message_id, object));
            }
            return;
        }
        if tag == "pairing" {
            self.handle_pairing(object);
            return;
        }
        if self.live_json(tag, object) || self.lanes_json(tag, object) || self.coordination_json(tag, object) || self.transit_json(tag, object) || self.chat_view_json(tag) || self.roster_rows_json(tag, object) || self.send_while_working_json(tag, object) {
            return;
        }
        if tag == "server-info" {
            self.outage.server_info_attempt = 0;
            self.keep_working_token();
            self.server_name = object.get("name").and_then(Value::as_str).unwrap_or("Clarp").to_owned();
            self.server_version = json::string(object, "clarp_version");
            self.changes.push(Change::ServerInfo);
            // A Host that answered is kept for switching back to it.
            let before = clarp_core::hosts::list(&self.settings);
            clarp_core::hosts::remember(&mut self.settings, &self.base_url, &self.server_name);
            clarp_core::hosts::set_identity(&mut self.settings, &self.base_url, "", &json::string(object, "server_id"));
            if clarp_core::hosts::list(&self.settings) != before {
                self.changes.push(Change::Hosts);
            }
            self.set_connecting(false);
            self.force_snapshot();
            self.live_server_info(object);
            self.mutate_roster(|r| r.set_stall_feature(clarp_core::stall::offered_by(object)));
            self.mutate_roster(|r| r.set_availability_feature(clarp_core::availability::offered_by(object)));
            self.coordination_server_info(object);
            self.lanes_server_info(object);
            self.launch_server_info(object);
            self.transit_server_info(object);
            self.chat_view_server_info(object);
            self.roster_rows_server_info(object);
            self.form_events_server_info(object);
            self.stop_server_info(object);
            self.search_server_info(object);
            self.queue_server_info(object);
            self.send_while_working_server_info(object);
            self.artifact_summary_server_info(object);
            self.live_rewatch();
            self.sse.start();
        } else if let Some(generation) = tag.strip_prefix("snapshot:") {
            if generation.parse::<u64>().ok() != Some(self.snapshot_generation) {
                return;
            }
            self.complete_snapshot_request();
            self.apply_snapshot(object);
            // A fresh roster for a catch-up round (`outage.rs`).
            let generation = self.snapshot_generation;
            self.catch_up_snapshot(generation);
        } else if let Some(session) = tag.strip_prefix("log-tail:") {
            self.apply_log(session, object, LoadKind::Tail);
        } else if let Some(session) = tag.strip_prefix("log-replace:") {
            self.apply_log(session, object, LoadKind::Replace);
        } else if let Some(session) = tag.strip_prefix("log-delta:") {
            self.apply_log(session, object, LoadKind::Delta);
        } else if let Some(session) = tag.strip_prefix("log-older:") {
            self.apply_log(session, object, LoadKind::Older);
        } else if let Some(client_id) = tag.strip_prefix("send:") {
            // The 2xx is what makes this send delivered (CLIENT-SEND-500), in
            // the chat that sent it, even after its timer marked it failed.
            if let Some(session) = self.send_answered(client_id) {
                self.send_while_working_send_answer(&session, object);
                self.with_conversation(&session, |c| c.confirm_delivery(client_id));
                self.request_delta(&session);
            }
        }
        // select: and other acknowledgements need no action.
    }

    fn handle_failure(&mut self, tag: &str, message: &str, status: u16) {
        let detail = if status > 0 { format!("{message} (HTTP {status})") } else { message.to_owned() };
        eprintln!("Engine: {tag} failed: {detail}");
        self.stop_failure(tag);
        if self.form_events_failure(tag, message, status) || self.host_status_failed(tag, &detail) || self.narrator_failure(tag, status) || self.live_failure(tag, &detail, status) || self.lanes_failure(tag, message, status) || self.coordination_failure(tag, &detail) || self.transit_failure(tag, message, status) || self.chat_view_failure(tag) || self.send_while_working_failure(tag, message) || self.roster_rows_failure(tag, status) || self.host_search_failure(tag, message, status) {
            return;
        }
        if tag == "desktop-presence" || tag == "application-activity" || tag == "client-event" {
            // Older or offline Hosts ignore these optional leases and
            // diagnostics: say so once, never as a chat error.
            if self.quiet_failures.insert(tag.to_owned()) {
                eprintln!("Engine: {tag} is not accepted by this Host: {detail}");
            }
            return;
        }
        if self.lifecycle_failure(tag, message, status) {
            return;
        }
        if self.render_spec_failure(tag, message, status) || self.panels_failure(tag, message, status) {
            return;
        }
        if tag.starts_with("composer-upload:") {
            self.finish_upload(tag, None);
            self.set_error(&if status > 0 { format!("{message} (HTTP {status})") } else { message.to_owned() });
            return;
        }
        if let Some((session, message_id)) = self.tool_detail_requests.remove(tag) {
            // Left available, so opening the row again retries.
            eprintln!("Engine: tool details for {session}/{message_id} failed ({status}): {message}");
            return;
        }
        if let Some(generation) = tag.strip_prefix("snapshot:") {
            // Not a banner: the explorer and the switcher say the list is
            // stale, and the engine retries on its own.
            if generation.parse::<u64>().ok() == Some(self.snapshot_generation) {
                self.snapshot_failed(&detail);
                self.catch_up_snapshot_failed(status);
            }
            return;
        }
        if tag == "agent-conversations" {
            self.rooms_in_flight = false;
            if std::mem::take(&mut self.rooms_dirty) {
                self.schedule_agent_conversations();
            }
            // A Host without this route simply has no pair rooms to show; it
            // is said once and not asked again until the next connection.
            if status == 404 {
                if !self.rooms_unsupported {
                    eprintln!("clarp-engine: this Host has no pair conversations (GET /agent-conversations: HTTP 404)");
                }
                self.rooms_unsupported = true;
            } else {
                eprintln!("clarp-engine: agent conversations failed: {message} (HTTP {status})");
            }
            if !self.rooms.is_empty() {
                self.rooms.clear();
                self.changes.push(Change::Rooms);
            }
            return;
        }
        let detail = if status > 0 { format!("{message} (HTTP {status})") } else { message.to_owned() };
        // On the way to the Host, a read: kept quiet, tried again, caught up.
        if self.outage_read_failed(tag, &detail, status) {
            return;
        }
        self.set_error(&detail);
        self.error_source = tag.to_owned();
        // No HTTP status: the Host was unreachable. Reconnecting answers it.
        self.error_is_transport = status == 0;
        if tag == "server-info" || tag == "pairing" {
            self.set_connecting(false);
            self.set_connection_state(if status == 401 { "unauthorized" } else { "offline" });
        } else if tag.starts_with("log-") {
            let session = tag.split_once(':').map(|(_, s)| s.to_owned()).unwrap_or_default();
            self.log_in_flight.remove(&session);
            self.pending_log_mode.remove(&session);
            self.with_conversation(&session, |c| {
                c.set_loading(false);
                c.set_error(&detail);
            });
            self.viewport_log_settled(&session);
            // The Host answered (with a refusal): a catch-up round has
            // nothing more to read for this chat.
            let answered = self.outage.log_sent.remove(&session);
            self.catch_up_log(&session, answered);
        } else if let Some(client_id) = tag.strip_prefix("send:") {
            // The chat that sent it, also after its timer marked it failed.
            let owner = self.send_answered(client_id);
            let timed = self.deliveries.remove(client_id).map(|(session, _)| session);
            if let Some(session) = timed.or(owner) {
                // The Host answered with a reason (e.g. 413 'too long for this
                // agent'): the bubble says it, so Resend is not pressed blindly.
                let reason = if status > 0 { message } else { "" };
                self.with_conversation(&session, |c| c.mark_delivery_failed_because(client_id, reason));
            }
            if self.deliveries.is_empty() {
                self.set_sending(false);
            }
        }
    }

    fn handle_sse(&mut self, signal: SseSignal) {
        self.panels_sse(&signal);
        match signal {
            SseSignal::Connected(connected) => {
                if self.connected != connected {
                    self.connected = connected;
                    self.changes.push(Change::Connection);
                }
                if connected {
                    // Live at once, or after a catch-up round (`outage.rs`).
                    self.outage_stream_connected();
                } else if self.sse.running() {
                    // Dropped: the stream tries again by itself. The list,
                    // the open chat and any host-updating line stay as they
                    // are for the grace (`outage.rs`); the next snapshot
                    // ends a host-updating line.
                    self.live_stream_dropped();
                    self.viewport_dropped();
                    self.mutate_roster(Roster::stream_dropped);
                    self.outage_stream_dropped();
                } else {
                    // A deliberate stop: the caller already set the state.
                    self.live_stream_dropped();
                    self.viewport_dropped();
                    self.mutate_roster(Roster::mark_transport_unavailable);
                    let sessions: Vec<String> = self.conversations.keys().cloned().collect();
                    for session in sessions {
                        self.with_conversation(&session, Conversation::clear_running_activity);
                    }
                }
            }
            SseSignal::Error(message) => {
                // On the way to the Host: quiet for the grace, then the
                // calm out-of-reach treatment; never the raw words.
                eprintln!("Engine: event stream: {message}");
                self.outage_failed(&message);
            }
            SseSignal::Rejected { message, .. } => {
                // The Host itself refused (a token it no longer knows): said
                // at once; a stream that connects again clears it, as before.
                self.set_error(&message);
                self.error_is_transport = true;
            }
            SseSignal::Event(event) => self.handle_event(&event),
            SseSignal::Resubscribed => self.live_resubscribed(),
        }
    }

    fn handle_event(&mut self, event: &Object) {
        let kind = json::string(event, "type");
        let session = json::string(event, "session");
        self.lifecycle_event(&kind, &session);
        self.send_while_working_event(&kind, &session, event);
        match kind.as_str() {
            "audio" => {
                // A clip for the chat means its voice works again: a later
                // failure may say so once more. Speech is now set up here.
                if !self.settings.boolean(SPEECH_HEARD, false) {
                    self.settings.set(SPEECH_HEARD, true);
                }
                if !session.is_empty() {
                    self.voice_errors_shown.remove(&session);
                    self.ensure_conversation(&session);
                    self.with_conversation(&session, |c| c.set_voice_error(""));
                }
                self.changes.push(Change::Clips(vec![event.clone()]));
            }
            "tts-error" => {
                // Speech that is muted here, or never worked on this desktop
                // (a Host without a voice), is not an error the user asked
                // to hear about; and a chat says it once, not every reply.
                if self.muted || !self.settings.boolean(SPEECH_HEARD, false) || (!session.is_empty() && !self.voice_errors_shown.insert(session.clone())) {
                    return;
                }
                let message = event.get("message").and_then(Value::as_str).map_or_else(|| json::string(event, "error"), str::to_owned);
                if session.is_empty() {
                    self.set_error(&message);
                } else {
                    self.ensure_conversation(&session);
                    self.with_conversation(&session, |c| c.set_voice_error(&message));
                }
            }
            "agent-transit" => self.transit_event(event),
            "agent-roster" => {
                self.chat_view_roster(event);
                self.roster_event(event);
            }
            clarp_core::availability::HOST_UPDATING_EVENT => self.mutate_roster(|r| r.apply_host_updating(event)),
            "delivery-updated" => {
                // F4: a send's delivery state changed without a new revision;
                // the hint carries no state, so re-read the chat's tail.
                if self.conversations.contains_key(&session) {
                    self.request_tail(&session, false);
                }
            }
            "transcript-updated" => {
                if self.conversations.contains_key(&session) {
                    self.request_delta(&session);
                }
                self.refresh_pair_conversations_for(&session);
            }
            "live" => self.live_event(event),
            "stream" => self.viewport_stream(event),
            "live-changed" => self.viewport_changed(event),
            "lane-settings-updated" => self.lanes_event(),
            "agent-state" => {
                self.mutate_roster(|r| r.apply_state_event(event));
                self.stop_state_event(&session, event);
                let state = json::string(event, "kind");
                // Live items show what the agent does: no placeholder rows.
                if self.live_owns(&session) {
                    return;
                }
                let persona = self.roster.find(&session).map(|a| display_name(a).to_owned()).unwrap_or(session.clone());
                if state == "thinking" {
                    self.with_conversation(&session, |c| c.show_transient_thinking(&persona));
                } else if matches!(state.as_str(), "tool" | "waiting" | "interrupted" | "done" | "idle" | "stopped") {
                    self.with_conversation(&session, Conversation::clear_running_activity);
                }
            }
            "agent-activity" => {
                self.mutate_roster(|r| r.apply_activity_event(event));
                self.stop_activity_event(&session, event);
                let mut status =
                    event.get("activity_status").and_then(Value::as_str).map_or_else(|| json::string(event, "status"), str::to_owned);
                if status.is_empty() && clarp_core::protocol::is_busy_state(&json::string(event, "state")) {
                    status = "running".into();
                }
                let busy = self.roster.find(&session).map(|a| a.busy);
                if self.live_owns(&session) {
                    // The live items and the status line replace activity rows.
                } else if status != "running" || busy.is_none_or(|b| b) {
                    self.with_conversation(&session, |c| c.apply_activity_event(event));
                }
            }
            "agent-focus" => self.mutate_roster(|r| r.apply_focus_event(event)),
            "queue-updated" => self.mutate_roster(|r| r.apply_queue_event(event)),
            "user-notification" => {
                self.request_snapshot();
                self.mutate_roster(|r| r.apply_notification_event(event));
                if session == self.selected {
                    self.mutate_roster(|r| r.clear_unread(&session));
                } else {
                    let title = event.get("persona").and_then(Value::as_str).unwrap_or(&session).to_owned();
                    let body = event
                        .get("preview_parts")
                        .and_then(Value::as_object)
                        .and_then(clarp_core::chat_preview::line)
                        .unwrap_or_else(|| clarp_core::decision_receipt::preview_line(&json::string(event, "preview")));
                    self.changes.push(Change::Notification { title, body });
                }
            }
            "server-version" => {
                let version = json::string(event, "version");
                if !version.is_empty() && version != self.server_version {
                    self.server_version = version;
                    self.changes.push(Change::ServerInfo);
                    self.request_snapshot();
                }
            }
            "remote-action" if json::string(event, "action") == "stop-agent" => self.stop(),
            // Unknown types are ignored by contract (additive-only).
            _ => {}
        }
    }
}

impl Engine {
    /// Writes what waits in memory (drafts, the pane layout) and stops the
    /// event stream; for a window that exits without dropping the engine.
    pub fn shutdown(&mut self) {
        self.sse.stop();
        self.flush_drafts();
        self.flush_panes();
    }
}

impl Drop for Engine {
    fn drop(&mut self) {
        self.sse.stop();
        self.flush_drafts();
        self.flush_panes();
    }
}

/// The name a roster row shows.
pub fn agent_name(agent: &Agent) -> &str {
    display_name(agent)
}
