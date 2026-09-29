//! Rust replacement for `desktop/src/app/AppController`, ported in slices.
//! It owns the child models QML reaches through its properties, drives the
//! Host protocol through `clarp-net`, and turns every network result into a
//! queued call on the Qt thread.
//!
//! Slice 1: connection, agent snapshot, conversation log sync, sending with
//! delivery confirmation, stop, and SSE dispatch for roster/transcript events.

use std::collections::{HashMap, HashSet};
use std::pin::Pin;
use std::time::{Duration, Instant};

use clarp_core::conversation::LoadKind;
use clarp_core::endpoint::SseTiming;
use clarp_core::json::{self, Object};
use clarp_core::protocol::{display_name, is_busy_state};
use clarp_core::settings::{Settings, default_token, normalized_base_url};
use clarp_net::{ApiClient, ApiReply, SseClient, SseSignal};
use cxx::UniquePtr;
use cxx_qt::{ConnectionType, CxxQtThread, CxxQtType, QMetaObjectConnectionGuard, Threading};
use cxx_qt_lib::QString;
use serde_json::{Value, json};
use url::Url;

use super::agent_list_model::qobject::{AgentListModel, new_agent_list_model};
use super::conversation_model::qobject::{ConversationModel, new_conversation_model};
use super::directory_models::qobject::{ContactListModel, VoiceListModel, new_contact_list_model, new_voice_list_model};
use super::pane_tree_model::qobject::{PaneTreeModel, new_pane_tree_model};

#[cxx_qt::bridge]
pub mod qobject {
    unsafe extern "C++" {
        include!("cxx-qt-lib/qstring.h");
        type QString = cxx_qt_lib::QString;

        include!("clarp-desktop/src/bridge/agent_list_model.cxxqt.h");
        type AgentListModel = crate::bridge::agent_list_model::qobject::AgentListModel;
        include!("clarp-desktop/src/bridge/conversation_model.cxxqt.h");
        type ConversationModel = crate::bridge::conversation_model::qobject::ConversationModel;
        include!("clarp-desktop/src/bridge/directory_models.cxxqt.h");
        type ContactListModel = crate::bridge::directory_models::qobject::ContactListModel;
        type VoiceListModel = crate::bridge::directory_models::qobject::VoiceListModel;
        include!("clarp-desktop/src/bridge/pane_tree_model.cxxqt.h");
        type PaneTreeModel = crate::bridge::pane_tree_model::qobject::PaneTreeModel;
    }

    extern "RustQt" {
        #[qobject]
        #[qml_element]
        #[qproperty(*mut AgentListModel, agents, READ = agents_value, CONSTANT)]
        #[qproperty(*mut AgentListModel, archived_agents, cxx_name = "archivedAgents", READ = archived_agents_value, CONSTANT)]
        #[qproperty(*mut ContactListModel, contacts, READ = contacts_value, CONSTANT)]
        #[qproperty(*mut PaneTreeModel, panes, READ = panes_value, CONSTANT)]
        #[qproperty(*mut VoiceListModel, voices, READ = voices_value, CONSTANT)]
        #[qproperty(*mut ConversationModel, conversation, READ = conversation_value, NOTIFY = conversation_changed)]
        #[qproperty(QString, base_url, cxx_name = "baseUrl", READ = base_url_value, WRITE = set_base_url, NOTIFY = base_url_changed)]
        #[qproperty(QString, selected_session, cxx_name = "selectedSession", READ = selected_session_value, NOTIFY = selected_session_changed)]
        #[qproperty(QString, selected_name, cxx_name = "selectedName", READ = selected_name_value, NOTIFY = selected_agent_changed)]
        #[qproperty(QString, selected_state, cxx_name = "selectedState", READ = selected_state_value, NOTIFY = selected_agent_changed)]
        #[qproperty(QString, selected_backend, cxx_name = "selectedBackend", READ = selected_backend_value, NOTIFY = selected_agent_changed)]
        #[qproperty(bool, connected, READ = connected_value, NOTIFY = connected_changed)]
        #[qproperty(bool, connecting, READ = connecting_value, NOTIFY = connecting_changed)]
        #[qproperty(bool, sending, READ = sending_value, NOTIFY = sending_changed)]
        #[qproperty(QString, connection_state, cxx_name = "connectionState", READ = connection_state_value, NOTIFY = connection_state_changed)]
        #[qproperty(QString, error_message, cxx_name = "errorMessage", READ = error_message_value, NOTIFY = error_message_changed)]
        #[qproperty(QString, server_name, cxx_name = "serverName", READ = server_name_value, NOTIFY = server_info_changed)]
        #[qproperty(QString, server_version, cxx_name = "serverVersion", READ = server_version_value, NOTIFY = server_info_changed)]
        #[qproperty(bool, show_when_ready, cxx_name = "showWhenReady", READ = show_when_ready_value, WRITE = set_show_when_ready, NOTIFY = show_when_ready_changed)]
        #[qproperty(bool, tools_visible, cxx_name = "toolsVisible", READ = tools_visible_value, WRITE = set_tools_visible, NOTIFY = tools_visible_changed)]
        #[qproperty(i32, activity_display_mode, cxx_name = "activityDisplayMode", READ = activity_display_mode_value, WRITE = set_activity_display_mode, NOTIFY = tools_visible_changed)]
        #[qproperty(QString, composer_focus_pane, cxx_name = "composerFocusPane", READ = composer_focus_pane_value, NOTIFY = composer_focus_pane_changed)]
        #[qproperty(u64, agent_revision, cxx_name = "agentRevision", READ = agent_revision_value, NOTIFY = agent_revision_changed)]
        type AppController = super::AppControllerRust;
    }

