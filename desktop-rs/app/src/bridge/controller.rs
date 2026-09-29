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
use cxx_qt_lib::{QString, QUrl};
use serde_json::{Value, json};
use url::Url;

use super::agent_list_model::qobject::{AgentListModel, new_agent_list_model};
use super::conversation_model::qobject::{ConversationModel, new_conversation_model};
use super::directory_models::qobject::{ContactListModel, VoiceListModel, new_contact_list_model, new_voice_list_model};
use super::pane_tree_model::qobject::{PaneTreeModel, new_pane_tree_model};
use super::audio_controller::qobject::{AudioController, new_audio_controller};
use super::avatar_motion::qobject::{AvatarMotionClock, new_avatar_motion_clock};
use super::tool_narrator::qobject::{ToolNarrator, new_tool_narrator};

#[cxx_qt::bridge]
pub mod qobject {
    unsafe extern "C++" {
        include!("cxx-qt-lib/qstring.h");
        type QString = cxx_qt_lib::QString;
        include!("cxx-qt-lib/qjsonarray.h");
        type QJsonArray = cxx_qt_lib::QJsonArray;
        include!("cxx-qt-lib/qjsonobject.h");
        type QJsonObject = cxx_qt_lib::QJsonObject;
        include!("cxx-qt-lib/qstringlist.h");
        type QStringList = cxx_qt_lib::QStringList;
        include!("cxx-qt-lib/qurl.h");
        type QUrl = cxx_qt_lib::QUrl;

        include!("clarp-desktop/src/bridge/agent_list_model.cxxqt.h");
        type AgentListModel = crate::bridge::agent_list_model::qobject::AgentListModel;
        include!("clarp-desktop/src/bridge/conversation_model.cxxqt.h");
        type ConversationModel = crate::bridge::conversation_model::qobject::ConversationModel;
        include!("clarp-desktop/src/bridge/directory_models.cxxqt.h");
        type ContactListModel = crate::bridge::directory_models::qobject::ContactListModel;
        type VoiceListModel = crate::bridge::directory_models::qobject::VoiceListModel;
        include!("clarp-desktop/src/bridge/pane_tree_model.cxxqt.h");
        type PaneTreeModel = crate::bridge::pane_tree_model::qobject::PaneTreeModel;
        include!("clarp-desktop/src/bridge/tool_narrator.cxxqt.h");
        type ToolNarrator = crate::bridge::tool_narrator::qobject::ToolNarrator;
        include!("clarp-desktop/src/bridge/audio_controller.cxxqt.h");
        type AudioController = crate::bridge::audio_controller::qobject::AudioController;
        include!("clarp-desktop/src/bridge/avatar_motion.cxxqt.h");
        type AvatarMotionClock = crate::bridge::avatar_motion::qobject::AvatarMotionClock;
    }

    extern "RustQt" {
        #[qobject]
        #[qml_element]
        #[qproperty(*mut AgentListModel, agents, READ = agents_value, CONSTANT)]
        #[qproperty(*mut AgentListModel, archived_agents, cxx_name = "archivedAgents", READ = archived_agents_value, CONSTANT)]
        #[qproperty(*mut ContactListModel, contacts, READ = contacts_value, CONSTANT)]
        #[qproperty(*mut PaneTreeModel, panes, READ = panes_value, CONSTANT)]
        #[qproperty(*mut VoiceListModel, voices, READ = voices_value, CONSTANT)]
        #[qproperty(*mut AudioController, audio, READ = audio_value, CONSTANT)]
        #[qproperty(*mut AvatarMotionClock, avatar_motion, cxx_name = "avatarMotion", READ = avatar_motion_value, CONSTANT)]
        #[qproperty(*mut ToolNarrator, tool_narrator, cxx_name = "toolNarrator", READ = tool_narrator_value, CONSTANT)]
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
        #[qproperty(u64, process_revision, cxx_name = "processRevision", READ = process_revision_value, NOTIFY = process_revision_changed)]
        #[qproperty(QJsonArray, attention_items, cxx_name = "attentionItems", READ = attention_items_value, NOTIFY = updates_changed)]
        #[qproperty(QJsonArray, background_jobs, cxx_name = "backgroundJobs", READ = background_jobs_value, NOTIFY = updates_changed)]
        #[qproperty(QJsonArray, update_artifacts, cxx_name = "updateArtifacts", READ = update_artifacts_value, NOTIFY = updates_changed)]
        #[qproperty(bool, updates_loading, cxx_name = "updatesLoading", READ = updates_loading_value, NOTIFY = updates_changed)]
        #[qproperty(QString, updates_error, cxx_name = "updatesError", READ = updates_error_value, NOTIFY = updates_changed)]
        #[qproperty(i32, attention_count, cxx_name = "attentionCount", READ = attention_count_value, NOTIFY = updates_changed)]
        #[qproperty(QString, next_attention_target, cxx_name = "nextAttentionTarget", READ = next_attention_session, NOTIFY = updates_changed)]
        #[qproperty(QString, starting_contact, cxx_name = "startingContact", READ = starting_contact_value, NOTIFY = contact_launch_changed)]
        #[qproperty(QString, last_working_directory, cxx_name = "lastWorkingDirectory", READ = last_working_directory_value, NOTIFY = launch_defaults_changed)]
        #[qproperty(QString, last_backend, cxx_name = "lastBackend", READ = last_backend_value, NOTIFY = launch_defaults_changed)]
        #[qproperty(QJsonArray, backend_options, cxx_name = "backendOptions", READ = backend_options_value, NOTIFY = model_catalog_changed)]
        #[qproperty(bool, minimal_ui, cxx_name = "minimalUi", READ = minimal_ui_value, WRITE = set_minimal_ui, NOTIFY = minimal_ui_changed)]
        #[qproperty(bool, workspace_bar_visible, cxx_name = "workspaceBarVisible", READ = workspace_bar_visible_value, WRITE = set_workspace_bar_visible, NOTIFY = workspace_bar_visible_changed)]
        #[qproperty(bool, timestamps_visible, cxx_name = "timestampsVisible", READ = timestamps_visible_value, WRITE = set_timestamps_visible, NOTIFY = timestamps_visible_changed)]
        #[qproperty(bool, muted, READ = muted_value, WRITE = set_muted, NOTIFY = muted_changed)]
        #[qproperty(bool, pause_mobile_push, cxx_name = "pauseMobilePush", READ = pause_mobile_push_value, WRITE = set_pause_mobile_push, NOTIFY = pause_mobile_push_changed)]
        #[qproperty(bool, new_agent_on_startup, cxx_name = "newAgentOnStartup", READ = new_agent_on_startup_value, WRITE = set_new_agent_on_startup, NOTIFY = new_agent_on_startup_changed)]
        #[qproperty(bool, anonymous_agents, cxx_name = "anonymousAgents", READ = anonymous_agents_value, WRITE = set_anonymous_agents, NOTIFY = anonymous_agents_changed)]
        #[qproperty(bool, has_stored_credential, cxx_name = "hasStoredCredential", READ = has_stored_credential_value, NOTIFY = has_stored_credential_changed)]
        #[qproperty(bool, shared_filesystem, cxx_name = "sharedFilesystem", READ = shared_filesystem_value, WRITE = set_shared_filesystem, NOTIFY = shared_filesystem_changed)]
        #[qproperty(QString, reading_theme, cxx_name = "readingTheme", READ = reading_theme_value, WRITE = set_reading_theme, NOTIFY = reading_theme_changed)]
        #[qproperty(QJsonObject, reading_style, cxx_name = "readingStyle", READ = reading_style_value, NOTIFY = reading_theme_changed)]
        #[qproperty(QJsonArray, reading_themes, cxx_name = "readingThemes", READ = reading_themes_value, CONSTANT)]
        #[qproperty(u64, avatar_revision, cxx_name = "avatarRevision", READ = avatar_revision_value, NOTIFY = avatar_revision_changed)]
        #[qproperty(u64, media_revision, cxx_name = "mediaRevision", READ = media_revision_value, NOTIFY = media_changed)]
        #[qproperty(u64, composer_revision, cxx_name = "composerRevision", READ = composer_revision_value, NOTIFY = composer_revision_changed)]
        #[qproperty(bool, uploading, READ = uploading_value, NOTIFY = composer_revision_changed)]
        #[qproperty(i32, unread_agent_conversations, cxx_name = "unreadAgentConversations", READ = zero_count, NOTIFY = agent_revision_changed)]
        #[qproperty(QJsonArray, agent_conversations, cxx_name = "agentConversations", READ = empty_array, NOTIFY = agent_revision_changed)]
        #[qproperty(QJsonObject, profile_task_plan, cxx_name = "profileTaskPlan", READ = profile_task_plan_value, NOTIFY = profile_changed)]
        #[qproperty(QString, profile_session, cxx_name = "profileSession", READ = profile_session_value, NOTIFY = profile_changed)]
        #[qproperty(bool, profile_loading, cxx_name = "profileLoading", READ = profile_loading_value, NOTIFY = profile_changed)]
        #[qproperty(QString, profile_error, cxx_name = "profileError", READ = profile_error_value, NOTIFY = profile_changed)]
        #[qproperty(QJsonArray, past_sessions, cxx_name = "pastSessions", READ = past_sessions_value, NOTIFY = past_sessions_changed)]
        #[qproperty(bool, past_sessions_loading, cxx_name = "pastSessionsLoading", READ = past_sessions_loading_value, NOTIFY = past_sessions_changed)]
        #[qproperty(QJsonArray, launch_directories, cxx_name = "launchDirectories", READ = launch_directories_value, NOTIFY = launch_directories_changed)]
        #[qproperty(bool, launch_directories_loading, cxx_name = "launchDirectoriesLoading", READ = launch_directories_loading_value, NOTIFY = launch_directories_changed)]
        #[qproperty(QJsonArray, directory_suggestions, cxx_name = "directorySuggestions", READ = directory_suggestions_value, NOTIFY = paths_changed)]
        #[qproperty(QJsonArray, favorite_paths, cxx_name = "favoritePaths", READ = favorite_paths_value, NOTIFY = paths_changed)]
        #[qproperty(QJsonArray, assignment_contacts, cxx_name = "assignmentContacts", READ = assignment_contacts_value, NOTIFY = assignment_contacts_changed)]
        #[qproperty(QJsonArray, profile_prompts, cxx_name = "profilePrompts", READ = profile_prompts_value, NOTIFY = profile_changed)]
        #[qproperty(bool, profile_prompts_have_more, cxx_name = "profilePromptsHaveMore", READ = profile_prompts_have_more_value, NOTIFY = profile_changed)]
        #[qproperty(bool, profile_prompts_loading, cxx_name = "profilePromptsLoading", READ = profile_prompts_loading_value, NOTIFY = profile_changed)]
        #[qproperty(QJsonObject, profile_heartbeat, cxx_name = "profileHeartbeat", READ = profile_heartbeat_value, NOTIFY = profile_changed)]
        #[qproperty(QJsonObject, diagnostics_health, cxx_name = "diagnosticsHealth", READ = diagnostics_health_value, NOTIFY = settings_status_changed)]
        #[qproperty(QJsonObject, transcription_capabilities, cxx_name = "transcriptionCapabilities", READ = transcription_capabilities_value, NOTIFY = settings_status_changed)]
        #[qproperty(QJsonObject, tts_provider_status, cxx_name = "ttsProviderStatus", READ = tts_provider_status_value, NOTIFY = settings_status_changed)]
        #[qproperty(bool, settings_status_loading, cxx_name = "settingsStatusLoading", READ = settings_status_loading_value, NOTIFY = settings_status_changed)]
        #[qproperty(QString, voice_bio, cxx_name = "voiceBio", READ = voice_bio_value, NOTIFY = voices_changed)]
        #[qproperty(bool, voices_loading, cxx_name = "voicesLoading", READ = voices_loading_value, NOTIFY = voices_changed)]
        #[qproperty(QJsonObject, orchestrator_settings, cxx_name = "orchestratorSettings", READ = orchestrator_settings_value, NOTIFY = orchestrator_changed)]
        #[qproperty(QString, orchestrator_last_decision, cxx_name = "orchestratorLastDecision", READ = orchestrator_last_decision_value, NOTIFY = orchestrator_changed)]
        #[qproperty(bool, orchestrator_loading, cxx_name = "orchestratorLoading", READ = orchestrator_loading_value, NOTIFY = orchestrator_changed)]
        #[qproperty(QJsonArray, teams, READ = teams_value, NOTIFY = teams_changed)]
        #[qproperty(QJsonArray, team_messages, cxx_name = "teamMessages", READ = team_messages_value, NOTIFY = teams_changed)]
        #[qproperty(QString, selected_team_id, cxx_name = "selectedTeamId", READ = selected_team_id_value, NOTIFY = teams_changed)]
        #[qproperty(bool, teams_loading, cxx_name = "teamsLoading", READ = teams_loading_value, NOTIFY = teams_changed)]
        #[qproperty(QString, teams_error, cxx_name = "teamsError", READ = teams_error_value, NOTIFY = teams_changed)]
        #[qproperty(QJsonArray, turn_queue_items, cxx_name = "turnQueueItems", READ = turn_queue_items_value, NOTIFY = turn_queue_changed)]
        #[qproperty(QString, turn_queue_session, cxx_name = "turnQueueSession", READ = turn_queue_session_value, NOTIFY = turn_queue_changed)]
        #[qproperty(bool, turn_queue_paused, cxx_name = "turnQueuePaused", READ = turn_queue_paused_value, NOTIFY = turn_queue_changed)]
        #[qproperty(bool, turn_queue_loading, cxx_name = "turnQueueLoading", READ = turn_queue_loading_value, NOTIFY = turn_queue_changed)]
        #[qproperty(QString, turn_queue_error, cxx_name = "turnQueueError", READ = turn_queue_error_value, NOTIFY = turn_queue_changed)]
        type AppController = super::AppControllerRust;
    }

