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
| `qml/components/ArtifactSummaryCard.qml` | 70 | ui/transcript.slint | todo |
| `qml/components/AssignAgentDialog.qml` | 146 | ui/panels/assignagentdialog.slint | todo |
| `qml/components/AttachFileDialog.qml` | 12 | `slint-app/src/main.rs` `choose_attachment` (desktop portal file chooser via `rfd`, off the UI thread) | wip: the chooser itself is not opened in checks (`CLARP_TEST_ATTACH_FILE` stands in); drag-and-drop pending (Slint has no file drop; winit 0.30 has none on Wayland) |
| `qml/components/AvatarActivity.qml` | 38 | ui/sidebar.slint | todo |
| `qml/components/ChatList.qml` | 456 | `slint-app/ui/sidebar.slint` (search, All/Unread, pair rooms and archive rows, nesting via `clarp_core::sidebar`) | wip: in the e2e; keyboard navigation, hide/collapse, restore from archive and a search/scope check pending |
| `qml/components/ChatRow.qml` | 301 | `slint-app/ui/sidebar.slint` `Row` (name, stamp, preview, activity line, queue, muted, unread, busy ring, helper depth) | wip: portraits, process glyph, done-helpers lines pending |
| `qml/components/Composer.qml` | 461 | `slint-app/ui/composer.slint` + `engine/src/composer.rs` (per-chat drafts flushed after 1 s idle and on close, attachments with upload/shared-filesystem, chips with thumbnails, queue and quota notices, Enter/Shift+Enter/Ctrl+Enter/Ctrl+End/Escape/Ctrl+Shift+O, send/stop) | verified: `engine/tests/host_flow.rs::drafts_and_attachments_belong_to_the_chat_and_survive_a_restart`, `slint-app/tests/check.sh composer` (`docs/checks/composer-01-notices.png`); clipboard image paste, transcription line and Silence button arrive with the platform/audio step; starting-contact line with the start-agent dialog |
| `qml/components/ConnectionPage.qml` | 151 | ui/panels/connectionpage.slint | todo |
| `qml/components/ConversationPane.qml` | 575 | `slint-app/ui/workspace.slint` `Pane` (header, transcript, composer per pane; pair rooms read-only) | verified: `slint-app/tests/check.sh panes` (`docs/checks/panes-01-split.png`), `engine/tests/host_flow.rs::panes_follow_the_selection_and_their_layout_is_restored`; typing indicator and pair participants pending |
| `qml/components/DeferredPanel.qml` | 74 | ui/workspace.slint | todo |
| `qml/components/DisplayCellCard.qml` | 200 | ui/transcript.slint | todo |
| `qml/components/HeaderContext.qml` | 115 | `slint-app/ui/workspace.slint` `Header` (name, workspace kind icon and label via `clarp_core::workspace`, model · effort, working) | wip: in `panes-01-split.png`; runtime popover pending |
| `qml/components/KeyboardMap.qml` | 146 | `slint-app/src/keymap.rs` (same states, parents, guards, overrides, import validation) + `slint-app/src/commands.rs` (Main.qml `runCommand`) with one root key handler in `app.slint` | verified: `keymap::tests` (4) and `check.sh panes` (Ctrl+Alt+V/Left/Z/X, Escape, E, J, Enter, Ctrl+Shift+K typed through the map); actions of unported dialogs fall through to the focused control until their step |
| `qml/components/KeymapEditor.qml` | 32 | ui/workspace.slint | todo |
| `qml/components/LaunchDirectoryPicker.qml` | 113 | ui/panels/launchdirectorypicker.slint | todo |
| `qml/components/MediaGallery.qml` | 163 | ui/panels/mediagallery.slint | todo |
| `qml/components/MessageDelegate.qml` | 538 | `slint-app/ui/transcript.slint` `Block`/message rows + `engine/src/blocks.rs` (headings, prose/lists via StyledText, code, quotes, tables, rules; `blocks::tests`), timestamps, agent sender labels, pending/not-delivered, activity toggle | wip: rendered in both themes (`slint-app/docs/screens/`) and checked by `slint-app/tests/check.sh transcript`; text selection (Slint's StyledText has none) and media pending |
| `qml/components/NavRail.qml` | 145 | `slint-app/ui/navrail.slint` (same SVG icons, attention badge, mute, settings, Host initial) | wip: surfaces other than chats pending |
| `qml/components/NewSessionHub.qml` | 458 | ui/panels/newsessionhub.slint | todo |
| `qml/components/OrchestratorDialog.qml` | 193 | ui/panels/orchestratordialog.slint | todo |
| `qml/components/PairRow.qml` | 117 | `slint-app/ui/sidebar.slint` room rows + `engine` rooms (unread from seen revisions; `engine/tests/host_flow.rs::pair_rooms_load_unread_and_are_read_once_opened`) | wip: participants' portraits pending |
| `qml/components/PaneLeaf.qml` | 69 | `slint-app/ui/workspace.slint` `Pane` (active highlight, click to activate) | verified: `slint-app/tests/check.sh panes` (`docs/checks/panes-01-split.png`), `engine/tests/host_flow.rs::panes_follow_the_selection_and_their_layout_is_restored` |
| `qml/components/PreviewVersionPanel.qml` | 114 | ui/panels/previewversionpanel.slint | todo |
| `qml/components/ProcessGlyph.qml` | 118 | ui/sidebar.slint | todo |
| `qml/components/ProcessIndicator.qml` | 85 | ui/sidebar.slint | todo |
| `qml/components/ProcessPopover.qml` | 236 | ui/sidebar.slint | todo |
| `qml/components/QueueDialog.qml` | 167 | ui/panels/queuedialog.slint | todo |
| `qml/components/QuickSwitcher.qml` | 440 | ui/switcher.slint (prototype exists) | todo |
| `qml/components/RenameAgentDialog.qml` | 99 | ui/panels/renameagentdialog.slint | todo |
| `qml/components/ReportView.qml` | 154 | ui/panels/reportview.slint | todo |
| `qml/components/SettingsPanel.qml` | 625 | ui/panels/settingspanel.slint | todo |
| `qml/components/ShortcutBar.qml` | 50 | `slint-app/ui/app.slint` `ShortcutBar` (mode, hints from `keymap::hints`, connection state) | verified: `check.sh panes` (INSERT/CONVERSATION/AGENTS, hide) |
| `qml/components/StartAgentDialog.qml` | 368 | ui/panels/startagentdialog.slint | todo |
| `qml/components/StatusPill.qml` | 49 | ui/sidebar.slint | todo |
| `qml/components/TeamsPanel.qml` | 564 | ui/panels/teamspanel.slint | todo |
| `qml/components/Theme.qml` | 47 | `slint-app/ui/theme.slint` `Palette` set from `clarp_core::reading_theme`; std widgets follow light/dark | verified: `engine/tests/host_flow.rs::preferences_are_remembered` + terminal and paper renders |
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
| `qml/components/UpdatesPanel.qml` | 331 | ui/panels/updatespanel.slint | todo |
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
| Record microphone PCM, upload it to `/transcribe`, and carry the returned trace/transcription ids into `/send`. |  | todo |
| Play announced clips, select sources by protocol precedence, and acknowledge queued/start/success/failure states. |  | todo |
| Treat `user-notification` as the only unread/desktop-notification decision. |  | todo |
| Create/relaunch/fork/release agents and expose the desktop overview, voice-selection, and orchestrator settings workflows. |  | todo |
| Preserve the desktop pane workspace, collapsible agent rail, keyboard-driven navigation, quick switcher, tool visibility, and scroll-to-latest behavior. | `slint-app/tests/check.sh transcript` covers tool visibility and scroll-to-latest | partial: panes, rail collapse, switcher pending |

## End-to-end

`slint-app/tests/run-e2e.sh` runs the Slint app against `tests/qa/host.py` in a
loopback-only network namespace (isolated HOME, system PATH), headless on
Slint's software renderer. The driver (`slint-app/src/driver.rs`) types and
sends through the real composer, sees the reply stream and complete, and opens
a second chat. Screenshots: `slint-app/docs/e2e/`.