    unsafe extern "RustQt" {
        fn agents_value(self: &AppController) -> *mut AgentListModel;
        fn archived_agents_value(self: &AppController) -> *mut AgentListModel;
        fn contacts_value(self: &AppController) -> *mut ContactListModel;
        fn panes_value(self: &AppController) -> *mut PaneTreeModel;
        fn voices_value(self: &AppController) -> *mut VoiceListModel;
        fn conversation_value(self: &AppController) -> *mut ConversationModel;
        fn base_url_value(self: &AppController) -> QString;
        #[cxx_name = "setBaseUrl"]
        fn set_base_url(self: Pin<&mut AppController>, value: QString);
        fn selected_session_value(self: &AppController) -> QString;
        fn selected_name_value(self: &AppController) -> QString;
        fn selected_state_value(self: &AppController) -> QString;
        fn selected_backend_value(self: &AppController) -> QString;
        fn connected_value(self: &AppController) -> bool;
        fn connecting_value(self: &AppController) -> bool;
        fn sending_value(self: &AppController) -> bool;
        fn connection_state_value(self: &AppController) -> QString;
        fn error_message_value(self: &AppController) -> QString;
        fn server_name_value(self: &AppController) -> QString;
        fn server_version_value(self: &AppController) -> QString;
        fn show_when_ready_value(self: &AppController) -> bool;
        #[cxx_name = "setShowWhenReady"]
        fn set_show_when_ready(self: Pin<&mut AppController>, value: bool);
        fn tools_visible_value(self: &AppController) -> bool;
        #[cxx_name = "setToolsVisible"]
        fn set_tools_visible(self: Pin<&mut AppController>, value: bool);
        fn activity_display_mode_value(self: &AppController) -> i32;
        #[cxx_name = "setActivityDisplayMode"]
        fn set_activity_display_mode(self: Pin<&mut AppController>, mode: i32);
        fn composer_focus_pane_value(self: &AppController) -> QString;
        fn agent_revision_value(self: &AppController) -> u64;

        #[qsignal]
        #[cxx_name = "conversationChanged"]
        fn conversation_changed(self: Pin<&mut AppController>);
        #[qsignal]
        #[cxx_name = "baseUrlChanged"]
        fn base_url_changed(self: Pin<&mut AppController>);
        #[qsignal]
        #[cxx_name = "selectedSessionChanged"]
        fn selected_session_changed(self: Pin<&mut AppController>);
        #[qsignal]
        #[cxx_name = "selectedAgentChanged"]
        fn selected_agent_changed(self: Pin<&mut AppController>);
        #[qsignal]
        #[cxx_name = "connectedChanged"]
        fn connected_changed(self: Pin<&mut AppController>);
        #[qsignal]
        #[cxx_name = "connectingChanged"]
        fn connecting_changed(self: Pin<&mut AppController>);
        #[qsignal]
        #[cxx_name = "sendingChanged"]
        fn sending_changed(self: Pin<&mut AppController>);
        #[qsignal]
        #[cxx_name = "connectionStateChanged"]
        fn connection_state_changed(self: Pin<&mut AppController>);
        #[qsignal]
        #[cxx_name = "errorMessageChanged"]
        fn error_message_changed(self: Pin<&mut AppController>);
        #[qsignal]
        #[cxx_name = "serverInfoChanged"]
        fn server_info_changed(self: Pin<&mut AppController>);
        #[qsignal]
        #[cxx_name = "showWhenReadyChanged"]
        fn show_when_ready_changed(self: Pin<&mut AppController>);
        #[qsignal]
        #[cxx_name = "toolsVisibleChanged"]
        fn tools_visible_changed(self: Pin<&mut AppController>);
        #[qsignal]
        #[cxx_name = "composerFocusPaneChanged"]
        fn composer_focus_pane_changed(self: Pin<&mut AppController>);
        #[qsignal]
        #[cxx_name = "agentRevisionChanged"]
        fn agent_revision_changed(self: Pin<&mut AppController>);
        #[qsignal]
        #[cxx_name = "notificationRequested"]
        fn notification_requested(self: Pin<&mut AppController>, title: QString, body: QString);

        #[qinvokable]
        #[cxx_name = "connectToServer"]
        fn connect_to_server(self: Pin<&mut AppController>, url: &QString, token: &QString);
        #[qinvokable]
        fn reconnect(self: Pin<&mut AppController>);
        #[qinvokable]
        #[cxx_name = "selectSession"]
        fn select_session(self: Pin<&mut AppController>, session: &QString);
        #[qinvokable]
        #[cxx_name = "refreshConversation"]
        fn refresh_conversation(self: Pin<&mut AppController>);
        #[qinvokable]
        #[cxx_name = "refreshSession"]
        fn refresh_session(self: Pin<&mut AppController>, session: &QString);
        #[qinvokable]
        #[cxx_name = "loadOlder"]
        fn load_older(self: Pin<&mut AppController>);
        #[qinvokable]
        #[cxx_name = "loadOlderSession"]
        fn load_older_session(self: Pin<&mut AppController>, session: &QString);
        #[qinvokable]
        #[cxx_name = "sendMessage"]
        fn send_message(self: Pin<&mut AppController>, text: &QString, queue_if_busy: bool);
        #[qinvokable]
        #[cxx_name = "sendMessageTo"]
        fn send_message_to(self: Pin<&mut AppController>, session: &QString, text: &QString, queue_if_busy: bool);
        #[qinvokable]
        #[cxx_name = "retryFailedMessage"]
        fn retry_failed_message(self: Pin<&mut AppController>, session: &QString, message_id: &QString);
        #[qinvokable]
        #[cxx_name = "retryLatestFailedMessage"]
        fn retry_latest_failed_message(self: Pin<&mut AppController>);
        #[qinvokable]
        #[cxx_name = "stopAgent"]
        fn stop_agent(self: Pin<&mut AppController>);
        #[qinvokable]
        #[cxx_name = "stopSession"]
        fn stop_session(self: Pin<&mut AppController>, session: &QString);
        #[qinvokable]
        #[cxx_name = "clearError"]
        fn clear_error(self: Pin<&mut AppController>);
        #[qinvokable]
        #[cxx_name = "conversationForSession"]
        fn conversation_for_session(self: Pin<&mut AppController>, session: &QString) -> *mut ConversationModel;
        #[qinvokable]
        #[cxx_name = "agentName"]
        fn agent_name(self: &AppController, session: &QString) -> QString;
        #[qinvokable]
        #[cxx_name = "agentState"]
        fn agent_state(self: &AppController, session: &QString) -> QString;
        #[qinvokable]
        #[cxx_name = "agentBackend"]
        fn agent_backend(self: &AppController, session: &QString) -> QString;
        #[qinvokable]
        #[cxx_name = "agentWorkingDirectory"]
        fn agent_working_directory(self: &AppController, session: &QString) -> QString;
        #[qinvokable]
        #[cxx_name = "agentQueueCount"]
        fn agent_queue_count(self: &AppController, session: &QString) -> i32;
        #[qinvokable]
        #[cxx_name = "nextAttentionSession"]
        fn next_attention_session(self: &AppController) -> QString;
        #[qinvokable]
        #[cxx_name = "requestComposerFocus"]
        fn request_composer_focus(self: Pin<&mut AppController>, pane_id: &QString);
        #[qinvokable]
        #[cxx_name = "refreshAgents"]
        fn refresh_agents(self: Pin<&mut AppController>);
    }

    impl cxx_qt::Threading for AppController {}
    impl cxx_qt::Initialize for AppController {}
}

use qobject::AppController;

/// A send not confirmed by `/log` within this window is marked failed.
const DELIVERY_TIMEOUT: Duration = Duration::from_millis(20_000);
/// Snapshots are coalesced to at most one per interval.
const SNAPSHOT_MIN_INTERVAL: Duration = Duration::from_millis(700);

fn qs(text: &str) -> QString {
    QString::from(text)
}

fn pointer<T>(model: &UniquePtr<T>) -> *mut T
where
    T: cxx::memory::UniquePtrTarget,
{
    model.as_ref().map_or(std::ptr::null_mut(), |m| m as *const T as *mut T)
}

/// Back-pointer for queued handlers of child-model signals; each handler's
/// guard is owned by the controller, so it never outlives it.
struct ControllerPtr(*mut AppController);
unsafe impl Send for ControllerPtr {}

impl ControllerPtr {
    fn get(&self) -> *mut AppController {
        self.0
    }
}

