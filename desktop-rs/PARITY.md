# Rust desktop parity checklist

The C++ test list covers declared and inline-defined test slots plus standalone test programs.

Status: `todo` / `wip` / `ported` (Rust code exists) / `verified` (Rust test or headless check named).
The C++ `desktop/` tree stays the reference until every row is `verified`.

## Modules

| C++ source | Lines | Rust target | Status |
|---|---|---|---|
| `src/app/AppController` | 3771 | `app/src/bridge/controller.rs` | wip: connect/snapshot/selection/log sync/send/stop/SSE, drafts/focus, updates/jobs, launch/lifecycle/catalog, narrator, preferences, teams and turn queue, profile/prompt history, settings status/TTS, voices, orchestrator, composer attachments (upload, shared filesystem, send), past sessions/resume, launch directories, directory suggestions/favorites, contact assignment, pairing and keyring credentials, avatars (portrait fetch/round/disk cache, failures not retried) and chat media (list, image file cache, clarp-media links), links (web/mail only, Hyprland workspace helper, local reports), clipboard (arboard), agent files and native CLI terminal, artifacts and reports (Markdown as is, HTML sanitised), on-demand tool details, voice preview and pair rooms (listed, unread by revision, read-only selection, member updates) verified in `app/tests/qml/controller*_probe.qml` against `fake_host.py`; directories, clipboard image paste, audio, styled markdown pending |
| `src/app/CredentialStore` | 245 | `net/src/credentials.rs` (zbus) | verified (`net/tests/credentials.rs` via `net/tests/run-keyring-tests.sh`: throwaway gnome-keyring and keyring-less private buses; `app/tests/qml/controller_credentials_probe.qml` pairs, reconnects from the keyring and forgets). Deviation: a failed `/pairing/exchange` returns the page to offline instead of staying in "pairing" |
| `src/app/InstanceServer` | 199 | `core/src/instance.rs` + `app/src/bridge/instance_server.rs` | verified: same socket key (build path+mtime, instance, Host, token, config, display, renderer) and wire format; runs alone for help/version/versions, screenshots, restore, probes, `CLARP_SEPARATE_PROCESS`; 0700 socket, removed on quit; a second launch opens its window in the running process (`app/tests/run-desktop-services.sh`) |
| `src/app/MarkdownStyle` | 250 | `core/src/markdown_style.rs` (pulldown-cmark) + `styledMarkdownHtml` in the controller | verified (`core/tests/markdown_style.rs`, `app/tests/qml/markdown_style_probe.qml`, screenshot of a styled reply). Deviation: rendered directly to Qt rich-text HTML instead of restyling a QTextDocument; raw HTML in a message stays text; `styleMarkdown(textDocument)` is not ported (no QML caller) |
| `src/app/PreviewVersions` | 147 | `core/src/preview.rs` + `app/src/bridge/preview_versions.rs` | verified (core: `core/tests/preview.rs`); the bridge runs the helper off-thread and quits through `QCoreApplication::quit` bound with cxx `#[Self]` |
| `src/app/StallMonitor` | 247 | `core/src/diagnostics.rs` + `app/src/stall_monitor.rs` + `app/src/bridge/diagnostics.rs` | verified: heartbeat plus dispatcher `awake()` wake time, signal-captured GUI stack (named from debug info, C++ demangled), memory-mark captures every 512 MB, same env knobs and log path; `app/tests/qml/diagnostics_probe.qml` (A/B: fails without wake times) |
| `src/app/TimeFormat` | 138 | `core/src/time_format.rs` | verified (`conversation.rs::stamps_*`, `compact_*`); en_US formats fixed, QLocale-driven locale pending |
| `src/app/ToolNarrator` | 802 | `core/src/narrator.rs` + `app/src/bridge/tool_narrator.rs` | verified for the shared-Host path (`core/tests/narrator.rs`, `app/tests/qml/narrator_probe.qml`); the local `codex exec` fallback and script-context evidence are not ported (the app always uses the Host) |
| `src/app/TranscriptCache` | 123 | `core/src/transcript_cache.rs` + controller restore/250 ms save | verified (`core/tests/transcript_cache.rs`; `app/tests/qml/controller_transcript_cache_probe.qml` opens a chat from the cache with the Host unreachable, restore mutation caught) |
| `src/app/WorkspaceContext` | 86 | `core/src/workspace.rs` | verified (`core/tests/workspace.rs`, real Git worktree) |
| `src/main` | 1164 | `app/src/main.rs` + `app/build.rs` | wip: Clarp.Desktop module built from `desktop/CMakeLists.txt` (QML + resources via `app/qml`, `app/resources` symlinks; QTP0004 subdirectory qmldirs generated), Basic style, software renderer, own `ClarpRust` settings namespace, Main loads and runs against the fake Host offscreen with no QML warnings; Main loaded inside its module with `launchOnStartup`/`sidebarVisible`; screenshot runs (`CLARP_SCREENSHOT_PATH`/`_SIZE`/`_SELECT_SESSION`/`_DELAY_MS`, `QQuickWindow::grabWindow` via `app/src/bridge/window_capture.rs`) checked by `app/tests/run-screenshot.sh`; launch options (`core/src/launch.rs`, `core/tests/launch.rs`: --anonymous/--contact/--new-agent/--no-new-agent/--backend/--cwd/--model/--effort/--preview-versions validation, launch-on-startup, auto-start launch mode, empty startup; Main's `openLaunchAgent` called from the root document), preview relaunch restore; instance forwarding (see InstanceServer); windows are torn down before exit so drafts save; screenshot scenarios pending |
| `src/media/AudioController` | 859 | `core/src/audio.rs` + `app/src/bridge/audio_controller.rs` + `app/src/audio_output.rs` | wip: player state machine and dictation bookkeeping verified in `core/tests/audio.rs`; `app.audio` plays announced and recoverable clips with acks, silence and mute through the test sink (`controller_audio_probe.qml`) and fails fast without a backend (`controller_audio_nobackend_probe.qml`); audible output via rodio on the default device (mp3/mp4-aac/wav/flac/ogg + raw PCM, 1.2x speed without pitch compensation), decoders verified without a device in `controller_audio_decode_probe.qml`; audible playback not machine-verified (probes never open the speakers); microphone capture via rodio (16 kHz mono preferred, s16 WAV) with dictation delivered to the recording's chat in `controller_dictation_probe.qml` (fixture input) and too-short recordings dropped in `controller_dictation_short_probe.qml`; the real microphone is not machine-verified (probes never open it), coordinated across windows |
| `src/media/AudioCoordinator` | 363 | `core/src/audio_journal.rs` + `app/src/audio_coordinator.rs` (zbus) | verified: journal (`core/tests/audio_journal.rs`: once-only offers, takeover never replays a started clip, invalid journal fails closed, bounded completed, C++-compatible bus name/state); election over the session bus with the C++ path, interface and method names in `app/tests/qml/controller_shared_playback_probe.qml` (one download, never replayed, mute across windows; election bypass mutation caught) |
| `src/media/PortraitImage` | 59 | `core/src/media.rs` (`image` crate) | verified (`core/tests/media.rs::a_portrait_is_a_192px_circle`) |
| `src/media/RecordingSession` | 56 | `core/src/audio.rs` (`RecordingLease`, flock) | verified (`core/tests/audio.rs::the_microphone_is_exclusive_across_windows`) |
| `src/media/WavEncoder` | 58 | `core/src/audio.rs` | verified (`core/tests/audio.rs::wav_encoding_produces_a_valid_pcm_header`) |
| `src/models/AgentFilterModel` | 368 | `core/src/sidebar.rs` + `app/src/bridge/agent_filter_model.rs` | verified (`core/tests/sidebar.rs`, `app/tests/qml/agent_filter_model_probe.qml`); uses `invalidateRowsFilter()` (deprecated in 6.13) because cxx cannot name `QFlags<Direction>` for `endFilterChange` |
| `src/models/AgentListModel` | 708 | `core/src/roster.rs` + `app/src/bridge/agent_list_model.rs` | verified (`core/tests/roster.rs`, `app/tests/qml/agent_list_model_probe.qml`); name order is case-insensitive, not ICU collation |
| `src/models/BackgroundJobTracker` | 159 | `core/src/jobs.rs` | verified (`roster.rs::background_job_tracker_keeps_only_active_jobs`) |
| `src/models/ContactListModel` | 139 | `core/src/directory.rs` + `app/src/bridge/directory_models.rs` | verified (`core/tests/directory.rs`, `directory_models_probe.qml`) |
| `src/models/ConversationModel` | 830 | `core/src/conversation.rs` + `app/src/bridge/conversation_model.rs` | verified (`core/tests/conversation.rs`, `app/tests/qml/conversation_model_probe.qml`) |
| `src/models/ConversationPresentationModel` | 437 | `core/src/presentation.rs` + `app/src/bridge/presentation_model.rs` | verified (`core/tests/presentation.rs`, `presentation_model_probe.qml`, `narrator_probe.qml` for explanation runs); a row-diffing list model rather than a proxy |
| `src/models/PaneTreeModel` | 1041 | `core/src/panes.rs` + `app/src/bridge/pane_tree_model.rs` | verified (`core/tests/panes.rs`, `app/tests/qml/pane_tree_model_probe.qml`); layouts persist to `~/.config/MaxTeaBag/ClarpRust/workspaces.json` (JSON + lock file + per-window recovery files), not the C++ QSettings keys, and the legacy `workspace/paneTree` key is not migrated |
| `src/models/VoiceListModel` | 113 | `core/src/directory.rs` + `app/src/bridge/directory_models.rs` | verified (`core/tests/directory.rs`, `directory_models_probe.qml`) |
| `src/network/ApiClient` | 238 | `net/src/api.rs` + `core/src/endpoint.rs` | verified (`net/tests/clients.rs`, `core/tests/endpoint.rs`) |
| `src/network/SseClient` | 197 | `net/src/sse.rs` + `core/src/endpoint.rs` | verified (`net/tests/clients.rs::sse_*`) |
| `src/network/SseParser` | 91 | `core/src/sse.rs` | verified (`protocol.rs::sse_parser_*`) |
| `src/platform/DesktopIntegration` | 97 | `app/src/bridge/desktop_services.rs` (ksni StatusNotifierItem + org.freedesktop.Notifications over zbus) | verified by `app/tests/run-desktop-services.sh` (fake tray host and notification daemon on a private bus: registration, menu, mute from the tray, notifications for replies in closed chats, Show, Quit). Deviation: the tray waits for a tray host that starts later instead of giving up at launch; MPRIS pending |
| `src/platform/DesktopPresence` | 224 | `core/src/presence.rs` + `app/src/bridge/desktop_services.rs` | verified: rules in `core/tests/presence.rs` (the four C++ cases plus lease renewal and input expiry); the real window reports foreground activity to the Host with logind session state in `app/tests/run-desktop-services.sh`. Input is detected with `QEvent::isInputEvent` (cxx cannot name `QEvent::Type`), so hover counts as input |
| `src/platform/MprisIntegration` | 252 | `app/src/mpris.rs` + `app/src/bridge/desktop_services.rs` | verified: `org.mpris.MediaPlayer2.Clarp.instance<pid>` root and player interfaces, same properties and metadata, one combined PropertiesChanged per audio change, PlayPause/Play/Pause/Stop reach AudioController (across windows through the coordinator), Raise shows the window; driven over a private bus in `app/tests/run-desktop-services.sh` |
| `src/protocol/ProtocolTypes` | 799 | `core/src/{protocol,text,json}.rs` | verified (`core/tests/protocol.rs`) |
| `src/terminal/TerminalLaunch` | 36 | `core/src/links.rs` | verified (`core/tests/links.rs`; `app/tests/qml/controller_terminal_probe.qml`) |
| `src/views/TranscriptLayout` | 890 | `core/src/transcript_layout.rs` + `app/src/bridge/transcript_layout.rs` + `app/src/bridge/quick.rs` | verified: geometry in `core/tests/transcript_layout.rs`; the native QQuickItem (cxx-qt base QQuickItem, C++ creation semantics via QQmlComponent begin/complete + indexOfProperty + QQmlPropertyMap, signature-forwarded model/Flickable signals) runs the C++ test scene in `app/tests/qml/transcript_layout_probe.qml` |
| `src/views/TranscriptRows` | 310 | `core/src/transcript_rows.rs` + `app/src/bridge/transcript_rows.rs` | verified (`core/tests/transcript_rows.rs`; list model over any source via signature-forwarded signals in `app/tests/qml/transcript_rows_probe.qml`: finishing splits without reset, rows map around split messages) |
| `src/app/AvatarMotionClock.h` (header-only) | 224 | `core/src/avatar_motion.rs` + `app/src/bridge/avatar_motion.rs` | verified (`core/tests/avatar_motion.rs`; the real Main.qml binds `avatarMotion` without errors); pending: window-visibility watching (`watchMotionWindow`) with the platform step, and observers are released by QML `onDestruction` rather than a `destroyed` hook |
| `src/app/DesktopPalette.h` (header-only) | 40 | `desktop-rs/src/app/` | todo |
| `src/app/KeyboardSmokeCheck.h` (header-only) | 330 | `desktop-rs/src/app/` | todo |
| `src/app/LocalReport.h` (header-only) | 40 | `core/src/links.rs::local_report_path` | verified (`core/tests/links.rs::only_readable_non_executable_reports_open`); content is checked by magic bytes/UTF-8 instead of the freedesktop MIME database |
| `src/app/MemoryDiagnostics.h` (header-only) | 71 | `core/src/diagnostics.rs` + `app/src/bridge/diagnostics.rs` | verified: minute `memory {...}` line (RSS split, window items/text items, controller `memoryCounters`, CPU, stalls); `app/tests/run-desktop-services.sh` checks it on the real window |
| `src/app/PairSidebarSmokeCheck.h` (header-only) | 62 | `desktop-rs/src/app/` | todo |
| `src/app/PreviewRelaunch.h` (header-only) | 15 | `core/src/preview.rs` | verified (`core/tests/preview.rs::relaunch_preserves_host_and_session`) |
| `src/app/ReadingTheme.h` (header-only) | 282 | `core/src/reading_theme.rs` + generated `reading_themes.json` | verified (`core/tests/reading_theme.rs`); fonts resolved through fontconfig (`app/src/fonts.rs`) |
| `src/app/ReadyReplySmokeCheck.h` (header-only) | 85 | `desktop-rs/src/app/` | todo |
| `src/app/StartupTrace.h` (header-only) | 52 | `desktop-rs/src/app/` | todo |
| `src/app/TranscriptScrollSmokeCheck.h` (header-only) | 440 | `desktop-rs/src/app/` | todo |
| `src/app/VoiceViewportSmokeCheck.h` (header-only) | 153 | `desktop-rs/src/app/` | todo |
| `src/models/TreeOrder.h` (header-only) | 79 | `core/src/tree.rs` | verified (`roster.rs::tree_order_matches_the_team_walk`) |

## Behaviors (REWRITE_PLAN.md: Existing behavior to preserve)

| Behavior | Rust test / check | Status |
|---|---|---|
| Discover and render the live agent roster from `/agents/snapshot`. | `app/tests/qml/controller_probe.qml` (fake Host) + `core/tests/roster.rs` | verified |
| Select and follow an agent, including server-wide focus updates. | `app/tests/qml/controller_probe.qml` (fake Host) (select, POST /select, pane follows) + `roster.rs::focus_events_mark_exactly_one_row` | verified |
| Load tail history, paginate older messages, and apply revision deltas. | `app/tests/qml/controller_probe.qml` (fake Host) (tail, SSE-driven delta) + `conversation.rs` | wip: older-page request not yet probed end to end |
| Replace a conversation when its `conversation_id` changes or the server returns `replace_required`. | `conversation.rs::conversation_change_requests_replacement` + controller replace-on-replacementRequired | wip: controller path not yet probed end to end |
| Merge growing messages by id and revision without duplicating turns. | `conversation.rs::growing_reply_rejects_stale_revision`, `contract_fixtures.rs` | verified |
| Open one SSE stream, resume with `Last-Event-ID`, reconnect on silence or failure, and ignore unknown additive fields/events. | | wip: client verified in `net/tests/clients.rs` (resume, watchdog, backoff, stop) and `contract_fixtures.rs` (unknown events/fields); app wiring pending |
| Send idempotent messages with an optimistic `u-<client_msg_id>` row and keep delivery pending until that id appears in `/log`. | `app/tests/qml/controller_probe.qml` (fake Host) (optimistic row, confirmed by u-<id>, unfiled send stays pending) | wip: 20 s delivery-timeout failure not exercised |
| Stop a running turn and represent queued, waiting, interrupted, and active states accurately. | | todo |
| Record microphone PCM, upload it to `/transcribe`, and carry the returned trace/transcription ids into `/send`. | `app/tests/qml/controller_dictation_probe.qml` (fixture input) + `core/tests/audio.rs` | verified with a fixture input; live microphone not machine-verified |
| Play announced clips, select sources by protocol precedence, and acknowledge queued/start/success/failure states. | `core/tests/audio.rs` + `app/tests/qml/controller_audio_probe.qml` (test sink) | wip: verified through the silent sink; audible output pending |
| Treat `user-notification` as the only unread/desktop-notification decision. | `app/tests/qml/controller_probe.qml` (fake Host) (no notification for the open chat) + `roster.rs` | wip: notification for a background chat not probed |
| Create/relaunch/fork/release agents and expose the desktop overview, voice-selection, and orchestrator settings workflows. | | todo |
| Preserve the desktop pane workspace, collapsible agent rail, keyboard-driven navigation, quick switcher, tool visibility, and scroll-to-latest behavior. | | todo |

## C++ test cases to port

| C++ test | Rust test | Status |
|---|---|---|
| `tst_native_core::spawnedLifecycleNeverBecomesTranscriptTool` | `core/tests/conversation.rs::spawned_lifecycle_never_becomes_transcript_tool` | verified |
| `tst_native_core::voiceErrorsStayInTheirSession` | | todo |
| `tst_native_core::sidebarPreviewIsPlainText` | `core/tests/protocol.rs::sidebar_preview_is_plain_text` | verified |
| `tst_native_core::clipboardImageBecomesAttachmentWithoutSending` | | todo |
| `tst_native_core::relaunchPreservesHostSessionAndDraft` | `core/tests/preview.rs::relaunch_preserves_host_and_session` + `app/tests/qml/controller_restore_probe.qml` | verified: relaunch environment and arguments, restored chat before and after the roster; drafts are durable per Host and chat (`controller_lifecycle_probe.qml`) |
| `tst_native_core::previewRestartCapturesContextAndRejectsBusy` | `core/tests/preview.rs::preview_restart_captures_context_and_rejects_busy` | verified |
| `tst_native_core::restoredSessionDoesNotFallBackToAnotherAgent` | `app/tests/qml/controller_restore_missing_probe.qml` (+ `controller_restore_probe.qml`) | verified |
| `tst_native_core::localReportsRequireOriginAndSafeReadableFiles` | | todo |
| `tst_native_core::leadingDayTracksVisibleHistory` | `core/tests/presentation.rs::leading_day_tracks_visible_history` | verified (core) |
| `tst_native_core::oldActivityGroupsAreLazyAndVisitScoped` | `core/tests/presentation.rs::old_activity_groups_are_lazy_and_visit_scoped` | verified (core) |
| `tst_native_core::consecutiveExplanationsCollapseWithoutChangingTranscript` | `core/tests/presentation.rs::consecutive_explanations_collapse_without_changing_transcript` | verified (core) |
| `tst_native_core::streamedTokenAnnouncesOnlyItsRows` | | todo |
| `tst_native_core::spokenMarkupLeavesNoGapsInTheText` | `core/tests/protocol.rs::spoken_markup_leaves_no_gaps_in_the_text` | verified |
| `tst_native_core::quickSwitcherPutsTheExactNameFirst` | | todo |
| `tst_native_core::unreachableHostErrorClearsWhenItIsBack` | `app/tests/qml/controller_lifecycle_probe.qml` (fake Host outage switch) | verified |
| `tst_native_core::attachedToolElapsedUsesAssistantBoundaryAndPreservesSender` | `core/tests/presentation.rs::attached_tool_elapsed_uses_assistant_boundary_and_preserves_sender` | verified (core) |
| `tst_native_core::logMergeRefreshesExplanationsOncePerBatch` | `core/tests/presentation.rs::log_merge_costs_one_lookup_per_tool` | wip: lookup bound verified; the Rust adapter diffs rows instead of one whole-transcript dataChanged, so that assertion is replaced by "no reset, per-row updates" |
| `tst_native_core::secondLaunchIsForwardedToTheRunningInstance` | `core/src/instance.rs::tests::a_second_launch_is_forwarded_to_the_listener` + `some_launches_always_run_alone` + `app/tests/run-desktop-services.sh` | verified |
| `tst_native_core::readyModePreservesActivityAndHidesOnlyProvisionalBody` | `core/tests/presentation.rs::ready_mode_preserves_activity_and_hides_only_provisional_body` | verified (core) |
| `tst_native_core::idleContactStartsFreshWithSavedDefaults` | `app/tests/qml/controller_launch_probe.qml` | verified |
| `tst_native_core::idleContactUsesDialogLaunchValues` | `app/tests/qml/controller_launch_probe.qml` | verified |
| `tst_native_core::hostDefaultDirectoryReplacesHomePlaceholder` | `app/tests/qml/controller_launch_probe.qml` | verified |
| `tst_native_core::contactCreateShowsHostMessageWithoutHttpSuffix` | `app/tests/qml/controller_launch_probe.qml` | verified |
| `tst_native_core::newAgentWaitsForOwnRosterAndRejectsLateSnapshots` | `app/tests/qml/controller_launch_probe.qml` (session-only path, retry without a second create) | wip: a held late snapshot released after creation is not simulated |
| `tst_native_core::fastLaunchOpensWithoutWaitingForFleet` | | todo |
| `tst_native_core::resumeLaunchOpensExactSessionWithoutFleet` | `app/tests/qml/controller_paths_probe.qml` + `app/tests/qml/controller_launch_mode_probe.qml` | verified: resume request shape; launch mode loads no fleet, suppresses other agents' speech errors, shows only the launched agent, resumes the fleet about 500 ms after creation |
| `tst_native_core::launchPoolCarriesBackendModelAndHandlesEmpty` | `app/tests/qml/controller_launch_probe.qml` | wip: pool request shape and empty-pool signal verified; model/effort on a pool start not asserted |
| `tst_native_core::redesignedRosterFiltersWithoutMutatingSource` | `core/tests/sidebar.rs::redesigned_roster_filters_without_mutating_source` + `agent_filter_model_probe.qml` | verified |
| `tst_native_core::agentRowParsesBackgroundAndHelperFieldsSafely` | `protocol.rs::agent_row_parses_background_and_helper_fields_safely` + `conversation.rs::compact_durations_read_like_the_cpp_client` | verified |
| `tst_native_core::backgroundJobTrackerKeepsOnlyActiveJobs` | `core/tests/roster.rs::background_job_tracker_keeps_only_active_jobs` | verified |
| `tst_native_core::rosterPrefersLiveJobCountsAndCountsHelpers` | `core/tests/roster.rs::roster_prefers_live_job_counts_and_counts_helpers` | verified |
| `tst_native_core::treeOrderMatchesTheTeamWalk` | `core/tests/roster.rs::tree_order_matches_the_team_walk` | verified |
| `tst_native_core::sidebarNestsHelpersAndCollapsesFinishedOnes` | `core/tests/sidebar.rs::sidebar_nests_helpers_and_collapses_finished_ones` + `agent_filter_model_probe.qml` | verified |
| `tst_native_core::sidebarUpdatesNeverResetTheRows` | `core/tests/sidebar.rs::sidebar_order_survives_filter_round_trips` + probe `resets === 0` | verified |
| `tst_native_core::sidebarStatusUpdatesDoNotRebuildTree` | `agent_filter_model.rs::tree_changing` gates rebuilds | wip: no read-counting source test yet |
| `tst_native_core::subagentCellsDescribePhaseNameAndTask` | `protocol.rs::subagent_cells_describe_phase_name_and_task` + `conversation.rs::subagent_cells_are_annotated_on_the_way_out_only` | verified |
| `tst_native_core::controllerTracksJobsFromListAndEvents` | `app/tests/qml/controller_jobs_probe.qml` | verified |
| `tst_native_core::rosterLookupIsConsistentDuringStructuralSignals` | adapter `indexOfSession` reads the replayed mirror | wip: needs a probe that checks lookups inside rowsInserted/Removed/Moved handlers |
| `tst_native_core::circularPortraitsAreBoundedAndAntialiased` | | todo |
| `tst_native_core::agentTerminalLaunchesNativeCliThroughDefaultTerminal` | `app/tests/qml/controller_terminal_probe.qml` | verified (stub `xdg-terminal-exec` and CLIs on PATH; exact command per backend; refused without a shared filesystem) |
| `tst_native_core::sseParserHandlesChunksCommentsAndReplayIds` | `core/tests/protocol.rs::sse_parser_handles_chunks_comments_and_replay_ids` | verified |
| `tst_native_core::nextAttentionCyclesWaitingUnreadAndPending` | `core/tests/roster.rs::next_attention_cycles_waiting_unread_and_pending` | verified |
| `tst_native_core::readyPresentationRetainsCanonicalStreamAndRevealsFinal` | `core/tests/presentation.rs::ready_presentation_*` + `app/tests/qml/controller_lifecycle_probe.qml` (setting restored) | verified |
| `tst_native_core::sseCursorIsScopedToOneHost` | `core/tests/endpoint.rs::sse_cursor_is_scoped_to_one_host` | verified |
| `tst_native_core::snapshotFiltersArchivedAgentsAndPatchesEvents` | `core/tests/roster.rs::snapshot_filters_archived_agents_and_patches_events` | verified |
| `tst_native_core::agentSnapshotDiffsInPlaceAndRejectsStaleState` | `core/tests/roster.rs::agent_snapshot_diffs_in_place_and_rejects_stale_state` | verified |
| `tst_native_core::backendQuotaNoticeNamesReasonResetAndFallback` | `core/tests/protocol.rs::backend_quota_notice_names_reason_reset_and_fallback` | verified |
| `tst_native_core::tailThenDeltaMatchesGoldenFixture` | `core/tests/conversation.rs::tail_then_delta_matches_golden_fixture` | verified |
| `tst_native_core::streamingRowsUpdateInPlaceAndRetireWhenFinalized` | `core/tests/conversation.rs::streaming_rows_update_in_place_and_retire_when_finalized` | verified |
| `tst_native_core::activityRowsUpdateInPlaceBySemanticIdentity` | `core/tests/conversation.rs::activity_rows_update_in_place_by_semantic_identity` | verified |
| `tst_native_core::olderHistoryPrependsWithoutReorderingTheTail` | `core/tests/conversation.rs::older_history_prepends_without_reordering_the_tail` | verified |
| `tst_native_core::growingReplyRejectsStaleRevision` | `core/tests/conversation.rs::growing_reply_rejects_stale_revision` | verified |
| `tst_native_core::optimisticDeliveryStaysVisibleUntilConfirmed` | `core/tests/conversation.rs::optimistic_delivery_stays_visible_until_confirmed` | verified |
| `tst_native_core::emptyStartupWaitsForExplicitChoiceAndRetryTargetsLatestFailure` | `app/tests/qml/empty_startup_probe.qml` | verified |
| `tst_native_core::conversationChangeRequestsReplacement` | `core/tests/conversation.rs::conversation_change_requests_replacement` | verified |
| `tst_native_core::clipSourcePrecedenceMatchesContract` | `core/tests/protocol.rs::clip_source_precedence_matches_contract` | verified |
| `tst_native_core::wavEncodingProducesAValidPcmHeader` | `core/tests/audio.rs::wav_encoding_produces_a_valid_pcm_header` | verified |
| `tst_native_core::paneTreeSplitsClosesNavigatesAndZooms` | `core/tests/panes.rs::pane_tree_splits_closes_navigates_and_zooms` | verified |
| `tst_native_core::paneWorkspacePersistenceIsAsyncAndConflictSafe` | `core/tests/panes.rs::pane_workspace_persistence_is_conflict_safe` + `a_queued_layout_replaces_*` + `pane_tree_model_probe.qml` (worker-thread writes) | verified |
| `tst_native_core::apiClientRejectsCrossOriginAuthenticatedMedia` | `net/tests/clients.rs::api_client_rejects_cross_origin_authenticated_media` | verified |
| `tst_native_core::apiClientDropsRepliesFromPreviousEndpointGeneration` | `net/tests/clients.rs::api_client_drops_replies_from_previous_endpoint_generation` | verified |
| `tst_native_core::paneDraftAndFocusSurviveLayoutStateChanges` | `app/tests/qml/controller_lifecycle_probe.qml` | verified |
| `tst_native_core::paneActivationAlwaysTargetsItsComposer` | `app/tests/qml/controller_lifecycle_probe.qml` (focus follows on the next event-loop turn, not synchronously) | verified |
| `tst_native_core::paneDraftIsDurableAndScopedToServerAndConversation` | `app/tests/qml/controller_lifecycle_probe.qml` + `app/tests/qml/controller_attachments_probe.qml` + `core/tests/settings.rs::draft_keys_are_scoped_to_host_and_session` + `core/tests/attachments.rs` | verified |
| `tst_native_core::transcriptCacheRestoresDurableRowsWithoutStaleRegression` | `core/tests/transcript_cache.rs::transcript_cache_restores_durable_rows_without_stale_regression` | verified |
| `tst_native_core::credentialStoreRoundTrip` | `net/tests/credentials.rs::token_round_trips_through_an_isolated_keyring` | verified |
| `tst_native_core::appControllerCompletesCoreProtocolFlow` | `app/tests/qml/controller_probe.qml` | wip: core flow verified; audio/clip parts pending (media step) |
| `tst_native_core::connectedControllerShutsDownWithoutLateSseCallbacks` | `app/tests/qml/controller_lifecycle_probe.qml` | verified |
| `tst_native_core::contactsExcludeActivePersonas` | `core/tests/directory.rs::contacts_exclude_active_personas` | verified |
| `tst_native_core::microphoneCanCaptureNativePcm` | | todo: needs a real input device; probes never open the microphone |
| `tst_native_core::backgroundTranscriptionsKeepTheirChatOwnership` | `core/tests/audio.rs::background_transcriptions_keep_their_chat_ownership` + `app/tests/qml/controller_dictation_probe.qml` | verified |
| `tst_native_core::clipFailsFastWithoutMediaBackend` | `app/tests/qml/controller_audio_nobackend_probe.qml` + `core/tests/audio.rs::clips_fail_fast_without_a_media_backend_and_report_it_once` | verified |
| `tst_native_core::narrationClipWithoutMediaBackendStaysBounded` | | todo |
| `tst_native_core::sharedPlaybackDoesNotDuplicateDownloads` | `app/tests/qml/controller_shared_playback_probe.qml` | wip: one download and cross-window mute verified; the microphone-held part is covered by `core/tests/audio.rs::recording_holds_the_queue_and_mute_or_silence_drains_it` rather than a second process holding the lease |
| `tst_native_core::markdownParagraphsBecomeVisibleDisplayBlocks` | `core/tests/protocol.rs::markdown_paragraphs_become_visible_display_blocks` | verified |
| `tst_native_core::hugeMarkdownBlocksAreNotRetainedInTheStyleCache` | | todo |
| `tst_native_core::agentReplyKeepsItsAuthorAndNamesTheAnsweredAgent` | | todo |
| `tst_native_core::pairConversationRoomsAreReadOnlyProjections` | `app/tests/qml/controller_pair_rooms_probe.qml` | verified: listed and unread, identical lists change nothing (mutation caught), read-only selection (no /select, no clips), member updates refresh, older Host without the route shows no rooms and no error |
| `tst_native_core::onlyWebAndMailLinksAreOpenable` | `core/tests/protocol.rs::only_web_and_mail_links_are_openable` | verified |
| `tst_native_core::toolOutputLinksAreAnchoredWithoutChangingTheText` | `core/tests/protocol.rs::tool_output_links_are_anchored_without_changing_the_text` | wip: QTextDocument round trip pending (app crate) |
| `tst_native_core::reportHtmlCannotFetchRemoteResources` | `core/tests/protocol.rs::report_html_cannot_fetch_remote_resources` | verified |
| `tst_native_core::reportHtmlKeepsStructureButNeverFetchesRemoteResources` | `core/tests/protocol.rs::report_html_keeps_structure_but_never_fetches_remote_resources` | wip: QTextDocument plain-text check pending (app crate) |
| `tst_native_core::reportForArtifactExposesSanitizedBody` | | todo |
| `tst_native_core::portedUrlsBecomeLinksWithoutChangingVisibleText` | `core/tests/protocol.rs::ported_urls_become_links_without_changing_visible_text` | wip: QTextDocument setMarkdown check pending (app crate) |
| `tst_tool_narrator::viewportOwnersShareAndReleaseQueuedActivity` | `core/tests/narrator.rs::viewport_owners_share_and_release_queued_activity` | verified |
| `tst_tool_narrator::sharedHostPollsWithoutStartingLocalCodex` | `core/tests/narrator.rs::shared_host_polls_until_ready_and_caches + narrator_probe.qml` | verified |
| `tst_tool_narrator::optInDeduplicatesBatchesAndPreservesCache` | `core/tests/narrator.rs::identical_requests_are_deduplicated_* + payload_redacts_and_never_sends_results` | wip: local-codex argv/diagnostics assertions not applicable (not ported) |
| `tst_tool_narrator::disableCancelsAndRejectsLateReplies` | `core/tests/narrator.rs::late_replies_after_disable_are_rejected` | verified |
| `tst_tool_narrator::failureFallsBackWithoutRetryStorm_data` | | todo |
| `tst_tool_narrator::failureFallsBackWithoutRetryStorm` | `core/tests/narrator.rs::failures_fall_back_without_a_retry_storm` | verified (Host failure, invalid reply, timeout, failed row) |
| `tst_tool_narrator::scriptContextIsOptInBoundedAndInvalidatesCache` | `core/tests/narrator.rs::—` | not ported: local-codex script evidence only |
| `tst_tool_narrator::memoizedLookupsFollowEveryFieldThatIsSent` | `core/tests/narrator.rs::memoized_lookups_follow_every_field_that_is_sent` | verified |
| `tst_tool_narrator::detailLevelsChangeInstructionsAndDiscardPreviousTranslations` | `core/tests/narrator.rs::detail_levels_discard_previous_translations` | verified (instructions are the Host's now) |
| `tst_avatar_motion::main` (standalone program) | `core/tests/avatar_motion.rs` | wip: ticking lifecycle (observe, reduced motion, foreground, reconcile) verified; window-visibility half pending with `watchMotionWindow` |
| `tst_avatar_render::main` (standalone program) | | todo |
| `tst_desktop_presence::eligibilityExpiresWithoutUserInput` | `core/tests/presence.rs::eligibility_expires_without_user_input` | verified (core) |
| `tst_desktop_presence::focusLockSleepAndPreferenceReleasePresence` | `core/tests/presence.rs::focus_lock_sleep_and_preference_release_presence` | verified (core) |
| `tst_desktop_presence::maintenanceActivityDoesNotDependOnPushPreference` | `core/tests/presence.rs::maintenance_activity_does_not_depend_on_push_preference` | verified (core) |
| `tst_desktop_presence::unknownSessionNeverSuppresses` | `core/tests/presence.rs::unknown_session_never_suppresses` | verified (core) |
| `tst_markdown_style::codeUsesMonoAndBackground` | `core/tests/markdown_style.rs::code_uses_mono_and_background` | verified (core) |
| `tst_markdown_style::headingsScaleWithTheBodyNotTheWeb` | `core/tests/markdown_style.rs::headings_scale_with_the_body_not_the_web` | verified (core) |
| `tst_markdown_style::mixedInlineFormatsInOneBlockStayInRange` | `core/tests/markdown_style.rs::large_messages_with_mixed_inline_formats_render` | verified (core) |
| `tst_markdown_style::optionsParseFromQml` | `core/tests/markdown_style.rs::options_parse_from_qml` | verified (core) |
| `tst_markdown_style::quotesAndTablesAreStyled` | `core/tests/markdown_style.rs::quotes_lists_and_tables_are_styled` | verified (core) |
| `tst_markdown_style::secondPassIsANoOp` | `app/tests/qml/markdown_style_probe.qml` (cached re-render) | verified: rendering is pure and cached; there is no in-place restyle pass to repeat |
| `tst_markdown_style::styledHtmlCarriesTheFinalLayout` | `core/tests/markdown_style.rs::styled_html_carries_the_final_layout` | verified (core) |
| `tst_palette::overridesLightHostInEveryState` | | todo |
| `tst_reading_theme::bodySizeAndMeasureStayInReadingRange` | `core/tests/reading_theme.rs::body_size_and_measure_stay_in_reading_range` | verified |
| `tst_reading_theme::bodyTextMeetsAaaOnEverySurface` | `core/tests/reading_theme.rs::body_text_meets_aaa_on_every_surface` | verified |
| `tst_reading_theme::chromeRolesStayReadableOnEveryTheme` | `core/tests/reading_theme.rs::chrome_roles_stay_readable_on_every_theme` | verified |
| `tst_reading_theme::fontFallsBackToTheNextInstalledFamily` | `core/tests/reading_theme.rs::font_falls_back_to_the_next_installed_family` | verified |
| `tst_reading_theme::idsAreUniqueAndUnknownFallsBackToTerminal` | `core/tests/reading_theme.rs::ids_are_unique_and_unknown_falls_back_to_terminal` | verified |
| `tst_reading_theme::noPurePolarityExtremes` | `core/tests/reading_theme.rs::no_pure_polarity_extremes` | verified |
| `tst_reading_theme::optionsMirrorThemes` | `core/tests/reading_theme.rs::options_mirror_themes` | verified |
| `tst_reading_theme::secondaryTextMeetsAa` | `core/tests/reading_theme.rs::secondary_text_meets_aa` | verified |
| `tst_stall_monitor::blockedGuiThreadIsLoggedWithItsStack` | `app/tests/qml/diagnostics_probe.qml` | verified |
| `tst_stall_monitor::disabledMonitorDoesNothing` | `app/src/bridge/diagnostics.rs` (threshold 0 starts no watchdog; screenshot runs default to 0) | checked by review |
| `tst_stall_monitor::memoryThresholdCapturesTheGuiStack` | `core/src/diagnostics.rs::memory_captures_at_the_mark_and_every_512_mb_more` + `diagnostics_probe.qml` | verified |
| `tst_stall_monitor::postedBlockDurationsUseWakeTime` | `core/src/diagnostics.rs::posted_block_durations_use_wake_time` + `diagnostics_probe.qml` short blocks | verified |
| `tst_stall_monitor::shortPausesAreNotStalls` | `core/src/diagnostics.rs::short_pauses_are_not_stalls` + `diagnostics_probe.qml` | verified |
| `tst_transcript_layout::anchorSurvivesChangesAboveViewport` | `core/tests/transcript_layout.rs::anchor_survives_changes_above_viewport` | verified (core) |
| `tst_transcript_layout::createdRowsMatchDelegateHeightsAndAreContiguous` | `core/tests/transcript_layout.rs::created_rows_match_delegate_heights_and_are_contiguous` | verified (core) |
| `tst_transcript_layout::followingTracksEndAfterModelAndHeightChanges` | `core/tests/transcript_layout.rs::following_tracks_end_after_model_and_height_changes` | verified (core) |
| `tst_transcript_layout::modelResetKeepsSameMessageId` | `core/tests/transcript_layout.rs::model_reset_keeps_same_message_id` | verified (core) |
| `tst_transcript_layout::positionAtRowStaysAtTopAfterNeighbourMeasurement` | `core/tests/transcript_layout.rs::position_at_row_stays_at_top_after_neighbour_measurement` | verified (core) |
| `tst_transcript_layout::rowsFarFromViewportAreNotCreated` | `core/tests/transcript_layout.rs::rows_far_from_viewport_are_not_created` | verified (core) |
| `tst_transcript_layout::widthChangeKeepsAnchorOnScreen` | `core/tests/transcript_layout.rs::width_change_keeps_anchor_on_screen` | verified (core) |
| `tst_transcript_rows::bigTablesSplitIntoChunksThatRepeatTheHeader` | `core/tests/transcript_rows.rs::big_tables_split_into_chunks_that_repeat_the_header` | verified (core) |
| `tst_transcript_rows::finishingMessageSplitsWithoutResetting` | `core/tests/transcript_rows.rs::finishing_message_splits_without_resetting` | verified (core) |
| `tst_transcript_rows::insertsAndRemovesMapAroundSplitMessages` | `core/tests/transcript_rows.rs::inserts_and_removes_map_around_split_messages` | verified (core) |
| `tst_transcript_rows::longTextSplitsAtBlockBoundaries` | `core/tests/transcript_rows.rs::long_text_splits_at_block_boundaries` | verified (core) |
| `tst_transcript_rows::smallMessagesStayWhole` | `core/tests/transcript_rows.rs::small_messages_stay_whole` | verified (core) |
| `tst_workspace_context::ordinaryDirectoryAndRealGitWorktree` | `core/tests/workspace.rs::ordinary_directory_and_real_git_worktree` | verified |

## Qt model adapters

Core models record ordered ops (`Reset`/`Insert`/`Remove`/`Update`/`Signal`);
each cxx-qt adapter replays them onto a row mirror between the matching
`begin*/end*` calls, so views never see a half-applied change and Qt never
re-enters Rust state mid-mutation. Core tests replay the ops onto a mirror and
assert it equals the model after every step. Qt's row signals are private
(`QPrivateSignal`) and cannot be connected from Rust, so `AgentListModel`
emits a public `structureChanged` after replaying structural ops; the sidebar
proxy subscribes through a per-thread registry of live roster models. `app/tests/run-qml-probes.sh`
drives each adapter through a real ListView offscreen.

## Contract fixtures

`core/tests/contract_fixtures.rs` runs all 24 `contract/fixtures` scenarios
through the Rust sync (`core/src/sync.rs`) and delivery (`core/src/delivery.rs`)
reducers, ported from `static/lib/conversation-sync.js` and `delivery.js`,
mirroring `tests/contract/fixtures.test.js`. Status: verified (24/24; a
mutation check that disabled fetch coalescing and inverted the revision guard
failed 3 fixtures). The three `clients: ["web"]` fixtures run through the
reducer too; they are web UI policy, so the native client does not count them
toward behavior parity. Unknown expectation keys fail the runner.

## Python/QML test harnesses

- `audio_coordination.py` — todo
- `multiple_instances.py` — todo
- `report_no_fetch.py` — todo
- `test_preview_adoption.py` — todo
- `` — todo
