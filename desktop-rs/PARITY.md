# Rust desktop parity checklist

The C++ test list covers declared and inline-defined test slots plus standalone test programs.

Status: `todo` / `wip` / `ported` (Rust code exists) / `verified` (Rust test or headless check named).
The C++ `desktop/` tree stays the reference until every row is `verified`.

## Modules

| C++ source | Lines | Rust target | Status |
|---|---|---|---|
| `src/app/AppController` | 3771 | `app/src/bridge/controller.rs` | wip: connect/snapshot/selection/log sync/send/stop/SSE, drafts/focus, updates/jobs, launch/lifecycle/catalog, narrator, preferences, teams and turn queue, profile/prompt history, settings status/TTS, voices, orchestrator, composer attachments (upload, shared filesystem, send), past sessions/resume, launch directories, directory suggestions/favorites, contact assignment, pairing and keyring credentials verified in `app/tests/qml/controller*_probe.qml` against `fake_host.py`; directories, credentials, clipboard image paste, media pending |
| `src/app/CredentialStore` | 245 | `net/src/credentials.rs` (zbus) | verified (`net/tests/credentials.rs` via `net/tests/run-keyring-tests.sh`: throwaway gnome-keyring and keyring-less private buses; `app/tests/qml/controller_credentials_probe.qml` pairs, reconnects from the keyring and forgets). Deviation: a failed `/pairing/exchange` returns the page to offline instead of staying in "pairing" |
| `src/app/InstanceServer` | 199 | `desktop-rs/src/app/` | todo |
| `src/app/MarkdownStyle` | 250 | `desktop-rs/src/app/` | todo |
| `src/app/PreviewVersions` | 147 | `core/src/preview.rs` + `app/src/bridge/preview_versions.rs` | verified (core: `core/tests/preview.rs`); the bridge runs the helper off-thread and quits through `QCoreApplication::quit` bound with cxx `#[Self]` |
| `src/app/StallMonitor` | 247 | `desktop-rs/src/app/` | todo |
| `src/app/TimeFormat` | 138 | `core/src/time_format.rs` | verified (`conversation.rs::stamps_*`, `compact_*`); en_US formats fixed, QLocale-driven locale pending |
| `src/app/ToolNarrator` | 802 | `core/src/narrator.rs` + `app/src/bridge/tool_narrator.rs` | verified for the shared-Host path (`core/tests/narrator.rs`, `app/tests/qml/narrator_probe.qml`); the local `codex exec` fallback and script-context evidence are not ported (the app always uses the Host) |
| `src/app/TranscriptCache` | 123 | `desktop-rs/src/app/` | todo |
| `src/app/WorkspaceContext` | 86 | `core/src/workspace.rs` | verified (`core/tests/workspace.rs`, real Git worktree) |
| `src/main` | 1164 | `app/src/main.rs` + `app/build.rs` | wip: Clarp.Desktop module built from `desktop/CMakeLists.txt` (QML + resources via `app/qml`, `app/resources` symlinks; QTP0004 subdirectory qmldirs generated), Basic style, software renderer, own `ClarpRust` settings namespace, Main loaded inside its module with `launchOnStartup`/`sidebarVisible`; instance forwarding, launch options, tray, screenshots pending |
| `src/media/AudioController` | 859 | `desktop-rs/src/media/` | todo |
| `src/media/AudioCoordinator` | 363 | `desktop-rs/src/media/` | todo |
| `src/media/PortraitImage` | 59 | `desktop-rs/src/media/` | todo |
| `src/media/RecordingSession` | 56 | `desktop-rs/src/media/` | todo |
| `src/media/WavEncoder` | 58 | `desktop-rs/src/media/` | todo |
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
| `src/platform/DesktopIntegration` | 97 | `desktop-rs/src/platform/` | todo |
| `src/platform/DesktopPresence` | 224 | `desktop-rs/src/platform/` | todo |
| `src/platform/MprisIntegration` | 252 | `desktop-rs/src/platform/` | todo |
| `src/protocol/ProtocolTypes` | 799 | `core/src/{protocol,text,json}.rs` | verified (`core/tests/protocol.rs`) |
| `src/terminal/TerminalLaunch` | 36 | `desktop-rs/src/terminal/` | todo |
| `src/views/TranscriptLayout` | 890 | `desktop-rs/src/views/` | todo |
| `src/views/TranscriptRows` | 310 | `desktop-rs/src/views/` | todo |
| `src/app/AvatarMotionClock.h` (header-only) | 224 | `desktop-rs/src/app/` | todo |
| `src/app/DesktopPalette.h` (header-only) | 40 | `desktop-rs/src/app/` | todo |
| `src/app/KeyboardSmokeCheck.h` (header-only) | 330 | `desktop-rs/src/app/` | todo |
| `src/app/LocalReport.h` (header-only) | 40 | `desktop-rs/src/app/` | todo |
| `src/app/MemoryDiagnostics.h` (header-only) | 71 | `desktop-rs/src/app/` | todo |
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
| Record microphone PCM, upload it to `/transcribe`, and carry the returned trace/transcription ids into `/send`. | | todo |
| Play announced clips, select sources by protocol precedence, and acknowledge queued/start/success/failure states. | | todo |
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
| `tst_native_core::relaunchPreservesHostSessionAndDraft` | `core/tests/preview.rs::relaunch_preserves_host_and_session` | wip: environment and arguments verified; restoreDesktopSession pending |
| `tst_native_core::previewRestartCapturesContextAndRejectsBusy` | `core/tests/preview.rs::preview_restart_captures_context_and_rejects_busy` | verified |
| `tst_native_core::restoredSessionDoesNotFallBackToAnotherAgent` | | todo |
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
| `tst_native_core::secondLaunchIsForwardedToTheRunningInstance` | | todo |
| `tst_native_core::readyModePreservesActivityAndHidesOnlyProvisionalBody` | `core/tests/presentation.rs::ready_mode_preserves_activity_and_hides_only_provisional_body` | verified (core) |
| `tst_native_core::idleContactStartsFreshWithSavedDefaults` | `app/tests/qml/controller_launch_probe.qml` | verified |
| `tst_native_core::idleContactUsesDialogLaunchValues` | `app/tests/qml/controller_launch_probe.qml` | verified |
| `tst_native_core::hostDefaultDirectoryReplacesHomePlaceholder` | `app/tests/qml/controller_launch_probe.qml` | verified |
| `tst_native_core::contactCreateShowsHostMessageWithoutHttpSuffix` | `app/tests/qml/controller_launch_probe.qml` | verified |
| `tst_native_core::newAgentWaitsForOwnRosterAndRejectsLateSnapshots` | `app/tests/qml/controller_launch_probe.qml` (session-only path, retry without a second create) | wip: a held late snapshot released after creation is not simulated |
| `tst_native_core::fastLaunchOpensWithoutWaitingForFleet` | | todo |
| `tst_native_core::resumeLaunchOpensExactSessionWithoutFleet` | `app/tests/qml/controller_paths_probe.qml` | wip: resume request shape verified; launch mode (no fleet snapshot) lands with main wiring |
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
| `tst_native_core::agentTerminalLaunchesNativeCliThroughDefaultTerminal` | | todo |
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
| `tst_native_core::wavEncodingProducesAValidPcmHeader` | | todo |
| `tst_native_core::paneTreeSplitsClosesNavigatesAndZooms` | `core/tests/panes.rs::pane_tree_splits_closes_navigates_and_zooms` | verified |
| `tst_native_core::paneWorkspacePersistenceIsAsyncAndConflictSafe` | `core/tests/panes.rs::pane_workspace_persistence_is_conflict_safe` + `a_queued_layout_replaces_*` + `pane_tree_model_probe.qml` (worker-thread writes) | verified |
| `tst_native_core::apiClientRejectsCrossOriginAuthenticatedMedia` | `net/tests/clients.rs::api_client_rejects_cross_origin_authenticated_media` | verified |
| `tst_native_core::apiClientDropsRepliesFromPreviousEndpointGeneration` | `net/tests/clients.rs::api_client_drops_replies_from_previous_endpoint_generation` | verified |
| `tst_native_core::paneDraftAndFocusSurviveLayoutStateChanges` | `app/tests/qml/controller_lifecycle_probe.qml` | verified |
| `tst_native_core::paneActivationAlwaysTargetsItsComposer` | `app/tests/qml/controller_lifecycle_probe.qml` (focus follows on the next event-loop turn, not synchronously) | verified |
| `tst_native_core::paneDraftIsDurableAndScopedToServerAndConversation` | `app/tests/qml/controller_lifecycle_probe.qml` + `app/tests/qml/controller_attachments_probe.qml` + `core/tests/settings.rs::draft_keys_are_scoped_to_host_and_session` + `core/tests/attachments.rs` | verified |
| `tst_native_core::transcriptCacheRestoresDurableRowsWithoutStaleRegression` | | todo |
| `tst_native_core::credentialStoreRoundTrip` | `net/tests/credentials.rs::token_round_trips_through_an_isolated_keyring` | verified |
| `tst_native_core::appControllerCompletesCoreProtocolFlow` | `app/tests/qml/controller_probe.qml` | wip: core flow verified; audio/clip parts pending (media step) |
| `tst_native_core::connectedControllerShutsDownWithoutLateSseCallbacks` | `app/tests/qml/controller_lifecycle_probe.qml` | verified |
| `tst_native_core::contactsExcludeActivePersonas` | `core/tests/directory.rs::contacts_exclude_active_personas` | verified |
| `tst_native_core::microphoneCanCaptureNativePcm` | | todo |
| `tst_native_core::backgroundTranscriptionsKeepTheirChatOwnership` | | todo |
| `tst_native_core::clipFailsFastWithoutMediaBackend` | | todo |
| `tst_native_core::narrationClipWithoutMediaBackendStaysBounded` | | todo |
| `tst_native_core::sharedPlaybackDoesNotDuplicateDownloads` | | todo |
| `tst_native_core::markdownParagraphsBecomeVisibleDisplayBlocks` | `core/tests/protocol.rs::markdown_paragraphs_become_visible_display_blocks` | verified |
| `tst_native_core::hugeMarkdownBlocksAreNotRetainedInTheStyleCache` | | todo |
| `tst_native_core::agentReplyKeepsItsAuthorAndNamesTheAnsweredAgent` | | todo |
| `tst_native_core::pairConversationRoomsAreReadOnlyProjections` | | todo |
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
| `tst_avatar_motion::main` (standalone program) | | todo |
| `tst_avatar_render::main` (standalone program) | | todo |
| `tst_desktop_presence::eligibilityExpiresWithoutUserInput` | | todo |
| `tst_desktop_presence::focusLockSleepAndPreferenceReleasePresence` | | todo |
| `tst_desktop_presence::maintenanceActivityDoesNotDependOnPushPreference` | | todo |
| `tst_desktop_presence::unknownSessionNeverSuppresses` | | todo |
| `tst_markdown_style::codeUsesMonoAndBackground` | | todo |
| `tst_markdown_style::headingsScaleWithTheBodyNotTheWeb` | | todo |
| `tst_markdown_style::mixedInlineFormatsInOneBlockStayInRange` | | todo |
| `tst_markdown_style::optionsParseFromQml` | | todo |
| `tst_markdown_style::quotesAndTablesAreStyled` | | todo |
| `tst_markdown_style::secondPassIsANoOp` | | todo |
| `tst_markdown_style::styledHtmlCarriesTheFinalLayout` | | todo |
| `tst_palette::overridesLightHostInEveryState` | | todo |
| `tst_reading_theme::bodySizeAndMeasureStayInReadingRange` | `core/tests/reading_theme.rs::body_size_and_measure_stay_in_reading_range` | verified |
| `tst_reading_theme::bodyTextMeetsAaaOnEverySurface` | `core/tests/reading_theme.rs::body_text_meets_aaa_on_every_surface` | verified |
| `tst_reading_theme::chromeRolesStayReadableOnEveryTheme` | `core/tests/reading_theme.rs::chrome_roles_stay_readable_on_every_theme` | verified |
| `tst_reading_theme::fontFallsBackToTheNextInstalledFamily` | `core/tests/reading_theme.rs::font_falls_back_to_the_next_installed_family` | verified |
| `tst_reading_theme::idsAreUniqueAndUnknownFallsBackToTerminal` | `core/tests/reading_theme.rs::ids_are_unique_and_unknown_falls_back_to_terminal` | verified |
| `tst_reading_theme::noPurePolarityExtremes` | `core/tests/reading_theme.rs::no_pure_polarity_extremes` | verified |
| `tst_reading_theme::optionsMirrorThemes` | `core/tests/reading_theme.rs::options_mirror_themes` | verified |
| `tst_reading_theme::secondaryTextMeetsAa` | `core/tests/reading_theme.rs::secondary_text_meets_aa` | verified |
| `tst_stall_monitor::blockedGuiThreadIsLoggedWithItsStack` | | todo |
| `tst_stall_monitor::disabledMonitorDoesNothing` | | todo |
| `tst_stall_monitor::memoryThresholdCapturesTheGuiStack` | | todo |
| `tst_stall_monitor::postedBlockDurationsUseWakeTime` | | todo |
| `tst_stall_monitor::shortPausesAreNotStalls` | | todo |
| `tst_transcript_layout::anchorSurvivesChangesAboveViewport` | | todo |
| `tst_transcript_layout::createdRowsMatchDelegateHeightsAndAreContiguous` | | todo |
| `tst_transcript_layout::followingTracksEndAfterModelAndHeightChanges` | | todo |
| `tst_transcript_layout::modelResetKeepsSameMessageId` | | todo |
| `tst_transcript_layout::positionAtRowStaysAtTopAfterNeighbourMeasurement` | | todo |
| `tst_transcript_layout::rowsFarFromViewportAreNotCreated` | | todo |
| `tst_transcript_layout::widthChangeKeepsAnchorOnScreen` | | todo |
| `tst_transcript_rows::bigTablesSplitIntoChunksThatRepeatTheHeader` | | todo |
| `tst_transcript_rows::finishingMessageSplitsWithoutResetting` | | todo |
| `tst_transcript_rows::insertsAndRemovesMapAroundSplitMessages` | | todo |
| `tst_transcript_rows::longTextSplitsAtBlockBoundaries` | | todo |
| `tst_transcript_rows::smallMessagesStayWhole` | | todo |
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