/// A child model the controller owns; null until `initialize`.
pub struct Owned<T: cxx::memory::UniquePtrTarget>(UniquePtr<T>);

impl<T: cxx::memory::UniquePtrTarget> Default for Owned<T> {
    fn default() -> Self {
        Self(UniquePtr::null())
    }
}

impl<T: cxx::memory::UniquePtrTarget> std::ops::Deref for Owned<T> {
    type Target = UniquePtr<T>;
    fn deref(&self) -> &UniquePtr<T> {
        &self.0
    }
}

impl<T: cxx::memory::UniquePtrTarget> std::ops::DerefMut for Owned<T> {
    fn deref_mut(&mut self) -> &mut UniquePtr<T> {
        &mut self.0
    }
}

#[derive(Default)]
pub struct AppControllerRust {
    agents: Owned<AgentListModel>,
    archived: Owned<AgentListModel>,
    contacts: Owned<ContactListModel>,
    panes: Owned<PaneTreeModel>,
    voices: Owned<VoiceListModel>,
    empty_conversation: Owned<ConversationModel>,
    conversations: HashMap<String, UniquePtr<ConversationModel>>,
    current: Option<String>,
    guards: Vec<QMetaObjectConnectionGuard>,

    api: Option<ApiClient>,
    sse: Option<SseClient>,
    settings: Settings,
    base_url: String,
    token: String,

    selected_session: String,
    waiting_for_session_choice: bool,
    connected: bool,
    connecting: bool,
    sending: bool,
    connection_state: String,
    error_message: String,
    error_is_transport: bool,
    server_name: String,
    server_version: String,
    show_when_ready: bool,
    activity_display_mode: i32,
    composer_focus_pane: String,
    agent_revision: u64,

    snapshot_generation: u64,
    snapshot_in_flight: bool,
    snapshot_dirty: bool,
    snapshot_scheduled: bool,
    snapshot_last: Option<Instant>,
    log_in_flight: HashSet<String>,
    pending_log_mode: HashMap<String, String>,
    /// client_msg_id → (session, timer token)
    deliveries: HashMap<String, (String, u64)>,
    delivery_counter: u64,
}

impl cxx_qt::Initialize for AppController {
    fn initialize(mut self: Pin<&mut Self>) {
        {
            let mut rust = self.as_mut().rust_mut();
            rust.agents = Owned(new_agent_list_model());
            rust.archived = Owned(new_agent_list_model());
            rust.contacts = Owned(new_contact_list_model());
            rust.panes = Owned(new_pane_tree_model());
            rust.voices = Owned(new_voice_list_model());
            rust.empty_conversation = Owned(new_conversation_model());
            rust.settings = Settings::user();
            let saved = rust.settings.string("connection/baseUrl", "http://127.0.0.1:7682");
            rust.base_url = normalized_base_url(&std::env::var("CLARP_BASE_URL").unwrap_or(saved));
            rust.token = std::env::var("CLARP_TOKEN").unwrap_or_else(|_| default_token());
            rust.show_when_ready = rust.settings.boolean("conversation/showWhenReady", false);
            let tools_visible = rust.settings.boolean("conversation/toolsVisible", false);
            rust.activity_display_mode =
                rust.settings.integer("conversation/activityDisplayMode", i64::from(tools_visible)).clamp(0, 2) as i32;
            rust.waiting_for_session_choice = std::env::var_os("CLARP_EMPTY_STARTUP").is_some();
            rust.connection_state = "offline".into();
        }
        if let Some(archive) = self.as_mut().rust_mut().archived.as_mut() {
            archive.make_archive();
        }
        let focus = self.panes.as_ref().map(|p| p.core().active_pane_id().to_owned()).unwrap_or_default();
        self.as_mut().rust_mut().composer_focus_pane = focus;

        let qt = self.qt_thread();
        let api_thread = qt.clone();
        let api = ApiClient::new(crate::runtime::handle(), move |reply| {
            let tag = match &reply {
                ApiReply::Json { tag, .. } | ApiReply::Bytes { tag, .. } | ApiReply::Failed { tag, .. } => tag.clone(),
            };
            if api_thread.queue(move |controller| controller.handle_reply(reply)).is_err() {
                eprintln!("AppController: dropped reply {tag}; the controller is gone");
            }
        });
        let sse_thread = qt.clone();
        let sse = SseClient::new(crate::runtime::handle(), SseTiming::default(), move |signal| {
            if sse_thread.queue(move |controller| controller.handle_sse(signal)).is_err() {
                eprintln!("AppController: dropped an SSE signal; the controller is gone");
            }
        });
        self.as_mut().rust_mut().api = Some(api);
        self.as_mut().rust_mut().sse = Some(sse);
        self.as_mut().connect_children();
        // Connect once the event loop runs, like the C++ QTimer::singleShot(0).
        if qt.queue(|controller| controller.reconnect()).is_err() {
            eprintln!("AppController: could not schedule the first connection");
        }
    }
}

impl Drop for AppControllerRust {
    fn drop(&mut self) {
        // Detach child-signal handlers before the children go away.
        self.guards.clear();
        if let Some(sse) = self.sse.as_mut() {
            sse.stop();
        }
    }
}

impl AppController {
    // ---- property getters --------------------------------------------------

    fn agents_value(&self) -> *mut AgentListModel {
        pointer(&self.agents)
    }
    fn archived_agents_value(&self) -> *mut AgentListModel {
        pointer(&self.archived)
    }
    fn contacts_value(&self) -> *mut ContactListModel {
        pointer(&self.contacts)
    }
    fn panes_value(&self) -> *mut PaneTreeModel {
        pointer(&self.panes)
    }
    fn voices_value(&self) -> *mut VoiceListModel {
        pointer(&self.voices)
    }
    fn conversation_value(&self) -> *mut ConversationModel {
        match self.current.as_ref().and_then(|s| self.conversations.get(s)) {
            Some(model) => pointer(model),
            None => pointer(&self.empty_conversation),
        }
    }
    fn base_url_value(&self) -> QString {
        qs(&self.base_url)
    }
    fn selected_session_value(&self) -> QString {
        qs(&self.selected_session)
    }
    fn selected_name_value(&self) -> QString {
        self.roster_agent(&self.selected_session).map_or_else(QString::default, |a| qs(display_name(a)))
    }
    fn selected_state_value(&self) -> QString {
        self.roster_agent(&self.selected_session).map_or_else(QString::default, |a| qs(&a.latest_state))
    }
    fn selected_backend_value(&self) -> QString {
        self.roster_agent(&self.selected_session).map_or_else(QString::default, |a| qs(&a.backend))
    }
    fn connected_value(&self) -> bool {
        self.connected
    }
    fn connecting_value(&self) -> bool {
        self.connecting
    }
    fn sending_value(&self) -> bool {
        self.sending
    }
    fn connection_state_value(&self) -> QString {
        qs(&self.connection_state)
    }
    fn error_message_value(&self) -> QString {
        qs(&self.error_message)
    }
    fn server_name_value(&self) -> QString {
        qs(&self.server_name)
    }
    fn server_version_value(&self) -> QString {
        qs(&self.server_version)
    }
    fn show_when_ready_value(&self) -> bool {
        self.show_when_ready
    }
    fn tools_visible_value(&self) -> bool {
        self.activity_display_mode == 1
    }
    fn activity_display_mode_value(&self) -> i32 {
        self.activity_display_mode
    }
    fn composer_focus_pane_value(&self) -> QString {
        qs(&self.composer_focus_pane)
    }
    fn agent_revision_value(&self) -> u64 {
        self.agent_revision
    }

