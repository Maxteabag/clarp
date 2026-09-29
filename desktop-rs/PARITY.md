# Rust desktop parity checklist

Status: `todo` / `wip` / `ported` (Rust code exists) / `verified` (Rust test or headless check named).
The C++ `desktop/` tree stays the reference until every row is `verified`.

## Modules

| C++ source | Lines | Rust target | Status |
|---|---|---|---|
| `src/app/AppController` | 4730 | `desktop-rs/src/app/` | todo |
| `src/app/CredentialStore` | 245 | `desktop-rs/src/app/` | todo |
| `src/app/InstanceServer` | 199 | `desktop-rs/src/app/` | todo |
| `src/app/MarkdownStyle` | 250 | `desktop-rs/src/app/` | todo |
| `src/app/PreviewVersions` | 147 | `desktop-rs/src/app/` | todo |
| `src/app/StallMonitor` | 247 | `desktop-rs/src/app/` | todo |
| `src/app/TimeFormat` | 138 | `core/src/time_format.rs` | verified (`conversation.rs::stamps_*`, `compact_*`); en_US formats fixed, QLocale-driven locale pending |
| `src/app/ToolNarrator` | 820 | `desktop-rs/src/app/` | todo |
| `src/app/TranscriptCache` | 123 | `desktop-rs/src/app/` | todo |
| `src/app/WorkspaceContext` | 86 | `desktop-rs/src/app/` | todo |
| `src/main` | 1164 | `desktop-rs/src/main/` | todo |
| `src/main` | 1164 | `desktop-rs/src/main/` | todo |
| `src/media/AudioController` | 859 | `desktop-rs/src/media/` | todo |
| `src/media/AudioCoordinator` | 363 | `desktop-rs/src/media/` | todo |
| `src/media/PortraitImage` | 59 | `desktop-rs/src/media/` | todo |
| `src/media/RecordingSession` | 56 | `desktop-rs/src/media/` | todo |
| `src/media/WavEncoder` | 58 | `desktop-rs/src/media/` | todo |
| `src/models/AgentFilterModel` | 368 | `desktop-rs/src/models/` | todo |
| `src/models/AgentListModel` | 708 | `core/src/roster.rs` + `app/src/bridge/agent_list_model.rs` | verified (`core/tests/roster.rs`, `app/tests/qml/agent_list_model_probe.qml`); name order is case-insensitive, not ICU collation |
| `src/models/BackgroundJobTracker` | 159 | `core/src/jobs.rs` | verified (`roster.rs::background_job_tracker_keeps_only_active_jobs`) |
| `src/models/ContactListModel` | 139 | `desktop-rs/src/models/` | todo |
| `src/models/ConversationModel` | 830 | `core/src/conversation.rs` + `app/src/bridge/conversation_model.rs` | verified (`core/tests/conversation.rs`, `app/tests/qml/conversation_model_probe.qml`) |
| `src/models/ConversationPresentationModel` | 437 | `desktop-rs/src/models/` | todo |
| `src/models/PaneTreeModel` | 1041 | `desktop-rs/src/models/` | todo |
| `src/models/VoiceListModel` | 113 | `desktop-rs/src/models/` | todo |
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
| `src/app/PreviewRelaunch.h` (header-only) | 15 | `desktop-rs/src/app/` | todo |
| `src/app/ReadingTheme.h` (header-only) | 282 | `desktop-rs/src/app/` | todo |
| `src/app/ReadyReplySmokeCheck.h` (header-only) | 85 | `desktop-rs/src/app/` | todo |
| `src/app/StartupTrace.h` (header-only) | 52 | `desktop-rs/src/app/` | todo |
| `src/app/TranscriptScrollSmokeCheck.h` (header-only) | 440 | `desktop-rs/src/app/` | todo |
| `src/app/VoiceViewportSmokeCheck.h` (header-only) | 153 | `desktop-rs/src/app/` | todo |
| `src/models/TreeOrder.h` (header-only) | 79 | `core/src/tree.rs` | verified (`roster.rs::tree_order_matches_the_team_walk`) |

## Behaviors (REWRITE_PLAN.md: Existing behavior to preserve)