    unsafe extern "RustQt" {
        fn agents_value(self: &AppController) -> *mut AgentListModel;
        fn archived_agents_value(self: &AppController) -> *mut AgentListModel;
        fn contacts_value(self: &AppController) -> *mut ContactListModel;
        fn panes_value(self: &AppController) -> *mut PaneTreeModel;
        fn voices_value(self: &AppController) -> *mut VoiceListModel;
        fn tool_narrator_value(self: &AppController) -> *mut ToolNarrator;
        fn avatar_motion_value(self: &AppController) -> *mut AvatarMotionClock;
        fn audio_value(self: &AppController) -> *mut AudioController;
        #[qinvokable]
        #[cxx_name = "toggleRecordingForSession"]
        fn toggle_recording_for_session(self: Pin<&mut AppController>, session: &QString);
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
        fn process_revision_value(self: &AppController) -> u64;
        fn attention_items_value(self: &AppController) -> QJsonArray;
        fn background_jobs_value(self: &AppController) -> QJsonArray;
        fn update_artifacts_value(self: &AppController) -> QJsonArray;
        fn updates_loading_value(self: &AppController) -> bool;
        fn updates_error_value(self: &AppController) -> QString;
        fn attention_count_value(self: &AppController) -> i32;
        fn starting_contact_value(self: &AppController) -> QString;
        fn last_working_directory_value(self: &AppController) -> QString;
        fn last_backend_value(self: &AppController) -> QString;
        fn backend_options_value(self: &AppController) -> QJsonArray;
        fn minimal_ui_value(self: &AppController) -> bool;
        #[cxx_name = "setMinimalUi"]
        fn set_minimal_ui(self: Pin<&mut AppController>, value: bool);
        fn workspace_bar_visible_value(self: &AppController) -> bool;
        #[cxx_name = "setWorkspaceBarVisible"]
        fn set_workspace_bar_visible(self: Pin<&mut AppController>, value: bool);
        fn timestamps_visible_value(self: &AppController) -> bool;
        #[cxx_name = "setTimestampsVisible"]
        fn set_timestamps_visible(self: Pin<&mut AppController>, value: bool);
        fn muted_value(self: &AppController) -> bool;
        #[cxx_name = "setMuted"]
        fn set_muted(self: Pin<&mut AppController>, value: bool);
        fn pause_mobile_push_value(self: &AppController) -> bool;
        #[cxx_name = "setPauseMobilePush"]
        fn set_pause_mobile_push(self: Pin<&mut AppController>, value: bool);
        fn new_agent_on_startup_value(self: &AppController) -> bool;
        #[cxx_name = "setNewAgentOnStartup"]
        fn set_new_agent_on_startup(self: Pin<&mut AppController>, value: bool);
        fn anonymous_agents_value(self: &AppController) -> bool;
        #[cxx_name = "setAnonymousAgents"]
        fn set_anonymous_agents(self: Pin<&mut AppController>, value: bool);
        fn shared_filesystem_value(self: &AppController) -> bool;
        #[cxx_name = "setSharedFilesystem"]
        fn set_shared_filesystem(self: Pin<&mut AppController>, value: bool);
        fn reading_theme_value(self: &AppController) -> QString;
        #[cxx_name = "setReadingTheme"]
        fn set_reading_theme(self: Pin<&mut AppController>, value: QString);
        fn reading_style_value(self: &AppController) -> QJsonObject;
        fn reading_themes_value(self: &AppController) -> QJsonArray;
        fn zero_revision(self: &AppController) -> u64;
        fn avatar_revision_value(self: &AppController) -> u64;
        fn media_revision_value(self: &AppController) -> u64;
        #[qsignal]
        #[cxx_name = "avatarRevisionChanged"]
        fn avatar_revision_changed(self: Pin<&mut AppController>);
        #[qsignal]
        #[cxx_name = "mediaChanged"]
        fn media_changed(self: Pin<&mut AppController>);
        #[qinvokable]
        #[cxx_name = "avatarSource"]
        fn avatar_source(self: Pin<&mut AppController>, session: &QString) -> QUrl;
        #[qinvokable]
        #[cxx_name = "contactAvatarSource"]
        fn contact_avatar_source(self: Pin<&mut AppController>, name: &QString) -> QUrl;
        #[qinvokable]
        #[cxx_name = "mediaForSession"]
        fn media_for_session(self: &AppController, session: &QString) -> QJsonArray;
        #[qinvokable]
        #[cxx_name = "mediaSource"]
        fn media_source(self: &AppController, asset_id: &QString) -> QUrl;
        #[qinvokable]
        #[cxx_name = "resolveMediaMarkdown"]
        fn resolve_media_markdown(self: &AppController, markdown: &QString) -> QString;
        #[qinvokable]
        #[cxx_name = "loadMedia"]
        fn load_media(self: Pin<&mut AppController>, session: &QString);
        fn composer_revision_value(self: &AppController) -> u64;
        fn uploading_value(self: &AppController) -> bool;
        #[qsignal]
        #[cxx_name = "composerRevisionChanged"]
        fn composer_revision_changed(self: Pin<&mut AppController>);
        #[qinvokable]
        #[cxx_name = "composerAttachments"]
        fn composer_attachments(self: &AppController, pane_id: &QString, session: &QString) -> QJsonArray;
        #[qinvokable]
        #[cxx_name = "composerCanSend"]
        fn composer_can_send(self: &AppController, pane_id: &QString, session: &QString) -> bool;
        #[qinvokable]
        #[cxx_name = "attachLocalFile"]
        fn attach_local_file(self: Pin<&mut AppController>, pane_id: &QString, session: &QString, file_url: &QUrl);
        #[qinvokable]
        #[cxx_name = "removeComposerAttachment"]
        fn remove_composer_attachment(self: Pin<&mut AppController>, pane_id: &QString, session: &QString, attachment_id: &QString);
        #[qinvokable]
        #[cxx_name = "sendComposerMessage"]
        fn send_composer_message(self: Pin<&mut AppController>, pane_id: &QString, session: &QString, text: &QString, queue_if_busy: bool) -> bool;
        /// Image paste needs clipboard bindings that are not ported yet:
        /// returning false lets the native text paste proceed.
        #[qinvokable]
        #[cxx_name = "pasteClipboardImage"]
        fn paste_clipboard_image(self: Pin<&mut AppController>, pane_id: &QString, session: &QString) -> bool;
        fn false_value(self: &AppController) -> bool;
        fn zero_count(self: &AppController) -> i32;
        fn empty_array(self: &AppController) -> QJsonArray;
        fn teams_value(self: &AppController) -> QJsonArray;
        fn profile_task_plan_value(self: &AppController) -> QJsonObject;
        fn profile_session_value(self: &AppController) -> QString;
        fn profile_loading_value(self: &AppController) -> bool;
        fn profile_error_value(self: &AppController) -> QString;
        fn profile_prompts_value(self: &AppController) -> QJsonArray;
        fn past_sessions_value(self: &AppController) -> QJsonArray;
        fn past_sessions_loading_value(self: &AppController) -> bool;
        fn launch_directories_value(self: &AppController) -> QJsonArray;
        fn launch_directories_loading_value(self: &AppController) -> bool;
        fn directory_suggestions_value(self: &AppController) -> QJsonArray;
        fn favorite_paths_value(self: &AppController) -> QJsonArray;
        fn assignment_contacts_value(self: &AppController) -> QJsonArray;
        #[qsignal]
        #[cxx_name = "pastSessionsChanged"]
        fn past_sessions_changed(self: Pin<&mut AppController>);
        #[qsignal]
        #[cxx_name = "launchDirectoriesChanged"]
        fn launch_directories_changed(self: Pin<&mut AppController>);
        #[qsignal]
        #[cxx_name = "pathsChanged"]
        fn paths_changed(self: Pin<&mut AppController>);
        #[qsignal]
        #[cxx_name = "assignmentContactsChanged"]
        fn assignment_contacts_changed(self: Pin<&mut AppController>);
        #[qsignal]
        #[cxx_name = "contactAssignmentRequested"]
        fn contact_assignment_requested(self: Pin<&mut AppController>, session: &QString, automatic: bool);
        #[qsignal]
        #[cxx_name = "contactAssignmentSucceeded"]
        fn contact_assignment_succeeded(self: Pin<&mut AppController>, session: &QString);
        #[qinvokable]
        #[cxx_name = "resumeLaunchSession"]
        fn resume_launch_session(self: Pin<&mut AppController>, backend: &QString, session_id: &QString, anonymous: bool) -> bool;
        #[qinvokable]
        #[cxx_name = "loadPastSessions"]
        fn load_past_sessions(self: Pin<&mut AppController>, working_directory: &QString, backend: &QString, all_projects: bool);
        #[qinvokable]
        #[cxx_name = "loadLaunchDirectories"]
        fn load_launch_directories(self: Pin<&mut AppController>, query: &QString);
        #[qinvokable]
        #[cxx_name = "loadDirectorySuggestions"]
        fn load_directory_suggestions(self: Pin<&mut AppController>, path: &QString);
        #[qinvokable]
        #[cxx_name = "loadFavoritePaths"]
        fn load_favorite_paths(self: Pin<&mut AppController>);
        #[qinvokable]
        #[cxx_name = "loadAssignmentContacts"]
        fn load_assignment_contacts(self: Pin<&mut AppController>, session: &QString);
        #[qinvokable]
        #[cxx_name = "requestContactAssignment"]
        fn request_contact_assignment(self: Pin<&mut AppController>, session: &QString, automatic: bool);
        #[qinvokable]
        #[cxx_name = "assignContact"]
        fn assign_contact(self: Pin<&mut AppController>, session: &QString, mode: &QString, name: &QString);
        fn profile_prompts_have_more_value(self: &AppController) -> bool;
        fn profile_prompts_loading_value(self: &AppController) -> bool;
        fn profile_heartbeat_value(self: &AppController) -> QJsonObject;
        fn diagnostics_health_value(self: &AppController) -> QJsonObject;
        fn transcription_capabilities_value(self: &AppController) -> QJsonObject;
        fn tts_provider_status_value(self: &AppController) -> QJsonObject;
        fn settings_status_loading_value(self: &AppController) -> bool;
        fn voice_bio_value(self: &AppController) -> QString;
        fn voices_loading_value(self: &AppController) -> bool;
        fn orchestrator_settings_value(self: &AppController) -> QJsonObject;
        fn orchestrator_last_decision_value(self: &AppController) -> QString;
        fn orchestrator_loading_value(self: &AppController) -> bool;
        #[qsignal]
        #[cxx_name = "profileChanged"]
        fn profile_changed(self: Pin<&mut AppController>);
        #[qsignal]
        #[cxx_name = "settingsStatusChanged"]
        fn settings_status_changed(self: Pin<&mut AppController>);
        #[qsignal]
        #[cxx_name = "voicesChanged"]
        fn voices_changed(self: Pin<&mut AppController>);
        #[qsignal]
        #[cxx_name = "orchestratorChanged"]
        fn orchestrator_changed(self: Pin<&mut AppController>);
        #[qinvokable]
        #[cxx_name = "loadAgentProfile"]
        fn load_agent_profile(self: Pin<&mut AppController>, session: &QString);
        #[qinvokable]
        #[cxx_name = "loadPromptHistory"]
        fn load_prompt_history(self: Pin<&mut AppController>, session: &QString, load_more: bool);
        #[qinvokable]
        #[cxx_name = "loadSettingsStatus"]
        fn load_settings_status(self: Pin<&mut AppController>);
        #[qinvokable]
        #[cxx_name = "setTtsProviders"]
        fn set_tts_providers(self: Pin<&mut AppController>, provider: &QString, fallback: &QString, voice: &QString);
        #[qinvokable]
        #[cxx_name = "loadVoices"]
        fn load_voices(self: Pin<&mut AppController>, session: &QString);
        #[qinvokable]
        #[cxx_name = "chooseVoice"]
        fn choose_voice(self: Pin<&mut AppController>, session: &QString, voice_id: &QString);
        #[qinvokable]
        #[cxx_name = "loadOrchestrator"]
        fn load_orchestrator(self: Pin<&mut AppController>);
        #[qinvokable]
        #[cxx_name = "saveOrchestrator"]
        fn save_orchestrator(self: Pin<&mut AppController>, enabled: bool, fallback_only: bool, confidence: f64, provider: &QString, model: &QString, effort: &QString, timeout_ms: i32);
        fn team_messages_value(self: &AppController) -> QJsonArray;
        fn selected_team_id_value(self: &AppController) -> QString;
        fn teams_loading_value(self: &AppController) -> bool;
        fn teams_error_value(self: &AppController) -> QString;
        fn turn_queue_items_value(self: &AppController) -> QJsonArray;
        fn turn_queue_session_value(self: &AppController) -> QString;
        fn turn_queue_paused_value(self: &AppController) -> bool;
        fn turn_queue_loading_value(self: &AppController) -> bool;
        fn turn_queue_error_value(self: &AppController) -> QString;
        #[qsignal]
        #[cxx_name = "teamsChanged"]
        fn teams_changed(self: Pin<&mut AppController>);
        #[qsignal]
        #[cxx_name = "turnQueueChanged"]
        fn turn_queue_changed(self: Pin<&mut AppController>);
        #[qinvokable]
        #[cxx_name = "loadTeams"]
        fn load_teams(self: Pin<&mut AppController>);
        #[qinvokable]
        #[cxx_name = "selectTeam"]
        fn select_team(self: Pin<&mut AppController>, team_id: &QString);
        #[qinvokable]
        #[cxx_name = "createTeam"]
        fn create_team(self: Pin<&mut AppController>, name: &QString, color: &QString);
        #[qinvokable]
        #[cxx_name = "updateTeam"]
        fn update_team(self: Pin<&mut AppController>, team_id: &QString, name: &QString, color: &QString, leader_agent_id: &QString);
        #[qinvokable]
        #[cxx_name = "addTeamMember"]
        fn add_team_member(self: Pin<&mut AppController>, team_id: &QString, agent_id: &QString);
        #[qinvokable]
        #[cxx_name = "removeTeamMember"]
        fn remove_team_member(self: Pin<&mut AppController>, team_id: &QString, agent_id: &QString);
        #[qinvokable]
        #[cxx_name = "setTeamNudging"]
        fn set_team_nudging(self: Pin<&mut AppController>, team_id: &QString, enabled: bool);
        #[qinvokable]
        #[cxx_name = "deleteTeam"]
        fn delete_team(self: Pin<&mut AppController>, team_id: &QString);
        #[qinvokable]
        #[cxx_name = "teamNameById"]
        fn team_name_by_id(self: &AppController, team_id: &QString) -> QString;
        #[qinvokable]
        #[cxx_name = "teamAgentChoices"]
        fn team_agent_choices(self: &AppController) -> QJsonArray;
        #[qinvokable]
        #[cxx_name = "loadTurnQueue"]
        fn load_turn_queue(self: Pin<&mut AppController>, session: &QString);
        #[qinvokable]
        #[cxx_name = "updateQueuedTurn"]
        fn update_queued_turn(self: Pin<&mut AppController>, queue_id: &QString, text: &QString);
        #[qinvokable]
        #[cxx_name = "deleteQueuedTurn"]
        fn delete_queued_turn(self: Pin<&mut AppController>, queue_id: &QString);
        #[qinvokable]
        #[cxx_name = "sendQueuedTurn"]
        fn send_queued_turn(self: Pin<&mut AppController>, queue_id: &QString);
        #[qsignal]
        #[cxx_name = "minimalUiChanged"]
        fn minimal_ui_changed(self: Pin<&mut AppController>);
        #[qsignal]
        #[cxx_name = "workspaceBarVisibleChanged"]
        fn workspace_bar_visible_changed(self: Pin<&mut AppController>);
        #[qsignal]
        #[cxx_name = "timestampsVisibleChanged"]
        fn timestamps_visible_changed(self: Pin<&mut AppController>);
        #[qsignal]
        #[cxx_name = "mutedChanged"]
        fn muted_changed(self: Pin<&mut AppController>);
        #[qsignal]
        #[cxx_name = "pauseMobilePushChanged"]
        fn pause_mobile_push_changed(self: Pin<&mut AppController>);
        #[qsignal]
        #[cxx_name = "newAgentOnStartupChanged"]
        fn new_agent_on_startup_changed(self: Pin<&mut AppController>);
        #[qsignal]
        #[cxx_name = "anonymousAgentsChanged"]
        fn anonymous_agents_changed(self: Pin<&mut AppController>);
        #[qsignal]
        #[cxx_name = "sharedFilesystemChanged"]
        fn shared_filesystem_changed(self: Pin<&mut AppController>);
        #[qsignal]
        #[cxx_name = "readingThemeChanged"]
        fn reading_theme_changed(self: Pin<&mut AppController>);

        #[qinvokable]
        #[cxx_name = "agentDetails"]
        fn agent_details(self: Pin<&mut AppController>, session: &QString) -> QJsonObject;
        #[qinvokable]
        #[cxx_name = "agentModel"]
        fn agent_model(self: &AppController, session: &QString) -> QString;
        #[qinvokable]
        #[cxx_name = "agentEffort"]
        fn agent_effort(self: &AppController, session: &QString) -> QString;
        #[qinvokable]
        #[cxx_name = "agentQuotaNotice"]
        fn agent_quota_notice(self: &AppController, session: &QString) -> QString;
        #[qinvokable]
        #[cxx_name = "agentNameById"]
        fn agent_name_by_id(self: &AppController, agent_id: &QString) -> QString;
        #[qinvokable]
        #[cxx_name = "agentSessionById"]
        fn agent_session_by_id(self: &AppController, agent_id: &QString) -> QString;
        #[qinvokable]
        #[cxx_name = "matchingAgents"]
        fn matching_agents(self: &AppController, query: &QString) -> QJsonArray;
        #[qinvokable]
        #[cxx_name = "isPairSession"]
        fn is_pair_session(self: &AppController, session: &QString) -> bool;
        #[qinvokable]
        #[cxx_name = "chatStamp"]
        fn chat_stamp(self: &AppController, time: i64) -> QString;
        #[qinvokable]
        #[cxx_name = "markStartup"]
        fn mark_startup(self: &AppController, milestone: &QString);
        #[qinvokable]
        #[cxx_name = "markdownDisplayBlocks"]
        fn markdown_display_blocks(self: &AppController, markdown: &QString) -> QStringList;
        #[qinvokable]
        #[cxx_name = "linkifiedOutput"]
        fn linkified_output(self: &AppController, text: &QString) -> QString;
        #[qinvokable]
        #[cxx_name = "canLinkifyOutput"]
        fn can_linkify_output(self: &AppController, text: &QString) -> bool;
        #[qinvokable]
        #[cxx_name = "backgroundJobProgress"]
        fn background_job_progress(self: &AppController, job: &QJsonObject) -> f64;
        #[qsignal]
        #[cxx_name = "contactLaunchChanged"]
        fn contact_launch_changed(self: Pin<&mut AppController>);
        #[qsignal]
        #[cxx_name = "launchDefaultsChanged"]
        fn launch_defaults_changed(self: Pin<&mut AppController>);
        #[qsignal]
        #[cxx_name = "modelCatalogChanged"]
        fn model_catalog_changed(self: Pin<&mut AppController>);
        #[qsignal]
        #[cxx_name = "agentMutationSucceeded"]
        fn agent_mutation_succeeded(self: Pin<&mut AppController>, session: QString);
        #[qsignal]
        #[cxx_name = "launchPoolEmpty"]
        fn launch_pool_empty(self: Pin<&mut AppController>);

        #[qinvokable]
        #[cxx_name = "startAnonymousAgent"]
        fn start_anonymous_agent(self: Pin<&mut AppController>, backend: &QString, model: &QString, effort: &QString) -> bool;
        #[qinvokable]
        #[cxx_name = "startAvailableContact"]
        fn start_available_contact(self: Pin<&mut AppController>, backend: &QString, model: &QString, effort: &QString) -> bool;
        #[qinvokable]
        #[cxx_name = "quickStartContact"]
        fn quick_start_contact(self: Pin<&mut AppController>, name: &QString, backend: &QString, model: &QString, effort: &QString) -> bool;
        #[qinvokable]
        #[cxx_name = "matchingContacts"]
        fn matching_contacts(self: &AppController, query: &QString) -> QJsonArray;
        #[qinvokable]
        #[cxx_name = "quickStartBackend"]
        fn quick_start_backend(self: &AppController) -> QString;
        #[qinvokable]
        #[cxx_name = "setLaunchDirectory"]
        fn set_launch_directory(self: Pin<&mut AppController>, path: &QString);
        #[qinvokable]
        #[cxx_name = "launchDirectory"]
        fn launch_directory(self: &AppController) -> QString;
        #[qinvokable]
        #[cxx_name = "createAgent"]
        fn create_agent(self: Pin<&mut AppController>, name: &QString, working_directory: &QString, backend: &QString, model: &QString, effort: &QString, replace_session: &QString, mode: &QString, past_session_id: &QString, mcp_servers: &QJsonArray);
        #[qinvokable]
        #[cxx_name = "releaseAgent"]
        fn release_agent(self: Pin<&mut AppController>, session: &QString);
        #[qinvokable]
        #[cxx_name = "setAgentHeartbeat"]
        fn set_agent_heartbeat(self: Pin<&mut AppController>, session: &QString, enabled: bool);
        #[qinvokable]
        #[cxx_name = "setAgentDreaming"]
        fn set_agent_dreaming(self: Pin<&mut AppController>, session: &QString, enabled: bool);
        #[qinvokable]
        #[cxx_name = "setAgentPushMuted"]
        fn set_agent_push_muted(self: Pin<&mut AppController>, session: &QString, muted: bool);
        #[qinvokable]
        #[cxx_name = "renameAgent"]
        fn rename_agent(self: Pin<&mut AppController>, session: &QString, name: &QString);
        #[qinvokable]
        #[cxx_name = "archiveAgent"]
        fn archive_agent(self: Pin<&mut AppController>, session: &QString);
        #[qinvokable]
        #[cxx_name = "setAgentArchived"]
        fn set_agent_archived(self: Pin<&mut AppController>, session: &QString, archived: bool);
        #[qinvokable]
        #[cxx_name = "setScheduleEnabled"]
        fn set_schedule_enabled(self: Pin<&mut AppController>, schedule_id: &QString, enabled: bool);
        #[qinvokable]
        #[cxx_name = "setAgentLlm"]
        fn set_agent_llm(self: Pin<&mut AppController>, session: &QString, model: &QString, effort: &QString);
        #[qinvokable]
        #[cxx_name = "compactSession"]
        fn compact_session(self: Pin<&mut AppController>, session: &QString);
        #[qinvokable]
        #[cxx_name = "setAgentMcp"]
        fn set_agent_mcp(self: Pin<&mut AppController>, session: &QString, servers: &QJsonArray);
        #[qinvokable]
        #[cxx_name = "modelsForBackend"]
        fn models_for_backend(self: &AppController, backend: &QString) -> QJsonArray;
        #[qinvokable]
        #[cxx_name = "effortsForModel"]
        fn efforts_for_model(self: &AppController, backend: &QString, model: &QString) -> QJsonArray;
        #[qinvokable]
        #[cxx_name = "defaultEffortForModel"]
        fn default_effort_for_model(self: &AppController, backend: &QString, model: &QString) -> QString;
        #[qinvokable]
        #[cxx_name = "backendSupportsResume"]
        fn backend_supports_resume(self: &AppController, backend: &QString) -> bool;
        #[qinvokable]
        #[cxx_name = "backendSupportsFork"]
        fn backend_supports_fork(self: &AppController, backend: &QString) -> bool;

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
        #[cxx_name = "processRevisionChanged"]
        fn process_revision_changed(self: Pin<&mut AppController>);
        #[qsignal]
        #[cxx_name = "updatesChanged"]
        fn updates_changed(self: Pin<&mut AppController>);

        #[qinvokable]
        #[cxx_name = "loadUpdates"]
        fn load_updates(self: Pin<&mut AppController>);
        #[qinvokable]
        #[cxx_name = "agentProcesses"]
        fn agent_processes(self: &AppController, session: &QString) -> QJsonObject;
        #[qinvokable]
        #[cxx_name = "resolveDecision"]
        fn resolve_decision(self: Pin<&mut AppController>, decision_id: &QString, choice: &QString, revision: i32);
        #[qinvokable]
        #[cxx_name = "cancelBackgroundJob"]
        fn cancel_background_job(self: Pin<&mut AppController>, job_id: &QString);
        #[qinvokable]
        #[cxx_name = "updateActionPending"]
        fn update_action_pending(self: &AppController, kind: &QString, id: &QString) -> bool;
        /// Takes the job as JSON text (QML: `JSON.stringify(job)`).
        #[qinvokable]
        #[cxx_name = "backgroundJobProgressText"]
        fn background_job_progress_text(self: &AppController, job: &QString) -> f64;
        #[qsignal]
        #[cxx_name = "notificationRequested"]
        fn notification_requested(self: Pin<&mut AppController>, title: QString, body: QString);
        #[qsignal]
        #[cxx_name = "draftChanged"]
        fn draft_changed(self: Pin<&mut AppController>, session: QString, text: QString, origin_pane_id: QString);

        #[qinvokable]
        #[cxx_name = "paneDraft"]
        fn pane_draft(self: &AppController, pane_id: &QString, session: &QString) -> QString;
        #[qinvokable]
        #[cxx_name = "setPaneDraft"]
        fn set_pane_draft(self: Pin<&mut AppController>, pane_id: &QString, session: &QString, text: &QString);
        #[qinvokable]
        #[cxx_name = "flushPendingDrafts"]
        fn flush_pending_drafts(self: Pin<&mut AppController>);

        #[qinvokable]
        #[cxx_name = "connectToServer"]
        fn connect_to_server(self: Pin<&mut AppController>, url: &QString, token: &QString);
        #[qinvokable]
        #[cxx_name = "pairDevice"]
        fn pair_device(self: Pin<&mut AppController>, url: &QString, code: &QString);
        #[qinvokable]
        #[cxx_name = "forgetCredential"]
        fn forget_credential(self: Pin<&mut AppController>);
        fn has_stored_credential_value(self: &AppController) -> bool;
        #[qsignal]
        #[cxx_name = "hasStoredCredentialChanged"]
        fn has_stored_credential_changed(self: Pin<&mut AppController>);
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
    narrator: Owned<ToolNarrator>,
    avatar_motion: Owned<AvatarMotionClock>,
    audio: Owned<AudioController>,
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
    process_revision: u64,
    jobs: clarp_core::jobs::JobTracker,
    attention_items: Vec<Value>,
    background_jobs: Vec<Value>,
    update_artifacts: Vec<Value>,
    updates_generation: u64,
    updates_pending: i32,
    updates_error: String,
    pending_update_actions: HashSet<String>,
    model_catalog: Object,
    starting_contact: String,
    starting_backend: String,
    last_working_directory: String,
    last_backend: String,
    launch_directory: String,
    pending_created_session: String,
    minimal_ui: bool,
    workspace_bar_visible: bool,
    timestamps_visible: bool,
    muted: bool,
    pause_mobile_push: bool,
    new_agent_on_startup: bool,
    anonymous_agents: bool,
    shared_filesystem: bool,
    has_stored_credential: bool,
    shared_filesystem_override: String,
    reading_theme: String,
    workspace: clarp_core::workspace::WorkspaceContext,
    teams: Vec<Value>,
    team_messages: Vec<Value>,
    selected_team_id: String,
    team_list_loading: bool,
    team_messages_loading: bool,
    teams_error: String,
    team_list_generation: u64,
    team_messages_generation: u64,
    turn_queue_items: Vec<Value>,
    turn_queue_session: String,
    turn_queue_paused: bool,
    turn_queue_loading: bool,
    turn_queue_error: String,
    turn_queue_generation: u64,
    queue_action_sessions: HashMap<String, String>,
    composer_revision: u64,
    avatars: AvatarCache,
    contact_avatars: AvatarCache,
    next_avatar_request: u64,
    avatar_revision: u64,
    media_revision: u64,
    media_generations: HashMap<String, u64>,
    /// tag -> (session, generation)
    media_list_requests: HashMap<String, (String, u64)>,
    media_assets: HashMap<String, Vec<Value>>,
    /// asset id -> cached file URL
    media_sources: HashMap<String, String>,
    /// tag -> (asset id, session)
    media_content_requests: HashMap<String, (String, String)>,
    media_directory: Option<std::path::PathBuf>,
    /// tag → pending upload (session, attachment id and metadata)
    pending_uploads: HashMap<String, Object>,
    profile_session: String,
    profile_task_plan: Object,
    profile_heartbeat: Object,
    profile_prompts: Vec<Value>,
    past_sessions: Vec<Value>,
    past_sessions_loading: bool,
    past_sessions_generation: u64,
    launch_directories: Vec<Value>,
    launch_directories_loading: bool,
    launch_directory_generation: u64,
    directory_suggestions: Vec<Value>,
    favorite_paths: Vec<Value>,
    assignment_session: String,
    assignment_contacts: Vec<Value>,
    profile_prompt_cursor: String,
    profile_prompts_have_more: bool,
    profile_prompts_loading: bool,
    profile_loading: bool,
    profile_error: String,
    profile_generation: u64,
    prompt_history_generation: u64,
    /// tag → (session, generation, load_more)
    prompt_history_requests: HashMap<String, (String, u64, bool)>,
    diagnostics_health: Object,
    transcription_capabilities: Object,
    tts_provider_status: Object,
    settings_status_pending: i32,
    settings_status_generation: u64,
    voice_bio: String,
    voices_loading: bool,
    orchestrator_settings: Object,
    orchestrator_last_decision: String,
    orchestrator_loading: bool,
    created_snapshot_attempts: u32,
    /// Draft text by settings key, held in memory until the composer idles.
    pending_drafts: HashMap<String, String>,
    draft_flush_token: u64,
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
            rust.narrator = Owned(new_tool_narrator());
            rust.avatar_motion = Owned(new_avatar_motion_clock());
            rust.audio = Owned(new_audio_controller());
            rust.settings = Settings::user();
            let saved = rust.settings.string("connection/baseUrl", "http://127.0.0.1:7682");
            rust.base_url = normalized_base_url(&std::env::var("CLARP_BASE_URL").unwrap_or(saved));
            rust.token = std::env::var("CLARP_TOKEN").unwrap_or_else(|_| default_token());
            rust.show_when_ready = rust.settings.boolean("conversation/showWhenReady", false);
            let tools_visible = rust.settings.boolean("conversation/toolsVisible", false);
            rust.activity_display_mode =
                rust.settings.integer("conversation/activityDisplayMode", i64::from(tools_visible)).clamp(0, 2) as i32;
            rust.waiting_for_session_choice = std::env::var_os("CLARP_EMPTY_STARTUP").is_some();
            rust.last_working_directory = rust.settings.string("launch/workingDirectory", "~");
            rust.last_backend = rust.settings.string("launch/backend", "");
            rust.minimal_ui = rust.settings.boolean("appearance/minimalUi", false);
            rust.workspace_bar_visible = rust.settings.boolean("appearance/workspaceBar", true);
            rust.timestamps_visible = rust.settings.boolean("conversation/timestampsVisible", false);
            rust.muted = rust.settings.boolean("audio/muted", false);
            rust.pause_mobile_push = rust.settings.boolean("notifications/pauseMobileWhileDesktopActive", true);
            rust.new_agent_on_startup = rust.settings.boolean("launch/newAgentOnStartup", true);
            rust.anonymous_agents = rust.settings.boolean("launch/anonymousAgents", true);
            rust.reading_theme = clarp_core::reading_theme::normalized_theme_id(
                &rust.settings.string("appearance/readingTheme", clarp_core::reading_theme::default_theme_id()));
            let shared_host = std::env::var("CLARP_SHARED_FILESYSTEM_HOST").unwrap_or_default();
            rust.shared_filesystem_override =
                if shared_host.trim().is_empty() { String::new() } else { normalized_base_url(&shared_host) };
            rust.shared_filesystem = rust.compute_shared_filesystem();
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
        let narrator_api = api.clone();
        self.as_mut().rust_mut().api = Some(api);
        self.as_mut().rust_mut().sse = Some(sse);
        let level = self.settings.integer("experiments/toolDetailLevel", 0) as i32;
        if let Some(narrator) = self.as_mut().narrator_mut() {
            let mut narrator = narrator;
            narrator.as_mut().set_api(narrator_api);
            narrator.set_detail_level(level);
        }
        self.as_mut().connect_children();
        // Screenshot runs open a requested chat once the fleet has loaded.
        if std::env::var_os("CLARP_SCREENSHOT_PATH").is_some()
            && let Ok(session) = std::env::var("CLARP_SCREENSHOT_SELECT_SESSION")
            && !session.is_empty()
        {
            let qt = self.qt_thread();
            crate::runtime::after(Duration::from_millis(1_400), move || {
                let queued = qt.queue(move |controller| controller.select_session(&qs(&session)));
                if queued.is_err() {
                    eprintln!("AppController: dropped the screenshot selection; the controller is gone");
                }
            });
        }
        // Connect once the event loop runs, like the C++ QTimer::singleShot(0).
        if qt.queue(|controller| controller.connect_or_look_up_token()).is_err() {
            eprintln!("AppController: could not schedule the first connection");
        }
    }
}