    fn roster(&self) -> Option<&clarp_core::roster::Roster> {
        self.agents.as_ref().map(|model| model.roster())
    }

    fn roster_agent(&self, session: &str) -> Option<&clarp_core::protocol::Agent> {
        self.roster().and_then(|roster| roster.find(session))
    }

    // ---- child models ------------------------------------------------------

    fn agents_mut(self: Pin<&mut Self>) -> Option<Pin<&mut AgentListModel>> {
        unsafe { self.rust_mut().get_unchecked_mut() }.agents.as_mut()
    }

    fn archived_mut(self: Pin<&mut Self>) -> Option<Pin<&mut AgentListModel>> {
        unsafe { self.rust_mut().get_unchecked_mut() }.archived.as_mut()
    }

    fn panes_mut(self: Pin<&mut Self>) -> Option<Pin<&mut PaneTreeModel>> {
        unsafe { self.rust_mut().get_unchecked_mut() }.panes.as_mut()
    }

    fn conversation_mut(self: Pin<&mut Self>, session: &str) -> Option<Pin<&mut ConversationModel>> {
        unsafe { self.rust_mut().get_unchecked_mut() }.conversations.get_mut(session).and_then(|m| m.as_mut())
    }

    /// Queued (not direct) handlers, so a child signal emitted while the
    /// controller is mid-call never re-enters it.
    fn connect_children(mut self: Pin<&mut Self>) {
        let this = ControllerPtr(unsafe { self.as_mut().get_unchecked_mut() } as *mut AppController);
        let mut guards = Vec::new();
        if let Some(panes) = self.as_mut().panes_mut() {
            guards.push(panes.connect_active_pane_changed(
                move |_| {
                    // SAFETY: the guard lives in the controller.
                    unsafe { Pin::new_unchecked(&mut *this.get()) }.active_pane_changed();
                },
                ConnectionType::QueuedConnection,
            ));
        }
        let this = ControllerPtr(unsafe { self.as_mut().get_unchecked_mut() } as *mut AppController);
        if let Some(agents) = self.as_mut().agents_mut() {
            guards.push(agents.connect_structure_changed(
                move |_| unsafe { Pin::new_unchecked(&mut *this.get()) }.bump_agent_revision(),
                ConnectionType::QueuedConnection,
            ));
        }
        self.as_mut().rust_mut().guards.extend(guards);
    }

    fn bump_agent_revision(mut self: Pin<&mut Self>) {
        self.as_mut().rust_mut().agent_revision += 1;
        self.as_mut().agent_revision_changed();
        self.selected_agent_changed();
    }

    fn active_pane_changed(mut self: Pin<&mut Self>) {
        let (session, pane) = match self.panes.as_ref() {
            Some(panes) => (panes.core().active_session(), panes.core().active_pane_id().to_owned()),
            None => return,
        };
        if !session.is_empty() && session != self.selected_session {
            self.as_mut().select(&session);
        } else if session.is_empty() && !self.selected_session.is_empty() {
            self.as_mut().rust_mut().selected_session.clear();
            self.as_mut().rust_mut().current = None;
            self.as_mut().selected_session_changed();
            self.as_mut().conversation_changed();
            self.as_mut().selected_agent_changed();
        }
        if !pane.is_empty() {
            self.set_composer_focus(&pane);
        }
    }

    fn ensure_conversation(mut self: Pin<&mut Self>, session: &str) {
        if session.is_empty() || self.conversations.contains_key(session) {
            return;
        }
        let mut model = new_conversation_model();
        if let Some(model) = model.as_mut() {
            model.mutate(|core| core.open_session(session));
        }
        let this = ControllerPtr(unsafe { self.as_mut().get_unchecked_mut() } as *mut AppController);
        let owned = session.to_owned();
        let replacement = model.as_mut().map(|m| {
            m.connect_replacement_required(
                move |_| unsafe { Pin::new_unchecked(&mut *this.get()) }.request_tail(&owned, true),
                ConnectionType::QueuedConnection,
            )
        });
        let this = ControllerPtr(unsafe { self.as_mut().get_unchecked_mut() } as *mut AppController);
        let confirmed = model.as_mut().map(|m| {
            m.connect_delivery_confirmed(
                move |_, client_id| {
                    let client_id = client_id.to_string();
                    unsafe { Pin::new_unchecked(&mut *this.get()) }.delivery_confirmed(&client_id);
                },
                ConnectionType::QueuedConnection,
            )
        });
        let mut rust = self.as_mut().rust_mut();
        rust.guards.extend(replacement.into_iter().chain(confirmed));
        rust.conversations.insert(session.to_owned(), model);
    }

    // ---- state setters -----------------------------------------------------

    fn set_connecting(mut self: Pin<&mut Self>, value: bool) {
        if self.connecting != value {
            self.as_mut().rust_mut().connecting = value;
            self.connecting_changed();
        }
    }

    fn set_connection_state(mut self: Pin<&mut Self>, state: &str) {
        if self.connection_state != state {
            self.as_mut().rust_mut().connection_state = state.to_owned();
            self.connection_state_changed();
        }
    }

    fn set_error(mut self: Pin<&mut Self>, message: &str) {
        self.as_mut().rust_mut().error_is_transport = false;
        if self.error_message != message {
            self.as_mut().rust_mut().error_message = message.to_owned();
            self.error_message_changed();
        }
    }

    fn set_sending(mut self: Pin<&mut Self>, value: bool) {
        if self.sending != value {
            self.as_mut().rust_mut().sending = value;
            self.sending_changed();
        }
    }

    fn set_composer_focus(mut self: Pin<&mut Self>, pane: &str) {
        self.as_mut().rust_mut().composer_focus_pane = pane.to_owned();
        self.composer_focus_pane_changed();
    }

