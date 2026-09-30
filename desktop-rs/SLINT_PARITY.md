# Slint parity

The whole desktop UI rebuilt in Slint (no Qt, no QML), goal `slint-ui`.
Each QML file maps to its Slint implementation; a row is **done** only with
the Slint code and a check (headless test or probe) named here.

## Architecture decision

The QML app's logic lives in the cxx-qt bridge (`app/src/bridge/controller.rs`
and models), which is Qt-bound. The Slint app gets a new UI-neutral crate,
`engine/`, ported from that bridge: plain Rust state, commands and change
notifications on top of `clarp-core`/`clarp-net`, with no toolkit types. The
Qt bridge stays as it is (PR #201 must not break); moving it onto `engine/`
later removes the duplication. The Slint app (`slint-app/`) is a thin view
over `engine/`, so its behaviour tests run against the engine without a UI.

## QML components (62 files, 11688 lines)

| QML file | Lines | Slint target | Status |
|---|---|---|---|
| `qml/Main.qml` | 874 | `slint-app/ui/app.slint` + `slint-app/src/main.rs` | wip: window, header with connection state, error banner, sidebar/transcript/composer layout on `clarp-engine`; e2e `slint-app/tests/run-e2e.sh` (roster, send, streaming, second chat) passes |
| `qml/PreviewVersionWindow.qml` | 42 | preview versions window | todo |
| `qml/components/ActivityExplanation.qml` | 134 | ui/transcript.slint | todo |
| `qml/components/ActivitySweep.qml` | 15 | ui/sidebar.slint | todo |
| `qml/components/AgentAvatar.qml` | 81 | ui/sidebar.slint | todo |
| `qml/components/AgentOverview.qml` | 499 | ui/panels/agentoverview.slint | todo |
| `qml/components/AgentProfilePanel.qml` | 653 | ui/panels/agentprofilepanel.slint | todo |
| `qml/components/AgentRail.qml` | 52 | ui/sidebar.slint | todo |
| `qml/components/ArtifactSummaryCard.qml` | 70 | `slint-app/ui/updates.slint` `ArtifactCard` + `slint-app/src/updates_view.rs` `artifact_view` (kind, outcome/failed border, title, summary, plan/workflow progress, countdown, Read, Open chat, form notice) | verified: `check.sh updates` (`docs/checks/updates-02-panel.png`); the profile panel's use arrives with that panel |
| `qml/components/AssignAgentDialog.qml` | 146 | `slint-app/ui/agent_dialogs.slint` (`AssignAgentDialog`) + `slint-app/src/agent_dialogs_view.rs` | verified: `slint-app/tests/check.sh agent-dialogs` (Ctrl+A, Up/Down choice with wrap, choose a contact, a refused new name keeps the Host's reason, Ctrl+Shift+A assigns at once, Escape; `docs/checks/agent-dialogs-02-assign.png`, `agent-dialogs-03-assign-refused.png`); also opens on the engine's `AssignmentRequested`. Radio rows are drawn (Slint has no radio button); the contact list is a ComboBox reached by mouse or Tab |
| `qml/components/AttachFileDialog.qml` | 12 | `slint-app/src/main.rs` `choose_attachment` (desktop portal file chooser via `rfd`, off the UI thread) | wip: the chooser itself is not opened in checks (`CLARP_TEST_ATTACH_FILE` stands in); drag-and-drop pending (Slint has no file drop; winit 0.30 has none on Wayland) |
| `qml/components/AvatarActivity.qml` | 38 | ui/sidebar.slint | todo |
| `qml/components/ChatList.qml` | 456 | `slint-app/ui/sidebar.slint` (search, All/Unread, pair rooms and archive rows, nesting via `clarp_core::sidebar`) | wip: in the e2e; keyboard navigation, hide/collapse, restore from archive and a search/scope check pending |
| `qml/components/ChatRow.qml` | 301 | `slint-app/ui/sidebar.slint` `Row` (name, stamp, preview, activity line, queue, muted, unread, busy ring, helper depth, process indicator) | wip: process indicator verified by `check.sh updates` (`docs/checks/updates-01-processes.png`); portraits, done-helpers lines and the collapsed rail's indicator pending |
| `qml/components/Composer.qml` | 461 | `slint-app/ui/composer.slint` + `engine/src/composer.rs` (per-chat drafts flushed after 1 s idle and on close, attachments with upload/shared-filesystem, chips with thumbnails, queue and quota notices, Enter/Shift+Enter/Ctrl+Enter/Ctrl+End/Escape/Ctrl+Shift+O, send/stop) | verified: `engine/tests/host_flow.rs::drafts_and_attachments_belong_to_the_chat_and_survive_a_restart`, `slint-app/tests/check.sh composer` (`docs/checks/composer-01-notices.png`); clipboard image paste, transcription line and Silence button arrive with the platform/audio step; starting-contact line with the start-agent dialog |
| `qml/components/ConnectionPage.qml` | 151 | ui/panels/connectionpage.slint | todo |
| `qml/components/ConversationPane.qml` | 575 | `slint-app/ui/workspace.slint` `Pane` (header, transcript, composer per pane; pair rooms read-only) | verified: `slint-app/tests/check.sh panes` (`docs/checks/panes-01-split.png`), `engine/tests/host_flow.rs::panes_follow_the_selection_and_their_layout_is_restored`; typing indicator and pair participants pending |
| `qml/components/DeferredPanel.qml` | 74 | ui/workspace.slint | todo |
| `qml/components/DisplayCellCard.qml` | 200 | ui/transcript.slint | todo |
| `qml/components/HeaderContext.qml` | 115 | `slint-app/ui/workspace.slint` `Header` (name, workspace kind icon and label via `clarp_core::workspace`, model · effort, working) | wip: in `panes-01-split.png`; runtime popover pending |
| `qml/components/KeyboardMap.qml` | 146 | `slint-app/src/keymap.rs` (same states, parents, guards, overrides, import validation) + `slint-app/src/commands.rs` (Main.qml `runCommand`) with one root key handler in `app.slint` | verified: `keymap::tests` (4) and `check.sh panes` (Ctrl+Alt+V/Left/Z/X, Escape, E, J, Enter, Ctrl+Shift+K typed through the map); actions of unported dialogs fall through to the focused control until their step |
| `qml/components/KeymapEditor.qml` | 32 | `slint-app/ui/dialogs.slint` `KeymapEditor` on the shared `Modal` (Escape / click outside close) + `commands.rs` (bindings in settings `keymap/bindings`, validated by `keymap::import`) | verified: `slint-app/tests/check.sh keymap` (`docs/checks/keymap-01.png`) |
| `qml/components/LaunchDirectoryPicker.qml` | 113 | `slint-app/ui/launch.slint` (`DirectoryPicker`, inside the hub) + `slint-app/src/launch_view.rs` | verified: `slint-app/tests/check.sh launch` (Ctrl+Alt+D, typed query against the Host's launch directories, Enter chooses; `docs/checks/launch-03-directory.png`). Same rules as QML: "~" for an empty query, 80 ms query delay, Enter while loading chooses once the Host answers, Tab completes |
| `qml/components/MediaGallery.qml` | 163 | ui/panels/mediagallery.slint | todo |
| `qml/components/MessageDelegate.qml` | 538 | `slint-app/ui/transcript.slint` `Block`/message rows + `engine/src/blocks.rs` (headings, prose/lists via StyledText, code, quotes, tables, rules; `blocks::tests`), timestamps, agent sender labels, pending/not-delivered, activity toggle | wip: rendered in both themes (`slint-app/docs/screens/`) and checked by `slint-app/tests/check.sh transcript`; text selection (Slint's StyledText has none) and media pending |
| `qml/components/NavRail.qml` | 145 | `slint-app/ui/navrail.slint` (same SVG icons, attention badge from `engine.attention_count()`, mute, settings, Host initial); chats/updates/teams/settings switch through `commands::run` | wip: `check.sh updates` (badge counts 2 then 1, rail opens the Updates), `check.sh settings`; overview pending |
| `qml/components/NewSessionHub.qml` | 458 | `slint-app/ui/launch.slint` (`NewSessionHub`, `LaunchHub` global) + `slint-app/src/launch_view.rs` | verified: `launch_view::tests` (2), `slint-app/tests/check.sh launch` (Ctrl+N and the sidebar's +, the "launch" keyboard state, Ctrl+Right provider, model/effort editor, contact search, Enter quick-start, Ctrl+Alt+D folder, New contact creation, the empty-pool fallback to New contact, Escape stepping back, click outside, "Connect to a server first" offline; `docs/checks/launch-01-hub.png` … `launch-06-offline.png`). Changes: cards show the initial, not the persona portrait (no AgentAvatar yet); the model menus list "Server default" once (QML also showed the catalog's "Provider default"); `quick-new-agent` opens the hub as in QML. Left: the command-line launch (`--backend …`) is not parsed by `main.rs` yet; `launch_view::open_launch` implements it (the check drives it) |
| `qml/components/OrchestratorDialog.qml` | 193 | ui/panels/orchestratordialog.slint | todo |
| `qml/components/PairRow.qml` | 117 | `slint-app/ui/sidebar.slint` room rows + `engine` rooms (unread from seen revisions; `engine/tests/host_flow.rs::pair_rooms_load_unread_and_are_read_once_opened`) | wip: participants' portraits pending |
| `qml/components/PaneLeaf.qml` | 69 | `slint-app/ui/workspace.slint` `Pane` (active highlight, click to activate) | verified: `slint-app/tests/check.sh panes` (`docs/checks/panes-01-split.png`), `engine/tests/host_flow.rs::panes_follow_the_selection_and_their_layout_is_restored` |
| `qml/components/PreviewVersionPanel.qml` | 114 | ui/panels/previewversionpanel.slint | todo |
| `qml/components/ProcessGlyph.qml` | 118 | `slint-app/ui/processes.slint` `ProcessGlyph` (hourglass / agent as SVGs under `ui/icons/`, coloured by the theme's link colour) | verified: `check.sh updates` (`docs/checks/updates-01-processes.png`); the running sub-agent's hop animation waits for the motion clock (reduced motion) port |
| `qml/components/ProcessIndicator.qml` | 85 | `slint-app/ui/processes.slint` `ProcessIndicator` (glyph kind, count badge, summary as accessible label) in the sidebar row; counts from the roster rows (`Change::Roster` after `Change::Processes`) | verified: `check.sh updates` (the job's hourglass shows on Rachel's row; clicking it opens the popover); the pane header's indicator arrives with the header work; no hover tooltip (Slint has none) |
| `qml/components/ProcessPopover.qml` | 236 | `slint-app/ui/processes.slint` `ProcessPopover` + `slint-app/src/updates_view.rs` (from `engine.agent_processes(session)`, refreshed on `Change::Processes`/`Roster` and every second while open; right-aligned under the indicator; helper rows open their chat) | verified: `check.sh updates` (job row with kind, detail, elapsed, heartbeat; count; Escape and click outside close it; `docs/checks/updates-01-processes.png`) |
| `qml/components/QueueDialog.qml` | 167 | `slint-app/ui/agent_dialogs.slint` (`QueueDialog`) + `slint-app/src/agent_dialogs_view.rs` | verified: `slint-app/tests/check.sh agent-dialogs` (opened from the composer's queue line, edit, send now, delete, Escape, click outside; `docs/checks/agent-dialogs-04-queue.png`). Save / Send now / Delete / Reload / Close are icons. Change: the composer's queue line opens it (QML opened it from the pane menu and the profile, which are not ported yet) |
| `qml/components/QuickSwitcher.qml` | 440 | `slint-app/ui/switcher.slint` + `slint-app/src/switcher.rs` (commands, settings toggles incl. reading themes and tool activity, agents ranked by `switcher_rank`, selection kept by identity) + `commands.rs` | verified: `switcher::tests` (2), `slint-app/tests/check.sh switcher` (Ctrl+K, ranking, Up/Down/Enter, a command, a setting, Escape, click outside; `docs/checks/switcher-01-search.png`); idle contacts are rows (after agents with a query), and "Start an idle contact" (Ctrl+Alt+N, `new-contact`) lists only them: verified by `check.sh launch` (`docs/checks/launch-07-contacts.png`); tool-detail levels arrive with the narrator |
| `qml/components/RenameAgentDialog.qml` | 99 | `slint-app/ui/agent_dialogs.slint` (`RenameAgentDialog`) + `slint-app/src/agent_dialogs_view.rs` | verified: `slint-app/tests/check.sh agent-dialogs` (F2, the name selected, Enter renames and closes on the mutation, Escape, click outside; `docs/checks/agent-dialogs-01-rename.png`). The same check covers `release-agent` (Ctrl+Shift+R), `retry-message` (Ctrl+Alt+R after a failed send) and `agent-terminal` (Ctrl+Alt+T, recorded through `CLARP_TEST_TERMINAL_LOG`) |
| `qml/components/ReportView.qml` | 154 | `slint-app/ui/report.slint` `ReportView` on the shared `Modal` + `slint-app/src/updates_view.rs` (`engine.report_for_artifact`; Markdown through the transcript's `Block`s; sanitized HTML flattened to Markdown by `html_markdown`) | verified: `check.sh updates` (Markdown and HTML reports, Escape and click outside close; `docs/checks/updates-03-report.png`), `updates_view::tests`; a card over the window instead of a full-window page so a click outside can close it; HTML is shown as its text, headings, lists and links (no rich text in Slint); no text selection |
| `qml/components/SettingsPanel.qml` | 625 | `slint-app/ui/settings.slint` + `slint-app/src/settings_view.rs` (all sections; Host status and voice routing via `engine/src/host_status.rs`; voice routing as provider/fallback choices instead of a dialog) | verified: `slint-app/tests/check.sh settings` (`docs/checks/settings-01.png`); tool-detail slider waits for the narrator port |
| `qml/components/ShortcutBar.qml` | 50 | `slint-app/ui/app.slint` `ShortcutBar` (mode, hints from `keymap::hints`, connection state) | verified: `check.sh panes` (INSERT/CONVERSATION/AGENTS, hide) |
| `qml/components/StartAgentDialog.qml` | 368 | `slint-app/ui/launch.slint` (`StartAgentDialog`, `StartAgent` global) + `slint-app/src/launch_view.rs` | wip, verified: `slint-app/tests/check.sh launch` (typed-path suggestions and favourite folders load, Enter starts the named agent; `docs/checks/launch-08-start.png`). Main.qml opens it only from AgentOverview (Start, Relaunch) and the profile (Relaunch), which are not ported: they call `launch_view::open_start_agent(app, window, replace_session, name)`. Change: Enter in the name or workspace field starts. Left: resume/fork past sessions are built but not driven by a check |
| `qml/components/StatusPill.qml` | 49 | ui/sidebar.slint | todo (not used by the Updates, Teams or process views; arrives with the switcher, profile, overview and connection page) |
| `qml/components/TeamsPanel.qml` | 564 | `slint-app/ui/teams.slint` (`Teams`, `TeamCreateDialog`, `TeamEditDialog`, `TeamMemberDialog`, `TeamDeleteDialog` on `Modal`) + `slint-app/src/teams_view.rs` (grouped/flat order, colours, leader choices from members, 10 s reload of the selected team) | verified: `check.sh teams` (Ctrl+3, first team, grouping, create by Enter, select, edit name/colour, add and remove a member, nudging, delete dialog closed by Escape and click outside then confirmed, Escape back to chats; `docs/checks/teams-01-panel.png` … `teams-04-delete.png`), `teams_view::tests`; the "···" menu became edit / nudge / delete icon buttons in the team header; no resizable split between list and messages; messages are not selectable |
| `qml/components/Theme.qml` | 47 | `slint-app/ui/theme.slint` `Palette` set from `clarp_core::reading_theme`; std widgets follow light/dark | verified: `engine/tests/host_flow.rs::preferences_are_remembered` + terminal and paper renders; reading font resolved from the theme's families (`fc-list`, off the UI thread) for the transcript and composer only, as in Qt |
| `qml/components/ThemedComboBox.qml` | 31 | ui/widgets.slint (shared controls, theme global) | todo |
| `qml/components/ToolCard.qml` | 150 | `slint-app/ui/transcript.slint` `ToolCard` (status, name, summary, Details for command/input/result); groups via `clarp_core::presentation`, missing calls from `/message-tool-details` (`Engine::load_tool_details`) | verified: `check.sh transcript` (fold, open, fetch, card contents, fold again; `docs/checks/transcript-01-tools.png`); display cells pending |
| `qml/components/TranscriptList.qml` | 478 | ui/transcript.slint | todo |
| `qml/components/TranscriptView.qml` | 177 | `slint-app/ui/transcript.slint` `Transcript` (virtualised `ListView`, follow/pause, Up/Down/Page/Home/End after Escape) | verified: `check.sh transcript` (opens at latest, Page Up pauses, new rows keep the reader's place, End resumes, following tracks new rows); reaching the top loads the page before with the reader's place held (bottom-anchored), rows updated in place by id (`sync_rows`) so streaming never rebuilds the list |
| `qml/components/TuiBusyIndicator.qml` | 25 | ui/widgets.slint (shared controls, theme global) | todo |
| `qml/components/TuiButton.qml` | 14 | ui/widgets.slint (shared controls, theme global) | todo |
| `qml/components/TuiCheckBox.qml` | 20 | ui/widgets.slint (shared controls, theme global) | todo |
| `qml/components/TuiLabel.qml` | 3 | ui/widgets.slint (shared controls, theme global) | todo |
| `qml/components/TuiRadioButton.qml` | 20 | ui/widgets.slint (shared controls, theme global) | todo |
| `qml/components/TuiSwitch.qml` | 20 | ui/widgets.slint (shared controls, theme global) | todo |
| `qml/components/TuiText.qml` | 2 | ui/widgets.slint (shared controls, theme global) | todo |
| `qml/components/TuiTextArea.qml` | 14 | ui/widgets.slint (shared controls, theme global) | todo |
| `qml/components/TuiTextField.qml` | 14 | ui/widgets.slint (shared controls, theme global) | todo |
| `qml/components/TuiToolButton.qml` | 14 | ui/widgets.slint (shared controls, theme global) | todo |
| `qml/components/TypingIndicator.qml` | 42 | ui/transcript.slint | todo |
| `qml/components/UpdatesPanel.qml` | 331 | `slint-app/ui/updates.slint` `Updates` + `slint-app/src/updates_view.rs` (attention decisions with their choices, background jobs with progress and cancel, artifacts, loading and error, empty state, 10 s reload while shown) + `commands.rs` (`updates`, `next-attention` Ctrl+J / N with the attention guard live, Ctrl+R) | verified: `check.sh updates` (Ctrl+2, resolve, cancel, Ctrl+R, report open/close, Escape to chats, rail button, Ctrl+J to the next attention chat; `docs/checks/updates-02-panel.png`, `updates-04-next-attention.png`); `engine/tests/panels.rs`; no tooltips on the icon buttons (accessible labels instead) |
| `qml/components/VoiceDialog.qml` | 123 | ui/panels/voicedialog.slint | todo |
| `qml/components/Workspace.qml` | 171 | `slint-app/ui/workspace.slint` `Workspace` (panes from `PaneTree::view_layout`, split handles with drag/double-click balance, workspace tabs, save-conflict warning) + `engine/src/workspace.rs` (persistence off the UI thread, joined on close) + `slint-app/src/panes.rs` | verified: `slint-app/tests/check.sh panes` (`docs/checks/panes-01-split.png`), `engine/tests/host_flow.rs::panes_follow_the_selection_and_their_layout_is_restored` (also split down, resize, new workspace, Ctrl+Alt+W) |

## REWRITE_PLAN behaviours (against the Slint app)

| Behaviour | Slint check | Status |
|---|---|---|
| Discover and render the live agent roster from `/agents/snapshot`. |  | todo |
| Select and follow an agent, including server-wide focus updates. |  | todo |
| Load tail history, paginate older messages, and apply revision deltas. | `engine/tests/host_flow.rs` (tail, delta on send); `slint-app/tests/check.sh transcript` (100-row tail, Home loads 100 older twice, nothing older after the first message, deltas while paused and following) | verified |
| Replace a conversation when its `conversation_id` changes or the server returns `replace_required`. |  | todo |
| Merge growing messages by id and revision without duplicating turns. |  | todo |
| Open one SSE stream, resume with `Last-Event-ID`, reconnect on silence or failure, and ignore unknown additive fields/events. |  | todo |
| Send idempotent messages with an optimistic `u-<client_msg_id>` row and keep delivery pending until that id appears in `/log`. | `engine/tests/host_flow.rs::a_send_shows_at_once_and_is_confirmed_by_the_log`; the e2e sends through the Slint composer against tests/qa/host.py; `check.sh composer` checks the `/send` body (text + attachment path, `queue_if_busy`) | verified |
| Stop a running turn and represent queued, waiting, interrupted, and active states accurately. |  | todo |
| Record microphone PCM, upload it to `/transcribe`, and carry the returned trace/transcription ids into `/send`. | `slint-app/tests/check.sh voice` (Ctrl+Shift+Space records the fixture "microphone" `CLARP_AUDIO_INPUT=file:`, WAV to `/transcribe`, `/send` carries `trace_id` and the same `transcription_id`) | verified |
| Play announced clips, select sources by protocol precedence, and acknowledge queued/start/success/failure states. | `slint-app/tests/check.sh voice` (a recoverable clip is queued, downloaded, played on the silent output and acknowledged `queued`, `play-start`, `play-ok` in order); source precedence and failure acks are `clarp_core::audio` unit tests | verified |
| Treat `user-notification` as the only unread/desktop-notification decision. | `slint-app/tests/check.sh desktop` (a `user-notification` for a chat that is not open raises the notification, recorded via `CLARP_TEST_NOTIFY_LOG`); unread from `clarp_core::roster` tests | verified |
| Create/relaunch/fork/release agents and expose the desktop overview, voice-selection, and orchestrator settings workflows. |  | todo |
| Preserve the desktop pane workspace, collapsible agent rail, keyboard-driven navigation, quick switcher, tool visibility, and scroll-to-latest behavior. | `slint-app/tests/check.sh transcript` covers tool visibility and scroll-to-latest | partial: panes, rail collapse, switcher pending |

## End-to-end

`slint-app/tests/run-e2e.sh` runs the Slint app against `tests/qa/host.py` in a
loopback-only network namespace (isolated HOME, system PATH), headless on
Slint's software renderer. The driver (`slint-app/src/driver.rs`) types and
sends through the real composer, sees the reply stream and complete, and opens
a second chat. Screenshots: `slint-app/docs/e2e/`.


## Platform (no QML counterpart)

| Qt app piece | Slint app | Status |
|---|---|---|
| `app/src/bridge/audio_controller.rs` + `audio_output.rs`, `audio_input.rs`, `audio_coordinator.rs` | `slint-app/src/platform/audio.rs` (same logic, notices instead of Qt signals) + the three modules copied unchanged | verified: `check.sh voice`; checks run with `CLARP_AUDIO_OUTPUT=null` and a fixture microphone, never the user's devices |
| `app/src/mpris.rs` | `slint-app/src/platform/mpris.rs` (copied) + `platform::serve_mpris` / `publish_playback` | verified: `check.sh voice` queries Identity and PlaybackStatus on the check's private bus |
| `app/src/bridge/desktop_services.rs` (tray, notifications, presence, logind) | `slint-app/src/platform/desktop.rs` (input from winit window events and the root key handler instead of a Qt event filter) | verified: `check.sh desktop` (notification, presence and activity reports); the tray needs a StatusNotifier host, which the checks' private bus has not |
| `app/src/bridge/instance_server.rs` | `slint-app/src/launch.rs` over `clarp_core::instance` | verified: `CLARP_TEST_INSTANCE=1 check.sh instance` (a second launch hands over in ~4 ms and exits 0) |
| `app/src/bridge/diagnostics.rs` + `stall_monitor.rs` | `slint-app/src/platform/diagnostics.rs` + `stall_monitor.rs` (copied); engine wakes stand in for the dispatcher's `awake` | verified: `CLARP_STALL_THRESHOLD_MS=150 CLARP_STALL_LOG=… check.sh diagnostics` (a 600 ms block is logged with its stack) |