/// Portrait sources for sessions (or contact names), like the C++ maps:
/// the URL each one wants, the image shown, the URL that last failed, and
/// the requests in flight (tag -> (key, url)).
#[derive(Default)]
struct AvatarCache {
    urls: HashMap<String, String>,
    sources: HashMap<String, String>,
    failures: HashMap<String, String>,
    requests: HashMap<String, (String, String)>,
}

impl AppControllerRust {
    /// Screenshot runs never read or write the portrait cache.
    fn portrait_cache(&self) -> Option<std::path::PathBuf> {
        if std::env::var_os("CLARP_SCREENSHOT_SCENARIO").is_some() {
            return None;
        }
        clarp_core::media::cache_dir()
    }

    fn compute_shared_filesystem(&self) -> bool {
        (!self.shared_filesystem_override.is_empty() && self.shared_filesystem_override == self.base_url)
            || self.settings.boolean(&clarp_core::settings::shared_filesystem_key(&self.base_url), false)
    }

    fn flush_drafts(&mut self) {
        for (key, text) in std::mem::take(&mut self.pending_drafts) {
            if text.is_empty() {
                self.settings.remove(&key);
            } else {
                self.settings.set(&key, text);
            }
        }
    }
}

impl Drop for AppControllerRust {
    fn drop(&mut self) {
        self.flush_drafts();
        if let Some(directory) = self.media_directory.take()
            && let Err(error) = std::fs::remove_dir_all(&directory)
        {
            eprintln!("AppController: could not remove {}: {error}", directory.display());
        }
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
    fn tool_narrator_value(&self) -> *mut ToolNarrator {
        pointer(&self.narrator)
    }
    fn avatar_motion_value(&self) -> *mut AvatarMotionClock {
        pointer(&self.avatar_motion)
    }
    fn audio_value(&self) -> *mut AudioController {
        pointer(&self.audio)
    }

    fn toggle_recording_for_session(mut self: Pin<&mut Self>, session: &QString) {
        let recording = self.audio.as_ref().is_some_and(|a| a.recording_pub());
        if session.is_empty() && !recording {
            return;
        }
        if let Some(audio) = self.as_mut().audio_mut() {
            audio.toggle_recording_for_session(session);
        }
    }

    /// The selected chat's clips that were announced while this window was
    /// not listening, played now.
    fn request_recoverable_clips(self: Pin<&mut Self>, session: &str) {
        if session.is_empty() {
            return;
        }
        if let Some(api) = self.api.as_ref() {
            api.get(&format!("recoverable:{session}"), "/clips/recoverable", &[("session", session)]);
        }
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

    fn process_revision_value(&self) -> u64 {
        self.process_revision
    }
    fn attention_items_value(&self) -> cxx_qt_lib::QJsonArray {
        crate::qjson::to_qjson_array(&self.attention_items)
    }
    fn background_jobs_value(&self) -> cxx_qt_lib::QJsonArray {
        crate::qjson::to_qjson_array(&self.background_jobs)
    }
    fn update_artifacts_value(&self) -> cxx_qt_lib::QJsonArray {
        crate::qjson::to_qjson_array(&self.update_artifacts)
    }
    fn updates_loading_value(&self) -> bool {
        self.updates_pending > 0
    }
    fn updates_error_value(&self) -> QString {
        qs(&self.updates_error)
    }
    fn attention_count_value(&self) -> i32 {
        self.attention_items.len() as i32
    }

    fn starting_contact_value(&self) -> QString {
        qs(&self.starting_contact)
    }
    fn last_working_directory_value(&self) -> QString {
        qs(&self.last_working_directory)
    }
    fn last_backend_value(&self) -> QString {
        qs(&self.last_backend)
    }
    fn backend_options_value(&self) -> cxx_qt_lib::QJsonArray {
        crate::qjson::to_qjson_array(&clarp_core::catalog::backend_options(&self.model_catalog))
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

    fn audio_mut(self: Pin<&mut Self>) -> Option<Pin<&mut AudioController>> {
        unsafe { self.rust_mut().get_unchecked_mut() }.audio.as_mut()
    }

    fn avatar_motion_mut(self: Pin<&mut Self>) -> Option<Pin<&mut AvatarMotionClock>> {
        unsafe { self.rust_mut().get_unchecked_mut() }.avatar_motion.as_mut()
    }

    fn narrator_mut(self: Pin<&mut Self>) -> Option<Pin<&mut ToolNarrator>> {
        unsafe { self.rust_mut().get_unchecked_mut() }.narrator.as_mut()
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
        let muted = self.muted;
        let raw = unsafe { self.as_mut().get_unchecked_mut() } as *mut AppController;
        if let Some(mut audio) = self.as_mut().audio_mut() {
            audio.as_mut().set_muted(muted);
            let this = ControllerPtr(raw);
            guards.push(audio.as_mut().connect_muted_changed(
                move |_, muted| unsafe { Pin::new_unchecked(&mut *this.get()) }.set_muted(muted),
                ConnectionType::QueuedConnection,
            ));
            let this = ControllerPtr(raw);
            guards.push(audio.as_mut().connect_media_error(
                move |_, message| unsafe { Pin::new_unchecked(&mut *this.get()) }.set_error(&message.to_string()),
                ConnectionType::QueuedConnection,
            ));
            let this = ControllerPtr(raw);
            guards.push(audio.connect_transcription_ready(
                move |_, text, trace, transcription, hands_free, target| {
                    let (text, trace, transcription, target) = (text.to_string(), trace.to_string(), transcription.to_string(), target.to_string());
                    unsafe { Pin::new_unchecked(&mut *this.get()) }.deliver_dictation(&text, &trace, &transcription, hands_free, &target);
                },
                ConnectionType::QueuedConnection,
            ));
        }
        let reduced = self.settings.boolean("appearance/reducedMotion", false);
        let this = ControllerPtr(unsafe { self.as_mut().get_unchecked_mut() } as *mut AppController);
        if let Some(mut motion) = self.as_mut().avatar_motion_mut() {
            motion.as_mut().set_reduced_motion(reduced);
            guards.push(motion.connect_changed(
                move |_| unsafe { Pin::new_unchecked(&mut *this.get()) }.save_reduced_motion(),
                ConnectionType::QueuedConnection,
            ));
        }
        let this = ControllerPtr(unsafe { self.as_mut().get_unchecked_mut() } as *mut AppController);
        if let Some(narrator) = self.as_mut().narrator_mut() {
            guards.push(narrator.connect_detail_level_changed(
                move |_| unsafe { Pin::new_unchecked(&mut *this.get()) }.save_narrator_level(),
                ConnectionType::QueuedConnection,
            ));
        }
        self.as_mut().rust_mut().guards.extend(guards);
    }

    fn save_narrator_level(mut self: Pin<&mut Self>) {
        let Some(level) = self.narrator.as_ref().map(|n| n.detail_level()) else { return };
        self.as_mut().rust_mut().settings.set("experiments/toolDetailLevel", level);
        if level > 0 {
            self.as_mut().rust_mut().settings.set("experiments/toolLastTranslationLevel", level);
        }
    }

    fn save_reduced_motion(mut self: Pin<&mut Self>) {
        let Some(reduced) = self.avatar_motion.as_ref().map(|m| m.reduced_motion_value_pub()) else { return };
        if self.settings.boolean("appearance/reducedMotion", false) != reduced {
            self.as_mut().rust_mut().settings.set("appearance/reducedMotion", reduced);
        }
    }

    fn bump_agent_revision(mut self: Pin<&mut Self>) {
        // Avatars pulse while their agent thinks, runs a tool or compacts.
        let active: std::collections::HashSet<String> = self
            .roster()
            .map(|roster| {
                roster
                    .sessions()
                    .into_iter()
                    .filter(|s| matches!(roster.display_state(s).as_deref(), Some("thinking" | "tool" | "compacting" | "running")))
                    .collect()
            })
            .unwrap_or_default();
        if let Some(motion) = self.as_mut().avatar_motion_mut() {
            motion.reconcile(&active);
        }
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
        if let Some(narrator) = self.as_mut().narrator_mut() {
            narrator.reset();
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
        let shared = self.compute_shared_filesystem();
        if shared != self.shared_filesystem {
            self.as_mut().rust_mut().shared_filesystem = shared;
            self.as_mut().shared_filesystem_changed();
        }
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

    // ---- drafts ------------------------------------------------------------

    fn draft_key(&self, session: &str) -> String {
        format!("{}/text", clarp_core::settings::draft_scope_key(&self.base_url, session))
    }

    /// A draft belongs to the chat on this Host, not the pane: any pane that
    /// opens the chat shows it.
    fn pane_draft(&self, _pane_id: &QString, session: &QString) -> QString {
        let session = session.to_string();
        if session.is_empty() {
            return QString::default();
        }
        let key = self.draft_key(&session);
        match self.pending_drafts.get(&key) {
            Some(text) => qs(text),
            None => qs(&self.settings.string(&key, "")),
        }
    }

    /// Drafts change on every keystroke; writing settings per key blocked
    /// typing in the C++ client, so text waits in memory until the composer
    /// has been idle for a second (or the controller closes).
    fn set_pane_draft(mut self: Pin<&mut Self>, pane_id: &QString, session: &QString, text: &QString) {
        let (pane, session_text, value) = (pane_id.to_string(), session.to_string(), text.to_string());
        if pane.is_empty() || session_text.is_empty() || self.pane_draft(pane_id, session).to_string() == value {
            return;
        }
        let key = self.draft_key(&session_text);
        let token = {
            let mut rust = self.as_mut().rust_mut();
            rust.pending_drafts.insert(key, value);
            rust.draft_flush_token += 1;
            rust.draft_flush_token
        };
        let qt = self.qt_thread();
        crate::runtime::after(Duration::from_millis(1000), move || {
            let queued = qt.queue(move |mut controller| {
                if controller.draft_flush_token == token {
                    controller.as_mut().rust_mut().flush_drafts();
                }
            });
            if queued.is_err() {
                eprintln!("AppController: dropped a draft flush; the controller is gone");
            }
        });
        self.draft_changed(session.clone(), text.clone(), pane_id.clone());
    }

    fn flush_pending_drafts(mut self: Pin<&mut Self>) {
        self.as_mut().rust_mut().flush_drafts();
    }

    // ---- connection --------------------------------------------------------

    fn connect_to_server(mut self: Pin<&mut Self>, url: &QString, token: &QString) {
        self.as_mut().set_base_url(url.clone());
        self.as_mut().rust_mut().token = token.to_string().trim().to_owned();
        self.connect_or_look_up_token();
    }

    /// Without a token, ask the keyring for this Host's first; the lookup
    /// connects either way.
    fn connect_or_look_up_token(self: Pin<&mut Self>) {
        if !self.token.is_empty() {
            self.reconnect();
            return;
        }
        let (base, qt) = (self.base_url.clone(), self.qt_thread());
        crate::runtime::handle().spawn(async move {
            let token = clarp_net::credentials::lookup(&base).await;
            if qt.queue(move |controller| controller.credential_looked_up(&base, token)).is_err() {
                eprintln!("AppController: dropped a keyring lookup; the controller is gone");
            }
        });
    }

    fn credential_looked_up(mut self: Pin<&mut Self>, server: &str, token: String) {
        if normalized_base_url(server) != self.base_url {
            return;
        }
        let stored = !token.is_empty();
        if self.token.is_empty() {
            self.as_mut().rust_mut().token = token;
        }
        self.as_mut().set_has_stored_credential(stored);
        self.reconnect();
    }

    fn set_has_stored_credential(mut self: Pin<&mut Self>, stored: bool) {
        if self.has_stored_credential != stored {
            self.as_mut().rust_mut().has_stored_credential = stored;
            self.has_stored_credential_changed();
        }
    }

    fn has_stored_credential_value(&self) -> bool {
        self.has_stored_credential
    }

    fn store_credential(self: Pin<&mut Self>, token: String) {
        let (base, qt) = (self.base_url.clone(), self.qt_thread());
        crate::runtime::handle().spawn(async move {
            let result = clarp_net::credentials::store(&base, &token).await;
            if qt.queue(move |controller| controller.credential_stored(&base, result)).is_err() {
                eprintln!("AppController: dropped a keyring store result; the controller is gone");
            }
        });
    }

    fn credential_stored(self: Pin<&mut Self>, server: &str, result: Result<(), String>) {
        match result {
            Err(message) => {
                eprintln!("AppController: storing the device token failed: {message}");
                if !message.is_empty() {
                    self.set_error(&message);
                }
            }
            Ok(()) if normalized_base_url(server) == self.base_url => self.set_has_stored_credential(true),
            Ok(()) => {}
        }
    }

    fn forget_credential(self: Pin<&mut Self>) {
        let (base, qt) = (self.base_url.clone(), self.qt_thread());
        crate::runtime::handle().spawn(async move {
            let result = clarp_net::credentials::remove(&base).await;
            if qt.queue(move |controller| controller.credential_removed(&base, result)).is_err() {
                eprintln!("AppController: dropped a keyring remove result; the controller is gone");
            }
        });
    }

    fn credential_removed(mut self: Pin<&mut Self>, server: &str, result: Result<(), String>) {
        if let Err(message) = result {
            eprintln!("AppController: forgetting the device token failed: {message}");
            self.set_error(&message);
            return;
        }
        if normalized_base_url(server) != self.base_url {
            return;
        }
        self.as_mut().rust_mut().token.clear();
        if let Some(sse) = self.as_mut().rust_mut().sse.as_mut() {
            sse.stop();
        }
        if let Some(audio) = self.as_mut().audio_mut() {
            audio.set_endpoint(None, "");
        }
        self.as_mut().set_connecting(false);
        self.as_mut().set_connection_state("offline");
        self.as_mut().set_error("");
        self.set_has_stored_credential(false);
    }

    fn pair_device(mut self: Pin<&mut Self>, url: &QString, code: &QString) {
        let code = code.to_string().trim().to_owned();
        if code.is_empty() {
            self.set_error("Enter the one-time pairing code");
            return;
        }
        self.as_mut().set_base_url(url.clone());
        let Some(endpoint) = Url::parse(&self.base_url).ok().filter(|u| u.host_str().is_some_and(|h| !h.is_empty())) else {
            self.set_error("Enter a valid Clarp server URL");
            return;
        };
        self.as_mut().rust_mut().token.clear();
        if let Some(sse) = self.as_mut().rust_mut().sse.as_mut() {
            sse.stop();
        }
        if let Some(api) = self.api.as_ref() {
            api.set_endpoint(endpoint, "");
        }
        self.as_mut().set_connecting(true);
        self.as_mut().set_connection_state("pairing");
        self.as_mut().set_error("");
        if let Some(api) = self.api.as_ref() {
            api.post_json("pairing", "/pairing/exchange", json!({"code": code, "device_name": "Clarp desktop"}), None);
        }
    }

    fn handle_pairing(mut self: Pin<&mut Self>, object: &Object) {
        let token = object.get("device").and_then(Value::as_object).map(|d| json::string(d, "token")).unwrap_or_default();
        if token.is_empty() {
            self.as_mut().set_connecting(false);
            self.as_mut().set_connection_state("offline");
            self.set_error("Pairing response did not contain a device credential");
            return;
        }
        self.as_mut().rust_mut().token = token.clone();
        self.as_mut().store_credential(token);
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
        if let Some(audio) = self.as_mut().audio_mut() {
            audio.set_endpoint(Some(endpoint.clone()), &token);
        }
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
        let uploads: Vec<String> = self.pending_uploads.keys().cloned().collect();
        for tag in uploads {
            self.as_mut().finish_upload(&tag, None);
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
        if self.as_mut().check_pending_created() {
            return;
        }

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
        self.as_mut().request_tail(session, false);
        self.request_recoverable_clips(session);
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

    fn send_internal(self: Pin<&mut Self>, session: &str, text: &str, queue_if_busy: bool) {
        self.send_with_voice(session, text, queue_if_busy, "", "", false);
    }

    /// A dictation goes to the chat it was recorded for, else the selection.
    fn deliver_dictation(mut self: Pin<&mut Self>, text: &str, trace: &str, transcription: &str, hands_free: bool, target: &str) {
        let target = clarp_core::protocol::voice_delivery_session(target, &self.selected_session).to_owned();
        self.as_mut().send_with_voice(&target, text, false, trace, transcription, hands_free);
    }

    fn send_with_voice(mut self: Pin<&mut Self>, session: &str, text: &str, queue_if_busy: bool, trace: &str, transcription: &str, hands_free: bool) {
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
        if let Some(audio) = self.as_mut().audio_mut() {
            audio.silence();
        }
        self.as_mut().set_sending(true);
        let mut body = json!({
            "session": session, "text": trimmed, "client_msg_id": client_id,
            "synthesize_audio": !self.muted, "hands_free": hands_free, "queue_if_busy": queue_if_busy,
        });
        if !trace.is_empty() {
            body["trace_id"] = json!(trace);
        }
        if !transcription.is_empty() {
            body["transcription_id"] = json!(transcription);
        }
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
        let pending: Vec<String> = self
            .attention_items
            .iter()
            .filter_map(|item| item.get("session").and_then(Value::as_str))
            .filter(|s| !s.is_empty())
            .map(str::to_owned)
            .collect();
        let next = self.roster().and_then(|r| r.next_attention_session(&self.selected_session, &pending));
        qs(&next.unwrap_or_default())
    }


    // ---- preferences and small queries -------------------------------------

    fn minimal_ui_value(&self) -> bool {
        self.minimal_ui
    }

    fn set_minimal_ui(mut self: Pin<&mut Self>, value: bool) {
        if self.minimal_ui == value {
            return;
        }
        self.as_mut().rust_mut().minimal_ui = value;
        self.as_mut().rust_mut().settings.set("appearance/minimalUi", value);
        self.minimal_ui_changed();
    }

    fn workspace_bar_visible_value(&self) -> bool {
        self.workspace_bar_visible
    }

    fn set_workspace_bar_visible(mut self: Pin<&mut Self>, value: bool) {
        if self.workspace_bar_visible == value {
            return;
        }
        self.as_mut().rust_mut().workspace_bar_visible = value;
        self.as_mut().rust_mut().settings.set("appearance/workspaceBar", value);
        self.workspace_bar_visible_changed();
    }

    fn timestamps_visible_value(&self) -> bool {
        self.timestamps_visible
    }

    fn set_timestamps_visible(mut self: Pin<&mut Self>, value: bool) {
        if self.timestamps_visible == value {
            return;
        }
        self.as_mut().rust_mut().timestamps_visible = value;
        self.as_mut().rust_mut().settings.set("conversation/timestampsVisible", value);
        self.timestamps_visible_changed();
    }

    fn muted_value(&self) -> bool {
        self.muted
    }

    fn set_muted(mut self: Pin<&mut Self>, value: bool) {
        if self.muted == value {
            return;
        }
        self.as_mut().rust_mut().muted = value;
        self.as_mut().rust_mut().settings.set("audio/muted", value);
        if let Some(audio) = self.as_mut().audio_mut() {
            audio.set_muted(value);
        }
        self.muted_changed();
    }

    fn pause_mobile_push_value(&self) -> bool {
        self.pause_mobile_push
    }

    fn set_pause_mobile_push(mut self: Pin<&mut Self>, value: bool) {
        if self.pause_mobile_push == value {
            return;
        }
        self.as_mut().rust_mut().pause_mobile_push = value;
        self.as_mut().rust_mut().settings.set("notifications/pauseMobileWhileDesktopActive", value);
        self.pause_mobile_push_changed();
    }

    fn new_agent_on_startup_value(&self) -> bool {
        self.new_agent_on_startup
    }

    fn set_new_agent_on_startup(mut self: Pin<&mut Self>, value: bool) {
        if self.new_agent_on_startup == value {
            return;
        }
        self.as_mut().rust_mut().new_agent_on_startup = value;
        self.as_mut().rust_mut().settings.set("launch/newAgentOnStartup", value);
        self.new_agent_on_startup_changed();
    }

    fn anonymous_agents_value(&self) -> bool {
        self.anonymous_agents
    }

    fn set_anonymous_agents(mut self: Pin<&mut Self>, value: bool) {
        if self.anonymous_agents == value {
            return;
        }
        self.as_mut().rust_mut().anonymous_agents = value;
        self.as_mut().rust_mut().settings.set("launch/anonymousAgents", value);
        self.anonymous_agents_changed();
    }

    fn shared_filesystem_value(&self) -> bool {
        self.shared_filesystem
    }

    fn set_shared_filesystem(mut self: Pin<&mut Self>, value: bool) {
        if self.shared_filesystem == value {
            return;
        }
        let key = clarp_core::settings::shared_filesystem_key(&self.base_url);
        self.as_mut().rust_mut().shared_filesystem = value;
        self.as_mut().rust_mut().settings.set(&key, value);
        self.shared_filesystem_changed();
    }

    fn reading_theme_value(&self) -> QString {
        qs(&self.reading_theme)
    }

    fn set_reading_theme(mut self: Pin<&mut Self>, value: QString) {
        let normalized = clarp_core::reading_theme::normalized_theme_id(&value.to_string());
        if self.reading_theme == normalized {
            return;
        }
        self.as_mut().rust_mut().reading_theme = normalized.clone();
        self.as_mut().rust_mut().settings.set("appearance/readingTheme", normalized);
        self.reading_theme_changed();
    }

    /// Fonts resolve here so a machine without Literata or Atkinson
    /// Hyperlegible falls back to the next listed family.
    fn reading_style_value(&self) -> cxx_qt_lib::QJsonObject {
        let style = clarp_core::reading_theme::style(&self.reading_theme, crate::fonts::installed);
        crate::qjson::to_qjson(&Value::Object(style)).to_object()
    }

    fn reading_themes_value(&self) -> cxx_qt_lib::QJsonArray {
        crate::qjson::to_qjson_array(&clarp_core::reading_theme::options())
    }

    // Placeholders until avatars, media, attachments and pair rooms land.
    fn zero_revision(&self) -> u64 {
        0
    }
    fn false_value(&self) -> bool {
        false
    }
    fn zero_count(&self) -> i32 {
        0
    }
    fn empty_array(&self) -> cxx_qt_lib::QJsonArray {
        cxx_qt_lib::QJsonArray::default()
    }

    fn agent_details(mut self: Pin<&mut Self>, session: &QString) -> cxx_qt_lib::QJsonObject {
        let session = session.to_string();
        let Some(agent) = self.roster_agent(&session).cloned() else { return cxx_qt_lib::QJsonObject::default() };
        let shared = self.shared_filesystem;
        let workspace = self.as_mut().rust_mut().workspace.describe(&agent.working_directory, shared);
        let state = self.roster().and_then(|r| r.display_state(&session)).unwrap_or_default();
        let details = json!({
            "agent_id": agent.agent_id, "session": agent.session, "name": display_name(&agent),
            "backend": agent.backend, "working_directory": agent.working_directory, "workspace": workspace,
            "model": agent.model, "effort": agent.effort,
            "default_effort": clarp_core::catalog::default_effort_for_model(&self.model_catalog, &agent.backend, &agent.model),
            "state": state, "status_text": agent.status_text, "context_tokens": agent.context_tokens,
            "context_window": agent.context_window, "queue_count": agent.queued_turn_count, "muted": agent.muted,
            "heartbeat_enabled": agent.heartbeat_enabled, "dreaming_enabled": agent.dreaming_enabled,
            "schedules": agent.schedules, "mcp_servers": agent.mcp_servers, "team_ids": agent.team_ids,
        });
        crate::qjson::to_qjson(&details).to_object()
    }

    fn agent_model(&self, session: &QString) -> QString {
        self.roster_agent(&session.to_string()).map_or_else(QString::default, |a| qs(&a.model))
    }

    fn agent_effort(&self, session: &QString) -> QString {
        self.roster_agent(&session.to_string()).map_or_else(QString::default, |a| qs(&a.effort))
    }

    fn agent_quota_notice(&self, session: &QString) -> QString {
        self.roster_agent(&session.to_string()).map_or_else(QString::default, |a| qs(&a.quota_notice(chrono::Utc::now())))
    }

    fn agent_name_by_id(&self, agent_id: &QString) -> QString {
        let id = agent_id.to_string();
        self.roster().and_then(|r| r.find_by_agent_id(&id)).map_or_else(|| qs(&id), |a| qs(display_name(a)))
    }

    fn agent_session_by_id(&self, agent_id: &QString) -> QString {
        let id = agent_id.to_string();
        self.roster().and_then(|r| r.find_by_agent_id(&id)).map_or_else(QString::default, |a| qs(&a.session))
    }

    fn matching_agents(&self, query: &QString) -> cxx_qt_lib::QJsonArray {
        let needle = query.to_string().trim().to_lowercase();
        let rows: Vec<Value> = self
            .roster()
            .map(|roster| roster.agents().to_vec())
            .unwrap_or_default()
            .iter()
            .filter(|agent| {
                needle.is_empty()
                    || [display_name(agent), agent.session.as_str(), agent.working_directory.as_str()]
                        .iter()
                        .any(|field| field.to_lowercase().contains(&needle))
            })
            .map(|agent| json!({"session": agent.session, "name": display_name(agent), "backend": agent.backend,
                                "state": agent.latest_state, "busy": agent.busy, "unread": agent.unread}))
            .collect();
        crate::qjson::to_qjson_array(&rows)
    }

    /// Agent-to-agent rooms are read-only Host projections, never agents.
    fn is_pair_session(&self, session: &QString) -> bool {
        session.to_string().starts_with("pair:")
    }

    fn chat_stamp(&self, time: i64) -> QString {
        qs(&clarp_core::time_format::chat_stamp(time, &chrono::Local::now()))
    }

    fn mark_startup(&self, milestone: &QString) {
        if std::env::var_os("CLARP_STARTUP_TRACE").is_some() {
            eprintln!("startup: {milestone}");
        }
    }

    fn markdown_display_blocks(&self, markdown: &QString) -> cxx_qt_lib::QStringList {
        let mut blocks = cxx_qt_lib::QStringList::default();
        for block in clarp_core::text::markdown_display_blocks(&markdown.to_string()) {
            blocks.append(qs(&block));
        }
        blocks
    }

    fn linkified_output(&self, text: &QString) -> QString {
        qs(&clarp_core::text::linkified_plain_text(&text.to_string()))
    }

    fn can_linkify_output(&self, text: &QString) -> bool {
        let text = text.to_string();
        text.chars().count() <= clarp_core::text::MAX_LINKIFIED_TEXT_LENGTH
            && ["http://", "https://", "www."].iter().any(|scheme| text.contains(scheme))
    }

    fn background_job_progress(&self, job: &cxx_qt_lib::QJsonObject) -> f64 {
        clarp_core::jobs::job_progress(&crate::qjson::from_qjson_object(job))
    }





    // ---- past sessions, launch directories, paths, assignment ----------------

    fn past_sessions_value(&self) -> cxx_qt_lib::QJsonArray {
        crate::qjson::to_qjson_array(&self.past_sessions)
    }
    fn past_sessions_loading_value(&self) -> bool {
        self.past_sessions_loading
    }
    fn launch_directories_value(&self) -> cxx_qt_lib::QJsonArray {
        crate::qjson::to_qjson_array(&self.launch_directories)
    }
    fn launch_directories_loading_value(&self) -> bool {
        self.launch_directories_loading
    }
    fn directory_suggestions_value(&self) -> cxx_qt_lib::QJsonArray {
        crate::qjson::to_qjson_array(&self.directory_suggestions)
    }
    fn favorite_paths_value(&self) -> cxx_qt_lib::QJsonArray {
        crate::qjson::to_qjson_array(&self.favorite_paths)
    }
    fn assignment_contacts_value(&self) -> cxx_qt_lib::QJsonArray {
        crate::qjson::to_qjson_array(&self.assignment_contacts)
    }

    fn resume_launch_session(mut self: Pin<&mut Self>, backend: &QString, session_id: &QString, anonymous: bool) -> bool {
        if self.as_mut().retry_created_agent() {
            return true;
        }
        let (backend, session_id) = (backend.to_string(), session_id.to_string());
        if !self.connected || session_id.is_empty() || !self.starting_contact.is_empty() {
            return false;
        }
        let mut body = json!({"backend": backend, "resume_session_id": session_id, "open_existing": true});
        body[if anonymous { "anonymous" } else { "auto_contact" }] = json!(true);
        self.post_contact_create("resume", &backend, body);
        true
    }

    fn load_past_sessions(mut self: Pin<&mut Self>, working_directory: &QString, backend: &QString, all_projects: bool) {
        let (cwd, backend) = (working_directory.to_string().trim().to_owned(), backend.to_string());
        if cwd.is_empty() || backend.is_empty() {
            return;
        }
        let generation = {
            let mut rust = self.as_mut().rust_mut();
            rust.past_sessions_loading = true;
            rust.past_sessions.clear();
            rust.past_sessions_generation += 1;
            rust.past_sessions_generation
        };
        self.as_mut().past_sessions_changed();
        let mut query = vec![("cwd", cwd.as_str()), ("backend", backend.as_str())];
        if all_projects {
            query.push(("scope", "all"));
        }
        if let Some(api) = self.api.as_ref() {
            api.get(&format!("past-sessions:{generation}"), "/past-sessions", &query);
        }
    }

    fn load_launch_directories(mut self: Pin<&mut Self>, query: &QString) {
        let generation = {
            let mut rust = self.as_mut().rust_mut();
            rust.launch_directories.clear();
            rust.launch_directories_loading = true;
            rust.launch_directory_generation += 1;
            rust.launch_directory_generation
        };
        self.as_mut().launch_directories_changed();
        if let Some(api) = self.api.as_ref() {
            api.get(&format!("launch-directories:{generation}"), "/launch-directories", &[("q", &query.to_string())]);
        }
    }

    fn load_directory_suggestions(mut self: Pin<&mut Self>, path: &QString) {
        let path = path.to_string().trim().to_owned();
        if path.is_empty() {
            if !self.directory_suggestions.is_empty() {
                self.as_mut().rust_mut().directory_suggestions.clear();
                self.paths_changed();
            }
            return;
        }
        if let Some(api) = self.api.as_ref() {
            api.get("directory-suggestions", "/dirs", &[("path", &path)]);
        }
    }

    fn load_favorite_paths(self: Pin<&mut Self>) {
        if let Some(api) = self.api.as_ref() {
            api.get("favorite-paths", "/favorite-paths", &[("limit", "5")]);
        }
    }

    fn load_assignment_contacts(mut self: Pin<&mut Self>, session: &QString) {
        let session = session.to_string();
        self.as_mut().rust_mut().assignment_session = session.clone();
        self.as_mut().rust_mut().assignment_contacts.clear();
        self.as_mut().assignment_contacts_changed();
        if let Some(api) = self.api.as_ref() {
            api.post_json(&format!("assignment-options:{session}"), "/agent-assign",
                          json!({"session": session, "mode": "options"}), None);
        }
    }

    fn request_contact_assignment(self: Pin<&mut Self>, session: &QString, automatic: bool) {
        self.contact_assignment_requested(session, automatic);
    }

    fn assign_contact(mut self: Pin<&mut Self>, session: &QString, mode: &QString, name: &QString) {
        let session = session.to_string();
        if session.is_empty() || !self.connected {
            self.set_error("Connect and select an agent before assigning a contact");
            return;
        }
        self.as_mut().set_error("");
        if let Some(api) = self.api.as_ref() {
            api.post_json(&format!("agent-assignment:{session}"), "/agent-assign",
                          json!({"session": session, "mode": mode.to_string(), "name": name.to_string()}), None);
        }
    }

    fn handle_launch_json(mut self: Pin<&mut Self>, tag: &str, object: &Object) -> bool {
        let array = |key: &str| object.get(key).and_then(Value::as_array).cloned().unwrap_or_default();
        if let Some(generation) = tag.strip_prefix("past-sessions:") {
            if generation.parse::<u64>().ok() == Some(self.past_sessions_generation) {
                self.as_mut().rust_mut().past_sessions = array("sessions");
                self.as_mut().rust_mut().past_sessions_loading = false;
                self.past_sessions_changed();
            }
        } else if let Some(generation) = tag.strip_prefix("launch-directories:") {
            if generation.parse::<u64>().ok() == Some(self.launch_directory_generation) {
                self.as_mut().rust_mut().launch_directories = array("matches");
                self.as_mut().apply_host_launch_directory_default(&json::string(object, "home"));
                self.as_mut().rust_mut().launch_directories_loading = false;
                self.launch_directories_changed();
            }
        } else if tag == "directory-suggestions" {
            self.as_mut().rust_mut().directory_suggestions = array("matches");
            self.paths_changed();
        } else if tag == "favorite-paths" {
            self.as_mut().rust_mut().favorite_paths = array("paths");
            self.paths_changed();
        } else if let Some(session) = tag.strip_prefix("assignment-options:") {
            if session == self.assignment_session {
                self.as_mut().rust_mut().assignment_contacts = array("contacts");
                self.assignment_contacts_changed();
            }
        } else if let Some(session) = tag.strip_prefix("agent-assignment:") {
            let session = qs(session);
            self.as_mut().request_snapshot();
            self.contact_assignment_succeeded(&session);
        } else {
            return false;
        }
        true
    }

    /// Returns true when the failure is stale and must not surface; current
    /// failures clear their loading state and fall through to the error.
    fn handle_launch_failure(mut self: Pin<&mut Self>, tag: &str) -> bool {
        if let Some(generation) = tag.strip_prefix("past-sessions:") {
            if generation.parse::<u64>().ok() != Some(self.past_sessions_generation) {
                return true;
            }
            self.as_mut().rust_mut().past_sessions_loading = false;
            self.past_sessions_changed();
        } else if let Some(generation) = tag.strip_prefix("launch-directories:") {
            if generation.parse::<u64>().ok() != Some(self.launch_directory_generation) {
                return true;
            }
            self.as_mut().rust_mut().launch_directories_loading = false;
            self.launch_directories_changed();
        } else if tag == "directory-suggestions" {
            self.as_mut().rust_mut().directory_suggestions.clear();
            self.paths_changed();
        } else if tag == "favorite-paths" {
            self.as_mut().rust_mut().favorite_paths.clear();
            self.paths_changed();
        }
        false
    }


    // ---- avatars and media ------------------------------------------------------

    fn avatar_revision_value(&self) -> u64 {
        self.avatar_revision
    }
    fn media_revision_value(&self) -> u64 {
        self.media_revision
    }

    fn agent_avatar_url(&self, session: &str) -> String {
        let Some(agent) = self.roster().and_then(|r| r.agents().iter().find(|a| a.session == session)) else {
            return String::new();
        };
        clarp_core::media::avatar_url(&agent.avatar_url, clarp_core::protocol::display_name(agent))
    }

    /// The portrait for `key`, requesting it when missing (C++
    /// `requestAvatarForSession`). Empty until it arrives.
    fn portrait(mut self: Pin<&mut Self>, key: &str, url: String, contact: bool) -> QUrl {
        let cache_root = self.portrait_cache();
        let base = self.base_url.clone();
        let tag = {
            let mut rust = self.as_mut().rust_mut();
            let next = rust.next_avatar_request + 1;
            let cache = if contact { &mut rust.contact_avatars } else { &mut rust.avatars };
            if url.is_empty() {
                cache.urls.remove(key);
                cache.sources.remove(key);
                cache.failures.remove(key);
                return QUrl::default();
            }
            if cache.urls.get(key) != Some(&url) {
                cache.urls.insert(key.to_owned(), url.clone());
                cache.sources.remove(key);
                cache.failures.remove(key);
            }
            if let Some(source) = cache.sources.get(key) {
                return QUrl::from(source.as_str());
            }
            if cache.failures.get(key) == Some(&url) {
                return QUrl::default();
            }
            if let Some(cached) = cache_root.map(|root| clarp_core::media::portrait_cache_path(&root, &format!("{base}{url}"))).filter(|p| p.exists()) {
                let source = Url::from_file_path(&cached).map(|u| u.to_string()).unwrap_or_default();
                cache.sources.insert(key.to_owned(), source.clone());
                return QUrl::from(source.as_str());
            }
            if cache.requests.values().any(|(k, u)| k == key && *u == url) {
                return QUrl::default();
            }
            let tag = format!("{}:{next}", if contact { "contact-avatar" } else { "avatar" });
            cache.requests.insert(tag.clone(), (key.to_owned(), url.clone()));
            rust.next_avatar_request = next;
            tag
        };
        if let Some(api) = self.api.as_ref() {
            api.get_bytes(&tag, &url);
        }
        QUrl::default()
    }

    fn avatar_source(mut self: Pin<&mut Self>, session: &QString) -> QUrl {
        let session = session.to_string();
        if session.is_empty() {
            return QUrl::default();
        }
        let url = self.agent_avatar_url(&session);
        self.as_mut().portrait(&session, url, false)
    }

    fn contact_avatar_source(mut self: Pin<&mut Self>, name: &QString) -> QUrl {
        let name = name.to_string();
        if name.is_empty() {
            return QUrl::default();
        }
        let url = self.contact_rows().iter().find(|c| c.name == name).map(|c| c.avatar_url.clone()).unwrap_or_default();
        self.as_mut().portrait(&name, url, true)
    }

    fn portrait_failed(mut self: Pin<&mut Self>, tag: &str) {
        let mut rust = self.as_mut().rust_mut();
        let cache = if tag.starts_with("contact-avatar:") { &mut rust.contact_avatars } else { &mut rust.avatars };
        if let Some((key, url)) = cache.requests.remove(tag) {
            cache.failures.insert(key, url);
        }
    }

    fn handle_portrait_bytes(self: Pin<&mut Self>, tag: String, bytes: Vec<u8>, content_type: &str) {
        let mime = clarp_core::media::mime(content_type);
        if bytes.is_empty() || bytes.len() > clarp_core::media::MAX_PORTRAIT_BYTES || !mime.starts_with("image/") {
            self.portrait_failed(&tag);
            return;
        }
        // Decoding and re-encoding costs tens of milliseconds a portrait;
        // do it off the GUI thread, then cache the result on disk.
        let qt = self.qt_thread();
        crate::runtime::handle().spawn_blocking(move || {
            let portrait = clarp_core::media::rounded_portrait(&bytes);
            let (bytes, mime) = match portrait {
                Some(png) => (png, "image/png".to_owned()),
                None => (bytes, mime),
            };
            if qt.queue(move |controller| controller.finish_portrait(&tag, bytes, &mime)).is_err() {
                eprintln!("AppController: dropped a portrait; the controller is gone");
            }
        });
    }

    fn finish_portrait(mut self: Pin<&mut Self>, tag: &str, bytes: Vec<u8>, mime: &str) {
        use base64::Engine;
        let contact = tag.starts_with("contact-avatar:");
        let request = {
            let mut rust = self.as_mut().rust_mut();
            let cache = if contact { &mut rust.contact_avatars } else { &mut rust.avatars };
            cache.requests.remove(tag)
        };
        let Some((key, url)) = request else { return };
        let current = if contact { self.contact_avatars.urls.get(&key) == Some(&url) } else { self.agent_avatar_url(&key) == url };
        if !current {
            return;
        }
        // Shown from memory now; the rounded PNG is cached for the next start.
        let source = format!("data:{mime};base64,{}", base64::engine::general_purpose::STANDARD.encode(&bytes));
        if mime == "image/png"
            && let Some(root) = self.portrait_cache()
        {
            let path = clarp_core::media::portrait_cache_path(&root, &format!("{}{url}", self.base_url));
            let written = path.parent().map_or(Ok(()), std::fs::create_dir_all).and_then(|_| std::fs::write(&path, &bytes));
            if let Err(error) = written {
                eprintln!("AppController: could not cache portrait {}: {error}", path.display());
            }
        }
        {
            let mut rust = self.as_mut().rust_mut();
            let cache = if contact { &mut rust.contact_avatars } else { &mut rust.avatars };
            cache.sources.insert(key.clone(), source);
            cache.failures.remove(&key);
            rust.avatar_revision += 1;
        }
        self.avatar_revision_changed();
    }

    fn media_for_session(&self, session: &QString) -> cxx_qt_lib::QJsonArray {
        crate::qjson::to_qjson_array(self.media_assets.get(&session.to_string()).map_or(&[][..], Vec::as_slice))
    }

    fn media_source(&self, asset_id: &QString) -> QUrl {
        self.media_sources.get(&asset_id.to_string()).map_or_else(QUrl::default, |u| QUrl::from(u.as_str()))
    }

    fn resolve_media_markdown(&self, markdown: &QString) -> QString {
        qs(&clarp_core::media::resolve_media_markdown(&markdown.to_string(), &self.media_sources))
    }

    fn load_media(mut self: Pin<&mut Self>, session: &QString) {
        let session = session.to_string();
        if session.is_empty() {
            return;
        }
        let tag = format!("media-list:{}", uuid::Uuid::new_v4());
        {
            let mut rust = self.as_mut().rust_mut();
            let generation = rust.media_generations.get(&session).copied().unwrap_or(0) + 1;
            rust.media_generations.insert(session.clone(), generation);
            rust.media_list_requests.insert(tag.clone(), (session.clone(), generation));
        }
        if let Some(api) = self.api.as_ref() {
            api.get(&tag, "/media", &[("session", &session), ("limit", "100")]);
        }
    }

    fn handle_media_list(mut self: Pin<&mut Self>, tag: &str, object: &Object) {
        let Some((session, generation)) = self.as_mut().rust_mut().media_list_requests.remove(tag) else { return };
        if self.media_generations.get(&session) != Some(&generation) {
            return;
        }
        let assets = object.get("assets").and_then(Value::as_array).cloned().unwrap_or_default();
        let mut fetch = Vec::new();
        for asset in &assets {
            let id = asset.get("asset_id").and_then(Value::as_str).unwrap_or_default();
            let mime = asset.get("mime_type").and_then(Value::as_str).unwrap_or_default();
            let url = asset.get("url").and_then(Value::as_str).unwrap_or_default();
            if id.is_empty() || !mime.starts_with("image/") || url.is_empty() || self.media_sources.contains_key(id) {
                continue;
            }
            fetch.push((format!("media-content:{}", uuid::Uuid::new_v4()), id.to_owned(), url.to_owned()));
        }
        {
            let mut rust = self.as_mut().rust_mut();
            rust.media_assets.insert(session.clone(), assets);
            for (tag, id, _) in &fetch {
                rust.media_content_requests.insert(tag.clone(), (id.clone(), session.clone()));
            }
            rust.media_revision += 1;
        }
        if let Some(api) = self.api.as_ref() {
            for (tag, _, url) in &fetch {
                api.get_bytes(tag, url);
            }
        }
        self.media_changed();
    }

    fn handle_media_bytes(mut self: Pin<&mut Self>, tag: &str, bytes: &[u8], content_type: &str) {
        let Some((asset, session)) = self.as_mut().rust_mut().media_content_requests.remove(tag) else { return };
        let still_current = self
            .media_assets
            .get(&session)
            .is_some_and(|assets| assets.iter().any(|a| a.get("asset_id").and_then(Value::as_str) == Some(asset.as_str())));
        let mime = clarp_core::media::mime(content_type);
        if asset.is_empty() || !still_current || bytes.is_empty() || bytes.len() > clarp_core::media::MAX_INLINE_MEDIA_BYTES || !mime.starts_with("image/") {
            return;
        }
        // Image bytes stay out of Markdown/QML strings: a data URL would be
        // copied each time a transcript delegate is rebuilt.
        if self.media_directory.is_none() {
            let directory = clarp_core::media::cache_dir().map(|root| root.join(format!("media-{}", std::process::id())));
            match directory.as_ref().map(std::fs::create_dir_all) {
                Some(Ok(())) => self.as_mut().rust_mut().media_directory = directory,
                other => {
                    if let Some(Err(error)) = other {
                        eprintln!("AppController: could not create the media cache: {error}");
                    }
                    self.set_error("Unable to cache chat images locally");
                    return;
                }
            }
        }
        let Some(directory) = self.media_directory.clone() else { return };
        let path = directory.join(clarp_core::media::media_file_name(&self.base_url, &asset));
        if let Err(error) = std::fs::write(&path, bytes) {
            eprintln!("AppController: could not cache {}: {error}", path.display());
            self.set_error("Unable to cache chat image");
            return;
        }
        let url = Url::from_file_path(&path).map(|u| u.to_string()).unwrap_or_default();
        {
            let mut rust = self.as_mut().rust_mut();
            rust.media_sources.insert(asset, url);
            rust.media_revision += 1;
        }
        self.media_changed();
    }

    fn handle_bytes(self: Pin<&mut Self>, tag: String, bytes: Vec<u8>, content_type: String) {
        if tag.starts_with("media-content:") {
            self.handle_media_bytes(&tag, &bytes, &content_type);
        } else if tag.starts_with("avatar:") || tag.starts_with("contact-avatar:") {
            self.handle_portrait_bytes(tag, bytes, &content_type);
        } else {
            eprintln!("AppController: unexpected bytes reply {tag}");
        }
    }

    // ---- composer attachments -------------------------------------------------

    fn attachments_key(&self, session: &str) -> String {
        format!("{}/attachments", clarp_core::settings::draft_scope_key(&self.base_url, session))
    }

    fn attachments_for(&self, session: &str) -> Vec<Value> {
        if session.is_empty() {
            return Vec::new();
        }
        self.settings.get(&self.attachments_key(session)).and_then(Value::as_array).cloned().unwrap_or_default()
    }

    fn store_attachments(mut self: Pin<&mut Self>, session: &str, attachments: Vec<Value>) {
        if session.is_empty() {
            return;
        }
        let key = self.attachments_key(session);
        if attachments.is_empty() {
            self.as_mut().rust_mut().settings.remove(&key);
        } else {
            self.as_mut().rust_mut().settings.set(&key, Value::Array(attachments));
        }
        self.as_mut().rust_mut().composer_revision += 1;
        self.composer_revision_changed();
    }

    fn composer_revision_value(&self) -> u64 {
        self.composer_revision
    }

    fn uploading_value(&self) -> bool {
        !self.pending_uploads.is_empty()
    }

    /// Attachments belong to the chat on this Host, like drafts.
    fn composer_attachments(&self, _pane_id: &QString, session: &QString) -> cxx_qt_lib::QJsonArray {
        crate::qjson::to_qjson_array(&self.attachments_for(&session.to_string()))
    }

    fn composer_can_send(&self, _pane_id: &QString, session: &QString) -> bool {
        clarp_core::attachments::can_send(&self.attachments_for(&session.to_string()))
    }

    fn attach_local_file(mut self: Pin<&mut Self>, pane_id: &QString, session: &QString, file_url: &cxx_qt_lib::QUrl) {
        let (pane, session) = (pane_id.to_string(), session.to_string());
        let path = Url::parse(&file_url.to_string()).ok().and_then(|u| u.to_file_path().ok());
        let metadata = path.as_ref().and_then(|p| std::fs::metadata(p).ok());
        let valid = metadata.as_ref().is_some_and(|m| m.is_file() && m.len() > 0 && m.len() <= clarp_core::attachments::MAX_UPLOAD_BYTES);
        let (Some(path), true) = (path, valid && !pane.is_empty() && !session.is_empty()) else {
            self.set_error("Choose a readable file no larger than 50 MB");
            return;
        };
        let canonical = std::fs::canonicalize(&path).unwrap_or(path.clone());
        let name = canonical.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
        let bytes = match std::fs::read(&canonical) {
            Ok(bytes) => bytes,
            Err(error) => {
                eprintln!("AppController: could not read {}: {error}", canonical.display());
                self.set_error(&format!("Could not read {name}"));
                return;
            }
        };
        let content_type = clarp_core::attachments::content_type(&name, &bytes[..bytes.len().min(64)]);
        let id = uuid::Uuid::new_v4().to_string();
        let mut attachments = self.attachments_for(&session);
        if self.shared_filesystem {
            // The Host reads the file where it is; nothing is uploaded.
            attachments.push(json!({"id": id, "path": canonical.to_string_lossy(), "name": name,
                                    "content_type": content_type, "local": true, "status": "ready"}));
            self.store_attachments(&session, attachments);
            return;
        }
        let tag = format!("composer-upload:{id}");
        let pending = json!({"session": session, "id": id, "name": name, "content_type": content_type,
                             "local_source": canonical.to_string_lossy(), "status": "uploading"});
        self.as_mut().rust_mut().pending_uploads.insert(tag.clone(), pending.as_object().cloned().unwrap_or_default());
        attachments.push(pending);
        self.as_mut().store_attachments(&session, attachments);
        if let Some(api) = self.api.as_ref() {
            let encoded_name = clarp_core::endpoint::percent_encode_segment(&name);
            api.post_bytes(&tag, "/upload", bytes, &content_type,
                           &[("X-File-Name", &encoded_name), ("X-Session", &session), ("X-Upload-ID", &id)]);
        }
    }

    fn remove_composer_attachment(mut self: Pin<&mut Self>, _pane_id: &QString, session: &QString, attachment_id: &QString) {
        let (session, id) = (session.to_string(), attachment_id.to_string());
        self.as_mut().rust_mut().pending_uploads.retain(|_, pending| {
            !(json::string(pending, "session") == session && json::string(pending, "id") == id)
        });
        let mut attachments = self.attachments_for(&session);
        let before = attachments.len();
        attachments.retain(|a| a.get("id").and_then(Value::as_str) != Some(id.as_str()));
        if attachments.len() != before {
            self.store_attachments(&session, attachments);
        }
    }

    fn send_composer_message(mut self: Pin<&mut Self>, pane_id: &QString, session: &QString, text: &QString, queue_if_busy: bool) -> bool {
        let session_id = session.to_string();
        let Some(outbound) = clarp_core::attachments::outbound_text(&text.to_string(), &self.attachments_for(&session_id)) else {
            self.set_error("Wait for attachments to finish uploading or remove them");
            return false;
        };
        if session_id.is_empty() || outbound.is_empty() {
            return false;
        }
        self.as_mut().set_pane_draft(pane_id, session, &QString::default());
        let key = self.attachments_key(&session_id);
        self.as_mut().rust_mut().settings.remove(&key);
        self.as_mut().rust_mut().composer_revision += 1;
        self.as_mut().composer_revision_changed();
        self.send_internal(&session_id, &outbound, queue_if_busy);
        true
    }

    fn paste_clipboard_image(self: Pin<&mut Self>, _pane_id: &QString, _session: &QString) -> bool {
        false
    }

    fn finish_upload(mut self: Pin<&mut Self>, tag: &str, object: Option<&Object>) {
        let Some(pending) = self.as_mut().rust_mut().pending_uploads.remove(tag) else { return };
        let (session, id) = (json::string(&pending, "session"), json::string(&pending, "id"));
        let server_path = object.map(|o| json::string(o, "path")).unwrap_or_default();
        let mut attachments = self.attachments_for(&session);
        let Some(slot) = attachments.iter_mut().find(|a| a.get("id").and_then(Value::as_str) == Some(id.as_str())) else {
            self.composer_revision_changed();
            return;
        };
        if server_path.is_empty() {
            slot["status"] = json!("failed");
            let completed_without_path = object.is_some();
            self.as_mut().store_attachments(&session, attachments);
            if completed_without_path {
                self.set_error("Upload completed without a file path");
            }
            return;
        }
        slot["path"] = json!(server_path);
        slot["status"] = json!("ready");
        if let Some(name) = object.map(|o| json::string(o, "name")).filter(|n| !n.is_empty()) {
            slot["name"] = json!(name);
        }
        self.store_attachments(&session, attachments);
    }

    // ---- profile, settings status, voices, orchestrator ----------------------

    fn qobject(value: &Object) -> cxx_qt_lib::QJsonObject {
        crate::qjson::to_qjson(&Value::Object(value.clone())).to_object()
    }
    fn profile_task_plan_value(&self) -> cxx_qt_lib::QJsonObject {
        Self::qobject(&self.profile_task_plan)
    }
    fn profile_session_value(&self) -> QString {
        qs(&self.profile_session)
    }
    fn profile_loading_value(&self) -> bool {
        self.profile_loading
    }
    fn profile_error_value(&self) -> QString {
        qs(&self.profile_error)
    }
    fn profile_prompts_value(&self) -> cxx_qt_lib::QJsonArray {
        crate::qjson::to_qjson_array(&self.profile_prompts)
    }
    fn profile_prompts_have_more_value(&self) -> bool {
        self.profile_prompts_have_more
    }
    fn profile_prompts_loading_value(&self) -> bool {
        self.profile_prompts_loading
    }
    fn profile_heartbeat_value(&self) -> cxx_qt_lib::QJsonObject {
        Self::qobject(&self.profile_heartbeat)
    }
    fn diagnostics_health_value(&self) -> cxx_qt_lib::QJsonObject {
        Self::qobject(&self.diagnostics_health)
    }
    fn transcription_capabilities_value(&self) -> cxx_qt_lib::QJsonObject {
        Self::qobject(&self.transcription_capabilities)
    }
    fn tts_provider_status_value(&self) -> cxx_qt_lib::QJsonObject {
        Self::qobject(&self.tts_provider_status)
    }
    fn settings_status_loading_value(&self) -> bool {
        self.settings_status_pending > 0
    }
    fn voice_bio_value(&self) -> QString {
        qs(&self.voice_bio)
    }
    fn voices_loading_value(&self) -> bool {
        self.voices_loading
    }
    fn orchestrator_settings_value(&self) -> cxx_qt_lib::QJsonObject {
        Self::qobject(&self.orchestrator_settings)
    }
    fn orchestrator_last_decision_value(&self) -> QString {
        qs(&self.orchestrator_last_decision)
    }
    fn orchestrator_loading_value(&self) -> bool {
        self.orchestrator_loading
    }

    fn load_agent_profile(mut self: Pin<&mut Self>, session: &QString) {
        let session = session.to_string();
        if session.is_empty() {
            return;
        }
        let generation = {
            let mut rust = self.as_mut().rust_mut();
            rust.profile_session = session.clone();
            rust.profile_task_plan.clear();
            rust.profile_heartbeat.clear();
            rust.profile_prompts.clear();
            rust.profile_error.clear();
            rust.profile_prompt_cursor.clear();
            rust.profile_prompts_have_more = false;
            rust.profile_loading = true;
            rust.profile_generation += 1;
            rust.profile_generation
        };
        self.as_mut().profile_changed();
        if let Some(api) = self.api.as_ref() {
            api.get(&format!("profile:{generation}:{session}"), "/task-plan", &[("session", &session)]);
            api.get(&format!("profile-heartbeat:{generation}:{session}"), "/agent-heartbeat/status", &[("session", &session)]);
        }
        // Media for the profile gallery arrives with the media step.
        self.as_mut().history_for(&session, false);
        self.as_mut().load_updates();
        self.load_teams();
    }

    fn load_prompt_history(self: Pin<&mut Self>, session: &QString, load_more: bool) {
        self.history_for(&session.to_string(), load_more);
    }

    fn history_for(mut self: Pin<&mut Self>, session: &str, load_more: bool) {
        if session.is_empty()
            || (load_more && (session != self.profile_session || !self.profile_prompts_have_more || self.profile_prompt_cursor.is_empty()))
        {
            return;
        }
        let tag = format!("prompt-history:{}", uuid::Uuid::new_v4());
        let cursor = self.profile_prompt_cursor.clone();
        {
            let mut rust = self.as_mut().rust_mut();
            if !load_more {
                rust.profile_session = session.to_owned();
                rust.profile_prompts.clear();
                rust.profile_prompt_cursor.clear();
                rust.profile_prompts_have_more = false;
            }
            rust.prompt_history_generation += 1;
            rust.profile_prompts_loading = true;
            let generation = rust.prompt_history_generation;
            rust.prompt_history_requests.insert(tag.clone(), (session.to_owned(), generation, load_more));
        }
        self.as_mut().profile_changed();
        if let Some(api) = self.api.as_ref() {
            let mut query = vec![("session", session), ("limit", "20")];
            if load_more {
                query.push(("before", &cursor));
            }
            api.get(&tag, "/identity/prompt-history", &query);
        }
    }

    fn load_settings_status(mut self: Pin<&mut Self>) {
        let generation = {
            let mut rust = self.as_mut().rust_mut();
            rust.settings_status_generation += 1;
            rust.settings_status_pending = 3;
            rust.settings_status_generation
        };
        self.as_mut().settings_status_changed();
        if let Some(api) = self.api.as_ref() {
            api.get(&format!("settings-status:{generation}:diagnostics"), "/diagnostics/health", &[]);
            api.get(&format!("settings-status:{generation}:transcription"), "/transcription-capabilities", &[]);
            api.get(&format!("settings-status:{generation}:tts"), "/tts/providers", &[]);
        }
    }

    fn set_tts_providers(mut self: Pin<&mut Self>, provider: &QString, fallback: &QString, voice: &QString) {
        let provider = provider.to_string().trim().to_owned();
        if provider.is_empty() {
            return;
        }
        let fallback = fallback.to_string().trim().to_owned();
        self.as_mut().rust_mut().settings_status_pending += 1;
        self.as_mut().settings_status_changed();
        let body = json!({"provider": provider, "fallback": if fallback.is_empty() { "none".to_owned() } else { fallback },
                          "voice": voice.to_string().trim()});
        if let Some(api) = self.api.as_ref() {
            api.post_json("settings-action:tts", "/tts/providers", body, None);
        }
    }

    fn load_voices(self: Pin<&mut Self>, session: &QString) {
        self.voices_for(&session.to_string());
    }

    fn voices_for(mut self: Pin<&mut Self>, session: &str) {
        if session.is_empty() {
            return;
        }
        self.as_mut().rust_mut().voice_bio.clear();
        self.as_mut().rust_mut().voices_loading = true;
        self.as_mut().voices_changed();
        if let Some(api) = self.api.as_ref() {
            api.get(&format!("voices:{session}"), "/voices", &[("for", session)]);
        }
    }

    fn choose_voice(self: Pin<&mut Self>, session: &QString, voice_id: &QString) {
        let (session, voice) = (session.to_string(), voice_id.to_string());
        if session.is_empty() || voice.is_empty() {
            return;
        }
        if let Some(api) = self.api.as_ref() {
            api.post_json(&format!("voice-select:{session}"), "/agent-voice", json!({"session": session, "voice_id": voice}), None);
        }
    }

    fn load_orchestrator(mut self: Pin<&mut Self>) {
        self.as_mut().rust_mut().orchestrator_loading = true;
        self.as_mut().orchestrator_changed();
        if let Some(api) = self.api.as_ref() {
            api.get("orchestrator-load", "/orchestrator/settings", &[]);
        }
    }

    #[allow(clippy::too_many_arguments)]
    fn save_orchestrator(mut self: Pin<&mut Self>, enabled: bool, fallback_only: bool, confidence: f64, provider: &QString, model: &QString, effort: &QString, timeout_ms: i32) {
        self.as_mut().rust_mut().orchestrator_loading = true;
        self.as_mut().orchestrator_changed();
        let provider = provider.to_string();
        let body = json!({
            "enabled": enabled, "fallback_only": fallback_only, "confidence_threshold": confidence.clamp(0.5, 0.99),
            "provider": if provider.is_empty() { "openai".to_owned() } else { provider },
            "model": model.to_string().trim(), "effort": effort.to_string().trim(), "timeout_ms": timeout_ms.clamp(250, 60_000),
        });
        if let Some(api) = self.api.as_ref() {
            api.post_json("orchestrator-save", "/orchestrator/settings", body, None);
        }
    }

    fn handle_profile_json(mut self: Pin<&mut Self>, tag: &str, object: &Object) -> bool {
        let fenced = |rest: &str, generation: u64, session: &str| {
            let (g, s) = rest.split_once(':').unwrap_or((rest, ""));
            g.parse::<u64>().ok() == Some(generation) && s == session
        };
        if let Some(rest) = tag.strip_prefix("profile:") {
            if fenced(rest, self.profile_generation, &self.profile_session) {
                {
                    let mut rust = self.as_mut().rust_mut();
                    rust.profile_task_plan = json::object(object, "plan");
                    rust.profile_loading = false;
                    rust.profile_error.clear();
                }
                self.profile_changed();
            }
        } else if let Some(rest) = tag.strip_prefix("profile-heartbeat:") {
            if fenced(rest, self.profile_generation, &self.profile_session) {
                self.as_mut().rust_mut().profile_heartbeat = object.clone();
                self.profile_changed();
            }
        } else if tag.starts_with("prompt-history:") {
            let Some((session, generation, load_more)) = self.as_mut().rust_mut().prompt_history_requests.remove(tag) else { return true };
            if session != self.profile_session || generation != self.prompt_history_generation {
                return true;
            }
            let incoming = json::array(object, "prompts");
            let page = json::object(object, "page");
            {
                let mut rust = self.as_mut().rust_mut();
                if load_more {
                    let mut seen: HashSet<String> =
                        rust.profile_prompts.iter().filter_map(|p| p.get("turn_id").and_then(Value::as_str)).map(str::to_owned).collect();
                    for prompt in incoming {
                        let id = prompt.get("turn_id").and_then(Value::as_str).unwrap_or_default().to_owned();
                        if id.is_empty() || seen.insert(id) {
                            rust.profile_prompts.push(prompt);
                        }
                    }
                } else {
                    rust.profile_prompts = incoming;
                }
                rust.profile_prompts_have_more = json::boolean(&page, "has_more");
                rust.profile_prompt_cursor = json::string(&page, "next_before");
                rust.profile_prompts_loading = false;
            }
            self.profile_changed();
        } else if let Some(rest) = tag.strip_prefix("settings-status:") {
            let (generation, kind) = rest.split_once(':').unwrap_or((rest, ""));
            if generation.parse::<u64>().ok() != Some(self.settings_status_generation) {
                return true;
            }
            {
                let mut rust = self.as_mut().rust_mut();
                match kind {
                    "diagnostics" => rust.diagnostics_health = object.clone(),
                    "transcription" => rust.transcription_capabilities = object.clone(),
                    "tts" => rust.tts_provider_status = object.clone(),
                    _ => {}
                }
                rust.settings_status_pending = (rust.settings_status_pending - 1).max(0);
            }
            self.settings_status_changed();
        } else if tag == "settings-action:tts" {
            {
                let mut rust = self.as_mut().rust_mut();
                rust.tts_provider_status = object.clone();
                rust.settings_status_pending = (rust.settings_status_pending - 1).max(0);
            }
            self.settings_status_changed();
        } else if let Some(session) = tag.strip_prefix("voices:") {
            let voice_id = self.roster_agent(session).map(|a| a.voice_id.clone()).unwrap_or_default();
            let voices = clarp_core::directory::voices_from_response(object, &voice_id);
            self.as_mut().rust_mut().voice_bio = json::string(object, "bio");
            if let Some(model) = unsafe { self.as_mut().rust_mut().get_unchecked_mut() }.voices.as_mut() {
                model.replace(voices);
            }
            self.as_mut().rust_mut().voices_loading = false;
            self.voices_changed();
        } else if let Some(session) = tag.strip_prefix("voice-select:") {
            let session = session.to_owned();
            self.as_mut().request_snapshot();
            self.as_mut().voices_for(&session);
            self.agent_mutation_succeeded(qs(&session));
        } else if tag == "orchestrator-load" || tag == "orchestrator-save" {
            let recent = json::array(object, "recent_decisions");
            {
                let mut rust = self.as_mut().rust_mut();
                rust.orchestrator_settings = json::object(object, "settings");
                if let Some(decision) = recent.first().and_then(Value::as_object) {
                    let action = decision.get("final_action").and_then(Value::as_str).map_or_else(|| json::string(decision, "decision_kind"), str::to_owned);
                    let target = decision.get("target_session").and_then(Value::as_str).unwrap_or("none").to_owned();
                    let confidence = decision.get("confidence").and_then(Value::as_f64).unwrap_or(0.0);
                    rust.orchestrator_last_decision = format!("{action}: {target} ({confidence:.2})");
                } else if object.contains_key("recent_decisions") {
                    rust.orchestrator_last_decision = "No decisions logged yet.".into();
                }
                rust.orchestrator_loading = false;
            }
            self.orchestrator_changed();
        } else {
            return false;
        }
        true
    }

    fn handle_profile_failure(mut self: Pin<&mut Self>, tag: &str, detail: &str) -> bool {
        if let Some(rest) = tag.strip_prefix("profile:") {
            if rest.split(':').next().and_then(|g| g.parse::<u64>().ok()) == Some(self.profile_generation) {
                self.as_mut().rust_mut().profile_loading = false;
                self.as_mut().rust_mut().profile_error = detail.to_owned();
                self.profile_changed();
            }
        } else if tag.starts_with("profile-heartbeat:") {
            // Optional on older Hosts; the profile stays usable without it.
        } else if tag.starts_with("prompt-history:") {
            let request = self.as_mut().rust_mut().prompt_history_requests.remove(tag);
            if request.is_some_and(|(_, generation, _)| generation == self.prompt_history_generation) {
                self.as_mut().rust_mut().profile_prompts_loading = false;
                self.as_mut().rust_mut().profile_error = detail.to_owned();
                self.profile_changed();
            }
        } else if let Some(rest) = tag.strip_prefix("settings-status:") {
            if rest.split(':').next().and_then(|g| g.parse::<u64>().ok()) == Some(self.settings_status_generation) {
                let pending = (self.settings_status_pending - 1).max(0);
                self.as_mut().rust_mut().settings_status_pending = pending;
                self.settings_status_changed();
            }
        } else if tag == "settings-action:tts" {
            let pending = (self.settings_status_pending - 1).max(0);
            self.as_mut().rust_mut().settings_status_pending = pending;
            self.as_mut().settings_status_changed();
            self.set_error(detail);
        } else if tag.starts_with("voices:") {
            self.as_mut().rust_mut().voices_loading = false;
            self.as_mut().voices_changed();
            self.set_error(detail);
        } else if tag.starts_with("orchestrator-") {
            self.as_mut().rust_mut().orchestrator_loading = false;
            self.as_mut().orchestrator_changed();
            self.set_error(detail);
        } else {
            return false;
        }
        true
    }

    // ---- teams and the turn queue --------------------------------------------

    fn teams_value(&self) -> cxx_qt_lib::QJsonArray {
        crate::qjson::to_qjson_array(&self.teams)
    }
    fn team_messages_value(&self) -> cxx_qt_lib::QJsonArray {
        crate::qjson::to_qjson_array(&self.team_messages)
    }
    fn selected_team_id_value(&self) -> QString {
        qs(&self.selected_team_id)
    }
    fn teams_loading_value(&self) -> bool {
        self.team_list_loading || self.team_messages_loading
    }
    fn teams_error_value(&self) -> QString {
        qs(&self.teams_error)
    }
    fn turn_queue_items_value(&self) -> cxx_qt_lib::QJsonArray {
        crate::qjson::to_qjson_array(&self.turn_queue_items)
    }
    fn turn_queue_session_value(&self) -> QString {
        qs(&self.turn_queue_session)
    }
    fn turn_queue_paused_value(&self) -> bool {
        self.turn_queue_paused
    }
    fn turn_queue_loading_value(&self) -> bool {
        self.turn_queue_loading
    }
    fn turn_queue_error_value(&self) -> QString {
        qs(&self.turn_queue_error)
    }

    fn load_teams(mut self: Pin<&mut Self>) {
        let generation = {
            let mut rust = self.as_mut().rust_mut();
            rust.team_list_generation += 1;
            rust.team_list_loading = true;
            rust.teams_error.clear();
            rust.team_list_generation
        };
        self.as_mut().teams_changed();
        if let Some(api) = self.api.as_ref() {
            api.get(&format!("team-list:{generation}"), "/teams", &[]);
        }
    }

    fn select_team(self: Pin<&mut Self>, team_id: &QString) {
        self.select_team_id(&team_id.to_string());
    }

    fn select_team_id(mut self: Pin<&mut Self>, team_id: &str) {
        if team_id.is_empty() {
            return;
        }
        let generation = {
            let mut rust = self.as_mut().rust_mut();
            let changed = rust.selected_team_id != team_id;
            rust.selected_team_id = team_id.to_owned();
            if changed {
                rust.team_messages.clear();
            }
            rust.team_messages_generation += 1;
            rust.team_messages_loading = changed || rust.team_messages.is_empty();
            rust.team_messages_generation
        };
        self.as_mut().teams_changed();
        let path = format!("/teams/{}/messages", clarp_core::endpoint::percent_encode_segment(team_id));
        if let Some(api) = self.api.as_ref() {
            api.get(&format!("team-messages:{generation}:{team_id}"), &path, &[("limit", "100")]);
        }
    }

    fn team_action(&self, tag: &str, method: &str, path: &str, body: Value) {
        let Some(api) = self.api.as_ref() else { return };
        match method {
            "DELETE" => api.delete(tag, path),
            _ => api.post_json(tag, path, body, None),
        }
    }

    fn create_team(self: Pin<&mut Self>, name: &QString, color: &QString) {
        let name = name.to_string().trim().to_owned();
        if !name.is_empty() {
            self.team_action("team-action:create", "POST", "/teams", json!({"name": name, "color": color.to_string()}));
        }
    }

    fn update_team(mut self: Pin<&mut Self>, team_id: &QString, name: &QString, color: &QString, leader_agent_id: &QString) {
        let (id, name, leader) = (team_id.to_string(), name.to_string().trim().to_owned(), leader_agent_id.to_string());
        if id.is_empty() || name.is_empty() {
            return;
        }
        if !leader.is_empty() {
            let member = self
                .teams
                .iter()
                .find(|t| t.get("team_id").and_then(Value::as_str) == Some(id.as_str()))
                .map(|t| t.get("member_agent_ids").and_then(Value::as_array).is_some_and(|m| m.iter().any(|a| a.as_str() == Some(leader.as_str()))));
            if member == Some(false) {
                self.as_mut().rust_mut().teams_error = "Team leader must already be a member".into();
                self.teams_changed();
                return;
            }
        }
        let path = format!("/teams/{}", clarp_core::endpoint::percent_encode_segment(&id));
        self.team_action("team-action:update", "POST", &path, json!({"name": name, "color": color.to_string().trim(), "leader": leader}));
    }

    fn add_team_member(self: Pin<&mut Self>, team_id: &QString, agent_id: &QString) {
        let (team, agent) = (team_id.to_string(), agent_id.to_string());
        if team.is_empty() || agent.is_empty() {
            return;
        }
        let path = format!("/teams/{}/members", clarp_core::endpoint::percent_encode_segment(&team));
        self.team_action("team-action:add-member", "POST", &path, json!({"agent_id": agent}));
    }

    fn remove_team_member(self: Pin<&mut Self>, team_id: &QString, agent_id: &QString) {
        let (team, agent) = (team_id.to_string(), agent_id.to_string());
        if team.is_empty() || agent.is_empty() {
            return;
        }
        let path = format!(
            "/teams/{}/members/{}",
            clarp_core::endpoint::percent_encode_segment(&team),
            clarp_core::endpoint::percent_encode_segment(&agent)
        );
        self.team_action("team-action:remove-member", "DELETE", &path, Value::Null);
    }

    fn set_team_nudging(self: Pin<&mut Self>, team_id: &QString, enabled: bool) {
        let team = team_id.to_string();
        if !team.is_empty() {
            self.team_action("team-action:nudging", "POST", "/team-nudging", json!({"team_id": team, "nudge_enabled": enabled}));
        }
    }

    fn delete_team(mut self: Pin<&mut Self>, team_id: &QString) {
        let team = team_id.to_string();
        if team.is_empty() {
            return;
        }
        if self.selected_team_id == team {
            self.as_mut().rust_mut().selected_team_id.clear();
            self.as_mut().rust_mut().team_messages.clear();
            self.as_mut().teams_changed();
        }
        let path = format!("/teams/{}", clarp_core::endpoint::percent_encode_segment(&team));
        self.team_action("team-action:delete", "DELETE", &path, Value::Null);
    }

    fn team_name_by_id(&self, team_id: &QString) -> QString {
        let id = team_id.to_string();
        let name = self
            .teams
            .iter()
            .find(|t| t.get("team_id").and_then(Value::as_str) == Some(id.as_str()))
            .map(|t| t.get("name").and_then(Value::as_str).unwrap_or(&id).to_owned());
        qs(&name.unwrap_or(id))
    }

    fn team_agent_choices(&self) -> cxx_qt_lib::QJsonArray {
        let choices: Vec<Value> = self
            .roster()
            .map(|r| r.agents().iter().map(|a| json!({"id": a.agent_id, "session": a.session, "name": display_name(a)})).collect())
            .unwrap_or_default();
        crate::qjson::to_qjson_array(&choices)
    }

    fn load_turn_queue(self: Pin<&mut Self>, session: &QString) {
        self.load_queue_for(&session.to_string());
    }

    fn load_queue_for(mut self: Pin<&mut Self>, session: &str) {
        if session.is_empty() {
            return;
        }
        let generation = {
            let mut rust = self.as_mut().rust_mut();
            if rust.turn_queue_session != session {
                rust.turn_queue_items.clear();
                rust.turn_queue_paused = false;
            }
            rust.turn_queue_session = session.to_owned();
            rust.turn_queue_loading = true;
            rust.turn_queue_error.clear();
            rust.turn_queue_generation += 1;
            rust.turn_queue_generation
        };
        self.as_mut().turn_queue_changed();
        if let Some(api) = self.api.as_ref() {
            api.get(&format!("turn-queue:{generation}:{session}"), "/turn-queue", &[("session", session)]);
        }
    }

    fn queue_action(mut self: Pin<&mut Self>, method: &str, path: String, body: Value) {
        if self.turn_queue_session.is_empty() {
            return;
        }
        let tag = format!("queue-action:{}", uuid::Uuid::new_v4());
        let session = self.turn_queue_session.clone();
        self.as_mut().rust_mut().queue_action_sessions.insert(tag.clone(), session);
        let Some(api) = self.api.as_ref() else { return };
        match method {
            "PUT" => api.put_json(&tag, &path, body),
            "DELETE" => api.delete(&tag, &path),
            _ => api.post_json(&tag, &path, body, None),
        }
    }

    fn update_queued_turn(self: Pin<&mut Self>, queue_id: &QString, text: &QString) {
        let (id, text) = (queue_id.to_string(), text.to_string().trim().to_owned());
        if !id.is_empty() && !text.is_empty() {
            self.queue_action("PUT", format!("/turn-queue/{}", clarp_core::endpoint::percent_encode_segment(&id)), json!({"text": text}));
        }
    }

    fn delete_queued_turn(self: Pin<&mut Self>, queue_id: &QString) {
        let id = queue_id.to_string();
        if !id.is_empty() {
            self.queue_action("DELETE", format!("/turn-queue/{}", clarp_core::endpoint::percent_encode_segment(&id)), Value::Null);
        }
    }

    fn send_queued_turn(self: Pin<&mut Self>, queue_id: &QString) {
        let id = queue_id.to_string();
        if !id.is_empty() {
            self.queue_action("POST", format!("/turn-queue/{}/send", clarp_core::endpoint::percent_encode_segment(&id)), json!({}));
        }
    }

    /// Replies for team and queue tags; true when the tag was one of them.
    fn handle_team_or_queue_json(mut self: Pin<&mut Self>, tag: &str, object: &Object) -> bool {
        if let Some(generation) = tag.strip_prefix("team-list:") {
            if generation.parse::<u64>().ok() != Some(self.team_list_generation) {
                return true;
            }
            let teams = json::array(object, "teams");
            let selected = self.selected_team_id.clone();
            let exists = selected.is_empty() || teams.iter().any(|t| t.get("team_id").and_then(Value::as_str) == Some(selected.as_str()));
            let first = teams.first().and_then(|t| t.get("team_id").and_then(Value::as_str)).map(str::to_owned);
            self.as_mut().rust_mut().teams = teams;
            self.as_mut().rust_mut().team_list_loading = false;
            self.as_mut().teams_changed();
            if !exists || selected.is_empty() {
                match first {
                    Some(first) => self.select_team_id(&first),
                    None if !exists => {
                        self.as_mut().rust_mut().selected_team_id.clear();
                        self.as_mut().rust_mut().team_messages.clear();
                        self.teams_changed();
                    }
                    None => {}
                }
            }
        } else if let Some(rest) = tag.strip_prefix("team-messages:") {
            let (generation, team) = rest.split_once(':').unwrap_or((rest, ""));
            if generation.parse::<u64>().ok() != Some(self.team_messages_generation) || team != self.selected_team_id {
                return true;
            }
            self.as_mut().rust_mut().team_messages = json::array(object, "messages");
            self.as_mut().rust_mut().team_messages_loading = false;
            self.teams_changed();
        } else if tag.starts_with("team-action:") {
            let selected = self.selected_team_id.clone();
            self.as_mut().load_teams();
            if !selected.is_empty() {
                self.select_team_id(&selected);
            }
        } else if let Some(rest) = tag.strip_prefix("turn-queue:") {
            let (generation, session) = rest.split_once(':').unwrap_or((rest, ""));
            if generation.parse::<u64>().ok() != Some(self.turn_queue_generation) || session != self.turn_queue_session {
                return true;
            }
            {
                let mut rust = self.as_mut().rust_mut();
                rust.turn_queue_items = json::array(object, "items");
                rust.turn_queue_paused = json::boolean(object, "paused");
                rust.turn_queue_loading = false;
                rust.turn_queue_error.clear();
            }
            self.turn_queue_changed();
        } else if tag.starts_with("queue-action:") {
            let session = self.as_mut().rust_mut().queue_action_sessions.remove(tag).unwrap_or_default();
            if !session.is_empty() && session == self.turn_queue_session {
                self.load_queue_for(&session);
            }
        } else {
            return false;
        }
        true
    }

    fn handle_team_or_queue_failure(mut self: Pin<&mut Self>, tag: &str, detail: &str) -> bool {
        if tag.starts_with("turn-queue:") || tag.starts_with("queue-action:") {
            if let Some(rest) = tag.strip_prefix("turn-queue:") {
                if rest.split(':').next().and_then(|g| g.parse::<u64>().ok()) != Some(self.turn_queue_generation) {
                    return true;
                }
                self.as_mut().rust_mut().turn_queue_loading = false;
            } else {
                self.as_mut().rust_mut().queue_action_sessions.remove(tag);
            }
            self.as_mut().rust_mut().turn_queue_error = detail.to_owned();
            self.turn_queue_changed();
            return true;
        }
        if tag.starts_with("team-list:") || tag.starts_with("team-messages:") || tag.starts_with("team-action:") {
            let generation = tag.split(':').nth(1).and_then(|g| g.parse::<u64>().ok());
            if tag.starts_with("team-list:") && generation != Some(self.team_list_generation) {
                return true;
            }
            if tag.starts_with("team-messages:") && generation != Some(self.team_messages_generation) {
                return true;
            }
            {
                let mut rust = self.as_mut().rust_mut();
                if tag.starts_with("team-list:") {
                    rust.team_list_loading = false;
                } else if tag.starts_with("team-messages:") {
                    rust.team_messages_loading = false;
                } else {
                    rust.team_list_loading = false;
                    rust.team_messages_loading = false;
                }
                rust.teams_error = detail.to_owned();
            }
            self.teams_changed();
            return true;
        }
        false
    }

    // ---- agent launch and lifecycle ------------------------------------------

    fn launch_directory_or_home(&self) -> String {
        if self.launch_directory.is_empty() { "~".into() } else { self.launch_directory.clone() }
    }

    fn launch_directory(&self) -> QString {
        qs(&self.launch_directory_or_home())
    }

    fn set_launch_directory(mut self: Pin<&mut Self>, path: &QString) {
        self.as_mut().rust_mut().launch_directory = path.to_string().trim().to_owned();
    }

    /// A fresh Host proposes its workspace root; only a still-default "~"
    /// is replaced, never a folder the user chose.
    fn apply_host_launch_directory_default(mut self: Pin<&mut Self>, directory: &str) {
        let directory = directory.trim();
        if directory.is_empty() || (!self.last_working_directory.trim().is_empty() && self.last_working_directory != "~") {
            return;
        }
        let changed = self.last_working_directory != directory || self.launch_directory != directory;
        self.as_mut().rust_mut().last_working_directory = directory.to_owned();
        self.as_mut().rust_mut().launch_directory = directory.to_owned();
        if changed {
            self.launch_defaults_changed();
        }
    }

    fn quick_start_backend_name(&self) -> String {
        if !self.last_backend.is_empty() {
            return self.last_backend.clone();
        }
        let selected = self.roster_agent(&self.selected_session).map(|a| a.backend.clone()).unwrap_or_default();
        if selected.is_empty() { "claude".into() } else { selected }
    }

    fn quick_start_backend(&self) -> QString {
        qs(&self.quick_start_backend_name())
    }

    fn contact_rows(&self) -> &[clarp_core::directory::Contact] {
        self.contacts.as_ref().map_or(&[], |model| model.contacts())
    }

    fn matching_contacts(&self, query: &QString) -> cxx_qt_lib::QJsonArray {
        let needle = query.to_string().trim().to_lowercase();
        let rows: Vec<Value> = self
            .contact_rows()
            .iter()
            .filter(|c| c.name.to_lowercase().contains(&needle))
            .map(|c| json!({"name": c.name, "description": c.description, "symbol": c.avatar_symbol, "avatarUrl": c.avatar_url}))
            .collect();
        crate::qjson::to_qjson_array(&rows)
    }

    /// A launch already created a session the roster has not shown yet: ask
    /// again instead of creating a second agent.
    fn retry_created_agent(mut self: Pin<&mut Self>) -> bool {
        if self.pending_created_session.is_empty() {
            return false;
        }
        self.as_mut().rust_mut().created_snapshot_attempts = 0;
        self.as_mut().set_error("");
        self.request_snapshot();
        true
    }

    fn post_contact_create(mut self: Pin<&mut Self>, starting: &str, backend: &str, mut body: Value) {
        self.as_mut().set_error("");
        self.as_mut().rust_mut().starting_contact = starting.to_owned();
        self.as_mut().rust_mut().starting_backend = backend.to_owned();
        self.as_mut().contact_launch_changed();
        body["cwd"] = json!(self.launch_directory_or_home());
        body["synthesize_audio"] = json!(false);
        if let Some(api) = self.api.as_ref() {
            api.post_json("contact-create", "/agents", body, None);
        }
    }

    fn with_llm(mut body: Value, model: &str, effort: &str) -> Value {
        if !model.trim().is_empty() {
            body["model"] = json!(model.trim());
        }
        if !effort.trim().is_empty() {
            body["effort"] = json!(effort.trim());
        }
        body
    }

    fn start_anonymous_agent(mut self: Pin<&mut Self>, backend: &QString, model: &QString, effort: &QString) -> bool {
        if self.as_mut().retry_created_agent() {
            return true;
        }
        let backend = backend.to_string();
        if !self.connected || backend.is_empty() || !self.starting_contact.is_empty() {
            return false;
        }
        let body = Self::with_llm(json!({"anonymous": true, "backend": backend}), &model.to_string(), &effort.to_string());
        self.post_contact_create("anonymous", &backend, body);
        true
    }

    fn start_available_contact(mut self: Pin<&mut Self>, backend: &QString, model: &QString, effort: &QString) -> bool {
        if self.as_mut().retry_created_agent() {
            return true;
        }
        let backend = backend.to_string();
        if !self.connected || backend.is_empty() || !self.starting_contact.is_empty() {
            return false;
        }
        let body = Self::with_llm(json!({"auto_contact": true, "backend": backend}), &model.to_string(), &effort.to_string());
        self.post_contact_create("pool", &backend, body);
        true
    }

    fn quick_start_contact(self: Pin<&mut Self>, name: &QString, backend: &QString, model: &QString, effort: &QString) -> bool {
        if !self.starting_contact.is_empty() {
            return false;
        }
        if !self.connected {
            self.set_error("Connect to the Host before starting a contact");
            return false;
        }
        let wanted = name.to_string().trim().to_lowercase();
        let Some(contact) = self.contact_rows().iter().find(|c| c.name.to_lowercase() == wanted).map(|c| c.name.clone()) else {
            self.set_error("This contact is no longer idle; choose its existing chat");
            return false;
        };
        let backend = backend.to_string().trim().to_owned();
        let backend = if backend.is_empty() { self.quick_start_backend_name() } else { backend };
        let body = Self::with_llm(json!({"name": contact, "backend": backend}), &model.to_string(), &effort.to_string());
        self.post_contact_create(&contact, &backend, body);
        true
    }

    fn create_agent(
        mut self: Pin<&mut Self>, name: &QString, working_directory: &QString, backend: &QString, model: &QString,
        effort: &QString, replace_session: &QString, mode: &QString, past_session_id: &QString, mcp_servers: &cxx_qt_lib::QJsonArray,
    ) {
        if self.as_mut().retry_created_agent() {
            return;
        }
        let (name, directory, backend) = (name.to_string().trim().to_owned(), working_directory.to_string().trim().to_owned(), backend.to_string().trim().to_owned());
        if name.is_empty() || directory.is_empty() || backend.is_empty() {
            self.set_error("Name, workspace, and backend are required");
            return;
        }
        let mut body = Self::with_llm(
            json!({"name": name, "session": name.to_lowercase(), "cwd": directory, "backend": backend, "synthesize_audio": false}),
            &model.to_string(), &effort.to_string(),
        );
        // MCP servers are chosen by name.
        let servers: Vec<Value> = mcp_servers
            .iter()
            .map(|v| v.to_string().to_string())
            .filter(|name| !name.is_empty())
            .map(Value::from)
            .collect();
        if !servers.is_empty() {
            body["mcp_servers"] = Value::Array(servers);
        }
        let changed = self.last_working_directory != directory || self.last_backend != backend;
        {
            let mut rust = self.as_mut().rust_mut();
            rust.last_working_directory = directory.clone();
            rust.last_backend = backend.clone();
            rust.settings.set("launch/workingDirectory", directory);
            rust.settings.set("launch/backend", backend);
        }
        if changed {
            self.as_mut().launch_defaults_changed();
        }
        let replace = replace_session.to_string();
        if !replace.is_empty() {
            body["replace_sid"] = json!(replace);
        }
        let past = past_session_id.to_string();
        match mode.to_string().as_str() {
            "resume" if !past.is_empty() => body["resume_session_id"] = json!(past),
            "fork" if !past.is_empty() => body["fork_session_id"] = json!(past),
            _ => {}
        }
        if let Some(api) = self.api.as_ref() {
            api.post_json(&format!("agent-create:{replace}"), "/agents", body, None);
        }
    }

    fn created_agent(mut self: Pin<&mut Self>, tag: &str, object: &Object) {
        if tag == "contact-create" {
            let backend = self.starting_backend.clone();
            if !backend.is_empty() && self.last_backend != backend {
                self.as_mut().rust_mut().last_backend = backend.clone();
                self.as_mut().rust_mut().settings.set("launch/backend", backend);
                self.as_mut().launch_defaults_changed();
            }
            let created_cwd = json::object(object, "agent").get("cwd").and_then(Value::as_str).map(str::to_owned);
            let directory = created_cwd.unwrap_or_else(|| self.launch_directory.clone());
            if !directory.is_empty() && self.last_working_directory != directory {
                self.as_mut().rust_mut().last_working_directory = directory.clone();
                self.as_mut().rust_mut().settings.set("launch/workingDirectory", directory);
                self.as_mut().launch_defaults_changed();
            }
            self.as_mut().rust_mut().starting_backend.clear();
        }
        let session = json::string(object, "session");
        if session.is_empty() {
            self.as_mut().rust_mut().starting_contact.clear();
            self.as_mut().contact_launch_changed();
            self.set_error("The Host did not return the new agent's session.");
            return;
        }
        let created = json::object(object, "agent");
        let upserted = json::string(&created, "session") == session
            && self.as_mut().agents_mut().is_some_and(|agents| agents.mutate(|core| core.upsert_created_agent(&created)));
        if upserted {
            {
                // Old fleet responses must not undo this authoritative creation.
                let mut rust = self.as_mut().rust_mut();
                rust.snapshot_generation += 1;
                rust.snapshot_in_flight = false;
                rust.snapshot_dirty = false;
                rust.pending_created_session.clear();
                rust.starting_contact.clear();
            }
            self.as_mut().contact_launch_changed();
            self.as_mut().finish_created(&session);
            return;
        }
        // Hosts that only return a session id: wait for the roster to show it,
        // ignoring any snapshot already in flight from before the creation.
        {
            let mut rust = self.as_mut().rust_mut();
            rust.snapshot_generation += 1;
            rust.snapshot_in_flight = false;
            rust.snapshot_dirty = false;
            rust.pending_created_session = session;
            rust.created_snapshot_attempts = 0;
        }
        self.request_snapshot();
    }

    fn finish_created(mut self: Pin<&mut Self>, session: &str) {
        self.as_mut().select(session);
        let pane = self.panes.as_ref().map(|p| p.core().active_pane_id().to_owned()).unwrap_or_default();
        self.as_mut().set_composer_focus(&pane);
        self.agent_mutation_succeeded(qs(session));
    }

    /// Called after every applied snapshot while a created session is awaited.
    fn check_pending_created(mut self: Pin<&mut Self>) -> bool {
        let expected = self.pending_created_session.clone();
        if expected.is_empty() {
            return false;
        }
        if self.roster_agent(&expected).is_none() {
            self.as_mut().rust_mut().created_snapshot_attempts += 1;
            if self.created_snapshot_attempts <= 5 {
                let qt = self.qt_thread();
                crate::runtime::after(Duration::from_millis(200), move || {
                    let queued = qt.queue(move |controller| {
                        if controller.pending_created_session == expected {
                            controller.request_snapshot();
                        }
                    });
                    if queued.is_err() {
                        eprintln!("AppController: dropped a created-agent retry; the controller is gone");
                    }
                });
            } else {
                self.set_error("Your new agent is still loading. Press Enter to retry.");
            }
            return true;
        }
        self.as_mut().rust_mut().pending_created_session.clear();
        self.as_mut().rust_mut().starting_contact.clear();
        self.as_mut().contact_launch_changed();
        self.finish_created(&expected);
        false
    }

    fn post_agent_setting(self: Pin<&mut Self>, session: &str, path: &str, body: Value) {
        if let Some(api) = self.api.as_ref() {
            api.post_json(&format!("agent-setting:{session}"), path, body, None);
        }
    }

    fn release_agent(self: Pin<&mut Self>, session: &QString) {
        let session = session.to_string();
        if session.is_empty() {
            return;
        }
        if let Some(api) = self.api.as_ref() {
            api.delete(&format!("agent-release:{session}"), &format!("/agents/{}", clarp_core::endpoint::percent_encode_segment(&session)));
        }
    }

    fn set_agent_heartbeat(self: Pin<&mut Self>, session: &QString, enabled: bool) {
        let session = session.to_string();
        self.post_agent_setting(&session, "/agent-heartbeat", json!({"session": session, "heartbeat_enabled": enabled}));
    }

    fn set_agent_dreaming(self: Pin<&mut Self>, session: &QString, enabled: bool) {
        let session = session.to_string();
        self.post_agent_setting(&session, "/agent-dreaming", json!({"session": session, "dreaming_enabled": enabled}));
    }

    fn set_agent_push_muted(self: Pin<&mut Self>, session: &QString, muted: bool) {
        let session = session.to_string();
        self.post_agent_setting(&session, "/agent-mute", json!({"session": session, "muted": muted}));
    }

    /// Display name only: session and agent ids are stable, so the chat and
    /// every pairing survive a rename.
    fn rename_agent(self: Pin<&mut Self>, session: &QString, name: &QString) {
        let (session, name) = (session.to_string(), name.to_string().trim().to_owned());
        if session.is_empty() || name.is_empty() {
            return;
        }
        self.post_agent_setting(&session, "/agent-rename", json!({"session": session, "name": name}));
    }

    fn archive_agent(self: Pin<&mut Self>, session: &QString) {
        self.set_agent_archived(session, true);
    }

    fn set_agent_archived(self: Pin<&mut Self>, session: &QString, archived: bool) {
        let session = session.to_string();
        self.post_agent_setting(&session, "/agent-archive", json!({"session": session, "archived": archived}));
    }

    fn set_schedule_enabled(self: Pin<&mut Self>, schedule_id: &QString, enabled: bool) {
        let id = schedule_id.to_string().trim().to_owned();
        if id.is_empty() {
            return;
        }
        if let Some(api) = self.api.as_ref() {
            api.post_json("schedule-toggle:", "/agent-schedules/toggle", json!({"schedule_id": id, "enabled": enabled}), None);
        }
    }

    fn set_agent_llm(self: Pin<&mut Self>, session: &QString, model: &QString, effort: &QString) {
        let session = session.to_string();
        self.post_agent_setting(&session, "/agent-llm", json!({"session": session, "model": model.to_string(), "effort": effort.to_string()}));
    }

    fn compact_session(self: Pin<&mut Self>, session: &QString) {
        let session = session.to_string();
        self.post_agent_setting(&session, "/compact", json!({"session": session}));
    }

    fn set_agent_mcp(self: Pin<&mut Self>, session: &QString, servers: &cxx_qt_lib::QJsonArray) {
        let session = session.to_string();
        let servers: Vec<Value> = servers.iter().map(|v| Value::from(v.to_string().to_string())).collect();
        self.post_agent_setting(&session, "/agent-mcp", json!({"session": session, "mcp_servers": servers}));
    }

    fn models_for_backend(&self, backend: &QString) -> cxx_qt_lib::QJsonArray {
        crate::qjson::to_qjson_array(&clarp_core::catalog::models_for_backend(&self.model_catalog, &backend.to_string()))
    }

    fn efforts_for_model(&self, backend: &QString, model: &QString) -> cxx_qt_lib::QJsonArray {
        crate::qjson::to_qjson_array(&clarp_core::catalog::efforts_for_model(&self.model_catalog, &backend.to_string(), &model.to_string()))
    }

    fn default_effort_for_model(&self, backend: &QString, model: &QString) -> QString {
        qs(&clarp_core::catalog::default_effort_for_model(&self.model_catalog, &backend.to_string(), &model.to_string()))
    }

    fn backend_supports_resume(&self, backend: &QString) -> bool {
        clarp_core::catalog::backend_supports(&self.model_catalog, &backend.to_string(), "supports_resume")
    }

    fn backend_supports_fork(&self, backend: &QString) -> bool {
        clarp_core::catalog::backend_supports(&self.model_catalog, &backend.to_string(), "supports_fork")
    }

    // ---- updates and background jobs ----------------------------------------

    fn load_updates(mut self: Pin<&mut Self>) {
        let generation = {
            let mut rust = self.as_mut().rust_mut();
            rust.updates_generation += 1;
            rust.updates_error.clear();
            rust.updates_pending = 3;
            rust.updates_generation
        };
        self.as_mut().updates_changed();
        if let Some(api) = self.api.as_ref() {
            api.get(&format!("updates:{generation}:attention"), "/attention", &[]);
            api.get(&format!("updates:{generation}:jobs"), "/background-jobs", &[]);
            api.get(&format!("updates:{generation}:artifacts"), "/artifacts", &[("limit", "50"), ("order", "updated")]);
        }
    }

    fn finish_update_request(mut self: Pin<&mut Self>, generation: u64) {
        if generation != self.updates_generation {
            return;
        }
        let pending = (self.updates_pending - 1).max(0);
        self.as_mut().rust_mut().updates_pending = pending;
        self.updates_changed();
    }

    /// The tracker changed: roster counts follow, and views re-read processes.
    fn jobs_changed(mut self: Pin<&mut Self>) {
        let counts = self.jobs.loaded().then(|| self.jobs.counts_by_agent());
        if let Some(agents) = self.as_mut().agents_mut() {
            match counts {
                Some(counts) => agents.mutate(|core| core.apply_live_job_counts(counts)),
                None => agents.mutate(|core| core.clear_live_job_counts()),
            }
        }
        self.as_mut().rust_mut().process_revision += 1;
        self.process_revision_changed();
    }

    fn agent_processes(&self, session: &QString) -> cxx_qt_lib::QJsonObject {
        let now = chrono::Utc::now().timestamp_millis();
        let described = self
            .roster()
            .and_then(|roster| clarp_core::roster::describe_agent_processes(roster, &self.jobs, &session.to_string(), now));
        match described {
            Some(object) => crate::qjson::to_qjson(&Value::Object(object)).to_object(),
            None => cxx_qt_lib::QJsonObject::default(),
        }
    }

    fn resolve_decision(mut self: Pin<&mut Self>, decision_id: &QString, choice: &QString, revision: i32) {
        let id = decision_id.to_string();
        let choice = match choice.to_string().as_str() {
            "yes" | "accepted" => "accepted",
            "no" | "rejected" => "rejected",
            _ => return,
        };
        let key = format!("decision:{id}");
        if id.is_empty() || self.pending_update_actions.contains(&key) {
            return;
        }
        self.as_mut().rust_mut().pending_update_actions.insert(key);
        self.as_mut().updates_changed();
        let path = format!("/decisions/{}/resolve", clarp_core::endpoint::percent_encode_segment(&id));
        if let Some(api) = self.api.as_ref() {
            api.post_json(&format!("update-action:decision:{id}"), &path,
                          json!({"choice": choice, "expected_revision": revision}), None);
        }
    }

    fn cancel_background_job(mut self: Pin<&mut Self>, job_id: &QString) {
        let id = job_id.to_string();
        let key = format!("job:{id}");
        if id.is_empty() || self.pending_update_actions.contains(&key) {
            return;
        }
        self.as_mut().rust_mut().pending_update_actions.insert(key);
        self.as_mut().updates_changed();
        let path = format!("/background-jobs/{}", clarp_core::endpoint::percent_encode_segment(&id));
        if let Some(api) = self.api.as_ref() {
            api.delete(&format!("update-action:job:{id}"), &path);
        }
    }

    fn update_action_pending(&self, kind: &QString, id: &QString) -> bool {
        self.pending_update_actions.contains(&format!("{kind}:{id}"))
    }

    fn background_job_progress_text(&self, job: &QString) -> f64 {
        match serde_json::from_str::<Value>(&job.to_string()) {
            Ok(Value::Object(job)) => clarp_core::jobs::job_progress(&job),
            _ => -1.0,
        }
    }

    // ---- network results ---------------------------------------------------

    fn handle_reply(mut self: Pin<&mut Self>, reply: ApiReply) {
        let narrator_tag = match &reply {
            ApiReply::Json { tag, .. } | ApiReply::Failed { tag, .. } => {
                self.narrator.as_ref().is_some_and(|n| n.owns_tag(tag))
            }
            ApiReply::Bytes { .. } => false,
        };
        if narrator_tag {
            if let Some(narrator) = self.as_mut().narrator_mut() {
                match &reply {
                    ApiReply::Json { tag, object } => narrator.handle_reply(tag, object),
                    ApiReply::Failed { tag, status, .. } => narrator.handle_failure(tag, *status),
                    ApiReply::Bytes { .. } => {}
                }
            }
            return;
        }
        match reply {
            ApiReply::Json { tag, object } => self.handle_json(&tag, &object),
            ApiReply::Failed { tag, message, status } => self.handle_failure(&tag, &message, status),
            ApiReply::Bytes { tag, bytes, content_type } => self.handle_bytes(tag, bytes, content_type),
        }
    }

    fn handle_json(mut self: Pin<&mut Self>, tag: &str, object: &Object) {
        if tag.starts_with("composer-upload:") {
            self.finish_upload(tag, Some(object));
            return;
        }
        if tag.starts_with("media-list:") {
            self.handle_media_list(tag, object);
            return;
        }
        if tag.starts_with("recoverable:") {
            let events: Vec<Object> =
                object.get("events").and_then(Value::as_array).into_iter().flatten().filter_map(|e| e.as_object().cloned()).collect();
            if let Some(mut audio) = self.as_mut().audio_mut() {
                for event in events {
                    audio.as_mut().enqueue_clip(event);
                }
            }
            return;
        }
        if self.as_mut().handle_launch_json(tag, object) {
            return;
        }
        if self.as_mut().handle_team_or_queue_json(tag, object) || self.as_mut().handle_profile_json(tag, object) {
            return;
        }
        if tag == "pairing" {
            self.handle_pairing(object);
            return;
        }
        if tag == "server-info" {
            let name = object.get("name").and_then(Value::as_str).unwrap_or("Clarp").to_owned();
            self.as_mut().rust_mut().server_name = name;
            self.as_mut().rust_mut().server_version = json::string(object, "clarp_version");
            self.as_mut().server_info_changed();
            self.as_mut().apply_host_launch_directory_default(&json::string(object, "default_cwd"));
            // A device token that worked is worth keeping; plain tokens from
            // config.toml or the environment are not written to the keyring.
            if self.token.starts_with("cld_") {
                let token = self.token.clone();
                self.as_mut().store_credential(token);
            }
            self.as_mut().set_connecting(false);
            self.as_mut().request_snapshot();
            if let Some(api) = self.api.as_ref() {
                api.get("model-catalog", "/agent-model-options", &[]);
            }
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
        } else if tag == "model-catalog" {
            self.as_mut().rust_mut().model_catalog = object.clone();
            self.model_catalog_changed();
        } else if tag == "contact-create" || tag.starts_with("agent-create:") {
            self.created_agent(tag, object);
        } else if let Some(session) = tag.strip_prefix("agent-release:").or_else(|| tag.strip_prefix("agent-setting:")) {
            let session = session.to_owned();
            self.as_mut().request_snapshot();
            self.agent_mutation_succeeded(qs(&session));
        } else if tag == "schedule-toggle:" {
            self.request_snapshot();
        } else if let Some(rest) = tag.strip_prefix("updates:") {
            let (generation, kind) = rest.split_once(':').unwrap_or((rest, ""));
            let Ok(generation) = generation.parse::<u64>() else { return };
            if generation != self.updates_generation {
                return;
            }
            match kind {
                "attention" => self.as_mut().rust_mut().attention_items = json::array(object, "items"),
                "jobs" => {
                    self.as_mut().rust_mut().background_jobs = json::array(object, "jobs");
                    if self.as_mut().rust_mut().jobs.apply_list(object) {
                        self.as_mut().jobs_changed();
                    }
                }
                "artifacts" => self.as_mut().rust_mut().update_artifacts = json::array(object, "artifacts"),
                _ => {}
            }
            self.finish_update_request(generation);
        } else if let Some(action) = tag.strip_prefix("update-action:") {
            self.as_mut().rust_mut().pending_update_actions.remove(action);
            self.as_mut().updates_changed();
            self.load_updates();
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
        if tag == "contact-create" {
            // The Host's own message, without an HTTP suffix: it explains
            // what to change (a forbidden folder, an occupied contact).
            self.as_mut().rust_mut().starting_backend.clear();
            self.as_mut().rust_mut().starting_contact.clear();
            if message == "contact_pool_empty" {
                self.as_mut().launch_pool_empty();
            } else {
                self.as_mut().set_error(message);
            }
            self.contact_launch_changed();
            return;
        }
        if self.as_mut().handle_launch_failure(tag) {
            return;
        }
        if tag.starts_with("media-list:") || tag.starts_with("media-content:") {
            self.as_mut().rust_mut().media_list_requests.remove(tag);
            self.as_mut().rust_mut().media_content_requests.remove(tag);
            eprintln!("AppController: {tag} failed: {message} (HTTP {status})");
            return;
        }
        if tag.starts_with("avatar:") || tag.starts_with("contact-avatar:") {
            eprintln!("AppController: {tag} failed: {message} (HTTP {status})");
            self.portrait_failed(tag);
            return;
        }
        let detail = if status > 0 { format!("{message} (HTTP {status})") } else { message.to_owned() };
        if tag.starts_with("composer-upload:") {
            self.as_mut().finish_upload(tag, None);
            self.set_error(&detail);
            return;
        }
        if self.as_mut().handle_team_or_queue_failure(tag, &detail) || self.as_mut().handle_profile_failure(tag, &detail) {
            return;
        }
        if let Some(rest) = tag.strip_prefix("updates:") {
            let generation = rest.split(':').next().and_then(|g| g.parse::<u64>().ok());
            if generation == Some(self.updates_generation) {
                self.as_mut().rust_mut().updates_error = detail;
                self.finish_update_request(generation.unwrap_or_default());
            }
            return;
        }
        if let Some(action) = tag.strip_prefix("update-action:") {
            self.as_mut().rust_mut().pending_update_actions.remove(action);
            self.as_mut().rust_mut().updates_error = detail;
            self.updates_changed();
            return;
        }
        self.as_mut().set_error(&detail);
        // No HTTP status: the Host was unreachable. Reconnecting answers it.
        self.as_mut().rust_mut().error_is_transport = status == 0;
        // Unlike the C++ client, a failed pairing does not leave the page
        // stuck in "pairing".
        if tag == "server-info" || tag == "pairing" {
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
                // After a deliberate stop (reconnect, pairing, forgetting the
                // token) the caller already set the state; the queued
                // disconnect must not turn it into "reconnecting".
                let running = self.sse.as_ref().is_some_and(SseClient::running);
                if connected || running {
                    self.as_mut().set_connection_state(if connected { "live" } else { "reconnecting" });
                }
                if connected {
                    // "Connection refused" from the outage must not outlive it.
                    if self.error_is_transport {
                        self.as_mut().set_error("");
                    }
                    self.as_mut().request_snapshot();
                    // Attention, jobs and artifacts follow the sidebar instead
                    // of competing with it for the Host on a cold start.
                    let qt = self.qt_thread();
                    crate::runtime::after(Duration::from_millis(1500), move || {
                        let queued = qt.queue(|controller| {
                            if controller.connected {
                                controller.load_updates();
                            }
                        });
                        if queued.is_err() {
                            eprintln!("AppController: dropped the first updates load; the controller is gone");
                        }
                    });
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
            "audio" => {
                if let Some(audio) = self.as_mut().audio_mut() {
                    audio.enqueue_clip(event.clone());
                }
            }
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
                if session == self.turn_queue_session {
                    self.load_queue_for(&session);
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
            "background-job-updated" => {
                // Apply the event's job at once so rows and the header change
                // without waiting for the refetch; the list stays authoritative.
                if self.jobs.loaded() && self.as_mut().rust_mut().jobs.apply_event(event) {
                    self.as_mut().jobs_changed();
                }
                self.load_updates();
            }
            "artifact-updated" | "attention-updated" => self.load_updates(),
            // Audio and teams arrive with later slices; unknown types are
            // ignored by contract (additive-only).
            _ => {}
        }
    }
}

/// Keep the thread type nameable for queued closures.
#[allow(dead_code)]
type ControllerThread = CxxQtThread<AppController>;