    fn set_base_url(mut self: Pin<&mut Self>, value: QString) {
        let normalized = normalized_base_url(&value.to_string());
        if self.base_url == normalized {
            return;
        }
        self.as_mut().reset_transient_state();
        if let Some(sse) = self.as_mut().rust_mut().sse.as_mut() {
            sse.stop();
        }
        self.as_mut().rust_mut().base_url = normalized.clone();
        self.as_mut().rust_mut().server_name.clear();
        self.as_mut().rust_mut().server_version.clear();
        let empty = serde_json::Map::from_iter([("agents".to_owned(), Value::Array(Vec::new()))]);
        if let Some(agents) = self.as_mut().agents_mut() {
            agents.mutate(|core| core.apply_snapshot(&empty));
        }
        if let Some(archived) = self.as_mut().archived_mut() {
            archived.mutate(|core| core.apply_snapshot(&empty));
        }
        if let Some(contacts) = unsafe { self.as_mut().rust_mut().get_unchecked_mut() }.contacts.as_mut() {
            contacts.replace(Vec::new());
        }
        self.as_mut().rust_mut().selected_session.clear();
        self.as_mut().rust_mut().current = None;
        let sessions: Vec<String> = self.conversations.keys().cloned().collect();
        for session in sessions {
            if let Some(model) = self.as_mut().conversation_mut(&session) {
                model.mutate(|core| {
                    core.open_session("");
                    core.open_session(&session);
                });
            }
        }
        self.as_mut().rust_mut().settings.set("connection/baseUrl", normalized);
        self.as_mut().base_url_changed();
        self.as_mut().selected_session_changed();
        self.as_mut().selected_agent_changed();
        self.as_mut().conversation_changed();
        self.server_info_changed();
    }

    fn set_show_when_ready(mut self: Pin<&mut Self>, value: bool) {
        if self.show_when_ready == value {
            return;
        }
        self.as_mut().rust_mut().show_when_ready = value;
        self.as_mut().rust_mut().settings.set("conversation/showWhenReady", value);
        self.show_when_ready_changed();
    }

    fn set_tools_visible(self: Pin<&mut Self>, visible: bool) {
        self.set_activity_display_mode(i32::from(visible));
    }

    fn set_activity_display_mode(mut self: Pin<&mut Self>, mode: i32) {
        let mode = mode.clamp(0, 2);
        if self.activity_display_mode == mode {
            return;
        }
        self.as_mut().rust_mut().activity_display_mode = mode;
        self.as_mut().rust_mut().settings.set("conversation/activityDisplayMode", mode);
        self.as_mut().rust_mut().settings.set("conversation/toolsVisible", mode == 1);
        self.tools_visible_changed();
    }

    fn clear_error(self: Pin<&mut Self>) {
        self.set_error("");
    }

    fn request_composer_focus(self: Pin<&mut Self>, pane_id: &QString) {
        self.set_composer_focus(&pane_id.to_string());
    }

    // ---- connection --------------------------------------------------------

    fn connect_to_server(mut self: Pin<&mut Self>, url: &QString, token: &QString) {
        self.as_mut().set_base_url(url.clone());
        self.as_mut().rust_mut().token = token.to_string().trim().to_owned();
        self.reconnect();
    }

    fn reconnect(mut self: Pin<&mut Self>) {
        let endpoint = match Url::parse(&self.base_url) {
            Ok(url) if url.host_str().is_some_and(|h| !h.is_empty()) => url,
            _ => {
                self.set_error("Enter a valid Clarp server URL");
                return;
            }
        };
        let token = self.token.clone();
        if let Some(sse) = self.as_mut().rust_mut().sse.as_mut() {
            sse.stop();
            sse.set_endpoint(endpoint.clone(), &token);
        }
        self.as_mut().reset_transient_state();
        if let Some(api) = self.api.as_ref() {
            api.set_endpoint(endpoint, &token);
        }
        self.as_mut().set_error("");
        self.as_mut().set_connecting(true);
        self.as_mut().set_connection_state("connecting");
        if let Some(api) = self.api.as_ref() {
            api.get("server-info", "/server-info", &[]);
        }
        // Fetch the sidebar alongside server-info instead of after the event
        // stream connects: that chain cost two extra round trips on start.
        self.request_snapshot();
    }

    fn reset_transient_state(mut self: Pin<&mut Self>) {
        let (in_flight, deliveries) = {
            let mut rust = self.as_mut().rust_mut();
            rust.snapshot_generation += 1;
            rust.snapshot_in_flight = false;
            rust.snapshot_dirty = false;
            rust.pending_log_mode.clear();
            let in_flight: Vec<String> = rust.log_in_flight.drain().collect();
            let deliveries: Vec<(String, String)> =
                rust.deliveries.drain().map(|(client, (session, _))| (client, session)).collect();
            (in_flight, deliveries)
        };
        for session in in_flight {
            if let Some(model) = self.as_mut().conversation_mut(&session) {
                model.mutate(|core| core.set_loading(false));
            }
        }
        for (client, session) in deliveries {
            if let Some(model) = self.as_mut().conversation_mut(&session) {
                model.mutate(|core| core.mark_delivery_failed(&client));
            }
        }
        self.set_sending(false);
    }

    // ---- snapshot ----------------------------------------------------------

    fn refresh_agents(self: Pin<&mut Self>) {
        self.request_snapshot();
    }

    fn request_snapshot(mut self: Pin<&mut Self>) {
        if self.snapshot_in_flight {
            self.as_mut().rust_mut().snapshot_dirty = true;
            return;
        }
        if let Some(elapsed) = self.snapshot_last.map(|t| t.elapsed()).filter(|e| *e < SNAPSHOT_MIN_INTERVAL) {
            if !self.snapshot_scheduled {
                self.as_mut().rust_mut().snapshot_scheduled = true;
                let qt = self.qt_thread();
                crate::runtime::after(SNAPSHOT_MIN_INTERVAL - elapsed, move || {
                    let queued = qt.queue(|mut controller| {
                        controller.as_mut().rust_mut().snapshot_scheduled = false;
                        controller.request_snapshot();
                    });
                    if queued.is_err() {
                        eprintln!("AppController: dropped a scheduled snapshot; the controller is gone");
                    }
                });
            }
            return;
        }
        let generation = {
            let mut rust = self.as_mut().rust_mut();
            rust.snapshot_in_flight = true;
            rust.snapshot_last = Some(Instant::now());
            rust.snapshot_generation += 1;
            rust.snapshot_generation
        };
        if let Some(api) = self.api.as_ref() {
            api.get(&format!("snapshot:{generation}"), "/agents/snapshot", &[]);
        }
    }

    fn complete_snapshot_request(mut self: Pin<&mut Self>) {
        self.as_mut().rust_mut().snapshot_in_flight = false;
        if std::mem::take(&mut self.as_mut().rust_mut().snapshot_dirty) {
            let qt = self.qt_thread();
            crate::runtime::after(Duration::from_millis(100), move || {
                if qt.queue(|controller| controller.request_snapshot()).is_err() {
                    eprintln!("AppController: dropped a follow-up snapshot; the controller is gone");
                }
            });
        }
    }