| Behavior | Rust test / check | Status |
|---|---|---|
| Discover and render the live agent roster from `/agents/snapshot`. | | todo |
| Select and follow an agent, including server-wide focus updates. | | todo |
| Load tail history, paginate older messages, and apply revision deltas. | | todo |
| Replace a conversation when its `conversation_id` changes or the server returns `replace_required`. | | todo |
| Merge growing messages by id and revision without duplicating turns. | | todo |
| Open one SSE stream, resume with `Last-Event-ID`, reconnect on silence or failure, and ignore unknown additive fields/events. | | wip: client verified in `net/tests/clients.rs` (resume, watchdog, backoff, stop) and `contract_fixtures.rs` (unknown events/fields); app wiring pending |
| Send idempotent messages with an optimistic `u-<client_msg_id>` row and keep delivery pending until that id appears in `/log`. | | todo |
| Stop a running turn and represent queued, waiting, interrupted, and active states accurately. | | todo |
| Record microphone PCM, upload it to `/transcribe`, and carry the returned trace/transcription ids into `/send`. | | todo |
| Play announced clips, select sources by protocol precedence, and acknowledge queued/start/success/failure states. | | todo |
| Treat `user-notification` as the only unread/desktop-notification decision. | | todo |
| Create/relaunch/fork/release agents and expose the desktop overview, voice-selection, and orchestrator settings workflows. | | todo |
| Preserve the desktop pane workspace, collapsible agent rail, keyboard-driven navigation, quick switcher, tool visibility, and scroll-to-latest behavior. | | todo |

## C++ test cases to port