    fn apply_snapshot(mut self: Pin<&mut Self>, object: &Object) {
        if let Some(agents) = self.as_mut().agents_mut() {
            agents.mutate(|core| core.apply_snapshot(object));
        }
        if let Some(archived) = self.as_mut().archived_mut() {
            archived.mutate(|core| core.apply_snapshot(object));
        }
        let active: HashSet<String> = self
            .roster()
            .map(|roster| roster.agents().iter().map(|a| display_name(a).to_lowercase()).collect())
            .unwrap_or_default();
        let contacts = clarp_core::directory::contacts_from_snapshot(object, &active);
        if let Some(model) = unsafe { self.as_mut().rust_mut().get_unchecked_mut() }.contacts.as_mut() {
            model.replace(contacts);
        }
        self.as_mut().bump_agent_revision();

        let selected_known = self.roster_agent(&self.selected_session).is_some();
        if self.selected_session.is_empty() || !selected_known {
            let first = self.roster().and_then(|r| r.first_session()).map(str::to_owned);
            match first {
                Some(first) if !self.waiting_for_session_choice => self.as_mut().select(&first),
                _ if !self.selected_session.is_empty() => {
                    self.as_mut().rust_mut().selected_session.clear();
                    self.as_mut().rust_mut().current = None;
                    if let Some(panes) = self.as_mut().panes_mut() {
                        panes.mutate(|core| core.set_active_session(""));
                    }
                    self.as_mut().selected_session_changed();
                    self.as_mut().conversation_changed();
                    self.as_mut().selected_agent_changed();
                }
                _ => {}
            }
        }
        // Caches behind the Host's head, or on a dead conversation, catch up.
        let stale: Vec<(String, bool)> = self
            .conversations
            .iter()
            .filter_map(|(session, model)| {
                let agent = self.roster_agent(session)?;
                let conversation = model.as_ref()?.conversation();
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
                self.as_mut().request_tail(&session, false);
            } else {
                self.as_mut().request_delta(&session);
            }
        }
    }

    // ---- selection and log sync -------------------------------------------

    fn select_session(self: Pin<&mut Self>, session: &QString) {
        self.select(&session.to_string());
    }

    fn select(mut self: Pin<&mut Self>, session: &str) {
        if session.is_empty() {
            return;
        }
        self.as_mut().rust_mut().waiting_for_session_choice = false;
        let changed = self.selected_session != session;
        self.as_mut().rust_mut().selected_session = session.to_owned();
        if let Some(agents) = self.as_mut().agents_mut() {
            agents.mutate(|core| core.clear_unread(session));
        }
        if changed {
            self.as_mut().ensure_conversation(session);
            self.as_mut().rust_mut().current = Some(session.to_owned());
            if let Some(panes) = self.as_mut().panes_mut() {
                panes.mutate(|core| core.set_active_session(session));
            }
            self.as_mut().selected_session_changed();
            self.as_mut().conversation_changed();
            self.as_mut().selected_agent_changed();
        }
        // The Host refuses focus on a janitor (inspection-only).
        let janitor = self.roster_agent(session).is_some_and(|a| a.janitor);
        if !janitor
            && let Some(api) = self.api.as_ref() {
                api.post_json(&format!("select:{session}"), "/select", json!({"session": session}), None);
            }
        self.request_tail(session, false);
    }

    fn refresh_conversation(self: Pin<&mut Self>) {
        let session = self.selected_session.clone();
        self.request_tail(&session, false);
    }

    fn refresh_session(self: Pin<&mut Self>, session: &QString) {
        self.request_tail(&session.to_string(), false);
    }

    /// One log request per session at a time; a newer need is queued, with
    /// replace outranking tail outranking delta.
    fn begin_log_request(mut self: Pin<&mut Self>, session: &str, mode: &str) -> bool {
        if !self.log_in_flight.contains(session) {
            self.as_mut().rust_mut().log_in_flight.insert(session.to_owned());
            return true;
        }
        let queued = self.pending_log_mode.get(session).cloned().unwrap_or_default();
        if queued.is_empty() || mode == "replace" || (mode == "tail" && queued != "replace") {
            self.as_mut().rust_mut().pending_log_mode.insert(session.to_owned(), mode.to_owned());
        }
        false
    }

    fn continue_pending_log_request(mut self: Pin<&mut Self>, session: &str) {
        if self.log_in_flight.contains(session) {
            return;
        }
        match self.as_mut().rust_mut().pending_log_mode.remove(session).as_deref() {
            Some("tail") => self.request_tail(session, false),
            Some("replace") => self.request_tail(session, true),
            Some("delta") => self.request_delta(session),
            _ => {}
        }
    }

    fn request_tail(mut self: Pin<&mut Self>, session: &str, replace: bool) {
        let session = if session.is_empty() { self.selected_session.clone() } else { session.to_owned() };
        if session.is_empty() {
            return;
        }
        if !self.as_mut().begin_log_request(&session, if replace { "replace" } else { "tail" }) {
            return;
        }
        self.as_mut().ensure_conversation(&session);
        if let Some(model) = self.as_mut().conversation_mut(&session) {
            model.mutate(|core| core.set_loading(true));
        }
        let tag = format!("{}{session}", if replace { "log-replace:" } else { "log-tail:" });
        if let Some(api) = self.api.as_ref() {
            api.get(&tag, "/log", &[("session", &session), ("limit", "100"), ("include_automated", "0")]);
        }
    }

    fn request_delta(mut self: Pin<&mut Self>, session: &str) {
        let session = if session.is_empty() { self.selected_session.clone() } else { session.to_owned() };
        if session.is_empty() || !self.as_mut().begin_log_request(&session, "delta") {
            return;
        }
        self.as_mut().ensure_conversation(&session);
        let after = self
            .conversations
            .get(&session)
            .and_then(|m| m.as_ref())
            .map_or(0, |m| m.conversation().latest_revision())
            .to_string();
        if let Some(api) = self.api.as_ref() {
            api.get(
                &format!("log-delta:{session}"),
                "/log",
                &[("session", &session), ("limit", "100"), ("include_automated", "0"), ("after_revision", &after)],
            );
        }
    }

    fn load_older(self: Pin<&mut Self>) {
        let session = self.selected_session.clone();
        self.load_older_for(&session);
    }

    fn load_older_session(self: Pin<&mut Self>, session: &QString) {
        self.load_older_for(&session.to_string());
    }

    fn load_older_for(mut self: Pin<&mut Self>, session: &str) {
        if session.is_empty() {
            return;
        }
        self.as_mut().ensure_conversation(session);
        let before = match self.conversations.get(session).and_then(|m| m.as_ref()) {
            Some(model) if model.conversation().has_more() && !model.conversation().is_empty() => {
                model.conversation().rows()[0].id.clone()
            }
            _ => return,
        };
        if self.log_in_flight.contains(session) {
            return;
        }
        self.as_mut().rust_mut().log_in_flight.insert(session.to_owned());
        if let Some(model) = self.as_mut().conversation_mut(session) {
            model.mutate(|core| core.set_loading(true));
        }
        if let Some(api) = self.api.as_ref() {
            api.get(
                &format!("log-older:{session}"),
                "/log",
                &[("session", session), ("limit", "100"), ("include_automated", "0"), ("before", &before)],
            );
        }
    }

    fn apply_log(mut self: Pin<&mut Self>, session: &str, object: &Object, kind: LoadKind) {
        self.as_mut().rust_mut().log_in_flight.remove(session);
        self.as_mut().ensure_conversation(session);
        if let Some(model) = self.as_mut().conversation_mut(session) {
            model.mutate(|core| core.apply_log(object, kind));
        }
        if kind == LoadKind::Delta && json::boolean(object, "has_more") {
            self.as_mut().request_delta(session);
        }
        self.continue_pending_log_request(session);
    }

    // ---- sending -----------------------------------------------------------

    fn send_message(self: Pin<&mut Self>, text: &QString, queue_if_busy: bool) {
        let session = self.selected_session.clone();
        self.send_internal(&session, &text.to_string(), queue_if_busy);
    }

    fn send_message_to(self: Pin<&mut Self>, session: &QString, text: &QString, queue_if_busy: bool) {
        self.send_internal(&session.to_string(), &text.to_string(), queue_if_busy);
    }

    fn send_internal(mut self: Pin<&mut Self>, session: &str, text: &str, queue_if_busy: bool) {
        let trimmed = text.trim();
        if trimmed.is_empty() || session.is_empty() {
            return;
        }
        self.as_mut().ensure_conversation(session);
        if let Some(agents) = self.as_mut().agents_mut() {
            agents.mutate(|core| core.record_outgoing_activity(session));
        }
        let client_id = uuid::Uuid::new_v4().to_string();
        if let Some(model) = self.as_mut().conversation_mut(session) {
            model.mutate(|core| core.add_optimistic(&client_id, trimmed));
        }
        self.as_mut().set_sending(true);
        let body = json!({
            "session": session, "text": trimmed, "client_msg_id": client_id,
            // Audio is ported with the media step; until then never ask for speech.
            "synthesize_audio": false, "hands_free": false, "queue_if_busy": queue_if_busy,
        });
        if let Some(api) = self.api.as_ref() {
            api.post_json(&format!("send:{client_id}"), "/send", body, None);
        }
        let token = {
            let mut rust = self.as_mut().rust_mut();
            rust.delivery_counter += 1;
            let token = rust.delivery_counter;
            rust.deliveries.insert(client_id.clone(), (session.to_owned(), token));
            token
        };
        let qt = self.qt_thread();
        crate::runtime::after(DELIVERY_TIMEOUT, move || {
            let queued = qt.queue(move |controller| controller.delivery_timed_out(&client_id, token));
            if queued.is_err() {
                eprintln!("AppController: dropped a delivery timeout; the controller is gone");
            }
        });
    }

    fn delivery_timed_out(mut self: Pin<&mut Self>, client_id: &str, token: u64) {
        let Some((session, _)) = self.deliveries.get(client_id).filter(|(_, t)| *t == token).cloned() else {
            return;
        };
        self.as_mut().rust_mut().deliveries.remove(client_id);
        if let Some(model) = self.as_mut().conversation_mut(&session) {
            model.mutate(|core| core.mark_delivery_failed(client_id));
        }
        if self.deliveries.is_empty() {
            self.set_sending(false);
        }
    }

    fn delivery_confirmed(mut self: Pin<&mut Self>, client_id: &str) {
        self.as_mut().rust_mut().deliveries.remove(client_id);
        if self.deliveries.is_empty() {
            self.set_sending(false);
        }
    }

    fn retry_failed_message(mut self: Pin<&mut Self>, session: &QString, message_id: &QString) {
        let (session, id) = (session.to_string(), message_id.to_string());
        let text = self.as_mut().conversation_mut(&session).and_then(|m| m.mutate(|core| core.take_failed_message_for_retry(&id)));
        if let Some(text) = text.filter(|t| !t.is_empty()) {
            self.send_internal(&session, &text, false);
        }
    }

    fn retry_latest_failed_message(self: Pin<&mut Self>) {
        if self.selected_session.is_empty() || self.sending {
            return;
        }
        let failed = self
            .conversations
            .get(&self.selected_session)
            .and_then(|m| m.as_ref())
            .and_then(|m| m.conversation().rows().iter().rev().find(|r| r.delivery_failed).map(|r| r.id.clone()));
        if let Some(id) = failed {
            let session = qs(&self.selected_session);
            self.retry_failed_message(&session, &qs(&id));
        }
    }

    fn stop_agent(self: Pin<&mut Self>) {
        let session = qs(&self.selected_session);
        self.stop_session(&session);
    }

    fn stop_session(self: Pin<&mut Self>, session: &QString) {
        let session = session.to_string();
        if session.is_empty() {
            return;
        }
        if let Some(api) = self.api.as_ref() {
            api.post_json(&format!("stop:{session}"), "/stop", json!({"session": session}), None);
        }
    }

    // ---- queries -----------------------------------------------------------

    fn conversation_for_session(mut self: Pin<&mut Self>, session: &QString) -> *mut ConversationModel {
        let session = session.to_string();
        if session.is_empty() {
            return pointer(&self.empty_conversation);
        }
        self.as_mut().ensure_conversation(&session);
        self.conversations.get(&session).map_or(std::ptr::null_mut(), pointer)
    }

    fn agent_name(&self, session: &QString) -> QString {
        let session = session.to_string();
        self.roster_agent(&session).map_or_else(|| qs(&session), |a| qs(display_name(a)))
    }

    fn agent_state(&self, session: &QString) -> QString {
        qs(&self.roster().and_then(|r| r.display_state(&session.to_string())).unwrap_or_default())
    }

    fn agent_backend(&self, session: &QString) -> QString {
        self.roster_agent(&session.to_string()).map_or_else(QString::default, |a| qs(&a.backend))
    }

    fn agent_working_directory(&self, session: &QString) -> QString {
        self.roster_agent(&session.to_string()).map_or_else(QString::default, |a| qs(&a.working_directory))
    }

    fn agent_queue_count(&self, session: &QString) -> i32 {
        self.roster_agent(&session.to_string()).map_or(0, |a| a.queued_turn_count)
    }

    fn next_attention_session(&self) -> QString {
        let next = self.roster().and_then(|r| r.next_attention_session(&self.selected_session, &[]));
        qs(&next.unwrap_or_default())
    }

    // ---- network results ---------------------------------------------------

    fn handle_reply(self: Pin<&mut Self>, reply: ApiReply) {
        match reply {
            ApiReply::Json { tag, object } => self.handle_json(&tag, &object),
            ApiReply::Failed { tag, message, status } => self.handle_failure(&tag, &message, status),
            ApiReply::Bytes { tag, .. } => eprintln!("AppController: unexpected bytes reply {tag}"),
        }
    }