| C++ test | Rust test | Status |
|---|---|---|
| `tst_native_core::spawnedLifecycleNeverBecomesTranscriptTool` | `core/tests/conversation.rs::spawned_lifecycle_never_becomes_transcript_tool` | verified |
| `tst_native_core::voiceErrorsStayInTheirSession` | | todo |
| `tst_native_core::sidebarPreviewIsPlainText` | `core/tests/protocol.rs::sidebar_preview_is_plain_text` | verified |
| `tst_native_core::clipboardImageBecomesAttachmentWithoutSending` | | todo |
| `tst_native_core::relaunchPreservesHostSessionAndDraft` | | todo |
| `tst_native_core::previewRestartCapturesContextAndRejectsBusy` | | todo |
| `tst_native_core::restoredSessionDoesNotFallBackToAnotherAgent` | | todo |
| `tst_native_core::localReportsRequireOriginAndSafeReadableFiles` | | todo |
| `tst_native_core::leadingDayTracksVisibleHistory` | | todo |
| `tst_native_core::oldActivityGroupsAreLazyAndVisitScoped` | | todo |
| `tst_native_core::consecutiveExplanationsCollapseWithoutChangingTranscript` | | todo |
| `tst_native_core::streamedTokenAnnouncesOnlyItsRows` | | todo |
| `tst_native_core::spokenMarkupLeavesNoGapsInTheText` | `core/tests/protocol.rs::spoken_markup_leaves_no_gaps_in_the_text` | verified |
| `tst_native_core::quickSwitcherPutsTheExactNameFirst` | | todo |
| `tst_native_core::unreachableHostErrorClearsWhenItIsBack` | | todo |
| `tst_native_core::attachedToolElapsedUsesAssistantBoundaryAndPreservesSender` | | todo |
| `tst_native_core::logMergeRefreshesExplanationsOncePerBatch` | | todo |
| `tst_native_core::secondLaunchIsForwardedToTheRunningInstance` | | todo |
| `tst_native_core::readyModePreservesActivityAndHidesOnlyProvisionalBody` | | todo |
| `tst_native_core::idleContactStartsFreshWithSavedDefaults` | | todo |
| `tst_native_core::idleContactUsesDialogLaunchValues` | | todo |
| `tst_native_core::hostDefaultDirectoryReplacesHomePlaceholder` | | todo |
| `tst_native_core::contactCreateShowsHostMessageWithoutHttpSuffix` | | todo |
| `tst_native_core::newAgentWaitsForOwnRosterAndRejectsLateSnapshots` | | todo |
| `tst_native_core::fastLaunchOpensWithoutWaitingForFleet` | | todo |
| `tst_native_core::resumeLaunchOpensExactSessionWithoutFleet` | | todo |
| `tst_native_core::launchPoolCarriesBackendModelAndHandlesEmpty` | | todo |
| `tst_native_core::redesignedRosterFiltersWithoutMutatingSource` | | todo |
| `tst_native_core::agentRowParsesBackgroundAndHelperFieldsSafely` | `protocol.rs::agent_row_parses_background_and_helper_fields_safely` + `conversation.rs::compact_durations_read_like_the_cpp_client` | verified |
| `tst_native_core::backgroundJobTrackerKeepsOnlyActiveJobs` | `core/tests/roster.rs::background_job_tracker_keeps_only_active_jobs` | verified |
| `tst_native_core::rosterPrefersLiveJobCountsAndCountsHelpers` | `core/tests/roster.rs::roster_prefers_live_job_counts_and_counts_helpers` | verified |
| `tst_native_core::treeOrderMatchesTheTeamWalk` | `core/tests/roster.rs::tree_order_matches_the_team_walk` | verified |
| `tst_native_core::sidebarNestsHelpersAndCollapsesFinishedOnes` | | todo |
| `tst_native_core::sidebarUpdatesNeverResetTheRows` | | todo |
| `tst_native_core::sidebarStatusUpdatesDoNotRebuildTree` | | todo |
| `tst_native_core::subagentCellsDescribePhaseNameAndTask` | `protocol.rs::subagent_cells_describe_phase_name_and_task` + `conversation.rs::subagent_cells_are_annotated_on_the_way_out_only` | verified |
| `tst_native_core::controllerTracksJobsFromListAndEvents` | | todo |
| `tst_native_core::rosterLookupIsConsistentDuringStructuralSignals` | | todo |
| `tst_native_core::circularPortraitsAreBoundedAndAntialiased` | | todo |
| `tst_native_core::agentTerminalLaunchesNativeCliThroughDefaultTerminal` | | todo |
| `tst_native_core::sseParserHandlesChunksCommentsAndReplayIds` | `core/tests/protocol.rs::sse_parser_handles_chunks_comments_and_replay_ids` | verified |
| `tst_native_core::nextAttentionCyclesWaitingUnreadAndPending` | `core/tests/roster.rs::next_attention_cycles_waiting_unread_and_pending` | verified |
| `tst_native_core::readyPresentationRetainsCanonicalStreamAndRevealsFinal` | | todo |
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
| `tst_native_core::emptyStartupWaitsForExplicitChoiceAndRetryTargetsLatestFailure` | | todo |
| `tst_native_core::conversationChangeRequestsReplacement` | `core/tests/conversation.rs::conversation_change_requests_replacement` | verified |
| `tst_native_core::clipSourcePrecedenceMatchesContract` | `core/tests/protocol.rs::clip_source_precedence_matches_contract` | verified |
| `tst_native_core::wavEncodingProducesAValidPcmHeader` | | todo |
| `tst_native_core::paneTreeSplitsClosesNavigatesAndZooms` | | todo |
| `tst_native_core::paneWorkspacePersistenceIsAsyncAndConflictSafe` | | todo |
| `tst_native_core::apiClientRejectsCrossOriginAuthenticatedMedia` | `net/tests/clients.rs::api_client_rejects_cross_origin_authenticated_media` | verified |
| `tst_native_core::apiClientDropsRepliesFromPreviousEndpointGeneration` | `net/tests/clients.rs::api_client_drops_replies_from_previous_endpoint_generation` | verified |
| `tst_native_core::paneDraftAndFocusSurviveLayoutStateChanges` | | todo |
| `tst_native_core::paneActivationAlwaysTargetsItsComposer` | | todo |
| `tst_native_core::paneDraftIsDurableAndScopedToServerAndConversation` | | todo |
| `tst_native_core::transcriptCacheRestoresDurableRowsWithoutStaleRegression` | | todo |
| `tst_native_core::credentialStoreRoundTrip` | | todo |
| `tst_native_core::appControllerCompletesCoreProtocolFlow` | | todo |
| `tst_native_core::connectedControllerShutsDownWithoutLateSseCallbacks` | | todo |
| `tst_native_core::contactsExcludeActivePersonas` | | todo |
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
| `tst_tool_narrator::viewportOwnersShareAndReleaseQueuedActivity` | | todo |
| `tst_tool_narrator::sharedHostPollsWithoutStartingLocalCodex` | | todo |
| `tst_tool_narrator::optInDeduplicatesBatchesAndPreservesCache` | | todo |
| `tst_tool_narrator::disableCancelsAndRejectsLateReplies` | | todo |
| `tst_tool_narrator::failureFallsBackWithoutRetryStorm_data` | | todo |
| `tst_tool_narrator::failureFallsBackWithoutRetryStorm` | | todo |
| `tst_tool_narrator::scriptContextIsOptInBoundedAndInvalidatesCache` | | todo |
| `tst_tool_narrator::memoizedLookupsFollowEveryFieldThatIsSent` | | todo |
| `tst_tool_narrator::detailLevelsChangeInstructionsAndDiscardPreviousTranslations` | | todo |

## Qt model adapters

Core models record ordered ops (`Reset`/`Insert`/`Remove`/`Update`/`Signal`);
each cxx-qt adapter replays them onto a row mirror between the matching
`begin*/end*` calls, so views never see a half-applied change and Qt never
re-enters Rust state mid-mutation. Core tests replay the ops onto a mirror and
assert it equals the model after every step. `app/tests/run-qml-probes.sh`
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