    fn handle_json(mut self: Pin<&mut Self>, tag: &str, object: &Object) {
        if tag == "server-info" {
            let name = object.get("name").and_then(Value::as_str).unwrap_or("Clarp").to_owned();
            self.as_mut().rust_mut().server_name = name;
            self.as_mut().rust_mut().server_version = json::string(object, "clarp_version");
            self.as_mut().server_info_changed();
            self.as_mut().set_connecting(false);
            self.as_mut().request_snapshot();
            if let Some(sse) = self.as_mut().rust_mut().sse.as_mut() {
                sse.start();
            }
        } else if let Some(generation) = tag.strip_prefix("snapshot:") {
            if generation.parse::<u64>().ok() != Some(self.snapshot_generation) {
                return;
            }
            self.as_mut().complete_snapshot_request();
            self.apply_snapshot(object);
        } else if let Some(session) = tag.strip_prefix("log-tail:") {
            self.apply_log(session, object, LoadKind::Tail);
        } else if let Some(session) = tag.strip_prefix("log-replace:") {
            self.apply_log(session, object, LoadKind::Replace);
        } else if let Some(session) = tag.strip_prefix("log-delta:") {
            self.apply_log(session, object, LoadKind::Delta);
        } else if let Some(session) = tag.strip_prefix("log-older:") {
            self.apply_log(session, object, LoadKind::Older);
        } else if let Some(client_id) = tag.strip_prefix("send:") {
            let session = self.deliveries.get(client_id).map(|(s, _)| s.clone()).unwrap_or_else(|| self.selected_session.clone());
            self.request_delta(&session);
        }
        // select:, stop: and other acknowledgements need no action.
    }

    fn handle_failure(mut self: Pin<&mut Self>, tag: &str, message: &str, status: u16) {
        if let Some(generation) = tag.strip_prefix("snapshot:") {
            if generation.parse::<u64>().ok() != Some(self.snapshot_generation) {
                return;
            }
            self.as_mut().complete_snapshot_request();
        }
        let detail = if status > 0 { format!("{message} (HTTP {status})") } else { message.to_owned() };
        self.as_mut().set_error(&detail);
        // No HTTP status: the Host was unreachable. Reconnecting answers it.
        self.as_mut().rust_mut().error_is_transport = status == 0;
        if tag == "server-info" {
            self.as_mut().set_connecting(false);
            self.set_connection_state(if status == 401 { "unauthorized" } else { "offline" });
        } else if tag.starts_with("log-") {
            let session = tag.split_once(':').map(|(_, s)| s.to_owned()).unwrap_or_default();
            self.as_mut().rust_mut().log_in_flight.remove(&session);
            self.as_mut().rust_mut().pending_log_mode.remove(&session);
            if let Some(model) = self.as_mut().conversation_mut(&session) {
                model.mutate(|core| {
                    core.set_loading(false);
                    core.set_error(&detail);
                });
            }
        } else if let Some(client_id) = tag.strip_prefix("send:") {
            let session = self.as_mut().rust_mut().deliveries.remove(client_id).map(|(s, _)| s);
            if let Some(model) = session.and_then(|s| self.as_mut().conversation_mut(&s)) {
                model.mutate(|core| core.mark_delivery_failed(client_id));
            }
            if self.deliveries.is_empty() {
                self.set_sending(false);
            }
        }
    }

    fn handle_sse(mut self: Pin<&mut Self>, signal: SseSignal) {
        match signal {
            SseSignal::Connected(connected) => {
                self.as_mut().rust_mut().connected = connected;
                self.as_mut().connected_changed();
                self.as_mut().set_connection_state(if connected { "live" } else { "reconnecting" });
                if connected {
                    // "Connection refused" from the outage must not outlive it.
                    if self.error_is_transport {
                        self.as_mut().set_error("");
                    }
                    self.request_snapshot();
                } else {
                    if let Some(agents) = self.as_mut().agents_mut() {
                        agents.mutate(|core| core.mark_transport_unavailable());
                    }
                    if let Some(archived) = self.as_mut().archived_mut() {
                        archived.mutate(|core| core.mark_transport_unavailable());
                    }
                    let sessions: Vec<String> = self.conversations.keys().cloned().collect();
                    for session in sessions {
                        if let Some(model) = self.as_mut().conversation_mut(&session) {
                            model.mutate(|core| core.clear_running_activity());
                        }
                    }
                }
            }
            SseSignal::Error(message) => {
                self.as_mut().set_error(&message);
                self.as_mut().rust_mut().error_is_transport = true;
            }
            SseSignal::Event(event) => self.handle_event(&event),
        }
    }

    fn handle_event(mut self: Pin<&mut Self>, event: &Object) {
        let kind = json::string(event, "type");
        let session = json::string(event, "session");
        match kind.as_str() {
            "agent-roster" => self.request_snapshot(),
            "transcript-updated" => {
                if self.conversations.contains_key(&session) {
                    self.request_delta(&session);
                }
            }
            "agent-state" => {
                if let Some(agents) = self.as_mut().agents_mut() {
                    agents.mutate(|core| core.apply_state_event(event));
                }
                let state = json::string(event, "kind");
                let persona = self.roster_agent(&session).map(|a| display_name(a).to_owned()).unwrap_or(session.clone());
                if let Some(model) = self.as_mut().conversation_mut(&session) {
                    if state == "thinking" {
                        model.mutate(|core| core.show_transient_thinking(&persona));
                    } else if matches!(state.as_str(), "tool" | "waiting" | "interrupted" | "done" | "idle" | "stopped") {
                        model.mutate(|core| core.clear_running_activity());
                    }
                }
                if session == self.selected_session {
                    self.selected_agent_changed();
                }
            }
            "agent-activity" => {
                // A "running" step arriving after the turn ended would sit
                // under the finished reply until the next turn clears it.
                let mut status = event
                    .get("activity_status")
                    .and_then(Value::as_str)
                    .map_or_else(|| json::string(event, "status"), str::to_owned);
                if status.is_empty() && is_busy_state(&json::string(event, "state")) {
                    status = "running".into();
                }
                let busy = self.roster_agent(&session).map(|a| a.busy);
                if (status != "running" || busy.is_none_or(|b| b))
                    && let Some(model) = self.as_mut().conversation_mut(&session) {
                        model.mutate(|core| core.apply_activity_event(event));
                    }
            }
            "agent-focus" => {
                if let Some(agents) = self.as_mut().agents_mut() {
                    agents.mutate(|core| core.apply_focus_event(event));
                }
            }
            "queue-updated" => {
                if let Some(agents) = self.as_mut().agents_mut() {
                    agents.mutate(|core| core.apply_queue_event(event));
                }
            }
            "user-notification" => {
                self.as_mut().request_snapshot();
                if let Some(agents) = self.as_mut().agents_mut() {
                    agents.mutate(|core| core.apply_notification_event(event));
                }
                if session == self.selected_session {
                    if let Some(agents) = self.as_mut().agents_mut() {
                        agents.mutate(|core| core.clear_unread(&session));
                    }
                } else {
                    let title = event.get("persona").and_then(Value::as_str).unwrap_or(&session).to_owned();
                    self.notification_requested(qs(&title), qs(&json::string(event, "preview")));
                }
            }
            "server-version" => {
                let version = json::string(event, "version");
                if !version.is_empty() && version != self.server_version {
                    self.as_mut().rust_mut().server_version = version;
                    self.as_mut().server_info_changed();
                    self.request_snapshot();
                }
            }
            "remote-action"
                if json::string(event, "action") == "stop-agent" => {
                    self.stop_agent();
                }
            // Audio, updates, teams and jobs arrive with later slices; unknown
            // types are ignored by contract (additive-only).
            _ => {}
        }
    }
}

/// Keep the thread type nameable for queued closures.
#[allow(dead_code)]
type ControllerThread = CxxQtThread<AppController>;
